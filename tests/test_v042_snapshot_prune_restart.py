from __future__ import annotations

from pathlib import Path
import threading
from typing import Any

import pytest

from app.database import Database
from app.host_control import HostControlError
from app.service import OpsService
from app.stabilization import Stabilizer
from tests.test_lifecycle_snapshots import (
    CompatibleExecutor,
    FakeHostControl,
    settings,
)
from tests.test_v042_snapshot_retention import _owned, _record_snapshot_sources
from tests.test_v042_snapshot_consistency import (
    SnapshotUpdateExecutor,
    _approved_update,
)
from tests.test_service import FakeClock


class SimulatedBackendRestart(BaseException):
    pass


def expected_identity(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": 1,
        "vmid": int(str(snapshot["name"]).split("-")[2]),
        "snapshot_name": snapshot["name"],
        "kind": snapshot["kind"],
        "host_source_job_id": snapshot["source_job_id"],
        "pve_snaptime": snapshot["pve_snaptime"],
    }


class RestartablePruneHost(FakeHostControl):
    def __init__(self, db: Database) -> None:
        super().__init__()
        self.db = db
        self.interrupt_name: str | None = None
        self.interrupt_after_remote_success = False
        self.complete_running_on_wait = False
        self.wait_error: HostControlError | None = None
        self.execute_error: HostControlError | None = None
        self.delete_submissions: list[tuple[str, str]] = []

    def execute(
        self,
        operation_type: str,
        vmid: int,
        request_id: str,
        *,
        snapshot_name: str | None = None,
        snapshot_kind: str | None = None,
        expected_source_job_id: str | None = None,
        expected_pve_snaptime: int | None = None,
        release_fingerprint: str | None = None,
    ) -> dict[str, Any]:
        if operation_type != "snapshot_delete":
            return super().execute(
                operation_type,
                vmid,
                request_id,
                snapshot_name=snapshot_name,
                snapshot_kind=snapshot_kind,
                expected_source_job_id=expected_source_job_id,
                expected_pve_snaptime=expected_pve_snaptime,
                release_fingerprint=release_fingerprint,
            )
        assert snapshot_name is not None
        active = self.db.active_jobs()
        assert len(active) == 1
        assert active[0]["operation_type"] == "snapshot_prune"
        current = active[0]["result"]["current"]
        assert current["snapshot_name"] == snapshot_name
        assert current["request_id"] == request_id
        assert current["phase"] == "submitted"
        assert current["expected_snapshot_identity"] == {
            "version": 1,
            "vmid": vmid,
            "snapshot_name": snapshot_name,
            "kind": snapshot_kind,
            "host_source_job_id": expected_source_job_id,
            "pve_snaptime": expected_pve_snaptime,
        }
        self.calls.append(
            (operation_type, vmid, request_id, snapshot_name, release_fingerprint)
        )
        self.delete_submissions.append((request_id, snapshot_name))
        if self.execute_error is not None:
            raise self.execute_error
        if snapshot_name == self.interrupt_name:
            status = "succeeded" if self.interrupt_after_remote_success else "running"
            self.existing_jobs[request_id] = {
                "status": status,
                "result": {},
            }
            if self.interrupt_after_remote_success:
                self.snapshots = [
                    item for item in self.snapshots
                    if item["name"] != snapshot_name
                ]
            raise SimulatedBackendRestart()
        self.snapshots = [
            item for item in self.snapshots
            if item["name"] != snapshot_name
        ]
        self.existing_jobs[request_id] = {"status": "succeeded", "result": {}}
        return {}

    def wait_existing_job(
        self,
        operation_type: str,
        vmid: int,
        request_id: str,
        *,
        snapshot_name: str | None = None,
        snapshot_kind: str | None = None,
        expected_source_job_id: str | None = None,
        expected_pve_snaptime: int | None = None,
        release_fingerprint: str | None = None,
    ) -> dict[str, Any]:
        self.reattach_calls.append(
            (
                operation_type,
                vmid,
                request_id,
                snapshot_name,
                release_fingerprint,
            )
        )
        if self.wait_error is not None:
            raise self.wait_error
        existing = self.existing_jobs.get(request_id)
        if existing is None:
            raise HostControlError("remote job missing", status="not_found")
        if (
            str(existing.get("status")) == "running"
            and self.complete_running_on_wait
        ):
            self.snapshots = [
                item for item in self.snapshots
                if item["name"] != snapshot_name
            ]
            existing["status"] = "succeeded"
        if str(existing.get("status")) != "succeeded":
            raise HostControlError(
                str(existing.get("error") or "child delete failed"),
                status=str(existing.get("status") or "failed"),
            )
        return dict(existing.get("result") or {})


