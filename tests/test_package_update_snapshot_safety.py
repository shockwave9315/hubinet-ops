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
    SnapshotSubmissionRefusedBeforeCallback,
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
    HostSubmissionState,
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
        # An otherwise well-formed payload with one extra field is rejected
        # by the exact-shape check, not silently ignored.
        f'{SNAPSHOT_METADATA_MARKER} {{"protocol":1,'
        f'"kind":"pre_update",'
        f'"job_id":"00000000-0000-0000-0000-000000000001",'
        f'"resource_id":"00000000-0000-0000-0000-000000000002",'
        f'"resource_continuity_revision":1,'
        f'"inventory_source_id":"00000000-0000-0000-0000-000000000003",'
        f'"backend_instance_id":"00000000-0000-0000-0000-000000000004",'
        f'"extra_field":"unexpected"}}',
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
        def ensure_pre_update_snapshot_submitted(self, **kwargs):
            # By the time the host boundary is reached at all, the durable
            # write-ahead checkpoint must already be committed and visible.
            observed.append(store.package_update_job(job.job_id).checkpoint)
            raise RuntimeError("host process died mid-submission")

        def inspect_job_snapshot_state(self, **kwargs) -> HostSnapshotResult:
            # Nothing has been journaled for this operation yet, so a NEW
            # submission is still permitted and the orchestrator proceeds to
            # the submission critical section above.
            return HostSnapshotResult(
                outcome=SnapshotOperationOutcome.UNCERTAIN,
                snapshot_operation_id=kwargs["snapshot_operation_id"],
                submission_state=HostSubmissionState.ABSENT,
            )

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
    """A dark host boundary that records every call it receives.

    Mimics just enough of the real host's journal-driven ``submission_state``
    for the orchestrator's split read-then-submit contract to exercise
    correctly: before any submission call it reports ``absent`` (a NEW
    submission is permitted); after one, it reports back whatever the queued
    submission result itself carried, exactly as the real host's own
    ``inspect_job_snapshot_state`` would keep reporting the same durable
    journal phase until something changes it.
    """

    def __init__(self, *results: HostSnapshotResult) -> None:
        self._results = list(results)
        self.create_calls: list[dict] = []
        self.inspect_calls: list[dict] = []
        self.seal_calls: list[dict] = []
        self._last_submission_result: HostSnapshotResult | None = None
        self._sealed = False

    def ensure_pre_update_snapshot_submitted(self, **kwargs) -> HostSnapshotResult:
        self.create_calls.append(kwargs)
        result = self._results.pop(0)
        self._last_submission_result = result
        return result

    def inspect_job_snapshot_state(self, **kwargs) -> HostSnapshotResult:
        self.inspect_calls.append(kwargs)
        if self._sealed:
            return HostSnapshotResult(
                outcome=SnapshotOperationOutcome.NOT_SUBMITTED,
                snapshot_operation_id=kwargs["snapshot_operation_id"],
                submission_state=HostSubmissionState.SEALED_NOT_SUBMITTED,
                reason="host durably sealed this snapshot operation before submission",
            )
        if self._last_submission_result is not None:
            if (
                self._last_submission_result.outcome
                is SnapshotOperationOutcome.NOT_SUBMITTED
            ):
                # NOT_SUBMITTED is only ever produced by the mutating
                # operation's own pre-submission-window error path; the
                # journal never advances past `intent` for it, so a
                # subsequent read-only inspection sees exactly that -- never
                # a NOT_SUBMITTED outcome of its own, which no real inspect
                # response ever carries.
                return HostSnapshotResult(
                    outcome=SnapshotOperationOutcome.UNCERTAIN,
                    snapshot_operation_id=kwargs["snapshot_operation_id"],
                    submission_state=HostSubmissionState.INTENT,
                    reason=self._last_submission_result.reason,
                )
            return self._last_submission_result
        return HostSnapshotResult(
            outcome=SnapshotOperationOutcome.UNCERTAIN,
            snapshot_operation_id=kwargs["snapshot_operation_id"],
            submission_state=HostSubmissionState.ABSENT,
        )

    def seal_operation_never_submitted(self, **kwargs) -> HostSnapshotResult:
        self.seal_calls.append(kwargs)
        if self._last_submission_result is not None and (
            self._last_submission_result.submission_state
            not in (None, HostSubmissionState.ABSENT, HostSubmissionState.INTENT)
        ):
            return self._last_submission_result
        self._sealed = True
        return HostSnapshotResult(
            outcome=SnapshotOperationOutcome.NOT_SUBMITTED,
            snapshot_operation_id=kwargs["snapshot_operation_id"],
            submission_state=HostSubmissionState.SEALED_NOT_SUBMITTED,
            reason="host durably sealed this snapshot operation before submission",
        )


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


def test_a_fresh_database_initializes_at_the_current_schema_version(
    tmp_path: Path,
) -> None:
    from app.inventory.store import AUTHORITY_SCHEMA_MARKER, AUTHORITY_SCHEMA_VERSION

    assert AUTHORITY_SCHEMA_VERSION == 12
    store = InventoryAuthorityStore(tmp_path / "authority.db")
    with sqlite3.connect(tmp_path / "authority.db") as connection:
        marker, version = connection.execute(
            "SELECT marker, schema_version FROM authority_schema"
        ).fetchone()
        user_version = connection.execute("PRAGMA user_version").fetchone()[0]
    assert (marker, version, user_version) == (
        AUTHORITY_SCHEMA_MARKER,
        AUTHORITY_SCHEMA_VERSION,
        AUTHORITY_SCHEMA_VERSION,
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


# ===========================================================================
# Final prove-and-seal and stale-authority correction matrix
# ===========================================================================


def test_a_deleted_guest_is_sealed_and_releases_the_slot_without_submission(
    tmp_path: Path,
) -> None:
    from tests.test_package_snapshot_helper import helper as snapshot_helper

    store, authority, job, identity, _, pve, journal, _ = _dark_system(tmp_path)
    pve.present = False

    result = PackageUpdateSnapshotOrchestrator(
        authority, _dark_channel(pve, journal)
    ).ensure_job_owned_snapshot(job.job_id)

    assert result.outcome is SnapshotOperationOutcome.NOT_SUBMITTED
    assert pve.submissions == 0
    assert not any(argv[1] == "create" for argv in pve.argvs)
    assert journal.read(identity.snapshot_operation_id)["phase"] == (
        "sealed_not_submitted"
    )
    blocked = store.package_update_job(job.job_id)
    assert blocked.status is PackageUpdateJobStatus.BLOCKED
    assert blocked.terminal_reason == (
        "host durably sealed this snapshot operation before submission"
    )
    approval = store.package_plan_approval(blocked.resource_id)
    assert _issue(
        authority, _ResourceRef(blocked.resource_id), approval
    ).status is PackageUpdateJobStatus.ACTIVE
    assert snapshot_helper.JOURNAL_PHASES[1] == "sealed_not_submitted"


def test_delayed_helper_launched_before_flock_must_obey_the_seal(
    tmp_path: Path,
) -> None:
    """Known P2: the orphan is paused before VmidMutationLock.__enter__."""

    import threading

    from tests.test_package_snapshot_helper import helper as snapshot_helper

    store, authority, job, identity, ownership, pve, journal, _ = _dark_system(
        tmp_path
    )
    authority.record_package_update_preflight_passed(job.job_id)
    job = authority.record_package_update_snapshot_intent(job.job_id)
    _break_incarnation_continuity_at_the_same_locator(store, authority)
    other_resource, _, other_approval = _add_approved_resource(store, authority)

    request = {
        "request_version": 1,
        "operation": "ensure_pre_update_snapshot_submitted",
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
            "resource_continuity_revision": ownership.resource_continuity_revision,
            "inventory_source_id": ownership.inventory_source_id,
            "backend_instance_id": ownership.backend_instance_id,
        },
    }
    launched_before_flock = threading.Event()
    enter_helper = threading.Event()
    delayed_result: dict[str, object] = {}

    def delayed_remote_helper() -> None:
        # This is the exact vulnerable seam: the SSH-side operation exists,
        # but real helper.handle_request has not reached its real flock yet.
        launched_before_flock.set()
        assert enter_helper.wait(timeout=10)
        delayed_result["value"] = snapshot_helper.handle_request(
            request, runner=pve, journal=journal
        )

    thread = threading.Thread(target=delayed_remote_helper)
    thread.start()
    try:
        assert launched_before_flock.wait(timeout=10)
        released = PackageUpdateSnapshotOrchestrator(
            authority, _dark_channel(pve, journal)
        ).ensure_job_owned_snapshot(job.job_id)
        assert released.outcome is SnapshotOperationOutcome.NOT_SUBMITTED
        assert journal.read(identity.snapshot_operation_id)["phase"] == (
            "sealed_not_submitted"
        )
        assert store.package_update_job(job.job_id).status is (
            PackageUpdateJobStatus.BLOCKED
        )
        assert pve.submissions == 0
    finally:
        enter_helper.set()
        thread.join(timeout=10)
    assert not thread.is_alive()
    assert delayed_result["value"]["submission_state"] == "sealed_not_submitted"
    assert pve.submissions == 0
    assert _issue(authority, other_resource, other_approval).status is (
        PackageUpdateJobStatus.ACTIVE
    )


def test_durable_seal_before_backend_commit_and_lost_response_retry_cleanly(
    tmp_path: Path,
) -> None:
    store, authority, job, identity, ownership, pve, journal, _ = _dark_system(
        tmp_path
    )
    authority.record_package_update_preflight_passed(job.job_id)
    job = authority.record_package_update_snapshot_intent(job.job_id)
    real = _dark_channel(pve, journal)

    # Host seal commits, but the backend has not committed BLOCKED yet.
    sealed = real.seal_operation_never_submitted(
        snapshot_operation_id=identity.snapshot_operation_id,
        snapshot_name=identity.snapshot_name,
        vmid=job.expected_vmid,
        expected_node=job.expected_node_name,
        ownership=ownership,
    )
    assert sealed.submission_state is HostSubmissionState.SEALED_NOT_SUBMITTED
    assert store.package_update_job(job.job_id).status is PackageUpdateJobStatus.ACTIVE

    class LoseFirstSealResponse:
        def __init__(self) -> None:
            self.seals = 0

        def inspect_job_snapshot_state(self, **kwargs):
            return real.inspect_job_snapshot_state(**kwargs)

        def ensure_pre_update_snapshot_submitted(self, **kwargs):
            raise AssertionError("sealed operations must never reach submission")

        def seal_operation_never_submitted(self, **kwargs):
            self.seals += 1
            real.seal_operation_never_submitted(**kwargs)
            if self.seals == 1:
                raise RuntimeError("seal response was lost")
            return real.seal_operation_never_submitted(**kwargs)

    lossy = LoseFirstSealResponse()
    first = PackageUpdateSnapshotOrchestrator(authority, lossy).ensure_job_owned_snapshot(
        job.job_id
    )
    assert first.outcome is SnapshotOperationOutcome.UNCERTAIN
    assert store.package_update_job(job.job_id).status is PackageUpdateJobStatus.ACTIVE

    second = PackageUpdateSnapshotOrchestrator(authority, lossy).ensure_job_owned_snapshot(
        job.job_id
    )
    assert second.outcome is SnapshotOperationOutcome.NOT_SUBMITTED
    assert second.job.status is PackageUpdateJobStatus.BLOCKED
    assert pve.submissions == 0
    events = store.list_package_update_job_events(job.job_id)
    assert sum(
        event.event_type
        is PackageUpdateEventType.SNAPSHOT_BLOCKED_BEFORE_SUBMISSION
        for event in events
    ) == 1


