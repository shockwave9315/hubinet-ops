"""Dark snapshot helper: typed shape, journal, flock, and no-blind-replay.

Exercises `deploy/hubinet-package-snapshot-helper.py` entirely against a fake
`pvesh` runner and a temporary journal directory. Nothing here runs a real
`pvesh`, `pct`, `ssh`, or any PVE operation, and the helper has no package
mutation and no snapshot delete operation to exercise in the first place.
"""

from __future__ import annotations

import errno
import importlib.util
import json
import os
import re
from pathlib import Path
import sys
import time
from types import ModuleType
import uuid

import pytest

from tests.pve_9_2_3_cli_schema import PveshSchemaError, parse_pvesh_9_2_3


ROOT = Path(__file__).resolve().parents[1]
HELPER_PATH = ROOT / "deploy" / "hubinet-package-snapshot-helper.py"


def _load_helper() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "hubinet_package_snapshot_helper", HELPER_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


helper = _load_helper()
REAL_PREPARE_DETACHED_RUNNER = helper._prepare_detached_runner


class _SynchronousExecutor:
    """An in-process stand-in that keeps the production RELEASE ordering.

    Like the real executor it runs nothing when it is prepared: the capture
    happens only when `release()` is called, which the helper does strictly
    after the durable `submitted` write. `abandon()` runs nothing at all.
    """

    def __init__(self, run) -> None:
        self._run = run
        self.released = False
        self.abandoned = False

    def release(self) -> None:
        self.released = True
        self._run()

    def abandon(self) -> None:
        self.abandoned = True


@pytest.fixture(autouse=True)
def _synchronous_detached_runner(monkeypatch):
    """Keep fake-runner state in-process; production spawn is tested separately."""

    def prepare(runner, journal, operation_id, argv, _lease):
        return _SynchronousExecutor(
            lambda: helper._run_capture_child(runner, journal, operation_id, argv)
        )

    monkeypatch.setattr(helper, "_prepare_detached_runner", prepare)

NODE = "pve-a"
VMID = 112
UPID = f"UPID:{NODE}:0000ABCD:000DC5EA:57500527:vzsnapshot:{VMID}:root@pam:"
OTHER_UPID = f"UPID:{NODE}:0000ABCE:000DC5EB:57500528:vzsnapshot:{VMID}:root@pam:"

BACKEND_ID = "11111111-1111-4111-8111-111111111111"
JOB_ID = "22222222-2222-4222-8222-222222222222"
RESOURCE_ID = "33333333-3333-4333-8333-333333333333"
SOURCE_ID = "44444444-4444-4444-8444-444444444444"
CONTINUITY = 3


def _ownership(**overrides) -> dict:
    ownership = {
        "job_id": JOB_ID,
        "resource_id": RESOURCE_ID,
        "resource_continuity_revision": CONTINUITY,
        "inventory_source_id": SOURCE_ID,
        "backend_instance_id": BACKEND_ID,
    }
    ownership.update(overrides)
    return ownership


def _identity(ownership: dict) -> tuple[str, str]:
    return helper.derive_snapshot_identity(
        backend_instance_id=ownership["backend_instance_id"],
        job_id=ownership["job_id"],
        resource_id=ownership["resource_id"],
        resource_continuity_revision=ownership["resource_continuity_revision"],
    )


def _request(operation="ensure_pre_update_snapshot_submitted", **overrides) -> dict:
    ownership = overrides.pop("ownership", _ownership())
    operation_id, snapshot_name = _identity(ownership)
    request = {
        "request_version": 1,
        "operation": operation,
        "target": {"vmid": VMID, "expected_node": NODE},
        "operation_identity": {
            "snapshot_operation_id": overrides.pop(
                "snapshot_operation_id", operation_id
            ),
            "snapshot_name": overrides.pop("snapshot_name", snapshot_name),
        },
        "ownership": ownership,
    }
    request.update(overrides)
    return request


class FakePve:
    """A minimal fake `pvesh` that records every argv it is handed."""

    def __init__(
        self,
        *,
        snapshots=None,
        lock=None,
        task_sequence=None,
        submit_upid=UPID,
        submit_returncode=0,
        status="running",
        node=NODE,
        local_node=NODE,
        resource_type="lxc",
        present=True,
        vmid=VMID,
    ) -> None:
        self.vmid = vmid
        self.snapshots = list(snapshots if snapshots is not None else [
            {"name": "current", "description": "You are here!"}
        ])
        self.lock = lock
        self.task_sequence = list(
            task_sequence
            if task_sequence is not None
            else [{"upid": UPID, "status": "stopped", "exitstatus": "OK"}]
        )
        self.submit_upid = submit_upid
        self.submit_returncode = submit_returncode
        self.status = status
        self.node = node
        self.local_node = local_node
        self.resource_type = resource_type
        self.present = present
        self.argvs: list[tuple[str, ...]] = []
        #: Every (argv, timeout) pair, so a test can prove WHICH deadline
        #: each command class was actually run under.
        self.deadlines: list[tuple[tuple[str, ...], float | None]] = []
        self.submissions = 0
        #: What a successful submission adds to the canonical listing.
        self.on_submit = None

    def __call__(self, argv, timeout, max_output):
        argv = tuple(argv)
        self.argvs.append(argv)
        self.deadlines.append((argv, timeout))
        try:
            parse_pvesh_9_2_3(argv)
        except PveshSchemaError as exc:
            return helper.CommandResult(
                255,
                b"",
                f"400 Parameter verification failed.\n{exc}\n".encode("utf-8"),
            )
        return helper.CommandResult(*self._dispatch(argv))

    def _json(self, payload):
        return 0, json.dumps(payload).encode("utf-8"), b""

    def _dispatch(self, argv):
        call = parse_pvesh_9_2_3(argv)
        if call.verb == "get" and call.path == "/cluster/status":
            return self._json(
                [{"type": "node", "name": self.local_node, "local": 1}]
            )
        if call.verb == "get" and call.path == "/cluster/resources":
            rows = []
            if self.present:
                rows.append(
                    {
                        "vmid": self.vmid,
                        "type": self.resource_type,
                        "node": self.node,
                        "status": self.status,
                    }
                )
            return self._json(rows)
        if call.verb == "get":
            lxc_read = re.fullmatch(
                r"/nodes/([^/]+)/lxc/(\d+)/(config|snapshot)", call.path
            )
            if lxc_read is not None:
                requested_node, requested_vmid, kind = lxc_read.groups()
                if (
                    requested_node != self.node
                    or int(requested_vmid) != self.vmid
                    or not self.present
                ):
                    return 2, b"", b"guest is not available on requested node"
                if kind == "config":
                    return self._json({"lock": self.lock} if self.lock else {})
                return self._json(self.snapshots)
        if call.verb == "get" and "/tasks/" in call.path:
            payload = (
                self.task_sequence.pop(0)
                if len(self.task_sequence) > 1
                else self.task_sequence[0]
            )
            return self._json(payload)
        if call.verb == "create":
            match = re.fullmatch(
                r"/nodes/([^/]+)/lxc/(\d+)/snapshot", call.path
            )
            if (
                match is None
                or match.group(1) != self.node
                or int(match.group(2)) != self.vmid
                or not self.present
            ):
                return 2, b"", b"guest is not available on requested node"
            self.submissions += 1
            if self.on_submit is not None:
                self.on_submit(self)
            if self.submit_returncode != 0:
                return self.submit_returncode, b"", b"submission failed"
            return self._json(self.submit_upid)
        raise AssertionError(f"unexpected pvesh call {call!r}")


def _pvesh_call(argv: tuple[str, ...]):
    return parse_pvesh_9_2_3(argv)


def _is_pvesh(argv: tuple[str, ...], verb: str) -> bool:
    return _pvesh_call(argv).verb == verb


def _completed_snapshot(ownership: dict, name: str, **extra) -> dict:
    entry = {
        "name": name,
        # PVE re-reads a description with a newline appended per line.
        "description": helper.build_snapshot_description(ownership) + "\n",
        "snaptime": 1_700_000_000,
    }
    entry.update(extra)
    return entry


def _journal(tmp_path: Path):
    return helper.OperationJournal(tmp_path / "snapshot-operations")


def _handle(request, pve, journal):
    return helper.handle_request(request, runner=pve, journal=journal)


# ---------------------------------------------------------------------------
# Request schema
# ---------------------------------------------------------------------------


def test_only_the_three_typed_operations_exist() -> None:
    assert helper.OPERATIONS == (
        "inspect_job_snapshot_state",
        "ensure_pre_update_snapshot_submitted",
        "seal_operation_never_submitted",
    )


def test_exact_request_shape_is_required() -> None:
    assert helper.validate_request(_request())["operation"] == (
        "ensure_pre_update_snapshot_submitted"
    )
    for mutate in (
        lambda r: r.pop("ownership"),
        lambda r: r.update(extra="field"),
        lambda r: r.update(request_version=2),
        lambda r: r.update(operation="delete_snapshot"),
        lambda r: r.update(operation="rollback_snapshot"),
        lambda r: r["target"].update(extra=1),
        lambda r: r["target"].update(vmid=99),
        lambda r: r["target"].update(vmid="112"),
        lambda r: r["target"].update(expected_node="-flag"),
        lambda r: r["operation_identity"].update(snapshot_name="current"),
        lambda r: r["operation_identity"].update(snapshot_name="vzdump"),
        lambda r: r["operation_identity"].update(snapshot_name="9bad"),
        lambda r: r["operation_identity"].update(snapshot_name="has.dot"),
        lambda r: r["ownership"].update(job_id="not-a-uuid"),
        lambda r: r["ownership"].update(resource_continuity_revision=0),
        lambda r: r["ownership"].update(resource_continuity_revision="3"),
        lambda r: r.pop("operation_identity"),
    ):
        request = _request()
        mutate(request)
        with pytest.raises(helper.RequestError):
            helper.validate_request(request)


