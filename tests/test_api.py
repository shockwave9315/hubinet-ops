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
