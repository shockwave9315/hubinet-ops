from __future__ import annotations

import json
import time

from app.mqtt import MqttTelemetry
from tests.test_mqtt import FakeClient, config, wait_for


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def monotonic(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _state(cpu: float, **changes):
    state = {
        "vmid": 100,
        "resource_type": "qemu",
        "adapter": "haos",
        "runtime_status": "running",
        "qemu_status": "running",
        "health_status": "healthy",
        "last_refresh": "2026-07-29T12:00:00+00:00",
        "uptime_seconds": 100,
        "cpu": {"usage": cpu / 100, "usage_percent": cpu},
        "memory": {"used_bytes": 100},
        "disk": {"used_bytes": None, "total_bytes": 1000},
        "network": {"in_bytes": 100, "out_bytes": 200},
    }
    state.update(changes)
    return state


def _telemetry(clock: FakeClock, client: FakeClient) -> MqttTelemetry:
    settings = {
        **config(),
        "cpu_publish_deadband_percent": 0.5,
        "telemetry_heartbeat_seconds": 300,
    }
    telemetry = MqttTelemetry(
        settings,
        {100: {"resource_type": "qemu", "adapter": "haos"}},
        client_factory=lambda **_: client,
        monotonic=clock.monotonic,
    )
    telemetry.set_state_provider(lambda: ({"version": "0.4.2"}, []))
    telemetry.start()
    return telemetry


def _state_payloads(client: FakeClient) -> list[dict]:
    return [
        json.loads(payload)
        for topic, payload, _, _ in client.published
        if topic == "hubinet/ops/resource/100/state"
    ]


def test_small_cpu_fluctuations_are_suppressed_and_cpu_is_rounded() -> None:
    clock = FakeClock()
    client = FakeClient("cpu-deadband")
    telemetry = _telemetry(clock, client)

    telemetry.publish_resource_state(100, _state(2.04))
    telemetry.publish_resource_state(
        100,
        _state(
            2.34,
            last_refresh="2026-07-29T12:00:30+00:00",
            uptime_seconds=130,
            network={"in_bytes": 999, "out_bytes": 1000},
        ),
    )
    wait_for(lambda: len(_state_payloads(client)) == 1)
    time.sleep(0.05)

    assert len(_state_payloads(client)) == 1
    assert _state_payloads(client)[0]["cpu"]["usage_percent"] == 2.0
    telemetry.stop()


def test_larger_cpu_change_publishes_immediately() -> None:
    clock = FakeClock()
    client = FakeClient("cpu-change")
    telemetry = _telemetry(clock, client)

    telemetry.publish_resource_state(100, _state(2.0))
    telemetry.publish_resource_state(100, _state(2.5))
    wait_for(lambda: len(_state_payloads(client)) == 2)

    assert [item["cpu"]["usage_percent"] for item in _state_payloads(client)] == [
        2.0,
        2.5,
    ]
    telemetry.stop()


def test_telemetry_heartbeat_publishes_at_five_minutes_not_before() -> None:
    clock = FakeClock()
    client = FakeClient("cpu-heartbeat")
    telemetry = _telemetry(clock, client)

    telemetry.publish_resource_state(100, _state(2.0))
    clock.advance(299)
    telemetry.publish_resource_state(100, _state(2.1))
    wait_for(lambda: len(_state_payloads(client)) == 1)
    clock.advance(1)
    telemetry.publish_resource_state(100, _state(2.1))
    wait_for(lambda: len(_state_payloads(client)) == 2)

    assert len(_state_payloads(client)) == 2
    telemetry.stop()


def test_restart_always_publishes_initial_state() -> None:
    first_clock = FakeClock()
    first_client = FakeClient("restart-first")
    first = _telemetry(first_clock, first_client)
    first.publish_resource_state(100, _state(2.0))
    wait_for(lambda: len(_state_payloads(first_client)) == 1)
    first.stop()

    second_clock = FakeClock()
    second_client = FakeClient("restart-second")
    second = _telemetry(second_clock, second_client)
    second.publish_resource_state(100, _state(2.0))
    wait_for(lambda: len(_state_payloads(second_client)) == 1)

    assert len(_state_payloads(second_client)) == 1
    second.stop()


def test_error_and_runtime_transitions_bypass_cpu_deadband() -> None:
    clock = FakeClock()
    client = FakeClient("runtime-bypass")
    telemetry = _telemetry(clock, client)

    telemetry.publish_resource_state(100, _state(2.0))
    telemetry.publish_resource_state(
        100,
        _state(2.1, health_status="critical", last_error="read-only probe failed"),
    )
    telemetry.publish_resource_state(
        100,
        _state(
            2.1,
            health_status="offline",
            runtime_status="stopped",
            qemu_status="stopped",
        ),
    )
    wait_for(lambda: len(_state_payloads(client)) == 3)

    payloads = _state_payloads(client)
    assert payloads[1]["health_status"] == "critical"
    assert payloads[2]["runtime_status"] == "stopped"
    telemetry.stop()
