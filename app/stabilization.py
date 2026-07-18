from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from .executor import ExecutorError


class HealthExecutor(Protocol):
    def run(
        self,
        action: str,
        vmid: int,
        argument: str | None = None,
        timeout: int | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class StabilizationPolicy:
    post_update_timeout_seconds: float = 300
    post_rollback_timeout_seconds: float = 300
    repair_timeout_seconds: float = 180
    poll_interval_seconds: float = 10
    initial_grace_seconds: float = 10
    required_consecutive_successes: int = 2

    @classmethod
    def from_config(cls, value: dict[str, Any] | None) -> "StabilizationPolicy":
        cfg = value or {}
        return cls(
            post_update_timeout_seconds=max(1, float(cfg.get("post_update_timeout_seconds", 300))),
            post_rollback_timeout_seconds=max(1, float(cfg.get("post_rollback_timeout_seconds", 300))),
            repair_timeout_seconds=max(1, float(cfg.get("repair_timeout_seconds", 180))),
            poll_interval_seconds=max(0.1, float(cfg.get("poll_interval_seconds", 10))),
            initial_grace_seconds=max(0, float(cfg.get("initial_grace_seconds", 10))),
            required_consecutive_successes=max(1, int(cfg.get("required_consecutive_successes", 2))),
        )


class Stabilizer:
    def __init__(
        self,
        executor: HealthExecutor,
        stop_event: threading.Event,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] | None = None,
    ):
        self.executor = executor
        self.stop_event = stop_event
        self.monotonic = monotonic
        self.sleep = sleep or self._interruptible_sleep

    def wait(
        self,
        *,
        vmid: int,
        phase: str,
        timeout_seconds: float,
        policy: StabilizationPolicy,
        emit: Callable[..., None],
        initial_grace: bool = True,
    ) -> dict[str, Any]:
        if initial_grace and policy.initial_grace_seconds:
            emit(
                stage=self._stage(phase),
                progress=self._progress(phase),
                event_type="stabilization_grace",
                message=f"Waiting {policy.initial_grace_seconds:g}s before service checks",
            )
            self.sleep(policy.initial_grace_seconds)
        deadline = self.monotonic() + timeout_seconds
        consecutive = 0
        last_data: dict[str, Any] = {}
        while self.monotonic() <= deadline:
            if self.stop_event.is_set():
                raise ExecutorError("Agent shutdown interrupted stabilization", data=last_data)
            try:
                result = self.executor.run("inspect", vmid, timeout=120)
                last_data = dict(result.get("data", {}))
                ok = self._is_stable(last_data)
            except ExecutorError as exc:
                last_data = dict(exc.data)
                ok = False

            docker = last_data.get("docker") or {}
            required = list(docker.get("required") or [])
            containers = list(docker.get("containers") or [])
            by_name = {str(item.get("name")): item for item in containers if isinstance(item, dict)}
            healthy = sum(
                1
                for name in required
                if _container_ok(by_name.get(str(name)), bool(docker.get("require_health", True)))
            )
            total = len(required)
            if ok:
                consecutive += 1
            else:
                consecutive = 0
            message = "Services healthy" if ok else "Services are not stable"
            if total:
                message = f"Docker required containers {healthy}/{total} healthy"
            emit(
                stage=self._stage(phase),
                progress=self._progress(phase),
                event_type="stabilization_poll",
                message=message,
                details={
                    "consecutive_successes": consecutive,
                    "required_consecutive_successes": policy.required_consecutive_successes,
                    "docker_available": docker.get("available"),
                    "docker_required_healthy": healthy,
                    "docker_required_total": total,
                    "services": last_data.get("services", {}),
                    "failures": list(last_data.get("failures") or [])[:20],
                },
            )
            if consecutive >= policy.required_consecutive_successes:
                return last_data
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                break
            self.sleep(min(policy.poll_interval_seconds, remaining))
        raise ExecutorError(f"{phase} stabilization timed out", data=last_data)

    def _interruptible_sleep(self, seconds: float) -> None:
        if self.stop_event.wait(seconds):
            raise ExecutorError("Agent shutdown interrupted stabilization")

    @staticmethod
    def _is_stable(data: dict[str, Any]) -> bool:
        health = data.get("health_status", data.get("health"))
        if health != "healthy" or data.get("lxc_status", "running") != "running":
            return False
        if any(str(value) != "active" for value in (data.get("services") or {}).values()):
            return False
        docker = data.get("docker") or {}
        if not docker.get("enabled", False):
            return True
        if not docker.get("available", False):
            return False
        required = list(docker.get("required") or [])
        if not required:
            return bool(docker.get("required_ok", True))
        by_name = {
            str(item.get("name")): item
            for item in (docker.get("containers") or [])
            if isinstance(item, dict)
        }
        require_health = bool(docker.get("require_health", True))
        return all(_container_ok(by_name.get(str(name)), require_health) for name in required)

    @staticmethod
    def _stage(phase: str) -> str:
        if phase == "rollback":
            return "rollback_healthcheck"
        return "repair" if phase == "repair" else "healthcheck"

    @staticmethod
    def _progress(phase: str) -> int:
        if phase == "rollback":
            return 92
        return 86 if phase == "repair" else 82


def _container_ok(item: dict[str, Any] | None, require_health: bool) -> bool:
    if not item or not item.get("running", False):
        return False
    return not require_health or item.get("health") in {"healthy", "none"}
