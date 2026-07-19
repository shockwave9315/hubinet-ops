from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


def parse_utc_timestamp(value: Any) -> datetime | None:
    """Parse an ISO timestamp into aware UTC, returning None for malformed input."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except (TypeError, ValueError, OverflowError):
            return None
    else:
        return None

    try:
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except (TypeError, ValueError, OverflowError):
        return None
