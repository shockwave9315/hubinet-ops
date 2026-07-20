from __future__ import annotations

import math
import os
import re
from ipaddress import ip_address
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

ALLOWED_REPAIR_ACTIONS = {"restart_services", "restart_required_containers"}
OPERATOR_CAPABILITIES = {
    "refresh",
    "scan",
    "approve",
    "reject",
    "retry_healthcheck",
    "rollback",
    "start",
    "shutdown",
    "reboot",
    "force_stop",
    "snapshot_create",
    "snapshot_list",
    "snapshot_rollback",
    "snapshot_delete",
    "self_update",
}
RECOVERY_SCAN_KEYS = {"enabled", "delay_seconds", "cooldown_seconds"}
MONITORING_KEYS = {"inspect", "update_scan"}
RESOURCE_TYPES = {"lxc", "qemu"}
RESOURCE_ADAPTERS = {"apt", "haos", "agent_self"}
RESOURCE_KEYS = {
    "resource_type",
    "adapter",
    "name",
    "display_name",
    "enabled",
    "criticality",
    "ip_address",
    "guest_agent",
    "monitoring",
    "operator_capabilities",
    "approval_mode",
    "automatic_rollback",
    "manual_rollback_allowed",
    "recovery_scan",
    "repair_actions",
    "dashboard_path",
    "stabilization",
    "required_services",
    "docker",
    "os",
    "executor_contract",
    "snapshot_retention",
}


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
    def monitoring_scheduler(self) -> dict[str, Any]:
        return self.raw.get("monitoring_scheduler", {})

    @property
    def home_assistant(self) -> dict[str, Any]:
        return self.raw.get("home_assistant", {})

    @property
    def mqtt(self) -> dict[str, Any]:
        return self.raw.get("mqtt", {})

    @property
    def host_control(self) -> dict[str, Any]:
        return self.raw.get("host_control", {})

    @property
    def resources(self) -> dict[int, dict[str, Any]]:
        return _normalized_resources(self.raw)

    @property
    def containers(self) -> dict[int, dict[str, Any]]:
        """0.3.x compatibility alias exposing only Proxmox LXC resources."""
        return {
            vmid: cfg
            for vmid, cfg in self.resources.items()
            if cfg.get("resource_type") == "lxc"
        }


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
    normalized_raw = dict(raw)
    normalized_raw.pop("containers", None)
    normalized_raw["resources"] = _normalized_resources(raw)

    db_path.parent.mkdir(parents=True, exist_ok=True)
    return Settings(
        raw=normalized_raw,
        config_path=config_path,
        db_path=db_path,
        api_token=api_token,
    )


