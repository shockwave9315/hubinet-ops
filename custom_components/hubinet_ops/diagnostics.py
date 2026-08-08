"""Diagnostics for Hubinet Ops.

The redaction shape follows Home Assistant Core's Apache-2.0 ``proxmoxve``
diagnostics and is expanded for bearer/header safety.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .api import HubinetOpsSnapshot
from .const import CONF_API_TOKEN, CONF_BASE_URL
from .coordinator import HubinetOpsConfigEntry

TO_REDACT = {
    CONF_API_TOKEN,
    CONF_BASE_URL,
    "authorization",
    "Authorization",
    "bearer_token",
    "credentials",
    "headers",
    "password",
    "token",
}


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
    return async_redact_data(diagnostics, TO_REDACT)
