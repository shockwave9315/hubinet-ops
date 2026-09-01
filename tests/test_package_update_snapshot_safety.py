"""Job-owned snapshot safety: identity, write-ahead, confirmation, recovery.

Covers the durable half of one package update job's single pre-update PVE
snapshot: how its identity is derived, how the write-ahead uncertainty
boundary behaves, what counts as canonical confirmation, what happens after a
crash, and which snapshot -- if any -- that job may later roll back to.

Nothing here performs, or can reach, a real PVE operation or any package
mutation. There is no APT execution anywhere in this stage.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sqlite3
import uuid

import pytest

from app.inventory import (
    AuthorityConflict,
    AuthorityInvariantError,
    InventoryAuthority,
    InventoryAuthorityStore,
    ObservedSnapshot,
    PackageUpdateCheckpoint,
    PackageUpdateEventType,
    PackageUpdateJobStatus,
    SnapshotIdentityError,
    SnapshotOwnership,
    build_snapshot_ownership,
    derive_pre_update_snapshot_identity,
    encode_snapshot_description,
    parse_snapshot_description,
    validate_pve_snapshot_name,
)
from app.inventory.snapshot_identity import (
    PVE_RESERVED_SNAPSHOT_NAMES,
    PVE_SNAPSHOT_NAME_MAX_LENGTH,
    SNAPSHOT_METADATA_MARKER,
)
from app.package_update_snapshot import (
    HostSnapshotResult,
    PackageUpdateSnapshotOrchestrator,
    SnapshotEvidenceError,
    SnapshotOperationOutcome,
    SnapshotTaskState,
    classify_task_status,
    parse_canonical_snapshot_listing,
)
from tests.test_package_scan_authority import _reconcile
from tests.test_package_update_job_authority import (
    _add_approved_resource,
    _approved_system,
    _issue,
)


UPID = "UPID:pve-a:0000ABCD:000DC5EA:57500527:vzsnapshot:112:root@pam:"
OTHER_UPID = "UPID:pve-a:0000ABCE:000DC5EB:57500528:vzsnapshot:112:root@pam:"


# ---------------------------------------------------------------------------
# Canonical listing fixtures
# ---------------------------------------------------------------------------


def _current_entry() -> ObservedSnapshot:
    """PVE's synthetic listing row, exactly as its API returns it."""

    return ObservedSnapshot(
        name="current",
        description="You are here!",
        is_current_pseudo_entry=True,
    )


def _owned_entry(
    ownership: SnapshotOwnership, name: str, **overrides
) -> ObservedSnapshot:
    entry = ObservedSnapshot(
        name=name,
        # Reproduces the real PVE round trip: the LXC config parser appends a
        # newline to every description line it reads back.
        description=encode_snapshot_description(ownership) + "\n",
        snaptime=1_700_000_000,
        ownership=ownership,
    )
    return replace(entry, **overrides) if overrides else entry


def _foreign_entry(name: str = "operator-manual") -> ObservedSnapshot:
    return ObservedSnapshot(
        name=name, description="taken by hand before a config change", snaptime=1
    )


def _prepared(tmp_path: Path):
    """One issued job advanced to the write-ahead snapshot intent."""

    clock, store, authority, resource, scan, approval = _approved_system(tmp_path)
    job = _issue(authority, resource, approval)
    identity = authority.package_update_snapshot_identity(job.job_id)
    ownership = authority.package_update_snapshot_ownership(job.job_id)
    authority.record_package_update_preflight_passed(job.job_id)
    job = authority.record_package_update_snapshot_intent(job.job_id)
    return clock, store, authority, resource, job, identity, ownership


def _canonical(ownership: SnapshotOwnership, identity) -> tuple[ObservedSnapshot, ...]:
    return (_current_entry(), _foreign_entry(), _owned_entry(ownership, identity.snapshot_name))


# ===========================================================================
# A. IDENTITY
# ===========================================================================


def test_same_job_derives_the_same_snapshot_identity_across_restart(
    tmp_path: Path,
) -> None:
    clock, store, authority, resource, _, approval = _approved_system(tmp_path)
    job = _issue(authority, resource, approval)

    first = authority.package_update_snapshot_identity(job.job_id)
    path = store.path
    store.close()
    reopened = InventoryAuthorityStore(path, now=clock)
    second = InventoryAuthority(reopened, now=clock).package_update_snapshot_identity(
        job.job_id
    )

    assert first == second
    # Derivation is a pure function of immutable identity, never of the clock.
    assert first.snapshot_name == derive_pre_update_snapshot_identity(
        backend_instance_id=reopened.backend_instance().backend_instance_id,
        job_id=job.job_id,
        resource_id=job.resource_id,
        resource_continuity_revision=job.expected_resource_continuity_revision,
    ).snapshot_name


def test_derived_snapshot_name_satisfies_pve_naming_constraints() -> None:
    identity = derive_pre_update_snapshot_identity(
        backend_instance_id=str(uuid.uuid4()),
        job_id=str(uuid.uuid4()),
        resource_id=str(uuid.uuid4()),
        resource_continuity_revision=1,
    )
    name = identity.snapshot_name
    # pve-configid: leading letter, then [A-Za-z0-9_-], maxLength 40.
    assert validate_pve_snapshot_name(name) == name
    assert 2 <= len(name) <= PVE_SNAPSHOT_NAME_MAX_LENGTH
    assert name[0].isalpha()
    assert set(name) <= set(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
    )
    assert name.lower() not in PVE_RESERVED_SNAPSHOT_NAMES


@pytest.mark.parametrize(
    "name",
    ["current", "vzdump", "9leading", "a", "", "has.dot", "has space", "x" * 41],
)
def test_pve_invalid_or_reserved_snapshot_names_are_rejected(name: str) -> None:
    with pytest.raises(SnapshotIdentityError):
        validate_pve_snapshot_name(name)


def test_different_jobs_on_the_same_resource_cannot_collide(tmp_path: Path) -> None:
    _, store, authority, resource, _, approval = _approved_system(tmp_path)
    first = _issue(authority, resource, approval)
    first_identity = authority.package_update_snapshot_identity(first.job_id)
    authority.recover_interrupted_package_update_jobs()
    second = _issue(authority, resource, approval)
    second_identity = authority.package_update_snapshot_identity(second.job_id)

    assert first.resource_id == second.resource_id
    assert first_identity.snapshot_name != second_identity.snapshot_name
    assert (
        first_identity.snapshot_operation_id != second_identity.snapshot_operation_id
    )


def test_same_job_identity_on_a_different_backend_instance_differs() -> None:
    job_id, resource_id = str(uuid.uuid4()), str(uuid.uuid4())
    one = derive_pre_update_snapshot_identity(
        backend_instance_id=str(uuid.uuid4()),
        job_id=job_id,
        resource_id=resource_id,
        resource_continuity_revision=1,
    )
    two = derive_pre_update_snapshot_identity(
        backend_instance_id=str(uuid.uuid4()),
        job_id=job_id,
        resource_id=resource_id,
        resource_continuity_revision=1,
    )
    assert one.snapshot_name != two.snapshot_name


def test_ownership_metadata_round_trips_through_pve_description_framing() -> None:
    ownership = build_snapshot_ownership(
        job_id=str(uuid.uuid4()),
        resource_id=str(uuid.uuid4()),
        resource_continuity_revision=7,
        inventory_source_id=str(uuid.uuid4()),
        backend_instance_id=str(uuid.uuid4()),
    )
    description = encode_snapshot_description(ownership)
    assert description.isascii()
    # PVE re-reads a description with a newline appended to every line, so an
    # exact-string comparison would be wrong; the parser must normalise.
    assert parse_snapshot_description(description + "\n") == ownership
    assert parse_snapshot_description("  " + description + "  \n") == ownership


@pytest.mark.parametrize(
    "description",
    [
        f"{SNAPSHOT_METADATA_MARKER} not-json",
        f"{SNAPSHOT_METADATA_MARKER} {{}}",
        f"{SNAPSHOT_METADATA_MARKER}{{\"protocol\":1}}",
        f'{SNAPSHOT_METADATA_MARKER} {{"protocol":2,"kind":"pre_update",'
        f'"job_id":"00000000-0000-0000-0000-000000000001",'
        f'"resource_id":"00000000-0000-0000-0000-000000000002",'
        f'"resource_continuity_revision":1,'
        f'"inventory_source_id":"00000000-0000-0000-0000-000000000003",'
        f'"backend_instance_id":"00000000-0000-0000-0000-000000000004"}}',
        "hubinet-ops-snapshot but no marker line at all",
    ],
)
def test_malformed_hubinet_metadata_is_rejected_not_ignored(description: str) -> None:
    with pytest.raises(SnapshotIdentityError):
        parse_snapshot_description(description)


def test_duplicate_marker_lines_are_rejected() -> None:
    ownership = build_snapshot_ownership(
        job_id=str(uuid.uuid4()),
        resource_id=str(uuid.uuid4()),
        resource_continuity_revision=1,
        inventory_source_id=str(uuid.uuid4()),
        backend_instance_id=str(uuid.uuid4()),
    )
    doubled = encode_snapshot_description(ownership)
    doubled = doubled + "\n" + doubled.splitlines()[1]
    with pytest.raises(SnapshotIdentityError):
        parse_snapshot_description(doubled)


def test_foreign_and_manual_snapshots_are_never_owned() -> None:
    assert parse_snapshot_description("nightly backup") is None
    assert parse_snapshot_description("") is None
    listing = parse_canonical_snapshot_listing(
        [
            {"name": "current", "description": "You are here!"},
            {"name": "before-migration", "description": "manual", "snaptime": 5},
        ]
    )
    assert [entry.ownership for entry in listing] == [None, None]
    assert [entry.ownership_malformed for entry in listing] == [False, False]


# ===========================================================================
# B. WRITE-AHEAD INTENT
# ===========================================================================


def test_snapshot_intent_is_committed_before_any_host_mutation_callback(
    tmp_path: Path,
) -> None:
    _, store, authority, resource, _, approval = _approved_system(tmp_path)
    job = _issue(authority, resource, approval)
    authority.record_package_update_preflight_passed(job.job_id)
    observed: list[PackageUpdateCheckpoint] = []

    class RecordingHostControl:
        def create_pre_update_snapshot(self, **kwargs):
            # By the time the host boundary is reached at all, the durable
            # write-ahead checkpoint must already be committed and visible.
            observed.append(store.package_update_job(job.job_id).checkpoint)
            raise RuntimeError("host process died mid-submission")

        def inspect_job_snapshot_state(self, **kwargs):  # pragma: no cover
            raise AssertionError("not used")

    result = PackageUpdateSnapshotOrchestrator(
        authority, RecordingHostControl()
    ).ensure_job_owned_snapshot(job.job_id)

    assert observed == [PackageUpdateCheckpoint.SNAPSHOT_MAY_HAVE_STARTED]
    assert result.outcome is SnapshotOperationOutcome.UNCERTAIN
    persisted = store.package_update_job(job.job_id)
    assert persisted.checkpoint is PackageUpdateCheckpoint.SNAPSHOT_MAY_HAVE_STARTED
    assert persisted.status is PackageUpdateJobStatus.ACTIVE
    assert persisted.snapshot_operation_id is not None
    assert persisted.snapshot_intent_recorded_at is not None
    assert persisted.snapshot_confirmed_at is None
    types = [event.event_type for event in store.list_package_update_job_events(job.job_id)]
    assert PackageUpdateEventType.SNAPSHOT_INTENT_RECORDED in types
    assert PackageUpdateEventType.SNAPSHOT_OUTCOME_UNCERTAIN in types


