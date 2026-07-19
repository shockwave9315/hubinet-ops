#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.ha_entities import (
    agent_entity_id,
    normalize_resource_identity,
    resource_entity_id,
)

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


def _label(vmid: int, cfg: dict[str, Any]) -> str:
    kind = "VM" if normalize_resource_identity(cfg).resource_type == "qemu" else "CT"
    return f"{kind}{vmid} · {cfg['display_name']}"


def _entity(vmid: int, cfg: dict[str, Any], key: str) -> str:
    return resource_entity_id(vmid, cfg, key)


def _title(title: str, subtitle: str = "") -> dict[str, Any]:
    return {"type": "custom:mushroom-title-card", "title": title, "subtitle": subtitle}


def _section(*cards: dict[str, Any]) -> dict[str, Any]:
    return {"type": "grid", "cards": list(cards)}


def _entity_card(
    entity: str,
    name: str,
    icon: str,
    color: str = "blue",
) -> dict[str, Any]:
    return {
        "type": "custom:mushroom-entity-card",
        "entity": entity,
        "name": name,
        "icon": icon,
        "icon_color": color,
    }


def _entity_grid(cards: Iterable[dict[str, Any]], columns: int = 2) -> dict[str, Any]:
    return {
        "type": "grid",
        "columns": columns,
        "square": False,
        "cards": list(cards),
    }


def _chip(
    entity: str,
    icon: str,
    *,
    icon_color: str,
    content: str | None = None,
) -> dict[str, Any]:
    return {
        "type": "template",
        "entity": entity,
        "icon": icon,
        "content": content or "{{ states(entity) }}",
        "icon_color": icon_color,
        "tap_action": {"action": "more-info"},
    }


def _chips(chips: Iterable[dict[str, Any]]) -> dict[str, Any]:
    return {
        "type": "custom:mushroom-chips-card",
        "alignment": "justify",
        "chips": list(chips),
    }


def _resource_status(vmid: int, cfg: dict[str, Any]) -> dict[str, Any]:
    identity = normalize_resource_identity(cfg)
    health = _entity(vmid, cfg, "health_status")
    runtime = _entity(vmid, cfg, "runtime_status")
    secondary_parts = [f"Runtime: {{{{ states('{runtime}') }}}}"]
    if identity.adapter == "apt":
        secondary_parts.extend(
            [
                f"aktualizacje: {{{{ states('{_entity(vmid, cfg, 'update_status')}') }}}}",
                f"pakiety: {{{{ states('{_entity(vmid, cfg, 'pending_updates')}') }}}}",
            ]
        )
    elif identity.resource_type == "qemu":
        secondary_parts.append(
            f"Guest Agent: {{{{ states('{_entity(vmid, cfg, 'guest_agent_status')}') }}}}"
        )
    else:
        secondary_parts.extend(
            [
                f"usługa: {{{{ states('{_entity(vmid, cfg, 'service_status')}') }}}}",
                f"API: {{{{ states('{_entity(vmid, cfg, 'api_health')}') }}}}",
            ]
        )
    return {
        "type": "custom:mushroom-template-card",
        "entity": health,
        "primary": _label(vmid, cfg),
        "secondary": " · ".join(secondary_parts),
        "multiline_secondary": True,
        "icon": ICONS.get(vmid, "mdi:server"),
        "color": (
            "{% set health = states(entity) %} "
            "{{ 'green' if health == 'healthy' else 'amber' if health == 'degraded' else 'red' }}"
        ),
        "badge_icon": (
            "{{ 'mdi:check-circle' if is_state(entity, 'healthy') else 'mdi:alert-circle' }}"
        ),
        "badge_color": (
            "{{ 'green' if is_state(entity, 'healthy') else "
            "'amber' if is_state(entity, 'degraded') else 'red' }}"
        ),
        "tap_action": {"action": "more-info"},
    }


