#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config" / "config.example.yaml"
DEFAULT_OUTPUT = ROOT / "home-assistant" / "dashboards" / "hubinet_ops.yaml"

ICONS = {
    100: "mdi:home-assistant",
    101: "mdi:cloud-lock-outline",
    102: "mdi:database",
    103: "mdi:access-point-network",
    104: "mdi:cloud",
    105: "mdi:shield-check-outline",
    106: "mdi:weather-partly-cloudy",
    107: "mdi:image-multiple-outline",
    108: "mdi:dns-outline",
    109: "mdi:water-pump",
    110: "mdi:shield-crown-outline",
}


def _prefix(vmid: int, cfg: dict[str, Any]) -> str:
    return f"vm{vmid}" if cfg["resource_type"] == "qemu" else f"ct{vmid}"


def _label(vmid: int, cfg: dict[str, Any]) -> str:
    kind = "VM" if cfg["resource_type"] == "qemu" else "CT"
    return f"{kind}{vmid} · {cfg['display_name']}"


def _entity(vmid: int, cfg: dict[str, Any], key: str) -> str:
    compatibility_suffixes = {
        "pending_updates": "pending_update_count",
        "disk_used_percent": "disk_used",
        "disk_free_mb": "disk_free",
        "memory_used_percent": "memory_used",
    }
    return (
        f"sensor.hubinet_ops_{_prefix(vmid, cfg)}_"
        f"{compatibility_suffixes.get(key, key)}"
    )


def _title_card(title: str, subtitle: str) -> dict[str, Any]:
    return {
        "type": "custom:mushroom-title-card",
        "title": title,
        "subtitle": subtitle,
    }


def _overview_card(vmid: int, cfg: dict[str, Any]) -> dict[str, Any]:
    health = _entity(vmid, cfg, "health_status")
    return {
        "type": "custom:mushroom-template-card",
        "entity": health,
        "primary": _label(vmid, cfg),
        "secondary": (
            "Zdrowie: {{ states(entity) }} · status: "
            f"{{{{ states('{_entity(vmid, cfg, 'runtime_status')}') }}}}"
        ),
        "multiline_secondary": True,
        "icon": ICONS.get(vmid, "mdi:server"),
        "color": (
            "{% set health = states(entity) %} "
            "{{ 'green' if health == 'healthy' else 'amber' if health == 'degraded' else 'red' }}"
        ),
        "tap_action": {
            "action": "navigate",
            "navigation_path": str(cfg["dashboard_path"]),
        },
    }


def _entities_card(title: str, entities: list[str]) -> dict[str, Any]:
    return {"type": "entities", "title": title, "entities": entities}


def _control_card(vmid: int, cfg: dict[str, Any], action: str) -> dict[str, Any]:
    names = {
        "start": "Uruchom",
        "shutdown": "Wyłącz łagodnie",
        "reboot": "Uruchom ponownie",
        "refresh": "Odśwież stan",
        "scan": "Sprawdź aktualizacje",
        "approve": "Zatwierdź plan",
        "reject": "Odrzuć plan",
        "retry_healthcheck": "Ponów healthcheck",
        "rollback": "Rollback",
    }
    service_names = {
        "approve": "script.hubinet_ops_approve_container",
        "reject": "script.hubinet_ops_reject_container",
        "retry_healthcheck": "script.hubinet_ops_retry_healthcheck",
        "rollback": "script.hubinet_ops_rollback",
    }
    service = service_names.get(action, f"script.hubinet_ops_{action}_container")
    name = names[action]
    return {
        "type": "custom:mushroom-template-card",
        "primary": name,
        "icon": {
            "start": "mdi:play",
            "shutdown": "mdi:power",
            "reboot": "mdi:restart",
        }.get(action, "mdi:shield-check-outline"),
        "tap_action": {
            "action": "perform-action",
            "perform_action": service,
            "data": {"vmid": vmid},
            "confirmation": {
                "text": f"Potwierdź akcję „{name}” dla CT{vmid}."
            },
        },
    }


def _control_conditions(vmid: int, cfg: dict[str, Any], action: str) -> list[dict[str, Any]]:
    runtime = _entity(vmid, cfg, "runtime_status")
    operation = _entity(vmid, cfg, "operation_status")
    conditions = [{"entity": operation, "state_not": "running"}]
    if action == "start":
        conditions.extend(
            [
                {"entity": runtime, "state": "stopped"},
                {"entity": operation, "state_not": "waiting_approval"},
            ]
        )
    elif action in {"shutdown", "reboot", "scan"}:
        conditions.append({"entity": runtime, "state": "running"})
        if action in {"shutdown", "reboot"}:
            conditions.append({"entity": operation, "state_not": "waiting_approval"})
    elif action in {"approve", "reject"}:
        conditions = [{"entity": operation, "state": "waiting_approval"}]
    return conditions