def test_snapshot_intent_requires_a_passed_preflight(tmp_path: Path) -> None:
    _, _, authority, resource, _, approval = _approved_system(tmp_path)
    job = _issue(authority, resource, approval)
    with pytest.raises(AuthorityConflict, match="preflight"):
        authority.record_package_update_snapshot_intent(job.job_id)


def test_repeating_snapshot_intent_never_creates_a_second_identity(
    tmp_path: Path,
) -> None:
    _, store, authority, _, job, identity, _ = _prepared(tmp_path)
    again = authority.record_package_update_snapshot_intent(job.job_id)
    assert again.snapshot_operation_id == identity.snapshot_operation_id
    assert again.snapshot_name == identity.snapshot_name
    assert (
        again.snapshot_intent_recorded_at == job.snapshot_intent_recorded_at
    )
    intents = [
        event
        for event in store.list_package_update_job_events(job.job_id)
        if event.event_type is PackageUpdateEventType.SNAPSHOT_INTENT_RECORDED
    ]
    assert len(intents) == 1


def test_no_package_mutation_capability_exists_at_the_intent_boundary(
    tmp_path: Path,
) -> None:
    _, _, authority, _, _, _, _ = _prepared(tmp_path)
    # There is no APT/package execution method anywhere on the authority in
    # this stage; the snapshot stage cannot lead into one.
    for forbidden in (
        "execute_package_update",
        "start_package_update",
        "apply_package_update",
        "install_packages",
        "delete_package_update_snapshot",
        "rollback_package_update",
    ):
        assert not hasattr(authority, forbidden), forbidden


# ===========================================================================
# C/D. PVE TASK SEMANTICS
# ===========================================================================


@pytest.mark.parametrize(
    ("payload", "terminal", "state", "succeeded"),
    [
        ({"upid": UPID, "status": "running"}, False, SnapshotTaskState.RUNNING, False),
        (
            {"upid": UPID, "status": "stopped", "exitstatus": "OK"},
            True,
            SnapshotTaskState.OK,
            True,
        ),
        (
            {"upid": UPID, "status": "stopped", "exitstatus": "WARNINGS: 3"},
            True,
            SnapshotTaskState.WARNING,
            True,
        ),
        (
            {"upid": UPID, "status": "stopped", "exitstatus": "command failed"},
            True,
            SnapshotTaskState.ERROR,
            False,
        ),
        # 'stopped' on its own is never success.
        ({"upid": UPID, "status": "stopped"}, True, SnapshotTaskState.UNKNOWN, False),
        (
            {"upid": UPID, "status": "stopped", "exitstatus": "unexpected status"},
            True,
            SnapshotTaskState.UNKNOWN,
            False,
        ),
        (
            {"upid": UPID, "status": "stopped", "exitstatus": "WARNINGS: many"},
            True,
            SnapshotTaskState.ERROR,
            False,
        ),
    ],
)
def test_pve_task_status_follows_pve_own_success_rule(
    payload, terminal, state, succeeded
) -> None:
    status = classify_task_status(payload)
    assert (status.terminal, status.state, status.succeeded) == (
        terminal,
        state,
        succeeded,
    )


@pytest.mark.parametrize(
    "payload",
    [None, {}, {"upid": UPID}, {"upid": UPID, "status": "finished"}, {"status": "stopped"}],
)
def test_malformed_pve_task_status_is_rejected(payload) -> None:
    with pytest.raises(SnapshotEvidenceError):
        classify_task_status(payload)


def test_successful_task_plus_canonical_snapshot_confirms(tmp_path: Path) -> None:
    _, store, authority, _, job, identity, ownership = _prepared(tmp_path)
    authority.record_package_update_snapshot_task(job.job_id, UPID)

    confirmed = authority.confirm_package_update_snapshot(
        job.job_id, _canonical(ownership, identity)
    )

    assert confirmed.checkpoint is PackageUpdateCheckpoint.SNAPSHOT_CONFIRMED
    assert confirmed.snapshot_confirmed_at is not None
    assert confirmed.snapshot_name == identity.snapshot_name
    assert confirmed.snapshot_task_upid == UPID
    assert confirmed.status is PackageUpdateJobStatus.ACTIVE
    types = [e.event_type for e in store.list_package_update_job_events(job.job_id)]
    assert PackageUpdateEventType.SNAPSHOT_TASK_OBSERVED in types
    assert PackageUpdateEventType.SNAPSHOT_CONFIRMED in types


def test_task_failure_with_canonical_absence_blocks_without_confirming(
    tmp_path: Path,
) -> None:
    _, store, authority, _, job, _, _ = _prepared(tmp_path)
    failed = authority.fail_package_update_snapshot(
        job.job_id,
        "PVE snapshot task terminated in a failure state",
        (_current_entry(), _foreign_entry()),
    )
    assert failed.status is PackageUpdateJobStatus.BLOCKED
    assert failed.checkpoint is PackageUpdateCheckpoint.SNAPSHOT_MAY_HAVE_STARTED
    assert failed.snapshot_confirmed_at is None
    assert [
        e.event_type for e in store.list_package_update_job_events(job.job_id)
    ][-1] is PackageUpdateEventType.SNAPSHOT_FAILED


def test_canonical_absence_alone_never_fakes_success(tmp_path: Path) -> None:
    _, _, authority, _, job, _, _ = _prepared(tmp_path)
    with pytest.raises(AuthorityConflict, match="does not contain"):
        authority.confirm_package_update_snapshot(
            job.job_id, (_current_entry(), _foreign_entry())
        )


def test_a_job_whose_snapshot_may_exist_cannot_be_declared_failed(
    tmp_path: Path,
) -> None:
    _, _, authority, _, job, identity, ownership = _prepared(tmp_path)
    # Present under our name -> uncertain, never "failed".
    with pytest.raises(AuthorityConflict, match="uncertain"):
        authority.fail_package_update_snapshot(
            job.job_id, "task failed", _canonical(ownership, identity)
        )
    # An incomplete entry under our name is equally not proof of absence.
    with pytest.raises(AuthorityConflict, match="uncertain"):
        authority.fail_package_update_snapshot(
            job.job_id,
            "task failed",
            (
                _current_entry(),
                _owned_entry(ownership, identity.snapshot_name, incomplete=True),
            ),
        )
    # And neither is an unattributable Hubinet-looking snapshot.
    with pytest.raises(AuthorityConflict, match="uncertain"):
        authority.fail_package_update_snapshot(
            job.job_id,
            "task failed",
            (
                _current_entry(),
                ObservedSnapshot(
                    name="other", description="junk", ownership_malformed=True
                ),
            ),
        )


# ===========================================================================
# E. CANONICAL CONFIRMATION
# ===========================================================================


def test_duplicate_job_owned_snapshots_fail_closed(tmp_path: Path) -> None:
    _, _, authority, _, job, identity, ownership = _prepared(tmp_path)
    with pytest.raises(AuthorityConflict, match="different name"):
        authority.confirm_package_update_snapshot(
            job.job_id,
            (
                _current_entry(),
                _owned_entry(ownership, identity.snapshot_name),
                _owned_entry(ownership, "hubinet-preupd-copyofthesamejobmetadata"),
            ),
        )


def test_exact_name_with_mismatched_metadata_fails_closed(tmp_path: Path) -> None:
    _, _, authority, _, job, identity, ownership = _prepared(tmp_path)
    impostor = replace(ownership, resource_continuity_revision=ownership.resource_continuity_revision + 1)
    with pytest.raises(AuthorityConflict, match="exact ownership metadata"):
        authority.confirm_package_update_snapshot(
            job.job_id,
            (_current_entry(), _owned_entry(impostor, identity.snapshot_name)),
        )


def test_exact_name_with_no_metadata_at_all_fails_closed(tmp_path: Path) -> None:
    _, _, authority, _, job, identity, _ = _prepared(tmp_path)
    with pytest.raises(AuthorityConflict, match="exact ownership metadata"):
        authority.confirm_package_update_snapshot(
            job.job_id,
            (
                _current_entry(),
                ObservedSnapshot(name=identity.snapshot_name, description="mine now"),
            ),
        )


def test_exact_name_with_malformed_metadata_fails_closed(tmp_path: Path) -> None:
    _, _, authority, _, job, identity, _ = _prepared(tmp_path)
    with pytest.raises(AuthorityConflict, match="malformed"):
        authority.confirm_package_update_snapshot(
            job.job_id,
            (
                _current_entry(),
                ObservedSnapshot(
                    name=identity.snapshot_name,
                    description=f"{SNAPSHOT_METADATA_MARKER} broken",
                    ownership_malformed=True,
                ),
            ),
        )


def test_incomplete_snapshot_is_never_confirmed(tmp_path: Path) -> None:
    _, _, authority, _, job, identity, ownership = _prepared(tmp_path)
    with pytest.raises(AuthorityConflict, match="incomplete"):
        authority.confirm_package_update_snapshot(
            job.job_id,
            (
                _current_entry(),
                _owned_entry(ownership, identity.snapshot_name, incomplete=True),
            ),
        )


def test_current_pseudo_entry_is_never_a_confirmable_snapshot(tmp_path: Path) -> None:
    _, _, authority, _, job, identity, ownership = _prepared(tmp_path)
    # Even carrying our exact metadata, PVE's synthetic row is not a snapshot.
    with pytest.raises(AuthorityConflict, match="does not contain"):
        authority.confirm_package_update_snapshot(
            job.job_id,
            (
                replace(
                    _owned_entry(ownership, "current"),
                    is_current_pseudo_entry=True,
                    ownership=None,
                ),
            ),
        )


def test_unattributable_hubinet_snapshot_elsewhere_fails_closed(
    tmp_path: Path,
) -> None:
    _, _, authority, _, job, identity, ownership = _prepared(tmp_path)
    with pytest.raises(AuthorityConflict, match="ambiguous"):
        authority.confirm_package_update_snapshot(
            job.job_id,
            (
                _current_entry(),
                _owned_entry(ownership, identity.snapshot_name),
                ObservedSnapshot(
                    name="hubinet-preupd-somethingelseentirelyxx",
                    description=f"{SNAPSHOT_METADATA_MARKER} garbage",
                    ownership_malformed=True,
                ),
            ),
        )


def test_confirmation_is_idempotent_and_write_once(tmp_path: Path) -> None:
    _, store, authority, _, job, identity, ownership = _prepared(tmp_path)
    observed = _canonical(ownership, identity)
    first = authority.confirm_package_update_snapshot(job.job_id, observed)
    second = authority.confirm_package_update_snapshot(job.job_id, observed)
    assert first == second
    confirmations = [
        e
        for e in store.list_package_update_job_events(job.job_id)
        if e.event_type is PackageUpdateEventType.SNAPSHOT_CONFIRMED
    ]
    assert len(confirmations) == 1


# ===========================================================================
# F. CRASH / RETRY
# ===========================================================================


class FakeHostControl:
    """A dark host boundary that records every call it receives."""

    def __init__(self, *results: HostSnapshotResult) -> None:
        self._results = list(results)
        self.create_calls: list[dict] = []
        self.inspect_calls: list[dict] = []

    def create_pre_update_snapshot(self, **kwargs) -> HostSnapshotResult:
        self.create_calls.append(kwargs)
        return self._results.pop(0)

    def inspect_job_snapshot_state(self, **kwargs) -> HostSnapshotResult:
        self.inspect_calls.append(kwargs)
        return self._results.pop(0)


