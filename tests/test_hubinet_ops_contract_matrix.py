from __future__ import annotations

from pathlib import Path
import sys
from types import ModuleType
from typing import Any

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CUSTOM_COMPONENTS_PATH = REPOSITORY_ROOT / "custom_components"
HUBINET_OPS_PATH = CUSTOM_COMPONENTS_PATH / "hubinet_ops"
for package_name, package_path in (
    ("custom_components", CUSTOM_COMPONENTS_PATH),
    ("custom_components.hubinet_ops", HUBINET_OPS_PATH),
):
    if package_name not in sys.modules:
        package = ModuleType(package_name)
        package.__path__ = [str(package_path)]
        sys.modules[package_name] = package

from custom_components.hubinet_ops.contract import (  # noqa: E402
    BackendInformation,
    DetailStatus,
    HubinetOpsSnapshot,
    InventorySourceSnapshot,
    LifecycleState,
    NodeAvailability,
    NodeSnapshot,
    ObservationalContinuity,
    PresenceState,
    ResourceSnapshot,
    ResourceStateLevel,
    ResourceType,
    SecurityContinuity,
    SourceContext,
    SourceFreshness,
    SourceHealth,
    SourceHealthOrigin,
)


BACKEND_ID = "6a172b5d-d820-4cac-904f-dfb17d42163e"
SOURCE_A_ID = "cfe64f8e-2529-4692-9c23-526479961dbc"
SOURCE_B_ID = "44e24b73-f593-4625-a182-2e2db9541688"
ENDPOINT_A_ID = "7b784024-62d8-4f3e-bb63-af9fe65fcc8e"
ENDPOINT_B_ID = "9e2ef36f-f6db-4e23-93fe-85ad573682f5"
NODE_AVAILABLE_ID = "811d7ea4-470f-42d4-aa06-d2c5c9249a1e"
NODE_UNAVAILABLE_ID = "6f1c0770-6ca6-4c20-ab56-8cb645f63ee3"
RESOURCE_ID = "b50f157b-d2fb-4fff-9497-42c5c239ef49"
SUCCESSOR_ID = "c5321ec5-7259-421a-94ab-195a9c5e5d81"
THIRD_RESOURCE_ID = "727f79b7-9d89-45e5-88da-21adfd94f08a"
BINDING_ID = "16fa5f7e-94c4-4f9a-879b-6221a59f5cd0"
SUCCESSOR_BINDING_ID = "d485a25a-b0f3-4dfa-b7c7-89c73b67b4bc"
THIRD_BINDING_ID = "708f1b4a-e26e-49b8-8b98-8625038182c0"


BASE_RESOURCE: dict[str, Any] = {
    "resource_id": RESOURCE_ID,
    "inventory_source_id": SOURCE_A_ID,
    "active_binding_id": BINDING_ID,
    "resource_type": ResourceType.LXC,
    "vmid": 101,
    "locator_generation": 1,
    "resource_continuity_revision": 1,
    "name": "Matrix resource",
    "status": "running",
    "current_node_id": NODE_AVAILABLE_ID,
    "last_known_node_id": None,
    "presence": PresenceState.PRESENT,
    "lifecycle": LifecycleState.ACTIVE,
    "observational_continuity": ObservationalContinuity.CONSISTENT,
    "security_continuity": SecurityContinuity.UNVERIFIED,
    "detail_status": DetailStatus.OK,
    "node_availability": NodeAvailability.AVAILABLE,
    "state_level": ResourceStateLevel.DISCOVERED,
    "retained_policy": {},
    "effective_policy": {},
    "policy_applicable": False,
    "suspended_reason": None,
    "effective_capabilities": frozenset(),
    "state": {},
    "termination_reason": None,
    "successor_resource_id": None,
}

