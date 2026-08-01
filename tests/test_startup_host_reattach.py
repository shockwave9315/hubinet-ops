from __future__ import annotations

import threading
import hashlib
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest

from app.config import Settings
from app.database import Database
from app.host_control import HostControlClient
from app.service import OpsService
from tests.test_v042_snapshot_retention import _record_snapshot_source


PVE_SNAPTIME = 1_785_286_400


def _expected_identity(snapshot_name: str) -> dict[str, Any]:
    return {
        "version": 1,
        "vmid": 110,
        "snapshot_name": snapshot_name,
        "kind": "manual",
        "host_source_job_id": hashlib.sha256(
            snapshot_name.encode("utf-8")
        ).hexdigest()[:32],
        "pve_snaptime": PVE_SNAPTIME,
    }


def _remote_argument(operation_type: str, snapshot_name: str | None) -> str | None:
    if operation_type not in {"snapshot_rollback", "snapshot_delete"}:
        return snapshot_name
    identity = _expected_identity(str(snapshot_name))
    return HostControlClient._snapshot_identity_argument(
        vmid=110,
        snapshot_name=str(snapshot_name),
        snapshot_kind="manual",
        expected_source_job_id=str(identity["host_source_job_id"]),
        expected_pve_snaptime=int(identity["pve_snaptime"]),
    )


class NoopExecutor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int, str | None]] = []

    def run(
        self,
        action: str,
        vmid: int,
        argument: str | None = None,
        timeout: int | None = None,
        on_event: Callable[..., None] | None = None,
    ) -> dict[str, Any]:
        self.calls.append((action, vmid, argument))
        return {"ok": True, "data": {}}


def _settings(tmp_path: Path) -> Settings:
    capabilities = {
        "refresh": True,
        "scan": False,
        "approve": True,
        "reject": True,
        "retry_healthcheck": False,
        "rollback": False,
        "start": True,
        "shutdown": True,
        "reboot": True,
        "force_stop": True,
        "snapshot_create": True,
        "snapshot_list": True,
        "snapshot_rollback": True,
        "snapshot_delete": True,
        "self_update": True,
    }
    return Settings(
        raw={
            "scheduler": {"enabled": False},
            "mqtt": {"enabled": False},
            "home_assistant": {},
            "resources": {
                110: {
                    "resource_type": "lxc",
                    "adapter": "agent_self",
                    "name": "hubinet-ops",
                    "enabled": True,
                    "monitoring": {"inspect": True, "update_scan": False},
                    "operator_capabilities": capabilities,
                    "snapshot_retention": 5,
                    "manual_snapshot_restore_allowed": True,
                    "manual_rollback_allowed": False,
                }
            },
        },
        config_path=tmp_path / "config.yaml",
        db_path=tmp_path / "ops.db",
        api_token="a" * 64,
    )


def _client(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    sleep: Callable[[float], None] = lambda _seconds: None,
) -> HostControlClient:
    monkeypatch.setenv("TEST_REATTACH_HOSTD_TOKEN", "t" * 64)
    return HostControlClient(
        {
            "base_url": "http://hostd.invalid:8741",
            "backend_token_env": "TEST_REATTACH_HOSTD_TOKEN",
            "poll_interval_seconds": 0.001,
            "poll_error_retries": 3,
        },
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=sleep,
    )


def _create_active_job(
    db: Database,
    operation_type: str,
    request_id: str,
    *,
    snapshot_name: str | None = None,
    status: str = "running",
) -> dict[str, Any]:
    job, _ = db.create_operation_job(
        vmid=110,
        container_name="hubinet-ops",
        operation_type=operation_type,
        request_id=request_id,
        snapshot_name=snapshot_name,
        result=(
            {"expected_snapshot_identity": _expected_identity(snapshot_name)}
            if snapshot_name is not None
            and operation_type in {"snapshot_rollback", "snapshot_delete"}
            else None
        ),
    )
    if status == "running":
        return db.update_job(
            job["id"],
            status="running",
            stage="executing",
            progress=30,
        )
    return job


