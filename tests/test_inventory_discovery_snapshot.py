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


SOURCE_ID = str(uuid.uuid4())
RUN_ID = str(uuid.uuid4())
ENDPOINT_ID = str(uuid.uuid4())


def snapshot(*, resources=(), nodes=(), source_id: str = SOURCE_ID, **changes):
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
    return NormalizedDiscoverySnapshot(**values)


def node(name="pve-a"):
    return DiscoveredNode(name, "online", True, "2026-08-14T12:00:00+00:00", {"cpu": {"count": 8}})


def resource(vmid=100, kind="qemu", source_id=SOURCE_ID, node_name="pve-a"):
    return DiscoveredResource(source_id, vmid, kind, f"guest-{vmid}", "running", node_name,
                              "2026-08-14T12:00:00+00:00", DetailReadStatus.OK,
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


def test_detail_failure_does_not_degrade_complete_baseline() -> None:
    item = DiscoveredResource(SOURCE_ID, 100, "qemu", "guest", "unknown", "pve-a",
                              "2026-08-14T12:00:00+00:00",
                              DetailReadStatus.TEMPORARILY_UNAVAILABLE, {})
    current = snapshot(nodes=(node(),), resources=(item,),
                       detail_summary={"ok_count": 0, "temporarily_unavailable_count": 1, "error_count": 0},
                       failed_detail_scopes=("/nodes/pve-a/qemu/100/config",))
    assert current.baseline_completeness is BaselineCompleteness.COMPLETE


def test_complete_baseline_requires_matching_boundary_and_permission_coverage() -> None:
    with pytest.raises(ValueError, match="matching boundary"):
        snapshot(acl_topology_hash_after="changed")
    with pytest.raises(ValueError, match="consistent complete"):
        snapshot(permission_coverage_complete=False)


def test_snapshot_hash_covers_security_and_issuance_provenance() -> None:
    original = snapshot()
    changed = snapshot(
        permission_snapshot_hash_before="other",
        permission_snapshot_hash_after="other",
    )
    assert changed.snapshot_hash != original.snapshot_hash
