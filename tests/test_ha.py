from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).parents[1]
PVE = ROOT / "deploy" / "pve"
sys.path.insert(0, str(PVE))

from app.mqtt import _ct_entities
from hubinet_ops_hostd import REQUEST_ID_RE
from scripts.validate_yaml import HomeAssistantLoader

PACKAGE = ROOT / "home-assistant" / "packages" / "hubinet_ops.yaml"
DASHBOARD = ROOT / "home-assistant" / "dashboards" / "hubinet_ops.yaml"
SECRETS_EXAMPLE = ROOT / "home-assistant" / "secrets.example.yaml"
INSTALLER = ROOT / "deploy" / "install-ha-from-pve.sh"
LAST_REFRESH_RECORDER_EXCLUSIONS = {
    "sensor.hubinet_ops_agent_last_refresh",
    "sensor.hubinet_ops_vm100_last_refresh",
    *(f"sensor.hubinet_ops_ct{vmid}_last_refresh" for vmid in range(101, 111)),
}
PROGRESS_RECORDER_EXCLUSIONS = {
    *(f"sensor.hubinet_ops_ct{vmid}_job_progress" for vmid in range(101, 111)),
}


def _load(path: Path) -> Any:
    return yaml.load(path.read_text(encoding="utf-8"), Loader=HomeAssistantLoader)


def _walk(value: Any):
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _automation(automation_id: str) -> dict[str, Any]:
    data = _load(PACKAGE)
    return next(item for item in data["automation"] if item["id"] == automation_id)


def test_all_repository_yaml_parses() -> None:
    tracked = subprocess.check_output(
        ["git", "ls-files", "*.yaml", "*.yml"],
        cwd=ROOT,
        text=True,
    ).splitlines()
    for name in tracked:
        _load(ROOT / name)


def test_recorder_keeps_exact_timestamp_excludes_and_omits_ephemeral_progress() -> None:
    recorder = _load(PACKAGE)["recorder"]

    assert set(recorder) == {"exclude"}
    assert set(recorder["exclude"]) == {"entities"}
    exclusions = set(recorder["exclude"]["entities"])
    assert exclusions & LAST_REFRESH_RECORDER_EXCLUSIONS == LAST_REFRESH_RECORDER_EXCLUSIONS
    assert exclusions == LAST_REFRESH_RECORDER_EXCLUSIONS | PROGRESS_RECORDER_EXCLUSIONS


def test_current_automations_replace_the_legacy_webhook_automation() -> None:
    data = _load(PACKAGE)
    automation_ids = {item["id"] for item in data["automation"]}

    assert automation_ids == {
        "hubinet_ops_webhook_notifications_v022",
        "hubinet_ops_live_progress_v040",
        "hubinet_ops_health_watchdog_v022",
    }
    assert "hubinet_ops_webhook_v021" not in PACKAGE.read_text(encoding="utf-8")


def test_notifications_are_navigation_only_and_use_private_target() -> None:
    data = _load(PACKAGE)
    notification_actions = [
        item
        for item in _walk(data["automation"])
        if isinstance(item, dict)
        and item.get("action") == "hubinet_ops_notify_service"
    ]

    assert notification_actions
    for action in notification_actions:
        mobile_data = action["data"]["data"]
        assert mobile_data["url"]
        assert mobile_data["clickAction"]
        assert "actions" not in mobile_data

    automation_text = PACKAGE.read_text(encoding="utf-8").split("automation:", 1)[1]
    assert "script.hubinet_ops_" not in automation_text
    assert "rest_command." not in automation_text
    assert "authenticationRequired" not in automation_text
    assert "notify.mobile_app_" not in PACKAGE.read_text(encoding="utf-8")
    assert "!secret hubinet_ops_notify_service" in PACKAGE.read_text(encoding="utf-8")


def test_live_progress_runs_only_for_active_jobs_and_reuses_one_tag() -> None:
    progress = _automation("hubinet_ops_live_progress_v040")
    text = PACKAGE.read_text(encoding="utf-8")

    assert progress["mode"] == "parallel"
    assert progress["max"] == 9
    assert {trigger["entity_id"] for trigger in progress["triggers"]} == {
        *(f"sensor.hubinet_ops_ct{vmid}_active_job_id" for vmid in range(101, 110)),
    }
    assert all("attribute" not in trigger for trigger in progress["triggers"])
    assert all("to" not in trigger for trigger in progress["triggers"])
    assert "active_job_id" in progress["conditions"][0]["value_template"]
    assert "operation_status" in progress["conditions"][0]["value_template"]

    repeat = progress["actions"][0]["repeat"]
    assert repeat["while"][0]["condition"] == "template"
    assert "states(entity_prefix ~ 'operation_status') == 'running'" in repeat["while"][0][
        "value_template"
    ]
    assert "states(entity_prefix ~ 'active_job_id')" in repeat["while"][0][
        "value_template"
    ]
    assert all(
        lifecycle_stage not in progress["conditions"][0]["value_template"]
        for lifecycle_stage in ("starting", "shutting_down", "rebooting")
    )
    assert repeat["sequence"][-1]["delay"] == "00:00:10"

    assert 'tag: "hubinet_ops_ct{{ vmid }}_job"' in text
    assert text.count('tag: "{{ job_tag }}"') >= 5
    assert "plan_tag" not in text
    assert "progress: -1" not in text
    assert 'seconds: "/10"' not in text


