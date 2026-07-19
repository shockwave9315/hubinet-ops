from __future__ import annotations

from pathlib import Path

import yaml

from scripts.generate_ha_dashboard import DEFAULT_CONFIG, DEFAULT_OUTPUT, render


def _dashboard() -> dict:
    return yaml.safe_load(render(DEFAULT_CONFIG))


def _view(data: dict, path: str) -> dict:
    return next(view for view in data["views"] if view["path"] == path)


def _text(value: object) -> str:
    return yaml.safe_dump(value, allow_unicode=True, sort_keys=False)


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


def test_vm100_has_guest_agent_but_no_apt_or_lifecycle() -> None:
    text = _text(_view(_dashboard(), "vm-100"))

    assert "QEMU Guest Agent" in text
    assert "guest_agent_status" in text
    assert "APT i weryfikacja" not in text
    assert "perform-action" not in text
    assert "Tryb obserwacji" in text


def test_ct106_is_only_view_with_controls_and_start_hides_for_waiting_plan() -> None:
    data = _dashboard()
    ct106 = _text(_view(data, "ct-106"))

    for name in (
        "Uruchom", "Wyłącz łagodnie", "Uruchom ponownie", "Odśwież stan",
        "Sprawdź aktualizacje", "Zatwierdź plan", "Odrzuć plan",
        "Ponów healthcheck",
    ):
        assert name in ct106
    assert "name: Rollback" not in ct106
    assert "state_not: waiting_approval" in ct106
    assert "confirmation:" in ct106
    for path in (
        "vm-100", "ct-101", "ct-102", "ct-103", "ct-104", "ct-105",
        "ct-107", "ct-108", "ct-109", "ct-110",
    ):
        view_text = _text(_view(data, path))
        assert "perform-action" not in view_text
        assert "Tryb obserwacji — sterowanie zablokowane przez politykę backendu" in view_text


def test_adapter_specific_cards_are_generated() -> None:
    data = _dashboard()

    assert "Docker" in _text(_view(data, "ct-109"))
    assert "Weryfikacja końcowa" in _text(_view(data, "ct-101"))
    self_text = _text(_view(data, "ct-110"))
    assert "Self-health agenta" in self_text
    assert "configured_resource_count" in self_text
    assert "APT i weryfikacja" not in self_text
