from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.inventory import (
    BaselineCompleteness,
    BaselineMode,
    DetailReadStatus,
    DiscoveryRunCompletionEvidence,
    DiscoveredNode,
    DiscoveredResource,
    InventoryAuthority,
    InventoryAuthorityStore,
    NormalizedDiscoverySnapshot,
    SourceAvailability,
    evaluate_permission_coverage,
)
from app.inventory.discovery import ProviderGuestLocatorSet, ProviderNodeScope


NOW = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)


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


def normalized_snapshot_for(
    run,
    source_id,
    *,
    resources,
    nodes=None,
    mode=BaselineMode.CLUSTER,
    provider_node_names=None,
    provider_guest_locators=None,
    source_availability=SourceAvailability.AVAILABLE,
    failed_baseline_scopes=(),
):
    resources = tuple(resources)
    nodes = nodes if nodes is not None else (
        DiscoveredNode("pve-a", "online", True, NOW.isoformat(), {}),
    )
    provider_node_names = provider_node_names or tuple(
        node.external_node_name for node in nodes
    )
    if provider_guest_locators is None:
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
        source_availability=source_availability,
        baseline_completeness=BaselineCompleteness.COMPLETE,
        baseline_mode=mode,
        acl_topology_hash_before="acl",
        acl_topology_hash_after="acl",
        permission_snapshot_hash_before="perms",
        permission_snapshot_hash_after="perms",
        permission_coverage_complete=True,
        boundary_consistent=True,
        covered_nodes=tuple(node.external_node_name for node in nodes),
        failed_baseline_scopes=failed_baseline_scopes,
        detail_summary={
            "ok_count": sum(
                detail is DetailReadStatus.OK
                for _, _, _, _, _, detail, _ in resources
            ),
            "temporarily_unavailable_count": sum(
                detail is DetailReadStatus.TEMPORARILY_UNAVAILABLE
                for _, _, _, _, _, detail, _ in resources
            ),
            "error_count": sum(
                detail is DetailReadStatus.ERROR
                for _, _, _, _, _, detail, _ in resources
            ),
        },
        failed_detail_scopes=(),
        nodes=nodes,
        resources=tuple(
            DiscoveredResource(source_id, vmid, kind, name, status, node_name,
                               NOW.isoformat(), detail, facts)
            for vmid, kind, name, status, node_name, detail, facts in resources
        ),
        provider_node_scope=ProviderNodeScope._from_provider(
            mode, tuple(sorted(provider_node_names))
        ),
        provider_guest_locators=provider_guest_locators,
    )


def complete_snapshot(store, authority, source_id, *, resources, nodes=None, mode=BaselineMode.CLUSTER):
    run = authority.issue_discovery_run(source_id, 1)
    authority.mark_discovery_run_running(source_id, run.run_id)
    normalized = normalized_snapshot_for(
        run, source_id, resources=resources, nodes=nodes, mode=mode
    )
    authority.finalize_successful_discovery_run(source_id, run.run_id, normalized)
    return run, normalized


def guest(vmid=100, kind="qemu", name="guest", node="pve-a", detail=DetailReadStatus.OK):
    return (vmid, kind, name, "running", node, detail, {"memory": 1024})


def test_first_observation_allocates_backend_identity_binding_and_generation(tmp_path: Path) -> None:
    store, authority, source_id = make_authority(tmp_path)
    before = store.backend_instance()
    run, _ = complete_snapshot(store, authority, source_id, resources=(guest(),))
    resources = store.list_resources(source_id)
    bindings = store.list_bindings(source_id)
    after = store.backend_instance()
    assert len(resources) == len(bindings) == 1
    assert resources[0].active_binding_id == bindings[0].binding_id
    assert resources[0].locator_generation == bindings[0].locator_generation == 1
    assert resources[0].security_continuity == "unverified"
    assert resources[0].state_level == "discovered"
    assert store.source_state(source_id).source.last_committed_run_sequence == run.discovery_run_sequence
    assert after.inventory_revision == before.inventory_revision + 1
    # Issuance, running, and successful reconciliation are three visible views.
    assert after.published_state_revision == before.published_state_revision + 3


