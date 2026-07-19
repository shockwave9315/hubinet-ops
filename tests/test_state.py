from __future__ import annotations

import pytest

from app.state import display_status, normalize_state


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            {"health_status": "healthy", "update_status": "up_to_date"},
            ("healthy", "up_to_date", "idle", None),
        ),
        (
            {"health_status": "healthy", "pending_updates": 4},
            ("healthy", "update_available", "idle", None),
        ),
        (
            {
                "health_status": "healthy",
                "update_status": "update_available",
                "operation_status": "failed",
                "last_operation_result": "rolled_back",
            },
            ("healthy", "update_available", "failed", "rolled_back"),
        ),
        (
            {
                "health_status": "critical",
                "operation_status": "manual_intervention",
                "last_operation_result": "manual_intervention",
            },
            ("critical", "unknown", "manual_intervention", "manual_intervention"),
        ),
    ],
)
def test_state_dimensions_are_independent(payload: dict, expected: tuple) -> None:
    state = normalize_state(payload)
    assert (
        state["health_status"],
        state["update_status"],
        state["operation_status"],
        state["last_operation_result"],
    ) == expected


def test_display_status_never_mutates_dimensions() -> None:
    state = normalize_state(
        {
            "health_status": "healthy",
            "update_status": "update_available",
            "operation_status": "failed",
            "last_operation_result": "rolled_back",
        }
    )
    assert display_status(state) == "failed"
    assert state["update_status"] == "update_available"


def test_malformed_legacy_values_fall_back_without_crashing() -> None:
    state = normalize_state(
        {
            "pending_updates": "not-a-number",
            "job_progress": float("inf"),
            "health_score": True,
            "updates": ["not", "a", "mapping"],
            "recent_job_events": "not-a-list",
            "last_job_event": "bad",
            "expected_lxc_status": "destroyed",
            "intentional_shutdown": "false",
            "lifecycle_health_pending": 1,
        }
    )
    assert state["pending_updates"] == 0
    assert state["job_progress"] == 0
    assert state["health_score"] == 0
    assert state["updates"] == {"pending_count": 0, "packages": []}
    assert state["recent_job_events"] == []
    assert state["last_job_event"] is None
    assert state["expected_lxc_status"] is None
    assert state["intentional_shutdown"] is False
    assert state["lifecycle_health_pending"] is False


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({}, 0),
        ({"updates": {"pending_count": 7}}, 7),
        (
            {
                "pending_updates": None,
                "update_status": "up_to_date",
                "updates": {"pending_count": 0},
            },
            None,
        ),
        ({"updates": {"pending_count": None}}, None),
        ({"pending_updates": 0, "updates": {"pending_count": None}}, None),
        ({"pending_updates": "not-a-number"}, 0),
        ({"pending_updates": True}, 0),
        ({"pending_updates": -4}, 0),
        ({"pending_updates": "5"}, 5),
    ],
)
def test_pending_update_count_normalization_is_consistent(
    payload: dict,
    expected: int | None,
) -> None:
    state = normalize_state(payload)

    if expected is None:
        assert state["pending_updates"] is None
        assert state["updates"]["pending_count"] is None
        assert state["update_status"] == "unknown"
    else:
        assert state["pending_updates"] == expected
        assert state["updates"]["pending_count"] == expected


def test_waiting_approval_clears_stale_job_runtime_fields() -> None:
    state = normalize_state(
        {
            "health_status": "healthy",
            "update_status": "update_available",
            "operation_status": "waiting_approval",
            "job_stage": "completed",
            "job_progress": 100,
            "active_job_id": "old-terminal-job",
            "active_plan_id": "current-plan",
            "last_operation_result": "failed",
        }
    )

    assert state["operation_status"] == "waiting_approval"
    assert state["job_stage"] == "idle"
    assert state["job_progress"] == 0
    assert state["active_job_id"] is None
    assert state["active_plan_id"] == "current-plan"
    assert state["last_operation_result"] == "failed"


def test_terminal_operation_keeps_history_but_has_no_active_job() -> None:
    state = normalize_state(
        {
            "operation_status": "rolled_back",
            "job_stage": "completed",
            "job_progress": 100,
            "active_job_id": "finished-job",
            "last_operation_result": "rolled_back",
        }
    )

    assert state["active_job_id"] is None
    assert state["job_stage"] == "completed"
    assert state["job_progress"] == 100
    assert state["last_operation_result"] == "rolled_back"


def test_running_operation_preserves_active_job() -> None:
    state = normalize_state(
        {
            "operation_status": "running",
            "job_stage": "updating",
            "job_progress": 42,
            "active_job_id": "active-job",
        }
    )

    assert state["active_job_id"] == "active-job"
    assert state["job_stage"] == "updating"
    assert state["job_progress"] == 42


def test_legacy_state_defaults_to_lxc_apt_contract() -> None:
    state = normalize_state({"lxc_status": "running", "hostname": "legacy"})

    assert state["resource_type"] == "lxc"
    assert state["adapter"] == "apt"
    assert state["runtime_status"] == "running"
    assert state["lxc_status"] == "running"
    assert state["hostname"] == "legacy"
    assert state["pending_updates"] == 0


def test_qemu_state_uses_common_contract_without_fake_apt_values() -> None:
    state = normalize_state(
        {
            "resource_type": "qemu",
            "adapter": "haos",
            "qemu_status": "running",
            "guest_agent_status": "available",
            "ip_addresses": ["192.0.2.10"],
        }
    )

    assert state["runtime_status"] == "running"
    assert state["qemu_status"] == "running"
    assert state["pending_updates"] is None
    assert state["updates"]["pending_count"] is None
    assert state["packages_remaining_count"] is None
    assert state["guest_agent_status"] == "available"


def test_common_resource_values_are_bounded_and_fail_closed() -> None:
    state = normalize_state(
        {
            "resource_type": "unsafe",
            "adapter": "shell",
            "runtime_status": "destroyed",
            "uptime_seconds": -8,
            "ip_addresses": "not-a-list",
            "cpu": [],
            "monitoring": {"inspect": True, "bad": "yes"},
            "guest_agent_status": "broken",
        }
    )

    assert state["resource_type"] == "lxc"
    assert state["adapter"] == "apt"
    assert state["runtime_status"] == "unknown"
    assert state["uptime_seconds"] == 0
    assert state["ip_addresses"] == []
    assert state["cpu"] == {}
    assert state["monitoring"] == {"inspect": True}
    assert state["guest_agent_status"] == "unknown"
