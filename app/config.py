from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

ALLOWED_REPAIR_ACTIONS = {"restart_services", "restart_required_containers"}


@dataclass(frozen=True)
class Settings:
    raw: dict[str, Any]
    config_path: Path
    db_path: Path
    api_token: str

    @property
    def api(self) -> dict[str, Any]:
        return self.raw.get("api", {})

    @property
    def executor(self) -> dict[str, Any]:
        return self.raw.get("executor", {})

    @property
    def scheduler(self) -> dict[str, Any]:
        return self.raw.get("scheduler", {})

    @property
    def home_assistant(self) -> dict[str, Any]:
        return self.raw.get("home_assistant", {})

    @property
    def mqtt(self) -> dict[str, Any]:
        return self.raw.get("mqtt", {})

    @property
    def containers(self) -> dict[int, dict[str, Any]]:
        source = self.raw.get("containers", {})
        return {int(k): dict(v) for k, v in source.items()}


def load_settings() -> Settings:
    config_path = Path(os.environ.get("HUBINET_OPS_CONFIG", "/etc/hubinet-ops/config.yaml"))
    db_path = Path(os.environ.get("HUBINET_OPS_DB", "/var/lib/hubinet-ops/ops.db"))
    api_token = os.environ.get("HUBINET_OPS_API_TOKEN", "").strip()

    if not config_path.exists():
        raise RuntimeError(f"Config file not found: {config_path}")
    if len(api_token) < 32:
        raise RuntimeError("HUBINET_OPS_API_TOKEN must contain at least 32 characters")

    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise RuntimeError(f"Invalid YAML configuration: {exc}") from exc
    if not isinstance(raw, dict):
        raise RuntimeError("Top-level YAML config must be an object")

    validate_config(raw)

    db_path.parent.mkdir(parents=True, exist_ok=True)
    return Settings(raw=raw, config_path=config_path, db_path=db_path, api_token=api_token)


def validate_config(raw: dict[str, Any]) -> None:
    containers = raw.get("containers", {})
    if not isinstance(containers, dict) or not containers:
        raise RuntimeError("containers must be a non-empty object")

    normalized_vmids: set[int] = set()
    for raw_vmid, value in containers.items():
        try:
            vmid = int(raw_vmid)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Container VMIDs must be integers") from exc
        if vmid <= 0 or not isinstance(value, dict):
            raise RuntimeError(f"Invalid container configuration for VMID {raw_vmid}")
        if vmid in normalized_vmids:
            raise RuntimeError(f"Duplicate container VMID after normalization: {vmid}")
        normalized_vmids.add(vmid)

        actions_raw = value.get("repair_actions") or []
        if not isinstance(actions_raw, list):
            raise RuntimeError(f"CT{vmid} repair_actions must be a list")
        actions = {str(action) for action in actions_raw}
        if not actions <= ALLOWED_REPAIR_ACTIONS:
            raise RuntimeError(f"CT{vmid} contains an unsupported repair action")

        stabilization = value.get("stabilization") or {}
        if not isinstance(stabilization, dict):
            raise RuntimeError(f"CT{vmid} stabilization must be an object")
        for key in (
            "post_update_timeout_seconds",
            "post_rollback_timeout_seconds",
            "repair_timeout_seconds",
            "poll_interval_seconds",
        ):
            if key in stabilization:
                number = _finite_float(stabilization[key], f"CT{vmid} stabilization.{key}")
                if number <= 0:
                    raise RuntimeError(f"CT{vmid} stabilization.{key} must be positive")
        if "initial_grace_seconds" in stabilization:
            grace = _finite_float(
                stabilization["initial_grace_seconds"],
                f"CT{vmid} stabilization.initial_grace_seconds",
            )
            if grace < 0:
                raise RuntimeError(f"CT{vmid} stabilization.initial_grace_seconds cannot be negative")
        if "required_consecutive_successes" in stabilization:
            successes = _strict_int(
                stabilization["required_consecutive_successes"],
                f"CT{vmid} stabilization.required_consecutive_successes",
            )
            if successes <= 0:
                raise RuntimeError(
                    f"CT{vmid} stabilization.required_consecutive_successes must be positive"
                )

    mqtt = raw.get("mqtt") or {}
    if not isinstance(mqtt, dict):
        raise RuntimeError("mqtt must be an object")
    if mqtt.get("enabled") and not str(mqtt.get("host", "")).strip():
        raise RuntimeError("mqtt.host is required when MQTT is enabled")

    port = _strict_int(mqtt.get("port", 1883), "mqtt.port")
    if port < 1 or port > 65535:
        raise RuntimeError("mqtt.port must be between 1 and 65535")
    keepalive = _strict_int(mqtt.get("keepalive_seconds", 60), "mqtt.keepalive_seconds")
    if keepalive <= 0:
        raise RuntimeError("mqtt.keepalive_seconds must be positive")
    reconnect_min = _strict_int(
        mqtt.get("reconnect_min_seconds", 2), "mqtt.reconnect_min_seconds"
    )
    reconnect_max = _strict_int(
        mqtt.get("reconnect_max_seconds", 60), "mqtt.reconnect_max_seconds"
    )
    if reconnect_min <= 0 or reconnect_max <= 0:
        raise RuntimeError("MQTT reconnect delays must be positive")
    if reconnect_min > reconnect_max:
        raise RuntimeError("mqtt.reconnect_min_seconds cannot exceed reconnect_max_seconds")


def _finite_float(value: Any, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{name} must be a number") from exc
    if not math.isfinite(result):
        raise RuntimeError(f"{name} must be finite")
    return result


def _strict_int(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise RuntimeError(f"{name} must be an integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if isinstance(value, float) and not value.is_integer():
        raise RuntimeError(f"{name} must be an integer")
    if isinstance(value, str):
        text = value.strip()
        if not text or text.lstrip("+-").isdigit():
            raise RuntimeError(f"{name} must be an integer")
    return result