def test_retry_after_uncertainty_reattaches_and_never_confirms_blindly(
    tmp_path: Path,
) -> None:
    _, store, authority, resource, _, approval = _approved_system(tmp_path)
    job = _issue(authority, resource, approval)
    identity = authority.package_update_snapshot_identity(job.job_id)
    ownership = authority.package_update_snapshot_ownership(job.job_id)

    uncertain = HostSnapshotResult(
        outcome=SnapshotOperationOutcome.UNCERTAIN,
        snapshot_operation_id=identity.snapshot_operation_id,
        task_upid=UPID,
        reason="task identity known but still running at the polling bound",
    )
    host = FakeHostControl(uncertain, uncertain)
    orchestrator = PackageUpdateSnapshotOrchestrator(authority, host)

    first = orchestrator.ensure_job_owned_snapshot(job.job_id)
    second = orchestrator.ensure_job_owned_snapshot(job.job_id)

    assert first.outcome is second.outcome is SnapshotOperationOutcome.UNCERTAIN
    # Both attempts addressed the identical deterministic operation identity,
    # which is what lets the host journal reattach instead of resubmitting.
    assert {call["snapshot_operation_id"] for call in host.create_calls} == {
        identity.snapshot_operation_id
    }
    assert {call["snapshot_name"] for call in host.create_calls} == {
        identity.snapshot_name
    }
    persisted = store.package_update_job(job.job_id)
    assert persisted.status is PackageUpdateJobStatus.ACTIVE
    assert persisted.checkpoint is PackageUpdateCheckpoint.SNAPSHOT_MAY_HAVE_STARTED
    assert persisted.snapshot_task_upid == UPID
    assert persisted.snapshot_confirmed_at is None
    assert ownership.job_id == job.job_id


def test_a_conflicting_task_identity_is_refused_and_stays_uncertain(
    tmp_path: Path,
) -> None:
    _, store, authority, _, job, identity, _ = _prepared(tmp_path)
    authority.record_package_update_snapshot_task(job.job_id, UPID)
    # Idempotent for the same task...
    assert (
        authority.record_package_update_snapshot_task(job.job_id, UPID).snapshot_task_upid
        == UPID
    )
    # ...and a conflict for a different one, because two tasks would mean two
    # submissions.
    with pytest.raises(AuthorityConflict, match="different PVE"):
        authority.record_package_update_snapshot_task(job.job_id, OTHER_UPID)
    assert store.package_update_job(job.job_id).snapshot_task_upid == UPID


def test_orchestrator_treats_a_conflicting_task_as_uncertain(tmp_path: Path) -> None:
    _, store, authority, resource, _, approval = _approved_system(tmp_path)
    job = _issue(authority, resource, approval)
    identity = authority.package_update_snapshot_identity(job.job_id)
    authority.record_package_update_preflight_passed(job.job_id)
    authority.record_package_update_snapshot_intent(job.job_id)
    authority.record_package_update_snapshot_task(job.job_id, UPID)

    host = FakeHostControl(
        HostSnapshotResult(
            outcome=SnapshotOperationOutcome.COMPLETED,
            snapshot_operation_id=identity.snapshot_operation_id,
            task_upid=OTHER_UPID,
            task=classify_task_status(
                {"upid": OTHER_UPID, "status": "stopped", "exitstatus": "OK"}
            ),
            snapshots=(),
        )
    )
    result = PackageUpdateSnapshotOrchestrator(
        authority, host
    ).ensure_job_owned_snapshot(job.job_id)

    assert result.outcome is SnapshotOperationOutcome.UNCERTAIN
    assert store.package_update_job(job.job_id).snapshot_task_upid == UPID
    assert store.package_update_job(job.job_id).snapshot_confirmed_at is None


def test_successful_task_without_canonical_listing_stays_uncertain(
    tmp_path: Path,
) -> None:
    _, store, authority, resource, _, approval = _approved_system(tmp_path)
    job = _issue(authority, resource, approval)
    identity = authority.package_update_snapshot_identity(job.job_id)
    host = FakeHostControl(
        HostSnapshotResult(
            outcome=SnapshotOperationOutcome.COMPLETED,
            snapshot_operation_id=identity.snapshot_operation_id,
            task_upid=UPID,
            task=classify_task_status(
                {"upid": UPID, "status": "stopped", "exitstatus": "OK"}
            ),
            snapshots=None,
        )
    )
    result = PackageUpdateSnapshotOrchestrator(
        authority, host
    ).ensure_job_owned_snapshot(job.job_id)
    assert result.outcome is SnapshotOperationOutcome.UNCERTAIN
    assert store.package_update_job(job.job_id).snapshot_confirmed_at is None


def test_a_host_answer_for_another_operation_is_uncertain(tmp_path: Path) -> None:
    _, store, authority, resource, _, approval = _approved_system(tmp_path)
    job = _issue(authority, resource, approval)
    host = FakeHostControl(
        HostSnapshotResult(
            outcome=SnapshotOperationOutcome.COMPLETED,
            snapshot_operation_id=str(uuid.uuid4()),
        )
    )
    result = PackageUpdateSnapshotOrchestrator(
        authority, host
    ).ensure_job_owned_snapshot(job.job_id)
    assert result.outcome is SnapshotOperationOutcome.UNCERTAIN
    assert store.package_update_job(job.job_id).snapshot_confirmed_at is None


def test_orchestrator_confirms_only_on_complete_strict_evidence(
    tmp_path: Path,
) -> None:
    _, store, authority, resource, _, approval = _approved_system(tmp_path)
    job = _issue(authority, resource, approval)
    identity = authority.package_update_snapshot_identity(job.job_id)
    ownership = authority.package_update_snapshot_ownership(job.job_id)
    host = FakeHostControl(
        HostSnapshotResult(
            outcome=SnapshotOperationOutcome.COMPLETED,
            snapshot_operation_id=identity.snapshot_operation_id,
            task_upid=UPID,
            task=classify_task_status(
                {"upid": UPID, "status": "stopped", "exitstatus": "OK"}
            ),
            snapshots=_canonical(ownership, identity),
        )
    )
    result = PackageUpdateSnapshotOrchestrator(
        authority, host
    ).ensure_job_owned_snapshot(job.job_id)

    assert result.outcome is SnapshotOperationOutcome.COMPLETED
    assert result.job.checkpoint is PackageUpdateCheckpoint.SNAPSHOT_CONFIRMED
    assert store.package_update_job(job.job_id).snapshot_name == identity.snapshot_name


# ===========================================================================
# G. GLOBAL SINGLE-FLIGHT
# ===========================================================================


def test_an_uncertain_snapshot_operation_keeps_owning_the_global_slot(
    tmp_path: Path,
) -> None:
    _, store, authority, resource, job, _, _ = _prepared(tmp_path)
    other_resource, _, other_approval = _add_approved_resource(store, authority)
    authority.record_package_update_snapshot_uncertain(
        job.job_id, "task outcome unknown"
    )

    with pytest.raises(AuthorityConflict, match="global slot"):
        _issue(authority, other_resource, other_approval)

    # A restart must not change that.
    assert authority.recover_interrupted_package_update_jobs() == ()
    with pytest.raises(AuthorityConflict, match="global slot"):
        _issue(authority, other_resource, other_approval)
    assert store.package_update_job(job.job_id).status is PackageUpdateJobStatus.ACTIVE


# ===========================================================================
# H. STARTUP RECOVERY
# ===========================================================================


def test_startup_interrupts_issued_and_preflight_jobs(tmp_path: Path) -> None:
    _, store, authority, resource, _, approval = _approved_system(tmp_path)
    issued = _issue(authority, resource, approval)
    assert authority.recover_interrupted_package_update_jobs() == (issued.job_id,)
    assert store.package_update_job(issued.job_id).status is (
        PackageUpdateJobStatus.INTERRUPTED
    )

    second = _issue(authority, resource, approval)
    authority.record_package_update_preflight_passed(second.job_id)
    assert authority.recover_interrupted_package_update_jobs() == (second.job_id,)
    assert store.package_update_job(second.job_id).status is (
        PackageUpdateJobStatus.INTERRUPTED
    )


def test_startup_never_terminalizes_or_replays_an_uncertain_snapshot_operation(
    tmp_path: Path,
) -> None:
    _, store, authority, _, job, identity, _ = _prepared(tmp_path)
    authority.record_package_update_snapshot_task(job.job_id, UPID)
    before = store.package_update_job(job.job_id)

    assert authority.recover_interrupted_package_update_jobs() == ()

    after = store.package_update_job(job.job_id)
    assert after == before
    assert after.status is PackageUpdateJobStatus.ACTIVE
    assert after.checkpoint is PackageUpdateCheckpoint.SNAPSHOT_MAY_HAVE_STARTED
    assert after.snapshot_operation_id == identity.snapshot_operation_id
    assert after.snapshot_task_upid == UPID


def test_startup_may_interrupt_a_confirmed_snapshot_job_and_retains_the_snapshot(
    tmp_path: Path,
) -> None:
    _, store, authority, _, job, identity, ownership = _prepared(tmp_path)
    authority.confirm_package_update_snapshot(
        job.job_id, _canonical(ownership, identity)
    )

    assert authority.recover_interrupted_package_update_jobs() == (job.job_id,)

    recovered = store.package_update_job(job.job_id)
    assert recovered.status is PackageUpdateJobStatus.INTERRUPTED
    # Interruption is pre-package-mutation and the snapshot is retained: its
    # identity and confirmation stay on the durable record, and nothing in
    # this stage deletes it.
    assert recovered.snapshot_name == identity.snapshot_name
    assert recovered.snapshot_confirmed_at is not None
    assert recovered.checkpoint is PackageUpdateCheckpoint.SNAPSHOT_CONFIRMED


def test_repeated_startup_recovery_is_idempotent(tmp_path: Path) -> None:
    _, store, authority, _, job, _, _ = _prepared(tmp_path)
    assert authority.recover_interrupted_package_update_jobs() == ()
    assert authority.recover_interrupted_package_update_jobs() == ()
    events = store.list_package_update_job_events(job.job_id)
    assert not [
        e
        for e in events
        if e.event_type is PackageUpdateEventType.RESTART_INTERRUPTED
    ]

    _, store2, authority2, resource2, _, approval2 = _approved_system(
        tmp_path / "second"
    )
    issued = _issue(authority2, resource2, approval2)
    assert authority2.recover_interrupted_package_update_jobs() == (issued.job_id,)
    assert authority2.recover_interrupted_package_update_jobs() == ()
    assert (
        len(
            [
                e
                for e in store2.list_package_update_job_events(issued.job_id)
                if e.event_type is PackageUpdateEventType.RESTART_INTERRUPTED
            ]
        )
        == 1
    )


# ===========================================================================
# I. ROLLBACK TARGET AUTHORITY
# ===========================================================================


