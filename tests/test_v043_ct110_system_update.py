from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
from typing import Any

import pytest

from app.database import Database
from app.host_control import HostControlError
from app.service import OpsService
from tests.test_lifecycle_snapshots import CompatibleExecutor, run_queued, settings

PVE = Path(__file__).parents[1] / "deploy" / "pve"
sys.path.insert(0, str(PVE))

from hubinet_ops_ct110_system import (  # type: ignore[import-not-found]  # noqa: E402
    SystemUpdateError,
    prepare as prepare_host_supervisor,
    run as run_host_supervisor,
)
from hubinet_ops_hostd import HostJobStore  # type: ignore[import-not-found]  # noqa: E402
from hubinet_ops_release import read_marker  # type: ignore[import-not-found]  # noqa: E402


PACKAGES = [
    {
        "name": "bind9-dnsutils",
        "current": "1:9.20.25-1~deb13u1",
        "target": "1:9.20.26-1~deb13u1",
        "security": True,
    },
    {
        "name": "bind9-host",
        "current": "1:9.20.25-1~deb13u1",
        "target": "1:9.20.26-1~deb13u1",
        "security": True,
    },
    {
        "name": "bind9-libs",
        "current": "1:9.20.25-1~deb13u1",
        "target": "1:9.20.26-1~deb13u1",
        "security": True,
    },
    {
        "name": "libexpat1",
        "current": "2.8.1-1",
        "target": "2.8.2-1~deb13u1",
        "security": True,
    },
]


