from pathlib import Path
from typing import Any

from app.config import Settings
from app.database import Database
from app.service import OpsService


class FakeExecutor:
    def run(self, action: str, vmid: int, argument: str | None = None, timeout: int | None = None) -> dict[str, Any]:
        if action == "inspect":
            return {
                "ok": True,
                "data": {
                    "lxc_status": "running",
                    "health": "healthy",
                    "health_score": 100,
                    "disk": {"free_mb": 6000, "used_percent": 36.0},
                    "memory": {"used_percent": 10.0},
                    "docker": {"enabled": vmid == 106, "healthy": 3, "total": 3},
                },
            }
        if action == "scan":
            return {
                "ok": True,
                "data": {
                    "pending_count": 2,
                    "packages": [
                        {"name": "systemd", "current": "1", "target": "2"},
                        {"name": "curl", "current": "1", "target": "2"},
                    ],
                    "fingerprint": "abc",
                },
            }
        raise AssertionError(action)


def settings(tmp_path: Path) -> Settings:
    return Settings(
        raw={
            "scheduler": {"approval_ttl_minutes": 60},
            "home_assistant": {},
            "containers": {
                106: {
                    "name": "pogoda",
                    "enabled": True,
                    "adapter": "apt",
                    "criticality": "low",
                    "automatic_rollback": True,
                    "dashboard_path": "/hubinet-ops/ct-106",
                }
            },
        },
        config_path=tmp_path / "config.yaml",
        db_path=tmp_path / "ops.db",
        api_token="x" * 64,
    )


def test_refresh_and_scan_create_dashboard_state(tmp_path: Path) -> None:
    cfg = settings(tmp_path)
    db = Database(cfg.db_path)
    service = OpsService(cfg, db, FakeExecutor())  # type: ignore[arg-type]
    service._ensure_initial_states()

    refreshed = service.refresh_container(106)
    assert refreshed["status"] == "healthy"
    assert refreshed["docker"]["healthy"] == 3

    result = service.scan_container(106)
    assert result["status"] == "plan_created"
    state = service.get_state(106)
    assert state["status"] == "waiting_approval"
    assert state["pending_updates"] == 2
    assert state["risk"] == "high"
    assert state["active_plan_id"]
