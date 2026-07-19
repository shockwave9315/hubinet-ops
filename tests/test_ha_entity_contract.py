from __future__ import annotations

import json
import queue
import re
from pathlib import Path

import pytest
import yaml

from app.ha_entities import (
    ResourceIdentity,
    normalize_resource_identity,
    obsolete_discovery_keys,
    resource_entity_specs,
    resource_prefix,
)
from app.mqtt import MqttTelemetry
from app.mqtt_budget import bounded_state
from app.state import normalize_state
from scripts.generate_ha_dashboard import DEFAULT_CONFIG, build_dashboard, render


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


def test_qemu_cpu_share_is_exposed_as_an_explicit_ha_percentage() -> None:
    state = bounded_state(
        normalize_state(
            {
                "resource_type": "qemu",
                "adapter": "haos",
                "qemu_status": "running",
                "cpu": {"usage": 0.125, "cores": 4},
            }
        )
    )
    assert state["cpu"]["usage"] == 0.125
    assert state["cpu"]["usage_percent"] == 12.5

    configs, _ = _discovery()
    cpu_sensor = configs[
        "homeassistant/sensor/hubinet_ops_vm100_cpu_usage/config"
    ]
    assert cpu_sensor["unit_of_measurement"] == "%"
    assert cpu_sensor["value_template"] == (
        "{{ value_json.cpu.usage_percent | default(none) }}"
    )


def test_resource_identity_is_canonical_and_shared() -> None:
    lower = {"resource_type": "qemu", "adapter": "haos"}
    mixed = {"resource_type": "QeMu", "adapter": "HaOs"}

    assert normalize_resource_identity(lower) == ResourceIdentity("qemu", "haos")
    assert normalize_resource_identity(mixed) == ResourceIdentity("qemu", "haos")
    assert resource_entity_specs(mixed) == resource_entity_specs(lower)
    assert resource_prefix(100, mixed) == "vm100"
    assert obsolete_discovery_keys(mixed) == ("cpu_load_1m",)


@pytest.mark.parametrize(
    "cfg",
    [
        {"resource_type": None, "adapter": "haos"},
        {"resource_type": "qemu", "adapter": None},
        {"adapter": "haos"},
        {"resource_type": "qemu"},
    ],
)
def test_resource_identity_rejects_none_and_missing_fields(cfg: dict) -> None:
    with pytest.raises(ValueError, match="Missing resource identity field"):
        normalize_resource_identity(cfg)


def test_resource_identity_rejects_unsupported_combination_without_fallback() -> None:
    with pytest.raises(ValueError, match="Unsupported resource entity contract"):
        normalize_resource_identity({"resource_type": "qemu", "adapter": "apt"})


def test_dashboard_reports_controlled_resource_identity_error() -> None:
    with pytest.raises(RuntimeError, match="Invalid resource identity for VMID 100"):
        build_dashboard({100: {"resource_type": "qemu"}})


def test_obsolete_030_discovery_is_cleared_with_exact_retained_topics() -> None:
    _, raw = _discovery()
    expected = {
        "homeassistant/sensor/hubinet_ops_vm100_cpu_load_1m/config",
        "homeassistant/sensor/hubinet_ops_ct110_cpu_usage/config",
        "homeassistant/sensor/hubinet_ops_ct110_network_in_bytes/config",
        "homeassistant/sensor/hubinet_ops_ct110_network_out_bytes/config",
    }
    cleared = {topic for topic, payload in raw.items() if payload == ""}

    assert cleared == expected
    assert not any("ct101" in topic for topic in cleared)


def test_cleanup_exactly_matches_030_discovery_minus_031_discovery() -> None:
    legacy_common = {
        "health_status", "health_score", "runtime_status", "uptime_seconds",
        "last_refresh", "last_error", "cpu_usage", "cpu_load_1m", "cpu_cores",
        "memory_used_bytes", "memory_total_bytes", "disk_used_bytes",
        "disk_total_bytes", "network_in_bytes", "network_out_bytes",
    }
    legacy_by_vmid = {
        100: legacy_common | {"qemu_status", "guest_agent_status", "ip_addresses"},
        110: legacy_common | {"lxc_status", "service_status", "api_health", "agent_version"},
    }
    resources = _resources()
    retired_topics = set()
    for vmid, legacy_keys in legacy_by_vmid.items():
        current_keys = {spec.key for spec in resource_entity_specs(resources[vmid])}
        prefix = resource_prefix(vmid, resources[vmid])
        retired_topics.update(
            f"homeassistant/sensor/hubinet_ops_{prefix}_{key}/config"
            for key in legacy_keys - current_keys
        )

    _, raw = _discovery()
    cleared_topics = {topic for topic, payload in raw.items() if payload == ""}
    assert retired_topics == {
        "homeassistant/sensor/hubinet_ops_vm100_cpu_load_1m/config",
        "homeassistant/sensor/hubinet_ops_ct110_cpu_usage/config",
        "homeassistant/sensor/hubinet_ops_ct110_network_in_bytes/config",
        "homeassistant/sensor/hubinet_ops_ct110_network_out_bytes/config",
    }
    assert cleared_topics == retired_topics


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
        "homeassistant/sensor/hubinet_ops_vm100_cpu_load_1m/config",
        "homeassistant/sensor/hubinet_ops_ct110_cpu_usage/config",
        "homeassistant/sensor/hubinet_ops_ct110_network_in_bytes/config",
        "homeassistant/sensor/hubinet_ops_ct110_network_out_bytes/config",
    }
    assert {item.topic for item in cleared} == expected_topics
    assert len(cleared) == 2 * len(expected_topics)
    assert all(item.retain and item.force for item in cleared)