def test_old_helper_without_seal_support_never_releases(tmp_path: Path) -> None:
    _, store, authority, _, job, identity, _ = _prepared(tmp_path)
    _break_incarnation_continuity_at_the_same_locator(store, authority)

    class OldHelper:
        def inspect_job_snapshot_state(self, **kwargs):
            return HostSnapshotResult(
                outcome=SnapshotOperationOutcome.UNCERTAIN,
                snapshot_operation_id=identity.snapshot_operation_id,
                submission_state=HostSubmissionState.ABSENT,
            )

        def ensure_pre_update_snapshot_submitted(self, **kwargs):
            raise AssertionError("stale authority must refuse submission")

    result = PackageUpdateSnapshotOrchestrator(
        authority, OldHelper()
    ).ensure_job_owned_snapshot(job.job_id)
    assert result.outcome is SnapshotOperationOutcome.UNCERTAIN
    assert store.package_update_job(job.job_id).status is PackageUpdateJobStatus.ACTIVE


def test_operation_in_progress_does_not_route_to_seal_or_release(tmp_path: Path) -> None:
    from tests.test_package_snapshot_helper import helper as snapshot_helper

    store, authority, job, identity, _, pve, journal, _ = _dark_system(tmp_path)
    with snapshot_helper.VmidMutationLock(job.expected_vmid, journal.directory):
        result = PackageUpdateSnapshotOrchestrator(
            authority, _dark_channel(pve, journal)
        ).ensure_job_owned_snapshot(job.job_id)

    assert result.outcome is SnapshotOperationOutcome.UNCERTAIN
    assert store.package_update_job(job.job_id).status is PackageUpdateJobStatus.ACTIVE
    assert journal.read(identity.snapshot_operation_id) is None
    assert pve.submissions == 0


@pytest.mark.parametrize("journal_failure", ["corrupt", "mismatch"])
def test_corrupt_or_mismatched_journal_never_seals_or_releases_backend_slot(
    tmp_path: Path, journal_failure: str
) -> None:
    store, authority, job, identity, _, pve, journal, _ = _dark_system(tmp_path)
    journal.ensure_directory()
    if journal_failure == "corrupt":
        (
            journal.directory / f"op-{identity.snapshot_operation_id}.json"
        ).write_text("{corrupt")
    else:
        journal.write(
            {
                "journal_version": 1,
                "snapshot_operation_id": identity.snapshot_operation_id,
                "request_fingerprint": "0" * 64,
                "vmid": job.expected_vmid,
                "expected_node": job.expected_node_name,
                "snapshot_name": identity.snapshot_name,
                "phase": "intent",
            }
        )
    other_resource, _, other_approval = _add_approved_resource(store, authority)

    result = PackageUpdateSnapshotOrchestrator(
        authority, _dark_channel(pve, journal)
    ).ensure_job_owned_snapshot(job.job_id)

    assert result.outcome is SnapshotOperationOutcome.UNCERTAIN
    assert store.package_update_job(job.job_id).status is PackageUpdateJobStatus.ACTIVE
    assert pve.submissions == 0
    assert not any(argv[1] == "create" for argv in pve.argvs)
    with pytest.raises(AuthorityConflict, match="active package update job"):
        _issue(authority, other_resource, other_approval)


def test_pre_submission_config_read_failure_routes_through_seal(tmp_path: Path) -> None:
    store, authority, job, identity, _, pve, journal, _ = _dark_system(tmp_path)
    dispatch = pve._dispatch

    def fail_config(argv):
        if argv[:2] == ("pvesh", "get") and argv[2].endswith("/config"):
            return 1, b"", b"read failed"
        return dispatch(argv)

    pve._dispatch = fail_config
    result = PackageUpdateSnapshotOrchestrator(
        authority, _dark_channel(pve, journal)
    ).ensure_job_owned_snapshot(job.job_id)

    assert result.outcome is SnapshotOperationOutcome.NOT_SUBMITTED
    assert journal.read(identity.snapshot_operation_id)["phase"] == (
        "sealed_not_submitted"
    )
    assert store.package_update_job(job.job_id).status is PackageUpdateJobStatus.BLOCKED
    assert pve.submissions == 0


def test_transient_not_submitted_from_seal_does_not_recurse(tmp_path: Path) -> None:
    _, store, authority, _, job, identity, _ = _prepared(tmp_path)
    _break_incarnation_continuity_at_the_same_locator(store, authority)

    class TransientSeal:
        def __init__(self) -> None:
            self.seal_calls = 0

        def inspect_job_snapshot_state(self, **kwargs):
            return HostSnapshotResult(
                outcome=SnapshotOperationOutcome.UNCERTAIN,
                snapshot_operation_id=identity.snapshot_operation_id,
                submission_state=HostSubmissionState.ABSENT,
            )

        def ensure_pre_update_snapshot_submitted(self, **kwargs):
            raise AssertionError("stale authority must refuse submission")

        def seal_operation_never_submitted(self, **kwargs):
            self.seal_calls += 1
            return HostSnapshotResult(
                outcome=SnapshotOperationOutcome.NOT_SUBMITTED,
                snapshot_operation_id=identity.snapshot_operation_id,
                submission_state=HostSubmissionState.INTENT,
            )

    host = TransientSeal()
    result = PackageUpdateSnapshotOrchestrator(
        authority, host
    ).ensure_job_owned_snapshot(job.job_id)
    assert host.seal_calls == 1
    assert result.outcome is SnapshotOperationOutcome.UNCERTAIN
    assert store.package_update_job(job.job_id).status is PackageUpdateJobStatus.ACTIVE


def test_snapshot_host_control_rejects_contradictory_cross_fields() -> None:
    channel = _channel(None)
    operation_id = str(uuid.uuid4())
    base = {
        "response_version": 1,
        "ok": True,
        "snapshot_operation_id": operation_id,
        "outcome": "uncertain",
    }
    contradictions = (
        {"submission_state": "intent", "task_upid": UPID},
        {"submission_state": "absent", "task_upid": UPID},
        {"submission_state": "task_known"},
        {"submission_state": "sealed_not_submitted", "task_upid": UPID},
        {"submission_state": "sealed_not_submitted", "outcome": "completed"},
        {"submission_state": "terminal", "outcome": "not_submitted"},
        {
            "submission_state": "terminal",
            "task_upid": UPID,
            "task": {"upid": UPID, "status": "running"},
        },
    )
    for fields in contradictions:
        parsed = channel._parse_payload({**base, **fields}, operation_id)
        assert parsed.outcome is SnapshotOperationOutcome.UNCERTAIN
        assert parsed.submission_state is None
        assert "contradictory" in str(parsed.reason)


def test_snapshot_ssh_argv_has_bounded_connect_and_liveness_options() -> None:
    channel = _channel(None)
    assert channel._argv() == (
        "ssh",
        "-T",
        "-p",
        "22",
        "-i",
        "/etc/hubinet-ops/snapshot-key",
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "UserKnownHostsFile=/etc/hubinet-ops/known_hosts",
        "-o",
        "PasswordAuthentication=no",
        "-o",
        "KbdInteractiveAuthentication=no",
        "-o",
        "ForwardAgent=no",
        "-o",
        "ClearAllForwardings=yes",
        "-o",
        "ConnectTimeout=30",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=2",
        "hubinet-snapshot@pve.example.internal",
    )


