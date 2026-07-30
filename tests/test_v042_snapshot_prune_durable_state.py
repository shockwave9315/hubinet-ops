from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from app.contracts import (
    SNAPSHOT_PRUNE_DELETED_HISTORY_LIMIT,
    SNAPSHOT_PRUNE_STATE_VERSION,
)
from app.database import Database
from app.service import OpsService
from tests.test_lifecycle_snapshots import CompatibleExecutor, settings
from tests.test_v042_snapshot_prune_restart import (
    RestartablePruneHost,
    SimulatedBackendRestart,
)
from tests.test_v042_snapshot_retention import _owned


def _managed_snapshots(vmid: int, count: int) -> list[dict[str, Any]]:
    newest = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
    return [
        _owned(
            vmid,
            (newest - timedelta(minutes=index)).strftime("%Y%m%dT%H%M%SZ"),
            created_at=(newest - timedelta(minutes=index)).isoformat(),
        )
        for index in range(count)
    ]


def _queue_all(
    service: OpsService,
    request_id: str,
) -> dict[str, Any]:
    return service.queue_snapshot_prune(
        106,
        "all_unprotected",
        request_id,
        confirmation="DELETE_ALL_UNPROTECTED",
    )


def test_snapshot_prune_insert_contains_complete_state_atomically(
    tmp_path: Path,
) -> None:
    cfg = settings(tmp_path)
    db = Database(cfg.db_path)

    job, created = db.create_snapshot_prune_job(
        vmid=106,
        container_name="ct-106",
        request_id="atomic-prune-insert-0001",
        mode="all_unprotected",
        retention_target=None,
        source_job_id=None,
    )

    assert created is True
    assert job["status"] == "queued"
    assert job["result"] == {
        "prune_version": SNAPSHOT_PRUNE_STATE_VERSION,
        "mode": "all_unprotected",
        "retention_target": None,
        "source_job_id": None,
        "deleted": [],
        "deleted_count": 0,
        "deleted_history_truncated": False,
        "current": None,
        "phase": "selecting",
    }
    with sqlite3.connect(cfg.db_path) as conn:
        raw = conn.execute(
            "SELECT status,result FROM jobs WHERE id=?",
            (job["id"],),
        ).fetchone()
    assert raw is not None
    assert raw[0] == "queued"
    assert json.loads(raw[1]) == job["result"]


def test_worker_cannot_claim_prune_before_atomic_insert_commits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = settings(tmp_path)
    creator_db = Database(cfg.db_path)
    worker_db = Database(cfg.db_path)
    inserted = threading.Event()
    allow_commit = threading.Event()
    original_connect = creator_db._connect

    class PausingConnection:
        def __init__(self, connection: sqlite3.Connection) -> None:
            self.connection = connection

        def __enter__(self) -> PausingConnection:
            self.connection.__enter__()
            return self

        def __exit__(self, *args: object) -> object:
            return self.connection.__exit__(*args)

        def execute(
            self,
            statement: str,
            parameters: tuple[Any, ...] = (),
        ) -> sqlite3.Cursor:
            cursor = self.connection.execute(statement, parameters)
            if statement.startswith("INSERT INTO jobs "):
                inserted.set()
                assert allow_commit.wait(timeout=5)
            return cursor

        def __getattr__(self, name: str) -> Any:
            return getattr(self.connection, name)

    monkeypatch.setattr(
        creator_db,
        "_connect",
        lambda: PausingConnection(original_connect()),
    )
    created: list[dict[str, Any]] = []
    claimed: list[dict[str, Any] | None] = []
    creator = threading.Thread(
        target=lambda: created.append(
            creator_db.create_snapshot_prune_job(
                vmid=106,
                container_name="ct-106",
                request_id="atomic-worker-race-0001",
                mode="oldest",
                retention_target=None,
                source_job_id=None,
            )[0]
        )
    )
    creator.start()
    assert inserted.wait(timeout=5)
    with sqlite3.connect(cfg.db_path) as observer:
        assert observer.execute(
            "SELECT 1 FROM jobs WHERE request_id='atomic-worker-race-0001'"
        ).fetchone() is None
    worker = threading.Thread(target=lambda: claimed.append(worker_db.next_queued_job()))
    worker.start()
    worker.join(timeout=0.1)
    assert worker.is_alive()

    allow_commit.set()
    creator.join(timeout=5)
    worker.join(timeout=5)

    assert not creator.is_alive()
    assert not worker.is_alive()
    assert created[0]["result"]["current"] is None
    assert claimed[0] is not None
    assert claimed[0]["result"] == created[0]["result"]


