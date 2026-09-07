"""Shared semantic rules for typed health-probe observations."""

from __future__ import annotations

from .models import HealthProbeKind, HealthProbeOutcome


# Closed durable taxonomy.  Raw guest output never becomes a reason.
#
# Bounded health settling (ARCHITECTURE.md, "Job-bound healthcheck execution")
# applies only to the explicit advanced `systemd_unit_active` contract now --
# the built-in `guest_operational` baseline is a single one-shot execution,
# never settled (see "Major fix: baseline independence" in ARCHITECTURE.md).
# Every token below is either a POSITIVE proof (PASSED), a proof the declared
# object exists but does not satisfy the probe (FAILED, never recheckable
# once durable), or a truthful non-answer that a bounded settling round may
# legitimately see again on its very next observation (UNKNOWN). A reason is
# added here only when the helper and the backend both need to name the exact
# same fact; see `deploy/hubinet-package-health-helper.py` for the guest-side
# state machine that produces each one.
HEALTH_PROBE_REASONS: frozenset[str] = frozenset(
    {
        "unit_active",
        "unit_not_active",
        "unit_activating",
        "unit_deactivating",
        "unit_reloading",
        "unit_job_pending",
        # The guest_operational kind -- v0.5's BUILT-IN DEFAULT contract
        # for a package-managed LXC, no longer a discovery fallback.
        # PASS only -- the exact resource context revalidated and the one
        # fixed, code-owned, read-only guest liveness operation succeeded.
        # There is deliberately no distinct FAIL token: infrastructure
        # uncertainty about a guest that this stage cannot positively prove
        # down is UNKNOWN, reusing the exact same generic UNKNOWN tokens
        # every other kind already shares (guest_unavailable, command_
        # failed, command_timed_out, malformed_output) -- never a guess.
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
        # PR #80 review finding 1: the absolute settling deadline ran out
        # before this probe's own family could safely start (or finish)
        # another subprocess -- computed fresh, immediately before each one,
        # never inferred from a value calculated before an earlier
        # subprocess in the same round consumed real wall-clock time. Never
        # a verdict, and applies to any probe kind's family. Meaningful only
        # for the advanced systemd settling window: the baseline's one-shot
        # execution has no round to exhaust.
        "settling_budget_exhausted",
    }
)

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
            # systemd's own transient job states: a unit mid (de)activation
            # or reload is not yet a verdict either way, and
            # `unit_job_pending` is the same fact for a unit currently
            # inactive/failed with systemd's own Job property still pending --
            # `systemctl show --property=Job` is the load-bearing distinction
            # from a settled, empty-Job inactive/failed unit, which stays
            # `unit_not_active` and definitive.
            "unit_activating",
            "unit_deactivating",
            "unit_reloading",
            "unit_job_pending",
            "settling_budget_exhausted",
        }
    ),
}

#: The UNKNOWN family, named once so every layer that has to talk about "why
#: is there still no result" agrees on the same closed set: the authority's
#: own unresolved-event validation, the R0 job readback, and Home Assistant's
#: independent re-validation of what the backend sent.
#:
#: A whole-request refusal (`guest_unavailable` for an exact current LXC PVE
#: proves stopped, say) is classified from exactly this set, so a job that
#: reached no verdict can never be summarized by a token that only ever
#: describes a positive proof or a proven-false conjunct.
UNRESOLVED_HEALTH_REASONS: frozenset[str] = HEALTH_PROBE_REASONS_BY_OUTCOME[
    HealthProbeOutcome.UNKNOWN
]

#: Every probe kind that NAMES a target. `GUEST_OPERATIONAL` is deliberately
#: absent: it names no unit, so no target-shaped reason can describe it.
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
    # PR #80 final review: a probe kind that carries NO target can never
    # truthfully report a target-SHAPED reason. Both tokens are produced
    # only where a request-supplied target actually exists -- the systemd
    # batch's positional block-count check, and the remote-node
    # shell-inertness check over `data_arguments` -- and the
    # `guest_operational` family passes no data arguments at all.
    "probe_target_not_exact": frozenset(_TARGETED_HEALTH_PROBE_KINDS),
    "probe_target_ambiguous": frozenset(_TARGETED_HEALTH_PROBE_KINDS),
}


class HealthProbeSemanticError(ValueError):
    """A probe kind, outcome, and reason do not describe one coherent fact."""


def require_health_probe_semantics(
    kind: HealthProbeKind, outcome: HealthProbeOutcome, reason: object
) -> str:
    """Validate and return one coherent bounded observation reason."""

    if not isinstance(kind, HealthProbeKind):
        raise HealthProbeSemanticError("health probe kind is not supported")
    if not isinstance(outcome, HealthProbeOutcome):
        raise HealthProbeSemanticError("health probe outcome is not supported")
    if not isinstance(reason, str) or reason not in HEALTH_PROBE_REASONS:
        raise HealthProbeSemanticError(
            "health probe reason is not a known bounded token"
        )
    if reason not in HEALTH_PROBE_REASONS_BY_OUTCOME[outcome]:
        raise HealthProbeSemanticError(
            "reason that contradicts its own outcome"
        )
    allowed_kinds = HEALTH_PROBE_REASON_KINDS.get(reason)
    if allowed_kinds is not None and kind not in allowed_kinds:
        raise HealthProbeSemanticError(
            "reason impossible for that probe kind"
        )
    return reason