def test_stable_rename_and_node_migration_preserve_resource_and_binding_identity(tmp_path: Path) -> None:
    store, authority, source_id = make_authority(tmp_path)
    complete_snapshot(store, authority, source_id, resources=(guest(),))
    old = store.list_resources(source_id)[0]
    nodes = (DiscoveredNode("pve-b", "online", True, NOW.isoformat(), {}),)
    complete_snapshot(store, authority, source_id, resources=(guest(name="renamed", node="pve-b"),), nodes=nodes)
    current = store.list_resources(source_id)[0]
    assert current.resource_id == old.resource_id
    assert current.active_binding_id == old.active_binding_id
    assert current.locator_generation == old.locator_generation
    assert current.name == "renamed"
    assert current.current_node_id != old.current_node_id


def test_unresolved_current_relation_preserves_and_then_clears_last_known_node(
    tmp_path: Path,
) -> None:
    store, authority, source_id = make_authority(tmp_path)
    complete_snapshot(store, authority, source_id, resources=(guest(node="pve-a"),))
    original = store.list_resources(source_id)[0]
    pve_a_node_id = original.current_node_id

    pve_a = (DiscoveredNode("pve-a", "online", True, NOW.isoformat(), {}),)
    complete_snapshot(
        store,
        authority,
        source_id,
        resources=(guest(node=None),),
        nodes=pve_a,
    )
    unresolved = store.list_resources(source_id)[0]
    assert unresolved.resource_id == original.resource_id
    assert unresolved.active_binding_id == original.active_binding_id
    assert unresolved.locator_generation == original.locator_generation
    assert unresolved.presence == "present"
    assert unresolved.node_availability == "unresolved"
    assert unresolved.current_node_id is None
    assert unresolved.last_known_node_id == pve_a_node_id

    # Repeating an unresolved observation retains the same presentation history.
    complete_snapshot(
        store,
        authority,
        source_id,
        resources=(guest(node=None),),
        nodes=pve_a,
    )
    assert store.list_resources(source_id)[0].last_known_node_id == pve_a_node_id

    # Resolving to the same node makes it current again and clears last-known.
    complete_snapshot(
        store,
        authority,
        source_id,
        resources=(guest(node="pve-a"),),
        nodes=pve_a,
    )
    resolved_same = store.list_resources(source_id)[0]
    assert resolved_same.current_node_id == pve_a_node_id
    assert resolved_same.last_known_node_id is None

    # A later unresolved relation may resolve to a different retained node.
    complete_snapshot(
        store,
        authority,
        source_id,
        resources=(guest(node=None),),
        nodes=pve_a,
    )
    pve_b = (DiscoveredNode("pve-b", "online", True, NOW.isoformat(), {}),)
    complete_snapshot(
        store,
        authority,
        source_id,
        resources=(guest(node="pve-b"),),
        nodes=pve_b,
    )
    resolved_other = store.list_resources(source_id)[0]
    assert resolved_other.resource_id == original.resource_id
    assert resolved_other.active_binding_id == original.active_binding_id
    assert resolved_other.locator_generation == original.locator_generation
    assert resolved_other.current_node_id != pve_a_node_id
    assert resolved_other.last_known_node_id is None


def test_first_unresolved_observation_has_no_invented_last_known_node(tmp_path: Path) -> None:
    store, authority, source_id = make_authority(tmp_path)
    complete_snapshot(
        store,
        authority,
        source_id,
        resources=(guest(node=None),),
        nodes=(DiscoveredNode("pve-a", "online", True, NOW.isoformat(), {}),),
    )
    resource = store.list_resources(source_id)[0]
    assert resource.current_node_id is None
    assert resource.last_known_node_id is None
    assert resource.node_availability == "unresolved"


