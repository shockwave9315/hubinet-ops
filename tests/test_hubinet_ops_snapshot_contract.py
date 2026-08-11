from __future__ import annotations

from dataclasses import replace
import importlib.util
from pathlib import Path
import sys
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import pytest

API_PATH = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "hubinet_ops"
    / "api.py"
)
API_SPEC = importlib.util.spec_from_file_location("hubinet_ops_api_contract", API_PATH)
assert API_SPEC is not None and API_SPEC.loader is not None
api = importlib.util.module_from_spec(API_SPEC)
sys.modules[API_SPEC.name] = api
API_SPEC.loader.exec_module(api)

BackendInformation = api.BackendInformation
DetailStatus = api.DetailStatus
HubinetOpsSnapshot = api.HubinetOpsSnapshot
InventorySourceSnapshot = api.InventorySourceSnapshot
LifecycleState = api.LifecycleState
NodeAvailability = api.NodeAvailability
NodeSnapshot = api.NodeSnapshot
ObservationalContinuity = api.ObservationalContinuity
PresenceState = api.PresenceState
ResourceSnapshot = api.ResourceSnapshot
ResourceStateLevel = api.ResourceStateLevel
ResourceType = api.ResourceType
SecurityContinuity = api.SecurityContinuity
SourceContext = api.SourceContext
SourceFreshness = api.SourceFreshness
SourceHealth = api.SourceHealth
SourceHealthOrigin = api.SourceHealthOrigin

BACKEND_ID = "6a172b5d-d820-4cac-904f-dfb17d42163e"
SOURCE_ID = "cfe64f8e-2529-4692-9c23-526479961dbc"
SOURCE_B_ID = "44e24b73-f593-4625-a182-2e2db9541688"
NODE_A = "811d7ea4-470f-42d4-aa06-d2c5c9249a1e"
NODE_B = "6f1c0770-6ca6-4c20-ab56-8cb645f63ee3"
RESOURCE_ID = "b50f157b-d2fb-4fff-9497-42c5c239ef49"
SUCCESSOR_ID = "c5321ec5-7259-421a-94ab-195a9c5e5d81"
THIRD_RESOURCE_ID = "727f79b7-9d89-45e5-88da-21adfd94f08a"
ENDPOINT_A_ID = "7b784024-62d8-4f3e-bb63-af9fe65fcc8e"
ENDPOINT_B_ID = "9e2ef36f-f6db-4e23-93fe-85ad573682f5"


def _test_only_binding_id(label: str) -> str:
    """Return a stable UUID for one test binding without production semantics."""

    return str(uuid5(NAMESPACE_URL, f"hubinet-ops test binding: {label}"))


def _test_only_resource_id(label: str) -> str:
    """Return a stable UUID for one test resource without production semantics."""

    return str(uuid5(NAMESPACE_URL, f"hubinet-ops test resource: {label}"))


def context(
    *,
    revision: int = 3,
    trust_revision: int = 2,
    endpoint_id: str = ENDPOINT_A_ID,
    locator: str = "https://pve.example.test:8006",
    canonicalization_version: int = 1,
) -> SourceContext:
    return SourceContext(
        source_config_revision=revision,
        endpoint_id=endpoint_id,
        canonical_transport_locator=locator,
        canonicalization_contract_version=canonicalization_version,
        transport_trust_revision=trust_revision,
    )


def source(
    *,
    source_id: str = SOURCE_ID,
    name: str = "Test Proxmox",
    facts: dict[str, Any] | None = None,
) -> InventorySourceSnapshot:
    current_context = context(
        endpoint_id=ENDPOINT_B_ID if source_id == SOURCE_B_ID else ENDPOINT_A_ID
    )
    return InventorySourceSnapshot(
        inventory_source_id=source_id,
        name=name,
        provider_kind="proxmox",
        health=SourceHealth.HEALTHY,
        freshness=SourceFreshness.FRESH,
        health_origin=SourceHealthOrigin.DISCOVERY_RUN,
        health_reason="authoritative_inventory_commit",
        last_issued_run_sequence=5,
        latest_completed_run_sequence=5,
        latest_completed_outcome="success",
        last_health_run_sequence=5,
        last_run_health_outcome="success",
        last_committed_run_sequence=5,
        last_successful_observed_at="2026-08-08T11:59:30+00:00",
        freshness_reference_at="2026-08-08T11:59:00+00:00",
        freshness_valid_until="2026-08-08T12:04:00+00:00",
        current_context=current_context,
        committed_context=current_context,
        facts=facts or {},
    )


def node(
    node_id: str = NODE_A,
    *,
    name: str = "pve-a",
    status: str = "online",
    available: bool = True,
    facts: dict[str, Any] | None = None,
) -> NodeSnapshot:
    return NodeSnapshot(
        node_id=node_id,
        inventory_source_id=SOURCE_ID,
        name=name,
        status=status,
        available=available,
        facts=facts or {},
    )


def resource(
    resource_id: str = RESOURCE_ID,
    *,
    generation: int = 1,
    continuity_revision: int = 1,
    current_node_id: str | None = NODE_A,
    last_known_node_id: str | None = None,
    presence: PresenceState = PresenceState.PRESENT,
    lifecycle: LifecycleState = LifecycleState.ACTIVE,
    observational_continuity: ObservationalContinuity = (
        ObservationalContinuity.CONSISTENT
    ),
    security_continuity: SecurityContinuity = SecurityContinuity.UNVERIFIED,
    detail_status: DetailStatus = DetailStatus.OK,
    node_availability: NodeAvailability = NodeAvailability.AVAILABLE,
    name: str = "Cloudflared",
    status: str = "running",
    retained_policy: dict[str, Any] | None = None,
    effective_policy: dict[str, Any] | None = None,
    policy_applicable: bool = False,
    suspended_reason: str | None = None,
    effective_capabilities: frozenset[str] = frozenset(),
    state: dict[str, Any] | None = None,
    state_level: ResourceStateLevel = ResourceStateLevel.OBSERVED,
    termination_reason: str | None = None,
    successor_resource_id: str | None = None,
) -> ResourceSnapshot:
    terminal = presence in {
        PresenceState.CONFIRMED_REMOVED,
        PresenceState.NOT_CURRENT,
    }
    return ResourceSnapshot(
        resource_id=resource_id,
        inventory_source_id=SOURCE_ID,
        active_binding_id=(
            None if terminal else _test_only_binding_id(resource_id)
        ),
        resource_type=ResourceType.LXC,
        vmid=101,
        locator_generation=generation,
        resource_continuity_revision=continuity_revision,
        name=name,
        status=status,
        current_node_id=current_node_id,
        last_known_node_id=last_known_node_id,
        presence=presence,
        lifecycle=lifecycle,
        observational_continuity=observational_continuity,
        security_continuity=security_continuity,
        detail_status=detail_status,
        node_availability=node_availability,
        state_level=state_level,
        retained_policy=retained_policy or {},
        effective_policy=effective_policy or {},
        policy_applicable=policy_applicable,
        suspended_reason=suspended_reason,
        effective_capabilities=effective_capabilities,
        state=state or {},
        termination_reason=termination_reason,
        successor_resource_id=successor_resource_id,
    )


def snapshot(
    *,
    sources: Any = None,
    nodes: Any = None,
    resources: Any = None,
    inventory_revision: int = 10,
    published_state_revision: int = 20,
) -> HubinetOpsSnapshot:
    return HubinetOpsSnapshot(
        backend=BackendInformation(
            backend_instance_id=BACKEND_ID,
            name="Hubinet Ops Test",
            version="0.5.0.dev0",
            api_version="0.5-draft",
        ),
        sources=(source(),) if sources is None else sources,
        nodes=(node(),) if nodes is None else nodes,
        resources=(resource(),) if resources is None else resources,
        inventory_revision=inventory_revision,
        published_state_revision=published_state_revision,
        published_at="2026-08-08T12:00:00+00:00",
    )


def missing_resource() -> ResourceSnapshot:
    return resource(
        continuity_revision=2,
        current_node_id=None,
        last_known_node_id=NODE_A,
        presence=PresenceState.MISSING,
        lifecycle=LifecycleState.QUARANTINED,
        observational_continuity=ObservationalContinuity.UNCERTAIN,
        detail_status=DetailStatus.NOT_APPLICABLE,
        node_availability=NodeAvailability.NOT_APPLICABLE,
        status="unknown",
    )


def confirmed_removed_resource() -> ResourceSnapshot:
    return resource(
        continuity_revision=2,
        current_node_id=None,
        last_known_node_id=NODE_A,
        presence=PresenceState.CONFIRMED_REMOVED,
        lifecycle=LifecycleState.RETIRED,
        observational_continuity=ObservationalContinuity.CONSISTENT,
        detail_status=DetailStatus.NOT_APPLICABLE,
        node_availability=NodeAvailability.NOT_APPLICABLE,
        status="unknown",
        termination_reason="confirmed_removed",
    )


def not_current_resource(
    *,
    resource_id: str = RESOURCE_ID,
    generation: int = 4,
    successor_resource_id: str = SUCCESSOR_ID,
) -> ResourceSnapshot:
    return resource(
        resource_id,
        generation=generation,
        continuity_revision=2,
        current_node_id=None,
        last_known_node_id=NODE_A,
        presence=PresenceState.NOT_CURRENT,
        lifecycle=LifecycleState.RETIRED,
        observational_continuity=ObservationalContinuity.REPLACED,
        detail_status=DetailStatus.NOT_APPLICABLE,
        node_availability=NodeAvailability.NOT_APPLICABLE,
        status="unknown",
        termination_reason="replaced",
        successor_resource_id=successor_resource_id,
    )


def time_expiry_source(**changes: Any) -> InventorySourceSnapshot:
    values = {
        "health": SourceHealth.DEGRADED,
        "freshness": SourceFreshness.STALE,
        "health_origin": SourceHealthOrigin.TIME_EXPIRY,
        "health_reason": "freshness_deadline_elapsed",
    }
    values.update(changes)
    return replace(source(), **values)


def successful_commit_source(
    sequence: int = 6,
    *,
    previous: InventorySourceSnapshot | None = None,
) -> InventorySourceSnapshot:
    committed = source() if previous is None else previous
    return replace(
        committed,
        last_issued_run_sequence=sequence,
        latest_completed_run_sequence=sequence,
        latest_completed_outcome="success",
        last_health_run_sequence=sequence,
        last_run_health_outcome="success",
        last_committed_run_sequence=sequence,
        last_successful_observed_at="2026-08-08T12:01:30+00:00",
        freshness_reference_at="2026-08-08T12:01:00+00:00",
        freshness_valid_until="2026-08-08T12:06:00+00:00",
        committed_context=committed.current_context,
    )


def with_successful_source_commit(
    view: HubinetOpsSnapshot,
    *,
    sequence: int = 7,
    source_id: str = SOURCE_ID,
) -> HubinetOpsSnapshot:
    return replace(
        view,
        sources=tuple(
            successful_commit_source(sequence, previous=item)
            if item.inventory_source_id == source_id
            else item
            for item in view.sources
        ),
    )