def _resource_view(vmid: int, cfg: dict[str, Any]) -> dict[str, Any]:
    prefix = _prefix(vmid, cfg)
    common = [
        _entity(vmid, cfg, "health_status"),
        _entity(vmid, cfg, "health_score"),
        _entity(vmid, cfg, "runtime_status"),
        _entity(vmid, cfg, "uptime_seconds"),
        _entity(vmid, cfg, "last_refresh"),
        _entity(vmid, cfg, "last_error"),
    ]
    if cfg["resource_type"] == "lxc":
        common.append(_entity(vmid, cfg, "lxc_status"))
    if cfg["adapter"] != "apt":
        common.extend(
            [
                _entity(vmid, cfg, "cpu_usage"),
                _entity(vmid, cfg, "cpu_load_1m"),
                _entity(vmid, cfg, "cpu_cores"),
                _entity(vmid, cfg, "memory_used_bytes"),
                _entity(vmid, cfg, "memory_total_bytes"),
                _entity(vmid, cfg, "disk_used_bytes"),
                _entity(vmid, cfg, "disk_total_bytes"),
                _entity(vmid, cfg, "network_in_bytes"),
                _entity(vmid, cfg, "network_out_bytes"),
            ]
        )
    cards: list[dict[str, Any]] = [
        _title_card(_label(vmid, cfg), f"Adapter: {cfg['adapter']} · {cfg['criticality']}"),
        _entities_card("Stan zasobu", common),
    ]
    if cfg["adapter"] == "apt":
        common.extend(
            [
                _entity(vmid, cfg, "operation_status"),
                _entity(vmid, cfg, "job_stage"),
                _entity(vmid, cfg, "job_progress"),
                _entity(vmid, cfg, "risk"),
                _entity(vmid, cfg, "disk_used_percent"),
                _entity(vmid, cfg, "disk_free_mb"),
                _entity(vmid, cfg, "memory_used_percent"),
                _entity(vmid, cfg, "active_plan_id"),
                _entity(vmid, cfg, "active_job_id"),
                _entity(vmid, cfg, "last_scan"),
                _entity(vmid, cfg, "last_update"),
                _entity(vmid, cfg, "last_operation_result"),
                _entity(vmid, cfg, "rollback_allowed"),
            ]
        )
        cards[1] = _entities_card("Stan zasobu", common)
        cards.append(
            _entities_card(
                "Weryfikacja końcowa",
                [
                    _entity(vmid, cfg, "update_status"),
                    _entity(vmid, cfg, "pending_updates"),
                    _entity(vmid, cfg, "verification_status"),
                    _entity(vmid, cfg, "apt_check_ok"),
                    _entity(vmid, cfg, "dpkg_audit_ok"),
                    _entity(vmid, cfg, "reboot_required"),
                    _entity(vmid, cfg, "packages_remaining_count"),
                ],
            )
        )
        cards.append(
            _entities_card(
                "Recovery scan",
                [
                    _entity(vmid, cfg, "recovery_scan_status"),
                    _entity(vmid, cfg, "last_recovery_scan"),
                    _entity(vmid, cfg, "last_recovery_scan_result"),
                ],
            )
        )
        health_entity = _entity(vmid, cfg, "health_status")
        cards.append(
            {
                "type": "markdown",
                "title": "Pakiety APT",
                "content": (
                    f"{{% set updates = state_attr('{health_entity}', 'updates') or {{}} %}}\n"
                    "{% set packages = updates.get('packages', []) %}\n"
                    f"{{% set meta = state_attr('{health_entity}', 'attribute_payload') or {{}} %}}\n"
                    "{% set total = meta.get('packages_total', packages | count) %}\n"
                    "{% set visible = packages | count %}\n"
                    "**Łącznie: {{ total }} · widoczne: {{ visible }}**\n\n"
                    "{% for package in packages[:30] %}- `{{ package.get('name', '?') }}`\n{% endfor %}"
                    "{% if visible > 30 %}\n… {{ visible - 30 }} kolejnych widocznych pakietów.\n{% endif %}"
                    "{% if meta.get('truncated') %}\nPodgląd ogranicza limit atrybutów 10 KB.\n{% endif %}"
                ),
            }
        )
        cards.append(
            {
                "type": "markdown",
                "title": "Logi live",
                "content": (
                    f"{{% set events = state_attr('{health_entity}', 'recent_job_events') or [] %}}\n"
                    "{% for event in events[-25:] | reverse %}"
                    "- `{{ event.get('created_at', '') }}` **{{ event.get('stage', '') }}** "
                    "{{ event.get('message', '') | replace('|', '¦') }}\n"
                    "{% endfor %}"
                ),
            }
        )
    docker = cfg.get("docker") or {}
    if docker.get("enabled"):
        cards.append(
            {
                "type": "markdown",
                "title": "Docker",
                "content": (
                    f"**Required healthy:** {{{{ states('{_entity(vmid, cfg, 'docker_required_healthy')}') }}}}/"
                    f"{{{{ states('{_entity(vmid, cfg, 'docker_required_total')}') }}}}\n\n"
                    f"Wykryte kontenery: `{{{{ state_attr('{_entity(vmid, cfg, 'health_status')}', 'docker') }}}}`"
                ),
            }
        )
    if cfg["resource_type"] == "qemu":
        cards.append(
            _entities_card(
                "QEMU Guest Agent",
                [
                    _entity(vmid, cfg, "qemu_status"),
                    _entity(vmid, cfg, "guest_agent_status"),
                    _entity(vmid, cfg, "ip_addresses"),
                ],
            )
        )
    if cfg["adapter"] == "agent_self":
        cards.append(
            _entities_card(
                "Self-health agenta",
                [
                    _entity(vmid, cfg, "service_status"),
                    _entity(vmid, cfg, "api_health"),
                    _entity(vmid, cfg, "agent_version"),
                    "sensor.hubinet_ops_agent_active_job_count",
                    "sensor.hubinet_ops_agent_configured_resource_count",
                ],
            )
        )
        cards.append(
            {
                "type": "markdown",
                "title": "Ostatnie warning/error",
                "content": (
                    f"{{% set warnings = state_attr('{_entity(vmid, cfg, 'health_status')}', 'recent_warnings') or [] %}}\n"
                    "{% for warning in warnings[-20:] | reverse %}- {{ warning | replace('|', '¦') }}\n{% endfor %}"
                ),
            }
        )

    capabilities = cfg["operator_capabilities"]
    enabled_actions = [name for name, enabled in capabilities.items() if enabled]
    if not enabled_actions:
        cards.append(
            {
                "type": "markdown",
                "title": "Tryb obserwacji",
                "content": "Tryb obserwacji — sterowanie zablokowane przez politykę backendu.",
            }
        )
    else:
        for action in enabled_actions:
            cards.append(
                {
                    "type": "conditional",
                    "conditions": _control_conditions(vmid, cfg, action),
                    "card": _control_card(vmid, cfg, action),
                }
            )

    return {
        "title": f"{'VM' if cfg['resource_type'] == 'qemu' else 'CT'}{vmid}",
        "path": str(cfg["dashboard_path"]).rsplit("/", 1)[-1],
        "icon": ICONS.get(vmid, "mdi:server"),
        "type": "sections",
        "max_columns": 4,
        "sections": [{"type": "grid", "cards": cards}],
    }


