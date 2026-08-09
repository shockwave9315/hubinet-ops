"""Credentials-redacted diagnostics for the published Hubinet Ops view."""

from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Any

from homeassistant.components.diagnostics import REDACTED
from homeassistant.core import HomeAssistant

from .api import HubinetOpsSnapshot, SourceContext
from .const import CONF_API_TOKEN, CONF_BASE_URL
from .coordinator import HubinetOpsConfigEntry

_ACRONYM_WORD_BOUNDARY = re.compile(r"(?<=[A-Z])(?=[A-Z][a-z])")
_CAMEL_WORD_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_KEY_SEPARATOR = re.compile(r"[^a-z0-9]+")

_REPOSITORY_SECRET_KEYS = frozenset(
    {
        CONF_API_TOKEN,
        CONF_BASE_URL,
        "api_key",
        "authorization",
        "authorization_header",
        "canonical_transport_locator",
        "credential",
        "credentials",
        "headers",
        "hubinet_ops_ha_ssh_key",
        "passphrase",
        "password",
        "private_key",
        "secret",
        "secrets",
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
    """Normalize word boundaries and separators without changing the published key."""

    normalized = _ACRONYM_WORD_BOUNDARY.sub("_", str(key))
    normalized = _CAMEL_WORD_BOUNDARY.sub("_", normalized)
    return _KEY_SEPARATOR.sub("_", normalized.casefold()).strip("_")


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


def _context_diagnostics(context: SourceContext | None) -> dict[str, Any] | None:
    if context is None:
        return None
    return {
        "source_config_revision": context.source_config_revision,
        "endpoint_id": context.endpoint_id,
        "canonical_transport_locator": context.canonical_transport_locator,
        "canonicalization_contract_version": (
            context.canonicalization_contract_version
        ),
        "transport_trust_revision": context.transport_trust_revision,
    }


def _snapshot_diagnostics(snapshot: HubinetOpsSnapshot) -> dict[str, Any]:
    return {
        "backend": {
            "backend_instance_id": snapshot.backend.backend_instance_id,
            "name": snapshot.backend.name,
            "version": snapshot.backend.version,
            "api_version": snapshot.backend.api_version,
        },
        "inventory_revision": snapshot.inventory_revision,
        "published_state_revision": snapshot.published_state_revision,
        "published_at": snapshot.published_at,
        "sources": [
            {
                "inventory_source_id": source.inventory_source_id,
                "name": source.name,
                "provider_kind": source.provider_kind,
                "health": source.health.value,
                "freshness": source.freshness.value,
                "health_origin": source.health_origin.value,
                "health_reason": source.health_reason,
                "last_issued_run_sequence": source.last_issued_run_sequence,
                "latest_completed_run_sequence": (
                    source.latest_completed_run_sequence
                ),
                "latest_completed_outcome": source.latest_completed_outcome,
                "last_health_run_sequence": source.last_health_run_sequence,
                "last_run_health_outcome": source.last_run_health_outcome,
                "last_committed_run_sequence": source.last_committed_run_sequence,
                "last_successful_observed_at": (
                    source.last_successful_observed_at
                ),
                "freshness_reference_at": source.freshness_reference_at,
                "freshness_valid_until": source.freshness_valid_until,
                "current_context": _context_diagnostics(source.current_context),
                "committed_context": _context_diagnostics(source.committed_context),
                "facts": _diagnostic_value(source.facts),
            }
            for source in snapshot.sources
        ],
        "nodes": [
            {
                "node_id": node.node_id,
                "inventory_source_id": node.inventory_source_id,
                "name": node.name,
                "status": node.status,
                "available": node.available,
                "facts": _diagnostic_value(node.facts),
            }
            for node in snapshot.nodes
        ],
        "resources": [
            {
                "resource_id": resource.resource_id,
                "inventory_source_id": resource.inventory_source_id,
                "active_binding_id": resource.active_binding_id,
                "resource_type": resource.resource_type.value,
                "vmid": resource.vmid,
                "locator_generation": resource.locator_generation,
                "resource_continuity_revision": (
                    resource.resource_continuity_revision
                ),
                "name": resource.name,
                "current_node_id": resource.current_node_id,
                "last_known_node_id": resource.last_known_node_id,
                "status": resource.status,
                "state_level": resource.state_level.value,
                "presence": resource.presence.value,
                "lifecycle": resource.lifecycle.value,
                "observational_continuity": (
                    resource.observational_continuity.value
                ),
                "security_continuity": resource.security_continuity.value,
                "detail_status": resource.detail_status.value,
                "node_availability": resource.node_availability.value,
                "retained_policy": _diagnostic_value(resource.retained_policy),
                "effective_policy": _diagnostic_value(resource.effective_policy),
                "policy_applicable": resource.policy_applicable,
                "suspended_reason": resource.suspended_reason,
                "effective_capabilities": sorted(
                    resource.effective_capabilities
                ),
                "termination_reason": resource.termination_reason,
                "successor_resource_id": resource.successor_resource_id,
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
