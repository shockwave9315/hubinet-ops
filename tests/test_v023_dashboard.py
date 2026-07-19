from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).parents[1]
DASHBOARD = ROOT / "home-assistant" / "dashboards" / "hubinet_ops.yaml"
AGENT_UPGRADE = ROOT / "deploy" / "upgrade-0.2.3-from-pve.sh"
DASHBOARD_INSTALLER = ROOT / "deploy" / "install-ha-dashboard-0.2.3-from-pve.sh"


def test_dashboard_reports_authoritative_package_total_and_preview_truncation() -> None:
    text = DASHBOARD.read_text(encoding="utf-8")

    assert text.count("{% set meta = state_attr(") == 9
    assert text.count("{% set total = meta.get(''packages_total''") == 9
    assert text.count("{% set visible = packages | count %}") == 9
    assert text.count("limit atrybutów 10 KB") == 9
    assert text.count("kolejnych widocznych pakietów") == 9
    assert "**{{ packages | count }} pakietów" not in text
    assert "packages | count - 30" not in text


def test_agent_upgrade_explicitly_restores_after_health_validation_failure() -> None:
    text = AGENT_UPGRADE.read_text(encoding="utf-8")
    tail = text.split('echo "Agent 0.2.3 health validation failed" >&2', 1)[1]

    assert "restore_agent 1" in tail
    assert "exit 1" not in tail


def test_dashboard_only_installer_is_backed_up_and_non_destructive() -> None:
    text = DASHBOARD_INSTALLER.read_text(encoding="utf-8")

    assert "/config/backups/hubinet-ops/" in text
    assert "/config/dashboards/hubinet_ops.yaml.new" in text
    assert "ha core check" in text
    assert "hubinet_ops.yaml" in text
    assert "/config/packages/" not in text
    assert "/config/secrets.yaml" not in text
    assert "ha core restart" not in text
