from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

from .executor import EventCallback, Executor, ExecutorError
from .mqtt import VERSION
from .security import sanitize_text

LOGGER = logging.getLogger("hubinet_ops.adapters")

APT_ACTIONS = {
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
    "verify",
    "start",
    "shutdown",
    "reboot",
}
READ_ONLY_ACTIONS = {"status", "inspect"}


class SelfInspector:
    """Bounded local observations for CT110; never invokes SSH or hubinet-maint."""

    def __init__(
        self,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        proc_root: Path = Path("/proc"),
    ) -> None:
        self._runner = runner
        self._proc_root = proc_root

    def inspect(self) -> dict[str, Any]:
        service_status = self._fixed_command(
            ["/usr/bin/systemctl", "is-active", "hubinet-ops.service"],
            timeout=5,
            accepted={0, 3},
        ).strip() or "unknown"
        journal = self._fixed_command(
            [
                "/usr/bin/journalctl",
                "-u",
                "hubinet-ops.service",
                "-p",
                "warning",
                "-n",
                "20",
                "--no-pager",
                "--output=short-iso",
            ],
            timeout=8,
            accepted={0},
        )
        try:
            disk = shutil.disk_usage("/")
            disk_data = {
                "total_bytes": disk.total,
                "used_bytes": disk.used,
                "free_bytes": disk.free,
            }
        except OSError as exc:
            LOGGER.warning("Local disk inspection failed: %s", sanitize_text(exc, limit=300))
            disk_data = {}
        return {
            "health_status": "healthy" if service_status == "active" else "degraded",
            "service_status": service_status,
            "api_health": "ok",
            "agent_version": VERSION,
            "uptime_seconds": self._uptime(),
            "cpu": self._cpu(),
            "memory": self._memory(),
            "disk": disk_data,
            "recent_warnings": self._sanitized_lines(journal),
        }

    def _fixed_command(
        self,
        argv: list[str],
        *,
        timeout: int,
        accepted: set[int],
    ) -> str:
        try:
            completed = self._runner(
                argv,
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
                shell=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            LOGGER.warning(
                "Local self-inspection command failed: %s",
                sanitize_text(exc, limit=300),
            )
            return ""
        if completed.returncode not in accepted:
            LOGGER.warning("Local self-inspection command returned rc=%s", completed.returncode)
            return ""
        return str(completed.stdout or "")[:16_000]

    def _uptime(self) -> int:
        try:
            raw = (self._proc_root / "uptime").read_text(encoding="utf-8").split()[0]
            return max(0, int(float(raw)))
        except (OSError, ValueError, IndexError):
            return 0

    def _memory(self) -> dict[str, int]:
        values: dict[str, int] = {}
        try:
            for line in (self._proc_root / "meminfo").read_text(encoding="utf-8").splitlines():
                key, raw = line.split(":", 1)
                if key in {"MemTotal", "MemAvailable"}:
                    values[key] = int(raw.strip().split()[0]) * 1024
        except (OSError, ValueError, IndexError):
            return {}
        total = values.get("MemTotal", 0)
        available = values.get("MemAvailable", 0)
        return {
            "total_bytes": total,
            "available_bytes": available,
            "used_bytes": max(0, total - available),
        }

    def _cpu(self) -> dict[str, Any]:
        load_1m: float | None = None
        try:
            load_1m = float(
                (self._proc_root / "loadavg").read_text(encoding="utf-8").split()[0]
            )
        except (OSError, ValueError, IndexError):
            pass
        return {"cores": int(os.cpu_count() or 0), "load_1m": load_1m}

    @staticmethod
    def _sanitized_lines(raw: str) -> list[str]:
        lines = []
        for line in raw.splitlines()[-20:]:
            safe = sanitize_text(line, limit=500)
            if safe:
                lines.append(safe)
        return lines


class ResourceExecutor:
    """Selects a fixed executor adapter from validated resource configuration."""

    def __init__(
        self,
        executor: Executor,
        resources: dict[int, dict[str, Any]],
        self_inspector: SelfInspector | None = None,
    ) -> None:
        self._executor = executor
        self._resources = resources
        self._self_inspector = self_inspector or SelfInspector()

    def run(
        self,
        action: str,
        vmid: int,
        argument: str | None = None,
        timeout: int | None = None,
        on_event: EventCallback | None = None,
    ) -> dict[str, Any]:
        try:
            cfg = self._resources[int(vmid)]
        except (KeyError, TypeError, ValueError) as exc:
            raise ExecutorError("Unknown resource VMID") from exc

        adapter = str(cfg.get("adapter", "apt"))
        resource_type = str(cfg.get("resource_type", "lxc"))
        if adapter == "apt" and resource_type == "lxc":
            if action not in APT_ACTIONS:
                raise ExecutorError(f"Action not supported by apt adapter: {action}")
            return self._executor.run(action, vmid, argument, timeout, on_event)
        if adapter == "haos" and resource_type == "qemu":
            if action not in READ_ONLY_ACTIONS or argument is not None:
                raise ExecutorError(f"Action not supported by haos adapter: {action}")
            return self._executor.run(action, vmid, None, timeout, on_event)
        if adapter == "agent_self" and resource_type == "lxc" and int(vmid) == 110:
            if action not in READ_ONLY_ACTIONS or argument is not None:
                raise ExecutorError(f"Action not supported by agent_self adapter: {action}")
            pve = self._executor.run("status", vmid, None, timeout, on_event)
            if action == "status":
                return pve
            data = dict(pve.get("data") or {})
            try:
                data.update(self._self_inspector.inspect())
            except Exception as exc:
                raise ExecutorError(f"Agent self-inspection failed: {exc}") from exc
            return {"ok": True, "data": data}
        raise ExecutorError("Invalid resource adapter configuration")