def _service(
    tmp_path: Path,
    *,
    mode: str = "all_unprotected",
) -> tuple[OpsService, Database, RestartablePruneHost, dict[str, Any]]:
    cfg = settings(tmp_path)
    db = Database(cfg.db_path)
    host = RestartablePruneHost(db)
    service = OpsService(cfg, db, CompatibleExecutor(), host_control=host)
    job = service.queue_snapshot_prune(
        106,
        mode,
        f"restart-prune-{mode}-0001",
        confirmation=(
            "DELETE_ALL_UNPROTECTED"
            if mode == "all_unprotected"
            else None
        ),
    )
    return service, db, host, job


def test_snapshot_pruning_event_stage_is_persisted_and_unknown_fails_closed(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "ops.db")
    job, _ = db.create_operation_job(
        vmid=106,
        container_name="ct-106",
        operation_type="snapshot_prune",
        request_id="snapshot-prune-stage-test-0001",
    )
    event = db.insert_job_event(
        job_id=job["id"],
        vmid=106,
        level="info",
        stage="snapshot_pruning",
        progress=50,
        event_type="snapshot_pruning",
        message="Pruning snapshots",
    )

    assert event["stage"] == "snapshot_pruning"
    assert db.get_job(job["id"])["stage"] == "snapshot_pruning"
    unknown = db.insert_job_event(
        job_id=job["id"],
        vmid=106,
        level="info",
        stage="truly_unknown_stage",
        progress=51,
        event_type="unknown_stage",
        message="Unknown stage",
    )
    assert unknown["stage"] == "idle"
    assert db.get_job(job["id"])["stage"] == "idle"


def _start_and_interrupt(
    service: OpsService,
    db: Database,
    host: RestartablePruneHost,
    snapshot_name: str,
    *,
    after_success: bool = False,
) -> dict[str, Any]:
    host.interrupt_name = snapshot_name
    host.interrupt_after_remote_success = after_success
    running = db.next_queued_job()
    assert running is not None
    with pytest.raises(SimulatedBackendRestart):
        service._run_job(running)
    persisted = db.get_job(str(running["id"]))
    assert persisted["status"] == "running"
    assert persisted["result"]["current"]["snapshot_name"] == snapshot_name
    assert persisted["result"]["current"]["phase"] == "submitted"
    return persisted


def test_restart_reattaches_first_running_child_and_keeps_outer_job_active(
    tmp_path: Path,
) -> None:
    service, db, host, _job = _service(tmp_path)
    target = _owned(
        106,
        "20260727T120000Z",
        created_at="2026-07-27T12:00:00+00:00",
    )
    host.snapshots = [target]
    _record_snapshot_sources(db, host.snapshots)

    persisted = _start_and_interrupt(service, db, host, target["name"])
    child_request_id = persisted["result"]["current"]["request_id"]
    with pytest.raises(ValueError, match="Another destructive"):
        service.queue_snapshot_create(106, "blocked-during-prune-0001")

    host.complete_running_on_wait = True
    host.interrupt_name = None
    restarted = OpsService(
        service.settings,
        db,
        CompatibleExecutor(),
        host_control=host,
    )
    restarted._reconcile_startup_jobs()

    assert host.reattach_calls[0][:4] == (
        "snapshot_delete",
        106,
        child_request_id,
        target["name"],
    )
    assert host.delete_submissions == [(child_request_id, target["name"])]
    terminal = db.get_job(str(persisted["id"]))
    assert terminal["status"] == "success"
    assert terminal["result"]["deleted"] == [target["name"]]
    state = restarted.get_state(106)
    assert state["snapshot_count"] == 0
    assert state["managed_snapshots"] == []


