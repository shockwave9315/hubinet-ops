from __future__ import annotations

import json
import inspect
import queue
import re
from pathlib import Path

import pytest
import yaml

from app.ha_entities import (
    AGENT_SELF_ENTITY_SPECS,
    APT_ENTITY_SPECS,
    HA_STATE_MAX_LENGTH,
    QEMU_ENTITY_SPECS,
    ResourceIdentity,
    bounded_ha_state_text,
    normalize_resource_identity,
    obsolete_discovery_keys,
    resource_entity_specs,
    resource_prefix,
)
from app.mqtt import MqttTelemetry
from app.mqtt_budget import HA_ATTRIBUTE_BUDGET_BYTES, bounded_attributes, bounded_state
from app.state import normalize_state
from scripts.generate_ha_dashboard import DEFAULT_CONFIG, build_dashboard, render


PRODUCTION_REGISTRY_FIXTURE = (
    Path(__file__).parent / "fixtures" / "production_entity_registry_0_3_1.yaml"
)


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


def _production_registry() -> dict[str, str]:
    fixture = yaml.safe_load(PRODUCTION_REGISTRY_FIXTURE.read_text(encoding="utf-8"))
    registry: dict[str, str] = {}
    for group in fixture["groups"].values():
        for vmid in group["vmids"]:
            for key, suffix in group["suffixes"].items():
                values = {"vmid": vmid, "key": key, "suffix": suffix}
                registry[group["unique_pattern"].format(**values)] = group[
                    "entity_pattern"
                ].format(**values)
    return registry


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


def test_dashboard_preserves_production_031_ids_and_discovers_new_040_entities() -> None:
    dashboard = render(DEFAULT_CONFIG)
    registry = _production_registry()
    configs, _ = _discovery()
    published = {payload["default_entity_id"] for payload in configs.values()}

    assert "Nie znaleziono encji" not in dashboard
    references = _dashboard_sensor_ids(dashboard)
    assert references <= set(registry.values()) | published
    assert references - set(registry.values()) <= published
    assert registry["hubinet_ops_ct_101_apt_check_ok"] == (
        "sensor.hubinet_ops_ct101_apt_check"
    )
    assert registry["hubinet_ops_ct_101_dpkg_audit_ok"] == (
        "sensor.hubinet_ops_ct101_dpkg_audit"
    )
    assert registry["hubinet_ops_ct_101_packages_remaining_count"] == (
        "sensor.hubinet_ops_ct101_packages_remaining"
    )


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
        "hubinet_ops_ct_101_apt_check_ok": "sensor.hubinet_ops_ct101_apt_check",
        "hubinet_ops_ct_101_dpkg_audit_ok": "sensor.hubinet_ops_ct101_dpkg_audit",
        "hubinet_ops_ct_101_packages_remaining_count": "sensor.hubinet_ops_ct101_packages_remaining",
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
        assert "default(none)" in template or " is none " in template

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


def test_data_size_discovery_uses_gib_without_changing_raw_backend_bytes() -> None:
    state = bounded_state(
        {
            "memory": {"used_bytes": 5_838_413_824, "total_bytes": 34_359_738_368},
            "disk": {"used_bytes": 5_838_413_824, "total_bytes": 34_359_738_368},
            "network": {"in_bytes": 5_838_413_824, "out_bytes": 34_359_738_368},
        }
    )
    assert state["memory"]["used_bytes"] == 5_838_413_824
    assert state["memory"]["total_bytes"] == 34_359_738_368
    assert round(5_838_413_824 / 1_073_741_824, 2) == 5.44
    assert round(34_359_738_368 / 1_073_741_824, 2) == 32.00

    configs, _ = _discovery()
    for topic in (
        "homeassistant/sensor/hubinet_ops_vm100_memory_used_bytes/config",
        "homeassistant/sensor/hubinet_ops_vm100_network_in_bytes/config",
        "homeassistant/sensor/hubinet_ops_ct110_disk_free_bytes/config",
    ):
        payload = configs[topic]
        assert payload["device_class"] == "data_size"
        assert payload["unit_of_measurement"] == "GiB"
        assert payload["state_class"] in {"measurement", "total_increasing"}
        assert "1073741824" in payload["value_template"]


