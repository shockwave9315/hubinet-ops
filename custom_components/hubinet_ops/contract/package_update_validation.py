"""Snapshot-contract validation for the per-resource package-update job.

What the published snapshot carries about an update job is a *summary*: what
the durable job is, and the timestamps that say how far it got. It never
carries the frozen package rows, the per-probe health results, the append-only
event log, helper output, or command text -- those are exact material an
operator reads through the explicit response-capable action that exists for
them, never something every coordinator poll drags into entity state. The
per-probe health results and the event log ARE part of that explicit action's
own response (``PackageUpdateJobView``, validated below) -- only the concise
snapshot *summary* excludes them.

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

from .enums import (
    HealthProbeKind,
    HealthProbeOutcome,
    PackageUpdateHealthOutcome,
    PackageUpdateJobState,
)
from .health_contract_validation import (
    MAX_HEALTH_PROBES,
    MAX_HEALTH_PROBE_TARGET_LENGTH,
)
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

#: Mirrors `app/inventory/health_observation.py::HEALTH_PROBE_REASONS`, the
#: backend's closed durable reason taxonomy. Home Assistant validates this
#: independently rather than trusting the payload, exactly like
#: `PACKAGE_UPDATE_CHECKPOINTS` above: a reason outside this bounded set is
#: refused rather than rendered as if it were operator-meaningful text, and
#: nothing here is raw guest output -- every token is a fixed classification
#: the backend itself chose.
HEALTH_PROBE_REASONS: frozenset[str] = frozenset(
    {
        "unit_active",
        "unit_not_active",
        "unit_activating",
        "unit_deactivating",
        "unit_reloading",
        "unit_job_pending",
        "guest_operational_confirmed",
        "probe_target_not_exact",
        "probe_target_ambiguous",
        "guest_unavailable",
        "command_failed",
        "command_timed_out",
        "malformed_output",
        "host_unreachable",
        "host_response_rejected",
        "resource_context_changed",
        "settling_budget_exhausted",
    }
)

#: Bounded settling evidence taxonomy (frozen post-Human1 health
#: architecture, Stage 2). ``None`` means no evidence to show yet;
#: ``"observation"`` is an unresolved evaluation's bounded per-probe
#: evidence -- never a verdict, never recheck-until-pass; ``"verdict"`` is a
#: durable, non-recheckable PASSED/FAILED result.
HEALTH_EVIDENCE_KINDS: frozenset[str] = frozenset({"observation", "verdict"})

#: Mirrors `app/inventory/health_observation.py::UNRESOLVED_HEALTH_REASONS`:
#: the UNKNOWN family, and the ONLY tokens `health_reason` may carry.
#:
#: Deliberately mirrored rather than imported -- this integration validates
#: what the backend sent instead of trusting it, and a shared import would
#: make the two agree by construction rather than by proof. A backend that
#: sends a PASSED-only token as the reason a job has no result is sending a
#: self-contradictory payload, and it is refused here.
UNRESOLVED_HEALTH_REASONS: frozenset[str] = frozenset(
    {
        "probe_target_not_exact",
        "probe_target_ambiguous",
        "guest_unavailable",
        "command_failed",
        "command_timed_out",
        "malformed_output",
        "host_unreachable",
        "host_response_rejected",
        "resource_context_changed",
        "unit_activating",
        "unit_deactivating",
        "unit_reloading",
        "unit_job_pending",
        "settling_budget_exhausted",
    }
)

#: Mirrors `app/inventory/health_observation.py::HEALTH_PROBE_REASONS_BY_
#: OUTCOME`. A reason token that is syntactically bounded (in
#: `HEALTH_PROBE_REASONS`) but semantically impossible for the outcome it
#: accompanies -- e.g. `container_health_starting` (an UNKNOWN-only token)
#: paired with `outcome=passed` -- is exactly the kind of self-contradictory
#: payload PR #80 review found HA was not independently proving impossible.
HEALTH_PROBE_REASONS_BY_OUTCOME: dict[HealthProbeOutcome, frozenset[str]] = {
    HealthProbeOutcome.PASSED: frozenset(
        {
            "unit_active",
            "guest_operational_confirmed",
        }
    ),
    HealthProbeOutcome.FAILED: frozenset(
        {
            "unit_not_active",
        }
    ),
    HealthProbeOutcome.UNKNOWN: frozenset(
        {
            "probe_target_not_exact",
            "probe_target_ambiguous",
            "guest_unavailable",
            "command_failed",
            "command_timed_out",
            "malformed_output",
            "host_unreachable",
            "host_response_rejected",
            "resource_context_changed",
            "unit_activating",
            "unit_deactivating",
            "unit_reloading",
            "unit_job_pending",
            "settling_budget_exhausted",
        }
    ),
}

#: Mirrors `app/inventory/health_observation.py::HEALTH_PROBE_REASON_KINDS`.
#: A reason absent from this mapping is generic/structural (a whole-request
#: or transport-level classification, e.g. `command_failed`) and legal for
#: any probe kind; one present here is legal ONLY for the kind(s) listed.
#: Mirrors `app/inventory/health_observation.py::_TARGETED_HEALTH_PROBE_
#: KINDS`: every probe kind that NAMES a target. `GUEST_OPERATIONAL` names
#: none, and is deliberately absent.
_TARGETED_HEALTH_PROBE_KINDS: frozenset[HealthProbeKind] = frozenset(
    {
        HealthProbeKind.SYSTEMD_UNIT_ACTIVE,
    }
)

HEALTH_PROBE_REASON_KINDS: dict[str, frozenset[HealthProbeKind]] = {
    "unit_active": frozenset({HealthProbeKind.SYSTEMD_UNIT_ACTIVE}),
    "unit_not_active": frozenset({HealthProbeKind.SYSTEMD_UNIT_ACTIVE}),
    "unit_activating": frozenset({HealthProbeKind.SYSTEMD_UNIT_ACTIVE}),
    "unit_deactivating": frozenset({HealthProbeKind.SYSTEMD_UNIT_ACTIVE}),
    "unit_reloading": frozenset({HealthProbeKind.SYSTEMD_UNIT_ACTIVE}),
    "unit_job_pending": frozenset({HealthProbeKind.SYSTEMD_UNIT_ACTIVE}),
    "guest_operational_confirmed": frozenset({HealthProbeKind.GUEST_OPERATIONAL}),
    # PR #80 final review: a targetless probe kind can never truthfully
    # report a target-SHAPED reason. Mirrors the backend's own
    # `_TARGETED_HEALTH_PROBE_KINDS`.
    "probe_target_not_exact": _TARGETED_HEALTH_PROBE_KINDS,
    "probe_target_ambiguous": _TARGETED_HEALTH_PROBE_KINDS,
}

#: The two states in which no job material may be present at all.
_JOBLESS_STATES = (
    PackageUpdateJobState.UNSUPPORTED,
    PackageUpdateJobState.NOT_STARTED,
)


def validate_package_update_job_summary(summary: "PackageUpdateJobSummary") -> None:
    _require_enum_instance(
        summary.state, PackageUpdateJobState, "package_update_job.state"
    )
    if type(summary.rollback_available) is not bool:
        raise ValueError("package_update_job.rollback_available must be a boolean")
    material = (
        summary.job_id,
        summary.checkpoint,
        summary.issued_at,
        summary.package_count,
        summary.health_outcome,
        summary.health_started_at,
        summary.health_completed_at,
        summary.snapshot_confirmed_at,
        summary.mutation_completed_at,
        summary.rollback_completed_at,
        summary.terminalized_at,
        summary.terminal_reason,
    )
    if summary.state in _JOBLESS_STATES:
        if any(value is not None for value in material) or summary.rollback_available:
            raise ValueError(
                "package_update_job carries material without a job"
            )
        return

    _require_uuid_identity(summary.job_id, "package_update_job.job_id")
    if summary.checkpoint not in PACKAGE_UPDATE_CHECKPOINTS:
        raise ValueError("package_update_job.checkpoint is not a known checkpoint")
    _require_text(summary.issued_at, "package_update_job.issued_at")
    if summary.package_count is not None and (
        type(summary.package_count) is not int or summary.package_count < 1
    ):
        raise ValueError("package_update_job.package_count must be positive")
    if summary.health_outcome is not None:
        _require_enum_instance(
            summary.health_outcome,
            PackageUpdateHealthOutcome,
            "package_update_job.health_outcome",
        )
    for value, name in (
        (summary.snapshot_confirmed_at, "snapshot_confirmed_at"),
        (summary.mutation_completed_at, "mutation_completed_at"),
        (summary.health_started_at, "health_started_at"),
        (summary.health_completed_at, "health_completed_at"),
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

    if summary.rollback_available:
        raise ValueError("only an active package update job can allow rollback")

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
    _validate_package_update_job_health_probes(view)


def _validate_unresolved_health_reason(view: "PackageUpdateJobView") -> None:
    """Prove the whole-request UNKNOWN classification is bounded and CURRENT.

    Two independent rules, and neither is derivable from the other:

    - it is one token from the closed UNKNOWN family, or absent. A
      free-form string, an unknown token, or a token that only ever
      describes a positive proof (`unit_active`) or a proven-false conjunct
      (`container_absent`) is a self-contradictory answer to "why is there
      still no result", and is refused rather than rendered.
    - it may accompany an unresolved job only. Once ``health_outcome``
      exists the job HAS a definitive result, and a prior attempt's UNKNOWN
      classification is history; publishing it alongside would describe a
      finished job as still blocked. A PASSED verdict carrying
      `guest_unavailable` is exactly the stale-reason payload this refuses.

    Deliberately NOT coupled to ``health_evidence``: the case this field
    exists for -- a refusal before any probe round ran -- has no evidence at
    all, so requiring one would reject the very payload it must accept.
    """

    reason = view.health_reason
    if reason is None:
        return
    if not isinstance(reason, str) or reason not in UNRESOLVED_HEALTH_REASONS:
        raise ValueError(
            "job health_reason is not a known bounded unresolved reason token"
        )
    if view.health_outcome is not None:
        raise ValueError(
            "job health_reason must be absent once a definitive health "
            "verdict exists"
        )


def _validate_package_update_job_health_probes(view: "PackageUpdateJobView") -> None:
    """Validate the per-probe health evidence, if any is present.

    Present only once there is something to show at all -- exactly when
    ``health_evidence`` is also present -- and never otherwise: a job with
    neither a verdict nor an unresolved evaluation's observation has nothing
    to show, and a payload claiming otherwise is outside the contract.

    ``health_evidence == "verdict"`` requires every probe ``definitive``;
    ``"observation"`` requires every probe NOT ``definitive`` -- a payload
    may never mix the two, and definitive evidence is exactly and only
    durable verdict evidence (`app/inventory_runtime.py`,
    ``_package_update_health_body``). ``"verdict"`` additionally requires
    ``health_outcome`` to be present (the two are the same durable fact from
    two sides); ``"observation"`` requires it absent -- a job with a durable
    verdict never ALSO carries stale UNKNOWN observation evidence.
    """

    probes = view.health_probes
    if len(probes) > MAX_HEALTH_PROBES:
        raise ValueError("job health_probes exceeds the maximum probe count")
    if view.health_evidence is not None and view.health_evidence not in HEALTH_EVIDENCE_KINDS:
        raise ValueError("job health_evidence is not a known evidence kind")
    _validate_unresolved_health_reason(view)
    if bool(probes) != (view.health_evidence is not None):
        raise ValueError(
            "job health_probes must be present exactly when health_evidence "
            "is present"
        )
    if (view.health_evidence == "verdict") != (view.health_outcome is not None):
        raise ValueError(
            "job health_evidence must be \"verdict\" exactly when a "
            "definitive health verdict is present"
        )
    seen_indexes: set[int] = set()
    for probe in probes:
        if type(probe.probe_index) is not int or probe.probe_index < 0:
            raise ValueError("job health probe index must be a non-negative integer")
        if probe.probe_index in seen_indexes:
            raise ValueError("job health probes contain a duplicate index")
        seen_indexes.add(probe.probe_index)
        _require_enum_instance(probe.kind, HealthProbeKind, "job health probe kind")
        if probe.kind is HealthProbeKind.GUEST_OPERATIONAL:
            if probe.target is not None:
                raise ValueError(
                    "a guest_operational job health probe must not carry a target"
                )
        else:
            if probe.target is None:
                raise ValueError("job health probe target is required for this kind")
            _require_text(probe.target, "job health probe target")
            if len(probe.target) > MAX_HEALTH_PROBE_TARGET_LENGTH:
                raise ValueError("job health probe target is too long")
        _require_enum_instance(
            probe.outcome, HealthProbeOutcome, "job health probe outcome"
        )
        _require_text(probe.checked_at, "job health probe checked_at")
        if probe.reason not in HEALTH_PROBE_REASONS:
            raise ValueError(
                "job health probe reason is not a known bounded token"
            )
        # Independent semantic coherence -- a probe kind/outcome/reason
        # triple that is individually well-typed but jointly impossible
        # (e.g. outcome=passed with reason=unit_activating, an UNKNOWN-only
        # token; or a guest_operational probe carrying a systemd-only reason)
        # is refused here rather than rendered as if it described a real
        # observation. Mirrors `app/inventory/health_observation.py::
        # require_health_probe_semantics`.
        if probe.reason not in HEALTH_PROBE_REASONS_BY_OUTCOME[probe.outcome]:
            raise ValueError(
                "job health probe reason contradicts its own outcome"
            )
        allowed_kinds = HEALTH_PROBE_REASON_KINDS.get(probe.reason)
        if allowed_kinds is not None and probe.kind not in allowed_kinds:
            raise ValueError(
                "job health probe reason is impossible for that probe kind"
            )
        if type(probe.definitive) is not bool:
            raise ValueError("job health probe definitive must be a boolean")
        if probe.definitive != (view.health_evidence == "verdict"):
            raise ValueError(
                "job health probe definitive must match the job's own "
                "health_evidence kind"
            )
    if probes and seen_indexes != set(range(len(probes))):
        raise ValueError("job health probes are not canonically indexed")
    # Aggregate verdict <-> per-probe coherence, mirroring the backend's own
    # ALL-OF aggregation (`app/inventory/models.py::aggregate_health_
    # outcome`): a payload naming a verdict that the accompanying probe rows
    # positively disprove is refused, never rendered as if it were truthful.
    if view.health_evidence == "verdict":
        # Only a COMPLETE DECISIVE observation set may ever be finalized
        # (PRODUCT.md, "What healthy means"): a definitive verdict can never
        # carry a probe this integration itself could not resolve, even
        # beside a proven FAILED sibling. Checked independently of the
        # backend's own refusal (`InventoryAuthority.
        # complete_package_update_health`) rather than trusted.
        if any(probe.outcome is HealthProbeOutcome.UNKNOWN for probe in probes):
            raise ValueError(
                "a definitive health verdict may not carry an unresolved "
                "(UNKNOWN) probe"
            )
        if view.health_outcome is PackageUpdateHealthOutcome.PASSED and not all(
            probe.outcome is HealthProbeOutcome.PASSED for probe in probes
        ):
            raise ValueError(
                "a PASSED verdict requires every probe to have PASSED"
            )
        if view.health_outcome is PackageUpdateHealthOutcome.FAILED and not any(
            probe.outcome is HealthProbeOutcome.FAILED for probe in probes
        ):
            raise ValueError(
                "a FAILED verdict requires at least one probe to have FAILED"
            )
