from __future__ import annotations

from typing import Any

import pytest

from app.config import validate_config


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
    with pytest.raises(RuntimeError, match="Duplicate container VMID"):
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


def test_example_policy_keeps_ct101_denied_and_ct106_enabled() -> None:
    import yaml
    from pathlib import Path

    raw = yaml.safe_load(Path("config/config.example.yaml").read_text(encoding="utf-8"))
    validate_config(raw)
    ct101 = raw["containers"][101]["operator_capabilities"]
    ct106 = raw["containers"][106]["operator_capabilities"]
    assert ct101 and not any(ct101.values())
    assert ct106 and all(ct106.values())
    assert raw["containers"][101]["recovery_scan"]["enabled"] is False
    assert raw["containers"][106]["recovery_scan"] == {
        "enabled": True,
        "delay_seconds": 90,
        "cooldown_seconds": 900,
    }
