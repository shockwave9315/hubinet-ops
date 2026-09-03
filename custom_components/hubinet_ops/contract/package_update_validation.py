"""Snapshot-contract validation for the per-resource package-update job.

What the published snapshot carries about an update job is a *summary*: what
the durable job is, and the timestamps that say how far it got. It never
carries the frozen package rows, the per-probe health results, the append-only
event log, helper output, or command text -- those are exact material an
operator reads through the explicit response-capable action that exists for
them, never something every coordinator poll drags into entity state.

Home Assistant validates this shape independently rather than rendering
whatever arrives, exactly like every other part of this contract. Two rules
here are worth naming because getting either wrong would let the integration
tell an operator something untrue:

- an absent job is ``not_started``, and ``not_started`` is never a success;
- ``health_outcome`` has two members and no ``unknown``. An evaluation that
  could not reach a verdict writes nothing durable, so its representation is
  ``None`` -- "no verdict" -- and ``None`` is not a pass.

This module also refuses to infer anything from adjacency. Home Assistant may
skip arbitrary published revisions, so nothing here reconstructs a transition,
a previous checkpoint, or an intermediate state from two observations.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .enums import PackageUpdateHealthOutcome, PackageUpdateJobState
from .primitives import _require_enum_instance, _require_text, _require_uuid_identity

if TYPE_CHECKING:
    from .models import PackageUpdateJobSummary, PackageUpdateJobView


#: The durable checkpoint vocabulary, mirrored so a payload naming a
#: checkpoint this integration does not understand is refused rather than
#: displayed. Order is not encoded here on purpose: Home Assistant must never
#: compare two checkpoints to decide what "must have happened in between".
PACKAGE_UPDATE_CHECKPOINTS: frozenset[str] = frozenset(
    {
        "issued",
        "preflight_passed",
        "snapshot_may_have_started",
        "snapshot_confirmed",
        "mutation_may_have_started",
        "mutation_completed",
        "health_started",
        "health_completed",
        "rollback_may_have_started",
        "rollback_completed",
    }
)

#: The two states in which no job material may be present at all.
_JOBLESS_STATES = (
    PackageUpdateJobState.UNSUPPORTED,
    PackageUpdateJobState.NOT_STARTED,
)


def validate_package_update_job_summary(summary: "PackageUpdateJobSummary") -> None:
    _require_enum_instance(
        summary.state, PackageUpdateJobState, "package_update_job.state"
    )
    material = (
        summary.job_id,
        summary.checkpoint,
        summary.issued_at,
        summary.health_outcome,
        summary.snapshot_confirmed_at,
        summary.mutation_completed_at,
        summary.rollback_completed_at,
        summary.terminalized_at,
        summary.terminal_reason,
    )
    if summary.state in _JOBLESS_STATES:
        if any(value is not None for value in material):
            raise ValueError(
                "package_update_job carries material without a job"
            )
        return

    _require_uuid_identity(summary.job_id, "package_update_job.job_id")
    if summary.checkpoint not in PACKAGE_UPDATE_CHECKPOINTS:
        raise ValueError("package_update_job.checkpoint is not a known checkpoint")
    _require_text(summary.issued_at, "package_update_job.issued_at")
    if summary.health_outcome is not None:
        _require_enum_instance(
            summary.health_outcome,
            PackageUpdateHealthOutcome,
            "package_update_job.health_outcome",
        )
    for value, name in (
        (summary.snapshot_confirmed_at, "snapshot_confirmed_at"),
        (summary.mutation_completed_at, "mutation_completed_at"),
        (summary.rollback_completed_at, "rollback_completed_at"),
        (summary.terminalized_at, "terminalized_at"),
        (summary.terminal_reason, "terminal_reason"),
    ):
        if value is not None:
            _require_text(value, f"package_update_job.{name}")

    if summary.state is PackageUpdateJobState.ACTIVE:
        if summary.terminalized_at is not None:
            raise ValueError("an active package update job is not terminalized")
        if summary.health_outcome is PackageUpdateHealthOutcome.PASSED:
            # A passing verdict and `succeeded` are one indivisible durable
            # fact in the backend. A payload claiming an ACTIVE job passed is
            # outside the contract and must be refused, not rendered.
            raise ValueError(
                "an active package update job cannot carry a passing health "
                "verdict"
            )
        return

    if summary.terminalized_at is None:
        raise ValueError("a terminal package update job must be terminalized")
    if (
        summary.state is PackageUpdateJobState.SUCCEEDED
        and summary.health_outcome is not PackageUpdateHealthOutcome.PASSED
    ):
        # The single legal route to success is a proven passing verdict. No
        # exit code, proven mutation, or absence of observed failure is one.
        raise ValueError(
            "a succeeded package update job must carry a passing health verdict"
        )
    if (
        summary.state is PackageUpdateJobState.ROLLED_BACK
        and summary.rollback_completed_at is None
    ):
        raise ValueError(
            "a rolled-back package update job must carry a rollback completion"
        )


#: The bounded event levels the backend authors. A payload naming anything
#: else is outside the contract.
_EVENT_LEVELS = frozenset({"info", "warning", "error"})

#: How many events one action response may carry. The backend already bounds
#: its own reply; this is the independent Home Assistant-side refusal, so an
#: over-large tail is rejected rather than rendered.
MAX_PACKAGE_UPDATE_EVENTS = 200


def validate_package_update_job_view(view: "PackageUpdateJobView") -> None:
    _require_enum_instance(view.status, PackageUpdateJobState, "job status")
    if view.status in _JOBLESS_STATES:
        # A concrete job always has a real durable status. `unsupported` and
        # `not_started` describe the ABSENCE of one and can never name a job.
        raise ValueError("a package update job cannot be unsupported or not started")
    if view.checkpoint not in PACKAGE_UPDATE_CHECKPOINTS:
        raise ValueError("job checkpoint is not a known checkpoint")
    _require_text(view.issued_at, "job issued_at")
    _require_text(view.approved_plan_fingerprint, "job approved_plan_fingerprint")
    if type(view.package_count) is not int or view.package_count < 1:
        raise ValueError("job package_count must be a positive integer")
    if view.health_outcome is not None:
        _require_enum_instance(
            view.health_outcome, PackageUpdateHealthOutcome, "job health_outcome"
        )
    if (
        view.status is PackageUpdateJobState.SUCCEEDED
        and view.health_outcome is not PackageUpdateHealthOutcome.PASSED
    ):
        raise ValueError("a succeeded job must carry a passing health verdict")
    if (
        view.status is PackageUpdateJobState.ACTIVE
        and view.health_outcome is PackageUpdateHealthOutcome.PASSED
    ):
        raise ValueError("an active job cannot carry a passing health verdict")
    if type(view.rollback_available) is not bool:
        raise ValueError("job rollback_available must be a boolean")
    if view.rollback_available and view.status is not PackageUpdateJobState.ACTIVE:
        raise ValueError("only an active job can be rollback-capable")
    if len(view.events) > MAX_PACKAGE_UPDATE_EVENTS:
        raise ValueError("job event tail is too long")
    for event in view.events:
        if type(event.sequence) is not int or event.sequence < 1:
            raise ValueError("job event sequence must be a positive integer")
        _require_text(event.created_at, "job event created_at")
        _require_text(event.message, "job event message")
        _require_text(event.event_type, "job event event_type")
        if event.level not in _EVENT_LEVELS:
            raise ValueError("job event level is not a known level")
        if event.stage not in PACKAGE_UPDATE_CHECKPOINTS:
            raise ValueError("job event stage is not a known checkpoint")
