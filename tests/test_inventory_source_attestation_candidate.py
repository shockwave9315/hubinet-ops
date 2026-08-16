"""WAVE C1 Commit 4: candidate endpoint source-attestation checks/bindings.

Covers ADR 0003 SS14, 15, 17, 18, 19a, 20, 29 for the explicit,
operator-driven candidate-endpoint attestation check and its epoch-scoped
durable binding. A binding is retained prerequisite evidence only -- it
never activates, promotes, or replaces the active endpoint, never performs
failover, and never grants workload/resource trust or mutation authority.

TEST-FIXTURE BOUNDARY: no public authority API exists in this repository to
register a new candidate endpoint (ADR 0002's "operator requests a
different transport locator for an existing source" path is not yet
implemented). These tests establish a pre-existing candidate endpoint via
the same narrow raw-SQL INSERT into ``source_endpoints`` already used by
``tests/test_inventory_source_authority.py`` -- the narrowest existing
authoritative mechanism -- rather than inventing any
promotion/registration workflow to make setup convenient.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import sqlite3
import uuid

import pytest

from app.inventory import (
    AttestationEvidenceTier,
    AttestationOperation,
    AttestationOutcome,
    AuthorityConflict,
    InventoryAuthority,
    InventoryAuthorityStore,
    SourceAttestationEvidenceReading,
    SourceAttestationReadOutcome,
    SourceAttestationRelationshipGate,
    SourceAttestationStatus,
    TierTwoEvaluationStatus,
)
from app.inventory.attestation import ANCHOR_KIND_PVE_ROOT_CA_SHA256_FINGERPRINT


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
    canonicalization_version: int = 1,
    transport_trust_revision: int = 1,
) -> str:
    """Narrowest existing test-fixture mechanism for a pre-existing
    endpoint record (no public candidate-registration API exists yet)."""

    endpoint_id = str(uuid.uuid4())
    with store._transaction() as connection:
        connection.execute(
            "INSERT INTO source_endpoints("
            "endpoint_id, inventory_source_id, canonical_transport_locator, "
            "canonicalization_contract_version, lifecycle, transport_trust_revision, "
            "created_at) VALUES(?, ?, ?, ?, ?, ?, ?)",
            (
                endpoint_id,
                source_id,
                locator,
                canonicalization_version,
                lifecycle,
                transport_trust_revision,
                FIXED_NOW.isoformat(),
            ),
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


def force_relationship_gate_pending(store: InventoryAuthorityStore, source_id: str) -> None:
    with store._transaction() as connection:
        connection.execute(
            "UPDATE source_attestation_state SET relationship_gate="
            "'mismatch_pending_reattestation' WHERE inventory_source_id=?",
            (source_id,),
        )


# ---------------------------------------------------------------------------
# 1-5: source and candidate preconditions
# ---------------------------------------------------------------------------


def test_not_yet_attested_source_cannot_candidate_attest(tmp_path: Path) -> None:
    store, authority, source_id = create_authority(tmp_path)
    candidate_id = insert_endpoint(store, source_id)

    with pytest.raises(AuthorityConflict, match="no enrolled attestation anchor"):
        authority.check_candidate_attestation(
            source_id,
            endpoint_id=candidate_id,
            actor="operator:test",
            evidence_reader=FakeEvidenceReader(observed("deadbeef")),
        )
    assert store.list_candidate_attestation_bindings(source_id) == ()


def test_mismatch_pending_source_cannot_candidate_attest(tmp_path: Path) -> None:
    store, authority, source_id = create_authority(tmp_path)
    enroll(store, authority, source_id, "deadbeef")
    force_relationship_gate_pending(store, source_id)
    candidate_id = insert_endpoint(store, source_id)

    with pytest.raises(AuthorityConflict, match="unresolved attestation mismatch"):
        authority.check_candidate_attestation(
            source_id,
            endpoint_id=candidate_id,
            actor="operator:test",
            evidence_reader=FakeEvidenceReader(observed("deadbeef")),
        )
    assert store.list_candidate_attestation_bindings(source_id) == ()


def test_active_endpoint_cannot_be_used_as_candidate(tmp_path: Path) -> None:
    store, authority, source_id = create_authority(tmp_path)
    enroll(store, authority, source_id, "deadbeef")
    active_id = active_endpoint_id(store, source_id)

    with pytest.raises(ValueError, match="not the source's current active endpoint"):
        authority.check_candidate_attestation(
            source_id,
            endpoint_id=active_id,
            actor="operator:test",
            evidence_reader=FakeEvidenceReader(observed("deadbeef")),
        )


def test_endpoint_belonging_to_another_source_rejected(tmp_path: Path) -> None:
    store, authority, source_a = create_authority(tmp_path, name="A", locator="https://a.example:8006")
    _, _, source_b = create_authority(tmp_path, name="B", locator="https://b.example:8006")
    authority_b = InventoryAuthority(store, now=fixed_now)
    enroll(store, authority, source_a, "deadbeef")
    other_source_candidate = insert_endpoint(
        store, source_b, locator="https://b-candidate.example:8006"
    )

    with pytest.raises(Exception):
        authority.check_candidate_attestation(
            source_a,
            endpoint_id=other_source_candidate,
            actor="operator:test",
            evidence_reader=FakeEvidenceReader(observed("deadbeef")),
        )
    assert store.list_candidate_attestation_bindings(source_a) == ()


@pytest.mark.parametrize("lifecycle", ("inactive", "retired"))
def test_ineligible_endpoint_lifecycle_rejected(tmp_path: Path, lifecycle: str) -> None:
    store, authority, source_id = create_authority(tmp_path)
    enroll(store, authority, source_id, "deadbeef")
    ineligible_id = insert_endpoint(store, source_id, lifecycle=lifecycle)

    with pytest.raises(AuthorityConflict, match="not in an eligible admissibility state"):
        authority.check_candidate_attestation(
            source_id,
            endpoint_id=ineligible_id,
            actor="operator:test",
            evidence_reader=FakeEvidenceReader(observed("deadbeef")),
        )
    assert store.list_candidate_attestation_bindings(source_id) == ()


# ---------------------------------------------------------------------------
# 6-12: successful match -- binding shape and absence of side effects
# ---------------------------------------------------------------------------


def test_valid_candidate_matching_anchor_creates_accepted_event_and_binding(
    tmp_path: Path,
) -> None:
    store, authority, source_id = create_authority(tmp_path)
    enroll(store, authority, source_id, "deadbeef")
    candidate_id = insert_endpoint(store, source_id)

    event = authority.check_candidate_attestation(
        source_id,
        endpoint_id=candidate_id,
        actor="operator:test",
        evidence_reader=FakeEvidenceReader(observed("deadbeef")),
    )

    assert event.outcome is AttestationOutcome.ACCEPTED
    assert event.operation is AttestationOperation.CANDIDATE_CHECK
    bindings = store.list_candidate_attestation_bindings(source_id, endpoint_id=candidate_id)
    assert len(bindings) == 1
    binding = bindings[0]
    assert binding.event_id == event.event_id
    assert binding.inventory_source_id == source_id
    assert binding.endpoint_id == candidate_id
    assert binding.source_attestation_epoch == 1
    assert binding.endpoint_lifecycle_at_check == "candidate"


def test_event_and_binding_share_exact_source_endpoint_epoch_context(
    tmp_path: Path,
) -> None:
    store, authority, source_id = create_authority(tmp_path)
    enroll(store, authority, source_id, "deadbeef")
    candidate_id = insert_endpoint(
        store, source_id, locator="https://candidate.example:9999", transport_trust_revision=1
    )

    event = authority.check_candidate_attestation(
        source_id,
        endpoint_id=candidate_id,
        actor="operator:test",
        evidence_reader=FakeEvidenceReader(observed("deadbeef")),
    )
    binding = store.list_candidate_attestation_bindings(source_id, endpoint_id=candidate_id)[0]

    assert event.target_endpoint_id == binding.endpoint_id == candidate_id
    assert event.expected_source_attestation_epoch == binding.source_attestation_epoch == 1
    assert event.expected_canonical_transport_locator == binding.canonical_transport_locator
    assert (
        event.expected_canonicalization_contract_version
        == binding.canonicalization_contract_version
    )
    assert event.expected_transport_trust_revision == binding.transport_trust_revision
    assert event.evidence_tier == binding.evidence_tier
    assert event.tier2_evaluation == binding.tier2_evaluation


def test_matching_candidate_check_has_no_side_effects_beyond_the_binding(
    tmp_path: Path,
) -> None:
    store, authority, source_id = create_authority(tmp_path)
    enroll(store, authority, source_id, "deadbeef")
    candidate_id = insert_endpoint(store, source_id)
    state_before = store.attestation_state(source_id)
    health_before = store.source_state(source_id).runtime_health
    active_before = active_endpoint_id(store, source_id)
    revisions_before = store.backend_instance()

    authority.check_candidate_attestation(
        source_id,
        endpoint_id=candidate_id,
        actor="operator:test",
        evidence_reader=FakeEvidenceReader(observed("deadbeef")),
    )

    # No epoch change, no anchor change, no relationship-gate change.
    assert store.attestation_state(source_id) == state_before
    # No health/freshness change (no source-runtime-health row touched).
    assert store.source_state(source_id).runtime_health == health_before
    # No active-endpoint change -- the binding never activates anything.
    assert active_endpoint_id(store, source_id) == active_before
    assert active_endpoint_id(store, source_id) != candidate_id  # never promoted
    # No resource reconciliation occurred (no resources at all yet).
    assert store.list_resources(source_id) == ()
    # inventory_revision unaffected; published_state_revision may only
    # reflect the audit/binding write, never an inventory commit.
    revisions_after = store.backend_instance()
    assert revisions_after.inventory_revision == revisions_before.inventory_revision


# ---------------------------------------------------------------------------
# 13-15: tier semantics
# ---------------------------------------------------------------------------


def test_tier1_match_persists_tier1_and_not_evaluated(tmp_path: Path) -> None:
    store, authority, source_id = create_authority(tmp_path)
    enroll(store, authority, source_id, "deadbeef")
    candidate_id = insert_endpoint(store, source_id)

    authority.check_candidate_attestation(
        source_id,
        endpoint_id=candidate_id,
        actor="operator:test",
        evidence_reader=FakeEvidenceReader(observed("deadbeef", tier2_verified=None)),
    )

    binding = store.list_candidate_attestation_bindings(source_id, endpoint_id=candidate_id)[0]
    assert binding.evidence_tier is AttestationEvidenceTier.TIER_1
    assert binding.tier2_evaluation is TierTwoEvaluationStatus.NOT_EVALUATED


def test_genuine_tier2_verified_persists_tier2_verified(tmp_path: Path) -> None:
    store, authority, source_id = create_authority(tmp_path)
    enroll(store, authority, source_id, "deadbeef")
    candidate_id = insert_endpoint(store, source_id)

    authority.check_candidate_attestation(
        source_id,
        endpoint_id=candidate_id,
        actor="operator:test",
        evidence_reader=FakeEvidenceReader(observed("deadbeef", tier2_verified=True)),
    )

    binding = store.list_candidate_attestation_bindings(source_id, endpoint_id=candidate_id)[0]
    assert binding.evidence_tier is AttestationEvidenceTier.TIER_2
    assert binding.tier2_evaluation is TierTwoEvaluationStatus.VERIFIED


def test_tier2_failure_remains_tier1_never_fabricates_tier2(tmp_path: Path) -> None:
    store, authority, source_id = create_authority(tmp_path)
    enroll(store, authority, source_id, "deadbeef")
    candidate_id = insert_endpoint(store, source_id)

    authority.check_candidate_attestation(
        source_id,
        endpoint_id=candidate_id,
        actor="operator:test",
        evidence_reader=FakeEvidenceReader(observed("deadbeef", tier2_verified=False)),
    )

    binding = store.list_candidate_attestation_bindings(source_id, endpoint_id=candidate_id)[0]
    assert binding.evidence_tier is AttestationEvidenceTier.TIER_1
    assert binding.tier2_evaluation is TierTwoEvaluationStatus.FAILED


# ---------------------------------------------------------------------------
# 16-19: mismatch / unavailable / malformed / reader exception
# ---------------------------------------------------------------------------


def test_mismatch_audited_no_binding_lifecycle_unchanged(tmp_path: Path) -> None:
    store, authority, source_id = create_authority(tmp_path)
    enroll(store, authority, source_id, "deadbeef")
    candidate_id = insert_endpoint(store, source_id)

    event = authority.check_candidate_attestation(
        source_id,
        endpoint_id=candidate_id,
        actor="operator:test",
        evidence_reader=FakeEvidenceReader(observed("cafef00d")),
    )

    assert event.outcome is AttestationOutcome.MISMATCH
    assert store.list_candidate_attestation_bindings(source_id) == ()
    with store._read_connection() as connection:
        endpoints_by_id = {
            row["endpoint_id"]: row["lifecycle"]
            for row in connection.execute(
                "SELECT endpoint_id, lifecycle FROM source_endpoints "
                "WHERE inventory_source_id=?",
                (source_id,),
            ).fetchall()
        }
    assert endpoints_by_id[candidate_id] == "candidate"
    # The source relationship gate now reflects the unresolved mismatch.
    assert store.attestation_state(source_id).relationship_gate is (
        SourceAttestationRelationshipGate.MISMATCH_PENDING_REATTESTATION
    )
    assert store.attestation_state(source_id).source_attestation_epoch == 1
    assert store.attestation_state(source_id).anchor_value == "deadbeef"


def test_unavailable_audited_no_binding(tmp_path: Path) -> None:
    store, authority, source_id = create_authority(tmp_path)
    enroll(store, authority, source_id, "deadbeef")
    candidate_id = insert_endpoint(store, source_id)

    event = authority.check_candidate_attestation(
        source_id,
        endpoint_id=candidate_id,
        actor="operator:test",
        evidence_reader=FakeEvidenceReader(unavailable()),
    )

    assert event.outcome is AttestationOutcome.UNAVAILABLE
    assert store.list_candidate_attestation_bindings(source_id) == ()
    assert store.attestation_state(source_id).relationship_gate is (
        SourceAttestationRelationshipGate.CLEAR
    )


def test_malformed_audited_no_binding(tmp_path: Path) -> None:
    store, authority, source_id = create_authority(tmp_path)
    enroll(store, authority, source_id, "deadbeef")
    candidate_id = insert_endpoint(store, source_id)

    event = authority.check_candidate_attestation(
        source_id,
        endpoint_id=candidate_id,
        actor="operator:test",
        evidence_reader=FakeEvidenceReader(malformed()),
    )

    assert event.outcome is AttestationOutcome.MALFORMED
    assert store.list_candidate_attestation_bindings(source_id) == ()


def test_reader_exception_sanitized_audited_unavailable_no_binding(tmp_path: Path) -> None:
    store, authority, source_id = create_authority(tmp_path)
    enroll(store, authority, source_id, "deadbeef")
    candidate_id = insert_endpoint(store, source_id)

    class RaisingReader:
        def read(self, **kwargs) -> SourceAttestationEvidenceReading:
            raise RuntimeError(
                "connection refused to https://candidate.example:8006 token=SUPER-SECRET-1"
            )

    event = authority.check_candidate_attestation(
        source_id, endpoint_id=candidate_id, actor="operator:test", evidence_reader=RaisingReader()
    )

    assert event.outcome is AttestationOutcome.UNAVAILABLE
    assert event.reason == "attestation_evidence_reader_raised_exception"
    assert "SUPER-SECRET-1" not in event.reason
    assert store.list_candidate_attestation_bindings(source_id) == ()


# ---------------------------------------------------------------------------
# 20-25: SS19a races -- context changes between read and write
# ---------------------------------------------------------------------------


def test_source_config_revision_race_is_stale_cas_no_binding(tmp_path: Path) -> None:
    store, authority, source_id = create_authority(tmp_path)
    enroll(store, authority, source_id, "deadbeef")
    candidate_id = insert_endpoint(store, source_id)

    def mutate() -> None:
        InventoryAuthority(store, now=fixed_now).rotate_credential_reference(
            source_id, "secret://inventory/rotated"
        )

    with pytest.raises(AuthorityConflict, match="context changed"):
        authority.check_candidate_attestation(
            source_id,
            endpoint_id=candidate_id,
            actor="operator:test",
            evidence_reader=FakeEvidenceReader(observed("deadbeef"), mutate=mutate),
        )

    assert store.list_candidate_attestation_bindings(source_id) == ()
    candidate_events = [
        e
        for e in store.list_attestation_events(source_id)
        if e.operation is AttestationOperation.CANDIDATE_CHECK
    ]
    assert len(candidate_events) == 1
    assert candidate_events[0].outcome is AttestationOutcome.STALE_CAS


def test_source_attestation_epoch_race_is_stale_cas_no_binding(tmp_path: Path) -> None:
    store, authority, source_id = create_authority(tmp_path)
    enroll(store, authority, source_id, "deadbeef")
    candidate_id = insert_endpoint(store, source_id)

    def mutate() -> None:
        with store._transaction() as connection:
            connection.execute(
                "UPDATE source_attestation_state SET source_attestation_epoch="
                "source_attestation_epoch+1 WHERE inventory_source_id=?",
                (source_id,),
            )

    with pytest.raises(AuthorityConflict, match="context changed"):
        authority.check_candidate_attestation(
            source_id,
            endpoint_id=candidate_id,
            actor="operator:test",
            evidence_reader=FakeEvidenceReader(observed("deadbeef"), mutate=mutate),
        )

    assert store.list_candidate_attestation_bindings(source_id) == ()


def test_relationship_gate_race_is_stale_cas_no_binding(tmp_path: Path) -> None:
    store, authority, source_id = create_authority(tmp_path)
    enroll(store, authority, source_id, "deadbeef")
    candidate_id = insert_endpoint(store, source_id)
    other_candidate = insert_endpoint(
        store, source_id, locator="https://other-candidate.example:8006"
    )

    def mutate() -> None:
        InventoryAuthority(store, now=fixed_now).reattest_source(
            source_id,
            endpoint_id=active_endpoint_id(store, source_id),
            actor="operator:racer",
            evidence_reader=FakeEvidenceReader(observed("cafef00d")),
        )

    with pytest.raises(AuthorityConflict, match="context changed"):
        authority.check_candidate_attestation(
            source_id,
            endpoint_id=candidate_id,
            actor="operator:test",
            evidence_reader=FakeEvidenceReader(observed("deadbeef"), mutate=mutate),
        )

    assert store.list_candidate_attestation_bindings(source_id) == ()
    assert store.attestation_state(source_id).relationship_gate is (
        SourceAttestationRelationshipGate.MISMATCH_PENDING_REATTESTATION
    )
    # Unrelated other candidate is untouched by this race.
    assert store.list_candidate_attestation_bindings(source_id, endpoint_id=other_candidate) == ()


def test_candidate_locator_and_canonicalization_race_is_stale_cas_no_binding(
    tmp_path: Path,
) -> None:
    store, authority, source_id = create_authority(tmp_path)
    enroll(store, authority, source_id, "deadbeef")
    candidate_id = insert_endpoint(store, source_id)
    migrating_authority = InventoryAuthority(
        store, now=fixed_now, _test_migration_contracts={2: lambda locator: locator + "/v2"}
    )

    def mutate() -> None:
        # The only legitimate way to change a retained endpoint's locator
        # is a controlled canonicalization migration (ADR 0002); it moves
        # every retained endpoint of the source, including this candidate.
        migrating_authority.migrate_canonicalization_contract(source_id, 2)

    with pytest.raises(AuthorityConflict, match="context changed"):
        authority.check_candidate_attestation(
            source_id,
            endpoint_id=candidate_id,
            actor="operator:test",
            evidence_reader=FakeEvidenceReader(observed("deadbeef"), mutate=mutate),
        )

    assert store.list_candidate_attestation_bindings(source_id) == ()
    candidate_events = [
        e
        for e in store.list_attestation_events(source_id)
        if e.operation is AttestationOperation.CANDIDATE_CHECK
    ]
    assert len(candidate_events) == 1
    assert candidate_events[0].outcome is AttestationOutcome.STALE_CAS


def test_candidate_transport_trust_race_is_stale_cas_no_binding(tmp_path: Path) -> None:
    store, authority, source_id = create_authority(tmp_path)
    enroll(store, authority, source_id, "deadbeef")
    candidate_id = insert_endpoint(store, source_id)

    def mutate() -> None:
        with store._transaction() as connection:
            connection.execute(
                "UPDATE source_endpoints SET transport_trust_revision="
                "transport_trust_revision+1 WHERE endpoint_id=?",
                (candidate_id,),
            )

    with pytest.raises(AuthorityConflict, match="context changed"):
        authority.check_candidate_attestation(
            source_id,
            endpoint_id=candidate_id,
            actor="operator:test",
            evidence_reader=FakeEvidenceReader(observed("deadbeef"), mutate=mutate),
        )

    assert store.list_candidate_attestation_bindings(source_id) == ()


def test_candidate_lifecycle_retired_during_read_is_stale_cas_no_binding(
    tmp_path: Path,
) -> None:
    """ADR 0003 SS19a / SS29 negative witness 17 (mandatory)."""

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
            source_id,
            endpoint_id=candidate_id,
            actor="operator:test",
            evidence_reader=FakeEvidenceReader(observed("deadbeef"), mutate=mutate),
        )

    assert store.list_candidate_attestation_bindings(source_id) == ()
    candidate_events = [
        e
        for e in store.list_attestation_events(source_id)
        if e.operation is AttestationOperation.CANDIDATE_CHECK
    ]
    assert len(candidate_events) == 1
    assert candidate_events[0].outcome is AttestationOutcome.STALE_CAS


# ---------------------------------------------------------------------------
# 26: transaction-failure atomicity
# ---------------------------------------------------------------------------


def test_injected_failure_between_event_and_binding_leaves_no_partial_state(
    tmp_path: Path,
) -> None:
    class FailingAuthority(InventoryAuthority):
        def _after_attestation_transition(self, connection, *, event_id: str) -> None:
            raise RuntimeError("injected candidate binding failure")

    store, base_authority, source_id = create_authority(tmp_path)
    enroll(store, base_authority, source_id, "deadbeef")
    candidate_id = insert_endpoint(store, source_id)
    failing = FailingAuthority(store, now=fixed_now)
    before = store.backend_instance()

    with pytest.raises(RuntimeError, match="injected candidate binding"):
        failing.check_candidate_attestation(
            source_id,
            endpoint_id=candidate_id,
            actor="operator:test",
            evidence_reader=FakeEvidenceReader(observed("deadbeef")),
        )

    # Only the (already-committed, prior) enrollment event survives; the
    # candidate_check event/binding pair from this call rolled back together.
    remaining_events = store.list_attestation_events(source_id)
    assert len(remaining_events) == 1
    assert remaining_events[0].operation is AttestationOperation.ENROLLMENT
    assert store.list_candidate_attestation_bindings(source_id) == ()
    assert store.backend_instance() == before


# ---------------------------------------------------------------------------
# 27-28: restart/reopen and repeated-check immutability
# ---------------------------------------------------------------------------


def test_restart_reopen_round_trips_accepted_event_and_binding(tmp_path: Path) -> None:
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
    enroll(store, authority, source_id, "deadbeef")
    candidate_id = insert_endpoint(store, source_id)
    event = authority.check_candidate_attestation(
        source_id,
        endpoint_id=candidate_id,
        actor="operator:test",
        evidence_reader=FakeEvidenceReader(observed("deadbeef")),
    )
    store.close()

    reopened = InventoryAuthorityStore(path, now=fixed_now)
    reopened_event = reopened.attestation_event(event.event_id)
    reopened_bindings = reopened.list_candidate_attestation_bindings(
        source_id, endpoint_id=candidate_id
    )
    assert reopened_event == event
    assert len(reopened_bindings) == 1
    assert reopened_bindings[0].event_id == event.event_id


def test_repeated_candidate_checks_do_not_mutate_historical_bindings(
    tmp_path: Path,
) -> None:
    store, authority, source_id = create_authority(tmp_path)
    enroll(store, authority, source_id, "deadbeef")
    candidate_id = insert_endpoint(store, source_id)

    first_event = authority.check_candidate_attestation(
        source_id,
        endpoint_id=candidate_id,
        actor="operator:first",
        evidence_reader=FakeEvidenceReader(observed("deadbeef")),
    )
    first_binding = store.list_candidate_attestation_bindings(
        source_id, endpoint_id=candidate_id
    )[0]

    second_event = authority.check_candidate_attestation(
        source_id,
        endpoint_id=candidate_id,
        actor="operator:second",
        evidence_reader=FakeEvidenceReader(observed("deadbeef")),
    )

    bindings = store.list_candidate_attestation_bindings(source_id, endpoint_id=candidate_id)
    assert len(bindings) == 2
    assert first_event.event_id != second_event.event_id
    # The original binding row is retained unchanged (immutable audit trail).
    retained_first = next(b for b in bindings if b.event_id == first_event.event_id)
    assert retained_first == first_binding


# ---------------------------------------------------------------------------
# 29-30: old-epoch binding retention and ineligibility
# ---------------------------------------------------------------------------


def test_old_epoch_binding_retained_but_not_current_authority_after_epoch_bump(
    tmp_path: Path,
) -> None:
    store, authority, source_id = create_authority(tmp_path)
    enroll(store, authority, source_id, "deadbeef")
    candidate_id = insert_endpoint(store, source_id)
    authority.check_candidate_attestation(
        source_id,
        endpoint_id=candidate_id,
        actor="operator:test",
        evidence_reader=FakeEvidenceReader(observed("deadbeef")),
    )
    old_binding = store.list_candidate_attestation_bindings(
        source_id, endpoint_id=candidate_id
    )[0]
    assert old_binding.source_attestation_epoch == 1

    active_id = active_endpoint_id(store, source_id)
    authority.accept_source_attestation_anchor_change(
        source_id,
        endpoint_id=active_id,
        actor="operator:accept",
        evidence_reader=FakeEvidenceReader(observed("cafef00d")),
    )

    # The old binding remains, physically retained and unmodified.
    bindings = store.list_candidate_attestation_bindings(source_id, endpoint_id=candidate_id)
    assert len(bindings) == 1
    assert bindings[0] == old_binding
    assert bindings[0].source_attestation_epoch == 1  # never migrated to 2

    current_epoch = store.attestation_state(source_id).source_attestation_epoch
    assert current_epoch == 2
    # The retained binding is not current-epoch evidence: authority-layer
    # callers must compare binding.source_attestation_epoch against the
    # live current epoch themselves -- the schema does not silently treat
    # it as such.
    assert bindings[0].source_attestation_epoch != current_epoch


# ---------------------------------------------------------------------------
# 31-32: discovery independence
# ---------------------------------------------------------------------------


def test_active_discovery_run_not_fenced_by_matching_candidate_check(
    tmp_path: Path,
) -> None:
    store, authority, source_id = create_authority(tmp_path)
    enroll(store, authority, source_id, "deadbeef")
    candidate_id = insert_endpoint(store, source_id)
    run = authority.issue_discovery_run(source_id, 1)

    authority.check_candidate_attestation(
        source_id,
        endpoint_id=candidate_id,
        actor="operator:test",
        evidence_reader=FakeEvidenceReader(observed("deadbeef")),
    )

    assert store.source_state(source_id).source.active_discovery_run_id == run.run_id
    assert store.discovery_run(run.run_id).lifecycle.value == "issued"


def test_ordinary_discovery_still_uses_only_the_active_endpoint(tmp_path: Path) -> None:
    store, authority, source_id = create_authority(tmp_path)
    enroll(store, authority, source_id, "deadbeef")
    candidate_id = insert_endpoint(store, source_id)
    authority.check_candidate_attestation(
        source_id,
        endpoint_id=candidate_id,
        actor="operator:test",
        evidence_reader=FakeEvidenceReader(observed("deadbeef")),
    )

    run = authority.issue_discovery_run(source_id, 1)

    assert run.expected_endpoint_id == active_endpoint_id(store, source_id)
    assert run.expected_endpoint_id != candidate_id


# ---------------------------------------------------------------------------
# 33-34: schema defense-in-depth for manual/malformed rows
# ---------------------------------------------------------------------------


def test_manual_binding_from_event_with_non_clear_gate_rejected(tmp_path: Path) -> None:
    store, authority, source_id = create_authority(tmp_path)
    enroll(store, authority, source_id, "deadbeef")
    candidate_id = insert_endpoint(store, source_id)
    event_id = str(uuid.uuid4())
    binding_id = str(uuid.uuid4())

    with store._transaction() as connection:
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
            "VALUES(?, ?, ?, 'candidate_check', 'operator:test', ?, 1, ?, "
            "'https://candidate.example:8006', 1, 1, 1, "
            "'mismatch_pending_reattestation', "
            "'accepted', 'tier_1', 'not_evaluated', "
            "'pve_root_ca_sha256_fingerprint', 'deadbeef', 'candidate', 1, NULL, NULL, "
            "'malformed fixture: gate was not clear')",
            (event_id, source_id, candidate_id, FIXED_NOW.isoformat(), candidate_id),
        )

    with pytest.raises(
        sqlite3.IntegrityError, match="matching accepted candidate_check event"
    ):
        with store._transaction() as connection:
            connection.execute(
                "INSERT INTO candidate_attestation_bindings("
                "binding_id, inventory_source_id, endpoint_id, source_attestation_epoch, "
                "evidence_tier, tier2_evaluation, endpoint_lifecycle_at_check, "
                "canonical_transport_locator, canonicalization_contract_version, "
                "transport_trust_revision, matched_at, created_by, event_id) "
                "VALUES(?, ?, ?, 1, 'tier_1', 'not_evaluated', 'candidate', "
                "'https://candidate.example:8006', 1, 1, ?, 'operator:test', ?)",
                (binding_id, source_id, candidate_id, FIXED_NOW.isoformat(), event_id),
            )

    assert store.list_candidate_attestation_bindings(source_id) == ()


@pytest.mark.parametrize(
    "mismatched_field",
    ("evidence_tier", "tier2_evaluation", "endpoint_lifecycle_at_check", "canonical_transport_locator"),
)
def test_manual_binding_with_mismatched_event_context_rejected(
    tmp_path: Path, mismatched_field: str
) -> None:
    store, authority, source_id = create_authority(tmp_path)
    enroll(store, authority, source_id, "deadbeef")
    candidate_id = insert_endpoint(store, source_id)
    event_id = str(uuid.uuid4())
    binding_id = str(uuid.uuid4())

    with store._transaction() as connection:
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
            "VALUES(?, ?, ?, 'candidate_check', 'operator:test', ?, 1, ?, "
            "'https://candidate.example:8006', 1, 1, 1, 'clear', "
            "'accepted', 'tier_1', 'not_evaluated', "
            "'pve_root_ca_sha256_fingerprint', 'deadbeef', 'candidate', 1, NULL, NULL, "
            "'valid baseline event')",
            (event_id, source_id, candidate_id, FIXED_NOW.isoformat(), candidate_id),
        )

    binding_values = {
        "evidence_tier": "tier_1",
        "tier2_evaluation": "not_evaluated",
        "endpoint_lifecycle_at_check": "candidate",
        "canonical_transport_locator": "https://candidate.example:8006",
    }
    # Deliberately diverge exactly one field from the originating event.
    binding_values[mismatched_field] = {
        "evidence_tier": "tier_2",
        "tier2_evaluation": "verified",
        "endpoint_lifecycle_at_check": "inactive",
        "canonical_transport_locator": "https://different.example:8006",
    }[mismatched_field]

    with pytest.raises(
        sqlite3.IntegrityError, match="matching accepted candidate_check event"
    ):
        with store._transaction() as connection:
            connection.execute(
                "INSERT INTO candidate_attestation_bindings("
                "binding_id, inventory_source_id, endpoint_id, source_attestation_epoch, "
                "evidence_tier, tier2_evaluation, endpoint_lifecycle_at_check, "
                "canonical_transport_locator, canonicalization_contract_version, "
                "transport_trust_revision, matched_at, created_by, event_id) "
                "VALUES(?, ?, ?, 1, ?, ?, ?, ?, 1, 1, ?, 'operator:test', ?)",
                (
                    binding_id,
                    source_id,
                    candidate_id,
                    binding_values["evidence_tier"],
                    binding_values["tier2_evaluation"],
                    binding_values["endpoint_lifecycle_at_check"],
                    binding_values["canonical_transport_locator"],
                    FIXED_NOW.isoformat(),
                    event_id,
                ),
            )

    assert store.list_candidate_attestation_bindings(source_id) == ()
