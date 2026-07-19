from __future__ import annotations

import threading
from datetime import datetime
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
                {
                    "name": name,
                    "running": index < healthy,
                    "health": "healthy" if index < healthy else "starting",
                }
                for index, name in enumerate(names)
            ],
        },
    }


class WorkflowExecutor:
    def __init__(
        self,
        inspect_states: list[dict[str, Any]] | None = None,
        *,
        preflight_fingerprint: str = "updates",
    ) -> None:
        self.inspect_states = list(inspect_states or [docker_state(3)])
        self.last = self.inspect_states[-1]
        self.actions: list[str] = []
        self.preflight_fingerprint = preflight_fingerprint

    def run(
        self,
        action: str,
        vmid: int,
        argument=None,
        timeout=None,
        on_event=None,
    ) -> dict[str, Any]:
        self.actions.append(action)
        if action == "inspect":
            data = self.inspect_states.pop(0) if self.inspect_states else self.last
            return {"ok": True, "data": data}
        if action == "preflight":
            return {
                "ok": True,
                "data": {
                    "updates": {
                        "pending_count": 3,
                        "packages": [{"name": "systemd"}],
                        "fingerprint": self.preflight_fingerprint,
                    }
                },
            }
        if action == "scan":
            return {
                "ok": True,
                "data": {
                    "pending_count": 0,
                    "packages": [],
                    "fingerprint": "none",
                },
            }
        if action == "verify":
            return {
                "ok": True,
                "data": {
                    "apt_check_ok": True,
                    "dpkg_audit_ok": True,
                    "reboot_required": False,
                    "updates": {
                        "pending_count": 0,
                        "packages": [],
                        "fingerprint": "none",
                    },
                    "docker": {"required_healthy": 3, "required_total": 3},
                },
            }
        if action == "update" and on_event:
            on_event(
                {
                    "stage": "updating",
                    "progress": 50,
                    "message": "package",
                    "event_type": "package_updated",
                }
            )
        return {"ok": True, "data": {}}


def settings(
    tmp_path: Path,
    *,
    repair: bool = True,
    post_update_timeout: int = 1,
) -> Settings:
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


def observation_settings(tmp_path: Path) -> Settings:
    denied = {
        name: False
        for name in (
            "refresh", "scan", "approve", "reject", "retry_healthcheck",
            "rollback", "start", "shutdown", "reboot",
        )
    }
    return Settings(
        raw={
            "scheduler": {"enabled": False, "approval_ttl_minutes": 60},
            "home_assistant": {},
            "mqtt": {"enabled": False},
            "resources": {
                101: {
                    "name": "cloudflared",
                    "resource_type": "lxc",
                    "adapter": "apt",
                    "enabled": True,
                    "monitoring": {"inspect": True, "update_scan": True},
                    "operator_capabilities": denied,
                }
            },
        },
        config_path=tmp_path / "config.yaml",
        db_path=tmp_path / "observation.db",
        api_token="t" * 64,
    )


def test_observation_only_monitoring_is_independent_from_operator_policy(
    tmp_path: Path,
) -> None:
    executor = WorkflowExecutor()
    cfg = observation_settings(tmp_path)
    db = Database(cfg.db_path)
    service = OpsService(cfg, db, executor)

    refreshed = service.refresh_container(101, operator=False)
    scanned = service.scan_container(101, operator=False, source="scheduler")

    assert refreshed["health_status"] == "healthy"
    assert scanned["status"] == "up_to_date"
    assert executor.actions == ["inspect", "scan"]
    assert db.find_active_plan(101) is None
    with pytest.raises(ValueError, match="refresh.*blocked"):
        service.refresh_container(101, operator=True)
    with pytest.raises(ValueError, match="scan.*blocked"):
        service.scan_container(101, operator=True)


