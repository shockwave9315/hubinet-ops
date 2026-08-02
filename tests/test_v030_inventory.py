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
    assert resources[106]["manual_rollback_allowed"] is True
    assert {
        name
        for name, enabled in resources[100]["operator_capabilities"].items()
        if enabled
    } == {"snapshot_create", "snapshot_list", "snapshot_delete"}
    for vmid in range(101, 110):
        capabilities = resources[vmid]["operator_capabilities"]
        assert all(value for name, value in capabilities.items() if name != "self_update")
        assert capabilities["self_update"] is False
        assert resources[vmid]["manual_rollback_allowed"] is True
    ct110 = resources[110]["operator_capabilities"]
    assert ct110["start"] is True
    assert ct110["snapshot_create"] is True
    assert ct110["self_update"] is True
    assert ct110["approve"] is True
    assert ct110["reject"] is True


def test_inventory_services_and_docker_do_not_guess_unknown_names() -> None:
    resources = _resources()
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
    assert maintenance == [str(vmid) for vmid in range(101, 110)]
    assert lifecycle == [str(vmid) for vmid in range(101, 111)]
    assert mappings == ["100 qemu"] + [f"{vmid} lxc" for vmid in range(101, 111)]
    assert len({line.split()[0] for line in mappings}) == len(mappings)


def test_wrapper_routes_fixed_qemu_reads_and_blocks_managed_qemu_actions() -> None:
    wrapper = WRAPPER.read_text(encoding="utf-8")
    text = (ROOT / "deploy/pve/hubinet_ops_host_control.py").read_text(encoding="utf-8")
    assert '"qm", "status", str(vmid)' in text
    assert '"/cluster/resources", "--type", "vm"' in text
    assert 'f"/nodes/{node}/qemu/{vmid}/status/current"' in text
    assert "$(hostname)" not in wrapper + text
    assert "HostPolicy" in text
    assert "VMID not managed-executor allowed" in text
    assert "shell=False" in text
    assert "eval " not in wrapper + text
    assert "pct enter" not in text
    assert "pct console" not in text


def test_managed_profiles_exist_for_apt_and_supervised_ct110_inventory() -> None:
    profiles = ROOT / "deploy" / "managed" / "profiles"
    names = sorted(path.name for path in profiles.glob("ct*.json"))
    assert names == [f"ct{vmid}.json" for vmid in range(101, 111)]
    for path in profiles.glob("*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(data["services"], list)
        assert isinstance(data["docker"], dict)
    ct103 = json.loads((profiles / "ct103.json").read_text(encoding="utf-8"))
    assert ct103["min_free_mb"] == 512


def test_upgrade_is_transactional_archive_safe_and_read_only_for_resources() -> None:
    text = UPGRADE.read_text(encoding="utf-8")
    agent_backup = (ROOT / "deploy" / "agent" / "backup-0.3.0.sh").read_text(
        encoding="utf-8"
    )
    agent_restore = (ROOT / "deploy" / "agent" / "restore-0.3.0.sh").read_text(
        encoding="utf-8"
    )
    transaction = "\n".join((text, agent_backup, agent_restore))
    assert 'VERSION = "0.3.0"' in text
    assert "service_action stop" in agent_backup
    assert "test -s \"$backup/ops.db\"" in agent_backup
    assert "PRAGMA quick_check" in agent_backup
    assert "backup.complete" in agent_backup
    assert "preserving current ops.db" in agent_restore
    assert "restore_all" in text
    assert "observation-vmids" in text
    assert "managed-vmids" in text
    assert "resource-types" in text
    assert 'monitoring_scheduler["enabled"] = True' in text
    assert 'old.get("monitoring_scheduler") or old.get("scheduler")' in text
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
        assert forbidden not in transaction


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
