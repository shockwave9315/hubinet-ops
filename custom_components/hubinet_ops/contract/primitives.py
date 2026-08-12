"""Primitive validators and immutable JSON-like snapshot helpers."""

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Any
from uuid import UUID


def _deep_freeze(value: Any) -> Any:
    """Recursively freeze one JSON-like backend snapshot value."""

    if value is None or type(value) in {str, int, float, bool}:
        return value
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("snapshot mapping keys must be strings")
        return MappingProxyType(
            {key: _deep_freeze(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_deep_freeze(item) for item in value)
    raise TypeError(
        f"snapshot values must be JSON-like, got {type(value).__name__}"
    )


def _immutable_mapping(value: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """Return a recursively immutable copy of a JSON-like mapping."""

    frozen = _deep_freeze(value or {})
    assert isinstance(frozen, Mapping)
    return frozen


def _require_text(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must not be empty")


def _require_enum_instance(
    value: object, enum_type: type[StrEnum], field_name: str
) -> None:
    """Require one canonical enum member without normalizing wire values."""

    if not isinstance(value, enum_type):
        raise ValueError(
            f"{field_name} must be a canonical {enum_type.__name__} member"
        )


def _require_uuid_identity(value: str, field_name: str) -> None:
    """Require one canonical non-NIL UUID identity/reference."""

    _require_text(value, field_name)
    try:
        parsed = UUID(value)
    except (AttributeError, TypeError, ValueError):
        raise ValueError(f"{field_name} must be a canonical UUID") from None
    if parsed.int == 0:
        raise ValueError(f"{field_name} must not be the NIL UUID")
    if value != str(parsed):
        raise ValueError(
            f"{field_name} must use canonical lower-case hyphenated UUID text"
        )


def _require_positive(value: int, field_name: str) -> None:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer")


def _require_optional_positive(value: int | None, field_name: str) -> None:
    if value is not None:
        _require_positive(value, field_name)
