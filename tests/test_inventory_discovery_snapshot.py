from __future__ import annotations

from dataclasses import FrozenInstanceError
import uuid

import pytest

from app.inventory import (
    BaselineCompleteness,
    BaselineMode,
    DetailReadStatus,
    DiscoveredNode,
    DiscoveredResource,
    NormalizedDiscoverySnapshot,
    SourceAvailability,
)
from app.inventory.discovery import ProviderGuestLocatorSet, ProviderNodeScope


SOURCE_ID = str(uuid.uuid4())
RUN_ID = str(uuid.uuid4())
ENDPOINT_ID = str(uuid.uuid4())


def snapshot(*, resources=(), nodes=None, source_id: str = SOURCE_ID, **changes):
    if nodes is None:
        nodes = (node(),)
    values = dict(
        run_id=RUN_ID,
        discovery_run_sequence=1,
        inventory_source_id=source_id,
        expected_source_config_revision=1,
        endpoint_id=ENDPOINT_ID,
        canonical_transport_locator="https://pve.example:8006",
        canonicalization_contract_version=1,
        expected_transport_trust_revision=1,
        provider_contract_version=1,
        observed_at="2026-08-14T12:00:00+00:00",
        source_facts={"release": "9.0", "nested": {"mode": "cluster"}},
        source_availability=SourceAvailability.AVAILABLE,
        baseline_completeness=BaselineCompleteness.COMPLETE,
        baseline_mode=BaselineMode.CLUSTER,
        acl_topology_hash_before="acl",
        acl_topology_hash_after="acl",
        permission_snapshot_hash_before="permissions",
        permission_snapshot_hash_after="permissions",
        permission_coverage_complete=True,
        boundary_consistent=True,
        covered_nodes=tuple(node.external_node_name for node in nodes),
        failed_baseline_scopes=(),
        detail_summary={"ok_count": len(resources), "temporarily_unavailable_count": 0, "error_count": 0},
        failed_detail_scopes=(),
        nodes=nodes,
        resources=resources,
    )
    values.update(changes)
    if "provider_node_scope" not in values:
        provider_names = tuple(
            sorted({node.external_node_name for node in values["nodes"]})
        )
        if not provider_names:
            provider_names = tuple(sorted(values["covered_nodes"])) or ("pve-a",)
        values["provider_node_scope"] = ProviderNodeScope._from_provider(
            values["baseline_mode"], provider_names
        )
    if "provider_guest_locators" not in values:
        values["provider_guest_locators"] = guest_locators(values["resources"])
    return NormalizedDiscoverySnapshot(**values)


def guest_locators(resources) -> ProviderGuestLocatorSet:
    """Build provider guest-locator provenance matching the given resources.

    Tests that need a mismatch between the provider baseline and the
    normalized resources must pass an explicit ``provider_guest_locators``
    (or build one from a different resource set via this helper) rather than
    relying on this default self-consistent derivation.
    """

    return ProviderGuestLocatorSet._from_provider(
        tuple(
            {"vmid": item.vmid, "type": item.resource_type, "node": item.current_node_name}
            for item in resources
        )
    )


def node(name="pve-a", observed_at="2026-08-14T12:00:00+00:00"):
    return DiscoveredNode(name, "online", True, observed_at, {"cpu": {"count": 8}})


def resource(
    vmid=100,
    kind="qemu",
    source_id=SOURCE_ID,
    node_name="pve-a",
    observed_at="2026-08-14T12:00:00+00:00",
    detail_status=DetailReadStatus.OK,
):
    return DiscoveredResource(source_id, vmid, kind, f"guest-{vmid}", "running", node_name,
                              observed_at, detail_status,
                              {"config": {"memory": 1024}})


def test_valid_mixed_qemu_lxc_snapshot_is_deeply_immutable_and_detached() -> None:
    source_facts = {"nested": {"items": [1, 2]}}
    current = snapshot(nodes=(node(),), resources=(resource(), resource(101, "lxc")), source_facts=source_facts)
    source_facts["nested"]["items"].append(3)
    assert current.source_facts["nested"]["items"] == (1, 2)
    with pytest.raises(TypeError):
        current.source_facts["new"] = True
    with pytest.raises(FrozenInstanceError):
        current.observed_at = "later"
    assert len(current.snapshot_hash) == 64