AMBIGUOUS_PRESENT = {
    "lifecycle": LifecycleState.QUARANTINED,
    "observational_continuity": ObservationalContinuity.UNCERTAIN,
}
AMBIGUOUS_MISSING = {
    **AMBIGUOUS_PRESENT,
    "current_node_id": None,
    "last_known_node_id": NODE_AVAILABLE_ID,
    "presence": PresenceState.MISSING,
    "detail_status": DetailStatus.NOT_APPLICABLE,
    "node_availability": NodeAvailability.NOT_APPLICABLE,
    "status": "unknown",
}
CONFIRMED_REMOVED = {
    "active_binding_id": None,
    "current_node_id": None,
    "last_known_node_id": NODE_AVAILABLE_ID,
    "presence": PresenceState.CONFIRMED_REMOVED,
    "lifecycle": LifecycleState.RETIRED,
    "observational_continuity": ObservationalContinuity.CONSISTENT,
    "detail_status": DetailStatus.NOT_APPLICABLE,
    "node_availability": NodeAvailability.NOT_APPLICABLE,
    "status": "unknown",
    "termination_reason": "confirmed_removed",
}
NOT_CURRENT = {
    "active_binding_id": None,
    "current_node_id": None,
    "last_known_node_id": NODE_AVAILABLE_ID,
    "presence": PresenceState.NOT_CURRENT,
    "lifecycle": LifecycleState.RETIRED,
    "observational_continuity": ObservationalContinuity.REPLACED,
    "detail_status": DetailStatus.NOT_APPLICABLE,
    "node_availability": NodeAvailability.NOT_APPLICABLE,
    "status": "unknown",
    "termination_reason": "replaced",
    "successor_resource_id": SUCCESSOR_ID,
}


def resource(**changes: Any) -> ResourceSnapshot:
    return ResourceSnapshot(**{**BASE_RESOURCE, **changes})


def source(source_id: str = SOURCE_A_ID) -> InventorySourceSnapshot:
    endpoint_id = ENDPOINT_A_ID if source_id == SOURCE_A_ID else ENDPOINT_B_ID
    context = SourceContext(
        source_config_revision=3,
        endpoint_id=endpoint_id,
        canonical_transport_locator=(
            "https://pve-a.example.test:8006"
            if source_id == SOURCE_A_ID
            else "https://pve-b.example.test:8006"
        ),
        canonicalization_contract_version=1,
        transport_trust_revision=2,
    )
    return InventorySourceSnapshot(
        inventory_source_id=source_id,
        name="Matrix source",
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
        last_successful_observed_at="2026-08-12T11:59:30+00:00",
        freshness_reference_at="2026-08-12T11:59:00+00:00",
        freshness_valid_until="2026-08-12T12:04:00+00:00",
        current_context=context,
        committed_context=context,
    )


def node(
    node_id: str = NODE_AVAILABLE_ID,
    *,
    source_id: str = SOURCE_A_ID,
    available: bool = True,
) -> NodeSnapshot:
    return NodeSnapshot(
        node_id=node_id,
        inventory_source_id=source_id,
        name="pve-matrix",
        status="online" if available else "offline",
        available=available,
    )


def snapshot(
    resources: tuple[ResourceSnapshot, ...],
    *,
    sources: tuple[InventorySourceSnapshot, ...] | None = None,
    nodes: tuple[NodeSnapshot, ...] = (),
) -> HubinetOpsSnapshot:
    return HubinetOpsSnapshot(
        backend=BackendInformation(
            backend_instance_id=BACKEND_ID,
            name="Hubinet Ops Matrix",
            version="0.5.0.dev0",
            api_version="0.5-draft",
        ),
        sources=(source(),) if sources is None else sources,
        nodes=nodes,
        resources=resources,
        inventory_revision=10,
        published_state_revision=20,
        published_at="2026-08-12T12:00:00+00:00",
    )


