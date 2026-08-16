"""WAVE C1 Commit 5: final adversarial matrix + defense-in-depth closure.

Closes ADR 0003 SS9, 10, 10a, 11, 14, 15, 17, 18, 19a, 20, 21, 25, 27, 28,
29, 31 across the complete C1 package (Commits 1-4). This file is
deliberately compact: it reuses the same helper patterns already
established in the Commit 1-4 test files rather than duplicating their
bounded coverage, and adds only the witnesses genuinely new to this final
pass -- the strengthened event/binding provenance trigger (Part B), the
new live-eligible-source-at-insert trigger (Part C), and the new
security-context-requires-epoch-bump trigger (Part D).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import sqlite3
import uuid

import pytest

from app.inventory import (
    AttestationEvidenceTier,
    AttestationOutcome,
    AuthorityConflict,
    BaselineCompleteness,
    BaselineMode,
    DetailReadStatus,
    DiscoveredNode,
    DiscoveredResource,
    InventoryAuthority,
    InventoryAuthorityStore,
    NormalizedDiscoverySnapshot,
    SourceAttestationEvidenceReading,
    SourceAttestationReadOutcome,
    SourceAttestationRelationshipGate,
    SourceAttestationStatus,
    SourceAvailability,
    TierTwoEvaluationStatus,
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


def insert_endpoint(
    store: InventoryAuthorityStore,
    source_id: str,
    *,
    locator: str = "https://candidate.example:8006",
    lifecycle: str = "candidate",
) -> str:
    endpoint_id = str(uuid.uuid4())
    with store._transaction() as connection:
        connection.execute(
            "INSERT INTO source_endpoints("
            "endpoint_id, inventory_source_id, canonical_transport_locator, "
            "canonicalization_contract_version, lifecycle, transport_trust_revision, "
            "created_at) VALUES(?, ?, ?, 1, ?, 1, ?)",
            (endpoint_id, source_id, locator, lifecycle, FIXED_NOW.isoformat()),
        )
    return endpoint_id


class FakeEvidenceReader:
    def __init__(self, reading: SourceAttestationEvidenceReading, mutate=None) -> None:
        self.reading = reading
        self.mutate = mutate
        self.calls: list[dict] = []

    def read(self, **kwargs) -> SourceAttestationEvidenceReading:
        self.calls.append(kwargs)
        if self.mutate is not None:
            self.mutate()
        return self.reading


def observed(
    anchor_value: str = "deadbeef", *, tier2_verified: bool | None = None
) -> SourceAttestationEvidenceReading:
    return SourceAttestationEvidenceReading(
        outcome=SourceAttestationReadOutcome.OBSERVED,
        anchor_kind=ANCHOR_KIND_PVE_ROOT_CA_SHA256_FINGERPRINT,
        anchor_value=anchor_value,
        tier2_verified=tier2_verified,
    )


def unavailable() -> SourceAttestationEvidenceReading:
    return SourceAttestationEvidenceReading(outcome=SourceAttestationReadOutcome.UNAVAILABLE)


def malformed() -> SourceAttestationEvidenceReading:
    return SourceAttestationEvidenceReading(outcome=SourceAttestationReadOutcome.MALFORMED)


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


def complete_snapshot(
    store: InventoryAuthorityStore, authority: InventoryAuthority, source_id: str, *, vmid: int = 100
):
    run = authority.issue_discovery_run(source_id, 1)
    authority.mark_discovery_run_running(source_id, run.run_id)
    nodes = (DiscoveredNode("pve-a", "online", True, FIXED_NOW.isoformat(), {}),)
    resource = DiscoveredResource(
        inventory_source_id=source_id,
        vmid=vmid,
        resource_type="qemu",
        name="guest",
        status="running",
        current_node_name="pve-a",
        observed_at=FIXED_NOW.isoformat(),
        detail_status=DetailReadStatus.OK,
        facts={"memory": 1024},
    )
    snapshot = NormalizedDiscoverySnapshot(
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
        baseline_mode=BaselineMode.CLUSTER,
        acl_topology_hash_before="acl",
        acl_topology_hash_after="acl",
        permission_snapshot_hash_before="perms",
        permission_snapshot_hash_after="perms",
        permission_coverage_complete=True,
        boundary_consistent=True,
        covered_nodes=("pve-a",),
        failed_baseline_scopes=(),
        detail_summary={"ok_count": 1, "temporarily_unavailable_count": 0, "error_count": 0},
        failed_detail_scopes=(),
        nodes=nodes,
        resources=(resource,),
        provider_node_scope=ProviderNodeScope._from_provider(BaselineMode.CLUSTER, ("pve-a",)),
        provider_guest_locators=ProviderGuestLocatorSet._from_provider(
            ({"vmid": vmid, "type": "qemu", "node": "pve-a"},)
        ),
    )
    authority.finalize_successful_discovery_run(source_id, run.run_id, snapshot)
    return run


# ---------------------------------------------------------------------------
# Part A: final adversarial matrix (witnesses 1-16)
# ---------------------------------------------------------------------------


def test_a1_discovery_cannot_auto_enroll_not_yet_attested_source(tmp_path: Path) -> None:
    store, authority, source_id = create_authority(tmp_path)
    complete_snapshot(store, authority, source_id)
    complete_snapshot(store, authority, source_id, vmid=101)

    state = store.attestation_state(source_id)
    assert state.attestation_status is SourceAttestationStatus.NOT_YET_ATTESTED
    assert state.source_attestation_epoch == 0
    assert store.list_attestation_events(source_id) == ()


def test_a2_matching_candidate_without_enrollment_creates_no_binding(tmp_path: Path) -> None:
    store, authority, source_id = create_authority(tmp_path)
    candidate_id = insert_endpoint(store, source_id)
    reader = FakeEvidenceReader(observed("deadbeef"))

    with pytest.raises(AuthorityConflict, match="no enrolled attestation anchor"):
        authority.check_candidate_attestation(
            source_id, endpoint_id=candidate_id, actor="operator:test", evidence_reader=reader
        )

    # Fail-closed before any remote I/O: the reader is never even called.
    assert reader.calls == []
    assert store.list_candidate_attestation_bindings(source_id) == ()


def test_a3_mismatch_touches_only_the_relationship_gate(tmp_path: Path) -> None:
    store, authority, source_id = create_authority(tmp_path)
    enroll(store, authority, source_id, "deadbeef")
    complete_snapshot(store, authority, source_id)
    resource_before = store.list_resources(source_id)[0]
    sources_before = len(store.list_source_states())
    endpoint_id = active_endpoint_id(store, source_id)

    authority.reattest_source(
        source_id,
        endpoint_id=endpoint_id,
        actor="operator:reattest",
        evidence_reader=FakeEvidenceReader(observed("cafef00d")),
    )

    assert len(store.list_source_states()) == sources_before  # no new source
    resource_after = store.list_resources(source_id)[0]
    assert resource_after.resource_id == resource_before.resource_id
    assert resource_after.presence == resource_before.presence
    assert resource_after.lifecycle == resource_before.lifecycle
    assert resource_after.security_continuity == resource_before.security_continuity == "unverified"
    state = store.attestation_state(source_id)
    assert state.relationship_gate is SourceAttestationRelationshipGate.MISMATCH_PENDING_REATTESTATION
    assert state.source_attestation_epoch == 1
    assert state.anchor_value == "deadbeef"
    assert store.list_candidate_attestation_bindings(source_id) == ()


@pytest.mark.parametrize(
    "reading",
    (unavailable(), malformed()),
    ids=("unavailable", "malformed"),
)
def test_a4_unavailable_and_malformed_are_neither_match_nor_mismatch(
    tmp_path: Path, reading: SourceAttestationEvidenceReading
) -> None:
    store, authority, source_id = create_authority(tmp_path)
    enroll(store, authority, source_id, "deadbeef")
    endpoint_id = active_endpoint_id(store, source_id)

    event = authority.reattest_source(
        source_id,
        endpoint_id=endpoint_id,
        actor="operator:reattest",
        evidence_reader=FakeEvidenceReader(reading),
    )

    assert event.outcome not in (AttestationOutcome.MATCH, AttestationOutcome.MISMATCH)
    assert event.resulting_epoch is None
    state = store.attestation_state(source_id)
    assert state.source_attestation_epoch == 1
    assert state.relationship_gate is SourceAttestationRelationshipGate.CLEAR
    assert len(store.list_attestation_events(source_id)) == 2  # enroll + this attempt


def test_a4_reader_exception_is_neither_match_nor_mismatch(tmp_path: Path) -> None:
    store, authority, source_id = create_authority(tmp_path)
    enroll(store, authority, source_id, "deadbeef")
    endpoint_id = active_endpoint_id(store, source_id)

    class RaisingReader:
        def read(self, **kwargs) -> SourceAttestationEvidenceReading:
            raise RuntimeError("network error")

    event = authority.reattest_source(
        source_id, endpoint_id=endpoint_id, actor="operator:test", evidence_reader=RaisingReader()
    )

    assert event.outcome is AttestationOutcome.UNAVAILABLE
    state = store.attestation_state(source_id)
    assert state.source_attestation_epoch == 1
    assert state.relationship_gate is SourceAttestationRelationshipGate.CLEAR


def test_a5_two_candidates_with_same_anchor_neither_becomes_active(tmp_path: Path) -> None:
    store, authority, source_id = create_authority(tmp_path)
    enroll(store, authority, source_id, "deadbeef")
    active_before = active_endpoint_id(store, source_id)
    candidate_1 = insert_endpoint(store, source_id, locator="https://candidate-1.example:8006")
    candidate_2 = insert_endpoint(store, source_id, locator="https://candidate-2.example:8006")

    authority.check_candidate_attestation(
        source_id, endpoint_id=candidate_1, actor="operator:test",
        evidence_reader=FakeEvidenceReader(observed("deadbeef")),
    )
    authority.check_candidate_attestation(
        source_id, endpoint_id=candidate_2, actor="operator:test",
        evidence_reader=FakeEvidenceReader(observed("deadbeef")),
    )

    bindings_1 = store.list_candidate_attestation_bindings(source_id, endpoint_id=candidate_1)
    bindings_2 = store.list_candidate_attestation_bindings(source_id, endpoint_id=candidate_2)
    assert len(bindings_1) == len(bindings_2) == 1
    # Coexisting matching evidence is not physical-uniqueness proof and
    # activates nothing -- the active endpoint remains exactly the original.
    assert active_endpoint_id(store, source_id) == active_before
    assert active_endpoint_id(store, source_id) not in (candidate_1, candidate_2)


def test_a6_tier1_match_never_becomes_tier2_by_association(tmp_path: Path) -> None:
    store, authority, source_id = create_authority(tmp_path)
    endpoint_id = active_endpoint_id(store, source_id)

    event = authority.enroll_source_attestation(
        source_id,
        endpoint_id=endpoint_id,
        actor="operator:test",
        evidence_reader=FakeEvidenceReader(observed("deadbeef", tier2_verified=None)),
    )

    assert event.evidence_tier is AttestationEvidenceTier.TIER_1
    assert event.tier2_evaluation is TierTwoEvaluationStatus.NOT_EVALUATED
    state = store.attestation_state(source_id)
    assert state.evidence_tier is AttestationEvidenceTier.TIER_1


def test_a7_tier2_failure_with_matching_fingerprint_never_becomes_tier2(
    tmp_path: Path,
) -> None:
    store, authority, source_id = create_authority(tmp_path)
    endpoint_id = active_endpoint_id(store, source_id)

    # A matching tier-1 fingerprint is present, but tier-2 chain
    # verification genuinely failed -- the payload must never repair it.
    event = authority.enroll_source_attestation(
        source_id,
        endpoint_id=endpoint_id,
        actor="operator:test",
        evidence_reader=FakeEvidenceReader(observed("deadbeef", tier2_verified=False)),
    )

    assert event.evidence_tier is AttestationEvidenceTier.TIER_1
    assert event.tier2_evaluation is TierTwoEvaluationStatus.FAILED
    state = store.attestation_state(source_id)
    assert state.evidence_tier is AttestationEvidenceTier.TIER_1
    assert state.tier2_evaluation is TierTwoEvaluationStatus.FAILED


def test_a8_old_epoch_worker_remains_completion_fenced(tmp_path: Path) -> None:
    store, authority, source_id = create_authority(tmp_path)
    complete_snapshot(store, authority, source_id)
    run = authority.issue_discovery_run(source_id, 1)
    authority.mark_discovery_run_running(source_id, run.run_id)
    with store._transaction() as connection:
        connection.execute(
            "UPDATE source_attestation_state SET source_attestation_epoch=1 "
            "WHERE inventory_source_id=?",
            (source_id,),
        )
    before = store.backend_instance()

    with pytest.raises(AuthorityConflict, match="context changed"):
        authority.finalize_successful_discovery_run(
            source_id,
            run.run_id,
            NormalizedDiscoverySnapshot(
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
                source_facts={},
                source_availability=SourceAvailability.AVAILABLE,
                baseline_completeness=BaselineCompleteness.COMPLETE,
                baseline_mode=BaselineMode.CLUSTER,
                acl_topology_hash_before="acl",
                acl_topology_hash_after="acl",
                permission_snapshot_hash_before="perms",
                permission_snapshot_hash_after="perms",
                permission_coverage_complete=True,
                boundary_consistent=True,
                covered_nodes=("pve-a",),
                failed_baseline_scopes=(),
                detail_summary={"ok_count": 0, "temporarily_unavailable_count": 0, "error_count": 0},
                failed_detail_scopes=(),
                nodes=(DiscoveredNode("pve-a", "online", True, FIXED_NOW.isoformat(), {}),),
                resources=(),
                provider_node_scope=ProviderNodeScope._from_provider(BaselineMode.CLUSTER, ("pve-a",)),
                provider_guest_locators=ProviderGuestLocatorSet._from_provider(()),
            ),
        )

    after = store.backend_instance()
    assert after.inventory_revision == before.inventory_revision
    assert store.discovery_run(run.run_id).provider_outcome == "invalid"


def test_a9_committed_freshness_from_epoch_n_not_fresh_at_n_plus_1(tmp_path: Path) -> None:
    store, authority, source_id = create_authority(tmp_path)
    complete_snapshot(store, authority, source_id)
    assert authority.source_is_fresh_for_future_mutation(source_id) is True

    enroll(store, authority, source_id, "deadbeef")

    assert authority.source_is_fresh_for_future_mutation(source_id) is False
    assert store.attestation_state(source_id).source_attestation_epoch == 1


def test_a10_same_anchor_reconfirmation_preserves_freshness_and_bindings(
    tmp_path: Path,
) -> None:
    store, authority, source_id = create_authority(tmp_path)
    enroll(store, authority, source_id, "deadbeef")
    complete_snapshot(store, authority, source_id)
    candidate_id = insert_endpoint(store, source_id)
    authority.check_candidate_attestation(
        source_id, endpoint_id=candidate_id, actor="operator:test",
        evidence_reader=FakeEvidenceReader(observed("deadbeef")),
    )
    binding_before = store.list_candidate_attestation_bindings(source_id, endpoint_id=candidate_id)[0]
    endpoint_id = active_endpoint_id(store, source_id)

    authority.reattest_source(
        source_id,
        endpoint_id=endpoint_id,
        actor="operator:reattest",
        evidence_reader=FakeEvidenceReader(observed("deadbeef")),
    )

    assert store.attestation_state(source_id).source_attestation_epoch == 1
    assert authority.source_is_fresh_for_future_mutation(source_id) is True
    binding_after = store.list_candidate_attestation_bindings(source_id, endpoint_id=candidate_id)[0]
    assert binding_after == binding_before


def test_a11_accepted_anchor_change_n_to_n_plus_1_preserves_identity(tmp_path: Path) -> None:
    store, authority, source_id = create_authority(tmp_path)
    enroll(store, authority, source_id, "deadbeef")
    complete_snapshot(store, authority, source_id)
    resource_before = store.list_resources(source_id)[0]
    candidate_id = insert_endpoint(store, source_id)
    authority.check_candidate_attestation(
        source_id, endpoint_id=candidate_id, actor="operator:test",
        evidence_reader=FakeEvidenceReader(observed("deadbeef")),
    )
    old_binding = store.list_candidate_attestation_bindings(source_id, endpoint_id=candidate_id)[0]
    endpoint_id = active_endpoint_id(store, source_id)

    authority.accept_source_attestation_anchor_change(
        source_id,
        endpoint_id=endpoint_id,
        actor="operator:accept",
        evidence_reader=FakeEvidenceReader(observed("cafef00d")),
    )

    state = store.attestation_state(source_id)
    assert state.source_attestation_epoch == 2  # exact N -> N+1
    new_binding = store.list_candidate_attestation_bindings(source_id, endpoint_id=candidate_id)[0]
    assert new_binding == old_binding  # retained, unmodified
    assert new_binding.source_attestation_epoch == 1  # not current-epoch evidence
    resource_after = store.list_resources(source_id)[0]
    assert resource_after.resource_id == resource_before.resource_id
    assert resource_after.active_binding_id == resource_before.active_binding_id
    assert resource_after.locator_generation == resource_before.locator_generation
    assert resource_after.presence == resource_before.presence
    assert resource_after.observational_continuity == resource_before.observational_continuity


def test_a12_baseline_mode_independent_from_attestation_epoch(tmp_path: Path) -> None:
    store, authority, source_id = create_authority(tmp_path)
    enroll(store, authority, source_id, "deadbeef")
    run = authority.issue_discovery_run(source_id, 1)
    authority.mark_discovery_run_running(source_id, run.run_id)
    nodes = (DiscoveredNode("pve-a", "online", True, FIXED_NOW.isoformat(), {}),)
    resource = DiscoveredResource(
        inventory_source_id=source_id, vmid=100, resource_type="qemu", name="guest",
        status="running", current_node_name="pve-a", observed_at=FIXED_NOW.isoformat(),
        detail_status=DetailReadStatus.OK, facts={},
    )
    snapshot = NormalizedDiscoverySnapshot(
        run_id=run.run_id, discovery_run_sequence=run.discovery_run_sequence,
        inventory_source_id=source_id,
        expected_source_config_revision=run.expected_source_config_revision,
        endpoint_id=run.expected_endpoint_id,
        canonical_transport_locator=run.expected_canonical_transport_locator,
        canonicalization_contract_version=run.expected_canonicalization_contract_version,
        expected_transport_trust_revision=run.expected_transport_trust_revision,
        provider_contract_version=run.provider_contract_version,
        expected_source_attestation_epoch=run.expected_source_attestation_epoch,
        observed_at=FIXED_NOW.isoformat(), source_facts={},
        source_availability=SourceAvailability.AVAILABLE,
        baseline_completeness=BaselineCompleteness.COMPLETE,
        baseline_mode=BaselineMode.STANDALONE,  # mode transition, epoch untouched
        acl_topology_hash_before="acl", acl_topology_hash_after="acl",
        permission_snapshot_hash_before="perms", permission_snapshot_hash_after="perms",
        permission_coverage_complete=True, boundary_consistent=True,
        covered_nodes=("pve-a",), failed_baseline_scopes=(),
        detail_summary={"ok_count": 1, "temporarily_unavailable_count": 0, "error_count": 0},
        failed_detail_scopes=(), nodes=nodes, resources=(resource,),
        provider_node_scope=ProviderNodeScope._from_provider(BaselineMode.STANDALONE, ("pve-a",)),
        provider_guest_locators=ProviderGuestLocatorSet._from_provider(
            ({"vmid": 100, "type": "qemu", "node": "pve-a"},)
        ),
    )
    authority.finalize_successful_discovery_run(source_id, run.run_id, snapshot)

    assert store.attestation_state(source_id).source_attestation_epoch == 1  # unaffected


def test_a13_candidate_lifecycle_retired_during_read_is_stale_cas(tmp_path: Path) -> None:
    store, authority, source_id = create_authority(tmp_path)
    enroll(store, authority, source_id, "deadbeef")
    candidate_id = insert_endpoint(store, source_id)

    def mutate() -> None:
        with store._transaction() as connection:
            connection.execute(
                "UPDATE source_endpoints SET lifecycle='retired' WHERE endpoint_id=?",
                (candidate_id,),
            )

    with pytest.raises(AuthorityConflict, match="context changed"):
        authority.check_candidate_attestation(
            source_id, endpoint_id=candidate_id, actor="operator:test",
            evidence_reader=FakeEvidenceReader(observed("deadbeef"), mutate=mutate),
        )

    assert store.list_candidate_attestation_bindings(source_id) == ()


def test_a14_ordinary_discovery_uses_only_active_endpoint_with_bindings_present(
    tmp_path: Path,
) -> None:
    store, authority, source_id = create_authority(tmp_path)
    enroll(store, authority, source_id, "deadbeef")
    active_id = active_endpoint_id(store, source_id)
    for i in range(2):
        candidate_id = insert_endpoint(store, source_id, locator=f"https://candidate-{i}.example:8006")
        authority.check_candidate_attestation(
            source_id, endpoint_id=candidate_id, actor="operator:test",
            evidence_reader=FakeEvidenceReader(observed("deadbeef")),
        )

    run = authority.issue_discovery_run(source_id, 1)

    assert run.expected_endpoint_id == active_id
    assert active_endpoint_id(store, source_id) == active_id


def test_a15_attestation_and_bindings_grant_no_resource_trust_or_capability(
    tmp_path: Path,
) -> None:
    store, authority, source_id = create_authority(tmp_path)
    complete_snapshot(store, authority, source_id)
    enroll(store, authority, source_id, "deadbeef")
    candidate_id = insert_endpoint(store, source_id)
    authority.check_candidate_attestation(
        source_id, endpoint_id=candidate_id, actor="operator:test",
        evidence_reader=FakeEvidenceReader(observed("deadbeef")),
    )
    endpoint_id = active_endpoint_id(store, source_id)
    authority.accept_source_attestation_anchor_change(
        source_id, endpoint_id=endpoint_id, actor="operator:accept",
        evidence_reader=FakeEvidenceReader(observed("cafef00d")),
    )

    resource = store.list_resources(source_id)[0]
    assert resource.security_continuity == "unverified"  # never "trusted"
    assert resource.state_level == "discovered"  # never managed/maintenance


def test_a16_no_activation_or_failover_code_exists(tmp_path: Path) -> None:
    """Static boundary check, not a runtime probe: the only place
    source_endpoints.lifecycle is ever set to 'active' anywhere in the
    authority module is the one atomic initial-source-creation INSERT."""

    source_path = Path(__file__).resolve().parents[1] / "app" / "inventory" / "authority.py"
    text = source_path.read_text(encoding="utf-8")

    forbidden_method_names = (
        "def activate_candidate",
        "def promote_endpoint",
        "def replace_active_endpoint",
        "def failover",
        "def select_endpoint",
    )
    for name in forbidden_method_names:
        assert name not in text

    # No code path ever UPDATEs an endpoint's lifecycle at all -- the only
    # place source_endpoints.lifecycle is ever written is the one atomic
    # initial-endpoint-creation INSERT (VALUES(..., 'active', ...)); every
    # other occurrence of lifecycle='active' in this module is a read-only
    # WHERE filter (_require_active_endpoint_row), never a SET.
    assert "UPDATE source_endpoints SET lifecycle=" not in text
    assert "SET lifecycle='active'" not in text
    assert "VALUES(?, ?, ?, ?, 'active', 1, ?)" in text  # the one legitimate write


# ---------------------------------------------------------------------------
# Part B: strengthened event -> binding provenance (new fields)
# ---------------------------------------------------------------------------


def _insert_valid_candidate_event(
    connection: sqlite3.Connection,
    *,
    event_id: str,
    source_id: str,
    endpoint_id: str,
    actor: str = "operator:test",
    attempted_at: str = FIXED_NOW.isoformat(),
    expected_endpoint_id: str | None = None,
    previous_epoch: int = 1,
    resulting_epoch: int | None = None,
    resulting_relationship_gate: str | None = None,
) -> None:
    connection.execute(
        "INSERT INTO source_attestation_events("
        "event_id, inventory_source_id, target_endpoint_id, operation, actor, "
        "attempted_at, expected_source_config_revision, expected_endpoint_id, "
        "expected_canonical_transport_locator, "
        "expected_canonicalization_contract_version, "
        "expected_transport_trust_revision, expected_source_attestation_epoch, "
        "expected_relationship_gate, "
        "outcome, evidence_tier, tier2_evaluation, asserted_anchor_kind, "
        "asserted_anchor_value, endpoint_lifecycle_at_check, previous_epoch, "
        "resulting_epoch, resulting_relationship_gate, reason) "
        "VALUES(?, ?, ?, 'candidate_check', ?, ?, 1, ?, "
        "'https://candidate.example:8006', 1, 1, 1, 'clear', "
        "'accepted', 'tier_1', 'not_evaluated', "
        "'pve_root_ca_sha256_fingerprint', 'deadbeef', 'candidate', ?, ?, ?, "
        "'test event')",
        (
            event_id,
            source_id,
            endpoint_id,
            actor,
            attempted_at,
            expected_endpoint_id or endpoint_id,
            previous_epoch,
            resulting_epoch,
            resulting_relationship_gate,
        ),
    )


def _insert_matching_candidate_binding(
    connection: sqlite3.Connection,
    *,
    binding_id: str,
    source_id: str,
    endpoint_id: str,
    event_id: str,
    created_by: str = "operator:test",
    matched_at: str = FIXED_NOW.isoformat(),
) -> None:
    connection.execute(
        "INSERT INTO candidate_attestation_bindings("
        "binding_id, inventory_source_id, endpoint_id, source_attestation_epoch, "
        "evidence_tier, tier2_evaluation, endpoint_lifecycle_at_check, "
        "canonical_transport_locator, canonicalization_contract_version, "
        "transport_trust_revision, matched_at, created_by, event_id) "
        "VALUES(?, ?, ?, 1, 'tier_1', 'not_evaluated', 'candidate', "
        "'https://candidate.example:8006', 1, 1, ?, ?, ?)",
        (binding_id, source_id, endpoint_id, matched_at, created_by, event_id),
    )


def _force_attested_matching(store: InventoryAuthorityStore, source_id: str) -> None:
    with store._transaction() as connection:
        connection.execute(
            "UPDATE source_attestation_state SET attestation_status='attested', "
            "source_attestation_epoch=1, anchor_kind='pve_root_ca_sha256_fingerprint', "
            "anchor_value='deadbeef', evidence_tier='tier_1', tier2_evaluation='not_evaluated', "
            "relationship_gate='clear', accepted_at=?, accepted_by='operator:test', "
            "evaluated_endpoint_id=(SELECT endpoint_id FROM source_endpoints "
            "WHERE inventory_source_id=? AND lifecycle='active') "
            "WHERE inventory_source_id=?",
            (FIXED_NOW.isoformat(), source_id, source_id),
        )


def test_b_binding_rejected_when_expected_endpoint_id_diverges(tmp_path: Path) -> None:
    store, authority, source_id = create_authority(tmp_path)
    _force_attested_matching(store, source_id)
    candidate_id = insert_endpoint(store, source_id)
    other_candidate = insert_endpoint(store, source_id, locator="https://other.example:8006")
    event_id = str(uuid.uuid4())

    with store._transaction() as connection:
        # target_endpoint_id matches the binding, but expected_endpoint_id
        # (the redundant provenance field) diverges.
        _insert_valid_candidate_event(
            connection,
            event_id=event_id,
            source_id=source_id,
            endpoint_id=candidate_id,
            expected_endpoint_id=other_candidate,
        )

    with pytest.raises(
        sqlite3.IntegrityError, match="matching accepted candidate_check event"
    ):
        with store._transaction() as connection:
            _insert_matching_candidate_binding(
                connection, binding_id=str(uuid.uuid4()), source_id=source_id,
                endpoint_id=candidate_id, event_id=event_id,
            )


def test_b_binding_rejected_when_previous_epoch_diverges(tmp_path: Path) -> None:
    store, authority, source_id = create_authority(tmp_path)
    _force_attested_matching(store, source_id)
    candidate_id = insert_endpoint(store, source_id)
    event_id = str(uuid.uuid4())

    with store._transaction() as connection:
        _insert_valid_candidate_event(
            connection, event_id=event_id, source_id=source_id, endpoint_id=candidate_id,
            previous_epoch=2,  # diverges from expected_source_attestation_epoch=1
        )

    with pytest.raises(
        sqlite3.IntegrityError, match="matching accepted candidate_check event"
    ):
        with store._transaction() as connection:
            _insert_matching_candidate_binding(
                connection, binding_id=str(uuid.uuid4()), source_id=source_id,
                endpoint_id=candidate_id, event_id=event_id,
            )


def test_b_binding_rejected_when_actor_diverges(tmp_path: Path) -> None:
    store, authority, source_id = create_authority(tmp_path)
    _force_attested_matching(store, source_id)
    candidate_id = insert_endpoint(store, source_id)
    event_id = str(uuid.uuid4())

    with store._transaction() as connection:
        _insert_valid_candidate_event(
            connection, event_id=event_id, source_id=source_id, endpoint_id=candidate_id,
            actor="operator:original",
        )

    with pytest.raises(
        sqlite3.IntegrityError, match="matching accepted candidate_check event"
    ):
        with store._transaction() as connection:
            _insert_matching_candidate_binding(
                connection, binding_id=str(uuid.uuid4()), source_id=source_id,
                endpoint_id=candidate_id, event_id=event_id, created_by="operator:different",
            )


def test_b_binding_rejected_when_timestamps_diverge(tmp_path: Path) -> None:
    store, authority, source_id = create_authority(tmp_path)
    _force_attested_matching(store, source_id)
    candidate_id = insert_endpoint(store, source_id)
    event_id = str(uuid.uuid4())

    with store._transaction() as connection:
        _insert_valid_candidate_event(
            connection, event_id=event_id, source_id=source_id, endpoint_id=candidate_id,
        )

    with pytest.raises(
        sqlite3.IntegrityError, match="matching accepted candidate_check event"
    ):
        with store._transaction() as connection:
            _insert_matching_candidate_binding(
                connection, binding_id=str(uuid.uuid4()), source_id=source_id,
                endpoint_id=candidate_id, event_id=event_id,
                matched_at="2026-08-16T13:00:00+00:00",
            )


@pytest.mark.parametrize(
    ("resulting_epoch", "resulting_relationship_gate"),
    ((2, None), (None, "mismatch_pending_reattestation")),
    ids=("resulting_epoch_set", "resulting_relationship_gate_set"),
)
def test_b_binding_rejected_when_event_claims_a_transition(
    tmp_path: Path, resulting_epoch: int | None, resulting_relationship_gate: str | None
) -> None:
    """An 'accepted' candidate_check event can never legitimately claim an
    epoch bump or a relationship-gate transition -- a candidate match is
    retained evidence only, never a security-context transition."""

    store, authority, source_id = create_authority(tmp_path)
    _force_attested_matching(store, source_id)
    candidate_id = insert_endpoint(store, source_id)
    event_id = str(uuid.uuid4())

    with store._transaction() as connection:
        _insert_valid_candidate_event(
            connection, event_id=event_id, source_id=source_id, endpoint_id=candidate_id,
            resulting_epoch=resulting_epoch,
            resulting_relationship_gate=resulting_relationship_gate,
        )

    with pytest.raises(
        sqlite3.IntegrityError, match="matching accepted candidate_check event"
    ):
        with store._transaction() as connection:
            _insert_matching_candidate_binding(
                connection, binding_id=str(uuid.uuid4()), source_id=source_id,
                endpoint_id=candidate_id, event_id=event_id,
            )


# ---------------------------------------------------------------------------
# Part C: live-authority-at-insert defense in depth
# ---------------------------------------------------------------------------


def test_c_binding_rejected_when_source_is_not_currently_attested(tmp_path: Path) -> None:
    store, authority, source_id = create_authority(tmp_path)
    candidate_id = insert_endpoint(store, source_id)
    event_id = str(uuid.uuid4())

    with store._transaction() as connection:
        # A superficially well-formed accepted event, but the live source
        # was never actually enrolled.
        _insert_valid_candidate_event(
            connection, event_id=event_id, source_id=source_id, endpoint_id=candidate_id,
        )

    with pytest.raises(
        sqlite3.IntegrityError, match="currently eligible under the exact binding epoch"
    ):
        with store._transaction() as connection:
            _insert_matching_candidate_binding(
                connection, binding_id=str(uuid.uuid4()), source_id=source_id,
                endpoint_id=candidate_id, event_id=event_id,
            )
    assert store.list_candidate_attestation_bindings(source_id) == ()


def test_c_binding_rejected_when_live_epoch_has_since_advanced(tmp_path: Path) -> None:
    store, authority, source_id = create_authority(tmp_path)
    _force_attested_matching(store, source_id)
    candidate_id = insert_endpoint(store, source_id)
    event_id = str(uuid.uuid4())

    with store._transaction() as connection:
        _insert_valid_candidate_event(
            connection, event_id=event_id, source_id=source_id, endpoint_id=candidate_id,
        )
        # The live source has since moved to epoch 2 (e.g. a concurrent
        # accepted anchor change), while this event/binding attempt is
        # still scoped to the old epoch 1.
        connection.execute(
            "UPDATE source_attestation_state SET source_attestation_epoch=2 "
            "WHERE inventory_source_id=?",
            (source_id,),
        )

    with pytest.raises(
        sqlite3.IntegrityError, match="currently eligible under the exact binding epoch"
    ):
        with store._transaction() as connection:
            _insert_matching_candidate_binding(
                connection, binding_id=str(uuid.uuid4()), source_id=source_id,
                endpoint_id=candidate_id, event_id=event_id,
            )


def test_c_binding_rejected_when_live_relationship_gate_is_pending(tmp_path: Path) -> None:
    store, authority, source_id = create_authority(tmp_path)
    _force_attested_matching(store, source_id)
    candidate_id = insert_endpoint(store, source_id)
    event_id = str(uuid.uuid4())

    with store._transaction() as connection:
        _insert_valid_candidate_event(
            connection, event_id=event_id, source_id=source_id, endpoint_id=candidate_id,
        )
        connection.execute(
            "UPDATE source_attestation_state SET relationship_gate="
            "'mismatch_pending_reattestation' WHERE inventory_source_id=?",
            (source_id,),
        )

    with pytest.raises(
        sqlite3.IntegrityError, match="currently eligible under the exact binding epoch"
    ):
        with store._transaction() as connection:
            _insert_matching_candidate_binding(
                connection, binding_id=str(uuid.uuid4()), source_id=source_id,
                endpoint_id=candidate_id, event_id=event_id,
            )


def test_c_live_anchor_cannot_diverge_from_event_at_the_same_epoch(tmp_path: Path) -> None:
    """Composite defense-in-depth witness: Part D's epoch-bump trigger
    already makes "live anchor differs from a retained event's asserted
    anchor while the epoch is unchanged" an unreachable state in this
    schema (anchor and epoch are 1:1 coupled by construction) -- so the
    live-eligible-source trigger's own anchor-equality clause (Part C)
    can never even be exercised in isolation from the epoch clause it
    already shares. Proving the underlying mutation itself is rejected is
    the correct, stronger witness for this scenario."""

    store, authority, source_id = create_authority(tmp_path)
    _force_attested_matching(store, source_id)  # anchor_value='deadbeef', epoch=1
    candidate_id = insert_endpoint(store, source_id)
    event_id = str(uuid.uuid4())

    with store._transaction() as connection:
        _insert_valid_candidate_event(
            connection, event_id=event_id, source_id=source_id, endpoint_id=candidate_id,
        )

    with pytest.raises(
        sqlite3.IntegrityError, match="security-context change requires an epoch bump"
    ):
        with store._transaction() as connection:
            connection.execute(
                "UPDATE source_attestation_state SET anchor_value='rotated-out-of-band' "
                "WHERE inventory_source_id=?",
                (source_id,),
            )

    # The event/candidate-binding path is consequently unaffected: the
    # live anchor is still exactly what the retained event asserted.
    assert store.attestation_state(source_id).anchor_value == "deadbeef"
    with store._transaction() as connection:
        _insert_matching_candidate_binding(
            connection, binding_id=str(uuid.uuid4()), source_id=source_id,
            endpoint_id=candidate_id, event_id=event_id,
        )
    assert len(store.list_candidate_attestation_bindings(source_id, endpoint_id=candidate_id)) == 1


def test_c_binding_insert_time_only_does_not_invalidate_historical_rows(
    tmp_path: Path,
) -> None:
    """Part C is INSERT-time only: a binding legitimately created while
    live-eligible remains physically retained/readable after the live
    source state later becomes ineligible -- the trigger never re-fires
    on a read or on unrelated later writes."""

    store, authority, source_id = create_authority(tmp_path)
    enroll(store, authority, source_id, "deadbeef")
    candidate_id = insert_endpoint(store, source_id)
    authority.check_candidate_attestation(
        source_id, endpoint_id=candidate_id, actor="operator:test",
        evidence_reader=FakeEvidenceReader(observed("deadbeef")),
    )
    binding = store.list_candidate_attestation_bindings(source_id, endpoint_id=candidate_id)[0]

    endpoint_id = active_endpoint_id(store, source_id)
    authority.revoke_source_attestation(source_id, actor="operator:revoke", reason="reset")
    assert store.attestation_state(source_id).attestation_status is (
        SourceAttestationStatus.NOT_YET_ATTESTED
    )

    retained = store.list_candidate_attestation_bindings(source_id, endpoint_id=candidate_id)[0]
    assert retained == binding


# ---------------------------------------------------------------------------
# Part D: epoch as trust-context token defense in depth
# ---------------------------------------------------------------------------


def test_d_anchor_change_without_epoch_bump_rejected(tmp_path: Path) -> None:
    store, authority, source_id = create_authority(tmp_path)
    enroll(store, authority, source_id, "deadbeef")

    with pytest.raises(
        sqlite3.IntegrityError, match="security-context change requires an epoch bump"
    ):
        with store._transaction() as connection:
            connection.execute(
                "UPDATE source_attestation_state SET anchor_value='rotated-in-place' "
                "WHERE inventory_source_id=?",
                (source_id,),
            )
    assert store.attestation_state(source_id).anchor_value == "deadbeef"


def test_d_status_change_without_epoch_bump_rejected(tmp_path: Path) -> None:
    store, authority, source_id = create_authority(tmp_path)
    enroll(store, authority, source_id, "deadbeef")

    with pytest.raises(
        sqlite3.IntegrityError, match="security-context change requires an epoch bump"
    ):
        with store._transaction() as connection:
            connection.execute(
                "UPDATE source_attestation_state SET attestation_status='not_yet_attested' "
                "WHERE inventory_source_id=?",
                (source_id,),
            )
    assert store.attestation_state(source_id).attestation_status is (
        SourceAttestationStatus.ATTESTED
    )


def test_d_every_legitimate_status_or_anchor_change_advances_epoch(tmp_path: Path) -> None:
    store, authority, source_id = create_authority(tmp_path)
    endpoint_id = active_endpoint_id(store, source_id)

    # Initial enrollment: not_yet_attested/0 -> attested/1.
    authority.enroll_source_attestation(
        source_id, endpoint_id=endpoint_id, actor="operator:enroll",
        evidence_reader=FakeEvidenceReader(observed("deadbeef")),
    )
    assert store.attestation_state(source_id).source_attestation_epoch == 1

    # Accepted anchor change: attested/1 -> attested/2.
    authority.accept_source_attestation_anchor_change(
        source_id, endpoint_id=endpoint_id, actor="operator:accept",
        evidence_reader=FakeEvidenceReader(observed("cafef00d")),
    )
    assert store.attestation_state(source_id).source_attestation_epoch == 2

    # Revocation: attested/2 -> not_yet_attested/3.
    authority.revoke_source_attestation(source_id, actor="operator:revoke", reason="reset")
    assert store.attestation_state(source_id).source_attestation_epoch == 3
    assert store.attestation_state(source_id).attestation_status is (
        SourceAttestationStatus.NOT_YET_ATTESTED
    )


def test_d_mismatch_and_reconfirm_remain_epoch_unchanged_and_legal(tmp_path: Path) -> None:
    """The new epoch-bump trigger is scoped to attestation_status/anchor
    columns only -- it must never fire for the legitimate epoch-unchanged
    transitions (mismatch -> pending gate; same-anchor reconfirm)."""

    store, authority, source_id = create_authority(tmp_path)
    enroll(store, authority, source_id, "deadbeef")
    endpoint_id = active_endpoint_id(store, source_id)

    match_event = authority.reattest_source(
        source_id, endpoint_id=endpoint_id, actor="operator:reconfirm",
        evidence_reader=FakeEvidenceReader(observed("deadbeef")),
    )
    assert match_event.outcome is AttestationOutcome.MATCH
    assert store.attestation_state(source_id).source_attestation_epoch == 1

    mismatch_event = authority.reattest_source(
        source_id, endpoint_id=endpoint_id, actor="operator:mismatch",
        evidence_reader=FakeEvidenceReader(observed("cafef00d")),
    )
    assert mismatch_event.outcome is AttestationOutcome.MISMATCH
    state = store.attestation_state(source_id)
    assert state.source_attestation_epoch == 1  # unchanged
    assert state.relationship_gate is SourceAttestationRelationshipGate.MISMATCH_PENDING_REATTESTATION
    assert state.anchor_value == "deadbeef"  # unchanged
