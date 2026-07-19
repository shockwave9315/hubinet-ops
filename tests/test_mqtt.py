from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from app.mqtt import MqttTelemetry


@dataclass
class PublishResult:
    rc: int = 0


class FakeClient:
    def __init__(self, client_id: str, *, auto_connect: bool = True):
        self.client_id = client_id
        self.auto_connect = auto_connect
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
        if self.auto_connect:
            assert self.on_connect is not None
            self.on_connect(self, None, None, 0, None)

    def loop_start(self) -> None:
        pass

    def loop_stop(self) -> None:
        pass

    def disconnect(self) -> None:
        pass

    def publish(self, topic: str, payload: str, qos: int, retain: bool) -> PublishResult:
        self.published.append((topic, payload, qos, retain))
        return PublishResult()


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


def wait_for(predicate, timeout: float = 3) -> None:
    deadline = time.monotonic() + timeout
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
            {
                "version": "0.2.1",
                "configured_container_count": 1,
                "active_job_count": 0,
            },
            [{"vmid": 106, "health_status": "healthy", "updates": {"packages": []}}],
        )
    )
    telemetry.start()
    telemetry.publish_job(106, {"id": "job1"})
    telemetry.publish_event(106, {"message": "live"})
    wait_for(lambda: any(item[0].endswith("/event") for item in client.published))
    assert client.will == ("hubinet/ops/agent/availability", "offline", 1, True)
    assert client.reconnect == (2, 60)
    by_topic = {
        topic: (json.loads(payload) if payload.startswith("{") else payload, retain)
        for topic, payload, _, retain in client.published
    }
    assert by_topic["hubinet/ops/agent/state"][1] is True
    assert by_topic["hubinet/ops/ct/106/state"][1] is True
    assert by_topic["hubinet/ops/resource/106/state"][1] is True
    assert by_topic["hubinet/ops/ct/106/job"][1] is True
    assert by_topic["hubinet/ops/ct/106/event"][1] is False
    discovery = by_topic[
        "homeassistant/sensor/hubinet_ops_ct106_health_status/config"
    ][0]
    assert discovery["unique_id"] == "hubinet_ops_ct_106_health_status"
    assert discovery["device"]["identifiers"] == ["hubinet_ops_ct_106"]
    assert discovery["json_attributes_topic"] == "hubinet/ops/resource/106/state"
    progress_discovery = by_topic[
        "homeassistant/sensor/hubinet_ops_ct106_job_progress/config"
    ][0]
    assert "json_attributes_topic" not in progress_discovery
    telemetry.stop()


def test_resource_discovery_uses_type_specific_ids_models_and_agent_counts() -> None:
    client = FakeClient("agent")
    telemetry = MqttTelemetry(
        config(),
        {
            100: {"resource_type": "qemu", "adapter": "haos"},
            101: {"resource_type": "lxc", "adapter": "apt"},
            110: {"resource_type": "lxc", "adapter": "agent_self"},
        },
        client_factory=lambda **kwargs: client,
    )
    telemetry.set_state_provider(
        lambda: (
            {
                "version": "0.3.0",
                "configured_container_count": 2,
                "configured_resource_count": 3,
                "configured_lxc_count": 2,
                "configured_qemu_count": 1,
            },
            [
                {"vmid": 100, "resource_type": "qemu", "qemu_status": "running"},
                {"vmid": 101, "resource_type": "lxc", "lxc_status": "running"},
                {"vmid": 110, "resource_type": "lxc", "adapter": "agent_self"},
            ],
        )
    )
    telemetry.start()
    wait_for(
        lambda: any(
            topic == "homeassistant/sensor/hubinet_ops_ct110_health_status/config"
            for topic, *_ in client.published
        )
    )
    by_topic = {
        topic: json.loads(payload)
        for topic, payload, _, _ in client.published
        if payload.startswith("{")
    }

    vm = by_topic["homeassistant/sensor/hubinet_ops_vm100_health_status/config"]
    ct = by_topic["homeassistant/sensor/hubinet_ops_ct101_health_status/config"]
    agent = by_topic["homeassistant/sensor/hubinet_ops_ct110_health_status/config"]
    assert vm["unique_id"] == "hubinet_ops_vm_100_health_status"
    assert vm["device"]["model"] == "Observed Proxmox QEMU"
    assert ct["device"]["model"] == "Managed Proxmox LXC"
    assert agent["device"]["model"] == "Hubinet Ops Agent"
    assert "homeassistant/sensor/hubinet_ops_vm100_pending_updates/config" not in by_topic
    for key in ("configured_resource_count", "configured_lxc_count", "configured_qemu_count"):
        assert f"homeassistant/sensor/hubinet_ops_agent_{key}/config" in by_topic
    assert "homeassistant/sensor/hubinet_ops_agent_configured_container_count/config" in by_topic
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
    telemetry.set_state_provider(
        lambda: (
            {"version": "0.2.1"},
            [{"vmid": 106, "health_status": "healthy"}],
        )
    )
    telemetry.start()
    discovery_topic = "homeassistant/sensor/hubinet_ops_ct106_health_status/config"
    wait_for(
        lambda: sum(1 for item in client.published if item[0] == discovery_topic) >= 1
    )
    telemetry._on_disconnect(client, None, None, 1, None)
    telemetry._on_connect(client, None, None, 0, None)
    wait_for(
        lambda: sum(1 for item in client.published if item[0] == discovery_topic) >= 2
    )
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