def test_agent_last_refresh_discovery_is_nullable_diagnostic_text() -> None:
    configs, _ = _discovery()
    payload = configs["homeassistant/sensor/hubinet_ops_agent_last_refresh/config"]

    assert "device_class" not in payload
    assert payload["entity_category"] == "diagnostic"
    assert "default('unknown', true)" in payload["value_template"]


@pytest.mark.parametrize("length", [260, 2000])
def test_ha_text_state_helper_enforces_limit_without_mutating_source(
    length: int,
) -> None:
    source = "żółw🙂" * length
    rendered = bounded_ha_state_text(source, "none")

    assert len(rendered) == HA_STATE_MAX_LENGTH
    assert rendered == source[:HA_STATE_MAX_LENGTH]
    assert source.endswith("🙂")
    assert len(source) > HA_STATE_MAX_LENGTH


@pytest.mark.parametrize("value", [None, ""])
def test_ha_text_state_helper_preserves_none_fallback(value: str | None) -> None:
    assert bounded_ha_state_text(value, "none") == "none"


@pytest.mark.parametrize(
    "value",
    (
        "/hubinet-ops/" + "x" * 247,
        "/hubinet-ops/" + "x" * 1987,
        "/hubinet-ops/" + "żółw🙂" * 500,
    ),
)
def test_dashboard_path_ha_state_is_bounded_by_unicode_characters(value: str) -> None:
    rendered = bounded_ha_state_text(value, "none")
    specs = resource_entity_specs(_resources()[106])
    dashboard_spec = next(spec for spec in specs if spec.key == "dashboard_path")

    assert len(value) >= 260
    assert rendered == value[:HA_STATE_MAX_LENGTH]
    assert len(rendered) == HA_STATE_MAX_LENGTH
    assert f"[:{HA_STATE_MAX_LENGTH}]" in dashboard_spec.value_template


def test_dashboard_path_discovery_is_unique_and_preserves_stable_ids() -> None:
    configs, _ = _discovery()
    resources = _resources()

    for vmid, cfg in resources.items():
        prefix = resource_prefix(vmid, cfg)
        topic_suffix = f"hubinet_ops_{prefix}_dashboard_path/config"
        matches = [
            payload
            for topic, payload in configs.items()
            if topic.endswith(topic_suffix)
        ]
        assert len(matches) == 1
        payload = matches[0]
        kind = "vm" if cfg["resource_type"] == "qemu" else "ct"
        assert payload["object_id"] == f"hubinet_ops_{prefix}_dashboard_path"
        assert payload["unique_id"] == f"hubinet_ops_{kind}_{vmid}_dashboard_path"
        assert payload["default_entity_id"] == (
            f"sensor.hubinet_ops_{prefix}_dashboard_path"
        )
        assert payload["name"] == "Dashboard path"
        assert payload["state_topic"] == f"hubinet/ops/resource/{vmid}/state"


def test_discovery_has_no_manual_sensor_outside_entity_specs() -> None:
    source = inspect.getsource(MqttTelemetry.publish_discovery)

    assert source.count("self._discovery_sensor(") == 2
    assert source.count("value_template=spec.value_template") == 2
    assert 'value_template="{{ value_json.' not in source


def test_all_unbounded_diagnostic_entity_states_use_central_ha_limit() -> None:
    bounded_keys = {
        "active_job_id",
        "active_plan_id",
        "active_plan_status",
        "api_health",
        "agent_version",
        "dashboard_path",
        "executor_contract_error",
        "executor_missing_actions",
        "executor_profile_sha256",
        "executor_sha256",
        "executor_version",
        "ip_addresses",
        "guest_agent_status",
        "last_error",
        "last_job_event",
        "last_job_id",
        "last_operation_result",
        "last_recovery_scan_result",
        "latest_snapshot_kind",
        "latest_snapshot_name",
        "lifecycle_action",
        "lifecycle_error",
        "operation_type",
        "profile_validation_status",
        "self_update_release_fingerprint",
        "self_update_release_id",
        "self_update_release_version",
        "service_status",
        "verification_error",
    }
    specs = (
        list(APT_ENTITY_SPECS)
        + list(QEMU_ENTITY_SPECS)
        + list(AGENT_SELF_ENTITY_SPECS)
        + [
            spec
            for cfg in _resources().values()
            for spec in resource_entity_specs(cfg)
        ]
    )
    by_key: dict[str, list[str]] = {}
    for spec in specs:
        by_key.setdefault(spec.key, []).append(spec.value_template)

    assert bounded_keys <= set(by_key)
    for key in bounded_keys:
        assert all(f"[:{HA_STATE_MAX_LENGTH}]" in template for template in by_key[key])


