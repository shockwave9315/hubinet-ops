from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from app.config import Settings
from app.contracts import REQUIRED_APT_ACTIONS
from app.database import Database
from app.service import OpsService


EXECUTOR_HASH = "a" * 64
PROFILE_HASH = "b" * 64


class CompatibleExecutor:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def run(self, action: str, vmid: int, argument=None, timeout=None, on_event=None):
        self.calls.append(action)
        if action == "capabilities":
            return {
                "ok": True,
                "data": {
                    "version": "0.4.0",
                    "protocol_version": 1,
                    "supported_actions": sorted(REQUIRED_APT_ACTIONS),
                    "executor_sha256": EXECUTOR_HASH,
                    "profile_sha256": PROFILE_HASH,
                    "profile_validation_status": "valid",
                },
            }
        return {"ok": True, "data": {}}


class FakeHostControl:
    def __init__(self, status: str = "stopped") -> None:
        self.runtime = status
        self.calls: list[tuple[str, int, str, str | None, str | None]] = []
        self.snapshots: list[dict[str, Any]] = []
        self.release = {
            "version": "0.4.0",
            "release_id": "hubinet-ops-0.4.0-aaaaaaaaaaaaaaaa",
            "fingerprint": "a" * 64,
            "file_count": 136,
            "total_bytes": 1000,
        }

    def status(self, vmid: int) -> dict[str, Any]:
        return {"resource_type": "lxc", "runtime_status": self.runtime, "lxc_status": self.runtime}

    def list_snapshots(self, vmid: int) -> list[dict[str, Any]]:
        return [dict(item) for item in self.snapshots]

    def inspect_self_update_release(self, vmid: int) -> dict[str, Any]:
        assert vmid == 110
        return dict(self.release)

    def execute(
        self,
        operation_type: str,
        vmid: int,
        request_id: str,
        *,
        snapshot_name: str | None = None,
        release_fingerprint: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            (
                operation_type,
                vmid,
                request_id,
                snapshot_name,
                release_fingerprint,
            )
        )
        if operation_type == "lifecycle_start":
            self.runtime = "running"
        elif operation_type in {"lifecycle_shutdown", "lifecycle_force_stop"}:
            self.runtime = "stopped"
        elif operation_type == "lifecycle_reboot":
            self.runtime = "running"
        elif operation_type == "snapshot_create":
            assert snapshot_name
            self.snapshots.insert(
                0,
                {
                    "name": snapshot_name,
                    "description": "hubinet-ops",
                    "created_at": "2026-07-20T19:20:00+00:00",
                    "kind": "manual",
                    "owned_by_hubinet_ops": True,
                    "rollback_eligible": True,
                    "delete_eligible": True,
                    "source_job_id": request_id,
                },
            )
        elif operation_type == "snapshot_delete":
            self.snapshots = [item for item in self.snapshots if item["name"] != snapshot_name]
        elif operation_type == "self_update":
            assert release_fingerprint == self.release["fingerprint"]
            return {
                "version": self.release["version"],
                "release_id": self.release["release_id"],
                "fingerprint": release_fingerprint,
                "exit_code": 0,
            }
        return {"runtime_status": self.runtime, "lxc_status": self.runtime}


def settings(tmp_path: Path, *, vmid: int = 106, adapter: str = "apt") -> Settings:
    capabilities = {
        name: True
        for name in (
            "refresh", "scan", "approve", "reject", "retry_healthcheck",
            "start", "shutdown", "reboot", "force_stop", "snapshot_create",
            "snapshot_list", "snapshot_rollback", "snapshot_delete",
        )
    }
    if adapter == "agent_self":
        for name in ("scan", "retry_healthcheck"):
            capabilities[name] = False
        capabilities["self_update"] = True
    resource: dict[str, Any] = {
        "resource_type": "lxc",
        "adapter": adapter,
        "name": f"ct-{vmid}",
        "enabled": True,
        "monitoring": {"inspect": True, "update_scan": adapter == "apt"},
        "operator_capabilities": capabilities,
        "snapshot_retention": 5,
    }
    if adapter == "apt":
        resource["executor_contract"] = {
            "executor_sha256": EXECUTOR_HASH,
            "profile_sha256": PROFILE_HASH,
        }
    return Settings(
        raw={
            "scheduler": {"enabled": False},
            "mqtt": {"enabled": False},
            "home_assistant": {},
            "resources": {vmid: resource},
        },
        config_path=tmp_path / "config.yaml",
        db_path=tmp_path / "ops.db",
        api_token="t" * 64,
    )


