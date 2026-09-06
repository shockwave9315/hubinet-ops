"""Canonical immutable models for the snapshot contract."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import re
from types import MappingProxyType
from typing import Any

from .enums import (
    DetailStatus,
    HealthContractStatus,
    HealthProbeKind,
    HealthProbeOutcome,
    LifecycleState,
    NodeAvailability,
    ObservationalContinuity,
    PackageScanStatus,
    PackagePlanApprovalStatus,
    PackageUpdateHealthOutcome,
    PackageUpdateJobState,
    PresenceState,
    ResourceStateLevel,
    ResourceType,
    SecurityContinuity,
    SourceFreshness,
    SourceHealth,
    SourceHealthOrigin,
)
from .health_contract_validation import (
    validate_health_contract_summary,
    validate_health_probe,
    validate_resource_health_contract,
)
from .primitives import _immutable_mapping, _require_positive, _require_uuid_identity
from .package_scan_validation import validate_package_scan_snapshot
from .approval_validation import validate_package_plan_approval_snapshot
from .package_update_validation import (
    validate_package_update_job_summary,
    validate_package_update_job_view,
)
from .projections import (
    inventory_projection as _inventory_projection,
    source_reconciliation_projection as _source_reconciliation_projection,
)
from .resource_validation import validate_resource_snapshot
from .snapshot_validation import validate_snapshot
from .source_validation import (
    validate_inventory_source_snapshot,
    validate_source_context,
)
from .transition_validation import validate_transition


@dataclass(frozen=True, slots=True)
class BackendInformation:
    """Stable identity and version information for one backend instance."""

    backend_instance_id: str
    name: str
    version: str
    api_version: str

    def __post_init__(self) -> None:
        _require_uuid_identity(self.backend_instance_id, "backend_instance_id")


@dataclass(frozen=True, slots=True)
class SourceContext:
    """Exact current or committed source/transport provenance."""

    source_config_revision: int
    endpoint_id: str
    canonical_transport_locator: str
    canonicalization_contract_version: int
    transport_trust_revision: int

    def __post_init__(self) -> None:
        validate_source_context(self)


@dataclass(frozen=True, slots=True)
class InventorySourceSnapshot:
    """Published source identity, fixed health, freshness, and provenance."""

    inventory_source_id: str
    name: str
    provider_kind: str
    health: SourceHealth
    freshness: SourceFreshness
    health_origin: SourceHealthOrigin
    health_reason: str
    last_issued_run_sequence: int
    latest_completed_run_sequence: int | None
    latest_completed_outcome: str | None
    last_health_run_sequence: int | None
    last_run_health_outcome: str | None
    last_committed_run_sequence: int | None
    last_successful_observed_at: str | None
    freshness_reference_at: str | None
    freshness_valid_until: str | None
    current_context: SourceContext
    committed_context: SourceContext | None
    facts: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        validate_inventory_source_snapshot(self)

    @property
    def current_facts_available(self) -> bool:
        """Return backend-published current-fact eligibility for presentation."""

        return (
            self.health is SourceHealth.HEALTHY
            and self.freshness is SourceFreshness.FRESH
        )


@dataclass(frozen=True, slots=True)
class NodeSnapshot:
    """One backend-owned node record in a published source view."""

    node_id: str
    inventory_source_id: str
    name: str
    status: str
    available: bool = True
    facts: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        _require_uuid_identity(self.node_id, "node_id")
        _require_uuid_identity(self.inventory_source_id, "inventory_source_id")
        object.__setattr__(self, "facts", _immutable_mapping(self.facts))


@dataclass(frozen=True, slots=True)
class PackageScanOs:
    os_id: str
    version: str

    def __post_init__(self) -> None:
        if self.os_id not in {"debian", "ubuntu"}:
            raise ValueError("package scan OS id is unsupported")
        if not isinstance(self.version, str) or not self.version or len(self.version) > 200:
            raise ValueError("package scan OS version is invalid")


@dataclass(frozen=True, slots=True)
class PackageScanError:
    classification: str
    message: str

    def __post_init__(self) -> None:
        if self.classification not in {
            "guest_unavailable",
            "unsupported_resource_type",
            "unsupported_os",
            "package_manager_busy",
            "metadata_refresh_failed",
            "simulation_failed",
            "timeout",
            "malformed_plan",
            "stale_target",
            "execution_failed",
        }:
            raise ValueError("package scan error classification is invalid")
        if not isinstance(self.message, str) or not self.message or len(self.message) > 500:
            raise ValueError("package scan error message is invalid")


#: A dpkg/APT architecture string: 'all' (Architecture: all) or a real
#: architecture triplet such as 'amd64'/'i386'/'arm64'. See
#: ARCHITECTURE.md, "Binary package identity" -- (name, architecture) is
#: the durable binary-package identity, never name alone.
_ARCHITECTURE_RE = re.compile(r"[a-z][a-z0-9]*(-[a-z0-9]+)*")


@dataclass(frozen=True, slots=True)
class PackageScanPackage:
    name: str
    architecture: str
    installed_version: str
    candidate_version: str
    origin: str | None = None
    description: str | None = None
    security: bool | None = None

    def __post_init__(self) -> None:
        for value, field_name, maximum in (
            (self.name, "name", 300),
            (self.installed_version, "installed_version", 500),
            (self.candidate_version, "candidate_version", 500),
        ):
            if not isinstance(value, str) or not value or len(value) > maximum:
                raise ValueError(f"package scan {field_name} is invalid")
        if (
            not isinstance(self.architecture, str)
            or not (2 <= len(self.architecture) <= 32)
            or not _ARCHITECTURE_RE.fullmatch(self.architecture)
        ):
            raise ValueError("package scan architecture is invalid")
        for value, field_name in (
            (self.origin, "origin"),
            (self.description, "description"),
        ):
            if value is not None and (
                not isinstance(value, str) or not value or len(value) > 500
            ):
                raise ValueError(f"package scan {field_name} is invalid")
        if self.security not in {True, None}:
            raise ValueError("package scan security must be true or unknown")


@dataclass(frozen=True, slots=True)
class PackageScanSnapshot:
    status: PackageScanStatus = PackageScanStatus.NOT_SCANNED
    scan_run_id: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    os: PackageScanOs | None = None
    pending_count: int | None = None
    post_update_scan_pending: bool = False
    plan_fingerprint: str | None = None
    reboot_required: bool | None = None
    packages: tuple[PackageScanPackage, ...] = ()
    error: PackageScanError | None = None

    def __post_init__(self) -> None:
        validate_package_scan_snapshot(self)


@dataclass(frozen=True, slots=True)
class PackagePlanApprovalSnapshot:
    status: PackagePlanApprovalStatus = PackagePlanApprovalStatus.NONE
    approvable: bool = False
    approval_id: str | None = None
    reviewed_scan_run_id: str | None = None
    plan_fingerprint: str | None = None
    approved_at: str | None = None

    def __post_init__(self) -> None:
        validate_package_plan_approval_snapshot(self)


@dataclass(frozen=True, slots=True)
class HealthProbe:
    """One required typed probe: `kind` selects fixed argv, `target` is data.

    ``target`` is ``None`` for, and only for,
    ``HealthProbeKind.GUEST_OPERATIONAL`` -- that kind names no container or
    unit, and a faked target for it is exactly what the frozen design
    forbids.
    """

    kind: HealthProbeKind
    target: str | None

    def __post_init__(self) -> None:
        validate_health_probe(self)


@dataclass(frozen=True, slots=True)
class HealthContractSummary:
    """The per-resource health-contract fact carried in the snapshot.

    Identity only -- never the probe list, which belongs to the explicit
    health-contract action, and never a health result, which does not exist.
    """

    status: HealthContractStatus = HealthContractStatus.UNCONFIGURED
    revision: int | None = None
    fingerprint: str | None = None
    probe_count: int | None = None
    updated_at: str | None = None

    def __post_init__(self) -> None:
        validate_health_contract_summary(self)

    @property
    def configured(self) -> bool:
        """Whether a declared meaning of healthy exists for this resource."""

        return self.status is HealthContractStatus.CONFIGURED


@dataclass(frozen=True, slots=True)
class ResourceHealthContract:
    """One resource's complete health contract material, or its absence.

    ``probes`` is ``None`` when unconfigured, never an empty tuple: "no
    contract" and "a contract that requires nothing" are different claims, and
    only the first one is true.
    """

    resource_id: str
    status: HealthContractStatus = HealthContractStatus.UNCONFIGURED
    revision: int | None = None
    fingerprint: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    probes: tuple[HealthProbe, ...] | None = None

    def __post_init__(self) -> None:
        _require_uuid_identity(self.resource_id, "resource_id")
        validate_resource_health_contract(self)


@dataclass(frozen=True, slots=True)
class PackageUpdateJobSummary:
    """The per-resource package-update job fact carried in the snapshot.

    A concise state, not a replica of the event log. Everything detailed --
    the frozen package rows, the per-probe results, the append-only events --
    is response data from an explicitly invoked action.

    ``rollback_available`` here is durable CHECKPOINT ELIGIBILITY only --
    exactly what the immutable, revisioned snapshot may ever carry: whether
    this job's status/checkpoint could in principle accept a same-job
    rollback. It says nothing about whether the exact workload identity the
    job names is still current (that is a volatile fact and belongs outside
    snapshot revisioning -- see ``OperatorCapabilities.can_rollback_update``
    on ``OperatorAvailabilityView``, and ``PackageUpdateJobView.
    rollback_available`` for the explicit-action readback, both of which
    additionally require current-target validity). Never read this field to
    answer "is rollback currently available" -- present it, if at all, as
    history, not as an availability claim.
    """

    state: PackageUpdateJobState = PackageUpdateJobState.NOT_STARTED
    job_id: str | None = None
    checkpoint: str | None = None
    issued_at: str | None = None
    package_count: int | None = None
    health_outcome: PackageUpdateHealthOutcome | None = None
    health_started_at: str | None = None
    health_completed_at: str | None = None
    snapshot_confirmed_at: str | None = None
    mutation_completed_at: str | None = None
    rollback_available: bool = False
    rollback_completed_at: str | None = None
    terminalized_at: str | None = None
    terminal_reason: str | None = None

    def __post_init__(self) -> None:
        validate_package_update_job_summary(self)

    @property
    def in_progress(self) -> bool:
        """Whether a job owns the global destructive slot right now."""

        return self.state is PackageUpdateJobState.ACTIVE

    @property
    def rollback_completed(self) -> bool:
        return self.state is PackageUpdateJobState.ROLLED_BACK


@dataclass(frozen=True, slots=True)
class OperatorCapabilities:
    """One resource's point-in-time backend presentation availability.

    This object deliberately lives in ``OperatorAvailabilityView``, not in the
    revisioned inventory snapshot. Every mutation endpoint independently
    revalidates the complete rule when an operator presses a control.
    """

    can_review_update_plan: bool = False
    can_approve_update_plan: bool = False
    can_start_update: bool = False
    can_view_update_job: bool = False
    can_resume_update: bool = False
    can_rerun_health_evaluation: bool = False
    can_rollback_update: bool = False
    can_view_health_contract: bool = False
    can_configure_health_contract: bool = False

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"operator capability {name} must be a boolean")


@dataclass(frozen=True, slots=True)
class ResourceOperatorAvailability:
    """Current backend-owned presentation facts for one opaque resource."""

    resource_id: str
    capabilities: OperatorCapabilities

    def __post_init__(self) -> None:
        _require_uuid_identity(self.resource_id, "resource_id")
        if not isinstance(self.capabilities, OperatorCapabilities):
            raise ValueError("capabilities must be an OperatorCapabilities")


@dataclass(frozen=True, slots=True)
class OperatorAvailabilityView:
    """Volatile availability aligned to, but not versioned by, authority state."""

    backend_instance_id: str
    authority_published_state_revision: int
    resources: tuple[ResourceOperatorAvailability, ...]

    def __post_init__(self) -> None:
        _require_uuid_identity(self.backend_instance_id, "backend_instance_id")
        _require_positive(
            self.authority_published_state_revision,
            "authority_published_state_revision",
        )
        object.__setattr__(self, "resources", tuple(self.resources))
        resource_ids = {resource.resource_id for resource in self.resources}
        if len(resource_ids) != len(self.resources):
            raise ValueError("operator availability contains duplicate resources")

    @property
    def resources_by_id(self) -> Mapping[str, OperatorCapabilities]:
        """Return current availability keyed by backend resource identity."""

        return MappingProxyType(
            {
                resource.resource_id: resource.capabilities
                for resource in self.resources
            }
        )


@dataclass(frozen=True, slots=True)
class PackageUpdateJobEvent:
    """One bounded append-only job event, as an operator reads it.

    ``details`` is deliberately not carried across this boundary. The backend
    authors it as bounded typed data for direct API consumers, but a mapping
    of arbitrary shape rendered into a Home Assistant action response is
    exactly the kind of unbounded detail this integration keeps out. What an
    operator needs here is the classification and the sentence.
    """

    sequence: int
    created_at: str
    level: str
    stage: str
    event_type: str
    message: str


@dataclass(frozen=True, slots=True)
class PackageUpdateJobHealthProbeResult:
    """One frozen probe's health evidence, as an operator reads it.

    Every field is a bounded typed authority fact -- never raw helper
    stdout/stderr and never command text. ``kind``/``target`` are this job's
    own frozen probe material (already shown by ``view_health_contract``);
    ``outcome``/``checked_at``/``reason`` are what was observed for it.

    ``definitive`` distinguishes the two evidence kinds a job's
    ``health_evidence`` can carry (frozen post-Human1 health architecture,
    Stage 2): ``True`` means a durable, non-recheckable PASSED/FAILED
    verdict (``health_evidence == "verdict"``); ``False`` means an unresolved
    evaluation's bounded OBSERVATION evidence (``health_evidence ==
    "observation"``) -- never a verdict, and never laundered into one by
    re-running. Empty for a job with neither yet.
    """

    probe_index: int
    kind: HealthProbeKind
    #: ``None`` for, and only for, ``HealthProbeKind.GUEST_OPERATIONAL``.
    target: str | None
    outcome: HealthProbeOutcome
    checked_at: str
    reason: str
    definitive: bool = True


@dataclass(frozen=True, slots=True)
class PackageUpdateJobView:
    """One complete package-update job, as an explicit action returns it.

    Response data from an action an operator invoked, never entity state.
    Flat by design: Home Assistant renders these as a response mapping, and a
    nested shape would only invite a template to reach into it. Every field
    but one is a durable authority fact -- no helper output, no PVE task log,
    no command text, and no package rows. ``health_probes`` IS included
    (post-Human1 correction): a real operator had to read the backend's
    SQLite database directly to learn which frozen probe failed and why, so
    the per-probe evidence this stage already computes -- kind, target,
    outcome, checked-at, and a bounded reason token -- is now part of the
    explicit readback too. It stays empty until a definitive verdict exists.

    ``rollback_available`` is the one *authority-freshness* exception, and
    deliberately so (GitHub
    review P2 #3, Option A): it is the backend's own fresh, current-target-
    checked verdict -- the same proof
    ``InventoryAuthority.arm_package_update_rollback`` requires -- not merely
    a durable checkpoint fact. This is the one meaning every operator-visible
    "rollback available" in this integration converges on; contrast
    ``PackageUpdateJobSummary.rollback_available``, which is durable
    checkpoint eligibility only.
    """

    job_id: str
    request_id: str
    resource_id: str
    status: PackageUpdateJobState
    checkpoint: str
    issued_at: str
    approved_plan_fingerprint: str
    package_count: int
    snapshot_name: str | None = None
    snapshot_confirmed_at: str | None = None
    mutation_may_have_started_at: str | None = None
    mutation_completed_at: str | None = None
    health_contract_revision: int | None = None
    health_started_at: str | None = None
    health_completed_at: str | None = None
    health_outcome: PackageUpdateHealthOutcome | None = None
    rollback_may_have_started_at: str | None = None
    rollback_completed_at: str | None = None
    rollback_available: bool = False
    terminalized_at: str | None = None
    terminal_reason: str | None = None
    events: tuple[PackageUpdateJobEvent, ...] = ()
    health_probes: tuple[PackageUpdateJobHealthProbeResult, ...] = ()
    #: ``None`` (nothing to show yet), ``"observation"`` (bounded per-probe
    #: evidence from an unresolved evaluation), or ``"verdict"`` (a durable
    #: definitive result) -- see `PackageUpdateJobHealthProbeResult.definitive`.
    health_evidence: str | None = None

    def __post_init__(self) -> None:
        _require_uuid_identity(self.job_id, "job_id")
        _require_uuid_identity(self.request_id, "request_id")
        _require_uuid_identity(self.resource_id, "resource_id")
        validate_package_update_job_view(self)


@dataclass(frozen=True, slots=True)
class ResourceSnapshot:
    """One backend-owned resource incarnation and effective presentation view."""

    resource_id: str
    inventory_source_id: str
    active_binding_id: str | None
    resource_type: ResourceType
    vmid: int
    locator_generation: int
    resource_continuity_revision: int
    name: str
    status: str
    current_node_id: str | None
    last_known_node_id: str | None
    presence: PresenceState
    lifecycle: LifecycleState
    observational_continuity: ObservationalContinuity
    security_continuity: SecurityContinuity
    detail_status: DetailStatus
    node_availability: NodeAvailability
    state_level: ResourceStateLevel = ResourceStateLevel.DISCOVERED
    retained_policy: Mapping[str, Any] = field(
        default_factory=lambda: MappingProxyType({})
    )
    effective_policy: Mapping[str, Any] = field(
        default_factory=lambda: MappingProxyType({})
    )
    policy_applicable: bool = False
    suspended_reason: str | None = None
    effective_capabilities: frozenset[str] = field(default_factory=frozenset)
    state: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    package_scan: PackageScanSnapshot = field(default_factory=PackageScanSnapshot)
    package_plan_approval: PackagePlanApprovalSnapshot = field(
        default_factory=PackagePlanApprovalSnapshot
    )
    health_contract: HealthContractSummary = field(
        default_factory=HealthContractSummary
    )
    package_update_job: PackageUpdateJobSummary = field(
        default_factory=PackageUpdateJobSummary
    )
    termination_reason: str | None = None
    successor_resource_id: str | None = None

    def __post_init__(self) -> None:
        validate_resource_snapshot(self)

    @property
    def relation_node_id(self) -> str | None:
        """Return current or last-known presentation relation, if available."""

        return self.current_node_id or self.last_known_node_id


@dataclass(frozen=True, slots=True)
class HubinetOpsSnapshot:
    """One logical immutable backend view at a published-state revision."""

    backend: BackendInformation
    sources: tuple[InventorySourceSnapshot, ...]
    nodes: tuple[NodeSnapshot, ...]
    resources: tuple[ResourceSnapshot, ...]
    inventory_revision: int
    published_state_revision: int
    published_at: str

    def __post_init__(self) -> None:
        validate_snapshot(self)

    @property
    def sources_by_id(self) -> Mapping[str, InventorySourceSnapshot]:
        """Return sources keyed only by backend-owned source identity."""

        return MappingProxyType(
            {source.inventory_source_id: source for source in self.sources}
        )

    @property
    def nodes_by_id(self) -> Mapping[str, NodeSnapshot]:
        """Return nodes keyed only by backend-owned node identity."""

        return MappingProxyType({node.node_id: node for node in self.nodes})

    @property
    def resources_by_id(self) -> Mapping[str, ResourceSnapshot]:
        """Return resources keyed only by opaque backend resource identity."""

        return MappingProxyType(
            {resource.resource_id: resource for resource in self.resources}
        )

    @property
    def current_resources_by_locator(
        self,
    ) -> Mapping[tuple[str, int], ResourceSnapshot]:
        """Resolve current incumbents through active bindings, never VMID ordering."""

        return MappingProxyType(
            {
                (resource.inventory_source_id, resource.vmid): resource
                for resource in self.resources
                if resource.active_binding_id is not None
            }
        )

    @property
    def inventory_projection(self) -> tuple[tuple[Any, ...], ...]:
        """Return the explicit inventory-owned portion of this published view."""

        return _inventory_projection(self)

    @property
    def source_reconciliation_projection(
        self,
    ) -> Mapping[str, tuple[Any, ...]]:
        """Return successful discovery/reconciliation-owned state per source."""

        return _source_reconciliation_projection(self)

    def validate_revision_successor(self, previous: HubinetOpsSnapshot) -> None:
        """Reject regressing or mutable views for an existing backend entry."""

        validate_transition(previous, self)
