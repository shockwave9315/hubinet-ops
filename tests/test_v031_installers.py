from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).parents[1]
UPGRADE = ROOT / "deploy" / "upgrade-0.3.1-from-pve.sh"
HA_INSTALLER = ROOT / "deploy" / "install-ha-0.3.1-from-pve.sh"


def test_patch_upgrade_is_ct110_only_and_transactional() -> None:
    text = UPGRADE.read_text(encoding="utf-8")
    backup = (ROOT / "deploy/agent/backup-0.3.0.sh").read_text(encoding="utf-8")

    assert 'AGENT_VMID=110' in text
    assert 'VERSION = "0.3.1"' in text
    assert "deploy/agent/backup-0.3.0.sh" in text
    assert "deploy/agent/restore-0.3.0.sh" in text
    assert "service_action stop" in backup
    assert 'cp -a "$(root_path /etc/hubinet-ops/config.yaml)"' in backup
    assert 'cp -a "$database" "$backup/ops.db"' in backup
    assert "PRAGMA quick_check" in backup
    assert 'tar -C "$SOURCE_DIR" -czf "$ARCHIVE" app' in text
    assert '"version":"0.3.1"' in text
    assert "expected 11" in text
    for forbidden in (
        "install-managed.sh",
        "managed-vmids",
        "maintenance-vmids",
        "lifecycle-vmids",
        "hubinet-maint",
        "pct start",
        "pct stop",
        "pct reboot",
        "pct snapshot",
    ):
        assert forbidden not in text


def test_ha_patch_installer_backs_up_rolls_back_checks_and_never_restarts() -> None:
    text = HA_INSTALLER.read_text(encoding="utf-8")

    assert "before-0.3.1" in text
    assert "secrets.yaml" in text
    assert "hubinet_ops.package.yaml" in text
    assert "hubinet_ops.dashboard.yaml" in text
    assert "backup.complete" in text
    assert "restore_ha" in text
    assert 'generate_ha_dashboard.py" --check' in text
    assert "ha core check" in text
    assert "ha core restart" not in text
    assert ".storage/core.entity_registry" not in text
