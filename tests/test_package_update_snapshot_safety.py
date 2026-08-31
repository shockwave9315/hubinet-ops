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
