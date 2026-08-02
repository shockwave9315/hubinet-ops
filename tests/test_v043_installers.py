from __future__ import annotations

from pathlib import Path
import subprocess
import sys

import yaml


ROOT = Path(__file__).parents[1]
UPGRADE = ROOT / "deploy" / "upgrade-0.4.3-from-pve.sh"
HA_UPGRADE = ROOT / "deploy" / "install-ha-0.4.3-from-pve.sh"


def _upgrade_text() -> str:
    return UPGRADE.read_text(encoding="utf-8")


def test_upgrade_accepts_only_installed_042_and_targets_043() -> None:
    text = _upgrade_text()
    assert "supports only an installed 0.4.2 backend" in text
    assert "supports only an installed 0.4.2 hostd" in text
    assert 'data.get("version") != "0.4.2"' in text
    assert 'VERSION = "0.4.3"' in text
    assert "The 0.4.3 upgrade accepts no arguments" in text


def test_upgrade_backs_up_every_durable_layer_before_first_mutation() -> None:
    text = _upgrade_text()
    backup = text.index('for destination in "${HOST_DESTINATIONS[@]}"; do backup_host_file')
    managed = text.index("backup_managed_ct")
    agent = text.index('deploy/agent/backup-0.3.0.sh')
    first_host_install = text.index('install -m 0600 "$HOSTD_ENV_STAGE"')
    assert backup < first_host_install
    assert managed < first_host_install
    assert agent < first_host_install
    assert 'HOST_DESTINATIONS+=("$HOSTD_DATABASE")' in text
    assert "ops.db" in (ROOT / "deploy/agent/backup-0.3.0.sh").read_text(
        encoding="utf-8"
    )


def test_upgrade_installs_new_typed_host_and_ct110_supervisor_contracts() -> None:
    text = _upgrade_text()
    for artifact in (
        "hubinet_ops_host_control.py",
        "hubinet_ops_hostd.py",
        "hubinet_ops_release.py",
        "hubinet_ops_ct110_system.py",
        "hubinet-ops-self-update",
        "hubinet-ops-ct110-system-update",
        "ct110-system-update-vmids",
        "ct110-system-automatic-rollback-vmids",
    ):
        assert artifact in text
    assert "shell=True" not in text


def test_upgrade_rollback_is_reverse_order_and_restores_services_and_databases() -> None:
    text = _upgrade_text()
    rollback = text[text.index("rollback_all()") : text.index("exit_cleanup()")]
    assert "restore-0.3.0.sh" in rollback
    assert "seq 110 -1 101" in rollback
    assert "restore_host_file" in rollback
    assert "systemctl restart" in rollback
    assert "ROLLBACK INCOMPLETE" not in rollback or "manual intervention" in rollback


def test_upgrade_final_validation_is_read_only_and_covers_recovery_contracts() -> None:
    text = _upgrade_text()
    validation = text[text.index("wrapper=") :]
    for required in (
        "inspect 100",
        "inspect 106",
        "list-snapshots 100",
        "list-snapshots 106",
        "ct110-system-scan 110",
        "PVE newline snapshot fixture",
        "application-release",
        "PRAGMA user_version",
    ):
        assert required in validation
    for forbidden in ("apt upgrade", "snapshot-create", "snapshot-delete", "pct start", "pct stop"):
        assert forbidden not in validation


def test_config_migration_preserves_operator_data_and_enables_split_ct110_flows(
    tmp_path: Path,
) -> None:
    source = yaml.safe_load(
        (ROOT / "config" / "config.example.yaml").read_text(encoding="utf-8")
    )
    source["mqtt"]["host"] = "preserved.example.invalid"
    source["resources"][110]["monitoring"]["update_scan"] = False
    source["resources"][110]["operator_capabilities"]["scan"] = False
    source["resources"][110]["pre_update_snapshot"] = False
    source_path = tmp_path / "source.yaml"
    output_path = tmp_path / "output.yaml"
    source_path.write_text(
        yaml.safe_dump(source, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "migrate_config_0_4_3.py"),
            str(source_path),
            str(output_path),
            "--host-control-url",
            "http://192.0.2.10:8741",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    migrated = yaml.safe_load(output_path.read_text(encoding="utf-8"))
    assert migrated["mqtt"]["host"] == "preserved.example.invalid"
    ct110 = migrated["resources"][110]
    assert ct110["monitoring"]["update_scan"] is True
    assert ct110["operator_capabilities"]["scan"] is True
    assert ct110["operator_capabilities"]["self_update"] is True
    assert ct110["pre_update_snapshot"] is True


def test_ha_installer_validates_secrets_backs_up_checks_and_rolls_back() -> None:
    text = HA_UPGRADE.read_text(encoding="utf-8")
    assert "validate_ha_secrets_0_4_3.py" in text
    assert text.index("validate_ha_secrets_0_4_3.py") < text.index("backup_complete=true")
    assert "BACKUP_DIR=" in text
    assert "ha core check" in text
    assert "rollback_ha" in text
    assert "--restart-core" in text
    assert "ha core restart" in text
    assert "VM100" not in text


def test_installers_do_not_print_or_embed_secrets() -> None:
    combined = _upgrade_text() + HA_UPGRADE.read_text(encoding="utf-8")
    assert "set -x" not in combined
    assert "cat /etc/hubinet-ops/hostd.env" not in combined
    assert "echo $HUBINET_OPS_HOSTD" not in combined
    assert "Authorization: Bearer $HUBINET_OPS_HOSTD_BACKEND_TOKEN" in combined


def test_installer_runtime_smoke_is_ci_sandbox_only() -> None:
    test = (ROOT / "tests" / "test_installer_runtime_smoke.py").read_text(
        encoding="utf-8"
    )
    runner = (ROOT / "tests" / "shell" / "run_runtime_smoke_sandbox.sh").read_text(
        encoding="utf-8"
    )
    assert "HUBINET_OPS_EPHEMERAL_CI" in test
    assert "RUNNER_ENVIRONMENT" in test
    assert "--network none" in runner
    assert "--read-only" in runner
    assert "--cap-drop ALL" in runner
    assert "/var/run/docker.sock" in (
        ROOT / "tests" / "shell" / "runtime_smoke_sandbox_entrypoint.sh"
    ).read_text(encoding="utf-8")
