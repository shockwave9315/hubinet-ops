from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

from app.config import Settings
from app.database import Database
from app.executor import ExecutorError
from app.service import OpsService
from app.stabilization import Stabilizer


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def docker_state(healthy: int) -> dict[str, Any]:
    names = ["api", "worker", "redis"]
    return {
        "health": "healthy" if healthy == 3 else "degraded",
        "health_score": 100 if healthy == 3 else 75,
        "lxc_status": "running",
        "services": {"docker": "active", "containerd": "active"},
        "docker": {
            "enabled": True,
            "available": True,
            "required": names,
            "require_health": True,
            "containers": [
                {"name": name, "running": index < healthy, "health": "healthy" if index < healthy else "starting"}
                for index, name in enumerate(names)
            ],
        },
    }


class WorkflowExecutor:
    def __init__(self, inspect_states: list[dict[str, Any]] | None = None) -> None:
        self.inspect_states = list(inspect_states or [docker_state(3)])
        self.last = self.inspect_states[-1]
        self.actions: list[str] = []

    def run(self, action: str, vmid: int, argument=None, timeout=None, on_event=None) -> dict[str, Any]:
        self.actions.append(action)
        if action == "inspect":
            data = self.inspect_states.pop(0) if self.inspect_states else self.last
            return {"ok": True, "data": data}
        if action == "scan":
            return {"ok": True, "data": {"pending_count": 0, "packages": [], "fingerprint": "none"}}
        if action == "update" and on_event:
            on_event({"stage": "updating", "progress": 50, "message": "package", "event_type": "package_updated"})
        return {"ok": True, "data": {}}


def settings(tmp_path: Path, *, repair: bool = True, post_update_timeout: int = 1) -> Settings:
    return Settings(
        raw={
            "scheduler": {"enabled": False, "approval_ttl_minutes": 60},
            "home_assistant": {},
            "mqtt": {"enabled": False},
            "containers": {
                106: {
                    "name": "weather",
                    "enabled": True,
                    "adapter": "apt",
                    "criticality": "low",
                    "automatic_rollback": True,
                    "manual_rollback_allowed": True,
                    "repair_actions": ["restart_services"] if repair else [],
                    "dashboard_path": "/hubinet-ops/ct-106",
                    "stabilization": {
                        "post_update_timeout_seconds": post_update_timeout,
                        "post_rollback_timeout_seconds": 3,
                        "repair_timeout_seconds": 1,
                        "poll_interval_seconds": 1,
                        "initial_grace_seconds": 0,
                        "required_consecutive_successes": 2,
                    },
                }
            },
        },
        config_path=tmp_path / "config.yaml",
        db_path=tmp_path / "ops.db",
        api_token="x" * 64,
    )


def service_with(
    tmp_path: Path,
    executor: WorkflowExecutor,
    *,
    repair: bool = True,
    post_update_timeout: int = 1,
) -> tuple[OpsService, Database]:
    cfg = settings(tmp_path, repair=repair, post_update_timeout=post_update_timeout)
    db = Database(cfg.db_path)
    clock = FakeClock()
    stabilizer = Stabilizer(executor, threading.Event(), monotonic=clock.monotonic, sleep=clock.sleep)
    service = OpsService(cfg, db, executor, stabilizer=stabilizer)  # type: ignore[arg-type]
    service._ensure_initial_states()
    return service, db


def approved_job(service: OpsService, db: Database) -> dict[str, Any]:
    result = service.scan_container(106)
    if result["status"] == "up_to_date":
        plan = db.create_plan(
            vmid=106,
            container_name="weather",
            fingerprint="updates",
            risk="high",
            payload={"pending_count": 3},
            ttl_minutes=60,
        )
        service.approve(plan["id"])
    job = db.next_queued_job()
    assert job is not None
    return job


def test_refresh_preserves_failed_operation_state(tmp_path: Path) -> None:
    executor = WorkflowExecutor([docker_state(3)])
    service, _ = service_with(tmp_path, executor)
    state = service.get_state(106)
    state.update({"operation_status": "failed", "last_operation_result": "rolled_back", "health_status": "critical"})
    service._save_state(106, state)
    refreshed = service.refresh_container(106)
    assert refreshed["health_status"] == "healthy"
    assert refreshed["operation_status"] == "failed"
    assert refreshed["last_operation_result"] == "rolled_back"


def test_update_0_2_3_3_succeeds_without_repair_or_rollback(tmp_path: Path) -> None:
    executor = WorkflowExecutor([docker_state(0), docker_state(2), docker_state(3), docker_state(3)])
    service, db = service_with(tmp_path, executor, post_update_timeout=3)
    job = approved_job(service, db)
    service._run_job(job)
    result = db.get_job(job["id"])
    assert result["status"] == "success"
    assert "repair" not in executor.actions
    assert "rollback" not in executor.actions
    assert service.get_state(106)["update_status"] == "up_to_date"