def initial_source() -> InventorySourceSnapshot:
    return replace(
        source(),
        health=SourceHealth.NOT_YET_OBSERVED,
        freshness=SourceFreshness.NOT_YET_OBSERVED,
        health_origin=SourceHealthOrigin.INITIAL,
        health_reason="",
        last_issued_run_sequence=0,
        latest_completed_run_sequence=None,
        latest_completed_outcome=None,
        last_health_run_sequence=None,
        last_run_health_outcome=None,
        last_committed_run_sequence=None,
        last_successful_observed_at=None,
        freshness_reference_at=None,
        freshness_valid_until=None,
        committed_context=None,
    )


def retained_generation_resources(
    *generations: int,
    source_id: str = SOURCE_ID,
) -> tuple[ResourceSnapshot, ...]:
    return tuple(
        replace(
            confirmed_removed_resource(),
            resource_id=_test_only_resource_id(
                f"{source_id} retained generation {generation}"
            ),
            inventory_source_id=source_id,
            locator_generation=generation,
            last_known_node_id=NODE_A if source_id == SOURCE_ID else None,
        )
        for generation in generations
    )


def nonterminal_security_resource(
    security_continuity: SecurityContinuity,
    *,
    continuity_revision: int,
) -> ResourceSnapshot:
    if security_continuity is SecurityContinuity.REVOKED:
        return replace(
            missing_resource(),
            resource_continuity_revision=continuity_revision,
            security_continuity=security_continuity,
        )
    return resource(
        continuity_revision=continuity_revision,
        security_continuity=security_continuity,
    )


INVALID_UUID_TEXT = (
    "110",
    "resource-110",
    "not-a-uuid",
    "",
    " ",
    BACKEND_ID.upper(),
    BACKEND_ID.replace("-", ""),
    f"{{{BACKEND_ID}}}",
    f"urn:uuid:{BACKEND_ID}",
    "00000000-0000-0000-0000-000000000000",
)


def construct_published_uuid_field(field_name: str, value: str) -> object:
    """Construct one Phase 0 record with a selected UUID field value."""

    if field_name == "backend_instance_id":
        return BackendInformation(value, "Backend", "0.5.0", "0.5-draft")
    if field_name == "endpoint_id":
        return replace(context(), endpoint_id=value)
    if field_name == "source.inventory_source_id":
        return replace(source(), inventory_source_id=value)
    if field_name == "node_id":
        return replace(node(), node_id=value)
    if field_name == "node.inventory_source_id":
        return replace(node(), inventory_source_id=value)
    if field_name == "resource_id":
        return replace(resource(), resource_id=value)
    if field_name == "resource.inventory_source_id":
        return replace(resource(), inventory_source_id=value)
    if field_name == "active_binding_id":
        return replace(resource(), active_binding_id=value)
    if field_name == "current_node_id":
        return replace(resource(), current_node_id=value)
    if field_name == "last_known_node_id":
        return replace(missing_resource(), last_known_node_id=value)
    if field_name == "successor_resource_id":
        return not_current_resource(successor_resource_id=value)
    raise AssertionError(f"uncovered test field: {field_name}")


PUBLISHED_UUID_FIELDS = (
    "backend_instance_id",
    "endpoint_id",
    "source.inventory_source_id",
    "node_id",
    "node.inventory_source_id",
    "resource_id",
    "resource.inventory_source_id",
    "active_binding_id",
    "current_node_id",
    "last_known_node_id",
    "successor_resource_id",
)


@pytest.mark.parametrize("field_name", PUBLISHED_UUID_FIELDS)
@pytest.mark.parametrize("invalid_uuid", INVALID_UUID_TEXT)
def test_all_published_uuid_identity_fields_reject_noncanonical_or_nil_text(
    field_name: str, invalid_uuid: str
) -> None:
    with pytest.raises(ValueError):
        construct_published_uuid_field(field_name, invalid_uuid)


@pytest.mark.parametrize("field_name", PUBLISHED_UUID_FIELDS)
def test_all_published_uuid_identity_fields_accept_canonical_non_nil_uuid(
    field_name: str,
) -> None:
    construct_published_uuid_field(field_name, BACKEND_ID)


def test_uuid_identity_fields_require_strings() -> None:
    with pytest.raises(ValueError, match="backend_instance_id"):
        BackendInformation(
            None, "Backend", "0.5.0", "0.5-draft"  # type: ignore[arg-type]
        )


def test_resource_vmid_text_is_rejected_before_snapshot_registry_publication() -> None:
    with pytest.raises(ValueError, match="resource_id"):
        resource(resource_id="110")


@pytest.mark.parametrize(
    ("field_name", "malformed_value"),
    [
        pytest.param("health", "healthy", id="health-healthy-string"),
        pytest.param(
            "health", "source_unavailable", id="health-unavailable-string"
        ),
        pytest.param("health", "bogus", id="health-unknown-string"),
        pytest.param("health", None, id="health-none"),
        pytest.param("freshness", "fresh", id="freshness-fresh-string"),
        pytest.param("freshness", "stale", id="freshness-stale-string"),
        pytest.param("freshness", "bogus", id="freshness-unknown-string"),
        pytest.param("freshness", None, id="freshness-none"),
        pytest.param(
            "health_origin", "discovery_run", id="origin-discovery-string"
        ),
        pytest.param(
            "health_origin", "time_expiry", id="origin-time-expiry-string"
        ),
        pytest.param(
            "health_origin",
            "controlled_context_transition",
            id="origin-context-transition-string",
        ),
        pytest.param("health_origin", "initial", id="origin-initial-string"),
        pytest.param("health_origin", "bogus", id="origin-unknown-string"),
        pytest.param("health_origin", None, id="origin-none"),
    ],
)
def test_source_snapshot_rejects_noncanonical_enum_values(
    field_name: str, malformed_value: object
) -> None:
    with pytest.raises(ValueError, match=field_name):
        replace(source(), **{field_name: malformed_value})


def test_source_snapshot_rejects_all_string_expiry_before_provenance_logic() -> None:
    with pytest.raises(ValueError, match="health"):
        replace(
            source(),
            health="healthy",
            freshness="stale",
            health_origin="time_expiry",
            last_committed_run_sequence=None,
            last_successful_observed_at=None,
            freshness_reference_at=None,
            freshness_valid_until=None,
            committed_context=None,
        )


def test_source_snapshot_accepts_canonical_enum_state_fixtures() -> None:
    unavailable = replace(
        source(),
        health=SourceHealth.SOURCE_UNAVAILABLE,
        freshness=SourceFreshness.STALE,
        health_reason="active_endpoint_timeout",
        last_issued_run_sequence=6,
        latest_completed_run_sequence=6,
        latest_completed_outcome="source_unavailable",
        last_health_run_sequence=6,
        last_run_health_outcome="source_unavailable",
    )
    configuration_error = replace(
        unavailable,
        health=SourceHealth.CONFIGURATION_ERROR,
        health_reason="invalid_source_configuration",
        latest_completed_outcome="configuration_error",
        last_run_health_outcome="configuration_error",
    )
    controlled_transition = replace(
        time_expiry_source(),
        health_origin=SourceHealthOrigin.CONTROLLED_CONTEXT_TRANSITION,
        health_reason="source_context_changed",
    )
    states = (
        source(),
        unavailable,
        time_expiry_source(),
        configuration_error,
        initial_source(),
        controlled_transition,
    )

    assert {item.health for item in states} == set(SourceHealth)
    assert {item.freshness for item in states} == set(SourceFreshness)
    assert {item.health_origin for item in states} == set(SourceHealthOrigin)


@pytest.mark.parametrize(
    ("field_name", "malformed_value"),
    [
        pytest.param("resource_type", "bogus", id="resource-type-unknown"),
        pytest.param("resource_type", "lxc", id="resource-type-lxc-string"),
        pytest.param("resource_type", "qemu", id="resource-type-qemu-string"),
        pytest.param("resource_type", "", id="resource-type-empty"),
        pytest.param("resource_type", None, id="resource-type-none"),
        pytest.param("resource_type", 1, id="resource-type-integer"),
        pytest.param("presence", "present", id="presence-string"),
        pytest.param("lifecycle", "active", id="lifecycle-string"),
        pytest.param(
            "observational_continuity",
            "consistent",
            id="observational-continuity-string",
        ),
        pytest.param(
            "security_continuity", "trusted", id="security-continuity-string"
        ),
        pytest.param("detail_status", "ok", id="detail-status-string"),
        pytest.param(
            "node_availability", "available", id="node-availability-string"
        ),
        pytest.param("state_level", "observed", id="state-level-string"),
    ],
)
def test_resource_snapshot_rejects_noncanonical_enum_values(
    field_name: str, malformed_value: object
) -> None:
    with pytest.raises(ValueError, match=field_name):
        replace(resource(), **{field_name: malformed_value})


@pytest.mark.parametrize("resource_type", [ResourceType.LXC, ResourceType.QEMU])
def test_resource_snapshot_accepts_canonical_resource_types(
    resource_type: ResourceType,
) -> None:
    item = replace(resource(), resource_type=resource_type)
    assert item.resource_type is resource_type


def test_resource_snapshot_accepts_canonical_state_enum_members() -> None:
    item = resource()
    assert item.presence is PresenceState.PRESENT
    assert item.lifecycle is LifecycleState.ACTIVE
    assert item.observational_continuity is ObservationalContinuity.CONSISTENT
    assert item.security_continuity is SecurityContinuity.UNVERIFIED
    assert item.detail_status is DetailStatus.OK
    assert item.node_availability is NodeAvailability.AVAILABLE
    assert item.state_level is ResourceStateLevel.OBSERVED


def test_uuid_text_aliases_are_not_normalized_into_registry_identity() -> None:
    assert resource(resource_id=BACKEND_ID).resource_id == BACKEND_ID
    with pytest.raises(ValueError, match="canonical"):
        resource(resource_id=BACKEND_ID.upper())
    with pytest.raises(ValueError, match="canonical"):
        resource(resource_id=BACKEND_ID.replace("-", ""))


def test_duplicate_confirmed_removed_and_current_locator_generation_is_rejected() -> None:
    old = confirmed_removed_resource()
    current = resource(SUCCESSOR_ID, generation=1, name="Reused slot")
    with pytest.raises(ValueError, match="duplicate retained locator generation"):
        snapshot(resources=(old, current))


def test_duplicate_not_current_and_successor_locator_generation_is_rejected() -> None:
    old = not_current_resource(generation=4)
    successor = resource(SUCCESSOR_ID, generation=4, name="Successor")
    with pytest.raises(ValueError, match="duplicate retained locator generation"):
        snapshot(resources=(old, successor))


def test_duplicate_terminal_locator_generations_are_rejected() -> None:
    first = confirmed_removed_resource()
    second = replace(first, resource_id=SUCCESSOR_ID)
    with pytest.raises(ValueError, match="duplicate retained locator generation"):
        snapshot(resources=(first, second))


def test_retained_and_current_distinct_locator_generations_are_accepted() -> None:
    old = confirmed_removed_resource()
    current = resource(SUCCESSOR_ID, generation=2, name="Reused slot")
    view = snapshot(resources=(old, current))
    assert {item.locator_generation for item in view.resources} == {1, 2}


def test_direct_replacement_distinct_locator_generations_are_accepted() -> None:
    old = not_current_resource(generation=4)
    successor = resource(SUCCESSOR_ID, generation=5, name="Successor")
    view = snapshot(resources=(old, successor))
    assert view.resources_by_id[SUCCESSOR_ID].locator_generation == 5


