"""Canonical enumerations for the Phase 0 snapshot contract."""

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