ACCEPTED_RESOURCE_CASES = [
    pytest.param({}, id="normal-current-unverified"),
    pytest.param(
        {"security_continuity": SecurityContinuity.TRUSTED},
        id="normal-current-trusted",
    ),
    pytest.param(AMBIGUOUS_PRESENT, id="ambiguous-current-unverified"),
    pytest.param(
        {
            **AMBIGUOUS_PRESENT,
            "security_continuity": SecurityContinuity.REVOKED,
        },
        id="ambiguous-current-revoked",
    ),
    pytest.param(AMBIGUOUS_MISSING, id="ambiguous-missing-unverified"),
    pytest.param(
        {
            **AMBIGUOUS_MISSING,
            "last_known_node_id": None,
            "security_continuity": SecurityContinuity.REVOKED,
        },
        id="ambiguous-missing-revoked-without-node-history",
    ),
    pytest.param(CONFIRMED_REMOVED, id="removed-consistent-unverified"),
    pytest.param(
        {
            **CONFIRMED_REMOVED,
            "last_known_node_id": None,
            "security_continuity": SecurityContinuity.REVOKED,
        },
        id="removed-consistent-revoked-without-node-history",
    ),
    pytest.param(
        {
            **CONFIRMED_REMOVED,
            "observational_continuity": ObservationalContinuity.UNCERTAIN,
            "last_known_node_id": None,
        },
        id="removed-uncertain-unverified",
    ),
    pytest.param(
        {
            **CONFIRMED_REMOVED,
            "observational_continuity": ObservationalContinuity.UNCERTAIN,
            "security_continuity": SecurityContinuity.REVOKED,
        },
        id="removed-uncertain-revoked",
    ),
    pytest.param(NOT_CURRENT, id="replaced-predecessor-unverified"),
    pytest.param(
        {
            **NOT_CURRENT,
            "last_known_node_id": None,
            "security_continuity": SecurityContinuity.REVOKED,
        },
        id="replaced-predecessor-revoked-without-node-history",
    ),
    pytest.param(
        {"detail_status": DetailStatus.TEMPORARILY_UNAVAILABLE},
        id="present-detail-temporarily-unavailable",
    ),
    pytest.param(
        {"detail_status": DetailStatus.ERROR},
        id="present-detail-error",
    ),
    pytest.param(
        {
            "current_node_id": NODE_UNAVAILABLE_ID,
            "node_availability": NodeAvailability.UNAVAILABLE,
        },
        id="present-resolved-node-unavailable",
    ),
    pytest.param(
        {
            "current_node_id": None,
            "node_availability": NodeAvailability.UNRESOLVED,
        },
        id="present-unresolved-without-node-history",
    ),
    pytest.param(
        {
            "current_node_id": None,
            "last_known_node_id": NODE_AVAILABLE_ID,
            "node_availability": NodeAvailability.UNRESOLVED,
        },
        id="present-unresolved-with-last-known-node",
    ),
    pytest.param(
        {"state_level": ResourceStateLevel.OBSERVED},
        id="neutral-observed-level",
    ),
    pytest.param(
        {"state_level": ResourceStateLevel.MANAGED},
        id="neutral-managed-level",
    ),
    pytest.param(
        {"state_level": ResourceStateLevel.MAINTENANCE},
        id="neutral-maintenance-level",
    ),
    pytest.param(
        {"state_level": ResourceStateLevel.BREAK_GLASS},
        id="neutral-break-glass-level",
    ),
]


@pytest.mark.parametrize("changes", ACCEPTED_RESOURCE_CASES)
def test_canonical_resource_state_matrix_accepts_supported_states(
    changes: dict[str, Any],
) -> None:
    item = resource(**changes)
    assert item.policy_applicable is False
    assert item.effective_policy == {}
    assert item.effective_capabilities == frozenset()