def run_queued(service: OpsService, db: Database) -> dict[str, Any]:
    job = db.next_queued_job()
    assert job is not None
    service._run_job(job)
    return db.get_job(job["id"])


@pytest.mark.parametrize(
    ("action", "initial", "expected", "operation"),
    [
        ("start", "stopped", "running", "lifecycle_start"),
        ("shutdown", "running", "stopped", "lifecycle_shutdown"),
        ("reboot", "running", "running", "lifecycle_reboot"),
        ("force-stop", "running", "stopped", "lifecycle_force_stop"),
    ],
)
def test_lifecycle_jobs_are_typed_durable_and_terminal(
    tmp_path: Path,
    action: str,
    initial: str,
    expected: str,
    operation: str,
) -> None:
    cfg = settings(tmp_path)
    db = Database(cfg.db_path)
    host = FakeHostControl(initial)
    service = OpsService(
        cfg, db, CompatibleExecutor(), host_control=host  # type: ignore[arg-type]
    )

    queued = service.queue_lifecycle(106, action, f"request-{action}-12345678")
    terminal = run_queued(service, db)

    assert queued["operation_type"] == operation
    assert terminal["status"] == "success"
    assert terminal["progress"] == 100
    assert host.runtime == expected
    assert host.calls[0][0] == operation


def test_lifecycle_guards_runtime_active_job_plan_and_request_id(tmp_path: Path) -> None:
    cfg = settings(tmp_path)
    db = Database(cfg.db_path)
    host = FakeHostControl("running")
    service = OpsService(cfg, db, CompatibleExecutor(), host_control=host)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="requires stopped"):
        service.queue_lifecycle(106, "start")

    first = service.queue_lifecycle(106, "reboot", "request-reboot-0001")
    same = service.queue_lifecycle(106, "reboot", "request-reboot-0001")
    assert same["id"] == first["id"]
    with pytest.raises(ValueError, match="destructive maintenance job"):
        service.queue_lifecycle(106, "reboot", "request-reboot-0002")

    run_queued(service, db)
    db.create_plan(
        vmid=106, container_name="ct-106", fingerprint="fp", risk="low",
        payload={"pending_count": 1}, ttl_minutes=60,
    )
    with pytest.raises(ValueError, match="active update plan"):
        service.queue_lifecycle(106, "reboot")


def test_retry_healthcheck_is_a_durable_idempotent_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = settings(tmp_path)
    db = Database(cfg.db_path)
    host = FakeHostControl("running")
    service = OpsService(cfg, db, CompatibleExecutor(), host_control=host)  # type: ignore[arg-type]
    source, _ = db.create_operation_job(
        vmid=106,
        container_name="ct-106",
        operation_type="update",
        request_id="failed-update-0001",
    )
    db.update_job(source["id"], status="failed", stage="failed", progress=100)
    monkeypatch.setattr(
        service.stabilizer,
        "wait",
        lambda **_kwargs: {"health_status": "healthy", "health_score": 100},
    )

    queued = service.queue_retry_healthcheck(106, "retry-health-0001")
    same = service.queue_retry_healthcheck(106, "retry-health-0001")
    assert same["id"] == queued["id"]
    assert queued["operation_type"] == "retry_healthcheck"

    terminal = run_queued(service, db)
    assert terminal["status"] == "success"
    assert service.get_state(106)["health_status"] == "healthy"
    assert host.calls == []


