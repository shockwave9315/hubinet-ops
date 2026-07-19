from __future__ import annotations

import json
from pathlib import Path

import yaml

from app.config import validate_config

ROOT = Path(__file__).parents[1]
CONFIG = ROOT / "config" / "config.example.yaml"
WRAPPER = ROOT / "deploy" / "pve" / "hubinet-ops-host"
UPGRADE = ROOT / "deploy" / "upgrade-0.3.0-from-pve.sh"
HA_INSTALLER = ROOT / "deploy" / "install-ha-0.3.0-from-pve.sh"


def _resources() -> dict[int, dict]:
    raw = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    validate_config(raw)
    return {int(vmid): cfg for vmid, cfg in raw["resources"].items()}


def test_production_inventory_is_exact_and_fail_closed() -> None:
    resources = _resources()
    assert sorted(resources) == list(range(100, 111))
    assert resources[100]["resource_type"] == "qemu"
    assert resources[100]["adapter"] == "haos"
    assert resources[100]["guest_agent"] is True
    assert resources[110]["adapter"] == "agent_self"
    assert resources[106]["manual_rollback_allowed"] is False
    assert resources[106]["operator_capabilities"] == {
        "refresh": True,
        "scan": True,
        "approve": True,
        "reject": True,
        "retry_healthcheck": True,
        "rollback": False,
        "start": True,
        "shutdown": True,
        "reboot": True,
    }
    for vmid in (100, 101, 102, 103, 104, 105, 107, 108, 109, 110):
        assert not any(resources[vmid]["operator_capabilities"].values())


def test_inventory_services_and_docker_do_not_guess_unknown_names() -> None:
    resources = _resources()
    assert resources[105].get("required_services", []) == []
    assert resources[108].get("required_services", []) == []
    assert resources[107]["required_services"] == [
        "postgresql@16-main.service",
        "redis-server.service",
    ]
    assert resources[109]["docker"]["enabled"] is True
    assert resources[109]["docker"]["required"] == []
    assert resources[106]["docker"]["required"] == [
        "weatherhub-weather-api-1",
        "weatherhub-weather-worker-1",
        "weatherhub-redis-1",
    ]


def test_pve_allowlists_and_type_map_are_exact() -> None:
    observation = (ROOT / "deploy/pve/observation-vmids").read_text().splitlines()
    managed = (ROOT / "deploy/pve/managed-vmids").read_text().splitlines()
    maintenance = (ROOT / "deploy/pve/maintenance-vmids").read_text().splitlines()
    lifecycle = (ROOT / "deploy/pve/lifecycle-vmids").read_text().splitlines()
    mappings = (ROOT / "deploy/pve/resource-types").read_text().splitlines()

    assert observation == [str(vmid) for vmid in range(100, 111)]
    assert managed == [str(vmid) for vmid in range(101, 110)]
    assert maintenance == ["106"]
    assert lifecycle == ["106"]
    assert mappings == ["100 qemu"] + [f"{vmid} lxc" for vmid in range(101, 111)]
    assert len({line.split()[0] for line in mappings}) == len(mappings)


def test_wrapper_routes_fixed_qemu_reads_and_blocks_managed_qemu_actions() -> None:
    text = WRAPPER.read_text(encoding="utf-8")
    assert 'qm status "$vmid"' in text
    assert 'qm guest cmd "$vmid" network-get-interfaces' in text
    assert 'pvesh get "/nodes/$(hostname)/qemu/$vmid/status/current"' in text
    assert '[[ "$resource_type" == "lxc" ]] || fail "Managed action is supported only for LXC"' in text
    assert 'listed "$vmid" "$MAINTENANCE_ALLOWLIST"' in text
    assert "VMID must have exactly one resource type" in text
    assert "eval " not in text
    assert "pct enter" not in text
    assert "pct console" not in text


def test_managed_profiles_exist_only_for_apt_lxc_inventory() -> None:
    profiles = ROOT / "deploy" / "managed" / "profiles"
    names = sorted(path.name for path in profiles.glob("ct*.json"))
    assert names == [f"ct{vmid}.json" for vmid in range(101, 110)]
    for path in profiles.glob("*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(data["services"], list)
        assert isinstance(data["docker"], dict)


def test_upgrade_is_transactional_archive_safe_and_read_only_for_resources() -> None:
    text = UPGRADE.read_text(encoding="utf-8")
    assert 'VERSION = "0.3.0"' in text
    assert "systemctl stop hubinet-ops" in text
    assert "ops.db*" in text
    assert "restore_all" in text
    assert "observation-vmids" in text
    assert "managed-vmids" in text
    assert "resource-types" in text
    assert "GET /api/v1/resources" not in text  # curl uses the literal path, never a command field.
    assert "/api/v1/resources" in text
    assert "git ls-files" not in text
    assert "git init" not in text
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


def test_ha_installer_preserves_secrets_checks_yaml_and_never_restarts() -> None:
    text = HA_INSTALLER.read_text(encoding="utf-8")
    for secret in (
        "hubinet_ops_webhook_id",
        "hubinet_ops_notify_service",
        "hubinet_ops_authorization",
    ):
        assert secret in text
    assert "generate_ha_dashboard.py\" --check" in text
    assert "ha core check" in text
    assert "restore_ha" in text
    assert "ha core restart" not in text
    assert "/api/v1/resources/{{ vmid }}/start" in text
