from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml


ROOT = Path(__file__).parents[1]
UPGRADE = ROOT / "deploy" / "upgrade-0.4.2-from-pve.sh"
HA_INSTALLER = ROOT / "deploy" / "install-ha-0.4.2-from-pve.sh"


def test_release_versions_preserve_executor_and_database_contract() -> None:
    assert 'VERSION = "0.4.2"' in (ROOT / "app/mqtt.py").read_text(
        encoding="utf-8"
    )
    assert 'VERSION = "0.4.2"' in (
        ROOT / "deploy/pve/hubinet_ops_hostd.py"
    ).read_text(encoding="utf-8")
    assert 'EXECUTOR_VERSION = "0.4.1"' in (
        ROOT / "app/contracts.py"
    ).read_text(encoding="utf-8")
    assert 'VERSION = "0.4.1"' in (
        ROOT / "deploy/managed/hubinet-maint"
    ).read_text(encoding="utf-8")
    assert "PRAGMA user_version=400" in (
        ROOT / "app/database.py"
    ).read_text(encoding="utf-8")


def test_upgrade_is_exactly_041_to_042_transactional_and_read_only_at_validation() -> None:
    text = UPGRADE.read_text(encoding="utf-8")
    assert "supports only an installed 0.4.1 backend" in text
    assert "supports only an installed 0.4.1 hostd" in text
    assert 'data.get("version") != "0.4.1"' in text
    assert 'data.get("version") != "0.4.2"' in text
    assert "rollback_all()" in text
    assert "backup_host_file" in text
    assert "backup_managed_ct" in text
    assert "backup-0.3.0.sh" in text
    assert "validate_rollout_state_0_4_2.py" in text
    assert "unexpected database migration version" in text
    validation = text[text.index("qemu_smoke=") :]
    for forbidden in (
        " pct start ",
        " pct stop ",
        " pct shutdown ",
        " pct reboot ",
        " snapshot-create ",
        " snapshot-delete ",
        " snapshot-rollback ",
    ):
        assert forbidden not in validation


def test_ha_installer_is_transactional_and_keeps_secrets_on_stdin() -> None:
    text = HA_INSTALLER.read_text(encoding="utf-8")
    assert "${STAMP}-before-0.4.2" in text
    assert "rollback_ha()" in text
    assert "validate_ha_secrets_0_4_2.py\" -" in text
    assert "'cat /config/secrets.yaml' |" in text
    assert "ha core check" in text
    assert '--restart-core' in text
    assert "ssh \"${SSH_ARGS[@]}\" 'python3" not in text


def test_041_config_migration_is_idempotent_and_preserves_custom_retention(
    tmp_path: Path,
) -> None:
    source = yaml.safe_load(
        (ROOT / "config/config.example.yaml").read_text(encoding="utf-8")
    )
    vm100 = source["resources"][100]
    for name in ("snapshot_create", "snapshot_list", "snapshot_delete"):
        vm100["operator_capabilities"][name] = False
    vm100.pop("snapshot_retention_count", None)
    source["resources"][106]["snapshot_retention"] = 7
    source["resources"][106].pop("snapshot_retention_count", None)
    input_path = tmp_path / "config-041.yaml"
    first = tmp_path / "config-042.yaml"
    second = tmp_path / "config-042-again.yaml"
    input_path.write_text(
        yaml.safe_dump(source, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    command = [
        sys.executable,
        str(ROOT / "scripts/migrate_config_0_4_2.py"),
        str(input_path),
        str(first),
        "--host-control-url",
        "http://192.0.2.10:8741",
    ]
    subprocess.run(command, check=True, cwd=ROOT)
    subprocess.run(
        [
            *command[:2],
            str(first),
            str(second),
            *command[4:],
        ],
        check=True,
        cwd=ROOT,
    )

    migrated = yaml.safe_load(first.read_text(encoding="utf-8"))
    repeated = yaml.safe_load(second.read_text(encoding="utf-8"))
    assert migrated == repeated
    capabilities = migrated["resources"][100]["operator_capabilities"]
    assert {
        name for name, enabled in capabilities.items() if enabled
    } == {"snapshot_create", "snapshot_list", "snapshot_delete"}
    assert migrated["resources"][100]["snapshot_retention_count"] == 3
    assert migrated["resources"][106]["snapshot_retention_count"] == 7
    assert migrated["mqtt"]["cpu_publish_deadband_percent"] == 0.5
    assert migrated["mqtt"]["telemetry_heartbeat_seconds"] == 300


def test_041_config_migration_preserves_explicit_safety_policy(
    tmp_path: Path,
) -> None:
    source = yaml.safe_load(
        (ROOT / "config/config.example.yaml").read_text(encoding="utf-8")
    )
    vm100_capabilities = source["resources"][100]["operator_capabilities"]
    vm100_capabilities["refresh"] = True
    vm100_capabilities["start"] = False
    vm106 = source["resources"][106]
    disabled = {
        "rollback",
        "start",
        "shutdown",
        "reboot",
        "force_stop",
        "snapshot_create",
        "snapshot_list",
        "snapshot_rollback",
        "snapshot_delete",
    }
    for capability in disabled:
        vm106["operator_capabilities"][capability] = False
    vm106["manual_rollback_allowed"] = False
    vm106["manual_snapshot_restore_allowed"] = False
    vm106["pre_update_snapshot"] = False
    input_path = tmp_path / "config-041-custom-policy.yaml"
    first = tmp_path / "config-042-custom-policy.yaml"
    second = tmp_path / "config-042-custom-policy-again.yaml"
    input_path.write_text(
        yaml.safe_dump(source, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    command = [
        sys.executable,
        str(ROOT / "scripts/migrate_config_0_4_2.py"),
        str(input_path),
        str(first),
        "--host-control-url",
        "http://192.0.2.10:8741",
    ]
    subprocess.run(command, check=True, cwd=ROOT)
    subprocess.run(
        [
            *command[:2],
            str(first),
            str(second),
            *command[4:],
        ],
        check=True,
        cwd=ROOT,
    )

    migrated = yaml.safe_load(first.read_text(encoding="utf-8"))
    repeated = yaml.safe_load(second.read_text(encoding="utf-8"))
    assert migrated == repeated
    migrated_vm106 = migrated["resources"][106]
    assert all(
        migrated_vm106["operator_capabilities"][name] is False
        for name in disabled
    )
    assert migrated_vm106["manual_rollback_allowed"] is False
    assert migrated_vm106["manual_snapshot_restore_allowed"] is False
    assert migrated_vm106["pre_update_snapshot"] is False
    migrated_vm100 = migrated["resources"][100]["operator_capabilities"]
    assert migrated_vm100["refresh"] is True
    assert migrated_vm100["start"] is False
    assert all(
        migrated_vm100[name] is True
        for name in ("snapshot_create", "snapshot_list", "snapshot_delete")
    )