@pytest.mark.parametrize(
    ("outer_phase", "child_phase"),
    [
        ("child_prepared", "prepared"),
        ("child_submitted", "submitted"),
        ("child_remote_succeeded", "remote_succeeded"),
        ("unknown", "unknown"),
    ],
)
def test_request_retry_preserves_active_child_contract(
    tmp_path: Path,
    outer_phase: str,
    child_phase: str,
) -> None:
    cfg = settings(tmp_path)
    db = Database(cfg.db_path)
    host = RestartablePruneHost(db)
    service = OpsService(cfg, db, CompatibleExecutor(), host_control=host)
    job = service.queue_snapshot_prune(
        106,
        "oldest",
        "retry-active-child-0001",
    )
    name = "hubinet-ops-106-manual-20260730T120000Z"
    state = dict(job["result"])
    state.update(
        {
            "phase": outer_phase,
            "current": {
                "snapshot_name": name,
                "request_id": service._snapshot_prune_child_request_id(job, name),
                "phase": child_phase,
            },
        }
    )
    db.update_job(job["id"], status="running", result=state)
    before = db.get_job(job["id"])

    retried = service.queue_snapshot_prune(
        106,
        "oldest",
        "retry-active-child-0001",
    )

    assert retried == before
    assert db.get_job(job["id"]) == before
    assert host.delete_submissions == []


def test_retry_before_execution_and_after_progress_never_resets_state(
    tmp_path: Path,
) -> None:
    cfg = settings(tmp_path)
    db = Database(cfg.db_path)
    host = RestartablePruneHost(db)
    service = OpsService(cfg, db, CompatibleExecutor(), host_control=host)
    job = _queue_all(service, "retry-progress-prune-0001")

    assert _queue_all(service, "retry-progress-prune-0001") == job
    state = dict(job["result"])
    state.update(
        {
            "deleted": ["hubinet-ops-106-manual-20260730T120000Z"],
            "deleted_count": 101,
            "deleted_history_truncated": True,
            "phase": "selecting",
        }
    )
    db.update_job(job["id"], status="running", progress=73, result=state)
    before = db.get_job(job["id"])

    assert _queue_all(service, "retry-progress-prune-0001") == before
    assert db.get_job(job["id"]) == before


@pytest.mark.parametrize(
    "legacy_result",
    [
        None,
        {
            "deleted": ["hubinet-ops-106-manual-20260730T120000Z"],
            "deleted_count": 1,
            "mode": "oldest",
        },
    ],
)
def test_retry_preserves_pre_durable_manual_prune_job(
    tmp_path: Path,
    legacy_result: dict[str, Any] | None,
) -> None:
    cfg = settings(tmp_path)
    db = Database(cfg.db_path)
    host = RestartablePruneHost(db)
    service = OpsService(cfg, db, CompatibleExecutor(), host_control=host)
    job, _ = db.create_operation_job(
        vmid=106,
        container_name="ct-106",
        operation_type="snapshot_prune",
        request_id="legacy-manual-prune-retry-0001",
        snapshot_name="oldest",
    )
    db.update_job(job["id"], status="success", progress=100, result=legacy_result)
    before = db.get_job(job["id"])

    retried = service.queue_snapshot_prune(
        106,
        "oldest",
        "legacy-manual-prune-retry-0001",
    )

    assert retried == before
    assert db.get_job(job["id"]) == before
    assert host.delete_submissions == []