def test_rollback_target_is_only_this_jobs_own_confirmed_snapshot(
    tmp_path: Path,
) -> None:
    _, _, authority, _, job, identity, ownership = _prepared(tmp_path)
    authority.confirm_package_update_snapshot(
        job.job_id, _canonical(ownership, identity)
    )

    target = authority.select_package_update_rollback_target(
        job.job_id, _canonical(ownership, identity)
    )

    assert target.job_id == job.job_id
    assert target.snapshot_name == identity.snapshot_name
    assert target.snapshot_operation_id == identity.snapshot_operation_id
    assert target.expected_vmid == job.expected_vmid
    assert target.expected_node_name == job.expected_node_name


def test_a_job_without_a_confirmed_snapshot_has_no_rollback_target(
    tmp_path: Path,
) -> None:
    _, _, authority, _, job, identity, ownership = _prepared(tmp_path)
    with pytest.raises(AuthorityConflict, match="no confirmed"):
        authority.select_package_update_rollback_target(
            job.job_id, _canonical(ownership, identity)
        )


def test_no_foreign_or_newest_snapshot_can_become_a_rollback_target(
    tmp_path: Path,
) -> None:
    _, _, authority, _, job, identity, ownership = _prepared(tmp_path)
    authority.confirm_package_update_snapshot(
        job.job_id, _canonical(ownership, identity)
    )
    # A newer foreign snapshot, and a newer Hubinet-looking one, must not be
    # selectable, and their presence must not disturb the real answer.
    listing = (
        _current_entry(),
        _foreign_entry("newest-manual"),
        _owned_entry(ownership, identity.snapshot_name),
    )
    assert (
        authority.select_package_update_rollback_target(
            job.job_id, listing
        ).snapshot_name
        == identity.snapshot_name
    )
    # If this job's own snapshot is gone, nothing else substitutes for it.
    with pytest.raises(AuthorityConflict, match="does not contain"):
        authority.select_package_update_rollback_target(
            job.job_id, (_current_entry(), _foreign_entry("newest-manual"))
        )


def test_another_jobs_snapshot_is_never_this_jobs_rollback_target(
    tmp_path: Path,
) -> None:
    _, store, authority, resource, first, first_identity, first_ownership = _prepared(
        tmp_path
    )
    authority.confirm_package_update_snapshot(
        first.job_id, _canonical(first_ownership, first_identity)
    )
    # Interrupting a confirmed-snapshot job is safe and retains its snapshot,
    # which frees the global slot for a genuinely separate second job.
    assert authority.recover_interrupted_package_update_jobs() == (first.job_id,)

    approval = store.package_plan_approval(resource.resource_id)
    second = _issue(authority, resource, approval)
    second_identity = authority.package_update_snapshot_identity(second.job_id)
    second_ownership = authority.package_update_snapshot_ownership(second.job_id)
    authority.record_package_update_preflight_passed(second.job_id)
    authority.record_package_update_snapshot_intent(second.job_id)

    # Both jobs' snapshots are on the same guest at the same time.
    both = (
        _current_entry(),
        _owned_entry(first_ownership, first_identity.snapshot_name),
        _owned_entry(second_ownership, second_identity.snapshot_name),
    )
    authority.confirm_package_update_snapshot(second.job_id, both)

    target = authority.select_package_update_rollback_target(second.job_id, both)
    assert target.snapshot_name == second_identity.snapshot_name
    assert target.snapshot_name != first_identity.snapshot_name

    # The older job's snapshot is present and newer-looking candidates exist,
    # but the terminal first job has no rollback authority at all any more.
    with pytest.raises(AuthorityConflict, match="terminal"):
        authority.select_package_update_rollback_target(first.job_id, both)

    # And if the second job's own snapshot vanishes, the first job's snapshot
    # is never substituted for it.
    with pytest.raises(AuthorityConflict, match="does not contain"):
        authority.select_package_update_rollback_target(
            second.job_id,
            (
                _current_entry(),
                _owned_entry(first_ownership, first_identity.snapshot_name),
            ),
        )


def test_a_snapshot_carrying_another_jobs_metadata_under_this_name_fails_closed(
    tmp_path: Path,
) -> None:
    _, _, authority, _, job, identity, ownership = _prepared(tmp_path)
    authority.confirm_package_update_snapshot(
        job.job_id, _canonical(ownership, identity)
    )
    foreign_job = replace(ownership, job_id=str(uuid.uuid4()))
    with pytest.raises(AuthorityConflict, match="exact ownership metadata"):
        authority.select_package_update_rollback_target(
            job.job_id,
            (_current_entry(), _owned_entry(foreign_job, identity.snapshot_name)),
        )


def test_a_reused_vmid_incarnation_cannot_inherit_rollback_authority(
    tmp_path: Path,
) -> None:
    _, _, authority, _, job, identity, ownership = _prepared(tmp_path)
    authority.confirm_package_update_snapshot(
        job.job_id, _canonical(ownership, identity)
    )
    # Same VMID, same snapshot name, but a different resource incarnation.
    reincarnated = replace(ownership, resource_id=str(uuid.uuid4()))
    with pytest.raises(AuthorityConflict, match="exact ownership metadata"):
        authority.select_package_update_rollback_target(
            job.job_id,
            (_current_entry(), _owned_entry(reincarnated, identity.snapshot_name)),
        )


def test_a_continuity_revision_mismatch_rejects_the_rollback_target(
    tmp_path: Path,
) -> None:
    _, _, authority, _, job, identity, ownership = _prepared(tmp_path)
    authority.confirm_package_update_snapshot(
        job.job_id, _canonical(ownership, identity)
    )
    drifted = replace(
        ownership,
        resource_continuity_revision=ownership.resource_continuity_revision + 1,
    )
    with pytest.raises(AuthorityConflict, match="exact ownership metadata"):
        authority.select_package_update_rollback_target(
            job.job_id, (_current_entry(), _owned_entry(drifted, identity.snapshot_name))
        )


def test_malformed_metadata_under_the_job_snapshot_name_rejects_rollback(
    tmp_path: Path,
) -> None:
    _, _, authority, _, job, identity, ownership = _prepared(tmp_path)
    authority.confirm_package_update_snapshot(
        job.job_id, _canonical(ownership, identity)
    )
    with pytest.raises(AuthorityConflict, match="malformed"):
        authority.select_package_update_rollback_target(
            job.job_id,
            (
                _current_entry(),
                ObservedSnapshot(
                    name=identity.snapshot_name,
                    description=f"{SNAPSHOT_METADATA_MARKER} tampered",
                    ownership_malformed=True,
                ),
            ),
        )


def test_the_current_pseudo_entry_is_never_a_rollback_target(tmp_path: Path) -> None:
    _, _, authority, _, job, identity, ownership = _prepared(tmp_path)
    authority.confirm_package_update_snapshot(
        job.job_id, _canonical(ownership, identity)
    )
    with pytest.raises(AuthorityConflict, match="pseudo-entry"):
        authority.select_package_update_rollback_target(
            job.job_id,
            (
                ObservedSnapshot(
                    name=identity.snapshot_name,
                    description="You are here!",
                    is_current_pseudo_entry=True,
                ),
            ),
        )


def test_no_caller_supplied_snapshot_name_is_accepted_for_rollback() -> None:
    # The selector's signature has no place to pass a snapshot name at all:
    # the target comes from durable job authority, never from the caller.
    import inspect

    parameters = inspect.signature(
        InventoryAuthority.select_package_update_rollback_target
    ).parameters
    assert set(parameters) == {"self", "job_id", "observed"}


# ===========================================================================
# Canonical listing parser
# ===========================================================================


def test_canonical_listing_preserves_pseudo_entry_and_incompleteness() -> None:
    ownership = build_snapshot_ownership(
        job_id=str(uuid.uuid4()),
        resource_id=str(uuid.uuid4()),
        resource_continuity_revision=2,
        inventory_source_id=str(uuid.uuid4()),
        backend_instance_id=str(uuid.uuid4()),
    )
    listing = parse_canonical_snapshot_listing(
        [
            {"name": "current", "description": "You are here!", "running": 1},
            {
                "name": "hubinet-preupd-aaaaaaaaaaaaaaaaaaaaaaaa",
                "description": encode_snapshot_description(ownership) + "\n",
                # PVE reports snapstate for a snapshot still being made, even
                # though its declared return schema omits the field.
                "snapstate": "prepare",
                "snaptime": 0,
            },
            {"name": "manual", "description": "operator", "snaptime": 42, "parent": "current"},
        ]
    )
    assert listing[0].is_current_pseudo_entry and listing[0].snaptime is None
    assert listing[1].incomplete and listing[1].ownership == ownership
    # PVE emits snaptime 0 when it does not know one; that is not a timestamp.
    assert listing[1].snaptime is None
    assert listing[2].snaptime == 42 and listing[2].parent == "current"


def test_canonical_listing_marks_unparseable_hubinet_metadata_malformed() -> None:
    listing = parse_canonical_snapshot_listing(
        [{"name": "x", "description": f"{SNAPSHOT_METADATA_MARKER} nope"}]
    )
    assert listing[0].ownership is None
    assert listing[0].ownership_malformed is True


@pytest.mark.parametrize(
    "payload",
    [
        None,
        "not a list",
        [{"description": "no name"}],
        [{"name": 1, "description": ""}],
        [{"name": "a", "description": 5}],
        [{"name": "a", "description": "", "snaptime": "soon"}],
        [{"name": "a", "description": ""}, {"name": "a", "description": ""}],
    ],
)
def test_malformed_canonical_listings_are_rejected(payload) -> None:
    with pytest.raises(SnapshotEvidenceError):
        parse_canonical_snapshot_listing(payload)


# ===========================================================================
# SQL / state machine invariants (adversarial direct SQL)
# ===========================================================================


def _assert_sql_rejected(store, statement: str, parameters: tuple) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        with store._transaction() as connection:
            connection.execute(statement, parameters)


def test_snapshot_identity_cannot_change_once_the_operation_may_have_started(
    tmp_path: Path,
) -> None:
    _, store, _, _, job, identity, _ = _prepared(tmp_path)
    _assert_sql_rejected(
        store,
        "UPDATE package_update_jobs SET snapshot_name=? WHERE job_id=?",
        ("hubinet-preupd-ffffffffffffffffffffffff", job.job_id),
    )
    _assert_sql_rejected(
        store,
        "UPDATE package_update_jobs SET snapshot_operation_id=? WHERE job_id=?",
        (str(uuid.uuid4()), job.job_id),
    )
    _assert_sql_rejected(
        store,
        "UPDATE package_update_jobs SET snapshot_intent_recorded_at=? WHERE job_id=?",
        ("2030-01-01T00:00:00+00:00", job.job_id),
    )
    assert store.package_update_job(job.job_id).snapshot_name == identity.snapshot_name


def test_a_job_cannot_acquire_a_second_snapshot_operation_identity(
    tmp_path: Path,
) -> None:
    _, store, authority, resource, job, identity, _ = _prepared(tmp_path)
    _assert_sql_rejected(
        store,
        "UPDATE package_update_jobs SET snapshot_operation_id=?, snapshot_name=? "
        "WHERE job_id=?",
        (str(uuid.uuid4()), "hubinet-preupd-000000000000000000000000", job.job_id),
    )
    assert (
        store.package_update_job(job.job_id).snapshot_operation_id
        == identity.snapshot_operation_id
    )

    # Two different jobs can never share one snapshot operation identity.
    authority.fail_package_update_snapshot(
        job.job_id, "task failed", (_current_entry(),)
    )
    second = _issue(
        authority, resource, store.package_plan_approval(resource.resource_id)
    )
    _assert_sql_rejected(
        store,
        "UPDATE package_update_jobs SET checkpoint='snapshot_may_have_started', "
        "snapshot_operation_id=?, snapshot_name=?, snapshot_intent_recorded_at=? "
        "WHERE job_id=?",
        (
            identity.snapshot_operation_id,
            identity.snapshot_name,
            "2030-01-01T00:00:00+00:00",
            second.job_id,
        ),
    )


