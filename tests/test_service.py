from __future__ import annotations

import threading
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from app.config import Settings
from app.contracts import REQUIRED_APT_ACTIONS
from app.database import Database
from app.executor import ExecutorError
from app.service import OpsService
from app.stabilization import StabilizationPolicy, Stabilizer


EXECUTOR_HASH = "a" * 64
PROFILE_HASH = "b" * 64


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class UpdateSnapshotHost:
    def __init__(self) -> None:
        self.snapshots: list[dict[str, Any]] = []
        self.calls: list[tuple[str, int, str]] = []

    def list_snapshots(self, vmid: int) -> list[dict[str, Any]]:
        return [dict(item) for item in self.snapshots]

    def execute(
        self,
        operation_type: str,
        vmid: int,
        request_id: str,
        *,
        snapshot_name: str | None = None,
        snapshot_kind: str | None = None,
        expected_source_job_id: str | None = None,
        expected_pve_snaptime: int | None = None,
        release_fingerprint: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append((operation_type, vmid, request_id))
        if operation_type == "snapshot_rollback":
            assert snapshot_kind == "pre-update"
            assert expected_source_job_id
            assert expected_pve_snaptime
            return {"lxc_status": "running", "runtime_status": "running"}
        assert operation_type == "snapshot_create"
        assert snapshot_name is not None
        source_job_id = hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:32]
        snapshot = {
            "name": snapshot_name,
            "kind": "pre-update",
            "owned_by_hubinet_ops": True,
            "ownership_status": "owned",
            "rollback_eligible": True,
            "delete_eligible": True,
            "source_job_id": source_job_id,
            "pve_snaptime": 1785329640,
        }
        self.snapshots.insert(0, snapshot)
        return dict(snapshot)


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
        self.snapshots: list[dict[str, Any]] = []

    def run(
        self,
        action: str,
        vmid: int,
        argument=None,
        timeout=None,
        on_event=None,
    ) -> dict[str, Any]:
        self.actions.append(action)
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
        if action == "snapshot":
            assert argument
            kind = "pre-update" if "-pre-" in argument else "manual"
            self.snapshots.insert(
                0,
                {
                    "name": argument,
                    "created_at": "2026-07-29T00:00:00+00:00",
                    "kind": kind,
                    "owned_by_hubinet_ops": True,
                    "rollback_eligible": True,
                    "delete_eligible": True,
                },
            )
            return {"ok": True, "data": {}}
        if action == "list-snapshots":
            return {"ok": True, "data": {"snapshots": list(self.snapshots)}}
        if action in {"delete-snapshot", "snapshot-delete"}:
            self.snapshots = [
                item for item in self.snapshots if item.get("name") != argument
            ]
            return {"ok": True, "data": {}}
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
                    "automatic_rollback": True,
                    "pre_update_snapshot": True,
                    "manual_rollback_allowed": True,
                    "executor_contract": {
                        "executor_sha256": EXECUTOR_HASH,
                        "profile_sha256": PROFILE_HASH,
                    },
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
    assert executor.actions == ["status", "capabilities", "inspect", "scan"]
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


