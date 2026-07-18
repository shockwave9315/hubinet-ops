from __future__ import annotations

import pytest

from app.state import display_status, normalize_state


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"health_status": "healthy", "update_status": "up_to_date"}, ("healthy", "up_to_date", "idle", None)),
        ({"health_status": "healthy", "pending_updates": 4}, ("healthy", "update_available", "idle", None)),
        (
            {"health_status": "healthy", "update_status": "update_available", "operation_status": "failed", "last_operation_result": "rolled_back"},
            ("healthy", "update_available", "failed", "rolled_back"),
        ),
        (
            {"health_status": "critical", "operation_status": "manual_intervention", "last_operation_result": "manual_intervention"},
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
