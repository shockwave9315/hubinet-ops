from __future__ import annotations

from typing import Any

import yaml

from scripts.generate_ha_dashboard import (
    DEFAULT_CONFIG,
    DEFAULT_OUTPUT,
    _control_card,
    _control_conditions,
    load_resources,
    render,
)


def _dashboard() -> dict:
    return yaml.safe_load(render(DEFAULT_CONFIG))


def _view(data: dict, path: str) -> dict:
    return next(view for view in data["views"] if view["path"] == path)


def _text(value: object) -> str:
    return yaml.safe_dump(value, allow_unicode=True, sort_keys=False)


def _walk(value: Any):
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _section_title(section: dict) -> str:
    title = next(
        card
        for card in section["cards"]
        if card.get("type") == "custom:mushroom-title-card"
    )
    return str(title["title"])


def test_dashboard_generator_is_deterministic_and_checked_in() -> None:
    first = render(DEFAULT_CONFIG)
    second = render(DEFAULT_CONFIG)

    assert first == second
    assert DEFAULT_OUTPUT.read_text(encoding="utf-8") == first


def test_dashboard_contains_full_inventory_and_legacy_paths() -> None:
    data = _dashboard()
    paths = [view["path"] for view in data["views"]]

    assert paths == [
        "overview", "vm-100", "ct-101", "ct-102", "ct-103", "ct-104",
        "ct-105", "ct-106", "ct-107", "ct-108", "ct-109", "ct-110",
    ]
    overview = _text(_view(data, "overview"))
    for label in (
        "VM100 · Home Assistant", "CT101 · Cloudflared", "CT102 · MariaDB",
        "CT103 · MQTT", "CT104 · Nextcloud", "CT105 · AdGuard Home",
        "CT106 · WeatherHub", "CT107 · Immich", "CT108 · DDNS",
        "CT109 · Pompa", "CT110 · Hubinet Ops",
    ):
        assert label in overview


def test_overview_and_details_use_multiple_mushroom_sections() -> None:
    data = _dashboard()
    overview = _view(data, "overview")
    assert len(overview["sections"]) == 4
    assert "custom:mushroom-chips-card" in _text(overview)
    assert _text(overview).count("custom:mushroom-template-card") >= 11

    for view in data["views"][1:]:
        assert len(view["sections"]) >= 5
        assert all(section["type"] == "grid" for section in view["sections"])
        mega_entities = [
            item for item in _walk(view)
            if isinstance(item, dict) and item.get("type") == "entities"
        ]
        assert mega_entities == []


def test_every_conditional_card_has_complete_lovelace_state_conditions() -> None:
    conditional_cards = [
        item
        for item in _walk(_dashboard())
        if isinstance(item, dict) and item.get("type") == "conditional"
    ]

    assert conditional_cards
    for card in conditional_cards:
        conditions = card.get("conditions")
        assert isinstance(conditions, list) and conditions
        for condition in conditions:
            assert condition.get("condition") == "state"
            assert condition.get("entity")
            assert ("state" in condition) != ("state_not" in condition)
            expected_keys = {"condition", "entity"}
            expected_keys.add("state" if "state" in condition else "state_not")
            assert set(condition) == expected_keys