def test_snapshot_confirmed_requires_a_name_and_a_confirmed_timestamp(
    tmp_path: Path,
) -> None:
    _, store, authority, resource, _, approval = _approved_system(tmp_path)
    job = _issue(authority, resource, approval)
    _assert_sql_rejected(
        store,
        "UPDATE package_update_jobs SET checkpoint='snapshot_confirmed' WHERE job_id=?",
        (job.job_id,),
    )
    _assert_sql_rejected(
        store,
        "UPDATE package_update_jobs SET checkpoint='snapshot_confirmed', "
        "snapshot_confirmed_at=? WHERE job_id=?",
        ("2030-01-01T00:00:00+00:00", job.job_id),
    )


def test_mutation_can_never_precede_a_confirmed_snapshot(tmp_path: Path) -> None:
    _, store, _, _, job, _, _ = _prepared(tmp_path)
    for checkpoint in (
        "mutation_may_have_started",
        "mutation_completed",
        "health_started",
        "rollback_may_have_started",
        "rollback_completed",
    ):
        _assert_sql_rejected(
            store,
            "UPDATE package_update_jobs SET checkpoint=? WHERE job_id=?",
            (checkpoint, job.job_id),
        )
    assert (
        store.package_update_job(job.job_id).checkpoint
        is PackageUpdateCheckpoint.SNAPSHOT_MAY_HAVE_STARTED
    )


def test_the_checkpoint_can_never_move_backwards(tmp_path: Path) -> None:
    _, store, authority, _, job, identity, ownership = _prepared(tmp_path)
    _assert_sql_rejected(
        store,
        "UPDATE package_update_jobs SET checkpoint='preflight_passed' WHERE job_id=?",
        (job.job_id,),
    )
    authority.confirm_package_update_snapshot(
        job.job_id, _canonical(ownership, identity)
    )
    _assert_sql_rejected(
        store,
        "UPDATE package_update_jobs SET checkpoint='snapshot_may_have_started' "
        "WHERE job_id=?",
        (job.job_id,),
    )


def test_a_snapshot_task_cannot_be_rewritten_or_forged_without_an_operation(
    tmp_path: Path,
) -> None:
    _, store, authority, resource, _, approval = _approved_system(tmp_path)
    job = _issue(authority, resource, approval)
    _assert_sql_rejected(
        store,
        "UPDATE package_update_jobs SET snapshot_task_upid=? WHERE job_id=?",
        (UPID, job.job_id),
    )
    authority.record_package_update_preflight_passed(job.job_id)
    authority.record_package_update_snapshot_intent(job.job_id)
    authority.record_package_update_snapshot_task(job.job_id, UPID)
    _assert_sql_rejected(
        store,
        "UPDATE package_update_jobs SET snapshot_task_upid=? WHERE job_id=?",
        (OTHER_UPID, job.job_id),
    )


def test_a_snapshot_confirmation_timestamp_cannot_be_rewritten(
    tmp_path: Path,
) -> None:
    _, store, authority, _, job, identity, ownership = _prepared(tmp_path)
    authority.confirm_package_update_snapshot(
        job.job_id, _canonical(ownership, identity)
    )
    _assert_sql_rejected(
        store,
        "UPDATE package_update_jobs SET snapshot_confirmed_at=? WHERE job_id=?",
        ("2030-01-01T00:00:00+00:00", job.job_id),
    )


def test_the_schema_rejects_a_pve_invalid_snapshot_name(tmp_path: Path) -> None:
    _, store, authority, resource, _, approval = _approved_system(tmp_path)
    job = _issue(authority, resource, approval)
    for invalid in ("current", "vzdump", "9leading", "has.dot", "x", "has space"):
        _assert_sql_rejected(
            store,
            "UPDATE package_update_jobs SET snapshot_name=? WHERE job_id=?",
            (invalid, job.job_id),
        )


def test_a_terminal_job_can_never_be_reactivated_through_snapshot_fields(
    tmp_path: Path,
) -> None:
    _, store, authority, _, job, _, _ = _prepared(tmp_path)
    authority.fail_package_update_snapshot(
        job.job_id, "task failed", (_current_entry(),)
    )
    _assert_sql_rejected(
        store,
        "UPDATE package_update_jobs SET status='active', terminalized_at=NULL, "
        "terminal_reason=NULL WHERE job_id=?",
        (job.job_id,),
    )
    for method, arguments in (
        ("record_package_update_snapshot_task", (job.job_id, UPID)),
        ("record_package_update_snapshot_uncertain", (job.job_id, "late")),
    ):
        with pytest.raises(AuthorityConflict, match="terminal"):
            getattr(authority, method)(*arguments)


def test_a_persisted_identity_that_contradicts_its_derivation_is_an_invariant_error(
    tmp_path: Path,
) -> None:
    _, store, authority, resource, _, approval = _approved_system(tmp_path)
    job = _issue(authority, resource, approval)
    with store._transaction() as connection:
        connection.execute(
            "UPDATE package_update_jobs SET snapshot_name=? WHERE job_id=?",
            ("hubinet-preupd-tamperedbutwellformedxx", job.job_id),
        )
    with pytest.raises(AuthorityInvariantError, match="deterministic derivation"):
        authority.package_update_snapshot_identity(job.job_id)


# ===========================================================================
# L. SCHEMA VERSION
# ===========================================================================


def test_a_fresh_database_initializes_at_schema_v10(tmp_path: Path) -> None:
    from app.inventory.store import AUTHORITY_SCHEMA_MARKER, AUTHORITY_SCHEMA_VERSION

    assert AUTHORITY_SCHEMA_VERSION == 10
    store = InventoryAuthorityStore(tmp_path / "authority.db")
    with sqlite3.connect(tmp_path / "authority.db") as connection:
        marker, version = connection.execute(
            "SELECT marker, schema_version FROM authority_schema"
        ).fetchone()
        user_version = connection.execute("PRAGMA user_version").fetchone()[0]
    assert (marker, version, user_version) == (
        AUTHORITY_SCHEMA_MARKER,
        10,
        10,
    )
    assert store.list_package_update_jobs() == ()


def test_a_v9_database_is_rejected_and_never_migrated_in_place(
    tmp_path: Path,
) -> None:
    from app.inventory import AuthorityDatabaseRejected

    # A v10 database will not even accept the older version number: the
    # marker table's own CHECK pins it. So a genuine v9 database has to be
    # built by hand, exactly as a pre-release install would carry one.
    path = tmp_path / "authority.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE authority_schema ("
            "singleton INTEGER PRIMARY KEY CHECK(singleton = 1), "
            "marker TEXT NOT NULL, schema_version INTEGER NOT NULL)"
        )
        connection.execute(
            "INSERT INTO authority_schema(singleton, marker, schema_version) "
            "VALUES(1, 'hubinet_ops_0_5_authority', 9)"
        )
        connection.execute("PRAGMA user_version=9")

    # There is no v9 -> v10 migrator: the pre-release contract is an explicit,
    # backed-up authority reset through the product updater.
    with pytest.raises(AuthorityDatabaseRejected):
        InventoryAuthorityStore(path)
    # The rejected database is left exactly as it was; nothing is upgraded.
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT schema_version FROM authority_schema"
        ).fetchone()[0] == 9


def test_no_authority_schema_migrator_exists(tmp_path: Path) -> None:
    from app.inventory import store as store_module

    source = Path(store_module.__file__).read_text(encoding="utf-8")
    for forbidden in ("ALTER TABLE", "migrate_schema", "_MIGRATIONS", "upgrade_schema"):
        assert forbidden not in source, forbidden


def test_the_sql_checkpoint_order_matches_the_python_one(tmp_path: Path) -> None:
    """The schema's notion of checkpoint order must not drift from the code's."""

    import re

    from app.inventory.models import CHECKPOINT_ORDER
    from app.inventory.store import _checkpoint_rank_sql

    rendered = _checkpoint_rank_sql("checkpoint")
    pairs = re.findall(r"WHEN '([a-z_]+)' THEN (\d+)", rendered)
    assert [name for name, _ in pairs] == [c.value for c in CHECKPOINT_ORDER]
    assert [int(rank) for _, rank in pairs] == list(
        range(1, len(CHECKPOINT_ORDER) + 1)
    )

    # And every checkpoint the enum allows is also allowed by the schema's
    # own CHECK, so no legal state is unrepresentable.
    store = InventoryAuthorityStore(tmp_path / "authority.db")
    with sqlite3.connect(tmp_path / "authority.db") as connection:
        ddl = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='package_update_jobs'"
        ).fetchone()[0]
    for checkpoint in CHECKPOINT_ORDER:
        assert f"'{checkpoint.value}'" in ddl, checkpoint
    assert store.list_package_update_jobs() == ()


# ===========================================================================
# End-to-end across the real dark boundary
#
# Wires the real orchestrator to the real SSH host-control client to the real
# forced-command helper, with only the SSH process and `pvesh` faked. This is
# what proves the request/response contract actually agrees across the
# privilege boundary rather than only within each side's own tests.
# ===========================================================================


def _dark_channel(fake_pve, journal):
    """A host-control client whose SSH process runs the real helper."""

    import json as _json

    from app.package_scan_host_control import BoundedProcessResult
    from app.package_update_snapshot_host_control import (
        SshPackageUpdateSnapshotHostControl,
    )
    from tests.test_package_snapshot_helper import helper as snapshot_helper

    def runner(argv, input_bytes, timeout, max_output):
        assert argv[0] == "ssh"
        # The helper only ever sees the bounded JSON request on stdin; there
        # is no remote command text in the argv at all.
        assert not any(item.startswith("pvesh") for item in argv)
        response = snapshot_helper.handle_request(
            _json.loads(input_bytes.decode("utf-8")),
            runner=fake_pve,
            journal=journal,
        )
        payload = _json.dumps(response, ensure_ascii=True, separators=(",", ":"))
        return BoundedProcessResult(
            0 if response.get("ok") else 1, payload.encode("utf-8"), b""
        )

    return SshPackageUpdateSnapshotHostControl(
        host="pve.example.internal",
        port=22,
        user="hubinet-snapshot",
        private_key_path=Path("/etc/hubinet-ops/snapshot-key"),
        known_hosts_path=Path("/etc/hubinet-ops/known_hosts"),
        timeout_seconds=60,
        max_result_bytes=1024 * 1024,
        runner=runner,
    )


def _dark_system(tmp_path: Path):
    from tests.test_package_snapshot_helper import FakePve, helper as snapshot_helper

    _, store, authority, resource, _, approval = _approved_system(tmp_path)
    job = _issue(authority, resource, approval)
    identity = authority.package_update_snapshot_identity(job.job_id)
    ownership = authority.package_update_snapshot_ownership(job.job_id)
    upid = (
        f"UPID:{job.expected_node_name}:0000ABCD:000DC5EA:57500527:"
        f"vzsnapshot:{job.expected_vmid}:root@pam:"
    )
    # A faithful `pvesh` fake for THIS job's own node and VMID.
    pve = FakePve(
        snapshots=[{"name": "current", "description": "You are here!"}],
        task_sequence=[{"upid": upid, "status": "stopped", "exitstatus": "OK"}],
        submit_upid=upid,
        vmid=job.expected_vmid,
        node=job.expected_node_name,
    )
    journal = snapshot_helper.OperationJournal(tmp_path / "host-journal")
    return store, authority, job, identity, ownership, pve, journal, upid