def _fingerprint(packages: list[dict[str, Any]]) -> str:
    stable = [
        {
            "current": item["current"],
            "name": item["name"],
            "security": item["security"],
            "target": item["target"],
        }
        for item in packages
    ]
    return hashlib.sha256(
        json.dumps(stable, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


class Ct110SystemHost:
    def __init__(self) -> None:
        self.packages = [dict(item) for item in PACKAGES]
        self.calls: list[tuple[Any, ...]] = []
        self.result: dict[str, Any] = {
            "plan_fingerprint": _fingerprint(PACKAGES),
            "snapshot_proof": {
                "version": 3,
                "vmid": 110,
                "snapshot_name": "hubinet-ops-110-pre-20260802T120000Z",
                "kind": "pre-update",
                "host_source_job_id": "a" * 32,
                "pve_snaptime": 1785672000,
                "physically_confirmed": True,
            },
            "update": {"package_total": 4},
            "verification": {
                "apt_check_ok": True,
                "dpkg_audit_ok": True,
                "service_active": True,
                "health_endpoint_ok": True,
                "backend_version": "0.4.3",
                "reboot_required": True,
                "updates": {
                    "pending_count": 0,
                    "packages": [],
                    "fingerprint": _fingerprint([]),
                },
            },
        }

    def scan_ct110_system(self, vmid: int) -> dict[str, Any]:
        assert vmid == 110
        packages = [dict(item) for item in self.packages]
        self.calls.append(("scan", vmid))
        return {
            "pending_count": len(packages),
            "packages": packages,
            "fingerprint": _fingerprint(packages),
            "scanned_at": "2026-08-02T12:00:00+00:00",
            "security_updates_count": sum(
                item["security"] is True for item in packages
            ),
            "reboot_required": False,
        }

    def execute(
        self,
        operation_type: str,
        vmid: int,
        request_id: str,
        *,
        system_update_fingerprint: str | None = None,
        on_observed=None,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        self.calls.append(
            (operation_type, vmid, request_id, system_update_fingerprint)
        )
        if on_observed is not None:
            on_observed({"id": "a" * 32})
        if operation_type != "ct110_system_update":
            return {}
        return dict(self.result)

    def wait_existing_job(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("reattach", *args))
        callback = kwargs.get("on_observed")
        if callback is not None:
            callback({"id": "a" * 32})
        return dict(self.result)


def _service(tmp_path: Path) -> tuple[OpsService, Database, Ct110SystemHost]:
    cfg = settings(tmp_path, vmid=110, adapter="agent_self")
    resource = cfg.raw["resources"][110]
    resource["monitoring"]["update_scan"] = True
    resource["operator_capabilities"]["scan"] = True
    resource["pre_update_snapshot"] = True
    db = Database(cfg.db_path)
    host = Ct110SystemHost()
    return (
        OpsService(cfg, db, CompatibleExecutor(), host_control=host),  # type: ignore[arg-type]
        db,
        host,
    )


def test_ct110_system_scan_builds_separate_four_package_plan_with_stable_fingerprint(
    tmp_path: Path,
) -> None:
    service, db, host = _service(tmp_path)

    result = service.scan_container(110, operator=True)

    assert result["status"] == "plan_created"
    plan = result["plan"]
    assert plan["payload"]["plan_type"] == "ct110_system_update"
    assert plan["payload"]["pending_count"] == 4
    assert [item["name"] for item in plan["payload"]["packages"]] == [
        "bind9-dnsutils",
        "bind9-host",
        "bind9-libs",
        "libexpat1",
    ]
    assert plan["payload"]["security_updates_count"] == 4
    assert plan["fingerprint"] == _fingerprint(PACKAGES)
    assert db.list_jobs() == []
    assert host.calls == [("scan", 110)]


def test_ct110_system_approval_rechecks_and_blocks_changed_fingerprint(
    tmp_path: Path,
) -> None:
    service, db, host = _service(tmp_path)
    planned = service.scan_container(110, operator=True)
    host.packages[0]["target"] = "1:9.20.27-1~deb13u1"

    with pytest.raises(ValueError, match="fingerprint changed"):
        service.approve_active(110, "ct110-system-changed-0001")

    assert db.get_plan(planned["plan"]["id"])["status"] == "superseded"
    assert db.list_jobs() == []
    assert not any(call[0] == "ct110_system_update" for call in host.calls)


def test_ct110_system_update_requires_physical_snapshot_proof_before_success(
    tmp_path: Path,
) -> None:
    service, db, host = _service(tmp_path)
    service.scan_container(110, operator=True)
    approved = service.approve_active(110, "ct110-system-proof-0001")
    host.result.pop("snapshot_proof")

    terminal = run_queued(service, db)

    assert terminal["id"] == approved["job"]["id"]
    assert terminal["status"] == "failed"
    assert "snapshot proof" in terminal["error"].lower()


def test_ct110_system_update_is_host_supervised_and_persists_verification(
    tmp_path: Path,
) -> None:
    service, db, host = _service(tmp_path)
    service.scan_container(110, operator=True)
    approved = service.approve_active(110, "ct110-system-success-0001")
    same = service.approve_active(110, "ct110-system-success-0001")

    terminal = run_queued(service, db)
    state = service.get_state(110)

    assert same["job"]["id"] == approved["job"]["id"]
    assert terminal["status"] == "success"
    assert terminal["operation_type"] == "ct110_system_update"
    assert terminal["result"]["snapshot_proof"]["physically_confirmed"] is True
    assert state["system_update_status"] == "up_to_date"
    assert state["system_pending_updates"] == 0
    assert state["system_apt_check_ok"] is True
    assert state["system_dpkg_audit_ok"] is True
    assert state["system_reboot_required"] is True
    assert host.calls[-1] == (
        "ct110_system_update",
        110,
        "ct110-system-success-0001",
        _fingerprint(PACKAGES),
    )


def test_ct110_system_outcome_unknown_never_replays_mutation(
    tmp_path: Path,
) -> None:
    service, db, host = _service(tmp_path)
    service.scan_container(110, operator=True)
    approved = service.approve_active(110, "ct110-system-unknown-0001")
    job = db.update_job(
        approved["job"]["id"],
        status="running",
        stage="host_outcome_unknown",
    )

    def unavailable(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise HostControlError("hostd unavailable", status="unavailable")

    host.wait_existing_job = unavailable  # type: ignore[method-assign]
    service._reattach_host_control_job(job)

    current = db.get_job(job["id"])
    assert current["status"] == "running"
    assert current["stage"] in {"host_outcome_unknown", "host_reconciliation"}
    assert not any(call[0] == "ct110_system_update" for call in host.calls)


class SupervisorController:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.fingerprint = _fingerprint(PACKAGES)
        self.fail_action: str | None = None
        self.verify_result = {
            "apt_check_ok": True,
            "dpkg_audit_ok": True,
            "service_active": True,
            "health_endpoint_ok": True,
            "final_apt_scan_ok": True,
            "reboot_required": True,
            "updates": {
                "pending_count": 0,
                "packages": [],
                "fingerprint": _fingerprint([]),
            },
        }
        self.snapshot = {
            "name": "hubinet-ops-110-pre-20260802T120000Z",
            "vmid": 110,
            "kind": "pre-update",
            "source_job_id": "f" * 32,
            "pve_snaptime": 1785672000,
            "owned_by_hubinet_ops": True,
        }

    def execute(
        self,
        action: str,
        vmid: int,
        argument: str | None = None,
        *,
        source_job_id: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append((action, vmid, argument, source_job_id))
        if action == "ct110-system-scan":
            return {"fingerprint": self.fingerprint}
        if action == "snapshot-create":
            snapshot = dict(self.snapshot)
            snapshot["name"] = str(argument)
            snapshot["source_job_id"] = source_job_id
            return snapshot
        if action == "snapshot-rollback":
            return {"action": "rollback", "snapshot": self.snapshot["name"]}
        raise AssertionError(action)

    def _require_running(self, vmid: int) -> None:
        assert vmid == 110

    def _managed(self, vmid: int, action: str, *, timeout: int) -> dict[str, Any]:
        self.calls.append(("guest", vmid, action, timeout))
        if action == self.fail_action:
            message = (
                "health endpoint failed"
                if action == "verify"
                else f"{action} failed"
            )
            raise HostControlError(message)
        if action == "preflight":
            return {"updates": {"fingerprint": self.fingerprint}}
        if action == "update":
            return {"package_total": 4}
        if action == "verify":
            return dict(self.verify_result)
        raise AssertionError(action)


def _prepared_host_supervisor(
    tmp_path: Path,
) -> tuple[HostJobStore, dict[str, Any], Path, SupervisorController]:
    store = HostJobStore(tmp_path / "hostd.db")
    fingerprint = _fingerprint(PACKAGES)
    job, created = store.create(
        vmid=110,
        operation_type="ct110_system_update",
        request_id="host-ct110-system-0001",
        argument=fingerprint,
    )
    assert created is True
    job = store.begin_self_update_launch(job["id"])
    prepare_host_supervisor(
        result_dir=store.self_update_results,
        database=store.path,
        job_id=job["id"],
        fingerprint=fingerprint,
    )
    return store, job, store.self_update_results, SupervisorController()


def test_pve_supervisor_persists_snapshot_boundary_and_terminal_verification(
    tmp_path: Path,
) -> None:
    store, job, result_dir, controller = _prepared_host_supervisor(tmp_path)

    exit_code = run_host_supervisor(
        result_dir=result_dir,
        database=store.path,
        job_id=job["id"],
        fingerprint=str(job["argument"]),
        controller=controller,  # type: ignore[arg-type]
    )
    marker = read_marker(result_dir, job["id"])

    assert exit_code == 0
    assert marker is not None and marker["status"] == "succeeded"
    assert marker["snapshot_proof"]["physically_confirmed"] is True
    assert marker["verification"]["apt_check_ok"] is True
    assert [call[2] for call in controller.calls if call[0] == "guest"] == [
        "preflight",
        "update",
        "verify",
    ]
    reconciled = store.reconcile_startup_job(controller, job["id"])  # type: ignore[arg-type]
    assert reconciled["status"] == "succeeded"
    assert reconciled["result"]["snapshot_proof"]["physically_confirmed"] is True


def test_pve_supervisor_fingerprint_change_never_creates_snapshot_or_runs_apt(
    tmp_path: Path,
) -> None:
    store, job, result_dir, controller = _prepared_host_supervisor(tmp_path)
    controller.fingerprint = "b" * 64

    exit_code = run_host_supervisor(
        result_dir=result_dir,
        database=store.path,
        job_id=job["id"],
        fingerprint=str(job["argument"]),
        controller=controller,  # type: ignore[arg-type]
    )

    assert exit_code == 1
    assert not any(call[0] == "snapshot-create" for call in controller.calls)
    assert not any(call[:3] == ("guest", 110, "update") for call in controller.calls)


def test_pve_supervisor_never_repeats_apt_after_durable_mutation_boundary(
    tmp_path: Path,
) -> None:
    store, job, result_dir, controller = _prepared_host_supervisor(tmp_path)
    first = run_host_supervisor(
        result_dir=result_dir,
        database=store.path,
        job_id=job["id"],
        fingerprint=str(job["argument"]),
        controller=controller,  # type: ignore[arg-type]
    )

    with pytest.raises(SystemUpdateError, match="one-shot"):
        run_host_supervisor(
            result_dir=result_dir,
            database=store.path,
            job_id=job["id"],
            fingerprint=str(job["argument"]),
            controller=controller,  # type: ignore[arg-type]
        )

    assert first == 0
    assert sum(call[:3] == ("guest", 110, "update") for call in controller.calls) == 1


def test_apt_failure_requires_manual_intervention_without_automatic_rollback(
    tmp_path: Path,
) -> None:
    store, job, result_dir, controller = _prepared_host_supervisor(tmp_path)
    controller.fail_action = "update"

    exit_code = run_host_supervisor(
        result_dir=result_dir,
        database=store.path,
        job_id=job["id"],
        fingerprint=str(job["argument"]),
        controller=controller,  # type: ignore[arg-type]
        automatic_rollback=True,
    )
    marker = read_marker(result_dir, job["id"])

    assert exit_code == 1
    assert marker is not None and marker["manual_intervention_required"] is True
    assert not any(call[0] == "snapshot-rollback" for call in controller.calls)


@pytest.mark.parametrize("failed_check", ["apt_check_ok", "dpkg_audit_ok"])
def test_post_update_package_verification_failure_requires_manual_intervention(
    tmp_path: Path,
    failed_check: str,
) -> None:
    store, job, result_dir, controller = _prepared_host_supervisor(tmp_path)
    controller.verify_result[failed_check] = False

    exit_code = run_host_supervisor(
        result_dir=result_dir,
        database=store.path,
        job_id=job["id"],
        fingerprint=str(job["argument"]),
        controller=controller,  # type: ignore[arg-type]
        automatic_rollback=True,
    )
    marker = read_marker(result_dir, job["id"])

    assert exit_code == 1
    assert marker is not None and marker["manual_intervention_required"] is True
    assert not any(call[0] == "snapshot-rollback" for call in controller.calls)


def test_health_failure_uses_policy_gated_exact_snapshot_rollback(
    tmp_path: Path,
) -> None:
    store, job, result_dir, controller = _prepared_host_supervisor(tmp_path)
    controller.fail_action = "verify"

    exit_code = run_host_supervisor(
        result_dir=result_dir,
        database=store.path,
        job_id=job["id"],
        fingerprint=str(job["argument"]),
        controller=controller,  # type: ignore[arg-type]
        automatic_rollback=True,
    )

    assert exit_code == 1
    rollback = next(call for call in controller.calls if call[0] == "snapshot-rollback")
    identity = json.loads(str(rollback[2]))
    assert identity["snapshot_name"].startswith("hubinet-ops-110-pre-")
    assert identity["expected_source_job_id"] == job["id"]
    assert identity["expected_pve_snaptime"] == 1785672000


def test_pve_supervisor_persists_snapshot_proof_but_not_apt_if_fingerprint_changes(
    tmp_path: Path,
) -> None:
    store, job, result_dir, controller = _prepared_host_supervisor(tmp_path)
    
    original_execute = controller.execute
    scan_count = 0
    def mock_execute(action: str, vmid: int, argument: str | None = None, *, source_job_id: str | None = None) -> dict[str, Any]:
        nonlocal scan_count
        if action == "ct110-system-scan":
            scan_count += 1
            if scan_count == 3:
                return {"fingerprint": "changed" * 9}
        return original_execute(action, vmid, argument, source_job_id=source_job_id)
    controller.execute = mock_execute  # type: ignore

    print(controller.calls)
    exit_code = run_host_supervisor(
        result_dir=result_dir,
        database=store.path,
        job_id=job["id"],
        fingerprint=str(job["argument"]),
        controller=controller,  # type: ignore[arg-type]
    )
    assert exit_code == 1

    marker = read_marker(result_dir, job["id"])
    assert marker is not None
    assert "CT110 system state changed during snapshot creation" in marker["error"]
    
    assert any(call[0] == "snapshot-create" for call in controller.calls)
    assert marker is not None
    assert marker["snapshot_proof"] is not None
    assert marker.get("apt_started_at") is None
    assert not any(call[0] == "guest" and call[2] == "update" for call in controller.calls)

    with pytest.raises(SystemUpdateError, match="CT110 supervisor is not at the one-shot pre-mutation boundary"):
        run_host_supervisor(
            result_dir=result_dir,
            database=store.path,
            job_id=job["id"],
            fingerprint=str(job["argument"]),
            controller=controller,  # type: ignore[arg-type]
        )
    assert not any(call[0] == "guest" and call[2] == "update" for call in controller.calls)
