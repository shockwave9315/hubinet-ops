"""Crash-safe same-job rollback execution: authority, host journal, recovery.

Covers the whole boundary between a job that may already have mutated a
workload and one proven, at-most-once, same-job PVE snapshot rollback:

- the deterministic, domain-separated rollback operation identity;
- the write-ahead ``rollback_may_have_started`` uncertainty boundary and its
  SQL invariants, entered from BOTH ``mutation_may_have_started`` (failed,
  partial, or unproven mutation) and ``mutation_completed``, without ever
  fabricating ``mutation_completed_at``;
- the exact same-job target proof: no caller-supplied name, no foreign or
  other-job snapshot, no ``current`` pseudo-entry;
- the bounded submission critical section and its durable release proof;
- the crash/race matrix (crash before submission, crash after the write-ahead
  commit, lost response, restart while the task runs, terminal result before
  the DB records it, ambiguous evidence, a delayed submitter racing a seal);
- the completion proof: terminal non-error PVE task, the durable task
  identity, fresh canonical evidence, and the ``parent`` post-condition.

Nothing here runs a real ``pvesh``, ``pct``, ``ssh``, or PVE operation. The
host boundary is the actual dark helper module driven by a fake PVE, with an
isolated temporary journal directory and a JSON round trip through the real
transport parser.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
from types import ModuleType
import uuid

import pytest

from app.inventory import (
    PackageScanPackage,
    AuthorityConflict,
    HostRollbackState,
    InventoryAuthority,
    InventoryAuthorityStore,
    ObservedSnapshot,
    PackageUpdateCheckpoint,
    PackageUpdateEventType,
    PackageUpdateJobStatus,
    RollbackSubmissionRefusedBeforeCallback,
)
from app.inventory.rollback_identity import derive_package_rollback_identity
from app.inventory.snapshot_identity import build_snapshot_ownership
from app.package_update_rollback import (
    HostRollbackResult,
    PackageUpdateRollbackError,
    PackageUpdateRollbackOrchestrator,
    RollbackOperationOutcome,
)
from app.package_update_rollback_host_control import (
    SshPackageUpdateRollbackHostControl,
)
from tests.test_package_update_execution_gate import _ready_job
from tests.test_package_update_snapshot_safety import (
    _break_incarnation_continuity_at_the_same_locator,
    _current_entry,
    _foreign_entry,
    _owned_entry,
)


ROOT = Path(__file__).resolve().parents[1]
HELPER_PATH = ROOT / "deploy" / "hubinet-package-rollback-helper.py"

NODE = "pve-a"
UPID = "UPID:pve-a:0000ABCD:00ABCDEF:65000000:vzrollback:110:root@pam:"
OTHER_UPID = "UPID:pve-a:0000BEEF:00ABCDEF:65000000:vzrollback:110:root@pam:"


def _load_helper() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "hubinet_package_rollback_helper", HELPER_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


helper = _load_helper()


# ===========================================================================
# A fake PVE, driven through the REAL dark helper
# ===========================================================================


class FakePve:
    """A deterministic stand-in for the PVE host this helper talks to.

    It answers exactly the fixed ``pvesh`` argv shapes the dark helper issues
    and nothing else: an unrecognised command is a test failure, not a silent
    empty result. Its whole point is that the helper's own journal, lease,
    request-fingerprint, and refusal logic run for real against it.
    """

    def __init__(self, *, vmid: int, snapshot_name: str, ownership) -> None:
        self.vmid = vmid
        self.node = NODE
        self.snapshot_name = snapshot_name
        self.ownership = ownership
        self.present = True
        self.resource_type = "lxc"
        self.current_node = NODE
        self.config_lock: str | None = None
        self.snapshot_present = True
        self.snapshot_incomplete = False
        #: PVE's `current` pseudo-entry parent. Upstream `snapshot_rollback`
        #: sets this to the snapshot name in its second locked phase.
        self.current_parent: str | None = None
        #: Recorded real rollback submissions. MUST never exceed one.
        self.rollbacks: list[dict[str, str]] = []
        self.rollback_returncode = 0
        self.returned_upid: str | None = UPID
        self.task_status = "running"
        self.task_exitstatus: str | None = None
        self.fail_reads: set[str] = set()
        #: Overrides the target snapshot's PVE description. Tests set this
        #: AFTER authority has already armed the rollback, to model the real
        #: TOCTOU window: the physical snapshot name survives while its
        #: ownership metadata changes underneath.
        self.snapshot_description: str | None = None
        #: Extra listing rows, for ambiguity cases.
        self.extra_rows: list[dict] = []

    # -- rendering -----------------------------------------------------

    def _snapshot_rows(self) -> list[dict]:
        rows: list[dict] = []
        if self.snapshot_present:
            from app.inventory.snapshot_identity import encode_snapshot_description

            description = (
                self.snapshot_description
                if self.snapshot_description is not None
                # PVE's LXC config parser appends a newline to every
                # description line it reads back.
                else encode_snapshot_description(self.ownership) + "\n"
            )
            row = {
                "name": self.snapshot_name,
                "description": description,
                "snaptime": 1_700_000_000,
            }
            if self.snapshot_incomplete:
                row["snapstate"] = "prepare"
            rows.append(row)
        rows.extend(self.extra_rows)
        rows.append({"name": "operator-manual", "description": "by hand", "snaptime": 1})
        current = {"name": "current", "description": "You are here!", "digest": "d"}
        if self.current_parent is not None:
            current["parent"] = self.current_parent
        rows.append(current)
        return rows

    # -- the fixed argv surface ---------------------------------------

    def runner(self, argv, timeout, max_output):
        argv = tuple(argv)
        result = self._dispatch(argv)
        if result is None:
            raise AssertionError(f"fake PVE received an unexpected command: {argv}")
        return result

    def _ok(self, payload) -> helper.CommandResult:
        return helper.CommandResult(
            returncode=0,
            stdout=json.dumps(payload).encode("utf-8"),
            stderr=b"",
            timed_out=False,
            output_exceeded=False,
        )

    def _fail(self) -> helper.CommandResult:
        return helper.CommandResult(
            returncode=1,
            stdout=b"",
            stderr=b"refused",
            timed_out=False,
            output_exceeded=False,
        )

    def _dispatch(self, argv):
        if argv[:3] == ("pvesh", "get", "/cluster/resources"):
            if "resources" in self.fail_reads:
                return self._fail()
            rows = []
            if self.present:
                rows.append(
                    {
                        "vmid": self.vmid,
                        "type": self.resource_type,
                        "node": self.current_node,
                    }
                )
            return self._ok(rows)
        if argv[:2] == ("pvesh", "get") and argv[2].endswith("/config"):
            if "config" in self.fail_reads:
                return self._fail()
            config = {"hostname": "guest"}
            if self.config_lock is not None:
                config["lock"] = self.config_lock
            return self._ok(config)
        if argv[:2] == ("pvesh", "get") and argv[2].endswith("/snapshot"):
            if "snapshot" in self.fail_reads:
                return self._fail()
            return self._ok(self._snapshot_rows())
        if argv[:2] == ("pvesh", "get") and argv[2].endswith("/status"):
            if "task" in self.fail_reads:
                return self._fail()
            status = {"upid": UPID, "status": self.task_status}
            if self.task_exitstatus is not None:
                status["exitstatus"] = self.task_exitstatus
            return self._ok(status)
        if argv[:2] == ("pvesh", "create") and "/rollback" in argv[2]:
            # The one real destructive submission.
            assert "--start" in argv, "rollback must always pin PVE's start param"
            assert argv[argv.index("--start") + 1] == "0", (
                "this stage must always roll back with start=0"
            )
            self.rollbacks.append({"path": argv[2]})
            if self.rollback_returncode != 0:
                return self._fail()
            # Upstream force-stops the guest and sets parent = snapname.
            self.current_parent = self.snapshot_name
            if self.returned_upid is None:
                return self._ok(None)
            return self._ok(self.returned_upid)
        return None


class HelperBackedHostControl:
    """Drive the REAL dark helper over a JSON round trip, hermetically.

    Every request is rendered by the production transport's own request
    builder, handed to the helper's own dispatcher, serialized to JSON, and
    parsed by the production transport's own response parser. Only the
    process boundary (SSH) is replaced.
    """

    def __init__(self, pve: FakePve, journal_directory: Path) -> None:
        self.pve = pve
        # `journal_directory.parent` stands in for `JOURNAL_DURABILITY_ANCHOR`
        # (`/var/lib` in production): the caller's `tmp_path`-rooted parent is
        # exactly as durable a pre-existing boundary for this test as the real
        # anchor is in production.
        self.journal = helper.OperationJournal(
            journal_directory, anchor=journal_directory.parent
        )
        self.calls: list[str] = []
        self.fail_operation: dict[str, BaseException] = {}
        self.drop_response: set[str] = set()
        self._parser = _parser()

    def _run(self, operation: str, request):
        self.calls.append(operation)
        if operation in self.fail_operation:
            raise self.fail_operation[operation]
        payload = _request_payload(operation, request)
        if operation in self.drop_response:
            # Models a lost SSH answer: the host side really ran, the
            # backend never learned what it said.
            helper.handle(payload, runner=self.pve.runner, journal=self.journal)
            raise TimeoutError("ssh response lost")
        try:
            response = helper.handle(
                payload, runner=self.pve.runner, journal=self.journal
            )
        except helper.RollbackError as exc:
            response = {
                "response_version": 1,
                "ok": False,
                "rollback_operation_id": request.rollback_operation_id,
                "error": {
                    "classification": exc.classification,
                    "message": str(exc),
                    "submission": exc.submission,
                },
            }
        return self._parser(response, request.rollback_operation_id)

    def submit_same_job_rollback(self, request) -> HostRollbackResult:
        return self._run("submit_same_job_rollback", request)

    def inspect_rollback_state(self, request) -> HostRollbackResult:
        return self._run("inspect_rollback_state", request)

    def seal_rollback_never_submitted(self, request) -> HostRollbackResult:
        return self._run("seal_rollback_never_submitted", request)


def _transport() -> SshPackageUpdateRollbackHostControl:
    return SshPackageUpdateRollbackHostControl(
        host="pve.example",
        port=22,
        user="hubinet",
        private_key_path=Path("/etc/hubinet/key"),
        known_hosts_path=Path("/etc/hubinet/known_hosts"),
        submission_timeout_seconds=60,
        inspection_timeout_seconds=120,
        max_result_bytes=1024 * 1024,
    )


def _parser():
    """Return the production transport's own response parser, unmodified."""

    transport = _transport()
    return transport._parse_payload


def _request_payload(operation: str, request) -> dict:
    """Render one request exactly as the production transport does.

    Goes through the real transport so a change to its wire shape breaks
    these tests rather than silently diverging from the helper.
    """

    captured: dict = {}
    transport = _transport()

    def _capture(argv, encoded, timeout, max_result_bytes):
        captured["payload"] = json.loads(encoded.decode("utf-8"))
        return helper.CommandResult(
            returncode=0,
            stdout=b'{"response_version":1,"ok":false,'
            b'"rollback_operation_id":null,"error":{"classification":"x"}}',
            stderr=b"",
            timed_out=False,
            output_exceeded=False,
        )

    transport._runner = _capture
    getattr(transport, operation)(request)
    return captured["payload"]


