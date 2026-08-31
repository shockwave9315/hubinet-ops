"""Dark snapshot helper: typed shape, journal, flock, and no-blind-replay.

Exercises `deploy/hubinet-package-snapshot-helper.py` entirely against a fake
`pvesh` runner and a temporary journal directory. Nothing here runs a real
`pvesh`, `pct`, `ssh`, or any PVE operation, and the helper has no package
mutation and no snapshot delete operation to exercise in the first place.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path
import sys
from types import ModuleType
import uuid

import pytest


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


def _request(operation="create_pre_update_snapshot", **overrides) -> dict:
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
        self.resource_type = resource_type
        self.present = present
        self.argvs: list[tuple[str, ...]] = []
        self.submissions = 0
        #: What a successful submission adds to the canonical listing.
        self.on_submit = None

    def __call__(self, argv, timeout, max_output):
        self.argvs.append(tuple(argv))
        return helper.CommandResult(*self._dispatch(argv))

    def _json(self, payload):
        return 0, json.dumps(payload).encode("utf-8"), b""

    def _dispatch(self, argv):
        if argv[:3] == ("pvesh", "get", "/cluster/resources"):
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
        if argv[:2] == ("pvesh", "get") and argv[2].endswith("/config"):
            return self._json({"lock": self.lock} if self.lock else {})
        if argv[:2] == ("pvesh", "get") and argv[2].endswith("/snapshot"):
            return self._json(self.snapshots)
        if argv[:2] == ("pvesh", "get") and "/tasks/" in argv[2]:
            payload = (
                self.task_sequence.pop(0)
                if len(self.task_sequence) > 1
                else self.task_sequence[0]
            )
            return self._json(payload)
        if argv[:2] == ("pvesh", "create"):
            self.submissions += 1
            if self.on_submit is not None:
                self.on_submit(self)
            if self.submit_returncode != 0:
                return self.submit_returncode, b"", b"submission failed"
            return self._json(self.submit_upid)
        raise AssertionError(f"unexpected argv {argv!r}")


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


def test_only_the_two_typed_operations_exist() -> None:
    assert helper.OPERATIONS == (
        "inspect_job_snapshot_state",
        "create_pre_update_snapshot",
    )


def test_exact_request_shape_is_required() -> None:
    assert helper.validate_request(_request())["operation"] == (
        "create_pre_update_snapshot"
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
    assert set(re.findall(r'"pvesh", "([a-z]+)"', source)) == {"get", "create"}


def test_every_pvesh_path_is_built_from_fixed_constants(tmp_path: Path) -> None:
    pve = FakePve()
    pve.on_submit = lambda p: p.snapshots.append(
        _completed_snapshot(_ownership(), _identity(_ownership())[1])
    )
    _handle(_request(), pve, _journal(tmp_path))
    operation_id, snapshot_name = _identity(_ownership())
    for argv in pve.argvs:
        assert argv[0] == "pvesh"
        assert argv[1] in ("get", "create")
        # No shell, no command string, and no request-provided free text.
        assert not any("&&" in item or ";" in item or "|" in item for item in argv[:3])
    submissions = [argv for argv in pve.argvs if argv[1] == "create"]
    assert len(submissions) == 1
    assert submissions[0][2] == f"/nodes/{NODE}/lxc/{VMID}/snapshot"
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


def test_a_known_task_is_polled_and_never_resubmitted(tmp_path: Path) -> None:
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
    assert response["outcome"] == "completed"
    assert response["task_upid"] == UPID
    assert any("/tasks/" in argv[2] for argv in pve.argvs if argv[1] == "get")


def test_an_identical_retry_replays_the_terminal_answer(tmp_path: Path) -> None:
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

    assert first["outcome"] == second["outcome"] == "completed"
    assert pve.submissions == 1
    assert journal.read(operation_id)["phase"] == "terminal"


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
    assert record["phase"] == "terminal"
    assert record["snapshot_operation_id"] == operation_id


# ---------------------------------------------------------------------------
# Task semantics
# ---------------------------------------------------------------------------


def test_a_task_failure_is_terminal_and_never_confirms(tmp_path: Path) -> None:
    pve = FakePve(
        task_sequence=[{"upid": UPID, "status": "stopped", "exitstatus": "boom"}]
    )
    response = _handle(_request(), pve, _journal(tmp_path))
    assert response["outcome"] == "failed"
    assert pve.submissions == 1


def test_a_successful_task_without_the_canonical_snapshot_is_uncertain(
    tmp_path: Path,
) -> None:
    # Task says OK but the canonical listing never gains our snapshot.
    pve = FakePve()
    response = _handle(_request(), pve, _journal(tmp_path))
    assert response["outcome"] == "uncertain"
    assert "canonical state does not show" in response["reason"]


def test_a_task_still_running_at_the_bound_is_uncertain(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(helper, "TASK_POLL_TIMEOUT_SECONDS", 0.0)
    pve = FakePve(task_sequence=[{"upid": UPID, "status": "running"}])
    response = _handle(_request(), pve, _journal(tmp_path))
    assert response["outcome"] == "uncertain"
    assert "still running" in response["reason"]
    assert pve.submissions == 1


def test_a_submission_that_returns_no_usable_task_identity_is_uncertain(
    tmp_path: Path,
) -> None:
    for submit_upid, returncode in (("not-a-upid", 0), (UPID, 1)):
        pve = FakePve(submit_upid=submit_upid, submit_returncode=returncode)
        response = _handle(_request(), pve, _journal(tmp_path / str(returncode)))
        assert response["outcome"] == "uncertain"
        assert "task identity" in response["reason"]


@pytest.mark.parametrize("exitstatus", ["OK", "WARNINGS: 2"])
def test_pve_non_error_exit_statuses_are_accepted(
    tmp_path: Path, exitstatus: str
) -> None:
    ownership = _ownership()
    _, snapshot_name = _identity(ownership)
    pve = FakePve(
        task_sequence=[{"upid": UPID, "status": "stopped", "exitstatus": exitstatus}]
    )
    pve.on_submit = lambda p: p.snapshots.append(
        _completed_snapshot(ownership, snapshot_name)
    )
    response = _handle(_request(), pve, _journal(tmp_path / exitstatus[:2]))
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
    pve = FakePve(task_sequence=[status])
    pve.on_submit = lambda p: p.snapshots.append(
        _completed_snapshot(ownership, snapshot_name)
    )
    response = _handle(_request(), pve, _journal(tmp_path))
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
    assert not any(argv[1] == "create" for argv in pve.argvs)


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
    assert _handle(_request(), pve, journal)["outcome"] == "completed"


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
    assert not any(argv[1] == "create" for argv in pve.argvs)
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
    body = source[source.index("def _create_pre_update_snapshot"):]
    body = body[: body.index("def _extract_upid")]
    assert body.count('"pvesh", "create"') == 1
    guard = body.index('if phase != "intent":')
    submission = body.index('"pvesh", "create"')
    assert guard < submission
    for phase in ("terminal", "task_known", "submitted"):
        assert body.index(f'phase == "{phase}"') < submission
