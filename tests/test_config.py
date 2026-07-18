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


def test_repair_actions_must_be_a_supported_list() -> None:
    config = base_config()
    config["containers"][106]["repair_actions"] = "restart_services"
    with pytest.raises(RuntimeError, match="repair_actions must be a list"):
        validate_config(config)

    config["containers"][106]["repair_actions"] = ["arbitrary_shell"]
    with pytest.raises(RuntimeError, match="unsupported repair action"):
        validate_config(config)
