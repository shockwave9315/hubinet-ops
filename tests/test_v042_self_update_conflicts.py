from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest
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
    def inspect_self_update_release(self, vmid: int) -> dict[str, Any]:
        raise HostControlError(
            "No approved Hubinet Ops release is staged",
            http_status=409,
            code="staged_release_missing",
        )


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


def test_missing_staged_release_returns_structured_nonempty_409(
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

    assert response.status_code == 409
    assert response.json() == {
        "detail": {
            "code": "staged_release_missing",
            "message": "No approved Hubinet Ops release is staged",
            "required_action": (
                "Stage and validate the signed Hubinet Ops release on PVE, "
                "then refresh CT110."
            ),
        }
    }
    assert response.json() != {}


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


@pytest.mark.parametrize("release_state", ["none", "unknown", "unavailable"])
def test_ha_self_update_requests_backend_when_release_sensor_is_missing(
    release_state: str,
) -> None:
    script = _ha_self_update_script()
    branch = script["sequence"][0]["choose"][0]

    assert release_state in {"none", "unknown", "unavailable"}
    assert branch["conditions"] == "{{ vmid | int == 110 }}"
    assert branch["sequence"][0]["action"] == "rest_command.hubinet_ops_self_update_plan"
    assert "self_update_release_version" not in str(script)


def test_ha_self_update_does_not_request_backend_for_other_vmids() -> None:
    script = _ha_self_update_script()
    choose_step = script["sequence"][0]
    choose = choose_step["choose"]

    assert len(choose) == 1
    assert choose[0]["conditions"] == "{{ vmid | int == 110 }}"
    assert choose[0]["sequence"][0]["action"] == (
        "rest_command.hubinet_ops_self_update_plan"
    )
    assert choose_step["default"][0]["variables"]["response"]["status"] == 403


def test_ha_self_update_presents_structured_backend_conflicts() -> None:
    script = str(_ha_self_update_script())

    assert "detail.get('code', '')" in script
    assert "detail.get('message'," in script
    assert "staged_release_missing" in script
    assert "Brak przygotowanego wydania" in script
    assert "error_message" in script
    assert "Backend odrzucił przygotowanie planu aktualizacji" in script


def test_hostd_labels_missing_staged_release_with_stable_code() -> None:
    hostd = Path("deploy/pve/hubinet_ops_hostd.py").read_text(encoding="utf-8")

    assert '"staged_release_missing"' in hostd
    assert 'message == "No approved Hubinet Ops release is staged"' in hostd
