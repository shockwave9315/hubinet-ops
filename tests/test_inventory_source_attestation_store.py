"""WAVE C1 Commit 1: durable source-attestation authority schema/read side.

Covers ADR 0003's persistent model only -- schema shape, the fixed initial
not-yet-attested/epoch-0 sentinel, durable round-trip reads, and fail-closed
rejection of unsupported/legacy databases and malformed retained rows. No
explicit enrollment/re-attestation/revocation/candidate-check transitions
exist yet (WAVE C1 Commit 2+); this file proves the durable authority they
will use.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import sqlite3
import uuid

import pytest

from app.inventory import (
    AttestationEvidenceTier,
    AuthorityDatabaseRejected,
    AuthorityNotFound,
    InventoryAuthority,
    InventoryAuthorityStore,
    SourceAttestationStatus,
    TierTwoEvaluationStatus,
)
from app.inventory.store import AUTHORITY_SCHEMA_MARKER, AUTHORITY_SCHEMA_VERSION


FIXED_NOW = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)


def fixed_now() -> datetime:
    return FIXED_NOW


def create_source(
    store: InventoryAuthorityStore,
    *,
    name: str = "Primary",
    locator: str = "https://pve.example:8006",
) -> str:
    state = InventoryAuthority(store, now=fixed_now).create_inventory_source(
        provider_kind="proxmox",
        display_name=name,
        credential_reference=f"secret://inventory/{name.lower()}",
        transport_locator=locator,
    )
    return state.source.inventory_source_id


def _force_attested(
    connection: sqlite3.Connection,
    source_id: str,
    *,
    epoch: int = 1,
    evidence_tier: str = "tier_1",
    tier2_evaluation: str = "not_evaluated",
) -> None:
    """Directly force a source into an attested state for schema-only tests.

    No explicit enrollment authority method exists yet (Commit 2); this
    bypasses the (not-yet-implemented) operator path deliberately, purely to
    exercise schema constraints against an attested row.
    """

    connection.execute(
        "UPDATE source_attestation_state SET attestation_status='attested', "
        "source_attestation_epoch=?, anchor_kind='pve_root_ca_sha256_fingerprint', "
        "anchor_value='deadbeef', evidence_tier=?, tier2_evaluation=?, accepted_at=?, "
        "accepted_by='operator:test', evaluated_endpoint_id=("
        "SELECT endpoint_id FROM source_endpoints WHERE inventory_source_id=? "
        "AND lifecycle='active') WHERE inventory_source_id=?",
        (epoch, evidence_tier, tier2_evaluation, FIXED_NOW.isoformat(), source_id, source_id),
    )


def _insert_event(
    connection: sqlite3.Connection,
    *,
    event_id: str,
    source_id: str,
    target_endpoint_id: str,
    expected_endpoint_id: str,
    operation: str = "candidate_check",
    outcome: str = "accepted",
    expected_source_attestation_epoch: int = 1,
    expected_relationship_gate: str = "clear",
    evidence_tier: str = "tier_1",
    tier2_evaluation: str = "not_evaluated",
    previous_epoch: int = 1,
    resulting_epoch: str | None = None,
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
        "VALUES(?, ?, ?, ?, 'operator:test', ?, 1, ?, "
        "'https://pve.example:8006', 1, 1, ?, ?, ?, ?, ?, "
        "'pve_root_ca_sha256_fingerprint', 'deadbeef', 'candidate', ?, ?, ?, "
        "'test event')",
        (
            event_id,
            source_id,
            target_endpoint_id,
            operation,
            FIXED_NOW.isoformat(),
            expected_endpoint_id,
            expected_source_attestation_epoch,
            expected_relationship_gate,
            outcome,
            evidence_tier,
            tier2_evaluation,
            previous_epoch,
            resulting_epoch,
            resulting_relationship_gate,
        ),
    )


def _insert_candidate_binding(
    connection: sqlite3.Connection,
    *,
    binding_id: str,
    source_id: str,
    endpoint_id: str,
    event_id: str,
    epoch: int = 1,
    evidence_tier: str = "tier_1",
    tier2_evaluation: str = "not_evaluated",
) -> None:
    connection.execute(
        "INSERT INTO candidate_attestation_bindings("
        "binding_id, inventory_source_id, endpoint_id, source_attestation_epoch, "
        "evidence_tier, tier2_evaluation, endpoint_lifecycle_at_check, "
        "canonical_transport_locator, canonicalization_contract_version, "
        "transport_trust_revision, matched_at, created_by, event_id) "
        "VALUES(?, ?, ?, ?, ?, ?, 'candidate', 'https://pve.example:8006', "
        "1, 1, ?, 'operator:test', ?)",
        (
            binding_id,
            source_id,
            endpoint_id,
            epoch,
            evidence_tier,
            tier2_evaluation,
            FIXED_NOW.isoformat(),
            event_id,
        ),
    )


def test_fresh_database_declares_schema_version_4(tmp_path: Path) -> None:
    assert AUTHORITY_SCHEMA_VERSION == 4
    path = tmp_path / "authority.db"
    InventoryAuthorityStore(path, now=fixed_now)
    with sqlite3.connect(path) as connection:
        marker = connection.execute(
            "SELECT marker, schema_version FROM authority_schema"
        ).fetchone()
        assert marker == (AUTHORITY_SCHEMA_MARKER, 4)
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {
            "source_attestation_state",
            "source_attestation_events",
            "candidate_attestation_bindings",
        } <= tables


def test_source_creation_persists_not_yet_attested_epoch_zero(tmp_path: Path) -> None:
    store = InventoryAuthorityStore(tmp_path / "authority.db", now=fixed_now)
    source_id = create_source(store)

    state = store.attestation_state(source_id)

    assert state.inventory_source_id == source_id
    assert state.attestation_status is SourceAttestationStatus.NOT_YET_ATTESTED
    assert state.source_attestation_epoch == 0
    assert state.anchor_kind is None
    assert state.anchor_value is None
    assert state.evidence_tier is None
    assert state.tier2_evaluation is None
    assert state.accepted_at is None
    assert state.accepted_by is None
    assert state.evaluated_endpoint_id is None
    assert store.list_attestation_events(source_id) == ()
    assert store.list_candidate_attestation_bindings(source_id) == ()


def test_attestation_state_round_trips_across_reopen(tmp_path: Path) -> None:
    path = tmp_path / "authority.db"
    store = InventoryAuthorityStore(path, now=fixed_now)
    source_id = create_source(store)
    before = store.attestation_state(source_id)
    store.close()

    reopened = InventoryAuthorityStore(path, now=fixed_now)
    after = reopened.attestation_state(source_id)

    assert after == before


def test_discovery_run_and_source_runtime_health_carry_epoch_fields(
    tmp_path: Path,
) -> None:
    store = InventoryAuthorityStore(tmp_path / "authority.db", now=fixed_now)
    source_id = create_source(store)
    authority = InventoryAuthority(store, now=fixed_now)

    run = authority.issue_discovery_run(source_id, 1)
    assert run.expected_source_attestation_epoch == 0
    assert run.completion_source_attestation_epoch is None

    health = store.source_state(source_id).runtime_health
    assert health.committed_source_attestation_epoch is None


def test_issuance_captures_exact_nonzero_current_epoch_not_schema_default(
    tmp_path: Path,
) -> None:
    """Finding 4: issuance must not silently rely on the schema DEFAULT 0."""

    store = InventoryAuthorityStore(tmp_path / "authority.db", now=fixed_now)
    source_id = create_source(store)
    with store._transaction() as connection:
        _force_attested(connection, source_id, epoch=3)

    run = InventoryAuthority(store, now=fixed_now).issue_discovery_run(source_id, 1)

    assert run.expected_source_attestation_epoch == 3

    with pytest.raises(sqlite3.IntegrityError, match="issuance fields are immutable"):
        with store._transaction() as connection:
            connection.execute(
                "UPDATE discovery_runs SET expected_source_attestation_epoch=99 "
                "WHERE run_id=?",
                (run.run_id,),
            )


def test_attestation_state_not_found_for_unknown_source(tmp_path: Path) -> None:
    store = InventoryAuthorityStore(tmp_path / "authority.db", now=fixed_now)
    with pytest.raises(AuthorityNotFound, match="inventory source does not exist"):
        store.attestation_state(str(uuid.uuid4()))


def test_older_dormant_authority_schema_v3_is_rejected_without_migration(
    tmp_path: Path,
) -> None:
    """No implicit v3 -> v4 migration exists; an old schema fails closed."""

    path = tmp_path / "phase1a_v3.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE authority_schema (singleton INTEGER PRIMARY KEY, "
            "marker TEXT, schema_version INTEGER)"
        )
        connection.execute(
            "INSERT INTO authority_schema VALUES(1, ?, 3)",
            (AUTHORITY_SCHEMA_MARKER,),
        )
        connection.execute("PRAGMA user_version=3")
    before = path.read_bytes()

    with pytest.raises(AuthorityDatabaseRejected, match="unsupported"):
        InventoryAuthorityStore(path, now=fixed_now)

    assert path.read_bytes() == before


def test_legacy_04_database_is_still_rejected_without_modification(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE plans (id TEXT PRIMARY KEY);
            CREATE TABLE jobs (id TEXT PRIMARY KEY);
            PRAGMA user_version=400;
            """
        )
    before = path.read_bytes()

    with pytest.raises(AuthorityDatabaseRejected, match="legacy Hubinet Ops 0.4"):
        InventoryAuthorityStore(path, now=fixed_now)

    assert path.read_bytes() == before


