from __future__ import annotations

import math
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
            post_update_timeout_seconds=_positive_float(
                cfg.get("post_update_timeout_seconds", 300),
                "post_update_timeout_seconds",
            ),
            post_rollback_timeout_seconds=_positive_float(
                cfg.get("post_rollback_timeout_seconds", 300),
                "post_rollback_timeout_seconds",
            ),
            repair_timeout_seconds=_positive_float(
                cfg.get("repair_timeout_seconds", 180),
                "repair_timeout_seconds",
            ),
            poll_interval_seconds=_positive_float(
                cfg.get("poll_interval_seconds", 10),
                "poll_interval_seconds",
                minimum=0.1,
            ),
            initial_grace_seconds=_non_negative_float(
                cfg.get("initial_grace_seconds", 10),
                "initial_grace_seconds",
            ),
            required_consecutive_successes=_positive_int(
                cfg.get("required_consecutive_successes", 2),
                "required_consecutive_successes",
            ),
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
        last_error: str | None = None
        while self.monotonic() <= deadline:
            if self.stop_event.is_set():
                raise ExecutorError(
                    "Agent shutdown interrupted stabilization",
                    data=last_data,
                )
            remaining = max(0.0, deadline - self.monotonic())
            inspect_timeout = max(1, min(120, int(math.ceil(remaining)) or 1))
            try:
                result = self.executor.run("inspect", vmid, timeout=inspect_timeout)
                raw_data = result.get("data") if isinstance(result, dict) else None
                last_data = dict(raw_data) if isinstance(raw_data, dict) else {}
                last_error = None
                ok = self._is_stable(last_data)
            except ExecutorError as exc:
                last_data = dict(exc.data)
                last_error = str(exc)
                ok = False

            docker = last_data.get("docker") or {}
            required = list(docker.get("required") or [])
            containers = list(docker.get("containers") or [])
            by_name = {
                str(item.get("name")): item
                for item in containers
                if isinstance(item, dict)
            }
            require_health = bool(docker.get("require_health", True))
            healthy = sum(
                1
                for name in required
                if _container_ok(by_name.get(str(name)), require_health)
            )
            total = len(required)
            consecutive = consecutive + 1 if ok else 0
            message = "Services healthy" if ok else "Services are not stable"
            if total:
                message = f"Docker required containers {healthy}/{total} healthy"
            details = {
                "consecutive_successes": consecutive,
                "required_consecutive_successes": policy.required_consecutive_successes,
                "docker_available": docker.get("available"),
                "docker_required_healthy": healthy,
                "docker_required_total": total,
                "services": last_data.get("services", {}),
                "failures": list(last_data.get("failures") or [])[:20],
            }
            if last_error:
                details["inspect_error"] = last_error
            emit(
                stage=self._stage(phase),
                progress=self._progress(phase),
                event_type="stabilization_poll",
                message=message,
                details=details,
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
    if not require_health:
        return True
    return item.get("health") == "healthy"


def _positive_float(value: Any, name: str, *, minimum: float = 1.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"stabilization.{name} must be a number") from exc
    if not math.isfinite(result) or result < minimum:
        raise RuntimeError(f"stabilization.{name} must be at least {minimum:g}")
    return result


def _non_negative_float(value: Any, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"stabilization.{name} must be a number") from exc
    if not math.isfinite(result) or result < 0:
        raise RuntimeError(f"stabilization.{name} cannot be negative")
    return result


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise RuntimeError(f"stabilization.{name} must be an integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"stabilization.{name} must be an integer") from exc
    if isinstance(value, float) and not value.is_integer():
        raise RuntimeError(f"stabilization.{name} must be an integer")
    if isinstance(value, str) and not value.strip().lstrip("+-").isdigit():
        raise RuntimeError(f"stabilization.{name} must be an integer")
    if result <= 0:
        raise RuntimeError(f"stabilization.{name} must be positive")
    return result