def validate_config(raw: dict[str, Any]) -> None:
    if "resources" in raw and "containers" in raw:
        raise RuntimeError("resources and legacy containers cannot be configured together")
    legacy = "containers" in raw
    source_name = "containers" if legacy else "resources"
    resources = raw.get(source_name, {})
    if not isinstance(resources, dict) or not resources:
        raise RuntimeError("resources must be a non-empty object")

    normalized_vmids: set[int] = set()
    for raw_vmid, value in resources.items():
        try:
            vmid = _strict_int(raw_vmid, "Resource VMID")
        except RuntimeError as exc:
            raise RuntimeError("Resource VMIDs must be integers") from exc
        if vmid <= 0 or not isinstance(value, dict):
            raise RuntimeError(f"Invalid resource configuration for VMID {raw_vmid}")
        if vmid in normalized_vmids:
            raise RuntimeError(f"Duplicate resource VMID after normalization: {vmid}")
        normalized_vmids.add(vmid)

        unknown_keys = set(value) - RESOURCE_KEYS
        if unknown_keys:
            raise RuntimeError(
                f"Resource {vmid} contains unknown settings: "
                f"{', '.join(sorted(str(item) for item in unknown_keys))}"
            )

        resource_type = str(value.get("resource_type", "lxc" if legacy else ""))
        adapter = str(value.get("adapter", "apt" if legacy else ""))
        if resource_type not in RESOURCE_TYPES:
            raise RuntimeError(f"Resource {vmid} has unsupported resource_type: {resource_type}")
        if adapter not in RESOURCE_ADAPTERS:
            raise RuntimeError(f"Resource {vmid} has unsupported adapter: {adapter}")
        if adapter == "apt" and resource_type != "lxc":
            raise RuntimeError(f"Resource {vmid}: apt adapter is supported only for LXC")
        if adapter == "haos" and resource_type != "qemu":
            raise RuntimeError(f"Resource {vmid}: haos adapter is supported only for QEMU")
        if adapter == "agent_self" and (resource_type != "lxc" or vmid != 110):
            raise RuntimeError(
                "Resource adapter agent_self is allowed only for the agent resource VMID 110"
            )
        for key in ("name", "display_name"):
            if key in value and (
                not isinstance(value[key], str) or not value[key].strip()
            ):
                raise RuntimeError(f"Resource {vmid} {key} must be a non-empty string")
        if value.get("criticality", "medium") not in {"critical", "high", "medium", "low"}:
            raise RuntimeError(f"Resource {vmid} criticality is unsupported")
        if value.get("approval_mode", "always") != "always":
            raise RuntimeError(f"Resource {vmid} approval_mode must be always")
        if "ip_address" in value:
            try:
                ip_address(str(value["ip_address"]))
            except ValueError as exc:
                raise RuntimeError(f"Resource {vmid} ip_address is invalid") from exc
        if "dashboard_path" in value and (
            not isinstance(value["dashboard_path"], str)
            or not value["dashboard_path"].startswith("/hubinet-ops/")
        ):
            raise RuntimeError(f"Resource {vmid} dashboard_path is invalid")
        if "enabled" in value and not isinstance(value["enabled"], bool):
            raise RuntimeError(f"Resource {vmid} enabled must be a boolean")
        if "guest_agent" in value and not isinstance(value["guest_agent"], bool):
            raise RuntimeError(f"Resource {vmid} guest_agent must be a boolean")
        if resource_type != "qemu" and "guest_agent" in value:
            raise RuntimeError(f"Resource {vmid} guest_agent is supported only for QEMU")

        capabilities = value.get("operator_capabilities", {})
        if not isinstance(capabilities, dict):
            raise RuntimeError(f"Resource {vmid} operator_capabilities must be an object")
        unknown_capabilities = set(capabilities) - OPERATOR_CAPABILITIES
        if unknown_capabilities:
            raise RuntimeError(
                f"Resource {vmid} contains unknown operator capabilities: "
                f"{', '.join(sorted(str(item) for item in unknown_capabilities))}"
            )
        for capability, allowed in capabilities.items():
            if not isinstance(allowed, bool):
                raise RuntimeError(
                    f"Resource {vmid} operator_capabilities.{capability} must be a boolean"
                )

        monitoring_default = _legacy_monitoring_default(value)
        monitoring = value.get("monitoring", monitoring_default if legacy else None)
        if not isinstance(monitoring, dict):
            raise RuntimeError(f"Resource {vmid} monitoring must be an object")
        unknown_monitoring = set(monitoring) - MONITORING_KEYS
        if unknown_monitoring:
            raise RuntimeError(
                f"Resource {vmid} contains unknown monitoring settings: "
                f"{', '.join(sorted(str(item) for item in unknown_monitoring))}"
            )
        for key in MONITORING_KEYS:
            if key not in monitoring and not legacy:
                raise RuntimeError(f"Resource {vmid} monitoring.{key} is required")
            if key in monitoring and not isinstance(monitoring[key], bool):
                raise RuntimeError(f"Resource {vmid} monitoring.{key} must be a boolean")

        if adapter == "haos":
            forbidden = {
                name
                for name in (
                    "scan",
                    "approve",
                    "reject",
                    "retry_healthcheck",
                    "rollback",
                    "start",
                    "shutdown",
                    "reboot",
                )
                if bool(capabilities.get(name, False))
            }
            if forbidden:
                raise RuntimeError(
                    f"Resource {vmid} adapter {adapter} does not support operator actions: "
                    f"{', '.join(sorted(forbidden))}"
                )
            if bool(monitoring.get("update_scan", False)):
                raise RuntimeError(
                    f"Resource {vmid} adapter {adapter} does not support APT update scans"
                )
        if adapter == "agent_self":
            forbidden = {
                name
                for name in ("scan", "approve", "reject", "retry_healthcheck", "rollback")
                if bool(capabilities.get(name, False))
            }
            if forbidden:
                raise RuntimeError(
                    "Resource 110 agent_self supports only host lifecycle, snapshot, refresh, "
                    "and self-update capabilities"
                )

        actions_raw = value.get("repair_actions") or []
        if not isinstance(actions_raw, list):
            raise RuntimeError(f"Resource {vmid} repair_actions must be a list")
        actions = {str(action) for action in actions_raw}
        if not actions <= ALLOWED_REPAIR_ACTIONS:
            raise RuntimeError(f"Resource {vmid} contains an unsupported repair action")
        if adapter != "apt" and actions:
            raise RuntimeError(f"Resource {vmid} adapter {adapter} cannot configure repair_actions")

        executor_contract = value.get("executor_contract", {})
        if not isinstance(executor_contract, dict):
            raise RuntimeError(f"Resource {vmid} executor_contract must be an object")
        unknown_contract = set(executor_contract) - {
            "executor_sha256", "profile_sha256"
        }
        if unknown_contract:
            raise RuntimeError(f"Resource {vmid} executor_contract contains unknown settings")
        for key in ("executor_sha256", "profile_sha256"):
            if key in executor_contract and not _sha256(executor_contract[key]):
                raise RuntimeError(f"Resource {vmid} executor_contract.{key} must be SHA-256")
        if executor_contract and adapter != "apt":
            raise RuntimeError(f"Resource {vmid} executor_contract is supported only for apt")
        retention = _strict_int(
            value.get("snapshot_retention", 5),
            f"Resource {vmid} snapshot_retention",
        )
        if retention < 1 or retention > 100:
            raise RuntimeError(f"Resource {vmid} snapshot_retention must be between 1 and 100")

        required_services = value.get("required_services", [])
        if not isinstance(required_services, list) or not all(
            isinstance(item, str) and item.strip() for item in required_services
        ):
            raise RuntimeError(f"Resource {vmid} required_services must be a list of names")
        docker = value.get("docker", {})
        if not isinstance(docker, dict):
            raise RuntimeError(f"Resource {vmid} docker must be an object")
        unknown_docker = set(docker) - {"enabled", "require_health", "required"}
        if unknown_docker:
            raise RuntimeError(f"Resource {vmid} contains unknown docker settings")
        for key in ("enabled", "require_health"):
            if key in docker and not isinstance(docker[key], bool):
                raise RuntimeError(f"Resource {vmid} docker.{key} must be a boolean")
        required_docker = docker.get("required", [])
        if not isinstance(required_docker, list) or not all(
            isinstance(item, str) and item.strip() for item in required_docker
        ):
            raise RuntimeError(f"Resource {vmid} docker.required must be a list of names")
        if resource_type == "qemu" and required_services:
            raise RuntimeError(f"Resource {vmid} QEMU cannot configure required_services")
        if adapter != "apt" and bool(docker.get("enabled", False)):
            raise RuntimeError(f"Resource {vmid} adapter {adapter} cannot enable Docker checks")

        recovery = value.get("recovery_scan", {})
        if not isinstance(recovery, dict):
            raise RuntimeError(f"Resource {vmid} recovery_scan must be an object")
        unknown_recovery = set(recovery) - RECOVERY_SCAN_KEYS
        if unknown_recovery:
            raise RuntimeError(
                f"Resource {vmid} contains unknown recovery_scan settings: "
                f"{', '.join(sorted(str(item) for item in unknown_recovery))}"
            )
        enabled = recovery.get("enabled", False)
        if not isinstance(enabled, bool):
            raise RuntimeError(f"Resource {vmid} recovery_scan.enabled must be a boolean")
        if enabled and not bool(monitoring.get("update_scan", False)):
            raise RuntimeError(
                f"Resource {vmid} recovery_scan.enabled requires monitoring.update_scan"
            )
        delay = _strict_int(
            recovery.get("delay_seconds", 90),
            f"Resource {vmid} recovery_scan.delay_seconds",
        )
        cooldown = _strict_int(
            recovery.get("cooldown_seconds", max(900, delay)),
            f"Resource {vmid} recovery_scan.cooldown_seconds",
        )
        if delay < 1 or delay > 3600:
            raise RuntimeError(
                f"Resource {vmid} recovery_scan.delay_seconds must be between 1 and 3600"
            )
        if cooldown < delay or cooldown > 604800:
            raise RuntimeError(
                f"Resource {vmid} recovery_scan.cooldown_seconds must be between delay_seconds and 604800"
            )

        for key in ("automatic_rollback", "manual_rollback_allowed"):
            if key in value and not isinstance(value[key], bool):
                raise RuntimeError(f"Resource {vmid} {key} must be a boolean")
        if bool(capabilities.get("rollback", False)) and not bool(
            value.get("manual_rollback_allowed", False)
        ):
            raise RuntimeError(
                f"Resource {vmid} rollback capability requires manual_rollback_allowed"
            )
        if adapter != "apt" and (
            bool(value.get("automatic_rollback", False))
            or bool(value.get("manual_rollback_allowed", False))
            or enabled
        ):
            raise RuntimeError(
                f"Resource {vmid} adapter {adapter} cannot use update recovery or rollback"
            )

        stabilization = value.get("stabilization") or {}
        if not isinstance(stabilization, dict):
            raise RuntimeError(f"Resource {vmid} stabilization must be an object")
        for key in (
            "post_update_timeout_seconds",
            "post_rollback_timeout_seconds",
            "repair_timeout_seconds",
            "poll_interval_seconds",
        ):
            if key in stabilization:
                number = _finite_float(
                    stabilization[key], f"Resource {vmid} stabilization.{key}"
                )
                if number <= 0:
                    raise RuntimeError(
                        f"Resource {vmid} stabilization.{key} must be positive"
                    )
        if "initial_grace_seconds" in stabilization:
            grace = _finite_float(
                stabilization["initial_grace_seconds"],
                f"Resource {vmid} stabilization.initial_grace_seconds",
            )
            if grace < 0:
                raise RuntimeError(
                    f"Resource {vmid} stabilization.initial_grace_seconds cannot be negative"
                )
        if "required_consecutive_successes" in stabilization:
            successes = _strict_int(
                stabilization["required_consecutive_successes"],
                f"Resource {vmid} stabilization.required_consecutive_successes",
            )
            if successes <= 0:
                raise RuntimeError(
                    f"Resource {vmid} stabilization.required_consecutive_successes must be positive"
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

    host_control = raw.get("host_control") or {}
    if not isinstance(host_control, dict):
        raise RuntimeError("host_control must be an object")
    unknown_host_control = set(host_control) - {
        "enabled", "base_url", "token_env", "timeout_seconds",
        "operation_timeout_seconds", "poll_interval_seconds",
    }
    if unknown_host_control:
        raise RuntimeError("host_control contains unknown settings")
    if "token" in host_control:
        raise RuntimeError("host_control bearer token must be provided through the environment")
    if host_control.get("enabled"):
        parsed = urlsplit(str(host_control.get("base_url") or ""))
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise RuntimeError("host_control.base_url must be an HTTP(S) URL")
        token_env = str(host_control.get("token_env") or "HUBINET_OPS_HOSTD_TOKEN")
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{2,127}", token_env):
            raise RuntimeError("host_control.token_env is invalid")
    for key, default in (
        ("timeout_seconds", 30),
        ("operation_timeout_seconds", 1800),
    ):
        if _strict_int(host_control.get(key, default), f"host_control.{key}") <= 0:
            raise RuntimeError(f"host_control.{key} must be positive")
    if _finite_float(
        host_control.get("poll_interval_seconds", 1),
        "host_control.poll_interval_seconds",
    ) <= 0:
        raise RuntimeError("host_control.poll_interval_seconds must be positive")

    monitoring_scheduler = raw.get("monitoring_scheduler", {})
    if not isinstance(monitoring_scheduler, dict):
        raise RuntimeError("monitoring_scheduler must be an object")
    unknown_scheduler = set(monitoring_scheduler) - {
        "enabled",
        "scan_interval_minutes",
        "initial_scan_delay_seconds",
    }
    if unknown_scheduler:
        raise RuntimeError(
            "monitoring_scheduler contains unknown settings: "
            f"{', '.join(sorted(str(item) for item in unknown_scheduler))}"
        )
    if "enabled" in monitoring_scheduler and not isinstance(
        monitoring_scheduler["enabled"], bool
    ):
        raise RuntimeError("monitoring_scheduler.enabled must be a boolean")
    for key, default in (
        ("scan_interval_minutes", 360),
        ("initial_scan_delay_seconds", 60),
    ):
        value = _strict_int(
            monitoring_scheduler.get(key, default),
            f"monitoring_scheduler.{key}",
        )
        if value < 1:
            raise RuntimeError(f"monitoring_scheduler.{key} must be positive")


def _normalized_resources(raw: dict[str, Any]) -> dict[int, dict[str, Any]]:
    if "resources" in raw and "containers" in raw:
        raise RuntimeError("resources and legacy containers cannot be configured together")
    legacy = "containers" in raw
    source = raw.get("containers" if legacy else "resources", {})
    if not isinstance(source, dict):
        return {}
    normalized: dict[int, dict[str, Any]] = {}
    for raw_vmid, raw_cfg in source.items():
        vmid = _strict_int(raw_vmid, "Resource VMID")
        cfg = dict(raw_cfg) if isinstance(raw_cfg, dict) else {}
        cfg.setdefault("resource_type", "lxc")
        cfg.setdefault("adapter", "apt")
        if legacy:
            cfg.setdefault("monitoring", _legacy_monitoring_default(cfg))
        normalized[vmid] = cfg
    return normalized


def _legacy_monitoring_default(resource: dict[str, Any]) -> dict[str, bool]:
    capabilities = resource.get("operator_capabilities")
    capability_map = capabilities if isinstance(capabilities, dict) else {}
    recovery = resource.get("recovery_scan")
    recovery_enabled = bool(
        isinstance(recovery, dict) and recovery.get("enabled", False)
    )
    return {
        "inspect": True,
        "update_scan": bool(capability_map.get("scan", False)) or recovery_enabled,
    }


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
        if not text or not text.lstrip("+-").isdigit():
            raise RuntimeError(f"{name} must be an integer")
    return result


def _sha256(value: Any) -> bool:
    return bool(re.fullmatch(r"[a-f0-9]{64}", str(value or "")))
