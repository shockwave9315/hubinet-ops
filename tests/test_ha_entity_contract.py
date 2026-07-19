from __future__ import annotations

import json
import queue
import re
from pathlib import Path

import pytest
import yaml

from app.ha_entities import AGENT_SELF_OBSOLETE_KEYS
from app.mqtt import MqttTelemetry
from app.mqtt_budget import bounded_state
from app.state import normalize_state
from scripts.generate_ha_dashboard import DEFAULT_CONFIG, render


def _resources() -> dict[int, dict]:
    raw = yaml.safe_load(DEFAULT_CONFIG.read_text(encoding="utf-8"))
    return {int(vmid): dict(cfg) for vmid, cfg in raw["resources"].items()}


def _discovery() -> tuple[dict[str, dict], dict[str, str]]:
    telemetry = MqttTelemetry(
        {"enabled": True, "discovery_prefix": "homeassistant"},
        _resources(),
    )
    telemetry.publish_discovery(force=True)
    configs: dict[str, dict] = {}
    raw: dict[str, str] = {}
    while True:
        try:
            item = telemetry._queue.get_nowait()
        except queue.Empty:
            break
        assert item is not None
        raw[item.topic] = item.payload
        if item.payload:
            configs[item.topic] = json.loads(item.payload)
    return configs, raw


def _dashboard_sensor_ids(text: str) -> set[str]:
    return set(re.findall(r"sensor\.hubinet_ops_[a-z0-9_]+", text))


def _assert_references_published(dashboard: str, published: set[str]) -> None:
    missing = _dashboard_sensor_ids(dashboard) - published
    assert not missing, f"Dashboard references unpublished entities: {sorted(missing)}"


def test_full_inventory_dashboard_references_only_discovery_entities() -> None:
    configs, _ = _discovery()
    published = {payload["default_entity_id"] for payload in configs.values()}
    dashboard = render(DEFAULT_CONFIG)

    assert len(_resources()) == 11
    _assert_references_published(dashboard, published)
    for stale in (
        "sensor.hubinet_ops_vm100_guest_agent_status",
        "sensor.hubinet_ops_vm100_uptime_seconds",
        "sensor.hubinet_ops_vm100_disk_used_bytes",
        "sensor.hubinet_ops_ct110_network_received",
        "sensor.hubinet_ops_ct110_cpu_usage",
        "sensor.hubinet_ops_ct101_pending_updates",
    ):
        with pytest.raises(AssertionError, match="unpublished"):
            _assert_references_published(dashboard + "\n" + stale, published)


def test_discovery_uses_production_suffixes_and_preserves_unique_ids() -> None:
    configs, _ = _discovery()
    by_unique_id = {payload["unique_id"]: payload for payload in configs.values()}
    expected = {
        "hubinet_ops_vm_100_uptime_seconds": "sensor.hubinet_ops_vm100_uptime",
        "hubinet_ops_vm_100_memory_used_bytes": "sensor.hubinet_ops_vm100_memory_used",
        "hubinet_ops_vm_100_memory_total_bytes": "sensor.hubinet_ops_vm100_memory_total",
        "hubinet_ops_vm_100_disk_used_bytes": "sensor.hubinet_ops_vm100_disk_used",
        "hubinet_ops_vm_100_disk_total_bytes": "sensor.hubinet_ops_vm100_disk_total",
        "hubinet_ops_vm_100_network_in_bytes": "sensor.hubinet_ops_vm100_network_received",
        "hubinet_ops_vm_100_network_out_bytes": "sensor.hubinet_ops_vm100_network_sent",
        "hubinet_ops_vm_100_guest_agent_status": "sensor.hubinet_ops_vm100_guest_agent",
        "hubinet_ops_ct_101_pending_updates": "sensor.hubinet_ops_ct101_pending_update_count",
        "hubinet_ops_ct_101_disk_used_percent": "sensor.hubinet_ops_ct101_disk_used",
        "hubinet_ops_ct_101_disk_free_mb": "sensor.hubinet_ops_ct101_disk_free",
        "hubinet_ops_ct_101_memory_used_percent": "sensor.hubinet_ops_ct101_memory_used",
    }
    for unique_id, entity_id in expected.items():
        assert by_unique_id[unique_id]["default_entity_id"] == entity_id