def test_restart_after_remote_success_does_not_submit_duplicate_delete(
    tmp_path: Path,
) -> None:
    service, db, host, _job = _service(tmp_path)
    target = _owned(
        106,
        "20260727T120000Z",
        created_at="2026-07-27T12:00:00+00:00",
    )
    host.snapshots = [target]
    _record_snapshot_sources(db, host.snapshots)
    persisted = _start_and_interrupt(
        service,
        db,
        host,
        target["name"],
        after_success=True,
    )
    original_submissions = list(host.delete_submissions)

    host.interrupt_name = None
    OpsService(
        service.settings,
        db,
        CompatibleExecutor(),
        host_control=host,
    )._reconcile_startup_jobs()

    assert host.delete_submissions == original_submissions
    assert db.get_job(str(persisted["id"]))["status"] == "success"


def test_restart_before_submission_resubmits_stable_child_request(
    tmp_path: Path,
) -> None:
    service, db, host, job = _service(tmp_path, mode="oldest")
    target = _owned(
        106,
        "20260727T120000Z",
        created_at="2026-07-27T12:00:00+00:00",
    )
    host.snapshots = [target]
    _record_snapshot_sources(db, host.snapshots)
    request_id = service._snapshot_prune_child_request_id(job, target["name"])
    state = dict(job["result"])
    state.update(
        {
            "phase": "child_prepared",
                "current": {
                    "snapshot_name": target["name"],
                    "expected_snapshot_identity": expected_identity(target),
                "request_id": request_id,
                "phase": "prepared",
            },
        }
    )
    db.update_job(job["id"], status="running", result=state)

    service._reconcile_startup_jobs()

    assert host.reattach_calls[0][2:4] == (request_id, target["name"])
    assert host.delete_submissions == [(request_id, target["name"])]
    assert db.get_job(job["id"])["status"] == "success"


def test_restart_between_bulk_deletions_resumes_from_durable_deleted_list(
    tmp_path: Path,
) -> None:
    service, db, host, job = _service(tmp_path)
    newest = _owned(
        106,
        "20260729T120000Z",
        created_at="2026-07-29T12:00:00+00:00",
    )
    middle = _owned(
        106,
        "20260728T120000Z",
        created_at="2026-07-28T12:00:00+00:00",
    )
    oldest = _owned(
        106,
        "20260727T120000Z",
        created_at="2026-07-27T12:00:00+00:00",
    )
    host.snapshots = [newest, middle, oldest]
    _record_snapshot_sources(db, host.snapshots)
    host.interrupt_name = middle["name"]
    running = db.next_queued_job()
    assert running is not None

    with pytest.raises(SimulatedBackendRestart):
        service._run_job(running)

    persisted = db.get_job(job["id"])
    assert persisted["result"]["deleted"] == [oldest["name"]]
    host.complete_running_on_wait = True
    host.interrupt_name = None
    service._reconcile_startup_jobs()

    terminal = db.get_job(job["id"])
    assert terminal["status"] == "success"
    assert terminal["result"]["deleted"] == [
        oldest["name"],
        middle["name"],
        newest["name"],
    ]
    assert len({request for request, _name in host.delete_submissions}) == 3