REJECTED_RESOURCE_CASES = [
    pytest.param(
        {"observational_continuity": ObservationalContinuity.UNCERTAIN},
        id="normal-current-cannot-be-uncertain",
    ),
    pytest.param(
        {"security_continuity": SecurityContinuity.REVOKED},
        id="normal-current-cannot-be-revoked",
    ),
    pytest.param(
        {**AMBIGUOUS_PRESENT, "lifecycle": LifecycleState.ACTIVE},
        id="ambiguous-current-cannot-be-active",
    ),
    pytest.param(
        {
            **AMBIGUOUS_PRESENT,
            "security_continuity": SecurityContinuity.TRUSTED,
        },
        id="ambiguous-current-cannot-be-trusted",
    ),
    pytest.param(
        {**AMBIGUOUS_MISSING, "lifecycle": LifecycleState.ACTIVE},
        id="missing-cannot-be-active",
    ),
    pytest.param(
        {
            **AMBIGUOUS_MISSING,
            "observational_continuity": ObservationalContinuity.CONSISTENT,
        },
        id="missing-cannot-be-consistent",
    ),
    pytest.param(
        {
            **AMBIGUOUS_MISSING,
            "security_continuity": SecurityContinuity.TRUSTED,
        },
        id="missing-cannot-be-trusted",
    ),
    pytest.param(
        {**CONFIRMED_REMOVED, "lifecycle": LifecycleState.ACTIVE},
        id="removed-cannot-be-active",
    ),
    pytest.param(
        {
            **CONFIRMED_REMOVED,
            "observational_continuity": ObservationalContinuity.REPLACED,
        },
        id="removed-cannot-use-replaced-continuity",
    ),
    pytest.param(
        {
            **CONFIRMED_REMOVED,
            "security_continuity": SecurityContinuity.TRUSTED,
        },
        id="removed-cannot-remain-trusted",
    ),
    pytest.param(
        {**NOT_CURRENT, "lifecycle": LifecycleState.ACTIVE},
        id="replaced-predecessor-cannot-be-active",
    ),
    pytest.param(
        {
            **NOT_CURRENT,
            "observational_continuity": ObservationalContinuity.CONSISTENT,
        },
        id="not-current-requires-replaced-continuity",
    ),
    pytest.param(
        {**NOT_CURRENT, "security_continuity": SecurityContinuity.TRUSTED},
        id="replaced-predecessor-cannot-remain-trusted",
    ),
    pytest.param(
        {"active_binding_id": None},
        id="present-requires-active-binding",
    ),
    pytest.param(
        {**AMBIGUOUS_MISSING, "active_binding_id": None},
        id="missing-retains-active-binding",
    ),
    pytest.param(
        {**CONFIRMED_REMOVED, "active_binding_id": BINDING_ID},
        id="removed-forbids-active-binding",
    ),
    pytest.param(
        {**NOT_CURRENT, "active_binding_id": BINDING_ID},
        id="not-current-forbids-active-binding",
    ),
    pytest.param(
        {"detail_status": DetailStatus.NOT_APPLICABLE},
        id="present-requires-current-detail-status",
    ),
    pytest.param(
        {**AMBIGUOUS_MISSING, "detail_status": DetailStatus.OK},
        id="missing-detail-is-not-applicable",
    ),
    pytest.param(
        {**CONFIRMED_REMOVED, "detail_status": DetailStatus.ERROR},
        id="removed-detail-is-not-applicable",
    ),
    pytest.param(
        {**NOT_CURRENT, "detail_status": DetailStatus.TEMPORARILY_UNAVAILABLE},
        id="not-current-detail-is-not-applicable",
    ),
    pytest.param(
        {"last_known_node_id": NODE_UNAVAILABLE_ID},
        id="resolved-current-node-forbids-last-known-node",
    ),
    pytest.param(
        {"node_availability": NodeAvailability.UNRESOLVED},
        id="resolved-current-node-cannot-be-unresolved",
    ),
    pytest.param(
        {"current_node_id": None},
        id="unresolved-current-node-must-be-explicit",
    ),
    pytest.param(
        {
            "current_node_id": None,
            "node_availability": NodeAvailability.AVAILABLE,
        },
        id="unresolved-current-node-cannot-be-available",
    ),
    pytest.param(
        {**AMBIGUOUS_MISSING, "current_node_id": NODE_AVAILABLE_ID},
        id="missing-forbids-current-node",
    ),
    pytest.param(
        {
            **CONFIRMED_REMOVED,
            "node_availability": NodeAvailability.UNAVAILABLE,
        },
        id="removed-node-availability-is-not-applicable",
    ),
    pytest.param(
        {**NOT_CURRENT, "current_node_id": NODE_AVAILABLE_ID},
        id="not-current-forbids-current-node",
    ),
    pytest.param(
        {**CONFIRMED_REMOVED, "termination_reason": None},
        id="removed-requires-removal-reason",
    ),
    pytest.param(
        {**CONFIRMED_REMOVED, "termination_reason": "replaced"},
        id="removed-rejects-replacement-reason",
    ),
    pytest.param(
        {**CONFIRMED_REMOVED, "successor_resource_id": SUCCESSOR_ID},
        id="removed-cannot-name-successor",
    ),
    pytest.param(
        {**NOT_CURRENT, "termination_reason": None},
        id="not-current-requires-replacement-reason",
    ),
    pytest.param(
        {**NOT_CURRENT, "termination_reason": "confirmed_removed"},
        id="not-current-rejects-removal-reason",
    ),
    pytest.param(
        {**NOT_CURRENT, "successor_resource_id": None},
        id="not-current-requires-successor",
    ),
    pytest.param(
        {"termination_reason": "replaced"},
        id="nonterminal-forbids-termination-reason",
    ),
    pytest.param(
        {**AMBIGUOUS_MISSING, "successor_resource_id": SUCCESSOR_ID},
        id="nonterminal-forbids-successor-provenance",
    ),
]