def test_discovery_is_adapter_specific() -> None:
    configs, _ = _discovery()
    topics = set(configs)

    assert not any("hubinet_ops_vm100_cpu_load_1m" in topic for topic in topics)
    assert not any("hubinet_ops_ct110_cpu_usage" in topic for topic in topics)
    assert not any("hubinet_ops_ct110_network_in_bytes" in topic for topic in topics)
    assert not any("hubinet_ops_ct110_network_out_bytes" in topic for topic in topics)
    for key in (
        "cpu_load_1m",
        "cpu_cores",
        "memory_available_bytes",
        "disk_free_bytes",
        "recent_warnings",
    ):
        assert f"homeassistant/sensor/hubinet_ops_ct110_{key}/config" in topics


def test_numeric_discovery_never_falls_back_to_unknown_string() -> None:
    configs, _ = _discovery()
    numeric = [payload for payload in configs.values() if "unit_of_measurement" in payload]

    assert numeric
    for payload in numeric:
        template = payload["value_template"]
        assert "'unknown'" not in template
        assert "default(none)" in template

    state = bounded_state(
        {
            "memory": {"used_bytes": 123, "total_bytes": 456},
            "disk": {"used_bytes": 789},
        }
    )
    assert state["memory"]["used_bytes"] == 123
    assert state["memory"]["total_bytes"] == 456
    assert state["disk"]["used_bytes"] == 789
    assert "available_bytes" not in state["memory"]


def test_vm100_primary_ip_replaces_479_character_state_without_losing_attributes() -> None:
    addresses = ["192.168.4.168"] + [f"2001:db8::{index:032x}" for index in range(10)]
    addresses[-1] += "0" * (479 - len(", ".join(addresses)))
    assert len(", ".join(addresses)) == 479

    state = bounded_state(
        normalize_state(
            {
                "resource_type": "qemu",
                "adapter": "haos",
                "qemu_status": "running",
                "primary_ip_address": "192.168.4.168",
                "ip_addresses": addresses,
            }
        )
    )
    assert state["primary_ip_address"] == "192.168.4.168"
    assert len(state["primary_ip_address"]) < 255
    assert len(state["ip_addresses"]) == len(addresses)

    configs, _ = _discovery()
    ip_sensor = configs[
        "homeassistant/sensor/hubinet_ops_vm100_ip_addresses/config"
    ]
    assert ip_sensor["unique_id"] == "hubinet_ops_vm_100_ip_addresses"
    assert ip_sensor["name"] == "Primary IP"
    assert "primary_ip_address" in ip_sensor["value_template"]
    assert "join" not in ip_sensor["value_template"]


def test_obsolete_agent_self_discovery_is_cleared_with_exact_retained_topics() -> None:
    _, raw = _discovery()
    expected = {
        f"homeassistant/sensor/hubinet_ops_ct110_{key}/config"
        for key in AGENT_SELF_OBSOLETE_KEYS
    }
    cleared = {topic for topic, payload in raw.items() if payload == ""}

    assert cleared == expected
    assert not any("vm100" in topic or "ct101" in topic for topic in cleared)


def test_obsolete_discovery_cleanup_is_idempotent_and_retained() -> None:
    telemetry = MqttTelemetry(
        {"enabled": True, "discovery_prefix": "homeassistant"},
        _resources(),
    )
    telemetry.publish_discovery(force=True)
    telemetry.publish_discovery(force=True)
    cleared = []
    while True:
        try:
            item = telemetry._queue.get_nowait()
        except queue.Empty:
            break
        if item is not None and item.payload == "":
            cleared.append(item)

    expected_topics = {
        f"homeassistant/sensor/hubinet_ops_ct110_{key}/config"
        for key in AGENT_SELF_OBSOLETE_KEYS
    }
    assert {item.topic for item in cleared} == expected_topics
    assert len(cleared) == 2 * len(expected_topics)
    assert all(item.retain and item.force for item in cleared)