# ===========================================================================
# Shared fixtures
# ===========================================================================


def _fresh_material(job) -> tuple[PackageScanPackage, ...]:
    """The job's own frozen rows, as the scan-shaped values arming expects."""

    return tuple(
        PackageScanPackage(
            package_name=package.package_name,
            architecture=package.architecture,
            installed_version=package.installed_version,
            candidate_version=package.candidate_version,
            origin=package.origin,
            description=package.description,
            security=package.security,
        )
        for package in job.packages
    )


def _mutating_job(tmp_path: Path, *, complete_mutation: bool = False):
    """One ACTIVE job past the write-ahead package mutation boundary.

    ``complete_mutation`` chooses which of the TWO legal rollback entry
    points the job sits at. Neither path is allowed to fabricate the other's
    facts, which is exactly what the schema and these tests enforce.
    """

    clock, store, authority, resource, scan, approval, job = _ready_job(tmp_path)
    authority.arm_package_update_mutation(
        job.job_id, _fresh_material(job), prepared_evidence_digest="a" * 64
    )
    if complete_mutation:
        _force_mutation_completed(store, job.job_id, clock)
    job = authority.package_update_job(job.job_id)
    ownership = authority.package_update_snapshot_ownership(job.job_id)
    identity = authority.package_update_snapshot_identity(job.job_id)
    pve = FakePve(
        vmid=job.expected_vmid,
        snapshot_name=identity.snapshot_name,
        ownership=ownership,
    )
    host = HelperBackedHostControl(pve, tmp_path / "journal")
    orchestrator = PackageUpdateRollbackOrchestrator(
        authority,
        host,
        sleep=lambda _seconds: None,
        monotonic=_FakeMonotonic(5.0),
        task_poll_timeout_seconds=30.0,
        task_poll_interval_seconds=5.0,
    )
    return (
        clock,
        store,
        authority,
        resource,
        job,
        ownership,
        identity,
        pve,
        host,
        orchestrator,
    )


def _force_mutation_completed(store, job_id: str, clock) -> None:
    """Advance a job to a PROVEN completed mutation, through SQL.

    Deliberately not a shortcut around the completion proof: this fixture
    exists only so the rollback tests can exercise the second legal entry
    point without re-running the whole mutation stage, and it writes exactly
    the facts that stage's own proof would have written.
    """

    with store._transaction() as connection:
        connection.execute(
            "UPDATE package_update_jobs SET checkpoint='mutation_completed', "
            "mutation_completed_at=? WHERE job_id=?",
            (clock().isoformat(), job_id),
        )


class _FakeMonotonic:
    """A deterministic clock so bounded polling terminates without sleeping."""

    def __init__(self, step: float) -> None:
        self._now = 0.0
        self._step = step

    def __call__(self) -> float:
        self._now += self._step
        return self._now


def _canonical_after_rollback(ownership, identity):
    """A fresh listing exactly as PVE reports it AFTER a successful rollback."""

    from dataclasses import replace

    return (
        replace(_current_entry(), parent=identity.snapshot_name),
        _foreign_entry(),
        _owned_entry(ownership, identity.snapshot_name),
    )


def _canonical_before_rollback(ownership, identity):
    return (_current_entry(), _foreign_entry(), _owned_entry(ownership, identity.snapshot_name))


def _events(store, job_id):
    return [
        event.event_type
        for event in store.list_package_update_job_events(job_id, limit=200)
    ]


def _complete_rollback(pve) -> None:
    """Drive the fake PVE to the state a finished successful rollback leaves."""

    pve.task_status = "stopped"
    pve.task_exitstatus = "OK"


# ===========================================================================
# A. DETERMINISTIC, JOB-OWNED ROLLBACK IDENTITY
# ===========================================================================


def test_same_job_derives_the_same_rollback_identity_across_restart(
    tmp_path: Path,
) -> None:
    clock, store, authority, _, job, _, identity, *_ = _mutating_job(tmp_path)

    first = authority.package_update_rollback_identity(job.job_id)
    path = store.path
    store.close()
    reopened = InventoryAuthorityStore(path, now=clock)
    second = InventoryAuthority(reopened, now=clock).package_update_rollback_identity(
        job.job_id
    )

    assert first == second
    assert first.rollback_operation_id == derive_package_rollback_identity(
        backend_instance_id=reopened.backend_instance().backend_instance_id,
        job_id=job.job_id,
        resource_id=job.resource_id,
        resource_continuity_revision=job.expected_resource_continuity_revision,
        snapshot_operation_id=identity.snapshot_operation_id,
        snapshot_name=identity.snapshot_name,
    ).rollback_operation_id


def test_rollback_identity_is_domain_separated_from_snapshot_and_mutation(
    tmp_path: Path,
) -> None:
    _, _, authority, _, job, _, _, *_ = _mutating_job(tmp_path)

    rollback = authority.package_update_rollback_identity(job.job_id)
    snapshot = authority.package_update_snapshot_identity(job.job_id)
    mutation = authority.package_update_mutation_identity(job.job_id)

    assert len({
        rollback.rollback_operation_id,
        snapshot.snapshot_operation_id,
        mutation.mutation_operation_id,
    }) == 3


@pytest.mark.parametrize(
    "field,value",
    [
        ("job_id", str(uuid.uuid4())),
        ("resource_id", str(uuid.uuid4())),
        ("resource_continuity_revision", 99),
        ("snapshot_operation_id", str(uuid.uuid4())),
        ("snapshot_name", "hubinet-preupd-other"),
        ("backend_instance_id", str(uuid.uuid4())),
    ],
)
def test_any_different_identity_fact_derives_a_different_rollback_operation(
    tmp_path: Path, field: str, value
) -> None:
    _, store, authority, _, job, _, identity, *_ = _mutating_job(tmp_path)
    base = {
        "backend_instance_id": store.backend_instance().backend_instance_id,
        "job_id": job.job_id,
        "resource_id": job.resource_id,
        "resource_continuity_revision": job.expected_resource_continuity_revision,
        "snapshot_operation_id": identity.snapshot_operation_id,
        "snapshot_name": identity.snapshot_name,
    }

    original = derive_package_rollback_identity(**base)
    altered = derive_package_rollback_identity(**{**base, field: value})

    assert original.rollback_operation_id != altered.rollback_operation_id


def test_a_caller_cannot_supply_a_rollback_operation_identity(
    tmp_path: Path,
) -> None:
    _, _, authority, _, job, _, _, *_ = _mutating_job(tmp_path)

    # The public surface takes a job id and nothing else. There is no
    # parameter through which an operation identity could be injected.
    request = authority.package_update_rollback_request(job.job_id)
    derived = authority.package_update_rollback_identity(job.job_id)
    assert request.rollback_operation_id == derived.rollback_operation_id


# ===========================================================================
# B. THE WRITE-AHEAD BOUNDARY, FROM BOTH LEGAL ENTRY POINTS
# ===========================================================================


def test_an_unproven_mutation_reaches_the_rollback_boundary_without_completion(
    tmp_path: Path,
) -> None:
    """The whole reason schema v14 exists.

    A job whose package mutation failed, was partial, or simply could not be
    proven complete stays at ``mutation_may_have_started`` with
    ``mutation_completed_at`` NULL -- and is exactly the job that most needs
    compensating.
    """

    _, store, authority, _, job, ownership, identity, *_ = _mutating_job(tmp_path)
    assert job.checkpoint is PackageUpdateCheckpoint.MUTATION_MAY_HAVE_STARTED
    assert job.mutation_completed_at is None

    armed = authority.arm_package_update_rollback(
        job.job_id, _canonical_before_rollback(ownership, identity)
    )

    assert armed.checkpoint is PackageUpdateCheckpoint.ROLLBACK_MAY_HAVE_STARTED
    assert armed.rollback_operation_id is not None
    assert armed.rollback_may_have_started_at is not None
    # The load-bearing assertion: nothing fabricated a completion.
    assert armed.mutation_completed_at is None
    assert armed.status is PackageUpdateJobStatus.ACTIVE


def test_a_proven_mutation_also_reaches_the_rollback_boundary(
    tmp_path: Path,
) -> None:
    _, _, authority, _, job, ownership, identity, *_ = _mutating_job(
        tmp_path, complete_mutation=True
    )
    assert job.checkpoint is PackageUpdateCheckpoint.MUTATION_COMPLETED

    armed = authority.arm_package_update_rollback(
        job.job_id, _canonical_before_rollback(ownership, identity)
    )

    assert armed.checkpoint is PackageUpdateCheckpoint.ROLLBACK_MAY_HAVE_STARTED
    assert armed.mutation_completed_at is not None


def test_arming_is_idempotent_and_never_creates_a_second_boundary(
    tmp_path: Path,
) -> None:
    _, store, authority, _, job, ownership, identity, *_ = _mutating_job(tmp_path)
    observed = _canonical_before_rollback(ownership, identity)

    first = authority.arm_package_update_rollback(job.job_id, observed)
    second = authority.arm_package_update_rollback(job.job_id, observed)

    assert first.rollback_operation_id == second.rollback_operation_id
    assert first.rollback_may_have_started_at == second.rollback_may_have_started_at
    assert (
        _events(store, job.job_id).count(
            PackageUpdateEventType.ROLLBACK_MAY_HAVE_STARTED
        )
        == 1
    )


def test_a_job_before_the_mutation_boundary_can_never_arm_a_rollback(
    tmp_path: Path,
) -> None:
    """A mutation that provably never started has nothing to compensate."""

    _, _, authority, _, _, _, job = _ready_job(tmp_path)
    ownership = authority.package_update_snapshot_ownership(job.job_id)
    identity = authority.package_update_snapshot_identity(job.job_id)
    assert job.checkpoint is PackageUpdateCheckpoint.SNAPSHOT_CONFIRMED

    with pytest.raises(AuthorityConflict, match="not eligible"):
        authority.arm_package_update_rollback(
            job.job_id, _canonical_before_rollback(ownership, identity)
        )


def test_a_terminal_job_can_never_arm_a_rollback(tmp_path: Path) -> None:
    _, store, authority, _, job, ownership, identity, *_ = _mutating_job(tmp_path)
    with store._transaction() as connection:
        connection.execute(
            "UPDATE package_update_jobs SET status='blocked', terminalized_at=?, "
            "terminal_reason='done' WHERE job_id=?",
            ("2026-01-01T00:00:00+00:00", job.job_id),
        )

    with pytest.raises(AuthorityConflict, match="terminal"):
        authority.arm_package_update_rollback(
            job.job_id, _canonical_before_rollback(ownership, identity)
        )


# ===========================================================================
# C. THE EXACT SAME-JOB TARGET
# ===========================================================================


def test_a_foreign_snapshot_can_never_be_rolled_back_to(tmp_path: Path) -> None:
    _, _, authority, _, job, _, _, *_ = _mutating_job(tmp_path)

    with pytest.raises(AuthorityConflict, match="does not contain this job"):
        authority.arm_package_update_rollback(
            job.job_id, (_current_entry(), _foreign_entry())
        )


