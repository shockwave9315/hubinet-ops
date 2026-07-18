from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

from app.mqtt import _ct_entities
from scripts.validate_yaml import HomeAssistantLoader

ROOT = Path(__file__).parents[1]


def test_all_repository_yaml_parses() -> None:
    tracked = subprocess.check_output(
        ["git", "ls-files", "*.yaml", "*.yml"],
        cwd=ROOT,
        text=True,
    ).splitlines()
    for name in tracked:
        path = ROOT / name
        yaml.load(path.read_text(encoding="utf-8"), Loader=HomeAssistantLoader)


def test_notifications_only_contain_navigation_data() -> None:
    package_path = ROOT / "home-assistant" / "packages" / "hubinet_ops.yaml"
    data = yaml.load(
        package_path.read_text(encoding="utf-8"),
        Loader=HomeAssistantLoader,
    )
    automation = data["automation"][0]
    for choice in automation["actions"][0]["choose"]:
        notify_data = choice["sequence"][0]["data"]["data"]
        assert set(notify_data) == {"url", "clickAction"}
    text = package_path.read_text(encoding="utf-8")
    assert "authenticationRequired" not in text
    assert "!secret hubinet_ops_notify_service" in text
    assert "notify.mobile_app_" not in text


def test_dashboard_paths_and_dashboard_only_approval() -> None:
    dashboard = (
        ROOT / "home-assistant" / "dashboards" / "hubinet_ops.yaml"
    ).read_text(encoding="utf-8")
    package = (
        ROOT / "home-assistant" / "packages" / "hubinet_ops.yaml"
    ).read_text(encoding="utf-8")
    assert "path: ct-101" in dashboard
    assert "path: ct-106" in dashboard
    assert "script.hubinet_ops_approve_container" in dashboard
    notification_section = package.split("automation:", 1)[1]
    assert "hubinet_ops_approve" not in notification_section


def test_dashboard_sensor_ids_match_home_assistant_discovery_names() -> None:
    dashboard = (
        ROOT / "home-assistant" / "dashboards" / "hubinet_ops.yaml"
    ).read_text(encoding="utf-8")
    keys = {key for key, _, _, _ in _ct_entities()}
    suffixes = {
        "pending_updates": "pending_update_count",
        "disk_used_percent": "disk_used",
        "disk_free_mb": "disk_free",
        "memory_used_percent": "memory_used",
    }
    displayed = {
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
        "docker_required_healthy",
        "docker_required_total",
        "active_plan_id",
        "active_job_id",
        "last_scan",
        "last_update",
        "last_error",
        "last_operation_result",
        "rollback_allowed",
    }
    assert displayed <= keys

    for vmid in (101, 106):
        for key in displayed:
            suffix = suffixes.get(key, key)
            assert f"sensor.hubinet_ops_ct{vmid}_{suffix}" in dashboard
        for stale in suffixes:
            assert f"sensor.hubinet_ops_ct{vmid}_{stale}" not in dashboard


def test_ha_installer_requires_existing_private_notification_secrets() -> None:
    installer = (ROOT / "deploy" / "install-ha-0.2.1-from-pve.sh").read_text(
        encoding="utf-8"
    )
    assert "hubinet_ops_webhook_id" in installer
    assert "hubinet_ops_notify_service" in installer
    assert "notify.mobile_app_poco_x8" not in installer
