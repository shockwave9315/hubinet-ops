from __future__ import annotations

from pathlib import Path

from scripts.validate_yaml import HomeAssistantLoader
import yaml


ROOT = Path(__file__).parents[1]


def test_all_repository_yaml_parses() -> None:
    for path in [*ROOT.rglob("*.yaml"), *ROOT.rglob("*.yml")]:
        yaml.load(path.read_text(encoding="utf-8"), Loader=HomeAssistantLoader)


def test_notifications_only_contain_navigation_data() -> None:
    package_path = ROOT / "home-assistant" / "packages" / "hubinet_ops.yaml"
    data = yaml.load(package_path.read_text(encoding="utf-8"), Loader=HomeAssistantLoader)
    automation = data["automation"][0]
    for choice in automation["actions"][0]["choose"]:
        notify_data = choice["sequence"][0]["data"]["data"]
        assert set(notify_data) == {"url", "clickAction"}
    text = package_path.read_text(encoding="utf-8")
    assert "authenticationRequired" not in text


def test_dashboard_paths_and_dashboard_only_approval() -> None:
    dashboard = (ROOT / "home-assistant" / "dashboards" / "hubinet_ops.yaml").read_text(encoding="utf-8")
    package = (ROOT / "home-assistant" / "packages" / "hubinet_ops.yaml").read_text(encoding="utf-8")
    assert "path: ct-101" in dashboard
    assert "path: ct-106" in dashboard
    assert "script.hubinet_ops_approve_container" in dashboard
    notification_section = package.split("automation:", 1)[1]
    assert "hubinet_ops_approve" not in notification_section