def test_another_jobs_snapshot_can_never_be_rolled_back_to(tmp_path: Path) -> None:
    _, _, authority, _, job, _, identity, *_ = _mutating_job(tmp_path)
    other_ownership = build_snapshot_ownership(
        job_id=str(uuid.uuid4()),
        resource_id=job.resource_id,
        resource_continuity_revision=job.expected_resource_continuity_revision,
        inventory_source_id=job.inventory_source_id,
        backend_instance_id=str(uuid.uuid4()),
    )

    with pytest.raises(AuthorityConflict):
        authority.arm_package_update_rollback(
            job.job_id,
            (
                _current_entry(),
                _owned_entry(other_ownership, identity.snapshot_name),
            ),
        )


def test_the_current_pseudo_entry_can_never_be_a_rollback_target(
    tmp_path: Path,
) -> None:
    _, _, authority, _, job, ownership, identity, *_ = _mutating_job(tmp_path)
    from dataclasses import replace

    # PVE reporting the job's own snapshot name as its synthetic `current`
    # row is nonsense, and must fail closed rather than resolve either way.
    poisoned = replace(_current_entry(), name=identity.snapshot_name)

    with pytest.raises(AuthorityConflict, match="current pseudo-entry"):
        authority.arm_package_update_rollback(job.job_id, (poisoned,))


def test_an_incomplete_job_snapshot_is_never_a_rollback_target(
    tmp_path: Path,
) -> None:
    _, _, authority, _, job, ownership, identity, *_ = _mutating_job(tmp_path)
    from dataclasses import replace

    incomplete = replace(
        _owned_entry(ownership, identity.snapshot_name), incomplete=True
    )

    with pytest.raises(AuthorityConflict, match="incomplete"):
        authority.arm_package_update_rollback(
            job.job_id, (_current_entry(), incomplete)
        )


def test_duplicate_job_owned_snapshots_fail_closed(tmp_path: Path) -> None:
    _, _, authority, _, job, ownership, identity, *_ = _mutating_job(tmp_path)

    with pytest.raises(AuthorityConflict, match="another snapshot claims this job"):
        authority.arm_package_update_rollback(
            job.job_id,
            (
                _current_entry(),
                _owned_entry(ownership, identity.snapshot_name),
                _owned_entry(ownership, "hubinet-preupd-duplicate"),
            ),
        )


def test_malformed_hubinet_metadata_fails_closed(tmp_path: Path) -> None:
    _, _, authority, _, job, ownership, identity, *_ = _mutating_job(tmp_path)
    malformed = ObservedSnapshot(
        name="hubinet-preupd-broken",
        description="hubinet-ops-snapshot but not parseable",
        ownership_malformed=True,
    )

    with pytest.raises(AuthorityConflict, match="malformed"):
        authority.arm_package_update_rollback(
            job.job_id,
            (
                _current_entry(),
                _owned_entry(ownership, identity.snapshot_name),
                malformed,
            ),
        )


def test_the_rollback_request_always_names_the_jobs_own_snapshot(
    tmp_path: Path,
) -> None:
    _, _, authority, _, job, ownership, identity, *_ = _mutating_job(tmp_path)
    authority.arm_package_update_rollback(
        job.job_id, _canonical_before_rollback(ownership, identity)
    )

    request = authority.package_update_rollback_request(job.job_id)

    assert request.snapshot_name == identity.snapshot_name
    assert request.snapshot_operation_id == identity.snapshot_operation_id
    assert request.vmid == job.expected_vmid
    assert request.expected_node == job.expected_node_name


# ===========================================================================
# D. SQL INVARIANTS
# ===========================================================================


def _assert_sql_rejected(store, statement, parameters):
    with pytest.raises(sqlite3.IntegrityError):
        with store._transaction() as connection:
            connection.execute(statement, parameters)


def test_rolled_back_is_impossible_without_proven_rollback_completion(
    tmp_path: Path,
) -> None:
    _, store, authority, _, job, ownership, identity, *_ = _mutating_job(tmp_path)
    authority.arm_package_update_rollback(
        job.job_id, _canonical_before_rollback(ownership, identity)
    )

    _assert_sql_rejected(
        store,
        "UPDATE package_update_jobs SET status='rolled_back', terminalized_at=?, "
        "terminal_reason='forged' WHERE job_id=?",
        ("2026-01-01T00:00:00+00:00", job.job_id),
    )


def test_succeeded_is_impossible_without_proven_mutation_completion(
    tmp_path: Path,
) -> None:
    _, store, authority, _, job, *_ = _mutating_job(tmp_path)

    _assert_sql_rejected(
        store,
        "UPDATE package_update_jobs SET status='succeeded', terminalized_at=?, "
        "terminal_reason='forged' WHERE job_id=?",
        ("2026-01-01T00:00:00+00:00", job.job_id),
    )


def test_rollback_completion_is_impossible_without_the_write_ahead_boundary(
    tmp_path: Path,
) -> None:
    _, store, authority, _, job, *_ = _mutating_job(tmp_path)

    _assert_sql_rejected(
        store,
        "UPDATE package_update_jobs SET checkpoint='rollback_completed', "
        "rollback_completed_at=? WHERE job_id=?",
        ("2026-01-01T00:00:00+00:00", job.job_id),
    )


def test_a_rollback_task_is_impossible_without_a_rollback_operation(
    tmp_path: Path,
) -> None:
    _, store, authority, _, job, *_ = _mutating_job(tmp_path)

    _assert_sql_rejected(
        store,
        "UPDATE package_update_jobs SET rollback_task_upid=? WHERE job_id=?",
        (UPID, job.job_id),
    )


def test_the_rollback_operation_identity_is_write_once(tmp_path: Path) -> None:
    _, store, authority, _, job, ownership, identity, *_ = _mutating_job(tmp_path)
    authority.arm_package_update_rollback(
        job.job_id, _canonical_before_rollback(ownership, identity)
    )

    with pytest.raises(sqlite3.IntegrityError, match="write-once"):
        with store._transaction() as connection:
            connection.execute(
                "UPDATE package_update_jobs SET rollback_operation_id=? "
                "WHERE job_id=?",
                (str(uuid.uuid4()), job.job_id),
            )


def test_the_rollback_task_identity_is_write_once(tmp_path: Path) -> None:
    _, store, authority, _, job, ownership, identity, *_ = _mutating_job(tmp_path)
    authority.arm_package_update_rollback(
        job.job_id, _canonical_before_rollback(ownership, identity)
    )
    authority.record_package_update_rollback_task(job.job_id, UPID)

    with pytest.raises(sqlite3.IntegrityError, match="write-once"):
        with store._transaction() as connection:
            connection.execute(
                "UPDATE package_update_jobs SET rollback_task_upid=? WHERE job_id=?",
                (OTHER_UPID, job.job_id),
            )


def test_the_rollback_completion_timestamp_is_write_once(tmp_path: Path) -> None:
    _, store, authority, _, job, ownership, identity, pve, host, orchestrator = (
        _mutating_job(tmp_path)
    )
    _drive_to_successful_rollback(authority, orchestrator, job, ownership, identity, pve)

    with pytest.raises(sqlite3.IntegrityError, match="write-once"):
        with store._transaction() as connection:
            connection.execute(
                "UPDATE package_update_jobs SET rollback_completed_at=? "
                "WHERE job_id=?",
                ("2030-01-01T00:00:00+00:00", job.job_id),
            )


def test_a_rollback_operation_requires_a_confirmed_snapshot(tmp_path: Path) -> None:
    """A job with no confirmed snapshot has nothing to roll back TO."""

    _, store, authority, _, job, *_ = _mutating_job(tmp_path)

    # Directly attempting the write-ahead facts on a row whose confirmed
    # snapshot has been cleared is rejected by the schema itself.
    _assert_sql_rejected(
        store,
        "UPDATE package_update_jobs SET checkpoint='rollback_may_have_started', "
        "rollback_operation_id=?, rollback_may_have_started_at=?, "
        "snapshot_confirmed_at=NULL WHERE job_id=?",
        (str(uuid.uuid4()), "2026-01-01T00:00:00+00:00", job.job_id),
    )


def test_the_checkpoint_may_never_regress_out_of_the_rollback_window(
    tmp_path: Path,
) -> None:
    _, store, authority, _, job, ownership, identity, *_ = _mutating_job(tmp_path)
    authority.arm_package_update_rollback(
        job.job_id, _canonical_before_rollback(ownership, identity)
    )

    with pytest.raises(sqlite3.IntegrityError, match="never move backwards"):
        with store._transaction() as connection:
            connection.execute(
                "UPDATE package_update_jobs SET checkpoint='mutation_may_have_started' "
                "WHERE job_id=?",
                (job.job_id,),
            )


# ===========================================================================
# E. THE HOST JOURNAL
# ===========================================================================


def _helper_request(authority, job_id) -> dict:
    request = authority.package_update_rollback_request(job_id)
    return _request_payload("submit_same_job_rollback", request)


def test_submission_journals_submitted_before_pvesh_runs(tmp_path: Path) -> None:
    """The uncertainty boundary is durable BEFORE the destructive call."""

    _, _, authority, _, job, ownership, identity, pve, host, _ = _mutating_job(
        tmp_path
    )
    authority.arm_package_update_rollback(
        job.job_id, _canonical_before_rollback(ownership, identity)
    )
    request = authority.package_update_rollback_request(job.job_id)
    observed_phases: list[str | None] = []
    original = pve.runner

    def _watching(argv, timeout, max_output):
        if tuple(argv)[:2] == ("pvesh", "create"):
            record = host.journal.read(request.rollback_operation_id)
            observed_phases.append(record["phase"] if record else None)
        return original(argv, timeout, max_output)

    pve.runner = _watching
    host.submit_same_job_rollback(request)

    assert observed_phases == ["submitted"]


def test_a_submitted_operation_is_never_resubmitted(tmp_path: Path) -> None:
    _, _, authority, _, job, ownership, identity, pve, host, _ = _mutating_job(
        tmp_path
    )
    authority.arm_package_update_rollback(
        job.job_id, _canonical_before_rollback(ownership, identity)
    )
    request = authority.package_update_rollback_request(job.job_id)

    host.submit_same_job_rollback(request)
    host.submit_same_job_rollback(request)
    host.submit_same_job_rollback(request)

    assert len(pve.rollbacks) == 1


def test_a_sealed_operation_can_never_be_submitted(tmp_path: Path) -> None:
    _, _, authority, _, job, ownership, identity, pve, host, _ = _mutating_job(
        tmp_path
    )
    authority.arm_package_update_rollback(
        job.job_id, _canonical_before_rollback(ownership, identity)
    )
    request = authority.package_update_rollback_request(job.job_id)

    sealed = host.seal_rollback_never_submitted(request)
    assert sealed.rollback_state is HostRollbackState.SEALED_NOT_SUBMITTED

    # A delayed helper launched before the seal must obey it.
    result = host.submit_same_job_rollback(request)

    assert pve.rollbacks == []
    assert result.outcome is RollbackOperationOutcome.NOT_SUBMITTED


