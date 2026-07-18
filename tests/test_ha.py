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


def test_all_repository_yaml_parses() -> None:
    tracked = subprocess.check_output(
        ["git", "ls-files", "*.yaml", "*.yml"],
        cwd=ROOT,
        text=True,
    ).splitlines()
    for name in tracked:
        _load(ROOT / name)


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


def test_live_progress_notification_replaces_one_phone_notification_per_ct() -> None:
    text = PACKAGE.read_text(encoding="utf-8")

    assert 'seconds: "/10"' in text
    assert "live_update: true" in text
    assert "progress_max: 100" in text
    assert "alert_once: true" in text
    assert 'tag: "hubinet_ops_ct{{ repeat.item.vmid }}_job"' in text
    assert text.count('tag: "{{ job_tag }}"') >= 5
    assert "plan_tag" not in text
    assert "progress: -1" not in text
    assert "/hubinet-ops/ct-101" in text
    assert "/hubinet-ops/ct-106" in text


def test_dashboard_is_mushroom_sections_with_dashboard_only_approval() -> None:
    dashboard = _load(DASHBOARD)
    text = DASHBOARD.read_text(encoding="utf-8")

    assert [view["path"] for view in dashboard["views"]] == [
        "overview",
        "ct-101",
        "ct-106",
    ]
    assert all(view["type"] == "sections" for view in dashboard["views"])
    assert text.count("custom:mushroom-") >= 40
    assert "action: perform-action" in text
    assert "call-service" not in text
    assert "script.hubinet_ops_approve_container" in text

    automation_text = PACKAGE.read_text(encoding="utf-8").split("automation:", 1)[1]
    assert "hubinet_ops_approve" not in automation_text
    assert "hubinet_ops_reject" not in automation_text


def test_dashboard_has_bounded_reverse_chronological_live_logs_and_packages() -> None:
    text = DASHBOARD.read_text(encoding="utf-8")

    assert text.count("title: Logi live") == 2
    assert text.count("recent_job_events") >= 2
    assert text.count("events[-25:] | reverse") == 2
    assert text.count("packages[:30]") == 2
    assert "oraz {{ (packages | count) - 30 }} kolejnych pakietów" in text


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
        "last_scan",
        "last_update",
        "last_error",
        "last_operation_result",
        "rollback_allowed",
    }
    per_container = {
        101: set(),
        106: {"docker_required_healthy", "docker_required_total"},
    }
    assert common | per_container[106] <= keys

    for vmid in (101, 106):
        expected = common | per_container[vmid]
        for key in expected:
            suffix = suffixes.get(key, key)
            assert f"sensor.hubinet_ops_ct{vmid}_{suffix}" in dashboard
        for stale in suffixes:
            assert f"sensor.hubinet_ops_ct{vmid}_{stale}" not in dashboard


def test_ha_installer_requires_mushroom_and_private_notification_secrets() -> None:
    installer = (ROOT / "deploy" / "install-ha-0.2.1-from-pve.sh").read_text(
        encoding="utf-8"
    )
    assert "lovelace-mushroom/mushroom.js" in installer
    assert "hubinet_ops_webhook_id" in installer
    assert "hubinet_ops_notify_service" in installer
    assert "notify.mobile_app_poco_x8" not in installer