def test_snapshot_create_list_latest_rollback_delete_and_foreign_rejection(tmp_path: Path) -> None:
    cfg = settings(tmp_path)
    db = Database(cfg.db_path)
    host = FakeHostControl("running")
    host.snapshots.append(
        {
            "name": "foreign-backup",
            "created_at": "2026-01-01T00:00:00+00:00",
            "kind": None,
            "owned_by_hubinet_ops": False,
            "rollback_eligible": False,
            "delete_eligible": False,
        }
    )
    service = OpsService(
        cfg,
        db,
        CompatibleExecutor(),
        host_control=host,  # type: ignore[arg-type]
        now=lambda: datetime(2026, 7, 20, 19, 20, tzinfo=UTC),
    )

    created = service.queue_snapshot_create(106, "snapshot-create-0001")
    assert created["snapshot_name"] == "hubinet-ops-106-manual-20260720T192000Z"
    assert run_queued(service, db)["status"] == "success"
    listing = service.list_snapshots(106)
    assert listing["latest"]["name"] == created["snapshot_name"]
    assert service.get_state(106)["snapshot_count"] == 1

    rolled_back = service.queue_snapshot_action(
        106, "rollback", "latest", "snapshot-rollback-0001"
    )
    assert run_queued(service, db)["id"] == rolled_back["id"]
    state = service.get_state(106)
    assert state["verification_status"] == "unknown"
    assert state["packages_remaining_count"] is None

    deleted = service.queue_snapshot_action(
        106, "delete", created["snapshot_name"], "snapshot-delete-0001"
    )
    assert run_queued(service, db)["id"] == deleted["id"]
    assert service.list_snapshots(106)["latest"] is None
    with pytest.raises(ValueError, match="does not exist"):
        service.queue_snapshot_action(106, "rollback", "foreign-backup")


def test_snapshot_retention_never_deletes_foreign_or_active_rollback_source(tmp_path: Path) -> None:
    cfg = settings(tmp_path)
    db = Database(cfg.db_path)
    host = FakeHostControl("running")
    owned = []
    for index in range(7):
        name = f"hubinet-ops-106-manual-202607{20-index:02d}T120000Z"
        owned.append(
            {
                "name": name,
                "created_at": f"2026-07-{20-index:02d}T12:00:00+00:00",
                "kind": "manual",
                "owned_by_hubinet_ops": True,
                "rollback_eligible": True,
                "delete_eligible": True,
            }
        )
    host.snapshots = owned + [
        {
            "name": "foreign-backup",
            "created_at": "2020-01-01T00:00:00+00:00",
            "owned_by_hubinet_ops": False,
            "delete_eligible": False,
        }
    ]
    service = OpsService(cfg, db, CompatibleExecutor(), host_control=host)  # type: ignore[arg-type]
    source, _ = db.create_operation_job(
        vmid=106, container_name="ct-106", operation_type="snapshot_rollback",
        request_id="rollback-source-0001", snapshot_name=owned[-1]["name"],
    )
    db.update_job(source["id"], status="failed", stage="failed", progress=100)
    current = {"id": "c" * 32, "snapshot_name": owned[0]["name"]}

    service._enforce_snapshot_retention(106, current)

    remaining = {item["name"] for item in host.snapshots}
    assert "foreign-backup" in remaining
    assert owned[-1]["name"] in remaining
    assert owned[-2]["name"] not in remaining


def test_ct110_start_uses_host_control_when_agent_executor_is_unavailable(tmp_path: Path) -> None:
    cfg = settings(tmp_path, vmid=110, adapter="agent_self")
    db = Database(cfg.db_path)
    host = FakeHostControl("stopped")
    executor = CompatibleExecutor()
    service = OpsService(cfg, db, executor, host_control=host)  # type: ignore[arg-type]

    service.queue_lifecycle(110, "start", "ct110-offline-start")
    terminal = run_queued(service, db)

    assert terminal["status"] == "success"
    assert host.runtime == "running"
    assert executor.calls == []


def test_ct110_self_update_requires_plan_approval_and_rechecks_before_rollout(
    tmp_path: Path,
) -> None:
    cfg = settings(tmp_path, vmid=110, adapter="agent_self")
    db = Database(cfg.db_path)
    host = FakeHostControl("running")
    service = OpsService(cfg, db, CompatibleExecutor(), host_control=host)  # type: ignore[arg-type]

    planned = service.create_self_update_plan(110)

    assert planned["status"] == "plan_created"
    assert planned["plan"]["status"] == "waiting_approval"
    assert planned["plan"]["payload"]["release_id"] == host.release["release_id"]
    assert db.list_jobs() == []
    state = service.get_state(110)
    assert state["operation_status"] == "waiting_approval"
    assert state["self_update_release_fingerprint"] == "a" * 64

    approved = service.approve_active(110, "approve-self-update-0001")
    same = service.approve_active(110, "approve-self-update-0001")
    assert same["job"]["id"] == approved["job"]["id"]
    assert same["job"]["operation_type"] == "self_update"
    assert approved["job"]["request_id"] == "approve-self-update-0001"

    terminal = run_queued(service, db)
    assert terminal["status"] == "success"
    assert terminal["result"]["exit_code"] == 0
    assert host.calls == [
        (
            "self_update",
            110,
            "approve-self-update-0001",
            None,
            "a" * 64,
        )
    ]