def test_a_submitted_operation_can_never_be_sealed(tmp_path: Path) -> None:
    _, _, authority, _, job, ownership, identity, pve, host, _ = _mutating_job(
        tmp_path
    )
    authority.arm_package_update_rollback(
        job.job_id, _canonical_before_rollback(ownership, identity)
    )
    request = authority.package_update_rollback_request(job.job_id)
    host.submit_same_job_rollback(request)

    result = host.seal_rollback_never_submitted(request)

    # Sealing here would be a lie: PVE may already have force-stopped the
    # container. The helper refuses and the answer stays uncertain.
    assert result.outcome is RollbackOperationOutcome.UNCERTAIN
    assert result.rollback_state is not HostRollbackState.SEALED_NOT_SUBMITTED


def test_a_different_request_can_never_reuse_an_existing_operation(
    tmp_path: Path,
) -> None:
    _, _, authority, _, job, ownership, identity, pve, host, _ = _mutating_job(
        tmp_path
    )
    authority.arm_package_update_rollback(
        job.job_id, _canonical_before_rollback(ownership, identity)
    )
    payload = _helper_request(authority, job.job_id)
    helper.handle(payload, runner=pve.runner, journal=host.journal)

    # Same operation identity, materially different request.
    poisoned = json.loads(json.dumps(payload))
    poisoned["target"]["vmid"] = payload["target"]["vmid"] + 1

    with pytest.raises(helper.RollbackError) as raised:
        helper.handle(poisoned, runner=pve.runner, journal=host.journal)
    assert raised.value.classification == "request_mismatch"


def test_a_corrupt_journal_fails_closed(tmp_path: Path) -> None:
    _, _, authority, _, job, ownership, identity, pve, host, _ = _mutating_job(
        tmp_path
    )
    authority.arm_package_update_rollback(
        job.job_id, _canonical_before_rollback(ownership, identity)
    )
    payload = _helper_request(authority, job.job_id)
    helper.handle(payload, runner=pve.runner, journal=host.journal)
    operation_id = payload["operation_identity"]["rollback_operation_id"]
    (host.journal.directory / f"op-{operation_id}.json").write_text("{ not json")

    with pytest.raises(helper.RollbackError) as raised:
        helper.handle(payload, runner=pve.runner, journal=host.journal)
    assert raised.value.classification == "journal_corrupt"


def test_a_short_journal_write_never_becomes_the_durable_record(
    tmp_path: Path, monkeypatch
) -> None:
    journal = helper.OperationJournal(tmp_path / "j", anchor=tmp_path)
    operation_id = str(uuid.uuid4())
    record = {
        "journal_version": 1,
        "rollback_operation_id": operation_id,
        "phase": "intent",
        "request_fingerprint": "f" * 64,
        "vmid": 110,
    }
    real_write = helper.os.write

    def _short(descriptor, payload):
        return real_write(descriptor, payload[: max(1, len(payload) // 3)])

    monkeypatch.setattr(helper.os, "write", _short)
    monkeypatch.setattr(
        helper, "_write_all", lambda d, p: (_ for _ in ()).throw(OSError("short"))
    )

    with pytest.raises(OSError):
        journal.write(record)

    assert journal.read(operation_id) is None
    assert not list(journal.directory.glob("*.tmp"))


def test_concurrent_first_use_callers_each_prove_their_own_barrier(
    tmp_path: Path, monkeypatch
) -> None:
    """`exists` is never trusted as `durably linked`."""

    fsynced: list[str] = []
    real_fsync = helper.os.fsync
    real_open = helper.os.open

    def _tracking_fsync(descriptor):
        fsynced.append(str(descriptor))
        return real_fsync(descriptor)

    monkeypatch.setattr(helper.os, "fsync", _tracking_fsync)
    directory = tmp_path / "anchor" / "hubinet-ops" / "rollback-operations"
    (tmp_path / "anchor").mkdir()

    # First caller creates every level.
    helper._ensure_durable_directory(
        directory, mode=0o700, anchor=tmp_path / "anchor"
    )
    first = len(fsynced)
    assert first >= 2

    # Second caller finds them present and STILL fsyncs each parent.
    helper._ensure_durable_directory(
        directory, mode=0o700, anchor=tmp_path / "anchor"
    )
    assert len(fsynced) - first == first


def test_the_lease_serializes_operations_per_vmid(tmp_path: Path) -> None:
    directory = tmp_path / "journal"
    with helper.VmidRollbackLease(110, directory, anchor=tmp_path):
        with pytest.raises(helper.RollbackError) as raised:
            with helper.VmidRollbackLease(110, directory, anchor=tmp_path):
                pass
        assert raised.value.classification == "operation_in_progress"
    # Released again afterwards.
    with helper.VmidRollbackLease(110, directory, anchor=tmp_path):
        pass


# ===========================================================================
# F. PVE PRE-FLIGHT AND TARGET VALIDATION
# ===========================================================================


def test_a_missing_target_snapshot_refuses_before_submission(tmp_path: Path) -> None:
    _, _, authority, _, job, ownership, identity, pve, host, _ = _mutating_job(
        tmp_path
    )
    authority.arm_package_update_rollback(
        job.job_id, _canonical_before_rollback(ownership, identity)
    )
    pve.snapshot_present = False
    request = authority.package_update_rollback_request(job.job_id)

    result = host.submit_same_job_rollback(request)

    assert pve.rollbacks == []
    # Refused BEFORE `submitted`, so the operation is still releasable.
    assert result.outcome is RollbackOperationOutcome.NOT_SUBMITTED


def test_an_incomplete_target_snapshot_refuses_before_submission(
    tmp_path: Path,
) -> None:
    """Upstream `snapshot_rollback` dies on `snapstate`; we refuse first."""

    _, _, authority, _, job, ownership, identity, pve, host, _ = _mutating_job(
        tmp_path
    )
    authority.arm_package_update_rollback(
        job.job_id, _canonical_before_rollback(ownership, identity)
    )
    pve.snapshot_incomplete = True
    request = authority.package_update_rollback_request(job.job_id)

    result = host.submit_same_job_rollback(request)

    assert pve.rollbacks == []
    assert result.outcome is RollbackOperationOutcome.NOT_SUBMITTED


def test_an_in_flight_config_lock_refuses_before_submission(tmp_path: Path) -> None:
    _, _, authority, _, job, ownership, identity, pve, host, _ = _mutating_job(
        tmp_path
    )
    authority.arm_package_update_rollback(
        job.job_id, _canonical_before_rollback(ownership, identity)
    )
    pve.config_lock = "snapshot"
    request = authority.package_update_rollback_request(job.job_id)

    result = host.submit_same_job_rollback(request)

    assert pve.rollbacks == []
    assert result.outcome is RollbackOperationOutcome.NOT_SUBMITTED


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda pve: setattr(pve, "present", False), id="gone"),
        pytest.param(
            lambda pve: setattr(pve, "current_node", "pve-b"), id="moved-node"
        ),
        pytest.param(
            lambda pve: setattr(pve, "resource_type", "qemu"), id="wrong-type"
        ),
    ],
)
def test_a_wrong_live_target_is_never_rolled_back(tmp_path: Path, mutate) -> None:
    _, _, authority, _, job, ownership, identity, pve, host, _ = _mutating_job(
        tmp_path
    )
    authority.arm_package_update_rollback(
        job.job_id, _canonical_before_rollback(ownership, identity)
    )
    mutate(pve)
    request = authority.package_update_rollback_request(job.job_id)

    host.submit_same_job_rollback(request)

    assert pve.rollbacks == []


def test_the_real_submission_always_pins_start_to_zero(tmp_path: Path) -> None:
    """A successful rollback must leave the guest stopped, by construction."""

    _, _, authority, _, job, ownership, identity, pve, host, _ = _mutating_job(
        tmp_path
    )
    authority.arm_package_update_rollback(
        job.job_id, _canonical_before_rollback(ownership, identity)
    )
    request = authority.package_update_rollback_request(job.job_id)

    # The FakePve asserts `--start 0` on every submission it receives.
    host.submit_same_job_rollback(request)

    assert len(pve.rollbacks) == 1
    assert helper.ROLLBACK_START_AFTER == 0
    # And there is no way to ask for anything else: `start` is not a field of
    # the typed request at all.
    assert not hasattr(request, "start")


def test_the_helper_exposes_no_create_delete_or_lifecycle_operation() -> None:
    assert set(helper.OPERATIONS) == {
        "inspect_rollback_state",
        "submit_same_job_rollback",
        "seal_rollback_never_submitted",
    }
    source = HELPER_PATH.read_text()
    for forbidden in ("snapshot/delete", "vm_start", "pct start", "pct stop"):
        assert forbidden not in source


def _synthetic_payload(**overrides) -> dict:
    """One structurally complete request, for parser-level tests."""

    job_id = str(uuid.uuid4())
    resource_id = str(uuid.uuid4())
    backend_instance_id = str(uuid.uuid4())
    payload = {
        "request_version": 1,
        "operation": "submit_same_job_rollback",
        "target": {"vmid": 110, "expected_node": NODE},
        "operation_identity": {
            "rollback_operation_id": str(uuid.uuid4()),
            "snapshot_name": "hubinet-preupd-abc",
            "snapshot_operation_id": str(uuid.uuid4()),
        },
        "ownership": {
            "job_id": job_id,
            "resource_id": resource_id,
            "resource_continuity_revision": 1,
            "binding_id": str(uuid.uuid4()),
            "locator_generation": 1,
            "backend_instance_id": backend_instance_id,
        },
        "expected_snapshot_ownership": {
            "protocol": 1,
            "kind": "pre_update",
            "job_id": job_id,
            "resource_id": resource_id,
            "resource_continuity_revision": 1,
            "inventory_source_id": str(uuid.uuid4()),
            "backend_instance_id": backend_instance_id,
        },
    }
    for path, value in overrides.items():
        section, _, field = path.partition(".")
        if field:
            payload[section][field] = value
        else:
            payload[section] = value
    return payload


def test_a_reserved_snapshot_name_can_never_form_a_request() -> None:
    payload = _synthetic_payload(**{"operation_identity.snapshot_name": "current"})

    with pytest.raises(helper.RequestError, match="reserved"):
        helper.parse_request(payload)


def test_a_request_without_expected_snapshot_ownership_is_refused() -> None:
    payload = _synthetic_payload()
    del payload["expected_snapshot_ownership"]

    with pytest.raises(helper.RequestError, match="exact expected shape"):
        helper.parse_request(payload)


@pytest.mark.parametrize(
    "field,value",
    [
        ("protocol", 2),
        ("kind", "manual"),
        ("resource_continuity_revision", 0),
        ("job_id", "not-a-uuid"),
    ],
)
def test_malformed_expected_snapshot_ownership_is_refused(field, value) -> None:
    payload = _synthetic_payload(**{f"expected_snapshot_ownership.{field}": value})

    with pytest.raises(helper.RequestError):
        helper.parse_request(payload)


def test_expected_ownership_must_describe_this_operations_own_job() -> None:
    """A request expecting another job's snapshot metadata is incoherent."""

    payload = _synthetic_payload(
        **{"expected_snapshot_ownership.job_id": str(uuid.uuid4())}
    )

    with pytest.raises(helper.RequestError, match="own job and resource"):
        helper.parse_request(payload)