@pytest.mark.parametrize(
    "resources,nodes,source_id,match",
    (
        ((resource(source_id=str(uuid.uuid4())),), (node(),), SOURCE_ID, "wrong inventory source"),
        ((resource(), resource(100, "lxc")), (node(),), SOURCE_ID, "duplicate VMID"),
        ((resource(),), (node(), node()), SOURCE_ID, "duplicate external node"),
    ),
)
def test_normalized_snapshot_rejects_wrong_source_duplicate_slot_and_node(resources, nodes, source_id, match) -> None:
    with pytest.raises(ValueError, match=match):
        snapshot(resources=resources, nodes=nodes, source_id=source_id)


def test_normalized_resource_rejects_unsupported_type_and_malformed_detail_facts() -> None:
    with pytest.raises(ValueError, match="resource_type"):
        resource(kind="openvz")
    with pytest.raises(TypeError, match="JSON-like"):
        DiscoveredResource(SOURCE_ID, 100, "qemu", "guest", "running", None,
                           "2026-08-14T12:00:00+00:00", DetailReadStatus.ERROR,
                           {"bad": object()})


@pytest.mark.parametrize(
    "detail_status",
    (DetailReadStatus.TEMPORARILY_UNAVAILABLE, DetailReadStatus.ERROR),
)
def test_detail_failure_does_not_degrade_complete_baseline(detail_status) -> None:
    item = DiscoveredResource(SOURCE_ID, 100, "qemu", "guest", "unknown", "pve-a",
                              "2026-08-14T12:00:00+00:00",
                              detail_status, {})
    current = snapshot(
        nodes=(node(),),
        resources=(item,),
        detail_summary={
            "ok_count": 0,
            "temporarily_unavailable_count": int(
                detail_status is DetailReadStatus.TEMPORARILY_UNAVAILABLE
            ),
            "error_count": int(detail_status is DetailReadStatus.ERROR),
        },
        failed_detail_scopes=("/nodes/pve-a/qemu/100/config",),
    )
    assert current.baseline_completeness is BaselineCompleteness.COMPLETE


@pytest.mark.parametrize(
    "item,detail_summary",
    (
        (
            resource(detail_status=DetailReadStatus.ERROR),
            {"ok_count": 0, "temporarily_unavailable_count": 0, "error_count": "1"},
        ),
        (
            resource(detail_status=DetailReadStatus.ERROR),
            {"ok_count": 0, "temporarily_unavailable_count": 0, "error_count": -1},
        ),
        (
            resource(detail_status=DetailReadStatus.ERROR),
            {"ok_count": 1, "temporarily_unavailable_count": 0, "error_count": 0},
        ),
        (
            resource(),
            {"ok_count": 0, "temporarily_unavailable_count": 0, "error_count": 1},
        ),
        (
            resource(),
            {"ok_count": True, "temporarily_unavailable_count": 0, "error_count": 0},
        ),
        (
            resource(),
            {"ok_count": 1, "error_count": 0},
        ),
        (
            resource(),
            {
                "ok_count": 1,
                "temporarily_unavailable_count": 0,
                "error_count": 0,
                "unknown_count": 0,
            },
        ),
    ),
)
def test_detail_summary_rejects_malformed_or_contradictory_aggregates(
    item, detail_summary
) -> None:
    with pytest.raises(ValueError, match="detail_summary"):
        snapshot(resources=(item,), detail_summary=detail_summary)


def test_mixed_detail_summary_exactly_matches_resource_statuses() -> None:
    resources = (
        resource(100),
        resource(101, detail_status=DetailReadStatus.TEMPORARILY_UNAVAILABLE),
        resource(102, detail_status=DetailReadStatus.ERROR),
    )
    current = snapshot(
        resources=resources,
        detail_summary={
            "ok_count": 1,
            "temporarily_unavailable_count": 1,
            "error_count": 1,
        },
        failed_detail_scopes=(),
    )
    assert current.baseline_completeness is BaselineCompleteness.COMPLETE


@pytest.mark.parametrize(
    "failed_detail_scopes",
    (("",), (" /nodes/pve-a/qemu/100/config",), ("scope", "scope"), (1,)),
)
def test_failed_detail_scopes_require_unique_canonical_text(
    failed_detail_scopes,
) -> None:
    with pytest.raises(ValueError, match="failed_detail_scopes"):
        snapshot(failed_detail_scopes=failed_detail_scopes)