def test_observation_scan_reports_updates_without_creating_unapprovable_plan(
    tmp_path: Path,
) -> None:
    class PendingExecutor(WorkflowExecutor):
        def run(self, action: str, vmid: int, **kwargs: Any) -> dict[str, Any]:
            if action == "scan":
                self.actions.append(action)
                return {
                    "ok": True,
                    "data": {
                        "pending_count": 4,
                        "packages": [{"name": "openssl"}],
                        "fingerprint": "observed",
                    },
                }
            return super().run(action, vmid, **kwargs)

    executor = PendingExecutor()
    cfg = observation_settings(tmp_path)
    db = Database(cfg.db_path)
    service = OpsService(cfg, db, executor)

    result = service.scan_container(101, operator=False, source="scheduler")
    state = service.get_state(101)

    assert result["status"] == "updates_observed"
    assert state["pending_updates"] == 4
    assert state["operation_status"] == "idle"
    assert state["active_plan_id"] is None
    assert db.find_active_plan(101) is None


def test_agent_self_state_adds_inventory_jobs_and_mqtt_without_secrets(
    tmp_path: Path,
) -> None:
    denied = {
        name: False
        for name in (
            "refresh", "scan", "approve", "reject", "retry_healthcheck",
            "rollback", "start", "shutdown", "reboot",
        )
    }
    cfg = Settings(
        raw={
            "scheduler": {"enabled": False},
            "home_assistant": {},
            "mqtt": {"enabled": False, "password": "do-not-publish"},
            "resources": {
                100: {
                    "resource_type": "qemu", "adapter": "haos", "enabled": True,
                    "monitoring": {"inspect": True, "update_scan": False},
                    "operator_capabilities": denied,
                },
                110: {
                    "resource_type": "lxc", "adapter": "agent_self", "enabled": True,
                    "monitoring": {"inspect": True, "update_scan": False},
                    "operator_capabilities": denied,
                },
            },
        },
        config_path=tmp_path / "config.yaml",
        db_path=tmp_path / "self.db",
        api_token="t" * 64,
    )
    executor = WorkflowExecutor(
        [
            {
                "lxc_status": "running",
                "health_status": "healthy",
                "service_status": "active",
                "api_health": "ok",
                "recent_warnings": ["bounded warning"],
            }
        ]
    )
    service = OpsService(cfg, Database(cfg.db_path), executor)

    state = service.refresh_container(110)

    assert state["configured_resource_count"] == 2
    assert state["configured_lxc_count"] == 1
    assert state["configured_qemu_count"] == 1
    assert state["active_job_count"] == 0
    assert state["mqtt_availability"] == "disabled"
    assert "do-not-publish" not in str(state)


