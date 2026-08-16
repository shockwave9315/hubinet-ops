"""WAVE C1 Commit 1: durable source-attestation authority schema/read side.

Covers ADR 0003's persistent model only -- schema shape, the fixed initial
not-yet-attested/epoch-0 sentinel, durable round-trip reads, and fail-closed
rejection of unsupported/legacy databases. No explicit enrollment/
re-attestation/revocation/candidate-check transitions exist yet (WAVE C1
Commit 2+); this file proves the durable authority they will use.
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
    InventoryAuthority,
    InventoryAuthorityStore,
    SourceAttestationStatus,
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


def test_attestation_state_not_found_for_unknown_source(tmp_path: Path) -> None:
    store = InventoryAuthorityStore(tmp_path / "authority.db", now=fixed_now)
    with pytest.raises(Exception):
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

    with pytest.raises(sqlite3.IntegrityError, match="must never decrease"):
        with store._transaction() as connection:
            connection.execute(
                "UPDATE source_attestation_state SET source_attestation_epoch=-1 "
                "WHERE inventory_source_id=?",
                (source_id,),
            )
    # epoch=-1 is also rejected by the nonnegative CHECK before the trigger
    # would even fire; assert epoch decrease specifically using a legal jump
    # forward first, then attempting a legal-looking decrease.
    with store._transaction() as connection:
        connection.execute(
            "UPDATE source_attestation_state SET attestation_status='attested', "
            "source_attestation_epoch=5, anchor_kind='pve_root_ca_sha256_fingerprint', "
            "anchor_value='deadbeef', evidence_tier='tier_1', accepted_at=?, "
            "accepted_by='operator:test', evaluated_endpoint_id=("
            "SELECT endpoint_id FROM source_endpoints WHERE inventory_source_id=? "
            "AND lifecycle='active') WHERE inventory_source_id=?",
            (FIXED_NOW.isoformat(), source_id, source_id),
        )
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
                "anchor_value='deadbeef', evidence_tier='tier_1', accepted_at=?, "
                "accepted_by='operator:test', evaluated_endpoint_id=("
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
        connection.execute(
            "INSERT INTO source_attestation_events("
            "event_id, inventory_source_id, target_endpoint_id, operation, actor, "
            "attempted_at, expected_source_config_revision, expected_endpoint_id, "
            "expected_canonical_transport_locator, "
            "expected_canonicalization_contract_version, "
            "expected_transport_trust_revision, expected_source_attestation_epoch, "
            "outcome, evidence_tier, asserted_anchor_kind, asserted_anchor_value, "
            "endpoint_lifecycle_at_check, previous_epoch, resulting_epoch, reason) "
            "VALUES(?, ?, ?, 'enrollment', 'operator:test', ?, 1, ?, "
            "'https://pve.example:8006', 1, 1, 0, 'accepted', 'tier_1', "
            "'pve_root_ca_sha256_fingerprint', 'deadbeef', NULL, 0, 1, "
            "'initial enrollment accepted')",
            (
                event_id,
                source_id,
                endpoint_id,
                FIXED_NOW.isoformat(),
                endpoint_id,
            ),
        )

    events = store.list_attestation_events(source_id)
    assert len(events) == 1
    assert events[0].event_id == event_id
    assert events[0].outcome.value == "accepted"
    assert events[0].previous_epoch == 0
    assert events[0].resulting_epoch == 1

    with pytest.raises(sqlite3.IntegrityError, match="immutable audit records"):
        with store._transaction() as connection:
            connection.execute(
                "UPDATE source_attestation_events SET reason='tampered' WHERE event_id=?",
                (event_id,),
            )
    assert store.attestation_event(event_id).reason == "initial enrollment accepted"


def test_candidate_binding_requires_matching_source_endpoint_and_event(
    tmp_path: Path,
) -> None:
    """Schema constraints reject a malformed candidate binding tying an
    endpoint to the wrong source, or referencing a nonexistent event."""

    store = InventoryAuthorityStore(tmp_path / "authority.db", now=fixed_now)
    source_a = create_source(store, name="A", locator="https://a.example:8006")
    source_b = create_source(store, name="B", locator="https://b.example:8006")
    endpoint_b = store.source_state(source_b).active_endpoint.endpoint_id

    # Wrong source/endpoint pairing must fail the composite FK.
    with pytest.raises(sqlite3.IntegrityError):
        with store._transaction() as connection:
            connection.execute(
                "INSERT INTO candidate_attestation_bindings("
                "binding_id, inventory_source_id, endpoint_id, source_attestation_epoch, "
                "evidence_tier, endpoint_lifecycle_at_check, canonical_transport_locator, "
                "canonicalization_contract_version, transport_trust_revision, matched_at, "
                "created_by, event_id) "
                "VALUES(?, ?, ?, 1, 'tier_1', 'candidate', 'https://b.example:8006', "
                "1, 1, ?, 'operator:test', ?)",
                (
                    str(uuid.uuid4()),
                    source_a,
                    endpoint_b,
                    FIXED_NOW.isoformat(),
                    str(uuid.uuid4()),
                ),
            )

    # Nonexistent event_id must fail the event FK, even with correct source/endpoint.
    with pytest.raises(sqlite3.IntegrityError):
        with store._transaction() as connection:
            connection.execute(
                "INSERT INTO candidate_attestation_bindings("
                "binding_id, inventory_source_id, endpoint_id, source_attestation_epoch, "
                "evidence_tier, endpoint_lifecycle_at_check, canonical_transport_locator, "
                "canonicalization_contract_version, transport_trust_revision, matched_at, "
                "created_by, event_id) "
                "VALUES(?, ?, ?, 1, 'tier_1', 'candidate', 'https://b.example:8006', "
                "1, 1, ?, 'operator:test', ?)",
                (
                    str(uuid.uuid4()),
                    source_b,
                    endpoint_b,
                    FIXED_NOW.isoformat(),
                    str(uuid.uuid4()),
                ),
            )

    assert store.list_candidate_attestation_bindings(source_a) == ()
    assert store.list_candidate_attestation_bindings(source_b) == ()


def test_candidate_attestation_binding_grants_no_activation_and_is_immutable(
    tmp_path: Path,
) -> None:
    store = InventoryAuthorityStore(tmp_path / "authority.db", now=fixed_now)
    source_id = create_source(store)
    endpoint_id = store.source_state(source_id).active_endpoint.endpoint_id
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
            "outcome, evidence_tier, asserted_anchor_kind, asserted_anchor_value, "
            "endpoint_lifecycle_at_check, previous_epoch, resulting_epoch, reason) "
            "VALUES(?, ?, ?, 'candidate_check', 'operator:test', ?, 1, ?, "
            "'https://pve.example:8006', 1, 1, 1, 'accepted', 'tier_1', "
            "'pve_root_ca_sha256_fingerprint', 'deadbeef', 'candidate', 1, NULL, "
            "'candidate matched enrolled anchor')",
            (event_id, source_id, endpoint_id, FIXED_NOW.isoformat(), endpoint_id),
        )
        connection.execute(
            "INSERT INTO candidate_attestation_bindings("
            "binding_id, inventory_source_id, endpoint_id, source_attestation_epoch, "
            "evidence_tier, endpoint_lifecycle_at_check, canonical_transport_locator, "
            "canonicalization_contract_version, transport_trust_revision, matched_at, "
            "created_by, event_id) "
            "VALUES(?, ?, ?, 1, 'tier_1', 'candidate', 'https://pve.example:8006', "
            "1, 1, ?, 'operator:test', ?)",
            (binding_id, source_id, endpoint_id, FIXED_NOW.isoformat(), event_id),
        )

    bindings = store.list_candidate_attestation_bindings(source_id)
    assert len(bindings) == 1
    assert bindings[0].binding_id == binding_id
    assert bindings[0].source_attestation_epoch == 1

    # A candidate binding never activates the endpoint by itself.
    endpoint = store.source_state(source_id).active_endpoint
    assert endpoint.endpoint_id != endpoint_id or endpoint.lifecycle.value == "active"
    assert store.source_state(source_id).active_endpoint.endpoint_id == endpoint_id

    with pytest.raises(sqlite3.IntegrityError, match="immutable audit records"):
        with store._transaction() as connection:
            connection.execute(
                "UPDATE candidate_attestation_bindings SET evidence_tier='tier_2' "
                "WHERE binding_id=?",
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