def test_invalid_complete_node_coverage_cannot_mark_retained_resource_missing(
    tmp_path: Path,
) -> None:
    store, authority, source_id = make_authority(tmp_path)
    nodes = (
        DiscoveredNode("pve-a", "online", True, NOW.isoformat(), {}),
        DiscoveredNode("pve-b", "online", True, NOW.isoformat(), {}),
    )
    complete_snapshot(
        store,
        authority,
        source_id,
        resources=(guest(vmid=100, node="pve-a"), guest(vmid=101, node="pve-b")),
        nodes=nodes,
    )
    retained = next(
        resource for resource in store.list_resources(source_id) if resource.vmid == 101
    )
    retained_binding = next(
        binding for binding in store.list_bindings(source_id) if binding.vmid == 101
    )

    run = authority.issue_discovery_run(source_id, 1)
    authority.mark_discovery_run_running(source_id, run.run_id)
    before_backend = store.backend_instance()
    before_resources = store.list_resources(source_id)
    before_bindings = store.list_bindings(source_id)
    with pytest.raises(ValueError, match="provider-established covered node scope"):
        normalized_snapshot_for(
            run,
            source_id,
            resources=(guest(vmid=100, node="pve-a"),),
            nodes=(nodes[0],),
            provider_node_names=("pve-a", "pve-b"),
        )

    current = next(
        resource for resource in store.list_resources(source_id) if resource.vmid == 101
    )
    assert current.resource_id == retained.resource_id
    assert current.presence == "present"
    assert current.active_binding_id == retained.active_binding_id
    assert current.locator_generation == retained.locator_generation
    assert current.successor_resource_id is None
    assert store.list_resources(source_id) == before_resources
    assert store.list_bindings(source_id) == before_bindings
    assert retained_binding.valid_to_run_sequence is None
    assert store.backend_instance() == before_backend
    assert store.discovery_run(run.run_id).lifecycle.value == "running"
    assert store.source_state(source_id).source.active_discovery_run_id == run.run_id
    with store._transaction() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM resource_terminations WHERE inventory_source_id=?",
            (source_id,),
        ).fetchone()[0] == 0


def test_provider_observed_guest_omission_cannot_mark_retained_resource_missing(
    tmp_path: Path,
) -> None:
    store, authority, source_id = make_authority(tmp_path)
    complete_snapshot(
        store,
        authority,
        source_id,
        resources=(guest(vmid=100, node="pve-a"),),
    )
    retained = store.list_resources(source_id)[0]
    retained_binding = store.list_bindings(source_id)[0]
    before_inventory_revision = store.backend_instance().inventory_revision

    run = authority.issue_discovery_run(source_id, 1)
    authority.mark_discovery_run_running(source_id, run.run_id)
    # The provider baseline still observed VM 100; the caller-supplied
    # normalized resources omit it. This must be rejected fail-closed at
    # construction, before it can ever reach reconciliation.
    with pytest.raises(
        ValueError, match="normalized resources to exactly match"
    ):
        normalized_snapshot_for(
            run,
            source_id,
            resources=(),
            provider_guest_locators=ProviderGuestLocatorSet._from_provider(
                ({"vmid": 100, "type": "qemu", "node": "pve-a"},)
            ),
        )

    current = store.list_resources(source_id)[0]
    assert current.resource_id == retained.resource_id
    assert current.presence == "present"
    assert current.active_binding_id == retained.active_binding_id
    assert current.locator_generation == retained.locator_generation
    assert current.successor_resource_id is None
    assert store.list_bindings(source_id)[0] == retained_binding
    assert store.backend_instance().inventory_revision == before_inventory_revision
    assert store.discovery_run(run.run_id).lifecycle.value == "running"
    with store._transaction() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM resource_terminations WHERE inventory_source_id=?",
            (source_id,),
        ).fetchone()[0] == 0


def test_complete_multinode_scope_reconciles_and_persists_exact_audit(
    tmp_path: Path,
) -> None:
    store, authority, source_id = make_authority(tmp_path)
    nodes = (
        DiscoveredNode("pve-b", "online", True, NOW.isoformat(), {}),
        DiscoveredNode("pve-a", "online", True, NOW.isoformat(), {}),
    )
    run, normalized = complete_snapshot(
        store,
        authority,
        source_id,
        resources=(guest(vmid=100, node="pve-a"), guest(vmid=101, node="pve-b")),
        nodes=nodes,
    )
    resources = store.list_resources(source_id)
    completed = store.discovery_run(run.run_id)
    assert {resource.vmid for resource in resources} == {100, 101}
    assert all(resource.presence == "present" for resource in resources)
    assert normalized.covered_nodes == ("pve-a", "pve-b")
    assert completed.covered_nodes == normalized.covered_nodes
    assert completed.normalized_snapshot_hash == normalized.snapshot_hash

    store.close()
    restarted = InventoryAuthorityStore(tmp_path / "authority.db").discovery_run(
        run.run_id
    )
    assert restarted.covered_nodes == ("pve-a", "pve-b")
    assert restarted.normalized_snapshot_hash == normalized.snapshot_hash


