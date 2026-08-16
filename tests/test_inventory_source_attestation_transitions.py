"""WAVE C1 Commit 2: explicit operator-driven source attestation transitions.

Covers ADR 0003 SS12, 16, 17, 18, 19, 19a, 20, 21, 27, 28, 29 for the four
SOURCE-level transitions only: initial enrollment, re-attestation (same
anchor / mismatch), the separate explicit accept-new-anchor decision, and
explicit revocation/reset. Candidate endpoint attestation (Commit 4) and
discovery-completion epoch CAS/freshness fencing (Commit 3) are out of
scope here.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.inventory import (
    AttestationEvidenceTier,
    AttestationOperation,
    AttestationOutcome,
    AuthorityConflict,
    BaselineCompleteness,
    BaselineMode,
    DetailReadStatus,
    DiscoveredNode,
    DiscoveredResource,
    DiscoveryRunLifecycle,
    InventoryAuthority,
    InventoryAuthorityStore,
    NormalizedDiscoverySnapshot,
    SourceAttestationEvidenceReading,
    SourceAttestationReadOutcome,
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
) -> tuple[InventoryAuthorityStore, InventoryAuthority, str]:
    store = InventoryAuthorityStore(tmp_path / "authority.db", now=fixed_now)
    authority = InventoryAuthority(store, now=fixed_now)
    state = authority.create_inventory_source(
        provider_kind="proxmox",
        display_name="Primary",
        credential_reference="secret://inventory/primary",
        transport_locator="https://pve.example:8006",
    )
    return store, authority, state.source.inventory_source_id


def active_endpoint_id(store: InventoryAuthorityStore, source_id: str) -> str:
    return store.source_state(source_id).active_endpoint.endpoint_id


@dataclass
class FakeEvidenceReader:
    """Deterministic evidence reader with an optional pre-return callback.

    ``mutate``, when set, is invoked from inside ``read()`` -- i.e. after
    the authority layer has already left phase 1 and is calling into this
    reader, entirely outside any write transaction. Used to inject races
    between the read and the phase-3 CAS.
    """

    reading: SourceAttestationEvidenceReading
    mutate: Callable[[], None] | None = None
    calls: list[dict] = field(default_factory=list)

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


def complete_snapshot(
    store: InventoryAuthorityStore, authority: InventoryAuthority, source_id: str
) -> None:
    """Issue and successfully finalize one minimal discovery run, so the
    source becomes healthy/fresh with one resource."""

    run = authority.issue_discovery_run(source_id, 1)
    authority.mark_discovery_run_running(source_id, run.run_id)
    nodes = (DiscoveredNode("pve-a", "online", True, FIXED_NOW.isoformat(), {}),)
    resource = DiscoveredResource(
        inventory_source_id=source_id,
        vmid=100,
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
            ({"vmid": 100, "type": "qemu", "node": "pve-a"},)
        ),
    )
    authority.finalize_successful_discovery_run(source_id, run.run_id, snapshot)


# ---------------------------------------------------------------------------
# 1-4: initial enrollment
# ---------------------------------------------------------------------------


def test_initial_enrollment_epoch_0_to_1(tmp_path: Path) -> None:
    store, authority, source_id = create_authority(tmp_path)
    endpoint_id = active_endpoint_id(store, source_id)
    reader = FakeEvidenceReader(reading=observed("deadbeef"))

    event = authority.enroll_source_attestation(
        source_id, endpoint_id=endpoint_id, actor="operator:test", evidence_reader=reader
    )

    assert event.outcome is AttestationOutcome.ACCEPTED
    assert event.previous_epoch == 0
    assert event.resulting_epoch == 1
    assert event.operation is AttestationOperation.ENROLLMENT

    state = store.attestation_state(source_id)
    assert state.attestation_status is SourceAttestationStatus.ATTESTED
    assert state.source_attestation_epoch == 1
    assert state.anchor_kind == ANCHOR_KIND_PVE_ROOT_CA_SHA256_FINGERPRINT
    assert state.anchor_value == "deadbeef"
    assert state.evidence_tier is AttestationEvidenceTier.TIER_1
    assert state.tier2_evaluation is TierTwoEvaluationStatus.NOT_EVALUATED
    assert state.accepted_at == FIXED_NOW.isoformat()
    assert state.accepted_by == "operator:test"
    assert state.evaluated_endpoint_id == endpoint_id


def test_initial_enrollment_never_happens_automatically(tmp_path: Path) -> None:
    """Discovery itself has no attestation call anywhere in its API; only an
    explicit operator call can enroll a source."""

    store, authority, source_id = create_authority(tmp_path)
    complete_snapshot(store, authority, source_id)
    assert store.attestation_state(source_id).attestation_status is (
        SourceAttestationStatus.NOT_YET_ATTESTED
    )
    assert store.attestation_state(source_id).source_attestation_epoch == 0


def test_initial_enrollment_audit_persisted(tmp_path: Path) -> None:
    store, authority, source_id = create_authority(tmp_path)
    endpoint_id = active_endpoint_id(store, source_id)
    reader = FakeEvidenceReader(reading=observed("deadbeef"))

    event = authority.enroll_source_attestation(
        source_id, endpoint_id=endpoint_id, actor="operator:test", evidence_reader=reader
    )

    events = store.list_attestation_events(source_id)
    assert len(events) == 1
    assert events[0] == event
    assert events[0].asserted_anchor_value == "deadbeef"
    assert events[0].target_endpoint_id == endpoint_id


def test_unavailable_enrollment_no_state_change_audit_retained(tmp_path: Path) -> None:
    store, authority, source_id = create_authority(tmp_path)
    endpoint_id = active_endpoint_id(store, source_id)
    reader = FakeEvidenceReader(reading=unavailable())

    event = authority.enroll_source_attestation(
        source_id, endpoint_id=endpoint_id, actor="operator:test", evidence_reader=reader
    )

    assert event.outcome is AttestationOutcome.UNAVAILABLE
    assert event.resulting_epoch is None
    state = store.attestation_state(source_id)
    assert state.attestation_status is SourceAttestationStatus.NOT_YET_ATTESTED
    assert state.source_attestation_epoch == 0
    assert len(store.list_attestation_events(source_id)) == 1


def test_malformed_enrollment_no_state_change_audit_retained(tmp_path: Path) -> None:
    store, authority, source_id = create_authority(tmp_path)
    endpoint_id = active_endpoint_id(store, source_id)
    reader = FakeEvidenceReader(reading=malformed())

    event = authority.enroll_source_attestation(
        source_id, endpoint_id=endpoint_id, actor="operator:test", evidence_reader=reader
    )

    assert event.outcome is AttestationOutcome.MALFORMED
    assert event.resulting_epoch is None
    state = store.attestation_state(source_id)
    assert state.attestation_status is SourceAttestationStatus.NOT_YET_ATTESTED
    assert state.source_attestation_epoch == 0
    assert len(store.list_attestation_events(source_id)) == 1


# ---------------------------------------------------------------------------
# 5: reader called outside the write transaction
# ---------------------------------------------------------------------------


def test_reader_called_outside_write_transaction(tmp_path: Path) -> None:
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
    endpoint_id = state.active_endpoint.endpoint_id

    concurrent_store = InventoryAuthorityStore(path, now=fixed_now)
    concurrent_authority = InventoryAuthority(concurrent_store, now=fixed_now)
    concurrent_write_succeeded = False

    def concurrent_write() -> None:
        nonlocal concurrent_write_succeeded
        # If enroll_source_attestation were (incorrectly) holding a write
        # transaction open during evidence_reader.read(), this concurrent
        # BEGIN IMMEDIATE write from a second connection would block/fail.
        # A display-name rename touches no attestation CAS field, so the
        # enrollment itself is unaffected by this concurrent write.
        concurrent_authority.rename_inventory_source(source_id, "Renamed during read")
        concurrent_write_succeeded = True

    reader = FakeEvidenceReader(reading=observed("deadbeef"), mutate=concurrent_write)

    event = authority.enroll_source_attestation(
        source_id, endpoint_id=endpoint_id, actor="operator:test", evidence_reader=reader
    )

    assert concurrent_write_succeeded is True
    assert event.outcome is AttestationOutcome.ACCEPTED


# ---------------------------------------------------------------------------
# 6-9: stale-CAS races between phase 1 read and phase 3 write
# ---------------------------------------------------------------------------


def test_source_config_revision_change_during_read_is_stale_cas(tmp_path: Path) -> None:
    store, authority, source_id = create_authority(tmp_path)
    endpoint_id = active_endpoint_id(store, source_id)

    def mutate() -> None:
        InventoryAuthority(store, now=fixed_now).rotate_credential_reference(
            source_id, "secret://inventory/rotated"
        )

    reader = FakeEvidenceReader(reading=observed("deadbeef"), mutate=mutate)

    with pytest.raises(AuthorityConflict, match="context changed"):
        authority.enroll_source_attestation(
            source_id, endpoint_id=endpoint_id, actor="operator:test", evidence_reader=reader
        )

    state = store.attestation_state(source_id)
    assert state.attestation_status is SourceAttestationStatus.NOT_YET_ATTESTED
    assert state.source_attestation_epoch == 0
    events = store.list_attestation_events(source_id)
    assert len(events) == 1
    assert events[0].outcome is AttestationOutcome.STALE_CAS


def test_endpoint_context_change_during_read_is_stale_cas(tmp_path: Path) -> None:
    store, authority, source_id = create_authority(tmp_path)
    endpoint_id = active_endpoint_id(store, source_id)
    migrating_authority = InventoryAuthority(
        store, now=fixed_now, _test_migration_contracts={2: lambda locator: locator + "/v2"}
    )

    def mutate() -> None:
        migrating_authority.migrate_canonicalization_contract(source_id, 2)

    reader = FakeEvidenceReader(reading=observed("deadbeef"), mutate=mutate)

    with pytest.raises(AuthorityConflict, match="context changed"):
        authority.enroll_source_attestation(
            source_id, endpoint_id=endpoint_id, actor="operator:test", evidence_reader=reader
        )

    events = store.list_attestation_events(source_id)
    assert events[0].outcome is AttestationOutcome.STALE_CAS
    assert store.attestation_state(source_id).source_attestation_epoch == 0


def test_transport_trust_revision_change_during_read_is_stale_cas(tmp_path: Path) -> None:
    store, authority, source_id = create_authority(tmp_path)
    endpoint_id = active_endpoint_id(store, source_id)

    def mutate() -> None:
        InventoryAuthority(store, now=fixed_now).rotate_transport_trust(source_id)

    reader = FakeEvidenceReader(reading=observed("deadbeef"), mutate=mutate)

    with pytest.raises(AuthorityConflict, match="context changed"):
        authority.enroll_source_attestation(
            source_id, endpoint_id=endpoint_id, actor="operator:test", evidence_reader=reader
        )

    events = store.list_attestation_events(source_id)
    assert events[0].outcome is AttestationOutcome.STALE_CAS
    assert store.attestation_state(source_id).source_attestation_epoch == 0


def test_source_attestation_epoch_change_during_read_is_stale_cas_no_overwrite(
    tmp_path: Path,
) -> None:
    store, authority, source_id = create_authority(tmp_path)
    endpoint_id = active_endpoint_id(store, source_id)

    def mutate() -> None:
        with store._transaction() as connection:
            connection.execute(
                "UPDATE source_attestation_state SET source_attestation_epoch="
                "source_attestation_epoch+1 WHERE inventory_source_id=?",
                (source_id,),
            )

    reader = FakeEvidenceReader(reading=observed("deadbeef"), mutate=mutate)

    with pytest.raises(AuthorityConflict, match="context changed"):
        authority.enroll_source_attestation(
            source_id, endpoint_id=endpoint_id, actor="operator:test", evidence_reader=reader
        )

    # The raw epoch bump from mutate() survives (it was never overwritten by
    # a partial/late enrollment write); enrollment itself never applied.
    state = store.attestation_state(source_id)
    assert state.source_attestation_epoch == 1
    assert state.attestation_status is SourceAttestationStatus.NOT_YET_ATTESTED
    events = store.list_attestation_events(source_id)
    assert events[0].outcome is AttestationOutcome.STALE_CAS
    assert events[0].previous_epoch == 0
    assert events[0].resulting_epoch is None


# ---------------------------------------------------------------------------
# 10-13: re-attestation -- same anchor and mismatch
# ---------------------------------------------------------------------------


def _enroll(
    store: InventoryAuthorityStore, authority: InventoryAuthority, source_id: str, anchor: str = "deadbeef"
) -> None:
    endpoint_id = active_endpoint_id(store, source_id)
    authority.enroll_source_attestation(
        source_id,
        endpoint_id=endpoint_id,
        actor="operator:enroll",
        evidence_reader=FakeEvidenceReader(reading=observed(anchor)),
    )


def test_same_anchor_reattestation_epoch_unchanged(tmp_path: Path) -> None:
    store, authority, source_id = create_authority(tmp_path)
    _enroll(store, authority, source_id, "deadbeef")
    endpoint_id = active_endpoint_id(store, source_id)

    event = authority.reattest_source(
        source_id,
        endpoint_id=endpoint_id,
        actor="operator:reattest",
        evidence_reader=FakeEvidenceReader(reading=observed("deadbeef")),
    )

    assert event.outcome is AttestationOutcome.MATCH
    assert event.previous_epoch == 1
    assert event.resulting_epoch is None
    state = store.attestation_state(source_id)
    assert state.source_attestation_epoch == 1
    assert state.anchor_value == "deadbeef"
    assert state.accepted_by == "operator:enroll"  # untouched by the reconfirm


def test_same_anchor_reattestation_no_freshness_invalidation(tmp_path: Path) -> None:
    store, authority, source_id = create_authority(tmp_path)
    _enroll(store, authority, source_id, "deadbeef")
    complete_snapshot(store, authority, source_id)
    health_before = store.source_state(source_id).runtime_health
    assert health_before.health == "healthy"
    assert health_before.freshness == "fresh"
    endpoint_id = active_endpoint_id(store, source_id)

    authority.reattest_source(
        source_id,
        endpoint_id=endpoint_id,
        actor="operator:reattest",
        evidence_reader=FakeEvidenceReader(reading=observed("deadbeef")),
    )

    health_after = store.source_state(source_id).runtime_health
    assert health_after == health_before


def test_same_anchor_reattestation_no_published_revision_churn(tmp_path: Path) -> None:
    store, authority, source_id = create_authority(tmp_path)
    _enroll(store, authority, source_id, "deadbeef")
    endpoint_id = active_endpoint_id(store, source_id)
    before = store.backend_instance()

    authority.reattest_source(
        source_id,
        endpoint_id=endpoint_id,
        actor="operator:reattest",
        evidence_reader=FakeEvidenceReader(reading=observed("deadbeef")),
    )

    after = store.backend_instance()
    assert after.inventory_revision == before.inventory_revision
    assert after.published_state_revision == before.published_state_revision


def test_mismatch_audited_anchor_and_epoch_unchanged(tmp_path: Path) -> None:
    store, authority, source_id = create_authority(tmp_path)
    _enroll(store, authority, source_id, "deadbeef")
    endpoint_id = active_endpoint_id(store, source_id)
    before = store.backend_instance()

    event = authority.reattest_source(
        source_id,
        endpoint_id=endpoint_id,
        actor="operator:reattest",
        evidence_reader=FakeEvidenceReader(reading=observed("cafef00d")),
    )

    assert event.outcome is AttestationOutcome.MISMATCH
    assert event.resulting_epoch is None
    state = store.attestation_state(source_id)
    assert state.anchor_value == "deadbeef"
    assert state.source_attestation_epoch == 1
    assert state.attestation_status is SourceAttestationStatus.ATTESTED
    after = store.backend_instance()
    assert after == before


# ---------------------------------------------------------------------------
# 14-16: explicit accept-new-anchor
# ---------------------------------------------------------------------------


def test_explicit_accept_new_anchor_exact_epoch_increment(tmp_path: Path) -> None:
    store, authority, source_id = create_authority(tmp_path)
    _enroll(store, authority, source_id, "deadbeef")
    endpoint_id = active_endpoint_id(store, source_id)

    event = authority.accept_source_attestation_anchor_change(
        source_id,
        endpoint_id=endpoint_id,
        actor="operator:accept",
        evidence_reader=FakeEvidenceReader(reading=observed("cafef00d")),
    )

    assert event.outcome is AttestationOutcome.ACCEPTED
    assert event.previous_epoch == 1
    assert event.resulting_epoch == 2
    state = store.attestation_state(source_id)
    assert state.source_attestation_epoch == 2
    assert state.anchor_value == "cafef00d"
    assert state.accepted_by == "operator:accept"

    old_events = [
        e for e in store.list_attestation_events(source_id) if e.outcome is AttestationOutcome.ACCEPTED
    ]
    assert len(old_events) == 2  # initial enrollment + this anchor change, both retained


def test_reattest_never_silently_accepts_mismatch(tmp_path: Path) -> None:
    """reattest_source must never accept a new anchor by itself."""

    store, authority, source_id = create_authority(tmp_path)
    _enroll(store, authority, source_id, "deadbeef")
    endpoint_id = active_endpoint_id(store, source_id)

    authority.reattest_source(
        source_id,
        endpoint_id=endpoint_id,
        actor="operator:reattest",
        evidence_reader=FakeEvidenceReader(reading=observed("cafef00d")),
    )

    assert store.attestation_state(source_id).anchor_value == "deadbeef"
    assert store.attestation_state(source_id).source_attestation_epoch == 1


def test_accept_new_anchor_that_actually_matches_degrades_to_reconfirm(
    tmp_path: Path,
) -> None:
    """SS20's deterministic epoch rule is not a caller choice: even the
    explicit accept-path must not bump epoch if the read turns out to
    match the currently enrolled anchor after all."""

    store, authority, source_id = create_authority(tmp_path)
    _enroll(store, authority, source_id, "deadbeef")
    endpoint_id = active_endpoint_id(store, source_id)

    event = authority.accept_source_attestation_anchor_change(
        source_id,
        endpoint_id=endpoint_id,
        actor="operator:accept",
        evidence_reader=FakeEvidenceReader(reading=observed("deadbeef")),
    )

    assert event.outcome is AttestationOutcome.MATCH
    assert event.resulting_epoch is None
    assert store.attestation_state(source_id).source_attestation_epoch == 1


def test_accepted_anchor_change_health_stale_and_revision_effects(tmp_path: Path) -> None:
    store, authority, source_id = create_authority(tmp_path)
    _enroll(store, authority, source_id, "deadbeef")
    complete_snapshot(store, authority, source_id)
    health_before = store.source_state(source_id).runtime_health
    assert health_before.health == "healthy"
    assert health_before.freshness == "fresh"
    before = store.backend_instance()
    endpoint_id = active_endpoint_id(store, source_id)

    authority.accept_source_attestation_anchor_change(
        source_id,
        endpoint_id=endpoint_id,
        actor="operator:accept",
        evidence_reader=FakeEvidenceReader(reading=observed("cafef00d")),
    )

    health_after = store.source_state(source_id).runtime_health
    after = store.backend_instance()
    assert health_after.freshness == "stale"
    assert health_after.health_origin == "controlled_context_transition"
    assert "attestation" in health_after.health_reason
    assert after.published_state_revision == before.published_state_revision + 1
    assert after.inventory_revision == before.inventory_revision


def test_initial_enrollment_has_same_freshness_invalidation_behavior(tmp_path: Path) -> None:
    """ADR 0003 SS12's concrete witness: committing successfully while
    epoch=0 must not read as fresh once enrollment bumps it to 1."""

    store, authority, source_id = create_authority(tmp_path)
    complete_snapshot(store, authority, source_id)
    health_before = store.source_state(source_id).runtime_health
    assert health_before.health == "healthy"
    assert health_before.freshness == "fresh"
    before = store.backend_instance()
    endpoint_id = active_endpoint_id(store, source_id)

    authority.enroll_source_attestation(
        source_id,
        endpoint_id=endpoint_id,
        actor="operator:enroll",
        evidence_reader=FakeEvidenceReader(reading=observed("deadbeef")),
    )

    health_after = store.source_state(source_id).runtime_health
    after = store.backend_instance()
    assert health_after.freshness == "stale"
    assert health_after.health_origin == "controlled_context_transition"
    assert after.published_state_revision == before.published_state_revision + 1
    assert after.inventory_revision == before.inventory_revision


# ---------------------------------------------------------------------------
# 17: explicit revocation/reset
# ---------------------------------------------------------------------------


def test_explicit_revocation_epoch_increment_no_source_resource_replacement(
    tmp_path: Path,
) -> None:
    store, authority, source_id = create_authority(tmp_path)
    _enroll(store, authority, source_id, "deadbeef")
    complete_snapshot(store, authority, source_id)
    resource_before = store.list_resources(source_id)[0]
    before = store.backend_instance()

    event = authority.revoke_source_attestation(
        source_id, actor="operator:revoke", reason="suspected CA compromise"
    )

    assert event.outcome is AttestationOutcome.ACCEPTED
    assert event.operation is AttestationOperation.REVOCATION
    assert event.previous_epoch == 1
    assert event.resulting_epoch == 2
    assert event.reason == "suspected CA compromise"

    state = store.attestation_state(source_id)
    assert state.attestation_status is SourceAttestationStatus.NOT_YET_ATTESTED
    assert state.source_attestation_epoch == 2
    assert state.anchor_kind is None
    assert state.anchor_value is None
    assert state.evidence_tier is None
    assert state.tier2_evaluation is None
    assert state.accepted_at is None
    assert state.accepted_by is None
    assert state.evaluated_endpoint_id is None

    # Source/endpoint identity and discovered resources are untouched.
    assert store.source_state(source_id).source.inventory_source_id == source_id
    resource_after = store.list_resources(source_id)[0]
    assert resource_after.resource_id == resource_before.resource_id
    assert resource_after.active_binding_id == resource_before.active_binding_id
    assert resource_after.presence == resource_before.presence
    assert resource_after.observational_continuity == resource_before.observational_continuity

    after = store.backend_instance()
    assert after.published_state_revision == before.published_state_revision + 1
    assert after.inventory_revision == before.inventory_revision

    # Audit history is retained, not deleted.
    events = store.list_attestation_events(source_id)
    assert len(events) == 2  # enrollment + revocation


def test_revocation_requires_currently_attested_source(tmp_path: Path) -> None:
    store, authority, source_id = create_authority(tmp_path)
    with pytest.raises(AuthorityConflict, match="not currently attested"):
        authority.revoke_source_attestation(
            source_id, actor="operator:revoke", reason="no-op"
        )


def test_source_remains_fail_closed_for_reattestation_after_revocation(
    tmp_path: Path,
) -> None:
    store, authority, source_id = create_authority(tmp_path)
    _enroll(store, authority, source_id, "deadbeef")
    authority.revoke_source_attestation(source_id, actor="operator:revoke", reason="reset")
    endpoint_id = active_endpoint_id(store, source_id)

    with pytest.raises(AuthorityConflict, match="has not yet been enrolled"):
        authority.reattest_source(
            source_id,
            endpoint_id=endpoint_id,
            actor="operator:reattest",
            evidence_reader=FakeEvidenceReader(reading=observed("deadbeef")),
        )

    # Re-enrollment after revocation is possible and carries the epoch
    # forward (never resets to the pristine 0 sentinel).
    event = authority.enroll_source_attestation(
        source_id,
        endpoint_id=endpoint_id,
        actor="operator:enroll-again",
        evidence_reader=FakeEvidenceReader(reading=observed("newanchor")),
    )
    assert event.previous_epoch == 2
    assert event.resulting_epoch == 3
    assert store.attestation_state(source_id).attestation_status is (
        SourceAttestationStatus.ATTESTED
    )


# ---------------------------------------------------------------------------
# 18-19: active discovery run serialization
# ---------------------------------------------------------------------------


def test_epoch_changing_operation_fences_active_discovery_run(tmp_path: Path) -> None:
    store, authority, source_id = create_authority(tmp_path)
    endpoint_id = active_endpoint_id(store, source_id)
    run = authority.issue_discovery_run(source_id, 1)
    assert store.source_state(source_id).source.active_discovery_run_id == run.run_id

    authority.enroll_source_attestation(
        source_id,
        endpoint_id=endpoint_id,
        actor="operator:enroll",
        evidence_reader=FakeEvidenceReader(reading=observed("deadbeef")),
    )

    assert store.source_state(source_id).source.active_discovery_run_id is None
    fenced_run = store.discovery_run(run.run_id)
    assert fenced_run.lifecycle is DiscoveryRunLifecycle.ABANDONED
    assert fenced_run.terminal_reason == "attestation_epoch_transition"
    assert fenced_run.completed_at is None  # abandoned, not completed


def test_same_anchor_reconfirm_does_not_fence_active_run(tmp_path: Path) -> None:
    store, authority, source_id = create_authority(tmp_path)
    _enroll(store, authority, source_id, "deadbeef")
    endpoint_id = active_endpoint_id(store, source_id)
    run = authority.issue_discovery_run(source_id, 1)

    authority.reattest_source(
        source_id,
        endpoint_id=endpoint_id,
        actor="operator:reattest",
        evidence_reader=FakeEvidenceReader(reading=observed("deadbeef")),
    )

    current = store.discovery_run(run.run_id)
    assert current.lifecycle is DiscoveryRunLifecycle.ISSUED
    assert store.source_state(source_id).source.active_discovery_run_id == run.run_id


def test_revocation_fences_active_discovery_run(tmp_path: Path) -> None:
    store, authority, source_id = create_authority(tmp_path)
    _enroll(store, authority, source_id, "deadbeef")
    run = authority.issue_discovery_run(source_id, 1)

    authority.revoke_source_attestation(source_id, actor="operator:revoke", reason="reset")

    assert store.source_state(source_id).source.active_discovery_run_id is None
    fenced_run = store.discovery_run(run.run_id)
    assert fenced_run.lifecycle is DiscoveryRunLifecycle.ABANDONED


# ---------------------------------------------------------------------------
# 20: transaction-failure atomicity
# ---------------------------------------------------------------------------


def test_injected_transaction_failure_leaves_no_partial_state(tmp_path: Path) -> None:
    class FailingAuthority(InventoryAuthority):
        def _after_attestation_transition(self, connection, *, event_id: str) -> None:
            raise RuntimeError("injected attestation transition failure")

    store, base_authority, source_id = create_authority(tmp_path)
    endpoint_id = active_endpoint_id(store, source_id)
    failing = FailingAuthority(store, now=fixed_now)
    before = store.backend_instance()
    health_before = store.source_state(source_id).runtime_health

    with pytest.raises(RuntimeError, match="injected attestation transition"):
        failing.enroll_source_attestation(
            source_id,
            endpoint_id=endpoint_id,
            actor="operator:test",
            evidence_reader=FakeEvidenceReader(reading=observed("deadbeef")),
        )

    state = store.attestation_state(source_id)
    assert state.attestation_status is SourceAttestationStatus.NOT_YET_ATTESTED
    assert state.source_attestation_epoch == 0
    assert store.list_attestation_events(source_id) == ()
    assert store.source_state(source_id).runtime_health == health_before
    assert store.backend_instance() == before


def test_injected_transaction_failure_during_revocation_leaves_no_partial_state(
    tmp_path: Path,
) -> None:
    class FailingAuthority(InventoryAuthority):
        def _after_attestation_transition(self, connection, *, event_id: str) -> None:
            raise RuntimeError("injected revocation failure")

    store, base_authority, source_id = create_authority(tmp_path)
    _enroll(store, base_authority, source_id, "deadbeef")
    failing = FailingAuthority(store, now=fixed_now)
    before = store.backend_instance()
    attestation_before = store.attestation_state(source_id)
    events_before = store.list_attestation_events(source_id)

    with pytest.raises(RuntimeError, match="injected revocation"):
        failing.revoke_source_attestation(source_id, actor="operator:revoke", reason="reset")

    assert store.attestation_state(source_id) == attestation_before
    assert store.list_attestation_events(source_id) == events_before
    assert store.backend_instance() == before


# ---------------------------------------------------------------------------
# 21-22: discovered inventory identity is untouched by attestation epochs
# ---------------------------------------------------------------------------


def test_unverified_resources_remain_unverified_across_epoch_transition(
    tmp_path: Path,
) -> None:
    store, authority, source_id = create_authority(tmp_path)
    complete_snapshot(store, authority, source_id)
    resource_before = store.list_resources(source_id)[0]
    assert resource_before.security_continuity == "unverified"
    endpoint_id = active_endpoint_id(store, source_id)

    authority.enroll_source_attestation(
        source_id,
        endpoint_id=endpoint_id,
        actor="operator:enroll",
        evidence_reader=FakeEvidenceReader(reading=observed("deadbeef")),
    )
    authority.accept_source_attestation_anchor_change(
        source_id,
        endpoint_id=endpoint_id,
        actor="operator:accept",
        evidence_reader=FakeEvidenceReader(reading=observed("cafef00d")),
    )

    resource_after = store.list_resources(source_id)[0]
    assert resource_after.security_continuity == "unverified"
    assert resource_after.resource_id == resource_before.resource_id
    assert resource_after.active_binding_id == resource_before.active_binding_id
    assert resource_after.locator_generation == resource_before.locator_generation
    assert resource_after.presence == resource_before.presence
    assert resource_after.observational_continuity == resource_before.observational_continuity
    assert resource_after.resource_continuity_revision == resource_before.resource_continuity_revision


# ---------------------------------------------------------------------------
# 23: no secret material in attestation audit
# ---------------------------------------------------------------------------


def test_no_credential_reference_copied_into_attestation_audit(tmp_path: Path) -> None:
    store, authority, source_id = create_authority(tmp_path)
    endpoint_id = active_endpoint_id(store, source_id)
    credential_reference = store.source_state(source_id).source.credential_reference
    assert credential_reference.startswith("secret://")

    authority.enroll_source_attestation(
        source_id,
        endpoint_id=endpoint_id,
        actor="operator:test",
        evidence_reader=FakeEvidenceReader(reading=observed("deadbeef")),
    )
    authority.revoke_source_attestation(
        source_id, actor="operator:revoke", reason="suspected CA compromise"
    )
    authority.enroll_source_attestation(
        source_id,
        endpoint_id=endpoint_id,
        actor="operator:enroll-again",
        evidence_reader=FakeEvidenceReader(reading=observed("newanchor")),
    )

    # No authority code path derives any attestation-event/state field from
    # credential_reference; verify it never appears in any retained text
    # field regardless of what operations ran.
    for event in store.list_attestation_events(source_id):
        for value in (
            event.actor,
            event.reason,
            event.asserted_anchor_kind,
            event.asserted_anchor_value,
            event.endpoint_lifecycle_at_check,
        ):
            assert value is None or credential_reference not in value
    state = store.attestation_state(source_id)
    for value in (state.anchor_kind, state.anchor_value, state.accepted_by):
        assert value is None or credential_reference not in value