def test_restart_after_last_delete_before_final_refresh_finishes_canonical_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, db, host, job = _service(tmp_path, mode="oldest")
    target = _owned(
        106,
        "20260727T120000Z",
        created_at="2026-07-27T12:00:00+00:00",
    )
    host.snapshots = [target]
    _record_snapshot_sources(db, host.snapshots)
    original_persist = service._persist_snapshot_prune_state
    interrupted = False

    def persist_then_interrupt(
        current_job: dict[str, Any],
        state: dict[str, Any],
    ) -> None:
        nonlocal interrupted
        original_persist(current_job, state)
        if (
            not interrupted
            and state.get("current") is None
            and state.get("deleted") == [target["name"]]
        ):
            interrupted = True
            raise SimulatedBackendRestart()

    monkeypatch.setattr(
        service,
        "_persist_snapshot_prune_state",
        persist_then_interrupt,
    )
    running = db.next_queued_job()
    assert running is not None
    with pytest.raises(SimulatedBackendRestart):
        service._run_job(running)

    restarted = OpsService(
        service.settings,
        db,
        CompatibleExecutor(),
        host_control=host,
    )
    restarted._reconcile_startup_jobs()

    assert db.get_job(job["id"])["status"] == "success"
    assert restarted.get_state(106)["snapshot_count"] == 0


def test_failed_child_delete_fails_prune_but_preserves_durable_source(
    tmp_path: Path,
) -> None:
    service, db, host, job = _service(tmp_path, mode="oldest")
    target = _owned(
        106,
        "20260727T120000Z",
        created_at="2026-07-27T12:00:00+00:00",
    )
    host.snapshots = [target]
    _record_snapshot_sources(db, host.snapshots)
    request_id = service._snapshot_prune_child_request_id(job, target["name"])
    state = dict(job["result"])
    state.update(
        {
            "phase": "child_submitted",
                "current": {
                    "snapshot_name": target["name"],
                    "expected_snapshot_identity": expected_identity(target),
                "request_id": request_id,
                "phase": "submitted",
            },
        }
    )
    db.update_job(job["id"], status="running", result=state)
    host.existing_jobs[request_id] = {
        "status": "failed",
        "error": "PVE delete failed",
    }

    service._reconcile_startup_jobs()

    assert db.get_job(job["id"])["status"] == "failed"
    assert {item["name"] for item in host.snapshots} == {target["name"]}


@pytest.mark.parametrize("http_status", [400, 409])
def test_direct_nontransient_child_rejection_fails_prune_and_releases_lock(
    tmp_path: Path,
    http_status: int,
) -> None:
    service, db, host, job = _service(tmp_path, mode="oldest")
    target = _owned(
        106,
        "20260727T120000Z",
        created_at="2026-07-27T12:00:00+00:00",
    )
    host.snapshots = [target]
    _record_snapshot_sources(db, host.snapshots)
    host.execute_error = HostControlError(
        "child request rejected",
        status=None,
        http_status=http_status,
        code="request_rejected",
    )
    running = db.next_queued_job()
    assert running is not None

    service._run_job(running)

    terminal = db.get_job(job["id"])
    assert terminal["status"] == "failed"
    assert terminal["result"]["current"]["snapshot_name"] == target["name"]
    assert "child request rejected" in str(terminal["error"])
    assert db.active_job_count() == 0
    assert {item["name"] for item in host.snapshots} == {target["name"]}


def test_direct_http_500_child_error_stays_active_as_unknown(
    tmp_path: Path,
) -> None:
    service, db, host, job = _service(tmp_path, mode="oldest")
    target = _owned(
        106,
        "20260727T120000Z",
        created_at="2026-07-27T12:00:00+00:00",
    )
    host.snapshots = [target]
    _record_snapshot_sources(db, host.snapshots)
    host.execute_error = HostControlError(
        "hostd internal error",
        status=None,
        http_status=500,
    )
    running = db.next_queued_job()
    assert running is not None

    service._run_job(running)

    persisted = db.get_job(job["id"])
    assert persisted["status"] == "running"
    assert persisted["stage"] == "snapshot_pruning"
    assert persisted["result"]["phase"] == "unknown"
    assert db.active_job_count() == 1
    assert {item["name"] for item in host.snapshots} == {target["name"]}


