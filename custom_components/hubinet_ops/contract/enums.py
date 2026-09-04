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


class PackageUpdateJobState(StrEnum):
    """The concise per-resource package-update job state in the snapshot.

    Derived by the backend from one durable job's ``status``, and nothing
    else. Home Assistant never infers it, never combines two polls to reach
    it, and never treats an absent job as a successful one.
    """

    #: The resource type has no package-update lifecycle at all.
    UNSUPPORTED = "unsupported"
    #: No update job has ever been issued for this resource.
    NOT_STARTED = "not_started"
    #: A job owns the one global destructive slot right now.
    ACTIVE = "active"
    #: Every declared probe of the job's frozen contract was proven.
    SUCCEEDED = "succeeded"
    #: The job stopped before mutating: plan drift, or stale authority.
    BLOCKED = "blocked"
    #: The job reached a terminal failure.
    FAILED = "failed"
    #: An operator's same-job rollback completed. Never "succeeded".
    ROLLED_BACK = "rolled_back"
    #: A restart terminalized a still-pre-mutation job.
    INTERRUPTED = "interrupted"
    #: The job needs an operator, and no automatic step remains.
    MANUAL_INTERVENTION = "manual_intervention"


class PackageUpdateHealthOutcome(StrEnum):
    """The definitive verdict a job recorded, when it recorded one.

    There is deliberately no ``unknown`` member: an evaluation that could not
    reach a verdict writes nothing durable, so ``None`` -- no verdict yet --
    is the only representation of it, and it is never a pass.
    """

    PASSED = "passed"
    FAILED = "failed"


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
