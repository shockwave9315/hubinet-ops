"""Canonical enumerations for the snapshot contract."""

from enum import StrEnum


class ResourceType(StrEnum):
    """Immutable occupant types exposed by Hubinet Ops inventory."""

    QEMU = "qemu"
    LXC = "lxc"


class ResourceStateLevel(StrEnum):
    """Backend-owned policy/state level for a resource."""

    DISCOVERED = "discovered"
    OBSERVED = "observed"
    MANAGED = "managed"
    MAINTENANCE = "maintenance"
    BREAK_GLASS = "break_glass"


class PresenceState(StrEnum):
    """Canonical relation of one incarnation to its source-local slot."""

    PRESENT = "present"
    MISSING = "missing"
    CONFIRMED_REMOVED = "confirmed_removed"
    NOT_CURRENT = "not_current"


class LifecycleState(StrEnum):
    """Canonical inventory lifecycle axis."""

    ACTIVE = "active"
    QUARANTINED = "quarantined"
    RETIRED = "retired"


class ObservationalContinuity(StrEnum):
    """Backend assessment of observational incarnation continuity."""

    CONSISTENT = "consistent"
    UNCERTAIN = "uncertain"
    REPLACED = "replaced"


class SecurityContinuity(StrEnum):
    """Backend-owned security continuity axis."""

    UNVERIFIED = "unverified"
    TRUSTED = "trusted"
    REVOKED = "revoked"


class DetailStatus(StrEnum):
    """Reconciled status of the current per-resource detail read."""

    OK = "ok"
    TEMPORARILY_UNAVAILABLE = "temporarily_unavailable"
    ERROR = "error"
    NOT_APPLICABLE = "not_applicable"


class NodeAvailability(StrEnum):
    """Availability of the resource's current node relation."""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    UNRESOLVED = "unresolved"
    NOT_APPLICABLE = "not_applicable"


class SourceHealth(StrEnum):
    """Current backend-owned observation health of one inventory source."""

    HEALTHY = "healthy"
    SOURCE_UNAVAILABLE = "source_unavailable"
    DEGRADED = "degraded"
    CONFIGURATION_ERROR = "configuration_error"
    NOT_YET_OBSERVED = "not_yet_observed"


class SourceFreshness(StrEnum):
    """Current backend-materialized freshness of one inventory source."""

    FRESH = "fresh"
    STALE = "stale"
    NOT_YET_OBSERVED = "not_yet_observed"


class SourceHealthOrigin(StrEnum):
    """Provenance class for current source health/freshness."""

    DISCOVERY_RUN = "discovery_run"
    CONTROLLED_CONTEXT_TRANSITION = "controlled_context_transition"
    TIME_EXPIRY = "time_expiry"
    INITIAL = "initial"


class PackageScanStatus(StrEnum):
    UNSUPPORTED = "unsupported"
    NOT_SCANNED = "not_scanned"
    SCANNING = "scanning"
    SUCCESS = "success"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    UNAVAILABLE = "unavailable"


class PackagePlanApprovalStatus(StrEnum):
    NONE = "none"
    APPROVED = "approved"
    STALE = "stale"


class HealthProbeKind(StrEnum):
    """The exact typed probes an operator may declare in a health contract."""

    SYSTEMD_UNIT_ACTIVE = "systemd_unit_active"
    DOCKER_CONTAINER_RUNNING = "docker_container_running"
    DOCKER_CONTAINER_HEALTHY = "docker_container_healthy"


class HealthContractStatus(StrEnum):
    """Whether a resource has a declared meaning of healthy.

    `UNCONFIGURED` is not a health verdict and never a passing one -- it says
    the operator has not declared what healthy means for this workload. There
    is no health-RESULT enum here, because no health execution exists.
    """

    UNSUPPORTED = "unsupported"
    UNCONFIGURED = "unconfigured"
    CONFIGURED = "configured"