# ===========================================================================
# G. THE SUBMISSION CRITICAL SECTION
# ===========================================================================


def test_a_stale_locator_context_refuses_before_the_host_is_ever_called(
    tmp_path: Path,
) -> None:
    _, store, authority, resource, job, ownership, identity, *_ = _mutating_job(
        tmp_path
    )
    authority.arm_package_update_rollback(
        job.job_id, _canonical_before_rollback(ownership, identity)
    )
    _break_incarnation_continuity_at_the_same_locator(store, authority)
    calls = []

    with pytest.raises(RollbackSubmissionRefusedBeforeCallback):
        authority.execute_rollback_submission_if_current(
            job.job_id, lambda: calls.append("submitted")
        )

    assert calls == []


def test_a_stale_package_plan_never_withdraws_rollback_authority(
    tmp_path: Path,
) -> None:
    """The #67/#70 evidence-preservation rule, applied to compensation.

    A newer scan, or an approval gone stale, is EXPECTED after an update ran.
    It must never be able to strand a half-upgraded guest by withdrawing its
    recovery path.
    """

    _, store, authority, resource, job, ownership, identity, pve, host, orchestrator = (
        _mutating_job(tmp_path)
    )
    # Move the current world on from the approved plan entirely, the way it
    # really moves: a newer successful scan reporting different material.
    new_run = authority.issue_package_scan(job.resource_id)
    changed = authority.finalize_successful_package_scan(
        new_run.scan_run_id,
        os_id="debian",
        os_version="12",
        packages=(
            PackageScanPackage(
                package_name="unrelated",
                architecture="amd64",
                installed_version="1.0",
                candidate_version="2.0",
            ),
        ),
        reboot_required=None,
    )
    assert changed.plan_fingerprint != job.approved_plan_fingerprint

    armed = authority.arm_package_update_rollback(
        job.job_id, _canonical_before_rollback(ownership, identity)
    )
    assert armed.checkpoint is PackageUpdateCheckpoint.ROLLBACK_MAY_HAVE_STARTED

    submitted = []
    authority.execute_rollback_submission_if_current(
        job.job_id, lambda: submitted.append("ok")
    )
    assert submitted == ["ok"]


def test_a_stopped_guest_is_still_rollback_eligible(tmp_path: Path) -> None:
    """PVE force-stops the guest anyway; requiring `running` would fence
    exactly the guests that most need recovery."""

    _, store, authority, resource, job, ownership, identity, *_ = _mutating_job(
        tmp_path
    )
    with store._transaction() as connection:
        connection.execute(
            "UPDATE resource_incarnations SET status='stopped' WHERE resource_id=?",
            (job.resource_id,),
        )

    armed = authority.arm_package_update_rollback(
        job.job_id, _canonical_before_rollback(ownership, identity)
    )

    assert armed.checkpoint is PackageUpdateCheckpoint.ROLLBACK_MAY_HAVE_STARTED
    submitted = []
    authority.execute_rollback_submission_if_current(
        job.job_id, lambda: submitted.append("ok")
    )
    assert submitted == ["ok"]


def test_an_interleaving_writer_cannot_race_the_submission_boundary(
    tmp_path: Path,
) -> None:
    """The proof and the submission it authorizes share one transaction."""

    _, store, authority, resource, job, ownership, identity, *_ = _mutating_job(
        tmp_path
    )
    authority.arm_package_update_rollback(
        job.job_id, _canonical_before_rollback(ownership, identity)
    )
    seen: list[str] = []

    def _seam(connection, *, job_id):
        # Anything a concurrent writer could have done must be impossible
        # here: this transaction holds the store's one writer lock.
        seen.append("seam")

    authority._after_rollback_authority_proof = _seam
    authority.execute_rollback_submission_if_current(
        job.job_id, lambda: seen.append("submit")
    )

    assert seen == ["seam", "submit"]


# ===========================================================================
# H. END-TO-END ORCHESTRATION
# ===========================================================================


def _drive_to_successful_rollback(
    authority, orchestrator, job, ownership, identity, pve
):
    observed = _canonical_before_rollback(ownership, identity)
    # First pass submits and captures the task, which is still running.
    first = orchestrator.roll_back_to_job_snapshot(job.job_id, observed)
    assert first.outcome is RollbackOperationOutcome.UNCERTAIN
    _complete_rollback(pve)
    return orchestrator.roll_back_to_job_snapshot(job.job_id, observed)


def test_a_proven_rollback_terminalizes_the_job_rolled_back_exactly_once(
    tmp_path: Path,
) -> None:
    _, store, authority, _, job, ownership, identity, pve, host, orchestrator = (
        _mutating_job(tmp_path)
    )

    result = _drive_to_successful_rollback(
        authority, orchestrator, job, ownership, identity, pve
    )

    assert result.outcome is RollbackOperationOutcome.COMPLETED
    assert result.job.status is PackageUpdateJobStatus.ROLLED_BACK
    assert result.job.checkpoint is PackageUpdateCheckpoint.ROLLBACK_COMPLETED
    assert result.job.rollback_completed_at is not None
    assert result.job.rollback_task_upid == UPID
    # A rolled-back update is never a successful update.
    assert result.job.status is not PackageUpdateJobStatus.SUCCEEDED
    assert len(pve.rollbacks) == 1
    assert _events(store, job.job_id).count(
        PackageUpdateEventType.ROLLBACK_COMPLETED
    ) == 1


def test_a_proven_rollback_releases_the_global_destructive_slot(
    tmp_path: Path,
) -> None:
    _, store, authority, _, job, ownership, identity, pve, host, orchestrator = (
        _mutating_job(tmp_path)
    )
    _drive_to_successful_rollback(
        authority, orchestrator, job, ownership, identity, pve
    )

    with store._transaction() as connection:
        active = connection.execute(
            "SELECT COUNT(*) AS n FROM package_update_jobs WHERE status='active'"
        ).fetchone()["n"]
    assert active == 0


def test_a_proven_rollback_retains_the_job_owned_snapshot(tmp_path: Path) -> None:
    """No deletion anywhere in this stage."""

    _, _, authority, _, job, ownership, identity, pve, host, orchestrator = (
        _mutating_job(tmp_path)
    )
    _drive_to_successful_rollback(
        authority, orchestrator, job, ownership, identity, pve
    )

    assert pve.snapshot_present is True
    # The helper has no delete capability at all: no operation exposes one,
    # and no `pvesh delete` argv exists anywhere in the file.
    assert not any("delete" in operation for operation in helper.OPERATIONS)
    assert '"delete"' not in HELPER_PATH.read_text()


def test_an_unproven_mutation_rolls_back_without_ever_claiming_completion(
    tmp_path: Path,
) -> None:
    _, _, authority, _, job, ownership, identity, pve, host, orchestrator = (
        _mutating_job(tmp_path)
    )

    result = _drive_to_successful_rollback(
        authority, orchestrator, job, ownership, identity, pve
    )

    assert result.job.status is PackageUpdateJobStatus.ROLLED_BACK
    assert result.job.mutation_completed_at is None


def test_a_running_task_stays_uncertain_and_is_never_resubmitted(
    tmp_path: Path,
) -> None:
    _, _, authority, _, job, ownership, identity, pve, host, orchestrator = (
        _mutating_job(tmp_path)
    )
    observed = _canonical_before_rollback(ownership, identity)

    result = orchestrator.roll_back_to_job_snapshot(job.job_id, observed)

    assert result.outcome is RollbackOperationOutcome.UNCERTAIN
    assert result.job.status is PackageUpdateJobStatus.ACTIVE
    assert result.job.checkpoint is PackageUpdateCheckpoint.ROLLBACK_MAY_HAVE_STARTED
    assert len(pve.rollbacks) == 1


def test_a_terminal_failed_task_keeps_the_job_owned_and_fenced(
    tmp_path: Path,
) -> None:
    _, store, authority, _, job, ownership, identity, pve, host, orchestrator = (
        _mutating_job(tmp_path)
    )
    observed = _canonical_before_rollback(ownership, identity)
    orchestrator.roll_back_to_job_snapshot(job.job_id, observed)
    pve.task_status = "stopped"
    pve.task_exitstatus = "rollback failed: storage error"

    result = orchestrator.roll_back_to_job_snapshot(job.job_id, observed)

    assert result.outcome is RollbackOperationOutcome.FAILED
    assert result.job.status is PackageUpdateJobStatus.ACTIVE
    assert result.job.rollback_completed_at is None
    assert (
        PackageUpdateEventType.ROLLBACK_TERMINAL_FAILURE
        in _events(store, job.job_id)
    )
    # Never retried.
    orchestrator.roll_back_to_job_snapshot(job.job_id, observed)
    assert len(pve.rollbacks) == 1


def test_a_warnings_exit_status_is_a_non_error_by_pves_own_rule(
    tmp_path: Path,
) -> None:
    _, _, authority, _, job, ownership, identity, pve, host, orchestrator = (
        _mutating_job(tmp_path)
    )
    observed = _canonical_before_rollback(ownership, identity)
    orchestrator.roll_back_to_job_snapshot(job.job_id, observed)
    pve.task_status = "stopped"
    pve.task_exitstatus = "WARNINGS: 2"

    result = orchestrator.roll_back_to_job_snapshot(job.job_id, observed)

    assert result.outcome is RollbackOperationOutcome.COMPLETED
    assert result.job.status is PackageUpdateJobStatus.ROLLED_BACK


def test_a_stopped_task_with_no_exit_status_is_never_success(tmp_path: Path) -> None:
    _, _, authority, _, job, ownership, identity, pve, host, orchestrator = (
        _mutating_job(tmp_path)
    )
    observed = _canonical_before_rollback(ownership, identity)
    orchestrator.roll_back_to_job_snapshot(job.job_id, observed)
    pve.task_status = "stopped"
    pve.task_exitstatus = None

    result = orchestrator.roll_back_to_job_snapshot(job.job_id, observed)

    assert result.outcome is RollbackOperationOutcome.UNCERTAIN
    assert result.job.status is PackageUpdateJobStatus.ACTIVE


def test_completion_requires_the_current_parent_post_condition(
    tmp_path: Path,
) -> None:
    """`parent == snapname` is a required member of the evidence set."""

    _, _, authority, _, job, ownership, identity, pve, host, orchestrator = (
        _mutating_job(tmp_path)
    )
    authority.arm_package_update_rollback(
        job.job_id, _canonical_before_rollback(ownership, identity)
    )
    authority.record_package_update_rollback_task(job.job_id, UPID)

    with pytest.raises(AuthorityConflict, match="current parent"):
        authority.complete_package_update_rollback(
            job.job_id,
            _canonical_before_rollback(ownership, identity),
            task_succeeded=True,
        )


def test_completion_requires_the_durable_task_identity(tmp_path: Path) -> None:
    """A surviving snapshot proves nothing: upstream never deletes it."""

    _, _, authority, _, job, ownership, identity, *_ = _mutating_job(tmp_path)
    authority.arm_package_update_rollback(
        job.job_id, _canonical_before_rollback(ownership, identity)
    )

    with pytest.raises(AuthorityConflict, match="task identity"):
        authority.complete_package_update_rollback(
            job.job_id,
            _canonical_after_rollback(ownership, identity),
            task_succeeded=True,
        )