def build_dashboard(resources: dict[int, dict[str, Any]]) -> dict[str, Any]:
    overview_cards = [
        _title_card("Hubinet Ops", "Pełne inventory Proxmox · backend jest źródłem prawdy"),
        _entities_card(
            "Agent",
            [
                "sensor.hubinet_ops_agent_availability",
                "sensor.hubinet_ops_agent_version",
                "sensor.hubinet_ops_agent_configured_resource_count",
                "sensor.hubinet_ops_agent_configured_lxc_count",
                "sensor.hubinet_ops_agent_configured_qemu_count",
                "sensor.hubinet_ops_agent_active_job_count",
            ],
        ),
        *[_overview_card(vmid, cfg) for vmid, cfg in sorted(resources.items())],
    ]
    overview = {
        "title": "Centrum",
        "path": "overview",
        "icon": "mdi:server-network",
        "type": "sections",
        "max_columns": 4,
        "sections": [{"type": "grid", "cards": overview_cards}],
    }
    return {
        "title": "Hubinet Ops",
        "views": [overview]
        + [_resource_view(vmid, cfg) for vmid, cfg in sorted(resources.items())],
    }


def load_resources(path: Path) -> dict[int, dict[str, Any]]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    resources = raw.get("resources")
    if not isinstance(resources, dict):
        raise RuntimeError("Configuration must contain resources")
    return {int(vmid): dict(cfg) for vmid, cfg in resources.items()}


def render(config_path: Path = DEFAULT_CONFIG) -> str:
    return yaml.safe_dump(
        build_dashboard(load_resources(config_path)),
        allow_unicode=True,
        sort_keys=False,
        width=120,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate Hubinet Ops Home Assistant dashboard")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    generated = render(args.config)
    if args.check:
        if not args.output.exists() or args.output.read_text(encoding="utf-8") != generated:
            raise SystemExit("Dashboard is not up to date; run scripts/generate_ha_dashboard.py")
        return 0
    args.output.write_text(generated, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
