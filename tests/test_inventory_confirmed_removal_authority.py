"""WAVE A1 Commit 3: the Class-C confirmed-removal authority operation.

ADR 0004 §9-§24: two explicit operator assertions, exact current
sampled-absence witness, full source/attestation/resource/binding/revision/
freshness CAS, one atomic terminal transition. No remote I/O.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.inventory import (
    AuthorityConflict,
    AuthorityInvariantError,
    AuthorityNotFound,
    BaselineCompleteness,
    BaselineMode,
    DetailReadStatus,
    DiscoveredNode,
    DiscoveredResource,
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


def make_eligible_missing_resource(store, authority, source_id):
    """Enroll attestation, then present -> missing; return (resource, witness_run)."""

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


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_successful_confirmed_removal_full_terminal_transition(tmp_path: Path) -> None:
    store, authority, source_id = make_authority(tmp_path)
    resource, witness_run = make_eligible_missing_resource(store, authority, source_id)
    before_backend = store.backend_instance()

    result = confirm(authority, source_id, resource, witness_run)

    final = store.list_resources(source_id)[0]
    assert final.presence == "confirmed_removed"
    assert final.lifecycle == "retired"
    assert final.detail_status == "not_applicable"
    assert final.node_availability == "not_applicable"
    assert final.current_node_id is None
    assert final.observational_continuity == "uncertain"  # preserved from missing state
    assert final.security_continuity == "unverified"  # was never trusted
    assert final.termination_reason == "confirmed_removed"
    assert final.successor_resource_id is None
    assert final.resource_continuity_revision == resource.resource_continuity_revision + 1

    binding = [b for b in store.list_bindings(source_id) if b.binding_id == resource.active_binding_id][0]
    assert binding.valid_to_run_sequence == witness_run.discovery_run_sequence
    assert binding.closure_reason == "confirmed_removed"

    termination = store.resource_termination(resource.resource_id)
    assert termination.reason == "confirmed_removed"
    assert termination.successor_resource_id is None
    assert termination.run_sequence == witness_run.discovery_run_sequence
    assert termination.class_c_decision_id == result.decision_id

    assert result.removal_authority.decision_id == result.decision_id
    assert result.absence_attestation.decision_id == result.decision_id
    assert result.removal_authority.resource_id == resource.resource_id
    assert result.absence_attestation.resource_id == resource.resource_id
    assert result.removal_authority.evidence_id != result.absence_attestation.evidence_id

    after_backend = store.backend_instance()
    assert after_backend.inventory_revision == before_backend.inventory_revision + 1
    assert after_backend.published_state_revision == before_backend.published_state_revision + 1

    assert store.resource_absence_pointer(resource.resource_id) is None

    # Source health/sequences remain completely untouched by the decision.
    before_state = store.source_state(source_id)
    assert before_state.source.last_committed_run_sequence == witness_run.discovery_run_sequence
    assert before_state.source.last_issued_run_sequence == witness_run.discovery_run_sequence
    assert before_state.runtime_health.health == "healthy"
    assert before_state.runtime_health.freshness == "fresh"


def test_trusted_resource_security_continuity_revoked(tmp_path: Path) -> None:
    store, authority, source_id = make_authority(tmp_path)
    resource, witness_run = make_eligible_missing_resource(store, authority, source_id)
    with store._transaction() as connection:
        connection.execute(
            "UPDATE resource_incarnations SET security_continuity='trusted' WHERE resource_id=?",
            (resource.resource_id,),
        )
    resource = store.list_resources(source_id)[0]
    confirm(authority, source_id, resource, witness_run)
    final = store.list_resources(source_id)[0]
    assert final.security_continuity == "revoked"


# ---------------------------------------------------------------------------
# Two mandatory operator assertions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("confirms_removal,attests_absence", [(False, True), (True, False), (False, False)])
def test_both_assertions_mandatory(tmp_path: Path, confirms_removal, attests_absence) -> None:
    store, authority, source_id = make_authority(tmp_path)
    resource, witness_run = make_eligible_missing_resource(store, authority, source_id)
    with pytest.raises(ValueError):
        confirm(
            authority, source_id, resource, witness_run,
            confirms_removal=confirms_removal, attests_absence=attests_absence,
        )
    unchanged = store.list_resources(source_id)[0]
    assert unchanged.presence == "missing"


@pytest.mark.parametrize("field,value", [("confirms_removal", "true"), ("confirms_removal", 1), ("attests_absence", "yes"), ("attests_absence", 1)])
def test_truthy_non_bool_assertions_rejected(tmp_path: Path, field, value) -> None:
    store, authority, source_id = make_authority(tmp_path)
    resource, witness_run = make_eligible_missing_resource(store, authority, source_id)
    with pytest.raises(ValueError):
        confirm(authority, source_id, resource, witness_run, **{field: value})


def test_vmid_only_targeting_is_not_supported_by_the_call_boundary(tmp_path: Path) -> None:
    """The typed call boundary has no VMID-only entry point at all -- the
    exact resource_id/binding_id/generation/revision tuple is mandatory."""

    import inspect

    signature = inspect.signature(InventoryAuthority.confirm_class_c_resource_removal)
    required = {
        name for name, param in signature.parameters.items()
        if param.default is inspect._empty and name != "self"
    }
    assert {
        "inventory_source_id", "resource_id", "expected_binding_id", "expected_vmid",
        "expected_locator_generation", "expected_resource_continuity_revision",
        "expected_witness_run_id", "expected_witness_discovery_run_sequence",
    } <= required


# ---------------------------------------------------------------------------
# Source / attestation CAS
# ---------------------------------------------------------------------------


def test_active_discovery_run_blocks_confirmation(tmp_path: Path) -> None:
    store, authority, source_id = make_authority(tmp_path)
    resource, witness_run = make_eligible_missing_resource(store, authority, source_id)
    authority.issue_discovery_run(source_id, 1)
    with pytest.raises(AuthorityConflict):
        confirm(authority, source_id, resource, witness_run)


def test_not_yet_attested_source_blocks_confirmation(tmp_path: Path) -> None:
    store, authority, source_id = make_authority(tmp_path)
    complete_snapshot(store, authority, source_id, resources=(guest(),))
    witness_run = complete_snapshot(store, authority, source_id, resources=())
    resource = store.list_resources(source_id)[0]
    assert store.attestation_state(source_id).attestation_status.value == "not_yet_attested"
    with pytest.raises(AuthorityConflict):
        confirm(authority, source_id, resource, witness_run)


def test_mismatch_pending_reattestation_blocks_confirmation(tmp_path: Path) -> None:
    store, authority, source_id = make_authority(tmp_path)
    resource, witness_run = make_eligible_missing_resource(store, authority, source_id)
    authority.reattest_source(
        source_id, endpoint_id=active_endpoint_id(store, source_id),
        actor="operator:reattest", evidence_reader=_FakeReader("different-anchor"),
    )
    assert store.attestation_state(source_id).relationship_gate.value == "mismatch_pending_reattestation"
    with pytest.raises(AuthorityConflict):
        confirm(authority, source_id, resource, witness_run)


def test_epoch_bump_after_witness_invalidates_confirmation(tmp_path: Path) -> None:
    store, authority, source_id = make_authority(tmp_path)
    resource, witness_run = make_eligible_missing_resource(store, authority, source_id)
    authority.accept_source_attestation_anchor_change(
        source_id, endpoint_id=active_endpoint_id(store, source_id),
        actor="operator:rotate", evidence_reader=_FakeReader("new-anchor"),
    )
    with pytest.raises(AuthorityConflict):
        confirm(authority, source_id, resource, witness_run)


def test_same_anchor_reconfirmation_does_not_invalidate_witness(tmp_path: Path) -> None:
    store, authority, source_id = make_authority(tmp_path)
    resource, witness_run = make_eligible_missing_resource(store, authority, source_id)
    authority.reattest_source(
        source_id, endpoint_id=active_endpoint_id(store, source_id),
        actor="operator:reattest", evidence_reader=_FakeReader("deadbeef"),
    )
    result = confirm(authority, source_id, resource, witness_run)
    assert result.decision_id


# ---------------------------------------------------------------------------
# Resource / binding / revision CAS
# ---------------------------------------------------------------------------


def test_stale_resource_continuity_revision_rejected(tmp_path: Path) -> None:
    store, authority, source_id = make_authority(tmp_path)
    resource, witness_run = make_eligible_missing_resource(store, authority, source_id)
    with pytest.raises(AuthorityConflict):
        confirm(
            authority, source_id, resource, witness_run,
            expected_resource_continuity_revision=resource.resource_continuity_revision + 1,
        )


def test_stale_binding_id_rejected(tmp_path: Path) -> None:
    store, authority, source_id = make_authority(tmp_path)
    resource, witness_run = make_eligible_missing_resource(store, authority, source_id)
    with pytest.raises(AuthorityConflict):
        confirm(authority, source_id, resource, witness_run, expected_binding_id=_fresh_uuid())


def test_stale_locator_generation_rejected(tmp_path: Path) -> None:
    store, authority, source_id = make_authority(tmp_path)
    resource, witness_run = make_eligible_missing_resource(store, authority, source_id)
    with pytest.raises(AuthorityConflict):
        confirm(authority, source_id, resource, witness_run, expected_locator_generation=resource.locator_generation + 1)


def test_stale_vmid_rejected(tmp_path: Path) -> None:
    store, authority, source_id = make_authority(tmp_path)
    resource, witness_run = make_eligible_missing_resource(store, authority, source_id)
    with pytest.raises(AuthorityConflict):
        confirm(authority, source_id, resource, witness_run, expected_vmid=resource.vmid + 1)


def test_slot_present_again_before_commit_rejected(tmp_path: Path) -> None:
    store, authority, source_id = make_authority(tmp_path)
    resource, witness_run = make_eligible_missing_resource(store, authority, source_id)
    complete_snapshot(store, authority, source_id, resources=(guest(),))
    with pytest.raises(AuthorityConflict):
        confirm(authority, source_id, resource, witness_run)


def test_direct_replacement_already_won_rejected(tmp_path: Path) -> None:
    store, authority, source_id = make_authority(tmp_path)
    enroll(store, authority, source_id)
    complete_snapshot(store, authority, source_id, resources=(guest(kind="lxc"),))
    witness_run = complete_snapshot(store, authority, source_id, resources=())
    resource = store.list_resources(source_id)[0]
    complete_snapshot(store, authority, source_id, resources=(guest(kind="qemu"),))  # replaces
    with pytest.raises(AuthorityConflict):
        confirm(authority, source_id, resource, witness_run)


def test_unknown_resource_rejected(tmp_path: Path) -> None:
    store, authority, source_id = make_authority(tmp_path)
    resource, witness_run = make_eligible_missing_resource(store, authority, source_id)
    with pytest.raises(AuthorityNotFound):
        confirm(authority, source_id, resource, witness_run, resource_id=_fresh_uuid())


def test_present_resource_never_directly_confirmed_removed(tmp_path: Path) -> None:
    store, authority, source_id = make_authority(tmp_path)
    enroll(store, authority, source_id)
    complete_snapshot(store, authority, source_id, resources=(guest(),))
    resource = store.list_resources(source_id)[0]
    # There is no witness at all for a present resource -- must fail before
    # even reaching the presence check via the witness precondition, or via
    # the presence check itself if a witness happens to exist from history.
    with pytest.raises(AuthorityConflict):
        authority.confirm_class_c_resource_removal(
            source_id, resource.resource_id,
            expected_binding_id=resource.active_binding_id,
            expected_vmid=resource.vmid,
            expected_locator_generation=resource.locator_generation,
            expected_resource_continuity_revision=resource.resource_continuity_revision,
            expected_witness_run_id=_fresh_uuid(),
            expected_witness_discovery_run_sequence=1,
            actor="op", confirms_removal=True, attests_absence=True, reason="bad",
        )


# ---------------------------------------------------------------------------
# Witness / freshness CAS
# ---------------------------------------------------------------------------


def test_missing_witness_pointer_rejected(tmp_path: Path) -> None:
    store, authority, source_id = make_authority(tmp_path)
    enroll(store, authority, source_id)
    complete_snapshot(store, authority, source_id, resources=(guest(),))
    resource = store.list_resources(source_id)[0]
    with pytest.raises(AuthorityConflict):
        confirm(
            authority, source_id, resource,
            witness_run=type("R", (), {"run_id": _fresh_uuid(), "discovery_run_sequence": 999})(),
        )


def test_newer_successful_run_makes_witness_stale(tmp_path: Path) -> None:
    store, authority, source_id = make_authority(tmp_path)
    resource, witness_run = make_eligible_missing_resource(store, authority, source_id)
    complete_snapshot(store, authority, source_id, resources=())  # reconfirm at N+1
    with pytest.raises(AuthorityConflict):
        confirm(authority, source_id, resource, witness_run)
    # The N+1 witness works fine.
    resource_current = store.list_resources(source_id)[0]
    pointer = store.resource_absence_pointer(resource.resource_id)
    run_n1 = store.discovery_run(pointer.witness_run_id)
    result = confirm(authority, source_id, resource_current, run_n1)
    assert result.decision_id


def test_newer_failed_run_degrades_freshness_and_blocks(tmp_path: Path) -> None:
    from app.inventory import DiscoveryRunCompletionEvidence

    store, authority, source_id = make_authority(tmp_path)
    resource, witness_run = make_eligible_missing_resource(store, authority, source_id)
    run = authority.issue_discovery_run(source_id, 1)
    authority.finalize_failed_discovery_run(
        source_id, run.run_id,
        completion_evidence=DiscoveryRunCompletionEvidence(
            baseline_completeness=BaselineCompleteness.SOURCE_UNAVAILABLE
        ),
        reason="unreachable",
    )
    # last_committed_run_sequence still equals witness_run's sequence.
    assert store.source_state(source_id).source.last_committed_run_sequence == witness_run.discovery_run_sequence
    with pytest.raises(AuthorityConflict):
        confirm(authority, source_id, resource, witness_run)


def test_source_config_change_invalidates_confirmation(tmp_path: Path) -> None:
    store, authority, source_id = make_authority(tmp_path)
    resource, witness_run = make_eligible_missing_resource(store, authority, source_id)
    authority.rotate_credential_reference(source_id, "secret://inventory/rotated")
    with pytest.raises(AuthorityConflict):
        confirm(authority, source_id, resource, witness_run)


def test_transport_trust_rotation_invalidates_confirmation(tmp_path: Path) -> None:
    store, authority, source_id = make_authority(tmp_path)
    resource, witness_run = make_eligible_missing_resource(store, authority, source_id)
    authority.rotate_transport_trust(source_id)
    with pytest.raises(AuthorityConflict):
        confirm(authority, source_id, resource, witness_run)


def test_freshness_expiry_blocks_confirmation(tmp_path: Path) -> None:
    from datetime import timedelta

    clock = {"now": NOW}
    store = InventoryAuthorityStore(tmp_path / "authority.db", now=lambda: clock["now"])
    authority = InventoryAuthority(store, now=lambda: clock["now"])
    state = authority.create_inventory_source(
        provider_kind="proxmox", display_name="P",
        credential_reference="secret://x", transport_locator="https://pve.example:8006",
        freshness_duration_seconds=60,
    )
    source_id = state.source.inventory_source_id
    enroll(store, authority, source_id)
    complete_snapshot(store, authority, source_id, resources=(guest(),))
    witness_run = complete_snapshot(store, authority, source_id, resources=())
    resource = store.list_resources(source_id)[0]

    clock["now"] = NOW + timedelta(seconds=120)
    with pytest.raises(AuthorityConflict):
        confirm(authority, source_id, resource, witness_run)


def test_evidence_replay_rejected_once_already_terminal(tmp_path: Path) -> None:
    store, authority, source_id = make_authority(tmp_path)
    resource, witness_run = make_eligible_missing_resource(store, authority, source_id)
    confirm(authority, source_id, resource, witness_run)
    with pytest.raises(AuthorityConflict):
        confirm(authority, source_id, resource, witness_run)


def test_two_operators_racing_exactly_one_wins(tmp_path: Path) -> None:
    store, authority, source_id = make_authority(tmp_path)
    resource, witness_run = make_eligible_missing_resource(store, authority, source_id)
    first = confirm(authority, source_id, resource, witness_run, actor="operator:first")
    assert first.decision_id
    with pytest.raises(AuthorityConflict):
        confirm(authority, source_id, resource, witness_run, actor="operator:second")


# ---------------------------------------------------------------------------
# Atomicity / crash semantics
# ---------------------------------------------------------------------------


def test_injected_failure_before_commit_leaves_no_partial_state(tmp_path: Path, monkeypatch) -> None:
    store, authority, source_id = make_authority(tmp_path)
    resource, witness_run = make_eligible_missing_resource(store, authority, source_id)
    before = store.list_resources(source_id)[0]
    before_backend = store.backend_instance()

    real_bump = authority._bump_global_revisions

    def failing_bump(connection, **kwargs):
        raise RuntimeError("injected pre-commit failure")

    monkeypatch.setattr(authority, "_bump_global_revisions", failing_bump)
    with pytest.raises(RuntimeError, match="injected pre-commit failure"):
        confirm(authority, source_id, resource, witness_run)

    after = store.list_resources(source_id)[0]
    after_backend = store.backend_instance()
    assert after == before
    assert after_backend == before_backend
    assert store.resource_termination(resource.resource_id) is None
    assert store.resource_absence_pointer(resource.resource_id) is not None
    with store._read_connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM resource_removal_authorities").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM resource_absence_attestations").fetchone()[0] == 0


def test_reopen_after_committed_decision_is_fully_durable(tmp_path: Path) -> None:
    store, authority, source_id = make_authority(tmp_path)
    resource, witness_run = make_eligible_missing_resource(store, authority, source_id)
    result = confirm(authority, source_id, resource, witness_run)
    store.close()

    reopened = InventoryAuthorityStore(tmp_path / "authority.db")
    final = reopened.list_resources(source_id)[0]
    assert final.presence == "confirmed_removed"
    fetched = reopened.confirmed_removal_result(result.decision_id)
    assert fetched.resource_id == resource.resource_id
    assert fetched.termination.reason == "confirmed_removed"


# ---------------------------------------------------------------------------
# Post-terminal reappearance
# ---------------------------------------------------------------------------


def test_reappearance_after_confirmed_removal_creates_new_incarnation(tmp_path: Path) -> None:
    store, authority, source_id = make_authority(tmp_path)
    resource, witness_run = make_eligible_missing_resource(store, authority, source_id)
    confirm(authority, source_id, resource, witness_run)
    old = store.list_resources(source_id)[0]

    # Identical type/name/config/node as the original.
    complete_snapshot(store, authority, source_id, resources=(guest(),))
    resources = store.list_resources(source_id)
    assert len(resources) == 2
    new = next(r for r in resources if r.resource_id != old.resource_id)

    assert new.resource_id != old.resource_id
    assert new.locator_generation == old.locator_generation + 1
    assert new.active_binding_id != old.active_binding_id
    assert new.presence == "present"
    assert new.lifecycle == "active"
    assert new.security_continuity == "unverified"
    assert new.state_level == "discovered"

    # Old terminal record is completely unchanged.
    old_after = store.list_resources(source_id)[0] if old.resource_id == resources[0].resource_id else resources[0]
    old_current = next(r for r in resources if r.resource_id == old.resource_id)
    assert old_current.presence == "confirmed_removed"
    assert old_current.lifecycle == "retired"
    assert old_current.active_binding_id is None
    termination = store.resource_termination(old.resource_id)
    assert termination is not None
    assert termination.reason == "confirmed_removed"


def _fresh_uuid() -> str:
    import uuid

    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# decided_at boundary (corrective pass -- P2 #3)
# ---------------------------------------------------------------------------


def test_decided_at_is_captured_inside_transaction_after_cas(tmp_path: Path, monkeypatch) -> None:
    """ADR 0004 §19 step 3: `decided_at` must be this transaction's own
    decision timestamp -- captured only after every CAS precondition has
    already held inside the authoritative transaction, never before
    `BEGIN IMMEDIATE` even opened it.

    Deterministic proof, no sleeps: an incrementing sequence clock makes
    every `self._now()` call return a strictly later tick than the one
    before it. `_authority_decision_time()` (the freshness-evaluation
    clock read, §15) is always the *first* clock call inside the
    transaction; if `decided_at` were still captured before the
    transaction opens (the pre-corrective-pass bug), its tick would be
    strictly *older* than the recorded decision_time. This test fails
    against that old ordering and passes only when `decided_at` is
    strictly newer.
    """

    from datetime import timedelta

    ticks = {"n": 0}
    base = NOW

    def clock() -> datetime:
        ticks["n"] += 1
        return base + timedelta(seconds=ticks["n"])

    store = InventoryAuthorityStore(tmp_path / "authority.db", now=clock)
    authority = InventoryAuthority(store, now=clock)
    state = authority.create_inventory_source(
        provider_kind="proxmox",
        display_name="Primary",
        credential_reference="secret://inventory/primary",
        transport_locator="https://pve.example:8006",
    )
    source_id = state.source.inventory_source_id
    resource, witness_run = make_eligible_missing_resource(store, authority, source_id)

    recorded: dict[str, datetime] = {}
    real_decision_time = authority._authority_decision_time

    def wrapped() -> datetime:
        value = real_decision_time()
        recorded.setdefault("decision_time", value)
        return value

    monkeypatch.setattr(authority, "_authority_decision_time", wrapped)

    result = confirm(authority, source_id, resource, witness_run)

    assert "decision_time" in recorded
    persisted_decided_at = datetime.fromisoformat(result.removal_authority.decided_at)
    assert persisted_decided_at > recorded["decision_time"], (
        "decided_at must be captured strictly after the in-transaction "
        "freshness clock read, never before BEGIN IMMEDIATE opened"
    )
    # Both evidence records carry the identical formal decision timestamp.
    assert result.absence_attestation.decided_at == result.removal_authority.decided_at


# ---------------------------------------------------------------------------
# Explicit intermediate atomicity injection points (corrective pass -- P3 #1)
# ---------------------------------------------------------------------------
#
# The existing test_injected_failure_before_commit_leaves_no_partial_state
# (above) already proves the general atomicity guarantee by failing at the
# very last statement in the transaction. These two tests additionally prove
# the identical guarantee holds at two named intermediate points the
# original mission called out explicitly, using a real (non-temp) SQLite
# trigger installed ad hoc on the test's own database file -- never a
# production failure-injection API.


def _install_raising_trigger(db_path: Path, *, name: str, table: str, event: str, when: str = "") -> None:
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            f"CREATE TRIGGER {name} BEFORE {event} ON {table} {when} "
            f"BEGIN SELECT RAISE(ABORT, 'test-injected failure at {name}'); END"
        )


def test_injected_failure_after_evidence_before_resource_update(tmp_path: Path) -> None:
    store, authority, source_id = make_authority(tmp_path)
    resource, witness_run = make_eligible_missing_resource(store, authority, source_id)
    before = store.list_resources(source_id)[0]
    before_backend = store.backend_instance()

    _install_raising_trigger(
        tmp_path / "authority.db",
        name="test_fail_before_resource_update",
        table="resource_incarnations",
        event="UPDATE OF presence",
        when="WHEN NEW.presence='confirmed_removed'",
    )

    with pytest.raises(sqlite3.IntegrityError, match="test_fail_before_resource_update"):
        confirm(authority, source_id, resource, witness_run)

    after = store.list_resources(source_id)[0]
    after_backend = store.backend_instance()
    assert after == before
    assert after_backend == before_backend
    assert store.resource_termination(resource.resource_id) is None
    assert store.resource_absence_pointer(resource.resource_id) is not None
    binding = [b for b in store.list_bindings(source_id) if b.binding_id == resource.active_binding_id][0]
    assert binding.valid_to_run_sequence is None
    with store._read_connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM resource_removal_authorities").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM resource_absence_attestations").fetchone()[0] == 0


def test_injected_failure_after_binding_closure_before_tombstone(tmp_path: Path) -> None:
    store, authority, source_id = make_authority(tmp_path)
    resource, witness_run = make_eligible_missing_resource(store, authority, source_id)
    before = store.list_resources(source_id)[0]
    before_backend = store.backend_instance()

    _install_raising_trigger(
        tmp_path / "authority.db",
        name="test_fail_before_tombstone",
        table="resource_terminations",
        event="INSERT",
        when="WHEN NEW.reason='confirmed_removed'",
    )

    with pytest.raises(sqlite3.IntegrityError, match="test_fail_before_tombstone"):
        confirm(authority, source_id, resource, witness_run)

    after = store.list_resources(source_id)[0]
    after_backend = store.backend_instance()
    assert after == before
    assert after_backend == before_backend
    assert store.resource_termination(resource.resource_id) is None
    assert store.resource_absence_pointer(resource.resource_id) is not None
    binding = [b for b in store.list_bindings(source_id) if b.binding_id == resource.active_binding_id][0]
    assert binding.valid_to_run_sequence is None
    with store._read_connection() as connection:
        # Both evidence rows were inserted earlier in this same transaction,
        # but the later failure still rolls them back too -- one atomic
        # write, no partial acceptance regardless of how far it progressed.
        assert connection.execute("SELECT COUNT(*) FROM resource_removal_authorities").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM resource_absence_attestations").fetchone()[0] == 0


# ---------------------------------------------------------------------------
# In-flight pointer cannot authorize removal (corrective pass -- P3 #3)
# ---------------------------------------------------------------------------


def test_dangling_abandoned_run_pointer_cannot_authorize_removal(tmp_path: Path) -> None:
    """Raw SQL can attach a sampled-absence pointer to a still-active
    issued/running run (the one legitimate in-flight shape Commit 2's own
    reconciliation write produces inside its own transaction); that run can
    then be abandoned, leaving a dangling reference. This must never be
    usable to authorize Class-C removal -- the authority method's own CAS
    independently re-validates the witness run's actual final state."""

    store, authority, source_id = make_authority(tmp_path)
    resource, witness_run = make_eligible_missing_resource(store, authority, source_id)
    # Re-present, then go missing again with no witness pointer this time,
    # by directly attaching a raw pointer to a run that never completes.
    complete_snapshot(store, authority, source_id, resources=(guest(),))
    complete_snapshot(store, authority, source_id, resources=())
    resource = store.list_resources(source_id)[0]

    stray_run = authority.issue_discovery_run(source_id, 1)
    authority.mark_discovery_run_running(source_id, stray_run.run_id)
    with store._transaction() as connection:
        connection.execute(
            "INSERT INTO resource_absence_pointers("
            "resource_id, inventory_source_id, witness_run_id, "
            "witness_discovery_run_sequence, updated_at) VALUES(?, ?, ?, ?, ?) "
            "ON CONFLICT(resource_id) DO UPDATE SET witness_run_id=excluded.witness_run_id, "
            "witness_discovery_run_sequence=excluded.witness_discovery_run_sequence, "
            "updated_at=excluded.updated_at",
            (resource.resource_id, source_id, stray_run.run_id, stray_run.discovery_run_sequence, NOW.isoformat()),
        )
    authority.abandon_discovery_run(source_id, stray_run.run_id, reason="dangling_witness_test")

    with pytest.raises(AuthorityConflict):
        confirm(authority, source_id, resource, stray_run)

    # Nothing was authorized; the resource remains exactly as before.
    unchanged = store.list_resources(source_id)[0]
    assert unchanged.presence == "missing"
    assert unchanged.lifecycle == "quarantined"
    assert store.resource_termination(resource.resource_id) is None