def test_completion_requires_a_terminal_non_error_task(tmp_path: Path) -> None:
    _, _, authority, _, job, ownership, identity, *_ = _mutating_job(tmp_path)
    authority.arm_package_update_rollback(
        job.job_id, _canonical_before_rollback(ownership, identity)
    )
    authority.record_package_update_rollback_task(job.job_id, UPID)

    with pytest.raises(AuthorityConflict, match="non-error PVE task"):
        authority.complete_package_update_rollback(
            job.job_id,
            _canonical_after_rollback(ownership, identity),
            task_succeeded=False,
        )


def test_completion_is_idempotent(tmp_path: Path) -> None:
    _, _, authority, _, job, ownership, identity, pve, host, orchestrator = (
        _mutating_job(tmp_path)
    )
    first = _drive_to_successful_rollback(
        authority, orchestrator, job, ownership, identity, pve
    )
    second = authority.complete_package_update_rollback(
        job.job_id,
        _canonical_after_rollback(ownership, identity),
        task_succeeded=True,
    )

    assert second.rollback_completed_at == first.job.rollback_completed_at
    assert second.status is PackageUpdateJobStatus.ROLLED_BACK


# ===========================================================================
# I. CRASH, RESTART, AND LOST-RESPONSE RECOVERY
# ===========================================================================


def test_a_crash_before_the_write_ahead_commit_leaves_nothing_to_recover(
    tmp_path: Path,
) -> None:
    _, store, authority, _, job, ownership, identity, pve, host, _ = _mutating_job(
        tmp_path
    )

    reloaded = InventoryAuthority(store, now=lambda: authority._now())
    recovered = reloaded.package_update_job(job.job_id)

    assert recovered.checkpoint is PackageUpdateCheckpoint.MUTATION_MAY_HAVE_STARTED
    assert recovered.rollback_operation_id is None
    assert pve.rollbacks == []


def test_a_crash_after_arming_but_before_submission_seals_rather_than_guessing(
    tmp_path: Path,
) -> None:
    _, store, authority, _, job, ownership, identity, pve, host, orchestrator = (
        _mutating_job(tmp_path)
    )
    authority.arm_package_update_rollback(
        job.job_id, _canonical_before_rollback(ownership, identity)
    )
    # The guest is gone for good, so every future submission would refuse
    # identically and fence the global slot forever.
    pve.present = False
    _break_incarnation_continuity_at_the_same_locator(store, authority)

    result = orchestrator.roll_back_to_job_snapshot(
        job.job_id, _canonical_before_rollback(ownership, identity)
    )

    assert result.outcome is RollbackOperationOutcome.NOT_SUBMITTED
    assert result.job.status is PackageUpdateJobStatus.BLOCKED
    assert result.job.rollback_completed_at is None
    assert pve.rollbacks == []


def test_a_transient_absent_journal_is_never_trusted_as_a_release_proof(
    tmp_path: Path,
) -> None:
    """A helper launched by a dead backend may not hold its lease yet."""

    _, store, authority, _, job, ownership, identity, pve, host, orchestrator = (
        _mutating_job(tmp_path)
    )
    authority.arm_package_update_rollback(
        job.job_id, _canonical_before_rollback(ownership, identity)
    )
    request = authority.package_update_rollback_request(job.job_id)

    inspected = host.inspect_rollback_state(request)
    assert inspected.rollback_state is HostRollbackState.ABSENT
    # Absence is transient routing evidence, never a release proof, so it is
    # deliberately UNCERTAIN rather than NOT_SUBMITTED.
    assert inspected.outcome is RollbackOperationOutcome.UNCERTAIN

    # Absence alone must NOT have released anything.
    assert (
        authority.package_update_job(job.job_id).status
        is PackageUpdateJobStatus.ACTIVE
    )


def test_only_the_durable_seal_releases_an_armed_job(tmp_path: Path) -> None:
    _, store, authority, _, job, ownership, identity, pve, host, orchestrator = (
        _mutating_job(tmp_path)
    )
    authority.arm_package_update_rollback(
        job.job_id, _canonical_before_rollback(ownership, identity)
    )
    request = authority.package_update_rollback_request(job.job_id)

    def _seal_reporting(state):
        return lambda: (state, "reason", host.inspect_rollback_state(request))

    blocked, _ = authority.resolve_pre_rollback_block(
        job.job_id, _seal_reporting(HostRollbackState.INTENT)
    )
    assert blocked is False
    blocked, _ = authority.resolve_pre_rollback_block(
        job.job_id, _seal_reporting(HostRollbackState.ABSENT)
    )
    assert blocked is False
    blocked, _ = authority.resolve_pre_rollback_block(
        job.job_id, _seal_reporting(HostRollbackState.SEALED_NOT_SUBMITTED)
    )
    assert blocked is True
    assert (
        authority.package_update_job(job.job_id).status
        is PackageUpdateJobStatus.BLOCKED
    )


def test_a_recorded_task_permanently_forbids_the_pre_submission_seal(
    tmp_path: Path,
) -> None:
    _, _, authority, _, job, ownership, identity, pve, host, _ = _mutating_job(
        tmp_path
    )
    authority.arm_package_update_rollback(
        job.job_id, _canonical_before_rollback(ownership, identity)
    )
    authority.record_package_update_rollback_task(job.job_id, UPID)

    with pytest.raises(AuthorityConflict, match="already observed"):
        authority.resolve_pre_rollback_block(
            job.job_id,
            lambda: (HostRollbackState.SEALED_NOT_SUBMITTED, "lying seal", None),
        )


def test_a_lost_submission_response_never_resubmits(tmp_path: Path) -> None:
    _, store, authority, _, job, ownership, identity, pve, host, orchestrator = (
        _mutating_job(tmp_path)
    )
    observed = _canonical_before_rollback(ownership, identity)
    host.drop_response.add("submit_same_job_rollback")

    first = orchestrator.roll_back_to_job_snapshot(job.job_id, observed)

    assert first.outcome is RollbackOperationOutcome.UNCERTAIN
    assert first.job.status is PackageUpdateJobStatus.ACTIVE
    assert len(pve.rollbacks) == 1

    # A later attempt reattaches to the SAME operation rather than resubmitting.
    host.drop_response.clear()
    _complete_rollback(pve)
    second = orchestrator.roll_back_to_job_snapshot(job.job_id, observed)

    assert second.outcome is RollbackOperationOutcome.COMPLETED
    assert len(pve.rollbacks) == 1


def test_a_backend_restart_reattaches_instead_of_resubmitting(
    tmp_path: Path,
) -> None:
    clock, store, authority, _, job, ownership, identity, pve, host, orchestrator = (
        _mutating_job(tmp_path)
    )
    observed = _canonical_before_rollback(ownership, identity)
    orchestrator.roll_back_to_job_snapshot(job.job_id, observed)
    assert len(pve.rollbacks) == 1

    # Restart: a brand new authority over the same durable store.
    path = store.path
    store.close()
    reopened = InventoryAuthorityStore(path, now=clock)
    restarted = InventoryAuthority(reopened, now=clock)
    interrupted = restarted.recover_interrupted_package_update_jobs()

    assert job.job_id not in interrupted
    recovered = restarted.package_update_job(job.job_id)
    assert recovered.status is PackageUpdateJobStatus.ACTIVE
    assert recovered.checkpoint is PackageUpdateCheckpoint.ROLLBACK_MAY_HAVE_STARTED
    assert recovered.rollback_task_upid == UPID

    restarted_orchestrator = PackageUpdateRollbackOrchestrator(
        restarted,
        host,
        sleep=lambda _s: None,
        monotonic=_FakeMonotonic(5.0),
        task_poll_timeout_seconds=30.0,
        task_poll_interval_seconds=5.0,
    )
    _complete_rollback(pve)
    result = restarted_orchestrator.roll_back_to_job_snapshot(job.job_id, observed)

    assert result.outcome is RollbackOperationOutcome.COMPLETED
    assert len(pve.rollbacks) == 1


def test_startup_recovery_never_interrupts_a_rollback_in_flight(
    tmp_path: Path,
) -> None:
    _, store, authority, _, job, ownership, identity, pve, host, orchestrator = (
        _mutating_job(tmp_path)
    )
    authority.arm_package_update_rollback(
        job.job_id, _canonical_before_rollback(ownership, identity)
    )

    recovered = authority.recover_interrupted_package_update_jobs()

    assert job.job_id not in recovered
    after = authority.package_update_job(job.job_id)
    assert after.status is PackageUpdateJobStatus.ACTIVE
    assert after.checkpoint is PackageUpdateCheckpoint.ROLLBACK_MAY_HAVE_STARTED


def test_a_terminal_success_before_the_db_commit_is_recovered_not_repeated(
    tmp_path: Path,
) -> None:
    """The task finished; the backend died before recording it."""

    _, store, authority, _, job, ownership, identity, pve, host, orchestrator = (
        _mutating_job(tmp_path)
    )
    observed = _canonical_before_rollback(ownership, identity)
    orchestrator.roll_back_to_job_snapshot(job.job_id, observed)
    _complete_rollback(pve)

    # A fresh orchestrator, as if nothing after submission had been recorded.
    result = orchestrator.roll_back_to_job_snapshot(job.job_id, observed)

    assert result.outcome is RollbackOperationOutcome.COMPLETED
    assert result.job.status is PackageUpdateJobStatus.ROLLED_BACK
    assert len(pve.rollbacks) == 1


def test_a_submitted_operation_without_a_task_identity_stays_uncertain(
    tmp_path: Path,
) -> None:
    """Rollback has no unique canonical witness of its own."""

    _, _, authority, _, job, ownership, identity, pve, host, orchestrator = (
        _mutating_job(tmp_path)
    )
    pve.returned_upid = None
    observed = _canonical_before_rollback(ownership, identity)

    result = orchestrator.roll_back_to_job_snapshot(job.job_id, observed)

    assert result.outcome is RollbackOperationOutcome.UNCERTAIN
    assert result.job.status is PackageUpdateJobStatus.ACTIVE
    assert result.job.rollback_task_upid is None
    # And it is never released as never-submitted, nor resubmitted.
    assert len(pve.rollbacks) == 1
    again = orchestrator.roll_back_to_job_snapshot(job.job_id, observed)
    assert again.outcome is RollbackOperationOutcome.UNCERTAIN
    assert len(pve.rollbacks) == 1


def test_an_unreadable_pve_task_status_stays_uncertain(tmp_path: Path) -> None:
    _, _, authority, _, job, ownership, identity, pve, host, orchestrator = (
        _mutating_job(tmp_path)
    )
    observed = _canonical_before_rollback(ownership, identity)
    orchestrator.roll_back_to_job_snapshot(job.job_id, observed)
    pve.fail_reads.add("task")

    result = orchestrator.roll_back_to_job_snapshot(job.job_id, observed)

    assert result.outcome is RollbackOperationOutcome.UNCERTAIN
    assert result.job.status is PackageUpdateJobStatus.ACTIVE