def test_same_locator_generation_is_legal_under_different_sources() -> None:
    source_a_resource = replace(
        resource(),
        current_node_id=None,
        node_availability=NodeAvailability.UNRESOLVED,
    )
    source_b_resource = replace(
        resource(SUCCESSOR_ID),
        inventory_source_id=SOURCE_B_ID,
        current_node_id=None,
        node_availability=NodeAvailability.UNRESOLVED,
    )
    view = snapshot(
        sources=(source(), source(source_id=SOURCE_B_ID, name="Second")),
        nodes=(),
        resources=(source_a_resource, source_b_resource),
    )
    assert len(view.current_resources_by_locator) == 2


@pytest.mark.parametrize(
    "generations",
    [(4,), (4, 5), (4, 5, 6), (1_000_000_000, 1_000_000_001)],
    ids=["single", "pair", "three", "large-values"],
)
def test_retained_locator_generation_history_is_consecutive(
    generations: tuple[int, ...],
) -> None:
    view = snapshot(resources=retained_generation_resources(*generations))
    assert tuple(item.locator_generation for item in view.resources) == generations


@pytest.mark.parametrize(
    "generations",
    [(4, 6), (4, 5, 7), (1_000_000_000, 2_000_000_000)],
    ids=["single-gap", "later-gap", "large-gap"],
)
def test_retained_locator_generation_history_rejects_internal_gaps(
    generations: tuple[int, ...],
) -> None:
    with pytest.raises(ValueError, match="generations must be consecutive"):
        snapshot(resources=retained_generation_resources(*generations))


def test_gapped_terminal_history_is_rejected_before_valid_current_generation() -> None:
    retained = retained_generation_resources(4, 6)
    current = resource(SUCCESSOR_ID, generation=7, name="Current after gap")
    with pytest.raises(ValueError, match="generations must be consecutive"):
        snapshot(resources=(*retained, current))


def test_new_resource_cannot_backfill_older_locator_history() -> None:
    previous_current = resource(generation=4)
    previous = snapshot(resources=(previous_current,))
    backfilled = replace(
        confirmed_removed_resource(),
        resource_id=SUCCESSOR_ID,
        locator_generation=3,
    )
    incoming = snapshot(
        resources=(backfilled, previous_current),
        inventory_revision=11,
        published_state_revision=21,
    )
    with pytest.raises(ValueError, match="follow all previously retained generations"):
        incoming.validate_revision_successor(previous)


def test_consecutive_terminal_history_accepts_exact_current_generation() -> None:
    retained = retained_generation_resources(4, 5)
    current = resource(SUCCESSOR_ID, generation=6, name="Current occupant")
    snapshot(resources=(*retained, current))


def test_locator_generation_consecutiveness_is_independent_between_sources() -> None:
    source_a = retained_generation_resources(4, 5)
    source_b = retained_generation_resources(
        4,
        5,
        source_id=SOURCE_B_ID,
    )
    snapshot(
        sources=(source(), source(source_id=SOURCE_B_ID, name="Second")),
        resources=(*source_a, *source_b),
    )


def test_ambiguity_retains_exact_binding_and_generation_across_revisions() -> None:
    previous = snapshot()
    ambiguous = missing_resource()
    incoming = snapshot(
        resources=(ambiguous,),
        inventory_revision=11,
        published_state_revision=21,
    )
    incoming = with_successful_source_commit(incoming)
    incoming.validate_revision_successor(previous)
    assert ambiguous.active_binding_id == previous.resources[0].active_binding_id
    assert ambiguous.locator_generation == previous.resources[0].locator_generation


def direct_replacement_views() -> tuple[
    HubinetOpsSnapshot,
    HubinetOpsSnapshot,
    ResourceSnapshot,
    ResourceSnapshot,
]:
    previous = snapshot(resources=(resource(generation=4),))
    old = not_current_resource(generation=4)
    successor = resource(SUCCESSOR_ID, generation=5, name="Successor")
    incoming = snapshot(
        resources=(old, successor),
        inventory_revision=11,
        published_state_revision=21,
    )
    incoming = with_successful_source_commit(incoming)
    return previous, incoming, old, successor


def test_revision_successor_rejects_observed_binding_reassignment() -> None:
    previous, _, old, successor = direct_replacement_views()
    previous_binding = previous.resources[0].active_binding_id
    assert previous_binding is not None
    reused_successor = replace(successor, active_binding_id=previous_binding)
    incoming = snapshot(
        resources=(old, reused_successor),
        inventory_revision=11,
        published_state_revision=21,
    )

    with pytest.raises(ValueError, match="binding identity cannot move"):
        incoming.validate_revision_successor(previous)


def test_revision_successor_accepts_fresh_successor_binding() -> None:
    previous, incoming, _, successor = direct_replacement_views()
    previous_binding = previous.resources[0].active_binding_id
    assert previous_binding is not None
    assert successor.active_binding_id != previous_binding

    incoming.validate_revision_successor(previous)


def test_revision_successor_accepts_same_resource_retaining_binding() -> None:
    previous = snapshot()
    previous_binding = previous.resources[0].active_binding_id
    renamed = replace(previous.resources[0], name="Renamed")
    incoming = snapshot(
        resources=(renamed,),
        inventory_revision=11,
        published_state_revision=21,
    )
    incoming = with_successful_source_commit(incoming)

    incoming.validate_revision_successor(previous)
    assert incoming.resources[0].active_binding_id == previous_binding


def test_revision_successor_rejects_binding_reassignment_across_locators() -> None:
    previous_resource = replace(
        resource(generation=4),
        active_binding_id=_test_only_binding_id("observed owner"),
    )
    previous = snapshot(resources=(previous_resource,))
    closed = replace(confirmed_removed_resource(), locator_generation=4)
    different_locator = replace(
        resource(SUCCESSOR_ID, name="Different locator"),
        active_binding_id=_test_only_binding_id("observed owner"),
        vmid=202,
    )
    incoming = snapshot(
        resources=(closed, different_locator),
        inventory_revision=11,
        published_state_revision=21,
    )

    with pytest.raises(ValueError, match="binding identity cannot move"):
        incoming.validate_revision_successor(previous)


def resource_before_terminal(
    security_continuity: SecurityContinuity,
) -> ResourceSnapshot:
    if security_continuity is SecurityContinuity.REVOKED:
        return replace(
            missing_resource(),
            locator_generation=4,
            security_continuity=SecurityContinuity.REVOKED,
        )
    return resource(
        generation=4,
        continuity_revision=2,
        security_continuity=security_continuity,
    )


def terminal_resources(
    presence: PresenceState,
    security_continuity: SecurityContinuity,
    *,
    continuity_revision: int,
) -> tuple[ResourceSnapshot, ...]:
    if presence is PresenceState.NOT_CURRENT:
        old = replace(
            not_current_resource(generation=4),
            resource_continuity_revision=continuity_revision,
            security_continuity=security_continuity,
        )
        return (old, resource(SUCCESSOR_ID, generation=5, name="Successor"))
    removed = replace(
        confirmed_removed_resource(),
        locator_generation=4,
        resource_continuity_revision=continuity_revision,
        security_continuity=security_continuity,
    )
    return (removed,)


@pytest.mark.parametrize(
    "presence",
    [PresenceState.NOT_CURRENT, PresenceState.CONFIRMED_REMOVED],
    ids=["not-current", "confirmed-removed"],
)
@pytest.mark.parametrize(
    ("previous_security", "terminal_security"),
    [
        (SecurityContinuity.UNVERIFIED, SecurityContinuity.UNVERIFIED),
        (SecurityContinuity.UNVERIFIED, SecurityContinuity.REVOKED),
        (SecurityContinuity.TRUSTED, SecurityContinuity.REVOKED),
        (SecurityContinuity.REVOKED, SecurityContinuity.REVOKED),
    ],
    ids=[
        "unverified-to-unverified",
        "unverified-to-revoked",
        "trusted-to-revoked",
        "revoked-to-revoked",
    ],
)
def test_terminal_security_history_accepts_valid_transition_matrix(
    presence: PresenceState,
    previous_security: SecurityContinuity,
    terminal_security: SecurityContinuity,
) -> None:
    previous = snapshot(
        resources=(resource_before_terminal(previous_security),),
    )
    incoming = snapshot(
        resources=terminal_resources(
            presence,
            terminal_security,
            continuity_revision=7,
        ),
        inventory_revision=11,
        published_state_revision=27,
    )
    incoming = with_successful_source_commit(incoming)
    incoming.validate_revision_successor(previous)


@pytest.mark.parametrize(
    "presence",
    [PresenceState.NOT_CURRENT, PresenceState.CONFIRMED_REMOVED],
    ids=["not-current", "confirmed-removed"],
)
@pytest.mark.parametrize(
    "previous_security",
    [SecurityContinuity.TRUSTED, SecurityContinuity.REVOKED],
    ids=["trusted", "revoked"],
)
def test_terminal_security_history_rejects_known_state_erasure(
    presence: PresenceState,
    previous_security: SecurityContinuity,
) -> None:
    previous = snapshot(
        resources=(resource_before_terminal(previous_security),),
    )
    incoming = snapshot(
        resources=terminal_resources(
            presence,
            SecurityContinuity.UNVERIFIED,
            continuity_revision=7,
        ),
        inventory_revision=11,
        published_state_revision=27,
    )
    with pytest.raises(ValueError, match="cannot erase known security history"):
        incoming.validate_revision_successor(previous)


@pytest.mark.parametrize(
    "presence",
    [PresenceState.NOT_CURRENT, PresenceState.CONFIRMED_REMOVED],
    ids=["not-current", "confirmed-removed"],
)
def test_retained_terminal_resource_preserves_revoked_security(
    presence: PresenceState,
) -> None:
    retained = terminal_resources(
        presence,
        SecurityContinuity.REVOKED,
        continuity_revision=3,
    )
    previous = snapshot(
        resources=retained,
        inventory_revision=11,
        published_state_revision=21,
    )
    incoming = snapshot(
        resources=retained,
        inventory_revision=11,
        published_state_revision=22,
    )
    incoming.validate_revision_successor(previous)


@pytest.mark.parametrize(
    "presence",
    [PresenceState.NOT_CURRENT, PresenceState.CONFIRMED_REMOVED],
    ids=["not-current", "confirmed-removed"],
)
def test_retained_terminal_resource_cannot_downgrade_revoked_security(
    presence: PresenceState,
) -> None:
    previous_resources = terminal_resources(
        presence,
        SecurityContinuity.REVOKED,
        continuity_revision=3,
    )
    incoming_resources = terminal_resources(
        presence,
        SecurityContinuity.UNVERIFIED,
        continuity_revision=4,
    )
    previous = snapshot(
        resources=previous_resources,
        inventory_revision=11,
        published_state_revision=21,
    )
    incoming = snapshot(
        resources=incoming_resources,
        inventory_revision=12,
        published_state_revision=22,
    )
    with pytest.raises(ValueError, match="cannot erase known security history"):
        incoming.validate_revision_successor(previous)


