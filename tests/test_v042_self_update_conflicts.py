from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from app.database import Database
from app.host_control import HostControlError
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


def test_ha_self_update_preflight_skips_request_and_notifies_in_polish() -> None:
    package = Path("home-assistant/packages/hubinet_ops.yaml").read_text(
        encoding="utf-8"
    )
    script = package.split("  hubinet_ops_self_update:\n", 1)[1].split(
        "\n  hubinet_ops_scan_container:", 1
    )[0]

    assert "self_update_release_version" in script
    assert "release_version not in ['none', 'unknown', 'unavailable', '']" in script
    assert "Brak przygotowanego wydania" in script
    assert "Żądanie nie zostało wysłane" in script
    assert "persistent_notification.create" in script
    assert "Backend odrzucił przygotowanie planu aktualizacji" in script


def test_hostd_labels_missing_staged_release_with_stable_code() -> None:
    hostd = Path("deploy/pve/hubinet_ops_hostd.py").read_text(encoding="utf-8")

    assert '"staged_release_missing"' in hostd
    assert 'message == "No approved Hubinet Ops release is staged"' in hostd