def test_retry_preserves_v1_manual_prune_state_without_migration(
    tmp_path: Path,
) -> None:
    cfg = settings(tmp_path)
    db = Database(cfg.db_path)
    host = RestartablePruneHost(db)
    service = OpsService(cfg, db, CompatibleExecutor(), host_control=host)
    job = _queue_all(service, "legacy-v1-manual-retry-0001")
    legacy_state = dict(job["result"])
    legacy_state.pop("deleted_count")
    legacy_state.pop("deleted_history_truncated")
    legacy_state["prune_version"] = 1
    db.update_job(job["id"], status="running", progress=37, result=legacy_state)
    before = db.get_job(job["id"])

    retried = _queue_all(service, "legacy-v1-manual-retry-0001")

    assert retried == before
    assert db.get_job(job["id"]) == before


def test_retry_completed_delete_oldest_does_not_delete_next_snapshot(
    tmp_path: Path,
) -> None:
    cfg = settings(tmp_path)
    db = Database(cfg.db_path)
    host = RestartablePruneHost(db)
    host.snapshots = _managed_snapshots(106, 3)
    service = OpsService(cfg, db, CompatibleExecutor(), host_control=host)
    job = service.queue_snapshot_prune(
        106,
        "oldest",
        "retry-completed-oldest-0001",
    )
    running = db.next_queued_job()
    assert running is not None
    service._run_job(running)
    completed = db.get_job(job["id"])
    submissions = list(host.delete_submissions)

    retried = service.queue_snapshot_prune(
        106,
        "oldest",
        "retry-completed-oldest-0001",
    )

    assert retried == completed
    assert retried["result"]["phase"] == "completed"
    assert retried["result"]["deleted_count"] == 1
    assert host.delete_submissions == submissions
    assert len(host.snapshots) == 2


@pytest.mark.parametrize(
    ("mode", "retention_target", "source_job_id"),
    [
        ("all_unprotected", None, None),
        ("retention", 2, "source-job-000000000000000000000001"),
    ],
)
def test_snapshot_prune_retry_rejects_immutable_contract_mismatch(
    tmp_path: Path,
    mode: str,
    retention_target: int | None,
    source_job_id: str | None,
) -> None:
    cfg = settings(tmp_path)
    db = Database(cfg.db_path)
    job, _ = db.create_snapshot_prune_job(
        vmid=106,
        container_name="ct-106",
        request_id="prune-contract-mismatch-0001",
        mode=mode,
        retention_target=retention_target,
        source_job_id=source_job_id,
    )
    mismatches = (
        [
            {
                "mode": "oldest",
                "retention_target": None,
                "source_job_id": None,
            }
        ]
        if mode != "retention"
        else [
            {
                "mode": "retention",
                "retention_target": 3,
                "source_job_id": source_job_id,
            },
            {
                "mode": "retention",
                "retention_target": retention_target,
                "source_job_id": "source-job-000000000000000000000002",
            },
        ]
    )
    for mismatch in mismatches:
        with pytest.raises(ValueError, match="snapshot prune contract"):
            db.create_snapshot_prune_job(
                vmid=106,
                container_name="ct-106",
                request_id="prune-contract-mismatch-0001",
                **mismatch,
            )
    persisted = db.get_job(job["id"])
    assert persisted == job

    changed_version = dict(job["result"])
    changed_version["prune_version"] = 0
    db.update_job(job["id"], result=changed_version)
    with pytest.raises(ValueError, match="snapshot prune contract"):
        db.create_snapshot_prune_job(
            vmid=106,
            container_name="ct-106",
            request_id="prune-contract-mismatch-0001",
            mode=mode,
            retention_target=retention_target,
            source_job_id=source_job_id,
        )


