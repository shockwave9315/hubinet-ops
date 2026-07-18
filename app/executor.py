from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Any


class ExecutorError(RuntimeError):
    pass


@dataclass(frozen=True)
class Executor:
    config: dict[str, Any]

    def run(self, action: str, vmid: int, argument: str | None = None, timeout: int | None = None) -> dict[str, Any]:
        allowed_actions = {
            "status",
            "inspect",
            "scan",
            "preflight",
            "snapshot",
            "update",
            "healthcheck",
            "repair",
            "rollback",
            "delete-snapshot",
        }
        if action not in allowed_actions:
            raise ExecutorError(f"Action not allowed by agent: {action}")
        if vmid <= 0:
            raise ExecutorError("Invalid VMID")

        host = str(self.config["proxmox_host"])
        user = str(self.config.get("ssh_user", "root"))
        key = str(self.config["ssh_key"])
        known_hosts = str(self.config["known_hosts"])
        connect_timeout = int(self.config.get("connect_timeout_seconds", 10))
        command_timeout = timeout or int(self.config.get("command_timeout_seconds", 3900))

        remote_command = f"{action} {vmid}"
        if argument is not None:
            if not argument.replace("-", "").replace("_", "").isalnum():
                raise ExecutorError("Unsafe command argument")
            remote_command += f" {argument}"

        cmd = [
            "/usr/bin/ssh",
            "-i",
            key,
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={connect_timeout}",
            "-o",
            f"UserKnownHostsFile={known_hosts}",
            "-o",
            "StrictHostKeyChecking=yes",
            f"{user}@{host}",
            remote_command,
        ]

        try:
            completed = subprocess.run(
                cmd,
                text=True,
                capture_output=True,
                timeout=command_timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ExecutorError(f"Executor timeout for {action} on CT{vmid}") from exc

        stdout = completed.stdout.strip()
        stderr = completed.stderr.strip()
        try:
            payload = json.loads(stdout) if stdout else {}
        except json.JSONDecodeError as exc:
            raise ExecutorError(
                f"Host executor returned invalid JSON (rc={completed.returncode}): {stdout[:500]} {stderr[:500]}"
            ) from exc

        if completed.returncode != 0 or not payload.get("ok", False):
            message = payload.get("error") or stderr or f"Command failed with rc={completed.returncode}"
            raise ExecutorError(str(message))
        return payload