def _overview_card(vmid: int, cfg: dict[str, Any]) -> dict[str, Any]:
    card = _resource_status(vmid, cfg)
    card["tap_action"] = {
        "action": "navigate",
        "navigation_path": str(cfg["dashboard_path"]),
    }
    return card


def _resource_chips(vmid: int, cfg: dict[str, Any]) -> dict[str, Any]:
    identity = normalize_resource_identity(cfg)
    items = [
        _chip(
            _entity(vmid, cfg, "health_status"),
            "mdi:heart-pulse",
            icon_color=(
                "{% set value = states(entity) %} "
                "{{ 'green' if value == 'healthy' else 'amber' if value == 'degraded' "
                "else 'red' if value in ['critical', 'offline'] else 'grey' }}"
            ),
        ),
        _chip(
            _entity(vmid, cfg, "health_score"),
            "mdi:heart-pulse",
            content="{{ states(entity) }}%",
            icon_color=(
                "{% set value = states(entity) | int(-1) %} "
                "{{ 'green' if value >= 90 else 'amber' if value >= 70 else 'red' }}"
            ),
        ),
        _chip(
            _entity(vmid, cfg, "runtime_status"),
            "mdi:server",
            icon_color=(
                "{% set value = states(entity) %} "
                "{{ 'green' if value == 'running' else 'grey' if value == 'stopped' else 'amber' }}"
            ),
        ),
    ]
    if identity.adapter == "apt":
        items.extend(
            [
                _chip(
                    _entity(vmid, cfg, "update_status"),
                    "mdi:package-up",
                    icon_color=(
                        "{% set value = states(entity) %} "
                        "{{ 'green' if value == 'up_to_date' else 'blue' if value == 'scanning' "
                        "else 'amber' if value == 'update_available' else 'grey' }}"
                    ),
                ),
                _chip(
                    _entity(vmid, cfg, "operation_status"),
                    "mdi:progress-wrench",
                    icon_color=(
                        "{% set value = states(entity) %} "
                        "{{ 'green' if value in ['idle', 'success'] else 'blue' if value == 'running' "
                        "else 'amber' if value in ['waiting_approval', 'rolled_back'] else 'red' }}"
                    ),
                ),
                _chip(
                    _entity(vmid, cfg, "risk"),
                    "mdi:shield-alert-outline",
                    icon_color=(
                        "{% set value = states(entity) %} "
                        "{{ 'green' if value in ['none', 'low'] else 'amber' if value == 'medium' else 'red' }}"
                    ),
                ),
                _chip(
                    _entity(vmid, cfg, "pending_updates"),
                    "mdi:format-list-numbered",
                    content="{{ states(entity) }} pak.",
                    icon_color=(
                        "{% set value = states(entity) | int(-1) %} "
                        "{{ 'green' if value == 0 else 'amber' if value > 0 else 'grey' }}"
                    ),
                ),
            ]
        )
    elif identity.resource_type == "qemu":
        items.append(
            _chip(
                _entity(vmid, cfg, "guest_agent_status"),
                "mdi:lan-connect",
                icon_color=(
                    "{% set value = states(entity) %} "
                    "{{ 'green' if value == 'available' else 'red' if value == 'unavailable' else 'amber' }}"
                ),
            )
        )
    else:
        items.extend(
            [
                _chip(
                    _entity(vmid, cfg, "service_status"),
                    "mdi:application-cog",
                    icon_color="{{ 'green' if is_state(entity, 'active') else 'red' }}",
                ),
                _chip(
                    _entity(vmid, cfg, "api_health"),
                    "mdi:api",
                    icon_color="{{ 'green' if states(entity) in ['healthy', 'ok'] else 'red' }}",
                ),
                _chip(
                    _entity(vmid, cfg, "agent_version"),
                    "mdi:tag-outline",
                    icon_color="blue",
                ),
            ]
        )
    return _chips(items)