def test_production_periodic_scan_targets_only_apt_lxc_resources(
    tmp_path: Path,
) -> None:
    import yaml

    raw = yaml.safe_load(Path("config/config.example.yaml").read_text(encoding="utf-8"))
    cfg = Settings(
        raw=raw,
        config_path=tmp_path / "config.yaml",
        db_path=tmp_path / "inventory.db",
        api_token="t" * 64,
    )

    class InventoryExecutor(WorkflowExecutor):
        def __init__(self) -> None:
            super().__init__()
            self.vmids: list[int] = []

        def run(self, action: str, vmid: int, **kwargs: Any) -> dict[str, Any]:
            if action == "scan":
                self.vmids.append(vmid)
            return super().run(action, vmid, **kwargs)

    executor = InventoryExecutor()
    service = OpsService(cfg, Database(cfg.db_path), executor)

    results = service.scan_all(operator=False)

    assert cfg.monitoring_scheduler["enabled"] is True
    assert executor.vmids == list(range(101, 110))
    assert len(results) == 9
    assert all(item["status"] == "up_to_date" for item in results)
    assert 100 not in executor.vmids
    assert 110 not in executor.vmids
    for vmid in (101, 102, 103, 104, 105, 107, 108, 109):
        assert service.scan_container(vmid, operator=True)["status"] == "up_to_date"


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
    vm100_executor = WorkflowExecutor(
        [{"qemu_status": "running", "health_status": "healthy"}]
    )
    vm100_service = OpsService(
        cfg,
        Database(cfg.db_path),
        vm100_executor,
    )
    assert vm100_service.refresh_container(100)["health_status"] == "healthy"
    assert vm100_executor.actions == ["inspect"]
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

    assert executor.actions == ["status", "inspect"]
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
    host_control: Any = None,
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
    service = OpsService(
        cfg,
        db,
        executor,
        stabilizer=stabilizer,
        host_control=host_control,
    )  # type: ignore[arg-type]
    service._ensure_initial_states()
    return service, db


def test_refresh_probes_and_persists_compatible_executor_contract(
    tmp_path: Path,
) -> None:
    executor = WorkflowExecutor([docker_state(3)])
    service, _ = service_with(tmp_path, executor)

    state = service.refresh_container(106)

    assert executor.actions[:3] == ["status", "capabilities", "inspect"]
    assert state["executor_compatible"] is True
    assert state["executor_version"] == "0.4.1"
    assert state["executor_protocol_version"] == 1
    assert state["executor_sha256"] == EXECUTOR_HASH
    assert state["executor_profile_sha256"] == PROFILE_HASH
    assert state["profile_validation_status"] == "valid"
    assert state["health_status"] == "healthy"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("version", "0.4.0", "version 0.4.0 != 0.4.1"),
        ("executor_sha256", "c" * 64, "executor sha256 mismatch"),
    ],
)
def test_refresh_keeps_inspect_when_executor_contract_is_incompatible(
    tmp_path: Path,
    field: str,
    value: str,
    message: str,
) -> None:
    class IncompatibleExecutor(WorkflowExecutor):
        def run(self, action: str, vmid: int, **kwargs: Any) -> dict[str, Any]:
            result = super().run(action, vmid, **kwargs)
            if action == "capabilities":
                result["data"][field] = value
            return result

    executor = IncompatibleExecutor([docker_state(3)])
    service, _ = service_with(tmp_path, executor)

    state = service.refresh_container(106)

    assert executor.actions[:3] == ["status", "capabilities", "inspect"]
    assert state["executor_compatible"] is False
    assert state["health_status"] == "healthy"
    assert message in state["last_error"]


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
    if service.host_control is None:
        service.host_control = UpdateSnapshotHost()  # type: ignore[assignment]
    job = db.next_queued_job()
    assert job is not None
    return job