def test_stale_schema_object_mismatch_names_current_version(tmp_path: Path) -> None:
    """Finding 5: the rejection message must not say a stale prior version."""

    path = tmp_path / "authority.db"
    InventoryAuthorityStore(path, now=fixed_now)
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER source_attestation_epoch_monotonic")
    with pytest.raises(AuthorityDatabaseRejected, match="version 4"):
        InventoryAuthorityStore(path, now=fixed_now)


def test_attestation_state_identity_and_epoch_monotonic_triggers_fail_closed(
    tmp_path: Path,
) -> None:
    store = InventoryAuthorityStore(tmp_path / "authority.db", now=fixed_now)
    source_id = create_source(store)

    with pytest.raises(sqlite3.IntegrityError, match="identity is immutable"):
        with store._transaction() as connection:
            connection.execute(
                "UPDATE source_attestation_state SET inventory_source_id=? "
                "WHERE inventory_source_id=?",
                (str(uuid.uuid4()), source_id),
            )

    with store._transaction() as connection:
        _force_attested(connection, source_id, epoch=5)

    with pytest.raises(sqlite3.IntegrityError, match="must never decrease"):
        with store._transaction() as connection:
            connection.execute(
                "UPDATE source_attestation_state SET source_attestation_epoch=4 "
                "WHERE inventory_source_id=?",
                (source_id,),
            )
    assert store.attestation_state(source_id).source_attestation_epoch == 5


