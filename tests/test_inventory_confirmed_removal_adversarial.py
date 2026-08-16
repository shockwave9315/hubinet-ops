"""WAVE A1 Commit 4: close the ADR 0004 §28/§29 confirmed-removal adversarial matrix.

Most of the lettered matrix rows are already exercised by Commits 1-3's own
test suites (reused here rather than duplicated, per this repository's
established "close the exact family, do not restart a broad audit" review
discipline -- see the docstring table below for the exact cross-reference).
This file adds the rows/witnesses that were NOT yet covered: schema-level
raw-SQL corruption attempts against the evidence tables, a static
source-text boundary check that no polling-derived automatic removal path
exists, long-duration/many-reconfirmation non-authority witnesses, and the
remaining CAS dimensions (canonicalization migration, a witness that never
existed at all, ACL/permission-incomplete-equivalent outcomes).

Cross-reference (ADR 0004 §28 letter -> existing coverage):

    A, B     -- new tests below (single/repeated missing observation alone)
    C        -- new test below (long-duration reconfirmation alone)
    D, E     -- tests/test_inventory_confirmed_removal_reconciliation.py
                (test_partial_run_does_not_touch_pointer,
                test_source_unavailable_run_does_not_touch_pointer)
    F        -- new test below (configuration_error-class outcome)
    G        -- new test below (valid witness, no operator assertions)
    H, I     -- test_inventory_confirmed_removal_authority.py
                (test_both_assertions_mandatory)
    J        -- new test below (both assertions True, witness never existed)
    K        -- test_inventory_confirmed_removal_authority.py
                (test_successful_confirmed_removal_full_terminal_transition)
    L        -- test_inventory_confirmed_removal_authority.py
                (test_vmid_only_targeting_is_not_supported_by_the_call_boundary)
    M, N, O  -- test_inventory_confirmed_removal_authority.py
                (test_stale_resource_continuity_revision_rejected,
                test_stale_binding_id_rejected, test_stale_locator_generation_rejected)
    P        -- test_inventory_confirmed_removal_authority.py
                (test_newer_successful_run_makes_witness_stale)
    Q        -- test_inventory_confirmed_removal_authority.py
                (test_active_discovery_run_blocks_confirmation)
    R, R2    -- test_inventory_confirmed_removal_authority.py
                (test_freshness_expiry_blocks_confirmation,
                test_newer_failed_run_degrades_freshness_and_blocks)
    S        -- test_inventory_confirmed_removal_authority.py
                (test_source_config_change_invalidates_confirmation,
                test_transport_trust_rotation_invalidates_confirmation)
                plus new canonicalization-migration test below
    T, U, V  -- test_inventory_confirmed_removal_authority.py
                (test_epoch_bump_after_witness_invalidates_confirmation,
                test_mismatch_pending_reattestation_blocks_confirmation,
                test_same_anchor_reconfirmation_does_not_invalidate_witness)
    W        -- test_inventory_confirmed_removal_authority.py
                (test_slot_present_again_before_commit_rejected)
    X, Y     -- test_inventory_confirmed_removal_authority.py
                (test_reappearance_after_confirmed_removal_creates_new_incarnation)
    Z        -- test_inventory_confirmed_removal_authority.py
                (test_direct_replacement_already_won_rejected)
    AA       -- test_inventory_confirmed_removal_authority.py
                (test_injected_failure_before_commit_leaves_no_partial_state)
    AB, AC   -- test_inventory_confirmed_removal_authority.py
                (test_reopen_after_committed_decision_is_fully_durable)
    AD       -- test_inventory_confirmed_removal_authority.py
                (test_evidence_replay_rejected_once_already_terminal)
    AE       -- test_inventory_confirmed_removal_authority.py
                (test_epoch_bump_after_witness_invalidates_confirmation)
    AF       -- new static source-scan test below
    AG       -- new explicit test below (successor never inherits evidence)
    AH       -- test_inventory_confirmed_removal_authority.py
                (test_two_operators_racing_exactly_one_wins)
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
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
from app.inventory.attestation import (
    ANCHOR_KIND_PVE_ROOT_CA_SHA256_FINGERPRINT,
    SourceAttestationEvidenceReading,
    SourceAttestationReadOutcome,
)
from app.inventory.discovery import ProviderGuestLocatorSet, ProviderNodeScope

NOW = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)


class _FakeReader:
    def __init__(self, anchor: str = "deadbeef") -> None:
        self._anchor = anchor

    def read(self, **_kwargs) -> SourceAttestationEvidenceReading:
        return SourceAttestationEvidenceReading(
            outcome=SourceAttestationReadOutcome.OBSERVED,
            anchor_kind=ANCHOR_KIND_PVE_ROOT_CA_SHA256_FINGERPRINT,
            anchor_value=self._anchor,
        )


def make_authority(tmp_path: Path, *, now=lambda: NOW):
    store = InventoryAuthorityStore(tmp_path / "authority.db", now=now)
    authority = InventoryAuthority(store, now=now)
    source = authority.create_inventory_source(
        provider_kind="proxmox",
        display_name="Primary",
        credential_reference="secret://inventory/primary",
        transport_locator="https://pve.example:8006",
    )
    return store, authority, source.source.inventory_source_id


def active_endpoint_id(store: InventoryAuthorityStore, source_id: str) -> str:
    return store.source_state(source_id).active_endpoint.endpoint_id


def enroll(store, authority, source_id, anchor="deadbeef"):
    authority.enroll_source_attestation(
        source_id,
        endpoint_id=active_endpoint_id(store, source_id),
        actor="operator:enroll",
        evidence_reader=_FakeReader(anchor),
    )


def guest(vmid=101, kind="qemu", name="guest", node="pve-a", detail=DetailReadStatus.OK):
    return (vmid, kind, name, "running", node, detail, {"memory": 1024})


def normalized_snapshot_for(run, source_id, *, resources, nodes=None, observed_at=None):
    resources = tuple(resources)
    nodes = nodes if nodes is not None else (
        DiscoveredNode("pve-a", "online", True, (observed_at or NOW.isoformat()), {}),
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
        observed_at=observed_at or NOW.isoformat(),
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
                               observed_at or NOW.isoformat(), detail, facts)
            for vmid, kind, name, status, node_name, detail, facts in resources
        ),
        provider_node_scope=ProviderNodeScope._from_provider(
            BaselineMode.CLUSTER, tuple(sorted(node.external_node_name for node in nodes))
        ),
        provider_guest_locators=provider_guest_locators,
        expected_source_attestation_epoch=run.expected_source_attestation_epoch,
    )


def complete_snapshot(store, authority, source_id, *, resources, nodes=None, observed_at=None):
    run = authority.issue_discovery_run(source_id, 1)
    authority.mark_discovery_run_running(source_id, run.run_id)
    normalized = normalized_snapshot_for(run, source_id, resources=resources, nodes=nodes, observed_at=observed_at)
    authority.finalize_successful_discovery_run(source_id, run.run_id, normalized)
    return run


def make_eligible_missing_resource(store, authority, source_id):
    enroll(store, authority, source_id)
    complete_snapshot(store, authority, source_id, resources=(guest(),))
    witness_run = complete_snapshot(store, authority, source_id, resources=())
    resource = store.list_resources(source_id)[0]
    assert resource.presence == "missing"
    return resource, witness_run


def confirm(authority, source_id, resource, witness_run, **overrides):
    kwargs = dict(
        inventory_source_id=source_id,
        resource_id=resource.resource_id,
        expected_binding_id=resource.active_binding_id,
        expected_vmid=resource.vmid,
        expected_locator_generation=resource.locator_generation,
        expected_resource_continuity_revision=resource.resource_continuity_revision,
        expected_witness_run_id=witness_run.run_id,
        expected_witness_discovery_run_sequence=witness_run.discovery_run_sequence,
        actor="operator:confirm",
        confirms_removal=True,
        attests_absence=True,
        reason="confirming removal after verification",
    )
    kwargs.update(overrides)
    return authority.confirm_class_c_resource_removal(**kwargs)


def _fresh_uuid() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# A, B, C -- polling alone, however much, never authorizes removal
# ---------------------------------------------------------------------------


def test_row_a_single_missing_observation_never_authorizes_removal(tmp_path: Path) -> None:
    store, authority, source_id = make_authority(tmp_path)
    enroll(store, authority, source_id)
    complete_snapshot(store, authority, source_id, resources=(guest(),))
    complete_snapshot(store, authority, source_id, resources=())
    resource = store.list_resources(source_id)[0]
    assert resource.presence == "missing"
    assert resource.lifecycle == "quarantined"
    # No authority call was ever made -- nothing in the reconciliation path
    # itself can reach confirmed_removed.
    assert resource.presence != "confirmed_removed"


def test_row_b_one_hundred_reconfirmations_never_authorize_removal(tmp_path: Path) -> None:
    store, authority, source_id = make_authority(tmp_path)
    enroll(store, authority, source_id)
    complete_snapshot(store, authority, source_id, resources=(guest(),))
    resource_id = store.list_resources(source_id)[0].resource_id
    for _ in range(100):
        complete_snapshot(store, authority, source_id, resources=())
    resource = store.list_resources(source_id)[0]
    assert resource.resource_id == resource_id
    assert resource.presence == "missing"
    assert resource.presence != "confirmed_removed"
    # Overwritten, never appended -- still exactly one pointer row.
    assert len(store.list_resource_absence_pointers(source_id)) == 1


def test_row_c_long_duration_alone_never_authorizes_removal(tmp_path: Path) -> None:
    clock = {"now": NOW}
    store, authority, source_id = make_authority(tmp_path, now=lambda: clock["now"])
    enroll(store, authority, source_id)
    complete_snapshot(store, authority, source_id, resources=(guest(),))
    complete_snapshot(store, authority, source_id, resources=())
    clock["now"] = NOW + timedelta(days=30)
    # A month-equivalent duration elapses with no further discovery activity
    # at all -- time alone is not a discovery event and cannot itself
    # authorize anything (ADR 0004 §6).
    resource = store.list_resources(source_id)[0]
    assert resource.presence == "missing"
    assert resource.presence != "confirmed_removed"


# ---------------------------------------------------------------------------
# F -- ACL/permission-incomplete-equivalent outcome never authorizes removal
# ---------------------------------------------------------------------------


def test_row_f_configuration_error_outcome_never_touches_pointer_or_resource(tmp_path: Path) -> None:
    store, authority, source_id = make_authority(tmp_path)
    resource, witness_run = make_eligible_missing_resource(store, authority, source_id)
    pointer_before = store.resource_absence_pointer(resource.resource_id)

    run = authority.issue_discovery_run(source_id, 1)
    authority.finalize_failed_discovery_run(
        source_id, run.run_id,
        completion_evidence=DiscoveryRunCompletionEvidence(
            baseline_completeness=BaselineCompleteness.CONFIGURATION_ERROR
        ),
        reason="acl_topology_incomplete",
    )
    pointer_after = store.resource_absence_pointer(resource.resource_id)
    assert pointer_after == pointer_before
    resource_after = store.list_resources(source_id)[0]
    assert resource_after.presence == "missing"


# ---------------------------------------------------------------------------
# G, J -- witness alone or absent is never sufficient
# ---------------------------------------------------------------------------


def test_row_g_valid_witness_without_operator_assertions_rejected(tmp_path: Path) -> None:
    store, authority, source_id = make_authority(tmp_path)
    resource, witness_run = make_eligible_missing_resource(store, authority, source_id)
    with pytest.raises(ValueError):
        confirm(
            authority, source_id, resource, witness_run,
            confirms_removal=False, attests_absence=False,
        )
    assert store.list_resources(source_id)[0].presence == "missing"
    with store._read_connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM resource_removal_authorities"
        ).fetchone()[0] == 0


def test_row_j_both_assertions_true_but_witness_never_existed(tmp_path: Path) -> None:
    store, authority, source_id = make_authority(tmp_path)
    enroll(store, authority, source_id)
    complete_snapshot(store, authority, source_id, resources=(guest(),))
    complete_snapshot(store, authority, source_id, resources=())
    resource = store.list_resources(source_id)[0]
    with store._transaction() as connection:
        connection.execute(
            "DELETE FROM resource_absence_pointers WHERE resource_id=?", (resource.resource_id,)
        )
    with pytest.raises(AuthorityConflict):
        authority.confirm_class_c_resource_removal(
            source_id, resource.resource_id,
            expected_binding_id=resource.active_binding_id,
            expected_vmid=resource.vmid,
            expected_locator_generation=resource.locator_generation,
            expected_resource_continuity_revision=resource.resource_continuity_revision,
            expected_witness_run_id=_fresh_uuid(),
            expected_witness_discovery_run_sequence=1,
            actor="op", confirms_removal=True, attests_absence=True,
            reason="both true, no real witness",
        )


# ---------------------------------------------------------------------------
# S -- canonicalization migration also invalidates an outstanding witness
# ---------------------------------------------------------------------------


def test_row_s_canonicalization_migration_invalidates_confirmation(tmp_path: Path) -> None:
    store, authority, source_id = make_authority(tmp_path)
    authority = InventoryAuthority(
        store, now=lambda: NOW,
        _test_migration_contracts={2: lambda old: old.replace("pve.example", "pve2.example")},
    )
    resource, witness_run = make_eligible_missing_resource(store, authority, source_id)
    authority.migrate_canonicalization_contract(source_id, 2)
    with pytest.raises(AuthorityConflict):
        confirm(authority, source_id, resource, witness_run)


# ---------------------------------------------------------------------------
# AF -- no code path anywhere derives removal automatically from polling
# ---------------------------------------------------------------------------


def test_row_af_no_automatic_removal_code_path_exists() -> None:
    import inspect

    from app.inventory import authority as authority_module
    from app.inventory import reconciliation as reconciliation_module

    authority_source = inspect.getsource(authority_module)
    reconciliation_source = inspect.getsource(reconciliation_module)

    # confirmed_removed is written in exactly one place across the whole
    # dormant authority/reconciliation surface: the Class-C decision itself.
    assert "confirmed_removed" not in reconciliation_source
    occurrences = authority_source.count("presence='confirmed_removed'")
    assert occurrences == 1
    # No reconciliation call site is ever reachable from a bare polling
    # count/timer -- the only caller of the Class-C method is the operator
    # entry point itself; grep the whole authority module for any internal
    # self-invocation, which must not exist.
    assert "self.confirm_class_c_resource_removal(" not in authority_source


# ---------------------------------------------------------------------------
# AG -- successor never inherits Class-C evidence/trust
# ---------------------------------------------------------------------------


def test_row_ag_successor_has_no_linkage_to_predecessor_evidence(tmp_path: Path) -> None:
    store, authority, source_id = make_authority(tmp_path)
    resource, witness_run = make_eligible_missing_resource(store, authority, source_id)
    result = confirm(authority, source_id, resource, witness_run)
    complete_snapshot(store, authority, source_id, resources=(guest(),))
    successor = next(
        r for r in store.list_resources(source_id) if r.resource_id != resource.resource_id
    )
    assert successor.security_continuity == "unverified"
    assert successor.state_level == "discovered"
    with store._read_connection() as connection:
        linked = connection.execute(
            "SELECT COUNT(*) FROM resource_removal_authorities WHERE resource_id=?",
            (successor.resource_id,),
        ).fetchone()[0]
        linked_attestation = connection.execute(
            "SELECT COUNT(*) FROM resource_absence_attestations WHERE resource_id=?",
            (successor.resource_id,),
        ).fetchone()[0]
    assert linked == 0
    assert linked_attestation == 0
    assert store.resource_termination(successor.resource_id) is None


# ---------------------------------------------------------------------------
# Schema-level raw-SQL corruption attempts
# ---------------------------------------------------------------------------


def _decision_fields(resource, witness_run, attestation, source_id):
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
        actor="op",
        decided_at=NOW.isoformat(),
        reason="raw sql adversarial fixture",
    )


_COLUMNS = (
    "evidence_id", "decision_id", "inventory_source_id", "resource_id", "binding_id",
    "vmid", "locator_generation", "resource_continuity_revision", "witness_run_id",
    "witness_discovery_run_sequence", "source_config_revision", "endpoint_id",
    "canonical_transport_locator", "canonicalization_contract_version",
    "transport_trust_revision", "source_attestation_epoch", "actor", "decided_at", "reason",
)


def _insert(connection, table, *, evidence_id, decision_id, **fields):
    values = {"evidence_id": evidence_id, "decision_id": decision_id, **fields}
    connection.execute(
        f"INSERT INTO {table}({', '.join(_COLUMNS)}) VALUES({', '.join('?' for _ in _COLUMNS)})",
        tuple(values[c] for c in _COLUMNS),
    )


def test_foreign_source_reference_rejected_for_removal_authority(tmp_path: Path) -> None:
    store, authority, source_id = make_authority(tmp_path)
    resource, witness_run = make_eligible_missing_resource(store, authority, source_id)
    attestation = store.attestation_state(source_id)

    other_dir = tmp_path / "other"
    other_dir.mkdir()
    other_store, other_authority, other_source_id = make_authority(other_dir)
    complete_snapshot(other_store, other_authority, other_source_id, resources=(guest(vmid=555),))
    foreign_resource = other_store.list_resources(other_source_id)[0]

    fields = _decision_fields(resource, witness_run, attestation, source_id)
    fields["resource_id"] = foreign_resource.resource_id  # belongs to a different source
    with pytest.raises(sqlite3.IntegrityError):
        with store._transaction() as connection:
            _insert(connection, "resource_removal_authorities", evidence_id=_fresh_uuid(),
                    decision_id=_fresh_uuid(), **fields)


def test_foreign_binding_reference_rejected(tmp_path: Path) -> None:
    store, authority, source_id = make_authority(tmp_path)
    resource, witness_run = make_eligible_missing_resource(store, authority, source_id)
    attestation = store.attestation_state(source_id)
    fields = _decision_fields(resource, witness_run, attestation, source_id)
    fields["binding_id"] = _fresh_uuid()  # never existed at all
    with pytest.raises(sqlite3.IntegrityError):
        with store._transaction() as connection:
            _insert(connection, "resource_removal_authorities", evidence_id=_fresh_uuid(),
                    decision_id=_fresh_uuid(), **fields)


def test_foreign_witness_run_reference_rejected(tmp_path: Path) -> None:
    store, authority, source_id = make_authority(tmp_path)
    resource, witness_run = make_eligible_missing_resource(store, authority, source_id)
    attestation = store.attestation_state(source_id)

    other_dir = tmp_path / "other"
    other_dir.mkdir()
    other_store, other_authority, other_source_id = make_authority(other_dir)
    other_run = complete_snapshot(other_store, other_authority, other_source_id, resources=(guest(vmid=777),))

    fields = _decision_fields(resource, witness_run, attestation, source_id)
    fields["witness_run_id"] = other_run.run_id
    fields["witness_discovery_run_sequence"] = other_run.discovery_run_sequence
    with pytest.raises(sqlite3.IntegrityError):
        with store._transaction() as connection:
            _insert(connection, "resource_removal_authorities", evidence_id=_fresh_uuid(),
                    decision_id=_fresh_uuid(), **fields)


def test_absence_attestation_epoch_mismatch_rejected(tmp_path: Path) -> None:
    store, authority, source_id = make_authority(tmp_path)
    resource, witness_run = make_eligible_missing_resource(store, authority, source_id)
    attestation = store.attestation_state(source_id)
    fields = _decision_fields(resource, witness_run, attestation, source_id)
    decision_id = _fresh_uuid()
    mismatched = dict(fields)
    mismatched["source_attestation_epoch"] = fields["source_attestation_epoch"] + 1
    with pytest.raises(sqlite3.IntegrityError):
        with store._transaction() as connection:
            _insert(connection, "resource_removal_authorities", evidence_id=_fresh_uuid(),
                    decision_id=decision_id, **fields)
            _insert(connection, "resource_absence_attestations", evidence_id=_fresh_uuid(),
                    decision_id=decision_id, **mismatched)


# ---------------------------------------------------------------------------
# Same-source relational provenance (corrective pass -- P2 #2)
# ---------------------------------------------------------------------------


def _make_two_eligible_missing_resources(store, authority, source_id):
    enroll(store, authority, source_id)
    complete_snapshot(store, authority, source_id, resources=(guest(vmid=101), guest(vmid=102)))
    witness_run = complete_snapshot(store, authority, source_id, resources=())
    resources = store.list_resources(source_id)
    r1 = next(r for r in resources if r.vmid == 101)
    r2 = next(r for r in resources if r.vmid == 102)
    return r1, r2, witness_run


def test_same_source_resource_paired_with_unrelated_binding_rejected(tmp_path: Path) -> None:
    store, authority, source_id = make_authority(tmp_path)
    r1, r2, witness_run = _make_two_eligible_missing_resources(store, authority, source_id)
    attestation = store.attestation_state(source_id)
    fields = _decision_fields(r1, witness_run, attestation, source_id)
    fields["binding_id"] = r2.active_binding_id  # a real binding, but R2's, not R1's
    with pytest.raises(sqlite3.IntegrityError, match="binding provenance"):
        with store._transaction() as connection:
            _insert(connection, "resource_removal_authorities", evidence_id=_fresh_uuid(),
                    decision_id=_fresh_uuid(), **fields)


def test_correct_resource_binding_wrong_vmid_rejected(tmp_path: Path) -> None:
    store, authority, source_id = make_authority(tmp_path)
    r1, r2, witness_run = _make_two_eligible_missing_resources(store, authority, source_id)
    attestation = store.attestation_state(source_id)
    fields = _decision_fields(r1, witness_run, attestation, source_id)
    fields["vmid"] = r2.vmid  # a real vmid on this source, but not R1's
    with pytest.raises(sqlite3.IntegrityError, match="binding provenance"):
        with store._transaction() as connection:
            _insert(connection, "resource_removal_authorities", evidence_id=_fresh_uuid(),
                    decision_id=_fresh_uuid(), **fields)


def test_correct_resource_binding_wrong_locator_generation_rejected(tmp_path: Path) -> None:
    store, authority, source_id = make_authority(tmp_path)
    r1, r2, witness_run = _make_two_eligible_missing_resources(store, authority, source_id)
    attestation = store.attestation_state(source_id)
    fields = _decision_fields(r1, witness_run, attestation, source_id)
    fields["locator_generation"] = r1.locator_generation + 1  # never actually assigned
    with pytest.raises(sqlite3.IntegrityError, match="binding provenance"):
        with store._transaction() as connection:
            _insert(connection, "resource_removal_authorities", evidence_id=_fresh_uuid(),
                    decision_id=_fresh_uuid(), **fields)


def test_real_witness_run_wrong_sequence_rejected(tmp_path: Path) -> None:
    store, authority, source_id = make_authority(tmp_path)
    resource, witness_run = make_eligible_missing_resource(store, authority, source_id)
    attestation = store.attestation_state(source_id)
    fields = _decision_fields(resource, witness_run, attestation, source_id)
    fields["witness_discovery_run_sequence"] = witness_run.discovery_run_sequence + 1000
    with pytest.raises(sqlite3.IntegrityError, match="witness provenance"):
        with store._transaction() as connection:
            _insert(connection, "resource_removal_authorities", evidence_id=_fresh_uuid(),
                    decision_id=_fresh_uuid(), **fields)


def test_exact_matching_provenance_accepted(tmp_path: Path) -> None:
    """Positive control: fully consistent resource/binding/witness/context
    provenance -- both evidence rows and the linked tombstone remain legal."""

    store, authority, source_id = make_authority(tmp_path)
    resource, witness_run = make_eligible_missing_resource(store, authority, source_id)
    attestation = store.attestation_state(source_id)
    fields = _decision_fields(resource, witness_run, attestation, source_id)
    decision_id = _fresh_uuid()
    with store._transaction() as connection:
        _insert(connection, "resource_removal_authorities", evidence_id=_fresh_uuid(),
                decision_id=decision_id, **fields)
        _insert(connection, "resource_absence_attestations", evidence_id=_fresh_uuid(),
                decision_id=decision_id, **fields)
        connection.execute(
            "UPDATE resource_locator_bindings SET valid_to_run_sequence=?, "
            "closure_reason='confirmed_removed' WHERE binding_id=? AND valid_to_run_sequence IS NULL",
            (witness_run.discovery_run_sequence, resource.active_binding_id),
        )
        connection.execute(
            "INSERT INTO resource_terminations("
            "resource_id, inventory_source_id, binding_id, locator_generation, reason, "
            "successor_resource_id, run_sequence, class_c_decision_id, created_at) "
            "VALUES(?, ?, ?, ?, 'confirmed_removed', NULL, ?, ?, ?)",
            (resource.resource_id, source_id, resource.active_binding_id,
             resource.locator_generation, witness_run.discovery_run_sequence, decision_id, NOW.isoformat()),
        )
    termination = store.resource_termination(resource.resource_id)
    assert termination is not None
    assert termination.class_c_decision_id == decision_id


def test_resource_termination_immutable_after_commit(tmp_path: Path) -> None:
    store, authority, source_id = make_authority(tmp_path)
    resource, witness_run = make_eligible_missing_resource(store, authority, source_id)
    result = confirm(authority, source_id, resource, witness_run)
    assert result.termination.reason == "confirmed_removed"

    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        with store._transaction() as connection:
            connection.execute(
                "UPDATE resource_terminations SET run_sequence=999999 WHERE resource_id=?",
                (resource.resource_id,),
            )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        with store._transaction() as connection:
            connection.execute(
                "DELETE FROM resource_terminations WHERE resource_id=?", (resource.resource_id,)
            )
    unchanged = store.resource_termination(resource.resource_id)
    assert unchanged == result.termination


def test_replaced_termination_also_immutable(tmp_path: Path) -> None:
    """The immutability protection applies to BOTH terminal shapes, not
    only the new confirmed_removed path -- direct replacement's existing
    'replaced' tombstones are equally protected."""

    store, authority, source_id = make_authority(tmp_path)
    complete_snapshot(store, authority, source_id, resources=(guest(kind="lxc"),))
    old = store.list_resources(source_id)[0]
    complete_snapshot(store, authority, source_id, resources=(guest(kind="qemu"),))
    termination = store.resource_termination(old.resource_id)
    assert termination is not None
    assert termination.reason == "replaced"

    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        with store._transaction() as connection:
            connection.execute(
                "UPDATE resource_terminations SET run_sequence=999999 WHERE resource_id=?",
                (old.resource_id,),
            )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        with store._transaction() as connection:
            connection.execute(
                "DELETE FROM resource_terminations WHERE resource_id=?", (old.resource_id,)
            )
    unchanged = store.resource_termination(old.resource_id)
    assert unchanged == termination


def test_current_pointer_remains_updateable_no_append_only_growth(tmp_path: Path) -> None:
    store, authority, source_id = make_authority(tmp_path)
    enroll(store, authority, source_id)
    complete_snapshot(store, authority, source_id, resources=(guest(),))
    resource_id = store.list_resources(source_id)[0].resource_id
    for _ in range(25):
        complete_snapshot(store, authority, source_id, resources=())
    with store._read_connection() as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM resource_absence_pointers WHERE resource_id=?",
            (resource_id,),
        ).fetchone()[0]
    assert count == 1
