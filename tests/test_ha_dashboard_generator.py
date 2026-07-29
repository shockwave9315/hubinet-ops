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


def test_ct110_self_update_is_unavailable_without_staged_release() -> None:
    data = _dashboard()
    ct110 = _text(_view(data, "ct-110"))

    assert "Brak przygotowanego wydania" in ct110
    assert "Wymagany jest staged" in ct110
    assert "release na PVE" in ct110
    conditions = _control_conditions(110, load_resources(DEFAULT_CONFIG)[110], "self_update")
    rendered = _text(conditions)
    assert "sensor.hubinet_ops_ct110_self_update_release_version" in rendered
    for unavailable in ("none", "unknown", "unavailable"):
        assert f"state_not: {unavailable}" in rendered


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
    assert "yellow" in status_card["badge_color"]


def test_vm100_has_qemu_metrics_but_no_apt_or_controls() -> None:
    text = _text(_view(_dashboard(), "vm-100"))

    assert "Agent gościa QEMU" in text
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
        "active_plan_id", "active_plan_status", "self_update_release_id",
        "self_update_release_version", "self_update_release_fingerprint",
    ):
        assert f"sensor.hubinet_ops_ct110_{suffix}" in text
    for unsupported in ("cpu_usage", "network_received", "network_sent"):
        assert f"sensor.hubinet_ops_ct110_{unsupported}" not in text
    assert "pending_update_count" not in text
    assert text.count("perform-action") == 15
    assert "script.hubinet_ops_self_update" in text
    assert "script.hubinet_ops_approve_container" in text
    assert "script.hubinet_ops_reject_container" in text
    assert "script.hubinet_ops_scan_container" not in text


def test_all_lxc_views_have_policy_scoped_controls_and_vm100_has_none() -> None:
    data = _dashboard()
    apt_services = {
        "script.hubinet_ops_start_container",
        "script.hubinet_ops_shutdown_container",
        "script.hubinet_ops_reboot_container",
        "script.hubinet_ops_force_stop_container",
        "script.hubinet_ops_refresh_container",
        "script.hubinet_ops_scan_container",
        "script.hubinet_ops_approve_container",
        "script.hubinet_ops_reject_container",
        "script.hubinet_ops_retry_healthcheck",
        "script.hubinet_ops_snapshot_create",
        "script.hubinet_ops_snapshot_restore_latest",
        "script.hubinet_ops_snapshot_delete_latest",
    }
    for vmid in range(101, 110):
        view = _view(data, f"ct-{vmid}")
        services = {
            item["card"]["tap_action"]["perform_action"]
            for item in _walk(view)
            if isinstance(item, dict)
            and item.get("type") == "conditional"
            and (item.get("card") or {}).get("tap_action", {}).get("perform_action")
        }
        assert services == apt_services
        assert _text(view).count("perform-action") == 14
    assert "perform-action" not in _text(_view(data, "vm-100"))
    assert "Tryb obserwacji" in _text(_view(data, "vm-100"))


def test_lxc_control_guards_and_dangerous_confirmations_are_explicit() -> None:
    cfg = load_resources(DEFAULT_CONFIG)[106]
    for action in ("start", "shutdown", "reboot", "force_stop", "scan"):
        conditions = _control_conditions(106, cfg, action)
        entities = {item["entity"] for item in conditions}
        assert "sensor.hubinet_ops_ct106_active_job_id" in entities
        assert "sensor.hubinet_ops_ct106_lifecycle_status" in entities
        assert "sensor.hubinet_ops_ct106_operation_status" in entities
        assert "sensor.hubinet_ops_ct106_runtime_status" in entities
    for action in ("force_stop", "snapshot_rollback", "snapshot_delete"):
        card = _control_card(106, action)
        assert card["color"] == "red"
        confirmation = card["tap_action"]["confirmation"]
        assert confirmation["confirm_text"]
        assert confirmation["dismiss_text"] == "Anuluj"
        assert card["secondary"]
    restore_conditions = {
        item["entity"] for item in _control_conditions(106, cfg, "snapshot_rollback")
    }
    assert "sensor.hubinet_ops_ct106_snapshot_restore_allowed" in restore_conditions


def test_lxc_section_order_keeps_controls_high_and_exposes_snapshots() -> None:
    data = _dashboard()
    for vmid in range(101, 111):
        titles = [_section_title(section) for section in _view(data, f"ct-{vmid}")["sections"]]
        assert titles[1] == "Sterowanie"
        assert "Snapshoty" in titles
    for vmid in range(101, 110):
        titles = [_section_title(section) for section in _view(data, f"ct-{vmid}")["sections"]]
        assert titles[:4] == ["Status", "Sterowanie", "Zasoby", "Aktualizacje"]
        assert "Executor" in titles
        assert titles[-1] == "Pakiety i logi"


def test_verification_is_available_for_every_managed_apt_lxc() -> None:
    data = _dashboard()
    for vmid in range(101, 110):
        text = _text(_view(data, f"ct-{vmid}"))
        assert "Weryfikacja końcowa" in text
        assert "Brak wykonanej weryfikacji aktualizacji" in text
        assert f"sensor.hubinet_ops_ct{vmid}_apt_check" in text
        assert f"sensor.hubinet_ops_ct{vmid}_dpkg_audit" in text
        assert f"sensor.hubinet_ops_ct{vmid}_packages_remaining" in text


def test_live_logs_are_limited_to_ten_and_uptime_is_human_readable() -> None:
    data = _dashboard()
    ct106 = _text(_view(data, "ct-106"))
    assert "events[-10:]" in ct106
    assert "events[-25:]" not in ct106
    assert "timestamp_custom" in ct106 and "%H:%M" in ct106

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


def test_apt_views_have_resources_updates_verification_packages_and_optional_docker() -> None:
    data = _dashboard()
    apt = _text(_view(data, "ct-101"))
    for title in (
        "Zasoby", "Aktualizacje", "Historia i diagnostyka", "Pakiety i logi", "Logi na żywo",
    ):
        assert title in apt
    assert "Weryfikacja końcowa" in apt
    assert "Snapshoty" in apt
    assert "Executor" in apt
    assert "Weryfikacja końcowa" in _text(_view(data, "ct-106"))
    assert "Docker" not in apt
    assert "Docker" in _text(_view(data, "ct-106"))
    assert "Docker" in _text(_view(data, "ct-109"))


def test_dates_are_local_and_plan_job_ids_are_shortened() -> None:
    text = _text(_view(_dashboard(), "ct-106"))
    assert "timestamp_custom" in text
    assert "%d.%m.%Y %H:%M" in text
    assert "%H:%M" in text
    assert "identifier[:8]" in text
    assert "states(entity)[:8]" in text
    assert "Nieprawidłowa data" in text