def _observation_section(vmid: int, cfg: dict[str, Any]) -> dict[str, Any]:
    return _section(
        _title("Sterowanie", "Polityka backendu jest źródłem prawdy"),
        {
            "type": "custom:mushroom-template-card",
            "entity": _entity(vmid, cfg, "health_status"),
            "primary": "Tryb obserwacji — sterowanie zablokowane przez politykę backendu",
            "secondary": "Telemetria i diagnostyka pozostają dostępne.",
            "multiline_secondary": True,
            "icon": "mdi:eye-lock-outline",
            "color": "blue-grey",
            "tap_action": {"action": "more-info"},
        },
    )


def _control_card(vmid: int, action: str) -> dict[str, Any]:
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
    secondary = {
        "start": f"Start CT{vmid}",
        "shutdown": f"Graceful shutdown CT{vmid}",
        "reboot": f"Graceful reboot CT{vmid}",
        "refresh": "Health, zasoby i usługi",
        "scan": "Pobierz aktualną listę pakietów",
        "approve": "Snapshot, update i stabilizacja usług",
        "reject": "Nie wykonuj aktualizacji",
        "retry_healthcheck": "Sprawdź stabilizację usług jeszcze raz",
        "rollback": "Ręczny rollback ostatniej nieudanej operacji",
    }
    colors = {
        "start": "green",
        "shutdown": "red",
        "reboot": "amber",
        "refresh": "cyan",
        "scan": "blue",
        "approve": "green",
        "reject": "red",
        "retry_healthcheck": "amber",
        "rollback": "red",
    }
    confirmations = {
        "start": {
            "title": f"Uruchom CT{vmid}",
            "text": f"Uruchomić kontener CT{vmid}?",
            "confirm_text": f"Uruchom CT{vmid}",
            "dismiss_text": "Anuluj",
        },
        "shutdown": {
            "title": f"Wyłącz łagodnie CT{vmid}",
            "text": f"Łagodnie wyłączyć kontener CT{vmid}? Usługi przestaną być dostępne.",
            "confirm_text": f"Wyłącz CT{vmid}",
            "dismiss_text": "Anuluj",
        },
        "reboot": {
            "title": f"Uruchom ponownie CT{vmid}",
            "text": f"Łagodnie zrestartować kontener CT{vmid}? Usługi będą chwilowo niedostępne.",
            "confirm_text": f"Restartuj CT{vmid}",
            "dismiss_text": "Anuluj",
        },
        "refresh": {
            "title": f"Odśwież stan CT{vmid}",
            "text": f"Pobrać aktualny stan CT{vmid}?",
        },
        "scan": {
            "title": f"Skan CT{vmid}",
            "text": f"Sprawdzić dostępne aktualizacje dla CT{vmid}?",
        },
        "approve": {
            "title": f"Aktualizacja CT{vmid}",
            "text": f"Zatwierdzić aktualny plan aktualizacji CT{vmid}?",
            "confirm_text": "Aktualizuj",
            "dismiss_text": "Anuluj",
        },
        "reject": {
            "title": f"Odrzucenie planu CT{vmid}",
            "text": f"Odrzucić aktualny plan CT{vmid}?",
            "confirm_text": "Odrzuć plan",
            "dismiss_text": "Anuluj",
        },
        "retry_healthcheck": {
            "title": f"Ponów healthcheck CT{vmid}",
            "text": f"Ponowić healthcheck CT{vmid}?",
        },
        "rollback": {
            "title": f"Rollback CT{vmid}",
            "text": f"Przywrócić zapisany snapshot CT{vmid}?",
            "confirm_text": "Przywróć",
            "dismiss_text": "Anuluj",
        },
    }
    name = names[action]
    return {
        "type": "custom:mushroom-template-card",
        "primary": name,
        "secondary": secondary[action],
        "icon": {
            "start": "mdi:play",
            "shutdown": "mdi:power",
            "reboot": "mdi:restart",
            "refresh": "mdi:refresh",
            "scan": "mdi:magnify-scan",
            "approve": "mdi:check-decagram",
            "reject": "mdi:close-octagon-outline",
            "retry_healthcheck": "mdi:heart-pulse",
        }.get(action, "mdi:backup-restore"),
        "color": colors[action],
        "tap_action": {
            "action": "perform-action",
            "perform_action": service_names.get(
                action, f"script.hubinet_ops_{action}_container"
            ),
            "data": {"vmid": vmid},
            "confirmation": confirmations[action],
        },
    }


