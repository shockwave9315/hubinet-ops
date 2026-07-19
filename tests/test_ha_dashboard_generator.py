from __future__ import annotations

from typing import Any

import yaml

from scripts.generate_ha_dashboard import DEFAULT_CONFIG, DEFAULT_OUTPUT, render


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
    ct106 = _text(_view(data, "ct-106"))

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

    for path in (
        "vm-100", "ct-101", "ct-102", "ct-103", "ct-104", "ct-105",
        "ct-107", "ct-108", "ct-109", "ct-110",
    ):
        view_text = _text(_view(data, path))
        assert "perform-action" not in view_text
        assert "Tryb obserwacji — sterowanie zablokowane przez politykę backendu" in view_text


def test_apt_views_have_resources_updates_verification_packages_and_optional_docker() -> None:
    data = _dashboard()
    apt = _text(_view(data, "ct-101"))
    for title in (
        "Zasoby", "Aktualizacje", "Weryfikacja końcowa",
        "Historia i diagnostyka", "Pakiety i diagnostyka", "Logi live",
    ):
        assert title in apt
    assert "Docker" not in apt
    assert "Docker" in _text(_view(data, "ct-106"))
    assert "Docker" in _text(_view(data, "ct-109"))
