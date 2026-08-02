from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

from app.config import Settings
from app.contracts import REQUIRED_APT_ACTIONS
from app.database import Database
from app.executor import ExecutorError
from app.service import OpsService


def settings(tmp_path: Path) -> Settings:
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
                    "automatic_rollback": False,
                    "manual_rollback_allowed": False,
                    "operator_capabilities": {
                        "refresh": True,
                        "scan": True,
                        "approve": True,
                        "reject": True,
                        "retry_healthcheck": True,
                        "rollback": True,
                        "start": True,
                        "shutdown": True,
                        "reboot": True,
                    },
                    "recovery_scan": {
                        "enabled": True,
                        "delay_seconds": 90,
                        "cooldown_seconds": 900,
                    },
                    "repair_actions": [],
                    "executor_contract": {
                        "executor_sha256": "a" * 64,
                        "profile_sha256": "b" * 64,
                    },
                    "dashboard_path": "/hubinet-ops/ct-106",
                    "stabilization": {
                        "post_update_timeout_seconds": 1,
                        "post_rollback_timeout_seconds": 1,
                        "repair_timeout_seconds": 1,
                        "poll_interval_seconds": 1,
                        "initial_grace_seconds": 0,
                        "required_consecutive_successes": 1,
                    },
                }
            },
        },
        config_path=tmp_path / "config.yaml",
        db_path=tmp_path / "ops.db",
        api_token="x" * 64,
    )


class Executor:
    def __init__(self, *, changed_plan: bool = False) -> None:
        self.changed_plan = changed_plan

    def run(self, action: str, vmid: int, argument=None, timeout=None, on_event=None):
        if action == "capabilities":
            return {
                "ok": True,
                "data": {
                    "version": "0.4.3",
                    "protocol_version": 1,
                    "supported_actions": sorted(REQUIRED_APT_ACTIONS),
                    "executor_sha256": "a" * 64,
                    "profile_sha256": "b" * 64,
                    "profile_validation_status": "valid",
                },
            }
        if action == "inspect":
            return {
                "ok": True,
                "data": {
                    "health": "healthy",
                    "health_score": 100,
                    "lxc_status": "running",
                    "services": {},
                    "docker": {"enabled": False},
                },
            }
        if action == "preflight":
            return {
                "ok": True,
                "data": {
                    "updates": {
                        "pending_count": 1,
                        "packages": [{"name": "systemd"}],
                        "fingerprint": "changed" if self.changed_plan else "approved",
                    }
                },
            }
        if action == "scan":
            return {
                "ok": True,
                "data": {"pending_count": 0, "packages": [], "fingerprint": "none"},
            }
        if action == "verify":
            return {
                "ok": True,
                "data": {
                    "apt_check_ok": True,
                    "dpkg_audit_ok": True,
                    "reboot_required": False,
                    "updates": {"pending_count": 0, "packages": [], "fingerprint": "none"},
                },
            }
        return {"ok": True, "data": {}}


def make_service(tmp_path: Path, executor: Any) -> tuple[OpsService, Database]:
    cfg = settings(tmp_path)
    db = Database(cfg.db_path)
    service = OpsService(cfg, db, executor)
    service._ensure_initial_states()
    return service, db


def create_approved_job(service: OpsService, db: Database) -> tuple[dict, dict]:
    plan = db.create_plan(
        vmid=106,
        container_name="weather",
        fingerprint="approved",
        risk="high",
        payload={"pending_count": 1},
        ttl_minutes=60,
    )
    service.approve(plan["id"])
    job = db.next_queued_job()
    assert job is not None
    return plan, job


def test_successful_refresh_preserves_terminal_operation_error(tmp_path: Path) -> None:
    service, _ = make_service(tmp_path, Executor())
    state = service.get_state(106)
    state.update(
        {
            "health_status": "critical",
            "operation_status": "rolled_back",
            "last_operation_result": "rolled_back",
            "last_error": "update failed before rollback",
        }
    )
    service._save_state(106, state)

    refreshed = service.refresh_container(106)
    assert refreshed["health_status"] == "healthy"
    assert refreshed["operation_status"] == "rolled_back"
    assert refreshed["last_error"] == "update failed before rollback"


@pytest.mark.parametrize(
    "last_operation_result",
    ["failed", "interrupted", "manual_intervention"],
)
def test_successful_refresh_preserves_other_terminal_operation_errors(
    tmp_path: Path,
    last_operation_result: str,
) -> None:
    service, _ = make_service(tmp_path, Executor())
    state = service.get_state(106)
    state.update(
        {
            "last_operation_result": last_operation_result,
            "last_error": f"terminal {last_operation_result} error",
        }
    )
    service._save_state(106, state)

    refreshed = service.refresh_container(106)

    assert refreshed["last_operation_result"] == last_operation_result
    assert refreshed["last_error"] == f"terminal {last_operation_result} error"