def _state_condition(
    entity: str,
    *,
    state: str | None = None,
    state_not: str | None = None,
) -> dict[str, str]:
    if (state is None) == (state_not is None):
        raise ValueError("A state condition requires exactly one of state or state_not")
    condition = {"condition": "state", "entity": entity}
    condition["state" if state is not None else "state_not"] = (
        state if state is not None else state_not
    )
    return condition


def _control_conditions(
    vmid: int, cfg: dict[str, Any], action: str
) -> list[dict[str, Any]]:
    runtime = _entity(vmid, cfg, "lxc_status")
    operation = _entity(vmid, cfg, "operation_status")
    active_job = _entity(vmid, cfg, "active_job_id")
    lifecycle = _entity(vmid, cfg, "lifecycle_status")
    capability = _entity(vmid, cfg, f"capability_{action}")

    if action == "refresh":
        return [_state_condition(capability, state="allowed")]
    if action in {"approve", "reject"}:
        return [
            _state_condition(operation, state="waiting_approval"),
            _state_condition(capability, state="allowed"),
        ]
    if action == "retry_healthcheck":
        return [
            _state_condition(_entity(vmid, cfg, "health_status"), state="critical"),
            _state_condition(capability, state="allowed"),
            _state_condition(active_job, state="none"),
            _state_condition(lifecycle, state_not="running"),
        ]
    if action == "rollback":
        return [
            _state_condition(operation, state="manual_intervention"),
            _state_condition(_entity(vmid, cfg, "rollback_allowed"), state="allowed"),
            _state_condition(capability, state="allowed"),
            _state_condition(active_job, state="none"),
            _state_condition(lifecycle, state_not="running"),
        ]
    if action == "scan":
        return [
            _state_condition(runtime, state="running"),
            _state_condition(capability, state="allowed"),
            _state_condition(active_job, state="none"),
            _state_condition(lifecycle, state_not="running"),
            _state_condition(operation, state_not="running"),
        ]
    if action in {"start", "shutdown", "reboot"}:
        return [
            _state_condition(runtime, state="stopped" if action == "start" else "running"),
            _state_condition(capability, state="allowed"),
            _state_condition(active_job, state="none"),
            _state_condition(lifecycle, state_not="running"),
            _state_condition(operation, state_not="waiting_approval"),
            _state_condition(operation, state_not="running"),
        ]
    raise ValueError(f"Unsupported operator action: {action}")


def _controls_section(vmid: int, cfg: dict[str, Any]) -> dict[str, Any]:
    actions = [name for name, enabled in cfg["operator_capabilities"].items() if enabled]
    controls = [
        {
            "type": "conditional",
            "conditions": _control_conditions(vmid, cfg, action),
            "card": _control_card(vmid, action),
        }
        for action in actions
    ]
    return _section(
        _title("Sterowanie", "Każda akcja wymaga potwierdzenia"),
        _entity_grid(controls, columns=2),
    )


