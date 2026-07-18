from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

from app.config import Settings
from app.database import Database
from app.service import OpsService, _fingerprint, _risk_for


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
                    "criticality": "low",
                    "automatic_rollback": False,
                    "manual_rollback_allowed": False,
                    "repair_actions": [],
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
    def run(self, action: str, vmid: int, argument=None, timeout=None, on_event=None):
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
        if action == "scan":
            return {
                "ok": True,
                "data": {
                    "pending_count": 0,
                    "packages": [],
                    "fingerprint": _fingerprint({"packages": []}),
                },
            }
        return {"ok": True, "data": {}}


def service(tmp_path: Path, executor: Any | None = None) -> tuple[OpsService, Database]:
    cfg = settings(tmp_path)
    db = Database(cfg.db_path)
    result = OpsService(cfg, db, executor or Executor())
    result._ensure_initial_states()
    return result, db


def test_scan_restores_previous_terminal_stage_and_error(tmp_path: Path) -> None:
    ops, _ = service(tmp_path)
    state = ops.get_state(106)
    state.update(
        {
            "operation_status": "rolled_back",
            "job_stage": "completed",
            "last_operation_result": "rolled_back",
            "last_error": "update failed before successful rollback",
        }
    )
    ops._save_state(106, state)

    assert ops.scan_container(106)["status"] == "up_to_date"
    scanned = ops.get_state(106)
    assert scanned["operation_status"] == "rolled_back"
    assert scanned["job_stage"] == "completed"
    assert scanned["last_operation_result"] == "rolled_back"
    assert scanned["last_error"] == "update failed before successful rollback"


def test_refresh_re_reads_state_after_slow_inspect(tmp_path: Path) -> None:
    entered = threading.Event()
    release = threading.Event()

    class SlowExecutor(Executor):
        def run(self, action: str, vmid: int, argument=None, timeout=None, on_event=None):
            if action == "inspect":
                entered.set()
                assert release.wait(timeout=2)
            return super().run(action, vmid, argument, timeout, on_event)

    ops, _ = service(tmp_path, SlowExecutor())
    thread = threading.Thread(target=ops.refresh_container, args=(106,))
    thread.start()
    assert entered.wait(timeout=2)

    terminal = ops.get_state(106)
    terminal.update(
        {
            "operation_status": "failed",
            "job_stage": "failed",
            "job_progress": 100,
            "last_operation_result": "rolled_back",
            "last_error": "preserve this terminal result",
        }
    )
    ops._save_state(106, terminal)
    release.set()
    thread.join(timeout=2)
    assert not thread.is_alive()

    refreshed = ops.get_state(106)
    assert refreshed["health_status"] == "healthy"
    assert refreshed["operation_status"] == "failed"
    assert refreshed["job_stage"] == "failed"
    assert refreshed["last_operation_result"] == "rolled_back"
    assert refreshed["last_error"] == "preserve this terminal result"


def test_expired_waiting_plan_is_not_reused(tmp_path: Path) -> None:
    _, db = service(tmp_path)
    plan = db.create_plan(
        vmid=106,
        container_name="weather",
        fingerprint="fp",
        risk="low",
        payload={},
        ttl_minutes=-1,
    )
    assert db.find_active_plan(106, "fp") is None
    assert db.get_plan(plan["id"])["status"] == "expired"


def test_manual_operations_refuse_existing_vmid_lock(tmp_path: Path) -> None:
    ops, _ = service(tmp_path)
    lock = ops._scan_locks[106]
    assert lock.acquire(blocking=False)
    try:
        with pytest.raises(ValueError, match="scan or manual operation"):
            ops.retry_healthcheck(106)
        with pytest.raises(ValueError, match="scan or manual operation"):
            ops.manual_rollback(106)
    finally:
        lock.release()


def test_fallback_fingerprint_ignores_volatile_scan_timestamp() -> None:
    packages = [{"name": "systemd", "current": "1", "target": "2"}]
    assert _fingerprint({"packages": packages, "scanned_at": 1}) == _fingerprint(
        {"packages": packages, "scanned_at": 999}
    )


def test_arch_qualified_critical_package_is_high_risk() -> None:
    assert (
        _risk_for(
            {"criticality": "low"},
            {"pending_count": 1, "packages": [{"name": "libc6:amd64"}]},
        )
        == "high"
    )
