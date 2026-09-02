"""Shared semantic rules for typed health-probe observations."""

from __future__ import annotations

from .models import HealthProbeKind, HealthProbeOutcome


# Closed durable taxonomy.  Raw guest output never becomes a reason.
HEALTH_PROBE_REASONS: frozenset[str] = frozenset(
    {
        "unit_active",
        "container_running",
        "container_healthy",
        "unit_not_active",
        "container_not_running",
        "container_absent",
        "container_unhealthy",
        "container_health_starting",
        "container_has_no_healthcheck",
        "probe_target_not_exact",
        "probe_target_ambiguous",
        "guest_unavailable",
        "command_failed",
        "command_timed_out",
        "malformed_output",
        "docker_daemon_unavailable",
        "host_unreachable",
        "host_response_rejected",
        "resource_context_changed",
    }
)

HEALTH_PROBE_REASONS_BY_OUTCOME: dict[HealthProbeOutcome, frozenset[str]] = {
    HealthProbeOutcome.PASSED: frozenset(
        {"unit_active", "container_running", "container_healthy"}
    ),
    HealthProbeOutcome.FAILED: frozenset(
        {
            "unit_not_active",
            "container_not_running",
            "container_absent",
            "container_unhealthy",
            "container_health_starting",
            "container_has_no_healthcheck",
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
            "docker_daemon_unavailable",
            "host_unreachable",
            "host_response_rejected",
            "resource_context_changed",
        }
    ),
}

HEALTH_PROBE_REASON_KINDS: dict[str, frozenset[HealthProbeKind]] = {
    "unit_active": frozenset({HealthProbeKind.SYSTEMD_UNIT_ACTIVE}),
    "unit_not_active": frozenset({HealthProbeKind.SYSTEMD_UNIT_ACTIVE}),
    "container_running": frozenset({HealthProbeKind.DOCKER_CONTAINER_RUNNING}),
    "container_healthy": frozenset({HealthProbeKind.DOCKER_CONTAINER_HEALTHY}),
    "container_not_running": frozenset(
        {
            HealthProbeKind.DOCKER_CONTAINER_RUNNING,
            HealthProbeKind.DOCKER_CONTAINER_HEALTHY,
        }
    ),
    "container_absent": frozenset(
        {
            HealthProbeKind.DOCKER_CONTAINER_RUNNING,
            HealthProbeKind.DOCKER_CONTAINER_HEALTHY,
        }
    ),
    "container_unhealthy": frozenset({HealthProbeKind.DOCKER_CONTAINER_HEALTHY}),
    "container_health_starting": frozenset(
        {HealthProbeKind.DOCKER_CONTAINER_HEALTHY}
    ),
    "container_has_no_healthcheck": frozenset(
        {HealthProbeKind.DOCKER_CONTAINER_HEALTHY}
    ),
    "docker_daemon_unavailable": frozenset(
        {
            HealthProbeKind.DOCKER_CONTAINER_RUNNING,
            HealthProbeKind.DOCKER_CONTAINER_HEALTHY,
        }
    ),
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