def _apt_sections(vmid: int, cfg: dict[str, Any]) -> list[dict[str, Any]]:
    health = _entity(vmid, cfg, "health_status")
    resources = _entity_grid(
        [
            _entity_card(_entity(vmid, cfg, "disk_used_percent"), "Dysk zajęty", "mdi:harddisk"),
            _entity_card(_entity(vmid, cfg, "disk_free_mb"), "Wolne miejsce", "mdi:database-arrow-down-outline", "cyan"),
            _entity_card(_entity(vmid, cfg, "memory_used_percent"), "Pamięć RAM", "mdi:memory", "purple"),
            _entity_card(_entity(vmid, cfg, "lxc_status"), "Status LXC", "mdi:server", "green"),
            _entity_card(_entity(vmid, cfg, "uptime_seconds"), "Uptime", "mdi:timer-outline", "green"),
        ]
    )
    operation = {
        "type": "custom:mushroom-template-card",
        "entity": _entity(vmid, cfg, "operation_status"),
        "primary": "{{ states(entity) | replace('_', ' ') | title }}",
        "secondary": (
            f"Etap: {{{{ states('{_entity(vmid, cfg, 'job_stage')}') }}}} · "
            f"postęp: {{{{ states('{_entity(vmid, cfg, 'job_progress')}') }}}}%"
        ),
        "multiline_secondary": True,
        "icon": "mdi:progress-wrench",
        "color": "{{ 'blue' if is_state(entity, 'running') else 'amber' if is_state(entity, 'waiting_approval') else 'green' }}",
        "tap_action": {"action": "more-info"},
    }
    verification = _entity_grid(
        [
            _entity_card(_entity(vmid, cfg, "verification_status"), "Weryfikacja", "mdi:check-decagram"),
            _entity_card(_entity(vmid, cfg, "apt_check_ok"), "APT check", "mdi:package-check", "green"),
            _entity_card(_entity(vmid, cfg, "dpkg_audit_ok"), "dpkg audit", "mdi:shield-check-outline", "green"),
            _entity_card(_entity(vmid, cfg, "reboot_required"), "Restart wymagany", "mdi:restart-alert", "amber"),
            _entity_card(_entity(vmid, cfg, "packages_remaining_count"), "Pakiety pozostałe", "mdi:package-variant"),
            _entity_card(_entity(vmid, cfg, "last_operation_result"), "Ostatni wynik", "mdi:history"),
        ]
    )
    diagnostics = _entity_grid(
        [
            _entity_card(_entity(vmid, cfg, "last_refresh"), "Ostatnie odświeżenie", "mdi:refresh"),
            _entity_card(_entity(vmid, cfg, "last_scan"), "Ostatni skan", "mdi:magnify-scan"),
            _entity_card(_entity(vmid, cfg, "last_update"), "Ostatnia aktualizacja", "mdi:update", "green"),
            _entity_card(_entity(vmid, cfg, "active_plan_id"), "Aktywny plan", "mdi:clipboard-text-clock-outline", "amber"),
            _entity_card(_entity(vmid, cfg, "active_job_id"), "Aktywny job", "mdi:identifier"),
            _entity_card(_entity(vmid, cfg, "rollback_allowed"), "Rollback", "mdi:backup-restore", "amber"),
            _entity_card(_entity(vmid, cfg, "last_error"), "Ostatni błąd", "mdi:alert-circle-outline", "red"),
        ]
    )
    package_card = {
        "type": "markdown",
        "title": "Pakiety APT",
        "content": (
            f"{{% set updates = state_attr('{health}', 'updates') or {{}} %}}\n"
            "{% set packages = updates.get('packages', []) %}\n"
            f"{{% set meta = state_attr('{health}', 'attribute_payload') or {{}} %}}\n"
            "{% set total = meta.get('packages_total', packages | count) %}\n"
            "{% set visible = packages | count %}\n"
            "**Łącznie: {{ total }} · widoczne: {{ visible }}**\n\n"
            "{% for package in packages[:30] %}- `{{ package.get('name', '?') }}`\n{% endfor %}"
            "{% if visible > 30 %}\n_… {{ visible - 30 }} kolejnych widocznych pakietów._\n{% endif %}"
            "{% if meta.get('truncated') %}\n_Podgląd ogranicza limit atrybutów 10 KB._\n{% endif %}"
        ),
    }
    logs_card = {
        "type": "markdown",
        "title": "Logi live",
        "content": (
            f"{{% set events = state_attr('{health}', 'recent_job_events') or [] %}}\n"
            "{% for event in events[-25:] | reverse %}"
            "- `{{ event.get('created_at', '') }}` **{{ event.get('stage', '') }}** "
            "{{ event.get('message', '') | replace('|', '¦') }}\n"
            "{% endfor %}"
        ),
    }
    sections = [
        _section(
            _title(_label(vmid, cfg), f"Adapter APT · {cfg['criticality']}"),
            _resource_status(vmid, cfg),
            _resource_chips(vmid, cfg),
        ),
        _section(_title("Zasoby", "Ostatni pomiar z kontenera"), resources),
        _section(_title("Aktualizacje", "Stan operacji i planu"), operation),
        _section(_title("Weryfikacja końcowa", "APT, dpkg i wynik operacji"), verification),
        _section(
            _title("Recovery scan", "Stan bezpiecznego skanu odzyskiwania"),
            _entity_grid(
                [
                    _entity_card(_entity(vmid, cfg, "recovery_scan_status"), "Status", "mdi:shield-sync-outline"),
                    _entity_card(_entity(vmid, cfg, "last_recovery_scan"), "Ostatni skan", "mdi:history"),
                    _entity_card(_entity(vmid, cfg, "last_recovery_scan_result"), "Ostatni wynik", "mdi:shield-check-outline"),
                ]
            ),
        ),
        _section(_title("Historia i diagnostyka", "Ostatnie operacje oraz błędy"), diagnostics),
        _section(_title("Pakiety i diagnostyka", "Ograniczony podgląd MQTT"), package_card, logs_card),
    ]
    docker = cfg.get("docker") or {}
    if docker.get("enabled"):
        sections.append(
            _section(
                _title("Docker", "Tylko skonfigurowane kontenery"),
                _entity_grid(
                    [
                        _entity_card(_entity(vmid, cfg, "docker_required_healthy"), "Wymagane healthy", "mdi:docker", "green"),
                        _entity_card(_entity(vmid, cfg, "docker_required_total"), "Wymagane łącznie", "mdi:format-list-checks"),
                    ]
                ),
            )
        )
    sections.append(
        _controls_section(vmid, cfg)
        if any(cfg["operator_capabilities"].values())
        else _observation_section(vmid, cfg)
    )
    return sections