def test_mushroom_chips_use_semantic_colors_without_generic_red_fallback() -> None:
    data = _dashboard()
    overview = _view(data, "overview")
    overview_chips = next(
        item["chips"]
        for item in _walk(overview)
        if isinstance(item, dict) and item.get("type") == "custom:mushroom-chips-card"
    )
    overview_by_entity = {chip["entity"]: chip for chip in overview_chips}
    assert overview_by_entity["sensor.hubinet_ops_agent_version"]["icon_color"] == "blue"
    assert overview_by_entity[
        "sensor.hubinet_ops_agent_configured_resource_count"
    ]["icon_color"] == "blue"
    assert overview_by_entity["sensor.hubinet_ops_agent_last_refresh"]["icon_color"] == "blue-grey"
    assert "relative_time" in overview_by_entity[
        "sensor.hubinet_ops_agent_last_refresh"
    ]["content"]
    active_jobs = overview_by_entity["sensor.hubinet_ops_agent_active_job_count"][
        "icon_color"
    ]
    assert "value == 0" in active_jobs and "green" in active_jobs

    ct101 = _view(data, "ct-101")
    resource_chips = next(
        item["chips"]
        for item in _walk(ct101)
        if isinstance(item, dict) and item.get("type") == "custom:mushroom-chips-card"
    )
    by_entity = {chip["entity"]: chip for chip in resource_chips}
    health_score = by_entity["sensor.hubinet_ops_ct101_health_score"]["icon_color"]
    pending = by_entity[
        "sensor.hubinet_ops_ct101_pending_update_count"
    ]["icon_color"]
    operation = by_entity[
        "sensor.hubinet_ops_ct101_operation_status"
    ]["icon_color"]
    risk = by_entity["sensor.hubinet_ops_ct101_risk"]["icon_color"]
    assert "value >= 90" in health_score and "green" in health_score
    assert "value == 0" in pending and "green" in pending
    assert "['idle', 'success']" in operation and "green" in operation
    assert "['none', 'low']" in risk and "green" in risk

    status_card = next(
        item
        for item in _walk(ct101)
        if isinstance(item, dict)
        and item.get("type") == "custom:mushroom-template-card"
        and item.get("entity") == "sensor.hubinet_ops_ct101_health_status"
        and "badge_color" in item
    )
    assert "degraded" in status_card["badge_color"]
    assert "amber" in status_card["badge_color"]


def test_vm100_has_qemu_metrics_but_no_apt_or_controls() -> None:
    text = _text(_view(_dashboard(), "vm-100"))

    assert "QEMU Guest Agent" in text
    assert "sensor.hubinet_ops_vm100_guest_agent" in text
    assert "sensor.hubinet_ops_vm100_ip_addresses" in text
    assert "pending_update_count" not in text
    assert "Pakiety APT" not in text
    assert "perform-action" not in text
    assert "Tryb obserwacji" in text


def test_ct110_has_only_supported_self_metrics() -> None:
    text = _text(_view(_dashboard(), "ct-110"))

    for suffix in (
        "service_status", "api_health", "agent_version", "cpu_load_1m",
        "cpu_cores", "memory_used", "memory_total", "memory_available",
        "disk_used", "disk_total", "disk_free", "recent_warnings",
    ):
        assert f"sensor.hubinet_ops_ct110_{suffix}" in text
    for unsupported in ("cpu_usage", "network_received", "network_sent"):
        assert f"sensor.hubinet_ops_ct110_{unsupported}" not in text
    assert "pending_update_count" not in text
    assert "perform-action" not in text