def test_attestation_state_check_rejects_inconsistent_status_anchor_pairing(
    tmp_path: Path,
) -> None:
    store = InventoryAuthorityStore(tmp_path / "authority.db", now=fixed_now)
    source_id = create_source(store)

    with pytest.raises(sqlite3.IntegrityError):
        with store._transaction() as connection:
            connection.execute(
                "UPDATE source_attestation_state SET attestation_status='attested' "
                "WHERE inventory_source_id=?",
                (source_id,),
            )

    with pytest.raises(sqlite3.IntegrityError):
        with store._transaction() as connection:
            connection.execute(
                "UPDATE source_attestation_state SET anchor_kind='pve_root_ca_sha256_fingerprint', "
                "anchor_value='deadbeef' WHERE inventory_source_id=?",
                (source_id,),
            )


def test_attestation_state_rejects_unsupported_anchor_kind(tmp_path: Path) -> None:
    store = InventoryAuthorityStore(tmp_path / "authority.db", now=fixed_now)
    source_id = create_source(store)

    with pytest.raises(sqlite3.IntegrityError):
        with store._transaction() as connection:
            connection.execute(
                "UPDATE source_attestation_state SET attestation_status='attested', "
                "source_attestation_epoch=1, anchor_kind='some_other_kind', "
                "anchor_value='deadbeef', evidence_tier='tier_1', tier2_evaluation='not_evaluated', "
                "accepted_at=?, accepted_by='operator:test', evaluated_endpoint_id=("
                "SELECT endpoint_id FROM source_endpoints WHERE inventory_source_id=? "
                "AND lifecycle='active') WHERE inventory_source_id=?",
                (FIXED_NOW.isoformat(), source_id, source_id),
            )


