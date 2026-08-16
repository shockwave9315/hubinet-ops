"""WAVE C1 Commit 3: fence discovery/freshness by source_attestation_epoch.

Closes the ADR 0003 SS20/SS21 discovery-provenance contract: epoch becomes an
actual completion/freshness authority condition, a peer of
source_config_revision/endpoint/canonicalization/transport_trust_revision
everywhere those are already checked -- while `attestation_status` and
`relationship_gate` remain entirely irrelevant to ordinary read-only
discovery (ADR 0003 SS3/SS13). Candidate endpoint attestation (Commit 4) is
out of scope here.
"""

from __future__ import annotations

import dataclasses
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
    SourceAttestationEvidenceReading,
    SourceAttestationReadOutcome,
    SourceAttestationRelationshipGate,
    SourceAttestationStatus,
    SourceAvailability,
)
from app.inventory.attestation import ANCHOR_KIND_PVE_ROOT_CA_SHA256_FINGERPRINT
from app.inventory.discovery import ProviderGuestLocatorSet, ProviderNodeScope


FIXED_NOW = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)


def fixed_now() -> datetime:
    return FIXED_NOW


def create_authority(
    tmp_path: Path,
    *,
    name: str = "Primary",
    locator: str = "https://pve.example:8006",
) -> tuple[InventoryAuthorityStore, InventoryAuthority, str]:
    store = InventoryAuthorityStore(tmp_path / "authority.db", now=fixed_now)
    authority = InventoryAuthority(store, now=fixed_now)
    state = authority.create_inventory_source(
        provider_kind="proxmox",
        display_name=name,
        credential_reference=f"secret://inventory/{name.lower()}",
        transport_locator=locator,
    )
    return store, authority, state.source.inventory_source_id


def active_endpoint_id(store: InventoryAuthorityStore, source_id: str) -> str:
    return store.source_state(source_id).active_endpoint.endpoint_id


class FakeEvidenceReader:
    def __init__(self, reading: SourceAttestationEvidenceReading) -> None:
        self.reading = reading

    def read(self, **kwargs) -> SourceAttestationEvidenceReading:
        return self.reading


def observed(anchor_value: str = "deadbeef") -> SourceAttestationEvidenceReading:
    return SourceAttestationEvidenceReading(
        outcome=SourceAttestationReadOutcome.OBSERVED,
        anchor_kind=ANCHOR_KIND_PVE_ROOT_CA_SHA256_FINGERPRINT,
        anchor_value=anchor_value,
    )


def enroll(
    store: InventoryAuthorityStore,
    authority: InventoryAuthority,
    source_id: str,
    anchor: str = "deadbeef",
) -> None:
    endpoint_id = active_endpoint_id(store, source_id)
    authority.enroll_source_attestation(
        source_id,
        endpoint_id=endpoint_id,
        actor="operator:enroll",
        evidence_reader=FakeEvidenceReader(observed(anchor)),
    )


def bump_epoch_out_of_band(store: InventoryAuthorityStore, source_id: str, epoch: int) -> None:
    """Directly mutate the durable current epoch, bypassing every normal
    transition helper (Commit 2's active-run fencing included) -- the
    adversarial witness Commit 3's completion-time CAS must independently
    defend against regardless of how the epoch changed."""

    with store._transaction() as connection:
        connection.execute(
            "UPDATE source_attestation_state SET source_attestation_epoch=? "
            "WHERE inventory_source_id=?",
            (epoch, source_id),
        )