def test_snapshot_rollback_startup_reattaches_running_job_and_finalizes_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _settings(tmp_path)
    db = Database(cfg.db_path)
    snapshot = "hubinet-ops-110-manual-20260723T220000Z"
    request_id = "restart-snapshot-rollback-0001"
    local_job = _create_active_job(
        db,
        "snapshot_rollback",
        request_id,
        snapshot_name=snapshot,
    )
    source_job_id = hashlib.sha256(snapshot.encode("utf-8")).hexdigest()[:32]
    _record_snapshot_source(
        db,
        {
            "name": snapshot,
            "kind": "manual",
            "source_job_id": source_job_id,
            "pve_snaptime": PVE_SNAPTIME,
        },
    )
    db.upsert_container_state(
        110,
        {
            "vmid": 110,
            "verification_status": "passed",
            "last_verification": "2026-07-23T22:00:00+00:00",
            "apt_check_ok": True,
            "dpkg_audit_ok": True,
            "packages_remaining_count": 4,
            "pending_updates": 4,
            "update_status": "update_available",
            "updates": {"pending_count": 4, "packages": [{"name": "openssl"}]},
            "operation_status": "running",
            "active_job_id": local_job["id"],
        },
    )
    remote = {
        "id": "host-job-snapshot-rollback",
        "vmid": 110,
        "request_id": request_id,
        "operation_type": "snapshot_rollback",
        "argument": _remote_argument("snapshot_rollback", snapshot),
        "status": "running",
        "stage": "executing",
        "result": None,
        "error": None,
    }
    requests: list[httpx.Request] = []
    poll_waiting = threading.Event()
    allow_poll = threading.Event()

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.startswith("/api/v1/jobs/by-request/"):
            return httpx.Response(200, json=dict(remote))
        if request.url.path == f"/api/v1/jobs/{remote['id']}":
            return httpx.Response(200, json=dict(remote))
        if request.url.path == "/api/v1/resources/110/snapshots":
            return httpx.Response(
                200,
                json={
                    "snapshots": [
                        {
                            "name": snapshot,
                            "kind": "manual",
                            "owned_by_hubinet_ops": True,
                            "source_job_id": source_job_id,
                            "pve_snaptime": PVE_SNAPTIME,
                            "rollback_eligible": True,
                            "delete_eligible": True,
                        }
                    ]
                },
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    def block_before_poll(_seconds: float) -> None:
        poll_waiting.set()
        if not allow_poll.wait(timeout=5):
            raise AssertionError("test did not release host job polling")

    service = OpsService(
        cfg,
        db,
        NoopExecutor(),  # type: ignore[arg-type]
        host_control=_client(monkeypatch, handler, sleep=block_before_poll),
    )
    failures: list[BaseException] = []

    def reconcile() -> None:
        try:
            service._reconcile_startup_jobs()
        except BaseException as exc:
            failures.append(exc)

    startup = threading.Thread(target=reconcile)
    startup.start()
    assert poll_waiting.wait(timeout=5)
    assert db.get_job(local_job["id"])["status"] == "running"
    assert not [request for request in requests if request.method != "GET"]

    remote.update(
        {
            "status": "succeeded",
            "stage": "complete",
            "result": {"action": "rollback", "snapshot_name": snapshot},
        }
    )
    allow_poll.set()
    startup.join(timeout=5)

    assert not startup.is_alive()
    assert failures == []
    terminal = db.get_job(local_job["id"])
    assert terminal["status"] == "success"
    assert terminal["result"]["action"] == "rollback"
    assert terminal["result"]["snapshot_name"] == snapshot
    assert terminal["result"]["expected_snapshot_identity"] == _expected_identity(
        snapshot
    )
    state = service.get_state(110)
    assert state["snapshot_operation_status"] == "success"
    assert state["verification_status"] == "unknown"
    assert state["last_verification"] is None
    assert state["apt_check_ok"] is None
    assert state["dpkg_audit_ok"] is None
    assert state["packages_remaining_count"] is None
    assert state["pending_updates"] is None
    assert state["update_status"] == "unknown"
    assert state["updates"] == {"pending_count": None, "packages": []}
    assert state["snapshot_count"] == 1
    assert requests[0].url.path == (
        f"/api/v1/jobs/by-request/110/{request_id}"
    )
    assert not [request for request in requests if request.method != "GET"]


@pytest.mark.parametrize(
    ("remote_status", "expected_local_status", "expected_operation_status"),
    [
        ("failed", "failed", "failed"),
        ("interrupted", "interrupted", "unknown"),
    ],
)
def test_startup_reattach_propagates_remote_terminal_error_and_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    remote_status: str,
    expected_local_status: str,
    expected_operation_status: str,
) -> None:
    cfg = _settings(tmp_path)
    db = Database(cfg.db_path)
    request_id = f"remote-{remote_status}-rollback-0001"
    snapshot = "hubinet-ops-110-manual-20260723T220100Z"
    local_job = _create_active_job(
        db,
        "snapshot_rollback",
        request_id,
        snapshot_name=snapshot,
    )
    remote_result = {
        "action": "rollback",
        "exit_code": 37,
        "detail": f"exact-{remote_status}-result",
    }
    remote_error = f"exact remote {remote_status} error"
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "id": f"host-{remote_status}",
                "vmid": 110,
                "request_id": request_id,
                "operation_type": "snapshot_rollback",
                "argument": _remote_argument("snapshot_rollback", snapshot),
                "status": remote_status,
                "stage": remote_status,
                "result": remote_result,
                "error": remote_error,
            },
        )

    service = OpsService(
        cfg,
        db,
        NoopExecutor(),  # type: ignore[arg-type]
        host_control=_client(monkeypatch, handler),
    )
    service._reconcile_startup_jobs()

    terminal = db.get_job(local_job["id"])
    assert terminal["status"] == expected_local_status
    assert {
        key: terminal["result"][key] for key in remote_result
    } == remote_result
    assert terminal["result"]["expected_snapshot_identity"] == _expected_identity(
        snapshot
    )
    assert terminal["error"] == remote_error
    state = service.get_state(110)
    assert state["snapshot_operation_status"] == (
        "failed" if remote_status == "failed" else "unknown"
    )
    assert state["operation_status"] == expected_operation_status
    assert [request.method for request in requests] == ["GET"]


