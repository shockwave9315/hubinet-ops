"""Typed read-only contract for the authoritative Hubinet Ops snapshot.

This module intentionally does not define provisional HTTP endpoint paths. Phase 0
uses an injected transport so the backend 0.5 API can be finalized independently.
Home Assistant validates and presents a committed backend view; it never reconciles
inventory identity, presence, continuity, policy, or source freshness locally.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Protocol


class HubinetOpsApiError(RuntimeError):
    """Base exception raised by the Hubinet Ops API contract."""


class HubinetOpsInvalidAuth(HubinetOpsApiError):
    """The Hubinet Ops backend rejected the bearer token."""


class HubinetOpsCannotConnect(HubinetOpsApiError):
    """The Hubinet Ops backend could not be reached."""


class HubinetOpsInvalidResponse(HubinetOpsApiError):
    """The Hubinet Ops backend returned data outside the typed contract."""


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


def _deep_freeze(value: Any) -> Any:
    """Recursively freeze one JSON-like backend snapshot value."""

    if value is None or type(value) in {str, int, float, bool}:
        return value
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("snapshot mapping keys must be strings")
        return MappingProxyType(
            {key: _deep_freeze(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_deep_freeze(item) for item in value)
    raise TypeError(
        f"snapshot values must be JSON-like, got {type(value).__name__}"
    )


def _immutable_mapping(value: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """Return a recursively immutable copy of a JSON-like mapping."""

    frozen = _deep_freeze(value or {})
    assert isinstance(frozen, Mapping)
    return frozen


def _require_text(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must not be empty")


def _require_positive(value: int, field_name: str) -> None:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer")


def _require_optional_positive(value: int | None, field_name: str) -> None:
    if value is not None:
        _require_positive(value, field_name)


@dataclass(frozen=True, slots=True)
class BackendInformation:
    """Stable identity and version information for one backend instance."""

    backend_instance_id: str
    name: str
    version: str
    api_version: str

    def __post_init__(self) -> None:
        _require_text(self.backend_instance_id, "backend_instance_id")


@dataclass(frozen=True, slots=True)
class SourceContext:
    """Exact current or committed source/transport provenance."""

    source_config_revision: int
    endpoint_id: str
    canonical_transport_locator: str
    canonicalization_contract_version: int
    transport_trust_revision: int

    def __post_init__(self) -> None:
        _require_positive(self.source_config_revision, "source_config_revision")
        _require_text(self.endpoint_id, "endpoint_id")
        _require_text(
            self.canonical_transport_locator, "canonical_transport_locator"
        )
        _require_positive(
            self.canonicalization_contract_version,
            "canonicalization_contract_version",
        )
        _require_positive(
            self.transport_trust_revision, "transport_trust_revision"
        )


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
        _require_text(self.inventory_source_id, "inventory_source_id")
        _require_text(self.name, "source name")
        _require_text(self.provider_kind, "provider_kind")
        if (
            type(self.last_issued_run_sequence) is not int
            or self.last_issued_run_sequence < 0
        ):
            raise ValueError("last_issued_run_sequence must be a non-negative integer")
        for field_name in (
            "latest_completed_run_sequence",
            "last_health_run_sequence",
            "last_committed_run_sequence",
        ):
            _require_optional_positive(getattr(self, field_name), field_name)
        self._validate_sequence_pair(
            self.latest_completed_run_sequence,
            self.latest_completed_outcome,
            "latest completed run",
        )
        self._validate_sequence_pair(
            self.last_health_run_sequence,
            self.last_run_health_outcome,
            "last health run",
        )
        sequences = (
            self.latest_completed_run_sequence,
            self.last_health_run_sequence,
            self.last_committed_run_sequence,
        )
        if any(
            sequence is not None and sequence > self.last_issued_run_sequence
            for sequence in sequences
        ):
            raise ValueError("source run sequence exceeds last issued sequence")

        successful_fields = (
            self.last_successful_observed_at,
            self.freshness_reference_at,
            self.freshness_valid_until,
            self.committed_context,
        )
        has_successful_commit = self.last_committed_run_sequence is not None
        if (
            has_successful_commit
            and not all(item is not None for item in successful_fields)
        ) or (
            not has_successful_commit
            and any(item is not None for item in successful_fields)
        ):
            raise ValueError(
                "committed run, fixed freshness facts, and committed context must be published together"
            )
        for field_name in (
            "last_successful_observed_at",
            "freshness_reference_at",
            "freshness_valid_until",
        ):
            value = getattr(self, field_name)
            if value is not None:
                _require_text(value, field_name)

        if self.health_origin is SourceHealthOrigin.INITIAL:
            if self.health is not SourceHealth.NOT_YET_OBSERVED:
                raise ValueError("initial source health must be not_yet_observed")
            if self.freshness is not SourceFreshness.NOT_YET_OBSERVED:
                raise ValueError("initial source freshness must be not_yet_observed")
            if has_successful_commit:
                raise ValueError("initial source health cannot have committed provenance")
        else:
            _require_text(self.health_reason, "health_reason")
            if self.freshness is SourceFreshness.NOT_YET_OBSERVED:
                raise ValueError("non-initial source cannot be not_yet_observed")

        if self.health_origin is SourceHealthOrigin.DISCOVERY_RUN:
            if self.last_health_run_sequence is None:
                raise ValueError("discovery_run health origin requires run provenance")
        if self.health_origin is SourceHealthOrigin.TIME_EXPIRY:
            if (
                not has_successful_commit
                or self.freshness is not SourceFreshness.STALE
                or self.current_context != self.committed_context
                or self.last_health_run_sequence
                != self.last_committed_run_sequence
            ):
                raise ValueError(
                    "time_expiry requires stale exact committed run and context provenance"
                )
        if self.health_origin is SourceHealthOrigin.CONTROLLED_CONTEXT_TRANSITION:
            if self.freshness is not SourceFreshness.STALE:
                raise ValueError("controlled context transition must be stale")

        if self.freshness is SourceFreshness.FRESH:
            if (
                self.health is not SourceHealth.HEALTHY
                or self.health_origin is not SourceHealthOrigin.DISCOVERY_RUN
                or not has_successful_commit
                or self.current_context != self.committed_context
                or self.last_health_run_sequence
                != self.last_committed_run_sequence
            ):
                raise ValueError(
                    "fresh source requires a healthy authoritative discovery commit"
                )
        elif self.health not in {SourceHealth.HEALTHY, SourceHealth.NOT_YET_OBSERVED}:
            if self.freshness is not SourceFreshness.STALE:
                raise ValueError("unhealthy source must be stale")

        if has_successful_commit and (
            self.last_health_run_sequence is None
            or self.last_health_run_sequence < self.last_committed_run_sequence
        ):
            raise ValueError("committed inventory requires applied run-health provenance")
        if (
            self.last_health_run_sequence is not None
            and self.latest_completed_run_sequence is not None
            and self.latest_completed_run_sequence < self.last_health_run_sequence
        ):
            raise ValueError("applied health run must be included in completion provenance")

        object.__setattr__(self, "facts", _immutable_mapping(self.facts))

    @staticmethod
    def _validate_sequence_pair(
        sequence: int | None, outcome: str | None, label: str
    ) -> None:
        if (sequence is None) != (outcome is None):
            raise ValueError(f"{label} sequence and outcome must be published together")
        if outcome is not None:
            _require_text(outcome, f"{label} outcome")

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
        _require_text(self.node_id, "node_id")
        _require_text(self.inventory_source_id, "inventory_source_id")
        object.__setattr__(self, "facts", _immutable_mapping(self.facts))


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
    termination_reason: str | None = None
    successor_resource_id: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.resource_id, "resource_id")
        _require_text(self.inventory_source_id, "inventory_source_id")
        _require_positive(self.vmid, "vmid")
        _require_positive(self.locator_generation, "locator_generation")
        _require_positive(
            self.resource_continuity_revision, "resource_continuity_revision"
        )

        nonterminal = self.presence in {PresenceState.PRESENT, PresenceState.MISSING}
        if nonterminal:
            if self.active_binding_id is None:
                raise ValueError("nonterminal resource requires active_binding_id")
            _require_text(self.active_binding_id, "active_binding_id")
        elif self.active_binding_id is not None:
            raise ValueError("terminal resource must not have an active binding")

        self._validate_state_matrix()
        self._validate_node_relation()
        self._validate_terminal_relation()

        object.__setattr__(
            self, "retained_policy", _immutable_mapping(self.retained_policy)
        )
        object.__setattr__(
            self, "effective_policy", _immutable_mapping(self.effective_policy)
        )
        object.__setattr__(self, "state", _immutable_mapping(self.state))
        object.__setattr__(
            self, "effective_capabilities", frozenset(self.effective_capabilities)
        )

        policy_eligible = (
            self.presence is PresenceState.PRESENT
            and self.lifecycle is LifecycleState.ACTIVE
            and self.observational_continuity is ObservationalContinuity.CONSISTENT
            and self.security_continuity is SecurityContinuity.TRUSTED
        )
        if self.policy_applicable and not policy_eligible:
            raise ValueError("policy cannot be applicable outside trusted current state")
        if not self.policy_applicable and self.effective_capabilities:
            raise ValueError(
                "effective capabilities require backend-published policy applicability"
            )

    def _validate_state_matrix(self) -> None:
        case = (
            self.presence,
            self.lifecycle,
            self.observational_continuity,
        )
        valid_security: set[SecurityContinuity]
        if case == (
            PresenceState.PRESENT,
            LifecycleState.ACTIVE,
            ObservationalContinuity.CONSISTENT,
        ):
            valid_security = {
                SecurityContinuity.UNVERIFIED,
                SecurityContinuity.TRUSTED,
            }
        elif case in {
            (
                PresenceState.PRESENT,
                LifecycleState.QUARANTINED,
                ObservationalContinuity.UNCERTAIN,
            ),
            (
                PresenceState.MISSING,
                LifecycleState.QUARANTINED,
                ObservationalContinuity.UNCERTAIN,
            ),
        }:
            valid_security = {
                SecurityContinuity.UNVERIFIED,
                SecurityContinuity.REVOKED,
            }
        elif (
            self.presence is PresenceState.CONFIRMED_REMOVED
            and self.lifecycle is LifecycleState.RETIRED
            and self.observational_continuity
            in {
                ObservationalContinuity.CONSISTENT,
                ObservationalContinuity.UNCERTAIN,
            }
        ):
            valid_security = {
                SecurityContinuity.UNVERIFIED,
                SecurityContinuity.REVOKED,
            }
        elif case == (
            PresenceState.NOT_CURRENT,
            LifecycleState.RETIRED,
            ObservationalContinuity.REPLACED,
        ):
            valid_security = {
                SecurityContinuity.UNVERIFIED,
                SecurityContinuity.REVOKED,
            }
        else:
            raise ValueError("resource axes violate the canonical state matrix")
        if self.security_continuity not in valid_security:
            raise ValueError("security continuity violates the canonical state matrix")

        if self.presence is PresenceState.PRESENT:
            if self.detail_status is DetailStatus.NOT_APPLICABLE:
                raise ValueError("present resource requires a current detail status")
        elif self.detail_status is not DetailStatus.NOT_APPLICABLE:
            raise ValueError(
                "missing, confirmed_removed, and not_current require detail_status=not_applicable"
            )

    def _validate_node_relation(self) -> None:
        if self.presence is not PresenceState.PRESENT:
            if self.current_node_id is not None:
                raise ValueError("non-present resource must not have current_node_id")
            if self.node_availability is not NodeAvailability.NOT_APPLICABLE:
                raise ValueError("non-present resource node availability is not_applicable")
            if self.last_known_node_id is not None:
                _require_text(self.last_known_node_id, "last_known_node_id")
            return

        if self.current_node_id is None:
            if self.node_availability is not NodeAvailability.UNRESOLVED:
                raise ValueError("unresolved current node relation must be explicit")
            if self.last_known_node_id is not None:
                _require_text(self.last_known_node_id, "last_known_node_id")
            return

        _require_text(self.current_node_id, "current_node_id")
        if self.last_known_node_id is not None:
            raise ValueError("resolved current node forbids last_known_node_id")
        if self.node_availability not in {
            NodeAvailability.AVAILABLE,
            NodeAvailability.UNAVAILABLE,
        }:
            raise ValueError("resolved current node requires available or unavailable")

    def _validate_terminal_relation(self) -> None:
        if self.presence is PresenceState.NOT_CURRENT:
            if self.termination_reason != "replaced":
                raise ValueError("not_current resource requires replacement provenance")
            if self.successor_resource_id is None:
                raise ValueError("not_current resource requires successor_resource_id")
            _require_text(self.successor_resource_id, "successor_resource_id")
        elif self.presence is PresenceState.CONFIRMED_REMOVED:
            if self.termination_reason != "confirmed_removed":
                raise ValueError(
                    "confirmed_removed resource requires removal provenance"
                )
            if self.successor_resource_id is not None:
                raise ValueError("confirmed_removed resource cannot name a successor")
        elif self.termination_reason is not None or self.successor_resource_id is not None:
            raise ValueError("nonterminal resource cannot publish terminal provenance")

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
        object.__setattr__(self, "sources", tuple(self.sources))
        object.__setattr__(self, "nodes", tuple(self.nodes))
        object.__setattr__(self, "resources", tuple(self.resources))

        if type(self.inventory_revision) is not int or self.inventory_revision < 0:
            raise ValueError("inventory_revision must be a non-negative integer")
        _require_positive(self.published_state_revision, "published_state_revision")
        _require_text(self.published_at, "published_at")

        source_ids = {source.inventory_source_id for source in self.sources}
        if len(source_ids) != len(self.sources):
            raise ValueError("snapshot contains duplicate source identities")
        endpoint_owners: dict[str, str] = {}
        for source in self.sources:
            contexts = (source.current_context, source.committed_context)
            for context in contexts:
                if context is None:
                    continue
                owner = endpoint_owners.setdefault(
                    context.endpoint_id, source.inventory_source_id
                )
                if owner != source.inventory_source_id:
                    raise ValueError(
                        "endpoint identity cannot be shared across inventory sources"
                    )
        node_ids = {node.node_id for node in self.nodes}
        if len(node_ids) != len(self.nodes):
            raise ValueError("snapshot contains duplicate node identities")
        resource_ids = {resource.resource_id for resource in self.resources}
        if len(resource_ids) != len(self.resources):
            raise ValueError("snapshot contains duplicate resource identities")

        if any(node.inventory_source_id not in source_ids for node in self.nodes):
            raise ValueError("node references an unknown inventory source")
        if any(
            resource.inventory_source_id not in source_ids
            for resource in self.resources
        ):
            raise ValueError("resource references an unknown inventory source")

        nodes_by_id = {node.node_id: node for node in self.nodes}
        active_locators: set[tuple[str, int]] = set()
        locator_generations: set[tuple[str, int, int]] = set()
        resources_by_locator: dict[tuple[str, int], list[ResourceSnapshot]] = {}
        active_bindings: set[str] = set()
        for resource in self.resources:
            locator = (resource.inventory_source_id, resource.vmid)
            resources_by_locator.setdefault(locator, []).append(resource)
            locator_generation = (
                resource.inventory_source_id,
                resource.vmid,
                resource.locator_generation,
            )
            if locator_generation in locator_generations:
                raise ValueError(
                    "snapshot contains duplicate retained locator generation"
                )
            locator_generations.add(locator_generation)
            if resource.active_binding_id is not None:
                if locator in active_locators:
                    raise ValueError("snapshot contains multiple current occupants for a locator")
                active_locators.add(locator)
                if resource.active_binding_id in active_bindings:
                    raise ValueError("snapshot contains duplicate active binding identities")
                active_bindings.add(resource.active_binding_id)
            for node_id in (resource.current_node_id, resource.last_known_node_id):
                if node_id is None:
                    continue
                node = nodes_by_id.get(node_id)
                if node is None:
                    raise ValueError("resource references a node absent from the same snapshot")
                if node.inventory_source_id != resource.inventory_source_id:
                    raise ValueError("resource node relation crosses inventory sources")
            if resource.current_node_id is not None:
                node = nodes_by_id[resource.current_node_id]
                expected = (
                    NodeAvailability.AVAILABLE
                    if node.available
                    else NodeAvailability.UNAVAILABLE
                )
                if resource.node_availability is not expected:
                    raise ValueError("resource node availability disagrees with node record")

        for locator_resources in resources_by_locator.values():
            current = next(
                (
                    resource
                    for resource in locator_resources
                    if resource.active_binding_id is not None
                ),
                None,
            )
            terminal_generations = [
                resource.locator_generation
                for resource in locator_resources
                if resource.active_binding_id is None
            ]
            if (
                current is not None
                and terminal_generations
                and current.locator_generation != max(terminal_generations) + 1
            ):
                raise ValueError(
                    "current locator generation must follow retained terminal history"
                )

        resources_by_id = {
            resource.resource_id: resource for resource in self.resources
        }
        for old in self.resources:
            if old.successor_resource_id is None:
                continue
            if old.successor_resource_id == old.resource_id:
                raise ValueError("replacement lineage cannot reference itself")
            successor = resources_by_id.get(old.successor_resource_id)
            if successor is None:
                raise ValueError("replacement successor is absent from the same snapshot")
            if (
                successor.inventory_source_id != old.inventory_source_id
                or successor.vmid != old.vmid
                or successor.locator_generation != old.locator_generation + 1
            ):
                raise ValueError(
                    "replacement lineage violates locator history invariants"
                )

        sources_by_id = {
            source.inventory_source_id: source for source in self.sources
        }
        for resource in self.resources:
            source = sources_by_id[resource.inventory_source_id]
            if (
                (resource.policy_applicable or resource.effective_capabilities)
                and not source.current_facts_available
            ):
                raise ValueError(
                    "policy/capabilities require a fresh healthy source snapshot"
                )

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

        sources = tuple(
            sorted(
                (
                    source.inventory_source_id,
                    source.name,
                    source.provider_kind,
                    source.facts,
                )
                for source in self.sources
            )
        )
        nodes = tuple(
            sorted(
                (
                    node.node_id,
                    node.inventory_source_id,
                    node.name,
                    node.status,
                    node.available,
                    node.facts,
                )
                for node in self.nodes
            )
        )
        resources = tuple(
            sorted(
                (
                    resource.resource_id,
                    resource.inventory_source_id,
                    resource.active_binding_id,
                    resource.resource_type,
                    resource.vmid,
                    resource.locator_generation,
                    resource.resource_continuity_revision,
                    resource.name,
                    resource.status,
                    resource.current_node_id,
                    resource.last_known_node_id,
                    resource.presence,
                    resource.lifecycle,
                    resource.observational_continuity,
                    resource.security_continuity,
                    resource.detail_status,
                    resource.node_availability,
                    resource.state_level,
                    resource.retained_policy,
                    resource.state,
                    resource.termination_reason,
                    resource.successor_resource_id,
                )
                for resource in self.resources
            )
        )
        return sources, nodes, resources

    def validate_revision_successor(self, previous: HubinetOpsSnapshot) -> None:
        """Reject regressing or mutable views for an existing backend entry."""

        if self.backend.backend_instance_id != previous.backend.backend_instance_id:
            raise ValueError("snapshot belongs to a different backend instance")
        if self.inventory_revision < previous.inventory_revision:
            raise ValueError("inventory_revision must not regress")
        if self.published_state_revision < previous.published_state_revision:
            raise ValueError("published_state_revision must not regress")
        if (
            self.published_state_revision == previous.published_state_revision
            and self != previous
        ):
            raise ValueError("one published_state_revision must identify one immutable view")

        previous_sources_by_id = previous.sources_by_id
        previous_nodes_by_id = previous.nodes_by_id
        previous_resources_by_id = previous.resources_by_id
        current_source_ids = set(self.sources_by_id)
        current_node_ids = set(self.nodes_by_id)
        current_resource_ids = set(self.resources_by_id)

        for resource_id, old_resource in previous_resources_by_id.items():
            resource = self.resources_by_id.get(resource_id)
            if (
                resource is not None
                and old_resource.presence is PresenceState.NOT_CURRENT
                and (
                    resource.termination_reason != old_resource.termination_reason
                    or resource.successor_resource_id
                    != old_resource.successor_resource_id
                )
            ):
                raise ValueError("terminal replacement lineage is immutable")

        missing_source_ids = set(previous_sources_by_id) - current_source_ids
        if missing_source_ids:
            raise ValueError("published snapshot cannot omit a retained inventory source")
        missing_node_ids = set(previous_nodes_by_id) - current_node_ids
        if missing_node_ids:
            raise ValueError("published snapshot cannot omit a retained node")
        missing_resource_ids = set(previous_resources_by_id) - current_resource_ids
        if missing_resource_ids:
            raise ValueError("published snapshot cannot omit a retained resource")

        if (
            self.inventory_projection != previous.inventory_projection
            and self.inventory_revision <= previous.inventory_revision
        ):
            raise ValueError(
                "inventory-owned changes require a newer inventory_revision"
            )

        previous_endpoint_owners: dict[str, str] = {}
        for old_source in previous.sources:
            for context in (
                old_source.current_context,
                old_source.committed_context,
            ):
                if context is not None:
                    previous_endpoint_owners.setdefault(
                        context.endpoint_id, old_source.inventory_source_id
                    )
        for source in self.sources:
            for context in (source.current_context, source.committed_context):
                if context is None:
                    continue
                old_owner = previous_endpoint_owners.get(context.endpoint_id)
                if old_owner is not None and old_owner != source.inventory_source_id:
                    raise ValueError(
                        "endpoint identity cannot move between inventory sources"
                    )

        for source in self.sources:
            old_source = previous_sources_by_id.get(source.inventory_source_id)
            if old_source is None:
                continue
            if source.provider_kind != old_source.provider_kind:
                raise ValueError("provider_kind is immutable for an inventory source")
            if (
                source.current_context.source_config_revision
                < old_source.current_context.source_config_revision
            ):
                raise ValueError("source_config_revision must not regress")
            if (
                source.current_context.transport_trust_revision
                < old_source.current_context.transport_trust_revision
            ):
                raise ValueError("transport_trust_revision must not regress")
            if (
                source.current_context.endpoint_id
                != old_source.current_context.endpoint_id
            ):
                raise ValueError(
                    "endpoint_id is immutable for an existing inventory source"
                )
            if (
                source.current_context.canonicalization_contract_version
                == old_source.current_context.canonicalization_contract_version
            ):
                if (
                    source.current_context.canonical_transport_locator
                    != old_source.current_context.canonical_transport_locator
                ):
                    raise ValueError(
                        "canonical transport locator is immutable within a "
                        "canonicalization contract version"
                    )
            else:
                if (
                    source.current_context.source_config_revision
                    <= old_source.current_context.source_config_revision
                ):
                    raise ValueError(
                        "canonicalization migration requires a newer "
                        "source_config_revision"
                    )
                if (
                    source.current_context.canonicalization_contract_version
                    < old_source.current_context.canonicalization_contract_version
                ):
                    raise ValueError(
                        "canonicalization_contract_version must increase during migration"
                    )
            if (
                source.last_issued_run_sequence
                < old_source.last_issued_run_sequence
            ):
                raise ValueError("last_issued_run_sequence must not regress")
            for field_name in (
                "latest_completed_run_sequence",
                "last_health_run_sequence",
                "last_committed_run_sequence",
            ):
                old_sequence = getattr(old_source, field_name)
                sequence = getattr(source, field_name)
                if old_sequence is None:
                    continue
                if sequence is None:
                    raise ValueError(f"{field_name} cannot be cleared")
                if sequence < old_sequence:
                    raise ValueError(f"{field_name} must not regress")

            if (
                source.last_committed_run_sequence is not None
                and (
                    old_source.last_committed_run_sequence is None
                    or source.last_committed_run_sequence
                    > old_source.last_committed_run_sequence
                )
                and self.inventory_revision <= previous.inventory_revision
            ):
                raise ValueError(
                    "successful inventory commit requires a newer inventory_revision"
                )

            if (
                source.latest_completed_run_sequence
                == old_source.latest_completed_run_sequence
                and source.latest_completed_run_sequence is not None
                and source.latest_completed_outcome
                != old_source.latest_completed_outcome
            ):
                raise ValueError(
                    "latest completed outcome is immutable for its run sequence"
                )
            if (
                source.last_health_run_sequence
                == old_source.last_health_run_sequence
                and source.last_health_run_sequence is not None
                and source.last_run_health_outcome
                != old_source.last_run_health_outcome
            ):
                raise ValueError(
                    "last run health outcome is immutable for its run sequence"
                )
            if (
                source.last_committed_run_sequence
                == old_source.last_committed_run_sequence
                and source.last_committed_run_sequence is not None
                and (
                    source.last_successful_observed_at,
                    source.freshness_reference_at,
                    source.freshness_valid_until,
                    source.committed_context,
                )
                != (
                    old_source.last_successful_observed_at,
                    old_source.freshness_reference_at,
                    old_source.freshness_valid_until,
                    old_source.committed_context,
                )
            ):
                raise ValueError(
                    "successful commit provenance is immutable for its run sequence"
                )

        for node in self.nodes:
            old_node = previous_nodes_by_id.get(node.node_id)
            if (
                old_node is not None
                and node.inventory_source_id != old_node.inventory_source_id
            ):
                raise ValueError("node identity cannot move between sources")

        for resource in self.resources:
            old = previous_resources_by_id.get(resource.resource_id)
            if old is None:
                continue
            if resource.inventory_source_id != old.inventory_source_id:
                raise ValueError("resource identity cannot move between sources")
            if resource.resource_type is not old.resource_type:
                raise ValueError("resource_type is immutable for an incarnation")
            if resource.vmid != old.vmid:
                raise ValueError("resource locator is immutable for an incarnation")
            if resource.locator_generation != old.locator_generation:
                raise ValueError(
                    "locator_generation is immutable for an incarnation"
                )
            if resource.resource_continuity_revision < old.resource_continuity_revision:
                raise ValueError("resource_continuity_revision must not regress")

            old_terminal = old.presence in {
                PresenceState.CONFIRMED_REMOVED,
                PresenceState.NOT_CURRENT,
            }
            terminal = resource.presence in {
                PresenceState.CONFIRMED_REMOVED,
                PresenceState.NOT_CURRENT,
            }
            if old_terminal:
                if (
                    not terminal
                    or resource.presence is not old.presence
                    or resource.termination_reason != old.termination_reason
                    or resource.successor_resource_id != old.successor_resource_id
                ):
                    raise ValueError("terminal resource cannot be reopened or reclassified")
            elif terminal:
                if resource.active_binding_id is not None:
                    raise ValueError("terminal transition must close the active binding")
            elif resource.active_binding_id != old.active_binding_id:
                raise ValueError(
                    "active binding is immutable before a terminal transition"
                )

            security_relevant_transition = (
                resource.observational_continuity
                is not old.observational_continuity
                or resource.security_continuity is not old.security_continuity
                or (
                    (old.lifecycle is LifecycleState.QUARANTINED)
                    != (resource.lifecycle is LifecycleState.QUARANTINED)
                )
                or (terminal and not old_terminal)
            )
            if (
                security_relevant_transition
                and resource.resource_continuity_revision
                <= old.resource_continuity_revision
            ):
                raise ValueError(
                    "security-relevant resource transition requires a newer "
                    "resource_continuity_revision"
                )


class HubinetOpsTransport(Protocol):
    """Read-only transport implemented by the future backend API adapter."""

    async def validate_connection(self) -> BackendInformation:
        """Authenticate and validate the backend identity."""

    async def fetch_backend_information(self) -> BackendInformation:
        """Fetch backend identity and version information."""

    async def fetch_resource_snapshot(self) -> HubinetOpsSnapshot:
        """Fetch one logical inventory/state/policy snapshot."""


class HubinetOpsApi:
    """Read-only Hubinet Ops client independent from a concrete transport."""

    def __init__(
        self,
        *,
        base_url: str,
        api_token: str,
        verify_tls: bool,
        transport: HubinetOpsTransport,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._api_token = api_token
        self.verify_tls = verify_tls
        self._transport = transport

    async def async_validate_connection(self) -> BackendInformation:
        """Validate authentication and return stable backend information."""

        return await self._transport.validate_connection()

    async def async_fetch_backend_information(self) -> BackendInformation:
        """Fetch backend information."""

        return await self._transport.fetch_backend_information()

    async def async_fetch_resource_snapshot(self) -> HubinetOpsSnapshot:
        """Fetch one logical snapshot for the coordinator."""

        return await self._transport.fetch_resource_snapshot()


class HubinetOpsApiFactory(Protocol):
    """Factory boundary used by config flow, setup and fake transports."""

    def __call__(
        self, *, base_url: str, api_token: str, verify_tls: bool
    ) -> HubinetOpsApi:
        """Create a client bound only to the Hubinet Ops backend."""


class _UnconfiguredPhaseZeroTransport:
    """Fail closed until the backend 0.5 HTTP contract is finalized."""

    @staticmethod
    def _error() -> HubinetOpsCannotConnect:
        return HubinetOpsCannotConnect(
            "Hubinet Ops 0.5 backend transport is not configured in Phase 0"
        )

    async def validate_connection(self) -> BackendInformation:
        raise self._error()

    async def fetch_backend_information(self) -> BackendInformation:
        raise self._error()

    async def fetch_resource_snapshot(self) -> HubinetOpsSnapshot:
        raise self._error()


def phase_zero_api_factory(
    *, base_url: str, api_token: str, verify_tls: bool
) -> HubinetOpsApi:
    """Create the fail-closed Phase 0 client without inventing HTTP endpoints."""

    return HubinetOpsApi(
        base_url=base_url,
        api_token=api_token,
        verify_tls=verify_tls,
        transport=_UnconfiguredPhaseZeroTransport(),
    )
