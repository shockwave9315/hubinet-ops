from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import runpy
import sys

import pytest
import yaml

from app.mqtt import _ct_entities
from scripts.validate_yaml import HomeAssistantLoader


ROOT = Path(__file__).parents[1]
WRAPPER = ROOT / "deploy" / "pve" / "hubinet-ops-host"
UPGRADE = ROOT / "deploy" / "upgrade-0.2.4-from-pve.sh"
HA_INSTALLER = ROOT / "deploy" / "install-ha-0.2.4-from-pve.sh"
PACKAGE = ROOT / "home-assistant" / "packages" / "hubinet_ops.yaml"
DASHBOARD = ROOT / "home-assistant" / "dashboards" / "hubinet_ops.yaml"


def test_wrapper_has_only_fixed_graceful_lifecycle_verbs() -> None:
    text = WRAPPER.read_text(encoding="utf-8")
    implementation = (ROOT / "deploy/pve/hubinet_ops_host_control.py").read_text(
        encoding="utf-8"
    )
    assert "--forced-command" in text
    assert 'SSH_ORIGINAL_COMMAND:-$*' in text
    assert '["pct", "start", str(vmid)]' in implementation
    assert '["pct", "shutdown", str(vmid), "--timeout", "90"]' in implementation
    assert '["pct", "reboot", str(vmid), "--timeout", "90"]' in implementation
    assert "Action does not accept an argument" in implementation
    assert "HostPolicy" in implementation
    assert "shell=False" in implementation
    for forbidden in ("pct destroy", "pct console", "pct enter", " eval ", "generic-command"):
        assert forbidden not in text + implementation


def test_production_lifecycle_allowlist_contains_all_lxc() -> None:
    lifecycle_allowlist = ROOT / "deploy" / "pve" / "lifecycle-vmids"
    assert lifecycle_allowlist.read_text(encoding="utf-8").splitlines() == [
        str(vmid) for vmid in range(101, 111)
    ]
    installer = (ROOT / "deploy" / "pve" / "install-pve-access.sh").read_text(
        encoding="utf-8"
    )
    assert (
        'install -m 0640 "$SOURCE_DIR/lifecycle-vmids" '
        "/etc/hubinet-ops/lifecycle-vmids"
    ) in installer
    assert "host-control-vmids" in installer
    assert "hubinet_ops_host_control.py" in installer


def test_managed_verify_is_fixed_and_checks_integrity_services_and_docker() -> None:
    text = (ROOT / "deploy" / "managed" / "hubinet-maint").read_text(encoding="utf-8")
    assert 'VERSION = "0.4.3"' in text
    assert 'run(["apt-get", "check"]' in text
    assert 'run(["dpkg", "--audit"]' in text
    assert 'Path("/var/run/reboot-required").exists()' in text
    assert "updates = apt_scan()" in text
    assert "collect_health(config)" in text
    assert 'if action == "verify"' in text
    assert "Exactly one action is required" in text


def test_managed_verify_treats_final_repository_failure_as_warning_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if sys.platform == "win32":
        monkeypatch.setitem(sys.modules, "fcntl", SimpleNamespace())
    namespace = runpy.run_path(str(ROOT / "deploy" / "managed" / "hubinet-maint"))
    managed_globals = namespace["verify"].__globals__
    managed_globals["run"] = lambda *_args, **_kwargs: SimpleNamespace(
        returncode=0,
        stdout="",
        stderr="",
    )
    managed_globals["collect_health"] = lambda _config: (
        {
            "health_status": "healthy",
            "docker": {"required_healthy": 3, "required_total": 3},
        },
        [],
    )

    def failed_scan() -> dict[str, object]:
        raise RuntimeError("Temporary failure resolving repository")

    managed_globals["apt_scan"] = failed_scan
    data, failures = namespace["verify"]({})
    assert failures == []
    assert data["final_apt_scan_ok"] is False
    assert data["update_status"] == "unknown"
    assert data["packages_remaining_count"] is None
    assert "Temporary failure" in data["verification_warning"]