@pytest.mark.parametrize(
    ("previous_security", "incoming_security"),
    [
        (SecurityContinuity.UNVERIFIED, SecurityContinuity.UNVERIFIED),
        (SecurityContinuity.UNVERIFIED, SecurityContinuity.TRUSTED),
        (SecurityContinuity.UNVERIFIED, SecurityContinuity.REVOKED),
        (SecurityContinuity.TRUSTED, SecurityContinuity.TRUSTED),
        (SecurityContinuity.TRUSTED, SecurityContinuity.REVOKED),
        (SecurityContinuity.REVOKED, SecurityContinuity.REVOKED),
        (SecurityContinuity.REVOKED, SecurityContinuity.TRUSTED),
    ],
    ids=[
        "unverified-unverified",
        "unverified-trusted",
        "unverified-revoked",
        "trusted-trusted",
        "trusted-revoked",
        "revoked-revoked",
        "revoked-trusted",
    ],
)
def test_known_security_lower_bound_accepts_canonical_nonterminal_transitions(
    previous_security: SecurityContinuity,
    incoming_security: SecurityContinuity,
) -> None:
    previous_resource = nonterminal_security_resource(
        previous_security,
        continuity_revision=2,
    )
    security_changed = incoming_security is not previous_security
    incoming_resource = nonterminal_security_resource(
        incoming_security,
        continuity_revision=3 if security_changed else 2,
    )
    incoming = snapshot(
        resources=(incoming_resource,),
        inventory_revision=11 if security_changed else 10,
        published_state_revision=21,
    )
    previous = snapshot(resources=(previous_resource,))
    if (
        incoming.source_reconciliation_projection
        != previous.source_reconciliation_projection
    ):
        incoming = with_successful_source_commit(incoming)
    incoming.validate_revision_successor(previous)


@pytest.mark.parametrize(
    ("previous_security", "incoming_resource"),
    [
        (
            SecurityContinuity.TRUSTED,
            resource(
                continuity_revision=3,
                security_continuity=SecurityContinuity.UNVERIFIED,
            ),
        ),
        (
            SecurityContinuity.REVOKED,
            replace(
                missing_resource(),
                resource_continuity_revision=3,
                security_continuity=SecurityContinuity.UNVERIFIED,
            ),
        ),
    ],
    ids=["trusted-present-unverified", "revoked-missing-unverified"],
)
def test_known_security_lower_bound_rejects_nonterminal_erasure(
    previous_security: SecurityContinuity,
    incoming_resource: ResourceSnapshot,
) -> None:
    previous_resource = nonterminal_security_resource(
        previous_security,
        continuity_revision=2,
    )
    incoming = snapshot(
        resources=(incoming_resource,),
        inventory_revision=11,
        published_state_revision=21,
    )
    with pytest.raises(ValueError, match="cannot erase known security history"):
        incoming.validate_revision_successor(
            snapshot(resources=(previous_resource,))
        )


def test_revision_gap_may_skip_handoff_and_observe_trusted_successor() -> None:
    previous, _, old, successor = direct_replacement_views()
    incoming = snapshot(
        resources=(
            old,
            replace(
                successor,
                resource_continuity_revision=2,
                security_continuity=SecurityContinuity.TRUSTED,
            ),
        ),
        inventory_revision=12,
        published_state_revision=23,
    )
    incoming = with_successful_source_commit(incoming)
    incoming.validate_revision_successor(previous)


def test_revision_gap_allows_skipped_enrollment_before_replacement() -> None:
    previous, _, old, successor = direct_replacement_views()
    revoked_old = replace(
        old,
        resource_continuity_revision=3,
        security_continuity=SecurityContinuity.REVOKED,
    )
    incoming = snapshot(
        resources=(revoked_old, successor),
        inventory_revision=12,
        published_state_revision=23,
    )
    incoming = with_successful_source_commit(incoming)
    incoming.validate_revision_successor(previous)


def test_trusted_predecessor_may_be_revoked_by_replacement() -> None:
    previous = snapshot(
        resources=(
            resource(
                generation=4,
                continuity_revision=2,
                security_continuity=SecurityContinuity.TRUSTED,
            ),
        )
    )
    revoked_old = replace(
        not_current_resource(generation=4),
        resource_continuity_revision=3,
        security_continuity=SecurityContinuity.REVOKED,
    )
    incoming = snapshot(
        resources=(revoked_old, resource(SUCCESSOR_ID, generation=5)),
        inventory_revision=11,
        published_state_revision=21,
    )
    incoming = with_successful_source_commit(incoming)
    incoming.validate_revision_successor(previous)


def test_revision_gap_does_not_reconstruct_predecessor_security_path() -> None:
    previous, _, old, successor = direct_replacement_views()
    after_multiple_security_transitions = replace(
        old,
        resource_continuity_revision=5,
        security_continuity=SecurityContinuity.REVOKED,
    )
    incoming = snapshot(
        resources=(after_multiple_security_transitions, successor),
        inventory_revision=15,
        published_state_revision=27,
    )
    incoming = with_successful_source_commit(incoming)
    incoming.validate_revision_successor(previous)


def test_terminal_replacement_cannot_remain_trusted() -> None:
    with pytest.raises(ValueError, match="canonical state matrix"):
        replace(
            not_current_resource(generation=4),
            security_continuity=SecurityContinuity.TRUSTED,
        )


def test_terminal_replacement_requires_newer_continuity_revision() -> None:
    previous, _, old, successor = direct_replacement_views()
    unchanged_revision = replace(
        old,
        resource_continuity_revision=1,
        security_continuity=SecurityContinuity.REVOKED,
    )
    incoming = snapshot(
        resources=(unchanged_revision, successor),
        inventory_revision=11,
        published_state_revision=21,
    )
    with pytest.raises(
        ValueError,
        match="requires a newer resource_continuity_revision",
    ):
        incoming.validate_revision_successor(previous)


def test_terminal_replacement_cannot_retain_active_binding() -> None:
    with pytest.raises(ValueError, match="must not have an active binding"):
        replace(
            not_current_resource(generation=4),
            active_binding_id=_test_only_binding_id("still active"),
        )


def test_revision_gap_may_skip_handoff_and_successor_policy_transition() -> None:
    previous, _, old, successor = direct_replacement_views()
    policy_successor = replace(
        successor,
        resource_continuity_revision=3,
        security_continuity=SecurityContinuity.TRUSTED,
        state_level=ResourceStateLevel.MANAGED,
        retained_policy={"managed": True},
        effective_policy={"managed": True},
        policy_applicable=True,
        effective_capabilities=frozenset({"restart"}),
    )
    incoming = snapshot(
        resources=(old, policy_successor),
        inventory_revision=13,
        published_state_revision=24,
    )
    incoming = with_successful_source_commit(incoming)
    incoming.validate_revision_successor(previous)


@pytest.mark.parametrize(
    "successor_changes",
    [
        {"inventory_source_id": SOURCE_B_ID},
        {"vmid": 102},
        {"locator_generation": 6},
    ],
    ids=["wrong-source", "wrong-vmid", "wrong-generation"],
)
def test_direct_replacement_rejects_wrong_successor_locator_history(
    successor_changes: dict[str, Any],
) -> None:
    _, _, old, successor = direct_replacement_views()
    changed = replace(successor, **successor_changes)
    if changed.inventory_source_id == SOURCE_B_ID:
        changed = replace(
            changed,
            current_node_id=None,
            node_availability=NodeAvailability.UNRESOLVED,
        )
    sources = (
        (source(), source(source_id=SOURCE_B_ID, name="Second"))
        if changed.inventory_source_id == SOURCE_B_ID
        else (source(),)
    )
    with pytest.raises(
        ValueError,
        match="locator history|retained terminal history",
    ):
        snapshot(sources=sources, resources=(old, changed))


def test_terminal_successor_lineage_cannot_be_rewritten() -> None:
    previous, _, old, _ = direct_replacement_views()
    accepted = snapshot(
        resources=(old, resource(SUCCESSOR_ID, generation=5, name="Successor")),
        inventory_revision=11,
        published_state_revision=21,
    )
    accepted = with_successful_source_commit(accepted)
    accepted.validate_revision_successor(previous)

    rewritten_old = replace(old, successor_resource_id=THIRD_RESOURCE_ID)
    rewritten = snapshot(
        resources=(
            rewritten_old,
            resource(THIRD_RESOURCE_ID, generation=5, name="Different successor"),
        ),
        inventory_revision=12,
        published_state_revision=22,
    )
    rewritten = replace(rewritten, sources=accepted.sources)
    with pytest.raises(ValueError, match="replacement lineage is immutable"):
        rewritten.validate_revision_successor(accepted)


def test_terminal_successor_lineage_cannot_be_cleared() -> None:
    previous, accepted, old, successor = direct_replacement_views()
    accepted.validate_revision_successor(previous)
    cleared = replace(
        old,
        presence=PresenceState.CONFIRMED_REMOVED,
        observational_continuity=ObservationalContinuity.CONSISTENT,
        resource_continuity_revision=3,
        termination_reason="confirmed_removed",
        successor_resource_id=None,
    )
    incoming = snapshot(
        resources=(cleared, successor),
        inventory_revision=12,
        published_state_revision=22,
    )
    incoming = replace(incoming, sources=accepted.sources)
    with pytest.raises(ValueError, match="replacement lineage is immutable"):
        incoming.validate_revision_successor(accepted)


def test_replacement_successor_must_be_retained_in_same_snapshot() -> None:
    old = not_current_resource(generation=4)
    with pytest.raises(ValueError, match="successor is absent"):
        snapshot(resources=(old,))


def test_replacement_lineage_cannot_reference_itself() -> None:
    old = not_current_resource(
        generation=4,
        successor_resource_id=RESOURCE_ID,
    )
    with pytest.raises(ValueError, match="cannot reference itself"):
        snapshot(resources=(old,))


def test_valid_direct_replacement_handoff_is_accepted() -> None:
    previous, incoming, _, _ = direct_replacement_views()
    incoming.validate_revision_successor(previous)


def test_terminal_replacement_resource_cannot_be_reopened() -> None:
    old = not_current_resource(generation=4)
    successor = resource(SUCCESSOR_ID, generation=5, name="Successor")
    reopened_old = resource(generation=4, continuity_revision=3)
    with pytest.raises(ValueError, match="multiple current occupants for a locator"):
        snapshot(resources=(reopened_old, successor))


def test_revision_gap_may_observe_historical_successor_in_quarantine() -> None:
    previous, _, old, successor = direct_replacement_views()
    quarantined = replace(
        successor,
        resource_continuity_revision=2,
        lifecycle=LifecycleState.QUARANTINED,
        observational_continuity=ObservationalContinuity.UNCERTAIN,
    )
    incoming = snapshot(
        resources=(old, quarantined),
        inventory_revision=12,
        published_state_revision=23,
    )
    with_successful_source_commit(incoming).validate_revision_successor(previous)


def test_revision_gap_may_observe_historical_successor_confirmed_removed() -> None:
    previous, _, old, successor = direct_replacement_views()
    removed = replace(
        successor,
        active_binding_id=None,
        resource_continuity_revision=2,
        current_node_id=None,
        last_known_node_id=NODE_A,
        presence=PresenceState.CONFIRMED_REMOVED,
        lifecycle=LifecycleState.RETIRED,
        detail_status=DetailStatus.NOT_APPLICABLE,
        node_availability=NodeAvailability.NOT_APPLICABLE,
        status="unknown",
        termination_reason="confirmed_removed",
    )
    later = snapshot(
        resources=(old, removed),
        inventory_revision=12,
        published_state_revision=23,
    )
    later = with_successful_source_commit(later)
    later.validate_revision_successor(previous)
    assert later.resources_by_id[RESOURCE_ID].successor_resource_id == SUCCESSOR_ID
    assert later.resources_by_id[SUCCESSOR_ID].active_binding_id is None


