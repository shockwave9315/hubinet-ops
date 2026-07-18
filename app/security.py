from __future__ import annotations

import json
import re
from typing import Any

MAX_ERROR_LENGTH = 2000
MAX_LOG_LENGTH = 1000

_SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*)(?:bearer\s+)?[^\s,;]+"),
    re.compile(r"(?i)((?:api[_-]?token|mqtt[_-]?password|password|webhook[_-]?id)\s*[:=]\s*)[^\s,;]+"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+"),
    re.compile(r"https?://[^\s/]+/api/webhook/[^\s]+", re.IGNORECASE),
)


def sanitize_text(value: Any, *, limit: int = MAX_LOG_LENGTH) -> str:
    text = str(value or "").replace("\x00", "")
    for pattern in _SECRET_PATTERNS:
        replacement = r"\1[REDACTED]" if pattern.groups else "[REDACTED]"
        text = pattern.sub(replacement, text)
    return text[: max(0, limit)]


def sanitize_data(value: Any, *, depth: int = 0) -> Any:
    if depth > 8:
        return "[TRUNCATED]"
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for raw_key, item in list(value.items())[:200]:
            key = str(raw_key)[:100]
            lowered = key.lower().replace("-", "_")
            if lowered in {
                "authorization",
                "api_token",
                "token",
                "password",
                "mqtt_password",
                "private_key",
                "webhook_id",
            }:
                result[key] = "[REDACTED]"
            else:
                result[key] = sanitize_data(item, depth=depth + 1)
        return result
    if isinstance(value, list):
        return [sanitize_data(item, depth=depth + 1) for item in value[:200]]
    if isinstance(value, str):
        return sanitize_text(value, limit=MAX_ERROR_LENGTH)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return sanitize_text(value, limit=MAX_ERROR_LENGTH)


def bounded_json(value: Any, *, limit: int = 32_000) -> str:
    encoded = json.dumps(sanitize_data(value), ensure_ascii=False, separators=(",", ":"))
    if len(encoded) <= limit:
        return encoded
    return json.dumps({"truncated": True, "preview": encoded[: limit - 40]}, ensure_ascii=False)