def test_the_whole_dark_chain_creates_and_confirms_exactly_one_snapshot(
    tmp_path: Path,
) -> None:
    from tests.test_package_snapshot_helper import helper as snapshot_helper

    store, authority, job, identity, ownership, pve, journal, upid = _dark_system(
        tmp_path
    )
    pve.on_submit = lambda p: p.snapshots.append(
        {
            "name": identity.snapshot_name,
            "description": snapshot_helper.build_snapshot_description(
                {
                    "job_id": ownership.job_id,
                    "resource_id": ownership.resource_id,
                    "resource_continuity_revision": (
                        ownership.resource_continuity_revision
                    ),
                    "inventory_source_id": ownership.inventory_source_id,
                    "backend_instance_id": ownership.backend_instance_id,
                }
            )
            + "\n",
            "snaptime": 1_700_000_000,
        }
    )
    orchestrator = PackageUpdateSnapshotOrchestrator(
        authority, _dark_channel(pve, journal)
    )

    first = orchestrator.ensure_job_owned_snapshot(job.job_id)

    assert first.outcome is SnapshotOperationOutcome.COMPLETED
    assert first.job.checkpoint is PackageUpdateCheckpoint.SNAPSHOT_CONFIRMED
    assert first.job.snapshot_name == identity.snapshot_name
    assert first.job.snapshot_task_upid == upid
    assert pve.submissions == 1

    # Re-running the whole stage submits nothing further and stays confirmed.
    second = orchestrator.ensure_job_owned_snapshot(job.job_id)
    assert second.outcome is SnapshotOperationOutcome.COMPLETED
    assert pve.submissions == 1

    # And the rollback target resolves to exactly that snapshot.
    listing = parse_canonical_snapshot_listing(
        snapshot_helper.list_snapshots(pve, job.expected_vmid, job.expected_node_name)
    )
    target = orchestrator.select_rollback_target(job.job_id, listing)
    assert target.snapshot_name == identity.snapshot_name


def test_a_crash_between_the_write_ahead_intent_and_the_host_never_resubmits(
    tmp_path: Path,
) -> None:
    """The exact seam this whole stage exists for.

    The host accepts the mutation, then the caller dies before any answer is
    recorded. A later attempt must reattach to the same operation and must
    never issue a second snapshot request.
    """

    from tests.test_package_snapshot_helper import helper as snapshot_helper

    store, authority, job, identity, ownership, pve, journal, upid = _dark_system(
        tmp_path
    )

    def die_after_pve_accepts(p):
        p.snapshots.append(
            {
                "name": identity.snapshot_name,
                "description": snapshot_helper.build_snapshot_description(
                    {
                        "job_id": ownership.job_id,
                        "resource_id": ownership.resource_id,
                        "resource_continuity_revision": (
                            ownership.resource_continuity_revision
                        ),
                        "inventory_source_id": ownership.inventory_source_id,
                        "backend_instance_id": ownership.backend_instance_id,
                    }
                )
                + "\n",
                "snaptime": 1_700_000_000,
            }
        )
        raise KeyboardInterrupt("caller died after PVE accepted the mutation")

    pve.on_submit = die_after_pve_accepts
    orchestrator = PackageUpdateSnapshotOrchestrator(
        authority, _dark_channel(pve, journal)
    )

    with pytest.raises(KeyboardInterrupt):
        orchestrator.ensure_job_owned_snapshot(job.job_id)

    # Durable state proves a mutation may already have happened.
    crashed = store.package_update_job(job.job_id)
    assert crashed.status is PackageUpdateJobStatus.ACTIVE
    assert crashed.checkpoint is PackageUpdateCheckpoint.SNAPSHOT_MAY_HAVE_STARTED
    assert crashed.snapshot_confirmed_at is None
    # Startup recovery must not clear it or free the global slot.
    assert authority.recover_interrupted_package_update_jobs() == ()

    # The retry reattaches on the host journal's canonical evidence and
    # submits nothing further.
    pve.on_submit = None
    recovered = orchestrator.ensure_job_owned_snapshot(job.job_id)

    assert pve.submissions == 1
    assert recovered.outcome is SnapshotOperationOutcome.COMPLETED
    assert recovered.job.checkpoint is PackageUpdateCheckpoint.SNAPSHOT_CONFIRMED
    assert recovered.job.snapshot_name == identity.snapshot_name


def test_a_crash_leaving_no_snapshot_stays_uncertain_and_fenced(
    tmp_path: Path,
) -> None:
    store, authority, job, identity, ownership, pve, journal, upid = _dark_system(
        tmp_path
    )

    def die_without_creating(p):
        raise KeyboardInterrupt("caller died; PVE may or may not have started")

    pve.on_submit = die_without_creating
    orchestrator = PackageUpdateSnapshotOrchestrator(
        authority, _dark_channel(pve, journal)
    )
    with pytest.raises(KeyboardInterrupt):
        orchestrator.ensure_job_owned_snapshot(job.job_id)

    pve.on_submit = None
    retried = orchestrator.ensure_job_owned_snapshot(job.job_id)

    # No second submission, and the outcome is honestly unknown.
    assert pve.submissions == 1
    assert retried.outcome is SnapshotOperationOutcome.UNCERTAIN
    fenced = store.package_update_job(job.job_id)
    assert fenced.status is PackageUpdateJobStatus.ACTIVE
    assert fenced.checkpoint is PackageUpdateCheckpoint.SNAPSHOT_MAY_HAVE_STARTED
    assert fenced.snapshot_confirmed_at is None
    assert authority.recover_interrupted_package_update_jobs() == ()


# ===========================================================================
# The P2 witness: a pre-flight refusal must not fence the global slot forever
# ===========================================================================


class _ResourceRef:
    """Minimal stand-in for the `_issue` helper's resource argument."""

    def __init__(self, resource_id: str) -> None:
        self.resource_id = resource_id


def test_a_guest_that_moved_node_blocks_the_job_without_submitting_anything(
    tmp_path: Path,
) -> None:
    """The original witness, end to end through the real dark boundary.

    The job is already past its write-ahead uncertainty checkpoint when the
    host refuses on live target revalidation. Because the host's durable
    journal proves the submission subprocess was never launched, the job is
    terminalized instead of holding the one global destructive slot forever.
    """

    store, authority, job, identity, ownership, pve, journal, _ = _dark_system(
        tmp_path
    )
    # The guest is no longer on the node this job froze at issuance.
    pve.node = "pve-moved"

    orchestrator = PackageUpdateSnapshotOrchestrator(
        authority, _dark_channel(pve, journal)
    )
    result = orchestrator.ensure_job_owned_snapshot(job.job_id)

    # 1. Nothing was ever submitted.
    assert pve.submissions == 0
    assert not any(argv[1] == "create" for argv in pve.argvs)
    from tests.test_package_snapshot_helper import helper as snapshot_helper

    assert (
        snapshot_helper.OperationJournal(journal.directory)
        .read(identity.snapshot_operation_id)["phase"]
        == "intent"
    )

    # 2. The typed outcome is the proof, not unresolvable uncertainty.
    assert result.outcome is SnapshotOperationOutcome.NOT_SUBMITTED

    # 3. The job is terminal and its evidence stays honest.
    blocked = store.package_update_job(job.job_id)
    assert blocked.status is PackageUpdateJobStatus.BLOCKED
    assert blocked.checkpoint is PackageUpdateCheckpoint.SNAPSHOT_MAY_HAVE_STARTED
    assert blocked.snapshot_name == identity.snapshot_name
    assert blocked.snapshot_operation_id == identity.snapshot_operation_id
    assert blocked.snapshot_task_upid is None
    assert blocked.snapshot_confirmed_at is None
    assert blocked.terminal_reason
    events = store.list_package_update_job_events(job.job_id)
    assert (
        events[-1].event_type
        is PackageUpdateEventType.SNAPSHOT_BLOCKED_BEFORE_SUBMISSION
    )

    # 4. Startup recovery leaves the terminal job exactly as it is.
    before = store.list_package_update_job_events(job.job_id)
    assert authority.recover_interrupted_package_update_jobs() == ()
    assert store.package_update_job(job.job_id) == blocked
    assert store.list_package_update_job_events(job.job_id) == before

    # 5. The global destructive slot is free again.
    approval = store.package_plan_approval(blocked.resource_id)
    successor = _issue(authority, _ResourceRef(blocked.resource_id), approval)
    assert successor.status is PackageUpdateJobStatus.ACTIVE
    assert successor.job_id != job.job_id


def test_a_job_that_observed_a_task_can_never_be_released_as_unsubmitted(
    tmp_path: Path,
) -> None:
    """The durable job record outranks a host claim of non-submission.

    A recorded task identity proves the operation WAS submitted, so authority
    refuses the release outright and the orchestrator keeps the job fenced.
    """

    _, store, authority, _, job, identity, _ = _prepared(tmp_path)
    authority.record_package_update_snapshot_task(job.job_id, UPID)

    with pytest.raises(AuthorityConflict, match="cannot be released as unsubmitted"):
        authority.block_package_update_before_snapshot_submission(
            job.job_id, "host claims nothing was submitted"
        )

    host = FakeHostControl(
        HostSnapshotResult(
            outcome=SnapshotOperationOutcome.NOT_SUBMITTED,
            snapshot_operation_id=identity.snapshot_operation_id,
            reason="host proved no snapshot mutation was submitted",
        )
    )
    result = PackageUpdateSnapshotOrchestrator(
        authority, host
    ).ensure_job_owned_snapshot(job.job_id)

    assert result.outcome is SnapshotOperationOutcome.UNCERTAIN
    fenced = store.package_update_job(job.job_id)
    assert fenced.status is PackageUpdateJobStatus.ACTIVE
    assert fenced.snapshot_task_upid == UPID


def test_the_pre_submission_release_cannot_reach_other_checkpoints(
    tmp_path: Path,
) -> None:
    _, store, authority, resource, _, approval = _approved_system(tmp_path)
    job = _issue(authority, resource, approval)

    # Before the write-ahead intent there is nothing to release this way.
    with pytest.raises(AuthorityConflict, match="not inside a snapshot operation"):
        authority.block_package_update_before_snapshot_submission(
            job.job_id, "too early"
        )
    authority.record_package_update_preflight_passed(job.job_id)
    with pytest.raises(AuthorityConflict, match="not inside a snapshot operation"):
        authority.block_package_update_before_snapshot_submission(
            job.job_id, "still too early"
        )

    # And once terminal it cannot be reused.
    authority.record_package_update_snapshot_intent(job.job_id)
    authority.block_package_update_before_snapshot_submission(job.job_id, "blocked")
    assert store.package_update_job(job.job_id).status is (
        PackageUpdateJobStatus.BLOCKED
    )
    with pytest.raises(AuthorityConflict, match="terminal"):
        authority.block_package_update_before_snapshot_submission(
            job.job_id, "again"
        )


