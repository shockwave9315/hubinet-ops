from __future__ import annotations

import json
import logging
import time
from typing import Any

from app.mqtt import MqttTelemetry


class FakeClient:
    def __init__(self, client_id: str):
        self.client_id = client_id
        self.published: list[tuple[str, str, int, bool]] = []
        self.on_connect = None
        self.on_disconnect = None
        self.will = None
        self.reconnect = None
        self.credentials = None

    def username_pw_set(self, username: str, password: str) -> None:
        self.credentials = (username, password)

    def will_set(self, topic: str, payload: str, qos: int, retain: bool) -> None:
        self.will = (topic, payload, qos, retain)

    def reconnect_delay_set(self, min_delay: int, max_delay: int) -> None:
        self.reconnect = (min_delay, max_delay)

    def connect_async(self, host: str, port: int, keepalive: int) -> None:
        assert self.on_connect is not None
        self.on_connect(self, None, None, 0, None)

    def loop_start(self) -> None:
        pass

    def loop_stop(self) -> None:
        pass

    def disconnect(self) -> None:
        pass

    def publish(self, topic: str, payload: str, qos: int, retain: bool) -> None:
        self.published.append((topic, payload, qos, retain))


def config(**overrides: Any) -> dict[str, Any]:
    value = {
        "enabled": True,
        "host": "mqtt.test",
        "port": 1883,
        "username": "agent",
        "password": "secret",
        "base_topic": "hubinet/ops",
        "discovery_prefix": "homeassistant",
        "reconnect_min_seconds": 2,
        "reconnect_max_seconds": 60,
        "retain_state": True,
    }
    value.update(overrides)
    return value


def wait_for(predicate) -> None:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("timed out waiting for MQTT publisher")


def test_topics_retention_discovery_lwt_and_stable_ids() -> None:
    client = FakeClient("agent")
    telemetry = MqttTelemetry(
        config(),
        {106: {"dashboard_path": "/hubinet-ops/ct-106"}},
        client_factory=lambda **kwargs: client,
    )
    telemetry.set_state_provider(
        lambda: (
            {"version": "0.2.1", "configured_container_count": 1, "active_job_count": 0},
            [{"vmid": 106, "health_status": "healthy", "updates": {"packages": []}}],
        )
    )
    telemetry.start()
    telemetry.publish_job(106, {"id": "job1"})
    telemetry.publish_event(106, {"message": "live"})
    wait_for(lambda: any(item[0].endswith("/event") for item in client.published))
    assert client.will == ("hubinet/ops/agent/availability", "offline", 1, True)
    assert client.reconnect == (2, 60)
    by_topic = {topic: (json.loads(payload) if payload.startswith("{") else payload, retain) for topic, payload, _, retain in client.published}
    assert by_topic["hubinet/ops/agent/state"][1] is True
    assert by_topic["hubinet/ops/ct/106/state"][1] is True
    assert by_topic["hubinet/ops/ct/106/job"][1] is True
    assert by_topic["hubinet/ops/ct/106/event"][1] is False
    discovery = by_topic["homeassistant/sensor/hubinet_ops_ct106_health_status/config"][0]
    assert discovery["unique_id"] == "hubinet_ops_ct_106_health_status"
    assert discovery["device"]["identifiers"] == ["hubinet_ops_ct_106"]
    telemetry.stop()


def test_unchanged_retained_state_is_not_republished() -> None:
    client = FakeClient("agent")
    telemetry = MqttTelemetry(config(), {}, client_factory=lambda **kwargs: client)
    telemetry.set_state_provider(lambda: ({"version": "0.2.1"}, []))
    telemetry.start()
    state = {"vmid": 106, "health_status": "healthy"}
    telemetry.publish_container_state(106, state)
    telemetry.publish_container_state(106, state)
    topic = "hubinet/ops/ct/106/state"
    wait_for(lambda: any(item[0] == topic for item in client.published))
    time.sleep(0.05)
    assert sum(1 for item in client.published if item[0] == topic) == 1
    telemetry.stop()


def test_disabled_mqtt_is_noop() -> None:
    called = False

    def factory(**kwargs: Any) -> FakeClient:
        nonlocal called
        called = True
        return FakeClient("agent")

    telemetry = MqttTelemetry({"enabled": False}, {}, client_factory=factory)
    telemetry.start()
    telemetry.publish_event(106, {"message": "ignored"})
    telemetry.stop()
    assert called is False


def test_credentials_are_redacted_from_startup_errors(caplog) -> None:
    def broken(**kwargs: Any) -> FakeClient:
        raise RuntimeError("password=supersecret")

    caplog.set_level(logging.WARNING)
    MqttTelemetry(config(), {}, client_factory=broken).start()
    assert "supersecret" not in caplog.text
    assert "[REDACTED]" in caplog.text


def test_reconnect_republishes_discovery_and_full_state() -> None:
    client = FakeClient("agent")
    telemetry = MqttTelemetry(config(), {106: {}}, client_factory=lambda **kwargs: client)
    telemetry.set_state_provider(lambda: ({"version": "0.2.1"}, [{"vmid": 106, "health_status": "healthy"}]))
    telemetry.start()
    discovery_topic = "homeassistant/sensor/hubinet_ops_ct106_health_status/config"
    wait_for(lambda: sum(1 for item in client.published if item[0] == discovery_topic) >= 1)
    telemetry._on_disconnect(client, None, None, 1, None)
    telemetry._on_connect(client, None, None, 0, None)
    wait_for(lambda: sum(1 for item in client.published if item[0] == discovery_topic) >= 2)
    telemetry.stop()


def test_state_attributes_are_bounded() -> None:
    client = FakeClient("agent")
    telemetry = MqttTelemetry(config(), {}, client_factory=lambda **kwargs: client)
    telemetry.set_state_provider(lambda: ({"version": "0.2.1"}, []))
    telemetry.start()
    telemetry.publish_container_state(
        106,
        {
            "vmid": 106,
            "updates": {"packages": [{"name": str(index)} for index in range(250)]},
            "recent_job_events": [{"id": index} for index in range(60)],
        },
    )
    topic = "hubinet/ops/ct/106/state"
    wait_for(lambda: any(item[0] == topic for item in client.published))
    payload = json.loads(next(item[1] for item in client.published if item[0] == topic))
    assert len(payload["updates"]["packages"]) == 200
    assert len(payload["recent_job_events"]) == 50
    telemetry.stop()


def test_publish_error_never_raises_to_caller() -> None:
    class BrokenClient(FakeClient):
        def publish(self, topic: str, payload: str, qos: int, retain: bool) -> None:
            raise RuntimeError("broker unavailable")

    client = BrokenClient("agent")
    telemetry = MqttTelemetry(config(), {}, client_factory=lambda **kwargs: client)
    telemetry.set_state_provider(lambda: ({"version": "0.2.1"}, []))
    telemetry.start()
    telemetry.publish_event(106, {"message": "job continues"})
    time.sleep(0.05)
    telemetry.stop()