def test_task_known_snapshot_success_with_stale_authority_is_retained_not_confirmed(
    tmp_path: Path,
) -> None:
    from tests.test_package_snapshot_helper import helper as snapshot_helper

    store, authority, job, identity, ownership, pve, journal, _ = _dark_system(
        tmp_path
    )
    authority.record_package_update_preflight_passed(job.job_id)
    job = authority.record_package_update_snapshot_intent(job.job_id)
    pve.on_submit = lambda fake: fake.snapshots.append(
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
    channel = _dark_channel(pve, journal)
    submitted = channel.ensure_pre_update_snapshot_submitted(
        snapshot_operation_id=identity.snapshot_operation_id,
        snapshot_name=identity.snapshot_name,
        vmid=job.expected_vmid,
        expected_node=job.expected_node_name,
        ownership=ownership,
    )
    assert submitted.submission_state is HostSubmissionState.TASK_KNOWN
    assert pve.submissions == 1
    _break_incarnation_continuity_at_the_same_locator(store, authority)
    other_resource, _, other_approval = _add_approved_resource(store, authority)

    result = PackageUpdateSnapshotOrchestrator(
        authority, channel
    ).ensure_job_owned_snapshot(job.job_id)

    assert result.outcome is SnapshotOperationOutcome.COMPLETED
    retained = store.package_update_job(job.job_id)
    assert retained.status is PackageUpdateJobStatus.BLOCKED
    assert retained.checkpoint is PackageUpdateCheckpoint.SNAPSHOT_MAY_HAVE_STARTED
    assert retained.snapshot_task_upid is not None
    assert retained.snapshot_confirmed_at is None
    assert retained.snapshot_name == identity.snapshot_name
    assert pve.submissions == 1
    assert store.list_package_update_job_events(job.job_id)[-1].event_type is (
        PackageUpdateEventType.SNAPSHOT_RETAINED_AUTHORITY_STALE
    )
    listing = parse_canonical_snapshot_listing(
        snapshot_helper.list_snapshots(pve, job.expected_vmid, job.expected_node_name)
    )
    with pytest.raises(AuthorityConflict, match="terminal"):
        authority.select_package_update_rollback_target(job.job_id, listing)
    assert _issue(authority, other_resource, other_approval).status is (
        PackageUpdateJobStatus.ACTIVE
    )


def test_taskless_snapshot_success_with_stale_authority_is_retained(
    tmp_path: Path,
) -> None:
    from tests.test_package_snapshot_helper import helper as snapshot_helper

    store, authority, job, identity, ownership, pve, journal, _ = _dark_system(
        tmp_path
    )
    authority.record_package_update_preflight_passed(job.job_id)
    job = authority.record_package_update_snapshot_intent(job.job_id)
    pve.submit_upid = "not-a-upid"
    pve.on_submit = lambda fake: fake.snapshots.append(
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
    channel = _dark_channel(pve, journal)
    submitted = channel.ensure_pre_update_snapshot_submitted(
        snapshot_operation_id=identity.snapshot_operation_id,
        snapshot_name=identity.snapshot_name,
        vmid=job.expected_vmid,
        expected_node=job.expected_node_name,
        ownership=ownership,
    )
    assert submitted.submission_state is HostSubmissionState.SUBMITTED
    assert pve.submissions == 1
    _break_incarnation_continuity_at_the_same_locator(store, authority)

    result = PackageUpdateSnapshotOrchestrator(
        authority, channel
    ).ensure_job_owned_snapshot(job.job_id)

    assert result.outcome is SnapshotOperationOutcome.COMPLETED
    retained = store.package_update_job(job.job_id)
    assert retained.status is PackageUpdateJobStatus.BLOCKED
    assert retained.snapshot_task_upid is None
    assert retained.snapshot_confirmed_at is None
    assert pve.submissions == 1


def test_stale_authority_cannot_terminalize_ambiguous_snapshot_success(
    tmp_path: Path,
) -> None:
    _, store, authority, _, job, identity, ownership = _prepared(tmp_path)
    _break_incarnation_continuity_at_the_same_locator(store, authority)
    ambiguous = (
        _current_entry(),
        _owned_entry(ownership, identity.snapshot_name),
        _owned_entry(ownership, identity.snapshot_name),
    )

    with pytest.raises(AuthorityConflict, match="duplicate"):
        authority.block_package_update_after_snapshot_success_with_stale_authority(
            job.job_id, ambiguous
        )
    assert store.package_update_job(job.job_id).status is PackageUpdateJobStatus.ACTIVE


def test_stale_authority_cannot_terminalize_an_incomplete_snapshot(
    tmp_path: Path,
) -> None:
    _, store, authority, _, job, identity, ownership = _prepared(tmp_path)
    _break_incarnation_continuity_at_the_same_locator(store, authority)
    incomplete = replace(
        _owned_entry(ownership, identity.snapshot_name), incomplete=True
    )

    with pytest.raises(AuthorityConflict, match="incomplete"):
        authority.block_package_update_after_snapshot_success_with_stale_authority(
            job.job_id, (_current_entry(), incomplete)
        )
    fenced = store.package_update_job(job.job_id)
    assert fenced.status is PackageUpdateJobStatus.ACTIVE
    assert fenced.snapshot_confirmed_at is None


def test_current_authority_refuses_stale_success_terminalization_and_can_confirm(
    tmp_path: Path,
) -> None:
    from tests.test_package_plan_approval import _successful_plan
    from tests.test_package_scan_authority import _packages

    _, store, authority, resource, job, identity, ownership = _prepared(tmp_path)
    observed = _canonical(ownership, identity)

    drifted = tuple(
        replace(package, candidate_version=package.candidate_version + "+drift")
        for package in _packages()
    )
    _successful_plan(authority, resource.resource_id, drifted)
    with pytest.raises(AuthorityConflict):
        authority.confirm_package_update_snapshot(job.job_id, observed)
    # A later exact scan restores the job's authority before the resolver.
    _successful_plan(authority, resource.resource_id)

    blocked, unchanged = (
        authority.block_package_update_after_snapshot_success_with_stale_authority(
            job.job_id, observed
        )
    )
    assert blocked is False
    assert unchanged.status is PackageUpdateJobStatus.ACTIVE
    confirmed = authority.confirm_package_update_snapshot(job.job_id, observed)
    assert confirmed.checkpoint is PackageUpdateCheckpoint.SNAPSHOT_CONFIRMED


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
    frozen-node PVE read fails. The transient pre-submit journal observation
    routes the backend into a durable host seal, and only that seal permits
    terminalization instead of holding the global slot forever.
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
        == "sealed_not_submitted"
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
    assert blocked.terminal_reason == (
        "host durably sealed this snapshot operation before submission"
    )
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
        authority.resolve_pre_submission_block(
            job.job_id,
            lambda: (
                HostSubmissionState.SEALED_NOT_SUBMITTED,
                "host durably sealed submission",
                None,
            ),
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
        authority.resolve_pre_submission_block(
            job.job_id,
            lambda: (HostSubmissionState.SEALED_NOT_SUBMITTED, "too early", None),
        )
    authority.record_package_update_preflight_passed(job.job_id)
    with pytest.raises(AuthorityConflict, match="not inside a snapshot operation"):
        authority.resolve_pre_submission_block(
            job.job_id,
            lambda: (
                HostSubmissionState.SEALED_NOT_SUBMITTED,
                "still too early",
                None,
            ),
        )

    # And once terminal it cannot be reused.
    authority.record_package_update_snapshot_intent(job.job_id)
    blocked, _ = authority.resolve_pre_submission_block(
        job.job_id,
        lambda: (HostSubmissionState.SEALED_NOT_SUBMITTED, "blocked", None),
    )
    assert blocked is True
    assert store.package_update_job(job.job_id).status is (
        PackageUpdateJobStatus.BLOCKED
    )
    with pytest.raises(AuthorityConflict, match="terminal"):
        authority.resolve_pre_submission_block(
            job.job_id,
            lambda: (HostSubmissionState.SEALED_NOT_SUBMITTED, "again", None),
        )


def test_a_confirmed_snapshot_can_never_be_released_as_unsubmitted(
    tmp_path: Path,
) -> None:
    _, store, authority, _, job, identity, ownership = _prepared(tmp_path)
    authority.confirm_package_update_snapshot(
        job.job_id, _canonical(ownership, identity)
    )
    with pytest.raises(AuthorityConflict, match="not inside a snapshot operation"):
        authority.resolve_pre_submission_block(
            job.job_id,
            lambda: (
                HostSubmissionState.SEALED_NOT_SUBMITTED,
                "host durably sealed submission",
                None,
            ),
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


def test_operation_in_progress_never_parses_as_a_non_submission_proof() -> None:
    """A held per-VMID lease is UNCERTAIN, never absent/intent/not_submitted.

    `operation_in_progress` is raised by both the mutating operation and the
    read-only inspection when the host's per-VMID mutation lease is already
    held -- exactly the case where a remote submission-only helper may still
    be alive and between its own durable phases. It must never be parsed as
    proof of anything: not NOT_SUBMITTED, and no `submission_state` value a
    pre-submission block could ever act on.
    """

    from app.package_update_snapshot import HostSubmissionState
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
    parsed = channel._parse_payload(
        {
            "response_version": 1,
            "ok": False,
            "snapshot_operation_id": operation_id,
            "error": {
                "classification": "operation_in_progress",
                "message": "another snapshot operation holds this guest's lease",
            },
        },
        operation_id,
    )
    assert parsed.outcome is SnapshotOperationOutcome.UNCERTAIN
    assert parsed.submission_state is None
    assert parsed.submission_state not in (
        HostSubmissionState.ABSENT,
        HostSubmissionState.INTENT,
    )


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
    staged = orchestrator._apply_host_result(job, identity, ownership, result)
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

    Absence stays transient; the host-control fake must durably seal before
    the authority transition releases the job.
    """

    _, store, authority, resource, _, approval = _approved_system(tmp_path)
    job = _issue(authority, resource, approval)
    identity = authority.package_update_snapshot_identity(job.job_id)
    host = FakeHostControl(
        HostSnapshotResult(
            outcome=SnapshotOperationOutcome.NOT_SUBMITTED,
            snapshot_operation_id=identity.snapshot_operation_id,
            reason="transient host journal evidence is pre-submission",
        )
    )
    result = PackageUpdateSnapshotOrchestrator(
        authority, host
    ).ensure_job_owned_snapshot(job.job_id)

    assert result.outcome is SnapshotOperationOutcome.NOT_SUBMITTED
    assert store.package_update_job(job.job_id).status is (
        PackageUpdateJobStatus.BLOCKED
    )


# ===========================================================================
# The submission critical section: authority -> commit -> PVE-submit race
#
# The write-ahead checkpoint alone does not close the whole seam: another
# Hubinet writer can invalidate this job's authority AFTER it was proved and
# BEFORE a NEW PVE submission is actually sent, in the gap between the intent
# transaction and the host-control call. execute_snapshot_submission_if_current
# closes it by re-proving authority and calling the submission-only host
# operation inside the SAME transaction. These tests prove the actual race is
# closed, not merely that the predicate exists -- and that recovering
# evidence about an operation that may already have been submitted never
# needs, and never waits on, that same lock.
# ===========================================================================


def test_replacement_after_intent_commit_releases_the_slot_via_a_seal(
    tmp_path: Path,
) -> None:
    """A. The exact remaining race, at the Python boundary.

    Discovery reconciliation invalidates current Hubinet authority strictly
    AFTER the write-ahead intent commits and BEFORE the submission critical
    section runs. The host is never even asked whether it could submit -- a
    stale authority context always refuses a NEW submission. But because this
    resource/source is gone or replaced for good, every future retry would
    repeat that identical refusal, so a permanently stale authority context
    must not fence the job's global slot forever: a host seal, written under
    the same lease as submission strictly after the refusal, is what safely
    releases it.
    """

    _, store, authority, resource, job, identity, ownership = _prepared(tmp_path)
    # Break THIS job's own resource before a second, unrelated resource
    # exists -- otherwise `_break_incarnation_continuity_at_the_same_locator`
    # (which always targets `store.list_resources()[0]`) could break the
    # wrong one.
    _break_incarnation_continuity_at_the_same_locator(store, authority)
    other_resource, _, other_approval = _add_approved_resource(store, authority)

    host = FakeHostControl()
    orchestrator = PackageUpdateSnapshotOrchestrator(authority, host)

    result = orchestrator.ensure_job_owned_snapshot(job.job_id)

    # One transient read routes into one durable seal under the authority
    # writer lock. Neither call asks the host to submit.
    assert len(host.inspect_calls) == 1
    assert len(host.seal_calls) == 1
    assert host.create_calls == []

    assert result.outcome is SnapshotOperationOutcome.NOT_SUBMITTED
    blocked = store.package_update_job(job.job_id)
    assert blocked.status is PackageUpdateJobStatus.BLOCKED
    assert blocked.checkpoint is PackageUpdateCheckpoint.SNAPSHOT_MAY_HAVE_STARTED
    assert blocked.snapshot_task_upid is None
    assert blocked.snapshot_confirmed_at is None
    assert blocked.terminal_reason
    events = store.list_package_update_job_events(job.job_id)
    assert (
        events[-1].event_type
        is PackageUpdateEventType.SNAPSHOT_BLOCKED_BEFORE_SUBMISSION
    )

    # Startup recovery leaves the terminal job exactly as it is.
    before_events = store.list_package_update_job_events(job.job_id)
    assert authority.recover_interrupted_package_update_jobs() == ()
    assert store.package_update_job(job.job_id) == blocked
    assert store.list_package_update_job_events(job.job_id) == before_events

    # The global destructive slot is free again -- proved on a different
    # resource, since this test's own staleness mechanism (reconciliation
    # cycling the resource away and back) leaves THIS resource transitionally
    # non-active in its own right, which is not what is being proved here.
    successor = _issue(authority, other_resource, other_approval)
    assert successor.status is PackageUpdateJobStatus.ACTIVE
    assert successor.job_id != job.job_id


def test_replacement_after_intent_commit_releases_the_slot_through_the_real_dark_boundary(
    tmp_path: Path,
) -> None:
    """A. The same witness through the real dark boundary.

    Same VMID, same node, same LXC, still running -- every live PVE fact a
    host helper could independently verify stays exactly what the job froze
    at issuance. This is precisely the case a check-then-commit race would
    let through, and precisely why VMID/node/type/status are not treated as
    incarnation proof anywhere in this stage. Zero PVE submissions happen at
    any point, yet the job still reaches a safe terminal state.
    """

    store, authority, job, identity, ownership, pve, journal, _ = _dark_system(
        tmp_path
    )
    authority.record_package_update_preflight_passed(job.job_id)
    job = authority.record_package_update_snapshot_intent(job.job_id)
    assert job.checkpoint is PackageUpdateCheckpoint.SNAPSHOT_MAY_HAVE_STARTED

    assert pve.vmid == job.expected_vmid
    assert pve.node == job.expected_node_name
    assert pve.resource_type == "lxc"
    assert pve.status == "running"

    # Break THIS job's own resource before a second, unrelated resource
    # exists -- otherwise `_break_incarnation_continuity_at_the_same_locator`
    # (which always targets `store.list_resources()[0]`) could break the
    # wrong one.
    _break_incarnation_continuity_at_the_same_locator(store, authority)
    other_resource, _, other_approval = _add_approved_resource(store, authority)

    orchestrator = PackageUpdateSnapshotOrchestrator(
        authority, _dark_channel(pve, journal)
    )
    result = orchestrator.ensure_job_owned_snapshot(job.job_id)

    assert pve.submissions == 0
    assert not any(argv[1] == "create" for argv in pve.argvs)
    assert result.outcome is SnapshotOperationOutcome.NOT_SUBMITTED
    blocked = store.package_update_job(job.job_id)
    assert blocked.status is PackageUpdateJobStatus.BLOCKED
    assert blocked.checkpoint is PackageUpdateCheckpoint.SNAPSHOT_MAY_HAVE_STARTED
    assert blocked.snapshot_task_upid is None
    assert blocked.snapshot_confirmed_at is None

    # The global destructive slot is free again -- proved on a different
    # resource; see the sibling Python-boundary test for why.
    successor = _issue(authority, other_resource, other_approval)
    assert successor.status is PackageUpdateJobStatus.ACTIVE
    assert successor.job_id != job.job_id


def test_writer_cannot_interleave_inside_the_submission_critical_section(
    tmp_path: Path,
) -> None:
    """B. The actual race proof for the NEW submission critical section.

    A seam fires inside execute_snapshot_submission_if_current's own
    transaction, immediately after the authority proof and before the
    submission callback runs -- exactly where an interleaving writer could
    invalidate the job between them. From a second, genuinely separate
    connection, writing to this store must be impossible for the whole of
    that window: the critical section holds the database's one writer lock
    across both the proof and the submission it authorizes.
    """

    import sqlite3 as _sqlite3

    _, store, authority, resource, job, identity, ownership = _prepared(tmp_path)

    attempted: list[str] = []

    def seam(connection, *, job_id):
        other = _sqlite3.connect(store.path, timeout=0.1, isolation_level=None)
        try:
            with pytest.raises(_sqlite3.OperationalError, match="locked"):
                other.execute("BEGIN IMMEDIATE")
            attempted.append(job_id)
        finally:
            other.close()

    authority._after_package_update_authority_proof = seam

    result = authority.execute_snapshot_submission_if_current(
        job.job_id, lambda: "submitted-under-lock"
    )

    assert result == "submitted-under-lock"
    assert attempted == [job.job_id]


# ---------------------------------------------------------------------------
# P3-5: AuthorityConflict is overloaded no longer. execute_snapshot_submission_
# if_current raises SnapshotSubmissionRefusedBeforeCallback ONLY for the one
# case structurally guaranteed to mean the submission callback never ran --
# current authority itself proved false. A terminal job, a wrong checkpoint,
# or any other lifecycle conflict remain ordinary AuthorityConflict and say
# nothing about whether the host was called. An exception submit() itself
# raises must never be recast as either type.
# ---------------------------------------------------------------------------


def test_stale_authority_refusal_is_the_specific_pre_callback_type(
    tmp_path: Path,
) -> None:
    _, store, authority, resource, job, identity, ownership = _prepared(tmp_path)
    _break_incarnation_continuity_at_the_same_locator(store, authority)

    calls: list[str] = []
    with pytest.raises(SnapshotSubmissionRefusedBeforeCallback):
        authority.execute_snapshot_submission_if_current(
            job.job_id, lambda: calls.append("submitted") or "unreachable"
        )

    assert calls == []
    # The specific type IS an AuthorityConflict, so existing generic handlers
    # still catch it -- but it must never be the bare base type.
    assert isinstance(SnapshotSubmissionRefusedBeforeCallback("x"), AuthorityConflict)


def test_terminal_job_refusal_is_generic_not_the_pre_callback_type(
    tmp_path: Path,
) -> None:
    _, store, authority, resource, job, identity, ownership = _prepared(tmp_path)

    def seal():
        return (
            HostSubmissionState.SEALED_NOT_SUBMITTED,
            "sealed for this test",
            "evidence",
        )

    blocked, _ = authority.resolve_pre_submission_block(job.job_id, seal)
    assert blocked
    assert store.package_update_job(job.job_id).status is PackageUpdateJobStatus.BLOCKED

    calls: list[str] = []
    with pytest.raises(AuthorityConflict, match="terminal") as excinfo:
        authority.execute_snapshot_submission_if_current(
            job.job_id, lambda: calls.append("submitted") or "unreachable"
        )

    assert calls == []
    # Generic lifecycle conflict must NOT be misclassified as the specific
    # "current authority refused before any host call" proof.
    assert type(excinfo.value) is AuthorityConflict
    assert not isinstance(excinfo.value, SnapshotSubmissionRefusedBeforeCallback)


def test_wrong_checkpoint_refusal_is_generic_not_the_pre_callback_type(
    tmp_path: Path,
) -> None:
    from tests.test_package_snapshot_helper import helper as snapshot_helper

    store, authority, job, identity, ownership, pve, journal, upid = _dark_system(
        tmp_path
    )
    pve.on_submit = lambda fake: fake.snapshots.append(
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
    confirmed = orchestrator.ensure_job_owned_snapshot(job.job_id)
    assert confirmed.outcome is SnapshotOperationOutcome.COMPLETED
    assert (
        store.package_update_job(job.job_id).checkpoint
        is PackageUpdateCheckpoint.SNAPSHOT_CONFIRMED
    )

    # The job is still ACTIVE, but its checkpoint has moved past the write-
    # ahead boundary this method requires -- a second call must refuse
    # generically, never claim the specific pre-callback proof.
    calls: list[str] = []
    with pytest.raises(AuthorityConflict, match="not inside a snapshot") as excinfo:
        authority.execute_snapshot_submission_if_current(
            job.job_id, lambda: calls.append("submitted") or "unreachable"
        )

    assert calls == []
    assert type(excinfo.value) is AuthorityConflict
    assert not isinstance(excinfo.value, SnapshotSubmissionRefusedBeforeCallback)


def test_an_exception_after_the_callback_begins_is_never_recast(
    tmp_path: Path,
) -> None:
    _, store, authority, resource, job, identity, ownership = _prepared(tmp_path)

    class _HostExploded(RuntimeError):
        pass

    def submit():
        raise _HostExploded("host round trip failed mid-flight")

    with pytest.raises(_HostExploded):
        authority.execute_snapshot_submission_if_current(job.job_id, submit)

    # Current authority held and the callback genuinely ran, so this must
    # propagate completely unchanged -- never AuthorityConflict, and
    # certainly never the specific pre-callback refusal type.
    still_active = store.package_update_job(job.job_id)
    assert still_active.status is PackageUpdateJobStatus.ACTIVE
    assert still_active.checkpoint is PackageUpdateCheckpoint.SNAPSHOT_MAY_HAVE_STARTED


def test_reentry_with_intent_only_and_stale_authority_terminalizes_via_a_seal(
    tmp_path: Path,
) -> None:
    """C. Re-entry: the host journal can still be durably sealed.

    One prior attempt reaches the host and durably records intent, but a
    concurrent PVE operation blocks it before it ever submits -- the journal
    never advances past `intent`. On retry, current authority has since gone
    stale, so a NEW submission is refused, never by trying to prove this is
    "the same LXC" from live PVE facts. The retry durably seals the operation
    under the host lease, then safely terminalizes rather than fencing forever
    on an authority context that will never become current again.
    """

    store, authority, job, identity, ownership, pve, journal, _ = _dark_system(
        tmp_path
    )
    authority.record_package_update_preflight_passed(job.job_id)
    job = authority.record_package_update_snapshot_intent(job.job_id)

    pve.lock = "snapshot"
    orchestrator = PackageUpdateSnapshotOrchestrator(
        authority, _dark_channel(pve, journal)
    )
    first = orchestrator.ensure_job_owned_snapshot(job.job_id)
    assert first.outcome is SnapshotOperationOutcome.UNCERTAIN
    assert pve.submissions == 0
    from tests.test_package_snapshot_helper import helper as snapshot_helper

    assert (
        snapshot_helper.OperationJournal(journal.directory)
        .read(identity.snapshot_operation_id)["phase"]
        == "intent"
    )

    pve.lock = None
    # Break THIS job's own resource before a second, unrelated resource
    # exists -- otherwise `_break_incarnation_continuity_at_the_same_locator`
    # (which always targets `store.list_resources()[0]`) could break the
    # wrong one.
    _break_incarnation_continuity_at_the_same_locator(store, authority)
    other_resource, _, other_approval = _add_approved_resource(store, authority)

    result = orchestrator.ensure_job_owned_snapshot(job.job_id)

    assert pve.submissions == 0
    assert not any(argv[1] == "create" for argv in pve.argvs)
    assert result.outcome is SnapshotOperationOutcome.NOT_SUBMITTED
    blocked = store.package_update_job(job.job_id)
    assert blocked.status is PackageUpdateJobStatus.BLOCKED
    assert blocked.checkpoint is PackageUpdateCheckpoint.SNAPSHOT_MAY_HAVE_STARTED
    assert blocked.snapshot_task_upid is None

    # The global destructive slot is free again -- proved on a different
    # resource; see the sibling Python-boundary test for why.
    successor = _issue(authority, other_resource, other_approval)
    assert successor.status is PackageUpdateJobStatus.ACTIVE
    assert successor.job_id != job.job_id


def test_the_initial_pre_refusal_inspection_is_never_trusted_after_refusal(
    tmp_path: Path,
) -> None:
    """The mandatory race witness for the post-refusal liveness fix.

    Invocation B reads transient `absent`. Before B's seal acquires the host
    lease, invocation A submits and leaves `task_known`. The seal result, not
    B's initial read, must decide release and keep the job fenced.
    """

    _, store, authority, resource, job, identity, ownership = _prepared(tmp_path)
    # Break THIS job's own resource before a second, unrelated resource
    # exists -- otherwise `_break_incarnation_continuity_at_the_same_locator`
    # (which always targets `store.list_resources()[0]`) could break the
    # wrong one.
    _break_incarnation_continuity_at_the_same_locator(store, authority)
    other_resource, _, other_approval = _add_approved_resource(store, authority)

    before = HostSnapshotResult(
        outcome=SnapshotOperationOutcome.UNCERTAIN,
        snapshot_operation_id=identity.snapshot_operation_id,
        submission_state=HostSubmissionState.ABSENT,
    )
    after = HostSnapshotResult(
        outcome=SnapshotOperationOutcome.UNCERTAIN,
        snapshot_operation_id=identity.snapshot_operation_id,
        task_upid=UPID,
        submission_state=HostSubmissionState.TASK_KNOWN,
        reason="another invocation's authorized submission is already in flight",
    )

    class _RacingHostControl:
        """Simulates a concurrent, already-authorized submission by another
        invocation landing between this invocation's two reads."""

        def __init__(self) -> None:
            self.inspect_calls = 0
            self.create_calls = 0
            self.seal_calls = 0

        def inspect_job_snapshot_state(self, **kwargs) -> HostSnapshotResult:
            self.inspect_calls += 1
            return before if self.inspect_calls == 1 else after

        def seal_operation_never_submitted(self, **kwargs) -> HostSnapshotResult:
            self.seal_calls += 1
            return after

        def ensure_pre_update_snapshot_submitted(self, **kwargs) -> HostSnapshotResult:
            self.create_calls += 1
            raise AssertionError(
                "authority was stale: the host must never be asked to submit"
            )

    host = _RacingHostControl()
    result = PackageUpdateSnapshotOrchestrator(
        authority, host, task_poll_timeout_seconds=0.0
    ).ensure_job_owned_snapshot(job.job_id)

    # One initial read, one seal attempt, and one already-expired task poll.
    assert host.inspect_calls == 2
    assert host.seal_calls == 1
    assert host.create_calls == 0

    # The OLD `absent` read never releases the job: it must recover the task
    # the fresh read actually found, not fabricate a "never submitted" proof
    # from stale evidence.
    assert result.outcome is SnapshotOperationOutcome.UNCERTAIN
    fenced = store.package_update_job(job.job_id)
    assert fenced.status is PackageUpdateJobStatus.ACTIVE
    assert fenced.checkpoint is PackageUpdateCheckpoint.SNAPSHOT_MAY_HAVE_STARTED
    assert fenced.snapshot_task_upid == UPID
    with pytest.raises(AuthorityConflict, match="global slot"):
        _issue(authority, other_resource, other_approval)


def test_the_race_is_closed_through_the_real_dark_boundary(tmp_path: Path) -> None:
    """The same race witness, but invocation A's submission is real.

    A concurrent invocation is simulated by submitting through the SAME real
    dark channel (the same PVE fake and the same on-disk journal) directly,
    in between this invocation's own pre- and post-refusal reads -- proving
    the fresh re-read observes genuinely durable host state, not a value
    contrived at the Python level.
    """

    store, authority, job, identity, ownership, pve, journal, upid = _dark_system(
        tmp_path
    )
    authority.record_package_update_preflight_passed(job.job_id)
    job = authority.record_package_update_snapshot_intent(job.job_id)
    _break_incarnation_continuity_at_the_same_locator(store, authority)

    real_channel = _dark_channel(pve, journal)

    class _RaceThroughRealHelper:
        def __init__(self) -> None:
            self.inspect_calls = 0
            self.seal_calls = 0

        def inspect_job_snapshot_state(self, **kwargs) -> HostSnapshotResult:
            self.inspect_calls += 1
            return real_channel.inspect_job_snapshot_state(**kwargs)

        def seal_operation_never_submitted(self, **kwargs) -> HostSnapshotResult:
            self.seal_calls += 1
            # Invocation A wins the host lease and submits for real before B's
            # seal attempts to acquire the same lease.
            real_channel.ensure_pre_update_snapshot_submitted(**kwargs)
            return real_channel.seal_operation_never_submitted(**kwargs)

        def ensure_pre_update_snapshot_submitted(self, **kwargs) -> HostSnapshotResult:
            raise AssertionError(
                "authority was stale: the host must never be asked to submit"
            )

    host = _RaceThroughRealHelper()
    result = PackageUpdateSnapshotOrchestrator(
        authority, host, task_poll_timeout_seconds=0.0
    ).ensure_job_owned_snapshot(job.job_id)

    assert host.inspect_calls == 2
    assert host.seal_calls == 1
    assert pve.submissions == 1
    assert result.outcome is SnapshotOperationOutcome.UNCERTAIN
    fenced = store.package_update_job(job.job_id)
    assert fenced.status is PackageUpdateJobStatus.ACTIVE
    assert fenced.checkpoint is PackageUpdateCheckpoint.SNAPSHOT_MAY_HAVE_STARTED
    assert fenced.snapshot_task_upid == upid


def test_an_unreadable_post_refusal_inspection_stays_fenced(tmp_path: Path) -> None:
    """D + unknown-evidence guard: the fresh re-read itself can fail closed.

    If the post-refusal read cannot prove anything -- a transport failure,
    a malformed/unknown submission_state, a corrupt journal -- that must
    never be treated as proof of non-submission. The job stays active and
    fenced, never blocked and never resubmitted.
    """

    _, store, authority, resource, job, identity, ownership = _prepared(tmp_path)
    # Break THIS job's own resource before a second, unrelated resource
    # exists -- otherwise `_break_incarnation_continuity_at_the_same_locator`
    # (which always targets `store.list_resources()[0]`) could break the
    # wrong one.
    _break_incarnation_continuity_at_the_same_locator(store, authority)
    other_resource, _, other_approval = _add_approved_resource(store, authority)

    before = HostSnapshotResult(
        outcome=SnapshotOperationOutcome.UNCERTAIN,
        snapshot_operation_id=identity.snapshot_operation_id,
        submission_state=HostSubmissionState.ABSENT,
    )

    class _FailingSeal:
        def __init__(self) -> None:
            self.inspect_calls = 0
            self.create_calls = 0
            self.seal_calls = 0

        def inspect_job_snapshot_state(self, **kwargs) -> HostSnapshotResult:
            self.inspect_calls += 1
            return before

        def seal_operation_never_submitted(self, **kwargs) -> HostSnapshotResult:
            self.seal_calls += 1
            raise RuntimeError("transport lost the seal response")

        def ensure_pre_update_snapshot_submitted(self, **kwargs) -> HostSnapshotResult:
            self.create_calls += 1
            raise AssertionError(
                "authority was stale: the host must never be asked to submit"
            )

    host = _FailingSeal()
    result = PackageUpdateSnapshotOrchestrator(
        authority, host
    ).ensure_job_owned_snapshot(job.job_id)

    assert host.inspect_calls == 1
    assert host.seal_calls == 1
    assert host.create_calls == 0
    assert result.outcome is SnapshotOperationOutcome.UNCERTAIN
    fenced = store.package_update_job(job.job_id)
    assert fenced.status is PackageUpdateJobStatus.ACTIVE
    assert fenced.checkpoint is PackageUpdateCheckpoint.SNAPSHOT_MAY_HAVE_STARTED
    with pytest.raises(AuthorityConflict, match="global slot"):
        _issue(authority, other_resource, other_approval)


def test_reentry_after_submission_recovers_despite_stale_authority(
    tmp_path: Path,
) -> None:
    """D. Re-entry: the host journal proves a submission may already exist.

    Once the host's durable journal is past `intent`, staleness must never
    cause that evidence to be discarded: the retry must recover/observe,
    never resubmit, and keep the job fenced rather than releasing it on a
    stale authority context it never even had to consult.
    """

    store, authority, job, identity, ownership, pve, journal, upid = _dark_system(
        tmp_path
    )
    authority.record_package_update_preflight_passed(job.job_id)
    job = authority.record_package_update_snapshot_intent(job.job_id)

    pve.task_sequence = [{"upid": upid, "status": "running"}]
    orchestrator = PackageUpdateSnapshotOrchestrator(
        authority, _dark_channel(pve, journal), task_poll_timeout_seconds=0.0
    )
    first = orchestrator.ensure_job_owned_snapshot(job.job_id)
    assert first.outcome is SnapshotOperationOutcome.UNCERTAIN
    assert pve.submissions == 1
    assert store.package_update_job(job.job_id).snapshot_task_upid == upid

    _break_incarnation_continuity_at_the_same_locator(store, authority)

    second = orchestrator.ensure_job_owned_snapshot(job.job_id)

    assert pve.submissions == 1
    assert second.outcome is SnapshotOperationOutcome.UNCERTAIN
    fenced = store.package_update_job(job.job_id)
    assert fenced.status is PackageUpdateJobStatus.ACTIVE
    assert fenced.checkpoint is PackageUpdateCheckpoint.SNAPSHOT_MAY_HAVE_STARTED
    assert fenced.snapshot_task_upid == upid


def test_crash_after_task_known_is_journaled_never_resubmits_and_recovers(
    tmp_path: Path,
) -> None:
    """E + F. Crash inside the submission critical section, past task_known.

    The host durably crosses its own `submitted -> task_known` boundary and
    answers successfully; the caller then crashes before it ever applies that
    answer or the authority transaction cleanly returns. After restart, the
    host's durable journal -- not backend memory -- proves a task is already
    known, so retry must reattach and poll/recover, never resubmit.
    """

    store, authority, job, identity, ownership, pve, journal, upid = _dark_system(
        tmp_path
    )
    authority.record_package_update_preflight_passed(job.job_id)
    job = authority.record_package_update_snapshot_intent(job.job_id)

    from tests.test_package_snapshot_helper import helper as snapshot_helper

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

    real_channel = _dark_channel(pve, journal)

    class _CrashAfterHostAnswers:
        """Simulates the caller dying after the host crossed task_known."""

        def ensure_pre_update_snapshot_submitted(self, **kwargs):
            real_channel.ensure_pre_update_snapshot_submitted(**kwargs)
            raise KeyboardInterrupt("caller died after task_known was journaled")

        def inspect_job_snapshot_state(self, **kwargs):
            return real_channel.inspect_job_snapshot_state(**kwargs)

    orchestrator = PackageUpdateSnapshotOrchestrator(
        authority, _CrashAfterHostAnswers(), task_poll_timeout_seconds=0.0
    )
    with pytest.raises(KeyboardInterrupt):
        orchestrator.ensure_job_owned_snapshot(job.job_id)

    assert pve.submissions == 1
    crashed = store.package_update_job(job.job_id)
    assert crashed.checkpoint is PackageUpdateCheckpoint.SNAPSHOT_MAY_HAVE_STARTED
    # The caller crashed before the authority transaction ever applied the
    # host's answer, so nothing about the task was recorded here yet.
    assert crashed.snapshot_task_upid is None
    assert (
        snapshot_helper.OperationJournal(journal.directory)
        .read(identity.snapshot_operation_id)["phase"]
        == "task_known"
    )

    # Retry with a healthy channel: the durable host journal proves a task is
    # already known, so this must reattach and recover, never resubmit.
    recovered_orchestrator = PackageUpdateSnapshotOrchestrator(
        authority, real_channel, task_poll_timeout_seconds=0.0
    )
    recovered = recovered_orchestrator.ensure_job_owned_snapshot(job.job_id)

    assert pve.submissions == 1
    assert recovered.outcome is SnapshotOperationOutcome.COMPLETED
    assert recovered.job.checkpoint is PackageUpdateCheckpoint.SNAPSHOT_CONFIRMED
    assert recovered.job.snapshot_task_upid == upid


def test_task_polling_never_holds_the_authority_writer_lock(tmp_path: Path) -> None:
    """G. The guard against "fixed the race by locking the whole database".

    A second, genuinely separate writer must be able to acquire its own
    BEGIN IMMEDIATE transaction WHILE this orchestrator is bounded-polling a
    known task -- proving the submission critical section's writer lock was
    already released before polling ever starts, and stays released for the
    whole of it. The SAME window also proves each individual
    `inspect_job_snapshot_state` call releases the host's own per-VMID
    mutation lease before returning: it is acquired and released once per
    bounded read, never held across the orchestrator's polling loop.
    """

    import sqlite3 as _sqlite3

    store, authority, job, identity, ownership, pve, journal, upid = _dark_system(
        tmp_path
    )
    authority.record_package_update_preflight_passed(job.job_id)
    job = authority.record_package_update_snapshot_intent(job.job_id)

    from tests.test_package_snapshot_helper import helper as snapshot_helper

    # The task is still running for the first read, so the poll loop must
    # genuinely iterate (and therefore genuinely call `sleep`) at least once
    # before the canonical evidence -- added below, from inside that sleep --
    # resolves it.
    pve.task_sequence = [
        {"upid": upid, "status": "running"},
        {"upid": upid, "status": "stopped", "exitstatus": "OK"},
    ]

    acquired: list[bool] = []
    lease_acquired: list[bool] = []

    def sleep_and_probe(_seconds: float) -> None:
        other = _sqlite3.connect(store.path, timeout=0.1, isolation_level=None)
        try:
            other.execute("BEGIN IMMEDIATE")
            acquired.append(True)
            other.commit()
        finally:
            other.close()
        # The prior inspect call's own VmidMutationLock must already be
        # released: a fresh acquisition here must not raise
        # `operation_in_progress`.
        with snapshot_helper.VmidMutationLock(job.expected_vmid, journal.directory):
            lease_acquired.append(True)
        pve.snapshots.append(
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
        authority,
        _dark_channel(pve, journal),
        sleep=sleep_and_probe,
        monotonic=lambda: 0.0,
        task_poll_interval_seconds=0.0,
    )

    result = orchestrator.ensure_job_owned_snapshot(job.job_id)

    assert acquired == [True]
    assert lease_acquired == [True]
    assert result.outcome is SnapshotOperationOutcome.COMPLETED
    assert pve.submissions == 1


# ===========================================================================
# A known task cannot become "more terminal": stop polling once it IS
# terminal, whatever the durable journal phase still claims.
# ===========================================================================


def _bounded_clock_sleep():
    """A `sleep` that also advances `monotonic`, so a regression to the old
    "keep polling while task_known+uncertain" behaviour degrades to a few
    extra bounded iterations instead of hanging the test suite for real
    seconds or looping until the full configured timeout elapses.
    """

    clock = [0.0]
    calls: list[float] = []

    def sleep(seconds: float) -> None:
        calls.append(seconds)
        clock[0] += 1000.0

    return sleep, calls, lambda: clock[0]


def test_a_running_task_keeps_polling_until_it_resolves(tmp_path: Path) -> None:
    from tests.test_package_snapshot_helper import helper as snapshot_helper

    store, authority, job, identity, ownership, pve, journal, upid = _dark_system(
        tmp_path
    )
    pve.task_sequence = [
        {"upid": upid, "status": "running"},
        {"upid": upid, "status": "stopped", "exitstatus": "OK"},
    ]
    sleep, sleep_calls, monotonic = _bounded_clock_sleep()

    def sleep_then_confirm(seconds: float) -> None:
        sleep(seconds)
        pve.snapshots.append(
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
        authority,
        _dark_channel(pve, journal),
        sleep=sleep_then_confirm,
        monotonic=monotonic,
        task_poll_timeout_seconds=800.0,
        task_poll_interval_seconds=2.0,
    )

    result = orchestrator.ensure_job_owned_snapshot(job.job_id)

    # A genuinely non-terminal task still requires exactly one poll wait.
    assert sleep_calls == [2.0]
    assert result.outcome is SnapshotOperationOutcome.COMPLETED


def test_terminal_ok_task_with_canonical_snapshot_completes(tmp_path: Path) -> None:
    from tests.test_package_snapshot_helper import helper as snapshot_helper

    store, authority, job, identity, ownership, pve, journal, upid = _dark_system(
        tmp_path
    )
    pve.task_sequence = [{"upid": upid, "status": "stopped", "exitstatus": "OK"}]
    pve.on_submit = lambda fake: fake.snapshots.append(
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
    sleep, sleep_calls, monotonic = _bounded_clock_sleep()

    orchestrator = PackageUpdateSnapshotOrchestrator(
        authority,
        _dark_channel(pve, journal),
        sleep=sleep,
        monotonic=monotonic,
        task_poll_timeout_seconds=800.0,
        task_poll_interval_seconds=2.0,
    )

    result = orchestrator.ensure_job_owned_snapshot(job.job_id)

    assert result.outcome is SnapshotOperationOutcome.COMPLETED
    assert result.job.checkpoint is PackageUpdateCheckpoint.SNAPSHOT_CONFIRMED
    assert sleep_calls == []


def test_terminal_ok_task_with_canonical_absence_stays_uncertain_without_polling_the_full_timeout(
    tmp_path: Path,
) -> None:
    """P3-8D. A known task that is already terminal cannot become "more
    terminal" by polling it again: the durable journal phase stays
    `task_known` forever once a task identity is captured, so the OLD
    `_pending()` -- which looked only at `submission_state`/`outcome` -- would
    keep sleeping and re-reading for up to the whole configured timeout
    whenever canonical evidence stayed absent. The fix must recognise the
    LIVE task is already terminal on the very first read and stop
    immediately, letting canonical evidence (absent) decide UNCERTAIN through
    the existing strict rules -- never fabricating failure from an absence.
    """

    store, authority, job, identity, ownership, pve, journal, upid = _dark_system(
        tmp_path
    )
    # Terminal, non-error task -- but no owned snapshot is ever added, so
    # canonical evidence stays "absent" no matter how many times it is read.
    pve.task_sequence = [{"upid": upid, "status": "stopped", "exitstatus": "OK"}]
    sleep, sleep_calls, monotonic = _bounded_clock_sleep()

    orchestrator = PackageUpdateSnapshotOrchestrator(
        authority,
        _dark_channel(pve, journal),
        sleep=sleep,
        monotonic=monotonic,
        task_poll_timeout_seconds=800.0,
        task_poll_interval_seconds=2.0,
    )

    result = orchestrator.ensure_job_owned_snapshot(job.job_id)

    assert result.outcome is SnapshotOperationOutcome.UNCERTAIN
    # The whole point: zero polling waits once the live task is observed
    # terminal, never up to the full configured timeout.
    assert sleep_calls == []
    assert store.package_update_job(job.job_id).snapshot_task_upid == upid
    assert store.package_update_job(job.job_id).status is PackageUpdateJobStatus.ACTIVE


class _FixedInspectionHostControl:
    """Every inspect call answers with the SAME fixed, already-terminal
    result -- enough to prove the poll loop's entry decision (whether an
    already-terminal task is still treated as pending) without needing this
    double to simulate multiple distinct iterations.
    """

    def __init__(self, inspection_result: HostSnapshotResult) -> None:
        self._inspection_result = inspection_result
        self.inspect_calls = 0

    def ensure_pre_update_snapshot_submitted(self, **kwargs) -> HostSnapshotResult:
        raise AssertionError("must never submit in this test")

    def inspect_job_snapshot_state(self, **kwargs) -> HostSnapshotResult:
        self.inspect_calls += 1
        return self._inspection_result

    def seal_operation_never_submitted(self, **kwargs) -> HostSnapshotResult:
        raise AssertionError("must never seal in this test")


def test_terminal_task_with_unknown_exit_status_stays_uncertain_without_polling(
    tmp_path: Path,
) -> None:
    """P3-8D (variant). `status == "stopped"` alone makes a task terminal
    even with no `exitstatus` at all -- classified `SnapshotTaskState.UNKNOWN`
    at the orchestrator/backend layer, never success, but still genuinely
    terminal. `_pending()` must stop on it immediately, exactly like a
    terminal OK task with absent canonical evidence, never waiting up to the
    full configured timeout.
    """

    _, store, authority, resource, job, identity, ownership = _prepared(tmp_path)

    task = classify_task_status({"upid": UPID, "status": "stopped"})
    assert task.terminal
    assert task.state is SnapshotTaskState.UNKNOWN

    inspection = HostSnapshotResult(
        outcome=SnapshotOperationOutcome.UNCERTAIN,
        snapshot_operation_id=identity.snapshot_operation_id,
        task_upid=UPID,
        task=task,
        snapshots=(),
        submission_state=HostSubmissionState.TASK_KNOWN,
        reason="canonical job-owned snapshot evidence: absent",
    )
    host = _FixedInspectionHostControl(inspection)
    sleep, sleep_calls, monotonic = _bounded_clock_sleep()

    orchestrator = PackageUpdateSnapshotOrchestrator(
        authority,
        host,
        sleep=sleep,
        monotonic=monotonic,
        task_poll_timeout_seconds=800.0,
        task_poll_interval_seconds=2.0,
    )

    result = orchestrator.ensure_job_owned_snapshot(job.job_id)

    assert result.outcome is SnapshotOperationOutcome.UNCERTAIN
    assert sleep_calls == []
    # One initial read decides it; the poll loop body is never entered.
    assert host.inspect_calls == 1


# ===========================================================================
# The pre-submission block critical section: durable host seal -> block
#
# A transient host read alone is not enough. The host seal and backend block
# happen while the same backend writer transaction remains open, while the
# seal itself is serialized against delayed submitters by the host's per-VMID
# lease. resolve_pre_submission_block is the mirror image of
# execute_snapshot_submission_if_current through the authority-store lock.
# ===========================================================================


def test_concurrent_terminalization_during_pre_submission_block_does_not_leak_a_raw_exception(
    tmp_path: Path,
) -> None:
    """P3-2. A concurrent, compliant invocation can win the pre-submission
    seal and terminalize the job while THIS invocation is still mid-flight
    with a stale in-memory `job` snapshot (still ACTIVE at the write-ahead
    checkpoint). This invocation's own retry into
    `InventoryAuthority.resolve_pre_submission_block` then re-reads the job
    fresh, finds it already terminal, and raises `AuthorityConflict` --
    which the orchestrator's `except Exception:` handler routes into
    `_uncertain`. That call's OWN durable write
    (`record_package_update_snapshot_uncertain`) hits the identical
    already-terminal precondition and used to raise a second, completely
    unhandled `AuthorityConflict` straight out of the public orchestration
    surface. The winning invocation's own terminal state must be left
    completely untouched either way.
    """

    _, store, authority, resource, job, identity, ownership = _prepared(tmp_path)

    class _NeverCalledHostControl:
        def ensure_pre_update_snapshot_submitted(self, **kwargs):
            raise AssertionError("must never submit")

        def inspect_job_snapshot_state(self, **kwargs):
            raise AssertionError("must never inspect in this test")

        def seal_operation_never_submitted(self, **kwargs):
            raise AssertionError(
                "the racing seal attempt must never reach the host: the "
                "durable job-terminal check must refuse first"
            )

    orchestrator = PackageUpdateSnapshotOrchestrator(
        authority, _NeverCalledHostControl()
    )

    # Another, compliant invocation wins the seal first and terminalizes the
    # job -- exactly as a concurrent orchestrator instance would.
    def winning_seal():
        return (
            HostSubmissionState.SEALED_NOT_SUBMITTED,
            "host durably sealed this snapshot operation before submission",
            "winning-evidence",
        )

    blocked, _ = authority.resolve_pre_submission_block(job.job_id, winning_seal)
    assert blocked
    winner = store.package_update_job(job.job_id)
    assert winner.status is PackageUpdateJobStatus.BLOCKED

    # This invocation's own view of `job` is now stale (still ACTIVE at
    # snapshot_may_have_started), exactly as it would be mid-flight.
    result = orchestrator._resolve_pre_submission_block(job, identity, ownership)

    assert result.outcome is SnapshotOperationOutcome.UNCERTAIN
    # The winning invocation's own terminal state is authoritative and must
    # be left completely untouched -- never rewritten, never reopened.
    assert result.job.status is PackageUpdateJobStatus.BLOCKED
    assert result.job.terminal_reason == winner.terminal_reason
    assert result.job.terminalized_at == winner.terminalized_at


def test_uncertain_reraises_when_the_job_is_unexpectedly_still_eligible(
    tmp_path: Path,
) -> None:
    """The concurrent-terminalization tolerance in `_uncertain` must not
    swallow a genuinely unexpected conflict: if the durable job re-read still
    shows it eligible (active, still at the write-ahead checkpoint), the
    original conflict was not this race, and must still fail closed exactly
    as before.
    """

    _, store, authority, resource, job, identity, ownership = _prepared(tmp_path)

    class _AlwaysConflicts:
        def record_package_update_snapshot_uncertain(self, job_id, reason):
            raise AuthorityConflict("simulated unrelated invariant conflict")

        def package_update_job(self, job_id):
            return authority.package_update_job(job_id)

    orchestrator = PackageUpdateSnapshotOrchestrator(_AlwaysConflicts(), object())

    with pytest.raises(AuthorityConflict, match="simulated unrelated"):
        orchestrator._uncertain(job.job_id, "irrelevant reason")


def test_block_wins_first_against_an_interleaving_submission_writer(
    tmp_path: Path,
) -> None:
    """A. The direct race proof for the NEW pre-submission block section.

    A seam fires inside resolve_pre_submission_block's own transaction,
    immediately after the durable host seal and before the backend block --
    exactly where an interleaving submission critical section could
    otherwise race it. From a second, genuinely separate connection, a
    competing writer must be unable to acquire the writer lock for the whole
    of that window.
    """

    import sqlite3 as _sqlite3

    _, store, authority, resource, job, identity, ownership = _prepared(tmp_path)

    attempted: list[str] = []
    seal_calls: list[str] = []

    def seam(connection, *, job_id):
        other = _sqlite3.connect(store.path, timeout=0.1, isolation_level=None)
        try:
            with pytest.raises(_sqlite3.OperationalError, match="locked"):
                other.execute("BEGIN IMMEDIATE")
            attempted.append(job_id)
        finally:
            other.close()

    authority._after_pre_submission_block_proof = seam

    def seal() -> tuple[HostSubmissionState, str, str]:
        seal_calls.append(job.job_id)
        return (
            HostSubmissionState.SEALED_NOT_SUBMITTED,
            "host durably sealed submission",
            "evidence",
        )

    blocked, evidence = authority.resolve_pre_submission_block(job.job_id, seal)

    assert blocked is True
    assert evidence == "evidence"
    assert attempted == [job.job_id]
    # Exactly one bounded host seal -- never a poll loop.
    assert seal_calls == [job.job_id]
    blocked_job = store.package_update_job(job.job_id)
    assert blocked_job.status is PackageUpdateJobStatus.BLOCKED
    assert blocked_job.checkpoint is PackageUpdateCheckpoint.SNAPSHOT_MAY_HAVE_STARTED
    assert blocked_job.snapshot_task_upid is None
    assert blocked_job.snapshot_confirmed_at is None
    events = store.list_package_update_job_events(job.job_id)
    assert (
        events[-1].event_type
        is PackageUpdateEventType.SNAPSHOT_BLOCKED_BEFORE_SUBMISSION
    )

    # Afterwards, a competitor's submission attempt correctly refuses: the
    # job is already terminal, and its callback is never invoked.
    competitor_calls: list[str] = []
    with pytest.raises(AuthorityConflict, match="terminal"):
        authority.execute_snapshot_submission_if_current(
            job.job_id, lambda: competitor_calls.append("submitted")
        )
    assert competitor_calls == []

    # Startup recovery leaves the terminal job exactly as it is.
    before_events = store.list_package_update_job_events(job.job_id)
    assert authority.recover_interrupted_package_update_jobs() == ()
    assert store.package_update_job(job.job_id) == blocked_job
    assert store.list_package_update_job_events(job.job_id) == before_events

    # And the global destructive slot is free again.
    other_resource, _, other_approval = _add_approved_resource(store, authority)
    successor = _issue(authority, other_resource, other_approval)
    assert successor.status is PackageUpdateJobStatus.ACTIVE


def test_block_wins_first_through_the_real_dark_boundary(tmp_path: Path) -> None:
    """A. The same witness, through the real dark boundary.

    The host journal genuinely reaches `sealed_not_submitted`. Once the block commits,
    a competitor's submission attempt must never reach a real `pvesh
    create` -- it is refused before the host is ever asked.
    """

    store, authority, job, identity, ownership, pve, journal, _ = _dark_system(
        tmp_path
    )
    authority.record_package_update_preflight_passed(job.job_id)
    job = authority.record_package_update_snapshot_intent(job.job_id)

    channel = _dark_channel(pve, journal)

    def seal() -> tuple[HostSubmissionState | None, str, HostSnapshotResult]:
        result = channel.seal_operation_never_submitted(
            snapshot_operation_id=identity.snapshot_operation_id,
            snapshot_name=identity.snapshot_name,
            vmid=job.expected_vmid,
            expected_node=job.expected_node_name,
            ownership=ownership,
        )
        return result.submission_state, result.reason or "host seal", result

    blocked, evidence = authority.resolve_pre_submission_block(job.job_id, seal)

    assert blocked is True
    assert pve.submissions == 0
    assert not any(argv[1] == "create" for argv in pve.argvs)
    blocked_job = store.package_update_job(job.job_id)
    assert blocked_job.status is PackageUpdateJobStatus.BLOCKED

    # A competitor's submission attempt correctly refuses: the job is
    # already terminal, so the real host is never even asked.
    create_calls: list[dict] = []

    def tracking_submit() -> HostSnapshotResult:
        create_calls.append({})
        return channel.ensure_pre_update_snapshot_submitted(
            snapshot_operation_id=identity.snapshot_operation_id,
            snapshot_name=identity.snapshot_name,
            vmid=job.expected_vmid,
            expected_node=job.expected_node_name,
            ownership=ownership,
        )

    with pytest.raises(AuthorityConflict, match="terminal"):
        authority.execute_snapshot_submission_if_current(
            job.job_id, tracking_submit
        )
    assert create_calls == []
    assert pve.submissions == 0


def test_submission_wins_first_against_the_pre_submission_block(
    tmp_path: Path,
) -> None:
    """B. A submission that already crossed the door always wins.

    An authorized submission commits first, under its own writer lock. The
    block's OWN seal attempt -- taken later, under its own writer lock --
    must see whatever that submission left behind and refuse to
    terminalize; the durable task evidence remains recoverable regardless.
    """

    _, store, authority, resource, job, identity, ownership = _prepared(tmp_path)

    submitted = authority.execute_snapshot_submission_if_current(
        job.job_id, lambda: "submitted-for-real"
    )
    assert submitted == "submitted-for-real"
    # The backend has NOT yet persisted the task identity -- that only
    # happens in a later, separate transaction (see
    # PackageUpdateSnapshotOrchestrator._apply_host_result). This is exactly
    # the gap the block must not race: its own host seal result, not this
    # durable field, is what has to prove a submission is in flight.
    assert store.package_update_job(job.job_id).snapshot_task_upid is None

    seal_calls: list[str] = []

    def seal() -> tuple[HostSubmissionState, str, str]:
        seal_calls.append(job.job_id)
        return (
            HostSubmissionState.SUBMITTED,
            "host now shows a submission in flight",
            "post-submit-evidence",
        )

    blocked, evidence = authority.resolve_pre_submission_block(job.job_id, seal)

    assert blocked is False
    assert evidence == "post-submit-evidence"
    assert seal_calls == [job.job_id]
    fenced = store.package_update_job(job.job_id)
    assert fenced.status is PackageUpdateJobStatus.ACTIVE
    assert fenced.checkpoint is PackageUpdateCheckpoint.SNAPSHOT_MAY_HAVE_STARTED

    # The task evidence remains recoverable outside the block's lock, exactly
    # as the ordinary orchestrator pipeline would do it afterward.
    authority.record_package_update_snapshot_task(job.job_id, UPID)
    assert store.package_update_job(job.job_id).snapshot_task_upid == UPID


def test_the_block_path_stays_safe_even_though_authority_is_not_monotonic(
    tmp_path: Path,
) -> None:
    """C. "Once stale, always stale" is not a valid safety assumption.

    Authority can legitimately go stale and then become current again -- a
    package scan that no longer matches the job's frozen plan, followed by a
    new successful scan that reproduces the exact same material, is a real,
    ordinary way for that to happen; it is not this stage's job to change
    that. Demonstrate the round trip, then prove the pre-submission block
    stays safe regardless: even though authority is current again by the
    time the block's seal attempt runs, a competing, already-authorized
    submission that crossed the door earlier -- while authority was briefly
    stale for THIS job's own attempt -- must still win. The block decision
    depends only on the host seal result obtained under its own writer lock,
    never on Hubinet's authority state at any particular moment.
    """

    from dataclasses import replace as _replace

    from tests.test_package_plan_approval import _successful_plan
    from tests.test_package_scan_authority import _packages

    _, store, authority, resource, scan, approval = _approved_system(tmp_path)
    job = _issue(authority, resource, approval)
    authority.record_package_update_preflight_passed(job.job_id)
    job = authority.record_package_update_snapshot_intent(job.job_id)

    # Authority goes stale: a new scan finds different package material, so
    # the job's frozen plan fingerprint no longer matches the latest scan.
    drifted_packages = tuple(
        _replace(package, candidate_version=package.candidate_version + "+rebuild1")
        for package in _packages()
    )
    _successful_plan(authority, resource.resource_id, drifted_packages)
    with pytest.raises(AuthorityConflict, match="package plan fingerprint"):
        authority.execute_snapshot_submission_if_current(
            job.job_id, lambda: "must never run while stale"
        )

    # It legitimately becomes current again: a further scan reproduces the
    # job's exact original material and fingerprint.
    _successful_plan(authority, resource.resource_id)
    restored = authority.execute_snapshot_submission_if_current(
        job.job_id, lambda: "a-real-submission-while-current"
    )
    assert restored == "a-real-submission-while-current"
    # The backend has not yet persisted the task identity from that
    # submission -- the same gap test B exercises -- so only the host's seal
    # result, never this durable field, may decide the block below.
    assert store.package_update_job(job.job_id).snapshot_task_upid is None

    # A DIFFERENT, later invocation's seal attempt -- run after
    # authority is current again -- must still see that submission and
    # refuse to terminalize the job as unsubmitted. It never looks at
    # authority state at all.
    blocked, _ = authority.resolve_pre_submission_block(
        job.job_id,
        lambda: (
            HostSubmissionState.SUBMITTED,
            "host shows the submission that already crossed",
            None,
        ),
    )

    assert blocked is False
    authority.record_package_update_snapshot_task(job.job_id, UPID)
    fenced = store.package_update_job(job.job_id)
    assert fenced.status is PackageUpdateJobStatus.ACTIVE
    assert fenced.snapshot_task_upid == UPID


def test_seal_return_value_alone_decides_never_a_caller_supplied_flag(
    tmp_path: Path,
) -> None:
    """Only the exact typed sealed phase can terminalize under the lock."""

    import inspect as _pyinspect

    signature = _pyinspect.signature(
        InventoryAuthority.resolve_pre_submission_block
    )
    assert list(signature.parameters) == ["self", "job_id", "seal"]

    _, store, authority, resource, job, identity, ownership = _prepared(tmp_path)
    blocked, evidence = authority.resolve_pre_submission_block(
        job.job_id,
        lambda: (HostSubmissionState.INTENT, "not sealed", "unchanged"),
    )
    assert blocked is False
    assert evidence == "unchanged"
    assert store.package_update_job(job.job_id).status is PackageUpdateJobStatus.ACTIVE


# ===========================================================================
# Host-side serialization: the backend's fresh-proof lock is not enough on
# its own. A backend that lost its SQLite writer lock (crash, restart, a
# separate later attempt) cannot infer that a remote submission-only mutator
# it once invoked is also gone: that mutator is a separate process on the PVE
# host and may still be alive, holding the SAME per-VMID lease
# `_ensure_submitted`, `_inspect`, and `_seal_never_submitted` all join. The
# backend critical sections
# alone do not see that -- only the host's own VmidMutationLock does.
# ===========================================================================


def test_resolve_pre_submission_block_never_terminalizes_while_the_remote_mutator_is_alive(
    tmp_path: Path,
) -> None:
    """The most important regression: backend loses its lock, host does not.

    A remote submission-only mutator is paused mid-flight through the real
    dark boundary -- real per-VMID lease held, real on-disk journal at
    `intent` -- exactly where a caller that lost its SSH connection or
    crashed would leave it. resolve_pre_submission_block's own host seal,
    run inside its own SQLite writer lock, must join that SAME
    host lease and refuse to terminalize while the mutator might still be
    about to submit. Once the mutator genuinely finishes -- here, by
    actually submitting -- a later recovery attempt correctly sees that,
    never resubmits, and never releases the job as unsubmitted.
    """

    import threading

    from tests.test_package_snapshot_helper import FakePve, helper as snapshot_helper

    store, authority, job, identity, ownership, _, journal, upid = _dark_system(
        tmp_path
    )
    authority.record_package_update_preflight_passed(job.job_id)
    job = authority.record_package_update_snapshot_intent(job.job_id)

    entered_live_check = threading.Event()
    resume_mutator = threading.Event()

    class PausingPve(FakePve):
        def _dispatch(self, argv):
            if argv[:3] == ("pvesh", "get", "/cluster/resources"):
                entered_live_check.set()
                assert resume_mutator.wait(timeout=10)
            return super()._dispatch(argv)

    def _owned_snapshot_entry() -> dict:
        return {
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

    paused_pve = PausingPve(
        vmid=job.expected_vmid,
        node=job.expected_node_name,
        task_sequence=[{"upid": upid, "status": "stopped", "exitstatus": "OK"}],
        submit_upid=upid,
    )
    paused_pve.on_submit = lambda p: p.snapshots.append(_owned_snapshot_entry())

    mutator_channel = _dark_channel(paused_pve, journal)
    mutator_result: dict = {}

    def run_mutator() -> None:
        mutator_result["value"] = (
            mutator_channel.ensure_pre_update_snapshot_submitted(
                snapshot_operation_id=identity.snapshot_operation_id,
                snapshot_name=identity.snapshot_name,
                vmid=job.expected_vmid,
                expected_node=job.expected_node_name,
                ownership=ownership,
            )
        )

    mutator_thread = threading.Thread(target=run_mutator)
    mutator_thread.start()
    try:
        assert entered_live_check.wait(timeout=10)

        # The remote mutator is paused right here: the real per-VMID lease
        # is held, and the real on-disk journal is at `intent`.
        assert (
            snapshot_helper.OperationJournal(journal.directory)
            .read(identity.snapshot_operation_id)["phase"]
            == "intent"
        )

        # Backend B: a host seal through the real dark boundary,
        # run inside resolve_pre_submission_block's own writer lock.
        read_only_pve = FakePve(vmid=job.expected_vmid, node=job.expected_node_name)
        read_channel = _dark_channel(read_only_pve, journal)

        def seal() -> tuple[HostSubmissionState | None, str, HostSnapshotResult]:
            fresh = read_channel.seal_operation_never_submitted(
                snapshot_operation_id=identity.snapshot_operation_id,
                snapshot_name=identity.snapshot_name,
                vmid=job.expected_vmid,
                expected_node=job.expected_node_name,
                ownership=ownership,
            )
            return fresh.submission_state, fresh.reason or "host seal", fresh

        blocked, evidence = authority.resolve_pre_submission_block(
            job.job_id, seal
        )

        assert blocked is False
        assert evidence.outcome is SnapshotOperationOutcome.UNCERTAIN
        # The seal made zero PVE calls: the lease attempt
        # failed before ever touching PVE.
        assert read_only_pve.argvs == []
        assert read_only_pve.submissions == 0
        fenced = store.package_update_job(job.job_id)
        assert fenced.status is PackageUpdateJobStatus.ACTIVE
        assert fenced.checkpoint is PackageUpdateCheckpoint.SNAPSHOT_MAY_HAVE_STARTED
        assert paused_pve.submissions == 0
    finally:
        resume_mutator.set()
        mutator_thread.join(timeout=10)
    assert not mutator_thread.is_alive()

    # The mutator crossed the door for real, exactly once.
    result = mutator_result["value"]
    assert result.outcome is SnapshotOperationOutcome.UNCERTAIN
    assert result.submission_state is HostSubmissionState.TASK_KNOWN
    assert paused_pve.submissions == 1

    # Next backend recovery must not resubmit, and must not release the job
    # as unsubmitted -- it must recover to a confirmed snapshot instead.
    final_pve = FakePve(
        vmid=job.expected_vmid,
        node=job.expected_node_name,
        snapshots=list(paused_pve.snapshots),
        task_sequence=list(paused_pve.task_sequence),
    )
    recovery_channel = _dark_channel(final_pve, journal)
    recovered = PackageUpdateSnapshotOrchestrator(
        authority, recovery_channel
    ).ensure_job_owned_snapshot(job.job_id)

    assert final_pve.submissions == 0
    assert recovered.outcome is SnapshotOperationOutcome.COMPLETED
    assert recovered.job.checkpoint is PackageUpdateCheckpoint.SNAPSHOT_CONFIRMED
