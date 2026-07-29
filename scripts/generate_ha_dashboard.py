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

PL_UI_TRANSLATIONS = {
    "healthy": "sprawny",
    "degraded": "obniżony",
    "critical": "krytyczny",
    "offline": "offline",
    "running": "uruchomiony",
    "stopped": "zatrzymany",
    "active": "aktywna",
    "available": "dostępny",
    "unavailable": "niedostępny",
    "update_available": "dostępna aktualizacja",
    "up_to_date": "aktualny",
    "waiting_approval": "oczekuje na zatwierdzenie",
    "idle": "bezczynne",
    "success": "zakończono pomyślnie",
    "passed": "zakończono pomyślnie",
    "failed": "niepowodzenie",
    "blocked": "zablokowane",
    "allowed": "dozwolone",
    "compatible": "zgodny",
    "incompatible": "niezgodny",
    "valid": "prawidłowy",
    "none": "brak",
    "unknown": "Brak danych",
    "high": "wysoki",
    "medium": "średni",
    "low": "niski",
    "warning": "ostrzeżenie",
    "scanning": "skanowanie",
    "queued": "w kolejce",
    "completed": "zakończone",
    "rolled_back": "przywrócono snapshot",
    "manual_intervention": "wymagana interwencja",
    "insufficient_health_contract": "Ograniczona weryfikacja",
    "snapshot_delete": "Usuwanie snapshota",
    "recovery_scan": "Skan odzyskiwania",
    "break_glass_recovery": "Awaryjne odzyskiwanie",
    "self_update": "Plan aktualizacji Hubinet Ops",
    "pre-update": "przed aktualizacją",
    "manual": "ręczny",
    "ok": "prawidłowy",
    "yes": "tak",
    "no": "nie",
}
MISSING_STATES = ("unknown", "unavailable", "none", "None", "")

CONTROL_ORDER = (
    "start",
    "shutdown",
    "reboot",
    "force_stop",
    "refresh",
    "scan",
    "approve",
    "reject",
    "retry_healthcheck",
    "snapshot_create",
    "snapshot_rollback",
    "snapshot_delete",
    "self_update",
)


def _label(vmid: int, cfg: dict[str, Any]) -> str:
    kind = "VM" if normalize_resource_identity(cfg).resource_type == "qemu" else "CT"
    return f"{kind}{vmid} · {cfg['display_name']}"


def _entity(vmid: int, cfg: dict[str, Any], key: str) -> str:
    return resource_entity_id(vmid, cfg, key)


def _state_label(entity_expression: str = "entity") -> str:
    labels = repr(PL_UI_TRANSLATIONS)
    missing = repr(list(MISSING_STATES))
    return (
        f"{{% set labels = {labels} %}}"
        f"{{% set value = states({entity_expression}) %}}"
        f"{{{{ 'Brak danych' if value in {missing} "
        "else labels.get(value, value | replace('_', ' ')) }}}}"
    )


def _semantic_icon(fallback: str) -> str:
    return (
        "{% set value = states(entity) %}"
        "{{ 'mdi:check-circle' if value in ['healthy', 'up_to_date', 'success', 'passed', 'valid', 'compatible'] "
        "else 'mdi:update' if value == 'update_available' "
        "else 'mdi:clock-outline' if value == 'waiting_approval' "
        "else 'mdi:sync' if value in ['running', 'scanning'] "
        "else 'mdi:alert' if value in ['failed', 'critical', 'incompatible'] "
        "else 'mdi:alert-outline' if value in ['warning', 'degraded', 'insufficient_health_contract'] "
        f"else 'mdi:help-circle-outline' if value in {list(MISSING_STATES)!r} "
        f"else '{fallback}' }}}}"
    )


def _semantic_color(fallback: str) -> str:
    return (
        "{% set value = states(entity) %}"
        "{{ 'green' if value in ['healthy', 'up_to_date', 'success', 'passed', 'valid', 'compatible', 'allowed'] "
        "else 'orange' if value == 'update_available' "
        "else 'yellow' if value in ['waiting_approval', 'warning', 'degraded', 'insufficient_health_contract'] "
        "else 'blue' if value in ['running', 'scanning'] "
        "else 'red' if value in ['failed', 'critical', 'incompatible', 'blocked'] "
        f"else 'grey' if value in {list(MISSING_STATES)!r} "
        f"else '{fallback}' }}}}"
    )


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
        "type": "custom:mushroom-template-card",
        "entity": entity,
        "primary": name,
        "secondary": _state_label(),
        "icon": _semantic_icon(icon),
        "icon_color": _semantic_color(color),
        "fill_container": True,
        "multiline_secondary": True,
        "tap_action": {"action": "more-info"},
    }


def _entity_grid(cards: Iterable[dict[str, Any]], columns: int = 2) -> dict[str, Any]:
    return {
        "type": "grid",
        "columns": columns,
        "square": False,
        "cards": list(cards),
    }


def _uptime_card(entity: str) -> dict[str, Any]:
    return {
        "type": "custom:mushroom-template-card",
        "entity": entity,
        "primary": "Uptime",
        "secondary": (
            "{% set seconds = states(entity) | int(0) %}"
            "{% set days = seconds // 86400 %}"
            "{% set hours = (seconds % 86400) // 3600 %}"
            "{% set minutes = (seconds % 3600) // 60 %}"
            "{{ days }} d {{ hours }} h {{ minutes }} min"
        ),
        "icon": "mdi:timer-outline",
        "color": "green",
        "tap_action": {"action": "more-info"},
    }


def _timestamp_card(entity: str, name: str, icon: str) -> dict[str, Any]:
    return {
        "type": "custom:mushroom-template-card",
        "entity": entity,
        "primary": name,
        "secondary": (
            "{% set value = states(entity) %}"
            "{% if value in ['unknown', 'unavailable', 'none', 'None', ''] %}"
            "Brak danych"
            "{% else %}"
            "{% set stamp = as_timestamp(value, none) %}"
            "{{ stamp | timestamp_custom('%d.%m.%Y %H:%M', true) if stamp is not none else 'Nieprawidłowa data' }}"
            "{% endif %}"
        ),
        "icon": icon,
        "color": "blue-grey",
        "tap_action": {"action": "more-info"},
    }


def _format_bytes_value(value_expression: str) -> str:
    return (
        f"{{% set raw = {value_expression} %}}"
        "{% if raw in ['unknown', 'unavailable', 'none', 'None', '', none] %}"
        "Brak danych"
        "{% else %}"
        "{% set value = raw | float(0) %}"
        "{% if value >= 1099511627776 %}{{ (value / 1099511627776) | round(2) }} TiB"
        "{% elif value >= 1073741824 %}{{ (value / 1073741824) | round(2) }} GiB"
        "{% elif value >= 1048576 %}{{ (value / 1048576) | round(1) }} MiB"
        "{% elif value >= 1024 %}{{ (value / 1024) | round(1) }} KiB"
        "{% else %}{{ value | round(0) }} B{% endif %}"
        "{% endif %}"
    )