def test_disconnected_publisher_preserves_fifo_without_queue_rotation() -> None:
    client = FakeClient("agent", auto_connect=False)
    telemetry = MqttTelemetry(config(), {}, client_factory=lambda **kwargs: client)
    telemetry.start()
    telemetry.publish_container_state(106, {"sequence": 1})
    telemetry.publish_container_state(106, {"sequence": 2}, force=True)
    time.sleep(0.1)
    assert not any(topic.endswith("/ct/106/state") for topic, *_ in client.published)

    telemetry._connected.set()
    topic = "hubinet/ops/ct/106/state"
    wait_for(lambda: sum(1 for item in client.published if item[0] == topic) == 2)
    payloads = [
        json.loads(payload)
        for published_topic, payload, _, _ in client.published
        if published_topic == topic
    ]
    assert [payload["sequence"] for payload in payloads] == [1, 2]
    telemetry.stop()


def test_publish_failure_retries_same_item_before_later_items() -> None:
    class FlakyClient(FakeClient):
        def __init__(self) -> None:
            super().__init__("agent", auto_connect=False)
            self.attempts: list[str] = []
            self.failures = 1

        def publish(self, topic: str, payload: str, qos: int, retain: bool) -> PublishResult:
            if topic.endswith("/event"):
                message = json.loads(payload)["message"]
                self.attempts.append(message)
                if self.failures:
                    self.failures -= 1
                    return PublishResult(rc=1)
            return super().publish(topic, payload, qos, retain)

    client = FlakyClient()
    telemetry = MqttTelemetry(config(), {}, client_factory=lambda **kwargs: client)
    telemetry.start()
    telemetry.publish_event(106, {"message": "first"})
    telemetry.publish_event(106, {"message": "second"})
    telemetry._connected.set()
    wait_for(lambda: client.attempts.count("first") >= 2 and "second" in client.attempts)
    assert client.attempts[:4] == ["first", "first", "first", "second"]
    telemetry.stop()


def test_publish_exception_never_raises_to_caller() -> None:
    class BrokenClient(FakeClient):
        def publish(self, topic: str, payload: str, qos: int, retain: bool) -> PublishResult:
            if topic.endswith("/event"):
                raise RuntimeError("broker unavailable")
            return super().publish(topic, payload, qos, retain)

    client = BrokenClient("agent")
    telemetry = MqttTelemetry(config(), {}, client_factory=lambda **kwargs: client)
    telemetry.set_state_provider(lambda: ({"version": "0.2.1"}, []))
    telemetry.start()
    telemetry.publish_event(106, {"message": "job continues"})
    time.sleep(0.05)
    telemetry.stop()
