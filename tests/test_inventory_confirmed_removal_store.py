"""WAVE A1 Commit 1: schema v5 durable structures for Class-C confirmed removal.

ADR 0004. These tests exercise the schema layer only (fresh triggers/FKs/
CHECKs), via the same narrow raw-SQL fixture discipline already established
by ``tests/test_inventory_source_attestation_store.py`` -- no
``confirm_class_c_resource_removal`` authority method exists until Commit 3,
and reconciliation does not yet populate ``resource_absence_pointers`` until
Commit 2.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.inventory import (
    AttestationEvidenceTier,
    AuthorityDatabaseRejected,
    BaselineCompleteness,
    BaselineMode,
    DetailReadStatus,
    DiscoveredNode,
    DiscoveredResource,
    InventoryAuthority,
    InventoryAuthorityStore,
    NormalizedDiscoverySnapshot,
    SourceAvailability,
    TierTwoEvaluationStatus,
)
from app.inventory.attestation import (
    ANCHOR_KIND_PVE_ROOT_CA_SHA256_FINGERPRINT,
    SourceAttestationEvidenceReading,
    SourceAttestationReadOutcome,
)
from app.inventory.discovery import ProviderGuestLocatorSet, ProviderNodeScope
from app.inventory.store import AUTHORITY_SCHEMA_MARKER, AUTHORITY_SCHEMA_VERSION

FIXED_NOW = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)


def fixed_now() -> datetime:
    return FIXED_NOW


class _FakeReader:
    def __init__(self, anchor: str = "deadbeef") -> None:
        self._anchor = anchor

    def read(self, **_kwargs) -> SourceAttestationEvidenceReading:
        return SourceAttestationEvidenceReading(
            outcome=SourceAttestationReadOutcome.OBSERVED,
            anchor_kind=ANCHOR_KIND_PVE_ROOT_CA_SHA256_FINGERPRINT,
            anchor_value=self._anchor,
        )


def create_authority(tmp_path: Path) -> tuple[InventoryAuthorityStore, InventoryAuthority, str]:
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


def enroll(store: InventoryAuthorityStore, authority: InventoryAuthority, source_id: str) -> None:
    authority.enroll_source_attestation(
        source_id,
        endpoint_id=active_endpoint_id(store, source_id),
        actor="operator:enroll",
        evidence_reader=_FakeReader(),
    )


def guest(vmid=101, kind="qemu", name="guest", node="pve-a", detail=DetailReadStatus.OK):
    return (vmid, kind, name, "running", node, detail, {"memory": 1024})


def normalized_snapshot_for(run, source_id, *, resources, nodes=None):
    resources = tuple(resources)
    nodes = nodes if nodes is not None else (
        DiscoveredNode("pve-a", "online", True, FIXED_NOW.isoformat(), {}),
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
                               FIXED_NOW.isoformat(), detail, facts)
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


def make_missing_resource(store, authority, source_id):
    """Complete a present run then an absent run; return (resource_id, binding_id, witness_run)."""

    complete_snapshot(store, authority, source_id, resources=(guest(),))
    resource = store.list_resources(source_id)[0]
    witness_run = complete_snapshot(store, authority, source_id, resources=())
    resource = store.list_resources(source_id)[0]
    assert resource.presence == "missing"
    assert resource.lifecycle == "quarantined"
    return resource, witness_run


def _new_uuid() -> str:
    return str(uuid.uuid4())


def decision_fields(store, source_id, resource, witness_run, attestation, *, actor="op", reason="confirming removal"):
    return dict(
        inventory_source_id=source_id,
        resource_id=resource.resource_id,
        binding_id=resource.active_binding_id,
        vmid=resource.vmid,
        locator_generation=resource.locator_generation,
        resource_continuity_revision=resource.resource_continuity_revision,
        witness_run_id=witness_run.run_id,
        witness_discovery_run_sequence=witness_run.discovery_run_sequence,
        source_config_revision=witness_run.expected_source_config_revision,
        endpoint_id=witness_run.expected_endpoint_id,
        canonical_transport_locator=witness_run.expected_canonical_transport_locator,
        canonicalization_contract_version=witness_run.expected_canonicalization_contract_version,
        transport_trust_revision=witness_run.expected_transport_trust_revision,
        source_attestation_epoch=attestation.source_attestation_epoch,
        actor=actor,
        decided_at=FIXED_NOW.isoformat(),
        reason=reason,
    )


_COLUMNS = (
    "evidence_id", "decision_id", "inventory_source_id", "resource_id", "binding_id",
    "vmid", "locator_generation", "resource_continuity_revision", "witness_run_id",
    "witness_discovery_run_sequence", "source_config_revision", "endpoint_id",
    "canonical_transport_locator", "canonicalization_contract_version",
    "transport_trust_revision", "source_attestation_epoch", "actor", "decided_at", "reason",
)


def insert_removal_authority(connection: sqlite3.Connection, *, evidence_id, decision_id, **fields) -> None:
    values = {"evidence_id": evidence_id, "decision_id": decision_id, **fields}
    connection.execute(
        f"INSERT INTO resource_removal_authorities({', '.join(_COLUMNS)}) "
        f"VALUES({', '.join('?' for _ in _COLUMNS)})",
        tuple(values[c] for c in _COLUMNS),
    )


def insert_absence_attestation(connection: sqlite3.Connection, *, evidence_id, decision_id, **fields) -> None:
    values = {"evidence_id": evidence_id, "decision_id": decision_id, **fields}
    connection.execute(
        f"INSERT INTO resource_absence_attestations({', '.join(_COLUMNS)}) "
        f"VALUES({', '.join('?' for _ in _COLUMNS)})",
        tuple(values[c] for c in _COLUMNS),
    )


def insert_matching_decision(connection: sqlite3.Connection, fields: dict, *, decision_id: str | None = None) -> str:
    decision_id = decision_id or _new_uuid()
    insert_removal_authority(connection, evidence_id=_new_uuid(), decision_id=decision_id, **fields)
    insert_absence_attestation(connection, evidence_id=_new_uuid(), decision_id=decision_id, **fields)
    return decision_id


def insert_pointer(connection: sqlite3.Connection, *, resource_id, inventory_source_id, witness_run_id, witness_discovery_run_sequence, updated_at=None) -> None:
    connection.execute(
        "INSERT INTO resource_absence_pointers("
        "resource_id, inventory_source_id, witness_run_id, witness_discovery_run_sequence, updated_at) "
        "VALUES(?, ?, ?, ?, ?) "
        "ON CONFLICT(resource_id) DO UPDATE SET witness_run_id=excluded.witness_run_id, "
        "witness_discovery_run_sequence=excluded.witness_discovery_run_sequence, "
        "updated_at=excluded.updated_at",
        (resource_id, inventory_source_id, witness_run_id, witness_discovery_run_sequence,
         updated_at or FIXED_NOW.isoformat()),
    )


# ---------------------------------------------------------------------------
# Schema-level tests
# ---------------------------------------------------------------------------


def test_fresh_database_is_schema_v5(tmp_path: Path) -> None:
    store = InventoryAuthorityStore(tmp_path / "authority.db")
    assert AUTHORITY_SCHEMA_VERSION == 5
    counts = store.record_counts()
    assert "resource_absence_pointers" in counts
    assert "resource_removal_authorities" in counts
    assert "resource_absence_attestations" in counts
    assert counts["resource_absence_pointers"] == 0


def test_v4_database_rejected_without_migration(tmp_path: Path) -> None:
    path = tmp_path / "authority.db"
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "CREATE TABLE authority_schema (singleton INTEGER PRIMARY KEY, "
            "marker TEXT NOT NULL, schema_version INTEGER NOT NULL)"
        )
        connection.execute(
            "INSERT INTO authority_schema VALUES(1, ?, 4)", (AUTHORITY_SCHEMA_MARKER,)
        )
        connection.execute("PRAGMA user_version=4")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(AuthorityDatabaseRejected):
        InventoryAuthorityStore(path)


def test_reopen_survives_restart(tmp_path: Path) -> None:
    store, authority, source_id = create_authority(tmp_path)
    enroll(store, authority, source_id)
    resource, witness_run = make_missing_resource(store, authority, source_id)
    attestation = store.attestation_state(source_id)
    with store._transaction() as connection:
        insert_pointer(
            connection,
            resource_id=resource.resource_id,
            inventory_source_id=source_id,
            witness_run_id=witness_run.run_id,
            witness_discovery_run_sequence=witness_run.discovery_run_sequence,
        )
    store.close()

    reopened = InventoryAuthorityStore(tmp_path / "authority.db")
    pointer = reopened.resource_absence_pointer(resource.resource_id)
    assert pointer is not None
    assert pointer.witness_run_id == witness_run.run_id


def test_pointer_requires_matching_source_run(tmp_path: Path) -> None:
    store, authority, source_id = create_authority(tmp_path)
    enroll(store, authority, source_id)
    resource, witness_run = make_missing_resource(store, authority, source_id)

    other_dir = tmp_path / "other"
    other_dir.mkdir()
    other_store, other_authority, other_source_id = create_authority(other_dir)
    other_run = complete_snapshot(other_store, other_authority, other_source_id, resources=(guest(vmid=999),))

    with pytest.raises(sqlite3.IntegrityError):
        with store._transaction() as connection:
            insert_pointer(
                connection,
                resource_id=resource.resource_id,
                inventory_source_id=source_id,
                witness_run_id=other_run.run_id,
                witness_discovery_run_sequence=other_run.discovery_run_sequence,
            )


def test_pointer_requires_successful_witness_run(tmp_path: Path) -> None:
    store, authority, source_id = create_authority(tmp_path)
    enroll(store, authority, source_id)
    complete_snapshot(store, authority, source_id, resources=(guest(),))
    resource = store.list_resources(source_id)[0]
    # Issue but never complete a second run -- not a successful witness.
    run = authority.issue_discovery_run(source_id, 1)

    with pytest.raises(sqlite3.IntegrityError):
        with store._transaction() as connection:
            insert_pointer(
                connection,
                resource_id=resource.resource_id,
                inventory_source_id=source_id,
                witness_run_id=run.run_id,
                witness_discovery_run_sequence=run.discovery_run_sequence,
            )


def test_pointer_may_be_updated_and_cleared(tmp_path: Path) -> None:
    store, authority, source_id = create_authority(tmp_path)
    enroll(store, authority, source_id)
    resource, witness_run_1 = make_missing_resource(store, authority, source_id)
    with store._transaction() as connection:
        insert_pointer(
            connection,
            resource_id=resource.resource_id,
            inventory_source_id=source_id,
            witness_run_id=witness_run_1.run_id,
            witness_discovery_run_sequence=witness_run_1.discovery_run_sequence,
        )
    first = store.resource_absence_pointer(resource.resource_id)
    assert first.witness_run_id == witness_run_1.run_id

    witness_run_2 = complete_snapshot(store, authority, source_id, resources=())
    with store._transaction() as connection:
        insert_pointer(
            connection,
            resource_id=resource.resource_id,
            inventory_source_id=source_id,
            witness_run_id=witness_run_2.run_id,
            witness_discovery_run_sequence=witness_run_2.discovery_run_sequence,
        )
    updated = store.resource_absence_pointer(resource.resource_id)
    assert updated.witness_run_id == witness_run_2.run_id
    assert witness_run_2.run_id != witness_run_1.run_id

    with store._transaction() as connection:
        connection.execute(
            "DELETE FROM resource_absence_pointers WHERE resource_id=?", (resource.resource_id,)
        )
    assert store.resource_absence_pointer(resource.resource_id) is None


def test_pointer_identity_immutable(tmp_path: Path) -> None:
    store, authority, source_id = create_authority(tmp_path)
    enroll(store, authority, source_id)
    resource, witness_run = make_missing_resource(store, authority, source_id)
    with store._transaction() as connection:
        insert_pointer(
            connection,
            resource_id=resource.resource_id,
            inventory_source_id=source_id,
            witness_run_id=witness_run.run_id,
            witness_discovery_run_sequence=witness_run.discovery_run_sequence,
        )
    with pytest.raises(sqlite3.IntegrityError):
        with store._transaction() as connection:
            connection.execute(
                "UPDATE resource_absence_pointers SET inventory_source_id='bogus' WHERE resource_id=?",
                (resource.resource_id,),
            )


def test_removal_authority_and_absence_attestation_persist_and_round_trip(tmp_path: Path) -> None:
    store, authority, source_id = create_authority(tmp_path)
    enroll(store, authority, source_id)
    resource, witness_run = make_missing_resource(store, authority, source_id)
    attestation = store.attestation_state(source_id)
    fields = decision_fields(store, source_id, resource, witness_run, attestation)

    with store._transaction() as connection:
        decision_id = insert_matching_decision(connection, fields)

    # never touched by schema-level fixtures in this test
    assert store.resource_absence_pointer(resource.resource_id) is None

    removal_rows = [
        row for row in _fetch_all(store, "resource_removal_authorities") if row["decision_id"] == decision_id
    ]
    attestation_rows = [
        row for row in _fetch_all(store, "resource_absence_attestations") if row["decision_id"] == decision_id
    ]
    assert len(removal_rows) == 1
    assert len(attestation_rows) == 1
    assert removal_rows[0]["evidence_id"] != attestation_rows[0]["evidence_id"]

    removal = store.resource_removal_authority(removal_rows[0]["evidence_id"])
    attestation_evidence = store.resource_absence_attestation(attestation_rows[0]["evidence_id"])
    assert removal.decision_id == attestation_evidence.decision_id == decision_id
    assert removal.resource_id == attestation_evidence.resource_id == resource.resource_id


def _fetch_all(store: InventoryAuthorityStore, table: str) -> list[sqlite3.Row]:
    with store._read_connection() as connection:
        return connection.execute(f"SELECT * FROM {table}").fetchall()


def test_evidence_update_rejected(tmp_path: Path) -> None:
    store, authority, source_id = create_authority(tmp_path)
    enroll(store, authority, source_id)
    resource, witness_run = make_missing_resource(store, authority, source_id)
    attestation = store.attestation_state(source_id)
    fields = decision_fields(store, source_id, resource, witness_run, attestation)
    with store._transaction() as connection:
        decision_id = insert_matching_decision(connection, fields)
    with pytest.raises(sqlite3.IntegrityError):
        with store._transaction() as connection:
            connection.execute(
                "UPDATE resource_removal_authorities SET reason='changed' WHERE decision_id=?",
                (decision_id,),
            )
    with pytest.raises(sqlite3.IntegrityError):
        with store._transaction() as connection:
            connection.execute(
                "UPDATE resource_absence_attestations SET reason='changed' WHERE decision_id=?",
                (decision_id,),
            )


def test_evidence_delete_rejected(tmp_path: Path) -> None:
    store, authority, source_id = create_authority(tmp_path)
    enroll(store, authority, source_id)
    resource, witness_run = make_missing_resource(store, authority, source_id)
    attestation = store.attestation_state(source_id)
    fields = decision_fields(store, source_id, resource, witness_run, attestation)
    with store._transaction() as connection:
        decision_id = insert_matching_decision(connection, fields)
    with pytest.raises(sqlite3.IntegrityError):
        with store._transaction() as connection:
            connection.execute(
                "DELETE FROM resource_removal_authorities WHERE decision_id=?", (decision_id,)
            )
    with pytest.raises(sqlite3.IntegrityError):
        with store._transaction() as connection:
            connection.execute(
                "DELETE FROM resource_absence_attestations WHERE decision_id=?", (decision_id,)
            )


@pytest.mark.parametrize(
    "mutated_field,new_value",
    [
        ("resource_id", "not-a-real-resource"),
        ("actor", "someone-else"),
        ("reason", "a different reason"),
    ],
)
def test_absence_attestation_requires_field_for_field_match(tmp_path: Path, mutated_field, new_value) -> None:
    store, authority, source_id = create_authority(tmp_path)
    enroll(store, authority, source_id)
    resource, witness_run = make_missing_resource(store, authority, source_id)
    attestation = store.attestation_state(source_id)
    fields = decision_fields(store, source_id, resource, witness_run, attestation)
    decision_id = _new_uuid()
    mutated = dict(fields)
    mutated[mutated_field] = new_value
    with pytest.raises(sqlite3.IntegrityError):
        with store._transaction() as connection:
            insert_removal_authority(connection, evidence_id=_new_uuid(), decision_id=decision_id, **fields)
            insert_absence_attestation(connection, evidence_id=_new_uuid(), decision_id=decision_id, **mutated)


def test_direct_replacement_terminations_remain_legal(tmp_path: Path) -> None:
    store, authority, source_id = create_authority(tmp_path)
    complete_snapshot(store, authority, source_id, resources=(guest(kind="lxc"),))
    old = store.list_resources(source_id)[0]
    complete_snapshot(store, authority, source_id, resources=(guest(kind="qemu"),))
    termination = store.resource_termination(old.resource_id)
    assert termination is not None
    assert termination.reason == "replaced"
    assert termination.successor_resource_id is not None
    assert termination.class_c_decision_id is None


def test_confirmed_removed_termination_cannot_carry_successor(tmp_path: Path) -> None:
    store, authority, source_id = create_authority(tmp_path)
    enroll(store, authority, source_id)
    resource, witness_run = make_missing_resource(store, authority, source_id)
    attestation = store.attestation_state(source_id)
    fields = decision_fields(store, source_id, resource, witness_run, attestation)
    with pytest.raises(sqlite3.IntegrityError):
        with store._transaction() as connection:
            decision_id = insert_matching_decision(connection, fields)
            connection.execute(
                "INSERT INTO resource_terminations("
                "resource_id, inventory_source_id, binding_id, locator_generation, reason, "
                "successor_resource_id, run_sequence, class_c_decision_id, created_at) "
                "VALUES(?, ?, ?, ?, 'confirmed_removed', ?, ?, ?, ?)",
                (resource.resource_id, source_id, resource.active_binding_id,
                 resource.locator_generation, "some-successor", witness_run.discovery_run_sequence,
                 decision_id, FIXED_NOW.isoformat()),
            )


def test_confirmed_removed_termination_requires_matching_evidence(tmp_path: Path) -> None:
    store, authority, source_id = create_authority(tmp_path)
    enroll(store, authority, source_id)
    resource, witness_run = make_missing_resource(store, authority, source_id)
    attestation = store.attestation_state(source_id)
    fields = decision_fields(store, source_id, resource, witness_run, attestation)
    with pytest.raises(sqlite3.IntegrityError):
        with store._transaction() as connection:
            decision_id = insert_matching_decision(connection, fields)
            connection.execute(
                "INSERT INTO resource_terminations("
                "resource_id, inventory_source_id, binding_id, locator_generation, reason, "
                "successor_resource_id, run_sequence, class_c_decision_id, created_at) "
                "VALUES(?, ?, ?, ?, 'confirmed_removed', NULL, ?, ?, ?)",
                (resource.resource_id, source_id, resource.active_binding_id,
                 resource.locator_generation, witness_run.discovery_run_sequence + 999,
                 decision_id, FIXED_NOW.isoformat()),
            )


def test_confirmed_removed_termination_with_matching_evidence_succeeds(tmp_path: Path) -> None:
    store, authority, source_id = create_authority(tmp_path)
    enroll(store, authority, source_id)
    resource, witness_run = make_missing_resource(store, authority, source_id)
    attestation = store.attestation_state(source_id)
    fields = decision_fields(store, source_id, resource, witness_run, attestation)
    with store._transaction() as connection:
        decision_id = insert_matching_decision(connection, fields)
        connection.execute(
            "UPDATE resource_locator_bindings SET valid_to_run_sequence=?, closure_reason='confirmed_removed' "
            "WHERE binding_id=? AND valid_to_run_sequence IS NULL",
            (witness_run.discovery_run_sequence, resource.active_binding_id),
        )
        connection.execute(
            "INSERT INTO resource_terminations("
            "resource_id, inventory_source_id, binding_id, locator_generation, reason, "
            "successor_resource_id, run_sequence, class_c_decision_id, created_at) "
            "VALUES(?, ?, ?, ?, 'confirmed_removed', NULL, ?, ?, ?)",
            (resource.resource_id, source_id, resource.active_binding_id,
             resource.locator_generation, witness_run.discovery_run_sequence,
             decision_id, FIXED_NOW.isoformat()),
        )
    termination = store.resource_termination(resource.resource_id)
    assert termination.reason == "confirmed_removed"
    assert termination.successor_resource_id is None
    assert termination.class_c_decision_id == decision_id
    fetched = store.confirmed_removal_result(decision_id)
    assert fetched.resource_id == resource.resource_id
    assert fetched.termination.class_c_decision_id == decision_id


def test_record_counts_include_new_tables(tmp_path: Path) -> None:
    store, authority, source_id = create_authority(tmp_path)
    enroll(store, authority, source_id)
    resource, witness_run = make_missing_resource(store, authority, source_id)
    attestation = store.attestation_state(source_id)
    fields = decision_fields(store, source_id, resource, witness_run, attestation)
    with store._transaction() as connection:
        decision_id = insert_matching_decision(connection, fields)
        connection.execute(
            "UPDATE resource_locator_bindings SET valid_to_run_sequence=?, closure_reason='confirmed_removed' "
            "WHERE binding_id=? AND valid_to_run_sequence IS NULL",
            (witness_run.discovery_run_sequence, resource.active_binding_id),
        )
        connection.execute(
            "INSERT INTO resource_terminations("
            "resource_id, inventory_source_id, binding_id, locator_generation, reason, "
            "successor_resource_id, run_sequence, class_c_decision_id, created_at) "
            "VALUES(?, ?, ?, ?, 'confirmed_removed', NULL, ?, ?, ?)",
            (resource.resource_id, source_id, resource.active_binding_id,
             resource.locator_generation, witness_run.discovery_run_sequence,
             decision_id, FIXED_NOW.isoformat()),
        )
    counts = store.record_counts()
    assert counts["resource_removal_authorities"] == 1
    assert counts["resource_absence_attestations"] == 1
    assert counts["resource_terminations"] == 1