def test_watchdog_rejects_missing_to_state_before_accessing_it() -> None:
    watchdog = _automation("hubinet_ops_health_watchdog_v022")
    first_condition = watchdog["conditions"][0]["value_template"]

    assert "trigger.to_state is not none" in first_condition


def test_dashboard_is_mushroom_sections_with_dashboard_only_approval() -> None:
    dashboard = _load(DASHBOARD)
    text = DASHBOARD.read_text(encoding="utf-8")

    assert [view["path"] for view in dashboard["views"]] == [
        "overview", "vm-100", "ct-101", "ct-102", "ct-103", "ct-104",
        "ct-105", "ct-106", "ct-107", "ct-108", "ct-109", "ct-110",
    ]
    assert all(view["type"] == "sections" for view in dashboard["views"])
    assert text.count("custom:mushroom-") >= 20
    assert "action: perform-action" in text
    assert "call-service" not in text
    assert "script.hubinet_ops_approve_container" in text

    automation_text = PACKAGE.read_text(encoding="utf-8").split("automation:", 1)[1]
    assert "hubinet_ops_approve" not in automation_text
    assert "hubinet_ops_reject" not in automation_text


def test_dashboard_actions_exist_and_active_plan_decisions_use_only_vmid() -> None:
    package = _load(PACKAGE)
    dashboard = _load(DASHBOARD)
    invoked = {
        item["tap_action"]["perform_action"].removeprefix("script.")
        for item in _walk(dashboard)
        if isinstance(item, dict)
        and item.get("tap_action", {}).get("action") == "perform-action"
    }
    assert invoked <= set(package["script"])

    package_text = PACKAGE.read_text(encoding="utf-8")
    secrets_text = SECRETS_EXAMPLE.read_text(encoding="utf-8")
    assert "/plans/approve-active" in secrets_text
    assert "/plans/reject-active" in secrets_text
    assert "/resources/{{ vmid }}/self-update" in secrets_text
    self_update = package["script"]["hubinet_ops_self_update"]
    self_update_text = str(self_update)
    assert "script.hubinet_ops_check_application_release" in self_update_text
    assert "rest_command.hubinet_ops_host_start" not in self_update_text
    restore = package["script"]["hubinet_ops_snapshot_restore_latest"]
    restore_text = str(restore)
    assert "rest_command.hubinet_ops_snapshot_restore" in restore_text
    assert "rest_command.hubinet_ops_host_offline_snapshot_restore" not in restore_text
    assert "hubinet_ops_snapshot_rollback" not in restore_text
    assert "/snapshots/{{ snapshot_name }}/restore" in secrets_text
    assert "/snapshots/{{ snapshot_name }}/rollback" not in secrets_text
    assert "/containers/{{ vmid }}/rollback" in secrets_text
    assert "state_attr" not in package_text.split("hubinet_ops_approve_container:", 1)[1].split("automation:", 1)[0]
    assert "active_plan_id" not in package_text.split("script:", 1)[1].split("automation:", 1)[0]


