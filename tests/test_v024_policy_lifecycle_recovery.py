from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.database import Database
from app.executor import ExecutorError
from app.service import OpsService


CAPABILITIES = {
    "refresh": True,
    "scan": True,
    "approve": True,
    "reject": True,
    "retry_healthcheck": True,
    "rollback": True,
    "start": True,
    "shutdown": True,
    "reboot": True,
}


def settings(tmp_path: Path) -> Settings:
    return Settings(
        raw={
            "scheduler": {"enabled": False, "approval_ttl_minutes": 60},
            "mqtt": {"enabled": False},
            "home_assistant": {},
            "containers": {
                101: {
                    "name": "cloudflared",
                    "enabled": True,
                    "adapter": "apt",
                    "operator_capabilities": {key: False for key in CAPABILITIES},
                    "recovery_scan": {
                        "enabled": False,
                        "delay_seconds": 90,
                        "cooldown_seconds": 900,
                    },
                    "repair_actions": [],
                },
                106: {
                    "name": "weather",
                    "enabled": True,
                    "adapter": "apt",
                    "manual_rollback_allowed": True,
                    "operator_capabilities": dict(CAPABILITIES),
                    "recovery_scan": {
                        "enabled": True,
                        "delay_seconds": 90,
                        "cooldown_seconds": 900,
                    },
                    "repair_actions": [],
                },
            },
        },
        config_path=tmp_path / "config.yaml",
        db_path=tmp_path / "ops.db",
        api_token="t" * 64,
    )


class FakeExecutor:
    def __init__(self) -> None:
        self.actions: list[tuple[str, int, Any]] = []
        self.statuses: list[str] = []
        self.inspect_states: dict[int, list[dict[str, Any]]] = {}
        self.scan_data = {
            "pending_count": 1,
            "packages": [{"name": "curl", "current": "1", "target": "2"}],
            "fingerprint": "recovery-fingerprint",
        }

    def run(
        self,
        action: str,
        vmid: int,
        argument: Any = None,
        timeout: Any = None,
        on_event: Any = None,
    ) -> dict[str, Any]:
        self.actions.append((action, vmid, argument))
        if action == "status":
            return {"ok": True, "data": {"status": self.statuses.pop(0)}}
        if action == "inspect":
            values = self.inspect_states.setdefault(vmid, [])
            return {"ok": True, "data": values.pop(0)}
        if action == "scan":
            return {"ok": True, "data": dict(self.scan_data)}
        return {"ok": True, "data": {}}


class Clock:
    def __init__(self) -> None:
        self.monotonic_value = 0.0
        self.wall = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)

    def monotonic(self) -> float:
        return self.monotonic_value

    def now(self) -> datetime:
        return self.wall

    def advance(self, seconds: float) -> None:
        self.monotonic_value += seconds
        self.wall += timedelta(seconds=seconds)


def make_service(
    tmp_path: Path,
    executor: FakeExecutor,
    clock: Clock | None = None,
) -> tuple[OpsService, Database]:
    cfg = settings(tmp_path)
    db = Database(cfg.db_path)
    clock = clock or Clock()
    service = OpsService(
        cfg,
        db,
        executor,  # type: ignore[arg-type]
        monotonic=clock.monotonic,
        now=clock.now,
    )
    service._ensure_initial_states()
    return service, db


def test_lifecycle_uses_only_fixed_verbs_and_records_terminal_state(tmp_path: Path) -> None:
    executor = FakeExecutor()
    executor.statuses = ["stopped", "running"]
    service, _ = make_service(tmp_path, executor)

    state = service.lifecycle_container(106, "start")

    assert [action for action, _, _ in executor.actions] == ["status", "start", "status"]
    assert state["lifecycle_action"] == "start"
    assert state["lifecycle_status"] == "success"
    assert state["lxc_status"] == "running"
    assert state["lifecycle_started_at"]
    assert state["lifecycle_finished_at"]
    assert state["lifecycle_error"] is None
    with pytest.raises(ValueError, match="Unsupported"):
        service.lifecycle_container(106, "destroy")


@pytest.mark.parametrize(
    ("action", "before", "after"),
    [
        ("shutdown", "running", "stopped"),
        ("reboot", "running", "running"),
    ],
)
def test_ct106_graceful_lifecycle_actions_are_allowed(
    tmp_path: Path,
    action: str,
    before: str,
    after: str,
) -> None:
    executor = FakeExecutor()
    executor.statuses = [before, after]
    service, _ = make_service(tmp_path, executor)
    state = service.lifecycle_container(106, action)
    assert state["lifecycle_action"] == action
    assert state["lifecycle_status"] == "success"
    assert [item[0] for item in executor.actions] == ["status", action, "status"]