@pytest.mark.parametrize("changes", REJECTED_RESOURCE_CASES)
def test_canonical_resource_state_matrix_rejects_invalid_relationships(
    changes: dict[str, Any],
) -> None:
    with pytest.raises(ValueError):
        resource(**changes)


def successor(**changes: Any) -> ResourceSnapshot:
    values = {
        "resource_id": SUCCESSOR_ID,
        "active_binding_id": SUCCESSOR_BINDING_ID,
        "locator_generation": 5,
        "name": "Successor",
    }
    values.update(changes)
    return resource(**values)


def predecessor() -> ResourceSnapshot:
    return resource(
        **{
            **NOT_CURRENT,
            "locator_generation": 4,
            "resource_continuity_revision": 2,
        }
    )


ACCEPTED_SNAPSHOT_CASES = [
    pytest.param(
        (resource(),),
        (node(),),
        id="resolved-node-available-agrees-with-node-record",
    ),
    pytest.param(
        (
            resource(
                current_node_id=NODE_UNAVAILABLE_ID,
                node_availability=NodeAvailability.UNAVAILABLE,
            ),
        ),
        (node(NODE_UNAVAILABLE_ID, available=False),),
        id="resolved-node-unavailable-agrees-with-node-record",
    ),
    pytest.param(
        (
            resource(
                current_node_id=None,
                node_availability=NodeAvailability.UNRESOLVED,
            ),
        ),
        (),
        id="unresolved-node-without-history-needs-no-node-record",
    ),
    pytest.param(
        (
            resource(
                current_node_id=None,
                last_known_node_id=NODE_AVAILABLE_ID,
                node_availability=NodeAvailability.UNRESOLVED,
            ),
        ),
        (node(),),
        id="unresolved-node-retains-last-known-record",
    ),
    pytest.param(
        (resource(**AMBIGUOUS_MISSING),),
        (node(),),
        id="missing-resource-retains-last-known-record",
    ),
    pytest.param(
        (resource(**CONFIRMED_REMOVED),),
        (node(),),
        id="removed-resource-retains-last-known-record",
    ),
    pytest.param(
        (predecessor(), successor()),
        (node(),),
        id="retained-predecessor-points-to-active-successor",
    ),
    pytest.param(
        (predecessor(), successor(**AMBIGUOUS_PRESENT)),
        (node(),),
        id="retained-lineage-allows-quarantined-successor",
    ),
    pytest.param(
        (predecessor(), successor(**AMBIGUOUS_MISSING)),
        (node(),),
        id="retained-lineage-allows-missing-successor",
    ),
    pytest.param(
        (predecessor(), successor(**CONFIRMED_REMOVED)),
        (node(),),
        id="retained-lineage-allows-later-removed-successor",
    ),
    pytest.param(
        (
            predecessor(),
            successor(
                **{
                    **NOT_CURRENT,
                    "successor_resource_id": THIRD_RESOURCE_ID,
                }
            ),
            resource(
                resource_id=THIRD_RESOURCE_ID,
                active_binding_id=THIRD_BINDING_ID,
                locator_generation=6,
                name="Third occupant",
            ),
        ),
        (node(),),
        id="retained-lineage-allows-successor-to-have-successor",
    ),
]