@pytest.mark.parametrize(
    "source_availability,failed_baseline_scopes",
    (
        (SourceAvailability.AVAILABLE, ("/cluster/resources",)),
        (SourceAvailability.UNAVAILABLE, ()),
    ),
)
def test_contradictory_complete_outcome_cannot_mark_retained_resource_missing(
    tmp_path: Path,
    source_availability: SourceAvailability,
    failed_baseline_scopes: tuple[str, ...],
) -> None:
    store, authority, source_id = make_authority(tmp_path)
    complete_snapshot(store, authority, source_id, resources=(guest(),))
    retained = store.list_resources(source_id)[0]
    before_inventory_revision = store.backend_instance().inventory_revision

    run = authority.issue_discovery_run(source_id, 1)
    authority.mark_discovery_run_running(source_id, run.run_id)
    with pytest.raises(
        ValueError, match="available source and no failed baseline scopes"
    ):
        normalized_snapshot_for(
            run,
            source_id,
            resources=(),
            source_availability=source_availability,
            failed_baseline_scopes=failed_baseline_scopes,
        )

    current = store.list_resources(source_id)[0]
    assert current.resource_id == retained.resource_id
    assert current.active_binding_id == retained.active_binding_id
    assert current.locator_generation == retained.locator_generation
    assert current.presence == "present"
    assert store.backend_instance().inventory_revision == before_inventory_revision


def test_unresolved_history_survives_missing_and_direct_replacement(tmp_path: Path) -> None:
    store, authority, source_id = make_authority(tmp_path)
    complete_snapshot(store, authority, source_id, resources=(guest(node="pve-a"),))
    old = store.list_resources(source_id)[0]
    pve_a_node_id = old.current_node_id
    pve_a = (DiscoveredNode("pve-a", "online", True, NOW.isoformat(), {}),)
    complete_snapshot(
        store,
        authority,
        source_id,
        resources=(guest(node=None),),
        nodes=pve_a,
    )
    complete_snapshot(store, authority, source_id, resources=(), nodes=pve_a)
    missing = store.list_resources(source_id)[0]
    assert missing.last_known_node_id == pve_a_node_id

    complete_snapshot(
        store,
        authority,
        source_id,
        resources=(guest(node=None),),
        nodes=pve_a,
    )
    unresolved_predecessor = store.list_resources(source_id)[0]
    assert unresolved_predecessor.presence == "present"
    assert unresolved_predecessor.node_availability == "unresolved"
    assert unresolved_predecessor.last_known_node_id == pve_a_node_id

    complete_snapshot(
        store,
        authority,
        source_id,
        resources=(guest(kind="lxc", node=None),),
        nodes=pve_a,
    )
    resources = store.list_resources(source_id)
    predecessor = next(item for item in resources if item.resource_id == old.resource_id)
    successor = next(item for item in resources if item.active_binding_id is not None)
    assert predecessor.last_known_node_id == pve_a_node_id
    assert successor.current_node_id is None
    assert successor.last_known_node_id is None


def test_detail_error_preserves_present_and_complete_absence_marks_missing(tmp_path: Path) -> None:
    store, authority, source_id = make_authority(tmp_path)
    complete_snapshot(store, authority, source_id, resources=(guest(),))
    original = store.list_resources(source_id)[0]
    complete_snapshot(store, authority, source_id, resources=(guest(detail=DetailReadStatus.ERROR),))
    errored = store.list_resources(source_id)[0]
    assert errored.presence == "present"
    assert errored.detail_status == "error"
    complete_snapshot(store, authority, source_id, resources=())
    missing = store.list_resources(source_id)[0]
    assert missing.resource_id == original.resource_id
    assert missing.active_binding_id == original.active_binding_id
    assert missing.locator_generation == original.locator_generation
    assert missing.presence == "missing"
    assert missing.detail_status == "not_applicable"
    assert missing.lifecycle == "quarantined"


def test_partial_failed_run_does_not_create_missing_transition(tmp_path: Path) -> None:
    store, authority, source_id = make_authority(tmp_path)
    complete_snapshot(store, authority, source_id, resources=(guest(),))
    run = authority.issue_discovery_run(source_id, 1)
    authority.finalize_failed_discovery_run(
        source_id,
        run.run_id,
        completion_evidence=DiscoveryRunCompletionEvidence(
            BaselineCompleteness.PARTIAL
        ),
        reason="locator baseline page failed",
    )
    assert store.list_resources(source_id)[0].presence == "present"