def test_lifecycle_failure_is_persisted_and_notified(tmp_path: Path) -> None:
    class FailedExecutor(FakeExecutor):
        def run(self, action: str, vmid: int, argument=None, timeout=None, on_event=None):
            if action == "shutdown":
                self.actions.append((action, vmid, argument))
                raise ExecutorError("graceful shutdown timed out")
            return super().run(action, vmid, argument, timeout, on_event)

    executor = FailedExecutor()
    executor.statuses = ["running"]
    service, _ = make_service(tmp_path, executor)
    notifications: list[dict[str, Any]] = []
    service._notify_ha = notifications.append  # type: ignore[method-assign]
    with pytest.raises(ValueError, match="timed out"):
        service.lifecycle_container(106, "shutdown")
    state = service.get_state(106)
    assert state["lifecycle_status"] == "failed"
    assert state["lifecycle_error"] == "graceful shutdown timed out"
    assert state["operation_status"] == "failed"
    assert notifications[-1]["event_type"] == "lifecycle_failed"
    assert notifications[-1]["action"] == "shutdown"


def test_lifecycle_policy_and_conflicts_are_backend_enforced(tmp_path: Path) -> None:
    executor = FakeExecutor()
    service, db = make_service(tmp_path, executor)

    for action in ("start", "shutdown", "reboot"):
        with pytest.raises(ValueError, match="blocked by policy"):
            service.lifecycle_container(101, action)
    assert executor.actions == []

    plan = db.create_plan(
        vmid=106,
        container_name="weather",
        fingerprint="fp",
        risk="low",
        payload={},
        ttl_minutes=60,
    )
    db.approve_plan(plan["id"])
    with pytest.raises(ValueError, match="job is already active"):
        service.lifecycle_container(106, "reboot")

    lock = service._scan_locks[106]
    assert lock.acquire(blocking=False)
    try:
        with pytest.raises(ValueError, match="scan or lifecycle"):
            service.lifecycle_container(106, "shutdown")
    finally:
        lock.release()


def test_ct101_internal_telemetry_refresh_bypasses_operator_deny_only(
    tmp_path: Path,
) -> None:
    executor = FakeExecutor()
    executor.inspect_states[101] = [healthy()]
    service, _ = make_service(tmp_path, executor)
    assert service.refresh_container(101)["health_status"] == "healthy"
    assert ("inspect", 101, None) in executor.actions
    with pytest.raises(ValueError, match="blocked by policy"):
        service.refresh_container(101, operator=True)


def test_api_requires_auth_denies_ct101_and_never_executes_invalid_vmid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import_config = tmp_path / "import.yaml"
    import_config.write_text(
        "containers:\n  106:\n    enabled: true\nmqtt:\n  enabled: false\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HUBINET_OPS_CONFIG", str(import_config))
    monkeypatch.setenv("HUBINET_OPS_DB", str(tmp_path / "import.db"))
    monkeypatch.setenv("HUBINET_OPS_API_TOKEN", "i" * 64)
    main = importlib.import_module("app.main")

    executor = FakeExecutor()
    executor.statuses = ["stopped", "running"]
    cfg = settings(tmp_path)
    db = Database(cfg.db_path)
    client = TestClient(main.create_app(cfg, database=db, executor=executor))
    headers = {"Authorization": f"Bearer {cfg.api_token}"}

    assert client.post("/api/v1/containers/106/start").status_code == 401
    for path in ("start", "shutdown", "reboot", "scan"):
        assert client.post(f"/api/v1/containers/101/{path}", headers=headers).status_code == 409
    plan = db.create_plan(
        vmid=101,
        container_name="cloudflared",
        fingerprint="f",
        risk="low",
        payload={},
        ttl_minutes=60,
    )
    assert client.post(
        "/api/v1/plans/approve",
        headers=headers,
        json={"plan_id": plan["id"]},
    ).status_code == 409

    before = list(executor.actions)
    assert client.post("/api/v1/containers/999/start", headers=headers).status_code == 404
    assert executor.actions == before

    lock = client.app.state.service._scan_locks[106]
    assert lock.acquire(blocking=False)
    try:
        assert client.post("/api/v1/containers/106/start", headers=headers).status_code == 409
    finally:
        lock.release()
    assert client.post("/api/v1/containers/106/start", headers=headers).status_code == 200

    active = db.create_plan(
        vmid=106,
        container_name="weather",
        fingerprint="active",
        risk="low",
        payload={},
        ttl_minutes=60,
    )
    db.approve_plan(active["id"])
    assert client.post("/api/v1/containers/106/reboot", headers=headers).status_code == 409


def unhealthy(health: str) -> dict[str, Any]:
    return {
        "health_status": health,
        "health": health,
        "health_score": 30,
        "lxc_status": "running" if health != "offline" else "stopped",
    }


def healthy() -> dict[str, Any]:
    return {
        "health_status": "healthy",
        "health": "healthy",
        "health_score": 100,
        "lxc_status": "running",
    }


