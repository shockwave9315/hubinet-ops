from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import yaml

from app.mqtt_budget import bounded_attributes
from app.state import normalize_state
from scripts.generate_ha_dashboard import (
    DEFAULT_CONFIG,
    MISSING_STATES,
    PL_UI_TRANSLATIONS,
    _bytes_card,
    _bytes_summary_card,
    build_dashboard,
    load_resources,
    render,
)
from tests.test_host_control import HostController, policy


def _dashboard() -> dict[str, Any]:
    return yaml.safe_load(render(DEFAULT_CONFIG))


def _view(data: dict[str, Any], path: str) -> dict[str, Any]:
    return next(item for item in data["views"] if item["path"] == path)


def _walk(value: Any):
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _text(value: Any) -> str:
    return yaml.safe_dump(value, allow_unicode=True, sort_keys=False)


def test_overview_is_an_operational_center_with_balanced_inventory_groups() -> None:
    data = _dashboard()
    overview = _view(data, "overview")
    text = _text(overview)

    for title in (
        "Zasoby online",
        "Wymagają aktualizacji",
        "Błędy",
        "Oczekują na zatwierdzenie",
        "Aktywny job",
        "Ostatnio zakończona operacja",
    ):
        assert title in text
    summary = next(
        item
        for item in _walk(overview)
        if isinstance(item, dict)
        and item.get("type") == "grid"
        and any(
            card.get("primary") == "Zasoby online"
            for card in item.get("cards", [])
        )
    )
    assert summary["columns"] == 2
    assert len(summary["cards"]) == 6
    resource_groups = [
        section
        for section in overview["sections"]
        if "Zasoby · grupa" in _text(section)
    ]
    assert [len(section["cards"]) - 1 for section in resource_groups] == [4, 4, 3]
    assert "VM100–CT103" not in text
    assert "CT104–CT107" not in text
    assert "CT108–CT110" not in text


def test_balancing_is_derived_from_supplied_inventory_not_vmid_boundaries() -> None:
    resources = load_resources(DEFAULT_CONFIG)
    selected = {
        vmid: resources[source]
        for vmid, source in zip((12, 44, 301, 700, 999), (101, 102, 103, 104, 105))
    }
    for vmid, cfg in selected.items():
        cfg = dict(cfg)
        cfg["dashboard_path"] = f"/hubinet-ops/ct-{vmid}"
        selected[vmid] = cfg

    overview = build_dashboard(selected)["views"][0]
    groups = [
        section
        for section in overview["sections"]
        if "Zasoby · grupa" in _text(section)
    ]

    assert [len(section["cards"]) - 1 for section in groups] == [3, 2]


def test_final_verification_is_one_readable_summary_not_six_microtiles() -> None:
    view = _view(_dashboard(), "ct-105")
    verification_section = next(
        section
        for section in view["sections"]
        if "Weryfikacja końcowa" in _text(section)
    )
    text = _text(verification_section)

    assert "APT:" in text
    assert "dpkg:" in text
    assert "Ostatnia weryfikacja:" in text
    assert text.count("Weryfikacja końcowa ·") == 1
    assert "columns: 6" not in text
    assert "Brak danych" in text


def test_central_translation_layer_covers_known_operator_enums() -> None:
    expected = {
        "healthy": "sprawny",
        "running": "uruchomiony",
        "stopped": "zatrzymany",
        "update_available": "dostępna aktualizacja",
        "waiting_approval": "oczekuje na zatwierdzenie",
        "success": "zakończono pomyślnie",
        "failed": "niepowodzenie",
        "blocked": "zablokowane",
        "compatible": "zgodny",
        "incompatible": "niezgodny",
        "unknown": "Brak danych",
        "high": "wysoki",
    }
    assert {key: PL_UI_TRANSLATIONS[key] for key in expected} == expected

    visible_keys = {"primary", "secondary", "title", "subtitle", "name", "content"}
    raw_enums = set(expected) | {"up_to_date", "idle", "allowed", "valid", "none"}
    for item in _walk(_dashboard()):
        if not isinstance(item, dict):
            continue
        for key in visible_keys:
            value = item.get(key)
            if not isinstance(value, str) or "{{" in value or "{%" in value:
                continue
            words = set(value.replace("·", " ").replace(":", " ").split())
            assert not (words & raw_enums), (key, value)