@pytest.mark.parametrize("local_status", ["queued", "running"])
def test_missing_remote_job_interrupts_without_replaying_submit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    local_status: str,
) -> None:
    cfg = _settings(tmp_path)
    db = Database(cfg.db_path)
    request_id = f"crash-before-submit-{local_status}-0001"
    local_job = _create_active_job(
        db,
        "lifecycle_reboot",
        request_id,
        status=local_status,
    )
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(404, json={"error": "host job not found"})

    service = OpsService(
        cfg,
        db,
        NoopExecutor(),  # type: ignore[arg-type]
        host_control=_client(monkeypatch, handler),
    )
    service._reconcile_startup_jobs()

    terminal = db.get_job(local_job["id"])
    assert terminal["status"] == "interrupted"
    assert terminal["result"] is None
    assert "outcome is unknown" in terminal["error"]
    assert service.get_state(110)["operation_status"] == "unknown"
    assert [request.method for request in requests] == ["GET"]


@pytest.mark.parametrize(
    "operation_type",
    ["snapshot_create", "snapshot_create_ram"],
)
@pytest.mark.parametrize("retention_target", [0, 1])
def test_successful_snapshot_create_reattach_applies_retention_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation_type: str,
    retention_target: int,
) -> None:
    case_dir = tmp_path / f"{operation_type}-{retention_target}"
    case_dir.mkdir()
    cfg = _settings(case_dir)
    cfg.raw["resources"][110]["snapshot_retention_count"] = retention_target
    cfg.raw["resources"][110].pop("snapshot_retention", None)
    db = Database(cfg.db_path)
    request_id = f"reattach-{operation_type.replace('_', '-')}-{retention_target}-0001"
    snapshot = "hubinet-ops-110-manual-20260730T120000Z"
    host_source_job_id = "a" * 32
    local_job = _create_active_job(
        db,
        operation_type,
        request_id,
        snapshot_name=snapshot,
    )
    remote = {
        "id": host_source_job_id,
        "vmid": 110,
        "request_id": request_id,
        "operation_type": operation_type,
        "argument": _remote_argument(operation_type, snapshot),
        "status": "succeeded",
        "stage": "complete",
        "result": {
            "name": snapshot,
            "kind": "manual",
            "source_job_id": host_source_job_id,
            "pve_snaptime": PVE_SNAPTIME,
        },
        "error": None,
    }
    physical = {
        "name": snapshot,
        "description": (
            "hubinet-ops;kind=manual;"
            "created_at=2026-07-30T12:00:00+00:00;"
            f"source_job_id={host_source_job_id}"
        ),
        "created_at": "2026-07-30T12:00:00+00:00",
        "kind": "manual",
        "owned_by_hubinet_ops": True,
        "ownership_status": "owned",
        "rollback_eligible": True,
        "delete_eligible": True,
        "source_job_id": host_source_job_id,
        "pve_snaptime": PVE_SNAPTIME,
    }
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.startswith("/api/v1/jobs/by-request/"):
            return httpx.Response(200, json=remote)
        if request.url.path == "/api/v1/resources/110/snapshots":
            return httpx.Response(200, json={"snapshots": [physical]})
        raise AssertionError(
            f"unexpected request: {request.method} {request.url.path}"
        )

    service = OpsService(
        cfg,
        db,
        NoopExecutor(),  # type: ignore[arg-type]
        host_control=_client(monkeypatch, handler),
    )

    service._reconcile_startup_jobs()
    request_count = len(requests)
    service._reconcile_startup_jobs()

    source = db.get_job(local_job["id"])
    prunes = [
        job for job in db.list_jobs() if job["operation_type"] == "snapshot_prune"
    ]
    assert source["status"] == "success"
    assert source["result"]["source_job_id"] == host_source_job_id
    assert len(prunes) == (1 if retention_target else 0)
    if retention_target:
        assert prunes[0]["result"]["source_job_id"] == source["id"]
        assert prunes[0]["result"]["retention_target"] == retention_target
    assert len(requests) == request_count
    assert not [request for request in requests if request.method != "GET"]