@pytest.mark.parametrize(("resources", "nodes"), ACCEPTED_SNAPSHOT_CASES)
def test_canonical_resource_state_matrix_accepts_same_view_relationships(
    resources: tuple[ResourceSnapshot, ...],
    nodes: tuple[NodeSnapshot, ...],
) -> None:
    view = snapshot(resources, nodes=nodes)
    assert view.resources == resources


REJECTED_SNAPSHOT_CASES = [
    pytest.param(
        (resource(),),
        (),
        None,
        id="current-node-must-exist-in-same-view",
    ),
    pytest.param(
        (resource(**AMBIGUOUS_MISSING),),
        (),
        None,
        id="last-known-node-must-exist-in-same-view",
    ),
    pytest.param(
        (resource(),),
        (node(source_id=SOURCE_B_ID),),
        (source(), source(SOURCE_B_ID)),
        id="node-relation-cannot-cross-sources",
    ),
    pytest.param(
        (resource(),),
        (node(available=False),),
        None,
        id="available-resource-cannot-reference-unavailable-node",
    ),
    pytest.param(
        (resource(node_availability=NodeAvailability.UNAVAILABLE),),
        (node(),),
        None,
        id="unavailable-resource-cannot-reference-available-node",
    ),
    pytest.param(
        (predecessor(),),
        (node(),),
        None,
        id="replacement-successor-must-be-retained",
    ),
    pytest.param(
        (
            resource(
                **{
                    **NOT_CURRENT,
                    "locator_generation": 4,
                    "successor_resource_id": RESOURCE_ID,
                }
            ),
        ),
        (node(),),
        None,
        id="replacement-lineage-cannot-reference-self",
    ),
    pytest.param(
        (
            predecessor(),
            successor(
                inventory_source_id=SOURCE_B_ID,
                current_node_id=None,
                node_availability=NodeAvailability.UNRESOLVED,
            ),
        ),
        (node(),),
        (source(), source(SOURCE_B_ID)),
        id="replacement-successor-must-share-source",
    ),
    pytest.param(
        (predecessor(), successor(vmid=102)),
        (node(),),
        None,
        id="replacement-successor-must-share-locator",
    ),
    pytest.param(
        (predecessor(), successor(locator_generation=6)),
        (node(),),
        None,
        id="replacement-successor-must-use-next-generation",
    ),
]


@pytest.mark.parametrize(
    ("resources", "nodes", "sources"),
    REJECTED_SNAPSHOT_CASES,
)
def test_canonical_resource_state_matrix_rejects_same_view_contradictions(
    resources: tuple[ResourceSnapshot, ...],
    nodes: tuple[NodeSnapshot, ...],
    sources: tuple[InventorySourceSnapshot, ...] | None,
) -> None:
    with pytest.raises(ValueError):
        snapshot(resources, nodes=nodes, sources=sources)