@pytest.mark.parametrize(
    "source_availability,failed_baseline_scopes",
    (
        (SourceAvailability.UNAVAILABLE, ()),
        (SourceAvailability.AVAILABLE, ("/cluster/resources",)),
        (SourceAvailability.UNAVAILABLE, ("/nodes",)),
    ),
)
def test_complete_baseline_rejects_contradictory_outcome_evidence(
    source_availability, failed_baseline_scopes
) -> None:
    with pytest.raises(
        ValueError, match="available source and no failed baseline scopes"
    ):
        snapshot(
            source_availability=source_availability,
            failed_baseline_scopes=failed_baseline_scopes,
        )


def test_complete_baseline_accepts_available_source_without_baseline_failures() -> None:
    current = snapshot(
        source_availability=SourceAvailability.AVAILABLE,
        failed_baseline_scopes=(),
    )
    assert current.baseline_completeness is BaselineCompleteness.COMPLETE


def test_complete_baseline_requires_matching_boundary_and_permission_coverage() -> None:
    with pytest.raises(ValueError, match="matching boundary"):
        snapshot(acl_topology_hash_after="changed")
    with pytest.raises(ValueError, match="consistent complete"):
        snapshot(permission_coverage_complete=False)


@pytest.mark.parametrize(
    "mode,nodes,covered_nodes",
    (
        (BaselineMode.CLUSTER, (), ()),
        (BaselineMode.STANDALONE, (), ()),
        (BaselineMode.CLUSTER, (node("pve-a"),), ()),
        (BaselineMode.CLUSTER, (), ("pve-a",)),
        (BaselineMode.CLUSTER, (node("pve-a"),), ("pve-b",)),
    ),
)
def test_complete_baseline_requires_exact_non_empty_node_coverage(
    mode, nodes, covered_nodes
) -> None:
    with pytest.raises(ValueError, match="covered node scope"):
        snapshot(baseline_mode=mode, nodes=nodes, covered_nodes=covered_nodes)


def test_complete_baseline_accepts_valid_mode_specific_node_coverage() -> None:
    standalone = snapshot(
        baseline_mode=BaselineMode.STANDALONE,
        nodes=(node("pve-a"),),
        covered_nodes=("pve-a",),
    )
    cluster = snapshot(
        nodes=(node("pve-b"), node("pve-a")),
        covered_nodes=("pve-a", "pve-b"),
    )
    assert standalone.covered_nodes == ("pve-a",)
    assert set(cluster.covered_nodes) == {"pve-a", "pve-b"}


def test_complete_baseline_rejects_two_truncated_snapshot_collections_against_provider_scope() -> None:
    with pytest.raises(ValueError, match="provider-established covered node scope"):
        snapshot(
            nodes=(node("pve-a"),),
            covered_nodes=("pve-a",),
            provider_node_scope=ProviderNodeScope._from_provider(
                BaselineMode.CLUSTER, ("pve-a", "pve-b")
            ),
        )


def provider_guest_locators(*rows) -> ProviderGuestLocatorSet:
    return ProviderGuestLocatorSet._from_provider(
        tuple({"vmid": vmid, "type": kind, "node": node_name} for vmid, kind, node_name in rows)
    )


def test_complete_baseline_requires_provider_guest_locator_provenance() -> None:
    with pytest.raises(ValueError, match="provider guest-locator provenance"):
        snapshot(
            resources=(resource(),),
            provider_guest_locators=None,
        )


def test_complete_baseline_rejects_omitted_provider_observed_guest() -> None:
    # The provider baseline observed VM 100, but the caller-supplied
    # normalized resources omit it entirely — this must never silently
    # produce a COMPLETE snapshot (a retained VM 100 could otherwise be
    # reconciled as missing even though the provider still observed it).
    with pytest.raises(ValueError, match="exactly match"):
        snapshot(
            resources=(),
            provider_guest_locators=provider_guest_locators((100, "qemu", "pve-a")),
        )


def test_complete_baseline_rejects_invented_guest_absent_from_provider_baseline() -> None:
    with pytest.raises(ValueError, match="exactly match"):
        snapshot(
            resources=(resource(100),),
            provider_guest_locators=provider_guest_locators(),
        )


def test_complete_baseline_rejects_same_vmid_with_wrong_type() -> None:
    with pytest.raises(ValueError, match="exactly match"):
        snapshot(
            resources=(resource(100, "lxc"),),
            provider_guest_locators=provider_guest_locators((100, "qemu", "pve-a")),
        )


def test_complete_baseline_rejects_same_vmid_type_with_wrong_node() -> None:
    with pytest.raises(ValueError, match="exactly match"):
        snapshot(
            nodes=(node("pve-a"), node("pve-b")),
            covered_nodes=("pve-a", "pve-b"),
            resources=(resource(100, node_name="pve-b"),),
            provider_guest_locators=provider_guest_locators((100, "qemu", "pve-a")),
        )