def test_a_host_answering_a_different_operation_is_uncertain(
    tmp_path: Path,
) -> None:
    _, _, authority, _, job, ownership, identity, pve, host, orchestrator = (
        _mutating_job(tmp_path)
    )
    authority.arm_package_update_rollback(
        job.job_id, _canonical_before_rollback(ownership, identity)
    )

    class _Wrong:
        def inspect_rollback_state(self, request):
            return HostRollbackResult(
                outcome=RollbackOperationOutcome.COMPLETED,
                rollback_operation_id=str(uuid.uuid4()),
            )

        submit_same_job_rollback = inspect_rollback_state
        seal_rollback_never_submitted = inspect_rollback_state

    wrong = PackageUpdateRollbackOrchestrator(
        authority,
        _Wrong(),
        sleep=lambda _s: None,
        monotonic=_FakeMonotonic(5.0),
    )
    result = wrong.roll_back_to_job_snapshot(
        job.job_id, _canonical_before_rollback(ownership, identity)
    )

    assert result.outcome is RollbackOperationOutcome.UNCERTAIN
    assert result.job.status is PackageUpdateJobStatus.ACTIVE


# ===========================================================================
# J. CONCURRENCY
# ===========================================================================


def test_two_rollback_callers_produce_exactly_one_submission(
    tmp_path: Path,
) -> None:
    _, _, authority, _, job, ownership, identity, pve, host, orchestrator = (
        _mutating_job(tmp_path)
    )
    observed = _canonical_before_rollback(ownership, identity)
    second = PackageUpdateRollbackOrchestrator(
        authority,
        host,
        sleep=lambda _s: None,
        monotonic=_FakeMonotonic(5.0),
        task_poll_timeout_seconds=30.0,
        task_poll_interval_seconds=5.0,
    )

    orchestrator.roll_back_to_job_snapshot(job.job_id, observed)
    second.roll_back_to_job_snapshot(job.job_id, observed)

    assert len(pve.rollbacks) == 1


def test_a_seal_and_a_delayed_submitter_are_ordered_by_the_host_lease(
    tmp_path: Path,
) -> None:
    _, _, authority, _, job, ownership, identity, pve, host, _ = _mutating_job(
        tmp_path
    )
    authority.arm_package_update_rollback(
        job.job_id, _canonical_before_rollback(ownership, identity)
    )
    request = authority.package_update_rollback_request(job.job_id)

    # Seal wins the lease first.
    host.seal_rollback_never_submitted(request)
    # The delayed submitter reads the seal when it finally takes the lease.
    host.submit_same_job_rollback(request)

    assert pve.rollbacks == []


def test_uncertainty_recording_tolerates_a_concurrent_terminalization(
    tmp_path: Path,
) -> None:
    _, store, authority, _, job, ownership, identity, pve, host, orchestrator = (
        _mutating_job(tmp_path)
    )
    _drive_to_successful_rollback(
        authority, orchestrator, job, ownership, identity, pve
    )

    # The job is terminal. Recording uncertainty must not raise out of the
    # public surface, nor reopen the terminal answer.
    result = orchestrator._uncertain(job.job_id, "late uncertainty")

    assert result.outcome is RollbackOperationOutcome.UNCERTAIN
    assert result.job.status is PackageUpdateJobStatus.ROLLED_BACK


def test_only_one_job_may_own_a_given_rollback_operation_identity(
    tmp_path: Path,
) -> None:
    _, store, authority, _, job, ownership, identity, *_ = _mutating_job(tmp_path)
    authority.arm_package_update_rollback(
        job.job_id, _canonical_before_rollback(ownership, identity)
    )
    operation_id = authority.package_update_job(job.job_id).rollback_operation_id

    with store._transaction() as connection:
        rows = connection.execute(
            "SELECT COUNT(*) AS n FROM package_update_jobs "
            "WHERE rollback_operation_id=?",
            (operation_id,),
        ).fetchone()["n"]
    assert rows == 1


# ===========================================================================
# K. TRANSPORT CONTRACT
# ===========================================================================


def test_the_transport_refuses_an_unbounded_submission_timeout() -> None:
    from app.inventory import MAX_ROLLBACK_SUBMISSION_TIMEOUT_SECONDS

    with pytest.raises(ValueError, match="submission timeout"):
        SshPackageUpdateRollbackHostControl(
            host="pve.example",
            port=22,
            user="hubinet",
            private_key_path=Path("/k"),
            known_hosts_path=Path("/h"),
            submission_timeout_seconds=MAX_ROLLBACK_SUBMISSION_TIMEOUT_SECONDS + 1,
            inspection_timeout_seconds=120,
            max_result_bytes=1024,
        )


def test_the_writer_budget_still_exceeds_every_critical_section() -> None:
    from app.inventory import contention_policy as policy

    assert (
        policy.AUTHORITY_WRITER_WAIT_BUDGET_SECONDS
        > policy.MAX_ROLLBACK_CRITICAL_SECTION_SECONDS
    )
    assert policy.MAX_HOST_CRITICAL_SECTION_SECONDS >= (
        policy.MAX_ROLLBACK_CRITICAL_SECTION_SECONDS
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"response_version": 2, "ok": True},
        {"response_version": 1, "ok": True, "outcome": "made-up"},
        {"response_version": 1, "ok": True, "outcome": "completed", "task_upid": 7},
    ],
)
def test_a_malformed_host_response_is_uncertain(payload) -> None:
    transport = _transport()
    operation_id = str(uuid.uuid4())

    result = transport._parse_payload(
        {**payload, "rollback_operation_id": operation_id}, operation_id
    )

    assert result.outcome is RollbackOperationOutcome.UNCERTAIN


def test_a_submitted_state_can_never_report_completion() -> None:
    """Unlike snapshot create, rollback completion REQUIRES the task id."""

    transport = _transport()
    operation_id = str(uuid.uuid4())

    result = transport._parse_payload(
        {
            "response_version": 1,
            "ok": True,
            "rollback_operation_id": operation_id,
            "outcome": "completed",
            "rollback_state": "submitted",
        },
        operation_id,
    )

    assert result.outcome is RollbackOperationOutcome.UNCERTAIN


def test_only_the_exact_not_submitted_token_reports_a_release() -> None:
    transport = _transport()
    operation_id = str(uuid.uuid4())

    released = transport._parse_payload(
        {
            "response_version": 1,
            "ok": False,
            "rollback_operation_id": operation_id,
            "error": {"classification": "sealed", "submission": "not_submitted"},
        },
        operation_id,
    )
    unknown = transport._parse_payload(
        {
            "response_version": 1,
            "ok": False,
            "rollback_operation_id": operation_id,
            "error": {"classification": "sealed", "submission": "maybe"},
        },
        operation_id,
    )

    assert released.outcome is RollbackOperationOutcome.NOT_SUBMITTED
    assert unknown.outcome is RollbackOperationOutcome.UNCERTAIN


# ===========================================================================
# L. FINAL HOST-SIDE OWNERSHIP PROOF (a snapshot NAME is never ownership)
# ===========================================================================
#
# Authority proves the exact same-job ownership when it ARMS the rollback.
# These regressions cover the window AFTER that proof and BEFORE the
# destructive `pvesh` call, in which the physical snapshot name can survive
# while its ownership metadata changes underneath. The load-bearing shape is
# always the same: arm with correct ownership, mutate ONLY the fake PVE
# description, submit, and require zero rollbacks.


def _armed_request(authority, job, ownership, identity):
    """Arm the rollback with correct ownership and return its typed request."""

    authority.arm_package_update_rollback(
        job.job_id, _canonical_before_rollback(ownership, identity)
    )
    return authority.package_update_rollback_request(job.job_id)


def _foreign_description(job, identity, **overrides) -> str:
    """A well-formed Hubinet marker describing someone else's snapshot."""

    from app.inventory.snapshot_identity import (
        build_snapshot_ownership,
        encode_snapshot_description,
    )

    fields = {
        "job_id": job.job_id,
        "resource_id": job.resource_id,
        "resource_continuity_revision": job.expected_resource_continuity_revision,
        "inventory_source_id": job.inventory_source_id,
        "backend_instance_id": str(uuid.uuid4()),
    }
    fields.update(overrides)
    return encode_snapshot_description(build_snapshot_ownership(**fields)) + "\n"


def test_correct_ownership_still_submits_exactly_one_rollback(
    tmp_path: Path,
) -> None:
    """Positive control: the legal path stays legal."""

    _, _, authority, _, job, ownership, identity, pve, host, _ = _mutating_job(
        tmp_path
    )
    request = _armed_request(authority, job, ownership, identity)

    result = host.submit_same_job_rollback(request)

    assert len(pve.rollbacks) == 1
    assert result.rollback_state is HostRollbackState.TASK_KNOWN


def test_ownership_metadata_disappearing_after_arm_refuses_the_rollback(
    tmp_path: Path,
) -> None:
    """The exact witness this correction closes.

    Authority armed against a correctly-owned snapshot. Before the host
    submits, the same physical name comes to carry ordinary manual metadata.
    """

    _, _, authority, _, job, ownership, identity, pve, host, _ = _mutating_job(
        tmp_path
    )
    request = _armed_request(authority, job, ownership, identity)
    pve.snapshot_description = "taken by hand before a config change\n"

    result = host.submit_same_job_rollback(request)

    assert pve.rollbacks == []
    assert result.outcome is RollbackOperationOutcome.NOT_SUBMITTED
    # Refused BEFORE the durable boundary, so the operation stays releasable.
    assert host.journal.read(request.rollback_operation_id) is None


def test_another_jobs_ownership_after_arm_refuses_the_rollback(
    tmp_path: Path,
) -> None:
    _, _, authority, _, job, ownership, identity, pve, host, _ = _mutating_job(
        tmp_path
    )
    request = _armed_request(authority, job, ownership, identity)
    pve.snapshot_description = _foreign_description(
        job, identity, job_id=str(uuid.uuid4())
    )

    host.submit_same_job_rollback(request)

    assert pve.rollbacks == []
    assert host.journal.read(request.rollback_operation_id) is None


@pytest.mark.parametrize(
    "override",
    [
        pytest.param({"resource_id": str(uuid.uuid4())}, id="wrong-resource"),
        pytest.param({"resource_continuity_revision": 99}, id="wrong-incarnation"),
        pytest.param(
            {"inventory_source_id": str(uuid.uuid4())}, id="wrong-source"
        ),
    ],
)
def test_wrong_resource_ownership_after_arm_refuses_the_rollback(
    tmp_path: Path, override
) -> None:
    _, _, authority, _, job, ownership, identity, pve, host, _ = _mutating_job(
        tmp_path
    )
    request = _armed_request(authority, job, ownership, identity)
    # Keep this job's own job_id so the entry still claims THIS job -- only
    # the resource incarnation facts differ.
    pve.snapshot_description = _foreign_description(
        job, identity, backend_instance_id=ownership.backend_instance_id, **override
    )

    host.submit_same_job_rollback(request)

    assert pve.rollbacks == []