def build_snapshot(
    run,
    source_id: str,
    *,
    vmid: int = 100,
    node: str = "pve-a",
    mode: BaselineMode = BaselineMode.CLUSTER,
) -> NormalizedDiscoverySnapshot:
    nodes = (DiscoveredNode(node, "online", True, FIXED_NOW.isoformat(), {}),)
    resource = DiscoveredResource(
        inventory_source_id=source_id,
        vmid=vmid,
        resource_type="qemu",
        name="guest",
        status="running",
        current_node_name=node,
        observed_at=FIXED_NOW.isoformat(),
        detail_status=DetailReadStatus.OK,
        facts={"memory": 1024},
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
        expected_source_attestation_epoch=run.expected_source_attestation_epoch,
        observed_at=FIXED_NOW.isoformat(),
        source_facts={"release": "9.0"},
        source_availability=SourceAvailability.AVAILABLE,
        baseline_completeness=BaselineCompleteness.COMPLETE,
        baseline_mode=mode,
        acl_topology_hash_before="acl",
        acl_topology_hash_after="acl",
        permission_snapshot_hash_before="perms",
        permission_snapshot_hash_after="perms",
        permission_coverage_complete=True,
        boundary_consistent=True,
        covered_nodes=(node,),
        failed_baseline_scopes=(),
        detail_summary={"ok_count": 1, "temporarily_unavailable_count": 0, "error_count": 0},
        failed_detail_scopes=(),
        nodes=nodes,
        resources=(resource,),
        provider_node_scope=ProviderNodeScope._from_provider(mode, (node,)),
        provider_guest_locators=ProviderGuestLocatorSet._from_provider(
            ({"vmid": vmid, "type": "qemu", "node": node},)
        ),
    )


def complete_snapshot(
    store: InventoryAuthorityStore, authority: InventoryAuthority, source_id: str, **kwargs
):
    run = authority.issue_discovery_run(source_id, 1)
    authority.mark_discovery_run_running(source_id, run.run_id)
    snapshot = build_snapshot(run, source_id, **kwargs)
    authority.finalize_successful_discovery_run(source_id, run.run_id, snapshot)
    return run, snapshot


# ---------------------------------------------------------------------------
# 1-2: normalized snapshot epoch context
# ---------------------------------------------------------------------------


def test_snapshot_carries_expected_epoch_zero_by_default(tmp_path: Path) -> None:
    store, authority, source_id = create_authority(tmp_path)
    run = authority.issue_discovery_run(source_id, 1)
    snapshot = build_snapshot(run, source_id)

    assert run.expected_source_attestation_epoch == 0
    assert snapshot.expected_source_attestation_epoch == 0


def test_finalize_rejects_snapshot_epoch_mismatching_run_issuance_epoch(
    tmp_path: Path,
) -> None:
    store, authority, source_id = create_authority(tmp_path)
    run = authority.issue_discovery_run(source_id, 1)
    authority.mark_discovery_run_running(source_id, run.run_id)
    snapshot = build_snapshot(run, source_id)
    tampered = dataclasses.replace(snapshot, expected_source_attestation_epoch=5)

    with pytest.raises(AuthorityConflict, match="immutable run issuance context"):
        authority.finalize_successful_discovery_run(source_id, run.run_id, tampered)


def test_snapshot_rejects_negative_epoch() -> None:
    with pytest.raises(ValueError, match="nonnegative"):
        NormalizedDiscoverySnapshot(
            run_id="00000000-0000-4000-8000-000000000001",
            discovery_run_sequence=1,
            inventory_source_id="00000000-0000-4000-8000-000000000002",
            expected_source_config_revision=1,
            endpoint_id="00000000-0000-4000-8000-000000000003",
            canonical_transport_locator="https://pve.example:8006",
            canonicalization_contract_version=1,
            expected_transport_trust_revision=1,
            provider_contract_version=1,
            observed_at=FIXED_NOW.isoformat(),
            source_facts={},
            source_availability=SourceAvailability.AVAILABLE,
            baseline_completeness=BaselineCompleteness.COMPLETE,
            baseline_mode=BaselineMode.STANDALONE,
            acl_topology_hash_before="a",
            acl_topology_hash_after="a",
            permission_snapshot_hash_before="p",
            permission_snapshot_hash_after="p",
            permission_coverage_complete=True,
            boundary_consistent=True,
            covered_nodes=(),
            failed_baseline_scopes=(),
            detail_summary={"ok_count": 0, "temporarily_unavailable_count": 0, "error_count": 0},
            failed_detail_scopes=(),
            nodes=(),
            resources=(),
            expected_source_attestation_epoch=-1,
        )


# ---------------------------------------------------------------------------
# 3-6: committed provenance across an enrollment epoch transition
# ---------------------------------------------------------------------------