def test_complete_baseline_accepts_exact_cluster_guest_locator_match() -> None:
    current = snapshot(
        nodes=(node("pve-a"), node("pve-b")),
        covered_nodes=("pve-a", "pve-b"),
        resources=(resource(100, node_name="pve-a"), resource(101, "lxc", node_name="pve-b")),
        provider_guest_locators=provider_guest_locators(
            (100, "qemu", "pve-a"), (101, "lxc", "pve-b")
        ),
    )
    assert {(item.vmid, item.resource_type) for item in current.resources} == {
        (100, "qemu"),
        (101, "lxc"),
    }


def test_complete_baseline_accepts_exact_standalone_guest_locator_match() -> None:
    current = snapshot(
        baseline_mode=BaselineMode.STANDALONE,
        nodes=(node("pve-a"),),
        covered_nodes=("pve-a",),
        resources=(resource(100), resource(101, "lxc")),
        provider_guest_locators=provider_guest_locators(
            (100, "qemu", "pve-a"), (101, "lxc", "pve-a")
        ),
    )
    assert len(current.resources) == 2


def test_complete_baseline_accepts_guest_locators_regardless_of_ordering() -> None:
    # The comparison is a set comparison: neither the order guest_rows arrive
    # from the provider nor the order normalized resources are supplied in
    # may affect acceptance.
    resources = (resource(100, node_name="pve-a"), resource(101, "lxc", node_name="pve-b"))
    nodes = (node("pve-a"), node("pve-b"))
    forward = snapshot(
        nodes=nodes,
        covered_nodes=("pve-a", "pve-b"),
        resources=resources,
        provider_guest_locators=provider_guest_locators(
            (100, "qemu", "pve-a"), (101, "lxc", "pve-b")
        ),
    )
    reversed_order = snapshot(
        nodes=tuple(reversed(nodes)),
        covered_nodes=("pve-a", "pve-b"),
        resources=tuple(reversed(resources)),
        provider_guest_locators=provider_guest_locators(
            (101, "lxc", "pve-b"), (100, "qemu", "pve-a")
        ),
    )
    assert {(item.vmid, item.resource_type) for item in forward.resources} == {
        (item.vmid, item.resource_type) for item in reversed_order.resources
    }


def test_snapshot_hash_covers_security_and_issuance_provenance() -> None:
    original = snapshot()
    changed = snapshot(
        permission_snapshot_hash_before="other",
        permission_snapshot_hash_after="other",
    )
    assert changed.snapshot_hash != original.snapshot_hash


def test_snapshot_hash_commits_to_canonical_provider_node_scope() -> None:
    single = snapshot(nodes=(node("pve-a"),), covered_nodes=("pve-a",))
    multi = snapshot(
        nodes=(node("pve-b"), node("pve-a")),
        covered_nodes=("pve-b", "pve-a"),
    )
    assert single.snapshot_hash != multi.snapshot_hash
    assert multi.covered_nodes == ("pve-a", "pve-b")


def test_snapshot_hash_covers_nested_observation_timestamps() -> None:
    original = snapshot(nodes=(node(),), resources=(resource(),))
    changed_node = snapshot(
        nodes=(node(observed_at="2026-08-14T11:30:00+00:00"),),
        resources=(resource(),),
    )
    changed_resource = snapshot(
        nodes=(node(),),
        resources=(resource(observed_at="2026-08-14T11:30:00+00:00"),),
    )
    assert changed_node.snapshot_hash != original.snapshot_hash
    assert changed_resource.snapshot_hash != original.snapshot_hash


def test_snapshot_hash_is_stable_with_identical_nested_observation_evidence() -> None:
    nodes = (
        node("pve-a", "2026-08-14T11:00:00+00:00"),
        node("pve-b", "2026-08-14T11:30:00+00:00"),
    )
    resources = (
        resource(100, node_name="pve-a", observed_at="2026-08-14T11:15:00+00:00"),
        resource(101, "lxc", node_name="pve-b", observed_at="2026-08-14T11:45:00+00:00"),
    )
    first = snapshot(nodes=nodes, resources=resources)
    second = snapshot(nodes=tuple(reversed(nodes)), resources=tuple(resources))
    assert first.to_json_value() == second.to_json_value()
    assert first.snapshot_hash == second.snapshot_hash
