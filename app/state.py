from __future__ import annotations

import math
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
    "queued",
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
    "force_stopping",
    "snapshot_creating",
    "snapshot_rollback",
    "snapshot_deleting",
    "self_updating",
    "executing",
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

    resource_type = str(state.get("resource_type") or "lxc").lower()
    if resource_type not in {"lxc", "qemu"}:
        resource_type = "lxc"
    adapter = str(state.get("adapter") or ("haos" if resource_type == "qemu" else "apt"))
    if adapter not in {"apt", "haos", "agent_self"}:
        adapter = "apt" if resource_type == "lxc" else "haos"
    state["resource_type"] = resource_type
    state["adapter"] = adapter

    type_status_key = "qemu_status" if resource_type == "qemu" else "lxc_status"
    runtime_status = str(
        state.get(type_status_key) or state.get("runtime_status") or "unknown"
    ).lower()
    if runtime_status not in {"running", "stopped", "unknown"}:
        runtime_status = "unknown"
    state["runtime_status"] = runtime_status
    state[type_status_key] = runtime_status

    health = str(state.get("health_status") or legacy_health or "unknown")
    if runtime_status == "stopped":
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
        raw_pending = 0 if adapter == "apt" else None
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
    state.setdefault("last_job_id", None)
    state.setdefault("operation_type", None)
    state.setdefault("last_scan", None)
    state.setdefault("last_refresh", None)
    state.setdefault("last_update", None)
    state.setdefault("last_error", None)
    state["hostname"] = str(state.get("hostname") or "")[:255]
    state["os"] = str(state.get("os") or "")[:255]
    state["uptime_seconds"] = max(0, _safe_int(state.get("uptime_seconds"), 0))
    addresses = state.get("ip_addresses")
    state["ip_addresses"] = (
        [str(value)[:128] for value in addresses[:32]]
        if isinstance(addresses, list)
        else []
    )
    primary_ip = str(state.get("primary_ip_address") or "").strip()
    if not primary_ip:
        primary_ip = next(
            (
                value
                for value in state["ip_addresses"]
                if value
                and value != "::1"
                and not value.startswith("127.")
                and not value.lower().startswith("fe80:")
                and not value.startswith("172.30.")
            ),
            "",
        )
    state["primary_ip_address"] = primary_ip[:254]
    for key in ("cpu", "memory", "disk", "network", "services", "docker"):
        value = state.get(key)
        state[key] = dict(value) if isinstance(value, dict) else {}
    if resource_type == "qemu" and adapter == "haos":
        # Proxmox QEMU status/current defines cpu as a utilization share (0..1).
        # Preserve the raw diagnostic value and expose an explicit HA percentage.
        raw_cpu_usage = state["cpu"].get("usage")
        cpu_share = _safe_float(raw_cpu_usage)
        state["cpu"]["usage_percent"] = (
            None if cpu_share is None else round(max(0.0, min(1.0, cpu_share)) * 100, 3)
        )
    guest_agent = str(state.get("guest_agent_status") or "unknown")
    state["guest_agent_status"] = (
        guest_agent
        if guest_agent in {"available", "unavailable", "unknown"}
        else "unknown"
    )
    monitoring = state.get("monitoring")
    state["monitoring"] = (
        {str(key): value for key, value in monitoring.items() if isinstance(value, bool)}
        if isinstance(monitoring, dict)
        else {}
    )
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
        ("packages_remaining_count", 0 if adapter == "apt" else None),
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
    state.setdefault("executor_version", None)
    state.setdefault("executor_protocol_version", None)
    state["executor_compatible"] = state.get("executor_compatible") is True
    state.setdefault("executor_sha256", None)
    state.setdefault("executor_profile_sha256", None)
    missing_actions = state.get("executor_missing_actions")
    state["executor_missing_actions"] = (
        [str(action)[:64] for action in missing_actions[:32]]
        if isinstance(missing_actions, list)
        else []
    )
    state.setdefault("executor_last_checked_at", None)
    state["snapshot_count"] = max(0, _safe_int(state.get("snapshot_count"), 0))
    state.setdefault("latest_snapshot_name", None)
    state.setdefault("latest_snapshot_at", None)
    state.setdefault("latest_snapshot_kind", None)
    snapshot_operation = str(state.get("snapshot_operation_status") or "idle")
    state["snapshot_operation_status"] = (
        snapshot_operation
        if snapshot_operation in {"idle", "running", "success", "failed"}
        else "idle"
    )
    state.setdefault("profile_validation_status", "unknown")
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


def _safe_float(value: Any) -> float | None:
    try:
        if isinstance(value, bool):
            return None
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


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
