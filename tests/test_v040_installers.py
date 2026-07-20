from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]
UPGRADE = ROOT / "deploy" / "upgrade-0.4.0-from-pve.sh"
HA_INSTALLER = ROOT / "deploy" / "install-ha-0.4.0-from-pve.sh"


def test_upgrade_is_transactional_across_host_managed_lxc_and_agent() -> None:
    text = UPGRADE.read_text(encoding="utf-8")
    assert "rollback_all" in text
    assert "restore_managed_ct" in text
    assert "restore_host_file" in text
    assert "restore-0.3.0.sh" in text
    assert "hubinet_ops_hostd.py" in text
    assert "hubinet_ops_host_control.py" in text
    assert "hubinet-ops-hostd.service" in text
    assert "hubinet-ops-self-update" in text
    assert "for vmid in $(seq 101 109)" in text
    assert "pct mount" in text and "pct unmount" in text
    assert "hubinet-maint capabilities" in text
    assert "validate_capabilities" in text
    assert "profile_validation_status" in text


def test_upgrade_validation_is_read_only_and_uses_singular_state_endpoint() -> None:
    text = UPGRADE.read_text(encoding="utf-8")
    assert "/api/v1/state" in text
    assert "/api/v1/states" not in text
    assert "list-snapshots 106" in text
    assert "/api/v1/resources/106/snapshots" in text
    assert "VALIDATION_NOT_BEFORE" in text
    assert "executor_compatible" in text
    assert "set(resources) != expected" in text
    forbidden = (
        "pct start", "pct stop", "pct shutdown", "pct reboot",
        "pct snapshot", "pct rollback", "pct delsnapshot",
    )
    for command in forbidden:
        assert command not in text


def test_ha_installer_backs_up_all_user_files_checks_and_never_restarts() -> None:
    text = HA_INSTALLER.read_text(encoding="utf-8")
    for name in (
        "secrets.yaml", "configuration.yaml", "hubinet_ops.package.yaml",
        "hubinet_ops.dashboard.yaml",
    ):
        assert name in text
    assert "ha core check" in text
    assert "rollback_ha" in text
    assert "ha core restart" not in text
    assert "entity_registry" not in text
    assert "/config/.storage/lovelace_resources" in text


def test_config_migration_preserves_inventory_and_sets_040_policy(tmp_path: Path) -> None:
    source = yaml.safe_load((ROOT / "config" / "config.example.yaml").read_text(encoding="utf-8"))
    source["resources"][101]["ip_address"] = "198.51.100.77"
    source["mqtt"]["host"] = "private-broker.example"
    input_path = tmp_path / "config.yaml"
    output_path = tmp_path / "migrated.yaml"
    input_path.write_text(yaml.safe_dump(source, sort_keys=False), encoding="utf-8")

    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "migrate_config_0_4_0.py"),
            str(input_path),
            str(output_path),
            "--host-control-url",
            "http://192.0.2.10:8741",
        ],
        cwd=ROOT,
        check=True,
    )
    migrated = yaml.safe_load(output_path.read_text(encoding="utf-8"))
    assert set(map(int, migrated["resources"])) == set(range(100, 111))
    assert migrated["resources"][101]["ip_address"] == "198.51.100.77"
    assert migrated["mqtt"]["host"] == "private-broker.example"
    assert not any(migrated["resources"][100]["operator_capabilities"].values())
    for vmid in range(101, 110):
        caps = migrated["resources"][vmid]["operator_capabilities"]
        assert all(value for key, value in caps.items() if key != "self_update")
        assert caps["self_update"] is False
        assert len(migrated["resources"][vmid]["executor_contract"]["executor_sha256"]) == 64
    assert migrated["resources"][110]["operator_capabilities"]["self_update"] is True
    assert migrated["resources"][110]["operator_capabilities"]["approve"] is False
    assert migrated["host_control"]["token_env"] == "HUBINET_OPS_HOSTD_TOKEN"


def test_all_managed_profiles_pass_the_executor_schema() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "validate_managed_profiles.py")],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    for vmid in range(101, 110):
        assert f"ct{vmid}.json:" in result.stdout
