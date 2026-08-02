from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.database import Database
from app.executor import ExecutorError
from app.host_control import HostControlError
from app.service import OpsService
from tests.test_lifecycle_snapshots import (
    FakeHostControl,
    CompatibleExecutor,
    run_queued,
    settings as lifecycle_settings,
)
from tests.test_service import WorkflowExecutor, docker_state, settings as update_settings


class SnapshotUpdateExecutor(WorkflowExecutor):
    def run(self, action: str, vmid: int, argument=None, timeout=None, on_event=None):
        if action == "scan":
            self.actions.append(action)
            return {
                "ok": True,
                "data": {
                    "pending_count": 3,
                    "packages": [{"name": "systemd"}],
                    "fingerprint": self.preflight_fingerprint,
                },
            }
        return super().run(action, vmid, argument, timeout, on_event)


class PreUpdateHost(FakeHostControl):
    def __init__(self, trace: list[str] | None = None) -> None:
        super().__init__("running")
        self.trace = trace
        self.result_mutation: dict[str, Any] = {}
        self.listing_mutation: dict[str, Any] = {}
        self.hide_listing = False
        self.duplicate_listing = False

    def execute(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        result = super().execute(*args, **kwargs)
        if args[0] == "snapshot_create":
            if self.trace is not None:
                self.trace.append("hostd-snapshot-create")
            result.update(self.result_mutation)
        return result

    def list_snapshots(self, vmid: int) -> list[dict[str, Any]]:
        if self.trace is not None:
            self.trace.append("list-snapshots")
        if self.hide_listing:
            return []
        snapshots = super().list_snapshots(vmid)
        for snapshot in snapshots:
            snapshot.update(self.listing_mutation)
        if self.duplicate_listing and snapshots:
            snapshots.append(dict(snapshots[0]))
        return snapshots


class HealthyStabilizer:
    def wait(self, **_kwargs: Any) -> dict[str, Any]:
        return docker_state(3)


def _approved_update(
    tmp_path: Path,
    executor: SnapshotUpdateExecutor,
    host: PreUpdateHost | None = None,
) -> tuple[OpsService, Database, dict[str, Any]]:
    cfg = update_settings(tmp_path)
    cfg.raw["containers"][106]["pre_update_snapshot"] = True
    cfg.raw["containers"][106]["automatic_rollback"] = False
    db = Database(cfg.db_path)
    service = OpsService(
        cfg,
        db,
        executor,
        host_control=host or PreUpdateHost(),
    )
    scanned = service.scan_container(106)
    approved = service.approve(scanned["plan"]["id"])
    return service, db, approved["job"]


def test_pre_update_snapshot_is_refreshed_and_visible_before_apt_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []

    class OrderedExecutor(SnapshotUpdateExecutor):
        def run(self, action: str, vmid: int, argument=None, timeout=None, on_event=None):
            if action == "update":
                order.append(action)
            return super().run(action, vmid, argument, timeout, on_event)

    executor = OrderedExecutor(
        [docker_state(3), docker_state(3), docker_state(3)]
    )
    host = PreUpdateHost(order)
    service, db, job = _approved_update(tmp_path, executor, host)
    record_proof = db.record_pre_update_snapshot_proof

    def traced_record_proof(*args: Any, **kwargs: Any) -> dict[str, Any]:
        order.append("record-proof")
        return record_proof(*args, **kwargs)

    monkeypatch.setattr(db, "record_pre_update_snapshot_proof", traced_record_proof)

    service._run_job(db.get_job(job["id"]))

    assert order == [
        "hostd-snapshot-create",
        "list-snapshots",
        "record-proof",
        "list-snapshots",
        "update",
    ]
    snapshot_calls = [call for call in host.calls if call[0] == "snapshot_create"]
    assert len(snapshot_calls) == 1
    assert snapshot_calls[0][2] == f"pre-update-snapshot-{job['id']}"
    state = service.get_state(106)
    assert state["snapshot_count"] == 1
    assert state["latest_snapshot_name"] == db.get_job(job["id"])["snapshot_name"]
    assert state["latest_snapshot_kind"] == "pre-update"
    assert state["snapshot_state_stale"] is False
    proof = db.get_job(job["id"])["result"]["snapshot_proof"]
    assert proof == {
        "version": 3,
        "vmid": 106,
        "snapshot_name": db.get_job(job["id"])["snapshot_name"],
        "kind": "pre-update",
        "host_source_job_id": host.snapshots[0]["source_job_id"],
        "pve_snaptime": host.snapshots[0]["pve_snaptime"],
    }
    events = db.list_job_events(job["id"])
    mutation_index = next(
        index
        for index, event in enumerate(events)
        if event["event_type"] == "snapshot_mutation_succeeded"
    )
    created_index = next(
        index
        for index, event in enumerate(events)
        if event["event_type"] == "snapshot_created"
    )
    assert mutation_index < created_index


def test_host_control_unavailable_blocks_before_snapshot_mutation(
    tmp_path: Path,
) -> None:
    executor = SnapshotUpdateExecutor()
    service, db, job = _approved_update(tmp_path, executor)
    service.host_control = None

    service._run_job(db.get_job(job["id"]))

    assert db.get_job(job["id"])["status"] == "blocked"
    assert "snapshot_proof" not in dict(db.get_job(job["id"]).get("result") or {})
    assert "snapshot" not in executor.actions
    assert "update" not in executor.actions
    assert not any(
        event["event_type"] == "snapshot_mutation_succeeded"
        for event in db.list_job_events(job["id"])
    )


def test_host_control_failure_after_submit_keeps_outcome_unknown(
    tmp_path: Path,
) -> None:
    class UnavailableHost(PreUpdateHost):
        def __init__(self) -> None:
            super().__init__()
            self.submissions = 0

        def execute(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            self.submissions += 1
            raise HostControlError("hostd unavailable", status="unavailable")

    executor = SnapshotUpdateExecutor()
    host = UnavailableHost()
    service, db, job = _approved_update(tmp_path, executor, host)

    service._run_job(db.get_job(job["id"]))

    terminal = db.get_job(job["id"])
    assert terminal["status"] in {"queued", "running"}
    assert host.snapshots == []
    assert "snapshot_proof" not in dict(terminal.get("result") or {})
    assert terminal["result"]["pre_update_snapshot_create"]["phase"] == "outcome_unknown"
    assert db.get_plan(str(job["plan_id"]))["status"] == "approved"
    assert "update" not in executor.actions
    restarted = OpsService(service.settings, db, executor, host_control=host)
    restarted._reconcile_startup_jobs()
    restarted._reconcile_startup_jobs()
    assert host.submissions == 1
    assert db.get_job(job["id"])["status"] in {"queued", "running"}


def test_snapshot_proof_persistence_failure_blocks_update_without_replaying_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = SnapshotUpdateExecutor()
    service, db, job = _approved_update(tmp_path, executor)
    monkeypatch.setattr(
        db,
        "record_pre_update_snapshot_proof",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ValueError("simulated proof write failure")
        ),
    )

    service._run_job(db.get_job(job["id"]))
    after_failure = db.get_job(job["id"])
    modeled = service._refresh_snapshot_state(106)["snapshots"][0]
    host = service.host_control
    assert isinstance(host, PreUpdateHost)
    restarted = OpsService(service.settings, db, executor, host_control=host)
    after_restart = restarted._refresh_snapshot_state(106)["snapshots"][0]

    assert after_failure["status"] == "blocked"
    assert "update" not in executor.actions
    assert len([call for call in host.calls if call[0] == "snapshot_create"]) == 1
    assert "snapshot_proof" not in dict(after_failure.get("result") or {})
    assert modeled["owned_by_hubinet_ops"] is False
    assert after_restart["owned_by_hubinet_ops"] is False
    assert after_restart["ownership_status"] == "host_owned_unproven"


@pytest.mark.parametrize(
    "result_mutation",
    [
        {"source_job_id": None},
        {"source_job_id": "malformed"},
        {"name": "different-snapshot"},
        {"kind": "manual"},
        {"vmid": 999},
    ],
    ids=["missing-source", "malformed-source", "wrong-name", "wrong-kind", "wrong-vmid"],
)
def test_invalid_hostd_snapshot_result_blocks_without_proof_or_update(
    tmp_path: Path,
    result_mutation: dict[str, Any],
) -> None:
    executor = SnapshotUpdateExecutor()
    host = PreUpdateHost()
    host.result_mutation = result_mutation
    service, db, job = _approved_update(tmp_path, executor, host)

    service._run_job(db.get_job(job["id"]))

    terminal = db.get_job(job["id"])
    assert terminal["status"] == "blocked"
    assert "snapshot_proof" not in dict(terminal.get("result") or {})
    assert "update" not in executor.actions
    assert len([call for call in host.calls if call[0] == "snapshot_create"]) == 1


@pytest.mark.parametrize(
    "physical_mutation",
    [
        {"source_job_id": "b" * 32},
        {"source_job_id": ""},
        {"owned_by_hubinet_ops": False},
        {"kind": "manual"},
        {"ownership_status": "uncertain"},
        {"vmid": 999},
        {"vmid": "malformed"},
    ],
    ids=[
        "source-mismatch",
        "empty-source",
        "foreign",
        "wrong-kind",
        "uncertain",
        "conflicting-vmid",
        "malformed-vmid",
    ],
)
def test_invalid_physical_snapshot_never_receives_proof_or_starts_update(
    tmp_path: Path,
    physical_mutation: dict[str, Any],
) -> None:
    executor = SnapshotUpdateExecutor()
    host = PreUpdateHost()
    host.listing_mutation = physical_mutation
    service, db, job = _approved_update(tmp_path, executor, host)

    service._run_job(db.get_job(job["id"]))

    terminal = db.get_job(job["id"])
    assert terminal["status"] == "blocked"
    assert "snapshot_proof" not in dict(terminal.get("result") or {})
    assert "update" not in executor.actions


def test_duplicate_physical_snapshot_payload_fails_closed_without_proof(
    tmp_path: Path,
) -> None:
    executor = SnapshotUpdateExecutor()
    host = PreUpdateHost()
    host.duplicate_listing = True
    service, db, job = _approved_update(tmp_path, executor, host)

    service._run_job(db.get_job(job["id"]))

    terminal = db.get_job(job["id"])
    assert terminal["status"] == "blocked"
    assert "snapshot_proof" not in dict(terminal.get("result") or {})
    assert "update" not in executor.actions


def test_final_managed_refresh_failure_preserves_confirmed_proof_for_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = SnapshotUpdateExecutor()
    service, db, job = _approved_update(tmp_path, executor)
    monkeypatch.setattr(
        service,
        "_refresh_snapshot_state",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ValueError("simulated managed refresh failure")
        ),
    )

    service._run_job(db.get_job(job["id"]))

    terminal = db.get_job(job["id"])
    assert terminal["status"] == "blocked"
    assert terminal["result"]["snapshot_proof"]["snapshot_name"] == terminal["snapshot_name"]
    assert "update" not in executor.actions
    host = service.host_control
    assert isinstance(host, PreUpdateHost)
    restarted = OpsService(service.settings, db, executor, host_control=host)
    restarted._reconcile_startup_jobs()
    modeled = restarted._refresh_snapshot_state(106)["snapshots"][0]
    assert modeled["owned_by_hubinet_ops"] is True
    assert modeled["source_job_id"] == job["id"]


