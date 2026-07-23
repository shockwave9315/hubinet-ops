from __future__ import annotations

import json
from typing import Any

from .security import sanitize_data, sanitize_text

HA_ATTRIBUTE_BUDGET_BYTES = 10_000
HA_ATTRIBUTE_FIELDS = (
    "updates",
    "recent_job_events",
    "recent_warnings",
    "attribute_payload",
    "failed_units",
)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _json_bytes(value: Any) -> int:
    return len(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _sequence(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _package_preview(value: Any) -> dict[str, Any]:
    package = value if isinstance(value, dict) else {"name": value}
    preview: dict[str, Any] = {}
    for key in ("name", "current", "target"):
        if key in package:
            preview[key] = sanitize_text(package.get(key), limit=256)
    return preview


def _event_preview(value: Any) -> dict[str, Any]:
    event = value if isinstance(value, dict) else {"message": value}
    preview: dict[str, Any] = {}
    for key, limit in (
        ("created_at", 64),
        ("stage", 64),
        ("level", 32),
        ("event_type", 64),
        ("message", 1000),
    ):
        if key in event:
            preview[key] = sanitize_text(event.get(key), limit=limit)
    if "progress" in event:
        preview["progress"] = max(0, min(100, _safe_int(event.get("progress"), 0)))
    return preview


def _compact_state_base(state: dict[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {}
    for key, item in state.items():
        if key in {
            "recent_job_events", "updates", "failed_units", "ip_addresses",
            "recent_warnings",
        }:
            continue
        if item is None or isinstance(item, (bool, int, float)):
            compact[key] = item
        elif isinstance(item, str):
            compact[key] = sanitize_text(item, limit=2000 if key == "last_error" else 512)

    for key, fields in {
        "disk": (
            "used_percent",
            "free_mb",
            "used_bytes",
            "total_bytes",
            "free_bytes",
        ),
        "memory": (
            "used_percent",
            "used_bytes",
            "total_bytes",
            "available_bytes",
        ),
        "docker": ("enabled", "available", "required_healthy", "required_total"),
        "cpu": ("usage", "usage_percent", "cores", "load_1m"),
        "network": ("in_bytes", "out_bytes"),
    }.items():
        source = _mapping(state.get(key))
        compact[key] = {field: source.get(field) for field in fields if field in source}

    services = _mapping(state.get("services"))
    compact["services"] = {
        sanitize_text(key, limit=128): sanitize_text(value, limit=128)
        for key, value in list(services.items())[:50]
    }
    docker_source = _mapping(state.get("docker"))
    if "containers" in docker_source:
        compact["docker"]["containers"] = [
            {
                key: (
                    bool(item.get(key))
                    if key == "running"
                    else sanitize_text(item.get(key), limit=128)
                )
                for key in ("name", "running", "health")
                if key in item
            }
            for item in _sequence(docker_source.get("containers"))[:10]
            if isinstance(item, dict)
        ]

    capabilities = _mapping(state.get("operator_capabilities"))
    compact["operator_capabilities"] = {
        str(key): bool(value) for key, value in capabilities.items()
    }
    monitoring = _mapping(state.get("monitoring"))
    compact["monitoring"] = {
        str(key): bool(value) for key, value in monitoring.items()
    }

    updates = _mapping(state.get("updates"))
    compact["updates"] = {
        field: updates.get(field)
        for field in ("pending_count", "fingerprint")
        if field in updates
    }
    last_event = _mapping(state.get("last_job_event"))
    if last_event:
        compact["last_job_event"] = _event_preview(last_event)

    compact["failed_units"] = [
        sanitize_text(item, limit=256)
        for item in _sequence(state.get("failed_units"))[:20]
    ]
    compact["ip_addresses"] = [
        sanitize_text(item, limit=64)
        for item in _sequence(state.get("ip_addresses"))[:20]
    ]
    compact["recent_warnings"] = [
        sanitize_text(item, limit=500)
        for item in _sequence(state.get("recent_warnings"))[-20:]
    ]
    return compact


def _minimal_state(state: dict[str, Any]) -> dict[str, Any]:
    required = (
        "vmid",
        "resource_type",
        "adapter",
        "runtime_status",
        "health_status",
        "health_score",
        "lxc_status",
        "qemu_status",
        "guest_agent_status",
        "hostname",
        "os",
        "uptime_seconds",
        "service_status",
        "api_health",
        "agent_version",
        "update_status",
        "operation_status",
        "job_stage",
        "job_progress",
        "pending_updates",
        "risk",
        "active_plan_id",
        "active_job_id",
        "last_scan",
        "last_update",
        "last_error",
        "last_operation_result",
        "rollback_allowed",
        "snapshot_restore_allowed",
        "dashboard_path",
        "lifecycle_action",
        "lifecycle_status",
        "lifecycle_started_at",
        "lifecycle_finished_at",
        "lifecycle_error",
        "expected_lxc_status",
        "intentional_shutdown",
        "lifecycle_health_pending",
        "verification_status",
        "last_verification",
        "apt_check_ok",
        "dpkg_audit_ok",
        "reboot_required",
        "packages_updated_count",
        "packages_remaining_count",
        "docker_required_healthy",
        "docker_required_total",
        "verification_error",
        "recovery_scan_enabled",
        "recovery_scan_status",
        "recovery_scan_due_at",
        "last_recovery_scan",
        "last_recovery_scan_result",
        "last_terminal_event",
        "last_terminal_at",
        "recovery_notification_suppressed_until",
    )
    minimal = {key: state.get(key) for key in required if key in state}
    for key in (
        "cpu", "disk", "memory", "network", "services", "docker", "updates",
        "last_job_event", "operator_capabilities", "monitoring", "ip_addresses",
        "recent_warnings",
    ):
        if key in state:
            minimal[key] = state[key]
    if _json_bytes(minimal) <= HA_ATTRIBUTE_BUDGET_BYTES:
        return minimal
    minimal.pop("last_job_event", None)
    minimal["last_error"] = sanitize_text(minimal.get("last_error"), limit=256) or None
    for key, item in list(minimal.items()):
        if isinstance(item, str):
            minimal[key] = sanitize_text(item, limit=128)
    return minimal


def bounded_state(value: dict[str, Any]) -> dict[str, Any]:
    sanitized_raw = sanitize_data(value)
    sanitized = sanitized_raw if isinstance(sanitized_raw, dict) else {}
    updates_source = _mapping(sanitized.get("updates"))
    package_values = _sequence(updates_source.get("packages"))
    packages_source = [_package_preview(item) for item in package_values[:200]]
    packages_total = max(
        len(package_values),
        _safe_int(sanitized.get("pending_updates"), 0),
        _safe_int(updates_source.get("pending_count"), 0),
    )

    event_values = _sequence(sanitized.get("recent_job_events"))
    events_source = [_event_preview(item) for item in event_values[-50:]]
    events_total = len(event_values)

    state = _compact_state_base(sanitized)
    state["updates"] = {**_mapping(state.get("updates")), "packages": []}
    state["recent_job_events"] = []
    state["attribute_payload"] = {
        "budget_bytes": HA_ATTRIBUTE_BUDGET_BYTES,
        "packages_total": packages_total,
        "packages_visible": 0,
        "events_total": events_total,
        "events_visible": 0,
        "truncated": False,
    }

    if _json_bytes(state) > HA_ATTRIBUTE_BUDGET_BYTES:
        state = _minimal_state(state)
        state["updates"] = {**_mapping(state.get("updates")), "packages": []}
        state["recent_job_events"] = []
        state["attribute_payload"] = {
            "budget_bytes": HA_ATTRIBUTE_BUDGET_BYTES,
            "packages_total": packages_total,
            "packages_visible": 0,
            "events_total": events_total,
            "events_visible": 0,
            "truncated": bool(packages_total or events_total),
        }

    package_index = 0
    event_index = len(events_source) - 1
    while package_index < len(packages_source) or event_index >= 0:
        changed = False
        if package_index < len(packages_source):
            candidate = {
                **state,
                "updates": {
                    **state["updates"],
                    "packages": state["updates"]["packages"]
                    + [packages_source[package_index]],
                },
                "attribute_payload": {
                    **state["attribute_payload"],
                    "packages_visible": state["attribute_payload"]["packages_visible"]
                    + 1,
                },
            }
            if _json_bytes(candidate) <= HA_ATTRIBUTE_BUDGET_BYTES:
                state = candidate
                package_index += 1
                changed = True
            else:
                package_index = len(packages_source)

        if event_index >= 0:
            candidate = {
                **state,
                "recent_job_events": [events_source[event_index]]
                + state["recent_job_events"],
                "attribute_payload": {
                    **state["attribute_payload"],
                    "events_visible": state["attribute_payload"]["events_visible"]
                    + 1,
                },
            }
            if _json_bytes(candidate) <= HA_ATTRIBUTE_BUDGET_BYTES:
                state = candidate
                event_index -= 1
                changed = True
            else:
                event_index = -1

        if not changed:
            break

    meta = state["attribute_payload"]
    meta["truncated"] = (
        meta["packages_visible"] < meta["packages_total"]
        or meta["events_visible"] < meta["events_total"]
    )
    return state


def bounded_attributes(value: dict[str, Any]) -> dict[str, Any]:
    """Build the independently bounded payload attached to health_status only."""

    source = {
        key: value.get(key)
        for key in HA_ATTRIBUTE_FIELDS
        if key != "attribute_payload" and key in value
    }
    source["recent_warnings"] = [
        sanitize_text(item, limit=160)
        for item in _sequence(source.get("recent_warnings"))[-20:]
    ]
    source["failed_units"] = [
        sanitize_text(item, limit=160)
        for item in _sequence(source.get("failed_units"))[:20]
    ]
    bounded = bounded_state(source)
    attributes = {
        key: bounded[key]
        for key in HA_ATTRIBUTE_FIELDS
        if key in bounded
    }
    if _json_bytes(attributes) > HA_ATTRIBUTE_BUDGET_BYTES:
        raise ValueError("Home Assistant attribute payload exceeds its byte budget")
    return attributes