def test_long_diagnostics_remain_full_in_retained_resource_payload() -> None:
    telemetry = MqttTelemetry(
        {"enabled": True, "discovery_prefix": "homeassistant"},
        {106: _resources()[106]},
    )
    diagnostic = "diagnostic-" + "x" * 1989
    secret = "Bearer super-secret-token"
    telemetry.publish_resource_state(
        106,
        {
            "vmid": 106,
            "last_error": diagnostic,
            "verification_error": secret,
        },
    )
    state_item = telemetry._queue.get_nowait()
    attributes_item = telemetry._queue.get_nowait()
    assert state_item is not None
    assert attributes_item is not None
    payload = json.loads(state_item.payload)

    assert payload["last_error"] == diagnostic
    assert len(payload["last_error"]) > HA_STATE_MAX_LENGTH
    assert "super-secret-token" not in state_item.payload
    assert "super-secret-token" not in attributes_item.payload


def test_long_dashboard_path_remains_full_in_retained_resource_payload() -> None:
    telemetry = MqttTelemetry(
        {"enabled": True, "discovery_prefix": "homeassistant"},
        {106: _resources()[106]},
    )
    dashboard_path = "/hubinet-ops/" + "żółw🙂" * 397
    assert len(dashboard_path) == 1998

    telemetry.publish_resource_state(
        106,
        {"vmid": 106, "dashboard_path": dashboard_path},
    )
    state_item = telemetry._queue.get_nowait()
    assert state_item is not None
    payload = json.loads(state_item.payload)

    assert payload["dashboard_path"] == dashboard_path
    assert len(payload["dashboard_path"]) > HA_STATE_MAX_LENGTH


def test_health_discovery_uses_dedicated_attributes_topic_without_force_update() -> None:
    configs, _ = _discovery()

    for prefix, vmid in (("vm", 100), ("ct", 106), ("ct", 110)):
        payload = configs[
            f"homeassistant/sensor/hubinet_ops_{prefix}{vmid}_health_status/config"
        ]
        assert payload["json_attributes_topic"] == (
            f"hubinet/ops/resource/{vmid}/attributes"
        )
        assert payload["json_attributes_topic"] != payload["state_topic"]
        assert "force_update" not in payload


def test_dashboard_attributes_have_an_independent_ten_kib_budget() -> None:
    payload = bounded_attributes(
        {
            "last_refresh": "2026-07-20T18:00:00+00:00",
            "uptime_seconds": 100,
            "cpu": {"usage_percent": 3.0},
            "memory": {"used_bytes": 1},
            "disk": {"used_bytes": 2},
            "network": {"in_bytes": 3},
            "updates": {
                "packages": [
                    {"name": f"package-{index}-" + "x" * 200}
                    for index in range(200)
                ]
            },
            "recent_job_events": [
                {"message": f"event-{index}-" + "y" * 1000}
                for index in range(50)
            ],
            "recent_warnings": ["z" * 500 for _ in range(20)],
        }
    )

    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    assert len(encoded) <= HA_ATTRIBUTE_BUDGET_BYTES
    assert set(payload) <= {
        "updates",
        "recent_job_events",
        "recent_warnings",
        "attribute_payload",
        "failed_units",
    }
    for forbidden in (
        "last_refresh",
        "uptime_seconds",
        "cpu",
        "memory",
        "disk",
        "network",
        "health_score",
        "runtime_status",
    ):
        assert forbidden not in payload


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
                "cpu": {"usage": 0.0305257, "cores": 4},
            }
        )
    )
    assert state["cpu"]["usage"] == 0.0305257
    assert state["cpu"]["usage_percent"] == 3.053

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