def test_retention_handoff_retry_reuses_followup_without_mutation(
    tmp_path: Path,
) -> None:
    cfg = settings(tmp_path)
    db = Database(cfg.db_path)
    source, _ = db.create_operation_job(
        vmid=106,
        container_name="ct-106",
        operation_type="snapshot_create",
        request_id="handoff-source-idempotent-0001",
        snapshot_name="hubinet-ops-106-manual-20260730T120000Z",
    )
    request_id = f"retention-{source['id']}"
    terminal, prune, _event, created = db.terminalize_job_with_snapshot_prune(
        source["id"],
        source_status="success",
        terminal_result="success",
        error=None,
        prune_request_id=request_id,
        retention_target=2,
    )
    state = dict(prune["result"])
    state.update(
        {
            "phase": "child_submitted",
            "current": {
                "snapshot_name": "hubinet-ops-106-manual-20260729T120000Z",
                "request_id": "prune-child-request-preserved-0001",
                "phase": "submitted",
            },
            "deleted": ["hubinet-ops-106-manual-20260728T120000Z"],
            "deleted_count": 7,
            "deleted_history_truncated": True,
        }
    )
    db.update_job(prune["id"], status="running", progress=41, result=state)
    before = db.get_job(prune["id"])

    terminal_retry, prune_retry, _event_retry, created_retry = (
        db.terminalize_job_with_snapshot_prune(
            source["id"],
            source_status="success",
            terminal_result="success",
            error=None,
            prune_request_id=request_id,
            retention_target=2,
        )
    )

    assert created is True
    assert created_retry is False
    assert terminal["status"] == terminal_retry["status"] == "success"
    assert prune_retry == before
    assert db.get_job(prune["id"]) == before
    assert sum(
        job["operation_type"] == "snapshot_prune"
        for job in db.list_jobs()
    ) == 1


def test_retention_handoff_retry_preserves_v1_followup_without_mutation(
    tmp_path: Path,
) -> None:
    cfg = settings(tmp_path)
    db = Database(cfg.db_path)
    source, _ = db.create_operation_job(
        vmid=106,
        container_name="ct-106",
        operation_type="snapshot_create",
        request_id="legacy-handoff-source-0001",
        snapshot_name="hubinet-ops-106-manual-20260730T120000Z",
    )
    request_id = f"retention-{source['id']}"
    _terminal, prune, _event, _created = db.terminalize_job_with_snapshot_prune(
        source["id"],
        source_status="success",
        terminal_result="success",
        error=None,
        prune_request_id=request_id,
        retention_target=2,
    )
    legacy_state = dict(prune["result"])
    legacy_state.pop("deleted_count")
    legacy_state.pop("deleted_history_truncated")
    legacy_state["prune_version"] = 1
    db.update_job(prune["id"], status="running", progress=43, result=legacy_state)
    before = db.get_job(prune["id"])

    _source_retry, prune_retry, _event_retry, created_retry = (
        db.terminalize_job_with_snapshot_prune(
            source["id"],
            source_status="success",
            terminal_result="success",
            error=None,
            prune_request_id=request_id,
            retention_target=2,
        )
    )

    assert created_retry is False
    assert prune_retry == before
    assert db.get_job(prune["id"]) == before