def test_ct110_direct_host_commands_are_limited_to_start_and_explicit_recovery() -> None:
    package = _load(PACKAGE)
    commands = package["rest_command"]
    timestamp = "20260723T221530"
    snapshot = "hubinet-ops-110-manual-20260723T220000Z"

    def render_request_id(
        command: str,
        *,
        snapshot_name: str | None = None,
    ) -> str:
        payload = commands[command]["payload"]
        payload = payload.replace(
            "{{ utcnow().strftime('%Y%m%dT%H%M%S') }}",
            timestamp,
        ).replace(
            "{{ utcnow().strftime('%Y%m%dT%H%M%SZ') }}",
            f"{timestamp}Z",
        )
        payload = payload.replace("{{ vmid | int }}", "110")
        if snapshot_name is not None:
            payload = payload.replace("{{ snapshot_name }}", snapshot_name)
        parsed = json.loads(payload)
        return str(parsed["request_id"])

    request_ids = {
        "start": render_request_id("hubinet_ops_host_start"),
        "offline-restore": render_request_id(
            "hubinet_ops_host_offline_snapshot_restore",
            snapshot_name=snapshot,
        ),
        "offline-force-stop": render_request_id(
            "hubinet_ops_host_offline_force_stop"
        ),
    }

    assert request_ids == {
        "start": "ha-20260723T221530-110-start",
        "offline-restore": (
            "ha-20260723T221530-110-offline-restore-"
            "hubinet-ops-110-manual-20260723T220000Z"
        ),
        "offline-force-stop": "ha-20260723T221530-110-recovery-force-stop",
    }
    assert len(set(request_ids.values())) == len(request_ids)
    assert all(REQUEST_ID_RE.fullmatch(value) for value in request_ids.values())
    assert all(len(value) <= 128 for value in request_ids.values())
    assert snapshot in request_ids["offline-restore"]
    assert all(
        "{{ now().strftime" not in commands[name]["payload"]
        for name in (
            "hubinet_ops_host_start",
            "hubinet_ops_host_offline_snapshot_restore",
            "hubinet_ops_host_offline_force_stop",
        )
    )
    direct_host_actions = {
        item["action"]
        for script_name in (
            "hubinet_ops_start_container",
            "hubinet_ops_shutdown_container",
            "hubinet_ops_reboot_container",
            "hubinet_ops_force_stop_container",
            "hubinet_ops_snapshot_create",
            "hubinet_ops_snapshot_restore_latest",
            "hubinet_ops_snapshot_delete_latest",
        )
        for item in _walk(package["script"][script_name])
        if isinstance(item, dict)
        and str(item.get("action") or "").startswith("rest_command.hubinet_ops_host_")
    }
    assert direct_host_actions == {"rest_command.hubinet_ops_host_start"}
    assert (
        package["rest_command"]["hubinet_ops_host_offline_snapshot_restore"]["payload"]
        .find('"confirm":"RESTORE_CT110_OFFLINE"')
        >= 0
    )
    assert (
        package["rest_command"]["hubinet_ops_host_offline_force_stop"]["payload"]
        .find('"confirm":"FORCE_STOP_CT110_RECOVERY"')
        >= 0
    )


def test_dashboard_has_bounded_safe_reverse_chronological_logs_and_packages() -> None:
    text = DASHBOARD.read_text(encoding="utf-8")

    assert text.count("title: Logi na żywo") == 9
    assert text.count("recent_job_events") >= 9
    assert text.count("events[-10:] | reverse") == 9
    assert text.count("replace(''|'', ''¦'')") == 10
    assert text.count("packages[:30]") == 9
    assert text.count("kolejnych widocznych pakietów") == 9


def test_dashboard_reads_rich_content_from_health_status_attributes() -> None:
    text = DASHBOARD.read_text(encoding="utf-8")

    assert "state_attr(''sensor.hubinet_ops_ct101_health_status'', ''updates'')" in text
    assert "state_attr(''sensor.hubinet_ops_ct101_health_status'', ''recent_job_events'')" in text
    assert "state_attr(''sensor.hubinet_ops_ct110_health_status'', ''recent_warnings'')" in text


def test_dashboard_sensor_ids_match_home_assistant_discovery_names() -> None:
    dashboard = DASHBOARD.read_text(encoding="utf-8")
    keys = {key for key, _, _, _ in _ct_entities()}
    suffixes = {
        "pending_updates": "pending_update_count",
        "disk_used_percent": "disk_used",
        "disk_free_mb": "disk_free",
        "memory_used_percent": "memory_used",
    }
    common = {
        "health_status",
        "health_score",
        "lxc_status",
        "update_status",
        "operation_status",
        "job_stage",
        "job_progress",
        "pending_updates",
        "risk",
        "disk_used_percent",
        "disk_free_mb",
        "memory_used_percent",
        "active_plan_id",
        "active_job_id",
        "last_scan",
        "last_update",
        "last_error",
        "last_operation_result",
        "last_refresh",
    }
    per_container = {
        101: set(),
        106: {"docker_required_healthy", "docker_required_total", "rollback_allowed"},
    }
    assert common | per_container[106] <= keys

    for vmid in (101, 106):
        expected = common | per_container[vmid]
        for key in expected:
            suffix = suffixes.get(key, key)
            assert f"sensor.hubinet_ops_ct{vmid}_{suffix}" in dashboard
        for stale in suffixes:
            assert f"sensor.hubinet_ops_ct{vmid}_{stale}" not in dashboard


def test_ha_installer_requires_registered_mushroom_and_private_secrets() -> None:
    installer = INSTALLER.read_text(encoding="utf-8")

    assert not (ROOT / "deploy" / "install-ha-0.2.1-from-pve.sh").exists()
    assert "/config/.storage/lovelace_resources" in installer
    assert "/config/configuration.yaml" in installer
    assert "test -f /config/www/community/lovelace-mushroom" not in installer
    assert "hubinet_ops_webhook_id" in installer
    assert "hubinet_ops_notify_service" in installer
    assert "notify.mobile_app_poco_x8" not in installer
