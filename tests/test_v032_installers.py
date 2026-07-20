from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).parents[1]
UPGRADE = ROOT / "deploy" / "upgrade-0.3.2-from-pve.sh"
HA_INSTALLER = ROOT / "deploy" / "install-ha-0.3.2-from-pve.sh"


def test_032_upgrade_updates_agent_and_wrapper_transactionally_only() -> None:
    text = UPGRADE.read_text(encoding="utf-8")

    assert 'AGENT_VMID=110' in text
    assert 'VERSION = "0.3.2"' in text
    assert "deploy/agent/backup-0.3.0.sh" in text
    assert "deploy/agent/restore-0.3.0.sh" in text
    assert "deploy/pve/hubinet-ops-host" in text
    assert 'cp -a "$HOST_WRAPPER" "$WRAPPER_BACKUP"' in text
    assert 'cp -a "$WRAPPER_BACKUP" "$HOST_WRAPPER"' in text
    assert 'install -o root -g root -m 0755 "$SOURCE_WRAPPER" "$HOST_WRAPPER"' in text
    assert 'python3 -m py_compile "$SOURCE_DIR"/app/*.py' in text
    assert 'bash -n "$SOURCE_WRAPPER"' in text
    assert 'bash -n "$HOST_WRAPPER"' in text
    assert "SSH_ORIGINAL_COMMAND='inspect 100' /usr/local/sbin/hubinet-ops-host" in text
    assert "SSH_ORIGINAL_COMMAND='inspect 106' /usr/local/sbin/hubinet-ops-host" in text
    assert 'validate_wrapper_inspect qemu "$qemu_smoke"' in text
    assert 'validate_wrapper_inspect lxc "$lxc_smoke"' in text
    assert 'data["qemu_status"] == "running" and usage is None' in text
    assert 'tar -C "$SOURCE_DIR" -czf "$ARCHIVE" app' in text
    assert 'VALIDATION_NOT_BEFORE="$(date -u +%Y-%m-%dT%H:%M:%S+00:00)"' in text
    assert '"version":"0.3.2"' in text
    assert "/api/v1/states" in text
    assert 'set(resources) != expected_vmids' in text
    assert 'utc_timestamp(resources[vmid].get("last_refresh")) < validation_not_before' in text
    assert "math.isfinite(usage)" in text
    assert "0 <= usage <= 100" in text
    assert 'vm100.get("health_status") != "healthy"' in text
    assert 'ct106.get("health_status") != "healthy"' in text
    assert 'ct110.get("health_status") != "healthy"' in text
    assert 'ct110.get("health_score") != 100' in text
    for forbidden in (
        "install-managed.sh",
        "deploy/pve/managed-vmids",
        "deploy/pve/maintenance-vmids",
        "deploy/pve/lifecycle-vmids",
        "deploy/pve/observation-vmids",
        "deploy/pve/resource-types",
        "hubinet-maint",
        "pct start",
        "pct stop",
        "pct reboot",
        "pct snapshot",
    ):
        assert forbidden not in text


def test_032_ha_installer_backs_up_rolls_back_checks_and_never_restarts() -> None:
    text = HA_INSTALLER.read_text(encoding="utf-8")

    assert "before-0.3.2" in text
    assert "secrets.yaml" in text
    assert "hubinet_ops.package.yaml" in text
    assert "hubinet_ops.dashboard.yaml" in text
    assert "backup.complete" in text
    assert "restore_ha" in text
    assert 'generate_ha_dashboard.py" --check' in text
    assert "ha core check" in text
    assert "ha core restart" not in text
    assert ".storage/core.entity_registry" not in text
