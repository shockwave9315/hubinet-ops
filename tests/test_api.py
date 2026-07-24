from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from app.config import Settings
from app.database import Database


class FakeExecutor:
    def run(self, action: str, vmid: int, argument=None, timeout=None, on_event=None) -> dict[str, Any]:
        return {"ok": True, "data": {}}


class FakeSelfUpdateHost:
    def inspect_self_update_release(self, vmid: int) -> dict[str, Any]:
        assert vmid == 110
        return {
            "version": "0.4.0",
            "release_id": "hubinet-ops-0.4.0-aaaaaaaaaaaaaaaa",
            "fingerprint": "a" * 64,
            "file_count": 136,
            "total_bytes": 1000,
        }


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        raw={"scheduler": {"enabled": False}, "mqtt": {"enabled": False}, "home_assistant": {}, "containers": {106: {"enabled": True}}},
        config_path=tmp_path / "config.yaml",
        db_path=tmp_path / "ops.db",
        api_token="t" * 64,
    )


def test_event_endpoints_require_auth_and_bound_limits(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "import-config.yaml"
    config_path.write_text("scheduler:\n  enabled: false\ncontainers:\n  106:\n    enabled: true\n", encoding="utf-8")
    monkeypatch.setenv("HUBINET_OPS_CONFIG", str(config_path))
    monkeypatch.setenv("HUBINET_OPS_DB", str(tmp_path / "import.db"))
    monkeypatch.setenv("HUBINET_OPS_API_TOKEN", "i" * 64)
    main = importlib.import_module("app.main")

    cfg = make_settings(tmp_path)
    db = Database(cfg.db_path)
    plan = db.create_plan(vmid=106, container_name="weather", fingerprint="fp", risk="high", payload={}, ttl_minutes=60)
    _, job = db.approve_plan(plan["id"])
    db.insert_job_event(
        job_id=job["id"], vmid=106, level="info", stage="preflight", progress=10,
        event_type="started", message="started",
    )
    client = TestClient(main.create_app(cfg, database=db, executor=FakeExecutor()))
    headers = {"Authorization": f"Bearer {cfg.api_token}"}

    assert client.get(f"/api/v1/jobs/{job['id']}/events").status_code == 401
    response = client.get(f"/api/v1/jobs/{job['id']}/events?limit=1", headers=headers)
    assert response.status_code == 200
    assert response.json()[0]["event_type"] == "started"
    assert client.get("/api/v1/containers/106/events?limit=201", headers=headers).status_code == 422
    assert client.get("/api/v1/containers/999/events", headers=headers).status_code == 404
    assert client.get("/api/v1/jobs/missing/events", headers=headers).status_code == 404


def test_canonical_resources_include_qemu_and_container_alias_filters_it(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "resources-import.yaml"
    config_path.write_text(
        "scheduler:\n  enabled: false\ncontainers:\n  106:\n    enabled: true\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HUBINET_OPS_CONFIG", str(config_path))
    monkeypatch.setenv("HUBINET_OPS_DB", str(tmp_path / "resources-import.db"))
    monkeypatch.setenv("HUBINET_OPS_API_TOKEN", "i" * 64)
    main = importlib.import_module("app.main")
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
            "mqtt": {"enabled": False},
            "home_assistant": {},
            "resources": {
                100: {
                    "name": "home-assistant",
                    "display_name": "Home Assistant",
                    "resource_type": "qemu",
                    "adapter": "haos",
                    "enabled": True,
                    "monitoring": {"inspect": True, "update_scan": False},
                    "operator_capabilities": denied,
                },
                101: {
                    "name": "cloudflared",
                    "resource_type": "lxc",
                    "adapter": "apt",
                    "enabled": True,
                    "monitoring": {"inspect": True, "update_scan": True},
                    "operator_capabilities": denied,
                },
                110: {
                    "name": "hubinet-ops",
                    "resource_type": "lxc",
                    "adapter": "agent_self",
                    "enabled": True,
                    "monitoring": {"inspect": True, "update_scan": False},
                    "operator_capabilities": denied,
                },
            },
        },
        config_path=tmp_path / "config.yaml",
        db_path=tmp_path / "resources.db",
        api_token="t" * 64,
    )
    client = TestClient(main.create_app(cfg, executor=FakeExecutor()))
    headers = {"Authorization": f"Bearer {cfg.api_token}"}

    resources = client.get("/api/v1/resources", headers=headers)
    containers = client.get("/api/v1/containers", headers=headers)

    assert resources.status_code == 200
    assert client.get("/api/v1/resources").status_code == 401
    assert [item["vmid"] for item in resources.json()] == [100, 101, 110]
    assert resources.json()[0]["resource_type"] == "qemu"
    assert [item["vmid"] for item in containers.json()] == [101, 110]
    assert client.get("/api/v1/resources/100", headers=headers).json()["adapter"] == "haos"
    assert client.get("/api/v1/containers/100/state", headers=headers).status_code == 404
    assert client.post("/api/v1/resources/100/scan", headers=headers).status_code == 409
    assert client.post("/api/v1/resources/100/start", headers=headers).status_code == 409
    assert client.post("/api/v1/resources/101/refresh", headers=headers).status_code == 409
    assert client.post("/api/v1/resources/101/scan", headers=headers).status_code == 409
    assert client.post("/api/v1/resources/110/scan", headers=headers).status_code == 409
    assert client.post("/api/v1/resources/110/reboot", headers=headers).status_code == 409


