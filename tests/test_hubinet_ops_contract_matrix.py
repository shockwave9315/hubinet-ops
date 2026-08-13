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
