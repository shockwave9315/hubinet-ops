from __future__ import annotations

from dataclasses import replace
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
FOURTH_RESOURCE_ID = "4f83c2af-84ae-4f54-8a3e-6b98ffb85543"
BINDING_ID = "16fa5f7e-94c4-4f9a-879b-6221a59f5cd0"
SUCCESSOR_BINDING_ID = "d485a25a-b0f3-4dfa-b7c7-89c73b67b4bc"
THIRD_BINDING_ID = "708f1b4a-e26e-49b8-8b98-8625038182c0"
FOURTH_BINDING_ID = "ab077823-a177-4c48-bc72-4c6c001b2e21"


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
    inventory_revision: int = 10,
    published_state_revision: int = 20,
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
        inventory_revision=inventory_revision,
        published_state_revision=published_state_revision,
        published_at="2026-08-12T12:00:00+00:00",
    )


ACCEPTED_RESOURCE_CASES = [
    pytest.param({}, id="normal-current-unverified"),
    pytest.param(
        {"security_continuity": SecurityContinuity.TRUSTED},
        id="normal-current-trusted",
    ),
    pytest.param(
        {
            "security_continuity": SecurityContinuity.TRUSTED,
            "detail_status": DetailStatus.TEMPORARILY_UNAVAILABLE,
        },
        id="normal-current-trusted-detail-temporarily-unavailable",
    ),
    pytest.param(
        {
            "security_continuity": SecurityContinuity.TRUSTED,
            "detail_status": DetailStatus.ERROR,
        },
        id="normal-current-trusted-detail-error",
    ),
    pytest.param(AMBIGUOUS_PRESENT, id="ambiguous-current-unverified"),
    pytest.param(
        {
            **AMBIGUOUS_PRESENT,
            "detail_status": DetailStatus.TEMPORARILY_UNAVAILABLE,
        },
        id="ambiguous-current-unverified-detail-temporarily-unavailable",
    ),
    pytest.param(
        {
            **AMBIGUOUS_PRESENT,
            "detail_status": DetailStatus.ERROR,
        },
        id="ambiguous-current-unverified-detail-error",
    ),
    pytest.param(
        {
            **AMBIGUOUS_PRESENT,
            "security_continuity": SecurityContinuity.REVOKED,
        },
        id="ambiguous-current-revoked",
    ),
    pytest.param(
        {
            **AMBIGUOUS_PRESENT,
            "security_continuity": SecurityContinuity.REVOKED,
            "detail_status": DetailStatus.TEMPORARILY_UNAVAILABLE,
        },
        id="ambiguous-current-revoked-detail-temporarily-unavailable",
    ),
    pytest.param(
        {
            **AMBIGUOUS_PRESENT,
            "security_continuity": SecurityContinuity.REVOKED,
            "detail_status": DetailStatus.ERROR,
        },
        id="ambiguous-current-revoked-detail-error",
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


FAMILY_B_RETAINED_POLICY = {"managed": True}
FAMILY_B_CAPABILITIES = frozenset({"restart"})


FAMILY_B_ACCEPTED_RESOURCE_CASES = [
    pytest.param(
        {
            "security_continuity": SecurityContinuity.TRUSTED,
            "state_level": ResourceStateLevel.MANAGED,
            "retained_policy": FAMILY_B_RETAINED_POLICY,
            "effective_policy": {"managed": True},
            "policy_applicable": True,
            "effective_capabilities": FAMILY_B_CAPABILITIES,
        },
        True,
        FAMILY_B_CAPABILITIES,
        id="policy-accept-managed-trusted-applicable",
    ),
    pytest.param(
        {
            "security_continuity": SecurityContinuity.TRUSTED,
            "state_level": ResourceStateLevel.MAINTENANCE,
            "retained_policy": FAMILY_B_RETAINED_POLICY,
            "effective_policy": {"managed": True},
            "policy_applicable": True,
            "effective_capabilities": FAMILY_B_CAPABILITIES,
        },
        True,
        FAMILY_B_CAPABILITIES,
        id="policy-accept-maintenance-trusted-applicable",
    ),
    pytest.param(
        {
            "security_continuity": SecurityContinuity.TRUSTED,
            "state_level": ResourceStateLevel.MANAGED,
            "retained_policy": FAMILY_B_RETAINED_POLICY,
        },
        False,
        frozenset(),
        id="policy-accept-managed-trusted-not-applicable",
    ),
    pytest.param(
        {
            "security_continuity": SecurityContinuity.TRUSTED,
            "state_level": ResourceStateLevel.MAINTENANCE,
            "retained_policy": FAMILY_B_RETAINED_POLICY,
        },
        False,
        frozenset(),
        id="policy-accept-maintenance-trusted-not-applicable",
    ),
    pytest.param(
        {
            **AMBIGUOUS_PRESENT,
            "retained_policy": FAMILY_B_RETAINED_POLICY,
        },
        False,
        frozenset(),
        id="policy-accept-retained-while-quarantined",
    ),
    pytest.param(
        {
            **AMBIGUOUS_MISSING,
            "retained_policy": FAMILY_B_RETAINED_POLICY,
        },
        False,
        frozenset(),
        id="policy-accept-retained-while-missing",
    ),
    pytest.param(
        {
            **CONFIRMED_REMOVED,
            "retained_policy": FAMILY_B_RETAINED_POLICY,
        },
        False,
        frozenset(),
        id="policy-accept-retained-after-confirmed-removal",
    ),
    pytest.param(
        {
            **NOT_CURRENT,
            "retained_policy": FAMILY_B_RETAINED_POLICY,
        },
        False,
        frozenset(),
        id="policy-accept-retained-on-not-current-predecessor",
    ),
    pytest.param(
        {
            "state_level": ResourceStateLevel.DISCOVERED,
            "retained_policy": FAMILY_B_RETAINED_POLICY,
        },
        False,
        frozenset(),
        id="policy-accept-discovered-retained-but-not-applicable",
    ),
    pytest.param(
        {
            "state_level": ResourceStateLevel.OBSERVED,
            "retained_policy": FAMILY_B_RETAINED_POLICY,
        },
        False,
        frozenset(),
        id="policy-accept-observed-retained-but-not-applicable",
    ),
    pytest.param(
        {
            "state_level": ResourceStateLevel.BREAK_GLASS,
            "retained_policy": FAMILY_B_RETAINED_POLICY,
        },
        False,
        frozenset(),
        id="policy-accept-break-glass-retained-but-not-applicable",
    ),
]


@pytest.mark.parametrize(
    ("changes", "expected_applicable", "expected_capabilities"),
    FAMILY_B_ACCEPTED_RESOURCE_CASES,
)
def test_policy_matrix_accepts_supported_resource_relationships(
    changes: dict[str, Any],
    expected_applicable: bool,
    expected_capabilities: frozenset[str],
) -> None:
    item = resource(**changes)
    assert item.retained_policy == FAMILY_B_RETAINED_POLICY
    assert item.policy_applicable is expected_applicable
    assert item.effective_capabilities == expected_capabilities


FAMILY_B_REJECTED_POLICY_CASES = [
    pytest.param(
        {
            "security_continuity": SecurityContinuity.TRUSTED,
            "state_level": ResourceStateLevel.DISCOVERED,
        },
        id="policy-reject-discovered-applicable",
    ),
    pytest.param(
        {
            "security_continuity": SecurityContinuity.TRUSTED,
            "state_level": ResourceStateLevel.OBSERVED,
        },
        id="policy-reject-observed-applicable",
    ),
    pytest.param(
        {
            "security_continuity": SecurityContinuity.TRUSTED,
            "state_level": ResourceStateLevel.BREAK_GLASS,
        },
        id="policy-reject-break-glass-applicable",
    ),
    pytest.param(
        {"state_level": ResourceStateLevel.MANAGED},
        id="policy-reject-unverified-managed-applicable",
    ),
    pytest.param(
        {"state_level": ResourceStateLevel.MAINTENANCE},
        id="policy-reject-unverified-maintenance-applicable",
    ),
    pytest.param(
        {
            **AMBIGUOUS_PRESENT,
            "state_level": ResourceStateLevel.MANAGED,
        },
        id="policy-reject-quarantined-unverified-applicable",
    ),
    pytest.param(
        {
            **AMBIGUOUS_PRESENT,
            "security_continuity": SecurityContinuity.REVOKED,
            "state_level": ResourceStateLevel.MANAGED,
        },
        id="policy-reject-quarantined-revoked-applicable",
    ),
    pytest.param(
        {
            **AMBIGUOUS_MISSING,
            "state_level": ResourceStateLevel.MANAGED,
        },
        id="policy-reject-missing-applicable",
    ),
    pytest.param(
        {
            **CONFIRMED_REMOVED,
            "state_level": ResourceStateLevel.MANAGED,
        },
        id="policy-reject-confirmed-removed-applicable",
    ),
    pytest.param(
        {
            **NOT_CURRENT,
            "state_level": ResourceStateLevel.MANAGED,
        },
        id="policy-reject-not-current-applicable",
    ),
]


@pytest.mark.parametrize("changes", FAMILY_B_REJECTED_POLICY_CASES)
def test_policy_matrix_rejects_applicability_outside_eligibility_envelope(
    changes: dict[str, Any],
) -> None:
    with pytest.raises(
        ValueError,
        match="managed/maintenance trusted current state",
    ):
        resource(
            **changes,
            retained_policy=FAMILY_B_RETAINED_POLICY,
            policy_applicable=True,
        )


FAMILY_B_REJECTED_CAPABILITY_CASES = [
    pytest.param(
        {
            "security_continuity": SecurityContinuity.TRUSTED,
            "state_level": ResourceStateLevel.MANAGED,
        },
        id="capability-reject-managed-without-applicability",
    ),
    pytest.param(
        {
            "security_continuity": SecurityContinuity.TRUSTED,
            "state_level": ResourceStateLevel.MAINTENANCE,
        },
        id="capability-reject-maintenance-without-applicability",
    ),
    pytest.param(
        {"state_level": ResourceStateLevel.DISCOVERED},
        id="capability-reject-discovered-without-applicability",
    ),
    pytest.param(
        {"state_level": ResourceStateLevel.OBSERVED},
        id="capability-reject-observed-without-applicability",
    ),
    pytest.param(
        AMBIGUOUS_PRESENT,
        id="capability-reject-quarantined-without-applicability",
    ),
]


@pytest.mark.parametrize("changes", FAMILY_B_REJECTED_CAPABILITY_CASES)
def test_capability_matrix_rejects_authority_without_applicability(
    changes: dict[str, Any],
) -> None:
    with pytest.raises(
        ValueError,
        match="effective capabilities require backend-published policy applicability",
    ):
        resource(
            **changes,
            policy_applicable=False,
            effective_capabilities=FAMILY_B_CAPABILITIES,
        )


FAMILY_B_SOURCE_POLICY_CASES = [
    pytest.param(
        ResourceStateLevel.MANAGED,
        id="source-policy-managed-trusted-applicable",
    ),
    pytest.param(
        ResourceStateLevel.MAINTENANCE,
        id="source-policy-maintenance-trusted-applicable",
    ),
]


def time_expiry_source() -> InventorySourceSnapshot:
    return replace(
        source(),
        health=SourceHealth.HEALTHY,
        freshness=SourceFreshness.STALE,
        health_origin=SourceHealthOrigin.TIME_EXPIRY,
        health_reason="freshness_deadline_elapsed",
    )


@pytest.mark.parametrize("state_level", FAMILY_B_SOURCE_POLICY_CASES)
def test_source_policy_matrix_accepts_current_facts(
    state_level: ResourceStateLevel,
) -> None:
    item = resource(
        security_continuity=SecurityContinuity.TRUSTED,
        state_level=state_level,
        retained_policy=FAMILY_B_RETAINED_POLICY,
        policy_applicable=True,
        effective_capabilities=FAMILY_B_CAPABILITIES,
    )
    view = snapshot((item,), nodes=(node(),))
    assert view.sources[0].current_facts_available


@pytest.mark.parametrize("state_level", FAMILY_B_SOURCE_POLICY_CASES)
def test_source_policy_matrix_rejects_stale_current_facts(
    state_level: ResourceStateLevel,
) -> None:
    item = resource(
        security_continuity=SecurityContinuity.TRUSTED,
        state_level=state_level,
        retained_policy=FAMILY_B_RETAINED_POLICY,
        policy_applicable=True,
        effective_capabilities=FAMILY_B_CAPABILITIES,
    )
    stale = time_expiry_source()
    assert not stale.current_facts_available
    with pytest.raises(
        ValueError,
        match="policy/capabilities require a fresh healthy source snapshot",
    ):
        snapshot((item,), sources=(stale,), nodes=(node(),))


def continuity_snapshot(
    changes: dict[str, Any],
    *,
    inventory_revision: int,
    published_state_revision: int,
) -> HubinetOpsSnapshot:
    return snapshot(
        (resource(**changes),),
        nodes=(node(),),
        inventory_revision=inventory_revision,
        published_state_revision=published_state_revision,
    )


FAMILY_C_ACCEPTED_TRANSITION_CASES = [
    pytest.param(
        {"resource_continuity_revision": 2},
        {
            "resource_continuity_revision": 3,
            "security_continuity": SecurityContinuity.TRUSTED,
        },
        11,
        id="continuity-accept-unverified-to-trusted",
    ),
    pytest.param(
        {"resource_continuity_revision": 2},
        {
            **AMBIGUOUS_PRESENT,
            "resource_continuity_revision": 3,
            "security_continuity": SecurityContinuity.REVOKED,
        },
        11,
        id="continuity-accept-unverified-to-revoked-skipped-history",
    ),
    pytest.param(
        {
            **AMBIGUOUS_PRESENT,
            "resource_continuity_revision": 2,
            "security_continuity": SecurityContinuity.REVOKED,
        },
        {
            "resource_continuity_revision": 3,
            "security_continuity": SecurityContinuity.TRUSTED,
        },
        11,
        id="continuity-accept-revoked-to-trusted-resolution",
    ),
    pytest.param(
        {
            "resource_continuity_revision": 2,
            "security_continuity": SecurityContinuity.TRUSTED,
        },
        {
            "resource_continuity_revision": 2,
            "security_continuity": SecurityContinuity.TRUSTED,
        },
        10,
        id="continuity-accept-trusted-stable-without-bump",
    ),
    pytest.param(
        {
            **AMBIGUOUS_PRESENT,
            "resource_continuity_revision": 2,
            "security_continuity": SecurityContinuity.REVOKED,
        },
        {
            **AMBIGUOUS_PRESENT,
            "resource_continuity_revision": 2,
            "security_continuity": SecurityContinuity.REVOKED,
        },
        10,
        id="continuity-accept-revoked-stable-without-bump",
    ),
    pytest.param(
        {"resource_continuity_revision": 2},
        {
            **AMBIGUOUS_PRESENT,
            "resource_continuity_revision": 3,
        },
        11,
        id="continuity-accept-enter-quarantine-with-bump",
    ),
    pytest.param(
        {
            **AMBIGUOUS_PRESENT,
            "resource_continuity_revision": 2,
        },
        {"resource_continuity_revision": 3},
        11,
        id="continuity-accept-leave-quarantine-with-bump",
    ),
    pytest.param(
        {
            "resource_continuity_revision": 2,
            "security_continuity": SecurityContinuity.TRUSTED,
            "state_level": ResourceStateLevel.OBSERVED,
        },
        {
            "resource_continuity_revision": 3,
            "security_continuity": SecurityContinuity.TRUSTED,
            "state_level": ResourceStateLevel.MANAGED,
        },
        11,
        id="continuity-accept-observed-to-managed-with-bump",
    ),
    pytest.param(
        {
            "resource_continuity_revision": 2,
            "security_continuity": SecurityContinuity.TRUSTED,
            "state_level": ResourceStateLevel.OBSERVED,
        },
        {
            "resource_continuity_revision": 7,
            "security_continuity": SecurityContinuity.TRUSTED,
            "state_level": ResourceStateLevel.MANAGED,
        },
        11,
        id="continuity-accept-non-adjacent-revision-jump",
    ),
    pytest.param(
        {
            "resource_continuity_revision": 2,
            "security_continuity": SecurityContinuity.TRUSTED,
        },
        {
            **AMBIGUOUS_PRESENT,
            "resource_continuity_revision": 3,
            "security_continuity": SecurityContinuity.REVOKED,
        },
        11,
        id="continuity-accept-trusted-to-revoked",
    ),
    pytest.param(
        {"resource_continuity_revision": 2},
        {"resource_continuity_revision": 7},
        11,
        id="continuity-accept-newer-revision-with-hidden-transitions",
    ),
]


@pytest.mark.parametrize(
    ("previous_changes", "current_changes", "current_inventory_revision"),
    FAMILY_C_ACCEPTED_TRANSITION_CASES,
)
def test_continuity_revision_matrix_accepts_observable_transitions(
    previous_changes: dict[str, Any],
    current_changes: dict[str, Any],
    current_inventory_revision: int,
) -> None:
    previous = continuity_snapshot(
        previous_changes,
        inventory_revision=10,
        published_state_revision=20,
    )
    current = continuity_snapshot(
        current_changes,
        inventory_revision=current_inventory_revision,
        published_state_revision=21,
    )
    current.validate_revision_successor(previous)
    assert (
        current.source_reconciliation_projection
        == previous.source_reconciliation_projection
    )


FAMILY_C_REJECTED_UNBUMPED_TRANSITION_CASES = [
    pytest.param(
        {"resource_continuity_revision": 2},
        {
            "resource_continuity_revision": 2,
            "security_continuity": SecurityContinuity.TRUSTED,
        },
        id="continuity-reject-unverified-to-trusted-without-bump",
    ),
    pytest.param(
        {"resource_continuity_revision": 2},
        {
            **AMBIGUOUS_PRESENT,
            "resource_continuity_revision": 2,
        },
        id="continuity-reject-enter-quarantine-without-bump",
    ),
    pytest.param(
        {
            **AMBIGUOUS_PRESENT,
            "resource_continuity_revision": 2,
        },
        {"resource_continuity_revision": 2},
        id="continuity-reject-leave-quarantine-without-bump",
    ),
    pytest.param(
        {
            "resource_continuity_revision": 2,
            "security_continuity": SecurityContinuity.TRUSTED,
            "state_level": ResourceStateLevel.OBSERVED,
        },
        {
            "resource_continuity_revision": 2,
            "security_continuity": SecurityContinuity.TRUSTED,
            "state_level": ResourceStateLevel.MANAGED,
        },
        id="continuity-reject-observed-to-managed-without-bump",
    ),
    pytest.param(
        {"resource_continuity_revision": 2},
        {
            **AMBIGUOUS_PRESENT,
            "resource_continuity_revision": 2,
            "security_continuity": SecurityContinuity.REVOKED,
        },
        id="continuity-reject-unverified-to-revoked-without-bump",
    ),
]


@pytest.mark.parametrize(
    ("previous_changes", "current_changes"),
    FAMILY_C_REJECTED_UNBUMPED_TRANSITION_CASES,
)
def test_continuity_revision_matrix_rejects_security_change_without_bump(
    previous_changes: dict[str, Any],
    current_changes: dict[str, Any],
) -> None:
    previous = continuity_snapshot(
        previous_changes,
        inventory_revision=10,
        published_state_revision=20,
    )
    current = continuity_snapshot(
        current_changes,
        inventory_revision=11,
        published_state_revision=21,
    )
    with pytest.raises(
        ValueError,
        match="security-relevant resource transition requires a newer "
        "resource_continuity_revision",
    ):
        current.validate_revision_successor(previous)


def test_continuity_revision_matrix_rejects_revision_regression() -> None:
    previous = continuity_snapshot(
        {"resource_continuity_revision": 5},
        inventory_revision=10,
        published_state_revision=20,
    )
    current = continuity_snapshot(
        {"resource_continuity_revision": 4},
        inventory_revision=11,
        published_state_revision=21,
    )
    with pytest.raises(
        ValueError,
        match="resource_continuity_revision must not regress",
    ):
        current.validate_revision_successor(previous)


FAMILY_C_REJECTED_SECURITY_HISTORY_CASES = [
    pytest.param(
        {
            "resource_continuity_revision": 2,
            "security_continuity": SecurityContinuity.TRUSTED,
        },
        {"resource_continuity_revision": 3},
        id="continuity-reject-trusted-to-unverified",
    ),
    pytest.param(
        {
            **AMBIGUOUS_PRESENT,
            "resource_continuity_revision": 2,
            "security_continuity": SecurityContinuity.REVOKED,
        },
        {
            **AMBIGUOUS_PRESENT,
            "resource_continuity_revision": 3,
        },
        id="continuity-reject-revoked-to-unverified",
    ),
]


@pytest.mark.parametrize(
    ("previous_changes", "current_changes"),
    FAMILY_C_REJECTED_SECURITY_HISTORY_CASES,
)
def test_continuity_revision_matrix_rejects_security_history_erasure(
    previous_changes: dict[str, Any],
    current_changes: dict[str, Any],
) -> None:
    previous = continuity_snapshot(
        previous_changes,
        inventory_revision=10,
        published_state_revision=20,
    )
    current = continuity_snapshot(
        current_changes,
        inventory_revision=11,
        published_state_revision=21,
    )
    with pytest.raises(
        ValueError,
        match="resource cannot erase known security history",
    ):
        current.validate_revision_successor(previous)


def test_continuity_revision_matrix_rejects_terminal_reopening() -> None:
    previous = continuity_snapshot(
        {
            **CONFIRMED_REMOVED,
            "resource_continuity_revision": 2,
        },
        inventory_revision=10,
        published_state_revision=20,
    )
    current = continuity_snapshot(
        {"resource_continuity_revision": 3},
        inventory_revision=11,
        published_state_revision=21,
    )
    with pytest.raises(
        ValueError,
        match="terminal resource cannot be reopened or reclassified",
    ):
        current.validate_revision_successor(previous)


FAMILY_D_AUDIT_ONLY_OUTCOME = "audit_only_inapplicable"
FAMILY_D_FAILURE_OUTCOME = "source_unavailable"
FAMILY_D_OTHER_FAILURE_OUTCOME = "audit_or_failure"

FAMILY_D_INITIAL_SOURCE_CHANGES: dict[str, Any] = {
    "health": SourceHealth.NOT_YET_OBSERVED,
    "freshness": SourceFreshness.NOT_YET_OBSERVED,
    "health_origin": SourceHealthOrigin.INITIAL,
    "health_reason": "",
    "last_issued_run_sequence": 0,
    "latest_completed_run_sequence": None,
    "latest_completed_outcome": None,
    "last_health_run_sequence": None,
    "last_run_health_outcome": None,
    "last_committed_run_sequence": None,
    "last_successful_observed_at": None,
    "freshness_reference_at": None,
    "freshness_valid_until": None,
    "committed_context": None,
}

FAMILY_D_DEGRADED_SOURCE_CHANGES: dict[str, Any] = {
    "health": SourceHealth.SOURCE_UNAVAILABLE,
    "freshness": SourceFreshness.STALE,
    "health_origin": SourceHealthOrigin.DISCOVERY_RUN,
    "health_reason": "active_endpoint_timeout",
}


def source_with_run_provenance(**changes: Any) -> InventorySourceSnapshot:
    return replace(source(), **changes)


def initial_source_with_run_provenance(
    **changes: Any,
) -> InventorySourceSnapshot:
    return source_with_run_provenance(
        **{**FAMILY_D_INITIAL_SOURCE_CHANGES, **changes}
    )


def successful_source_run(
    sequence: int,
    **changes: Any,
) -> InventorySourceSnapshot:
    successful_changes = {
        "last_issued_run_sequence": sequence,
        "latest_completed_run_sequence": sequence,
        "latest_completed_outcome": "success",
        "last_health_run_sequence": sequence,
        "last_run_health_outcome": "success",
        "last_committed_run_sequence": sequence,
        "last_successful_observed_at": "2026-08-12T12:01:30+00:00",
        "freshness_reference_at": "2026-08-12T12:01:00+00:00",
        "freshness_valid_until": "2026-08-12T12:06:00+00:00",
        "committed_context": source().current_context,
    }
    return source_with_run_provenance(**{**successful_changes, **changes})


def source_only_snapshot(
    item: InventorySourceSnapshot,
    *,
    inventory_revision: int,
    published_state_revision: int,
) -> HubinetOpsSnapshot:
    return snapshot(
        (),
        sources=(item,),
        nodes=(),
        inventory_revision=inventory_revision,
        published_state_revision=published_state_revision,
    )


FAMILY_D_ACCEPTED_SINGLE_VIEW_CASES = [
    pytest.param(
        {},
        id="source-run-accept-canonical-successful-commit",
    ),
    pytest.param(
        {"last_issued_run_sequence": 6},
        id="source-run-accept-newer-issued-without-completion",
    ),
    pytest.param(
        {
            "last_issued_run_sequence": 6,
            "latest_completed_run_sequence": 6,
            "latest_completed_outcome": FAMILY_D_AUDIT_ONLY_OUTCOME,
        },
        id="source-run-accept-newer-non-success-completion",
    ),
    pytest.param(
        {
            **FAMILY_D_DEGRADED_SOURCE_CHANGES,
            "last_issued_run_sequence": 9,
            "latest_completed_run_sequence": 6,
            "latest_completed_outcome": FAMILY_D_AUDIT_ONLY_OUTCOME,
            "last_health_run_sequence": 4,
            "last_run_health_outcome": FAMILY_D_FAILURE_OUTCOME,
            "last_committed_run_sequence": 3,
        },
        id="source-run-accept-ordered-non-adjacent-gaps",
    ),
    pytest.param(
        {
            **FAMILY_D_INITIAL_SOURCE_CHANGES,
            "last_issued_run_sequence": 3,
        },
        id="source-run-accept-initial-issued-only",
    ),
    pytest.param(
        {
            **FAMILY_D_INITIAL_SOURCE_CHANGES,
            "last_issued_run_sequence": 5,
            "latest_completed_run_sequence": 4,
            "latest_completed_outcome": FAMILY_D_AUDIT_ONLY_OUTCOME,
        },
        id="source-run-accept-initial-completion-only",
    ),
]


@pytest.mark.parametrize("changes", FAMILY_D_ACCEPTED_SINGLE_VIEW_CASES)
def test_source_run_matrix_accepts_supported_single_view_provenance(
    changes: dict[str, Any],
) -> None:
    item = source_with_run_provenance(**changes)
    assert item.last_issued_run_sequence == changes.get(
        "last_issued_run_sequence", 5
    )


FAMILY_D_REJECTED_LATTICE_CASES = [
    pytest.param(
        {
            "last_issued_run_sequence": 6,
            "last_health_run_sequence": 5,
            "last_committed_run_sequence": 6,
        },
        id="source-run-reject-committed-after-health",
    ),
    pytest.param(
        {
            **FAMILY_D_DEGRADED_SOURCE_CHANGES,
            "last_issued_run_sequence": 6,
            "last_health_run_sequence": 6,
            "last_run_health_outcome": FAMILY_D_FAILURE_OUTCOME,
        },
        id="source-run-reject-health-after-completion",
    ),
    pytest.param(
        {
            "latest_completed_run_sequence": 6,
            "latest_completed_outcome": FAMILY_D_AUDIT_ONLY_OUTCOME,
        },
        id="source-run-reject-completion-after-issued",
    ),
    pytest.param(
        {
            "last_health_run_sequence": None,
            "last_run_health_outcome": None,
        },
        id="source-run-reject-committed-without-health",
    ),
    pytest.param(
        {
            "latest_completed_run_sequence": None,
            "latest_completed_outcome": None,
        },
        id="source-run-reject-health-without-completion",
    ),
]


@pytest.mark.parametrize("changes", FAMILY_D_REJECTED_LATTICE_CASES)
def test_source_run_matrix_rejects_sequence_lattice_violations(
    changes: dict[str, Any],
) -> None:
    with pytest.raises(ValueError, match="source run provenance must satisfy"):
        source_with_run_provenance(**changes)


FAMILY_D_REJECTED_SEQUENCE_OUTCOME_PAIR_CASES = [
    pytest.param(
        {
            "last_issued_run_sequence": 6,
            "latest_completed_run_sequence": 6,
            "latest_completed_outcome": None,
        },
        "latest completed run sequence and outcome must be published together",
        id="source-run-reject-completed-sequence-without-outcome",
    ),
    pytest.param(
        {
            "latest_completed_run_sequence": None,
            "latest_completed_outcome": FAMILY_D_AUDIT_ONLY_OUTCOME,
        },
        "latest completed run sequence and outcome must be published together",
        id="source-run-reject-completed-outcome-without-sequence",
    ),
    pytest.param(
        {
            **FAMILY_D_DEGRADED_SOURCE_CHANGES,
            "last_issued_run_sequence": 6,
            "latest_completed_run_sequence": 6,
            "latest_completed_outcome": FAMILY_D_AUDIT_ONLY_OUTCOME,
            "last_health_run_sequence": 6,
            "last_run_health_outcome": None,
        },
        "last health run sequence and outcome must be published together",
        id="source-run-reject-health-sequence-without-outcome",
    ),
    pytest.param(
        {
            "last_health_run_sequence": None,
            "last_run_health_outcome": FAMILY_D_FAILURE_OUTCOME,
        },
        "last health run sequence and outcome must be published together",
        id="source-run-reject-health-outcome-without-sequence",
    ),
]


@pytest.mark.parametrize(
    ("changes", "message"),
    FAMILY_D_REJECTED_SEQUENCE_OUTCOME_PAIR_CASES,
)
def test_source_run_matrix_rejects_unpaired_sequences_and_outcomes(
    changes: dict[str, Any],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        source_with_run_provenance(**changes)


FAMILY_D_REJECTED_SUCCESS_OUTCOME_CASES = [
    pytest.param(
        {
            **FAMILY_D_DEGRADED_SOURCE_CHANGES,
            "last_issued_run_sequence": 6,
            "latest_completed_run_sequence": 6,
            "latest_completed_outcome": FAMILY_D_AUDIT_ONLY_OUTCOME,
            "last_health_run_sequence": 6,
            "last_run_health_outcome": "success",
        },
        "successful applied health requires the exact committed run sequence",
        id="source-run-reject-successful-health-not-committed",
    ),
    pytest.param(
        {
            "last_issued_run_sequence": 6,
            "latest_completed_run_sequence": 6,
            "latest_completed_outcome": "success",
        },
        "successful completion requires the exact committed run sequence",
        id="source-run-reject-successful-completion-not-committed",
    ),
    pytest.param(
        {
            **FAMILY_D_DEGRADED_SOURCE_CHANGES,
            "last_issued_run_sequence": 6,
            "latest_completed_run_sequence": 6,
            "latest_completed_outcome": FAMILY_D_AUDIT_ONLY_OUTCOME,
            "last_run_health_outcome": FAMILY_D_FAILURE_OUTCOME,
        },
        "the committed run must retain a successful health outcome",
        id="source-run-reject-committed-health-non-success",
    ),
    pytest.param(
        {"latest_completed_outcome": FAMILY_D_AUDIT_ONLY_OUTCOME},
        "the committed run must retain a successful completion outcome",
        id="source-run-reject-committed-completion-non-success",
    ),
]


@pytest.mark.parametrize(
    ("changes", "message"),
    FAMILY_D_REJECTED_SUCCESS_OUTCOME_CASES,
)
def test_source_run_matrix_rejects_incoherent_success_outcomes(
    changes: dict[str, Any],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        source_with_run_provenance(**changes)


FAMILY_D_COMMITTED_BUNDLE_FIELDS = [
    pytest.param(
        "last_successful_observed_at",
        id="source-run-bundle-last-successful-observed-at",
    ),
    pytest.param(
        "freshness_reference_at",
        id="source-run-bundle-freshness-reference-at",
    ),
    pytest.param(
        "freshness_valid_until",
        id="source-run-bundle-freshness-valid-until",
    ),
    pytest.param(
        "committed_context",
        id="source-run-bundle-committed-context",
    ),
]


@pytest.mark.parametrize("field_name", FAMILY_D_COMMITTED_BUNDLE_FIELDS)
def test_source_run_matrix_rejects_incomplete_committed_provenance_bundle(
    field_name: str,
) -> None:
    with pytest.raises(
        ValueError,
        match="committed run, fixed freshness facts, and committed context "
        "must be published together",
    ):
        source_with_run_provenance(**{field_name: None})


@pytest.mark.parametrize("field_name", FAMILY_D_COMMITTED_BUNDLE_FIELDS)
def test_source_run_matrix_rejects_provenance_without_committed_run(
    field_name: str,
) -> None:
    retained_value = getattr(source(), field_name)
    with pytest.raises(
        ValueError,
        match="committed run, fixed freshness facts, and committed context "
        "must be published together",
    ):
        initial_source_with_run_provenance(**{field_name: retained_value})


FAMILY_D_REJECTED_MONOTONICITY_CASES = [
    pytest.param(
        source_with_run_provenance(
            last_issued_run_sequence=9,
            latest_completed_run_sequence=6,
            latest_completed_outcome=FAMILY_D_AUDIT_ONLY_OUTCOME,
        ),
        source_with_run_provenance(
            last_issued_run_sequence=8,
            latest_completed_run_sequence=6,
            latest_completed_outcome=FAMILY_D_AUDIT_ONLY_OUTCOME,
        ),
        "last_issued_run_sequence must not regress",
        id="source-run-reject-issued-regression",
    ),
    pytest.param(
        source_with_run_provenance(
            last_issued_run_sequence=9,
            latest_completed_run_sequence=8,
            latest_completed_outcome=FAMILY_D_AUDIT_ONLY_OUTCOME,
        ),
        source_with_run_provenance(
            last_issued_run_sequence=9,
            latest_completed_run_sequence=7,
            latest_completed_outcome=FAMILY_D_AUDIT_ONLY_OUTCOME,
        ),
        "latest_completed_run_sequence must not regress",
        id="source-run-reject-completed-regression",
    ),
    pytest.param(
        source_with_run_provenance(
            **FAMILY_D_DEGRADED_SOURCE_CHANGES,
            last_issued_run_sequence=9,
            latest_completed_run_sequence=8,
            latest_completed_outcome=FAMILY_D_AUDIT_ONLY_OUTCOME,
            last_health_run_sequence=7,
            last_run_health_outcome=FAMILY_D_FAILURE_OUTCOME,
        ),
        source_with_run_provenance(
            **FAMILY_D_DEGRADED_SOURCE_CHANGES,
            last_issued_run_sequence=9,
            latest_completed_run_sequence=8,
            latest_completed_outcome=FAMILY_D_AUDIT_ONLY_OUTCOME,
            last_health_run_sequence=6,
            last_run_health_outcome=FAMILY_D_FAILURE_OUTCOME,
        ),
        "last_health_run_sequence must not regress",
        id="source-run-reject-health-regression",
    ),
    pytest.param(
        source_with_run_provenance(
            **FAMILY_D_DEGRADED_SOURCE_CHANGES,
            last_issued_run_sequence=9,
            latest_completed_run_sequence=8,
            latest_completed_outcome=FAMILY_D_AUDIT_ONLY_OUTCOME,
            last_health_run_sequence=8,
            last_run_health_outcome=FAMILY_D_FAILURE_OUTCOME,
            last_committed_run_sequence=7,
        ),
        source_with_run_provenance(
            **FAMILY_D_DEGRADED_SOURCE_CHANGES,
            last_issued_run_sequence=9,
            latest_completed_run_sequence=8,
            latest_completed_outcome=FAMILY_D_AUDIT_ONLY_OUTCOME,
            last_health_run_sequence=8,
            last_run_health_outcome=FAMILY_D_FAILURE_OUTCOME,
            last_committed_run_sequence=6,
        ),
        "last_committed_run_sequence must not regress",
        id="source-run-reject-committed-regression",
    ),
    pytest.param(
        source_with_run_provenance(
            last_issued_run_sequence=6,
            latest_completed_run_sequence=6,
            latest_completed_outcome=FAMILY_D_AUDIT_ONLY_OUTCOME,
        ),
        initial_source_with_run_provenance(last_issued_run_sequence=6),
        "latest_completed_run_sequence cannot be cleared",
        id="source-run-reject-clear-completed",
    ),
    pytest.param(
        source_with_run_provenance(
            last_issued_run_sequence=6,
            latest_completed_run_sequence=6,
            latest_completed_outcome=FAMILY_D_AUDIT_ONLY_OUTCOME,
        ),
        initial_source_with_run_provenance(
            last_issued_run_sequence=6,
            latest_completed_run_sequence=6,
            latest_completed_outcome=FAMILY_D_AUDIT_ONLY_OUTCOME,
        ),
        "last_health_run_sequence cannot be cleared",
        id="source-run-reject-clear-health",
    ),
    pytest.param(
        source(),
        source_with_run_provenance(
            **FAMILY_D_DEGRADED_SOURCE_CHANGES,
            last_issued_run_sequence=6,
            latest_completed_run_sequence=6,
            latest_completed_outcome=FAMILY_D_AUDIT_ONLY_OUTCOME,
            last_health_run_sequence=6,
            last_run_health_outcome=FAMILY_D_FAILURE_OUTCOME,
            last_committed_run_sequence=None,
            last_successful_observed_at=None,
            freshness_reference_at=None,
            freshness_valid_until=None,
            committed_context=None,
        ),
        "last_committed_run_sequence cannot be cleared",
        id="source-run-reject-clear-committed",
    ),
]


@pytest.mark.parametrize(
    ("previous_source", "current_source", "message"),
    FAMILY_D_REJECTED_MONOTONICITY_CASES,
)
def test_source_run_matrix_rejects_sequence_regression_or_clearing(
    previous_source: InventorySourceSnapshot,
    current_source: InventorySourceSnapshot,
    message: str,
) -> None:
    previous = source_only_snapshot(
        previous_source,
        inventory_revision=10,
        published_state_revision=20,
    )
    current = source_only_snapshot(
        current_source,
        inventory_revision=10,
        published_state_revision=21,
    )
    with pytest.raises(ValueError, match=message):
        current.validate_revision_successor(previous)


FAMILY_D_ACCEPTED_TRANSITION_CASES = [
    pytest.param(
        source(),
        source_with_run_provenance(last_issued_run_sequence=9),
        10,
        id="source-run-accept-issued-gap-five-to-nine",
    ),
    pytest.param(
        source(),
        source_with_run_provenance(
            last_issued_run_sequence=9,
            latest_completed_run_sequence=8,
            latest_completed_outcome=FAMILY_D_AUDIT_ONLY_OUTCOME,
        ),
        10,
        id="source-run-accept-completed-gap-five-to-eight",
    ),
    pytest.param(
        source(),
        successful_source_run(9),
        11,
        id="source-run-accept-successful-commit-gap-five-to-nine",
    ),
    pytest.param(
        source_with_run_provenance(
            last_issued_run_sequence=7,
            latest_completed_run_sequence=7,
            latest_completed_outcome=FAMILY_D_AUDIT_ONLY_OUTCOME,
        ),
        successful_source_run(8),
        11,
        id="source-run-accept-new-commit-postdates-finalized-seven",
    ),
]


@pytest.mark.parametrize(
    ("previous_source", "current_source", "current_inventory_revision"),
    FAMILY_D_ACCEPTED_TRANSITION_CASES,
)
def test_source_run_matrix_accepts_non_adjacent_sequence_progression(
    previous_source: InventorySourceSnapshot,
    current_source: InventorySourceSnapshot,
    current_inventory_revision: int,
) -> None:
    previous = source_only_snapshot(
        previous_source,
        inventory_revision=10,
        published_state_revision=20,
    )
    current = source_only_snapshot(
        current_source,
        inventory_revision=current_inventory_revision,
        published_state_revision=21,
    )
    current.validate_revision_successor(previous)


def test_source_run_matrix_rejects_commit_of_previously_finalized_run() -> None:
    previous_source = source_with_run_provenance(
        last_issued_run_sequence=7,
        latest_completed_run_sequence=7,
        latest_completed_outcome=FAMILY_D_AUDIT_ONLY_OUTCOME,
    )
    current_source = successful_source_run(7)
    previous = source_only_snapshot(
        previous_source,
        inventory_revision=10,
        published_state_revision=20,
    )
    current = source_only_snapshot(
        current_source,
        inventory_revision=11,
        published_state_revision=21,
    )
    with pytest.raises(
        ValueError,
        match="a new commit must postdate every previously finalized run",
    ):
        current.validate_revision_successor(previous)


def test_source_run_matrix_rejects_commit_without_inventory_revision() -> None:
    previous = source_only_snapshot(
        source(),
        inventory_revision=10,
        published_state_revision=20,
    )
    current = source_only_snapshot(
        successful_source_run(9),
        inventory_revision=10,
        published_state_revision=21,
    )
    with pytest.raises(
        ValueError,
        match="successful inventory commit requires a newer inventory_revision",
    ):
        current.validate_revision_successor(previous)


FAMILY_D_REJECTED_OUTCOME_REWRITE_CASES = [
    pytest.param(
        source_with_run_provenance(
            last_issued_run_sequence=6,
            latest_completed_run_sequence=6,
            latest_completed_outcome=FAMILY_D_AUDIT_ONLY_OUTCOME,
        ),
        source_with_run_provenance(
            last_issued_run_sequence=6,
            latest_completed_run_sequence=6,
            latest_completed_outcome=FAMILY_D_OTHER_FAILURE_OUTCOME,
        ),
        "latest completed outcome is immutable for its run sequence",
        id="source-run-reject-rewrite-completed-outcome",
    ),
    pytest.param(
        source_with_run_provenance(
            **FAMILY_D_DEGRADED_SOURCE_CHANGES,
            last_issued_run_sequence=6,
            latest_completed_run_sequence=6,
            latest_completed_outcome=FAMILY_D_AUDIT_ONLY_OUTCOME,
            last_health_run_sequence=6,
            last_run_health_outcome=FAMILY_D_FAILURE_OUTCOME,
        ),
        source_with_run_provenance(
            **FAMILY_D_DEGRADED_SOURCE_CHANGES,
            last_issued_run_sequence=6,
            latest_completed_run_sequence=6,
            latest_completed_outcome=FAMILY_D_AUDIT_ONLY_OUTCOME,
            last_health_run_sequence=6,
            last_run_health_outcome=FAMILY_D_OTHER_FAILURE_OUTCOME,
        ),
        "last run health outcome is immutable for its run sequence",
        id="source-run-reject-rewrite-health-outcome",
    ),
]


@pytest.mark.parametrize(
    ("previous_source", "current_source", "message"),
    FAMILY_D_REJECTED_OUTCOME_REWRITE_CASES,
)
def test_source_run_matrix_rejects_finalized_outcome_rewrite(
    previous_source: InventorySourceSnapshot,
    current_source: InventorySourceSnapshot,
    message: str,
) -> None:
    previous = source_only_snapshot(
        previous_source,
        inventory_revision=10,
        published_state_revision=20,
    )
    current = source_only_snapshot(
        current_source,
        inventory_revision=10,
        published_state_revision=21,
    )
    with pytest.raises(ValueError, match=message):
        current.validate_revision_successor(previous)


def test_source_run_matrix_rejects_successful_commit_provenance_rewrite() -> None:
    previous = source_only_snapshot(
        source(),
        inventory_revision=10,
        published_state_revision=20,
    )
    current = source_only_snapshot(
        source_with_run_provenance(
            last_successful_observed_at="2026-08-12T12:00:30+00:00"
        ),
        inventory_revision=10,
        published_state_revision=21,
    )
    with pytest.raises(
        ValueError,
        match="successful commit provenance is immutable for its run sequence",
    ):
        current.validate_revision_successor(previous)


def test_source_run_matrix_accepts_reconciliation_change_with_new_commit() -> None:
    previous = source_only_snapshot(
        source_with_run_provenance(facts={"cluster": "alpha"}),
        inventory_revision=10,
        published_state_revision=20,
    )
    current = source_only_snapshot(
        successful_source_run(9, facts={"cluster": "beta"}),
        inventory_revision=11,
        published_state_revision=21,
    )
    current.validate_revision_successor(previous)


def test_source_run_matrix_rejects_reconciliation_change_without_new_commit(
) -> None:
    previous = source_only_snapshot(
        source_with_run_provenance(facts={"cluster": "alpha"}),
        inventory_revision=10,
        published_state_revision=20,
    )
    current = source_only_snapshot(
        source_with_run_provenance(facts={"cluster": "beta"}),
        inventory_revision=11,
        published_state_revision=21,
    )
    with pytest.raises(
        ValueError,
        match="discovery/reconciliation-owned source inventory changes require "
        "a newer last_committed_run_sequence",
    ):
        current.validate_revision_successor(previous)


FAMILY_E_SUCCESSOR_POLICY = {"owner": "successor"}
FAMILY_E_SUCCESSOR_CAPABILITIES = frozenset({"restart"})


def polling_gap_previous_snapshot() -> HubinetOpsSnapshot:
    return snapshot(
        (
            resource(
                locator_generation=4,
                resource_continuity_revision=1,
            ),
        ),
        nodes=(node(),),
        inventory_revision=10,
        published_state_revision=20,
    )


def polling_gap_current_snapshot(
    resources: tuple[ResourceSnapshot, ...],
    *,
    commit_sequence: int = 12,
    inventory_revision: int = 16,
    published_state_revision: int = 47,
) -> HubinetOpsSnapshot:
    return snapshot(
        resources,
        sources=(successful_source_run(commit_sequence),),
        nodes=(node(),),
        inventory_revision=inventory_revision,
        published_state_revision=published_state_revision,
    )


FAMILY_E_SUCCESSOR_EVOLUTION_ACCEPT_CASES = [
    pytest.param(
        {},
        {
            "resource_continuity_revision": 5,
            "security_continuity": SecurityContinuity.TRUSTED,
        },
        PresenceState.PRESENT,
        LifecycleState.ACTIVE,
        SecurityContinuity.TRUSTED,
        False,
        frozenset(),
        id="polling-gap-accept-trusted-successor",
    ),
    pytest.param(
        {},
        {
            "resource_continuity_revision": 6,
            "security_continuity": SecurityContinuity.TRUSTED,
            "state_level": ResourceStateLevel.MANAGED,
            "retained_policy": FAMILY_E_SUCCESSOR_POLICY,
            "effective_policy": FAMILY_E_SUCCESSOR_POLICY,
            "policy_applicable": True,
            "effective_capabilities": FAMILY_E_SUCCESSOR_CAPABILITIES,
        },
        PresenceState.PRESENT,
        LifecycleState.ACTIVE,
        SecurityContinuity.TRUSTED,
        True,
        FAMILY_E_SUCCESSOR_CAPABILITIES,
        id="polling-gap-accept-managed-policy-successor",
    ),
    pytest.param(
        {},
        {
            **AMBIGUOUS_PRESENT,
            "resource_continuity_revision": 5,
        },
        PresenceState.PRESENT,
        LifecycleState.QUARANTINED,
        SecurityContinuity.UNVERIFIED,
        False,
        frozenset(),
        id="polling-gap-accept-quarantined-successor",
    ),
    pytest.param(
        {},
        {
            **CONFIRMED_REMOVED,
            "resource_continuity_revision": 5,
        },
        PresenceState.CONFIRMED_REMOVED,
        LifecycleState.RETIRED,
        SecurityContinuity.UNVERIFIED,
        False,
        frozenset(),
        id="polling-gap-accept-removed-successor",
    ),
    pytest.param(
        {
            "resource_continuity_revision": 7,
            "security_continuity": SecurityContinuity.REVOKED,
        },
        {"resource_continuity_revision": 5},
        PresenceState.PRESENT,
        LifecycleState.ACTIVE,
        SecurityContinuity.UNVERIFIED,
        False,
        frozenset(),
        id="polling-gap-accept-skipped-predecessor-security-path",
    ),
]


@pytest.mark.parametrize(
    (
        "predecessor_changes",
        "successor_changes",
        "expected_presence",
        "expected_lifecycle",
        "expected_security",
        "expected_policy_applicable",
        "expected_capabilities",
    ),
    FAMILY_E_SUCCESSOR_EVOLUTION_ACCEPT_CASES,
)
def test_polling_gap_matrix_accepts_later_successor_evolution(
    predecessor_changes: dict[str, Any],
    successor_changes: dict[str, Any],
    expected_presence: PresenceState,
    expected_lifecycle: LifecycleState,
    expected_security: SecurityContinuity,
    expected_policy_applicable: bool,
    expected_capabilities: frozenset[str],
) -> None:
    previous = polling_gap_previous_snapshot()
    retained_predecessor = replace(predecessor(), **predecessor_changes)
    later_successor = successor(**successor_changes)
    current = polling_gap_current_snapshot(
        (retained_predecessor, later_successor)
    )

    current.validate_revision_successor(previous)

    retained = current.resources_by_id[RESOURCE_ID]
    observed_successor = current.resources_by_id[SUCCESSOR_ID]
    assert retained.successor_resource_id == SUCCESSOR_ID
    assert observed_successor.resource_id == SUCCESSOR_ID
    assert observed_successor.locator_generation == 5
    assert observed_successor.presence is expected_presence
    assert observed_successor.lifecycle is expected_lifecycle
    assert observed_successor.security_continuity is expected_security
    assert observed_successor.policy_applicable is expected_policy_applicable
    assert observed_successor.effective_capabilities == expected_capabilities
    if expected_presence is PresenceState.CONFIRMED_REMOVED:
        assert observed_successor.active_binding_id is None
    if predecessor_changes:
        assert retained.security_continuity is SecurityContinuity.REVOKED
        assert retained.resource_continuity_revision == 7
    assert current.sources[0].last_committed_run_sequence == 12
    assert current.inventory_revision - previous.inventory_revision > 1
    assert (
        current.published_state_revision
        - previous.published_state_revision
        > 1
    )


def test_polling_gap_matrix_accepts_ambiguity_without_identity_replacement() -> None:
    previous = polling_gap_previous_snapshot()
    old = previous.resources_by_id[RESOURCE_ID]
    ambiguous = replace(
        old,
        resource_continuity_revision=5,
        lifecycle=LifecycleState.QUARANTINED,
        observational_continuity=ObservationalContinuity.UNCERTAIN,
        security_continuity=SecurityContinuity.REVOKED,
    )
    current = polling_gap_current_snapshot((ambiguous,))

    current.validate_revision_successor(previous)

    retained = current.resources_by_id[RESOURCE_ID]
    assert retained.resource_id == old.resource_id
    assert retained.active_binding_id == old.active_binding_id
    assert retained.locator_generation == old.locator_generation == 4
    assert retained.effective_capabilities == frozenset()


def test_polling_gap_matrix_accepts_complete_skipped_successor_chain() -> None:
    previous = polling_gap_previous_snapshot()
    old_a = replace(predecessor(), resource_continuity_revision=5)
    old_b = successor(
        **{
            **NOT_CURRENT,
            "resource_continuity_revision": 7,
            "successor_resource_id": THIRD_RESOURCE_ID,
        }
    )
    current_c = resource(
        resource_id=THIRD_RESOURCE_ID,
        active_binding_id=THIRD_BINDING_ID,
        locator_generation=6,
        resource_continuity_revision=5,
        name="Third occupant",
    )
    current = polling_gap_current_snapshot((old_a, old_b, current_c))

    current.validate_revision_successor(previous)

    assert (
        current.resources_by_id[RESOURCE_ID].successor_resource_id
        == SUCCESSOR_ID
    )
    assert (
        current.resources_by_id[SUCCESSOR_ID].successor_resource_id
        == THIRD_RESOURCE_ID
    )
    assert current.resources_by_id[SUCCESSOR_ID].active_binding_id is None
    assert current.resources_by_id[THIRD_RESOURCE_ID].active_binding_id == (
        THIRD_BINDING_ID
    )
    assert sorted(
        item.locator_generation for item in current.resources
    ) == [4, 5, 6]


FAMILY_E_RETENTION_REJECT_CASES = [
    pytest.param(
        snapshot(
            (),
            sources=(source(),),
            nodes=(),
            inventory_revision=10,
            published_state_revision=20,
        ),
        snapshot(
            (),
            sources=(),
            nodes=(),
            inventory_revision=16,
            published_state_revision=50,
        ),
        "published snapshot cannot omit a retained inventory source",
        id="polling-gap-reject-retained-source-omission",
    ),
    pytest.param(
        snapshot(
            (),
            nodes=(node(),),
            inventory_revision=10,
            published_state_revision=20,
        ),
        snapshot(
            (),
            sources=(successful_source_run(12),),
            nodes=(),
            inventory_revision=16,
            published_state_revision=50,
        ),
        "published snapshot cannot omit a retained node",
        id="polling-gap-reject-retained-node-omission",
    ),
    pytest.param(
        polling_gap_previous_snapshot(),
        snapshot(
            (),
            sources=(successful_source_run(12),),
            nodes=(node(),),
            inventory_revision=16,
            published_state_revision=50,
        ),
        "published snapshot cannot omit a retained resource",
        id="polling-gap-reject-retained-resource-omission",
    ),
]


@pytest.mark.parametrize(
    ("previous", "current", "message"),
    FAMILY_E_RETENTION_REJECT_CASES,
)
def test_polling_gap_matrix_rejects_retained_entity_omission(
    previous: HubinetOpsSnapshot,
    current: HubinetOpsSnapshot,
    message: str,
) -> None:
    assert (
        current.published_state_revision
        - previous.published_state_revision
        > 1
    )
    with pytest.raises(ValueError, match=message):
        current.validate_revision_successor(previous)


def test_polling_gap_matrix_rejects_observed_lineage_rewrite() -> None:
    accepted = polling_gap_current_snapshot(
        (predecessor(), successor()),
        commit_sequence=12,
        inventory_revision=16,
        published_state_revision=47,
    )
    rewritten_predecessor = replace(
        predecessor(),
        successor_resource_id=THIRD_RESOURCE_ID,
    )
    different_successor = resource(
        resource_id=THIRD_RESOURCE_ID,
        active_binding_id=THIRD_BINDING_ID,
        locator_generation=5,
        name="Rewritten successor",
    )
    rewritten = polling_gap_current_snapshot(
        (rewritten_predecessor, different_successor),
        commit_sequence=18,
        inventory_revision=22,
        published_state_revision=90,
    )

    assert (
        accepted.resources_by_id[RESOURCE_ID].successor_resource_id
        == SUCCESSOR_ID
    )
    assert (
        rewritten.published_state_revision
        - accepted.published_state_revision
        > 1
    )
    with pytest.raises(
        ValueError,
        match="terminal replacement lineage is immutable",
    ):
        rewritten.validate_revision_successor(accepted)


def family_f_current_resource(
    *,
    resource_id: str = RESOURCE_ID,
    source_id: str = SOURCE_A_ID,
    binding_id: str = BINDING_ID,
    vmid: int = 101,
    generation: int = 4,
    resource_type: ResourceType = ResourceType.LXC,
    **changes: Any,
) -> ResourceSnapshot:
    values = {
        "resource_id": resource_id,
        "inventory_source_id": source_id,
        "active_binding_id": binding_id,
        "resource_type": resource_type,
        "vmid": vmid,
        "locator_generation": generation,
        "current_node_id": None,
        "last_known_node_id": None,
        "node_availability": NodeAvailability.UNRESOLVED,
    }
    values.update(changes)
    return resource(**values)


def family_f_removed_resource(
    *,
    resource_id: str = RESOURCE_ID,
    source_id: str = SOURCE_A_ID,
    vmid: int = 101,
    generation: int = 4,
    resource_type: ResourceType = ResourceType.LXC,
    **changes: Any,
) -> ResourceSnapshot:
    values = {
        **CONFIRMED_REMOVED,
        "resource_id": resource_id,
        "inventory_source_id": source_id,
        "resource_type": resource_type,
        "vmid": vmid,
        "locator_generation": generation,
        "resource_continuity_revision": 2,
        "last_known_node_id": None,
    }
    values.update(changes)
    return resource(**values)


def family_f_replaced_resource(
    *,
    successor_resource_id: str,
    resource_id: str = RESOURCE_ID,
    source_id: str = SOURCE_A_ID,
    vmid: int = 101,
    generation: int = 4,
    resource_type: ResourceType = ResourceType.LXC,
    **changes: Any,
) -> ResourceSnapshot:
    values = {
        **NOT_CURRENT,
        "resource_id": resource_id,
        "inventory_source_id": source_id,
        "resource_type": resource_type,
        "vmid": vmid,
        "locator_generation": generation,
        "resource_continuity_revision": 2,
        "last_known_node_id": None,
        "successor_resource_id": successor_resource_id,
    }
    values.update(changes)
    return resource(**values)


def family_f_missing_resource(
    *,
    resource_id: str = RESOURCE_ID,
    source_id: str = SOURCE_A_ID,
    binding_id: str = BINDING_ID,
    vmid: int = 101,
    generation: int = 4,
    **changes: Any,
) -> ResourceSnapshot:
    values = {
        **AMBIGUOUS_MISSING,
        "resource_continuity_revision": 2,
        "last_known_node_id": None,
    }
    values.update(changes)
    return family_f_current_resource(
        resource_id=resource_id,
        source_id=source_id,
        binding_id=binding_id,
        vmid=vmid,
        generation=generation,
        **values,
    )


FAMILY_F_SINGLE_VIEW_ACCEPT_CASES = [
    pytest.param(
        (
            family_f_current_resource(generation=4),
            family_f_current_resource(
                resource_id=SUCCESSOR_ID,
                source_id=SOURCE_B_ID,
                binding_id=SUCCESSOR_BINDING_ID,
                generation=4,
            ),
        ),
        (source(), source(SOURCE_B_ID)),
        {
            (SOURCE_A_ID, 101): RESOURCE_ID,
            (SOURCE_B_ID, 101): SUCCESSOR_ID,
        },
        (
            (SOURCE_A_ID, 101, 4),
            (SOURCE_B_ID, 101, 4),
        ),
        id="locator-accept-same-vmid-different-sources",
    ),
    pytest.param(
        (
            family_f_removed_resource(generation=4),
            family_f_current_resource(
                resource_id=SUCCESSOR_ID,
                binding_id=SUCCESSOR_BINDING_ID,
                generation=5,
            ),
        ),
        (source(),),
        {(SOURCE_A_ID, 101): SUCCESSOR_ID},
        (
            (SOURCE_A_ID, 101, 4),
            (SOURCE_A_ID, 101, 5),
        ),
        id="generation-accept-terminal-four-current-five",
    ),
    pytest.param(
        (
            family_f_removed_resource(generation=4),
            family_f_removed_resource(
                resource_id=SUCCESSOR_ID,
                generation=5,
            ),
            family_f_current_resource(
                resource_id=THIRD_RESOURCE_ID,
                binding_id=THIRD_BINDING_ID,
                generation=6,
            ),
        ),
        (source(),),
        {(SOURCE_A_ID, 101): THIRD_RESOURCE_ID},
        (
            (SOURCE_A_ID, 101, 4),
            (SOURCE_A_ID, 101, 5),
            (SOURCE_A_ID, 101, 6),
        ),
        id="generation-accept-two-terminal-generations-current-six",
    ),
    pytest.param(
        (
            family_f_removed_resource(generation=100),
            family_f_current_resource(
                resource_id=SUCCESSOR_ID,
                binding_id=SUCCESSOR_BINDING_ID,
                generation=101,
            ),
        ),
        (source(),),
        {(SOURCE_A_ID, 101): SUCCESSOR_ID},
        (
            (SOURCE_A_ID, 101, 100),
            (SOURCE_A_ID, 101, 101),
        ),
        id="generation-accept-history-minimum-one-hundred",
    ),
    pytest.param(
        (
            family_f_removed_resource(generation=4),
            family_f_current_resource(
                resource_id=SUCCESSOR_ID,
                binding_id=SUCCESSOR_BINDING_ID,
                generation=5,
            ),
            family_f_removed_resource(
                resource_id=THIRD_RESOURCE_ID,
                source_id=SOURCE_B_ID,
                generation=9,
            ),
            family_f_current_resource(
                resource_id=FOURTH_RESOURCE_ID,
                source_id=SOURCE_B_ID,
                binding_id=FOURTH_BINDING_ID,
                generation=10,
            ),
        ),
        (source(), source(SOURCE_B_ID)),
        {
            (SOURCE_A_ID, 101): SUCCESSOR_ID,
            (SOURCE_B_ID, 101): FOURTH_RESOURCE_ID,
        },
        (
            (SOURCE_A_ID, 101, 4),
            (SOURCE_A_ID, 101, 5),
            (SOURCE_B_ID, 101, 9),
            (SOURCE_B_ID, 101, 10),
        ),
        id="generation-accept-source-local-independent-histories",
    ),
]


@pytest.mark.parametrize(
    ("resources", "sources", "expected_current", "expected_history"),
    FAMILY_F_SINGLE_VIEW_ACCEPT_CASES,
)
def test_locator_binding_matrix_accepts_source_local_slot_geometry(
    resources: tuple[ResourceSnapshot, ...],
    sources: tuple[InventorySourceSnapshot, ...],
    expected_current: dict[tuple[str, int], str],
    expected_history: tuple[tuple[str, int, int], ...],
) -> None:
    view = snapshot(resources, sources=sources)

    assert {
        locator: item.resource_id
        for locator, item in view.current_resources_by_locator.items()
    } == expected_current
    assert sorted(
        (
            item.inventory_source_id,
            item.vmid,
            item.locator_generation,
        )
        for item in view.resources
    ) == sorted(expected_history)


FAMILY_F_SINGLE_VIEW_REJECT_CASES = [
    pytest.param(
        (
            family_f_current_resource(generation=4),
            family_f_current_resource(
                resource_id=SUCCESSOR_ID,
                binding_id=SUCCESSOR_BINDING_ID,
                generation=5,
            ),
        ),
        (source(),),
        "snapshot contains multiple current occupants for a locator",
        id="locator-reject-two-current-same-slot",
    ),
    pytest.param(
        (
            family_f_current_resource(generation=4),
            family_f_current_resource(
                resource_id=SUCCESSOR_ID,
                binding_id=SUCCESSOR_BINDING_ID,
                generation=5,
                resource_type=ResourceType.QEMU,
            ),
        ),
        (source(),),
        "snapshot contains multiple current occupants for a locator",
        id="locator-reject-lxc-qemu-concurrent-slot",
    ),
    pytest.param(
        (
            family_f_current_resource(generation=4),
            family_f_current_resource(
                resource_id=SUCCESSOR_ID,
                source_id=SOURCE_B_ID,
                binding_id=BINDING_ID,
                vmid=202,
                generation=9,
            ),
        ),
        (source(), source(SOURCE_B_ID)),
        "snapshot contains duplicate active binding identities",
        id="binding-reject-duplicate-active-identity-across-locators",
    ),
    pytest.param(
        (
            family_f_removed_resource(generation=4),
            family_f_removed_resource(
                resource_id=SUCCESSOR_ID,
                generation=4,
            ),
        ),
        (source(),),
        "snapshot contains duplicate retained locator generation",
        id="generation-reject-duplicate-retained-generation",
    ),
    pytest.param(
        (
            family_f_removed_resource(generation=4),
            family_f_current_resource(
                resource_id=SUCCESSOR_ID,
                binding_id=SUCCESSOR_BINDING_ID,
                generation=6,
            ),
        ),
        (source(),),
        "current locator generation must follow retained terminal history",
        id="generation-reject-confirmed-removal-reuse-skips-five",
    ),
    pytest.param(
        (
            family_f_replaced_resource(
                successor_resource_id=SUCCESSOR_ID,
            ),
            family_f_current_resource(
                resource_id=SUCCESSOR_ID,
                source_id=SOURCE_B_ID,
                binding_id=SUCCESSOR_BINDING_ID,
                generation=5,
            ),
        ),
        (source(), source(SOURCE_B_ID)),
        "replacement lineage violates locator history invariants",
        id="replacement-reject-successor-wrong-source",
    ),
    pytest.param(
        (
            family_f_replaced_resource(
                successor_resource_id=SUCCESSOR_ID,
            ),
            family_f_current_resource(
                resource_id=SUCCESSOR_ID,
                binding_id=SUCCESSOR_BINDING_ID,
                vmid=102,
                generation=5,
            ),
        ),
        (source(),),
        "replacement lineage violates locator history invariants",
        id="replacement-reject-successor-wrong-vmid",
    ),
    pytest.param(
        (
            family_f_replaced_resource(
                successor_resource_id=SUCCESSOR_ID,
            ),
            family_f_current_resource(
                resource_id=SUCCESSOR_ID,
                binding_id=SUCCESSOR_BINDING_ID,
                generation=6,
            ),
        ),
        (source(),),
        "current locator generation must follow retained terminal history",
        id="replacement-reject-successor-wrong-generation",
    ),
]


@pytest.mark.parametrize(
    ("resources", "sources", "message"),
    FAMILY_F_SINGLE_VIEW_REJECT_CASES,
)
def test_locator_binding_matrix_rejects_single_view_ownership_conflicts(
    resources: tuple[ResourceSnapshot, ...],
    sources: tuple[InventorySourceSnapshot, ...],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        snapshot(resources, sources=sources)


FAMILY_F_GENERATION_GAP_REJECT_CASES = [
    pytest.param((4, 6), id="generation-reject-internal-gap-four-six"),
    pytest.param((4, 5, 7), id="generation-reject-internal-gap-four-five-seven"),
    pytest.param((100, 102), id="generation-reject-internal-gap-hundred"),
]


@pytest.mark.parametrize("generations", FAMILY_F_GENERATION_GAP_REJECT_CASES)
def test_locator_binding_matrix_rejects_internal_generation_gaps(
    generations: tuple[int, ...],
) -> None:
    resource_ids = (RESOURCE_ID, SUCCESSOR_ID, THIRD_RESOURCE_ID)
    retained = tuple(
        family_f_removed_resource(
            resource_id=resource_ids[index],
            generation=generation,
        )
        for index, generation in enumerate(generations)
    )

    with pytest.raises(
        ValueError,
        match="retained locator generations must be consecutive",
    ):
        snapshot(retained)


FAMILY_F_TERMINAL_AND_REPLACEMENT_ACCEPT_CASES = [
    pytest.param(
        (family_f_current_resource(generation=4),),
        (family_f_removed_resource(generation=4),),
        None,
        None,
        id="binding-accept-present-to-confirmed-removed-closes-binding",
    ),
    pytest.param(
        (family_f_current_resource(generation=4),),
        (
            family_f_replaced_resource(
                successor_resource_id=SUCCESSOR_ID,
            ),
            family_f_current_resource(
                resource_id=SUCCESSOR_ID,
                binding_id=SUCCESSOR_BINDING_ID,
                generation=5,
            ),
        ),
        SUCCESSOR_ID,
        ResourceType.LXC,
        id="replacement-accept-present-predecessor-fresh-binding",
    ),
    pytest.param(
        (family_f_missing_resource(),),
        (
            family_f_replaced_resource(
                successor_resource_id=SUCCESSOR_ID,
                resource_continuity_revision=3,
            ),
            family_f_current_resource(
                resource_id=SUCCESSOR_ID,
                binding_id=SUCCESSOR_BINDING_ID,
                generation=5,
            ),
        ),
        SUCCESSOR_ID,
        ResourceType.LXC,
        id="replacement-accept-missing-predecessor-fresh-binding",
    ),
    pytest.param(
        (family_f_current_resource(generation=4),),
        (
            family_f_replaced_resource(
                successor_resource_id=SUCCESSOR_ID,
            ),
            family_f_current_resource(
                resource_id=SUCCESSOR_ID,
                binding_id=SUCCESSOR_BINDING_ID,
                generation=5,
                resource_type=ResourceType.QEMU,
            ),
        ),
        SUCCESSOR_ID,
        ResourceType.QEMU,
        id="replacement-accept-lxc-to-qemu-same-slot",
    ),
    pytest.param(
        (family_f_removed_resource(generation=4),),
        (
            family_f_removed_resource(generation=4),
            family_f_current_resource(
                resource_id=SUCCESSOR_ID,
                binding_id=SUCCESSOR_BINDING_ID,
                generation=5,
            ),
        ),
        SUCCESSOR_ID,
        ResourceType.LXC,
        id="replacement-accept-confirmed-removal-slot-reuse",
    ),
]


@pytest.mark.parametrize(
    (
        "previous_resources",
        "current_resources",
        "expected_current_id",
        "expected_current_type",
    ),
    FAMILY_F_TERMINAL_AND_REPLACEMENT_ACCEPT_CASES,
)
def test_locator_binding_matrix_accepts_terminal_and_replacement_transitions(
    previous_resources: tuple[ResourceSnapshot, ...],
    current_resources: tuple[ResourceSnapshot, ...],
    expected_current_id: str | None,
    expected_current_type: ResourceType | None,
) -> None:
    previous = snapshot(
        previous_resources,
        inventory_revision=10,
        published_state_revision=20,
    )
    current = snapshot(
        current_resources,
        sources=(successful_source_run(12),),
        inventory_revision=16,
        published_state_revision=47,
    )

    current.validate_revision_successor(previous)

    retained = current.resources_by_id[RESOURCE_ID]
    assert retained.active_binding_id is None
    if expected_current_id is not None:
        occupant = current.resources_by_id[expected_current_id]
        assert occupant.active_binding_id == SUCCESSOR_BINDING_ID
        assert occupant.active_binding_id != BINDING_ID
        assert occupant.locator_generation == 5
        assert occupant.resource_type is expected_current_type
    assert current.sources[0].last_committed_run_sequence == 12


FAMILY_F_NONTERMINAL_CONTINUITY_ACCEPT_CASES = [
    pytest.param(
        family_f_current_resource(generation=4),
        family_f_missing_resource(),
        PresenceState.MISSING,
        id="binding-accept-present-to-missing-retains-identity",
    ),
    pytest.param(
        family_f_missing_resource(),
        family_f_current_resource(
            generation=4,
            resource_continuity_revision=3,
        ),
        PresenceState.PRESENT,
        id="binding-accept-missing-to-present-retains-identity",
    ),
    pytest.param(
        family_f_current_resource(generation=4),
        family_f_current_resource(
            generation=4,
            resource_continuity_revision=2,
            lifecycle=LifecycleState.QUARANTINED,
            observational_continuity=ObservationalContinuity.UNCERTAIN,
        ),
        PresenceState.PRESENT,
        id="binding-accept-quarantine-retains-identity",
    ),
    pytest.param(
        family_f_current_resource(generation=4, name="Before rename"),
        family_f_current_resource(generation=4, name="After rename"),
        PresenceState.PRESENT,
        id="generation-accept-rename-does-not-create-incarnation",
    ),
]


@pytest.mark.parametrize(
    ("previous_resource", "current_resource", "expected_presence"),
    FAMILY_F_NONTERMINAL_CONTINUITY_ACCEPT_CASES,
)
def test_locator_binding_matrix_accepts_nonterminal_identity_continuity(
    previous_resource: ResourceSnapshot,
    current_resource: ResourceSnapshot,
    expected_presence: PresenceState,
) -> None:
    previous = snapshot(
        (previous_resource,),
        inventory_revision=10,
        published_state_revision=20,
    )
    current = snapshot(
        (current_resource,),
        sources=(successful_source_run(12),),
        inventory_revision=16,
        published_state_revision=47,
    )

    current.validate_revision_successor(previous)

    assert current_resource.presence is expected_presence
    assert current_resource.resource_id == previous_resource.resource_id
    assert current_resource.inventory_source_id == previous_resource.inventory_source_id
    assert current_resource.resource_type is previous_resource.resource_type
    assert current_resource.vmid == previous_resource.vmid
    assert current_resource.locator_generation == previous_resource.locator_generation
    assert current_resource.active_binding_id == previous_resource.active_binding_id


def test_locator_binding_matrix_rejects_nonterminal_binding_change() -> None:
    previous_resource = family_f_current_resource(generation=4)
    changed_binding = family_f_current_resource(
        generation=4,
        binding_id=SUCCESSOR_BINDING_ID,
    )
    previous = snapshot(
        (previous_resource,),
        inventory_revision=10,
        published_state_revision=20,
    )
    current = snapshot(
        (changed_binding,),
        sources=(successful_source_run(12),),
        inventory_revision=16,
        published_state_revision=47,
    )

    with pytest.raises(
        ValueError,
        match="active binding is immutable before a terminal transition",
    ):
        current.validate_revision_successor(previous)


FAMILY_F_BINDING_OWNER_MOVE_REJECT_CASES = [
    pytest.param(
        (
            family_f_replaced_resource(
                successor_resource_id=SUCCESSOR_ID,
            ),
            family_f_current_resource(
                resource_id=SUCCESSOR_ID,
                binding_id=BINDING_ID,
                generation=5,
            ),
        ),
        (successful_source_run(12),),
        id="binding-reject-successor-reuses-predecessor-binding",
    ),
    pytest.param(
        (
            family_f_removed_resource(generation=4),
            family_f_current_resource(
                resource_id=SUCCESSOR_ID,
                binding_id=BINDING_ID,
                vmid=202,
                generation=1,
            ),
        ),
        (successful_source_run(12),),
        id="binding-reject-owner-move-to-different-vmid",
    ),
    pytest.param(
        (
            family_f_removed_resource(generation=4),
            family_f_current_resource(
                resource_id=SUCCESSOR_ID,
                source_id=SOURCE_B_ID,
                binding_id=BINDING_ID,
                vmid=202,
                generation=1,
            ),
        ),
        (successful_source_run(12), source(SOURCE_B_ID)),
        id="binding-reject-owner-move-to-different-source",
    ),
]


@pytest.mark.parametrize(
    ("current_resources", "current_sources"),
    FAMILY_F_BINDING_OWNER_MOVE_REJECT_CASES,
)
def test_locator_binding_matrix_rejects_binding_owner_move(
    current_resources: tuple[ResourceSnapshot, ...],
    current_sources: tuple[InventorySourceSnapshot, ...],
) -> None:
    previous = snapshot(
        (family_f_current_resource(generation=4),),
        inventory_revision=10,
        published_state_revision=20,
    )
    current = snapshot(
        current_resources,
        sources=current_sources,
        inventory_revision=16,
        published_state_revision=47,
    )

    with pytest.raises(
        ValueError,
        match="active binding identity cannot move between resources",
    ):
        current.validate_revision_successor(previous)


FAMILY_F_INCARNATION_IMMUTABILITY_REJECT_CASES = [
    pytest.param(
        family_f_current_resource(
            source_id=SOURCE_B_ID,
            generation=4,
        ),
        (successful_source_run(12), source(SOURCE_B_ID)),
        "resource identity cannot move between sources",
        id="incarnation-reject-source-change",
    ),
    pytest.param(
        family_f_current_resource(
            generation=4,
            resource_type=ResourceType.QEMU,
        ),
        (successful_source_run(12),),
        "resource_type is immutable for an incarnation",
        id="incarnation-reject-type-change",
    ),
    pytest.param(
        family_f_current_resource(vmid=102, generation=4),
        (successful_source_run(12),),
        "resource locator is immutable for an incarnation",
        id="incarnation-reject-vmid-change",
    ),
    pytest.param(
        family_f_current_resource(generation=5),
        (successful_source_run(12),),
        "locator_generation is immutable for an incarnation",
        id="incarnation-reject-generation-change",
    ),
]


@pytest.mark.parametrize(
    ("current_resource", "current_sources", "message"),
    FAMILY_F_INCARNATION_IMMUTABILITY_REJECT_CASES,
)
def test_locator_binding_matrix_rejects_incarnation_identity_mutation(
    current_resource: ResourceSnapshot,
    current_sources: tuple[InventorySourceSnapshot, ...],
    message: str,
) -> None:
    previous = snapshot(
        (family_f_current_resource(generation=4),),
        inventory_revision=10,
        published_state_revision=20,
    )
    current = snapshot(
        (current_resource,),
        sources=current_sources,
        inventory_revision=16,
        published_state_revision=47,
    )

    with pytest.raises(ValueError, match=message):
        current.validate_revision_successor(previous)


def test_locator_binding_matrix_rejects_older_generation_backfill() -> None:
    current_resource = family_f_current_resource(generation=4)
    previous = snapshot(
        (current_resource,),
        inventory_revision=10,
        published_state_revision=20,
    )
    backfilled = family_f_removed_resource(
        resource_id=SUCCESSOR_ID,
        generation=3,
    )
    current = snapshot(
        (backfilled, current_resource),
        sources=(successful_source_run(12),),
        inventory_revision=16,
        published_state_revision=47,
    )

    with pytest.raises(
        ValueError,
        match="new locator history must follow all previously retained generations",
    ):
        current.validate_revision_successor(previous)


FAMILY_G_CANONICAL_LOCATOR_V2 = "https://canonical-v2.example.test:8006"
FAMILY_G_CANONICAL_LOCATOR_V3 = "https://canonical-v3.example.test:8006"
FAMILY_G_REWRITTEN_LOCATOR = "https://rewritten.example.test:8006"


def family_g_context(
    *,
    config_revision: int = 3,
    endpoint_id: str = ENDPOINT_A_ID,
    locator: str = "https://pve-a.example.test:8006",
    canonicalization_version: int = 1,
    trust_revision: int = 2,
) -> SourceContext:
    return SourceContext(
        source_config_revision=config_revision,
        endpoint_id=endpoint_id,
        canonical_transport_locator=locator,
        canonicalization_contract_version=canonicalization_version,
        transport_trust_revision=trust_revision,
    )


def family_g_controlled_source(
    *,
    current: SourceContext,
    committed: SourceContext,
    source_id: str = SOURCE_A_ID,
    **changes: Any,
) -> InventorySourceSnapshot:
    values = {
        "health": SourceHealth.DEGRADED,
        "freshness": SourceFreshness.STALE,
        "health_origin": SourceHealthOrigin.CONTROLLED_CONTEXT_TRANSITION,
        "health_reason": "source_context_changed",
        "current_context": current,
        "committed_context": committed,
    }
    values.update(changes)
    return replace(source(source_id), **values)


FAMILY_G_CONTEXT_ACCEPT_CASES = [
    pytest.param(
        family_g_context(),
        family_g_context(),
        True,
        id="context-accept-exact-current-and-committed",
    ),
    pytest.param(
        family_g_context(config_revision=4),
        family_g_context(config_revision=3),
        False,
        id="context-accept-config-progression",
    ),
    pytest.param(
        family_g_context(trust_revision=3),
        family_g_context(trust_revision=2),
        False,
        id="context-accept-trust-progression",
    ),
    pytest.param(
        family_g_context(
            config_revision=4,
            locator=FAMILY_G_CANONICAL_LOCATOR_V2,
            canonicalization_version=2,
        ),
        family_g_context(config_revision=3),
        False,
        id="context-accept-canonicalization-migration",
    ),
]


@pytest.mark.parametrize(
    ("current_context", "committed_context", "expected_available"),
    FAMILY_G_CONTEXT_ACCEPT_CASES,
)
def test_source_context_freshness_matrix_accepts_context_provenance(
    current_context: SourceContext,
    committed_context: SourceContext,
    expected_available: bool,
) -> None:
    if current_context == committed_context:
        item = replace(
            source(),
            current_context=current_context,
            committed_context=committed_context,
        )
    else:
        item = family_g_controlled_source(
            current=current_context,
            committed=committed_context,
        )

    assert item.current_context.endpoint_id == item.committed_context.endpoint_id
    assert item.current_context.source_config_revision >= (
        item.committed_context.source_config_revision
    )
    assert item.current_context.transport_trust_revision >= (
        item.committed_context.transport_trust_revision
    )
    assert item.current_context.canonicalization_contract_version >= (
        item.committed_context.canonicalization_contract_version
    )
    assert item.current_facts_available is expected_available


FAMILY_G_CONTEXT_REJECT_CASES = [
    pytest.param(
        family_g_context(config_revision=3),
        family_g_context(config_revision=4),
        "current source_config_revision cannot predate committed context",
        id="context-reject-current-config-before-commit",
    ),
    pytest.param(
        family_g_context(trust_revision=2),
        family_g_context(trust_revision=3),
        "current transport_trust_revision cannot predate committed context",
        id="context-reject-current-trust-before-commit",
    ),
    pytest.param(
        family_g_context(endpoint_id=ENDPOINT_A_ID),
        family_g_context(endpoint_id=ENDPOINT_B_ID),
        "current and committed context must reference the same endpoint",
        id="context-reject-current-committed-endpoint-mismatch",
    ),
    pytest.param(
        family_g_context(locator=FAMILY_G_REWRITTEN_LOCATOR),
        family_g_context(),
        "one canonicalization contract version cannot reinterpret the committed transport locator",
        id="context-reject-same-version-locator-reinterpretation",
    ),
    pytest.param(
        family_g_context(
            config_revision=4,
            canonicalization_version=1,
        ),
        family_g_context(
            config_revision=3,
            locator=FAMILY_G_CANONICAL_LOCATOR_V2,
            canonicalization_version=2,
        ),
        "current canonicalization contract cannot predate committed context",
        id="context-reject-current-canonicalization-before-commit",
    ),
    pytest.param(
        family_g_context(
            config_revision=3,
            locator=FAMILY_G_CANONICAL_LOCATOR_V2,
            canonicalization_version=2,
        ),
        family_g_context(config_revision=3),
        "canonicalization migration requires newer current source configuration",
        id="context-reject-migration-without-current-config-progression",
    ),
]


@pytest.mark.parametrize(
    ("current_context", "committed_context", "message"),
    FAMILY_G_CONTEXT_REJECT_CASES,
)
def test_source_context_freshness_matrix_rejects_context_incoherence(
    current_context: SourceContext,
    committed_context: SourceContext,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        family_g_controlled_source(
            current=current_context,
            committed=committed_context,
        )


def test_source_context_freshness_matrix_rejects_shared_endpoint_identity(
) -> None:
    shared = family_g_context(endpoint_id=ENDPOINT_A_ID)
    second_source = replace(
        source(SOURCE_B_ID),
        current_context=shared,
        committed_context=shared,
    )

    with pytest.raises(
        ValueError,
        match="endpoint identity cannot be shared across inventory sources",
    ):
        snapshot(
            (),
            sources=(source(), second_source),
            nodes=(),
        )


def test_source_context_freshness_matrix_rejects_endpoint_owner_move(
) -> None:
    previous = source_only_snapshot(
        source(),
        inventory_revision=10,
        published_state_revision=20,
    )
    source_a_new_endpoint = family_g_context(
        config_revision=4,
        endpoint_id=ENDPOINT_B_ID,
    )
    source_b_old_endpoint = family_g_context(
        endpoint_id=ENDPOINT_A_ID,
        locator="https://pve-b.example.test:8006",
    )
    current = snapshot(
        (),
        sources=(
            replace(
                source(),
                current_context=source_a_new_endpoint,
                committed_context=source_a_new_endpoint,
            ),
            replace(
                source(SOURCE_B_ID),
                current_context=source_b_old_endpoint,
                committed_context=source_b_old_endpoint,
            ),
        ),
        nodes=(),
        inventory_revision=11,
        published_state_revision=21,
    )

    with pytest.raises(
        ValueError,
        match="endpoint identity cannot move between inventory sources",
    ):
        current.validate_revision_successor(previous)


FAMILY_G_CROSS_SNAPSHOT_CONTEXT_REJECT_CASES = [
    pytest.param(
        source(),
        replace(
            source(),
            current_context=family_g_context(
                config_revision=4,
                endpoint_id=ENDPOINT_B_ID,
            ),
            committed_context=family_g_context(
                config_revision=4,
                endpoint_id=ENDPOINT_B_ID,
            ),
        ),
        10,
        "endpoint_id is immutable for an existing inventory source",
        id="endpoint-reject-existing-source-identity-change",
    ),
    pytest.param(
        source(),
        replace(source(), provider_kind="other_provider"),
        11,
        "provider_kind is immutable for an inventory source",
        id="context-reject-provider-kind-change",
    ),
    pytest.param(
        family_g_controlled_source(
            current=family_g_context(config_revision=4),
            committed=family_g_context(config_revision=3),
        ),
        family_g_controlled_source(
            current=family_g_context(config_revision=3),
            committed=family_g_context(config_revision=3),
        ),
        10,
        "source_config_revision must not regress",
        id="context-reject-config-revision-regression",
    ),
    pytest.param(
        family_g_controlled_source(
            current=family_g_context(trust_revision=3),
            committed=family_g_context(trust_revision=2),
        ),
        family_g_controlled_source(
            current=family_g_context(trust_revision=2),
            committed=family_g_context(trust_revision=2),
        ),
        10,
        "transport_trust_revision must not regress",
        id="context-reject-trust-revision-regression",
    ),
    pytest.param(
        source(),
        replace(
            source(),
            current_context=family_g_context(
                config_revision=4,
                locator=FAMILY_G_REWRITTEN_LOCATOR,
            ),
            committed_context=family_g_context(
                config_revision=4,
                locator=FAMILY_G_REWRITTEN_LOCATOR,
            ),
        ),
        10,
        "canonical transport locator is immutable within a canonicalization contract version",
        id="canonicalization-reject-same-version-locator-rewrite",
    ),
    pytest.param(
        source(),
        replace(
            source(),
            current_context=family_g_context(
                config_revision=3,
                locator=FAMILY_G_CANONICAL_LOCATOR_V2,
                canonicalization_version=2,
            ),
            committed_context=family_g_context(
                config_revision=3,
                locator=FAMILY_G_CANONICAL_LOCATOR_V2,
                canonicalization_version=2,
            ),
        ),
        10,
        "canonicalization migration requires a newer source_config_revision",
        id="canonicalization-reject-migration-without-config-bump",
    ),
    pytest.param(
        replace(
            source(),
            current_context=family_g_context(
                config_revision=3,
                locator=FAMILY_G_CANONICAL_LOCATOR_V2,
                canonicalization_version=2,
            ),
            committed_context=family_g_context(
                config_revision=3,
                locator=FAMILY_G_CANONICAL_LOCATOR_V2,
                canonicalization_version=2,
            ),
        ),
        replace(
            source(),
            current_context=family_g_context(config_revision=4),
            committed_context=family_g_context(config_revision=4),
        ),
        10,
        "canonicalization_contract_version must increase during migration",
        id="canonicalization-reject-version-regression",
    ),
]


@pytest.mark.parametrize(
    ("previous_source", "current_source", "inventory_revision", "message"),
    FAMILY_G_CROSS_SNAPSHOT_CONTEXT_REJECT_CASES,
)
def test_source_context_freshness_matrix_rejects_cross_snapshot_context_change(
    previous_source: InventorySourceSnapshot,
    current_source: InventorySourceSnapshot,
    inventory_revision: int,
    message: str,
) -> None:
    previous = source_only_snapshot(
        previous_source,
        inventory_revision=10,
        published_state_revision=20,
    )
    current = source_only_snapshot(
        current_source,
        inventory_revision=inventory_revision,
        published_state_revision=21,
    )

    with pytest.raises(ValueError, match=message):
        current.validate_revision_successor(previous)


def test_source_context_freshness_matrix_accepts_non_adjacent_canonicalization_migration(
) -> None:
    previous = source_only_snapshot(
        source(),
        inventory_revision=10,
        published_state_revision=20,
    )
    current_source = family_g_controlled_source(
        current=family_g_context(
            config_revision=5,
            locator=FAMILY_G_CANONICAL_LOCATOR_V3,
            canonicalization_version=3,
        ),
        committed=family_g_context(config_revision=3),
    )
    current = source_only_snapshot(
        current_source,
        inventory_revision=10,
        published_state_revision=50,
    )

    current.validate_revision_successor(previous)

    assert current_source.current_context.canonicalization_contract_version == 3
    assert current_source.current_context.source_config_revision == 5
    assert current_source.current_context.endpoint_id == ENDPOINT_A_ID
    assert current_source.current_context != current_source.committed_context
    assert not current_source.current_facts_available
    assert current.inventory_revision == previous.inventory_revision


def family_g_committed_history_source(
    *,
    current: SourceContext,
    committed: SourceContext,
) -> InventorySourceSnapshot:
    return successful_source_run(
        9,
        health=SourceHealth.DEGRADED,
        freshness=SourceFreshness.STALE,
        health_origin=SourceHealthOrigin.CONTROLLED_CONTEXT_TRANSITION,
        health_reason="source_context_changed_after_commit",
        current_context=current,
        committed_context=committed,
    )


FAMILY_G_COMMITTED_HISTORY_REJECT_CASES = [
    pytest.param(
        source(),
        family_g_committed_history_source(
            current=family_g_context(config_revision=4),
            committed=family_g_context(config_revision=2),
        ),
        "committed source_config_revision must not regress",
        id="committed-context-reject-config-regression",
    ),
    pytest.param(
        source(),
        family_g_committed_history_source(
            current=family_g_context(config_revision=4, trust_revision=3),
            committed=family_g_context(
                config_revision=3,
                trust_revision=1,
            ),
        ),
        "committed transport_trust_revision must not regress",
        id="committed-context-reject-trust-regression",
    ),
    pytest.param(
        replace(
            source(),
            current_context=family_g_context(
                config_revision=4,
                locator=FAMILY_G_CANONICAL_LOCATOR_V2,
                canonicalization_version=2,
            ),
            committed_context=family_g_context(
                config_revision=4,
                locator=FAMILY_G_CANONICAL_LOCATOR_V2,
                canonicalization_version=2,
            ),
        ),
        family_g_committed_history_source(
            current=family_g_context(
                config_revision=6,
                locator=FAMILY_G_CANONICAL_LOCATOR_V3,
                canonicalization_version=3,
            ),
            committed=family_g_context(config_revision=5),
        ),
        "committed canonicalization contract must not regress",
        id="committed-context-reject-canonicalization-regression",
    ),
    pytest.param(
        source(),
        family_g_committed_history_source(
            current=family_g_context(
                config_revision=5,
                locator=FAMILY_G_CANONICAL_LOCATOR_V2,
                canonicalization_version=2,
            ),
            committed=family_g_context(
                config_revision=4,
                locator=FAMILY_G_REWRITTEN_LOCATOR,
            ),
        ),
        "committed canonical locator is immutable within a canonicalization contract version",
        id="committed-context-reject-same-version-locator-rewrite",
    ),
    pytest.param(
        source(),
        family_g_committed_history_source(
            current=family_g_context(
                config_revision=4,
                locator=FAMILY_G_CANONICAL_LOCATOR_V3,
                canonicalization_version=3,
            ),
            committed=family_g_context(
                config_revision=3,
                locator=FAMILY_G_CANONICAL_LOCATOR_V2,
                canonicalization_version=2,
            ),
        ),
        "committed canonicalization migration requires a newer source_config_revision",
        id="committed-context-reject-migration-without-config-progression",
    ),
]


@pytest.mark.parametrize(
    ("previous_source", "current_source", "message"),
    FAMILY_G_COMMITTED_HISTORY_REJECT_CASES,
)
def test_source_context_freshness_matrix_rejects_committed_context_regression(
    previous_source: InventorySourceSnapshot,
    current_source: InventorySourceSnapshot,
    message: str,
) -> None:
    previous = source_only_snapshot(
        previous_source,
        inventory_revision=10,
        published_state_revision=20,
    )
    current = source_only_snapshot(
        current_source,
        inventory_revision=11,
        published_state_revision=21,
    )

    with pytest.raises(ValueError, match=message):
        current.validate_revision_successor(previous)


FAMILY_G_FRESHNESS_ACCEPT_CASES = [
    pytest.param(
        time_expiry_source(),
        SourceHealthOrigin.TIME_EXPIRY,
        5,
        False,
        id="freshness-accept-time-expiry-exact-commit",
    ),
    pytest.param(
        replace(
            time_expiry_source(),
            last_issued_run_sequence=6,
            latest_completed_run_sequence=6,
            latest_completed_outcome=FAMILY_D_AUDIT_ONLY_OUTCOME,
        ),
        SourceHealthOrigin.TIME_EXPIRY,
        6,
        False,
        id="freshness-accept-time-expiry-newer-audit-completion",
    ),
    pytest.param(
        source_with_run_provenance(
            last_issued_run_sequence=6,
            latest_completed_run_sequence=6,
            latest_completed_outcome=FAMILY_D_AUDIT_ONLY_OUTCOME,
        ),
        SourceHealthOrigin.DISCOVERY_RUN,
        6,
        True,
        id="freshness-accept-fresh-with-newer-audit-completion",
    ),
]


@pytest.mark.parametrize(
    ("item", "expected_origin", "expected_completed", "expected_available"),
    FAMILY_G_FRESHNESS_ACCEPT_CASES,
)
def test_source_context_freshness_matrix_accepts_materialized_freshness(
    item: InventorySourceSnapshot,
    expected_origin: SourceHealthOrigin,
    expected_completed: int,
    expected_available: bool,
) -> None:
    assert item.health_origin is expected_origin
    assert item.latest_completed_run_sequence == expected_completed
    assert item.current_context == item.committed_context
    assert item.last_health_run_sequence == item.last_committed_run_sequence
    assert item.current_facts_available is expected_available


FAMILY_G_FRESHNESS_REJECT_CASES = [
    pytest.param(
        {"health": SourceHealth.SOURCE_UNAVAILABLE},
        id="freshness-reject-source-unavailable-as-fresh",
    ),
    pytest.param(
        {"health": SourceHealth.DEGRADED},
        id="freshness-reject-degraded-as-fresh",
    ),
    pytest.param(
        {"health": SourceHealth.CONFIGURATION_ERROR},
        id="freshness-reject-configuration-error-as-fresh",
    ),
    pytest.param(
        {
            "current_context": family_g_context(config_revision=4),
            "committed_context": family_g_context(config_revision=3),
        },
        id="freshness-reject-current-context-differs-from-commit",
    ),
    pytest.param(
        {
            "last_issued_run_sequence": 6,
            "latest_completed_run_sequence": 6,
            "latest_completed_outcome": FAMILY_D_FAILURE_OUTCOME,
            "last_health_run_sequence": 6,
            "last_run_health_outcome": FAMILY_D_FAILURE_OUTCOME,
        },
        id="freshness-reject-applied-health-newer-than-commit",
    ),
    pytest.param(
        {
            "last_issued_run_sequence": 1,
            "latest_completed_run_sequence": 1,
            "latest_completed_outcome": FAMILY_D_FAILURE_OUTCOME,
            "last_health_run_sequence": 1,
            "last_run_health_outcome": FAMILY_D_FAILURE_OUTCOME,
            "last_committed_run_sequence": None,
            "last_successful_observed_at": None,
            "freshness_reference_at": None,
            "freshness_valid_until": None,
            "committed_context": None,
        },
        id="freshness-reject-without-successful-commit",
    ),
]


@pytest.mark.parametrize("changes", FAMILY_G_FRESHNESS_REJECT_CASES)
def test_source_context_freshness_matrix_rejects_non_authoritative_fresh_view(
    changes: dict[str, Any],
) -> None:
    with pytest.raises(
        ValueError,
        match="fresh source requires a healthy authoritative discovery commit",
    ):
        source_with_run_provenance(**changes)


def test_source_context_freshness_matrix_rejects_discovery_origin_without_run(
) -> None:
    with pytest.raises(
        ValueError,
        match="discovery_run health origin requires run provenance",
    ):
        initial_source_with_run_provenance(
            health=SourceHealth.DEGRADED,
            freshness=SourceFreshness.STALE,
            health_origin=SourceHealthOrigin.DISCOVERY_RUN,
            health_reason="missing_run_provenance",
        )


def test_source_context_freshness_matrix_rejects_fresh_controlled_transition(
) -> None:
    with pytest.raises(
        ValueError,
        match="controlled context transition must be stale",
    ):
        source_with_run_provenance(
            health_origin=SourceHealthOrigin.CONTROLLED_CONTEXT_TRANSITION,
            health_reason="source_context_changed",
        )


FAMILY_G_TIME_EXPIRY_REJECT_CASES = [
    pytest.param(
        {"current_context": family_g_context(config_revision=4)},
        id="freshness-reject-time-expiry-changed-context",
    ),
    pytest.param(
        {
            "last_issued_run_sequence": 6,
            "latest_completed_run_sequence": 6,
            "latest_completed_outcome": FAMILY_D_FAILURE_OUTCOME,
            "last_health_run_sequence": 6,
            "last_run_health_outcome": FAMILY_D_FAILURE_OUTCOME,
        },
        id="freshness-reject-time-expiry-newer-applied-health",
    ),
    pytest.param(
        {
            "last_issued_run_sequence": 1,
            "latest_completed_run_sequence": 1,
            "latest_completed_outcome": FAMILY_D_FAILURE_OUTCOME,
            "last_health_run_sequence": 1,
            "last_run_health_outcome": FAMILY_D_FAILURE_OUTCOME,
            "last_committed_run_sequence": None,
            "last_successful_observed_at": None,
            "freshness_reference_at": None,
            "freshness_valid_until": None,
            "committed_context": None,
        },
        id="freshness-reject-time-expiry-without-commit",
    ),
    pytest.param(
        {"freshness": SourceFreshness.FRESH},
        id="freshness-reject-time-expiry-as-fresh",
    ),
]


@pytest.mark.parametrize("changes", FAMILY_G_TIME_EXPIRY_REJECT_CASES)
def test_source_context_freshness_matrix_rejects_invalid_time_expiry(
    changes: dict[str, Any],
) -> None:
    with pytest.raises(
        ValueError,
        match="time_expiry requires stale exact committed run and context provenance",
    ):
        replace(time_expiry_source(), **changes)


def test_source_context_freshness_matrix_rejects_stale_resurrection_same_commit(
) -> None:
    previous = source_only_snapshot(
        time_expiry_source(),
        inventory_revision=10,
        published_state_revision=20,
    )
    current = source_only_snapshot(
        source(),
        inventory_revision=10,
        published_state_revision=500,
    )

    with pytest.raises(
        ValueError,
        match="a stale source requires a newer successful inventory commit before returning to fresh",
    ):
        current.validate_revision_successor(previous)


def test_source_context_freshness_matrix_accepts_non_adjacent_stale_recovery(
) -> None:
    previous = source_only_snapshot(
        time_expiry_source(),
        inventory_revision=10,
        published_state_revision=20,
    )
    current_source = successful_source_run(9)
    current = source_only_snapshot(
        current_source,
        inventory_revision=11,
        published_state_revision=500,
    )

    current.validate_revision_successor(previous)

    assert current_source.last_committed_run_sequence == 9
    assert current_source.last_committed_run_sequence - 5 > 1
    assert current_source.current_facts_available


FAMILY_H_FOREIGN_BACKEND_ID = "5b42fc41-61f3-4895-a04e-57fc761bc2c4"


def family_h_initial_source_b() -> InventorySourceSnapshot:
    return replace(
        initial_source_with_run_provenance(),
        inventory_source_id=SOURCE_B_ID,
        name="Initial matrix source B",
        current_context=source(SOURCE_B_ID).current_context,
    )


def test_snapshot_consistency_matrix_accepts_equal_immutable_view() -> None:
    previous = snapshot(())
    current = snapshot(())

    current.validate_revision_successor(previous)

    assert current == previous
    assert current.published_state_revision == previous.published_state_revision


def test_snapshot_consistency_matrix_rejects_changed_view_at_same_revision(
) -> None:
    previous = snapshot(())
    current = replace(
        previous,
        backend=replace(previous.backend, version="0.5.0.dev1"),
    )

    with pytest.raises(
        ValueError,
        match="one published_state_revision must identify one immutable view",
    ):
        current.validate_revision_successor(previous)


def test_snapshot_consistency_matrix_rejects_published_revision_regression(
) -> None:
    previous = snapshot(())
    current = snapshot((), published_state_revision=19)

    with pytest.raises(
        ValueError,
        match="published_state_revision must not regress",
    ):
        current.validate_revision_successor(previous)


def test_snapshot_consistency_matrix_accepts_non_adjacent_presentation_revision(
) -> None:
    previous = snapshot(())
    current = replace(
        snapshot((), published_state_revision=90),
        backend=replace(previous.backend, version="0.5.0.dev9"),
    )

    current.validate_revision_successor(previous)

    assert current.inventory_revision == previous.inventory_revision
    assert current.published_state_revision - previous.published_state_revision > 1


def test_snapshot_consistency_matrix_rejects_inventory_revision_regression(
) -> None:
    previous = snapshot(())
    current = snapshot(
        (),
        inventory_revision=9,
        published_state_revision=21,
    )

    with pytest.raises(
        ValueError,
        match="inventory_revision must not regress",
    ):
        current.validate_revision_successor(previous)


def test_snapshot_consistency_matrix_rejects_foreign_backend_instance() -> None:
    previous = snapshot(())
    current = replace(
        snapshot(
            (),
            inventory_revision=11,
            published_state_revision=21,
        ),
        backend=replace(
            previous.backend,
            backend_instance_id=FAMILY_H_FOREIGN_BACKEND_ID,
        ),
    )

    with pytest.raises(
        ValueError,
        match="snapshot belongs to a different backend instance",
    ):
        current.validate_revision_successor(previous)


FAMILY_H_DUAL_REVISION_CASES = [
    pytest.param(
        10,
        21,
        "inventory-owned changes require a newer inventory_revision",
        id="revision-reject-inventory-change-with-published-bump-only",
    ),
    pytest.param(
        11,
        20,
        "one published_state_revision must identify one immutable view",
        id="revision-reject-inventory-change-with-inventory-bump-only",
    ),
    pytest.param(
        11,
        21,
        None,
        id="revision-accept-inventory-change-with-both-bumps",
    ),
    pytest.param(
        50,
        90,
        None,
        id="revision-accept-inventory-change-with-non-adjacent-bumps",
    ),
]


@pytest.mark.parametrize(
    ("inventory_revision", "published_state_revision", "message"),
    FAMILY_H_DUAL_REVISION_CASES,
)
def test_snapshot_consistency_matrix_enforces_dual_revision_ownership(
    inventory_revision: int,
    published_state_revision: int,
    message: str | None,
) -> None:
    previous = snapshot((), sources=(source(),))
    renamed_source = replace(source(), name="Renamed matrix source")
    current = snapshot(
        (),
        sources=(renamed_source,),
        inventory_revision=inventory_revision,
        published_state_revision=published_state_revision,
    )

    assert current.inventory_projection != previous.inventory_projection
    assert (
        current.source_reconciliation_projection
        == previous.source_reconciliation_projection
    )
    if message is None:
        current.validate_revision_successor(previous)
        return

    with pytest.raises(ValueError, match=message):
        current.validate_revision_successor(previous)


FAMILY_H_RETAINED_POLICY_REVISION_CASES = [
    pytest.param(
        10,
        "inventory-owned changes require a newer inventory_revision",
        id="inventory-reject-retained-policy-change-without-bump",
    ),
    pytest.param(
        11,
        None,
        id="inventory-accept-retained-policy-change-with-bump",
    ),
]


@pytest.mark.parametrize(
    ("inventory_revision", "message"),
    FAMILY_H_RETAINED_POLICY_REVISION_CASES,
)
def test_snapshot_consistency_matrix_enforces_retained_policy_ownership(
    inventory_revision: int,
    message: str | None,
) -> None:
    previous = snapshot((resource(),), nodes=(node(),))
    current_resource = replace(
        resource(),
        retained_policy={"maintenance_window": "manual"},
    )
    current = snapshot(
        (current_resource,),
        nodes=(node(),),
        inventory_revision=inventory_revision,
        published_state_revision=21,
    )

    assert current.inventory_projection != previous.inventory_projection
    assert (
        current.source_reconciliation_projection
        == previous.source_reconciliation_projection
    )
    if message is None:
        current.validate_revision_successor(previous)
        return

    with pytest.raises(ValueError, match=message):
        current.validate_revision_successor(previous)


def test_snapshot_consistency_matrix_accepts_new_precommit_source_member(
) -> None:
    previous = snapshot(())
    initial_source_b = family_h_initial_source_b()
    current = snapshot(
        (),
        sources=(source(), initial_source_b),
        inventory_revision=11,
        published_state_revision=21,
    )

    current.validate_revision_successor(previous)

    assert current.inventory_projection != previous.inventory_projection
    assert current.sources_by_id[SOURCE_B_ID].last_committed_run_sequence is None


def test_snapshot_consistency_matrix_accepts_health_only_without_inventory_bump(
) -> None:
    previous = snapshot(())
    current = snapshot(
        (),
        sources=(time_expiry_source(),),
        inventory_revision=10,
        published_state_revision=90,
    )

    current.validate_revision_successor(previous)

    assert current.inventory_projection == previous.inventory_projection
    assert current.inventory_revision == previous.inventory_revision
    assert current.published_state_revision - previous.published_state_revision > 1


FAMILY_H_REVISION_PRIMITIVE_ACCEPT_CASES = [
    pytest.param(0, 1, id="primitive-accept-zero-inventory-revision"),
    pytest.param(10**12, 1, id="primitive-accept-large-inventory-revision"),
    pytest.param(0, 1, id="primitive-accept-one-published-revision"),
    pytest.param(0, 10**12, id="primitive-accept-large-published-revision"),
]


@pytest.mark.parametrize(
    ("inventory_revision", "published_state_revision"),
    FAMILY_H_REVISION_PRIMITIVE_ACCEPT_CASES,
)
def test_snapshot_consistency_matrix_accepts_revision_primitives(
    inventory_revision: int,
    published_state_revision: int,
) -> None:
    view = snapshot(
        (),
        inventory_revision=inventory_revision,
        published_state_revision=published_state_revision,
    )

    assert view.inventory_revision == inventory_revision
    assert view.published_state_revision == published_state_revision


FAMILY_H_REVISION_PRIMITIVE_REJECT_CASES = [
    pytest.param(
        -1,
        1,
        "inventory_revision must be a non-negative integer",
        id="primitive-reject-negative-inventory-revision",
    ),
    pytest.param(
        True,
        1,
        "inventory_revision must be a non-negative integer",
        id="primitive-reject-boolean-inventory-revision",
    ),
    pytest.param(
        0,
        0,
        "published_state_revision must be a positive integer",
        id="primitive-reject-zero-published-revision",
    ),
    pytest.param(
        0,
        False,
        "published_state_revision must be a positive integer",
        id="primitive-reject-boolean-published-revision",
    ),
]


@pytest.mark.parametrize(
    ("inventory_revision", "published_state_revision", "message"),
    FAMILY_H_REVISION_PRIMITIVE_REJECT_CASES,
)
def test_snapshot_consistency_matrix_rejects_revision_primitives(
    inventory_revision: int,
    published_state_revision: int,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        snapshot(
            (),
            inventory_revision=inventory_revision,
            published_state_revision=published_state_revision,
        )


def test_snapshot_consistency_matrix_rejects_empty_published_at() -> None:
    with pytest.raises(ValueError, match="published_at must not be empty"):
        replace(snapshot(()), published_at="")


FAMILY_H_DUPLICATE_IDENTITY_REJECT_CASES = [
    pytest.param(
        (source(), source()),
        (),
        (),
        "snapshot contains duplicate source identities",
        id="identity-reject-duplicate-source-id",
    ),
    pytest.param(
        (source(),),
        (node(), node()),
        (),
        "snapshot contains duplicate node identities",
        id="identity-reject-duplicate-node-id",
    ),
    pytest.param(
        (source(),),
        (),
        (resource(), resource()),
        "snapshot contains duplicate resource identities",
        id="identity-reject-duplicate-resource-id",
    ),
]


@pytest.mark.parametrize(
    ("sources", "nodes", "resources", "message"),
    FAMILY_H_DUPLICATE_IDENTITY_REJECT_CASES,
)
def test_snapshot_consistency_matrix_rejects_duplicate_top_level_identity(
    sources: tuple[InventorySourceSnapshot, ...],
    nodes: tuple[NodeSnapshot, ...],
    resources: tuple[ResourceSnapshot, ...],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        snapshot(resources, sources=sources, nodes=nodes)


FAMILY_H_UNKNOWN_SOURCE_REJECT_CASES = [
    pytest.param(
        (node(source_id=SOURCE_B_ID),),
        (),
        "node references an unknown inventory source",
        id="membership-reject-node-unknown-source",
    ),
    pytest.param(
        (),
        (
            family_f_current_resource(
                source_id=SOURCE_B_ID,
                binding_id=SUCCESSOR_BINDING_ID,
                generation=1,
            ),
        ),
        "resource references an unknown inventory source",
        id="membership-reject-resource-unknown-source",
    ),
]


@pytest.mark.parametrize(
    ("nodes", "resources", "message"),
    FAMILY_H_UNKNOWN_SOURCE_REJECT_CASES,
)
def test_snapshot_consistency_matrix_rejects_unknown_source_reference(
    nodes: tuple[NodeSnapshot, ...],
    resources: tuple[ResourceSnapshot, ...],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        snapshot(resources, nodes=nodes)


FAMILY_H_PRECOMMIT_INVENTORY_REJECT_CASES = [
    pytest.param(
        (node(),),
        (),
        id="precommit-reject-node-inventory",
    ),
    pytest.param(
        (),
        (family_f_current_resource(generation=1),),
        id="precommit-reject-resource-inventory",
    ),
]


@pytest.mark.parametrize(
    ("nodes", "resources"),
    FAMILY_H_PRECOMMIT_INVENTORY_REJECT_CASES,
)
def test_snapshot_consistency_matrix_rejects_precommit_source_inventory(
    nodes: tuple[NodeSnapshot, ...],
    resources: tuple[ResourceSnapshot, ...],
) -> None:
    with pytest.raises(
        ValueError,
        match="a source without a successful inventory commit cannot publish "
        "node or resource inventory",
    ):
        snapshot(
            resources,
            sources=(initial_source_with_run_provenance(),),
            nodes=nodes,
        )


def test_snapshot_consistency_matrix_accepts_precommit_source_coexistence(
) -> None:
    initial_source_b = family_h_initial_source_b()
    view = snapshot(
        (resource(),),
        sources=(source(), initial_source_b),
        nodes=(node(),),
    )

    assert view.resources_by_id[RESOURCE_ID].inventory_source_id == SOURCE_A_ID
    assert view.sources_by_id[SOURCE_B_ID].last_committed_run_sequence is None


FAMILY_H_ABSENT_NODE_REJECT_CASES = [
    pytest.param(
        resource(),
        id="graph-reject-current-node-absent",
    ),
    pytest.param(
        resource(**AMBIGUOUS_MISSING),
        id="graph-reject-last-known-node-absent",
    ),
]


@pytest.mark.parametrize("item", FAMILY_H_ABSENT_NODE_REJECT_CASES)
def test_snapshot_consistency_matrix_rejects_absent_node_reference(
    item: ResourceSnapshot,
) -> None:
    with pytest.raises(
        ValueError,
        match="resource references a node absent from the same snapshot",
    ):
        snapshot((item,))


FAMILY_H_CROSS_SOURCE_NODE_REJECT_CASES = [
    pytest.param(
        resource(),
        node(source_id=SOURCE_B_ID),
        id="graph-reject-current-node-crosses-source",
    ),
    pytest.param(
        resource(**AMBIGUOUS_MISSING),
        node(source_id=SOURCE_B_ID),
        id="graph-reject-last-known-node-crosses-source",
    ),
]


@pytest.mark.parametrize(
    ("item", "related_node"),
    FAMILY_H_CROSS_SOURCE_NODE_REJECT_CASES,
)
def test_snapshot_consistency_matrix_rejects_cross_source_node_relation(
    item: ResourceSnapshot,
    related_node: NodeSnapshot,
) -> None:
    with pytest.raises(
        ValueError,
        match="resource node relation crosses inventory sources",
    ):
        snapshot(
            (item,),
            sources=(source(), source(SOURCE_B_ID)),
            nodes=(related_node,),
        )


FAMILY_H_NODE_AVAILABILITY_ACCEPT_CASES = [
    pytest.param(
        resource(),
        node(),
        id="graph-accept-available-node-agreement",
    ),
    pytest.param(
        resource(
            current_node_id=NODE_UNAVAILABLE_ID,
            node_availability=NodeAvailability.UNAVAILABLE,
        ),
        node(NODE_UNAVAILABLE_ID, available=False),
        id="graph-accept-unavailable-node-agreement",
    ),
]


@pytest.mark.parametrize(
    ("item", "related_node"),
    FAMILY_H_NODE_AVAILABILITY_ACCEPT_CASES,
)
def test_snapshot_consistency_matrix_accepts_complete_same_view_graph(
    item: ResourceSnapshot,
    related_node: NodeSnapshot,
) -> None:
    view = snapshot((item,), nodes=(related_node,))

    assert view.sources_by_id[SOURCE_A_ID] == view.sources[0]
    assert view.nodes_by_id[related_node.node_id] == related_node
    assert view.resources_by_id[item.resource_id] == item
    assert item.current_node_id == related_node.node_id


FAMILY_H_NODE_AVAILABILITY_REJECT_CASES = [
    pytest.param(
        resource(node_availability=NodeAvailability.UNAVAILABLE),
        node(),
        id="graph-reject-available-node-published-unavailable",
    ),
    pytest.param(
        resource(
            current_node_id=NODE_UNAVAILABLE_ID,
            node_availability=NodeAvailability.AVAILABLE,
        ),
        node(NODE_UNAVAILABLE_ID, available=False),
        id="graph-reject-unavailable-node-published-available",
    ),
]


@pytest.mark.parametrize(
    ("item", "related_node"),
    FAMILY_H_NODE_AVAILABILITY_REJECT_CASES,
)
def test_snapshot_consistency_matrix_rejects_node_availability_disagreement(
    item: ResourceSnapshot,
    related_node: NodeSnapshot,
) -> None:
    with pytest.raises(
        ValueError,
        match="resource node availability disagrees with node record",
    ):
        snapshot((item,), nodes=(related_node,))


def test_snapshot_consistency_matrix_rejects_node_identity_source_move() -> None:
    previous = snapshot(
        (),
        sources=(source(), source(SOURCE_B_ID)),
        nodes=(node(),),
        inventory_revision=10,
        published_state_revision=20,
    )
    current = snapshot(
        (),
        sources=(source(), source(SOURCE_B_ID)),
        nodes=(node(source_id=SOURCE_B_ID),),
        inventory_revision=11,
        published_state_revision=21,
    )

    with pytest.raises(
        ValueError,
        match="node identity cannot move between sources",
    ):
        current.validate_revision_successor(previous)


def test_snapshot_consistency_matrix_defensively_freezes_top_level_collections(
) -> None:
    source_items = [source()]
    node_items = [node()]
    resource_items = [resource()]
    view = snapshot(
        resource_items,
        sources=source_items,
        nodes=node_items,
    )

    source_items.clear()
    node_items.clear()
    resource_items.clear()

    assert isinstance(view.sources, tuple)
    assert isinstance(view.nodes, tuple)
    assert isinstance(view.resources, tuple)
    assert tuple(item.inventory_source_id for item in view.sources) == (SOURCE_A_ID,)
    assert tuple(item.node_id for item in view.nodes) == (NODE_AVAILABLE_ID,)
    assert tuple(item.resource_id for item in view.resources) == (RESOURCE_ID,)