def _qemu_sections(vmid: int, cfg: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        _section(
            _title(_label(vmid, cfg), "HAOS · obserwacja QEMU"),
            _resource_status(vmid, cfg),
            _resource_chips(vmid, cfg),
        ),
        _section(
            _title("CPU i pamięć", "Metryki dostarczane przez Proxmox"),
            _entity_grid(
                [
                    _entity_card(_entity(vmid, cfg, "cpu_usage"), "CPU", "mdi:cpu-64-bit"),
                    _entity_card(_entity(vmid, cfg, "cpu_cores"), "Rdzenie CPU", "mdi:chip"),
                    _entity_card(_entity(vmid, cfg, "memory_used_bytes"), "RAM użyta", "mdi:memory", "purple"),
                    _entity_card(_entity(vmid, cfg, "memory_total_bytes"), "RAM łącznie", "mdi:memory", "purple"),
                ]
            ),
        ),
        _section(
            _title("Dysk i sieć", "Liczniki QEMU"),
            _entity_grid(
                [
                    _entity_card(_entity(vmid, cfg, "disk_used_bytes"), "Dysk użyty", "mdi:harddisk"),
                    _entity_card(_entity(vmid, cfg, "disk_total_bytes"), "Dysk łącznie", "mdi:harddisk"),
                    _entity_card(_entity(vmid, cfg, "network_in_bytes"), "Sieć odebrana", "mdi:download-network", "cyan"),
                    _entity_card(_entity(vmid, cfg, "network_out_bytes"), "Sieć wysłana", "mdi:upload-network", "cyan"),
                ]
            ),
        ),
        _section(
            _title("QEMU Guest Agent", "Adres główny nie przekracza limitu stanu HA"),
            _entity_grid(
                [
                    _entity_card(_entity(vmid, cfg, "qemu_status"), "Status QEMU", "mdi:monitor"),
                    _entity_card(_entity(vmid, cfg, "guest_agent_status"), "Guest Agent", "mdi:lan-connect", "green"),
                    _entity_card(_entity(vmid, cfg, "ip_addresses"), "Primary IP", "mdi:ip-network", "cyan"),
                    _entity_card(_entity(vmid, cfg, "uptime_seconds"), "Uptime", "mdi:timer-outline", "green"),
                ]
            ),
        ),
        _observation_section(vmid, cfg),
    ]


