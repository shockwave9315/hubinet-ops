from __future__ import annotations

from typing import Any

import pytest

from app.config import Settings, validate_config


def base_config() -> dict[str, Any]:
    return {
        "containers": {
            106: {
                "enabled": True,
                "repair_actions": [],
                "stabilization": {},
            }
        },
        "mqtt": {"enabled": False},
    }


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("post_update_timeout_seconds", "nope"),
        ("post_rollback_timeout_seconds", None),
        ("repair_timeout_seconds", float("nan")),
        ("poll_interval_seconds", 0),
        ("required_consecutive_successes", "1.5"),
        ("required_consecutive_successes", True),
    ],
)
def test_invalid_stabilization_values_raise_runtime_error(key: str, value: Any) -> None:
    config = base_config()
    config["containers"][106]["stabilization"][key] = value
    with pytest.raises(RuntimeError, match=f"stabilization.{key}"):
        validate_config(config)


def test_zero_initial_grace_is_allowed_but_negative_is_not() -> None:
    config = base_config()
    config["containers"][106]["stabilization"]["initial_grace_seconds"] = 0
    validate_config(config)

    config["containers"][106]["stabilization"]["initial_grace_seconds"] = -1
    with pytest.raises(RuntimeError, match="initial_grace_seconds"):
        validate_config(config)


@pytest.mark.parametrize("port", ["abc", 0, 65536, 1883.5, True])
def test_invalid_mqtt_port_is_descriptive(port: Any) -> None:
    config = base_config()
    config["mqtt"]["port"] = port
    with pytest.raises(RuntimeError, match="mqtt.port"):
        validate_config(config)


def test_quoted_integer_mqtt_settings_are_accepted() -> None:
    config = base_config()
    config["mqtt"] = {
        "enabled": True,
        "host": "broker.test",
        "port": "1883",
        "keepalive_seconds": "60",
        "reconnect_min_seconds": "2",
        "reconnect_max_seconds": "60",
    }
    validate_config(config)


def test_enabled_mqtt_requires_host_and_valid_reconnect_range() -> None:
    config = base_config()
    config["mqtt"] = {"enabled": True, "host": ""}
    with pytest.raises(RuntimeError, match="mqtt.host"):
        validate_config(config)

    config["mqtt"] = {
        "enabled": True,
        "host": "broker.test",
        "reconnect_min_seconds": 10,
        "reconnect_max_seconds": 2,
    }
    with pytest.raises(RuntimeError, match="cannot exceed"):
        validate_config(config)


def test_duplicate_normalized_vmids_are_rejected() -> None:
    config = base_config()
    config["containers"] = {106: {"enabled": True}, "106": {"enabled": True}}
    with pytest.raises(RuntimeError, match="Duplicate resource VMID"):
        validate_config(config)


@pytest.mark.parametrize("vmid", [True, 106.5, "106.5", 0, -1])
def test_invalid_vmids_are_rejected(vmid: Any) -> None:
    config = base_config()
    config["containers"] = {vmid: {"enabled": True}}
    with pytest.raises(RuntimeError, match="VMID|Invalid container"):
        validate_config(config)


def test_repair_actions_must_be_a_supported_list() -> None:
    config = base_config()
    config["containers"][106]["repair_actions"] = "restart_services"
    with pytest.raises(RuntimeError, match="repair_actions must be a list"):
        validate_config(config)

    config["containers"][106]["repair_actions"] = ["arbitrary_shell"]
    with pytest.raises(RuntimeError, match="unsupported repair action"):
        validate_config(config)


def test_operator_capabilities_and_recovery_scan_are_strictly_validated() -> None:
    config = base_config()
    config["containers"][106]["operator_capabilities"] = {
        "refresh": True,
        "scan": True,
    }
    config["containers"][106]["recovery_scan"] = {
        "enabled": True,
        "delay_seconds": 90,
        "cooldown_seconds": 900,
    }
    validate_config(config)

    config["containers"][106]["operator_capabilities"]["shell"] = True
    with pytest.raises(RuntimeError, match="unknown operator capabilities"):
        validate_config(config)

    del config["containers"][106]["operator_capabilities"]["shell"]
    config["containers"][106]["operator_capabilities"]["scan"] = 1
    with pytest.raises(RuntimeError, match="must be a boolean"):
        validate_config(config)

    config["containers"][106]["operator_capabilities"] = []
    with pytest.raises(RuntimeError, match="must be an object"):
        validate_config(config)


@pytest.mark.parametrize(
    ("recovery", "message"),
    [
        ({"enabled": "yes"}, "enabled must be a boolean"),
        ({"enabled": True, "delay_seconds": 0}, "delay_seconds"),
        ({"enabled": True, "delay_seconds": 90, "cooldown_seconds": 89}, "cooldown_seconds"),
        ({"enabled": True, "unknown": 1}, "unknown recovery_scan"),
    ],
)
def test_invalid_recovery_scan_settings_are_rejected(
    recovery: dict[str, Any],
    message: str,
) -> None:
    config = base_config()
    config["containers"][106]["recovery_scan"] = recovery
    with pytest.raises(RuntimeError, match=message):
        validate_config(config)


def test_recovery_delay_above_default_cooldown_uses_safe_dynamic_default() -> None:
    config = base_config()
    config["containers"][106]["recovery_scan"] = {
        "enabled": True,
        "delay_seconds": 1200,
    }
    validate_config(config)


