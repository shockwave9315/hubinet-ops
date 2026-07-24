from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import threading
from typing import Any

import pytest

from app.config import Settings
from app.contracts import REQUIRED_APT_ACTIONS
from app.database import Database
from app.host_control import HostControlError
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
                    "version": "0.4.1",
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
        self.reattach_calls: list[
            tuple[str, int, str, str | None, str | None]
        ] = []
        self.existing_jobs: dict[str, dict[str, Any]] = {}
        self.recovery_events: list[dict[str, Any]] = []
        self.acknowledged_recovery_ids: list[str] = []
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

    def list_recovery_events(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self.recovery_events]

    def acknowledge_recovery_event(self, recovery_id: str) -> dict[str, Any]:
        self.acknowledged_recovery_ids.append(recovery_id)
        return {"recovery_id": recovery_id, "acknowledged_at": "now"}

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

    def wait_existing_job(
        self,
        operation_type: str,
        vmid: int,
        request_id: str,
        *,
        snapshot_name: str | None = None,
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
        existing = self.existing_jobs.get(request_id)
        if existing is None:
            raise HostControlError(
                "Host control job was not found; operation outcome is unknown",
                status="not_found",
            )
        status = str(existing.get("status") or "succeeded")
        result = dict(existing.get("result") or {})
        if status != "succeeded":
            raise HostControlError(
                str(existing.get("error") or "Host control job failed"),
                status=status,
                result=result,
            )
        return result


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
        "manual_snapshot_restore_allowed": True,
        "manual_rollback_allowed": adapter == "apt",
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


def test_ct110_legacy_update_rollback_remains_disabled(
    tmp_path: Path,
) -> None:
    cfg = settings(tmp_path, vmid=110, adapter="agent_self")
    db = Database(cfg.db_path)
    service = OpsService(
        cfg,
        db,
        CompatibleExecutor(),
        host_control=FakeHostControl("stopped"),  # type: ignore[arg-type]
    )

    with pytest.raises(
        ValueError,
        match="Operator action rollback is blocked by policy for resource 110",
    ):
        service.manual_rollback(110)

    assert cfg.resources[110]["manual_rollback_allowed"] is False


def test_ct110_explicit_snapshot_restore_uses_independent_policy_offline(
    tmp_path: Path,
) -> None:
    cfg = settings(tmp_path, vmid=110, adapter="agent_self")
    db = Database(cfg.db_path)
    host = FakeHostControl("stopped")
    snapshot = "hubinet-ops-110-manual-20260723T190000Z"
    host.snapshots = [
        {
            "name": snapshot,
            "created_at": "2026-07-23T19:00:00+00:00",
            "kind": "manual",
            "owned_by_hubinet_ops": True,
            "rollback_eligible": True,
            "delete_eligible": True,
        }
    ]
    executor = CompatibleExecutor()
    service = OpsService(cfg, db, executor, host_control=host)  # type: ignore[arg-type]

    queued = service.queue_snapshot_action(
        110,
        "rollback",
        snapshot,
        "ct110-explicit-snapshot-restore",
    )
    terminal = run_queued(service, db)

    assert queued["operation_type"] == "snapshot_rollback"
    assert terminal["status"] == "success"
    assert host.calls[0][:4] == (
        "snapshot_rollback",
        110,
        "ct110-explicit-snapshot-restore",
        snapshot,
    )
    assert executor.calls == []


def test_ct110_restore_is_blocked_by_waiting_plan_and_queued_self_update(
    tmp_path: Path,
) -> None:
    cfg = settings(tmp_path, vmid=110, adapter="agent_self")
    db = Database(cfg.db_path)
    host = FakeHostControl("running")
    snapshot = "hubinet-ops-110-manual-20260724T120000Z"
    host.snapshots = [
        {
            "name": snapshot,
            "created_at": "2026-07-24T12:00:00+00:00",
            "kind": "manual",
            "owned_by_hubinet_ops": True,
            "rollback_eligible": True,
            "delete_eligible": True,
        }
    ]
    service = OpsService(
        cfg, db, CompatibleExecutor(), host_control=host  # type: ignore[arg-type]
    )
    service.create_self_update_plan(110)
    with pytest.raises(ValueError, match="active update plan"):
        service.queue_snapshot_action(
            110, "rollback", snapshot, "restore-waiting-plan-0001"
        )
    approved = service.approve_active(110, "queued-self-update-request-0001")
    db.update_plan_status(approved["plan"]["id"], "completed")
    with pytest.raises(ValueError, match="destructive maintenance job"):
        service.queue_snapshot_action(
            110, "rollback", snapshot, "restore-queued-update-0001"
        )
    assert db.get_job(approved["job"]["id"])["status"] == "queued"
    assert host.calls == []


def test_restore_plan_gate_and_local_job_insert_are_atomic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = settings(tmp_path, vmid=110, adapter="agent_self")
    db = Database(cfg.db_path)
    host = FakeHostControl("running")
    snapshot = "hubinet-ops-110-manual-20260724T121000Z"
    host.snapshots = [
        {
            "name": snapshot,
            "created_at": "2026-07-24T12:10:00+00:00",
            "kind": "manual",
            "owned_by_hubinet_ops": True,
            "rollback_eligible": True,
            "delete_eligible": True,
        }
    ]
    service = OpsService(
        cfg, db, CompatibleExecutor(), host_control=host  # type: ignore[arg-type]
    )
    reached_atomic_insert = threading.Event()
    release_insert = threading.Event()
    original = db.create_operation_job

    def gated_create(**kwargs: Any) -> tuple[dict[str, Any], bool]:
        reached_atomic_insert.set()
        assert release_insert.wait(timeout=5)
        return original(**kwargs)

    monkeypatch.setattr(db, "create_operation_job", gated_create)
    outcome: list[Exception | dict[str, Any]] = []

    def queue_restore() -> None:
        try:
            outcome.append(
                service.queue_snapshot_action(
                    110,
                    "rollback",
                    snapshot,
                    "atomic-restore-gate-request-0001",
                )
            )
        except Exception as exc:  # captured for assertion in the test thread
            outcome.append(exc)

    worker = threading.Thread(target=queue_restore)
    worker.start()
    assert reached_atomic_insert.wait(timeout=5)
    db.create_plan(
        vmid=110,
        container_name="ct-110",
        fingerprint="plan-arrived-before-insert",
        risk="high",
        payload={"plan_type": "self_update"},
        ttl_minutes=60,
    )
    release_insert.set()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert len(outcome) == 1
    assert isinstance(outcome[0], ValueError)
    assert "active update plan" in str(outcome[0])
    assert db.list_jobs() == []
    assert host.calls == []


def _seed_recovery_backend(
    tmp_path: Path,
) -> tuple[Database, FakeHostControl, OpsService, dict[str, Any], dict[str, Any], dict[str, Any]]:
    cfg = settings(tmp_path, vmid=110, adapter="agent_self")
    db = Database(cfg.db_path)
    host = FakeHostControl("running")
    service = OpsService(
        cfg, db, CompatibleExecutor(), host_control=host  # type: ignore[arg-type]
    )
    approved_plan = db.create_plan(
        vmid=110,
        container_name="ct-110",
        fingerprint="approved-before-offline-recovery",
        risk="high",
        payload={"plan_type": "self_update"},
        ttl_minutes=60,
    )
    approved_plan, active_job = db.approve_plan(
        approved_plan["id"],
        request_id="active-before-recovery-0001",
        operation_type="self_update",
    )
    waiting_plan = db.create_plan(
        vmid=106,
        container_name="ct-106",
        fingerprint="waiting-before-offline-recovery",
        risk="low",
        payload={"pending_count": 1},
        ttl_minutes=60,
    )
    db.upsert_container_state(
        110,
        {
            "resource_type": "lxc",
            "adapter": "agent_self",
            "active_plan_id": approved_plan["id"],
            "active_plan_status": "approved",
            "active_job_id": active_job["id"],
            "verification_status": "passed",
            "last_verification": "2026-07-24T10:00:00+00:00",
            "apt_check_ok": True,
            "dpkg_audit_ok": True,
            "pending_updates": 7,
            "updates": {"pending_count": 7, "packages": ["pkg"]},
            "update_status": "update_available",
        },
    )
    return db, host, service, approved_plan, active_job, waiting_plan


def _recovery_event(
    *,
    recovery_id: str,
    status: str,
    operation_type: str = "offline_snapshot_restore",
    mutation_started_at: str | None = "2026-07-24T11:00:01+00:00",
) -> dict[str, Any]:
    snapshot = (
        "hubinet-ops-110-manual-20260724T100000Z"
        if operation_type == "offline_snapshot_restore"
        else None
    )
    return {
        "recovery_id": recovery_id,
        "host_job_id": "b" * 32,
        "request_id": f"offline-recovery-{recovery_id[:8]}",
        "vmid": 110,
        "snapshot_name": snapshot,
        "operation_type": operation_type,
        "started_at": "2026-07-24T11:00:00+00:00",
        "mutation_started_at": mutation_started_at,
        "status": status,
        "result": {"snapshot": snapshot, "action": "rollback"},
        "error": "hostd restarted; rollback outcome is unknown"
        if status != "succeeded"
        else None,
        "completed_at": "2026-07-24T11:05:00+00:00",
    }


def test_startup_consumes_interrupted_started_recovery_idempotently_before_any_replay(
    tmp_path: Path,
) -> None:
    class AckFailOnceHostControl(FakeHostControl):
        def __init__(self, source: FakeHostControl) -> None:
            super().__init__(source.runtime)
            self.fail_ack = True

        def acknowledge_recovery_event(self, recovery_id: str) -> dict[str, Any]:
            if self.fail_ack:
                self.fail_ack = False
                raise HostControlError("simulated crash before ACK", status="unavailable")
            return super().acknowledge_recovery_event(recovery_id)

    db, original_host, original_service, approved_plan, active_job, waiting_plan = (
        _seed_recovery_backend(tmp_path)
    )
    host = AckFailOnceHostControl(original_host)
    service = OpsService(
        original_service.settings,
        db,
        CompatibleExecutor(),
        host_control=host,  # type: ignore[arg-type]
    )
    recovery_id = "a" * 32
    snapshot = "hubinet-ops-110-manual-20260724T100000Z"
    host.recovery_events = [
        _recovery_event(recovery_id=recovery_id, status="interrupted")
    ]

    service._consume_offline_recovery_events()

    assert db.get_plan(approved_plan["id"])["status"] == "recovered"
    assert db.get_plan(waiting_plan["id"])["status"] == "superseded"
    recovered_job = db.get_job(active_job["id"])
    assert recovered_job["status"] == "interrupted"
    assert recovered_job["result"]["recovery_id"] == recovery_id
    state = service.get_state(110)
    assert state["active_plan_id"] is None
    assert state["active_job_id"] is None
    assert state["verification_status"] == "unknown"
    assert state["last_verification"] is None
    assert state["apt_check_ok"] is None
    assert state["dpkg_audit_ok"] is None
    assert state["packages_remaining_count"] is None
    assert state["pending_updates"] is None
    assert state["update_status"] == "unknown"
    assert state["last_offline_recovery_id"] == recovery_id
    assert state["last_offline_recovery_snapshot"] == snapshot
    assert state["last_offline_recovery_status"] == "interrupted"
    assert "outcome is unknown" in state["last_offline_recovery_error"]
    assert state["last_offline_recovery_mutation_started_at"] is not None
    assert host.acknowledged_recovery_ids == []
    assert host.calls == []

    service._consume_offline_recovery_events()

    assert host.acknowledged_recovery_ids == [recovery_id]
    persisted = db.get_processed_recovery_event(recovery_id)
    assert persisted["status"] == "interrupted"
    assert persisted["error"] == "hostd restarted; rollback outcome is unknown"
    assert persisted["mutation_started_at"] is not None
    assert db.get_job(active_job["id"])["result"]["recovery_id"] == recovery_id
    assert host.calls == []

    changed_contract = dict(host.recovery_events[0])
    changed_contract["mutation_started_at"] = "2026-07-24T11:00:02+00:00"
    with pytest.raises(
        ValueError,
        match="contract changed after local persistence",
    ):
        db.apply_recovery_event(changed_contract)


@pytest.mark.parametrize(
    ("status", "operation_type", "mutation_started_at", "should_invalidate"),
    [
        ("succeeded", "offline_snapshot_restore", None, True),
        (
            "failed",
            "offline_snapshot_restore",
            "2026-07-24T11:00:01+00:00",
            True,
        ),
        ("interrupted", "offline_snapshot_restore", None, False),
        (
            "interrupted",
            "offline_force_stop",
            "2026-07-24T11:00:01+00:00",
            False,
        ),
    ],
)
def test_recovery_invalidation_requires_restore_success_or_started_mutation(
    tmp_path: Path,
    status: str,
    operation_type: str,
    mutation_started_at: str | None,
    should_invalidate: bool,
) -> None:
    db, host, service, approved_plan, active_job, waiting_plan = (
        _seed_recovery_backend(tmp_path)
    )
    recovery_id = {
        ("succeeded", "offline_snapshot_restore"): "c" * 32,
        ("failed", "offline_snapshot_restore"): "d" * 32,
        ("interrupted", "offline_snapshot_restore"): "e" * 32,
        ("interrupted", "offline_force_stop"): "f" * 32,
    }[(status, operation_type)]
    host.recovery_events = [
        _recovery_event(
            recovery_id=recovery_id,
            status=status,
            operation_type=operation_type,
            mutation_started_at=mutation_started_at,
        )
    ]

    service._consume_offline_recovery_events()

    assert db.get_processed_recovery_event(recovery_id)["status"] == status
    assert host.acknowledged_recovery_ids == [recovery_id]
    assert host.calls == []
    if should_invalidate:
        assert db.get_plan(approved_plan["id"])["status"] == "recovered"
        assert db.get_plan(waiting_plan["id"])["status"] == "superseded"
        invalidated_job = db.get_job(active_job["id"])
        assert invalidated_job["status"] == "interrupted"
        assert invalidated_job["result"]["recovery_status"] == status
        state = service.get_state(110)
        assert state["verification_status"] == "unknown"
        assert state["last_offline_recovery_status"] == status
        assert state["last_offline_recovery_error"] == (
            None
            if status == "succeeded"
            else "hostd restarted; rollback outcome is unknown"
        )
    else:
        assert db.get_plan(approved_plan["id"])["status"] == "approved"
        assert db.get_plan(waiting_plan["id"])["status"] == "waiting_approval"
        assert db.get_job(active_job["id"])["status"] == "queued"
        state = service.get_state(110)
        assert state["verification_status"] == "passed"
        assert state["last_offline_recovery_id"] is None


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
    host.existing_jobs[job["request_id"]] = {
        "status": "succeeded",
        "result": {"runtime_status": "running", "lxc_status": "running"},
    }

    service._reconcile_startup_jobs()

    assert db.get_job(job["id"])["status"] == "success"
    assert host.calls == []
    assert host.reattach_calls == [
        (
            "lifecycle_start",
            106,
            "startup-reconcile-0001",
            None,
            None,
        )
    ]