@pytest.mark.parametrize("previous", ["offline", "degraded", "critical"])
def test_recovery_transition_scans_once_after_fake_delay(
    tmp_path: Path,
    previous: str,
) -> None:
    executor = FakeExecutor()
    executor.inspect_states[106] = [unhealthy(previous), healthy()]
    clock = Clock()
    service, _ = make_service(tmp_path, executor, clock)

    service.refresh_container(106)
    service.refresh_container(106)
    assert service.get_state(106)["recovery_scan_status"] == "scheduled"
    assert not any(action == "scan" for action, _, _ in executor.actions)

    clock.advance(89)
    service._run_due_recovery_scans(clock.monotonic())
    assert not any(action == "scan" for action, _, _ in executor.actions)
    clock.advance(1)
    service._run_due_recovery_scans(clock.monotonic())

    assert sum(action == "scan" for action, _, _ in executor.actions) == 1
    assert "update" not in [action for action, _, _ in executor.actions]
    state = service.get_state(106)
    assert state["last_recovery_scan"]
    assert state["last_recovery_scan_result"] == "plan_created"


def test_startup_healthy_and_healthy_to_healthy_do_not_schedule(tmp_path: Path) -> None:
    executor = FakeExecutor()
    executor.inspect_states[106] = [healthy(), healthy()]
    service, _ = make_service(tmp_path, executor)
    service.refresh_container(106)
    service.refresh_container(106)
    assert service._recovery_due == {}
    assert not any(action == "scan" for action, _, _ in executor.actions)


def test_recovery_is_cancelled_when_health_drops_during_delay(tmp_path: Path) -> None:
    executor = FakeExecutor()
    executor.inspect_states[106] = [unhealthy("offline"), healthy(), unhealthy("degraded")]
    clock = Clock()
    service, _ = make_service(tmp_path, executor, clock)
    service.refresh_container(106)
    service.refresh_container(106)
    service.refresh_container(106)
    clock.advance(90)
    service._run_due_recovery_scans(clock.monotonic())
    assert service.get_state(106)["recovery_scan_status"] == "cancelled"
    assert not any(action == "scan" for action, _, _ in executor.actions)


def test_recovery_active_plan_job_cooldown_and_ct101_policy_block(tmp_path: Path) -> None:
    executor = FakeExecutor()
    executor.inspect_states[106] = [unhealthy("offline"), healthy()]
    executor.inspect_states[101] = [unhealthy("offline"), healthy()]
    clock = Clock()
    service, db = make_service(tmp_path, executor, clock)

    service.refresh_container(101)
    service.refresh_container(101)
    assert 101 not in service._recovery_due

    service.refresh_container(106)
    service.refresh_container(106)
    plan = db.create_plan(
        vmid=106,
        container_name="weather",
        fingerprint="existing",
        risk="low",
        payload={},
        ttl_minutes=60,
    )
    clock.advance(90)
    service._run_due_recovery_scans(clock.monotonic())
    assert service.get_state(106)["last_recovery_scan_result"] == "plan_active"
    assert not any(action == "scan" for action, _, _ in executor.actions)

    db.reject_plan(plan["id"])
    state = service.get_state(106)
    state["last_recovery_scan"] = clock.now().isoformat()
    service._save_state(106, state)
    service._schedule_recovery_scan(106)
    clock.advance(90)
    service._run_due_recovery_scans(clock.monotonic())
    assert service.get_state(106)["last_recovery_scan_result"] == "cooldown_active"


def test_recovery_scan_is_blocked_by_active_job(tmp_path: Path) -> None:
    executor = FakeExecutor()
    executor.inspect_states[106] = [unhealthy("degraded"), healthy()]
    clock = Clock()
    service, db = make_service(tmp_path, executor, clock)
    service.refresh_container(106)
    service.refresh_container(106)
    plan = db.create_plan(
        vmid=106,
        container_name="weather",
        fingerprint="approved",
        risk="low",
        payload={},
        ttl_minutes=60,
    )
    db.approve_plan(plan["id"])
    clock.advance(90)
    service._run_due_recovery_scans(clock.monotonic())
    assert service.get_state(106)["last_recovery_scan_result"] == "job_active"
    assert not any(action == "scan" for action, _, _ in executor.actions)


def test_agent_restart_reconciles_lifecycle_and_pending_recovery(tmp_path: Path) -> None:
    cfg = settings(tmp_path)
    db = Database(cfg.db_path)
    db.upsert_container_state(
        106,
        {
            "vmid": 106,
            "health_status": "healthy",
            "lxc_status": "running",
            "operation_status": "running",
            "job_stage": "rebooting",
            "lifecycle_action": "reboot",
            "lifecycle_status": "running",
            "recovery_scan_status": "scheduled",
            "recovery_scan_due_at": "2026-07-19T12:01:30+00:00",
        },
    )
    clock = Clock()
    service = OpsService(
        cfg,
        db,
        FakeExecutor(),  # type: ignore[arg-type]
        monotonic=clock.monotonic,
        now=clock.now,
    )
    service._ensure_initial_states()
    state = service.get_state(106)
    assert state["lifecycle_status"] == "failed"
    assert state["operation_status"] == "failed"
    assert "restarted" in state["lifecycle_error"]
    assert state["recovery_scan_status"] == "cancelled"
    assert state["recovery_scan_due_at"] is None
    assert state["last_recovery_scan_result"] == "agent_restarted"
