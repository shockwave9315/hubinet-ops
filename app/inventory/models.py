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


class SourceAttestationStatus(StrEnum):
    """Current accepted source-level trust-domain attestation state (ADR 0003)."""

    NOT_YET_ATTESTED = "not_yet_attested"
    ATTESTED = "attested"


class SourceAttestationRelationshipGate(StrEnum):
    """ADR 0003 §17 durable relationship gate.

    Deliberately separate from :class:`SourceAttestationStatus`: a mismatch
    is evidence that continuity is unproven, not a change to the enrolled
    anchor or epoch, so it must never be conflated with enrollment state.
    A ``mismatch_pending_reattestation`` source remains ``attested`` at its
    prior epoch/anchor -- this gate exists purely so a future attestation-
    gated action (e.g. Commit 4's candidate check) can fail closed on it
    without deriving current authority from audit history.
    """

    CLEAR = "clear"
    MISMATCH_PENDING_REATTESTATION = "mismatch_pending_reattestation"


class AttestationEvidenceTier(StrEnum):
    """ADR 0003 §4/§7 evidence tiers. Tier 3 does not exist and is never modeled."""

    TIER_1 = "tier_1"
    TIER_2 = "tier_2"


class AttestationOperation(StrEnum):
    """The kind of explicit, operator-driven attestation action attempted."""

    ENROLLMENT = "enrollment"
    REATTESTATION = "reattestation"
    REVOCATION = "revocation"
    CANDIDATE_CHECK = "candidate_check"


class AttestationOutcome(StrEnum):
    """Audited result of one attestation attempt (ADR 0003 §17/§18/§19a)."""

    MATCH = "match"
    MISMATCH = "mismatch"
    UNAVAILABLE = "unavailable"
    MALFORMED = "malformed"
    STALE_CAS = "stale_cas"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class TierTwoEvaluationStatus(StrEnum):
    """Explicit, structural tier-2 result distinct from a free-form reason.

    ``NOT_EVALUATED`` covers both "no reader capable of tier-2 verification"
    and "tier-1-only read, tier 2 never attempted" -- it is never conflated
    with ``FAILED`` (a genuine chain-verification attempt that did not
    validate). A failed or not-evaluated tier-2 result never erases an
    independently valid tier-1 observation (ADR 0003 §10a).
    """

    NOT_EVALUATED = "not_evaluated"
    FAILED = "failed"
    VERIFIED = "verified"


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
    committed_source_attestation_epoch: int | None


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
    expected_source_attestation_epoch: int
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
    completion_source_attestation_epoch: int | None


@dataclass(frozen=True, slots=True)
class SourceAttestationState:
    """Current accepted source-level trust-domain attestation (ADR 0003).

    Grants no resource/workload trust and no mutation authority by itself
    (ADR 0003 §28). ``source_attestation_epoch`` starts at 0 and is a
    backend-owned monotonic authority token, independent of
    ``source_config_revision``, ``transport_trust_revision``, and
    ``resource_continuity_revision``.
    """

    inventory_source_id: str
    attestation_status: SourceAttestationStatus
    source_attestation_epoch: int
    anchor_kind: str | None
    anchor_value: str | None
    evidence_tier: AttestationEvidenceTier | None
    tier2_evaluation: TierTwoEvaluationStatus | None
    relationship_gate: SourceAttestationRelationshipGate
    accepted_at: str | None
    accepted_by: str | None
    evaluated_endpoint_id: str | None


@dataclass(frozen=True, slots=True)
class SourceAttestationEvent:
    """One immutable, retained audit record of an attestation attempt/outcome.

    Retained regardless of the resulting decision; never overwritten by a
    later attempt (ADR 0003 §19, §29).
    """

    event_id: str
    inventory_source_id: str
    target_endpoint_id: str
    operation: AttestationOperation
    actor: str
    attempted_at: str
    expected_source_config_revision: int
    expected_endpoint_id: str
    expected_canonical_transport_locator: str
    expected_canonicalization_contract_version: int
    expected_transport_trust_revision: int
    expected_source_attestation_epoch: int
    expected_relationship_gate: SourceAttestationRelationshipGate
    outcome: AttestationOutcome
    evidence_tier: AttestationEvidenceTier | None
    tier2_evaluation: TierTwoEvaluationStatus | None
    asserted_anchor_kind: str | None
    asserted_anchor_value: str | None
    endpoint_lifecycle_at_check: str | None
    previous_epoch: int
    resulting_epoch: int | None
    resulting_relationship_gate: SourceAttestationRelationshipGate | None
    reason: str


@dataclass(frozen=True, slots=True)
class CandidateAttestationBinding:
    """One epoch-scoped candidate endpoint attestation binding (ADR 0003 §14).

    A prerequisite record only. It never activates the endpoint, never
    changes ``EndpointLifecycle``, and becomes unusable once
    ``source_attestation_epoch`` advances past ``source_attestation_epoch``
    recorded here.
    """

    binding_id: str
    inventory_source_id: str
    endpoint_id: str
    source_attestation_epoch: int
    evidence_tier: AttestationEvidenceTier
    tier2_evaluation: TierTwoEvaluationStatus
    endpoint_lifecycle_at_check: str
    canonical_transport_locator: str
    canonicalization_contract_version: int
    transport_trust_revision: int
    matched_at: str
    created_by: str
    event_id: str


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
