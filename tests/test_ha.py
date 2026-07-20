from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import yaml

from app.mqtt import _ct_entities
from scripts.validate_yaml import HomeAssistantLoader

ROOT = Path(__file__).parents[1]
PACKAGE = ROOT / "home-assistant" / "packages" / "hubinet_ops.yaml"
DASHBOARD = ROOT / "home-assistant" / "dashboards" / "hubinet_ops.yaml"
INSTALLER = ROOT / "deploy" / "install-ha-from-pve.sh"
LAST_REFRESH_RECORDER_EXCLUSIONS = {
    "sensor.hubinet_ops_agent_last_refresh",
    "sensor.hubinet_ops_vm100_last_refresh",
    *(f"sensor.hubinet_ops_ct{vmid}_last_refresh" for vmid in range(101, 111)),
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


def test_recorder_excludes_exactly_the_last_refresh_entities() -> None:
    recorder = _load(PACKAGE)["recorder"]

    assert set(recorder) == {"exclude"}
    assert set(recorder["exclude"]) == {"entities"}
    assert set(recorder["exclude"]["entities"]) == LAST_REFRESH_RECORDER_EXCLUSIONS


def test_current_automations_replace_the_legacy_webhook_automation() -> None:
    data = _load(PACKAGE)
    automation_ids = {item["id"] for item in data["automation"]}

    assert automation_ids == {
        "hubinet_ops_webhook_notifications_v022",
        "hubinet_ops_live_progress_v022",
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
    progress = _automation("hubinet_ops_live_progress_v022")
    text = PACKAGE.read_text(encoding="utf-8")

    assert progress["mode"] == "parallel"
    assert progress["max"] == 2
    assert {trigger["entity_id"] for trigger in progress["triggers"]} == {
        "sensor.hubinet_ops_ct101_health_status",
        "sensor.hubinet_ops_ct106_health_status",
    }
    assert all(trigger["attribute"] == "active_job_id" for trigger in progress["triggers"])
    assert all("to" not in trigger for trigger in progress["triggers"])
    assert "active_job_id" in progress["conditions"][0]["value_template"]
    assert "job_stage" in progress["conditions"][0]["value_template"]

    repeat = progress["actions"][0]["repeat"]
    assert repeat["while"][0]["condition"] == "template"
    assert "state_attr(state_entity, 'operation_status') == 'running'" in repeat["while"][0][
        "value_template"
    ]
    assert "state_attr(state_entity, 'active_job_id')" in repeat["while"][0][
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


def test_dashboard_has_bounded_safe_reverse_chronological_logs_and_packages() -> None:
    text = DASHBOARD.read_text(encoding="utf-8")

    assert text.count("title: Logi live") == 9
    assert text.count("recent_job_events") >= 9
    assert text.count("events[-10:] | reverse") == 9
    assert text.count("replace(''|'', ''¦'')") == 10
    assert text.count("packages[:30]") == 9
    assert text.count("kolejnych widocznych pakietów") == 9


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
