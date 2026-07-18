from __future__ import annotations

from typing import Any

HEALTH_STATUSES = {"healthy", "degraded", "critical", "unknown", "offline"}
UPDATE_STATUSES = {"unknown", "scanning", "up_to_date", "update_available"}
OPERATION_STATUSES = {
    "idle",
    "waiting_approval",
    "running",
    "success",
    "failed",
    "rolled_back",
    "manual_intervention",
}
JOB_STAGES = {
    "idle",
    "scanning",
    "preflight",
    "snapshot",
    "updating",
    "waiting_services",
    "healthcheck",
    "repair",
    "rollback",
    "rollback_wait",
    "rollback_healthcheck",
    "completed",
    "failed",
}
OPERATION_RESULTS = {None, "success", "failed", "rolled_back", "manual_intervention"}


def normalize_state(payload: dict[str, Any]) -> dict[str, Any]:
    state = dict(payload)
    legacy_status = str(state.pop("status", "") or "")
    legacy_health = str(state.pop("health", "") or "")

    health = str(state.get("health_status") or legacy_health or "unknown")
    if state.get("lxc_status") == "stopped":
        health = "offline"
    state["health_status"] = health if health in HEALTH_STATUSES else "unknown"

    pending = max(0, _safe_int(state.get("pending_updates"), 0))
    update = str(state.get("update_status") or "")
    if update not in UPDATE_STATUSES:
        if legacy_status == "scanning":
            update = "scanning"
        elif pending > 0 or legacy_status == "update_available":
            update = "update_available"
        elif state.get("last_scan"):
            update = "up_to_date"
        else:
            update = "unknown"
    state["update_status"] = update

    stage = _legacy_stage(str(state.get("job_stage") or "idle"))
    state["job_stage"] = stage
    operation = str(state.get("operation_status") or "")
    if operation not in OPERATION_STATUSES:
        operation = _legacy_operation(
            legacy_status,
            str(state.get("job_status") or ""),
            stage,
        )
    state["operation_status"] = operation

    has_explicit_result = "last_operation_result" in state
    result = state.get("last_operation_result")
    if result not in OPERATION_RESULTS or not has_explicit_result:
        result = _legacy_result(str(state.get("job_status") or ""), legacy_status)
    state["last_operation_result"] = result
    state["job_progress"] = max(
        0,
        min(100, _safe_int(state.get("job_progress"), 0)),
    )
    state["health_score"] = max(
        0,
        min(100, _safe_int(state.get("health_score"), 0)),
    )
    state["pending_updates"] = pending

    raw_updates = state.get("updates")
    updates = dict(raw_updates) if isinstance(raw_updates, dict) else {}
    updates["pending_count"] = max(
        0,
        _safe_int(updates.get("pending_count"), pending),
    )
    packages = updates.get("packages")
    updates["packages"] = list(packages)[:200] if isinstance(packages, list) else []
    state["updates"] = updates

    state.setdefault("risk", "none")
    state.setdefault("active_plan_id", None)
    state.setdefault("active_plan_status", None)
    state.setdefault("active_job_id", None)
    state.setdefault("last_scan", None)
    state.setdefault("last_refresh", None)
    state.setdefault("last_update", None)
    state.setdefault("last_error", None)
    recent = state.get("recent_job_events")
    state["recent_job_events"] = list(recent)[-50:] if isinstance(recent, list) else []
    if not isinstance(state.get("last_job_event"), (dict, type(None))):
        state["last_job_event"] = None
    else:
        state.setdefault("last_job_event", None)
    return state


def display_status(state: dict[str, Any]) -> str:
    operation = str(state.get("operation_status", "idle"))
    if operation != "idle":
        return operation
    health = str(state.get("health_status", "unknown"))
    if health != "healthy":
        return health
    return str(state.get("update_status", "unknown"))


def _safe_int(value: Any, default: int) -> int:
    try:
        if isinstance(value, bool):
            return default
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _legacy_stage(stage: str) -> str:
    mapping = {
        "queued": "preflight",
        "repairing": "repair",
        "rolling_back": "rollback",
        "rolled_back": "completed",
        "recovered_without_rollback": "completed",
        "manual_intervention": "failed",
        "interrupted": "failed",
        "internal_error": "failed",
    }
    value = mapping.get(stage, stage)
    return value if value in JOB_STAGES else "idle"


def _legacy_operation(status: str, job_status: str, stage: str) -> str:
    if job_status in {"queued", "running"} or stage not in {"idle", "completed", "failed"}:
        return "running"
    if job_status in {"success", "recovered"}:
        return "success"
    if job_status == "rolled_back":
        return "rolled_back"
    if status == "waiting_approval":
        return "waiting_approval"
    if status in {"manual_intervention", "error"}:
        return "manual_intervention"
    if job_status in {"failed", "blocked", "interrupted"}:
        return "failed"
    return "idle"


def _legacy_result(job_status: str, status: str) -> str | None:
    if job_status in {"success", "recovered"}:
        return "success"
    if job_status == "rolled_back":
        return "rolled_back"
    if status == "manual_intervention":
        return "manual_intervention"
    if job_status in {"failed", "blocked", "interrupted"}:
        return "failed"
    return None
