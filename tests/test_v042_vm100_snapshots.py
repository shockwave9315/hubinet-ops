from __future__ import annotations

import importlib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings, validate_config
from app.database import Database
from app.service import OpsService
from tests.test_lifecycle_snapshots import (
    CompatibleExecutor,
    FakeHostControl,
    run_queued,
)


def _settings(tmp_path: Path, *, retention: int = 3) -> Settings:
    capabilities = {
        name: name in {"snapshot_create", "snapshot_list", "snapshot_delete"}
        for name in (
            "refresh",
            "scan",
            "approve",
            "reject",
            "retry_healthcheck",
            "rollback",
            "start",
            "shutdown",
            "reboot",
            "force_stop",
            "snapshot_create",
            "snapshot_list",
            "snapshot_rollback",
            "snapshot_delete",
            "self_update",
        )
    }
    raw = {
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
                "operator_capabilities": capabilities,
                "snapshot_retention_count": retention,
                "pre_update_snapshot": False,
                "automatic_rollback": False,
                "manual_rollback_allowed": False,
                "manual_snapshot_restore_allowed": False,
                "recovery_scan": {"enabled": False},
                "repair_actions": [],
                "required_services": [],
                "docker": {"enabled": False, "require_health": False, "required": []},
            }
        },
    }
    validate_config(raw)
    return Settings(
        raw=raw,
        config_path=tmp_path / "config.yaml",
        db_path=tmp_path / "ops.db",
        api_token="t" * 64,
    )


def test_snapshot_create_request_requires_a_json_boolean(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "HUBINET_OPS_CONFIG",
        str(Path("config/config.example.yaml").resolve()),
    )
    monkeypatch.setenv("HUBINET_OPS_DB", str(tmp_path / "import.db"))
    monkeypatch.setenv("HUBINET_OPS_API_TOKEN", "i" * 64)
    monkeypatch.setenv("HUBINET_OPS_HOSTD_BACKEND_TOKEN", "b" * 64)
    monkeypatch.setenv("HUBINET_OPS_HOSTD_UPDATE_TOKEN", "u" * 64)
    main = importlib.import_module("app.main")
    SnapshotCreateRequest = main.SnapshotCreateRequest

    assert SnapshotCreateRequest().include_ram is False
    assert SnapshotCreateRequest(include_ram=True).include_ram is True
    with pytest.raises(ValidationError):
        SnapshotCreateRequest(include_ram="true")


def test_vm100_snapshots_refresh_and_apply_retention_for_both_ram_modes(
    tmp_path: Path,
) -> None:
    cfg = _settings(tmp_path, retention=1)
    db = Database(cfg.db_path)
    host = FakeHostControl(status="running")
    service = OpsService(cfg, db, CompatibleExecutor(), host_control=host)
    service._now = lambda: datetime(2026, 7, 29, 12, 0, 0, tzinfo=UTC)

    first = service.queue_snapshot_create(
        100, "vm100-snapshot-without-ram", include_ram=False
    )
    assert first["operation_type"] == "snapshot_create"
    assert run_queued(service, db)["status"] == "success"
    service._now = lambda: datetime(2026, 7, 29, 12, 0, 1, tzinfo=UTC)
    second = service.queue_snapshot_create(
        100, "vm100-snapshot-with-ram-001", include_ram=True
    )
    assert second["operation_type"] == "snapshot_create_ram"
    assert run_queued(service, db)["status"] == "success"

    state = service.get_state(100)
    assert state["snapshot_count"] == 1
    assert state["snapshot_state_stale"] is False
    operations = [call[0] for call in host.calls]
    assert "snapshot_create" in operations
    assert "snapshot_create_ram" in operations
    assert "snapshot_delete" in operations


def test_vm100_policy_rejects_lifecycle_restore_and_updates(tmp_path: Path) -> None:
    cfg = _settings(tmp_path)
    db = Database(cfg.db_path)
    service = OpsService(
        cfg,
        db,
        CompatibleExecutor(),
        host_control=FakeHostControl(status="running"),
    )

    with pytest.raises(ValueError, match="only for LXC"):
        service.queue_lifecycle(100, "shutdown", "vm100-shutdown-blocked")
    with pytest.raises(ValueError, match="blocked by policy"):
        service.queue_snapshot_action(
            100,
            "rollback",
            "hubinet-ops-100-manual-20260729T120000Z",
            "vm100-restore-blocked",
        )
    with pytest.raises(ValueError):
        service.scan_container(100)


def test_pve_vm100_allowlists_are_snapshot_only() -> None:
    root = Path("deploy/pve")
    for filename in (
        "host-control-vmids",
        "snapshot-create-vmids",
        "snapshot-delete-vmids",
    ):
        assert "100" in (root / filename).read_text(encoding="utf-8").splitlines()
    for filename in (
        "managed-vmids",
        "maintenance-vmids",
        "lifecycle-vmids",
        "snapshot-restore-vmids",
    ):
        assert "100" not in (root / filename).read_text(encoding="utf-8").splitlines()


def test_ha_vm100_ram_toggle_defaults_off_and_never_exposes_restore() -> None:
    package = Path("home-assistant/packages/hubinet_ops.yaml").read_text(
        encoding="utf-8"
    )
    assert "hubinet_ops_vm100_snapshot_include_ram:" in package
    assert "name: Dołącz stan RAM" in package
    assert "initial: false" in package
    assert '"include_ram":{{ include_ram' in package

    dashboard = Path("home-assistant/dashboards/hubinet_ops.yaml").read_text(
        encoding="utf-8"
    )
    vm100 = dashboard.split("path: vm-100", 1)[1].split("path: ct-101", 1)[0]
    assert "Snapshot nie obejmie stanu RAM" in vm100
    assert "script.hubinet_ops_snapshot_create" in vm100
    assert "script.hubinet_ops_snapshot_restore" not in vm100
    assert "script.hubinet_ops_start_container" not in vm100
