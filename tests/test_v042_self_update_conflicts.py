from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import yaml
from fastapi.testclient import TestClient

from app.database import Database
from app.host_control import HostControlError
from scripts.validate_yaml import HomeAssistantLoader
from tests.test_lifecycle_snapshots import (
    CompatibleExecutor,
    FakeHostControl,
    settings,
)


class MissingReleaseHost(FakeHostControl):
    def check_application_release(self, vmid: int) -> dict[str, Any]:
        return {
            "status": "no_release_published",
            "current_version": "0.4.3",
            "latest_version": None,
        }


class UnavailableReleaseHost(FakeHostControl):
    def check_application_release(self, vmid: int) -> dict[str, Any]:
        raise HostControlError("GitHub release discovery timed out", http_status=502)


def _import_main(tmp_path: Path, monkeypatch):
    config_path = tmp_path / "import-config.yaml"
    config_path.write_text(
        "scheduler:\n  enabled: false\ncontainers:\n  110:\n    enabled: true\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HUBINET_OPS_CONFIG", str(config_path))
    monkeypatch.setenv("HUBINET_OPS_DB", str(tmp_path / "import.db"))
    monkeypatch.setenv("HUBINET_OPS_API_TOKEN", "i" * 64)
    return importlib.import_module("app.main")


def test_no_published_release_returns_structured_http_200_without_a_plan(
    tmp_path: Path,
    monkeypatch,
) -> None:
    main = _import_main(tmp_path, monkeypatch)
    cfg = settings(tmp_path, vmid=110, adapter="agent_self")
    client = TestClient(
        main.create_app(
            cfg,
            database=Database(cfg.db_path),
            executor=CompatibleExecutor(),
            host_control=MissingReleaseHost(),
        )
    )

    response = client.post(
        "/api/v1/resources/110/self-update",
        headers={"Authorization": f"Bearer {cfg.api_token}"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "status": "no_release_published",
        "current_version": "0.4.3",
        "latest_version": None,
    }


def test_release_transport_failure_is_a_gateway_error_not_a_business_conflict(
    tmp_path: Path,
    monkeypatch,
) -> None:
    main = _import_main(tmp_path, monkeypatch)
    cfg = settings(tmp_path, vmid=110, adapter="agent_self")
    response = TestClient(
        main.create_app(
            cfg,
            database=Database(cfg.db_path),
            executor=CompatibleExecutor(),
            host_control=UnavailableReleaseHost(),
        )
    ).post(
        "/api/v1/resources/110/self-update",
        headers={"Authorization": f"Bearer {cfg.api_token}"},
    )

    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "application_release_unavailable"


def test_real_self_update_plan_conflict_has_stable_error_code(
    tmp_path: Path,
    monkeypatch,
) -> None:
    main = _import_main(tmp_path, monkeypatch)
    cfg = settings(tmp_path, vmid=110, adapter="agent_self")
    db = Database(cfg.db_path)
    db.create_plan(
        vmid=110,
        container_name="ct-110",
        fingerprint="apt-plan",
        risk="high",
        payload={"plan_type": "apt"},
        ttl_minutes=60,
    )
    client = TestClient(
        main.create_app(
            cfg,
            database=db,
            executor=CompatibleExecutor(),
            host_control=FakeHostControl(),
        )
    )

    response = client.post(
        "/api/v1/resources/110/self-update",
        headers={"Authorization": f"Bearer {cfg.api_token}"},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "active_plan_conflict"
    assert response.json()["detail"]["required_action"]


def _ha_self_update_script() -> dict[str, Any]:
    package = yaml.load(
        Path("home-assistant/packages/hubinet_ops.yaml").read_text(encoding="utf-8"),
        Loader=HomeAssistantLoader,
    )
    return package["script"]["hubinet_ops_self_update"]


def test_ha_application_check_always_requests_configured_backend_discovery() -> None:
    script = _ha_self_update_script()
    assert script["sequence"] == [
        {"action": "script.hubinet_ops_check_application_release"}
    ]


def test_ha_has_separate_system_and_application_actions() -> None:
    package = yaml.load(
        Path("home-assistant/packages/hubinet_ops.yaml").read_text(encoding="utf-8"),
        Loader=HomeAssistantLoader,
    )
    scripts = package["script"]
    for name in (
        "hubinet_ops_scan_ct110_system",
        "hubinet_ops_approve_ct110_system",
        "hubinet_ops_check_application_release",
        "hubinet_ops_install_application_release",
    ):
        assert name in scripts
    assert "staged_release_missing" not in str(scripts)


def test_hostd_exposes_release_discovery_without_a_user_supplied_repository() -> None:
    hostd = Path("deploy/pve/hubinet_ops_hostd.py").read_text(encoding="utf-8")

    assert "application-release" in hostd
    assert "application-release-check" in hostd
    assert "repo" not in Path("app/main.py").read_text(encoding="utf-8").split(
        'def self_update_resource', 1
    )[1].split('def retry_healthcheck', 1)[0]