def test_source_attestation_events_are_immutable_audit_records(tmp_path: Path) -> None:
    store = InventoryAuthorityStore(tmp_path / "authority.db", now=fixed_now)
    source_id = create_source(store)
    endpoint_id = store.source_state(source_id).active_endpoint.endpoint_id
    event_id = str(uuid.uuid4())

    with store._transaction() as connection:
        _insert_event(
            connection,
            event_id=event_id,
            source_id=source_id,
            target_endpoint_id=endpoint_id,
            expected_endpoint_id=endpoint_id,
            operation="enrollment",
            outcome="accepted",
            expected_source_attestation_epoch=0,
            previous_epoch=0,
            resulting_epoch=1,
        )

    events = store.list_attestation_events(source_id)
    assert len(events) == 1
    assert events[0].event_id == event_id
    assert events[0].outcome.value == "accepted"
    assert events[0].previous_epoch == 0
    assert events[0].resulting_epoch == 1
    assert events[0].tier2_evaluation is TierTwoEvaluationStatus.NOT_EVALUATED

    with pytest.raises(sqlite3.IntegrityError, match="immutable audit records"):
        with store._transaction() as connection:
            connection.execute(
                "UPDATE source_attestation_events SET reason='tampered' WHERE event_id=?",
                (event_id,),
            )
    assert store.attestation_event(event_id).reason == "test event"


def test_source_attestation_events_cannot_be_deleted(tmp_path: Path) -> None:
    """Finding 1: accepted audit events are retained authority, not just UPDATE-safe."""

    store = InventoryAuthorityStore(tmp_path / "authority.db", now=fixed_now)
    source_id = create_source(store)
    endpoint_id = store.source_state(source_id).active_endpoint.endpoint_id
    event_id = str(uuid.uuid4())
    with store._transaction() as connection:
        _insert_event(
            connection,
            event_id=event_id,
            source_id=source_id,
            target_endpoint_id=endpoint_id,
            expected_endpoint_id=endpoint_id,
            operation="enrollment",
            outcome="accepted",
            expected_source_attestation_epoch=0,
            previous_epoch=0,
            resulting_epoch=1,
        )

    with pytest.raises(sqlite3.IntegrityError, match="immutable audit records"):
        with store._transaction() as connection:
            connection.execute(
                "DELETE FROM source_attestation_events WHERE event_id=?", (event_id,)
            )

    assert store.attestation_event(event_id) is not None


def test_source_attestation_state_row_cannot_be_deleted(tmp_path: Path) -> None:
    """Finding 1: a source must never silently lose its mandatory current state row."""

    store = InventoryAuthorityStore(tmp_path / "authority.db", now=fixed_now)
    source_id = create_source(store)

    with pytest.raises(sqlite3.IntegrityError, match="mandatory and cannot be deleted"):
        with store._transaction() as connection:
            connection.execute(
                "DELETE FROM source_attestation_state WHERE inventory_source_id=?",
                (source_id,),
            )

    # The DB remains structurally accepted, and the source still has a
    # current attestation state row.
    assert store.attestation_state(source_id).source_attestation_epoch == 0