def test_non_propagating_guest_tree_cannot_mark_retained_resource_missing(
    tmp_path: Path,
) -> None:
    store, authority, source_id = make_authority(tmp_path)
    complete_snapshot(store, authority, source_id, resources=(guest(),))
    run = authority.issue_discovery_run(source_id, 1)
    permissions = {
        "/": {"Sys.Audit": 0},
        "/access": {"Sys.Audit": 0},
        "/nodes": {"Sys.Audit": 1},
        "/nodes/pve-a": {"Sys.Audit": 0},
        "/vms": {"VM.Audit": 0},
    }
    assert not evaluate_permission_coverage(permissions, node_names=("pve-a",))
    authority.finalize_failed_discovery_run(
        source_id,
        run.run_id,
        completion_evidence=DiscoveryRunCompletionEvidence(
            BaselineCompleteness.CONFIGURATION_ERROR
        ),
        reason="source-wide VM permission does not propagate",
    )
    assert store.list_resources(source_id)[0].presence == "present"


def test_missing_return_is_same_quarantined_identity_without_generation_split(tmp_path: Path) -> None:
    store, authority, source_id = make_authority(tmp_path)
    complete_snapshot(store, authority, source_id, resources=(guest(),))
    complete_snapshot(store, authority, source_id, resources=())
    old = store.list_resources(source_id)[0]
    complete_snapshot(store, authority, source_id, resources=(guest(),))
    returned = store.list_resources(source_id)[0]
    assert returned.resource_id == old.resource_id
    assert returned.active_binding_id == old.active_binding_id
    assert returned.locator_generation == old.locator_generation
    assert returned.presence == "present"
    assert returned.lifecycle == "quarantined"
    assert returned.observational_continuity == "uncertain"


def test_type_change_is_atomic_direct_replacement_with_retained_terminal_history(tmp_path: Path) -> None:
    store, authority, source_id = make_authority(tmp_path)
    complete_snapshot(store, authority, source_id, resources=(guest(kind="qemu"),))
    old = store.list_resources(source_id)[0]
    complete_snapshot(store, authority, source_id, resources=(guest(kind="lxc"),))
    resources = store.list_resources(source_id)
    old_after = next(item for item in resources if item.resource_id == old.resource_id)
    successor = next(item for item in resources if item.active_binding_id is not None)
    assert old_after.active_binding_id is None
    assert old_after.presence == "not_current"
    assert old_after.lifecycle == "retired"
    assert old_after.observational_continuity == "replaced"
    assert old_after.successor_resource_id == successor.resource_id
    assert successor.resource_id != old.resource_id
    assert successor.locator_generation == old.locator_generation + 1
    assert successor.resource_type == "lxc"
    assert successor.security_continuity == "unverified"
    assert len([binding for binding in store.list_bindings(source_id) if binding.valid_to_run_sequence is None]) == 1
    assert all(item.presence != "confirmed_removed" for item in resources)


def test_direct_replacement_handoff_rolls_back_at_internal_midpoint(
    tmp_path: Path, monkeypatch
) -> None:
    store, authority, source_id = make_authority(tmp_path)
    complete_snapshot(store, authority, source_id, resources=(guest(kind="qemu"),))
    old_resource = store.list_resources(source_id)[0]
    old_binding = store.list_bindings(source_id)[0]

    def fail_after_handoff(connection, snapshot):
        raise RuntimeError("injected replacement midpoint")

    monkeypatch.setattr(authority, "_after_reconciliation", fail_after_handoff)
    with pytest.raises(RuntimeError, match="replacement midpoint"):
        complete_snapshot(store, authority, source_id, resources=(guest(kind="lxc"),))
    assert store.list_resources(source_id) == (old_resource,)
    assert store.list_bindings(source_id) == (old_binding,)
    assert store.source_state(source_id).source.active_discovery_run_id is not None


# --- G8: standalone <-> cluster baseline_mode transition -------------------
#
# `baseline_mode` is per-run observed provenance recorded on each completed
# `discovery_runs` record (see ADR 0002's `ClusterStatusResult`/provider-v1
# contract and 0.5-inventory-model.md: "mutable presentation/observed:
# display name oraz cluster/standalone facts"). It is not part of
# `inventory_source_id`, `InventorySource` has no durable mode field, and a
# newly observed mode is not a controlled source configuration change.
# These tests lock that a legitimate standalone<->cluster transition never
# by itself triggers identity replacement, quarantine/missing, or a
# `source_config_revision` bump.


