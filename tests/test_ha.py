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


def test_dashboard_sensor_ids_match_discovery_object_ids() -> None:
    dashboard = (
        ROOT / "home-assistant" / "dashboards" / "hubinet_ops.yaml"
    ).read_text(encoding="utf-8")
    keys = {key for key, _, _, _ in _ct_entities()}
    for vmid in (101, 106):
        for key in keys:
            if key in {
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
            }:
                assert f"sensor.hubinet_ops_ct{vmid}_{key}" in dashboard

    assert "sensor.hubinet_ops_ct106_disk_used\n" not in dashboard
    assert "sensor.hubinet_ops_ct106_disk_free\n" not in dashboard
    assert "sensor.hubinet_ops_ct106_memory_used\n" not in dashboard
