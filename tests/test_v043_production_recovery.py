from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from app.database import Database
from app.host_control import HostControlError
from app.service import OpsService
PVE = Path(__file__).parents[1] / "deploy" / "pve"
sys.path.insert(0, str(PVE))

from hubinet_ops_host_control import (  # noqa: E402
    HostController,
    _snapshot_description_metadata,
    parse_snapshot,
)
from tests.test_host_control import policy
from tests.test_lifecycle_snapshots import (
    CompatibleExecutor,
    FakeHostControl,
    run_queued,
    settings,
)
from tests.test_service import docker_state
from tests.test_v042_snapshot_consistency import (
    HealthyStabilizer,
    SnapshotUpdateExecutor,
    _approved_update,
)
from tests.test_v042_snapshot_retention import _owned, _record_snapshot_source


PRODUCTION_CT109_NAME = "hubinet-ops-109-pre-20260801T212708Z"
PRODUCTION_CT109_SOURCE_JOB_ID = "3a337ae2263c4b57aa5e0e8d7a0fae51"
PRODUCTION_CT109_DESCRIPTION = (
    "hubinet-ops;kind=pre-update;created_at=2026-08-01T21:27:08+00:00;"
    f"source_job_id={PRODUCTION_CT109_SOURCE_JOB_ID}\n"
)


def _description(
    *,
    vmid: int = 109,
    kind: str = "pre-update",
    source_job_id: str = PRODUCTION_CT109_SOURCE_JOB_ID,
    line_ending: str = "",
) -> tuple[str, dict[str, str]]:
    name = f"hubinet-ops-{vmid}-pre-20260801T212708Z"
    parsed = parse_snapshot(name, vmid)
    assert parsed is not None
    return (
        "hubinet-ops;"
        f"kind={kind};created_at=2026-08-01T21:27:08+00:00;"
        f"source_job_id={source_job_id}{line_ending}",
        parsed,
    )


@pytest.mark.parametrize("line_ending", ["", "\n", "\r\n"])
def test_pve_snapshot_metadata_accepts_only_optional_trailing_crlf(
    line_ending: str,
) -> None:
    description, parsed = _description(line_ending=line_ending)

    assert _snapshot_description_metadata(description, parsed) == {
        "kind": "pre-update",
        "created_at": "2026-08-01T21:27:08+00:00",
        "source_job_id": PRODUCTION_CT109_SOURCE_JOB_ID,
    }


def test_exact_production_ct109_description_is_host_owned() -> None:
    parsed = parse_snapshot(PRODUCTION_CT109_NAME, 109)

    assert parsed is not None
    assert _snapshot_description_metadata(
        PRODUCTION_CT109_DESCRIPTION,
        parsed,
    ) == {
        "kind": "pre-update",
        "created_at": "2026-08-01T21:27:08+00:00",
        "source_job_id": PRODUCTION_CT109_SOURCE_JOB_ID,
    }


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.replace(PRODUCTION_CT109_SOURCE_JOB_ID, "not-a-job"),
        lambda value: value.replace(
            PRODUCTION_CT109_SOURCE_JOB_ID,
            PRODUCTION_CT109_SOURCE_JOB_ID + " ",
        ),
        lambda value: value.replace(
            ";source_job_id=",
            ";unknown=value;source_job_id=",
        ),
        lambda value: value.replace(";created_at=2026-08-01T21:27:08+00:00", ""),
        lambda value: value.replace(
            ";source_job_id=",
            f";kind=pre-update;source_job_id=",
        ),
        lambda value: value.replace("kind=pre-update", "kind=manual"),
        lambda value: "",
    ],
    ids=[
        "invalid-source-job-id",
        "space-after-source-job-id",
        "unknown-field",
        "missing-field",
        "duplicate-field",
        "wrong-kind",
        "empty-description",
    ],
)
def test_pve_snapshot_metadata_remains_fail_closed(mutate: Any) -> None:
    description, parsed = _description()

    assert _snapshot_description_metadata(mutate(description), parsed) is None


def test_pve_snapshot_metadata_rejects_wrong_vmid() -> None:
    description, _parsed = _description()
    wrong_vmid = parse_snapshot(PRODUCTION_CT109_NAME, 108)

    assert wrong_vmid is None
    assert _snapshot_description_metadata(description, wrong_vmid) is None


