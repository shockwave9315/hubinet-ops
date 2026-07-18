from __future__ import annotations

import json
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable

from .security import sanitize_data, sanitize_text

EventCallback = Callable[[dict[str, Any]], None]
MAX_STDERR = 8_000
MAX_MALFORMED_LINES = 20


class ExecutorError(RuntimeError):
    def __init__(self, message: str, *, data: dict[str, Any] | None = None):
        super().__init__(sanitize_text(message, limit=2000))
        self.data = sanitize_data(data or {})


@dataclass(frozen=True)
class Executor:
    config: dict[str, Any]

    def run(
        self,
        action: str,
        vmid: int,
        argument: str | None = None,
        timeout: int | None = None,
        on_event: EventCallback | None = None,
    ) -> dict[str, Any]:
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
        allowed_vmids = {int(value) for value in self.config.get("allowed_vmids", [])}
        if vmid <= 0 or vmid not in allowed_vmids:
            raise ExecutorError("Invalid or disallowed VMID")

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
        return self._run_process(cmd, action, vmid, command_timeout, on_event)

    def _run_process(
        self,
        cmd: list[str],
        action: str,
        vmid: int,
        timeout: int,
        on_event: EventCallback | None,
    ) -> dict[str, Any]:
        process = subprocess.Popen(
            cmd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=1,
            shell=False,
        )
        stderr_chunks: deque[str] = deque()
        stderr_size = 0

        def drain_stderr() -> None:
            nonlocal stderr_size
            assert process.stderr is not None
            for chunk in iter(lambda: process.stderr.read(1024), ""):
                stderr_chunks.append(chunk)
                stderr_size += len(chunk)
                while stderr_size > MAX_STDERR and stderr_chunks:
                    stderr_size -= len(stderr_chunks.popleft())

        stderr_thread = threading.Thread(target=drain_stderr, name="executor-stderr", daemon=True)
        stderr_thread.start()
        started = time.monotonic()
        timed_out = threading.Event()

        def terminate_on_timeout() -> None:
            timed_out.set()
            process.kill()

        timer = threading.Timer(timeout, terminate_on_timeout)
        timer.daemon = True
        timer.start()
        final: dict[str, Any] | None = None
        malformed: list[str] = []
        last_progress = 0

        assert process.stdout is not None
        try:
            for raw_line in process.stdout:
                if time.monotonic() - started > timeout:
                    process.kill()
                    raise ExecutorError(f"Executor timeout for {action} on CT{vmid}")
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    if len(malformed) < MAX_MALFORMED_LINES:
                        malformed.append(sanitize_text(line, limit=500))
                    continue
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "event":
                    progress = max(last_progress, min(99, int(item.get("progress", last_progress) or 0)))
                    last_progress = progress
                    event = sanitize_data(
                        {
                            "type": "event",
                            "stage": str(item.get("stage", action))[:64],
                            "progress": progress,
                            "level": str(item.get("level", "info"))[:16],
                            "event_type": str(item.get("event_type", "executor_event"))[:64],
                            "message": sanitize_text(item.get("message", ""), limit=1000),
                            "details": item.get("details", {}),
                        }
                    )
                    if on_event is not None:
                        on_event(event)
                    continue
                if item.get("type") == "result":
                    final = {
                        "ok": bool(item.get("ok", False)),
                        "data": sanitize_data(item.get("data", {})),
                    }
                    if item.get("error"):
                        final["error"] = sanitize_text(item["error"], limit=2000)
                    continue
                if "ok" in item:
                    final = sanitize_data(item)
        finally:
            timer.cancel()
            try:
                process.wait(timeout=max(1, timeout - int(time.monotonic() - started)))
            except subprocess.TimeoutExpired as exc:
                process.kill()
                process.wait(timeout=5)
                raise ExecutorError(f"Executor timeout for {action} on CT{vmid}") from exc
            stderr_thread.join(timeout=2)

        if timed_out.is_set():
            raise ExecutorError(f"Executor timeout for {action} on CT{vmid}")

        stderr = sanitize_text("".join(stderr_chunks)[-MAX_STDERR:], limit=MAX_STDERR)
        if final is None:
            detail = f"; malformed output: {malformed[-1]}" if malformed else ""
            raise ExecutorError(
                f"Host executor returned no valid JSON result (rc={process.returncode}){detail}; stderr: {stderr}"
            )
        if process.returncode != 0 or not final.get("ok", False):
            message = final.get("error") or stderr or f"Command failed with rc={process.returncode}"
            raise ExecutorError(str(message), data=final.get("data"))
        return final