def test_a_confirmed_snapshot_can_never_be_released_as_unsubmitted(
    tmp_path: Path,
) -> None:
    _, store, authority, _, job, identity, ownership = _prepared(tmp_path)
    authority.confirm_package_update_snapshot(
        job.job_id, _canonical(ownership, identity)
    )
    with pytest.raises(AuthorityConflict, match="not inside a snapshot operation"):
        authority.block_package_update_before_snapshot_submission(
            job.job_id, "host claims nothing was submitted"
        )


def test_uncertain_host_outcomes_never_release_the_global_slot(
    tmp_path: Path,
) -> None:
    """Positive control for the fencing this correction must not weaken."""

    _, store, authority, resource, _, approval = _approved_system(tmp_path)
    other_resource, _, other_approval = _add_approved_resource(store, authority)
    job = _issue(authority, resource, approval)
    identity = authority.package_update_snapshot_identity(job.job_id)

    host = FakeHostControl(
        HostSnapshotResult(
            outcome=SnapshotOperationOutcome.UNCERTAIN,
            snapshot_operation_id=identity.snapshot_operation_id,
            task_upid=UPID,
            reason="task still running at the polling bound",
        )
    )
    result = PackageUpdateSnapshotOrchestrator(
        authority, host
    ).ensure_job_owned_snapshot(job.job_id)

    assert result.outcome is SnapshotOperationOutcome.UNCERTAIN
    assert store.package_update_job(job.job_id).status is (
        PackageUpdateJobStatus.ACTIVE
    )
    with pytest.raises(AuthorityConflict, match="global slot"):
        _issue(authority, other_resource, other_approval)


def test_a_lost_ssh_answer_never_releases_the_global_slot(tmp_path: Path) -> None:
    """A transport failure says nothing about whether PVE ran the mutation."""

    from app.package_scan_host_control import BoundedProcessResult
    from app.package_update_snapshot_host_control import (
        SshPackageUpdateSnapshotHostControl,
    )

    store, authority, job, identity, ownership, pve, journal, _ = _dark_system(
        tmp_path
    )

    def lost(argv, input_bytes, timeout, max_output):
        return BoundedProcessResult(255, b"", b"", timed_out=True)

    channel = SshPackageUpdateSnapshotHostControl(
        host="pve.example.internal",
        port=22,
        user="hubinet-snapshot",
        private_key_path=Path("/etc/hubinet-ops/snapshot-key"),
        known_hosts_path=Path("/etc/hubinet-ops/known_hosts"),
        timeout_seconds=60,
        max_result_bytes=1024 * 1024,
        runner=lost,
    )
    result = PackageUpdateSnapshotOrchestrator(
        authority, channel
    ).ensure_job_owned_snapshot(job.job_id)

    assert result.outcome is SnapshotOperationOutcome.UNCERTAIN
    fenced = store.package_update_job(job.job_id)
    assert fenced.status is PackageUpdateJobStatus.ACTIVE
    assert fenced.checkpoint is PackageUpdateCheckpoint.SNAPSHOT_MAY_HAVE_STARTED
    assert authority.recover_interrupted_package_update_jobs() == ()


def test_an_unknown_or_absent_submission_token_stays_uncertain() -> None:
    """An older helper that reports no proof can never release a job."""

    from app.package_update_snapshot_host_control import (
        SshPackageUpdateSnapshotHostControl,
    )

    channel = SshPackageUpdateSnapshotHostControl(
        host="pve.example.internal",
        port=22,
        user="hubinet-snapshot",
        private_key_path=Path("/etc/hubinet-ops/snapshot-key"),
        known_hosts_path=Path("/etc/hubinet-ops/known_hosts"),
        timeout_seconds=60,
        max_result_bytes=1024 * 1024,
    )
    operation_id = str(uuid.uuid4())
    for error in (
        {"classification": "stale_target"},
        {"classification": "stale_target", "submission": "maybe"},
        {"classification": "stale_target", "submission": None},
        {"classification": "stale_target", "submission": "NOT_SUBMITTED"},
        {"classification": "stale_target", "submission": ["not_submitted"]},
    ):
        parsed = channel._parse_payload(
            {
                "response_version": 1,
                "ok": False,
                "snapshot_operation_id": operation_id,
                "error": error,
            },
            operation_id,
        )
        assert parsed.outcome is SnapshotOperationOutcome.UNCERTAIN, error

    proved = channel._parse_payload(
        {
            "response_version": 1,
            "ok": False,
            "snapshot_operation_id": operation_id,
            "error": {"classification": "stale_target", "submission": "not_submitted"},
        },
        operation_id,
    )
    assert proved.outcome is SnapshotOperationOutcome.NOT_SUBMITTED


# ===========================================================================
# Authority transition atomicity (check-then-commit race)
#
# Every transition whose safety depends on CURRENT authority must re-prove it
# inside the SAME SQLite transaction that commits the transition. Proving in
# one transaction and committing in another let discovery reconciliation
# replace the guest occupying the same VMID on the same node in between: the
# checkpoint CAS only sees job status/checkpoint, and the host helper can only
# verify live PVE VMID/type/node facts, never a backend resource incarnation.
# ===========================================================================


def _break_incarnation_continuity_at_the_same_locator(store, authority) -> None:
    """Invalidate the job's incarnation while every live PVE fact stays equal.

    Driven through real reconciliation: the guest disappears from one complete
    baseline and returns in the next. Afterwards the VMID, node, resource
    type, and running status are all byte-for-byte what the job froze, so the
    host helper's independent live checks cannot possibly catch it -- but the
    backend's incarnation lifecycle and continuity revision have moved, so the
    job's authority is stale. This is precisely the case a check-then-commit
    race would let through.
    """

    source_id = store.list_resources()[0].inventory_source_id
    before = store.list_resources()[0]
    _reconcile(authority, source_id, resource_present=False)
    _reconcile(authority, source_id)
    after = store.list_resources()[0]
    # Guard the fixture itself: it must change only backend-known facts.
    assert after.resource_id == before.resource_id
    assert (after.vmid, after.current_node_id, after.resource_type) == (
        before.vmid,
        before.current_node_id,
        before.resource_type,
    )
    assert after.lifecycle != "active"
    assert after.resource_continuity_revision != before.resource_continuity_revision


def _authority_transitions(authority):
    """The three transitions that claim current authority permits advancing."""

    return (
        (
            "preflight",
            PackageUpdateCheckpoint.ISSUED,
            lambda job_id: authority.record_package_update_preflight_passed(job_id),
        ),
        (
            "snapshot intent",
            PackageUpdateCheckpoint.PREFLIGHT_PASSED,
            lambda job_id: authority.record_package_update_snapshot_intent(job_id),
        ),
        (
            "snapshot confirmation",
            PackageUpdateCheckpoint.SNAPSHOT_MAY_HAVE_STARTED,
            lambda job_id: authority.confirm_package_update_snapshot(job_id, ()),
        ),
    )


def _advance_to(authority, job_id, checkpoint) -> None:
    if checkpoint is PackageUpdateCheckpoint.ISSUED:
        return
    authority.record_package_update_preflight_passed(job_id)
    if checkpoint is PackageUpdateCheckpoint.PREFLIGHT_PASSED:
        return
    authority.record_package_update_snapshot_intent(job_id)


@pytest.mark.parametrize("index", [0, 1, 2])
def test_broken_incarnation_continuity_refuses_every_authority_transition(
    tmp_path: Path, index: int
) -> None:
    _, store, authority, resource, _, approval = _approved_system(tmp_path)
    job = _issue(authority, resource, approval)
    name, checkpoint, transition = _authority_transitions(authority)[index]
    _advance_to(authority, job.job_id, checkpoint)

    before = store.package_update_job(job.job_id)
    before_events = store.list_package_update_job_events(job.job_id)
    assert before.checkpoint is checkpoint

    # Same VMID, same node, different backend incarnation.
    _break_incarnation_continuity_at_the_same_locator(store, authority)

    with pytest.raises(AuthorityConflict, match="authority context is stale"):
        transition(job.job_id)

    # Refusal is atomic: nothing about the job moved, and no event was left
    # behind by a partially applied transition.
    after = store.package_update_job(job.job_id)
    assert after == before, name
    assert store.list_package_update_job_events(job.job_id) == before_events, name


@pytest.mark.parametrize("index", [0, 1, 2])
def test_the_authority_proof_and_its_transition_share_one_transaction(
    tmp_path: Path, index: int
) -> None:
    """The actual race proof, not just that the predicate exists.

    A seam fires inside the transition transaction, immediately after the
    authority proof -- exactly where an interleaving writer used to be able to
    invalidate the job between the proof and the commit. From a second
    connection, that write must be impossible: the transition holds the
    database write lock across both.
    """

    import sqlite3 as _sqlite3

    _, store, authority, resource, _, approval = _approved_system(tmp_path)
    job = _issue(authority, resource, approval)
    _, checkpoint, transition = _authority_transitions(authority)[index]
    _advance_to(authority, job.job_id, checkpoint)

    attempted: list[str] = []

    def seam(connection, *, job_id):
        # A genuinely separate connection, with a short busy timeout so the
        # attempt fails fast instead of waiting out the store's own.
        other = _sqlite3.connect(store.path, timeout=0.1, isolation_level=None)
        try:
            # No other writer can begin -- so nothing can invalidate this job
            # between the authority proof just taken and the commit below.
            with pytest.raises(_sqlite3.OperationalError, match="locked"):
                other.execute("BEGIN IMMEDIATE")
            attempted.append(job_id)
        finally:
            other.close()

    authority._after_package_update_authority_proof = seam
    # The confirmation transition needs real canonical evidence to commit.
    if checkpoint is PackageUpdateCheckpoint.SNAPSHOT_MAY_HAVE_STARTED:
        identity = authority.package_update_snapshot_identity(job.job_id)
        ownership = authority.package_update_snapshot_ownership(job.job_id)
        authority.confirm_package_update_snapshot(
            job.job_id, _canonical(ownership, identity)
        )
    else:
        transition(job.job_id)

    assert attempted == [job.job_id]


@pytest.mark.parametrize("index", [0, 1, 2])
def test_a_transition_interrupted_after_its_proof_commits_nothing(
    tmp_path: Path, index: int
) -> None:
    """Proof, transition, and event are one atomic unit."""

    _, store, authority, resource, _, approval = _approved_system(tmp_path)
    job = _issue(authority, resource, approval)
    _, checkpoint, transition = _authority_transitions(authority)[index]
    _advance_to(authority, job.job_id, checkpoint)

    before = store.package_update_job(job.job_id)
    before_events = store.list_package_update_job_events(job.job_id)

    def die(connection, *, job_id):
        raise KeyboardInterrupt("process died between the proof and the commit")

    authority._after_package_update_authority_proof = die
    with pytest.raises(KeyboardInterrupt):
        transition(job.job_id)

    assert store.package_update_job(job.job_id) == before
    assert store.list_package_update_job_events(job.job_id) == before_events


