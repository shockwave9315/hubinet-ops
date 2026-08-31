"""Immutable value objects for the Hubinet Ops 0.5 authority core."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from collections.abc import Mapping


class EndpointLifecycle(StrEnum):
    ACTIVE = "active"
    CANDIDATE = "candidate"
    INACTIVE = "inactive"
    RETIRED = "retired"


class DiscoveryRunLifecycle(StrEnum):
    ISSUED = "issued"
    RUNNING = "running"
    COMPLETED = "completed"
    ABANDONED = "abandoned"


class PackageScanLifecycle(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"


class PackageScanOutcome(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class PackageUpdateJobStatus(StrEnum):
    ACTIVE = "active"
    SUCCEEDED = "succeeded"
    BLOCKED = "blocked"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"
    INTERRUPTED = "interrupted"
    MANUAL_INTERVENTION = "manual_intervention"


class PackageUpdateCheckpoint(StrEnum):
    ISSUED = "issued"
    PREFLIGHT_PASSED = "preflight_passed"
    SNAPSHOT_MAY_HAVE_STARTED = "snapshot_may_have_started"
    SNAPSHOT_CONFIRMED = "snapshot_confirmed"
    MUTATION_MAY_HAVE_STARTED = "mutation_may_have_started"
    MUTATION_COMPLETED = "mutation_completed"
    HEALTH_STARTED = "health_started"
    ROLLBACK_MAY_HAVE_STARTED = "rollback_may_have_started"
    ROLLBACK_COMPLETED = "rollback_completed"


#: Durable checkpoint order. ``snapshot_may_have_started`` is the write-ahead
#: uncertainty boundary: once a job reaches it, a PVE snapshot mutation may
#: already have been submitted, so nothing may ever conclude that no PVE
#: mutation happened. Used by the schema and by every typed transition; the
#: SQL CHECK/trigger set in ``store.py`` encodes exactly this order.
CHECKPOINT_ORDER: tuple[PackageUpdateCheckpoint, ...] = (
    PackageUpdateCheckpoint.ISSUED,
    PackageUpdateCheckpoint.PREFLIGHT_PASSED,
    PackageUpdateCheckpoint.SNAPSHOT_MAY_HAVE_STARTED,
    PackageUpdateCheckpoint.SNAPSHOT_CONFIRMED,
    PackageUpdateCheckpoint.MUTATION_MAY_HAVE_STARTED,
    PackageUpdateCheckpoint.MUTATION_COMPLETED,
    PackageUpdateCheckpoint.HEALTH_STARTED,
    PackageUpdateCheckpoint.ROLLBACK_MAY_HAVE_STARTED,
    PackageUpdateCheckpoint.ROLLBACK_COMPLETED,
)

_CHECKPOINT_RANKS = {
    checkpoint: rank for rank, checkpoint in enumerate(CHECKPOINT_ORDER, start=1)
}


def checkpoint_rank(checkpoint: PackageUpdateCheckpoint) -> int:
    """Return the durable ordinal of one package-update checkpoint."""

    try:
        return _CHECKPOINT_RANKS[PackageUpdateCheckpoint(checkpoint)]
    except (KeyError, ValueError) as exc:
        raise AuthorityInvariantError(
            "unknown package update checkpoint"
        ) from exc


class PackageUpdateEventLevel(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class PackageUpdateEventType(StrEnum):
    JOB_ISSUED = "job_issued"
    RESTART_INTERRUPTED = "restart_interrupted"
    PREFLIGHT_PASSED = "preflight_passed"
    SNAPSHOT_INTENT_RECORDED = "snapshot_intent_recorded"
    SNAPSHOT_TASK_OBSERVED = "snapshot_task_observed"
    SNAPSHOT_CONFIRMED = "snapshot_confirmed"
    SNAPSHOT_OUTCOME_UNCERTAIN = "snapshot_outcome_uncertain"
    SNAPSHOT_FAILED = "snapshot_failed"


class PackageScanFailure(StrEnum):
    GUEST_UNAVAILABLE = "guest_unavailable"
    UNSUPPORTED_RESOURCE_TYPE = "unsupported_resource_type"
    UNSUPPORTED_OS = "unsupported_os"
    PACKAGE_MANAGER_BUSY = "package_manager_busy"
    METADATA_REFRESH_FAILED = "metadata_refresh_failed"
    SIMULATION_FAILED = "simulation_failed"
    TIMEOUT = "timeout"
    MALFORMED_PLAN = "malformed_plan"
    STALE_TARGET = "stale_target"
    EXECUTION_FAILED = "execution_failed"


class PersistentSourceHealth(StrEnum):
    HEALTHY = "healthy"
    SOURCE_UNAVAILABLE = "source_unavailable"
    DEGRADED = "degraded"
    CONFIGURATION_ERROR = "configuration_error"
    NOT_YET_OBSERVED = "not_yet_observed"


class PersistentSourceFreshness(StrEnum):
    FRESH = "fresh"
    STALE = "stale"
    NOT_YET_OBSERVED = "not_yet_observed"


class PersistentSourceHealthOrigin(StrEnum):
    DISCOVERY_RUN = "discovery_run"
    CONTROLLED_CONTEXT_TRANSITION = "controlled_context_transition"
    TIME_EXPIRY = "time_expiry"
    INITIAL = "initial"


class AuthorityError(RuntimeError):
    """Base class for durable authority failures."""


class AuthorityDatabaseRejected(AuthorityError):
    """The database is not a recognized Hubinet Ops 0.5 authority database."""


class AuthorityConflict(AuthorityError):
    """A durable ownership or lifecycle precondition did not hold."""


class AuthorityNotFound(AuthorityError):
    """A requested durable authority record does not exist."""


class AuthorityInvariantError(AuthorityError):
    """Persisted state violates an invariant required by the authority layer."""


@dataclass(frozen=True, slots=True)
class BackendInstance:
    backend_instance_id: str
    created_at: str
    inventory_revision: int
    published_state_revision: int
    published_at: str


@dataclass(frozen=True, slots=True)
class InventorySource:
    inventory_source_id: str
    backend_instance_id: str
    provider_kind: str
    display_name: str
    credential_reference: str
    created_at: str
    source_config_revision: int
    last_issued_run_sequence: int
    last_committed_run_sequence: int | None
    active_discovery_run_id: str | None
    provider_contract_version: int
    freshness_duration_seconds: int
    facts: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class SourceEndpoint:
    endpoint_id: str
    inventory_source_id: str
    canonical_transport_locator: str
    canonicalization_contract_version: int
    lifecycle: EndpointLifecycle
    transport_trust_revision: int
    created_at: str


@dataclass(frozen=True, slots=True)
class SourceRuntimeHealth:
    inventory_source_id: str
    health: PersistentSourceHealth
    freshness: PersistentSourceFreshness
    health_origin: PersistentSourceHealthOrigin
    health_reason: str
    latest_completed_run_sequence: int | None
    latest_completed_outcome: str | None
    last_health_run_sequence: int | None
    last_run_health_outcome: str | None
    last_successful_observed_at: str | None
    freshness_reference_at: str | None
    freshness_valid_until: str | None
    committed_source_config_revision: int | None
    committed_endpoint_id: str | None
    committed_canonical_transport_locator: str | None
    committed_canonicalization_contract_version: int | None
    committed_transport_trust_revision: int | None


@dataclass(frozen=True, slots=True)
class DiscoveryRun:
    run_id: str
    inventory_source_id: str
    discovery_run_sequence: int
    issued_at: str
    expected_source_config_revision: int
    expected_endpoint_id: str
    expected_canonical_transport_locator: str
    expected_canonicalization_contract_version: int
    expected_transport_trust_revision: int
    provider_contract_version: int
    lifecycle: DiscoveryRunLifecycle
    terminalized_at: str | None
    terminal_reason: str | None
    completed_at: str | None
    provider_outcome: str | None
    observed_at: str | None
    normalized_snapshot_hash: str | None
    baseline_completeness: str | None
    source_availability: str | None
    baseline_mode: str | None
    permission_coverage_complete: bool | None
    boundary_consistent: bool | None
    covered_nodes: tuple[str, ...] | None
    failed_baseline_scopes: tuple[str, ...] | None
    acl_topology_hash_before: str | None
    acl_topology_hash_after: str | None
    permission_snapshot_hash_before: str | None
    permission_snapshot_hash_after: str | None
    detail_ok_count: int | None
    detail_temporarily_unavailable_count: int | None
    detail_error_count: int | None
    failed_detail_scopes: tuple[str, ...] | None
    completion_source_config_revision: int | None
    completion_endpoint_id: str | None
    completion_canonical_transport_locator: str | None
    completion_canonicalization_contract_version: int | None
    completion_transport_trust_revision: int | None


@dataclass(frozen=True, slots=True)
class InventorySourceState:
    source: InventorySource
    active_endpoint: SourceEndpoint
    runtime_health: SourceRuntimeHealth


@dataclass(frozen=True, slots=True)
class InventoryNode:
    node_id: str
    inventory_source_id: str
    external_node_name: str
    status: str
    available: bool
    facts: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ResourceIncarnation:
    resource_id: str
    inventory_source_id: str
    resource_type: str
    vmid: int
    resource_continuity_revision: int
    name: str
    status: str
    current_node_id: str | None
    last_known_node_id: str | None
    presence: str
    lifecycle: str
    observational_continuity: str
    security_continuity: str
    detail_status: str
    node_availability: str
    state_level: str
    active_binding_id: str | None
    locator_generation: int
    facts: Mapping[str, Any]
    termination_reason: str | None
    successor_resource_id: str | None


@dataclass(frozen=True, slots=True)
class ResourceLocatorBinding:
    binding_id: str
    inventory_source_id: str
    vmid: int
    locator_generation: int
    resource_id: str
    valid_from_run_sequence: int
    valid_to_run_sequence: int | None
    closure_reason: str | None


@dataclass(frozen=True, slots=True)
class ResourceTermination:
    """Retained terminal/tombstone record.

    The terminal/tombstone owner for direct replacement
    (``reason='replaced'``). ``run_sequence`` is always the exact discovery
    run that supplied the observation provenance for the closure.
    """

    resource_id: str
    inventory_source_id: str
    binding_id: str
    locator_generation: int
    reason: str
    successor_resource_id: str | None
    run_sequence: int
    created_at: str


@dataclass(frozen=True, slots=True)
class PackageScanPackage:
    package_name: str
    installed_version: str
    candidate_version: str
    origin: str | None = None
    description: str | None = None
    security: bool | None = None


@dataclass(frozen=True, slots=True)
class PackageScanRun:
    scan_run_id: str
    resource_id: str
    inventory_source_id: str
    committed_source_config_revision: int
    committed_endpoint_id: str
    committed_canonical_transport_locator: str
    committed_canonicalization_contract_version: int
    committed_transport_trust_revision: int
    provider_contract_version: int
    attempt_sequence: int
    expected_binding_id: str
    expected_locator_generation: int
    expected_resource_continuity_revision: int
    expected_vmid: int
    expected_node_id: str
    expected_node_name: str
    started_at: str
    lifecycle: PackageScanLifecycle
    completed_at: str | None
    outcome: PackageScanOutcome | None
    failure_class: PackageScanFailure | None
    error_message: str | None
    os_id: str | None
    os_version: str | None
    pending_count: int | None
    plan_fingerprint: str | None
    reboot_required: bool | None
    packages: tuple[PackageScanPackage, ...] = ()


@dataclass(frozen=True, slots=True)
class PackagePlanApproval:
    approval_id: str
    resource_id: str
    reviewed_scan_run_id: str
    approved_plan_fingerprint: str
    approved_at: str


@dataclass(frozen=True, slots=True)
class PackageUpdateJobPackage:
    package_index: int
    package_name: str
    installed_version: str
    candidate_version: str
    origin: str | None = None
    description: str | None = None
    security: bool | None = None


@dataclass(frozen=True, slots=True)
class PackageUpdateJob:
    job_id: str
    request_id: str
    issued_at: str
    resource_id: str
    approval_id: str
    approval_reviewed_scan_run_id: str
    approved_plan_fingerprint: str
    approval_approved_at: str
    current_plan_scan_run_id: str
    inventory_source_id: str
    committed_source_config_revision: int
    committed_endpoint_id: str
    committed_canonical_transport_locator: str
    committed_canonicalization_contract_version: int
    committed_transport_trust_revision: int
    provider_contract_version: int
    expected_resource_type: str
    expected_binding_id: str
    expected_locator_generation: int
    expected_resource_continuity_revision: int
    expected_vmid: int
    expected_node_id: str
    expected_node_name: str
    package_count: int
    status: PackageUpdateJobStatus
    checkpoint: PackageUpdateCheckpoint
    snapshot_operation_id: str | None
    snapshot_name: str | None
    snapshot_intent_recorded_at: str | None
    snapshot_task_upid: str | None
    snapshot_confirmed_at: str | None
    mutation_may_have_started_at: str | None
    mutation_completed_at: str | None
    health_started_at: str | None
    rollback_may_have_started_at: str | None
    rollback_completed_at: str | None
    terminalized_at: str | None
    terminal_reason: str | None
    packages: tuple[PackageUpdateJobPackage, ...] = ()


@dataclass(frozen=True, slots=True)
class PackageUpdateSnapshotIdentity:
    """One package update job's single deterministic pre-update snapshot.

    Both fields are derived from immutable job identity, so the exact same
    job derives the exact same identity after any restart. ``snapshot_name``
    is the physical PVE key; ``snapshot_operation_id`` keys the host-side
    durable operation journal.
    """

    snapshot_operation_id: str
    snapshot_name: str


@dataclass(frozen=True, slots=True)
class SnapshotOwnership:
    """Strict structured Hubinet ownership metadata carried by a snapshot.

    This, never the snapshot name, is the ownership proof. Anything that does
    not parse into exactly this shape is treated as malformed and fails
    closed rather than being silently skipped.
    """

    protocol: int
    kind: str
    job_id: str
    resource_id: str
    resource_continuity_revision: int
    inventory_source_id: str
    backend_instance_id: str


@dataclass(frozen=True, slots=True)
class ObservedSnapshot:
    """One canonical observation of a PVE LXC snapshot listing entry.

    ``is_current_pseudo_entry`` marks PVE's synthetic ``current`` row, which
    is never a real snapshot. ``incomplete`` marks an entry PVE still reports
    a ``snapstate`` for, i.e. a snapshot operation that has not finished.
    ``ownership_malformed`` marks an entry that looks Hubinet-owned but whose
    metadata did not parse strictly.
    """

    name: str
    description: str
    is_current_pseudo_entry: bool = False
    incomplete: bool = False
    snaptime: int | None = None
    parent: str | None = None
    ownership: SnapshotOwnership | None = None
    ownership_malformed: bool = False


@dataclass(frozen=True, slots=True)
class PackageUpdateRollbackTarget:
    """The one snapshot a specific package update job may roll back to."""

    job_id: str
    resource_id: str
    expected_vmid: int
    expected_node_name: str
    snapshot_name: str
    snapshot_operation_id: str
    snapshot_confirmed_at: str


@dataclass(frozen=True, slots=True)
class PackageUpdateJobEvent:
    job_id: str
    sequence: int
    created_at: str
    level: PackageUpdateEventLevel
    stage: PackageUpdateCheckpoint
    event_type: PackageUpdateEventType
    message: str
    details: Mapping[str, Any]