def test_the_helper_refuses_an_identity_it_did_not_derive_itself() -> None:
    # The backend does not get to choose a snapshot name: the helper
    # re-derives it from the request's own ownership facts and requires
    # equality, so it can never be talked into an arbitrary name.
    with pytest.raises(helper.RequestError, match="ownership derivation"):
        helper.validate_request(
            _request(snapshot_name="hubinet-preupd-aaaaaaaaaaaaaaaaaaaaaaaa")
        )
    with pytest.raises(helper.RequestError, match="ownership derivation"):
        helper.validate_request(_request(snapshot_operation_id=str(uuid.uuid4())))
    # A stale identity paired with changed ownership is refused too: the
    # identity is a function of exactly those facts.
    stale_operation_id, stale_name = _identity(_ownership())
    drifted = _request(ownership=_ownership(resource_continuity_revision=CONTINUITY + 1))
    drifted["operation_identity"] = {
        "snapshot_operation_id": stale_operation_id,
        "snapshot_name": stale_name,
    }
    with pytest.raises(helper.RequestError, match="ownership derivation"):
        helper.validate_request(drifted)
    # ...and so is a different job re-using this job's snapshot name.
    hijack = _request(ownership=_ownership(job_id=str(uuid.uuid4())))
    hijack["operation_identity"]["snapshot_name"] = stale_name
    with pytest.raises(helper.RequestError, match="ownership derivation"):
        helper.validate_request(hijack)


def test_the_helper_never_accepts_remote_command_text(monkeypatch) -> None:
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "rm -rf /")
    captured: list[str] = []
    monkeypatch.setattr(helper.sys.stdout, "write", captured.append)
    assert helper.main() == 2
    response = json.loads(captured[0])
    assert response["ok"] is False
    assert "command text is not accepted" in response["error"]["message"]


def test_the_helper_source_contains_no_mutation_or_delete_operation() -> None:
    # Skip the module docstring, which legitimately describes what is absent.
    source = HELPER_PATH.read_text(encoding="utf-8").split('"""', 2)[2]
    for forbidden in (
        '"delete"',
        "delsnapshot",
        "/rollback",
        "shell=True",
        "os.system",
        "os.popen",
        "subprocess.run",
        "subprocess.call",
        "shlex",
        "apt-get",
        "apt ",
        "dpkg",
        "pct exec",
        "SSH_ORIGINAL_COMMAND\", \"",
    ):
        assert forbidden not in source, forbidden
    # 'rollback' may appear only as a PVE config lock value this file *reads*
    # to detect an in-flight operation -- never as an operation it performs.
    assert source.count("rollback") == 1
    assert '_IN_FLIGHT_LOCKS = frozenset({"snapshot", "snapshot-delete", "rollback"})' in source
    # The only PVE verbs used at all are `get` and `create`.
    assert set(re.findall(r'"pvesh", (?:"--noproxy", )?"([a-z]+)"', source)) == {
        "get",
        "create",
    }


def test_every_pvesh_path_is_built_from_fixed_constants(tmp_path: Path) -> None:
    pve = FakePve()
    pve.on_submit = lambda p: p.snapshots.append(
        _completed_snapshot(_ownership(), _identity(_ownership())[1])
    )
    _handle(_request(), pve, _journal(tmp_path))
    operation_id, snapshot_name = _identity(_ownership())
    for argv in pve.argvs:
        assert argv[0] == "pvesh"
        assert _pvesh_call(argv).verb in ("get", "create")
        # No shell, no command string, and no request-provided free text.
        assert not any("&&" in item or ";" in item or "|" in item for item in argv[:3])
    submissions = [argv for argv in pve.argvs if _is_pvesh(argv, "create")]
    assert len(submissions) == 1
    assert _pvesh_call(submissions[0]).path == f"/nodes/{NODE}/lxc/{VMID}/snapshot"
    assert "--snapname" in submissions[0]
    assert submissions[0][submissions[0].index("--snapname") + 1] == snapshot_name


# ---------------------------------------------------------------------------
# Live target revalidation before mutation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "classification"),
    [
        ({"present": False}, "guest_unavailable"),
        ({"resource_type": "qemu"}, "unsupported_resource_type"),
        ({"node": "pve-b"}, "stale_target"),
        ({"status": "migrating"}, "guest_unavailable"),
    ],
)
def test_live_target_is_revalidated_before_any_mutation(
    tmp_path: Path, kwargs, classification
) -> None:
    pve = FakePve(**kwargs)
    response = _handle(_request(), pve, _journal(tmp_path))
    assert response["ok"] is False
    assert response["error"]["classification"] == classification
    assert pve.submissions == 0


def test_an_in_flight_guest_lock_stops_submission_without_failing(
    tmp_path: Path,
) -> None:
    pve = FakePve(lock="snapshot")
    response = _handle(_request(), pve, _journal(tmp_path))
    assert response["ok"] is True
    assert response["outcome"] == "uncertain"
    assert pve.submissions == 0


# ---------------------------------------------------------------------------
# Journal: idempotency and no blind replay
# ---------------------------------------------------------------------------


def test_intent_is_journaled_before_submission_and_survives_a_crash(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path)
    pve = FakePve()
    operation_id, _ = _identity(_ownership())
    phases: list[str] = []

    def crash(fake):
        phases.append(journal.read(operation_id)["phase"])
        raise KeyboardInterrupt("host died mid-submission")

    pve.on_submit = crash
    with pytest.raises(KeyboardInterrupt):
        _handle(_request(), pve, journal)

    # The journal was already at "submitted" when the process reached PVE.
    assert phases == ["submitted"]
    assert journal.read(operation_id)["phase"] == "submitted"