def test_ct106_is_only_view_with_capability_control_cards() -> None:
    data = _dashboard()
    ct106_view = _view(data, "ct-106")
    ct106 = _text(ct106_view)

    for name in (
        "Uruchom", "Wyłącz łagodnie", "Uruchom ponownie", "Odśwież stan",
        "Sprawdź aktualizacje", "Zatwierdź plan", "Odrzuć plan",
        "Ponów healthcheck",
    ):
        assert name in ct106
    assert "primary: Rollback" not in ct106
    assert "state_not: waiting_approval" in ct106
    assert "confirmation:" in ct106
    assert ct106.count("perform-action") == 8

    controls = {}
    for item in _walk(ct106_view):
        if not isinstance(item, dict) or item.get("type") != "conditional":
            continue
        card = item.get("card") or {}
        service = (card.get("tap_action") or {}).get("perform_action")
        if service:
            controls[service] = item
    expected_services = {
        "script.hubinet_ops_start_container",
        "script.hubinet_ops_shutdown_container",
        "script.hubinet_ops_reboot_container",
        "script.hubinet_ops_refresh_container",
        "script.hubinet_ops_scan_container",
        "script.hubinet_ops_approve_container",
        "script.hubinet_ops_reject_container",
        "script.hubinet_ops_retry_healthcheck",
    }
    assert set(controls) == expected_services
    assert "script.hubinet_ops_rollback" not in controls

    def signatures(service: str) -> set[tuple[str, str, str]]:
        return {
            (
                condition["entity"],
                "state" if "state" in condition else "state_not",
                condition.get("state", condition.get("state_not")),
            )
            for condition in controls[service]["conditions"]
        }

    common_idle = {
        ("sensor.hubinet_ops_ct106_active_job_id", "state", "none"),
        ("sensor.hubinet_ops_ct106_lifecycle_status", "state_not", "running"),
    }
    for action in ("start", "shutdown", "reboot"):
        service = f"script.hubinet_ops_{action}_container"
        expected = common_idle | {
            (f"sensor.hubinet_ops_ct106_capability_{action}", "state", "allowed"),
            (
                "sensor.hubinet_ops_ct106_lxc_status",
                "state",
                "stopped" if action == "start" else "running",
            ),
            ("sensor.hubinet_ops_ct106_operation_status", "state_not", "waiting_approval"),
            ("sensor.hubinet_ops_ct106_operation_status", "state_not", "running"),
        }
        assert signatures(service) == expected

    assert signatures("script.hubinet_ops_scan_container") == common_idle | {
        ("sensor.hubinet_ops_ct106_lxc_status", "state", "running"),
        ("sensor.hubinet_ops_ct106_capability_scan", "state", "allowed"),
        ("sensor.hubinet_ops_ct106_operation_status", "state_not", "running"),
    }
    assert signatures("script.hubinet_ops_refresh_container") == {
        ("sensor.hubinet_ops_ct106_capability_refresh", "state", "allowed")
    }
    for action in ("approve", "reject"):
        assert signatures(f"script.hubinet_ops_{action}_container") == {
            ("sensor.hubinet_ops_ct106_operation_status", "state", "waiting_approval"),
            (f"sensor.hubinet_ops_ct106_capability_{action}", "state", "allowed"),
        }
    assert signatures("script.hubinet_ops_retry_healthcheck") == common_idle | {
        ("sensor.hubinet_ops_ct106_health_status", "state", "critical"),
        (
            "sensor.hubinet_ops_ct106_capability_retry_healthcheck",
            "state",
            "allowed",
        ),
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
    }
    service_by_action = {
        action: f"script.hubinet_ops_{action}_container" for action in colors
    }
    service_by_action["retry_healthcheck"] = "script.hubinet_ops_retry_healthcheck"
    for action, color in colors.items():
        card = controls[service_by_action[action]]["card"]
        assert card["secondary"]
        assert card["color"] == color
        assert card["tap_action"]["confirmation"]["title"]
        assert card["tap_action"]["confirmation"]["text"]
    for action in ("start", "shutdown", "reboot", "approve", "reject"):
        confirmation = controls[f"script.hubinet_ops_{action}_container"]["card"][
            "tap_action"
        ]["confirmation"]
        assert confirmation["confirm_text"]
        assert confirmation["dismiss_text"] == "Anuluj"

    for path in (
        "vm-100", "ct-101", "ct-102", "ct-103", "ct-104", "ct-105",
        "ct-107", "ct-108", "ct-109", "ct-110",
    ):
        view_text = _text(_view(data, path))
        assert "perform-action" not in view_text
        assert "Tryb obserwacji — sterowanie zablokowane przez politykę backendu" in view_text