def _agent_self_sections(vmid: int, cfg: dict[str, Any]) -> list[dict[str, Any]]:
    health = _entity(vmid, cfg, "health_status")
    return [
        _section(
            _title(_label(vmid, cfg), "Self-health agenta"),
            _resource_status(vmid, cfg),
            _resource_chips(vmid, cfg),
        ),
        _section(
            _title("Usługa i API", "Lokalna kontrola CT110"),
            _entity_grid(
                [
                    _entity_card(_entity(vmid, cfg, "service_status"), "Usługa", "mdi:application-cog", "green"),
                    _entity_card(_entity(vmid, cfg, "api_health"), "API health", "mdi:api", "green"),
                    _entity_card(_entity(vmid, cfg, "agent_version"), "Wersja", "mdi:tag-outline"),
                    _entity_card(_entity(vmid, cfg, "uptime_seconds"), "Uptime", "mdi:timer-outline"),
                ]
            ),
        ),
        _section(
            _title("CPU i pamięć", "Bez nieobsługiwanych liczników sieci"),
            _entity_grid(
                [
                    _entity_card(_entity(vmid, cfg, "cpu_load_1m"), "Load 1m", "mdi:gauge"),
                    _entity_card(_entity(vmid, cfg, "cpu_cores"), "Rdzenie CPU", "mdi:chip"),
                    _entity_card(_entity(vmid, cfg, "memory_used_bytes"), "RAM użyta", "mdi:memory", "purple"),
                    _entity_card(_entity(vmid, cfg, "memory_total_bytes"), "RAM łącznie", "mdi:memory", "purple"),
                    _entity_card(_entity(vmid, cfg, "memory_available_bytes"), "RAM dostępna", "mdi:memory-arrow-down", "cyan"),
                ]
            ),
        ),
        _section(
            _title("Dysk", "Lokalny system plików agenta"),
            _entity_grid(
                [
                    _entity_card(_entity(vmid, cfg, "disk_used_bytes"), "Dysk użyty", "mdi:harddisk"),
                    _entity_card(_entity(vmid, cfg, "disk_total_bytes"), "Dysk łącznie", "mdi:harddisk"),
                    _entity_card(_entity(vmid, cfg, "disk_free_bytes"), "Dysk wolny", "mdi:database-arrow-down-outline", "cyan"),
                ]
            ),
        ),
        _section(
            _title("Ostatnie ostrzeżenia", "Maksymalnie 20 bezpiecznie zredagowanych wpisów"),
            _entity_card(_entity(vmid, cfg, "recent_warnings"), "Liczba ostrzeżeń", "mdi:alert-outline", "amber"),
            {
                "type": "markdown",
                "title": "Podgląd techniczny",
                "content": (
                    f"{{% set warnings = state_attr('{health}', 'recent_warnings') or [] %}}\n"
                    "{% for warning in warnings[-20:] | reverse %}- {{ warning | replace('|', '¦') }}\n{% endfor %}"
                    "{% if not warnings %}Brak ostatnich ostrzeżeń.{% endif %}"
                ),
            },
        ),
        _observation_section(vmid, cfg),
    ]