def test_run_issued_at_epoch_0_commits_epoch_0(tmp_path: Path) -> None:
    store, authority, source_id = create_authority(tmp_path)
    complete_snapshot(store, authority, source_id)

    health = store.source_state(source_id).runtime_health
    assert health.committed_source_attestation_epoch == 0
    assert health.health == "healthy"
    assert health.freshness == "fresh"


def test_enrollment_leaves_committed_epoch_0_retained_but_health_stale(
    tmp_path: Path,
) -> None:
    store, authority, source_id = create_authority(tmp_path)
    complete_snapshot(store, authority, source_id)

    enroll(store, authority, source_id)

    health = store.source_state(source_id).runtime_health
    assert health.committed_source_attestation_epoch == 0  # retained history
    assert health.freshness == "stale"
    assert health.health_origin == "controlled_context_transition"
    assert len(store.list_resources(source_id)) == 1  # old inventory retained


def test_run_issued_after_enrollment_captures_epoch_1(tmp_path: Path) -> None:
    store, authority, source_id = create_authority(tmp_path)
    enroll(store, authority, source_id)

    run = authority.issue_discovery_run(source_id, 1)

    assert run.expected_source_attestation_epoch == 1


def test_successful_run_at_epoch_1_restores_freshness(tmp_path: Path) -> None:
    store, authority, source_id = create_authority(tmp_path)
    complete_snapshot(store, authority, source_id)
    enroll(store, authority, source_id)
    assert store.source_state(source_id).runtime_health.freshness == "stale"

    complete_snapshot(store, authority, source_id)

    health = store.source_state(source_id).runtime_health
    assert health.committed_source_attestation_epoch == 1
    assert health.freshness == "fresh"
    assert health.health == "healthy"


# ---------------------------------------------------------------------------
# 7-8: N -> N+1 anchor-change transition
# ---------------------------------------------------------------------------


def test_accepted_anchor_change_invalidates_old_freshness_provenance(
    tmp_path: Path,
) -> None:
    store, authority, source_id = create_authority(tmp_path)
    enroll(store, authority, source_id, "deadbeef")
    complete_snapshot(store, authority, source_id)
    assert store.source_state(source_id).runtime_health.freshness == "fresh"
    endpoint_id = active_endpoint_id(store, source_id)

    authority.accept_source_attestation_anchor_change(
        source_id,
        endpoint_id=endpoint_id,
        actor="operator:accept",
        evidence_reader=FakeEvidenceReader(observed("cafef00d")),
    )

    health = store.source_state(source_id).runtime_health
    assert health.freshness == "stale"
    assert health.committed_source_attestation_epoch == 1  # retained history
    assert store.attestation_state(source_id).source_attestation_epoch == 2


def test_successful_run_under_new_epoch_restores_committed_provenance(
    tmp_path: Path,
) -> None:
    store, authority, source_id = create_authority(tmp_path)
    enroll(store, authority, source_id, "deadbeef")
    complete_snapshot(store, authority, source_id)
    endpoint_id = active_endpoint_id(store, source_id)
    authority.accept_source_attestation_anchor_change(
        source_id,
        endpoint_id=endpoint_id,
        actor="operator:accept",
        evidence_reader=FakeEvidenceReader(observed("cafef00d")),
    )

    complete_snapshot(store, authority, source_id)

    health = store.source_state(source_id).runtime_health
    assert health.committed_source_attestation_epoch == 2
    assert health.freshness == "fresh"


# ---------------------------------------------------------------------------
# 9-10: stale-worker defense in depth (bypassing Commit 2's active-run fence)
# ---------------------------------------------------------------------------