def test_ct106_section_order_and_observation_cards_are_high() -> None:
    data = _dashboard()
    ct106 = _view(data, "ct-106")
    assert [_section_title(section) for section in ct106["sections"]] == [
        "Status",
        "Sterowanie",
        "Zasoby",
        "Aktualizacje",
        "Weryfikacja końcowa",
        "Docker",
        "Historia i diagnostyka",
        "Recovery scan",
        "Pakiety i logi",
    ]

    for path in (
        "ct-101", "ct-102", "ct-103", "ct-104", "ct-105",
        "ct-107", "ct-108", "ct-109",
    ):
        titles = [_section_title(section) for section in _view(data, path)["sections"]]
        assert titles[1] == "Sterowanie"
        assert "Weryfikacja końcowa" not in titles
        assert titles[-1] == "Pakiety i logi"


def test_verification_is_ct106_only_and_unknown_has_clear_message() -> None:
    data = _dashboard()
    apt_paths = [f"ct-{vmid}" for vmid in range(101, 110)]
    for path in apt_paths:
        text = _text(_view(data, path))
        if path == "ct-106":
            assert "Weryfikacja końcowa" in text
            assert "Brak wykonanej weryfikacji aktualizacji" in text
            assert "sensor.hubinet_ops_ct106_apt_check" in text
            assert "sensor.hubinet_ops_ct106_dpkg_audit" in text
            assert "sensor.hubinet_ops_ct106_packages_remaining" in text
        else:
            assert "Weryfikacja końcowa" not in text


def test_live_logs_are_limited_to_ten_and_uptime_is_human_readable() -> None:
    data = _dashboard()
    ct106 = _text(_view(data, "ct-106"))
    assert "events[-10:]" in ct106
    assert "events[-25:]" not in ct106

    for path in ("vm-100", "ct-101", "ct-110"):
        uptime = next(
            item
            for item in _walk(_view(data, path))
            if isinstance(item, dict)
            and item.get("type") == "custom:mushroom-template-card"
            and item.get("primary") == "Uptime"
        )
        assert "seconds // 86400" in uptime["secondary"]
        assert "{{ days }} d {{ hours }} h {{ minutes }} min" in uptime["secondary"]


def test_future_rollback_control_remains_fully_guarded() -> None:
    cfg = load_resources(DEFAULT_CONFIG)[106]
    conditions = _control_conditions(106, cfg, "rollback")
    signatures = {
        (
            condition["entity"],
            "state" if "state" in condition else "state_not",
            condition.get("state", condition.get("state_not")),
        )
        for condition in conditions
    }
    assert signatures == {
        ("sensor.hubinet_ops_ct106_operation_status", "state", "manual_intervention"),
        ("sensor.hubinet_ops_ct106_rollback_allowed", "state", "allowed"),
        ("sensor.hubinet_ops_ct106_capability_rollback", "state", "allowed"),
        ("sensor.hubinet_ops_ct106_active_job_id", "state", "none"),
        ("sensor.hubinet_ops_ct106_lifecycle_status", "state_not", "running"),
    }
    card = _control_card(106, "rollback")
    assert card["color"] == "red"
    assert card["secondary"]
    assert card["tap_action"]["perform_action"] == "script.hubinet_ops_rollback"
    assert card["tap_action"]["data"] == {"vmid": 106}
    assert card["tap_action"]["confirmation"]["confirm_text"] == "Przywróć"
    assert card["tap_action"]["confirmation"]["dismiss_text"] == "Anuluj"


def test_apt_views_have_resources_updates_verification_packages_and_optional_docker() -> None:
    data = _dashboard()
    apt = _text(_view(data, "ct-101"))
    for title in (
        "Zasoby", "Aktualizacje", "Historia i diagnostyka", "Pakiety i logi", "Logi live",
    ):
        assert title in apt
    assert "Weryfikacja końcowa" not in apt
    assert "Weryfikacja końcowa" in _text(_view(data, "ct-106"))
    assert "Docker" not in apt
    assert "Docker" in _text(_view(data, "ct-106"))
    assert "Docker" in _text(_view(data, "ct-109"))