def test_ct110_self_update_fingerprint_change_blocks_before_host_mutation(
    tmp_path: Path,
) -> None:
    cfg = settings(tmp_path, vmid=110, adapter="agent_self")
    db = Database(cfg.db_path)
    host = FakeHostControl("running")
    service = OpsService(cfg, db, CompatibleExecutor(), host_control=host)  # type: ignore[arg-type]
    planned = service.create_self_update_plan(110)
    approved = service.approve_active(110, "approve-self-update-0002")
    host.release["fingerprint"] = "b" * 64
    host.release["release_id"] = "hubinet-ops-0.4.0-bbbbbbbbbbbbbbbb"

    terminal = run_queued(service, db)

    assert approved["job"]["id"] == terminal["id"]
    assert terminal["status"] == "failed"
    assert "changed before rollout" in terminal["error"]
    assert db.get_plan(planned["plan"]["id"])["status"] == "superseded"
    assert host.calls == []


def test_ct110_self_update_changed_release_cannot_be_approved(
    tmp_path: Path,
) -> None:
    cfg = settings(tmp_path, vmid=110, adapter="agent_self")
    db = Database(cfg.db_path)
    host = FakeHostControl("running")
    service = OpsService(cfg, db, CompatibleExecutor(), host_control=host)  # type: ignore[arg-type]
    planned = service.create_self_update_plan(110)
    host.release["fingerprint"] = "c" * 64
    host.release["release_id"] = "hubinet-ops-0.4.0-cccccccccccccccc"

    with pytest.raises(ValueError, match="fingerprint changed"):
        service.approve_active(110, "approve-self-update-0003")

    assert db.get_plan(planned["plan"]["id"])["status"] == "superseded"
    assert db.list_jobs() == []
    assert host.calls == []


def test_ct110_self_update_plan_can_be_rejected_without_rollout(
    tmp_path: Path,
) -> None:
    cfg = settings(tmp_path, vmid=110, adapter="agent_self")
    db = Database(cfg.db_path)
    host = FakeHostControl("running")
    service = OpsService(cfg, db, CompatibleExecutor(), host_control=host)  # type: ignore[arg-type]
    planned = service.create_self_update_plan(110)

    rejected = service.reject_active(110)

    assert rejected["plan"]["id"] == planned["plan"]["id"]
    assert rejected["plan"]["status"] == "rejected"
    assert db.list_jobs() == []
    assert host.calls == []


@pytest.mark.parametrize("terminal_plan_status", ["expired", "rejected", "completed"])
def test_terminal_self_update_plan_cannot_start_rollout(
    tmp_path: Path,
    terminal_plan_status: str,
) -> None:
    cfg = settings(tmp_path, vmid=110, adapter="agent_self")
    db = Database(cfg.db_path)
    host = FakeHostControl("running")
    service = OpsService(cfg, db, CompatibleExecutor(), host_control=host)  # type: ignore[arg-type]
    planned = service.create_self_update_plan(110)
    service.approve_active(110, f"approve-self-update-{terminal_plan_status}")
    db.update_plan_status(planned["plan"]["id"], terminal_plan_status)

    terminal = run_queued(service, db)

    assert terminal["status"] == "failed"
    assert f"status is {terminal_plan_status}, not approved" in terminal["error"]
    assert host.calls == []


def test_startup_reconciliation_marks_terminal_without_replaying(tmp_path: Path) -> None:
    cfg = settings(tmp_path)
    db = Database(cfg.db_path)
    host = FakeHostControl("running")
    service = OpsService(cfg, db, CompatibleExecutor(), host_control=host)  # type: ignore[arg-type]
    job, _ = db.create_operation_job(
        vmid=106, container_name="ct-106", operation_type="lifecycle_start",
        request_id="startup-reconcile-0001",
    )

    service._reconcile_startup_jobs()

    assert db.get_job(job["id"])["status"] == "success"
    assert host.calls == []