@pytest.mark.parametrize("target_present", [True, False])
def test_missing_remote_job_is_resubmitted_only_when_target_still_exists(
    tmp_path: Path,
    target_present: bool,
) -> None:
    service, db, host, job = _service(tmp_path, mode="oldest")
    target = _owned(
        106,
        "20260727T120000Z",
        created_at="2026-07-27T12:00:00+00:00",
    )
    host.snapshots = [target] if target_present else []
    _record_snapshot_sources(db, host.snapshots)
    request_id = service._snapshot_prune_child_request_id(job, target["name"])
    state = dict(job["result"])
    state.update(
        {
            "phase": "child_submitted",
                "current": {
                    "snapshot_name": target["name"],
                    "expected_snapshot_identity": expected_identity(target),
                "request_id": request_id,
                "phase": "submitted",
            },
        }
    )
    db.update_job(job["id"], status="running", result=state)

    service._reconcile_startup_jobs()

    assert host.delete_submissions == (
        [(request_id, target["name"])] if target_present else []
    )
    assert db.get_job(job["id"])["status"] == "success"


def test_ambiguous_remote_outcome_stays_active_and_fail_closed(
    tmp_path: Path,
) -> None:
    service, db, host, job = _service(tmp_path, mode="oldest")
    target = _owned(
        106,
        "20260727T120000Z",
        created_at="2026-07-27T12:00:00+00:00",
    )
    host.snapshots = [target]
    _record_snapshot_sources(db, host.snapshots)
    request_id = service._snapshot_prune_child_request_id(job, target["name"])
    state = dict(job["result"])
    state.update(
        {
            "phase": "child_submitted",
                "current": {
                    "snapshot_name": target["name"],
                    "expected_snapshot_identity": expected_identity(target),
                "request_id": request_id,
                "phase": "submitted",
            },
        }
    )
    db.update_job(job["id"], status="running", result=state)
    host.wait_error = HostControlError(
        "hostd temporarily unavailable",
        status="unavailable",
    )

    service._reconcile_startup_jobs()

    persisted = db.get_job(job["id"])
    assert persisted["status"] == "running"
    assert persisted["stage"] == "snapshot_pruning"
    assert persisted["result"]["phase"] == "unknown"
    assert db.active_job_count() == 1
    with pytest.raises(ValueError, match="Another destructive"):
        service.queue_snapshot_create(106, "blocked-unknown-prune-0001")
    outcome_unknown = next(
        event
        for event in db.list_job_events(job["id"])
        if event["event_type"] == "snapshot_pruning_outcome_unknown"
    )
    assert outcome_unknown["stage"] == "snapshot_pruning"


def test_retention_zero_terminalizes_source_without_followup(
    tmp_path: Path,
) -> None:
    cfg = settings(tmp_path)
    cfg.raw["resources"][106]["snapshot_retention_count"] = 0
    cfg.raw["resources"][106].pop("snapshot_retention", None)
    db = Database(cfg.db_path)
    service = OpsService(
        cfg,
        db,
        CompatibleExecutor(),
        host_control=RestartablePruneHost(db),
    )
    source, _ = db.create_operation_job(
        vmid=106,
        container_name="ct-106",
        operation_type="snapshot_create",
        request_id="retention-zero-source-0001",
        snapshot_name="hubinet-ops-106-manual-20260729T120000Z",
    )

    service._terminal_with_snapshot_retention(
        source,
        job_status="success",
        result="success",
        error=None,
    )

    assert db.get_job(source["id"])["status"] == "success"
    assert not any(
        job["operation_type"] == "snapshot_prune"
        for job in db.list_jobs()
    )


