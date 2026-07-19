from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.time_utils import parse_utc_timestamp


def test_parse_timestamp_normalizes_aware_and_naive_iso_to_utc() -> None:
    assert parse_utc_timestamp("2026-07-19T12:30:00") == datetime(
        2026, 7, 19, 12, 30, tzinfo=UTC
    )
    assert parse_utc_timestamp("2026-07-19T14:30:00+02:00") == datetime(
        2026, 7, 19, 12, 30, tzinfo=UTC
    )
    assert parse_utc_timestamp("2026-07-19T12:30:00Z") == datetime(
        2026, 7, 19, 12, 30, tzinfo=UTC
    )
    parsed = parse_utc_timestamp(
        datetime(2026, 7, 19, 14, 30, tzinfo=timezone(timedelta(hours=2)))
    )
    assert parsed == datetime(2026, 7, 19, 12, 30, tzinfo=UTC)
    assert parsed is not None and parsed.tzinfo is UTC


@pytest.mark.parametrize("value", [None, "", "not-a-timestamp", object()])
def test_parse_timestamp_returns_none_for_malformed_values(value: object) -> None:
    assert parse_utc_timestamp(value) is None