def test_wrong_backend_ownership_after_arm_refuses_the_rollback(
    tmp_path: Path,
) -> None:
    """A reused VMID on a different Hubinet installation never inherits it."""

    _, _, authority, _, job, ownership, identity, pve, host, _ = _mutating_job(
        tmp_path
    )
    request = _armed_request(authority, job, ownership, identity)
    pve.snapshot_description = _foreign_description(job, identity)

    host.submit_same_job_rollback(request)

    assert pve.rollbacks == []


def test_malformed_hubinet_ownership_after_arm_fails_closed(tmp_path: Path) -> None:
    _, _, authority, _, job, ownership, identity, pve, host, _ = _mutating_job(
        tmp_path
    )
    request = _armed_request(authority, job, ownership, identity)
    # Claims to be Hubinet's, but will not parse strictly.
    pve.snapshot_description = "hubinet-ops-snapshot-v1 {not-json\n"

    host.submit_same_job_rollback(request)

    assert pve.rollbacks == []


def test_a_second_entry_claiming_this_job_fails_closed(tmp_path: Path) -> None:
    """Ambiguity is never resolved in the operation's favour."""

    from app.inventory.snapshot_identity import encode_snapshot_description

    _, _, authority, _, job, ownership, identity, pve, host, _ = _mutating_job(
        tmp_path
    )
    request = _armed_request(authority, job, ownership, identity)
    pve.extra_rows = [
        {
            "name": "hubinet-preupd-duplicate",
            "description": encode_snapshot_description(ownership) + "\n",
            "snaptime": 2,
        }
    ]

    host.submit_same_job_rollback(request)

    assert pve.rollbacks == []


def test_malformed_metadata_on_an_unrelated_entry_fails_closed(
    tmp_path: Path,
) -> None:
    """An unattributable Hubinet-looking entry cannot be ruled out."""

    _, _, authority, _, job, ownership, identity, pve, host, _ = _mutating_job(
        tmp_path
    )
    request = _armed_request(authority, job, ownership, identity)
    pve.extra_rows = [
        {
            "name": "hubinet-preupd-broken",
            "description": "hubinet-ops-snapshot-v1 nonsense\n",
            "snaptime": 2,
        }
    ]

    host.submit_same_job_rollback(request)

    assert pve.rollbacks == []


def test_expected_ownership_is_bound_into_the_request_fingerprint(
    tmp_path: Path,
) -> None:
    """A different expected ownership can never reuse an existing journal."""

    _, _, authority, _, job, ownership, identity, pve, host, _ = _mutating_job(
        tmp_path
    )
    authority.arm_package_update_rollback(
        job.job_id, _canonical_before_rollback(ownership, identity)
    )
    payload = _helper_request(authority, job.job_id)
    helper.handle(payload, runner=pve.runner, journal=host.journal)

    poisoned = json.loads(json.dumps(payload))
    poisoned["expected_snapshot_ownership"]["inventory_source_id"] = str(uuid.uuid4())
    poisoned["ownership"]["job_id"] = poisoned["expected_snapshot_ownership"]["job_id"]

    with pytest.raises(helper.RollbackError) as raised:
        helper.handle(poisoned, runner=pve.runner, journal=host.journal)
    assert raised.value.classification == "request_mismatch"


def test_the_expected_ownership_comes_from_the_one_authority_derivation(
    tmp_path: Path,
) -> None:
    """No second derivation of a job's snapshot ownership exists."""

    _, _, authority, _, job, ownership, identity, *_ = _mutating_job(tmp_path)

    request = authority.package_update_rollback_request(job.job_id)

    assert request.expected_snapshot_ownership == ownership
    assert request.expected_snapshot_ownership == (
        authority.package_update_snapshot_ownership(job.job_id)
    )


# ===========================================================================
# M. ANY CONFIG LOCK REFUSES BEFORE THE SUBMITTED BOUNDARY
# ===========================================================================
#
# Upstream `PVE::AbstractConfig::check_lock` dies whenever `$conf->{lock}` is
# truthy -- it does not accept `backup`, `migrate`, or any other lock type.
# A curated allowlist of snapshot-family locks let a legitimately locked
# container reach the durable `submitted` record, after which PVE refuses the
# rollback and the operation can no longer be sealed or retried.


@pytest.mark.parametrize(
    "lock",
    ["snapshot", "snapshot-delete", "rollback", "backup", "migrate", "clone",
     "create", "disk", "fstrim", "mounted", "destroyed", "unknown-future-lock"],
)
def test_any_non_empty_config_lock_refuses_before_submitted(
    tmp_path: Path, lock: str
) -> None:
    _, _, authority, _, job, ownership, identity, pve, host, _ = _mutating_job(
        tmp_path
    )
    request = _armed_request(authority, job, ownership, identity)
    pve.config_lock = lock

    result = host.submit_same_job_rollback(request)

    assert pve.rollbacks == []
    assert result.outcome is RollbackOperationOutcome.NOT_SUBMITTED
    # Nothing durable was written, so the operation is still releasable.
    assert host.journal.read(request.rollback_operation_id) is None


def test_an_empty_config_lock_fails_closed(tmp_path: Path) -> None:
    """PVE never emits one; treating it as unlocked would be permissive."""

    _, _, authority, _, job, ownership, identity, pve, host, _ = _mutating_job(
        tmp_path
    )
    request = _armed_request(authority, job, ownership, identity)
    pve.config_lock = ""

    host.submit_same_job_rollback(request)

    assert pve.rollbacks == []


def test_no_config_lock_still_submits_normally(tmp_path: Path) -> None:
    """Positive control for the lock family."""

    _, _, authority, _, job, ownership, identity, pve, host, _ = _mutating_job(
        tmp_path
    )
    request = _armed_request(authority, job, ownership, identity)
    assert pve.config_lock is None

    host.submit_same_job_rollback(request)

    assert len(pve.rollbacks) == 1


def test_the_helper_keeps_no_config_lock_allowlist() -> None:
    """The refusal must be "any lock", not a curated set of names."""

    source = HELPER_PATH.read_text()
    assert "_IN_FLIGHT_LOCKS" not in source
    assert "snapshot-delete" not in source


# ===========================================================================
# N. THE COMPLETION/STATUS SCHEMA FAMILY
# ===========================================================================
#
# The application proof in `complete_package_update_rollback` is correct and
# already requires the durable task identity. These regressions cover the
# SCHEMA itself: v14 is supposed to reject impossible durable states caused by
# a buggy backend SQL statement, not merely by a well-behaved caller.
#
# The witness is deliberately the FULL coherent terminal write -- checkpoint,
# completion timestamp, status, and both terminal fields together -- because
# the weaker "status only" attempt was already rejected by other constraints
# and therefore never exercised this one.


def _armed_rollback_job(tmp_path: Path):
    """One ACTIVE job at rollback_may_have_started with NO task identity."""

    _, store, authority, _, job, ownership, identity, *_ = _mutating_job(tmp_path)
    authority.arm_package_update_rollback(
        job.job_id, _canonical_before_rollback(ownership, identity)
    )
    armed = authority.package_update_job(job.job_id)
    assert armed.checkpoint is PackageUpdateCheckpoint.ROLLBACK_MAY_HAVE_STARTED
    assert armed.rollback_task_upid is None
    assert armed.rollback_completed_at is None
    return store, authority, job, ownership, identity


def test_a_coherent_rolled_back_write_without_a_task_identity_is_rejected(
    tmp_path: Path,
) -> None:
    """The exact whole-PR review witness.

    A rollback has no unique canonical witness of its own -- the source
    snapshot survives either way, and `parent == snapname` is equally true
    after any earlier rollback to the same snapshot. The recorded UPID is the
    only durable fact tying a completion to THIS operation, so persisting
    `rolled_back` without it would release the global destructive slot and
    discard recovery ownership for a rollback PVE was never durably known to
    have accepted.
    """

    store, _, job, _, _ = _armed_rollback_job(tmp_path)

    _assert_sql_rejected(
        store,
        "UPDATE package_update_jobs SET checkpoint='rollback_completed', "
        "rollback_completed_at=?, status='rolled_back', terminalized_at=?, "
        "terminal_reason='forged' WHERE job_id=?",
        ("2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00", job.job_id),
    )
    after = store.package_update_job(job.job_id)
    assert after.status is PackageUpdateJobStatus.ACTIVE
    assert after.checkpoint is PackageUpdateCheckpoint.ROLLBACK_MAY_HAVE_STARTED
    assert after.rollback_completed_at is None


def test_the_completion_checkpoint_without_rolled_back_status_is_rejected(
    tmp_path: Path,
) -> None:
    """Forward half of the coherence: rank 9 implies the terminal status.

    A job left at `rollback_completed` while still ACTIVE would claim a
    completed rollback and keep holding the one global destructive slot.
    """

    store, _, job, _, _ = _armed_rollback_job(tmp_path)

    _assert_sql_rejected(
        store,
        "UPDATE package_update_jobs SET checkpoint='rollback_completed', "
        "rollback_completed_at=?, rollback_task_upid=? WHERE job_id=?",
        ("2026-01-01T00:00:00+00:00", UPID, job.job_id),
    )
    assert (
        store.package_update_job(job.job_id).status
        is PackageUpdateJobStatus.ACTIVE
    )


def test_rolled_back_without_the_completion_checkpoint_is_rejected(
    tmp_path: Path,
) -> None:
    """Reverse half: it follows transitively, and is asserted explicitly."""

    store, _, job, _, _ = _armed_rollback_job(tmp_path)

    _assert_sql_rejected(
        store,
        "UPDATE package_update_jobs SET rollback_task_upid=?, "
        "rollback_completed_at=?, status='rolled_back', terminalized_at=?, "
        "terminal_reason='forged' WHERE job_id=?",
        (UPID, "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00", job.job_id),
    )


def test_a_completion_timestamp_can_never_exist_without_a_task_identity(
    tmp_path: Path,
) -> None:
    """The invariant in isolation, independent of status or checkpoint."""

    store, _, job, _, _ = _armed_rollback_job(tmp_path)

    _assert_sql_rejected(
        store,
        "UPDATE package_update_jobs SET rollback_completed_at=? WHERE job_id=?",
        ("2026-01-01T00:00:00+00:00", job.job_id),
    )


def test_the_legal_authority_completion_still_reaches_rolled_back(
    tmp_path: Path,
) -> None:
    """Positive control: the new constraints do not block the legal path."""

    _, _, authority, _, job, ownership, identity, pve, host, orchestrator = (
        _mutating_job(tmp_path)
    )

    result = _drive_to_successful_rollback(
        authority, orchestrator, job, ownership, identity, pve
    )

    assert result.outcome is RollbackOperationOutcome.COMPLETED
    assert result.job.status is PackageUpdateJobStatus.ROLLED_BACK
    assert result.job.checkpoint is PackageUpdateCheckpoint.ROLLBACK_COMPLETED
    assert result.job.rollback_task_upid == UPID
    assert result.job.rollback_completed_at is not None
    # Written as ONE atomic statement, so the checkpoint and the terminal
    # status can never disagree even momentarily.
    assert result.job.terminalized_at is not None
    assert len(pve.rollbacks) == 1