def test_semantic_icons_and_colors_match_update_error_approval_and_warning() -> None:
    ct105 = _text(_view(_dashboard(), "ct-105"))

    assert "mdi:update" in ct105 and "orange" in ct105
    assert "waiting_approval" in ct105 and "yellow" in ct105
    assert "mdi:sync" in ct105 and "blue" in ct105
    assert "incompatible" in ct105 and "red" in ct105
    assert "insufficient_health_contract" in ct105 and "yellow" in ct105
    status = next(
        item
        for item in _walk(_view(_dashboard(), "ct-105"))
        if isinstance(item, dict) and "badge_icon" in item
    )
    assert status["badge_icon"].index("mdi:update") < status["badge_icon"].index(
        "mdi:check-circle"
    )


def test_adaptive_byte_helpers_show_binary_units_and_never_zero_for_unknown() -> None:
    byte_card = _bytes_card("sensor.bytes", "Dane", "mdi:database")
    summary = _bytes_summary_card(
        name="RAM",
        used="sensor.used",
        total="sensor.total",
        free="sensor.free",
        icon="mdi:memory",
    )
    text = _text([byte_card, summary])

    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        assert unit in text
    assert "Brak danych" in text
    missing_branch = text.split("Brak danych", 1)[0]
    assert "0 B" not in missing_branch


def test_vm100_zero_disk_usage_from_proxmox_is_canonical_unknown(
    tmp_path: Path,
) -> None:
    class QemuRunner:
        def __call__(
            self,
            argv: list[str],
            **_: Any,
        ) -> subprocess.CompletedProcess[str]:
            if argv[:2] == ["qm", "status"]:
                return subprocess.CompletedProcess(argv, 0, "status: running\n", "")
            if argv[:2] == ["pvesh", "get"] and "/status/current" in argv[2]:
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    json.dumps(
                        {
                            "name": "home-assistant",
                            "uptime": 100,
                            "cpus": 2,
                            "mem": 1024,
                            "maxmem": 2048,
                            "disk": 0,
                            "maxdisk": 4096,
                            "netin": 12,
                            "netout": 34,
                        }
                    ),
                    "",
                )
            if argv[:3] == ["pvesh", "get", "/cluster/resources"]:
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    json.dumps([{"vmid": 100, "cpu": 0.1}]),
                    "",
                )
            return subprocess.CompletedProcess(argv, 0, "", "")

    result = HostController(policy(tmp_path), runner=QemuRunner()).execute(
        "inspect", 100
    )
    state = normalize_state(
        {
            **result,
            "resource_type": "qemu",
            "adapter": "haos",
        }
    )

    assert result["disk"]["used_bytes"] is None
    assert result["disk"]["usage_known"] is False
    assert state["disk"]["used_bytes"] is None


def test_ct108_insufficient_contract_is_yellow_and_keeps_safety_message() -> None:
    view = _view(_dashboard(), "ct-108")
    text = _text(view)
    warning = next(
        item
        for item in _walk(view)
        if isinstance(item, dict)
        and item.get("primary") == "Ograniczona weryfikacja"
    )

    assert "Ograniczona weryfikacja" in text
    assert (
        "Hubinet Ops sprawdza stan systemu, ale nie ma kompletnego testu aplikacji"
        in warning["secondary"]
    )
    assert (
        "Automatyczny rollback po awarii aplikacji jest wyłączony"
        in warning["secondary"]
    )
    assert warning["icon_color"] == "yellow"
    assert "profile_validation_status" in text


def test_snapshot_list_is_bounded_and_safe_buttons_are_present() -> None:
    text = _text(_view(_dashboard(), "ct-106"))

    assert "managed_snapshots" in text
    assert "snapshots[:5]" in text
    assert "Przed aktualizacją" in text
    assert "chroniony" in text and "niechroniony" in text
    assert "Usuń najstarszy" in text
    assert "Usuń niechronione" in text
    assert "script.hubinet_ops_snapshot_delete_oldest" in text
    assert "script.hubinet_ops_snapshot_delete_unprotected" in text


def test_snapshot_attribute_preview_is_bounded() -> None:
    snapshots = [
        {
            "physical_name": f"hubinet-ops-106-manual-202607{i:02d}T120000Z",
            "logical_type": "manual",
            "vmid": 106,
            "created_at": "2026-07-29T12:00:00+00:00",
            "age_seconds": i,
            "protected": i == 0,
            "protection_reason": "active_job" if i == 0 else None,
            "owned_by_hubinet_ops": True,
        }
        for i in range(20)
    ]

    attributes = bounded_attributes({"managed_snapshots": snapshots})

    assert len(attributes["managed_snapshots"]) == 10
    assert attributes["managed_snapshots"][0]["protected"] is True
    assert all(item["owned_by_hubinet_ops"] for item in attributes["managed_snapshots"])
    assert all(state in ("unknown", "unavailable", "none", "None", "") for state in MISSING_STATES)