def test_revision_gap_may_skip_entire_intermediate_successor_generation() -> None:
    previous, _, old_a, successor_b = direct_replacement_views()
    old_b = not_current_resource(
        resource_id=SUCCESSOR_ID,
        generation=5,
        successor_resource_id=THIRD_RESOURCE_ID,
    )
    successor_c = resource(
        THIRD_RESOURCE_ID,
        generation=6,
        name="Second successor",
    )
    chained = snapshot(
        resources=(old_a, old_b, successor_c),
        inventory_revision=13,
        published_state_revision=24,
    )
    chained = with_successful_source_commit(chained)
    chained.validate_revision_successor(previous)
    assert chained.resources_by_id[RESOURCE_ID].successor_resource_id == SUCCESSOR_ID
    assert chained.resources_by_id[SUCCESSOR_ID].successor_resource_id == THIRD_RESOURCE_ID
    assert successor_b.resource_id == SUCCESSOR_ID


@pytest.mark.parametrize(
    ("generation", "match"),
    [
        (3, "follow retained terminal history"),
        (4, "duplicate retained locator generation"),
        (6, "follow retained terminal history"),
    ],
)
def test_current_binding_must_use_next_retained_generation(
    generation: int,
    match: str,
) -> None:
    terminal = replace(confirmed_removed_resource(), locator_generation=4)
    current = resource(SUCCESSOR_ID, generation=generation, name="New occupant")
    with pytest.raises(ValueError, match=match):
        snapshot(resources=(terminal, current))


def test_direct_replacement_cannot_skip_locator_generation() -> None:
    old = not_current_resource(generation=4)
    successor = resource(SUCCESSOR_ID, generation=6, name="Successor")
    with pytest.raises(ValueError, match="follow retained terminal history"):
        snapshot(resources=(old, successor))


def test_current_binding_uses_maximum_retained_generation_plus_one() -> None:
    terminal_ids = (RESOURCE_ID, SUCCESSOR_ID, THIRD_RESOURCE_ID)
    terminal = tuple(
        replace(
            confirmed_removed_resource(),
            resource_id=resource_id,
            locator_generation=generation,
        )
        for generation, resource_id in enumerate(terminal_ids, start=1)
    )
    invalid_current = resource(
        "df27e5ab-308d-4f4a-a2ab-26c70142395e",
        generation=5,
        name="Skipped generation",
    )
    with pytest.raises(ValueError, match="follow retained terminal history"):
        snapshot(resources=(*terminal, invalid_current))


def test_terminal_history_accepts_exact_next_generation() -> None:
    terminal = replace(confirmed_removed_resource(), locator_generation=4)
    current = resource(SUCCESSOR_ID, generation=5, name="New occupant")
    view = snapshot(resources=(terminal, current))
    assert view.current_resources_by_locator[(SOURCE_ID, 101)] is current


def test_multiple_terminal_generations_accept_exact_next_generation() -> None:
    terminal_ids = (RESOURCE_ID, SUCCESSOR_ID, THIRD_RESOURCE_ID)
    terminal = tuple(
        replace(
            confirmed_removed_resource(),
            resource_id=resource_id,
            locator_generation=generation,
        )
        for generation, resource_id in enumerate(terminal_ids, start=1)
    )
    current = resource(
        "df27e5ab-308d-4f4a-a2ab-26c70142395e",
        generation=4,
        name="Current occupant",
    )
    snapshot(resources=(*terminal, current))


def test_locator_generation_histories_are_independent_between_sources() -> None:
    terminal_a = replace(confirmed_removed_resource(), locator_generation=4)
    current_a = resource(SUCCESSOR_ID, generation=5, name="Source A current")
    terminal_b = replace(
        confirmed_removed_resource(),
        resource_id=THIRD_RESOURCE_ID,
        inventory_source_id=SOURCE_B_ID,
        locator_generation=9,
        last_known_node_id=None,
    )
    current_b = replace(
        resource(
            "df27e5ab-308d-4f4a-a2ab-26c70142395e",
            generation=10,
            name="Source B current",
        ),
        inventory_source_id=SOURCE_B_ID,
        current_node_id=None,
        node_availability=NodeAvailability.UNRESOLVED,
    )
    snapshot(
        sources=(source(), source(source_id=SOURCE_B_ID, name="Second")),
        resources=(terminal_a, current_a, terminal_b, current_b),
    )


def test_missing_resource_returns_present_without_generation_change() -> None:
    missing = missing_resource()
    previous = snapshot(resources=(missing,))
    returned = resource(continuity_revision=3)
    incoming = snapshot(
        resources=(returned,),
        inventory_revision=11,
        published_state_revision=21,
    )
    incoming = with_successful_source_commit(incoming)
    incoming.validate_revision_successor(previous)
    assert returned.locator_generation == missing.locator_generation
    assert returned.active_binding_id == missing.active_binding_id


def test_nonterminal_inventory_change_does_not_bump_locator_generation() -> None:
    previous = snapshot()
    renamed = replace(resource(), name="Renamed")
    incoming = snapshot(
        resources=(renamed,),
        inventory_revision=11,
        published_state_revision=21,
    )
    incoming = with_successful_source_commit(incoming)
    incoming.validate_revision_successor(previous)
    assert renamed.locator_generation == previous.resources[0].locator_generation


def test_successor_rejects_omitted_retained_source() -> None:
    previous = snapshot(nodes=(), resources=())
    incoming = snapshot(
        sources=(),
        nodes=(),
        resources=(),
        inventory_revision=11,
        published_state_revision=21,
    )
    with pytest.raises(ValueError, match="omit a retained inventory source"):
        incoming.validate_revision_successor(previous)


def test_successor_rejects_omitted_retained_node() -> None:
    previous = snapshot(resources=())
    incoming = snapshot(
        nodes=(),
        resources=(),
        inventory_revision=11,
        published_state_revision=21,
    )
    with pytest.raises(ValueError, match="omit a retained node"):
        incoming.validate_revision_successor(previous)


def test_successor_rejects_omitted_retained_resource() -> None:
    previous = snapshot()
    incoming = snapshot(
        resources=(),
        inventory_revision=11,
        published_state_revision=21,
    )
    with pytest.raises(ValueError, match="omit a retained resource"):
        incoming.validate_revision_successor(previous)


def test_terminal_resource_cannot_cross_an_omitted_published_view() -> None:
    previous = snapshot(resources=(confirmed_removed_resource(),))
    omitted = snapshot(
        resources=(),
        inventory_revision=11,
        published_state_revision=21,
    )
    with pytest.raises(ValueError, match="omit a retained resource"):
        omitted.validate_revision_successor(previous)


def test_trusted_current_resource_cannot_cross_an_omitted_published_view() -> None:
    trusted = resource(security_continuity=SecurityContinuity.TRUSTED)
    previous = snapshot(resources=(trusted,))
    omitted = snapshot(
        resources=(),
        inventory_revision=11,
        published_state_revision=21,
    )
    with pytest.raises(ValueError, match="omit a retained resource"):
        omitted.validate_revision_successor(previous)


def test_retained_resource_states_replace_omission() -> None:
    previous = snapshot()
    missing = snapshot(
        resources=(missing_resource(),),
        inventory_revision=11,
        published_state_revision=21,
    )
    with_successful_source_commit(missing).validate_revision_successor(previous)
    removed = snapshot(
        resources=(confirmed_removed_resource(),),
        inventory_revision=11,
        published_state_revision=21,
    )
    with_successful_source_commit(removed).validate_revision_successor(previous)


def test_direct_replacement_retains_old_and_adds_successor() -> None:
    previous = snapshot()
    old = resource(
        continuity_revision=2,
        current_node_id=None,
        last_known_node_id=NODE_A,
        presence=PresenceState.NOT_CURRENT,
        lifecycle=LifecycleState.RETIRED,
        observational_continuity=ObservationalContinuity.REPLACED,
        detail_status=DetailStatus.NOT_APPLICABLE,
        node_availability=NodeAvailability.NOT_APPLICABLE,
        status="unknown",
        termination_reason="replaced",
        successor_resource_id=SUCCESSOR_ID,
    )
    successor = resource(SUCCESSOR_ID, generation=2, name="Successor")
    incoming = snapshot(
        resources=(old, successor),
        inventory_revision=11,
        published_state_revision=21,
    )
    incoming = with_successful_source_commit(incoming)
    incoming.validate_revision_successor(previous)
    assert set(incoming.resources_by_id) == {RESOURCE_ID, SUCCESSOR_ID}


def test_top_level_snapshot_collections_are_defensively_frozen() -> None:
    source_items = [source()]
    node_items = [node()]
    resource_items = [resource()]
    view = snapshot(
        sources=source_items,
        nodes=node_items,
        resources=resource_items,
    )
    expected = snapshot(
        sources=tuple(source_items),
        nodes=tuple(node_items),
        resources=tuple(resource_items),
    )

    source_items.clear()
    node_items.append(node(NODE_B, name="pve-b"))
    resource_items.clear()

    assert isinstance(view.sources, tuple)
    assert isinstance(view.nodes, tuple)
    assert isinstance(view.resources, tuple)
    assert view == expected
    assert view.sources == expected.sources
    assert view.nodes == expected.nodes
    assert view.resources == expected.resources


def reconciliation_inventory_change_cases(
) -> list[tuple[str, HubinetOpsSnapshot, HubinetOpsSnapshot]]:
    base = snapshot(nodes=(node(), node(NODE_B, name="pve-b")))
    return [
        (
            "source-facts",
            snapshot(resources=()),
            snapshot(
                sources=(source(facts={"cluster": "lab"}),),
                resources=(),
                published_state_revision=21,
            ),
        ),
        (
            "resource-rename",
            base,
            snapshot(
                nodes=base.nodes,
                resources=(replace(resource(), name="Renamed Resource"),),
                published_state_revision=21,
            ),
        ),
        (
            "resource-node-relation",
            base,
            snapshot(
                nodes=base.nodes,
                resources=(replace(resource(), current_node_id=NODE_B),),
                published_state_revision=21,
            ),
        ),
        (
            "resource-facts-and-state",
            base,
            snapshot(
                nodes=base.nodes,
                resources=(
                    replace(
                        resource(),
                        status="stopped",
                        state={"memory": 2048, "tags": ["inventory"]},
                    ),
                ),
                published_state_revision=21,
            ),
        ),
        (
            "resource-continuity-state",
            base,
            snapshot(
                nodes=base.nodes,
                resources=(missing_resource(),),
                published_state_revision=21,
            ),
        ),
        (
            "node-presentation-facts",
            snapshot(resources=()),
            snapshot(
                nodes=(
                    node(
                        name="pve-a-renamed",
                        status="maintenance",
                        available=False,
                        facts={"cpu": 0.4},
                    ),
                ),
                resources=(),
                published_state_revision=21,
            ),
        ),
        (
            "node-member-addition",
            snapshot(resources=()),
            snapshot(
                nodes=(node(), node(NODE_B, name="pve-b")),
                resources=(),
                published_state_revision=21,
            ),
        ),
        (
            "resource-member-addition",
            snapshot(resources=()),
            snapshot(resources=(resource(),), published_state_revision=21),
        ),
    ]