def test_candidate_binding_requires_matching_source_endpoint_and_event(
    tmp_path: Path,
) -> None:
    store = InventoryAuthorityStore(tmp_path / "authority.db", now=fixed_now)
    source_a = create_source(store, name="A", locator="https://a.example:8006")
    source_b = create_source(store, name="B", locator="https://b.example:8006")
    endpoint_b = store.source_state(source_b).active_endpoint.endpoint_id

    # Wrong source/endpoint pairing must fail the composite FK on
    # source_endpoints before even reaching the matching-event trigger.
    with pytest.raises(sqlite3.IntegrityError):
        with store._transaction() as connection:
            _insert_candidate_binding(
                connection,
                binding_id=str(uuid.uuid4()),
                source_id=source_a,
                endpoint_id=endpoint_b,
                event_id=str(uuid.uuid4()),
            )

    # Nonexistent event_id must fail even with a correct source/endpoint pair.
    with pytest.raises(sqlite3.IntegrityError):
        with store._transaction() as connection:
            _insert_candidate_binding(
                connection,
                binding_id=str(uuid.uuid4()),
                source_id=source_b,
                endpoint_id=endpoint_b,
                event_id=str(uuid.uuid4()),
            )

    assert store.list_candidate_attestation_bindings(source_a) == ()
    assert store.list_candidate_attestation_bindings(source_b) == ()


def test_candidate_binding_rejects_event_from_wrong_source(tmp_path: Path) -> None:
    store = InventoryAuthorityStore(tmp_path / "authority.db", now=fixed_now)
    source_a = create_source(store, name="A", locator="https://a.example:8006")
    source_b = create_source(store, name="B", locator="https://b.example:8006")
    endpoint_a = store.source_state(source_a).active_endpoint.endpoint_id
    endpoint_b = store.source_state(source_b).active_endpoint.endpoint_id
    event_id = str(uuid.uuid4())

    with store._transaction() as connection:
        _insert_event(
            connection,
            event_id=event_id,
            source_id=source_b,
            target_endpoint_id=endpoint_b,
            expected_endpoint_id=endpoint_b,
        )
        # source_a must itself be live-eligible so the *only* violated
        # precondition is "event belongs to another source".
        _force_attested(connection, source_a, epoch=1)

    with pytest.raises(
        sqlite3.IntegrityError, match="matching accepted candidate_check event"
    ):
        with store._transaction() as connection:
            _insert_candidate_binding(
                connection,
                binding_id=str(uuid.uuid4()),
                source_id=source_a,
                endpoint_id=endpoint_a,
                event_id=event_id,
            )


def test_candidate_binding_rejects_event_for_wrong_endpoint(tmp_path: Path) -> None:
    store = InventoryAuthorityStore(tmp_path / "authority.db", now=fixed_now)
    source_id = create_source(store)
    active_endpoint = store.source_state(source_id).active_endpoint.endpoint_id
    event_id = str(uuid.uuid4())

    with store._transaction() as connection:
        candidate_id = str(uuid.uuid4())
        connection.execute(
            "INSERT INTO source_endpoints("
            "endpoint_id, inventory_source_id, canonical_transport_locator, "
            "canonicalization_contract_version, lifecycle, transport_trust_revision, "
            "created_at) VALUES(?, ?, 'https://other.example:8006', 1, 'candidate', 1, ?)",
            (candidate_id, source_id, FIXED_NOW.isoformat()),
        )
        # Event targets the active endpoint, not the candidate being bound.
        _insert_event(
            connection,
            event_id=event_id,
            source_id=source_id,
            target_endpoint_id=active_endpoint,
            expected_endpoint_id=active_endpoint,
        )
        _force_attested(connection, source_id, epoch=1)

    with pytest.raises(
        sqlite3.IntegrityError, match="matching accepted candidate_check event"
    ):
        with store._transaction() as connection:
            _insert_candidate_binding(
                connection,
                binding_id=str(uuid.uuid4()),
                source_id=source_id,
                endpoint_id=candidate_id,
                event_id=event_id,
            )


