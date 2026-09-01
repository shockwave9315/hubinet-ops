"""Canonical immutable models for the snapshot contract."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import re
from types import MappingProxyType
from typing import Any

from .enums import (
    DetailStatus,
    LifecycleState,
    NodeAvailability,
    ObservationalContinuity,
    PackageScanStatus,
    PackagePlanApprovalStatus,
    PresenceState,
    ResourceStateLevel,
    ResourceType,
    SecurityContinuity,
    SourceFreshness,
    SourceHealth,
    SourceHealthOrigin,
)
from .primitives import _immutable_mapping, _require_uuid_identity
from .package_scan_validation import validate_package_scan_snapshot
from .approval_validation import validate_package_plan_approval_snapshot
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