def independently_owned_inventory_change_cases(
) -> list[tuple[str, HubinetOpsSnapshot, HubinetOpsSnapshot]]:
    base = snapshot(nodes=(node(), node(NODE_B, name="pve-b")))
    return [
        (
            "source-display-name",
            snapshot(resources=()),
            snapshot(
                sources=(source(name="Renamed Source"),),
                resources=(),
                published_state_revision=21,
            ),
        ),
        (
            "retained-policy",
            base,
            snapshot(
                nodes=base.nodes,
                resources=(resource(retained_policy={"managed": False}),),
                published_state_revision=21,
            ),
        ),
        (
            "source-member-addition",
            snapshot(nodes=(), resources=()),
            snapshot(
                sources=(source(), source(source_id=SOURCE_B_ID, name="Second")),
                nodes=(),
                resources=(),
                published_state_revision=21,
            ),
        ),
    ]


def inventory_change_cases() -> list[tuple[str, HubinetOpsSnapshot, HubinetOpsSnapshot]]:
    return (
        reconciliation_inventory_change_cases()
        + independently_owned_inventory_change_cases()
    )


@pytest.mark.parametrize(
    ("name", "previous", "incoming"),
    inventory_change_cases(),
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_inventory_change_requires_new_inventory_revision(
    name: str,
    previous: HubinetOpsSnapshot,
    incoming: HubinetOpsSnapshot,
) -> None:
    del name
    with pytest.raises(ValueError, match="require a newer inventory_revision"):
        incoming.validate_revision_successor(previous)


@pytest.mark.parametrize(
    ("name", "previous", "incoming"),
    reconciliation_inventory_change_cases(),
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_reconciliation_inventory_change_rejects_only_newer_inventory_revision(
    name: str,
    previous: HubinetOpsSnapshot,
    incoming: HubinetOpsSnapshot,
) -> None:
    del name
    with pytest.raises(ValueError, match="newer last_committed_run_sequence"):
        replace(incoming, inventory_revision=11).validate_revision_successor(previous)


@pytest.mark.parametrize(
    ("name", "previous", "incoming"),
    reconciliation_inventory_change_cases(),
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_reconciliation_inventory_change_accepts_skipped_successful_commit(
    name: str,
    previous: HubinetOpsSnapshot,
    incoming: HubinetOpsSnapshot,
) -> None:
    del name
    with_successful_source_commit(
        replace(incoming, inventory_revision=11), sequence=7
    ).validate_revision_successor(previous)


@pytest.mark.parametrize(
    ("name", "previous", "incoming"),
    independently_owned_inventory_change_cases(),
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_independently_owned_inventory_change_does_not_require_source_commit(
    name: str,
    previous: HubinetOpsSnapshot,
    incoming: HubinetOpsSnapshot,
) -> None:
    del name
    replace(incoming, inventory_revision=11).validate_revision_successor(previous)


def test_new_resource_cannot_be_accepted_by_global_inventory_revision_alone() -> None:
    previous = snapshot(resources=())
    incoming = snapshot(
        resources=(resource(),),
        inventory_revision=11,
        published_state_revision=21,
    )

    with pytest.raises(ValueError, match="newer last_committed_run_sequence"):
        incoming.validate_revision_successor(previous)


def test_source_failure_cannot_create_missing_inventory_transition() -> None:
    previous = snapshot()
    failed_source = replace(
        source(),
        health=SourceHealth.SOURCE_UNAVAILABLE,
        freshness=SourceFreshness.STALE,
        health_reason="active_endpoint_timeout",
        last_issued_run_sequence=6,
        latest_completed_run_sequence=6,
        latest_completed_outcome="source_unavailable",
        last_health_run_sequence=6,
        last_run_health_outcome="source_unavailable",
    )
    incoming = snapshot(
        sources=(failed_source,),
        resources=(missing_resource(),),
        inventory_revision=11,
        published_state_revision=21,
    )

    with pytest.raises(ValueError, match="newer last_committed_run_sequence"):
        incoming.validate_revision_successor(previous)


@pytest.mark.parametrize(
    "precommit_source",
    [
        initial_source(),
        replace(
            initial_source(),
            health=SourceHealth.SOURCE_UNAVAILABLE,
            freshness=SourceFreshness.STALE,
            health_origin=SourceHealthOrigin.DISCOVERY_RUN,
            health_reason="first_observation_failed",
            last_issued_run_sequence=1,
            latest_completed_run_sequence=1,
            latest_completed_outcome="source_unavailable",
            last_health_run_sequence=1,
            last_run_health_outcome="source_unavailable",
        ),
    ],
    ids=["initial-not-yet-observed", "failed-before-first-commit"],
)
@pytest.mark.parametrize("inventory_kind", ["node", "resource"])
def test_precommit_source_cannot_publish_node_or_resource_inventory(
    precommit_source: InventorySourceSnapshot,
    inventory_kind: str,
) -> None:
    nodes = (node(),) if inventory_kind == "node" else ()
    resources = (resource(),) if inventory_kind == "resource" else ()

    with pytest.raises(ValueError, match="without a successful inventory commit"):
        snapshot(
            sources=(precommit_source,),
            nodes=nodes,
            resources=resources,
        )


def test_precommit_source_may_coexist_with_other_committed_source_inventory() -> None:
    second_source = replace(
        initial_source(),
        inventory_source_id=SOURCE_B_ID,
        name="Not yet observed",
        current_context=context(endpoint_id=ENDPOINT_B_ID),
    )

    view = snapshot(sources=(source(), second_source))

    assert view.resources[0].inventory_source_id == SOURCE_ID
    assert view.sources_by_id[SOURCE_B_ID].last_committed_run_sequence is None


@pytest.mark.parametrize(
    "resource_change",
    [
        {"status": "stopped"},
        {"state": {"memory": 2048}},
        {"detail_status": DetailStatus.ERROR},
        {"current_node_id": NODE_B},
        {"name": "Provider Rename"},
    ],
    ids=["status", "state-facts", "detail-status", "current-node", "name"],
)
def test_discovery_owned_resource_facts_cannot_rewrite_under_same_commit(
    resource_change: dict[str, Any],
) -> None:
    nodes = (node(), node(NODE_B, name="pve-b"))
    previous = snapshot(nodes=nodes)
    incoming = snapshot(
        nodes=nodes,
        resources=(replace(resource(), **resource_change),),
        inventory_revision=11,
        published_state_revision=21,
    )

    with pytest.raises(ValueError, match="newer last_committed_run_sequence"):
        incoming.validate_revision_successor(previous)


@pytest.mark.parametrize(
    "node_change",
    [
        {"name": "pve-renamed"},
        {"status": "maintenance"},
        {"available": False, "status": "offline"},
        {"facts": {"cpu": 0.4}},
    ],
    ids=["name", "status", "availability", "facts"],
)
def test_node_observation_cannot_rewrite_under_same_commit(
    node_change: dict[str, Any],
) -> None:
    previous = snapshot(nodes=(node(),), resources=())
    incoming = snapshot(
        nodes=(replace(node(), **node_change),),
        resources=(),
        inventory_revision=11,
        published_state_revision=21,
    )

    with pytest.raises(ValueError, match="newer last_committed_run_sequence"):
        incoming.validate_revision_successor(previous)


def test_successful_commit_then_failure_can_publish_newer_committed_missing() -> None:
    previous = snapshot()
    commit_six_then_failure_seven = replace(
        successful_commit_source(sequence=6),
        health=SourceHealth.SOURCE_UNAVAILABLE,
        freshness=SourceFreshness.STALE,
        health_reason="active_endpoint_timeout",
        last_issued_run_sequence=7,
        latest_completed_run_sequence=7,
        latest_completed_outcome="source_unavailable",
        last_health_run_sequence=7,
        last_run_health_outcome="source_unavailable",
    )
    incoming = snapshot(
        sources=(commit_six_then_failure_seven,),
        resources=(missing_resource(),),
        inventory_revision=11,
        published_state_revision=21,
    )

    incoming.validate_revision_successor(previous)
    assert incoming.sources[0].last_committed_run_sequence == 6
    assert incoming.resources[0].presence is PresenceState.MISSING


def test_security_only_transition_does_not_require_discovery_commit() -> None:
    previous = snapshot()
    enrolled = replace(
        resource(),
        security_continuity=SecurityContinuity.TRUSTED,
        resource_continuity_revision=2,
    )
    incoming = snapshot(
        resources=(enrolled,),
        inventory_revision=11,
        published_state_revision=21,
    )

    incoming.validate_revision_successor(previous)
    assert (
        incoming.source_reconciliation_projection
        == previous.source_reconciliation_projection
    )


def test_security_continuity_resolution_remains_independent_of_discovery() -> None:
    quarantined = resource(
        continuity_revision=2,
        lifecycle=LifecycleState.QUARANTINED,
        observational_continuity=ObservationalContinuity.UNCERTAIN,
        security_continuity=SecurityContinuity.REVOKED,
    )
    previous = snapshot(resources=(quarantined,))
    resolved = replace(
        quarantined,
        resource_continuity_revision=3,
        lifecycle=LifecycleState.ACTIVE,
        observational_continuity=ObservationalContinuity.CONSISTENT,
        security_continuity=SecurityContinuity.TRUSTED,
    )
    incoming = snapshot(
        resources=(resolved,),
        inventory_revision=11,
        published_state_revision=21,
    )

    incoming.validate_revision_successor(previous)
    assert (
        incoming.source_reconciliation_projection
        == previous.source_reconciliation_projection
    )


def test_health_only_source_transition_preserves_inventory_revision() -> None:
    previous = snapshot()
    unavailable = replace(
        source(),
        health=SourceHealth.SOURCE_UNAVAILABLE,
        freshness=SourceFreshness.STALE,
        health_reason="active_endpoint_timeout",
        last_issued_run_sequence=6,
        latest_completed_run_sequence=6,
        latest_completed_outcome="source_unavailable",
        last_health_run_sequence=6,
        last_run_health_outcome="source_unavailable",
    )
    incoming = snapshot(
        sources=(unavailable,),
        inventory_revision=10,
        published_state_revision=21,
    )
    incoming.validate_revision_successor(previous)
    assert incoming.inventory_projection == previous.inventory_projection


def test_time_expiry_and_capability_removal_preserve_inventory_revision() -> None:
    applicable = resource(
        security_continuity=SecurityContinuity.TRUSTED,
        retained_policy={"managed": True},
        effective_policy={"managed": True},
        policy_applicable=True,
        effective_capabilities=frozenset({"restart"}),
    )
    previous = snapshot(resources=(applicable,))
    expired_source = replace(
        source(),
        health=SourceHealth.HEALTHY,
        freshness=SourceFreshness.STALE,
        health_origin=SourceHealthOrigin.TIME_EXPIRY,
        health_reason="freshness_deadline_elapsed",
    )
    suspended = replace(
        applicable,
        effective_policy={},
        policy_applicable=False,
        suspended_reason="source_stale",
        effective_capabilities=frozenset(),
    )
    incoming = snapshot(
        sources=(expired_source,),
        resources=(suspended,),
        inventory_revision=10,
        published_state_revision=21,
    )
    incoming.validate_revision_successor(previous)
    assert incoming.inventory_projection == previous.inventory_projection


def test_completion_provenance_only_transition_preserves_inventory_revision() -> None:
    previous = snapshot()
    completion_only = replace(
        source(),
        last_issued_run_sequence=6,
        latest_completed_run_sequence=6,
        latest_completed_outcome="audit_only_inapplicable",
    )
    incoming = snapshot(
        sources=(completion_only,),
        inventory_revision=10,
        published_state_revision=21,
    )
    incoming.validate_revision_successor(previous)
    assert incoming.inventory_projection == previous.inventory_projection


def source_with_run_lattice(
    *,
    issued: int,
    completed: int | None,
    health: int | None,
    committed: int | None,
) -> InventorySourceSnapshot:
    changes: dict[str, Any] = {
        "last_issued_run_sequence": issued,
        "latest_completed_run_sequence": completed,
        "latest_completed_outcome": (
            None
            if completed is None
            else "success" if completed == committed else "audit_or_failure"
        ),
        "last_health_run_sequence": health,
        "last_run_health_outcome": (
            None
            if health is None
            else "success" if health == committed else "source_unavailable"
        ),
        "last_committed_run_sequence": committed,
    }
    if committed is None:
        changes.update(
            last_successful_observed_at=None,
            freshness_reference_at=None,
            freshness_valid_until=None,
            committed_context=None,
        )
    if health is None:
        changes.update(
            health=SourceHealth.NOT_YET_OBSERVED,
            freshness=SourceFreshness.NOT_YET_OBSERVED,
            health_origin=SourceHealthOrigin.INITIAL,
            health_reason="",
        )
    elif health == committed:
        changes.update(
            health=SourceHealth.HEALTHY,
            freshness=SourceFreshness.FRESH,
            health_origin=SourceHealthOrigin.DISCOVERY_RUN,
            health_reason="authoritative_inventory_commit",
        )
    else:
        changes.update(
            health=SourceHealth.SOURCE_UNAVAILABLE,
            freshness=SourceFreshness.STALE,
            health_origin=SourceHealthOrigin.DISCOVERY_RUN,
            health_reason="applicable_run_failed",
        )
    return replace(source(), **changes)


@pytest.mark.parametrize(
    ("issued", "completed", "health", "committed"),
    [
        (5, 5, 5, 5),
        (8, 7, 6, 5),
        (5, 5, None, None),
        (5, 5, 5, None),
    ],
    ids=[
        "all-equal",
        "ordered-gaps",
        "completion-only",
        "health-without-commit",
    ],
)
def test_source_run_provenance_accepts_partial_order(
    issued: int,
    completed: int | None,
    health: int | None,
    committed: int | None,
) -> None:
    run_source = source_with_run_lattice(
        issued=issued,
        completed=completed,
        health=health,
        committed=committed,
    )
    assert run_source.last_issued_run_sequence == issued


@pytest.mark.parametrize(
    ("issued", "completed", "health", "committed"),
    [
        (5, None, 5, None),
        (6, 5, 6, None),
        (6, 6, 5, 6),
        (5, 6, None, None),
        (5, 6, 6, None),
    ],
    ids=[
        "health-without-completion",
        "health-after-completion",
        "commit-after-health",
        "completion-after-issued",
        "health-after-issued",
    ],
)
def test_source_run_provenance_rejects_partial_order_violation(
    issued: int,
    completed: int | None,
    health: int | None,
    committed: int | None,
) -> None:
    with pytest.raises(ValueError, match="source run provenance must satisfy"):
        source_with_run_lattice(
            issued=issued,
            completed=completed,
            health=health,
            committed=committed,
        )


def test_initial_source_accepts_issued_and_completion_only_provenance() -> None:
    issued_only = replace(initial_source(), last_issued_run_sequence=3)
    completion_only = source_with_run_lattice(
        issued=5,
        completed=5,
        health=None,
        committed=None,
    )
    assert issued_only.last_health_run_sequence is None
    assert completion_only.latest_completed_run_sequence == 5
    assert completion_only.last_health_run_sequence is None


def test_initial_source_rejects_applied_run_health_provenance() -> None:
    applied = source_with_run_lattice(
        issued=5,
        completed=5,
        health=5,
        committed=None,
    )
    with pytest.raises(ValueError, match="cannot have applied run"):
        replace(
            applied,
            health=SourceHealth.NOT_YET_OBSERVED,
            freshness=SourceFreshness.NOT_YET_OBSERVED,
            health_origin=SourceHealthOrigin.INITIAL,
            health_reason="",
        )


def test_fresh_source_requires_successful_exact_committed_health_outcome() -> None:
    with pytest.raises(ValueError, match="successful health outcome"):
        replace(source(), last_run_health_outcome="source_unavailable")


def test_fresh_source_requires_successful_exact_committed_completion_outcome() -> None:
    with pytest.raises(ValueError, match="successful completion outcome"):
        replace(source(), latest_completed_outcome="source_unavailable")


def test_normal_successful_commit_is_fresh_and_current() -> None:
    committed = source()
    assert committed.last_run_health_outcome == "success"
    assert committed.latest_completed_outcome == "success"
    assert committed.current_facts_available


def test_fresh_source_allows_newer_audit_only_completion() -> None:
    committed = replace(
        source(),
        last_issued_run_sequence=6,
        latest_completed_run_sequence=6,
        latest_completed_outcome="audit_only_inapplicable",
    )
    assert committed.last_committed_run_sequence == 5
    assert committed.last_health_run_sequence == 5
    assert committed.latest_completed_run_sequence == 6
    assert committed.current_facts_available


def test_time_expiry_rejects_changed_current_context() -> None:
    with pytest.raises(ValueError, match="exact committed run and context"):
        time_expiry_source(current_context=context(revision=4))


def test_time_expiry_rejects_newer_health_run() -> None:
    with pytest.raises(ValueError, match="exact committed run and context"):
        time_expiry_source(
            last_issued_run_sequence=6,
            latest_completed_run_sequence=6,
            latest_completed_outcome="source_unavailable",
            last_health_run_sequence=6,
            last_run_health_outcome="source_unavailable",
        )


def test_time_expiry_rejects_missing_successful_commit() -> None:
    with pytest.raises(ValueError, match="exact committed run and context"):
        time_expiry_source(
            last_committed_run_sequence=None,
            last_successful_observed_at=None,
            freshness_reference_at=None,
            freshness_valid_until=None,
            committed_context=None,
        )


def test_time_expiry_rejects_non_stale_freshness() -> None:
    with pytest.raises(ValueError, match="exact committed run and context"):
        time_expiry_source(freshness=SourceFreshness.FRESH)


def test_time_expiry_accepts_exact_committed_run_and_context() -> None:
    expired = time_expiry_source()
    assert expired.current_context == expired.committed_context
    assert expired.last_health_run_sequence == expired.last_committed_run_sequence


def test_time_expiry_allows_newer_audit_only_completion() -> None:
    expired = time_expiry_source(
        last_issued_run_sequence=6,
        latest_completed_run_sequence=6,
        latest_completed_outcome="audit_only_inapplicable",
    )
    assert expired.latest_completed_run_sequence == 6
    assert expired.last_committed_run_sequence == 5
    assert expired.last_health_run_sequence == 5


@pytest.mark.parametrize("published_state_revision", [21, 500])
def test_expired_commit_cannot_return_to_fresh_without_new_commit(
    published_state_revision: int,
) -> None:
    previous = snapshot(
        sources=(time_expiry_source(),),
        resources=(),
    )
    incoming = snapshot(
        sources=(source(),),
        resources=(),
        published_state_revision=published_state_revision,
    )

    with pytest.raises(ValueError, match="stale source requires a newer"):
        incoming.validate_revision_successor(previous)


def test_expired_source_recovers_after_skipped_new_successful_commit() -> None:
    previous = snapshot(
        sources=(time_expiry_source(),),
        resources=(),
    )
    incoming = snapshot(
        sources=(successful_commit_source(sequence=7),),
        resources=(),
        inventory_revision=11,
        published_state_revision=500,
    )

    incoming.validate_revision_successor(previous)
    assert incoming.sources[0].last_committed_run_sequence == 7
    assert incoming.sources[0].current_facts_available


def test_expiry_cannot_be_hidden_by_intermediate_stale_view() -> None:
    expired = snapshot(
        sources=(time_expiry_source(),),
        resources=(),
    )
    still_stale = snapshot(
        sources=(
            replace(
                time_expiry_source(),
                health_origin=SourceHealthOrigin.CONTROLLED_CONTEXT_TRANSITION,
                health_reason="authority_remains_invalidated",
            ),
        ),
        resources=(),
        published_state_revision=21,
    )
    still_stale.validate_revision_successor(expired)
    resurrected = snapshot(
        sources=(source(),),
        resources=(),
        published_state_revision=22,
    )

    with pytest.raises(ValueError, match="stale source requires a newer"):
        resurrected.validate_revision_successor(still_stale)


def test_controlled_context_transition_is_not_misclassified_as_time_expiry() -> None:
    previous = snapshot(resources=())
    transitioned = replace(
        source(),
        health=SourceHealth.DEGRADED,
        freshness=SourceFreshness.STALE,
        health_origin=SourceHealthOrigin.CONTROLLED_CONTEXT_TRANSITION,
        health_reason="source_configuration_changed",
        current_context=context(revision=4),
    )
    incoming = snapshot(
        sources=(transitioned,),
        resources=(),
        published_state_revision=21,
    )
    incoming.validate_revision_successor(previous)


def test_newer_failed_health_run_is_not_misclassified_as_time_expiry() -> None:
    previous = snapshot(resources=())
    failed = replace(
        source(),
        health=SourceHealth.SOURCE_UNAVAILABLE,
        freshness=SourceFreshness.STALE,
        health_reason="active_endpoint_timeout",
        last_issued_run_sequence=6,
        latest_completed_run_sequence=6,
        latest_completed_outcome="source_unavailable",
        last_health_run_sequence=6,
        last_run_health_outcome="source_unavailable",
    )
    incoming = snapshot(
        sources=(failed,),
        resources=(),
        published_state_revision=21,
    )
    incoming.validate_revision_successor(previous)


def test_advanced_successful_commit_requires_new_inventory_revision() -> None:
    previous = snapshot(resources=())
    incoming = snapshot(
        sources=(successful_commit_source(),),
        resources=(),
        published_state_revision=21,
    )
    assert incoming.inventory_projection == previous.inventory_projection
    with pytest.raises(
        ValueError,
        match="successful inventory commit requires a newer inventory_revision",
    ):
        incoming.validate_revision_successor(previous)


def test_first_successful_commit_requires_new_inventory_revision() -> None:
    previous = snapshot(sources=(initial_source(),), nodes=(), resources=())
    incoming = snapshot(
        sources=(successful_commit_source(sequence=1),),
        nodes=(),
        resources=(),
        published_state_revision=21,
    )
    assert incoming.inventory_projection == previous.inventory_projection
    with pytest.raises(
        ValueError,
        match="successful inventory commit requires a newer inventory_revision",
    ):
        incoming.validate_revision_successor(previous)


def test_advanced_successful_commit_accepts_new_inventory_revision() -> None:
    previous = snapshot(resources=())
    incoming = snapshot(
        sources=(successful_commit_source(),),
        resources=(),
        inventory_revision=11,
        published_state_revision=21,
    )
    incoming.validate_revision_successor(previous)
    assert incoming.inventory_projection == previous.inventory_projection


def test_successful_commit_and_inventory_change_accept_new_inventory_revision() -> None:
    previous = snapshot()
    incoming = snapshot(
        sources=(successful_commit_source(),),
        resources=(replace(resource(), name="Renamed by reconciliation"),),
        inventory_revision=11,
        published_state_revision=21,
    )
    incoming.validate_revision_successor(previous)


def test_completion_only_advance_does_not_require_inventory_revision() -> None:
    previous = snapshot(resources=())
    completion_only = replace(
        source(),
        last_issued_run_sequence=6,
        latest_completed_run_sequence=6,
        latest_completed_outcome="audit_only_inapplicable",
    )
    snapshot(
        sources=(completion_only,),
        resources=(),
        published_state_revision=21,
    ).validate_revision_successor(previous)


def test_failed_health_advance_does_not_require_inventory_revision() -> None:
    previous = snapshot(resources=())
    failed = replace(
        source(),
        health=SourceHealth.SOURCE_UNAVAILABLE,
        freshness=SourceFreshness.STALE,
        health_reason="active_endpoint_timeout",
        last_issued_run_sequence=6,
        latest_completed_run_sequence=6,
        latest_completed_outcome="source_unavailable",
        last_health_run_sequence=6,
        last_run_health_outcome="source_unavailable",
    )
    snapshot(
        sources=(failed,),
        resources=(),
        published_state_revision=21,
    ).validate_revision_successor(previous)


def test_time_expiry_does_not_require_inventory_revision() -> None:
    previous = snapshot(resources=())
    snapshot(
        sources=(time_expiry_source(),),
        resources=(),
        published_state_revision=21,
    ).validate_revision_successor(previous)


def controlled_source(
    *,
    source_id: str,
    current: SourceContext,
    committed: SourceContext,
) -> InventorySourceSnapshot:
    return replace(
        source(source_id=source_id),
        health=SourceHealth.DEGRADED,
        freshness=SourceFreshness.STALE,
        health_origin=SourceHealthOrigin.CONTROLLED_CONTEXT_TRANSITION,
        health_reason="source_context_changed",
        current_context=current,
        committed_context=committed,
    )


def test_current_and_committed_context_may_match_exactly() -> None:
    current = source()
    assert current.current_context == current.committed_context


@pytest.mark.parametrize(
    ("current", "committed"),
    [
        (context(revision=4), context(revision=3)),
        (context(trust_revision=3), context(trust_revision=2)),
        (
            context(
                revision=4,
                locator="https://canonical-v2.example.test:8006",
                canonicalization_version=2,
            ),
            context(revision=3, canonicalization_version=1),
        ),
    ],
    ids=["newer-config", "newer-trust", "canonicalization-migration"],
)
def test_current_context_accepts_controlled_progression_from_committed_context(
    current: SourceContext,
    committed: SourceContext,
) -> None:
    transitioned = controlled_source(
        source_id=SOURCE_ID,
        current=current,
        committed=committed,
    )
    assert transitioned.current_context.source_config_revision >= (
        transitioned.committed_context.source_config_revision
    )


@pytest.mark.parametrize(
    ("current", "committed", "match"),
    [
        (
            context(revision=3),
            context(revision=4),
            "source_config_revision cannot predate",
        ),
        (
            context(trust_revision=3),
            context(trust_revision=4),
            "transport_trust_revision cannot predate",
        ),
        (
            context(endpoint_id=ENDPOINT_A_ID),
            context(endpoint_id=ENDPOINT_B_ID),
            "same endpoint",
        ),
        (
            context(locator="https://current.example.test:8006"),
            context(locator="https://committed.example.test:8006"),
            "cannot reinterpret",
        ),
        (
            context(revision=4, canonicalization_version=1),
            context(revision=3, canonicalization_version=2),
            "canonicalization contract cannot predate",
        ),
        (
            context(
                revision=3,
                locator="https://canonical-v2.example.test:8006",
                canonicalization_version=2,
            ),
            context(revision=3, canonicalization_version=1),
            "requires newer current source configuration",
        ),
    ],
    ids=[
        "config-regression",
        "trust-regression",
        "endpoint-mismatch",
        "same-version-locator-mismatch",
        "canonicalization-regression",
        "migration-without-config-progression",
    ],
)
def test_current_context_rejects_incoherent_committed_provenance(
    current: SourceContext,
    committed: SourceContext,
    match: str,
) -> None:
    with pytest.raises(ValueError, match=match):
        controlled_source(
            source_id=SOURCE_ID,
            current=current,
            committed=committed,
        )


def stale_source_after_successful_commit(
    *,
    current: SourceContext,
    committed: SourceContext,
) -> InventorySourceSnapshot:
    return replace(
        successful_commit_source(sequence=6),
        health=SourceHealth.DEGRADED,
        freshness=SourceFreshness.STALE,
        health_origin=SourceHealthOrigin.CONTROLLED_CONTEXT_TRANSITION,
        health_reason="source_context_changed_after_commit",
        current_context=current,
        committed_context=committed,
    )


@pytest.mark.parametrize(
    ("current", "committed", "match"),
    [
        (
            context(revision=4),
            context(revision=2),
            "committed source_config_revision must not regress",
        ),
        (
            context(revision=4, trust_revision=3),
            context(revision=3, trust_revision=1),
            "committed transport_trust_revision must not regress",
        ),
        (
            context(
                revision=4,
                locator="https://canonical-v2.example.test:8006",
                canonicalization_version=2,
            ),
            context(
                revision=3,
                locator="https://rewritten-v1.example.test:8006",
                canonicalization_version=1,
            ),
            "committed canonical locator is immutable",
        ),
        (
            context(
                revision=4,
                locator="https://canonical-v2.example.test:8006",
                canonicalization_version=2,
            ),
            context(
                revision=3,
                locator="https://canonical-v2.example.test:8006",
                canonicalization_version=2,
            ),
            "committed canonicalization migration requires a newer",
        ),
    ],
    ids=[
        "committed-config-regression",
        "committed-trust-regression",
        "committed-locator-rewrite",
        "committed-migration-without-config-progression",
    ],
)
def test_successor_rejects_regressing_committed_context_history(
    current: SourceContext,
    committed: SourceContext,
    match: str,
) -> None:
    incoming = snapshot(
        sources=(
            stale_source_after_successful_commit(
                current=current,
                committed=committed,
            ),
        ),
        nodes=(),
        resources=(),
        inventory_revision=11,
        published_state_revision=21,
    )
    with pytest.raises(ValueError, match=match):
        incoming.validate_revision_successor(snapshot(nodes=(), resources=()))


def test_successor_accepts_progressing_committed_canonicalization_history() -> None:
    migrated = stale_source_after_successful_commit(
        current=context(
            revision=5,
            trust_revision=3,
            locator="https://canonical-v2.example.test:8006",
            canonicalization_version=2,
        ),
        committed=context(
            revision=4,
            trust_revision=3,
            locator="https://canonical-v2.example.test:8006",
            canonicalization_version=2,
        ),
    )
    snapshot(
        sources=(migrated,),
        nodes=(),
        resources=(),
        inventory_revision=11,
        published_state_revision=21,
    ).validate_revision_successor(snapshot(nodes=(), resources=()))


def test_snapshot_rejects_shared_current_endpoint_identity() -> None:
    shared = context(endpoint_id=ENDPOINT_A_ID)
    second = replace(
        source(source_id=SOURCE_B_ID),
        current_context=shared,
        committed_context=shared,
    )
    with pytest.raises(ValueError, match="shared across inventory sources"):
        snapshot(sources=(source(), second), nodes=(), resources=())


def test_snapshot_rejects_committed_to_current_endpoint_sharing() -> None:
    shared = context(endpoint_id=ENDPOINT_A_ID)
    first = controlled_source(
        source_id=SOURCE_ID,
        current=shared,
        committed=shared,
    )
    second = replace(
        source(source_id=SOURCE_B_ID),
        current_context=shared,
        committed_context=shared,
    )
    with pytest.raises(ValueError, match="shared across inventory sources"):
        snapshot(sources=(first, second), nodes=(), resources=())


def test_snapshot_rejects_current_to_committed_endpoint_sharing() -> None:
    shared = context(endpoint_id=ENDPOINT_A_ID)
    second = controlled_source(
        source_id=SOURCE_B_ID,
        current=shared,
        committed=shared,
    )
    with pytest.raises(ValueError, match="shared across inventory sources"):
        snapshot(sources=(source(), second), nodes=(), resources=())


def test_endpoint_identity_cannot_move_to_new_source_across_snapshots() -> None:
    previous = snapshot(nodes=(), resources=())
    moved_to_second = context(endpoint_id=ENDPOINT_A_ID)
    incoming = snapshot(
        sources=(
            replace(
                source(),
                current_context=context(
                    revision=4,
                    endpoint_id=ENDPOINT_B_ID,
                ),
                committed_context=context(
                    revision=4,
                    endpoint_id=ENDPOINT_B_ID,
                ),
            ),
            replace(
                source(source_id=SOURCE_B_ID),
                current_context=moved_to_second,
                committed_context=moved_to_second,
            ),
        ),
        nodes=(),
        resources=(),
        inventory_revision=11,
        published_state_revision=21,
    )
    with pytest.raises(ValueError, match="cannot move between inventory sources"):
        incoming.validate_revision_successor(previous)


def test_current_and_committed_endpoint_identity_may_match_within_source() -> None:
    view = snapshot(nodes=(), resources=())
    assert view.sources[0].current_context.endpoint_id == ENDPOINT_A_ID
    assert view.sources[0].committed_context.endpoint_id == ENDPOINT_A_ID


def test_distinct_sources_accept_distinct_endpoint_identities() -> None:
    view = snapshot(
        sources=(source(), source(source_id=SOURCE_B_ID, name="Second")),
        nodes=(),
        resources=(),
    )
    assert {item.current_context.endpoint_id for item in view.sources} == {
        ENDPOINT_A_ID,
        ENDPOINT_B_ID,
    }


def test_same_source_preserves_endpoint_during_config_and_trust_transition() -> None:
    previous = snapshot(nodes=(), resources=())
    transitioned = controlled_source(
        source_id=SOURCE_ID,
        current=context(revision=4, trust_revision=3),
        committed=context(),
    )
    incoming = snapshot(
        sources=(transitioned,),
        nodes=(),
        resources=(),
        published_state_revision=21,
    )
    incoming.validate_revision_successor(previous)
    assert transitioned.current_context.endpoint_id == ENDPOINT_A_ID


def test_controlled_canonicalization_migration_preserves_endpoint_owner() -> None:
    previous = snapshot(nodes=(), resources=())
    migrated = controlled_source(
        source_id=SOURCE_ID,
        current=context(
            revision=4,
            endpoint_id=ENDPOINT_A_ID,
            locator="https://canonical-v2.example.test:8006",
            canonicalization_version=2,
        ),
        committed=context(),
    )
    incoming = snapshot(
        sources=(migrated,),
        nodes=(),
        resources=(),
        published_state_revision=21,
    )
    incoming.validate_revision_successor(previous)
    assert migrated.current_context.endpoint_id == ENDPOINT_A_ID