def test_upgrade_is_archive_safe_transactional_and_never_runs_managed_actions() -> None:
    text = UPGRADE.read_text(encoding="utf-8")
    assert 'VERSION = "0.2.4"' in text
    assert "git ls-files" not in text
    assert "git init" not in text
    assert "systemctl stop hubinet-ops" in text
    assert "ops.db*" in text
    assert "restore_all" in text
    assert "allowed-vmids" in text
    assert "lifecycle-vmids" in text
    assert "lifecycle-vmids.absent" in text
    assert '== "106"' in text
    assert "refusing to broaden" in text
    assert "operator_capabilities" in text
    assert "{name: False for name in all_caps}" in text
    assert "runuser -u hubinetops" in text
    assert "python3 -m py_compile /usr/local/sbin/hubinet-maint" in text
    assert '"/health"' not in text or '"version":"0.2.4"' in text
    for forbidden in (
        "hubinet-maint check-updates",
        "hubinet-maint update",
        "hubinet-maint repair",
        "hubinet-maint verify",
        "hubinet-ops-host start",
        "hubinet-ops-host shutdown",
        "hubinet-ops-host reboot",
        "hubinet-ops-host rollback",
    ):
        assert forbidden not in text


def test_ha_installer_preserves_private_secrets_and_does_not_restart() -> None:
    text = HA_INSTALLER.read_text(encoding="utf-8")
    assert "hubinet_ops_webhook_id" in text
    assert "hubinet_ops_notify_service" in text
    assert "hubinet_ops_authorization" in text
    assert "if ! grep" in text
    assert "hubinet_ops_start_url" in text
    assert "hubinet_ops_shutdown_url" in text
    assert "hubinet_ops_reboot_url" in text
    assert "ha core check" in text
    assert "restore_ha" in text
    assert "ha core restart" not in text
    assert "git " not in text


def test_dashboard_policy_controls_verification_recovery_and_navigation_only_push() -> None:
    dashboard = DASHBOARD.read_text(encoding="utf-8")
    package = PACKAGE.read_text(encoding="utf-8")
    loaded_dashboard = yaml.safe_load(dashboard)
    views = {
        view["path"]: yaml.safe_dump(view, allow_unicode=True)
        for view in loaded_dashboard["views"]
    }
    ct101 = views["ct-101"]
    ct106 = views["ct-106"]

    for service in (
        "script.hubinet_ops_start_container",
        "script.hubinet_ops_force_stop_container",
        "script.hubinet_ops_snapshot_create",
    ):
        assert service in ct101
    for label in ("Uruchom", "Wyłącz łagodnie", "Uruchom ponownie"):
        assert f"primary: {label}" in ct106
    for forbidden in ("destroy", "console"):
        assert forbidden not in dashboard.lower()
    assert "perform_action: terminal" not in dashboard.lower()
    assert "navigation_path: terminal" not in dashboard.lower()
    assert "title: Weryfikacja końcowa" in ct106
    assert "title: Skan odzyskiwania" in ct106
    assert "confirmation:" in ct106
    assert "recovery_notification_suppressed_until" in package
    assert "state_attr(trigger.entity_id, 'recovery_notification_suppressed_until')" in package
    progress = package.split("id: hubinet_ops_live_progress_v040", 1)[1].split(
        "id: hubinet_ops_health_watchdog_v022", 1
    )[0]
    assert "active_job_id" in progress
    assert "sensor.hubinet_ops_ct101_active_job_id" in progress
    assert "states(entity_prefix ~ 'operation_status')" in progress
    assert "states(entity_prefix ~ 'job_stage')" in progress
    assert "states(entity_prefix ~ 'job_progress')" in progress
    watchdog = package.split("id: hubinet_ops_health_watchdog_v022", 1)[1]
    assert "intentional_shutdown" in watchdog
    assert "lifecycle_status') != 'running'" in watchdog
    assert "LXC działa, weryfikacja usług oczekuje na telemetrię" in package
    assert "nie udało się sprawdzić" in package
    assert "verification_warning" in package
    assert "packages_updated_count" in package
    assert "authenticationRequired" not in package

    loaded = yaml.load(PACKAGE.read_text(encoding="utf-8"), Loader=HomeAssistantLoader)
    assert {
        "hubinet_ops_start_container",
        "hubinet_ops_shutdown_container",
        "hubinet_ops_reboot_container",
    } <= set(loaded["rest_command"])


def test_new_discovery_entity_ids_are_stable_and_existing_keys_remain() -> None:
    entities = {key: value_template for key, _, value_template, _ in _ct_entities()}
    keys = set(entities)
    assert {
        "health_status",
        "operation_status",
        "pending_updates",
        "lifecycle_status",
        "verification_status",
        "recovery_scan_status",
        "capability_start",
        "capability_shutdown",
        "capability_reboot",
    } <= keys
    assert "value_json.reboot_required is none" in entities["reboot_required"]
    assert entities["pending_updates"] == (
        "{{ value_json.pending_updates | default(none) }}"
    )
    assert "default(none)" in entities["packages_remaining_count"]
    assert "'unknown'" not in entities["packages_remaining_count"]