def test_a_submitted_operation_is_never_resubmitted(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    operation_id, snapshot_name = _identity(_ownership())
    journal.write(
        {
            "journal_version": 1,
            "snapshot_operation_id": operation_id,
            "request_fingerprint": helper.request_fingerprint(
                helper.validate_request(_request())
            ),
            "vmid": VMID,
            "expected_node": NODE,
            "snapshot_name": snapshot_name,
            "phase": "submitted",
        }
    )
    pve = FakePve()

    response = _handle(_request(), pve, journal)

    assert pve.submissions == 0
    assert response["outcome"] == "uncertain"
    assert "never recorded" in response["reason"]


def test_a_submitted_operation_recovers_only_on_strict_canonical_evidence(
    tmp_path: Path,
) -> None:
    ownership = _ownership()
    operation_id, snapshot_name = _identity(ownership)
    record = {
        "journal_version": 1,
        "snapshot_operation_id": operation_id,
        "request_fingerprint": helper.request_fingerprint(
            helper.validate_request(_request())
        ),
        "vmid": VMID,
        "expected_node": NODE,
        "snapshot_name": snapshot_name,
        "phase": "submitted",
    }

    # Exact snapshot, exact metadata, complete, guest not locked -> recovered.
    journal = _journal(tmp_path / "ok")
    journal.write(record)
    pve = FakePve(
        snapshots=[
            {"name": "current", "description": "You are here!"},
            _completed_snapshot(ownership, snapshot_name),
        ]
    )
    response = _handle(_request(), pve, journal)
    assert response["outcome"] == "completed"
    assert pve.submissions == 0
    assert journal.read(operation_id)["phase"] == "terminal"

    # Same evidence but the guest is still locked -> uncertain, no resubmit.
    journal = _journal(tmp_path / "locked")
    journal.write(record)
    pve = FakePve(
        lock="snapshot",
        snapshots=[_completed_snapshot(ownership, snapshot_name)],
    )
    response = _handle(_request(), pve, journal)
    assert response["outcome"] == "uncertain"
    assert pve.submissions == 0

    # Snapshot present but still incomplete -> uncertain, no resubmit.
    journal = _journal(tmp_path / "incomplete")
    journal.write(record)
    pve = FakePve(
        snapshots=[_completed_snapshot(ownership, snapshot_name, snapstate="prepare")]
    )
    response = _handle(_request(), pve, journal)
    assert response["outcome"] == "uncertain"
    assert pve.submissions == 0

    # Right name, wrong metadata -> uncertain, no resubmit.
    journal = _journal(tmp_path / "foreign")
    journal.write(record)
    pve = FakePve(
        snapshots=[
            _completed_snapshot(_ownership(job_id=str(uuid.uuid4())), snapshot_name)
        ]
    )
    response = _handle(_request(), pve, journal)
    assert response["outcome"] == "uncertain"
    assert pve.submissions == 0


def test_a_known_task_is_never_polled_or_resubmitted_by_the_submission_operation(
    tmp_path: Path,
) -> None:
    """The mutating operation returns the instant a task is journaled.

    It must never wait for PVE's physical task to completion: that is
    the read-only operation's job, exercised below. Zero PVE calls at all
    proves this returns promptly rather than blocking internally.
    """

    ownership = _ownership()
    operation_id, snapshot_name = _identity(ownership)
    journal = _journal(tmp_path)
    journal.write(
        {
            "journal_version": 1,
            "snapshot_operation_id": operation_id,
            "request_fingerprint": helper.request_fingerprint(
                helper.validate_request(_request())
            ),
            "vmid": VMID,
            "expected_node": NODE,
            "snapshot_name": snapshot_name,
            "phase": "task_known",
            "task_upid": UPID,
        }
    )
    pve = FakePve(
        snapshots=[
            {"name": "current", "description": "You are here!"},
            _completed_snapshot(ownership, snapshot_name),
        ]
    )
    response = _handle(_request(), pve, journal)

    assert pve.submissions == 0
    assert response["outcome"] == "uncertain"
    assert response["submission_state"] == "task_known"
    assert response["task_upid"] == UPID
    assert pve.argvs == []


def test_inspecting_a_known_task_reads_it_exactly_once_and_never_submits(
    tmp_path: Path,
) -> None:
    """Task completion is observed by the read-only operation, one read at a
    time -- never a poll-to-completion loop, and never a resubmission."""

    ownership = _ownership()
    operation_id, snapshot_name = _identity(ownership)
    journal = _journal(tmp_path)
    journal.write(
        {
            "journal_version": 1,
            "snapshot_operation_id": operation_id,
            "request_fingerprint": helper.request_fingerprint(
                helper.validate_request(_request())
            ),
            "vmid": VMID,
            "expected_node": NODE,
            "snapshot_name": snapshot_name,
            "phase": "task_known",
            "task_upid": UPID,
        }
    )
    pve = FakePve(
        snapshots=[
            {"name": "current", "description": "You are here!"},
            _completed_snapshot(ownership, snapshot_name),
        ]
    )
    response = _handle(_request("inspect_job_snapshot_state"), pve, journal)

    assert pve.submissions == 0
    assert response["outcome"] == "completed"
    assert response["task_upid"] == UPID
    assert response["submission_state"] == "task_known"
    assert len([argv for argv in pve.argvs if "/tasks/" in _pvesh_call(argv).path]) == 1


def test_a_running_task_is_reported_without_polling(tmp_path: Path) -> None:
    """The helper never blocks on PVE's physical task."""

    journal = _journal(tmp_path)
    pve = FakePve(task_sequence=[{"upid": UPID, "status": "running"}])

    submitted = _handle(_request(), pve, journal)
    assert submitted["outcome"] == "uncertain"
    assert submitted["submission_state"] == "task_known"
    assert pve.submissions == 1

    assert not any(
        "/tasks/" in _pvesh_call(argv).path
        for argv in pve.argvs
        if _is_pvesh(argv, "get")
    )

    inspected = _handle(_request("inspect_job_snapshot_state"), pve, journal)
    assert inspected["outcome"] == "absent"
    assert inspected["submission_state"] == "task_known"
    assert pve.submissions == 1
    # Exactly one task-status read: never an internal poll loop.
    assert len([argv for argv in pve.argvs if "/tasks/" in _pvesh_call(argv).path]) == 1


def test_an_identical_retry_never_resubmits_and_inspect_replays_the_outcome(
    tmp_path: Path,
) -> None:
    ownership = _ownership()
    operation_id, snapshot_name = _identity(ownership)
    journal = _journal(tmp_path)
    pve = FakePve(
        snapshots=[
            {"name": "current", "description": "You are here!"},
        ]
    )
    pve.on_submit = lambda p: p.snapshots.append(
        _completed_snapshot(ownership, snapshot_name)
    )

    first = _handle(_request(), pve, journal)
    second = _handle(_request(), pve, journal)

    # The second attempt reattaches to the identical journaled operation
    # instead of submitting again.
    assert first["outcome"] == second["outcome"] == "uncertain"
    assert first["submission_state"] == second["submission_state"] == "task_known"
    assert pve.submissions == 1
    assert journal.read(operation_id)["phase"] == "task_known"

    # The read-only operation observes the same completed outcome, however
    # many times it is asked, and still never resubmits.
    inspected_first = _handle(_request("inspect_job_snapshot_state"), pve, journal)
    inspected_second = _handle(_request("inspect_job_snapshot_state"), pve, journal)
    assert inspected_first["outcome"] == inspected_second["outcome"] == "completed"
    assert pve.submissions == 1


def test_the_same_operation_identity_with_a_different_request_is_refused(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path)
    ownership = _ownership()
    operation_id, snapshot_name = _identity(ownership)
    journal.write(
        {
            "journal_version": 1,
            "snapshot_operation_id": operation_id,
            "request_fingerprint": "0" * 64,
            "vmid": VMID,
            "expected_node": NODE,
            "snapshot_name": snapshot_name,
            "phase": "intent",
        }
    )
    pve = FakePve()
    response = _handle(_request(), pve, journal)
    assert response["ok"] is False
    assert response["error"]["classification"] == "operation_request_mismatch"
    assert pve.submissions == 0


def test_a_corrupt_journal_fails_closed(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    journal.ensure_directory()
    operation_id, _ = _identity(_ownership())
    (journal.directory / f"op-{operation_id}.json").write_text("{not json")
    pve = FakePve()
    response = _handle(_request(), pve, journal)
    assert response["ok"] is False
    assert response["error"]["classification"] == "journal_corrupt"
    assert pve.submissions == 0


def test_journal_writes_are_atomic_and_leave_no_temporary_file(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path)
    ownership = _ownership()
    operation_id, snapshot_name = _identity(ownership)
    pve = FakePve()
    pve.on_submit = lambda p: p.snapshots.append(
        _completed_snapshot(ownership, snapshot_name)
    )
    _handle(_request(), pve, journal)
    assert not list(journal.directory.glob("*.tmp"))
    record = json.loads((journal.directory / f"op-{operation_id}.json").read_text())
    # The injected hermetic runner completes inline and promotes the capture;
    # production detaches before the synchronous pvesh work begins.
    assert record["phase"] == "task_known"
    assert record["snapshot_operation_id"] == operation_id


def test_a_terminal_replay_never_rewrites_the_already_final_journal_record(
    tmp_path: Path,
) -> None:
    """P3-8B. `_ensure_submitted` replays an already-`terminal` journal
    record through `_finalize`, which must still re-read fresh canonical PVE
    evidence every time, but must never rewrite the durable, already-final
    journal record just to replay it -- zero `journal.write` calls, zero
    filesystem mutation of already-final evidence.
    """

    ownership = _ownership()
    operation_id, snapshot_name = _identity(ownership)
    journal = _journal(tmp_path)
    journal.write(
        {
            "journal_version": 1,
            "snapshot_operation_id": operation_id,
            "request_fingerprint": helper.request_fingerprint(
                helper.validate_request(_request())
            ),
            "vmid": VMID,
            "expected_node": NODE,
            "snapshot_name": snapshot_name,
            "phase": "terminal",
            "outcome": "completed",
            "reason": "recovered: canonical job-owned snapshot present",
        }
    )
    path = journal.directory / f"op-{operation_id}.json"
    before_bytes = path.read_bytes()
    before_stat = path.stat()

    write_calls: list[dict] = []

    def forbidden_write(record):
        write_calls.append(record)
        raise AssertionError("a terminal replay must never write the journal")

    journal.write = forbidden_write

    pve = FakePve(
        snapshots=[
            {"name": "current", "description": "You are here!"},
            _completed_snapshot(ownership, snapshot_name),
        ]
    )
    response = _handle(_request(), pve, journal)

    assert response["outcome"] == "completed"
    assert response["submission_state"] == "terminal"
    # The canonical PVE re-read is still required and reflected in the reply.
    assert response["snapshots"] is not None
    assert write_calls == []
    assert path.read_bytes() == before_bytes
    assert path.stat().st_mtime_ns == before_stat.st_mtime_ns


# ---------------------------------------------------------------------------
# Task semantics
# ---------------------------------------------------------------------------


def test_a_task_failure_is_terminal_and_never_confirms(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    pve = FakePve(
        task_sequence=[{"upid": UPID, "status": "stopped", "exitstatus": "boom"}]
    )
    submitted = _handle(_request(), pve, journal)
    assert submitted["outcome"] == "uncertain"
    assert submitted["submission_state"] == "task_known"
    assert pve.submissions == 1

    inspected = _handle(_request("inspect_job_snapshot_state"), pve, journal)
    assert inspected["outcome"] == "failed"
    assert pve.submissions == 1


def test_a_successful_task_without_the_canonical_snapshot_never_confirms(
    tmp_path: Path,
) -> None:
    # Task says OK but the canonical listing never gains our snapshot.
    journal = _journal(tmp_path)
    pve = FakePve()
    submitted = _handle(_request(), pve, journal)
    assert submitted["outcome"] == "uncertain"
    assert submitted["submission_state"] == "task_known"

    inspected = _handle(_request("inspect_job_snapshot_state"), pve, journal)
    assert inspected["outcome"] != "completed"
    assert inspected["outcome"] == "absent"


def test_a_submission_that_returns_no_usable_task_identity_is_uncertain(
    tmp_path: Path,
) -> None:
    for submit_upid, returncode in (("not-a-upid", 0), (UPID, 1)):
        pve = FakePve(submit_upid=submit_upid, submit_returncode=returncode)
        response = _handle(_request(), pve, _journal(tmp_path / str(returncode)))
        assert response["outcome"] == "uncertain"
        assert "task identity" in response["reason"]
        assert response["submission_state"] == "submitted"


def test_pvesh_status_prefix_then_exact_json_upid_reaches_task_known(
    tmp_path: Path,
) -> None:
    """Real pvesh create framing may precede its final JSON scalar."""

    ownership = _ownership()
    operation_id, snapshot_name = _identity(ownership)
    journal = _journal(tmp_path)
    pve = FakePve()
    pve.on_submit = lambda current: current.snapshots.append(
        _completed_snapshot(ownership, snapshot_name)
    )
    original = pve.__call__

    def prefixed(argv, timeout, max_output):
        result = original(argv, timeout, max_output)
        if _is_pvesh(tuple(argv), "create") and result.returncode == 0:
            return helper.CommandResult(
                returncode=0,
                stdout=b"200 OK\n" + result.stdout + b"\n",
                stderr=b"",
            )
        return result

    submitted = helper.handle_request(_request(), runner=prefixed, journal=journal)

    assert submitted["submission_state"] == "task_known"
    assert submitted["task_upid"] == UPID
    assert journal.read(operation_id)["phase"] == "task_known"
    assert journal.read(operation_id)["task_upid"] == UPID

    inspected = helper.handle_request(
        _request("inspect_job_snapshot_state"), runner=prefixed, journal=journal
    )
    assert inspected["outcome"] == "completed"
    assert pve.submissions == 1


@pytest.mark.parametrize(
    "stdout",
    [
        UPID.encode(),
        b'"' + UPID.encode() + b'"',
        b"progress\nwarning\n\"" + UPID.encode() + b"\"\n",
        b"progress without newline\"" + UPID.encode() + b"\"\n",
    ],
)
def test_upid_extraction_accepts_only_an_exact_terminal_machine_result(
    stdout: bytes,
) -> None:
    result = helper.CommandResult(returncode=0, stdout=stdout, stderr=b"")
    assert helper._extract_upid(result) == UPID


@pytest.mark.parametrize(
    "stdout",
    [
        b"progress only\n",
        b"prefix UPID:not-a-task\n\"" + UPID.encode() + b"\"\n",
        UPID.encode() + b"\n\"" + UPID.encode() + b"\"\n",
        b"prefix " + UPID.encode() + b" suffix\n",
        b'{"upid":"' + UPID.encode() + b'"}\n',
        b'["' + UPID.encode() + b'"]\n',
        b'"' + UPID.encode() + b'" garbage',
        b"\xff\"" + UPID.encode() + b'"',
    ],
)
def test_upid_extraction_rejects_unrelated_malformed_or_ambiguous_stdout(
    stdout: bytes,
) -> None:
    result = helper.CommandResult(returncode=0, stdout=stdout, stderr=b"")
    assert helper._extract_upid(result) is None


def test_remote_owner_refuses_before_submitted_and_create_is_noproxy(
    tmp_path: Path,
) -> None:
    refused_journal = _journal(tmp_path / "remote")
    refused = _handle(_request(), FakePve(local_node="pve-b"), refused_journal)
    operation_id, _ = _identity(_ownership())
    assert refused["ok"] is False
    assert refused["error"]["submission"] == helper.SUBMISSION_NOT_SUBMITTED
    assert refused_journal.read(operation_id)["phase"] == "intent"

    local = FakePve()
    _handle(_request(), local, _journal(tmp_path / "local"))
    create = next(argv for argv in local.argvs if _is_pvesh(argv, "create"))
    assert create[:3] == ("pvesh", "--noproxy", "create")
    assert _pvesh_call(create).noproxy is True


def test_pve_9_2_3_schema_rejects_human0_noproxy_as_endpoint_property() -> None:
    """Reproduce the real schema boundary, not a changed string assertion."""

    snapshot_name = _identity(_ownership())[1]
    historical_rc6 = (
        "pvesh",
        "create",
        f"/nodes/{NODE}/lxc/{VMID}/snapshot",
        "--snapname",
        snapshot_name,
        "--description",
        helper.build_snapshot_description(_ownership()),
        "--noproxy",
        "--output-format",
        "json",
    )
    with pytest.raises(PveshSchemaError, match="noproxy: property is not defined"):
        parse_pvesh_9_2_3(historical_rc6)

    result = FakePve()(historical_rc6, None, helper.MAX_CAPTURE_OUTPUT_BYTES)
    assert result.returncode == 255
    assert b"schema does not allow additional properties" in result.stderr


def test_submitted_capture_is_promoted_later_without_resubmission(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path)
    pve = FakePve(submit_upid="malformed")
    operation_id, _ = _identity(_ownership())
    first = _handle(_request(), pve, journal)
    assert first["submission_state"] == "submitted"
    journal.write_completed_capture(
        operation_id,
        helper.CommandResult(
            0, b"progress without newline\"" + UPID.encode() + b'"\n', b"warning"
        ),
    )

    recovered = _handle(_request("inspect_job_snapshot_state"), pve, journal)
    assert recovered["submission_state"] == "task_known"
    assert recovered["task_upid"] == UPID
    assert journal.read(operation_id)["phase"] == "task_known"
    assert pve.submissions == 1


def test_incomplete_capture_stays_submitted_and_never_resubmits(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    pve = FakePve(submit_upid="malformed")
    operation_id, _ = _identity(_ownership())
    _handle(_request(), pve, journal)
    journal._capture_path(operation_id, "complete.json").unlink()

    inspected = _handle(_request("inspect_job_snapshot_state"), pve, journal)
    assert inspected["submission_state"] == "submitted"
    assert journal.read(operation_id)["phase"] == "submitted"
    assert pve.submissions == 1


def test_ambiguous_completed_capture_stays_submitted_and_never_resubmits(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path)
    pve = FakePve(submit_upid="malformed")
    operation_id, _ = _identity(_ownership())
    _handle(_request(), pve, journal)
    journal.write_completed_capture(
        operation_id,
        helper.CommandResult(
            0,
            UPID.encode() + b'\n"' + OTHER_UPID.encode() + b'"\n',
            b"",
        ),
    )
    inspected = _handle(_request("inspect_job_snapshot_state"), pve, journal)
    assert inspected["submission_state"] == "submitted"
    assert journal.read(operation_id)["phase"] == "submitted"
    assert pve.submissions == 1


def test_real_detached_snapshot_runner_returns_while_physical_task_is_alive(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path)
    operation_id, _ = _identity(_ownership())
    started = tmp_path / "started"
    release = tmp_path / "release"

    def slow_runner(argv, timeout, max_output):
        started.touch()
        deadline = time.monotonic() + 3
        while not release.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        return helper.CommandResult(0, b'"' + UPID.encode() + b'"', b"")

    began = time.monotonic()
    with helper.VmidMutationLock(VMID, journal.directory) as lease:
        executor = REAL_PREPARE_DETACHED_RUNNER(
            slow_runner,
            journal,
            operation_id,
            ("pvesh", "--noproxy", "create", "/fixed"),
            lease,
        )
        executor.release()
    assert time.monotonic() - began < 1
    deadline = time.monotonic() + 2
    while not started.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert started.exists()
    with pytest.raises(helper.SnapshotError, match="holds this guest's lease"):
        with helper.VmidMutationLock(VMID, journal.directory):
            pass
    release.touch()
    deadline = time.monotonic() + 3
    while journal.read_completed_capture(operation_id) is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert journal.read_completed_capture(operation_id).stdout == b'"' + UPID.encode() + b'"'


@pytest.mark.parametrize("exitstatus", ["OK", "WARNINGS: 2"])
def test_pve_non_error_exit_statuses_are_accepted(
    tmp_path: Path, exitstatus: str
) -> None:
    ownership = _ownership()
    _, snapshot_name = _identity(ownership)
    journal = _journal(tmp_path / exitstatus[:2])
    pve = FakePve(
        task_sequence=[{"upid": UPID, "status": "stopped", "exitstatus": exitstatus}]
    )
    pve.on_submit = lambda p: p.snapshots.append(
        _completed_snapshot(ownership, snapshot_name)
    )
    _handle(_request(), pve, journal)
    response = _handle(_request("inspect_job_snapshot_state"), pve, journal)
    assert response["outcome"] == "completed"


@pytest.mark.parametrize(
    "status",
    [
        {"upid": UPID, "status": "stopped"},
        {"upid": UPID, "status": "stopped", "exitstatus": "unexpected status"},
        {"upid": UPID, "status": "stopped", "exitstatus": "WARNINGS: lots"},
    ],
)
def test_a_terminal_task_without_a_non_error_status_never_confirms(
    tmp_path: Path, status
) -> None:
    ownership = _ownership()
    _, snapshot_name = _identity(ownership)
    journal = _journal(tmp_path)
    pve = FakePve(task_sequence=[status])
    pve.on_submit = lambda p: p.snapshots.append(
        _completed_snapshot(ownership, snapshot_name)
    )
    _handle(_request(), pve, journal)
    response = _handle(_request("inspect_job_snapshot_state"), pve, journal)
    # 'stopped' alone is never success: this is a failure, not a confirmation.
    assert response["outcome"] == "failed"


# ---------------------------------------------------------------------------
# Ownership evidence
# ---------------------------------------------------------------------------


def test_ambiguous_hubinet_metadata_is_never_treated_as_owned(
    tmp_path: Path,
) -> None:
    ownership = _ownership()
    _, snapshot_name = _identity(ownership)
    pve = FakePve(
        snapshots=[
            {"name": "current", "description": "You are here!"},
            _completed_snapshot(ownership, snapshot_name),
            {"name": "junk", "description": "hubinet-ops-snapshot-v1 broken"},
        ]
    )
    response = _handle(_request("inspect_job_snapshot_state"), pve, _journal(tmp_path))
    assert response["outcome"] == "uncertain"


def test_a_foreign_snapshot_is_never_owned(tmp_path: Path) -> None:
    pve = FakePve(
        snapshots=[
            {"name": "current", "description": "You are here!"},
            {"name": "operator-manual", "description": "before a config change"},
        ]
    )
    response = _handle(_request("inspect_job_snapshot_state"), pve, _journal(tmp_path))
    assert response["outcome"] == "absent"
    assert pve.submissions == 0


def test_inspect_never_submits_anything(tmp_path: Path) -> None:
    pve = FakePve()
    _handle(_request("inspect_job_snapshot_state"), pve, _journal(tmp_path))
    assert pve.submissions == 0
    assert not any(_is_pvesh(argv, "create") for argv in pve.argvs)


# ---------------------------------------------------------------------------
# Per-VMID serialization
# ---------------------------------------------------------------------------


def test_snapshot_mutation_is_serialized_per_vmid_with_a_kernel_flock(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path)
    journal.ensure_directory()
    with helper.VmidMutationLock(VMID, journal.directory):
        pve = FakePve()
        response = _handle(_request(), pve, journal)
        assert response["ok"] is False
        assert response["error"]["classification"] == "operation_in_progress"
        assert pve.submissions == 0

    # Released again once the holder is gone.
    pve = FakePve()
    ownership = _ownership()
    _, snapshot_name = _identity(ownership)
    pve.on_submit = lambda p: p.snapshots.append(
        _completed_snapshot(ownership, snapshot_name)
    )
    submitted = _handle(_request(), pve, journal)
    assert submitted["outcome"] == "uncertain"
    assert submitted["submission_state"] == "task_known"
    inspected = _handle(_request("inspect_job_snapshot_state"), pve, journal)
    assert inspected["outcome"] == "completed"


def test_different_vmids_use_different_locks(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    journal.ensure_directory()
    with helper.VmidMutationLock(VMID, journal.directory):
        # A different VMID's lease is independent.
        with helper.VmidMutationLock(VMID + 1, journal.directory):
            pass


# ---------------------------------------------------------------------------
# Bounded input
# ---------------------------------------------------------------------------


def test_an_oversized_request_is_refused(monkeypatch) -> None:
    captured: list[str] = []
    monkeypatch.delenv("SSH_ORIGINAL_COMMAND", raising=False)
    monkeypatch.setattr(
        helper.sys, "stdin", type("S", (), {"buffer": type("B", (), {
            "read": staticmethod(lambda n: b"x" * n)
        })()})()
    )
    monkeypatch.setattr(helper.sys.stdout, "write", captured.append)
    assert helper.main() == 2
    assert "structural bound" in json.loads(captured[0])["error"]["message"]


def test_an_existing_owned_snapshot_is_never_submitted_for_again(
    tmp_path: Path,
) -> None:
    """A lost journal must not turn into a second mutation request.

    If this operation's exact snapshot is already present canonically, the
    helper recognises it and issues no `pvesh create` at all, rather than
    relying on PVE to reject the duplicate name.
    """

    ownership = _ownership()
    operation_id, snapshot_name = _identity(ownership)
    journal = _journal(tmp_path)
    pve = FakePve(
        snapshots=[
            {"name": "current", "description": "You are here!"},
            _completed_snapshot(ownership, snapshot_name),
        ]
    )

    response = _handle(_request(), pve, journal)

    assert response["outcome"] == "completed"
    assert pve.submissions == 0
    assert not any(_is_pvesh(argv, "create") for argv in pve.argvs)
    assert journal.read(operation_id)["phase"] == "terminal"


def test_an_ambiguous_canonical_state_refuses_to_submit(tmp_path: Path) -> None:
    ownership = _ownership()
    _, snapshot_name = _identity(ownership)
    # Right name, wrong owner: submitting here could destroy or collide with
    # something this job does not own.
    pve = FakePve(
        snapshots=[
            {"name": "current", "description": "You are here!"},
            _completed_snapshot(_ownership(job_id=str(uuid.uuid4())), snapshot_name),
        ]
    )
    response = _handle(_request(), pve, _journal(tmp_path))
    assert response["outcome"] == "uncertain"
    assert "refusing to submit" in response["reason"]
    assert pve.submissions == 0


def test_an_incomplete_snapshot_under_our_name_refuses_to_submit(
    tmp_path: Path,
) -> None:
    ownership = _ownership()
    _, snapshot_name = _identity(ownership)
    pve = FakePve(
        snapshots=[_completed_snapshot(ownership, snapshot_name, snapstate="prepare")]
    )
    response = _handle(_request(), pve, _journal(tmp_path))
    assert response["outcome"] == "uncertain"
    assert pve.submissions == 0


@pytest.mark.parametrize(
    "record_overrides",
    [
        {"phase": "task_known"},                       # no task_upid at all
        {"phase": "task_known", "task_upid": 12345},   # not text
        {"phase": "task_known", "task_upid": "UPID:"},  # not a decodable UPID
        {"phase": "terminal"},                          # no outcome
        {"phase": "terminal", "outcome": "maybe"},      # not a real outcome
        # P3-8A: no writer in this contract ever journals a terminal task
        # failure -- `_inspect` reports it live from a fresh task-status read
        # instead, every time -- so this combination is impossible, not a
        # legitimate state this reader must tolerate.
        {"phase": "terminal", "outcome": "failed"},
    ],
)
def test_a_journal_phase_missing_its_own_facts_never_degrades_into_a_resubmit(
    tmp_path: Path, record_overrides
) -> None:
    """A phase is only usable with the facts that phase promises.

    Anything else fails closed as corruption. It must never quietly fall
    through to the one path that is allowed to submit a mutation.
    """

    journal = _journal(tmp_path)
    operation_id, snapshot_name = _identity(_ownership())
    record = {
        "journal_version": 1,
        "snapshot_operation_id": operation_id,
        "request_fingerprint": helper.request_fingerprint(
            helper.validate_request(_request())
        ),
        "vmid": VMID,
        "expected_node": NODE,
        "snapshot_name": snapshot_name,
    }
    record.update(record_overrides)
    journal.write(record)

    pve = FakePve()
    response = _handle(_request(), pve, journal)

    assert response["ok"] is False
    assert response["error"]["classification"] == "journal_corrupt"
    assert pve.submissions == 0


def test_only_the_intent_phase_can_ever_reach_a_submission() -> None:
    # Structural: the submission call site is guarded so that every phase
    # other than 'intent' returns or raises before reaching it.
    source = HELPER_PATH.read_text(encoding="utf-8")
    body = source[source.index("def _ensure_submitted"):]
    body = body[: body.index("def _extract_upid")]
    assert body.count('"pvesh", "--noproxy", "create"') == 1
    guard = body.index('if phase != "intent":')
    submission = body.index('"pvesh", "--noproxy", "create"')
    assert guard < submission
    for phase in ("terminal", "task_known", "submitted"):
        assert body.index(f'phase == "{phase}"') < submission


# ---------------------------------------------------------------------------
# Submission proof: "not submitted" comes from the durable journal phase,
# never from an error name, an absence, a lock, or a transport failure.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "classification"),
    [
        ({"present": False}, "guest_unavailable"),
        ({"resource_type": "qemu"}, "unsupported_resource_type"),
        ({"node": "pve-b"}, "stale_target"),
        ({"status": "migrating"}, "guest_unavailable"),
    ],
)
def test_pre_submission_failures_are_reported_as_provably_not_submitted(
    tmp_path: Path, kwargs, classification
) -> None:
    """Every failure raised while the journal is still at `intent`.

    The submission subprocess is launched strictly after the fsynced
    `intent -> submitted` rename, so none of these can have submitted
    anything, whatever their classification happens to be called.
    """

    journal = _journal(tmp_path)
    pve = FakePve(**kwargs)
    operation_id, _ = _identity(_ownership())

    response = _handle(_request(), pve, journal)

    assert response["ok"] is False
    assert response["error"]["classification"] == classification
    assert response["error"]["submission"] == helper.SUBMISSION_NOT_SUBMITTED
    assert pve.submissions == 0
    assert journal.read(operation_id)["phase"] == "intent"


def test_a_failed_pre_submission_pve_read_is_still_not_submitted(
    tmp_path: Path,
) -> None:
    """Classification is irrelevant; the journal phase is what proves it."""

    class BrokenReads(FakePve):
        def _dispatch(self, argv):
            if _pvesh_call(tuple(argv)).path.endswith("/config"):
                return 1, b"", b"permission denied"
            return super()._dispatch(argv)

    pve = BrokenReads()
    response = _handle(_request(), pve, _journal(tmp_path))
    assert response["ok"] is False
    assert response["error"]["classification"] == "execution_failed"
    assert response["error"]["submission"] == helper.SUBMISSION_NOT_SUBMITTED
    assert pve.submissions == 0


def test_a_lock_or_ambiguous_state_before_submission_stays_uncertain(
    tmp_path: Path,
) -> None:
    """Not submitted by us is not the same as nothing happening.

    A guest locked by an in-flight PVE operation, or canonical state that is
    not a clean absence, must not be reported as a clean non-submission.
    """

    ownership = _ownership()
    _, snapshot_name = _identity(ownership)

    locked = _handle(_request(), FakePve(lock="snapshot"), _journal(tmp_path / "a"))
    assert locked["ok"] is True and locked["outcome"] == "uncertain"

    ambiguous = _handle(
        _request(),
        FakePve(
            snapshots=[
                _completed_snapshot(_ownership(job_id=str(uuid.uuid4())), snapshot_name)
            ]
        ),
        _journal(tmp_path / "b"),
    )
    assert ambiguous["ok"] is True and ambiguous["outcome"] == "uncertain"


@pytest.mark.parametrize("phase", ["submitted", "task_known"])
def test_failures_at_or_after_the_submission_boundary_are_never_not_submitted(
    tmp_path: Path, phase: str
) -> None:
    journal = _journal(tmp_path)
    operation_id, snapshot_name = _identity(_ownership())
    record = {
        "journal_version": 1,
        "snapshot_operation_id": operation_id,
        "request_fingerprint": helper.request_fingerprint(
            helper.validate_request(_request())
        ),
        "vmid": VMID,
        "expected_node": NODE,
        "snapshot_name": snapshot_name,
        "phase": phase,
    }
    if phase == "task_known":
        record["task_upid"] = UPID

    class BrokenReads(FakePve):
        def _dispatch(self, argv):
            if _is_pvesh(tuple(argv), "get"):
                return 1, b"", b"pve read failed"
            return super()._dispatch(argv)

    journal.write(record)
    pve = BrokenReads()
    # A journaled "submitted" is recovered by the mutating operation itself
    # (it must never resubmit). A journaled "task_known" is only ever
    # observed read-only from here on: the mutating operation returns its
    # cached facts immediately without touching PVE at all, so this failure
    # can only manifest through the read-only inspection.
    operation = (
        "inspect_job_snapshot_state"
        if phase == "task_known"
        else "ensure_pre_update_snapshot_submitted"
    )
    response = _handle(_request(operation), pve, journal)

    assert response["ok"] is False
    assert response["error"].get("submission") == helper.SUBMISSION_UNKNOWN
    assert pve.submissions == 0


def test_an_unreadable_journal_is_never_reported_as_not_submitted(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path)
    journal.ensure_directory()
    operation_id, _ = _identity(_ownership())
    (journal.directory / f"op-{operation_id}.json").write_text("{not json")
    pve = FakePve()

    response = _handle(_request(), pve, journal)

    assert response["ok"] is False
    assert response["error"]["classification"] == "journal_corrupt"
    assert response["error"]["submission"] == helper.SUBMISSION_UNKNOWN
    assert pve.submissions == 0


def test_a_lease_held_by_another_invocation_is_never_not_submitted(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path)
    journal.ensure_directory()
    with helper.VmidMutationLock(VMID, journal.directory):
        pve = FakePve()
        response = _handle(_request(), pve, journal)
    assert response["ok"] is False
    assert response["error"]["classification"] == "operation_in_progress"
    assert response["error"]["submission"] == helper.SUBMISSION_UNKNOWN
    assert pve.submissions == 0


def test_an_operation_request_mismatch_is_never_not_submitted(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path)
    operation_id, snapshot_name = _identity(_ownership())
    journal.write(
        {
            "journal_version": 1,
            "snapshot_operation_id": operation_id,
            "request_fingerprint": "0" * 64,
            "vmid": VMID,
            "expected_node": NODE,
            "snapshot_name": snapshot_name,
            "phase": "intent",
        }
    )
    pve = FakePve()
    response = _handle(_request(), pve, journal)
    assert response["error"]["classification"] == "operation_request_mismatch"
    assert response["error"]["submission"] == helper.SUBMISSION_UNKNOWN
    assert pve.submissions == 0


def test_the_submission_proof_defaults_to_unknown() -> None:
    """Fail-closed by construction: the default is never the releasing value."""

    import inspect as _inspect

    default = _inspect.signature(helper.SnapshotError).parameters["submission"].default
    assert default == helper.SUBMISSION_UNKNOWN
    assert helper.SnapshotError("x", "y").submission == helper.SUBMISSION_UNKNOWN


def test_not_submitted_is_only_reachable_before_physical_submission() -> None:
    """Structural guard on the only value that may release a fenced job.

    Every emission must lie on a path that provably precedes physical PVE
    work -- otherwise a failure after submission could inherit it. Since the
    detached executor is gated on a release byte, "before physical work" is
    now precisely "before `executor.release()`", which is a strictly stronger
    barrier than the journal write it follows.
    """

    source = HELPER_PATH.read_text(encoding="utf-8")
    marker = "submission=SUBMISSION_NOT_SUBMITTED"

    def _slice(start: str, end: str) -> str:
        return source[source.index(start) : source.index(end)]

    prepare = _slice("def _prepare_detached_runner", "def _reap")
    ensure = _slice("def _ensure_submitted", "def _extract_upid")

    # Emissions exist in exactly two functions and nowhere else: the executor
    # preparation, and the create flow itself.
    assert prepare.count(marker) > 0
    assert ensure.count(marker) > 0
    assert source.count(marker) == prepare.count(marker) + ensure.count(marker)

    # In the create flow every emission precedes the ONE thing that permits
    # physical PVE work.
    barrier = ensure.index("executor.release()")
    offset = 0
    while True:
        found = ensure.find(marker, offset)
        if found < 0:
            break
        assert found < barrier
        offset = found + 1

    # And the executor is always prepared BEFORE the durable submitted write,
    # so every preparation failure is provably pre-submission as well.
    assert ensure.index("_prepare_detached_runner(") < ensure.index(
        '"phase": "submitted"'
    )
    assert ensure.index('"phase": "submitted"') < barrier

    # The default is the fail-closed value, so nothing inherits it.
    assert 'submission: str = SUBMISSION_UNKNOWN' in source


# ---------------------------------------------------------------------------
# Host-side serialization: inspection must join the per-VMID mutation lease
#
# A backend that lost its SSH connection, timed out, or crashed cannot infer
# that the remote mutator is gone with it: the mutator is a separate process
# on the PVE host and may still be alive, still holding its lease, still
# between its own durable journal phases. A concurrent inspection must not be
# able to consume `intent` as current routing evidence while that lease is held.
# ---------------------------------------------------------------------------


def test_inspect_reports_operation_in_progress_while_the_mutator_holds_the_lease(
    tmp_path: Path,
) -> None:
    """The exact backend-died-but-remote-helper-still-alive witness.

    The submission-only mutator acquires its per-VMID lease and journals
    `intent`, then pauses mid-flight -- exactly where a caller that lost its
    SSH connection, timed out, or crashed would leave it: still alive, still
    holding the lease, journal not yet advanced past `intent`. A concurrent
    inspection reaching the SAME journal directory must not be able to read
    that `intent` as current routing evidence while the lease is held.
    """

    import threading

    journal = _journal(tmp_path)
    request = _request()
    operation_id = request["operation_identity"]["snapshot_operation_id"]

    entered_live_check = threading.Event()
    resume_mutator = threading.Event()

    class PausingPve(FakePve):
        def _dispatch(self, argv):
            if _pvesh_call(tuple(argv)).path == "/cluster/resources":
                entered_live_check.set()
                assert resume_mutator.wait(timeout=10)
            return super()._dispatch(argv)

    pve = PausingPve()
    mutator_response: dict = {}

    def run_mutator() -> None:
        mutator_response["value"] = helper.handle_request(
            request, runner=pve, journal=journal
        )

    mutator_thread = threading.Thread(target=run_mutator)
    mutator_thread.start()
    try:
        assert entered_live_check.wait(timeout=10)

        # The mutator is paused right here: lease held, journal at intent,
        # first live PVE read in flight -- nothing below resumes it.
        assert journal.read(operation_id)["phase"] == "intent"

        inspect_pve = FakePve()
        inspected = helper.handle_request(
            _request("inspect_job_snapshot_state"),
            runner=inspect_pve,
            journal=journal,
        )

        assert inspected["ok"] is False
        assert inspected["error"]["classification"] == "operation_in_progress"
        assert inspected["error"]["submission"] == helper.SUBMISSION_UNKNOWN
        # Inspection never mutated the journal or touched PVE at all.
        assert journal.read(operation_id)["phase"] == "intent"
        assert inspect_pve.argvs == []
        assert inspect_pve.submissions == 0
    finally:
        resume_mutator.set()
        mutator_thread.join(timeout=10)
    assert not mutator_thread.is_alive()


def test_the_lease_release_after_failure_still_lets_inspect_prove_intent(
    tmp_path: Path,
) -> None:
    """Failure-before-submit liveness positive control.

    The mutator fails before `submitted` and releases the lease. Once it is
    genuinely gone, a fresh inspection may safely observe the still-`intent`
    journal and prove non-submission -- the fix must not fence the job
    forever just because a mutator was once, briefly, in the way.
    """

    import threading

    journal = _journal(tmp_path)
    request = _request()
    operation_id = request["operation_identity"]["snapshot_operation_id"]

    entered_live_check = threading.Event()
    resume_mutator = threading.Event()

    class FailingPausingPve(FakePve):
        def _dispatch(self, argv):
            if _pvesh_call(tuple(argv)).path == "/cluster/resources":
                entered_live_check.set()
                assert resume_mutator.wait(timeout=10)
                # The guest is gone by the time this resumes: live-target
                # revalidation fails, proving non-submission.
                self.present = False
            return super()._dispatch(argv)

    pve = FailingPausingPve()
    mutator_response: dict = {}

    def run_mutator() -> None:
        mutator_response["value"] = helper.handle_request(
            request, runner=pve, journal=journal
        )

    mutator_thread = threading.Thread(target=run_mutator)
    mutator_thread.start()
    assert entered_live_check.wait(timeout=10)
    resume_mutator.set()
    mutator_thread.join(timeout=10)
    assert not mutator_thread.is_alive()

    failed = mutator_response["value"]
    assert failed["ok"] is False
    assert failed["error"]["submission"] == helper.SUBMISSION_NOT_SUBMITTED
    assert journal.read(operation_id)["phase"] == "intent"
    assert pve.submissions == 0

    # The lease is free now: a fresh inspection safely proves non-submission.
    inspected = helper.handle_request(
        _request("inspect_job_snapshot_state"), runner=pve, journal=journal
    )
    assert inspected["ok"] is True
    assert inspected["submission_state"] == "intent"
    assert pve.submissions == 0


def test_the_lease_release_after_submission_lets_inspect_see_task_known(
    tmp_path: Path,
) -> None:
    """Submitted/task_known crash-recovery positive control.

    The mutator crosses the door and releases the lease. A fresh inspection
    then correctly sees the submission -- never absent, never intent -- and
    exactly one PVE submission ever happens, however many times it is
    inspected afterward.
    """

    import threading

    journal = _journal(tmp_path)
    ownership = _ownership()
    _, snapshot_name = _identity(ownership)
    request = _request(ownership=ownership)

    entered_live_check = threading.Event()
    resume_mutator = threading.Event()

    class PausingPve(FakePve):
        def _dispatch(self, argv):
            if _pvesh_call(tuple(argv)).path == "/cluster/resources":
                entered_live_check.set()
                assert resume_mutator.wait(timeout=10)
            return super()._dispatch(argv)

    pve = PausingPve(
        task_sequence=[{"upid": UPID, "status": "stopped", "exitstatus": "OK"}]
    )
    pve.on_submit = lambda p: p.snapshots.append(
        _completed_snapshot(ownership, snapshot_name)
    )
    mutator_response: dict = {}

    def run_mutator() -> None:
        mutator_response["value"] = helper.handle_request(
            request, runner=pve, journal=journal
        )

    mutator_thread = threading.Thread(target=run_mutator)
    mutator_thread.start()
    assert entered_live_check.wait(timeout=10)
    resume_mutator.set()
    mutator_thread.join(timeout=10)
    assert not mutator_thread.is_alive()

    submitted = mutator_response["value"]
    assert submitted["ok"] is True
    assert submitted["outcome"] == "uncertain"
    assert submitted["submission_state"] == "task_known"
    assert pve.submissions == 1

    inspected_first = helper.handle_request(
        _request("inspect_job_snapshot_state", ownership=ownership),
        runner=pve,
        journal=journal,
    )
    inspected_second = helper.handle_request(
        _request("inspect_job_snapshot_state", ownership=ownership),
        runner=pve,
        journal=journal,
    )
    assert inspected_first["ok"] is True
    assert inspected_first["submission_state"] == "task_known"
    assert inspected_first["outcome"] == "completed"
    assert inspected_second["outcome"] == "completed"
    # Never a second submission, however many times it is inspected.
    assert pve.submissions == 1


# ---------------------------------------------------------------------------
# Durable prove-and-seal state machine
# ---------------------------------------------------------------------------


def test_seal_is_durable_idempotent_and_submit_must_obey_it(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    pve = FakePve()
    operation_id, _ = _identity(_ownership())

    first = _handle(_request("seal_operation_never_submitted"), pve, journal)
    second = _handle(_request("seal_operation_never_submitted"), pve, journal)
    delayed_submit = _handle(_request(), pve, journal)

    assert first == second
    assert first["outcome"] == "not_submitted"
    assert first["submission_state"] == "sealed_not_submitted"
    assert delayed_submit["outcome"] == "not_submitted"
    assert delayed_submit["submission_state"] == "sealed_not_submitted"
    assert journal.read(operation_id)["phase"] == "sealed_not_submitted"
    assert pve.argvs == []
    assert pve.submissions == 0


def test_seal_advances_intent_without_any_pve_read(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    request = helper.validate_request(_request())
    operation_id = request["snapshot_operation_id"]
    journal.write(
        {
            "journal_version": 1,
            "snapshot_operation_id": operation_id,
            "request_fingerprint": helper.request_fingerprint(request),
            "vmid": VMID,
            "expected_node": NODE,
            "snapshot_name": request["snapshot_name"],
            "phase": "intent",
        }
    )
    pve = FakePve(present=False, node="pve-moved")

    sealed = _handle(_request("seal_operation_never_submitted"), pve, journal)

    assert sealed["submission_state"] == "sealed_not_submitted"
    assert journal.read(operation_id)["phase"] == "sealed_not_submitted"
    assert pve.argvs == []
    assert pve.submissions == 0


def test_submit_wins_before_seal_and_is_never_resubmitted(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    pve = FakePve()

    submitted = _handle(_request(), pve, journal)
    refused = _handle(_request("seal_operation_never_submitted"), pve, journal)
    retried = _handle(_request(), pve, journal)

    assert submitted["submission_state"] == "task_known"
    assert refused["outcome"] == "uncertain"
    assert refused["submission_state"] == "task_known"
    assert refused["task_upid"] == UPID
    assert retried["submission_state"] == "task_known"
    assert pve.submissions == 1


@pytest.mark.parametrize("kind", ["corrupt", "mismatch"])
def test_seal_refuses_corrupt_or_mismatched_journal(
    tmp_path: Path, kind: str
) -> None:
    journal = _journal(tmp_path)
    journal.ensure_directory()
    operation_id, snapshot_name = _identity(_ownership())
    if kind == "corrupt":
        (journal.directory / f"op-{operation_id}.json").write_text("{bad json")
    else:
        journal.write(
            {
                "journal_version": 1,
                "snapshot_operation_id": operation_id,
                "request_fingerprint": "0" * 64,
                "vmid": VMID,
                "expected_node": NODE,
                "snapshot_name": snapshot_name,
                "phase": "intent",
            }
        )
    pve = FakePve()

    response = _handle(_request("seal_operation_never_submitted"), pve, journal)

    assert response["ok"] is False
    assert response["error"]["classification"] in (
        "journal_corrupt",
        "operation_request_mismatch",
    )
    assert pve.argvs == []
    assert pve.submissions == 0


def test_taskless_inspect_uses_the_same_no_in_flight_lock_bar(
    tmp_path: Path,
) -> None:
    ownership = _ownership()
    operation_id, snapshot_name = _identity(ownership)
    journal = _journal(tmp_path)
    journal.write(
        {
            "journal_version": 1,
            "snapshot_operation_id": operation_id,
            "request_fingerprint": helper.request_fingerprint(
                helper.validate_request(_request())
            ),
            "vmid": VMID,
            "expected_node": NODE,
            "snapshot_name": snapshot_name,
            "phase": "submitted",
        }
    )
    pve = FakePve(
        lock="snapshot",
        snapshots=[_completed_snapshot(ownership, snapshot_name)],
    )

    locked = _handle(_request("inspect_job_snapshot_state"), pve, journal)
    assert locked["outcome"] == "uncertain"
    assert locked["submission_state"] == "submitted"
    assert pve.submissions == 0

    pve.lock = None
    completed = _handle(_request("inspect_job_snapshot_state"), pve, journal)
    assert completed["outcome"] == "completed"
    assert completed["submission_state"] == "submitted"
    assert pve.submissions == 0


# ---------------------------------------------------------------------------
# P1-A. The detached destructive `pvesh` must outlive the ordinary bounded
# submission deadline. A physical snapshot legitimately slower than
# COMMAND_TIMEOUT_SECONDS must not have its `pvesh` killed: PVE's worker keeps
# running regardless, and killing the local process destroys the only channel
# through which the exact terminal UPID is still recoverable -- leaving a
# `submitted` operation that may NEVER be resubmitted permanently
# unattributable.
# ---------------------------------------------------------------------------


def test_reads_stay_bounded_but_the_destructive_capture_has_no_wall_clock(
    tmp_path: Path,
) -> None:
    pve = FakePve()
    journal = _journal(tmp_path)

    response = _handle(_request(), pve, journal)

    assert response["ok"] is True
    reads = [
        (argv, deadline)
        for argv, deadline in pve.deadlines
        if not _is_pvesh(argv, "create")
    ]
    submissions = [
        (argv, deadline)
        for argv, deadline in pve.deadlines
        if _is_pvesh(argv, "create")
    ]
    # Every read/preflight command keeps its ordinary bounded deadline...
    assert reads
    assert all(
        deadline == helper.COMMAND_TIMEOUT_SECONDS for _argv, deadline in reads
    )
    # ...and the one destructive submission runs under no wall clock at all.
    assert len(submissions) == 1
    argv, deadline = submissions[0]
    assert deadline is None
    assert deadline is helper.DETACHED_CAPTURE_NO_DEADLINE
    assert helper.COMMAND_TIMEOUT_SECONDS == 120.0
    assert argv[:3] == ("pvesh", "--noproxy", "create")
    assert pve.submissions == 1

    # And it is still at-most-once: a replay resubmits nothing.
    replay = _handle(_request(), pve, journal)
    assert replay["ok"] is True
    assert pve.submissions == 1


def _late_writer_argv(seconds: float, payload: str) -> tuple[str, ...]:
    """A child that writes only AFTER outliving a short bounded deadline."""

    return (
        sys.executable,
        "-c",
        f"import sys, time; time.sleep({seconds}); "
        f"sys.stdout.write({payload!r}); sys.stdout.flush()",
    )


def test_no_deadline_mode_lets_a_slow_child_reach_its_terminal_result() -> None:
    """Structural proof, in fractions of a second rather than 121 of them.

    The point is not the specific numbers: it is that the SAME child which a
    bounded deadline kills mid-flight (losing its output entirely) runs to its
    natural end and yields its exact stdout when the deadline is absent.
    """

    argv = _late_writer_argv(0.4, UPID)

    killed = helper._run_bounded(argv, 0.05, helper.MAX_CAPTURE_OUTPUT_BYTES)
    assert killed.timed_out is True
    assert helper._extract_upid(killed) is None

    survived = helper._run_bounded(
        argv, helper.DETACHED_CAPTURE_NO_DEADLINE, helper.MAX_CAPTURE_OUTPUT_BYTES
    )
    assert survived.timed_out is False
    assert survived.returncode == 0
    assert helper._extract_upid(survived) == UPID


def test_the_output_bound_still_fails_closed_without_a_deadline() -> None:
    argv = (
        sys.executable,
        "-c",
        "import sys; sys.stdout.write('x' * 200000); sys.stdout.flush()",
    )

    result = helper._run_bounded(argv, helper.DETACHED_CAPTURE_NO_DEADLINE, 1024)

    assert result.output_exceeded is True
    assert result.timed_out is False
    # Fail closed: an over-bound capture never yields a task identity.
    assert helper._extract_upid(result) is None


def test_the_detached_capture_child_asks_for_no_deadline(tmp_path: Path) -> None:
    """The seam itself: `_run_capture_child` must not pass the ordinary 120s."""

    seen: list[tuple[tuple[str, ...], float | None, int]] = []

    def recording_runner(argv, timeout, max_output):
        seen.append((tuple(argv), timeout, max_output))
        return helper.CommandResult(
            0, json.dumps(UPID).encode("utf-8"), b"", False, False
        )

    journal = _journal(tmp_path)
    operation_id, _name = _identity(_ownership())
    helper._run_capture_child(
        recording_runner, journal, operation_id, ("pvesh", "create", "/x")
    )

    assert len(seen) == 1
    _argv, timeout, max_output = seen[0]
    assert timeout is None
    assert max_output == helper.MAX_CAPTURE_OUTPUT_BYTES
    recovered = journal.read_completed_capture(operation_id)
    assert recovered is not None
    assert helper._extract_upid(recovered) == UPID


def test_an_over_bound_capture_is_never_promoted_even_with_a_valid_upid(
    tmp_path: Path,
) -> None:
    """The output bound is not a deadline, and removing the deadline must not
    weaken it: a capture that blew its size bound stays UNKNOWN, and nothing
    is resubmitted."""

    journal = _journal(tmp_path)
    pve = FakePve(submit_upid="malformed")
    operation_id, _ = _identity(_ownership())
    _handle(_request(), pve, journal)
    journal.write_completed_capture(
        operation_id,
        helper.CommandResult(
            0, b'"' + UPID.encode() + b'"', b"", False, True
        ),
    )

    inspected = _handle(_request("inspect_job_snapshot_state"), pve, journal)

    assert inspected["submission_state"] == "submitted"
    assert inspected.get("task_upid") is None
    assert journal.read(operation_id)["phase"] == "submitted"
    assert pve.submissions == 1


# ---------------------------------------------------------------------------
# THE SUBMISSION BARRIER.
#
# `submitted` is the durable record that says "a physical mutation may now
# exist, and must never be blindly repeated". Creating it merely because a
# detached runner was INTENDED is how an ordinary `fork` EAGAIN on a loaded
# host used to strand an operation forever: journal submitted, no executor,
# no capture, no possible completion, and no permission to resubmit.
#
# The repair is a two-phase handoff. The executor is established FIRST and
# proves it exists (READY), but is blocked and cannot reach `pvesh`. Only
# after the durable `submitted` write is it released (GO). So both halves
# hold: physical work can never precede the write-ahead record, and the
# write-ahead record is never created without a runner that can complete it.
#
# These exercise the REAL forks, the REAL pipes, and the REAL lease.
# ---------------------------------------------------------------------------


def _wait_until(predicate, seconds: float = 3.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _lease_is_free(directory: Path) -> bool:
    try:
        with helper.VmidMutationLock(VMID, directory):
            return True
    except helper.SnapshotError:
        return False


def _marker_runner(marker: Path):
    def runner(argv, timeout, max_output):
        marker.touch()
        return helper.CommandResult(0, b'"' + UPID.encode() + b'"', b"")

    return runner


def test_a_prepared_executor_exists_holds_the_lease_and_has_run_nothing(
    tmp_path: Path,
) -> None:
    """Cases 4 and 5. READY proves existence, never permission."""

    journal = _journal(tmp_path)
    operation_id, _ = _identity(_ownership())
    ran = tmp_path / "ran"

    with helper.VmidMutationLock(VMID, journal.directory) as lease:
        executor = REAL_PREPARE_DETACHED_RUNNER(
            _marker_runner(ran),
            journal,
            operation_id,
            ("pvesh", "--noproxy", "create", "/fixed"),
            lease,
        )
        # The executor is alive -- it inherited and still holds the lease --
        # and it has not touched PVE, because it has not been released.
        assert not _lease_is_free(journal.directory)
        assert not ran.exists()
        assert journal.read_completed_capture(operation_id) is None

        # Case 5: only the release byte lets physical work begin.
        executor.release()
        assert _wait_until(ran.exists)
        assert _wait_until(
            lambda: journal.read_completed_capture(operation_id) is not None
        )
    assert _wait_until(lambda: _lease_is_free(journal.directory))


def test_an_abandoned_executor_exits_having_submitted_nothing(
    tmp_path: Path,
) -> None:
    """Case 7. A controller that never crosses its durable boundary leaves an
    executor that reads EOF and mutates nothing -- deterministically, because
    the lease is only freed when that executor has exited."""

    journal = _journal(tmp_path)
    operation_id, _ = _identity(_ownership())
    ran = tmp_path / "ran"

    with helper.VmidMutationLock(VMID, journal.directory) as lease:
        executor = REAL_PREPARE_DETACHED_RUNNER(
            _marker_runner(ran),
            journal,
            operation_id,
            ("pvesh", "--noproxy", "create", "/fixed"),
            lease,
        )
        executor.abandon()

    # The executor has exited (it released the inherited lease), so if it were
    # ever going to run `pvesh` it already would have.
    assert _wait_until(lambda: _lease_is_free(journal.directory))
    assert not ran.exists()
    assert journal.read_completed_capture(operation_id) is None


def _detached_request(pve, journal, monkeypatch):
    """Drive the helper's REAL detached path with a fake `pvesh`.

    `handle_request` picks `detach = runner is None`, so the module-level
    `_run_bounded` is patched instead of injecting a runner -- otherwise the
    whole two-phase handoff under test is bypassed.
    """

    # Undo the module-wide synchronous seam: this is the one place that must
    # run the genuine two-phase handoff, forks and pipes included.
    monkeypatch.setattr(
        helper, "_prepare_detached_runner", REAL_PREPARE_DETACHED_RUNNER
    )
    monkeypatch.setattr(helper, "_run_bounded", pve)
    monkeypatch.setattr(helper, "DETACHED_READY_TIMEOUT_SECONDS", 2.0)
    return helper.handle_request(_request(), journal=journal)


def _failing_fork(monkeypatch, *, after: int):
    """Make `os.fork` raise EAGAIN on the (after+1)-th call in this process.

    A forked child inherits its own copy of the counter, so `after=1` fails
    the intermediate's second fork while letting the first one really happen.
    """

    calls = [0]
    real_fork = os.fork

    def fork():
        calls[0] += 1
        if calls[0] > after:
            raise OSError(errno.EAGAIN, "Resource temporarily unavailable")
        return real_fork()

    monkeypatch.setattr(os, "fork", fork)


@pytest.mark.parametrize("after", [0, 1])
def test_a_fork_failure_never_creates_a_submitted_operation(
    tmp_path: Path, monkeypatch, after: int
) -> None:
    """Cases 1 and 2. Either fork failing is provably pre-submission.

    ``after=0`` fails the first fork in the controlling helper; ``after=1``
    lets that one really happen and fails the intermediate's second fork --
    the exact case whose `finally: os._exit(0)` used to look to the parent
    exactly like a successful handoff.
    """

    journal = _journal(tmp_path)
    operation_id, _ = _identity(_ownership())
    pve = FakePve()
    _failing_fork(monkeypatch, after=after)

    response = _detached_request(pve, journal, monkeypatch)

    assert response["ok"] is False
    assert response["error"]["classification"] == "execution_failed"
    # The one token that may release a fenced job, and it is honest here:
    # the executor is gated on a release byte this path never writes.
    assert response["error"]["submission"] == "not_submitted"
    # No durable submission was created, so a later attempt is still free to
    # submit exactly once -- and nothing was submitted now.
    assert journal.read(operation_id)["phase"] == "intent"
    assert pve.submissions == 0
    assert journal.read_completed_capture(operation_id) is None
    assert _wait_until(lambda: _lease_is_free(journal.directory))


def test_an_executor_that_dies_before_ready_never_creates_a_submitted_operation(
    tmp_path: Path, monkeypatch
) -> None:
    """Case 3. Any grandchild setup failure lands on the same safe side."""

    journal = _journal(tmp_path)
    operation_id, _ = _identity(_ownership())
    pve = FakePve()

    def die_before_ready(*_args, **_kwargs):
        raise OSError(errno.EMFILE, "Too many open files")

    monkeypatch.setattr(helper, "_detached_executor", die_before_ready)

    response = _detached_request(pve, journal, monkeypatch)

    assert response["ok"] is False
    assert response["error"]["submission"] == "not_submitted"
    assert journal.read(operation_id)["phase"] == "intent"
    assert pve.submissions == 0
    assert _wait_until(lambda: _lease_is_free(journal.directory))


def test_a_journal_failure_after_ready_still_submits_nothing(
    tmp_path: Path, monkeypatch
) -> None:
    """The other side of the barrier: READY was proved, but the durable write
    failed, so the executor is abandoned rather than released."""

    journal = _journal(tmp_path)
    operation_id, _ = _identity(_ownership())
    pve = FakePve()
    real_write = helper.OperationJournal.write

    def refuse_submitted(self, record):
        if record.get("phase") == "submitted":
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_write(self, record)

    monkeypatch.setattr(helper.OperationJournal, "write", refuse_submitted)

    response = _detached_request(pve, journal, monkeypatch)

    assert response["ok"] is False
    assert response["error"]["submission"] == "not_submitted"
    assert journal.read(operation_id)["phase"] == "intent"
    assert pve.submissions == 0
    assert journal.read_completed_capture(operation_id) is None
    assert _wait_until(lambda: _lease_is_free(journal.directory))


def _physical_invocations(marker: Path) -> list[str]:
    if not marker.exists():
        return []
    return [
        line for line in marker.read_text(encoding="ascii").splitlines() if line
    ]


def test_the_real_two_phase_handoff_submits_exactly_once(
    tmp_path: Path, monkeypatch
) -> None:
    """Case 6 plus cases 9, 11 and 12. The whole production path: one
    destructive pvesh, the lease held across it, a durable capture, the
    ordinary exact-UPID promotion, and no wall clock on the physical work."""

    journal = _journal(tmp_path)
    operation_id, _ = _identity(_ownership())
    pve = FakePve()
    marker = tmp_path / "physical.log"

    def counting_runner(argv, timeout, max_output):
        # Ordinary reads keep the faithful fake; only the DESTRUCTIVE call is
        # counted on disk, because the executor that makes it is a forked
        # grandchild whose in-memory effects never come back here.
        if _is_pvesh(tuple(argv), "create"):
            with marker.open("a", encoding="ascii") as handle:
                # `ascii()` so the snapshot description's own newlines
                # cannot masquerade as extra invocations.
                handle.write(f"{timeout!r}|{ascii(tuple(argv))}\n")
        return pve(argv, timeout, max_output)

    response = _detached_request(counting_runner, journal, monkeypatch)

    assert response["ok"] is True
    assert response["submission_state"] in ("submitted", "task_known")
    assert _wait_until(lambda: _lease_is_free(journal.directory))
    # The executor really ran, in its own process, and left exact evidence.
    assert _wait_until(
        lambda: journal.read(operation_id)["phase"] == "task_known"
        or journal.read_completed_capture(operation_id) is not None
    )
    recovered = _handle(_request("inspect_job_snapshot_state"), pve, journal)
    assert recovered["task_upid"] == UPID
    assert journal.read(operation_id)["phase"] == "task_known"

    # Exactly ONE physical invocation, counted on disk across the fork.
    invocations = _physical_invocations(marker)
    assert len(invocations) == 1
    deadline_text, argv_text = invocations[0].split("|", 1)
    # Cases 11 and 12: no wall-clock deadline, still `--noproxy`.
    assert deadline_text == "None"
    assert "'--noproxy'" in argv_text

    # Case 9: repeated resume/inspection never submits a second time.
    for _ in range(3):
        assert _handle(_request(), pve, journal)["ok"] is True
        _handle(_request("inspect_job_snapshot_state"), pve, journal)
    assert _physical_invocations(marker) == invocations


def test_a_submitted_operation_with_no_executor_stays_unknown_and_fenced(
    tmp_path: Path,
) -> None:
    """Case 8. The irreducible window -- a controller lost after the durable
    write -- stays UNKNOWN. It is never resubmitted and never released."""

    journal = _journal(tmp_path)
    operation_id, snapshot_name = _identity(_ownership())
    journal.write(
        {
            "journal_version": 1,
            "snapshot_operation_id": operation_id,
            "request_fingerprint": helper.request_fingerprint(
                helper.validate_request(_request())
            ),
            "vmid": VMID,
            "expected_node": NODE,
            "snapshot_name": snapshot_name,
            "phase": "submitted",
        }
    )
    pve = FakePve()

    for _ in range(3):
        inspected = _handle(_request("inspect_job_snapshot_state"), pve, journal)
        assert inspected["submission_state"] == "submitted"
        assert inspected.get("task_upid") is None
        resumed = _handle(_request(), pve, journal)
        assert resumed["submission_state"] == "submitted"
        assert resumed["outcome"] == "uncertain"

    # Never resubmitted, never sealed away as a non-submission.
    assert pve.submissions == 0
    assert journal.read(operation_id)["phase"] == "submitted"
    sealed = _handle(_request("seal_operation_never_submitted"), pve, journal)
    assert sealed["outcome"] == "uncertain"
    assert sealed["submission_state"] == "submitted"