def test_candidate_binding_rejects_non_candidate_check_event(tmp_path: Path) -> None:
    store = InventoryAuthorityStore(tmp_path / "authority.db", now=fixed_now)
    source_id = create_source(store)
    endpoint_id = store.source_state(source_id).active_endpoint.endpoint_id
    event_id = str(uuid.uuid4())

    with store._transaction() as connection:
        # A re-enrollment-style event captured at expected epoch 1 (e.g.
        # after a prior revocation) so a candidate binding at epoch 1 is
        # schema-legal (candidate_attestation_bindings.source_attestation_
        # epoch requires >= 1) and the source can be made live-eligible at
        # that same epoch -- isolating this test to exactly the "operation
        # != candidate_check" precondition.
        _insert_event(
            connection,
            event_id=event_id,
            source_id=source_id,
            target_endpoint_id=endpoint_id,
            expected_endpoint_id=endpoint_id,
            operation="enrollment",
            outcome="accepted",
            expected_source_attestation_epoch=1,
            previous_epoch=1,
            resulting_epoch=2,
        )
        _force_attested(connection, source_id, epoch=1)

    with pytest.raises(
        sqlite3.IntegrityError, match="matching accepted candidate_check event"
    ):
        with store._transaction() as connection:
            _insert_candidate_binding(
                connection,
                binding_id=str(uuid.uuid4()),
                source_id=source_id,
                endpoint_id=endpoint_id,
                event_id=event_id,
                epoch=1,
            )


def test_candidate_binding_rejects_non_accepted_candidate_event(tmp_path: Path) -> None:
    store = InventoryAuthorityStore(tmp_path / "authority.db", now=fixed_now)
    source_id = create_source(store)
    endpoint_id = store.source_state(source_id).active_endpoint.endpoint_id
    event_id = str(uuid.uuid4())

    with store._transaction() as connection:
        _insert_event(
            connection,
            event_id=event_id,
            source_id=source_id,
            target_endpoint_id=endpoint_id,
            expected_endpoint_id=endpoint_id,
            operation="candidate_check",
            outcome="mismatch",
            evidence_tier=None,
            tier2_evaluation=None,
        )
        _force_attested(connection, source_id, epoch=1)

    with pytest.raises(
        sqlite3.IntegrityError, match="matching accepted candidate_check event"
    ):
        with store._transaction() as connection:
            _insert_candidate_binding(
                connection,
                binding_id=str(uuid.uuid4()),
                source_id=source_id,
                endpoint_id=endpoint_id,
                event_id=event_id,
            )


def test_candidate_binding_rejects_incompatible_epoch(tmp_path: Path) -> None:
    store = InventoryAuthorityStore(tmp_path / "authority.db", now=fixed_now)
    source_id = create_source(store)
    endpoint_id = store.source_state(source_id).active_endpoint.endpoint_id
    event_id = str(uuid.uuid4())

    with store._transaction() as connection:
        _insert_event(
            connection,
            event_id=event_id,
            source_id=source_id,
            target_endpoint_id=endpoint_id,
            expected_endpoint_id=endpoint_id,
            expected_source_attestation_epoch=1,
        )
        # Live-eligible at epoch 2 (matching the attempted binding's own
        # epoch), so only the event/binding epoch mismatch (1 vs. 2) is
        # under test here.
        _force_attested(connection, source_id, epoch=2)

    with pytest.raises(
        sqlite3.IntegrityError, match="matching accepted candidate_check event"
    ):
        with store._transaction() as connection:
            _insert_candidate_binding(
                connection,
                binding_id=str(uuid.uuid4()),
                source_id=source_id,
                endpoint_id=endpoint_id,
                event_id=event_id,
                epoch=2,  # event was evaluated at epoch 1, not 2
            )