def test_timeout_invokes_repair_and_repair_success_prevents_rollback(tmp_path: Path) -> None:
    executor = WorkflowExecutor([docker_state(0), docker_state(0), docker_state(3), docker_state(3)])
    service, db = service_with(tmp_path, executor)
    job = approved_job(service, db)
    service._run_job(job)
    assert db.get_job(job["id"])["status"] == "success"
    assert "repair" in executor.actions
    assert "rollback" not in executor.actions


def test_repair_failure_invokes_rollback_and_waits_for_0_3_3(tmp_path: Path) -> None:
    states = [
        docker_state(0), docker_state(0),
        docker_state(0), docker_state(0),
        docker_state(0), docker_state(3), docker_state(3),
    ]
    executor = WorkflowExecutor(states)
    service, db = service_with(tmp_path, executor)
    job = approved_job(service, db)
    service._run_job(job)
    assert db.get_job(job["id"])["status"] == "rolled_back"
    assert "repair" in executor.actions
    assert "rollback" in executor.actions
    assert service.get_state(106)["last_operation_result"] == "rolled_back"


def test_rollback_timeout_becomes_manual_intervention(tmp_path: Path) -> None:
    executor = WorkflowExecutor([docker_state(0)] * 12)
    service, db = service_with(tmp_path, executor)
    job = approved_job(service, db)
    service._run_job(job)
    assert db.get_job(job["id"])["status"] == "failed"
    state = service.get_state(106)
    assert state["operation_status"] == "manual_intervention"
    assert state["last_operation_result"] == "manual_intervention"


def test_scan_creates_waiting_approval_without_direct_update(tmp_path: Path) -> None:
    class ScanExecutor(WorkflowExecutor):
        def run(self, action: str, vmid: int, argument=None, timeout=None, on_event=None) -> dict[str, Any]:
            self.actions.append(action)
            if action == "scan":
                return {"ok": True, "data": {"pending_count": 2, "packages": [{"name": "systemd"}], "fingerprint": "fp"}}
            return super().run(action, vmid, argument, timeout, on_event)

    executor = ScanExecutor()
    service, _ = service_with(tmp_path, executor)
    result = service.scan_container(106)
    assert result["status"] == "plan_created"
    assert service.get_state(106)["operation_status"] == "waiting_approval"
    assert "update" not in executor.actions


def test_manual_rollback_requires_policy_and_failed_snapshot(tmp_path: Path) -> None:
    executor = WorkflowExecutor([docker_state(0), docker_state(3), docker_state(3)])
    service, db = service_with(tmp_path, executor)
    plan = db.create_plan(
        vmid=106, container_name="weather", fingerprint="fp", risk="high", payload={}, ttl_minutes=60
    )
    _, source = db.approve_plan(plan["id"])
    db.update_job(source["id"], status="failed", stage="failed", progress=100, snapshot_name="snap-safe")
    result = service.manual_rollback(106)
    assert result["status"] == "rolled_back"
    assert "rollback" in executor.actions

    service.settings.raw["containers"][106]["manual_rollback_allowed"] = False
    with pytest.raises(ValueError, match="not allowed"):
        service.manual_rollback(106)


def test_notification_uses_configured_dashboard_path(tmp_path: Path) -> None:
    service, _ = service_with(tmp_path, WorkflowExecutor())
    payload = service._notification("approval_required", 106, pending_count=80, risk="high")
    assert payload["dashboard_path"] == "/hubinet-ops/ct-106"
    assert set(payload) == {"event_type", "vmid", "container", "dashboard_path", "pending_count", "risk"}


def test_post_health_scan_failure_does_not_trigger_rollback(tmp_path: Path) -> None:
    class PostScanFailureExecutor(WorkflowExecutor):
        def __init__(self) -> None:
            super().__init__([docker_state(3), docker_state(3)])
            self.scan_count = 0

        def run(self, action: str, vmid: int, argument=None, timeout=None, on_event=None) -> dict[str, Any]:
            if action == "scan":
                self.scan_count += 1
                if self.scan_count > 1:
                    raise ExecutorError("post scan unavailable")
            return super().run(action, vmid, argument, timeout, on_event)

    executor = PostScanFailureExecutor()
    service, db = service_with(tmp_path, executor, post_update_timeout=2)
    job = approved_job(service, db)
    service._run_job(job)
    assert db.get_job(job["id"])["status"] == "success"
    assert "rollback" not in executor.actions
    state = service.get_state(106)
    assert state["update_status"] == "unknown"
    assert state["last_error"] == "post scan unavailable"