def test_example_policy_enables_full_lxc_control_and_keeps_vm100_denied() -> None:
    import yaml
    from pathlib import Path

    raw = yaml.safe_load(Path("config/config.example.yaml").read_text(encoding="utf-8"))
    validate_config(raw)
    vm100 = raw["resources"][100]["operator_capabilities"]
    ct101 = raw["resources"][101]["operator_capabilities"]
    ct110 = raw["resources"][110]["operator_capabilities"]
    assert vm100 and not any(vm100.values())
    assert all(value for name, value in ct101.items() if name != "self_update")
    assert ct101["self_update"] is False
    assert ct110["start"] is True
    assert ct110["snapshot_rollback"] is True
    assert ct110["self_update"] is True
    assert ct110["approve"] is True
    assert ct110["reject"] is True
    assert raw["resources"][101]["recovery_scan"]["enabled"] is False
    assert raw["resources"][106]["recovery_scan"] == {
        "enabled": True,
        "delay_seconds": 90,
        "cooldown_seconds": 900,
    }


def resource_config(**overrides: Any) -> dict[str, Any]:
    resource = {
        "resource_type": "lxc",
        "adapter": "apt",
        "enabled": True,
        "monitoring": {"inspect": True, "update_scan": True},
        "operator_capabilities": {},
    }
    resource.update(overrides)
    return {"resources": {101: resource}, "mqtt": {"enabled": False}}


def test_legacy_containers_are_exposed_as_lxc_resources_without_file_rewrite(
    tmp_path: Path,
) -> None:
    raw = {"containers": {101: {"enabled": True}}, "mqtt": {"enabled": False}}
    validate_config(raw)
    settings = Settings(
        raw=raw,
        config_path=tmp_path / "config.yaml",
        db_path=tmp_path / "ops.db",
        api_token="x" * 64,
    )

    assert settings.resources[101]["resource_type"] == "lxc"
    assert settings.resources[101]["adapter"] == "apt"
    assert settings.containers == settings.resources
    assert "resources" not in raw


def test_legacy_recovery_scan_uses_same_monitoring_default_during_validation_and_load(
    tmp_path: Path,
) -> None:
    raw = {
        "containers": {
            106: {
                "enabled": True,
                "operator_capabilities": {"scan": False},
                "recovery_scan": {
                    "enabled": True,
                    "delay_seconds": 90,
                    "cooldown_seconds": 900,
                },
            }
        },
        "mqtt": {"enabled": False},
    }

    validate_config(raw)
    loaded = Settings(
        raw=raw,
        config_path=tmp_path / "config.yaml",
        db_path=tmp_path / "ops.db",
        api_token="x" * 64,
    )

    assert loaded.resources[106]["monitoring"] == {
        "inspect": True,
        "update_scan": True,
    }


def test_production_monitoring_scheduler_is_enabled_for_observation_scans() -> None:
    import yaml
    from pathlib import Path

    raw = yaml.safe_load(Path("config/config.example.yaml").read_text(encoding="utf-8"))
    validate_config(raw)

    assert raw["monitoring_scheduler"] == {
        "enabled": True,
        "scan_interval_minutes": 360,
        "initial_scan_delay_seconds": 60,
    }
    for vmid in (101, 102, 103, 104, 105, 107, 108, 109):
        assert raw["resources"][vmid]["monitoring"]["update_scan"] is True
        assert raw["resources"][vmid]["operator_capabilities"]["scan"] is True
    assert raw["resources"][100]["monitoring"]["update_scan"] is False
    assert raw["resources"][110]["monitoring"]["update_scan"] is False


def test_resources_and_containers_conflict_fails_closed() -> None:
    with pytest.raises(RuntimeError, match="cannot be configured together"):
        validate_config({"resources": {101: {}}, "containers": {101: {}}})


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"resource_type": "container"}, "resource_type"),
        ({"resource_type": "qemu", "adapter": "apt"}, "only for LXC"),
        ({"resource_type": "lxc", "adapter": "haos"}, "only for QEMU"),
        ({"guest_agent": True}, "only for QEMU"),
        ({"resource_type": "qemu", "adapter": "haos", "monitoring": {"inspect": True, "update_scan": False}, "required_services": ["x"]}, "required_services"),
        ({"resource_type": "qemu", "adapter": "haos", "monitoring": {"inspect": True, "update_scan": False}, "docker": {"enabled": True}}, "Docker"),
        ({"ip_address": "not-an-ip"}, "ip_address"),
        ({"criticality": "urgent"}, "criticality"),
        ({"approval_mode": "automatic"}, "approval_mode"),
        ({"dashboard_path": "/other/path"}, "dashboard_path"),
    ],
)
def test_resource_type_adapter_combinations_fail_closed(
    overrides: dict[str, Any],
    message: str,
) -> None:
    with pytest.raises(RuntimeError, match=message):
        validate_config(resource_config(**overrides))


def test_recovery_scan_requires_monitoring_update_scan() -> None:
    config = resource_config(
        monitoring={"inspect": True, "update_scan": False},
        recovery_scan={"enabled": True, "delay_seconds": 90, "cooldown_seconds": 900},
    )
    with pytest.raises(RuntimeError, match="requires monitoring.update_scan"):
        validate_config(config)
