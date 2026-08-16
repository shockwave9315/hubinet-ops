"""WAVE A1 Commit 2: sampled-absence provenance capture (ADR 0004 §12).

Extends only ordinary successful complete-baseline reconciliation. Does not
change the accepted missing/replacement identity algorithm (ADR 0001/0002);
these tests prove the pointer is captured/updated/cleared as a strictly
additive, non-security side effect alongside the unchanged existing
behavior.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.inventory import (
    AuthorityConflict,
    BaselineCompleteness,
    BaselineMode,
    DetailReadStatus,
    DiscoveredNode,
    DiscoveredResource,
    DiscoveryRunCompletionEvidence,
    InventoryAuthority,
    InventoryAuthorityStore,
    NormalizedDiscoverySnapshot,
    SourceAvailability,
)
from app.inventory.discovery import ProviderGuestLocatorSet, ProviderNodeScope

NOW = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)


def make_authority(tmp_path: Path):
    store = InventoryAuthorityStore(tmp_path / "authority.db", now=lambda: NOW)
    authority = InventoryAuthority(store, now=lambda: NOW)
    source = authority.create_inventory_source(
        provider_kind="proxmox",
        display_name="Primary",
        credential_reference="secret://inventory/primary",
        transport_locator="https://pve.example:8006",
    )
    return store, authority, source.source.inventory_source_id


def normalized_snapshot_for(run, source_id, *, resources, nodes=None):
    resources = tuple(resources)
    nodes = nodes if nodes is not None else (
        DiscoveredNode("pve-a", "online", True, NOW.isoformat(), {}),
    )
    provider_guest_locators = ProviderGuestLocatorSet._from_provider(
        tuple(
            {"vmid": vmid, "type": kind, "node": node_name}
            for vmid, kind, name, status, node_name, detail, facts in resources
        )
    )
    return NormalizedDiscoverySnapshot(
        run_id=run.run_id,
        discovery_run_sequence=run.discovery_run_sequence,
        inventory_source_id=source_id,
        expected_source_config_revision=run.expected_source_config_revision,
        endpoint_id=run.expected_endpoint_id,
        canonical_transport_locator=run.expected_canonical_transport_locator,
        canonicalization_contract_version=run.expected_canonicalization_contract_version,
        expected_transport_trust_revision=run.expected_transport_trust_revision,
        provider_contract_version=run.provider_contract_version,
        observed_at=NOW.isoformat(),
        source_facts={"release": "9.0"},
        source_availability=SourceAvailability.AVAILABLE,
        baseline_completeness=BaselineCompleteness.COMPLETE,
        baseline_mode=BaselineMode.CLUSTER,
        acl_topology_hash_before="acl",
        acl_topology_hash_after="acl",
        permission_snapshot_hash_before="perms",
        permission_snapshot_hash_after="perms",
        permission_coverage_complete=True,
        boundary_consistent=True,
        covered_nodes=tuple(node.external_node_name for node in nodes),
        failed_baseline_scopes=(),
        detail_summary={
            "ok_count": len(resources),
            "temporarily_unavailable_count": 0,
            "error_count": 0,
        },
        failed_detail_scopes=(),
        nodes=nodes,
        resources=tuple(
            DiscoveredResource(source_id, vmid, kind, name, status, node_name,
                               NOW.isoformat(), detail, facts)
            for vmid, kind, name, status, node_name, detail, facts in resources
        ),
        provider_node_scope=ProviderNodeScope._from_provider(
            BaselineMode.CLUSTER, tuple(sorted(node.external_node_name for node in nodes))
        ),
        provider_guest_locators=provider_guest_locators,
        expected_source_attestation_epoch=run.expected_source_attestation_epoch,
    )


def complete_snapshot(store, authority, source_id, *, resources, nodes=None):
    run = authority.issue_discovery_run(source_id, 1)
    authority.mark_discovery_run_running(source_id, run.run_id)
    normalized = normalized_snapshot_for(run, source_id, resources=resources, nodes=nodes)
    authority.finalize_successful_discovery_run(source_id, run.run_id, normalized)
    return run


def guest(vmid=101, kind="qemu", name="guest", node="pve-a", detail=DetailReadStatus.OK):
    return (vmid, kind, name, "running", node, detail, {"memory": 1024})


def test_first_transition_to_missing_creates_pointer(tmp_path: Path) -> None:
    store, authority, source_id = make_authority(tmp_path)
    complete_snapshot(store, authority, source_id, resources=(guest(),))
    resource = store.list_resources(source_id)[0]
    assert store.resource_absence_pointer(resource.resource_id) is None

    run_missing = complete_snapshot(store, authority, source_id, resources=())
    pointer = store.resource_absence_pointer(resource.resource_id)
    assert pointer is not None
    assert pointer.witness_run_id == run_missing.run_id
    assert pointer.witness_discovery_run_sequence == run_missing.discovery_run_sequence
    resource_after = store.list_resources(source_id)[0]
    assert resource_after.presence == "missing"
    assert resource_after.lifecycle == "quarantined"


def test_already_missing_reconfirm_overwrites_pointer_without_extra_revision(tmp_path: Path) -> None:
    store, authority, source_id = make_authority(tmp_path)
    complete_snapshot(store, authority, source_id, resources=(guest(),))
    resource = store.list_resources(source_id)[0]
    run_n = complete_snapshot(store, authority, source_id, resources=())
    pointer_n = store.resource_absence_pointer(resource.resource_id)
    resource_at_n = store.list_resources(source_id)[0]

    run_n1 = complete_snapshot(store, authority, source_id, resources=())
    pointer_n1 = store.resource_absence_pointer(resource.resource_id)
    resource_at_n1 = store.list_resources(source_id)[0]

    assert run_n1.discovery_run_sequence == run_n.discovery_run_sequence + 1
    assert pointer_n.witness_run_id == run_n.run_id
    assert pointer_n1.witness_run_id == run_n1.run_id
    assert pointer_n1.witness_run_id != pointer_n.witness_run_id
    # Overwritten, not appended: exactly one row for this resource.
    assert len([p for p in store.list_resource_absence_pointers(source_id) if p.resource_id == resource.resource_id]) == 1
    # No security-relevant transition on the already-missing resource.
    assert resource_at_n1.resource_continuity_revision == resource_at_n.resource_continuity_revision
    assert resource_at_n1.presence == "missing"
    assert resource_at_n1.lifecycle == "quarantined"


def test_pointer_survives_restart(tmp_path: Path) -> None:
    store, authority, source_id = make_authority(tmp_path)
    complete_snapshot(store, authority, source_id, resources=(guest(),))
    resource = store.list_resources(source_id)[0]
    run = complete_snapshot(store, authority, source_id, resources=())
    store.close()

    reopened = InventoryAuthorityStore(tmp_path / "authority.db")
    pointer = reopened.resource_absence_pointer(resource.resource_id)
    assert pointer is not None
    assert pointer.witness_run_id == run.run_id


def test_present_reappearance_clears_pointer(tmp_path: Path) -> None:
    store, authority, source_id = make_authority(tmp_path)
    complete_snapshot(store, authority, source_id, resources=(guest(),))
    resource = store.list_resources(source_id)[0]
    complete_snapshot(store, authority, source_id, resources=())
    assert store.resource_absence_pointer(resource.resource_id) is not None

    complete_snapshot(store, authority, source_id, resources=(guest(),))
    assert store.resource_absence_pointer(resource.resource_id) is None
    resource_after = store.list_resources(source_id)[0]
    assert resource_after.presence == "present"
    assert resource_after.lifecycle == "quarantined"  # ADR 0001: returning from gap
    assert resource_after.observational_continuity == "uncertain"


def test_partial_run_does_not_touch_pointer(tmp_path: Path) -> None:
    store, authority, source_id = make_authority(tmp_path)
    complete_snapshot(store, authority, source_id, resources=(guest(),))
    resource = store.list_resources(source_id)[0]
    run_missing = complete_snapshot(store, authority, source_id, resources=())
    pointer_before = store.resource_absence_pointer(resource.resource_id)

    run = authority.issue_discovery_run(source_id, 1)
    authority.finalize_failed_discovery_run(
        source_id,
        run.run_id,
        completion_evidence=DiscoveryRunCompletionEvidence(
            baseline_completeness=BaselineCompleteness.PARTIAL
        ),
        reason="baseline_incomplete",
    )
    pointer_after = store.resource_absence_pointer(resource.resource_id)
    assert pointer_after == pointer_before


def test_source_unavailable_run_does_not_touch_pointer(tmp_path: Path) -> None:
    store, authority, source_id = make_authority(tmp_path)
    complete_snapshot(store, authority, source_id, resources=(guest(),))
    resource = store.list_resources(source_id)[0]
    complete_snapshot(store, authority, source_id, resources=())
    pointer_before = store.resource_absence_pointer(resource.resource_id)

    run = authority.issue_discovery_run(source_id, 1)
    authority.finalize_failed_discovery_run(
        source_id,
        run.run_id,
        completion_evidence=DiscoveryRunCompletionEvidence(
            baseline_completeness=BaselineCompleteness.SOURCE_UNAVAILABLE
        ),
        reason="unreachable",
    )
    pointer_after = store.resource_absence_pointer(resource.resource_id)
    assert pointer_after == pointer_before


def test_stale_abandoned_worker_cannot_touch_pointer(tmp_path: Path) -> None:
    store, authority, source_id = make_authority(tmp_path)
    complete_snapshot(store, authority, source_id, resources=(guest(),))
    resource = store.list_resources(source_id)[0]
    complete_snapshot(store, authority, source_id, resources=())
    pointer_before = store.resource_absence_pointer(resource.resource_id)

    stale_run = authority.issue_discovery_run(source_id, 1)
    authority.mark_discovery_run_running(source_id, stale_run.run_id)
    authority.abandon_discovery_run(source_id, stale_run.run_id, reason="fenced_stale_worker")

    # A late/fenced worker attempting to finalize successfully is rejected
    # by the exact-ownership check before reconciliation ever runs again.
    stale_snapshot = normalized_snapshot_for(stale_run, source_id, resources=())
    with pytest.raises(AuthorityConflict, match="not the active source owner"):
        authority.finalize_successful_discovery_run(source_id, stale_run.run_id, stale_snapshot)

    pointer_after = store.resource_absence_pointer(resource.resource_id)
    assert pointer_after == pointer_before


def test_direct_replacement_clears_old_pointer(tmp_path: Path) -> None:
    store, authority, source_id = make_authority(tmp_path)
    complete_snapshot(store, authority, source_id, resources=(guest(kind="lxc"),))
    old = store.list_resources(source_id)[0]
    complete_snapshot(store, authority, source_id, resources=())  # goes missing
    assert store.resource_absence_pointer(old.resource_id) is not None

    complete_snapshot(store, authority, source_id, resources=(guest(kind="qemu"),))  # replaces
    assert store.resource_absence_pointer(old.resource_id) is None
    old_after = store.list_resources(source_id)[0]
    assert old_after.presence == "not_current"
    successor = store.list_resources(source_id)[1]
    assert store.resource_absence_pointer(successor.resource_id) is None


def test_pointer_run_always_belongs_to_exact_source(tmp_path: Path) -> None:
    store_a, authority_a, source_a = make_authority(tmp_path / "a")
    store_b, authority_b, source_b = make_authority(tmp_path / "b")
    complete_snapshot(store_a, authority_a, source_a, resources=(guest(),))
    complete_snapshot(store_b, authority_b, source_b, resources=(guest(),))
    resource_a = store_a.list_resources(source_a)[0]
    resource_b = store_b.list_resources(source_b)[0]

    run_a = complete_snapshot(store_a, authority_a, source_a, resources=())
    run_b = complete_snapshot(store_b, authority_b, source_b, resources=())

    pointer_a = store_a.resource_absence_pointer(resource_a.resource_id)
    pointer_b = store_b.resource_absence_pointer(resource_b.resource_id)
    assert pointer_a.inventory_source_id == source_a
    assert pointer_a.witness_run_id == run_a.run_id
    assert pointer_b.inventory_source_id == source_b
    assert pointer_b.witness_run_id == run_b.run_id


def test_rollback_leaves_inventory_and_pointer_unchanged(tmp_path: Path, monkeypatch) -> None:
    store, authority, source_id = make_authority(tmp_path)
    complete_snapshot(store, authority, source_id, resources=(guest(),))
    resource = store.list_resources(source_id)[0]
    complete_snapshot(store, authority, source_id, resources=())
    pointer_before = store.resource_absence_pointer(resource.resource_id)
    resource_before = store.list_resources(source_id)[0]

    def fail_after_reconciliation(connection, snapshot):
        raise RuntimeError("injected midpoint failure")

    monkeypatch.setattr(authority, "_after_reconciliation", fail_after_reconciliation)
    with pytest.raises(RuntimeError, match="injected midpoint failure"):
        complete_snapshot(store, authority, source_id, resources=())

    pointer_after = store.resource_absence_pointer(resource.resource_id)
    resource_after = store.list_resources(source_id)[0]
    assert pointer_after == pointer_before
    assert resource_after == resource_before


def test_pointer_reconfirm_does_not_fabricate_identity_or_security_transitions(tmp_path: Path) -> None:
    store, authority, source_id = make_authority(tmp_path)
    complete_snapshot(store, authority, source_id, resources=(guest(),))
    resource = store.list_resources(source_id)[0]
    complete_snapshot(store, authority, source_id, resources=())
    once_missing = store.list_resources(source_id)[0]

    complete_snapshot(store, authority, source_id, resources=())
    twice_missing = store.list_resources(source_id)[0]

    assert twice_missing.resource_id == once_missing.resource_id
    assert twice_missing.active_binding_id == once_missing.active_binding_id
    assert twice_missing.locator_generation == once_missing.locator_generation
    assert twice_missing.resource_continuity_revision == once_missing.resource_continuity_revision
    assert twice_missing.security_continuity == once_missing.security_continuity
    assert twice_missing.presence == "missing"
    assert twice_missing.lifecycle == "quarantined"
    bindings = [b for b in store.list_bindings(source_id) if b.valid_to_run_sequence is None]
    assert len(bindings) == 1