def test_stale_successful_worker_no_reconciliation_invalid_completion(
    tmp_path: Path,
) -> None:
    store, authority, source_id = create_authority(tmp_path)
    complete_snapshot(store, authority, source_id)  # establish epoch-0 baseline
    run = authority.issue_discovery_run(source_id, 1)
    authority.mark_discovery_run_running(source_id, run.run_id)
    snapshot = build_snapshot(run, source_id, vmid=101)
    before = store.backend_instance()

    bump_epoch_out_of_band(store, source_id, 1)

    with pytest.raises(AuthorityConflict, match="context changed"):
        authority.finalize_successful_discovery_run(source_id, run.run_id, snapshot)

    assert len(store.list_resources(source_id)) == 1  # unchanged from the baseline run
    assert store.list_resources(source_id)[0].vmid == 100
    after = store.backend_instance()
    assert after.inventory_revision == before.inventory_revision
    completed_run = store.discovery_run(run.run_id)
    assert completed_run.provider_outcome == "invalid"
    assert completed_run.terminal_reason == "completion_context_changed"
    assert store.source_state(source_id).source.active_discovery_run_id is None
    health = store.source_state(source_id).runtime_health
    assert health.committed_source_attestation_epoch == 0  # last-known, retained
    assert health.freshness == "fresh"  # untouched by the rejected stale worker


def test_stale_failed_worker_does_not_overwrite_current_health(tmp_path: Path) -> None:
    store, authority, source_id = create_authority(tmp_path)
    complete_snapshot(store, authority, source_id)  # establish epoch-0 baseline
    baseline_health = store.source_state(source_id).runtime_health
    run = authority.issue_discovery_run(source_id, 1)
    authority.mark_discovery_run_running(source_id, run.run_id)

    bump_epoch_out_of_band(store, source_id, 1)

    evidence = DiscoveryRunCompletionEvidence(
        baseline_completeness=BaselineCompleteness.SOURCE_UNAVAILABLE
    )
    authority.finalize_failed_discovery_run(
        source_id, run.run_id, completion_evidence=evidence, reason="source_unreachable"
    )

    health = store.source_state(source_id).runtime_health
    # Current health authority is untouched by the stale-epoch failure.
    assert health.health == baseline_health.health == "healthy"
    assert health.freshness == baseline_health.freshness == "fresh"
    assert health.last_health_run_sequence == baseline_health.last_health_run_sequence
    assert health.last_run_health_outcome == baseline_health.last_run_health_outcome
    # The completion audit alone (not the health authority) may still
    # record that the attempt happened and terminalize the run.
    assert health.latest_completed_run_sequence == 2
    assert health.latest_completed_outcome == "source_unavailable"
    assert store.discovery_run(run.run_id).lifecycle.value == "completed"


# ---------------------------------------------------------------------------
# 11-12: committed-context/freshness check requires exact epoch equality
# ---------------------------------------------------------------------------


def test_source_is_fresh_returns_false_on_epoch_only_mismatch(tmp_path: Path) -> None:
    store, authority, source_id = create_authority(tmp_path)
    complete_snapshot(store, authority, source_id)
    assert authority.source_is_fresh_for_future_mutation(source_id) is True

    # Only the current epoch changes; every other committed context field
    # (source_config_revision/endpoint/locator/canonicalization/transport
    # trust) still matches exactly.
    bump_epoch_out_of_band(store, source_id, 1)

    assert authority.source_is_fresh_for_future_mutation(source_id) is False


# ---------------------------------------------------------------------------
# 13: restart/reopen preserves epoch provenance
# ---------------------------------------------------------------------------


def test_restart_reopen_preserves_epoch_provenance(tmp_path: Path) -> None:
    path = tmp_path / "authority.db"
    store = InventoryAuthorityStore(path, now=fixed_now)
    authority = InventoryAuthority(store, now=fixed_now)
    state = authority.create_inventory_source(
        provider_kind="proxmox",
        display_name="Primary",
        credential_reference="secret://inventory/primary",
        transport_locator="https://pve.example:8006",
    )
    source_id = state.source.inventory_source_id
    enroll(store, authority, source_id)
    run, _ = complete_snapshot(store, authority, source_id)
    store.close()

    reopened = InventoryAuthorityStore(path, now=fixed_now)
    reopened_run = reopened.discovery_run(run.run_id)
    assert reopened_run.expected_source_attestation_epoch == 1
    assert reopened_run.completion_source_attestation_epoch == 1
    health = reopened.source_state(source_id).runtime_health
    assert health.committed_source_attestation_epoch == 1


# ---------------------------------------------------------------------------
# 14-17: attestation status/relationship gate never gate ordinary discovery
# ---------------------------------------------------------------------------