def service_with(
    tmp_path: Path,
    executor: WorkflowExecutor,
    *,
    repair: bool = True,
    post_update_timeout: int = 1,
    database: Database | None = None,
) -> tuple[OpsService, Database]:
    cfg = settings(tmp_path, repair=repair, post_update_timeout=post_update_timeout)
    db = database or Database(cfg.db_path)
    clock = FakeClock()
    stabilizer = Stabilizer(
        executor,
        threading.Event(),
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    service = OpsService(cfg, db, executor, stabilizer=stabilizer)  # type: ignore[arg-type]
    service._ensure_initial_states()
    return service, db


def approved_job(service: OpsService, db: Database) -> dict[str, Any]:
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
    state.update(
        {
            "operation_status": "failed",
            "last_operation_result": "rolled_back",
            "health_status": "critical",
        }
    )
    service._save_state(106, state)
    refreshed = service.refresh_container(106)
    assert refreshed["health_status"] == "healthy"
    assert refreshed["operation_status"] == "failed"
    assert refreshed["last_operation_result"] == "rolled_back"


def test_update_0_2_3_3_succeeds_without_repair_or_rollback(tmp_path: Path) -> None:
    executor = WorkflowExecutor(
        [docker_state(0), docker_state(2), docker_state(3), docker_state(3)]
    )
    service, db = service_with(tmp_path, executor, post_update_timeout=3)
    job = approved_job(service, db)
    service._run_job(job)
    result = db.get_job(job["id"])
    assert result["status"] == "success"
    assert "repair" not in executor.actions
    assert "rollback" not in executor.actions
    assert service.get_state(106)["update_status"] == "up_to_date"


def test_timeout_invokes_repair_and_repair_success_prevents_rollback(
    tmp_path: Path,
) -> None:
    executor = WorkflowExecutor(
        [docker_state(0), docker_state(0), docker_state(3), docker_state(3)]
    )
    service, db = service_with(tmp_path, executor)
    job = approved_job(service, db)
    service._run_job(job)
    assert db.get_job(job["id"])["status"] == "success"
    assert "repair" in executor.actions
    assert "rollback" not in executor.actions


def test_repair_failure_invokes_rollback_and_waits_for_0_3_3(tmp_path: Path) -> None:
    states = [
        docker_state(0),
        docker_state(0),
        docker_state(0),
        docker_state(0),
        docker_state(0),
        docker_state(3),
        docker_state(3),
    ]
    executor = WorkflowExecutor(states)
    service, db = service_with(tmp_path, executor)
    job = approved_job(service, db)
    service._run_job(job)
    assert db.get_job(job["id"])["status"] == "rolled_back"
    assert "repair" in executor.actions
    assert "rollback" in executor.actions
    state = service.get_state(106)
    assert state["last_operation_result"] == "rolled_back"
    assert state["last_terminal_event"] == "job_rolled_back"
    assert state["recovery_notification_suppressed_until"]


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
        def run(
            self,
            action: str,
            vmid: int,
            argument=None,
            timeout=None,
            on_event=None,
        ) -> dict[str, Any]:
            self.actions.append(action)
            if action == "scan":
                return {
                    "ok": True,
                    "data": {
                        "pending_count": 2,
                        "packages": [{"name": "systemd"}],
                        "fingerprint": "fp",
                    },
                }
            return super().run(action, vmid, argument, timeout, on_event)

    executor = ScanExecutor()
    service, _ = service_with(tmp_path, executor)
    result = service.scan_container(106)
    assert result["status"] == "plan_created"
    assert service.get_state(106)["operation_status"] == "waiting_approval"
    assert "update" not in executor.actions


def test_scan_is_blocked_while_job_is_queued_or_running(tmp_path: Path) -> None:
    executor = WorkflowExecutor()
    service, db = service_with(tmp_path, executor)
    plan = db.create_plan(
        vmid=106,
        container_name="weather",
        fingerprint="updates",
        risk="high",
        payload={},
        ttl_minutes=60,
    )
    service.approve(plan["id"])
    result = service.scan_container(106, operator=False)
    assert result["status"] == "skipped"
    assert result["reason"] == "job_active"
    assert "scan" not in executor.actions


def test_changed_plan_is_blocked_before_snapshot_or_update(tmp_path: Path) -> None:
    executor = WorkflowExecutor(preflight_fingerprint="changed")
    service, db = service_with(tmp_path, executor)
    job = approved_job(service, db)
    service._run_job(job)
    result = db.get_job(job["id"])
    assert result["status"] == "blocked"
    assert "snapshot" not in executor.actions
    assert "update" not in executor.actions
    assert "changed after approval" in str(result["error"])


def test_empty_revalidated_plan_is_blocked_before_snapshot(tmp_path: Path) -> None:
    class EmptyPreflightExecutor(WorkflowExecutor):
        def run(self, action: str, vmid: int, argument=None, timeout=None, on_event=None):
            self.actions.append(action)
            if action == "preflight":
                return {
                    "ok": True,
                    "data": {
                        "updates": {
                            "pending_count": 0,
                            "packages": [],
                            "fingerprint": "none",
                        }
                    },
                }
            return super().run(action, vmid, argument, timeout, on_event)

    executor = EmptyPreflightExecutor()
    service, db = service_with(tmp_path, executor)
    job = approved_job(service, db)
    service._run_job(job)
    assert db.get_job(job["id"])["status"] == "blocked"
    assert "snapshot" not in executor.actions


def test_execute_type_error_is_not_retried(tmp_path: Path) -> None:
    class TypeErrorExecutor(WorkflowExecutor):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        def run(self, action: str, vmid: int, argument=None, timeout=None, on_event=None):
            self.calls += 1
            raise TypeError("internal callback bug")

    executor = TypeErrorExecutor()
    service, _ = service_with(tmp_path, executor)
    with pytest.raises(TypeError, match="internal callback bug"):
        service._execute("update", 106, 60, lambda **_: None)
    assert executor.calls == 1


def test_interrupted_job_is_reconciled_into_container_state(tmp_path: Path) -> None:
    cfg = settings(tmp_path)
    db = Database(cfg.db_path)
    plan = db.create_plan(
        vmid=106,
        container_name="weather",
        fingerprint="updates",
        risk="high",
        payload={},
        ttl_minutes=60,
    )
    _, job = db.approve_plan(plan["id"])
    db.update_job(job["id"], status="running", stage="updating", progress=50)
    db.upsert_container_state(
        106,
        {
            "vmid": 106,
            "health_status": "healthy",
            "operation_status": "running",
            "job_stage": "updating",
            "job_progress": 50,
        },
    )

    restarted_db = Database(cfg.db_path)
    service, _ = service_with(
        tmp_path,
        WorkflowExecutor(),
        database=restarted_db,
    )
    state = service.get_state(106)
    assert restarted_db.get_job(job["id"])["status"] == "interrupted"
    assert state["operation_status"] == "failed"
    assert state["job_stage"] == "failed"
    assert state["job_progress"] == 100
    assert "restarted" in state["last_error"]


def test_manual_rollback_requires_policy_and_failed_snapshot(tmp_path: Path) -> None:
    executor = WorkflowExecutor([docker_state(0), docker_state(3), docker_state(3)])
    service, db = service_with(tmp_path, executor)
    plan = db.create_plan(
        vmid=106,
        container_name="weather",
        fingerprint="fp",
        risk="high",
        payload={},
        ttl_minutes=60,
    )
    _, source = db.approve_plan(plan["id"])
    db.update_job(
        source["id"],
        status="failed",
        stage="failed",
        progress=100,
        snapshot_name="snap-safe",
    )
    result = service.manual_rollback(106)
    assert result["status"] == "rolled_back"
    assert "rollback" in executor.actions

    service.settings.raw["containers"][106]["manual_rollback_allowed"] = False
    with pytest.raises(ValueError, match="not allowed"):
        service.manual_rollback(106)


def test_notification_uses_configured_dashboard_path(tmp_path: Path) -> None:
    service, _ = service_with(tmp_path, WorkflowExecutor())
    payload = service._notification(
        "approval_required",
        106,
        pending_count=80,
        risk="high",
    )
    assert payload["dashboard_path"] == "/hubinet-ops/ct-106"
    assert set(payload) == {
        "event_type",
        "vmid",
        "container",
        "dashboard_path",
        "pending_count",
        "risk",
    }


def test_post_update_verification_failure_triggers_rollback(tmp_path: Path) -> None:
    class VerificationFailureExecutor(WorkflowExecutor):
        def __init__(self) -> None:
            super().__init__([docker_state(3), docker_state(3)])

        def run(
            self,
            action: str,
            vmid: int,
            argument=None,
            timeout=None,
            on_event=None,
        ) -> dict[str, Any]:
            if action == "verify":
                raise ExecutorError(
                    "apt-get check failed",
                    data={"apt_check_ok": False, "dpkg_audit_ok": True},
                )
            return super().run(action, vmid, argument, timeout, on_event)

    executor = VerificationFailureExecutor()
    service, db = service_with(tmp_path, executor, post_update_timeout=2)
    job = approved_job(service, db)
    service._run_job(job)
    assert db.get_job(job["id"])["status"] == "rolled_back"
    assert "rollback" in executor.actions
    state = service.get_state(106)
    assert state["verification_status"] == "failed"
    assert state["apt_check_ok"] is False


def test_verification_reboot_warning_and_success_webhook_summary(tmp_path: Path) -> None:
    class WarningExecutor(WorkflowExecutor):
        def run(self, action: str, vmid: int, argument=None, timeout=None, on_event=None):
            if action == "verify":
                self.actions.append(action)
                return {
                    "ok": True,
                    "data": {
                        "apt_check_ok": True,
                        "dpkg_audit_ok": True,
                        "reboot_required": True,
                        "updates": {
                            "pending_count": 0,
                            "packages": [],
                            "fingerprint": "none",
                        },
                        "docker": {"required_healthy": 3, "required_total": 3},
                    },
                }
            return super().run(action, vmid, argument, timeout, on_event)

    executor = WarningExecutor([docker_state(3), docker_state(3)])
    service, db = service_with(tmp_path, executor)
    job = approved_job(service, db)
    notifications: list[dict[str, Any]] = []
    service._notify_ha = notifications.append  # type: ignore[method-assign]
    service._run_job(job)

    state = service.get_state(106)
    assert state["verification_status"] == "warning"
    assert state["reboot_required"] is True
    assert state["apt_check_ok"] is True
    assert state["dpkg_audit_ok"] is True
    success = next(item for item in notifications if item["event_type"] == "job_success")
    assert success["packages_updated_count"] == 0
    assert success["packages_remaining_count"] == 0
    assert success["docker_required_healthy"] == 3
    assert success["docker_required_total"] == 3
    assert success["verification_status"] == "warning"
    assert success["duration_seconds"] >= 0
    assert state["last_terminal_event"] == "job_success"
    assert state["recovery_notification_suppressed_until"]
    assert (
        datetime.fromisoformat(state["recovery_notification_suppressed_until"])
        - datetime.fromisoformat(state["last_terminal_at"])
    ).total_seconds() == 180


def test_transient_final_apt_scan_failure_is_warning_without_rollback(
    tmp_path: Path,
) -> None:
    class TransientScanFailureExecutor(WorkflowExecutor):
        def run(self, action: str, vmid: int, argument=None, timeout=None, on_event=None):
            if action == "verify":
                self.actions.append(action)
                return {
                    "ok": True,
                    "data": {
                        "apt_check_ok": True,
                        "dpkg_audit_ok": True,
                        "reboot_required": False,
                        "final_apt_scan_ok": False,
                        "verification_warning": (
                            "final apt scan failed: Temporary failure resolving repository"
                        ),
                        "update_status": "unknown",
                        "updates": {"pending_count": 0, "packages": []},
                        "docker": {"required_healthy": 3, "required_total": 3},
                    },
                }
            return super().run(action, vmid, argument, timeout, on_event)

    executor = TransientScanFailureExecutor([docker_state(3), docker_state(3)])
    service, db = service_with(tmp_path, executor)
    job = approved_job(service, db)
    notifications: list[dict[str, Any]] = []
    service._notify_ha = notifications.append  # type: ignore[method-assign]
    service._run_job(job)

    assert db.get_job(job["id"])["status"] == "success"
    assert "rollback" not in executor.actions
    state = service.get_state(106)
    assert state["update_status"] == "unknown"
    assert state["verification_status"] == "warning"
    assert state["packages_remaining_count"] is None
    assert state["pending_updates"] is None
    assert state["updates"]["pending_count"] is None
    assert "Temporary failure" in state["verification_error"]
    assert db.find_active_plan(106) is None
    success = next(item for item in notifications if item["event_type"] == "job_success")
    assert success["packages_remaining_count"] is None
    assert "Temporary failure" in success["verification_warning"]


def test_malformed_job_timestamp_does_not_change_success_result(tmp_path: Path) -> None:
    service, db = service_with(
        tmp_path,
        WorkflowExecutor([docker_state(3), docker_state(3)]),
    )
    job = approved_job(service, db)
    job["created_at"] = "not-a-timestamp"
    notifications: list[dict[str, Any]] = []
    service._notify_ha = notifications.append  # type: ignore[method-assign]

    service._run_job(job)

    assert db.get_job(job["id"])["status"] == "success"
    success = next(item for item in notifications if item["event_type"] == "job_success")
    assert success["duration_seconds"] is None


@pytest.mark.parametrize(
    "post_terminal_error",
    [
        RuntimeError("webhook failed after terminal success"),
        ExecutorError("executor-style failure after terminal success"),
    ],
)
def test_worker_errors_preserve_existing_terminal_and_followup_plan(
    tmp_path: Path,
    post_terminal_error: Exception,
) -> None:
    class FollowupExecutor(WorkflowExecutor):
        def run(self, action: str, vmid: int, argument=None, timeout=None, on_event=None):
            if action == "verify":
                self.actions.append(action)
                return {
                    "ok": True,
                    "data": {
                        "apt_check_ok": True,
                        "dpkg_audit_ok": True,
                        "reboot_required": False,
                        "updates": {
                            "pending_count": 1,
                            "packages": [
                                {"name": "curl", "current": "1", "target": "2"}
                            ],
                            "fingerprint": "followup-after-success",
                        },
                        "docker": {"required_healthy": 3, "required_total": 3},
                    },
                }
            return super().run(action, vmid, argument, timeout, on_event)

    service, db = service_with(
        tmp_path,
        FollowupExecutor([docker_state(3), docker_state(3)]),
    )
    job = approved_job(service, db)

    def fail_after_followup(payload: dict[str, Any]) -> None:
        if payload["event_type"] == "approval_required":
            raise post_terminal_error

    service._notify_ha = fail_after_followup  # type: ignore[method-assign]
    if isinstance(post_terminal_error, ExecutorError):
        service._run_job(job)
    else:
        with pytest.raises(RuntimeError, match="after terminal success"):
            service._run_job(job)
        service._handle_unhandled_worker_failure(job)

    assert db.get_job(job["id"])["status"] == "success"
    followup = db.find_active_plan(106, "followup-after-success")
    assert followup is not None
    state = service.get_state(106)
    assert state["active_plan_id"] == followup["id"]
    assert state["operation_status"] == "waiting_approval"
    terminal_events = [
        event
        for event in db.list_job_events(job["id"])
        if event["event_type"] in {"job_success", "job_failed", "job_rolled_back"}
    ]
    assert len(terminal_events) == 1


def test_remaining_packages_create_new_waiting_plan_without_duplicate(tmp_path: Path) -> None:
    class RemainingExecutor(WorkflowExecutor):
        def run(self, action: str, vmid: int, argument=None, timeout=None, on_event=None):
            if action == "verify":
                self.actions.append(action)
                return {
                    "ok": True,
                    "data": {
                        "apt_check_ok": True,
                        "dpkg_audit_ok": True,
                        "reboot_required": False,
                        "updates": {
                            "pending_count": 1,
                            "packages": [{"name": "curl", "current": "1", "target": "2"}],
                            "fingerprint": "remaining",
                        },
                        "docker": {"required_healthy": 3, "required_total": 3},
                    },
                }
            return super().run(action, vmid, argument, timeout, on_event)

    service, db = service_with(tmp_path, RemainingExecutor([docker_state(3), docker_state(3)]))
    job = approved_job(service, db)
    service._run_job(job)
    assert db.get_job(job["id"])["status"] == "success"
    state = service.get_state(106)
    assert state["packages_remaining_count"] == 1
    assert state["verification_status"] == "warning"
    assert state["operation_status"] == "waiting_approval"
    followup = db.find_active_plan(106, "remaining")
    assert followup is not None
    assert service._create_followup_plan(
        106,
        service.settings.containers[106],
        followup["payload"],
    )["id"] == followup["id"]


@pytest.mark.parametrize(
    ("message", "data"),
    [
        ("dpkg audit failed", {"apt_check_ok": True, "dpkg_audit_ok": False}),
        (
            "required docker container is unhealthy",
            {
                "apt_check_ok": True,
                "dpkg_audit_ok": True,
                "docker_required_healthy": 2,
                "docker_required_total": 3,
            },
        ),
    ],
)
def test_integrity_or_docker_verification_failure_follows_rollback_policy(
    tmp_path: Path,
    message: str,
    data: dict[str, Any],
) -> None:
    class FailedExecutor(WorkflowExecutor):
        def run(self, action: str, vmid: int, argument=None, timeout=None, on_event=None):
            if action == "verify":
                self.actions.append(action)
                raise ExecutorError(message, data=data)
            return super().run(action, vmid, argument, timeout, on_event)

    executor = FailedExecutor([docker_state(3), docker_state(3), docker_state(3), docker_state(3)])
    service, db = service_with(tmp_path, executor)
    job = approved_job(service, db)
    service._run_job(job)
    assert db.get_job(job["id"])["status"] == "rolled_back"
    assert "rollback" in executor.actions
    assert service.get_state(106)["verification_status"] == "failed"
