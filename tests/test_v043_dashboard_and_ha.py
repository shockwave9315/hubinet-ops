from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

import yaml
from jinja2 import Environment, TemplateSyntaxError

from scripts.generate_ha_dashboard import (
    DEFAULT_CONFIG,
    _state_label,
    render,
)


ROOT = Path(__file__).parents[1]
PACKAGE = ROOT / "home-assistant" / "packages" / "hubinet_ops.yaml"


def _walk(value: Any, path: str = "root") -> Iterator[tuple[str, Any]]:
    yield path, value
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _walk(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk(child, f"{path}[{index}]")


def _assert_valid_ha_template(value: str, path: str) -> None:
    environment = Environment(autoescape=False)
    try:
        environment.parse(value)
        tokens = list(environment.lex(value))
    except TemplateSyntaxError as exc:
        raise AssertionError(f"invalid Jinja at {path}: {exc}") from exc
    leaked = [
        token_value
        for _line, token_type, token_value in tokens
        if token_type == "data"
        and any(marker in token_value for marker in ("{{", "}}", "{%", "%}"))
    ]
    assert leaked == [], f"literal Jinja delimiter remains at {path}: {leaked!r}"


def test_every_generated_dashboard_template_parses_without_literal_closing_braces() -> None:
    dashboard = yaml.safe_load(render(DEFAULT_CONFIG))
    checked: list[str] = []

    for path, value in _walk(dashboard):
        if isinstance(value, str) and any(
            marker in value for marker in ("{{", "}}", "{%", "%}")
        ):
            _assert_valid_ha_template(value, path)
            checked.append(path)

    assert checked
    view_paths = {view["path"] for view in dashboard["views"]}
    assert {"vm-100", *(f"ct-{vmid}" for vmid in range(101, 111))} <= view_paths


def test_exact_production_state_label_regression_has_no_literal_double_closing() -> None:
    template = _state_label()

    _assert_valid_ha_template(template, "_state_label")
    assert "}}}}" not in template


def test_ha_result_handler_distinguishes_business_outcomes_and_mapping_details() -> None:
    package = PACKAGE.read_text(encoding="utf-8")
    report_start = package.index("  hubinet_ops_report_action:")
    report = package[
        report_start : package.index("  hubinet_ops_start_container:", report_start)
    ]

    for status in (
        "nothing_to_delete",
        "nothing_to_do",
        "up_to_date",
        "update_available",
        "no_release_published",
    ):
        assert status in report
    assert "detail is mapping" in report
    assert "Brak niechronionych snapshotów do usunięcia" in report
    assert "Usługa Hubinet Ops jest niedostępna" in report
    assert "Błąd sieci" in report
    assert "result.get('status', 0) in [200, 202]" not in report


def test_ct110_dashboard_separates_debian_system_from_application_release() -> None:
    dashboard = yaml.safe_load(render(DEFAULT_CONFIG))
    ct110 = next(view for view in dashboard["views"] if view["path"] == "ct-110")
    rendered = yaml.safe_dump(ct110, allow_unicode=True, sort_keys=False)

    assert "System Debian CT110" in rendered
    assert "Aplikacja Hubinet Ops" in rendered
    assert "Skanuj aktualizacje systemu CT110" in rendered
    assert "Zatwierdź aktualizację systemu" in rendered
    assert "Sprawdź nowe wydanie Hubinet Ops" in rendered
    assert "Zainstaluj wydanie Hubinet Ops" in rendered