def _resource_view(vmid: int, cfg: dict[str, Any]) -> dict[str, Any]:
    identity = normalize_resource_identity(cfg)
    if identity.adapter == "apt":
        sections = _apt_sections(vmid, cfg)
    elif identity.resource_type == "qemu":
        sections = _qemu_sections(vmid, cfg)
    else:
        sections = _agent_self_sections(vmid, cfg)
    return {
        "title": f"{'VM' if identity.resource_type == 'qemu' else 'CT'}{vmid}",
        "path": str(cfg["dashboard_path"]).rsplit("/", 1)[-1],
        "icon": ICONS.get(vmid, "mdi:server"),
        "type": "sections",
        "max_columns": 4,
        "sections": sections,
    }


def build_dashboard(resources: dict[int, dict[str, Any]]) -> dict[str, Any]:
    resources = normalize_dashboard_resources(resources)
    agent_chips = _chips(
        [
            _chip(
                agent_entity_id("availability"),
                "mdi:mqtt",
                icon_color="{{ 'green' if is_state(entity, 'online') else 'red' }}",
            ),
            _chip(
                agent_entity_id("version"),
                "mdi:tag-outline",
                icon_color="blue",
            ),
            _chip(
                agent_entity_id("configured_resource_count"),
                "mdi:server-network",
                content="{{ states(entity) }} zasobów",
                icon_color="blue",
            ),
            _chip(
                agent_entity_id("active_job_count"),
                "mdi:progress-wrench",
                content="{{ states(entity) }} zadań",
                icon_color=(
                    "{% set value = states(entity) | int(-1) %} "
                    "{{ 'green' if value == 0 else 'amber' if value > 0 else 'grey' }}"
                ),
            ),
            _chip(
                agent_entity_id("last_refresh"),
                "mdi:clock-outline",
                icon_color="blue-grey",
            ),
        ]
    )
    items = sorted(resources.items())
    overview_sections = [
        _section(
            _title("Hubinet Ops", "Pełne inventory Proxmox · backend jest źródłem prawdy"),
            agent_chips,
        )
    ]
    for index in range(0, len(items), 4):
        chunk = items[index : index + 4]
        overview_sections.append(
            _section(
                _title("Zasoby", f"{_label(chunk[0][0], chunk[0][1]).split(' · ')[0]}–{_label(chunk[-1][0], chunk[-1][1]).split(' · ')[0]}"),
                *[_overview_card(vmid, cfg) for vmid, cfg in chunk],
            )
        )
    overview = {
        "title": "Centrum",
        "path": "overview",
        "icon": "mdi:server-network",
        "type": "sections",
        "max_columns": 4,
        "sections": overview_sections,
    }
    return {
        "title": "Hubinet Ops",
        "views": [overview]
        + [_resource_view(vmid, cfg) for vmid, cfg in sorted(resources.items())],
    }


def normalize_dashboard_resources(
    resources: Mapping[int, Mapping[str, Any]],
) -> dict[int, dict[str, Any]]:
    normalized: dict[int, dict[str, Any]] = {}
    for raw_vmid, raw_cfg in resources.items():
        try:
            vmid = int(raw_vmid)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"Invalid resource VMID: {raw_vmid!r}") from exc
        if not isinstance(raw_cfg, Mapping):
            raise RuntimeError(f"Resource {vmid} must be a mapping")
        try:
            identity = normalize_resource_identity(raw_cfg)
        except ValueError as exc:
            raise RuntimeError(f"Invalid resource identity for VMID {vmid}: {exc}") from exc
        cfg = dict(raw_cfg)
        cfg["resource_type"] = identity.resource_type
        cfg["adapter"] = identity.adapter
        normalized[vmid] = cfg
    return normalized


def load_resources(path: Path) -> dict[int, dict[str, Any]]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    resources = raw.get("resources")
    if not isinstance(resources, dict):
        raise RuntimeError("Configuration must contain resources")
    return normalize_dashboard_resources(resources)


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
