from __future__ import annotations

from typing import Any

HEALTH_STATUSES = {"healthy", "degraded", "critical", "unknown", "offline"}
UPDATE_STATUSES = {"unknown", "scanning", "up_to_date", "update_available"}
LIFECYCLE_STATUSES = {"idle", "running", "success", "failed"}
VERIFICATION_STATUSES = {"unknown", "running", "passed", "warning", "failed"}
RECOVERY_SCAN_STATUSES = {"disabled", "idle", "scheduled", "running", "completed", "blocked", "cancelled", "failed"}
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
    "verifying",
    "starting",
    "shutting_down",
    "rebooting",
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

    raw_updates = state.get("updates")
    updates = dict(raw_updates) if isinstance(raw_updates, dict) else {}
    explicit_unknown = (
        "pending_updates" in state and state.get("pending_updates") is None
    ) or ("pending_count" in updates and updates.get("pending_count") is None)
    if explicit_unknown:
        raw_pending = None
    elif "pending_updates" in state:
        raw_pending = state.get("pending_updates")
    elif "pending_count" in updates:
        raw_pending = updates.get("pending_count")
    else:
        raw_pending = 0
    pending = None if raw_pending is None else max(0, _safe_int(raw_pending, 0))
    update = str(state.get("update_status") or "")
    if pending is None:
        update = "unknown"
    elif update not in UPDATE_STATUSES:
        if legacy_status == "scanning":
            update = "scanning"
        elif (pending is not None and pending > 0) or legacy_status == "update_available":
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

    updates["pending_count"] = pending
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
    capabilities = state.get("operator_capabilities")
    state["operator_capabilities"] = (
        {str(key): bool(value) for key, value in capabilities.items()}
        if isinstance(capabilities, dict)
        else {}
    )
    lifecycle_status = str(state.get("lifecycle_status") or "idle")
    state["lifecycle_status"] = (
        lifecycle_status if lifecycle_status in LIFECYCLE_STATUSES else "idle"
    )
    state.setdefault("lifecycle_action", None)
    state.setdefault("lifecycle_started_at", None)
    state.setdefault("lifecycle_finished_at", None)
    state.setdefault("lifecycle_error", None)
    expected_lxc = str(state.get("expected_lxc_status") or "")
    state["expected_lxc_status"] = (
        expected_lxc if expected_lxc in {"running", "stopped"} else None
    )
    state["intentional_shutdown"] = state.get("intentional_shutdown") is True
    state["lifecycle_health_pending"] = state.get("lifecycle_health_pending") is True
    verification = str(state.get("verification_status") or "unknown")
    state["verification_status"] = (
        verification if verification in VERIFICATION_STATUSES else "unknown"
    )
    for key, default in (
        ("last_verification", None),
        ("apt_check_ok", None),
        ("dpkg_audit_ok", None),
        ("reboot_required", None),
        ("packages_updated_count", 0),
        ("packages_remaining_count", 0),
        ("docker_required_healthy", 0),
        ("docker_required_total", 0),
        ("verification_error", None),
    ):
        state.setdefault(key, default)
    recovery = str(state.get("recovery_scan_status") or "disabled")
    state["recovery_scan_status"] = (
        recovery if recovery in RECOVERY_SCAN_STATUSES else "disabled"
    )
    state.setdefault("recovery_scan_enabled", False)
    state.setdefault("recovery_scan_due_at", None)
    state.setdefault("last_recovery_scan", None)
    state.setdefault("last_recovery_scan_result", None)
    state.setdefault("last_terminal_event", None)
    state.setdefault("last_terminal_at", None)
    state.setdefault("recovery_notification_suppressed_until", None)
    recent = state.get("recent_job_events")
    state["recent_job_events"] = list(recent)[-50:] if isinstance(recent, list) else []
    if not isinstance(state.get("last_job_event"), (dict, type(None))):
        state["last_job_event"] = None
    else:
        state.setdefault("last_job_event", None)

    # active_job_id describes a currently executable job, never historical work.
    # Terminal outcomes remain available through last_operation_result and job_stage.
    if state["operation_status"] != "running":
        state["active_job_id"] = None

    # Waiting for approval is a plan state, not a running or completed job. A stale
    # terminal progress value from an earlier operation must not leak into this state.
    if state["operation_status"] == "waiting_approval":
        state["job_stage"] = "idle"
        state["job_progress"] = 0

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
