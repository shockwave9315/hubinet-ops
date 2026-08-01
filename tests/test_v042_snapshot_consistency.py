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


def _approved_update(
    tmp_path: Path,
    executor: SnapshotUpdateExecutor,
) -> tuple[OpsService, Database, dict[str, Any]]:
    cfg = update_settings(tmp_path)
    cfg.raw["containers"][106]["pre_update_snapshot"] = True
    cfg.raw["containers"][106]["automatic_rollback"] = False
    db = Database(cfg.db_path)
    service = OpsService(cfg, db, executor)
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
            if action in {"snapshot", "list-snapshots", "update"}:
                order.append(action)
            return super().run(action, vmid, argument, timeout, on_event)

    executor = OrderedExecutor()
    service, db, job = _approved_update(tmp_path, executor)
    record_proof = db.record_pre_update_snapshot_proof

    def traced_record_proof(*args: Any, **kwargs: Any) -> dict[str, Any]:
        order.append("record-proof")
        return record_proof(*args, **kwargs)

    monkeypatch.setattr(db, "record_pre_update_snapshot_proof", traced_record_proof)

    service._run_job(db.get_job(job["id"]))

    assert order == [
        "snapshot",
        "list-snapshots",
        "record-proof",
        "list-snapshots",
        "update",
    ]
    assert executor.actions.index("snapshot") < executor.actions.index("list-snapshots")
    assert executor.actions.index("list-snapshots") < executor.actions.index("update")
    state = service.get_state(106)
    assert state["snapshot_count"] == 1
    assert state["latest_snapshot_name"] == db.get_job(job["id"])["snapshot_name"]
    assert state["latest_snapshot_kind"] == "pre-update"
    assert state["snapshot_state_stale"] is False
    proof = db.get_job(job["id"])["result"]["snapshot_proof"]
    assert proof == {
        "version": 1,
        "vmid": 106,
        "snapshot_name": db.get_job(job["id"])["snapshot_name"],
        "kind": "pre-update",
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


def test_snapshot_executor_failure_before_mutation_marker_leaves_snapshot_uncertain(
    tmp_path: Path,
) -> None:
    class FailingSnapshotExecutor(SnapshotUpdateExecutor):
        def run(self, action: str, vmid: int, argument=None, timeout=None, on_event=None):
            if action == "snapshot":
                self.actions.append(action)
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
                raise ExecutorError("snapshot executor failed")
            return super().run(action, vmid, argument, timeout, on_event)

    executor = FailingSnapshotExecutor()
    service, db, job = _approved_update(tmp_path, executor)

    service._run_job(db.get_job(job["id"]))
    modeled = service._refresh_snapshot_state(106)["snapshots"][0]

    assert db.get_job(job["id"])["status"] == "blocked"
    assert not any(
        event["event_type"] == "snapshot_mutation_succeeded"
        for event in db.list_job_events(job["id"])
    )
    assert modeled["owned_by_hubinet_ops"] is False
    assert modeled["ownership_status"] == "uncertain"
    assert modeled["protected"] is True
    assert modeled["delete_eligible"] is False
    assert modeled["rollback_eligible"] is False


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
    restarted = OpsService(service.settings, db, executor)
    after_restart = restarted._refresh_snapshot_state(106)["snapshots"][0]

    assert after_failure["status"] == "blocked"
    assert "update" not in executor.actions
    assert executor.actions.count("snapshot") == 1
    assert "snapshot_proof" not in dict(after_failure.get("result") or {})
    assert modeled["owned_by_hubinet_ops"] is False
    assert after_restart["owned_by_hubinet_ops"] is False
    assert after_restart["ownership_status"] == "uncertain"


@pytest.mark.parametrize(
    "physical_mutation",
    [
        {"owned_by_hubinet_ops": False},
        {"kind": "manual"},
        {"ownership_status": "uncertain"},
        {"vmid": 999},
        {"vmid": "malformed"},
    ],
    ids=["foreign", "wrong-kind", "uncertain", "conflicting-vmid", "malformed-vmid"],
)
def test_invalid_physical_snapshot_never_receives_proof_or_starts_update(
    tmp_path: Path,
    physical_mutation: dict[str, Any],
) -> None:
    class InvalidPhysicalSnapshotExecutor(SnapshotUpdateExecutor):
        def run(self, action: str, vmid: int, argument=None, timeout=None, on_event=None):
            result = super().run(action, vmid, argument, timeout, on_event)
            if action == "snapshot":
                self.snapshots[0].update(physical_mutation)
            return result

    executor = InvalidPhysicalSnapshotExecutor()
    service, db, job = _approved_update(tmp_path, executor)

    service._run_job(db.get_job(job["id"]))

    terminal = db.get_job(job["id"])
    assert terminal["status"] == "blocked"
    assert "snapshot_proof" not in dict(terminal.get("result") or {})
    assert "update" not in executor.actions


def test_duplicate_physical_snapshot_payload_fails_closed_without_proof(
    tmp_path: Path,
) -> None:
    class DuplicateSnapshotExecutor(SnapshotUpdateExecutor):
        def run(self, action: str, vmid: int, argument=None, timeout=None, on_event=None):
            result = super().run(action, vmid, argument, timeout, on_event)
            if action == "snapshot":
                self.snapshots.append(dict(self.snapshots[0]))
            return result

    executor = DuplicateSnapshotExecutor()
    service, db, job = _approved_update(tmp_path, executor)

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
    restarted = OpsService(service.settings, db, executor)
    modeled = restarted._refresh_snapshot_state(106)["snapshots"][0]
    assert modeled["owned_by_hubinet_ops"] is True
    assert modeled["source_job_id"] == job["id"]


def test_crash_before_physical_confirmation_leaves_no_restart_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = SnapshotUpdateExecutor()
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
    assert executor.actions.count("snapshot") == 1
    assert "update" not in executor.actions
    restarted = OpsService(service.settings, db, executor)
    modeled = restarted._refresh_snapshot_state(106)["snapshots"][0]
    assert modeled["owned_by_hubinet_ops"] is False
    assert modeled["ownership_status"] == "uncertain"


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

    assert proof == {
        "version": 1,
        "vmid": 106,
        "snapshot_name": terminal["snapshot_name"],
        "kind": "pre-update",
    }
    assert proof["snapshot_name"] != "executor-controlled"


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

    executor = SpoofingSnapshotExecutor()
    service, db, job = _approved_update(tmp_path, executor)
    service.settings.raw["containers"][106]["operator_capabilities"].update(
        {"snapshot_list": True, "snapshot_delete": True}
    )

    service._run_job(db.get_job(job["id"]))
    events = db.list_job_events(job["id"])
    modeled = service._refresh_snapshot_state(106)["snapshots"][0]
    host = FakeHostControl("running")
    host.snapshots = list(executor.snapshots)
    service.host_control = host
    service.queue_snapshot_prune(106, "oldest", "spoofed-proof-prune-0001")
    prune = run_queued(service, db)

    assert db.get_job(job["id"])["status"] == "blocked"
    assert not any(event["event_type"] == reserved_type for event in events)
    forwarded = next(
        event
        for event in events
        if event["event_type"] == f"executor_{reserved_type}"
    )
    assert forwarded["details"] == (
        {}
        if malformed_details
        else {"snapshot_name": db.get_job(job["id"])["snapshot_name"]}
    )
    assert any(
        event["event_type"] == "snapshot_executor_progress" for event in events
    )
    assert modeled["owned_by_hubinet_ops"] is False
    assert modeled["ownership_status"] == "uncertain"
    assert modeled["rollback_eligible"] is False
    assert modeled["delete_eligible"] is False
    assert prune["result"]["deleted"] == []
    assert not any(call[0] == "snapshot_delete" for call in host.calls)


def test_unconfirmed_pre_update_snapshot_blocks_before_apt_mutation(
    tmp_path: Path,
) -> None:
    class MissingFromListingExecutor(SnapshotUpdateExecutor):
        expose_snapshot = False

        def run(self, action: str, vmid: int, argument=None, timeout=None, on_event=None):
            if action == "list-snapshots" and not self.expose_snapshot:
                self.actions.append(action)
                return {"ok": True, "data": {"snapshots": []}}
            return super().run(action, vmid, argument, timeout, on_event)

    executor = MissingFromListingExecutor()
    service, db, job = _approved_update(tmp_path, executor)

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

    executor.expose_snapshot = True
    delayed = service._refresh_snapshot_state(106)["snapshots"][0]
    assert delayed["owned_by_hubinet_ops"] is False
    assert delayed["ownership_status"] == "uncertain"
    assert delayed["rollback_eligible"] is False
    assert delayed["delete_eligible"] is False

    service.settings.raw["containers"][106]["operator_capabilities"].update(
        {"snapshot_list": True, "snapshot_delete": True}
    )
    host = FakeHostControl("running")
    host.snapshots = list(executor.snapshots)
    service.host_control = host
    with pytest.raises(ValueError, match="missing, foreign, or ineligible"):
        service.manual_rollback(106)
    service.queue_snapshot_prune(106, "oldest", "delayed-unproven-prune-0001")
    prune = run_queued(service, db)
    assert prune["result"]["deleted"] == []
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

    executor = FailingAfterSnapshotExecutor()
    cfg = update_settings(tmp_path)
    cfg.raw["containers"][106]["pre_update_snapshot"] = True
    cfg.raw["containers"][106]["automatic_rollback"] = True
    db = Database(cfg.db_path)
    service = OpsService(cfg, db, executor)
    scanned = service.scan_container(106)
    job = service.approve(scanned["plan"]["id"])["job"]

    service._run_job(db.get_job(job["id"]))

    assert db.get_job(job["id"])["status"] == "rolled_back"
    assert executor.actions.count("list-snapshots") >= 2
    assert service.get_state(106)["snapshot_state_stale"] is False
