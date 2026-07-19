from __future__ import annotations

import json
from typing import Any

from .security import sanitize_data, sanitize_text

HA_ATTRIBUTE_BUDGET_BYTES = 10_000


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _json_bytes(value: Any) -> int:
    return len(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )


def _package_preview(value: Any) -> dict[str, Any]:
    package = dict(value or {}) if isinstance(value, dict) else {"name": value}
    preview: dict[str, Any] = {}
    for key in ("name", "current", "target"):
        if key in package:
            preview[key] = sanitize_text(package.get(key), limit=256)
    return preview


def _event_preview(value: Any) -> dict[str, Any]:
    event = dict(value or {}) if isinstance(value, dict) else {"message": value}
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
        if key in {"recent_job_events", "updates", "failed_units", "ip_addresses"}:
            continue
        if item is None or isinstance(item, (bool, int, float)):
            compact[key] = item
        elif isinstance(item, str):
            compact[key] = sanitize_text(item, limit=2000 if key == "last_error" else 512)

    for key, fields in {
        "disk": ("used_percent", "free_mb"),
        "memory": ("used_percent",),
        "docker": ("required_healthy", "required_total"),
    }.items():
        source = dict(state.get(key) or {})
        compact[key] = {field: source.get(field) for field in fields if field in source}

    updates = dict(state.get("updates") or {})
    compact["updates"] = {
        field: updates.get(field)
        for field in ("pending_count", "fingerprint")
        if field in updates
    }
    last_event = dict(state.get("last_job_event") or {})
    if last_event:
        compact["last_job_event"] = _event_preview(last_event)
    compact["failed_units"] = [
        sanitize_text(item, limit=256)
        for item in list(state.get("failed_units") or [])[:20]
    ]
    compact["ip_addresses"] = [
        sanitize_text(item, limit=64)
        for item in list(state.get("ip_addresses") or [])[:20]
    ]
    return compact


def _minimal_state(state: dict[str, Any]) -> dict[str, Any]:
    required = (
        "vmid",
        "health_status",
        "health_score",
        "lxc_status",
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
        "dashboard_path",
    )
    minimal = {key: state.get(key) for key in required if key in state}
    for key in ("disk", "memory", "docker", "updates", "last_job_event"):
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
    sanitized = sanitize_data(value)
    updates_source = dict(sanitized.get("updates") or {})
    packages_source = [
        _package_preview(item)
        for item in list(updates_source.get("packages") or [])[:200]
    ]
    packages_total = max(
        len(packages_source),
        _safe_int(sanitized.get("pending_updates"), 0),
        _safe_int(updates_source.get("pending_count"), 0),
    )
    events_source = [
        _event_preview(item)
        for item in list(sanitized.get("recent_job_events") or [])[-50:]
    ]

    state = _compact_state_base(sanitized)
    updates = dict(state.get("updates") or {})
    updates["packages"] = []
    state["updates"] = updates
    state["recent_job_events"] = []
    state["attribute_payload"] = {
        "budget_bytes": HA_ATTRIBUTE_BUDGET_BYTES,
        "packages_total": packages_total,
        "packages_visible": 0,
        "events_total": len(events_source),
        "events_visible": 0,
        "truncated": False,
    }

    if _json_bytes(state) > HA_ATTRIBUTE_BUDGET_BYTES:
        state = _minimal_state(state)
        state["updates"] = {**dict(state.get("updates") or {}), "packages": []}
        state["recent_job_events"] = []
        state["attribute_payload"] = {
            "budget_bytes": HA_ATTRIBUTE_BUDGET_BYTES,
            "packages_total": packages_total,
            "packages_visible": 0,
            "events_total": len(events_source),
            "events_visible": 0,
            "truncated": bool(packages_total or events_source),
        }

    package_index = 0
    event_index = len(events_source) - 1
    while package_index < len(packages_source) or event_index >= 0:
        changed = False
        if package_index < len(packages_source):
            candidate = sanitize_data(state)
            candidate["updates"]["packages"].append(packages_source[package_index])
            candidate["attribute_payload"]["packages_visible"] += 1
            if _json_bytes(candidate) <= HA_ATTRIBUTE_BUDGET_BYTES:
                state = candidate
                package_index += 1
                changed = True
            else:
                package_index = len(packages_source)

        if event_index >= 0:
            candidate = sanitize_data(state)
            candidate["recent_job_events"].insert(0, events_source[event_index])
            candidate["attribute_payload"]["events_visible"] += 1
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
