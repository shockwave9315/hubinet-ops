from __future__ import annotations

from datetime import UTC, datetime
import subprocess
import sys
from pathlib import Path

import yaml

from scripts.validate_rollout_state_0_4_2 import validate as validate_rollout


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


def test_upgrade_reuses_canonical_host_control_health_url() -> None:
    text = UPGRADE.read_text(encoding="utf-8")
    smoke = (ROOT / "tests/shell/runtime_smoke_0_4_2.sh").read_text(
        encoding="utf-8"
    )

    assert text.count('"${HOST_CONTROL_URL%/}/health"') == 2
    assert "HUBINET_OPS_HOSTD_HEALTH_URL" not in text
    assert 'host=f"[{bind}]" if ":" in bind else bind' in text
    assert "HUBINET_OPS_HOST_CONTROL_URL is required for wildcard bind" in text
    for marker in (
        "http://192.0.2.10:8741/health",
        "http://[2001:db8::10]:8741/health",
        "http://[2001:db8::20]:8741/",
        "http://:::8741",
        "wildcard_missing",
    ):
        assert marker in smoke


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


def test_041_config_migration_preserves_omitted_safety_defaults(
    tmp_path: Path,
) -> None:
    source = yaml.safe_load(
        (ROOT / "config/config.example.yaml").read_text(encoding="utf-8")
    )
    omitted = source["resources"][105]
    for key in (
        "operator_capabilities",
        "manual_rollback_allowed",
        "manual_snapshot_restore_allowed",
        "pre_update_snapshot",
    ):
        omitted.pop(key, None)
    partial = source["resources"][106]
    partial["operator_capabilities"] = {"refresh": True}
    vm100 = source["resources"][100]
    vm100["operator_capabilities"] = {"refresh": True}
    vm100.pop("manual_rollback_allowed", None)
    vm100.pop("manual_snapshot_restore_allowed", None)
    vm100.pop("pre_update_snapshot", None)

    input_path = tmp_path / "config-041-omitted-policy.yaml"
    first = tmp_path / "config-042-omitted-policy.yaml"
    second = tmp_path / "config-042-omitted-policy-again.yaml"
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
        [*command[:2], str(first), str(second), *command[4:]],
        check=True,
        cwd=ROOT,
    )

    migrated = yaml.safe_load(first.read_text(encoding="utf-8"))
    repeated = yaml.safe_load(second.read_text(encoding="utf-8"))
    assert migrated == repeated
    migrated_omitted = migrated["resources"][105]
    assert not any(migrated_omitted["operator_capabilities"].values())
    assert migrated_omitted["manual_rollback_allowed"] is False
    assert migrated_omitted["manual_snapshot_restore_allowed"] is False
    assert migrated_omitted["pre_update_snapshot"] is False
    migrated_partial = migrated["resources"][106]["operator_capabilities"]
    assert migrated_partial["refresh"] is True
    assert not any(
        enabled for name, enabled in migrated_partial.items() if name != "refresh"
    )
    migrated_vm100 = migrated["resources"][100]
    assert {
        name
        for name, enabled in migrated_vm100["operator_capabilities"].items()
        if enabled
    } == {"refresh", "snapshot_create", "snapshot_list", "snapshot_delete"}
    assert migrated_vm100["manual_rollback_allowed"] is False
    assert migrated_vm100["manual_snapshot_restore_allowed"] is False
    assert migrated_vm100["pre_update_snapshot"] is False


def _fresh_rollout_payload() -> dict:
    resources = {}
    for vmid in range(100, 111):
        state = {
            "last_refresh": "2026-07-31T12:30:00+00:00",
            "health_status": "healthy",
            "health_score": 100,
        }
        if vmid == 100:
            state.update(
                qemu_status="running",
                cpu={"usage_percent": 3.5},
            )
        if 101 <= vmid <= 109:
            state.update(
                executor_compatible=True,
                executor_version="0.4.1",
                executor_protocol_version=1,
            )
        resources[str(vmid)] = state
    return {"version": "0.4.2", "resources": resources}


def _rollout_errors(vm100: dict) -> list[str]:
    payload = _fresh_rollout_payload()
    payload["resources"]["100"] = {
        **payload["resources"]["100"],
        **vm100,
    }
    return validate_rollout(
        payload,
        datetime(2026, 7, 31, 12, 0, tzinfo=UTC),
    )


def test_rollout_validator_accepts_running_vm100_with_valid_cpu() -> None:
    assert _rollout_errors({}) == []


def test_rollout_validator_accepts_stopped_vm100_without_cpu() -> None:
    payload = _fresh_rollout_payload()
    vm100 = payload["resources"]["100"]
    vm100.update(qemu_status="stopped", health_status="offline")
    del vm100["cpu"]
    assert validate_rollout(
        payload,
        datetime(2026, 7, 31, 12, 0, tzinfo=UTC),
    ) == []


def test_rollout_validator_accepts_stopped_vm100_with_null_cpu() -> None:
    assert _rollout_errors(
        {
            "qemu_status": "stopped",
            "health_status": "offline",
            "cpu": {"usage_percent": None},
        }
    ) == []


def test_rollout_validator_rejects_running_vm100_without_valid_cpu() -> None:
    assert any("bad_vm100" in error for error in _rollout_errors({"cpu": {}}))


def test_rollout_validator_rejects_running_vm100_without_healthy_status() -> None:
    assert any(
        "bad_vm100" in error
        for error in _rollout_errors({"health_status": "offline"})
    )


def test_rollout_validator_rejects_stopped_vm100_without_offline_status() -> None:
    assert any(
        "bad_vm100" in error
        for error in _rollout_errors({"qemu_status": "stopped"})
    )


def test_rollout_validator_rejects_stopped_vm100_with_invalid_cpu() -> None:
    for usage in (True, float("inf"), -0.1, 100.1, "3.5"):
        assert any(
            "bad_vm100" in error
            for error in _rollout_errors(
                {
                    "qemu_status": "stopped",
                    "health_status": "offline",
                    "cpu": {"usage_percent": usage},
                }
            )
        )


def test_rollout_validator_rejects_missing_qemu_status() -> None:
    payload = _fresh_rollout_payload()
    del payload["resources"]["100"]["qemu_status"]
    errors = validate_rollout(
        payload,
        datetime(2026, 7, 31, 12, 0, tzinfo=UTC),
    )
    assert any("bad_vm100" in error for error in errors)


def test_rollout_validator_rejects_unknown_qemu_status() -> None:
    assert any(
        "bad_vm100" in error
        for error in _rollout_errors({"qemu_status": "paused"})
    )
