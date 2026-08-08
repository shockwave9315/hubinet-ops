"""Diagnostics for Hubinet Ops.

The redaction shape follows Home Assistant Core's Apache-2.0 ``proxmoxve``
diagnostics and is expanded for bearer/header safety.
"""

from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Any

from homeassistant.components.diagnostics import REDACTED
from homeassistant.core import HomeAssistant

from .api import HubinetOpsSnapshot
from .const import CONF_API_TOKEN, CONF_BASE_URL
from .coordinator import HubinetOpsConfigEntry

_KEY_SEPARATOR = re.compile(r"[^a-z0-9]+")

_REPOSITORY_SECRET_KEYS = frozenset(
    {
        CONF_API_TOKEN,
        CONF_BASE_URL,
        "api_key",
        "authorization",
        "authorization_header",
        "credential",
        "credentials",
        "headers",
        "hubinet_ops_ha_ssh_key",
        "passphrase",
        "password",
        "private_key",
        "secret",
        "ssh_key",
        "ssh_private_key",
        "token",
        "token_value",
        "webhook_id",
        "webhook_url",
    }
)

_REPOSITORY_SECRET_SUFFIXES = (
    "_api_key",
    "_authorization",
    "_credentials",
    "_passphrase",
    "_password",
    "_private_key",
    "_secret",
    "_token",
    "_webhook_id",
)


def _normalize_diagnostic_key(key: Any) -> str:
    """Normalize case and separators without changing the published key."""

    return _KEY_SEPARATOR.sub("_", str(key).casefold()).strip("_")


def _is_repository_secret_key(key: Any) -> bool:
    """Return whether a normalized key carries repository-sensitive data."""

    normalized = _normalize_diagnostic_key(key)
    return (
        normalized in _REPOSITORY_SECRET_KEYS
        or normalized.endswith(_REPOSITORY_SECRET_SUFFIXES)
        or (
            normalized.startswith("hubinet_ops_")
            and normalized.endswith("_url")
        )
    )


def _redact_repository_secrets(value: Any) -> Any:
    """Recursively redact secret fields while retaining diagnostic structure."""

    if isinstance(value, Mapping):
        return {
            key: (
                REDACTED
                if _is_repository_secret_key(key)
                else _redact_repository_secrets(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact_repository_secrets(item) for item in value]
    return value


def _diagnostic_value(value: Any) -> Any:
    """Convert frozen snapshot values to redaction-friendly JSON containers."""

    if isinstance(value, Mapping):
        return {key: _diagnostic_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_diagnostic_value(item) for item in value]
    return value


def _snapshot_diagnostics(snapshot: HubinetOpsSnapshot) -> dict[str, Any]:
    return {
        "backend": {
            "instance_id": snapshot.backend.instance_id,
            "name": snapshot.backend.name,
            "version": snapshot.backend.version,
            "api_version": snapshot.backend.api_version,
        },
        "generated_at": snapshot.generated_at,
        "nodes": [
            {
                "node_id": node.node_id,
                "name": node.name,
                "status": node.status,
                "available": node.available,
                "facts": _diagnostic_value(node.facts),
            }
            for node in snapshot.nodes
        ],
        "resources": [
            {
                "identity": {
                    "instance_id": resource.identity.instance_id,
                    "resource_type": resource.identity.resource_type.value,
                    "vmid": resource.identity.vmid,
                },
                "name": resource.name,
                "node_id": resource.node_id,
                "last_known_node_id": resource.last_known_node_id,
                "status": resource.status,
                "state_level": resource.state_level.value,
                "policy": _diagnostic_value(resource.policy),
                "capabilities": sorted(resource.capabilities),
                "available": resource.available,
                "presence": resource.presence.value,
                "state": _diagnostic_value(resource.state),
            }
            for resource in snapshot.resources
        ],
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, config_entry: HubinetOpsConfigEntry
) -> dict[str, Any]:
    """Return credentials-redacted config and current backend snapshot."""

    diagnostics = {
        "config_entry": config_entry.as_dict(),
        "snapshot": _snapshot_diagnostics(config_entry.runtime_data.data),
    }
    return _redact_repository_secrets(diagnostics)