@pytest.mark.parametrize(
    "first_mode,second_mode",
    (
        (BaselineMode.STANDALONE, BaselineMode.CLUSTER),
        (BaselineMode.CLUSTER, BaselineMode.STANDALONE),
    ),
)
def test_standalone_cluster_mode_transition_preserves_identity_and_source_revision(
    tmp_path: Path, first_mode: BaselineMode, second_mode: BaselineMode
) -> None:
    store, authority, source_id = make_authority(tmp_path)
    node_a = (DiscoveredNode("pve-a", "online", True, NOW.isoformat(), {}),)

    run1, _ = complete_snapshot(
        store,
        authority,
        source_id,
        resources=(guest(vmid=100, kind="qemu", node="pve-a"),),
        nodes=node_a,
        mode=first_mode,
    )
    before = store.list_resources(source_id)[0]
    before_binding = store.list_bindings(source_id)[0]
    before_source = store.source_state(source_id).source

    run2, _ = complete_snapshot(
        store,
        authority,
        source_id,
        resources=(guest(vmid=100, kind="qemu", node="pve-a"),),
        nodes=node_a,
        mode=second_mode,
    )
    after = store.list_resources(source_id)[0]
    after_binding = store.list_bindings(source_id)[0]
    after_source = store.source_state(source_id).source

    # Same backend source identity throughout the transition.
    assert after_source.inventory_source_id == before_source.inventory_source_id == source_id

    # baseline_mode is per-run observed provenance, not source identity: each
    # completed run records the mode actually observed on that run.
    assert store.discovery_run(run1.run_id).baseline_mode == first_mode.value
    assert store.discovery_run(run2.run_id).baseline_mode == second_mode.value

    # Recording a new observed mode alone is not a controlled source
    # configuration change and must not bump source_config_revision.
    assert after_source.source_config_revision == before_source.source_config_revision

    # The same source-local VMID with the same resource_type remains the
    # same incarnation: no replacement, no generation bump.
    assert after.resource_id == before.resource_id
    assert after.active_binding_id == before.active_binding_id == before_binding.binding_id
    assert after_binding.binding_id == before_binding.binding_id
    assert after.locator_generation == before.locator_generation == 1
    assert after.successor_resource_id is None

    # No quarantine/missing transition occurs solely because mode changed.
    assert after.presence == "present"
    assert after.lifecycle == "active"
    assert after.observational_continuity == "consistent"
    assert after.security_continuity == before.security_continuity == "unverified"


def test_cluster_to_standalone_mode_transition_with_topology_change_updates_node_facts_not_identity(
    tmp_path: Path,
) -> None:
    store, authority, source_id = make_authority(tmp_path)
    cluster_nodes = (
        DiscoveredNode("pve-a", "online", True, NOW.isoformat(), {}),
        DiscoveredNode("pve-b", "online", True, NOW.isoformat(), {}),
    )
    complete_snapshot(
        store,
        authority,
        source_id,
        resources=(guest(vmid=100, kind="qemu", node="pve-a"),),
        nodes=cluster_nodes,
        mode=BaselineMode.CLUSTER,
    )
    before = store.list_resources(source_id)[0]
    before_source = store.source_state(source_id).source
    pve_b_before = next(
        node for node in store.list_nodes(source_id) if node.external_node_name == "pve-b"
    )
    assert pve_b_before.available is True

    standalone_nodes = (DiscoveredNode("pve-a", "online", True, NOW.isoformat(), {}),)
    complete_snapshot(
        store,
        authority,
        source_id,
        resources=(guest(vmid=100, kind="qemu", node="pve-a"),),
        nodes=standalone_nodes,
        mode=BaselineMode.STANDALONE,
    )
    after = store.list_resources(source_id)[0]
    after_source = store.source_state(source_id).source

    # Legitimate topology change: pve-b's departure from the observed scope
    # is a node fact, not an identity or source-revision effect.
    pve_b_after = next(
        node for node in store.list_nodes(source_id) if node.external_node_name == "pve-b"
    )
    assert pve_b_after.available is False

    # Resource identity, binding, and generation are unaffected by the mode
    # transition itself.
    assert after.resource_id == before.resource_id
    assert after.active_binding_id == before.active_binding_id
    assert after.locator_generation == before.locator_generation
    assert after.presence == "present"
    assert after.lifecycle == "active"
    assert after.successor_resource_id is None
    assert after_source.source_config_revision == before_source.source_config_revision