def test_crash_after_proof_preserves_exact_source_ownership_on_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = SnapshotUpdateExecutor()
    service, db, job = _approved_update(tmp_path, executor)
    monkeypatch.setattr(
        service,
        "_refresh_snapshot_state",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            SystemExit("simulated crash after proof")
        ),
    )

    with pytest.raises(SystemExit, match="simulated crash after proof"):
        service._run_job(db.get_job(job["id"]))

    crashed = db.get_job(job["id"])
    assert crashed["result"]["snapshot_proof"]["version"] == 3
    assert "update" not in executor.actions
    host = service.host_control
    assert isinstance(host, PreUpdateHost)
    restarted = OpsService(service.settings, db, executor, host_control=host)
    restarted._reconcile_startup_jobs()
    modeled = restarted._refresh_snapshot_state(106)["snapshots"][0]
    assert modeled["owned_by_hubinet_ops"] is True
    assert modeled["host_source_job_id"] == crashed["result"]["snapshot_proof"][
        "host_source_job_id"
    ]


def test_crash_before_physical_confirmation_resumes_without_duplicate_create(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = SnapshotUpdateExecutor(
        [docker_state(3), docker_state(3), docker_state(3)]
    )
    service, db, job = _approved_update(tmp_path, executor)
    monkeypatch.setattr(
        service,
        "_confirm_physical_pre_update_snapshot",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(SystemExit("simulated crash")),
    )

    with pytest.raises(SystemExit, match="simulated crash"):
        service._run_job(db.get_job(job["id"]))

    crashed = db.get_job(job["id"])
    assert "snapshot_proof" not in dict(crashed.get("result") or {})
    assert crashed["result"]["pre_update_snapshot_create"]["phase"] == "confirming"
    host = service.host_control
    assert isinstance(host, PreUpdateHost)
    assert len([call for call in host.calls if call[0] == "snapshot_create"]) == 1
    assert "update" not in executor.actions
    restarted = OpsService(
        service.settings,
        db,
        executor,
        host_control=host,
        stabilizer=HealthyStabilizer(),  # type: ignore[arg-type]
    )
    restarted._reconcile_startup_jobs()
    assert len([call for call in host.calls if call[0] == "snapshot_create"]) == 1
    resumed = db.get_job(job["id"])
    assert resumed["status"] == "success"
    assert resumed["result"]["pre_update_snapshot_create"]["phase"] == "completed"
    assert resumed["result"]["snapshot_proof"]["version"] == 3
    assert "update" in executor.actions
    modeled = restarted._refresh_snapshot_state(106)["snapshots"][0]
    assert modeled["owned_by_hubinet_ops"] is True
    assert modeled["ownership_status"] == "managed"


def test_restart_from_pre_update_remote_succeeded_confirms_without_duplicate_post(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = SnapshotUpdateExecutor(
        [docker_state(3), docker_state(3), docker_state(3)]
    )
    host = PreUpdateHost()
    service, db, job = _approved_update(tmp_path, executor, host)
    persist = db.persist_pre_update_create_contract
    crashed = False

    def crash_after_remote_success(
        job_id: str,
        contract: dict[str, Any],
    ) -> dict[str, Any]:
        nonlocal crashed
        saved = persist(job_id, contract)
        if contract.get("phase") == "remote_succeeded" and not crashed:
            crashed = True
            raise SystemExit("crash after remote snapshot success")
        return saved

    monkeypatch.setattr(
        db, "persist_pre_update_create_contract", crash_after_remote_success
    )
    with pytest.raises(SystemExit, match="remote snapshot success"):
        service._run_job(db.get_job(job["id"]))

    before_restart = db.get_job(job["id"])
    assert before_restart["result"]["pre_update_snapshot_create"]["phase"] == "remote_succeeded"
    assert "snapshot_proof" not in before_restart["result"]
    assert "update" not in executor.actions
    assert len([call for call in host.calls if call[0] == "snapshot_create"]) == 1

    restarted = OpsService(
        service.settings,
        db,
        executor,
        host_control=host,
        stabilizer=HealthyStabilizer(),  # type: ignore[arg-type]
    )
    restarted._reconcile_startup_jobs()

    completed = db.get_job(job["id"])
    assert completed["status"] == "success"
    assert completed["result"]["pre_update_snapshot_create"]["phase"] == "completed"
    assert completed["result"]["snapshot_proof"]["version"] == 3
    assert len([call for call in host.calls if call[0] == "snapshot_create"]) == 1
    assert "update" in executor.actions


def test_pre_update_confirmation_mismatch_after_restart_blocks_without_recreate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = SnapshotUpdateExecutor(
        [docker_state(3), docker_state(3), docker_state(3)]
    )
    host = PreUpdateHost()
    service, db, job = _approved_update(tmp_path, executor, host)
    monkeypatch.setattr(
        service,
        "_confirm_physical_pre_update_snapshot",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            SystemExit("crash before confirmation")
        ),
    )
    with pytest.raises(SystemExit, match="before confirmation"):
        service._run_job(db.get_job(job["id"]))

    assert db.get_job(job["id"])["result"]["pre_update_snapshot_create"]["phase"] == "confirming"
    assert "snapshot_proof" not in db.get_job(job["id"])["result"]
    assert len([call for call in host.calls if call[0] == "snapshot_create"]) == 1
    host.listing_mutation["pve_snaptime"] = int(host.snapshots[0]["pve_snaptime"]) + 1

    restarted = OpsService(service.settings, db, executor, host_control=host)
    restarted._reconcile_startup_jobs()

    blocked = db.get_job(job["id"])
    assert blocked["status"] == "blocked"
    assert "snapshot_proof" not in blocked["result"]
    assert "update" not in executor.actions
    assert len([call for call in host.calls if call[0] == "snapshot_create"]) == 1


def test_executor_snapshot_result_cannot_create_or_overwrite_backend_proof(
    tmp_path: Path,
) -> None:
    class SpoofingResultExecutor(SnapshotUpdateExecutor):
        def run(self, action: str, vmid: int, argument=None, timeout=None, on_event=None):
            result = super().run(action, vmid, argument, timeout, on_event)
            if action == "snapshot":
                return {
                    "ok": True,
                    "data": {
                        "snapshot_proof": {
                            "version": 1,
                            "vmid": 999,
                            "snapshot_name": "executor-controlled",
                            "kind": "pre-update",
                        }
                    },
                }
            return result

    executor = SpoofingResultExecutor(
        [docker_state(3), docker_state(3), docker_state(3)]
    )
    service, db, job = _approved_update(tmp_path, executor)

    service._run_job(db.get_job(job["id"]))
    terminal = db.get_job(job["id"])
    proof = terminal["result"]["snapshot_proof"]

    assert proof["version"] == 3
    assert proof["vmid"] == 106
    assert proof["snapshot_name"] == terminal["snapshot_name"]
    assert proof["kind"] == "pre-update"
    assert len(proof["host_source_job_id"]) == 32
    assert proof["snapshot_name"] != "executor-controlled"
    assert "snapshot" not in executor.actions


@pytest.mark.parametrize(
    ("reserved_type", "malformed_details"),
    [
        ("snapshot_mutation_succeeded", False),
        ("snapshot_created", False),
        ("snapshot_mutation_succeeded", True),
    ],
)
def test_executor_cannot_spoof_backend_snapshot_proof_before_failure(
    tmp_path: Path,
    reserved_type: str,
    malformed_details: bool,
) -> None:
    class SpoofingSnapshotExecutor(SnapshotUpdateExecutor):
        def run(self, action: str, vmid: int, argument=None, timeout=None, on_event=None):
            if action == "snapshot":
                self.actions.append(action)
                assert on_event is not None
                on_event(
                    {
                        "event_type": "snapshot_executor_progress",
                        "message": "Snapshot command started",
                        "details": {"snapshot_name": argument},
                    }
                )
                on_event(
                    {
                        "event_type": reserved_type,
                        "message": "Untrusted executor claim",
                        "details": (
                            ["malformed", argument]
                            if malformed_details
                            else {"snapshot_name": argument}
                        ),
                    }
                )
                self.snapshots.append(
                    {
                        "name": argument,
                        "created_at": "2026-07-29T00:00:00+00:00",
                        "kind": "pre-update",
                        "owned_by_hubinet_ops": True,
                        "rollback_eligible": True,
                        "delete_eligible": True,
                    }
                )
                raise ExecutorError("snapshot executor failed after spoofed event")
            return super().run(action, vmid, argument, timeout, on_event)

    executor = SpoofingSnapshotExecutor(
        [docker_state(3), docker_state(3), docker_state(3)]
    )
    service, db, job = _approved_update(tmp_path, executor)

    service._run_job(db.get_job(job["id"]))
    events = db.list_job_events(job["id"])

    assert db.get_job(job["id"])["result"]["snapshot_proof"]["version"] == 3
    assert "snapshot" not in executor.actions
    assert "update" in executor.actions
    assert sum(event["event_type"] == reserved_type for event in events) == 1
    assert not any(
        event["event_type"] == f"executor_{reserved_type}" for event in events
    )


def test_unconfirmed_pre_update_snapshot_blocks_before_apt_mutation(
    tmp_path: Path,
) -> None:
    executor = SnapshotUpdateExecutor()
    host = PreUpdateHost()
    host.hide_listing = True
    service, db, job = _approved_update(tmp_path, executor, host)

    service._run_job(db.get_job(job["id"]))

    assert "update" not in executor.actions
    terminal = db.get_job(job["id"])
    assert terminal["status"] == "blocked"
    assert "snapshot_proof" not in dict(terminal.get("result") or {})
    state = service.get_state(106)
    assert state["snapshot_state_stale"] is True
    assert state["snapshot_refresh_required"] is True
    assert any(
        event["event_type"] == "snapshot_confirmation_failed"
        for event in db.list_job_events(job["id"])
    )

    host.hide_listing = False
    delayed = service._refresh_snapshot_state(106)["snapshots"][0]
    assert delayed["owned_by_hubinet_ops"] is False
    assert delayed["ownership_status"] == "host_owned_unproven"
    assert delayed["rollback_eligible"] is False
    assert delayed["delete_eligible"] is False

    service.settings.raw["containers"][106]["operator_capabilities"].update(
        {"snapshot_list": True, "snapshot_delete": True}
    )
    with pytest.raises(ValueError, match="missing, foreign, or ineligible"):
        service.manual_rollback(106)
    before_jobs = [item["id"] for item in db.list_jobs()]
    prune = service.queue_snapshot_prune(
        106, "oldest", "delayed-unproven-prune-0001"
    )
    assert prune == {
        "status": "nothing_to_delete",
        "mode": "oldest",
        "deleted_count": 0,
    }
    assert [item["id"] for item in db.list_jobs()] == before_jobs
    assert not any(call[0] == "snapshot_delete" for call in host.calls)


def test_manual_snapshot_create_and_delete_refresh_canonical_state(
    tmp_path: Path,
) -> None:
    cfg = lifecycle_settings(tmp_path)
    db = Database(cfg.db_path)
    host = FakeHostControl()
    service = OpsService(cfg, db, CompatibleExecutor(), host_control=host)

    created = service.queue_snapshot_create(106, "manual-create-refresh-0001")
    run_queued(service, db)
    assert service.get_state(106)["snapshot_count"] == 1
    assert service.get_state(106)["latest_snapshot_name"] == created["snapshot_name"]

    service.queue_snapshot_action(
        106,
        "delete",
        created["snapshot_name"],
        "manual-delete-refresh-0001",
    )
    run_queued(service, db)
    assert service.get_state(106)["snapshot_count"] == 0
    assert service.get_state(106)["latest_snapshot_name"] is None


def test_refresh_failure_keeps_successful_mutation_and_marks_state_stale(
    tmp_path: Path,
) -> None:
    class RefreshFailingHost(FakeHostControl):
        def __init__(self) -> None:
            super().__init__()
            self.fail_listing = False
            self.list_calls = 0

        def list_snapshots(self, vmid: int) -> list[dict[str, Any]]:
            self.list_calls += 1
            if self.fail_listing:
                raise HostControlError("temporary snapshot read failure", status="unavailable")
            return super().list_snapshots(vmid)

        def execute(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            result = super().execute(*args, **kwargs)
            if args[0] == "snapshot_create":
                self.fail_listing = True
            return result

    cfg = lifecycle_settings(tmp_path)
    db = Database(cfg.db_path)
    host = RefreshFailingHost()
    service = OpsService(cfg, db, CompatibleExecutor(), host_control=host)
    queued = service.queue_snapshot_create(106, "manual-create-stale-0001")

    terminal = run_queued(service, db)

    assert terminal["status"] == "success"
    assert len(host.snapshots) == 1
    assert host.list_calls == 3
    state = service.get_state(106)
    assert state["snapshot_state_stale"] is True
    assert state["snapshot_refresh_required"] is True
    assert "temporary snapshot read failure" in state["snapshot_refresh_warning"]
    assert any(
        event["event_type"] == "snapshot_refresh_failed"
        and event["level"] == "warning"
        for event in db.list_job_events(queued["id"])
    )


def test_automatic_rollback_refreshes_snapshot_state(tmp_path: Path) -> None:
    class FailingAfterSnapshotExecutor(SnapshotUpdateExecutor):
        def run(self, action: str, vmid: int, argument=None, timeout=None, on_event=None):
            if action == "update":
                self.actions.append(action)
                raise ExecutorError("update failed")
            return super().run(action, vmid, argument, timeout, on_event)

    class TrackingHost(PreUpdateHost):
        def __init__(self) -> None:
            super().__init__()
            self.rollback_contract: tuple[str, dict[str, Any]] | None = None

        def execute(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            if args[0] == "snapshot_rollback":
                self.rollback_contract = (str(args[2]), dict(kwargs))
            return super().execute(*args, **kwargs)

    executor = FailingAfterSnapshotExecutor()
    cfg = update_settings(tmp_path)
    cfg.raw["containers"][106]["pre_update_snapshot"] = True
    cfg.raw["containers"][106]["automatic_rollback"] = True
    db = Database(cfg.db_path)
    host = TrackingHost()
    service = OpsService(
        cfg,
        db,
        executor,
        host_control=host,
    )
    scanned = service.scan_container(106)
    job = service.approve(scanned["plan"]["id"])["job"]

    service._run_job(db.get_job(job["id"]))

    assert db.get_job(job["id"])["status"] == "rolled_back"
    assert "list-snapshots" not in executor.actions
    assert "rollback" not in executor.actions
    assert host.rollback_contract is not None
    request_id, contract = host.rollback_contract
    proof = db.get_job(job["id"])["result"]["snapshot_proof"]
    assert request_id == f"automatic-rollback-{job['id']}"
    assert contract["expected_source_job_id"] == proof["host_source_job_id"]
    assert contract["expected_pve_snaptime"] == proof["pve_snaptime"]
    assert service.get_state(106)["snapshot_state_stale"] is False


def test_automatic_rollback_reattaches_same_identity_after_backend_restart(
    tmp_path: Path,
) -> None:
    class FailingUpdateExecutor(SnapshotUpdateExecutor):
        def run(self, action: str, vmid: int, argument=None, timeout=None, on_event=None):
            if action == "update":
                self.actions.append(action)
                raise ExecutorError("update failed")
            return super().run(action, vmid, argument, timeout, on_event)

    class CrashAfterRemoteRollback(PreUpdateHost):
        def execute(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            if args[0] == "snapshot_rollback":
                self.existing_jobs[str(args[2])] = {
                    "status": "succeeded",
                    "result": {"lxc_status": "running"},
                }
                raise SystemExit("backend crashed after host rollback")
            return super().execute(*args, **kwargs)

    executor = FailingUpdateExecutor(
        [docker_state(3), docker_state(3), docker_state(3)]
    )
    cfg = update_settings(tmp_path)
    cfg.raw["containers"][106]["pre_update_snapshot"] = True
    cfg.raw["containers"][106]["automatic_rollback"] = True
    db = Database(cfg.db_path)
    host = CrashAfterRemoteRollback()
    service = OpsService(cfg, db, executor, host_control=host)
    scanned = service.scan_container(106)
    job = service.approve(scanned["plan"]["id"])["job"]

    with pytest.raises(SystemExit, match="backend crashed"):
        service._run_job(db.get_job(job["id"]))
    crashed = db.get_job(job["id"])
    assert crashed["stage"] == "rollback"

    restarted = OpsService(cfg, db, executor, host_control=host)
    restarted._reconcile_startup_jobs()

    assert db.get_job(job["id"])["status"] == "rolled_back"
    assert host.reattach_calls[-1][2] == f"automatic-rollback-{job['id']}"


def test_automatic_rollback_host_identity_mismatch_blocks_physical_rollback(
    tmp_path: Path,
) -> None:
    class FailingUpdateExecutor(SnapshotUpdateExecutor):
        def run(self, action: str, vmid: int, argument=None, timeout=None, on_event=None):
            if action == "update":
                self.actions.append(action)
                raise ExecutorError("update failed")
            return super().run(action, vmid, argument, timeout, on_event)

    class RejectingRollbackHost(PreUpdateHost):
        def __init__(self) -> None:
            super().__init__()
            self.physical_rollback_calls = 0

        def execute(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            if args[0] == "snapshot_rollback":
                raise HostControlError(
                    "Physical snapshot identity changed before mutation",
                    status="failed",
                )
            return super().execute(*args, **kwargs)

    executor = FailingUpdateExecutor()
    cfg = update_settings(tmp_path)
    cfg.raw["containers"][106]["pre_update_snapshot"] = True
    cfg.raw["containers"][106]["automatic_rollback"] = True
    db = Database(cfg.db_path)
    host = RejectingRollbackHost()
    service = OpsService(cfg, db, executor, host_control=host)
    job = service.approve(service.scan_container(106)["plan"]["id"])["job"]

    service._run_job(db.get_job(job["id"]))

    terminal = db.get_job(job["id"])
    assert terminal["status"] == "failed"
    assert "identity changed" in terminal["error"]
    assert "rollback" not in executor.actions
    assert host.physical_rollback_calls == 0