class SnapshotListRunner:
    def __init__(self, snapshot: dict[str, Any]) -> None:
        self.snapshot = snapshot

    def __call__(self, argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        if argv[:2] == ["pvesh", "get"] and argv[2].endswith("/snapshot"):
            return subprocess.CompletedProcess(argv, 0, json.dumps([self.snapshot]), "")
        if argv[:2] == ["readlink", "-f"]:
            return subprocess.CompletedProcess(argv, 0, "/etc/pve/nodes/pve-a\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")


@pytest.mark.parametrize(
    ("vmid", "resource_type", "name"),
    [
        (101, "lxc", "hubinet-ops-101-pre-20260801T212708Z"),
        (100, "qemu", "hubinet-ops-100-pre-20260801T212708Z"),
    ],
)
@pytest.mark.parametrize("line_ending", ["", "\n", "\r\n"])
def test_lxc_and_qemu_snapshot_lists_accept_pve_line_endings(
    tmp_path: Path,
    vmid: int,
    resource_type: str,
    name: str,
    line_ending: str,
) -> None:
    description = (
        "hubinet-ops;kind=pre-update;created_at=2026-08-01T21:27:08+00:00;"
        f"source_job_id={PRODUCTION_CT109_SOURCE_JOB_ID}{line_ending}"
    )
    runner = SnapshotListRunner(
        {
            "snapname": name,
            "description": description,
            "snaptime": 1_785_619_632,
        }
    )
    controller = HostController(policy(tmp_path), runner=runner)

    listed = controller.list_snapshots(vmid, resource_type)

    assert len(listed) == 1
    assert listed[0]["owned_by_hubinet_ops"] is True
    assert listed[0]["ownership_status"] == "owned"
    assert listed[0]["source_job_id"] == PRODUCTION_CT109_SOURCE_JOB_ID


@pytest.mark.parametrize(
    ("snapshot", "status"),
    [
        (
            {
                "snapname": "foreign-backup",
                "description": PRODUCTION_CT109_DESCRIPTION,
                "snaptime": 1_785_619_632,
            },
            "foreign",
        ),
        (
            {
                "snapname": "current",
                "description": "",
                "current": 1,
            },
            "foreign",
        ),
        (
            {
                "snapname": "hubinet-ops-101-pre-20260801T212708Z",
                "description": "",
                "snaptime": 1_785_619_632,
            },
            "uncertain",
        ),
    ],
)
def test_foreign_current_and_empty_description_remain_ineligible(
    tmp_path: Path,
    snapshot: dict[str, Any],
    status: str,
) -> None:
    controller = HostController(
        policy(tmp_path),
        runner=SnapshotListRunner(snapshot),
    )

    listed = controller.list_snapshots(101, "lxc")

    assert listed[0]["ownership_status"] == status
    assert listed[0]["owned_by_hubinet_ops"] is False
    assert listed[0]["rollback_eligible"] is False
    assert listed[0]["delete_eligible"] is False


def test_host_owned_snapshot_without_backend_proof_is_explicitly_unproven(
    tmp_path: Path,
) -> None:
    cfg = settings(tmp_path)
    db = Database(cfg.db_path)
    host = FakeHostControl("running")
    snapshot = _owned(
        106,
        "20260801T212708Z",
        created_at="2026-08-01T21:27:08+00:00",
    )
    host.snapshots = [snapshot]
    service = OpsService(cfg, db, CompatibleExecutor(), host_control=host)

    modeled = service.list_snapshots(106)["snapshots"][0]

    assert modeled["host_owned"] is True
    assert modeled["backend_proven"] is False
    assert modeled["ownership_status"] == "host_owned_unproven"
    assert modeled["owned_by_hubinet_ops"] is False
    assert modeled["rollback_eligible"] is False
    assert modeled["delete_eligible"] is False
    state = service.get_state(106)
    assert state["snapshot_unproven_count"] == 1
    assert state["latest_unproven_snapshot_name"] == snapshot["name"]


def test_backend_proof_promotes_exact_physical_snapshot_to_managed(
    tmp_path: Path,
) -> None:
    cfg = settings(tmp_path)
    db = Database(cfg.db_path)
    host = FakeHostControl("running")
    snapshot = _owned(
        106,
        "20260801T212708Z",
        created_at="2026-08-01T21:27:08+00:00",
    )
    host.snapshots = [snapshot]
    source = _record_snapshot_source(db, snapshot)
    service = OpsService(cfg, db, CompatibleExecutor(), host_control=host)

    modeled = service.list_snapshots(106)["snapshots"][0]

    assert modeled["host_owned"] is True
    assert modeled["backend_proven"] is True
    assert modeled["ownership_status"] == "managed"
    assert modeled["owned_by_hubinet_ops"] is True
    assert modeled["source_job_id"] == source["id"]


class ReconciliationHost(FakeHostControl):
    def __init__(self, remote: dict[str, Any]) -> None:
        super().__init__("running")
        self.remote = remote
        self.lookup_calls: list[tuple[int, str]] = []

    def find_job_by_request_id(
        self,
        vmid: int,
        request_id: str,
    ) -> dict[str, Any] | None:
        self.lookup_calls.append((vmid, request_id))
        return dict(self.remote)


def _orphaned_042_update(
    tmp_path: Path,
    *,
    remote_mutation: dict[str, Any] | None = None,
) -> tuple[OpsService, Database, dict[str, Any], dict[str, Any], ReconciliationHost]:
    cfg = settings(tmp_path)
    db = Database(cfg.db_path)
    backend_job_id = "b" * 32
    host_job_id = "c" * 32
    snapshot = {
        **_owned(
            106,
            "20260801T212708Z",
            kind="pre-update",
            created_at="2026-08-01T21:27:08+00:00",
        ),
        "source_job_id": host_job_id,
        "pve_snaptime": 1_785_619_632,
    }
    request_id = f"pre-update-snapshot-{backend_job_id}"
    now = "2026-08-01T21:27:08+00:00"
    result = {
        "pre_update_snapshot_create": {
            "version": 1,
            "request_id": request_id,
            "phase": "definitive_failed",
            "snapshot_name": snapshot["name"],
            "host_job_id": host_job_id,
            "snapshot_identity": None,
            "last_error": "Snapshot create physical identity mismatch",
        }
    }
    with db._lock, db._connect() as connection:
        connection.execute(
            "INSERT INTO jobs "
            "(id,request_id,operation_type,plan_id,vmid,container_name,status,stage,"
            "progress,snapshot_name,result,error,created_at,updated_at) "
            "VALUES(?,?, 'update',NULL,106,'ct-106','failed','failed',100,?,?,?, ?,?)",
            (
                backend_job_id,
                backend_job_id,
                snapshot["name"],
                json.dumps(result, sort_keys=True),
                "Pre-update snapshot host operation failed",
                now,
                now,
            ),
        )
    remote = {
        "id": host_job_id,
        "request_id": request_id,
        "vmid": 106,
        "operation_type": "snapshot_create",
        "argument": snapshot["name"],
        "status": "failed",
        "stage": "failed",
        "result": None,
        "error": "Snapshot create physical identity mismatch",
    }
    remote.update(remote_mutation or {})
    host = ReconciliationHost(remote)
    host.snapshots = [snapshot]
    service = OpsService(cfg, db, CompatibleExecutor(), host_control=host)
    return service, db, db.get_job(backend_job_id), snapshot, host


def test_orphaned_042_snapshot_proof_is_reconciled_from_complete_durable_chain(
    tmp_path: Path,
) -> None:
    service, db, job, snapshot, host = _orphaned_042_update(tmp_path)

    reconciled = service.reconcile_snapshot_proofs()
    modeled = service.list_snapshots(106)["snapshots"][0]

    assert reconciled == [
        {
            "vmid": 106,
            "snapshot_name": snapshot["name"],
            "backend_job_id": job["id"],
            "host_job_id": snapshot["source_job_id"],
            "status": "reconciled",
        }
    ]
    assert host.lookup_calls == [(106, f"pre-update-snapshot-{job['id']}")]
    assert db.has_snapshot_proof(
        job["id"],
        106,
        snapshot["name"],
        snapshot["source_job_id"],
        snapshot["pve_snaptime"],
    )
    assert modeled["ownership_status"] == "managed"
    assert modeled["backend_proven"] is True


@pytest.mark.parametrize(
    "remote_mutation",
    [
        {"id": "d" * 32},
        {"request_id": "pre-update-snapshot-" + "d" * 32},
        {"vmid": 109},
        {"operation_type": "snapshot_delete"},
        {"argument": "hubinet-ops-106-pre-20260801T220000Z"},
        {"status": "running"},
    ],
    ids=[
        "host-job-id",
        "request-id",
        "vmid",
        "operation",
        "snapshot-name",
        "nonterminal-host-job",
    ],
)
def test_orphaned_snapshot_stays_unproven_when_durable_chain_is_incomplete(
    tmp_path: Path,
    remote_mutation: dict[str, Any],
) -> None:
    service, db, job, snapshot, _host = _orphaned_042_update(
        tmp_path,
        remote_mutation=remote_mutation,
    )

    assert service.reconcile_snapshot_proofs() == []
    modeled = service.list_snapshots(106)["snapshots"][0]

    assert not db.has_snapshot_proof(
        job["id"],
        106,
        snapshot["name"],
        snapshot["source_job_id"],
        snapshot["pve_snaptime"],
    )
    assert modeled["ownership_status"] == "host_owned_unproven"
    assert modeled["delete_eligible"] is False
    assert modeled["rollback_eligible"] is False


def test_operator_refresh_rebuilds_snapshot_state_after_manual_pve_deletion(
    tmp_path: Path,
) -> None:
    cfg = settings(tmp_path)
    db = Database(cfg.db_path)
    host = FakeHostControl("running")
    snapshot = _owned(
        106,
        "20260801T212708Z",
        created_at="2026-08-01T21:27:08+00:00",
    )
    host.snapshots = [snapshot]
    _record_snapshot_source(db, snapshot)
    service = OpsService(cfg, db, CompatibleExecutor(), host_control=host)
    service.list_snapshots(106)
    assert service.get_state(106)["snapshot_count"] == 1

    host.snapshots = []
    refreshed = service.refresh_container(106, operator=True)

    assert refreshed["snapshot_count"] == 0
    assert refreshed["latest_snapshot_name"] is None
    assert refreshed["latest_snapshot_at"] is None
    assert refreshed["managed_snapshots"] == []
    assert refreshed["snapshot_state_stale"] is False


def test_operator_refresh_snapshot_failure_preserves_last_known_state_and_marks_stale(
    tmp_path: Path,
) -> None:
    class FailingSnapshotHost(FakeHostControl):
        fail = False

        def list_snapshots(self, vmid: int) -> list[dict[str, Any]]:
            if self.fail:
                raise HostControlError("simulated PVE snapshot list failure")
            return super().list_snapshots(vmid)

    cfg = settings(tmp_path)
    db = Database(cfg.db_path)
    host = FailingSnapshotHost("running")
    snapshot = _owned(
        106,
        "20260801T212708Z",
        created_at="2026-08-01T21:27:08+00:00",
    )
    host.snapshots = [snapshot]
    _record_snapshot_source(db, snapshot)
    service = OpsService(cfg, db, CompatibleExecutor(), host_control=host)
    service.list_snapshots(106)

    host.fail = True
    refreshed = service.refresh_container(106, operator=True)

    assert refreshed["snapshot_count"] == 1
    assert refreshed["latest_snapshot_name"] == snapshot["name"]
    assert refreshed["snapshot_state_stale"] is True
    assert "snapshot" in refreshed["snapshot_refresh_warning"].lower()


def test_manual_prune_without_candidate_is_http_style_noop_and_creates_no_job(
    tmp_path: Path,
) -> None:
    cfg = settings(tmp_path)
    db = Database(cfg.db_path)
    host = FakeHostControl("running")
    service = OpsService(cfg, db, CompatibleExecutor(), host_control=host)

    for index in range(3):
        result = service.queue_snapshot_prune(
            106,
            "oldest",
            f"manual-prune-noop-{index:04d}",
        )
        assert result == {
            "status": "nothing_to_delete",
            "mode": "oldest",
            "deleted_count": 0,
        }

    assert db.list_jobs() == []
    assert host.calls == []


def test_prune_candidate_disappearing_before_worker_never_deletes_replacement(
    tmp_path: Path,
) -> None:
    cfg = settings(tmp_path)
    db = Database(cfg.db_path)
    host = FakeHostControl("running")
    original = _owned(
        106,
        "20260801T210000Z",
        created_at="2026-08-01T21:00:00+00:00",
    )
    replacement = _owned(
        106,
        "20260801T220000Z",
        created_at="2026-08-01T22:00:00+00:00",
    )
    host.snapshots = [original]
    _record_snapshot_source(db, original)
    _record_snapshot_source(db, replacement)
    service = OpsService(cfg, db, CompatibleExecutor(), host_control=host)

    queued = service.queue_snapshot_prune(
        106,
        "oldest",
        "manual-prune-race-0001",
    )
    host.snapshots = [replacement]
    terminal = run_queued(service, db)

    assert queued["operation_type"] == "snapshot_prune"
    assert terminal["status"] == "success"
    assert terminal["result"]["status"] == "target_disappeared"
    assert terminal["result"]["deleted_count"] == 0
    assert host.snapshots == [replacement]
    assert not any(call[0] == "snapshot_delete" for call in host.calls)


class NewlinePveRunner:
    def __init__(self) -> None:
        self.snapshots: list[dict[str, Any]] = []

    def __call__(self, argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        if argv[:2] == ["pct", "snapshot"]:
            description = argv[argv.index("--description") + 1] + "\n"
            self.snapshots.append(
                {
                    "snapname": argv[3],
                    "description": description,
                    "snaptime": 1_785_619_632,
                }
            )
        if argv[:2] == ["pvesh", "get"] and argv[2].endswith("/snapshot"):
            return subprocess.CompletedProcess(argv, 0, json.dumps(self.snapshots), "")
        if argv[:2] == ["readlink", "-f"]:
            return subprocess.CompletedProcess(argv, 0, "/etc/pve/nodes/pve-a\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")


class NewlinePveHost(FakeHostControl):
    def __init__(self, tmp_path: Path) -> None:
        super().__init__("running")
        self.runner = NewlinePveRunner()
        self.controller = HostController(policy(tmp_path), runner=self.runner)

    def list_snapshots(self, vmid: int) -> list[dict[str, Any]]:
        return self.controller.list_snapshots(vmid, "lxc")

    def execute(
        self,
        operation_type: str,
        vmid: int,
        request_id: str,
        *,
        snapshot_name: str | None = None,
        on_observed=None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        if operation_type != "snapshot_create":
            return super().execute(
                operation_type,
                vmid,
                request_id,
                snapshot_name=snapshot_name,
                on_observed=on_observed,
                **kwargs,
            )
        assert snapshot_name is not None
        host_job_id = hashlib.sha256(
            f"{vmid}:{request_id}:{snapshot_name}".encode("utf-8")
        ).hexdigest()[:32]
        if on_observed is not None:
            on_observed({"id": host_job_id})
        return self.controller.execute(
            "snapshot-create",
            vmid,
            snapshot_name,
            source_job_id=host_job_id,
        )


def test_full_update_path_accepts_exact_pve_newline_and_reaches_terminal_success(
    tmp_path: Path,
) -> None:
    executor = SnapshotUpdateExecutor(
        [docker_state(3), docker_state(3), docker_state(3)]
    )
    host = NewlinePveHost(tmp_path)
    service, db, job = _approved_update(tmp_path, executor, host)
    service.stabilizer = HealthyStabilizer()  # type: ignore[assignment]

    service._run_job(job)
    terminal = db.get_job(job["id"])

    assert terminal["status"] == "success"
    assert "update" in executor.actions
    assert "verify" in executor.actions
    proof = terminal["result"]["snapshot_proof"]
    assert proof["version"] == 3
    assert proof["snapshot_name"] == terminal["snapshot_name"]
    assert proof["host_source_job_id"] == terminal["result"][
        "pre_update_snapshot_create"
    ]["host_job_id"]
    assert host.runner.snapshots[0]["description"].endswith("\n")
    assert not host.runner.snapshots[0]["description"].endswith("\n\n")