def _bytes_card(entity: str, name: str, icon: str, color: str = "blue") -> dict[str, Any]:
    return {
        "type": "custom:mushroom-template-card",
        "entity": entity,
        "primary": name,
        "secondary": _format_bytes_value("states(entity)"),
        "icon": icon,
        "icon_color": (
            "{{ 'grey' if states(entity) in ['unknown', 'unavailable', 'none', ''] "
            f"else '{color}' }}}}"
        ),
        "fill_container": True,
        "tap_action": {"action": "more-info"},
    }


def _bytes_summary_card(
    *,
    name: str,
    used: str,
    total: str,
    free: str | None,
    icon: str,
) -> dict[str, Any]:
    used_label = _format_bytes_value(f"states('{used}')")
    total_label = _format_bytes_value(f"states('{total}')")
    free_label = (
        _format_bytes_value(f"states('{free}')")
        if free is not None
        else _format_bytes_value(
            f"(states('{total}') | float - states('{used}') | float) "
            f"if states('{used}') not in {list(MISSING_STATES)!r} "
            f"and states('{total}') not in {list(MISSING_STATES)!r} else none"
        )
    )
    return {
        "type": "custom:mushroom-template-card",
        "entity": used,
        "primary": name,
        "secondary": (
            "Użyto: "
            + used_label
            + " · Łącznie: "
            + total_label
            + " · Wolne: "
            + free_label
            + f"{{% set used = states('{used}') %}}"
            + f"{{% set total = states('{total}') %}}"
            + "{% if used not in ['unknown', 'unavailable', 'none', ''] "
            + "and total not in ['unknown', 'unavailable', 'none', ''] "
            + "and total | float(0) > 0 %}"
            + " · {{ ((used | float) / (total | float) * 100) | round(1) }}%"
            + "{% endif %}"
        ),
        "multiline_secondary": True,
        "icon": icon,
        "icon_color": (
            "{{ 'grey' if states(entity) in ['unknown', 'unavailable', 'none', ''] "
            "else 'purple' }}"
        ),
        "fill_container": True,
        "tap_action": {"action": "more-info"},
    }


def _plan_card(vmid: int, cfg: dict[str, Any]) -> dict[str, Any]:
    plan = _entity(vmid, cfg, "active_plan_id")
    status = _entity(vmid, cfg, "active_plan_status")
    packages = _entity(vmid, cfg, "pending_updates")
    risk = _entity(vmid, cfg, "risk")
    return {
        "type": "custom:mushroom-template-card",
        "entity": plan,
        "primary": (
            f"{{% if states('{status}') == 'waiting_approval' %}}"
            f"Oczekuje · {{{{ states('{packages}') }}}} pakietów · ryzyko "
            + _state_label(repr(risk))
            + "{% else %}"
            + _state_label(repr(status))
            + "{% endif %}"
        ),
        "secondary": "{{ ('ID ' ~ states(entity)[:8]) if states(entity) not in ['none', 'unknown', 'unavailable'] else 'Brak aktywnego planu' }}",
        "icon": "mdi:clipboard-text-clock-outline",
        "color": "amber",
        "tap_action": {"action": "more-info"},
    }


