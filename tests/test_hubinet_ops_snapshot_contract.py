from __future__ import annotations

from dataclasses import replace
import importlib.util
from pathlib import Path
import sys
from typing import Any

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


def context(*, revision: int = 3, trust_revision: int = 2) -> SourceContext:
    return SourceContext(
        source_config_revision=revision,
        endpoint_id="7b784024-62d8-4f3e-bb63-af9fe65fcc8e",
        canonical_transport_locator="https://pve.example.test:8006",
        canonicalization_contract_version=1,
        transport_trust_revision=trust_revision,
    )


def source(
    *,
    source_id: str = SOURCE_ID,
    name: str = "Test Proxmox",
    facts: dict[str, Any] | None = None,
) -> InventorySourceSnapshot:
    current_context = context()
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
        active_binding_id=None if terminal else f"binding-{resource_id}",
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
        state_level=ResourceStateLevel.OBSERVED,
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
    snapshot(
        resources=(missing_resource(),),
        inventory_revision=11,
        published_state_revision=21,
    ).validate_revision_successor(previous)
    snapshot(
        resources=(confirmed_removed_resource(),),
        inventory_revision=11,
        published_state_revision=21,
    ).validate_revision_successor(previous)


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


def inventory_change_cases() -> list[tuple[str, HubinetOpsSnapshot, HubinetOpsSnapshot]]:
    base = snapshot(nodes=(node(), node(NODE_B, name="pve-b")))
    return [
        (
            "source-name-and-facts",
            snapshot(resources=()),
            snapshot(
                sources=(source(name="Renamed Source", facts={"cluster": "lab"}),),
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
            "retained-policy",
            base,
            snapshot(
                nodes=base.nodes,
                resources=(resource(retained_policy={"managed": False}),),
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
    inventory_change_cases(),
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_inventory_change_accepts_newer_inventory_revision(
    name: str,
    previous: HubinetOpsSnapshot,
    incoming: HubinetOpsSnapshot,
) -> None:
    del name
    replace(incoming, inventory_revision=11).validate_revision_successor(previous)


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