def test_get_resource_builds_only_requested_item_with_one_state_query(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = Settings(
        raw={
            "resources": {
                vmid: {
                    "resource_type": "lxc",
                    "adapter": "apt",
                    "enabled": True,
                    "monitoring": {"inspect": True, "update_scan": False},
                    "operator_capabilities": {},
                }
                for vmid in (101, 102, 103)
            },
            "mqtt": {"enabled": False},
        },
        config_path=tmp_path / "config.yaml",
        db_path=tmp_path / "single-resource.db",
        api_token="x" * 64,
    )
    db = Database(cfg.db_path)
    service = OpsService(cfg, db, WorkflowExecutor())
    calls: list[int] = []
    original = db.get_resource_state

    def counted(vmid: int) -> dict[str, Any] | None:
        calls.append(vmid)
        return original(vmid)

    monkeypatch.setattr(db, "get_resource_state", counted)

    item = service.get_resource(102)

    assert item["vmid"] == 102
    assert calls == [102]


def test_executor_data_null_is_safe_across_refresh_scan_and_lifecycle(
    tmp_path: Path,
) -> None:
    class NullDataExecutor(WorkflowExecutor):
        def __init__(self) -> None:
            super().__init__()
            self.status_calls = 0

        def run(self, action: str, vmid: int, **kwargs: Any) -> dict[str, Any]:
            if action in {"inspect", "scan"}:
                self.actions.append(action)
                return {"ok": True, "data": None}
            if action == "status":
                self.status_calls += 1
                if self.status_calls == 1:
                    return {"ok": True, "data": {"lxc_status": "running"}}
                return {"ok": True, "data": None}
            if action == "reboot":
                return {"ok": True, "data": None}
            return super().run(action, vmid, **kwargs)

    service, _ = service_with(tmp_path, NullDataExecutor())

    assert service.refresh_container(106)["health_status"] == "unknown"
    assert service.scan_container(106)["status"] == "up_to_date"
    with pytest.raises(ValueError, match="requires a running container.*unknown"):
        service.lifecycle_container(106, "reboot")

    class NullInitialStatusExecutor(WorkflowExecutor):
        def run(self, action: str, vmid: int, **kwargs: Any) -> dict[str, Any]:
            if action == "status":
                return {"ok": True, "data": None}
            return super().run(action, vmid, **kwargs)

    initial_status_path = tmp_path / "initial-status"
    initial_status_path.mkdir()
    second, _ = service_with(
        initial_status_path,
        NullInitialStatusExecutor(),
    )
    with pytest.raises(ValueError, match="current state is unknown"):
        second.lifecycle_container(106, "start")


def test_null_preflight_data_blocks_job_without_type_error(tmp_path: Path) -> None:
    class NullPreflightExecutor(WorkflowExecutor):
        def run(
            self,
            action: str,
            vmid: int,
            argument: Any = None,
            timeout: Any = None,
            on_event: Any = None,
        ) -> dict[str, Any]:
            if action == "preflight":
                return {"ok": True, "data": None}
            return super().run(action, vmid, argument, timeout, on_event)

    service, db = service_with(tmp_path, NullPreflightExecutor())
    job = approved_job(service, db)

    service._run_job(job)

    assert db.get_job(job["id"])["status"] == "blocked"


def test_null_update_and_verify_data_do_not_change_success_to_worker_error(
    tmp_path: Path,
) -> None:
    class NullUpdateVerifyExecutor(WorkflowExecutor):
        def run(
            self,
            action: str,
            vmid: int,
            argument: Any = None,
            timeout: Any = None,
            on_event: Any = None,
        ) -> dict[str, Any]:
            if action in {"update", "verify"}:
                return {"ok": True, "data": None}
            return super().run(action, vmid, argument, timeout, on_event)

    service, db = service_with(tmp_path, NullUpdateVerifyExecutor())
    job = approved_job(service, db)

    service._run_job(job)

    completed = db.get_job(job["id"])
    assert completed["status"] == "success"
    assert service.get_state(106)["packages_updated_count"] == 0


def test_stabilization_treats_null_executor_data_as_unhealthy_not_type_error() -> None:
    class NullInspectExecutor:
        def run(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            return {"ok": True, "data": None}

    clock = FakeClock()
    stabilizer = Stabilizer(
        NullInspectExecutor(),  # type: ignore[arg-type]
        threading.Event(),
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    policy = StabilizationPolicy(
        post_update_timeout_seconds=1,
        poll_interval_seconds=1,
        initial_grace_seconds=0,
        required_consecutive_successes=1,
    )

    with pytest.raises(ExecutorError, match="stabilization timed out"):
        stabilizer.wait(
            vmid=106,
            phase="update",
            timeout_seconds=1,
            policy=policy,
            emit=lambda **kwargs: None,
        )


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
    state = service.get_state(106)
    assert state["operation_status"] == "success"
    assert state["job_stage"] == "completed"
    assert state["last_operation_result"] == "success"
    assert state["update_status"] == "up_to_date"
    assert state["pending_updates"] == 0
    assert state["verification_status"] == "passed"
    assert state["snapshot_name"].startswith("hubinet-ops-106-pre-")
    assert len(state["snapshot_name"]) <= 40


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
    assert "rollback" not in executor.actions
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
    service._reconcile_startup_jobs()
    state = service.get_state(106)
    assert restarted_db.get_job(job["id"])["status"] == "interrupted"
    assert state["operation_status"] == "failed"
    assert state["job_stage"] == "failed"
    assert state["job_progress"] == 100
    assert "restarted" in state["last_error"]


def test_manual_rollback_requires_policy_and_failed_snapshot(tmp_path: Path) -> None:
    snapshot = "hubinet-ops-106-pre-20260724T180227Z"

    class RollbackHost:
        def status(self, vmid: int) -> dict[str, Any]:
            return {"lxc_status": "running", "runtime_status": "running"}

        def list_snapshots(self, vmid: int) -> list[dict[str, Any]]:
            return [
                {
                    "name": snapshot,
                    "kind": "pre-update",
                    "vmid": 106,
                    "owned_by_hubinet_ops": True,
                    "rollback_eligible": True,
                    "delete_eligible": True,
                    "source_job_id": "c" * 32,
                    "pve_snaptime": 1785329640,
                }
            ]

        def execute(
            self,
            operation_type: str,
            vmid: int,
            request_id: str,
            *,
            snapshot_name: str | None = None,
            snapshot_kind: str | None = None,
            expected_source_job_id: str | None = None,
            expected_pve_snaptime: int | None = None,
            release_fingerprint: str | None = None,
        ) -> dict[str, Any]:
            assert operation_type == "snapshot_rollback"
            assert snapshot_name == snapshot
            return {"lxc_status": "running", "runtime_status": "running"}

    executor = WorkflowExecutor([docker_state(0), docker_state(3), docker_state(3)])
    service, db = service_with(
        tmp_path,
        executor,
        host_control=RollbackHost(),
    )
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
        snapshot_name=snapshot,
    )
    db.record_pre_update_snapshot_proof(
        source["id"], 106, snapshot, "c" * 32, 1785329640
    )
    db.update_job(source["id"], status="failed", stage="failed", progress=100)
    db.update_plan_status(plan["id"], "failed")
    result = service.manual_rollback(106)
    assert result["status"] == "success"
    assert "rollback" not in executor.actions
    assert "capabilities" in executor.actions

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
            if action == "scan":
                self.actions.append(action)
                return {
                    "ok": True,
                    "data": {
                        "pending_count": 1,
                        "packages": [{"name": "curl"}],
                        "fingerprint": "recovery-after-rollback",
                    },
                }
            return super().run(action, vmid, argument, timeout, on_event)

    executor = VerificationFailureExecutor()
    service, db = service_with(tmp_path, executor, post_update_timeout=2)
    job = approved_job(service, db)
    service._run_job(job)
    assert db.get_job(job["id"])["status"] == "rolled_back"
    assert "rollback" not in executor.actions
    state = service.get_state(106)
    assert state["verification_status"] == "unknown"
    assert state["apt_check_ok"] is None
    assert state["packages_remaining_count"] is None
    assert any(
        "apt-get check failed" in event["message"]
        for event in db.list_job_events(job["id"])
    )
    recovery = service.scan_container(106, source="recovery")
    assert recovery["plan"]["id"] != job["plan_id"]


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
    assert "rollback" not in executor.actions
    state = service.get_state(106)
    assert state["verification_status"] == "unknown"
    assert state["packages_remaining_count"] is None
    assert any(
        message in event["message"]
        for event in db.list_job_events(job["id"])
    )
