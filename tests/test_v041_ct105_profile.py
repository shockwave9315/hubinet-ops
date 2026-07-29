import json
from pathlib import Path
import pytest

from app.config import Settings
from app.database import Database
from tests.test_service import WorkflowExecutor, settings

def test_ct105_profile_content():
    profile_path = Path("deploy/managed/profiles/ct105.json")
    assert profile_path.exists()
    
    with profile_path.open() as f:
        data = json.load(f)
        
    assert "AdGuardHome.service" in data.get("services", [])
    assert len(data.get("services", [])) == 1

def test_ct105_config_settings():
    import yaml
    with open('config/config.example.yaml', 'r') as f:
        cfg = yaml.safe_load(f)
    ct105 = cfg['resources'][105]
    
    assert "AdGuardHome.service" in ct105["required_services"]
    assert ct105["automatic_rollback"] is False
    assert ct105["pre_update_snapshot"] is True

def test_ct105_healthcheck_fails_when_adguard_fails(tmp_path: Path):
    from tests.test_v041_pre_update_snapshot import create_ops
    from app.executor import ExecutorError
    
    service, executor, db = create_ops(
        tmp_path,
        pre_update_snapshot=True,
        automatic_rollback=False,
    )
    # create_ops configures 106. Let's just copy 106 to 105 in settings
    key = "resources" if "resources" in service.settings.raw else "containers"
    service.settings.raw[key][105] = dict(service.settings.raw[key][106])
    import threading
    service._scan_locks[105] = threading.Lock()
    # Ensure it's returned by _resource
    # Since settings.resources is a property, modifying settings.raw updates it dynamically

    
    def failing_executor(action, vmid, *args, **kwargs):
        executor.actions.append(action)
        if action == "inspect":
            raise ExecutorError("healthcheck failed on AdGuardHome.service")
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
    
    # Run against CT105
    plan = service.scan_container(105)
    job = service.approve(plan["plan"]["id"])
    job_id = job["job"]["id"]

    service._run_job(db.get_job(job_id))
    
    final_job = db.get_job(job_id)
    assert final_job["status"] == "failed"
    state = service.get_state(105)
    assert state["operation_status"] == "manual_intervention"

def test_dashboard_no_insufficient_warning_for_ct105():
    import subprocess
    result = subprocess.run(["python", "scripts/validate_managed_profiles.py"], capture_output=True, text=True)
    assert result.returncode == 0
    assert "ct105.json: valid" in result.stdout

def test_ct105_profile_is_valid():
    import subprocess
    result = subprocess.run(["python", "scripts/validate_managed_profiles.py"], capture_output=True, text=True)
    assert result.returncode == 0

