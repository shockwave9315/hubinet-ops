from __future__ import annotations

import threading
from typing import Any

import pytest

from app.executor import ExecutorError
from app.stabilization import StabilizationPolicy, Stabilizer


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class SequenceExecutor:
    def __init__(self, states: list[dict[str, Any]]) -> None:
        self.states = list(states)
        self.last = states[-1]
        self.timeouts: list[int | None] = []

    def run(
        self,
        action: str,
        vmid: int,
        argument=None,
        timeout=None,
        on_event=None,
    ) -> dict[str, Any]:
        assert action == "inspect"
        self.timeouts.append(timeout)
        state = self.states.pop(0) if self.states else self.last
        return {"ok": True, "data": state}


def docker_state(
    healthy: int,
    *,
    available: bool = True,
    missing_health: bool = False,
) -> dict[str, Any]:
    names = ["api", "worker", "redis"]
    containers = [
        {
            "name": name,
            "running": index < healthy or missing_health,
            "health": (
                "none"
                if missing_health
                else "healthy" if index < healthy else "starting"
            ),
        }
        for index, name in enumerate(names)
    ]
    return {
        "health_status": "healthy" if healthy == 3 and available else "degraded",
        "lxc_status": "running",
        "services": {"docker": "active", "containerd": "active"},
        "docker": {
            "enabled": True,
            "available": available,
            "required": names,
            "require_health": True,
            "containers": containers,
        },
    }


def run_sequence(
    states: list[dict[str, Any]],
    timeout: float = 10,
) -> tuple[dict[str, Any], list[dict]]:
    clock = FakeClock()
    events: list[dict] = []
    stabilizer = Stabilizer(
        SequenceExecutor(states),
        threading.Event(),
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    result = stabilizer.wait(
        vmid=106,
        phase="update",
        timeout_seconds=timeout,
        policy=StabilizationPolicy(
            poll_interval_seconds=1,
            initial_grace_seconds=0,
            required_consecutive_successes=2,
        ),
        emit=lambda **event: events.append(event),
    )
    return result, events


def test_docker_0_2_3_3_succeeds() -> None:
    result, events = run_sequence(
        [docker_state(0), docker_state(2), docker_state(3), docker_state(3)]
    )
    assert result["health_status"] == "healthy"
    assert [
        event["details"]["docker_required_healthy"] for event in events
    ] == [0, 2, 3, 3]


def test_temporary_docker_unavailable_recovers() -> None:
    result, _ = run_sequence(
        [docker_state(0, available=False), docker_state(3), docker_state(3)]
    )
    assert result["docker"]["available"] is True


def test_required_container_without_healthcheck_is_not_accepted() -> None:
    clock = FakeClock()
    state = docker_state(0, missing_health=True)
    state["health_status"] = "healthy"
    stabilizer = Stabilizer(
        SequenceExecutor([state]),
        threading.Event(),
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    with pytest.raises(ExecutorError, match="timed out"):
        stabilizer.wait(
            vmid=106,
            phase="update",
            timeout_seconds=1,
            policy=StabilizationPolicy(
                poll_interval_seconds=1,
                initial_grace_seconds=0,
                required_consecutive_successes=1,
            ),
            emit=lambda **event: None,
        )


def test_timeout_contains_last_observed_state() -> None:
    clock = FakeClock()
    executor = SequenceExecutor([docker_state(0)])
    stabilizer = Stabilizer(
        executor,
        threading.Event(),
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    with pytest.raises(ExecutorError) as caught:
        stabilizer.wait(
            vmid=106,
            phase="rollback",
            timeout_seconds=2,
            policy=StabilizationPolicy(
                poll_interval_seconds=1,
                initial_grace_seconds=0,
                required_consecutive_successes=2,
            ),
            emit=lambda **event: None,
        )
    assert caught.value.data["docker"]["available"] is True
    assert all(timeout is not None and timeout <= 2 for timeout in executor.timeouts)


def test_shutdown_interrupts_wait() -> None:
    stop = threading.Event()
    stop.set()
    stabilizer = Stabilizer(
        SequenceExecutor([docker_state(0)]),
        stop,
        sleep=lambda _: None,
    )
    with pytest.raises(ExecutorError, match="shutdown"):
        stabilizer.wait(
            vmid=106,
            phase="update",
            timeout_seconds=10,
            policy=StabilizationPolicy(initial_grace_seconds=0),
            emit=lambda **event: None,
        )
