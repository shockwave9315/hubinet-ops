from __future__ import annotations

import pytest
from pathlib import Path

from app.config import Settings
from app.database import Database
from app.executor import ExecutorError
from app.host_control import HostControlError
from app.service import OpsService
from app.config import validate_config
from app.stabilization import Stabilizer
from tests.test_service import (
    FakeClock,
    UpdateSnapshotHost,
    WorkflowExecutor,
    docker_state,
    settings,
)
import threading

def create_ops(tmp_path: Path, **kwargs) -> tuple[OpsService, WorkflowExecutor, Database]:
    cfg = settings(tmp_path, post_update_timeout=5)
    # Configure CT106 with kwargs
    key = "resources" if "resources" in cfg.raw else "containers"
    cfg.raw[key][106].update(kwargs)
    cfg.raw[key][106]["executor_contract"] = {
        "executor_sha256": "a" * 64,
        "profile_sha256": "b" * 64,
    }
    validate_config(cfg.raw)
    cfg.resources[106].update(kwargs)
    db = Database(cfg.db_path)
    executor = WorkflowExecutor()
    clock = FakeClock()
    
    def test_executor(action, vmid, *args, **kwargs):
        executor.actions.append(action)
        if action == "scan":
            return {
                "ok": True,
                "data": {
                    "pending_count": 3,
                    "packages": [{"name": "systemd"}],
                    "fingerprint": executor.preflight_fingerprint,
                }
            }
        # Pop the action since WorkflowExecutor.run also appends it
        executor.actions.pop()
        return WorkflowExecutor.run(executor, action, vmid, *args, **kwargs)
        
    executor.run = test_executor
    service = OpsService(
        cfg,
        db,
        executor,
        host_control=UpdateSnapshotHost(),
    )
    service.stabilizer = Stabilizer(
        executor,
        threading.Event(),
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    return service, executor, db


def test_pre_update_snapshot_policy_no_auto_rollback(tmp_path: Path) -> None:
    # A. CT105: pre_update_snapshot=true, automatic_rollback=false, insufficient_health_contract
    service, executor, db = create_ops(
        tmp_path,
        pre_update_snapshot=True,
        automatic_rollback=False,
    )
    def executor_with_capabilities(action, vmid, *args, **kwargs):
        executor.actions.append(action)
        if action == "capabilities":
            from app.contracts import REQUIRED_APT_ACTIONS
            return {"ok": True, "data": {
                "version": "0.4.3", "protocol_version": 1, "supported_actions": list(REQUIRED_APT_ACTIONS),
                "executor_sha256": "a" * 64, "profile_sha256": "b" * 64,
                "profile_validation_status": "insufficient_health_contract"
            }}
        if action == "scan":
            return {
                "ok": True,
                "data": {
                    "pending_count": 3,
                    "packages": [{"name": "systemd"}],
                    "fingerprint": executor.preflight_fingerprint,
                }
            }
        executor.actions.pop()
        return WorkflowExecutor.run(executor, action, vmid, *args, **kwargs)
        
    executor.run = executor_with_capabilities

    plan = service.scan_container(106)
    if "plan" not in plan:
        raise ValueError(f"Scan returned no plan: {plan}")
    job = service.approve(plan["plan"]["id"])
    job_id = job["job"]["id"]

    service._run_job(db.get_job(job_id))
    
    assert "snapshot" not in executor.actions
    assert len(service.host_control.snapshots) == 1
    assert "update" in executor.actions
    assert "preflight" in executor.actions
    
    final_job = db.get_job(job_id)
    assert final_job["status"] == "success"
    
    # Snapshot exists and retained
    assert final_job.get("snapshot_name") is not None


def test_pre_update_snapshot_policy_failed_update(tmp_path: Path) -> None:
    # B. Ten sam CT105, ale update kończy się błędem
    service, executor, db = create_ops(
        tmp_path,
        pre_update_snapshot=True,
        automatic_rollback=False,
    )
    
    def failing_executor(action, vmid, *args, **kwargs):
        executor.actions.append(action)
        if action == "update":
            raise ExecutorError("update failed")
        if action == "scan":
            return {
                "ok": True,
                "data": {
                    "pending_count": 3,
                    "packages": [{"name": "systemd"}],
                    "fingerprint": executor.preflight_fingerprint,
                }
            }
        executor.actions.pop()
        return WorkflowExecutor.run(executor, action, vmid, *args, **kwargs)
        
    executor.run = failing_executor
    
    plan = service.scan_container(106)
    if "plan" not in plan:
        raise ValueError(f"Scan returned no plan: {plan}")
    job = service.approve(plan["plan"]["id"])
    job_id = job["job"]["id"]

    service._run_job(db.get_job(job_id))
    
    assert "snapshot" not in executor.actions
    assert len(service.host_control.snapshots) == 1
    assert "update" in executor.actions
    assert "repair" not in executor.actions  # No repair/rollback because auto_rollback is false
    assert "rollback" not in executor.actions
    final_job = db.get_job(job_id)
    assert final_job["status"] == "failed"
    state = service.get_state(106)
    assert state["operation_status"] == "manual_intervention"
    assert state["last_operation_result"] == "manual_intervention"
    assert final_job.get("snapshot_name") is not None


def test_pre_update_snapshot_policy_auto_rollback(tmp_path: Path) -> None:
    # C. CT106: pre_update_snapshot=true, automatic_rollback=true, valid health contract
    service, executor, db = create_ops(
        tmp_path,
        pre_update_snapshot=True,
        automatic_rollback=True,
    )
    repair_called = []
    def failing_executor(action, vmid, *args, **kwargs):
        executor.actions.append(action)
        if action == "inspect":
            if not repair_called:
                raise ExecutorError("healthcheck failed")
        if action == "repair":
            repair_called.append(1)
            raise ExecutorError("repair failed")
        if action == "scan":
            return {
                "ok": True,
                "data": {
                    "pending_count": 3,
                    "packages": [{"name": "systemd"}],
                    "fingerprint": executor.preflight_fingerprint,
                }
            }
        executor.actions.pop()
        return WorkflowExecutor.run(executor, action, vmid, *args, **kwargs)
        
    executor.run = failing_executor
    
    plan = service.scan_container(106)
    if "plan" not in plan:
        raise ValueError(f"Scan returned no plan: {plan}")
    job = service.approve(plan["plan"]["id"])
    job_id = job["job"]["id"]

    service._run_job(db.get_job(job_id))
    
    assert "snapshot" not in executor.actions
    assert len(service.host_control.snapshots) == 1
    assert "update" in executor.actions
    assert "rollback" not in executor.actions
    assert any(
        call[0] == "snapshot_rollback"
        for call in service.host_control.calls
    )
    final_job = db.get_job(job_id)
    assert final_job["status"] == "rolled_back"
    state = service.get_state(106)
    assert state["operation_status"] == "rolled_back"
    assert state["last_operation_result"] == "rolled_back"


def test_pre_update_snapshot_policy_no_snapshot(tmp_path: Path) -> None:
    # D. Jawne: pre_update_snapshot=false, automatic_rollback=false
    service, executor, db = create_ops(
        tmp_path,
        pre_update_snapshot=False,
        automatic_rollback=False,
    )
    
    plan = service.scan_container(106)
    if "plan" not in plan:
        raise ValueError(f"Scan returned no plan: {plan}")
    job = service.approve(plan["plan"]["id"])
    job_id = job["job"]["id"]

    service._run_job(db.get_job(job_id))
    
    assert "preflight" in executor.actions
    assert "snapshot" not in executor.actions
    assert "update" in executor.actions


def test_pre_update_snapshot_policy_invalid_config(tmp_path: Path) -> None:
    # E. Niepoprawne: pre_update_snapshot=false, automatic_rollback=true
    with pytest.raises(RuntimeError, match="automatic_rollback requires pre_update_snapshot"):
        create_ops(
            tmp_path,
            pre_update_snapshot=False,
            automatic_rollback=True,
        )


def test_pre_update_snapshot_policy_snapshot_failure(tmp_path: Path) -> None:
    # F. Snapshot creation failure
    service, executor, db = create_ops(
        tmp_path,
        pre_update_snapshot=True,
        automatic_rollback=False,
    )
    
    def failing_executor(action, vmid, *args, **kwargs):
        executor.actions.append(action)
        if action == "scan":
            return {
                "ok": True,
                "data": {
                    "pending_count": 3,
                    "packages": [{"name": "systemd"}],
                    "fingerprint": executor.preflight_fingerprint,
                }
            }
        executor.actions.pop()
        return WorkflowExecutor.run(executor, action, vmid, *args, **kwargs)
        
    executor.run = failing_executor
    def failing_host_snapshot(*args, **kwargs):
        raise HostControlError("snapshot failed", status="failed")

    service.host_control.execute = failing_host_snapshot
    
    plan = service.scan_container(106)
    if "plan" not in plan:
        raise ValueError(f"Scan returned no plan: {plan}")
    job = service.approve(plan["plan"]["id"])
    job_id = job["job"]["id"]

    service._run_job(db.get_job(job_id))
    
    assert "snapshot" not in executor.actions
    assert "update" not in executor.actions
    
    final_job = db.get_job(job_id)
    assert final_job["status"] == "blocked"
    state = service.get_state(106)
    assert state["operation_status"] == "failed"
    assert state["last_operation_result"] == "failed"


def test_all_managed_lxc_have_pre_update_snapshot() -> None:
    import yaml
    with open('config/config.example.yaml', 'r') as f:
        cfg = yaml.safe_load(f)
    
    for vmid in range(101, 110):
        if vmid in cfg['resources']:
            assert cfg['resources'][vmid].get('pre_update_snapshot') is True, f"CT{vmid} must have pre_update_snapshot=true"
    
    if 100 in cfg['resources']:
        assert cfg['resources'][100].get('pre_update_snapshot') is False, "VM100 must have pre_update_snapshot=false"
        
    if 110 in cfg['resources']:
        assert cfg['resources'][110].get('pre_update_snapshot') is True, "CT110 system updates must require a pre-update snapshot"