def _job_card(vmid: int, cfg: dict[str, Any]) -> dict[str, Any]:
    active = _entity(vmid, cfg, "active_job_id")
    last = _entity(vmid, cfg, "last_job_id")
    operation = _entity(vmid, cfg, "operation_type")
    stage = _entity(vmid, cfg, "job_stage")
    progress = _entity(vmid, cfg, "job_progress")
    return {
        "type": "custom:mushroom-template-card",
        "entity": active,
        "primary": (
            _state_label(repr(operation))
            + " · "
            + _state_label(repr(stage))
            + f" · {{{{ states('{progress}') }}}}%"
        ),
        "secondary": (
            f"{{% set identifier = states('{active}') if states('{active}') not in ['none', 'unknown', 'unavailable'] else states('{last}') %}}"
            "{{ ('ID ' ~ identifier[:8]) if identifier not in ['none', 'unknown', 'unavailable'] else 'Brak joba' }}"
        ),
        "icon": "mdi:progress-wrench",
        "color": "blue",
        "tap_action": {"action": "more-info"},
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
        "content": content or _state_label(),
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
    secondary_parts = ["Runtime: " + _state_label(repr(runtime))]
    profile = (
        _entity(vmid, cfg, "profile_validation_status")
        if identity.adapter == "apt"
        else ""
    )
    update = _entity(vmid, cfg, "update_status") if identity.adapter == "apt" else None
    if identity.adapter == "apt":
        secondary_parts.extend(
            [
                "aktualizacje: " + _state_label(repr(str(update))),
                f"pakiety: {{{{ states('{_entity(vmid, cfg, 'pending_updates')}') }}}}",
            ]
        )
    elif identity.resource_type == "qemu":
        secondary_parts.append(
            "Agent gościa: "
            + _state_label(repr(_entity(vmid, cfg, "guest_agent_status")))
        )
    else:
        secondary_parts.extend(
            [
                "usługa: "
                + _state_label(repr(_entity(vmid, cfg, "service_status"))),
                "API: " + _state_label(repr(_entity(vmid, cfg, "api_health"))),
            ]
        )
    return {
        "type": "custom:mushroom-template-card",
        "entity": health,
        "primary": _label(vmid, cfg),
        "secondary": " · ".join(secondary_parts),
        "multiline_secondary": True,
        "icon": (
            (
                f"{{% set update = states('{update}') %}}"
                f"{{% set profile = states('{profile}') %}}"
                "{{ 'mdi:update' if update == 'update_available' "
                "else 'mdi:alert-outline' if profile == 'insufficient_health_contract' "
                "else 'mdi:check-circle' if is_state(entity, 'healthy') "
                "else 'mdi:alert-circle' }}"
            )
            if identity.adapter == "apt"
            else _semantic_icon(ICONS.get(vmid, "mdi:server"))
        ),
        "color": (
            (
                f"{{% set update = states('{update}') %}}"
                f"{{% set profile = states('{profile}') %}}"
                "{% set health = states(entity) %}"
                "{{ 'orange' if update == 'update_available' "
                "else 'yellow' if profile == 'insufficient_health_contract' "
                "else 'green' if health == 'healthy' "
                "else 'yellow' if health == 'degraded' "
                "else 'grey' if health in ['unknown', 'unavailable'] else 'red' }}"
            )
            if identity.adapter == "apt"
            else _semantic_color("blue")
        ),
        "badge_icon": (
            (
                f"{{% set update = states('{update}') %}}"
                f"{{% set profile = states('{profile}') %}}"
                "{{ 'mdi:update' if update == 'update_available' "
                "else 'mdi:alert-outline' if profile == 'insufficient_health_contract' "
                "else 'mdi:check-circle' if is_state(entity, 'healthy') "
                "else 'mdi:alert-circle' }}"
            )
            if identity.adapter == "apt"
            else _semantic_icon("mdi:server")
        ),
        "badge_color": (
            (
                f"{{% set update = states('{update}') %}}"
                f"{{% set profile = states('{profile}') %}}"
                "{{ 'orange' if update == 'update_available' "
                "else 'yellow' if profile == 'insufficient_health_contract' "
                "else 'green' if is_state(entity, 'healthy') "
                "else 'yellow' if is_state(entity, 'degraded') "
                "else 'red' }}"
            )
            if identity.adapter == "apt"
            else _semantic_color("blue")
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


def _qemu_snapshot_controls(vmid: int) -> dict[str, Any]:
    include_ram = "input_boolean.hubinet_ops_vm100_snapshot_include_ram"
    return _section(
        _title(
            "Tworzenie snapshota VM100",
            "Aktualizacje, lifecycle i restore pozostają zablokowane",
        ),
        _entity_grid(
            [
                {
                    "type": "tile",
                    "entity": include_ram,
                    "name": "Dołącz stan RAM",
                    "icon": "mdi:memory",
                },
                {
                    "type": "custom:mushroom-template-card",
                    "entity": include_ram,
                    "primary": "Utwórz snapshot",
                    "secondary": (
                        "{{ 'Stan RAM zostanie dołączony' if is_state(entity, 'on') "
                        "else 'Bez stanu RAM (opcja domyślna)' }}"
                    ),
                    "icon": "mdi:camera-plus-outline",
                    "icon_color": "purple",
                    "tap_action": {
                        "action": "perform-action",
                        "perform_action": "script.hubinet_ops_snapshot_create",
                        "data": {"vmid": vmid},
                        "confirmation": {
                            "title": "Utwórz snapshot VM100",
                            "text": (
                                "{{ 'Snapshot obejmie stan RAM.' "
                                "if is_state('input_boolean.hubinet_ops_vm100_snapshot_include_ram', 'on') "
                                "else 'Snapshot nie obejmie stanu RAM.' }}"
                            ),
                        },
                    },
                },
            ],
            columns=2,
        ),
    )


def _control_card(vmid: int, action: str) -> dict[str, Any]:
    names = {
        "start": "Uruchom",
        "shutdown": "Wyłącz łagodnie",
        "reboot": "Uruchom ponownie",
        "force_stop": "Zatrzymaj natychmiast",
        "refresh": "Odśwież stan",
        "scan": "Sprawdź aktualizacje",
        "approve": "Zatwierdź plan",
        "reject": "Odrzuć plan",
        "retry_healthcheck": "Ponów test zdrowia",
        "snapshot_create": "Utwórz snapshot",
        "snapshot_rollback": "Przywróć ostatni snapshot",
        "snapshot_delete": "Usuń ostatni snapshot",
        "self_update": "Przygotuj plan aktualizacji",
    }
    service_names = {
        "approve": "script.hubinet_ops_approve_container",
        "reject": "script.hubinet_ops_reject_container",
        "retry_healthcheck": "script.hubinet_ops_retry_healthcheck",
        "force_stop": "script.hubinet_ops_force_stop_container",
        "snapshot_create": "script.hubinet_ops_snapshot_create",
        "snapshot_rollback": "script.hubinet_ops_snapshot_restore_latest",
        "snapshot_delete": "script.hubinet_ops_snapshot_delete_latest",
        "self_update": "script.hubinet_ops_self_update",
    }
    secondary = {
        "start": f"Start CT{vmid}",
        "shutdown": f"Łagodne wyłączenie CT{vmid}",
        "reboot": f"Łagodne ponowne uruchomienie CT{vmid}",
        "force_stop": f"Natychmiast odetnij CT{vmid}",
        "refresh": "Health, zasoby i usługi",
        "scan": "Pobierz aktualną listę pakietów",
        "approve": "Snapshot, update i stabilizacja usług",
        "reject": "Nie wykonuj aktualizacji",
        "retry_healthcheck": "Sprawdź stabilizację usług jeszcze raz",
        "snapshot_create": "Ręczny punkt przywracania Hubinet Ops",
        "snapshot_rollback": "Jawnie przywróć stan z ostatniego snapshotu",
        "snapshot_delete": "Trwale usuń ostatni snapshot Hubinet Ops",
        "self_update": "Najpierw utwórz plan, następnie zatwierdź go ręcznie",
    }
    colors = {
        "start": "green",
        "shutdown": "red",
        "reboot": "amber",
        "force_stop": "red",
        "refresh": "cyan",
        "scan": "blue",
        "approve": "green",
        "reject": "red",
        "retry_healthcheck": "amber",
        "snapshot_create": "purple",
        "snapshot_rollback": "red",
        "snapshot_delete": "red",
        "self_update": "amber",
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
        "force_stop": {
            "title": f"Natychmiast zatrzymaj CT{vmid}",
            "text": "Kontener zostanie odcięty bez łagodnego zamknięcia. Grozi to utratą niezapisanych danych.",
            "confirm_text": "Zatrzymaj natychmiast",
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
        "snapshot_create": {
            "title": f"Utwórz snapshot CT{vmid}",
            "text": "Utworzyć ręczny snapshot oznaczony jako własność Hubinet Ops?",
        },
        "snapshot_rollback": {
            "title": f"Przywróć snapshot CT{vmid}",
            "text": "Bieżący stan kontenera zostanie zastąpiony stanem z ostatniego snapshotu Hubinet Ops.",
            "confirm_text": "Przywróć snapshot",
            "dismiss_text": "Anuluj",
        },
        "snapshot_delete": {
            "title": f"Usuń snapshot CT{vmid}",
            "text": "Ostatni snapshot Hubinet Ops zostanie trwale usunięty i nie będzie można go przywrócić.",
            "confirm_text": "Usuń snapshot",
            "dismiss_text": "Anuluj",
        },
        "self_update": {
            "title": "Przygotuj plan aktualizacji Hubinet Ops",
            "text": "Staged release zostanie odczytany bez zmian. Rollout wymaga osobnego ręcznego zatwierdzenia aktywnego planu.",
            "confirm_text": "Utwórz plan",
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
            "force_stop": "mdi:stop-circle",
            "refresh": "mdi:refresh",
            "scan": "mdi:magnify-scan",
            "approve": "mdi:check-decagram",
            "reject": "mdi:close-octagon-outline",
            "retry_healthcheck": "mdi:heart-pulse",
            "snapshot_create": "mdi:camera-plus-outline",
            "snapshot_rollback": "mdi:backup-restore",
            "snapshot_delete": "mdi:delete-alert-outline",
            "self_update": "mdi:update",
        }[action],
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
    runtime = _entity(vmid, cfg, "runtime_status")
    operation = _entity(vmid, cfg, "operation_status")
    active_job = _entity(vmid, cfg, "active_job_id")
    lifecycle = _entity(vmid, cfg, "lifecycle_status")
    capability = _entity(vmid, cfg, f"capability_{action}")

    idle = [
        _state_condition(active_job, state="none"),
        _state_condition(lifecycle, state_not="running"),
        _state_condition(operation, state_not="running"),
    ]
    if action == "refresh":
        return [_state_condition(capability, state="allowed")]
    if action in {"approve", "reject"}:
        return [
            _state_condition(
                _entity(vmid, cfg, "active_plan_status"),
                state="waiting_approval",
            ),
            _state_condition(capability, state="allowed"),
            _state_condition(active_job, state="none"),
        ]
    if action == "retry_healthcheck":
        return [
            _state_condition(_entity(vmid, cfg, "health_status"), state="critical"),
            _state_condition(capability, state="allowed"),
            _state_condition(active_job, state="none"),
            _state_condition(lifecycle, state_not="running"),
        ]
    if action == "scan":
        return [
            _state_condition(runtime, state="running"),
            _state_condition(capability, state="allowed"),
            *idle,
        ]
    if action in {"start", "shutdown", "reboot", "force_stop"}:
        return [
            _state_condition(runtime, state="stopped" if action == "start" else "running"),
            _state_condition(capability, state="allowed"),
            _state_condition(operation, state_not="waiting_approval"),
            *idle,
        ]
    if action == "snapshot_create":
        return [
            _state_condition(capability, state="allowed"),
            *idle,
        ]
    if action in {"snapshot_rollback", "snapshot_delete"}:
        conditions = [
            _state_condition(capability, state="allowed"),
            _state_condition(
                _entity(vmid, cfg, "latest_snapshot_name"), state_not="none"
            ),
            _state_condition(operation, state_not="waiting_approval"),
            *idle,
        ]
        if action == "snapshot_rollback":
            conditions.insert(
                1,
                _state_condition(
                    _entity(vmid, cfg, "snapshot_restore_allowed"),
                    state="allowed",
                ),
            )
        return conditions
    if action == "self_update":
        release_version = _entity(vmid, cfg, "self_update_release_version")
        return [
            _state_condition(runtime, state="running"),
            _state_condition(capability, state="allowed"),
            _state_condition(operation, state_not="waiting_approval"),
            _state_condition(release_version, state_not="none"),
            _state_condition(release_version, state_not="unknown"),
            _state_condition(release_version, state_not="unavailable"),
            *idle,
        ]
    raise ValueError(f"Unsupported operator action: {action}")


def _controls_section(vmid: int, cfg: dict[str, Any]) -> dict[str, Any]:
    capabilities = cfg["operator_capabilities"]
    actions = [name for name in CONTROL_ORDER if capabilities.get(name, False)]
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


def _snapshot_section(vmid: int, cfg: dict[str, Any]) -> dict[str, Any]:
    health = _entity(vmid, cfg, "health_status")
    controls: list[dict[str, Any]] = []
    if cfg["operator_capabilities"].get("snapshot_delete", False):
        controls.extend(
            [
                {
                    "type": "custom:mushroom-template-card",
                    "primary": "Usuń najstarszy",
                    "secondary": "Najstarszy niechroniony snapshot Hubinet Ops",
                    "icon": "mdi:delete-clock-outline",
                    "icon_color": "orange",
                    "tap_action": {
                        "action": "perform-action",
                        "perform_action": "script.hubinet_ops_snapshot_delete_oldest",
                        "data": {"vmid": vmid},
                        "confirmation": {
                            "title": "Usuń najstarszy snapshot",
                            "text": (
                                "Backend ponownie sprawdzi własność i ochronę "
                                "przed usunięciem."
                            ),
                        },
                    },
                },
                {
                    "type": "custom:mushroom-template-card",
                    "primary": "Usuń niechronione",
                    "secondary": "Wszystkie niechronione snapshoty Hubinet Ops",
                    "icon": "mdi:delete-sweep-outline",
                    "icon_color": "red",
                    "tap_action": {
                        "action": "perform-action",
                        "perform_action": "script.hubinet_ops_snapshot_delete_unprotected",
                        "data": {"vmid": vmid},
                        "confirmation": {
                            "title": "Usuń wszystkie niechronione",
                            "text": (
                                "Potwierdzasz usunięcie wszystkich aktualnie "
                                "niechronionych snapshotów Hubinet Ops. Backend "
                                "ponownie sprawdzi ochronę każdego wpisu."
                            ),
                        },
                    },
                },
            ]
        )
    return _section(
        _title(
            "Snapshoty",
            "Wyłącznie punkty należące do Hubinet Ops · retencja i ochrona po stronie backendu",
        ),
        _entity_grid(
            [
                _entity_card(
                    _entity(vmid, cfg, "snapshot_count"),
                    "Liczba snapshotów",
                    "mdi:camera-burst",
                ),
                _entity_card(
                    _entity(vmid, cfg, "latest_snapshot_name"),
                    "Ostatni snapshot",
                    "mdi:camera-outline",
                ),
                _entity_card(
                    _entity(vmid, cfg, "latest_snapshot_kind"),
                    "Typ",
                    "mdi:shape-outline",
                ),
                _timestamp_card(
                    _entity(vmid, cfg, "latest_snapshot_at"),
                    "Utworzony",
                    "mdi:calendar-clock",
                ),
                _entity_card(
                    _entity(vmid, cfg, "snapshot_operation_status"),
                    "Ostatnia operacja",
                    "mdi:progress-check",
                ),
            ]
        ),
        {
            "type": "markdown",
            "title": "Ostatnie snapshoty Hubinet Ops",
            "content": (
                f"{{% set snapshots = state_attr('{health}', 'managed_snapshots') or [] %}}\n"
                "{% for snapshot in snapshots[:5] %}"
                "{% set stamp = as_timestamp(snapshot.get('created_at'), none) %}"
                "- **{{ 'Przed aktualizacją' if snapshot.get('logical_type') == 'pre-update' "
                "else 'Ręczny' }}** · "
                "{{ stamp | timestamp_custom('%d.%m.%Y %H:%M', true) if stamp is not none else 'Brak danych' }} · "
                "{{ 'chroniony: ' ~ (snapshot.get('protection_reason') or 'polityka') "
                "if snapshot.get('protected') else 'niechroniony' }}\n"
                "{% endfor %}"
                "{% if not snapshots %}Brak zarządzanych snapshotów lub wymagane jest odświeżenie.{% endif %}"
            ),
        },
        *([_entity_grid(controls, columns=2)] if controls else []),
    )


def _ct110_break_glass_section() -> dict[str, Any]:
    def card(
        primary: str,
        secondary: str,
        service: str,
        confirmation: str,
        icon: str,
    ) -> dict[str, Any]:
        return {
            "type": "custom:mushroom-template-card",
            "primary": primary,
            "secondary": secondary,
            "multiline_secondary": True,
            "icon": icon,
            "color": "red",
            "tap_action": {
                "action": "perform-action",
                "perform_action": service,
                "confirmation": {
                    "title": primary,
                    "text": confirmation,
                    "confirm_text": "Rozumiem — wykonaj recovery",
                    "dismiss_text": "Anuluj",
                },
            },
        }

    return _section(
        _title(
            "Awaryjne odzyskiwanie CT110",
            "Osobny token recovery; brak automatycznego fallbacku z backendu",
        ),
        _entity_grid(
            [
                card(
                    "AWARYJNE przywrócenie CT110 offline",
                    "Tylko gdy CT110 jest zatrzymany. Następny start unieważni "
                    "przywrócone aktywne plany i joby.",
                    "script.hubinet_ops_offline_snapshot_restore_ct110",
                    "Przywrócić ostatni kwalifikujący się snapshot zatrzymanego "
                    "CT110? To jest operacja recovery poza backendem.",
                    "mdi:backup-restore",
                ),
                card(
                    "AWARYJNE wymuszenie zatrzymania CT110",
                    "Osobna operacja recovery, nie zwykły force-stop backendu.",
                    "script.hubinet_ops_offline_force_stop_ct110",
                    "Wymusić zatrzymanie CT110 bez łagodnego zamknięcia?",
                    "mdi:alert-octagon",
                ),
            ],
            columns=2,
        ),
    )


def _executor_section(vmid: int, cfg: dict[str, Any]) -> dict[str, Any]:
    return _section(
        _title("Executor", "Kontrakt wymagany przed operacjami destrukcyjnymi"),
        _entity_grid(
            [
                _entity_card(
                    _entity(vmid, cfg, "executor_compatible"),
                    "Zgodność",
                    "mdi:shield-check-outline",
                ),
                _entity_card(
                    _entity(vmid, cfg, "profile_validation_status"),
                    "Kontrakt",
                    "mdi:file-certificate-outline",
                ),
                _entity_card(
                    _entity(vmid, cfg, "executor_version"),
                    "Wersja",
                    "mdi:tag-outline",
                ),
                _entity_card(
                    _entity(vmid, cfg, "executor_protocol_version"),
                    "Protokół",
                    "mdi:connection",
                ),
                _entity_card(
                    _entity(vmid, cfg, "executor_missing_actions"),
                    "Brakujące akcje",
                    "mdi:alert-outline",
                    "amber",
                ),
                _timestamp_card(
                    _entity(vmid, cfg, "executor_last_checked_at"),
                    "Ostatnie sprawdzenie",
                    "mdi:clock-check-outline",
                ),
            ]
        ),
    )


def _apt_sections(vmid: int, cfg: dict[str, Any]) -> list[dict[str, Any]]:
    health = _entity(vmid, cfg, "health_status")
    resources = _entity_grid(
        [
            _entity_card(_entity(vmid, cfg, "disk_used_percent"), "Dysk zajęty", "mdi:harddisk"),
            _entity_card(_entity(vmid, cfg, "disk_free_mb"), "Wolne miejsce", "mdi:database-arrow-down-outline", "cyan"),
            _entity_card(_entity(vmid, cfg, "memory_used_percent"), "Pamięć RAM", "mdi:memory", "purple"),
            _entity_card(_entity(vmid, cfg, "lxc_status"), "Status LXC", "mdi:server", "green"),
            _uptime_card(_entity(vmid, cfg, "uptime_seconds")),
        ]
    )
    operation = {
        "type": "custom:mushroom-template-card",
        "entity": _entity(vmid, cfg, "operation_status"),
        "primary": _state_label(),
        "secondary": (
            "Etap: "
            + _state_label(repr(_entity(vmid, cfg, "job_stage")))
            + " · "
            f"postęp: {{{{ states('{_entity(vmid, cfg, 'job_progress')}') }}}}%"
        ),
        "multiline_secondary": True,
        "icon": "mdi:progress-wrench",
        "icon_color": _semantic_color("green"),
        "tap_action": {"action": "more-info"},
    }
    verification_status = _entity(vmid, cfg, "verification_status")
    apt_check = _entity(vmid, cfg, "apt_check_ok")
    dpkg_audit = _entity(vmid, cfg, "dpkg_audit_ok")
    reboot = _entity(vmid, cfg, "reboot_required")
    remaining = _entity(vmid, cfg, "packages_remaining_count")
    last_result = _entity(vmid, cfg, "last_operation_result")
    last_verification = _entity(vmid, cfg, "last_verification")
    verification_details = {
        "type": "custom:mushroom-template-card",
        "entity": verification_status,
        "primary": "Weryfikacja końcowa · " + _state_label(),
        "secondary": (
            "APT: "
            + _state_label(repr(apt_check))
            + " · dpkg: "
            + _state_label(repr(dpkg_audit))
            + " · restart: "
            + _state_label(repr(reboot))
            + f" · pakiety pozostałe: {{{{ states('{remaining}') "
            "if states('"
            + remaining
            + "') not in ['unknown', 'unavailable', 'none', ''] else 'Brak danych' }}}}"
            + " · ostatni wynik: "
            + _state_label(repr(last_result))
            + f"\nOstatnia weryfikacja: {{{{ as_timestamp(states('{last_verification}'), none) "
            "| timestamp_custom('%d.%m.%Y %H:%M', true) "
            "if as_timestamp(states('"
            + last_verification
            + "'), none) is not none else 'Brak danych' }}}}"
        ),
        "multiline_secondary": True,
        "icon": _semantic_icon("mdi:check-decagram"),
        "icon_color": _semantic_color("blue-grey"),
        "fill_container": True,
        "tap_action": {"action": "more-info"},
    }
    verification = _section(
        {
            "type": "conditional",
            "conditions": [_state_condition(verification_status, state="unknown")],
            "card": {
                "type": "custom:mushroom-template-card",
                "entity": verification_status,
                "primary": "Brak wykonanej weryfikacji aktualizacji",
                "secondary": "Szczegółowe wyniki pojawią się po uruchomieniu aktualizacji.",
                "multiline_secondary": True,
                "icon": "mdi:clipboard-alert-outline",
                "color": "blue-grey",
                "tap_action": {"action": "more-info"},
            },
        },
        {
            "type": "conditional",
            "conditions": [_state_condition(verification_status, state_not="unknown")],
            "card": verification_details,
        },
    )
    diagnostics = _entity_grid(
        [
            _timestamp_card(_entity(vmid, cfg, "last_refresh"), "Ostatnie odświeżenie", "mdi:refresh"),
            _timestamp_card(_entity(vmid, cfg, "last_scan"), "Ostatni skan", "mdi:magnify-scan"),
            _timestamp_card(_entity(vmid, cfg, "last_update"), "Ostatnia aktualizacja", "mdi:update"),
            _timestamp_card(_entity(vmid, cfg, "last_verification"), "Ostatnia weryfikacja", "mdi:check-decagram"),
            _timestamp_card(_entity(vmid, cfg, "lifecycle_started_at"), "Cykl życia rozpoczęty", "mdi:clock-start"),
            _timestamp_card(_entity(vmid, cfg, "lifecycle_finished_at"), "Cykl życia zakończony", "mdi:clock-check"),
            _plan_card(vmid, cfg),
            _job_card(vmid, cfg),
            _entity_card(_entity(vmid, cfg, "rollback_allowed"), "Rollback", "mdi:backup-restore", "amber"),
            _entity_card(_entity(vmid, cfg, "last_operation_result"), "Ostatni wynik", "mdi:history"),
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
        "title": "Logi na żywo",
        "content": (
            f"{{% set events = state_attr('{health}', 'recent_job_events') or [] %}}\n"
            "{% for event in events[-10:] | reverse %}"
            "{% set stamp = as_timestamp(event.get('created_at'), none) %}"
            "- `{{ stamp | timestamp_custom('%H:%M', true) if stamp is not none else '--:--' }}` **{{ event.get('stage', '') }}** "
            "{{ event.get('message', '') | replace('|', '¦') }}\n"
            "{% endfor %}"
        ),
    }
    health_warning = {
        "type": "conditional",
        "conditions": [
            {
                "condition": "state",
                "entity": _entity(vmid, cfg, "profile_validation_status"),
                "state": "insufficient_health_contract",
            }
        ],
        "card": {
            "type": "custom:mushroom-template-card",
            "entity": _entity(vmid, cfg, "profile_validation_status"),
            "primary": "Ograniczona weryfikacja",
            "secondary": (
                "Hubinet Ops sprawdza stan systemu, ale nie ma kompletnego testu "
                "aplikacji. Automatyczny rollback po awarii aplikacji jest wyłączony.\n"
                f"Profil: {cfg.get('name', f'ct-{vmid}')} · brakujący element: "
                f"{{{{ states('{_entity(vmid, cfg, 'executor_contract_error')}') "
                f"if states('{_entity(vmid, cfg, 'executor_contract_error')}') "
                "not in ['none', 'unknown', 'unavailable', ''] "
                "else 'test zdrowia aplikacji' }}}}"
            ),
            "multiline_secondary": True,
            "icon": "mdi:alert-outline",
            "icon_color": "yellow",
            "tap_action": {"action": "more-info"},
        },
    }
    status_section = _section(
        _title(
            "Status",
            f"{_label(vmid, cfg)} · Adapter APT · "
            f"{PL_UI_TRANSLATIONS.get(str(cfg['criticality']), cfg['criticality'])}",
        ),
        health_warning,
        _resource_status(vmid, cfg),
        _resource_chips(vmid, cfg),
    )
    control_section = (
        _controls_section(vmid, cfg)
        if any(cfg["operator_capabilities"].values())
        else _observation_section(vmid, cfg)
    )
    resource_section = _section(_title("Zasoby", "Ostatni pomiar z kontenera"), resources)
    update_section = _section(
        _title("Aktualizacje", "Stan operacji i planu"),
        _entity_grid(
            [
                _entity_card(_entity(vmid, cfg, "update_status"), "Status aktualizacji", "mdi:package-up"),
                _entity_card(_entity(vmid, cfg, "pending_updates"), "Pakiety oczekujące", "mdi:format-list-numbered", "amber"),
            ]
        ),
        operation,
    )
    history_section = _section(
        _title("Historia i diagnostyka", "Ostatnie operacje oraz błędy"), diagnostics
    )
    recovery_section = _section(
        _title("Skan odzyskiwania", "Stan bezpiecznego skanu odzyskiwania"),
        _entity_grid(
            [
                _entity_card(_entity(vmid, cfg, "recovery_scan_status"), "Status", "mdi:shield-sync-outline"),
                _timestamp_card(_entity(vmid, cfg, "last_recovery_scan"), "Ostatni skan", "mdi:history"),
                _entity_card(_entity(vmid, cfg, "last_recovery_scan_result"), "Ostatni wynik", "mdi:shield-check-outline"),
            ]
        ),
    )
    packages_section = _section(
        _title("Pakiety i logi", "Ograniczony podgląd MQTT"), package_card, logs_card
    )
    sections = [status_section, control_section, resource_section, update_section]
    if cfg["operator_capabilities"].get("approve", False):
        sections.append(
            _section(_title("Weryfikacja końcowa", "Wynik po aktualizacji"), verification)
        )
    sections.extend([_snapshot_section(vmid, cfg), _executor_section(vmid, cfg)])
    docker = cfg.get("docker") or {}
    if docker.get("enabled"):
        sections.append(
            _section(
                _title("Docker", "Tylko skonfigurowane kontenery"),
                _entity_grid(
                    [
                        _entity_card(_entity(vmid, cfg, "docker_required_healthy"), "Wymagane sprawne", "mdi:docker", "green"),
                        _entity_card(_entity(vmid, cfg, "docker_required_total"), "Wymagane łącznie", "mdi:format-list-checks"),
                    ]
                ),
            )
        )
    sections.extend([history_section, recovery_section, packages_section])
    return sections


def _qemu_sections(vmid: int, cfg: dict[str, Any]) -> list[dict[str, Any]]:
    sections = [
        _section(
            _title(_label(vmid, cfg), "HAOS · obserwacja QEMU"),
            _resource_status(vmid, cfg),
            _resource_chips(vmid, cfg),
        ),
        _section(
            _title("Sterowanie", "Backend pozostaje źródłem prawdy"),
            {
                "type": "custom:mushroom-template-card",
                "entity": _entity(vmid, cfg, "health_status"),
                "primary": "Tryb obserwacji z bezpiecznym zarządzaniem snapshotami",
                "secondary": (
                    "Aktualizacje APT, lifecycle i restore VM100 są zablokowane. "
                    "Dostępne są wyłącznie snapshoty należące do Hubinet Ops."
                ),
                "multiline_secondary": True,
                "icon": "mdi:eye-lock-outline",
                "color": "blue-grey",
                "tap_action": {"action": "more-info"},
            },
        ),
        _section(
            _title("CPU i pamięć", "Metryki dostarczane przez Proxmox"),
            _entity_grid(
                [
                    _entity_card(_entity(vmid, cfg, "cpu_usage"), "CPU", "mdi:cpu-64-bit"),
                    _entity_card(_entity(vmid, cfg, "cpu_cores"), "Rdzenie CPU", "mdi:chip"),
                    _bytes_summary_card(
                        name="RAM",
                        used=_entity(vmid, cfg, "memory_used_bytes"),
                        total=_entity(vmid, cfg, "memory_total_bytes"),
                        free=None,
                        icon="mdi:memory",
                    ),
                ]
            ),
        ),
        _section(
            _title("Dysk i sieć", "Liczniki QEMU"),
            _entity_grid(
                [
                    _bytes_summary_card(
                        name="Dysk",
                        used=_entity(vmid, cfg, "disk_used_bytes"),
                        total=_entity(vmid, cfg, "disk_total_bytes"),
                        free=None,
                        icon="mdi:harddisk",
                    ),
                    _bytes_card(
                        _entity(vmid, cfg, "network_in_bytes"),
                        "Sieć odebrana",
                        "mdi:download-network",
                        "cyan",
                    ),
                    _bytes_card(
                        _entity(vmid, cfg, "network_out_bytes"),
                        "Sieć wysłana",
                        "mdi:upload-network",
                        "cyan",
                    ),
                ]
            ),
        ),
        _section(
            _title("Agent gościa QEMU", "Adres główny nie przekracza limitu stanu HA"),
            _entity_grid(
                [
                    _entity_card(_entity(vmid, cfg, "qemu_status"), "Status QEMU", "mdi:monitor"),
                    _entity_card(_entity(vmid, cfg, "guest_agent_status"), "Agent gościa", "mdi:lan-connect", "green"),
                    _entity_card(_entity(vmid, cfg, "ip_addresses"), "Główny adres IP", "mdi:ip-network", "cyan"),
                    _uptime_card(_entity(vmid, cfg, "uptime_seconds")),
                    _timestamp_card(
                        _entity(vmid, cfg, "last_refresh"),
                        "Ostatnie odświeżenie",
                        "mdi:refresh",
                    ),
                ]
            ),
        ),
    ]
    if cfg["operator_capabilities"].get("snapshot_create", False):
        sections.insert(2, _qemu_snapshot_controls(vmid))
    if cfg["operator_capabilities"].get("snapshot_list", False):
        sections.insert(3, _snapshot_section(vmid, cfg))
    return sections


def _agent_self_sections(vmid: int, cfg: dict[str, Any]) -> list[dict[str, Any]]:
    health = _entity(vmid, cfg, "health_status")
    release_version = _entity(vmid, cfg, "self_update_release_version")
    missing_release = _section(
        _title(
            "Brak przygotowanego wydania",
            "Najpierw przygotuj i zweryfikuj wydanie Hubinet Ops na PVE, "
            "następnie odśwież stan CT110.",
        ),
        _entity_grid(
            [
                {
                    "type": "conditional",
                    "conditions": [_state_condition(release_version, state=state)],
                    "card": {
                        "type": "custom:mushroom-template-card",
                        "primary": "Brak przygotowanego wydania",
                        "secondary": (
                            "Akcja przygotowania planu jest niedostępna. "
                            "Wymagany jest staged release na PVE."
                        ),
                        "multiline_secondary": True,
                        "icon": "mdi:package-variant-remove",
                        "icon_color": "grey",
                        "tap_action": {"action": "none"},
                    },
                }
                for state in ("none", "unknown", "unavailable")
            ],
            columns=1,
        ),
    )
    return [
        _section(
            _title(_label(vmid, cfg), "Stan własny agenta"),
            _resource_status(vmid, cfg),
            _resource_chips(vmid, cfg),
        ),
        _controls_section(vmid, cfg),
        missing_release,
        _ct110_break_glass_section(),
        _section(
            _title(
                "Plan aktualizacji Hubinet Ops",
                "Wersja i fingerprint staged release zatwierdzane ręcznie",
            ),
            _entity_grid(
                [
                    _entity_card(
                        _entity(vmid, cfg, "active_plan_status"),
                        "Status planu",
                        "mdi:clipboard-check-outline",
                    ),
                    _entity_card(
                        _entity(vmid, cfg, "active_plan_id"),
                        "ID planu",
                        "mdi:identifier",
                    ),
                    _entity_card(
                        _entity(vmid, cfg, "self_update_release_version"),
                        "Wersja release",
                        "mdi:tag-outline",
                    ),
                    _entity_card(
                        _entity(vmid, cfg, "self_update_release_id"),
                        "ID wydania",
                        "mdi:package-variant-closed",
                    ),
                    _entity_card(
                        _entity(vmid, cfg, "self_update_release_fingerprint"),
                        "Odcisk wydania",
                        "mdi:fingerprint",
                    ),
                ]
            ),
        ),
        _section(
            _title("Usługa i API", "Lokalna kontrola CT110"),
            _entity_grid(
                [
                    _entity_card(_entity(vmid, cfg, "service_status"), "Usługa", "mdi:application-cog", "green"),
                    _entity_card(_entity(vmid, cfg, "api_health"), "Stan API", "mdi:api", "green"),
                    _entity_card(_entity(vmid, cfg, "agent_version"), "Wersja", "mdi:tag-outline"),
                    _uptime_card(_entity(vmid, cfg, "uptime_seconds")),
                ]
            ),
        ),
        _section(
            _title("CPU i pamięć", "Bez nieobsługiwanych liczników sieci"),
            _entity_grid(
                [
                    _entity_card(_entity(vmid, cfg, "cpu_load_1m"), "Obciążenie 1 min", "mdi:gauge"),
                    _entity_card(_entity(vmid, cfg, "cpu_cores"), "Rdzenie CPU", "mdi:chip"),
                    _bytes_summary_card(
                        name="RAM",
                        used=_entity(vmid, cfg, "memory_used_bytes"),
                        total=_entity(vmid, cfg, "memory_total_bytes"),
                        free=_entity(vmid, cfg, "memory_available_bytes"),
                        icon="mdi:memory",
                    ),
                ]
            ),
        ),
        _section(
            _title("Dysk", "Lokalny system plików agenta"),
            _entity_grid(
                [
                    _bytes_summary_card(
                        name="Dysk",
                        used=_entity(vmid, cfg, "disk_used_bytes"),
                        total=_entity(vmid, cfg, "disk_total_bytes"),
                        free=_entity(vmid, cfg, "disk_free_bytes"),
                        icon="mdi:harddisk",
                    ),
                ]
            ),
        ),
        _snapshot_section(vmid, cfg),
        _section(
            _title("Historia i diagnostyka", "Bieżący job i lokalnie sformatowane czasy"),
            _entity_grid(
                [
                    _job_card(vmid, cfg),
                    _timestamp_card(
                        _entity(vmid, cfg, "last_refresh"),
                        "Ostatnie odświeżenie",
                        "mdi:refresh",
                    ),
                    _timestamp_card(
                        _entity(vmid, cfg, "lifecycle_started_at"),
                        "Cykl życia rozpoczęty",
                        "mdi:clock-start",
                    ),
                    _timestamp_card(
                        _entity(vmid, cfg, "lifecycle_finished_at"),
                        "Cykl życia zakończony",
                        "mdi:clock-check",
                    ),
                    _entity_card(
                        _entity(vmid, cfg, "last_error"),
                        "Ostatni błąd",
                        "mdi:alert-circle-outline",
                        "red",
                    ),
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


def _count_summary_card(
    *,
    title: str,
    entities: list[str],
    accepted_states: tuple[str, ...],
    icon: str,
    color: str,
    suffix: str = "",
) -> dict[str, Any]:
    return {
        "type": "custom:mushroom-template-card",
        "primary": title,
        "secondary": (
            f"{{% set entities = {entities!r} %}}"
            "{% set ns = namespace(count=0) %}"
            "{% for item in entities %}"
            f"{{% if states(item) in {list(accepted_states)!r} %}}"
            "{% set ns.count = ns.count + 1 %}{% endif %}"
            "{% endfor %}{{ ns.count }}"
            + suffix
        ),
        "icon": icon,
        "icon_color": color,
        "fill_container": True,
    }


def _last_completed_card(resources: dict[int, dict[str, Any]]) -> dict[str, Any]:
    items = [
        {
            "at": _entity(vmid, cfg, "last_terminal_at"),
            "result": _entity(vmid, cfg, "last_operation_result"),
            "label": _label(vmid, cfg),
        }
        for vmid, cfg in sorted(resources.items())
        if normalize_resource_identity(cfg).resource_type == "lxc"
    ]
    return {
        "type": "custom:mushroom-template-card",
        "primary": "Ostatnio zakończona operacja",
        "secondary": (
            f"{{% set items = {items!r} %}}"
            f"{{% set labels = {PL_UI_TRANSLATIONS!r} %}}"
            "{% set ns = namespace(stamp=none, label='Brak danych', result='unknown') %}"
            "{% for item in items %}"
            "{% set stamp = as_timestamp(states(item.at), none) %}"
            "{% if stamp is not none and (ns.stamp is none or stamp > ns.stamp) %}"
            "{% set ns.stamp = stamp %}{% set ns.label = item.label %}"
            "{% set ns.result = states(item.result) %}{% endif %}{% endfor %}"
            "{{ ns.label if ns.stamp is none else ns.label ~ ' · ' ~ "
            "labels.get(ns.result, ns.result | replace('_', ' ')) ~ ' · ' ~ "
            "(ns.stamp | timestamp_custom('%d.%m.%Y %H:%M', true)) }}"
        ),
        "icon": "mdi:history",
        "icon_color": "blue-grey",
        "fill_container": True,
    }


def _balanced_groups(
    items: list[tuple[int, dict[str, Any]]],
    *,
    target_size: int = 4,
) -> list[list[tuple[int, dict[str, Any]]]]:
    if not items:
        return []
    group_count = max(1, (len(items) + target_size - 1) // target_size)
    base, remainder = divmod(len(items), group_count)
    groups: list[list[tuple[int, dict[str, Any]]]] = []
    offset = 0
    for index in range(group_count):
        size = base + (1 if index < remainder else 0)
        groups.append(items[offset : offset + size])
        offset += size
    return groups


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
                content=(
                    "{{ relative_time(states(entity) | as_datetime) "
                    "if states(entity) not in ['unknown', 'unavailable'] else 'brak' }}"
                ),
                icon_color="blue-grey",
            ),
        ]
    )
    items = sorted(resources.items())
    runtime_entities = [_entity(vmid, cfg, "runtime_status") for vmid, cfg in items]
    update_entities = [
        _entity(vmid, cfg, "update_status")
        for vmid, cfg in items
        if normalize_resource_identity(cfg).adapter == "apt"
    ]
    health_entities = [_entity(vmid, cfg, "health_status") for vmid, cfg in items]
    operation_entities = [
        _entity(vmid, cfg, "operation_status")
        for vmid, cfg in items
        if normalize_resource_identity(cfg).resource_type == "lxc"
    ]
    approval_entities = [
        _entity(vmid, cfg, "active_plan_status")
        for vmid, cfg in items
        if normalize_resource_identity(cfg).resource_type == "lxc"
    ]
    operational_summary = _entity_grid(
        [
            _count_summary_card(
                title="Zasoby online",
                entities=runtime_entities,
                accepted_states=("running",),
                icon="mdi:server-network",
                color="green",
                suffix=f" / {len(runtime_entities)}",
            ),
            _count_summary_card(
                title="Wymagają aktualizacji",
                entities=update_entities,
                accepted_states=("update_available",),
                icon="mdi:update",
                color="orange",
            ),
            _count_summary_card(
                title="Błędy",
                entities=health_entities + operation_entities,
                accepted_states=("critical", "failed", "incompatible"),
                icon="mdi:alert",
                color="red",
            ),
            _count_summary_card(
                title="Oczekują na zatwierdzenie",
                entities=approval_entities,
                accepted_states=("waiting_approval",),
                icon="mdi:clock-outline",
                color="yellow",
            ),
            {
                "type": "custom:mushroom-template-card",
                "entity": agent_entity_id("active_job_count"),
                "primary": "Aktywny job",
                "secondary": (
                    "{{ states(entity) if states(entity) not in "
                    "['unknown', 'unavailable', 'none', ''] else 'Brak danych' }}"
                ),
                "icon": "mdi:sync",
                "icon_color": (
                    "{{ 'blue' if states(entity) | int(0) > 0 else 'green' }}"
                ),
                "fill_container": True,
            },
            _last_completed_card(resources),
        ],
        columns=2,
    )
    overview_sections = [
        _section(
            _title(
                "Hubinet Ops",
                "Centrum operacyjne · backend jest źródłem prawdy",
            ),
            agent_chips,
            operational_summary,
        )
    ]
    for index, chunk in enumerate(_balanced_groups(items), start=1):
        overview_sections.append(
            _section(
                _title(
                    f"Zasoby · grupa {index}",
                    f"{len(chunk)} zasoby · grupa wyliczona z inventory",
                ),
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