def test_epoch_0_remains_valid_for_ordinary_discovery(tmp_path: Path) -> None:
    store, authority, source_id = create_authority(tmp_path)
    assert store.attestation_state(source_id).source_attestation_epoch == 0

    complete_snapshot(store, authority, source_id)

    assert len(store.list_resources(source_id)) == 1
    assert store.source_state(source_id).runtime_health.freshness == "fresh"


def test_not_yet_attested_does_not_block_discovery(tmp_path: Path) -> None:
    store, authority, source_id = create_authority(tmp_path)
    assert store.attestation_state(source_id).attestation_status is (
        SourceAttestationStatus.NOT_YET_ATTESTED
    )

    run, _ = complete_snapshot(store, authority, source_id)

    assert store.discovery_run(run.run_id).provider_outcome == "success"


def test_mismatch_pending_gate_does_not_block_ordinary_discovery(tmp_path: Path) -> None:
    store, authority, source_id = create_authority(tmp_path)
    enroll(store, authority, source_id, "deadbeef")
    endpoint_id = active_endpoint_id(store, source_id)
    authority.reattest_source(
        source_id,
        endpoint_id=endpoint_id,
        actor="operator:reattest",
        evidence_reader=FakeEvidenceReader(observed("cafef00d")),
    )
    assert store.attestation_state(source_id).relationship_gate is (
        SourceAttestationRelationshipGate.MISMATCH_PENDING_REATTESTATION
    )

    run, _ = complete_snapshot(store, authority, source_id)

    assert store.discovery_run(run.run_id).provider_outcome == "success"
    assert store.source_state(source_id).runtime_health.freshness == "fresh"
    assert len(store.list_resources(source_id)) == 1


def test_successful_discovery_never_clears_pending_mismatch(tmp_path: Path) -> None:
    store, authority, source_id = create_authority(tmp_path)
    enroll(store, authority, source_id, "deadbeef")
    endpoint_id = active_endpoint_id(store, source_id)
    authority.reattest_source(
        source_id,
        endpoint_id=endpoint_id,
        actor="operator:reattest",
        evidence_reader=FakeEvidenceReader(observed("cafef00d")),
    )

    complete_snapshot(store, authority, source_id)

    state = store.attestation_state(source_id)
    assert state.relationship_gate is (
        SourceAttestationRelationshipGate.MISMATCH_PENDING_REATTESTATION
    )
    assert state.source_attestation_epoch == 1
    assert state.anchor_value == "deadbeef"  # a successful observation is never proof


# ---------------------------------------------------------------------------
# 18: baseline_mode independence
# ---------------------------------------------------------------------------


def test_baseline_mode_transition_does_not_affect_epoch(tmp_path: Path) -> None:
    store, authority, source_id = create_authority(tmp_path)
    complete_snapshot(store, authority, source_id, mode=BaselineMode.CLUSTER)
    assert store.attestation_state(source_id).source_attestation_epoch == 0

    complete_snapshot(store, authority, source_id, mode=BaselineMode.STANDALONE)

    assert store.attestation_state(source_id).source_attestation_epoch == 0
    health = store.source_state(source_id).runtime_health
    assert health.freshness == "fresh"


# ---------------------------------------------------------------------------
# 19: same-anchor reconfirmation leaves committed freshness provenance valid
# ---------------------------------------------------------------------------


def test_same_anchor_reconfirm_leaves_committed_freshness_valid(tmp_path: Path) -> None:
    store, authority, source_id = create_authority(tmp_path)
    enroll(store, authority, source_id, "deadbeef")
    complete_snapshot(store, authority, source_id)
    assert store.source_state(source_id).runtime_health.freshness == "fresh"
    endpoint_id = active_endpoint_id(store, source_id)

    authority.reattest_source(
        source_id,
        endpoint_id=endpoint_id,
        actor="operator:reattest",
        evidence_reader=FakeEvidenceReader(observed("deadbeef")),
    )

    health = store.source_state(source_id).runtime_health
    assert health.freshness == "fresh"
    assert health.committed_source_attestation_epoch == 1
    assert authority.source_is_fresh_for_future_mutation(source_id) is True