def test_candidate_binding_remains_valid_historical_row_after_source_epoch_advances(
    tmp_path: Path,
) -> None:
    """A binding is never retroactively invalidated by the schema itself;
    epoch-eligibility at use time is Commit 4's authority-layer concern."""

    store = InventoryAuthorityStore(tmp_path / "authority.db", now=fixed_now)
    source_id = create_source(store)
    endpoint_id = store.source_state(source_id).active_endpoint.endpoint_id
    event_id = str(uuid.uuid4())
    binding_id = str(uuid.uuid4())

    with store._transaction() as connection:
        _force_attested(connection, source_id, epoch=1)
        _insert_event(
            connection,
            event_id=event_id,
            source_id=source_id,
            target_endpoint_id=endpoint_id,
            expected_endpoint_id=endpoint_id,
            expected_source_attestation_epoch=1,
        )
        _insert_candidate_binding(
            connection,
            binding_id=binding_id,
            source_id=source_id,
            endpoint_id=endpoint_id,
            event_id=event_id,
            epoch=1,
        )

    # Source is later re-attested to a newer epoch.
    with store._transaction() as connection:
        connection.execute(
            "UPDATE source_attestation_state SET source_attestation_epoch=2 "
            "WHERE inventory_source_id=?",
            (source_id,),
        )

    binding = store.list_candidate_attestation_bindings(source_id)[0]
    assert binding.binding_id == binding_id
    assert binding.source_attestation_epoch == 1
    assert store.attestation_state(source_id).source_attestation_epoch == 2


def test_candidate_attestation_binding_grants_no_activation_and_is_immutable(
    tmp_path: Path,
) -> None:
    store = InventoryAuthorityStore(tmp_path / "authority.db", now=fixed_now)
    source_id = create_source(store)
    endpoint_id = store.source_state(source_id).active_endpoint.endpoint_id
    event_id = str(uuid.uuid4())
    binding_id = str(uuid.uuid4())

    with store._transaction() as connection:
        _force_attested(connection, source_id, epoch=1)
        _insert_event(
            connection,
            event_id=event_id,
            source_id=source_id,
            target_endpoint_id=endpoint_id,
            expected_endpoint_id=endpoint_id,
        )
        _insert_candidate_binding(
            connection,
            binding_id=binding_id,
            source_id=source_id,
            endpoint_id=endpoint_id,
            event_id=event_id,
        )

    bindings = store.list_candidate_attestation_bindings(source_id)
    assert len(bindings) == 1
    assert bindings[0].binding_id == binding_id
    assert bindings[0].source_attestation_epoch == 1

    # A candidate binding never activates the endpoint by itself.
    assert store.source_state(source_id).active_endpoint.endpoint_id == endpoint_id

    with pytest.raises(sqlite3.IntegrityError, match="immutable audit records"):
        with store._transaction() as connection:
            connection.execute(
                "UPDATE candidate_attestation_bindings SET evidence_tier='tier_2' "
                "WHERE binding_id=?",
                (binding_id,),
            )

    with pytest.raises(sqlite3.IntegrityError, match="immutable audit records"):
        with store._transaction() as connection:
            connection.execute(
                "DELETE FROM candidate_attestation_bindings WHERE binding_id=?",
                (binding_id,),
            )


def test_record_counts_include_new_attestation_tables_and_roll_back_on_failure(
    tmp_path: Path,
) -> None:
    class FailingAttestationAuthority(InventoryAuthority):
        def _insert_initial_attestation_state(self, connection, *, source_id: str) -> None:
            raise RuntimeError("injected attestation persistence failure")

    store = InventoryAuthorityStore(tmp_path / "authority.db", now=fixed_now)
    before = store.backend_instance()

    with pytest.raises(RuntimeError, match="injected attestation"):
        FailingAttestationAuthority(store, now=fixed_now).create_inventory_source(
            provider_kind="proxmox",
            display_name="Will roll back",
            credential_reference="secret://inventory/failing",
            transport_locator="https://pve.example:8006",
        )

    counts = store.record_counts()
    assert counts["inventory_sources"] == 0
    assert counts["source_attestation_state"] == 0
    assert store.backend_instance() == before