def test_all_unprotected_prunes_150_with_bounded_audit_history(
    tmp_path: Path,
) -> None:
    cfg = settings(tmp_path)
    db = Database(cfg.db_path)
    protected = _owned(
        106,
        "20260720T120000Z",
        created_at="2026-07-20T12:00:00+00:00",
    )
    foreign = {
        "name": "foreign-operator-snapshot",
        "created_at": "2026-07-19T12:00:00+00:00",
        "owned_by_hubinet_ops": False,
        "delete_eligible": True,
    }
    rollback, _ = db.create_operation_job(
        vmid=106,
        container_name="ct-106",
        operation_type="snapshot_delete",
        request_id="large-prune-rollback-source-0001",
        snapshot_name=protected["name"],
    )
    db.update_job(rollback["id"], status="failed")
    host = RestartablePruneHost(db)
    host.snapshots = _managed_snapshots(106, 150) + [protected, foreign]
    service = OpsService(cfg, db, CompatibleExecutor(), host_control=host)
    job = _queue_all(service, "large-all-unprotected-0001")

    running = db.next_queued_job()
    assert running is not None
    service._run_job(running)

    terminal = db.get_job(job["id"])
    assert terminal["status"] == "success"
    assert terminal["result"]["deleted_count"] == 150
    assert len(host.delete_submissions) == 150
    assert len(terminal["result"]["deleted"]) == SNAPSHOT_PRUNE_DELETED_HISTORY_LIMIT
    assert terminal["result"]["deleted_history_truncated"] is True
    assert len(json.dumps(terminal["result"])) < 8_000
    assert {item["name"] for item in host.snapshots} == {
        protected["name"],
        foreign["name"],
    }
    canonical = service.get_state(106)
    assert canonical["snapshot_count"] == 1
    assert [item["name"] for item in canonical["managed_snapshots"]] == [
        protected["name"]
    ]


def test_restart_and_retry_after_deletion_100_continue_without_duplicate(
    tmp_path: Path,
) -> None:
    cfg = settings(tmp_path)
    db = Database(cfg.db_path)
    host = RestartablePruneHost(db)
    snapshots = _managed_snapshots(106, 150)
    host.snapshots = snapshots
    host.interrupt_name = snapshots[-101]["name"]
    service = OpsService(cfg, db, CompatibleExecutor(), host_control=host)
    job = _queue_all(service, "large-restart-after-100-0001")
    running = db.next_queued_job()
    assert running is not None

    with pytest.raises(SimulatedBackendRestart):
        service._run_job(running)

    interrupted = db.get_job(job["id"])
    assert interrupted["result"]["deleted_count"] == 100
    current = dict(interrupted["result"]["current"])
    before_retry = json.loads(json.dumps(interrupted))
    assert _queue_all(service, "large-restart-after-100-0001") == before_retry
    assert db.get_job(job["id"]) == before_retry
    assert current["request_id"] == service._snapshot_prune_child_request_id(
        job,
        current["snapshot_name"],
    )

    host.complete_running_on_wait = True
    host.interrupt_name = None
    OpsService(
        cfg,
        db,
        CompatibleExecutor(),
        host_control=host,
    )._reconcile_startup_jobs()

    terminal = db.get_job(job["id"])
    assert terminal["status"] == "success"
    assert terminal["result"]["deleted_count"] == 150
    assert len(host.delete_submissions) == 150
    assert len({request_id for request_id, _name in host.delete_submissions}) == 150
    assert host.snapshots == []


def test_retention_prunes_more_than_100_snapshots(
    tmp_path: Path,
) -> None:
    cfg = settings(tmp_path)
    cfg.raw["resources"][106]["snapshot_retention_count"] = 5
    cfg.raw["resources"][106].pop("snapshot_retention", None)
    db = Database(cfg.db_path)
    host = RestartablePruneHost(db)
    host.snapshots = _managed_snapshots(106, 160)
    service = OpsService(cfg, db, CompatibleExecutor(), host_control=host)
    source, _ = db.create_operation_job(
        vmid=106,
        container_name="ct-106",
        operation_type="snapshot_create",
        request_id="large-retention-source-0001",
        snapshot_name=host.snapshots[0]["name"],
    )

    prune = service._terminal_with_snapshot_retention(
        source,
        job_status="success",
        result="success",
        error=None,
    )

    terminal = db.get_job(prune["id"])
    assert db.get_job(source["id"])["status"] == "success"
    assert terminal["status"] == "success"
    assert terminal["result"]["mode"] == "retention"
    assert terminal["result"]["deleted_count"] == 155
    assert terminal["result"]["deleted_history_truncated"] is True
    assert len(host.snapshots) == 5