def test_broken_incarnation_continuity_never_reaches_host_submission(
    tmp_path: Path,
) -> None:
    """The critical original witness, end to end through the real boundary.

    The guest is replaced at the same VMID on the same node after the job was
    issued. The host helper independently verifies only live PVE VMID, type,
    node, and status -- all of which still match -- so nothing downstream could
    catch this. It must be refused in the backend, before any submission.
    """

    store, authority, job, identity, ownership, pve, journal, _ = _dark_system(
        tmp_path
    )
    # Live PVE still shows the same VMID, type, node and status.
    assert pve.vmid == job.expected_vmid
    assert pve.node == job.expected_node_name

    _break_incarnation_continuity_at_the_same_locator(store, authority)

    orchestrator = PackageUpdateSnapshotOrchestrator(
        authority, _dark_channel(pve, journal)
    )
    with pytest.raises(AuthorityConflict, match="authority context is stale"):
        orchestrator.ensure_job_owned_snapshot(job.job_id)

    assert pve.submissions == 0
    assert pve.argvs == []
    stalled = store.package_update_job(job.job_id)
    assert stalled.checkpoint is PackageUpdateCheckpoint.ISSUED
    assert stalled.snapshot_operation_id is None
    assert stalled.snapshot_name is None


def test_broken_continuity_after_preflight_cannot_record_snapshot_intent(
    tmp_path: Path,
) -> None:
    """The window that matters most: the last step before submission."""

    store, authority, job, identity, ownership, pve, journal, _ = _dark_system(
        tmp_path
    )
    authority.record_package_update_preflight_passed(job.job_id)
    _break_incarnation_continuity_at_the_same_locator(store, authority)

    orchestrator = PackageUpdateSnapshotOrchestrator(
        authority, _dark_channel(pve, journal)
    )
    with pytest.raises(AuthorityConflict, match="authority context is stale"):
        orchestrator.ensure_job_owned_snapshot(job.job_id)

    assert pve.submissions == 0
    stalled = store.package_update_job(job.job_id)
    assert stalled.checkpoint is PackageUpdateCheckpoint.PREFLIGHT_PASSED
    assert stalled.snapshot_operation_id is None


def test_post_submission_transitions_do_not_require_current_authority(
    tmp_path: Path,
) -> None:
    """Staleness must not discard evidence about a possible PVE mutation.

    Once a snapshot may already have been submitted, recording the task,
    recording uncertainty, and startup fencing are all about preserving
    evidence -- they are not claims that authority permits advancing, so a
    replaced incarnation must not stop them.
    """

    _, store, authority, _, job, identity, _ = _prepared(tmp_path)
    _break_incarnation_continuity_at_the_same_locator(store, authority)

    assert (
        authority.record_package_update_snapshot_task(job.job_id, UPID).snapshot_task_upid
        == UPID
    )
    assert authority.record_package_update_snapshot_uncertain(
        job.job_id, "outcome unknown"
    ).status is PackageUpdateJobStatus.ACTIVE
    assert authority.recover_interrupted_package_update_jobs() == ()
    assert store.package_update_job(job.job_id).status is (
        PackageUpdateJobStatus.ACTIVE
    )

    # ...but advancing to confirmation still refuses.
    with pytest.raises(AuthorityConflict, match="authority context is stale"):
        authority.confirm_package_update_snapshot(job.job_id, ())


# ===========================================================================
# An inspected canonical absence is an observation, not a failure
#
# `inspect_job_snapshot_state` is read-only. Absence does not prove that an
# already-submitted asynchronous PVE snapshot operation terminated: its task
# may still be queued or running and about to create the snapshot. So it must
# never reach the FAILED branch, which is what terminalizes a job.
# ===========================================================================


def _channel(runner):
    from app.package_update_snapshot_host_control import (
        SshPackageUpdateSnapshotHostControl,
    )

    return SshPackageUpdateSnapshotHostControl(
        host="pve.example.internal",
        port=22,
        user="hubinet-snapshot",
        private_key_path=Path("/etc/hubinet-ops/snapshot-key"),
        known_hosts_path=Path("/etc/hubinet-ops/known_hosts"),
        timeout_seconds=60,
        max_result_bytes=1024 * 1024,
        runner=runner,
    )


def test_an_inspected_absence_maps_to_uncertain_never_failed() -> None:
    channel = _channel(None)
    operation_id = str(uuid.uuid4())
    absent = channel._parse_payload(
        {
            "response_version": 1,
            "ok": True,
            "snapshot_operation_id": operation_id,
            "outcome": "absent",
            "snapshots": [{"name": "current", "description": "You are here!"}],
        },
        operation_id,
    )
    assert absent.outcome is SnapshotOperationOutcome.UNCERTAIN
    assert absent.outcome is not SnapshotOperationOutcome.FAILED
    # And absence is never the durable non-submission proof either.
    assert absent.outcome is not SnapshotOperationOutcome.NOT_SUBMITTED


def test_inspecting_a_submitted_operation_that_shows_nothing_stays_fenced(
    tmp_path: Path,
) -> None:
    """The real chain: journal says submitted, canonical state shows nothing.

    The task may still be about to run. The job must stay active and fenced,
    and nothing may terminalize it.
    """

    from tests.test_package_snapshot_helper import helper as snapshot_helper

    store, authority, job, identity, ownership, pve, journal, _ = _dark_system(
        tmp_path
    )
    authority.record_package_update_preflight_passed(job.job_id)
    authority.record_package_update_snapshot_intent(job.job_id)

    # A submission was made; its task identity was never durably captured.
    # The fingerprint must be the real one, or the helper would short-circuit
    # on a request mismatch and never compute canonical absence at all.
    fingerprint = snapshot_helper.request_fingerprint(
        snapshot_helper.validate_request(
            {
                "request_version": 1,
                "operation": "inspect_job_snapshot_state",
                "target": {
                    "vmid": job.expected_vmid,
                    "expected_node": job.expected_node_name,
                },
                "operation_identity": {
                    "snapshot_operation_id": identity.snapshot_operation_id,
                    "snapshot_name": identity.snapshot_name,
                },
                "ownership": {
                    "job_id": ownership.job_id,
                    "resource_id": ownership.resource_id,
                    "resource_continuity_revision": (
                        ownership.resource_continuity_revision
                    ),
                    "inventory_source_id": ownership.inventory_source_id,
                    "backend_instance_id": ownership.backend_instance_id,
                },
            }
        )
    )
    journal.write(
        {
            "journal_version": 1,
            "snapshot_operation_id": identity.snapshot_operation_id,
            "request_fingerprint": fingerprint,
            "vmid": job.expected_vmid,
            "expected_node": job.expected_node_name,
            "snapshot_name": identity.snapshot_name,
            "phase": "submitted",
        }
    )

    channel = _dark_channel(pve, journal)
    result = channel.inspect_job_snapshot_state(
        snapshot_operation_id=identity.snapshot_operation_id,
        snapshot_name=identity.snapshot_name,
        vmid=job.expected_vmid,
        expected_node=job.expected_node_name,
        ownership=ownership,
    )

    assert result.outcome is SnapshotOperationOutcome.UNCERTAIN
    assert pve.submissions == 0
    # ...and specifically because `absent` maps to UNCERTAIN, not because the
    # helper refused for some other reason.
    raw = snapshot_helper.handle_request(
        {
            "request_version": 1,
            "operation": "inspect_job_snapshot_state",
            "target": {
                "vmid": job.expected_vmid,
                "expected_node": job.expected_node_name,
            },
            "operation_identity": {
                "snapshot_operation_id": identity.snapshot_operation_id,
                "snapshot_name": identity.snapshot_name,
            },
            "ownership": {
                "job_id": ownership.job_id,
                "resource_id": ownership.resource_id,
                "resource_continuity_revision": (
                    ownership.resource_continuity_revision
                ),
                "inventory_source_id": ownership.inventory_source_id,
                "backend_instance_id": ownership.backend_instance_id,
            },
        },
        runner=pve,
        journal=journal,
    )
    assert raw["ok"] is True and raw["outcome"] == "absent"

    # Handing that observation to the orchestrator must not terminalize.
    orchestrator = PackageUpdateSnapshotOrchestrator(authority, channel)
    staged = orchestrator._apply_host_result(
        job.job_id, identity.snapshot_operation_id, result
    )
    assert staged.outcome is SnapshotOperationOutcome.UNCERTAIN
    fenced = store.package_update_job(job.job_id)
    assert fenced.status is PackageUpdateJobStatus.ACTIVE
    assert fenced.checkpoint is PackageUpdateCheckpoint.SNAPSHOT_MAY_HAVE_STARTED
    assert fenced.terminalized_at is None
    assert authority.recover_interrupted_package_update_jobs() == ()


def test_a_genuinely_failed_task_still_terminalizes(tmp_path: Path) -> None:
    """Positive control: the FAILED path keeps its real task-failure evidence."""

    _, store, authority, resource, _, approval = _approved_system(tmp_path)
    job = _issue(authority, resource, approval)
    identity = authority.package_update_snapshot_identity(job.job_id)
    host = FakeHostControl(
        HostSnapshotResult(
            outcome=SnapshotOperationOutcome.FAILED,
            snapshot_operation_id=identity.snapshot_operation_id,
            task_upid=UPID,
            task=classify_task_status(
                {"upid": UPID, "status": "stopped", "exitstatus": "boom"}
            ),
            snapshots=(_current_entry(), _foreign_entry()),
            reason="PVE snapshot task terminated in a failure state",
        )
    )
    result = PackageUpdateSnapshotOrchestrator(
        authority, host
    ).ensure_job_owned_snapshot(job.job_id)

    assert result.outcome is SnapshotOperationOutcome.FAILED
    assert store.package_update_job(job.job_id).status is (
        PackageUpdateJobStatus.BLOCKED
    )


def test_a_failed_task_whose_snapshot_might_exist_still_stays_uncertain(
    tmp_path: Path,
) -> None:
    """Positive control: FAILED still needs canonical proof of absence."""

    _, store, authority, resource, _, approval = _approved_system(tmp_path)
    job = _issue(authority, resource, approval)
    identity = authority.package_update_snapshot_identity(job.job_id)
    ownership = authority.package_update_snapshot_ownership(job.job_id)
    host = FakeHostControl(
        HostSnapshotResult(
            outcome=SnapshotOperationOutcome.FAILED,
            snapshot_operation_id=identity.snapshot_operation_id,
            task_upid=UPID,
            task=classify_task_status(
                {"upid": UPID, "status": "stopped", "exitstatus": "boom"}
            ),
            snapshots=_canonical(ownership, identity),
            reason="PVE snapshot task terminated in a failure state",
        )
    )
    result = PackageUpdateSnapshotOrchestrator(
        authority, host
    ).ensure_job_owned_snapshot(job.job_id)

    assert result.outcome is SnapshotOperationOutcome.UNCERTAIN
    assert store.package_update_job(job.job_id).status is (
        PackageUpdateJobStatus.ACTIVE
    )


def test_only_the_durable_proof_releases_a_never_submitted_job(
    tmp_path: Path,
) -> None:
    """Positive control alongside the absence rule.

    Absence stays uncertain; the explicit host proof still releases.
    """

    _, store, authority, resource, _, approval = _approved_system(tmp_path)
    job = _issue(authority, resource, approval)
    identity = authority.package_update_snapshot_identity(job.job_id)
    host = FakeHostControl(
        HostSnapshotResult(
            outcome=SnapshotOperationOutcome.NOT_SUBMITTED,
            snapshot_operation_id=identity.snapshot_operation_id,
            reason="host proved no snapshot mutation was submitted (stale_target)",
        )
    )
    result = PackageUpdateSnapshotOrchestrator(
        authority, host
    ).ensure_job_owned_snapshot(job.job_id)

    assert result.outcome is SnapshotOperationOutcome.NOT_SUBMITTED
    assert store.package_update_job(job.job_id).status is (
        PackageUpdateJobStatus.BLOCKED
    )