@pytest.mark.parametrize("last_operation_result", [None, "success"])
def test_successful_refresh_clears_stale_transient_error(
    tmp_path: Path,
    last_operation_result: str | None,
) -> None:
    service, db = make_service(tmp_path, Executor())
    stale_error = (
        "Host executor returned no valid JSON result (rc=255); ".ljust(260, "x")
    )
    assert len(stale_error) == 260
    state = service.get_state(106)
    state.update(
        {
            "health_status": "healthy",
            "executor_compatible": True,
            "last_operation_result": last_operation_result,
            "last_error": stale_error,
            "last_job_id": "job-history-is-unchanged",
            "last_job_event": {
                "event_type": "completed",
                "message": "event-history-is-unchanged",
            },
        }
    )
    service._save_state(106, state)

    refreshed = service.refresh_container(106)

    assert refreshed["health_status"] == "healthy"
    assert refreshed["last_operation_result"] == last_operation_result
    assert refreshed["last_error"] is None
    assert refreshed["last_job_id"] == "job-history-is-unchanged"
    assert refreshed["last_job_event"]["message"] == "event-history-is-unchanged"
    persisted = db.get_resource_state(106)
    assert persisted is not None
    assert persisted["last_error"] is None


def test_transient_refresh_error_is_published_then_replaced_after_recovery(
    tmp_path: Path,
) -> None:
    class FailInspectOnce(Executor):
        def __init__(self) -> None:
            super().__init__()
            self.failed = False

        def run(self, action: str, vmid: int, argument=None, timeout=None, on_event=None):
            if action == "inspect" and not self.failed:
                self.failed = True
                raise ExecutorError("temporary SSH probe failure")
            return super().run(action, vmid, argument, timeout, on_event)

    class RecordingMqtt:
        availability = "online"

        def __init__(self) -> None:
            self.states: list[dict[str, Any]] = []

        def publish_resource_state(self, vmid: int, state: dict[str, Any]) -> None:
            assert vmid == 106
            self.states.append(dict(state))

        def publish_agent_state(self, state: dict[str, Any]) -> None:
            pass

    service, _ = make_service(tmp_path, FailInspectOnce())
    mqtt = RecordingMqtt()
    service.mqtt = mqtt

    failed = service.refresh_container(106)
    recovered = service.refresh_container(106)

    assert "temporary SSH probe failure" in failed["last_error"]
    assert recovered["last_error"] is None
    assert mqtt.states[-2]["last_error"] == failed["last_error"]
    assert mqtt.states[-1]["last_error"] is None


def test_scan_preserves_previous_terminal_error(tmp_path: Path) -> None:
    service, _ = make_service(tmp_path, Executor())
    state = service.get_state(106)
    state.update(
        {
            "operation_status": "rolled_back",
            "last_operation_result": "rolled_back",
            "last_error": "update failed before rollback",
        }
    )
    service._save_state(106, state)

    result = service.scan_container(106)
    assert result["status"] == "up_to_date"
    scanned = service.get_state(106)
    assert scanned["last_operation_result"] == "rolled_back"
    assert scanned["last_error"] == "update failed before rollback"


def test_blocked_job_clears_active_plan_and_exposes_final_plan_status(tmp_path: Path) -> None:
    service, db = make_service(tmp_path, Executor(changed_plan=True))
    plan, job = create_approved_job(service, db)
    service._run_job(job)

    state = service.get_state(106)
    assert db.get_plan(plan["id"])["status"] == "blocked"
    assert state["active_plan_id"] is None
    assert state["active_plan_status"] == "blocked"
    assert state["operation_status"] == "failed"


def test_approve_is_rejected_while_same_container_scan_is_running(tmp_path: Path) -> None:
    entered = threading.Event()
    release = threading.Event()

    class BlockingExecutor(Executor):
        def run(self, action: str, vmid: int, argument=None, timeout=None, on_event=None):
            if action == "scan":
                entered.set()
                assert release.wait(timeout=2)
            return super().run(action, vmid, argument, timeout, on_event)

    service, db = make_service(tmp_path, BlockingExecutor())
    plan = db.create_plan(
        vmid=106,
        container_name="weather",
        fingerprint="approved",
        risk="high",
        payload={"pending_count": 1},
        ttl_minutes=60,
    )
    thread = threading.Thread(target=service.scan_container, args=(106,))
    thread.start()
    assert entered.wait(timeout=2)
    try:
        with pytest.raises(ValueError, match="scan is running"):
            service.approve(plan["id"])
    finally:
        release.set()
        thread.join(timeout=2)


def test_retry_healthcheck_is_rejected_while_job_is_active(tmp_path: Path) -> None:
    service, db = make_service(tmp_path, Executor())
    plan = db.create_plan(
        vmid=106,
        container_name="weather",
        fingerprint="approved",
        risk="high",
        payload={},
        ttl_minutes=60,
    )
    service.approve(plan["id"])
    with pytest.raises(ValueError, match="already active"):
        service.retry_healthcheck(106)


def test_retry_healthcheck_requires_failed_previous_operation(tmp_path: Path) -> None:
    service, db = make_service(tmp_path, Executor())
    plan = db.create_plan(
        vmid=106,
        container_name="weather",
        fingerprint="approved",
        risk="high",
        payload={},
        ttl_minutes=60,
    )
    _, job = db.approve_plan(plan["id"])
    db.update_job(job["id"], status="success", stage="completed", progress=100)
    with pytest.raises(ValueError, match="only allowed after a failed operation"):
        service.retry_healthcheck(106)