@pytest.mark.parametrize(
    "operation_type",
    [
        "lifecycle_start",
        "lifecycle_shutdown",
        "lifecycle_reboot",
        "lifecycle_force_stop",
        "snapshot_create",
        "snapshot_rollback",
        "snapshot_delete",
    ],
)
def test_all_host_backed_lifecycle_and_snapshot_jobs_reattach_existing_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation_type: str,
) -> None:
    case_dir = tmp_path / operation_type
    case_dir.mkdir()
    cfg = _settings(case_dir)
    db = Database(cfg.db_path)
    request_id = f"reattach-{operation_type.replace('_', '-')}-0001"
    snapshot = (
        "hubinet-ops-110-manual-20260723T220200Z"
        if operation_type.startswith("snapshot_")
        else None
    )
    local_job = _create_active_job(
        db,
        operation_type,
        request_id,
        snapshot_name=snapshot,
    )
    host_job_id = hashlib.sha256(operation_type.encode("utf-8")).hexdigest()[:32]
    remote_result = (
        {
            "lxc_status": (
                "stopped"
                if operation_type in {
                    "lifecycle_shutdown",
                    "lifecycle_force_stop",
                }
                else "running"
            )
        }
        if operation_type.startswith("lifecycle_")
        else {
            "name": snapshot,
            "kind": "manual",
            "pve_snaptime": PVE_SNAPTIME,
            "operation": operation_type,
            **(
                {"source_job_id": host_job_id}
                if operation_type == "snapshot_create"
                else {}
            ),
        }
    )
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.startswith("/api/v1/jobs/by-request/"):
            return httpx.Response(
                200,
                json={
                    "id": host_job_id,
                    "vmid": 110,
                    "request_id": request_id,
                    "operation_type": operation_type,
                    "argument": _remote_argument(operation_type, snapshot),
                    "status": "succeeded",
                    "stage": "complete",
                    "result": remote_result,
                    "error": None,
                },
            )
        if request.url.path == "/api/v1/resources/110/snapshots":
            return httpx.Response(200, json={"snapshots": []})
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    service = OpsService(
        cfg,
        db,
        NoopExecutor(),  # type: ignore[arg-type]
        host_control=_client(monkeypatch, handler),
    )
    service._reconcile_startup_jobs()

    assert db.get_job(local_job["id"])["status"] == "success"
    assert requests[0].url.path == (
        f"/api/v1/jobs/by-request/110/{request_id}"
    )
    assert not [request for request in requests if request.method != "GET"]
    assert not any(
        job["operation_type"] == "snapshot_prune" for job in db.list_jobs()
    )


def test_self_update_startup_reattaches_by_request_without_execute_post(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _settings(tmp_path)
    db = Database(cfg.db_path)
    fingerprint = "a" * 64
    request_id = "self-update-startup-reattach-0001"
    plan = db.create_plan(
        vmid=110,
        container_name="hubinet-ops",
        fingerprint=fingerprint,
        risk="high",
        payload={
            "plan_type": "self_update",
            "fingerprint": fingerprint,
            "release_id": "hubinet-ops-0.4.0-aaaaaaaaaaaaaaaa",
            "version": "0.4.0",
        },
        ttl_minutes=60,
    )
    approved_plan, local_job = db.approve_plan(
        plan["id"],
        request_id=request_id,
        operation_type="self_update",
    )
    db.update_job(
        local_job["id"],
        status="running",
        stage="self_updating",
        progress=40,
    )
    requests: list[httpx.Request] = []
    remote_result = {
        "fingerprint": fingerprint,
        "release_id": "hubinet-ops-0.4.0-aaaaaaaaaaaaaaaa",
        "version": "0.4.0",
        "exit_code": 0,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "id": "host-self-update",
                "vmid": 110,
                "request_id": request_id,
                "operation_type": "self_update",
                "argument": fingerprint,
                "status": "succeeded",
                "stage": "complete",
                "result": remote_result,
                "error": None,
            },
        )

    service = OpsService(
        cfg,
        db,
        NoopExecutor(),  # type: ignore[arg-type]
        host_control=_client(monkeypatch, handler),
    )
    service._reconcile_startup_jobs()

    assert approved_plan["status"] == "approved"
    assert db.get_job(local_job["id"])["status"] == "success"
    assert db.get_job(local_job["id"])["result"] == remote_result
    assert db.get_plan(plan["id"])["status"] == "completed"
    assert [request.method for request in requests] == ["GET"]
    assert requests[0].url.path == (
        f"/api/v1/jobs/by-request/110/{request_id}"
    )