def test_state_endpoint_is_singular_and_active_plan_routes_return_explicit_conflict(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "active-plan-import.yaml"
    config_path.write_text(
        "scheduler:\n  enabled: false\ncontainers:\n  106:\n    enabled: true\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HUBINET_OPS_CONFIG", str(config_path))
    monkeypatch.setenv("HUBINET_OPS_DB", str(tmp_path / "active-plan-import.db"))
    monkeypatch.setenv("HUBINET_OPS_API_TOKEN", "i" * 64)
    main = importlib.import_module("app.main")
    cfg = Settings(
        raw={
            "scheduler": {"enabled": False},
            "mqtt": {"enabled": False},
            "home_assistant": {},
            "resources": {
                106: {
                    "resource_type": "lxc",
                    "adapter": "apt",
                    "enabled": True,
                    "monitoring": {"inspect": True, "update_scan": True},
                    "operator_capabilities": {"approve": True, "reject": True},
                }
            },
        },
        config_path=tmp_path / "config.yaml",
        db_path=tmp_path / "ops.db",
        api_token="t" * 64,
    )
    client = TestClient(main.create_app(cfg, executor=FakeExecutor()))
    headers = {"Authorization": f"Bearer {cfg.api_token}"}

    assert client.get("/api/v1/state", headers=headers).status_code == 200
    assert client.get("/api/v1/states", headers=headers).status_code == 404
    approve = client.post(
        "/api/v1/resources/106/plans/approve-active",
        headers=headers,
    )
    reject = client.post(
        "/api/v1/resources/106/plans/reject-active",
        headers=headers,
    )
    assert approve.status_code == 409
    assert reject.status_code == 409
    assert "no active waiting plan" in approve.json()["detail"]
    assert "no active waiting plan" in reject.json()["detail"]


def test_self_update_endpoint_creates_plan_before_active_approval(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "self-update-import.yaml"
    config_path.write_text(
        "scheduler:\n  enabled: false\ncontainers:\n  106:\n    enabled: true\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HUBINET_OPS_CONFIG", str(config_path))
    monkeypatch.setenv("HUBINET_OPS_DB", str(tmp_path / "self-update-import.db"))
    monkeypatch.setenv("HUBINET_OPS_API_TOKEN", "i" * 64)
    main = importlib.import_module("app.main")
    cfg = Settings(
        raw={
            "scheduler": {"enabled": False, "approval_ttl_minutes": 60},
            "mqtt": {"enabled": False},
            "home_assistant": {},
            "resources": {
                110: {
                    "name": "hubinet-ops",
                    "resource_type": "lxc",
                    "adapter": "agent_self",
                    "enabled": True,
                    "monitoring": {"inspect": True, "update_scan": False},
                    "operator_capabilities": {
                        "self_update": True,
                        "approve": True,
                        "reject": True,
                    },
                }
            },
        },
        config_path=tmp_path / "config.yaml",
        db_path=tmp_path / "self-update.db",
        api_token="t" * 64,
    )
    db = Database(cfg.db_path)
    client = TestClient(
        main.create_app(
            cfg,
            database=db,
            executor=FakeExecutor(),
            host_control=FakeSelfUpdateHost(),  # type: ignore[arg-type]
        )
    )
    headers = {"Authorization": f"Bearer {cfg.api_token}"}

    planned = client.post("/api/v1/resources/110/self-update", headers=headers)

    assert planned.status_code == 200
    assert planned.json()["plan"]["status"] == "waiting_approval"
    assert db.list_jobs() == []
    approved = client.post(
        "/api/v1/resources/110/plans/approve-active",
        headers=headers,
        json={"request_id": "ha-self-update-approval-0001"},
    )
    assert approved.status_code == 200
    assert approved.json()["job"]["operation_type"] == "self_update"