def test_manual_snapshot_creation_hands_off_to_durable_retention_job(
    tmp_path: Path,
) -> None:
    cfg = settings(tmp_path)
    cfg.raw["resources"][106]["snapshot_retention_count"] = 1
    cfg.raw["resources"][106].pop("snapshot_retention", None)
    db = Database(cfg.db_path)
    host = RestartablePruneHost(db)
    host.snapshots = [
        _owned(
            106,
            "20260728T120000Z",
            created_at="2026-07-28T12:00:00+00:00",
        ),
        _owned(
            106,
            "20260727T120000Z",
            created_at="2026-07-27T12:00:00+00:00",
        ),
    ]
    _record_snapshot_sources(db, host.snapshots)
    service = OpsService(cfg, db, CompatibleExecutor(), host_control=host)
    source = service.queue_snapshot_create(106, "create-retention-handoff-0001")
    running = db.next_queued_job()
    assert running is not None

    service._run_job(running)

    assert db.get_job(source["id"])["status"] == "success"
    prune = next(
        item for item in db.list_jobs()
        if item["operation_type"] == "snapshot_prune"
    )
    assert prune["status"] == "success"
    assert prune["result"]["deleted_count"] == 2
    assert len(host.snapshots) == 1


def test_successful_update_hands_off_to_hostd_retention_job(
    tmp_path: Path,
) -> None:
    executor = SnapshotUpdateExecutor()
    executor.snapshots = [
        _owned(
            106,
            "20260728T120000Z",
            created_at="2026-07-28T12:00:00+00:00",
        ),
        _owned(
            106,
            "20260727T120000Z",
            created_at="2026-07-27T12:00:00+00:00",
        ),
    ]
    service, db, source = _approved_update(tmp_path, executor)
    host = RestartablePruneHost(db)
    host.snapshots = executor.snapshots
    _record_snapshot_sources(db, host.snapshots)
    service.host_control = host
    service.settings.raw["containers"][106]["snapshot_retention_count"] = 1
    service.settings.raw["containers"][106].pop("snapshot_retention", None)
    clock = FakeClock()
    service.stabilizer = Stabilizer(
        executor,
        threading.Event(),
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    service._run_job(db.get_job(source["id"]))

    assert db.get_job(source["id"])["status"] == "success"
    prune = next(
        item for item in db.list_jobs()
        if item["operation_type"] == "snapshot_prune"
    )
    assert prune["status"] == "success"
    assert prune["result"]["deleted_count"] == 2
    assert len(host.snapshots) == 1


def test_prune_failure_does_not_rewrite_successful_create_source(
    tmp_path: Path,
) -> None:
    class FailingDeleteHost(RestartablePruneHost):
        def execute(
            self,
            operation_type: str,
            vmid: int,
            request_id: str,
            *,
            snapshot_name: str | None = None,
            snapshot_kind: str | None = None,
            expected_source_job_id: str | None = None,
            expected_pve_snaptime: int | None = None,
            release_fingerprint: str | None = None,
        ) -> dict[str, Any]:
            if operation_type == "snapshot_delete":
                active = self.db.active_jobs()
                assert len(active) == 1
                assert active[0]["operation_type"] == "snapshot_prune"
                raise HostControlError("PVE delete failed", status="failed")
            return super().execute(
                operation_type,
                vmid,
                request_id,
                snapshot_name=snapshot_name,
                snapshot_kind=snapshot_kind,
                expected_source_job_id=expected_source_job_id,
                expected_pve_snaptime=expected_pve_snaptime,
                release_fingerprint=release_fingerprint,
            )

    cfg = settings(tmp_path)
    cfg.raw["resources"][106]["snapshot_retention_count"] = 1
    cfg.raw["resources"][106].pop("snapshot_retention", None)
    db = Database(cfg.db_path)
    host = FailingDeleteHost(db)
    host.snapshots = [
        _owned(
            106,
            "20260728T120000Z",
            created_at="2026-07-28T12:00:00+00:00",
        )
    ]
    _record_snapshot_sources(db, host.snapshots)
    service = OpsService(cfg, db, CompatibleExecutor(), host_control=host)
    source = service.queue_snapshot_create(106, "create-prune-failure-0001")
    running = db.next_queued_job()
    assert running is not None

    service._run_job(running)

    assert db.get_job(source["id"])["status"] == "success"
    prune = next(
        item for item in db.list_jobs()
        if item["operation_type"] == "snapshot_prune"
    )
    assert prune["status"] == "failed"