def test_evidence_tier_enum_has_no_tier_3_value() -> None:
    assert {tier.value for tier in AttestationEvidenceTier} == {"tier_1", "tier_2"}


def test_tier2_evaluation_enum_distinguishes_unevaluated_failed_and_verified() -> None:
    assert {status.value for status in TierTwoEvaluationStatus} == {
        "not_evaluated",
        "failed",
        "verified",
    }


def test_tier2_evaluation_verified_requires_tier_2_accepted_class(tmp_path: Path) -> None:
    """Finding 3: a genuinely verified tier-2 result can never be paired with
    an accepted evidence class weaker than tier 2, at the schema layer."""

    store = InventoryAuthorityStore(tmp_path / "authority.db", now=fixed_now)
    source_id = create_source(store)

    with pytest.raises(sqlite3.IntegrityError):
        with store._transaction() as connection:
            _force_attested(
                connection,
                source_id,
                evidence_tier="tier_1",
                tier2_evaluation="verified",
            )

    # A failed or not-evaluated tier-2 result never blocks an independently
    # valid tier-1 acceptance.
    with store._transaction() as connection:
        _force_attested(
            connection, source_id, evidence_tier="tier_1", tier2_evaluation="failed"
        )
    state = store.attestation_state(source_id)
    assert state.evidence_tier is AttestationEvidenceTier.TIER_1
    assert state.tier2_evaluation is TierTwoEvaluationStatus.FAILED


def test_tier2_evaluation_recorded_distinctly_on_audit_events(tmp_path: Path) -> None:
    store = InventoryAuthorityStore(tmp_path / "authority.db", now=fixed_now)
    source_id = create_source(store)
    endpoint_id = store.source_state(source_id).active_endpoint.endpoint_id

    unavailable_event = str(uuid.uuid4())
    failed_event = str(uuid.uuid4())
    verified_event = str(uuid.uuid4())
    with store._transaction() as connection:
        _insert_event(
            connection,
            event_id=unavailable_event,
            source_id=source_id,
            target_endpoint_id=endpoint_id,
            expected_endpoint_id=endpoint_id,
            operation="enrollment",
            outcome="unavailable",
            evidence_tier=None,
            tier2_evaluation=None,
            expected_source_attestation_epoch=0,
            previous_epoch=0,
            resulting_epoch=None,
        )
        _insert_event(
            connection,
            event_id=failed_event,
            source_id=source_id,
            target_endpoint_id=endpoint_id,
            expected_endpoint_id=endpoint_id,
            operation="enrollment",
            outcome="accepted",
            evidence_tier="tier_1",
            tier2_evaluation="failed",
            expected_source_attestation_epoch=0,
            previous_epoch=0,
            resulting_epoch=1,
        )
        _insert_event(
            connection,
            event_id=verified_event,
            source_id=source_id,
            target_endpoint_id=endpoint_id,
            expected_endpoint_id=endpoint_id,
            operation="reattestation",
            outcome="match",
            evidence_tier="tier_2",
            tier2_evaluation="verified",
            expected_source_attestation_epoch=1,
            previous_epoch=1,
            resulting_epoch=None,
        )

    events = {event.event_id: event for event in store.list_attestation_events(source_id)}
    assert events[unavailable_event].tier2_evaluation is None
    assert events[failed_event].tier2_evaluation is TierTwoEvaluationStatus.FAILED
    assert events[failed_event].evidence_tier is AttestationEvidenceTier.TIER_1
    assert events[verified_event].tier2_evaluation is TierTwoEvaluationStatus.VERIFIED
    assert events[verified_event].evidence_tier is AttestationEvidenceTier.TIER_2
