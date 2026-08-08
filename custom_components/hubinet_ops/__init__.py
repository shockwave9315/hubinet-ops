"""Hubinet Ops native Home Assistant integration.

The config-entry setup shape was adapted from Home Assistant Core's Apache-2.0
``proxmoxve`` integration and changed to communicate only with Hubinet Ops.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .api import HubinetOpsApi, HubinetOpsApiFactory, phase_zero_api_factory
from .const import (
    CONF_API_TOKEN,
    CONF_BASE_URL,
    CONF_VERIFY_TLS,
    DATA_API_FACTORY,
    DOMAIN,
)
from .coordinator import HubinetOpsConfigEntry, HubinetOpsCoordinator

PLATFORMS = [Platform.SENSOR]


def create_api_client(
    hass: HomeAssistant, data: Mapping[str, Any]
) -> HubinetOpsApi:
    """Create a backend-only client through the configured transport factory."""

    domain_data = hass.data.setdefault(DOMAIN, {})
    factory: HubinetOpsApiFactory = domain_data.get(
        DATA_API_FACTORY, phase_zero_api_factory
    )
    return factory(
        base_url=str(data[CONF_BASE_URL]),
        api_token=str(data[CONF_API_TOKEN]),
        verify_tls=bool(data[CONF_VERIFY_TLS]),
    )


async def async_setup_entry(
    hass: HomeAssistant, entry: HubinetOpsConfigEntry
) -> bool:
    """Set up Hubinet Ops from one config entry."""

    api = create_api_client(hass, entry.data)
    coordinator = HubinetOpsCoordinator(hass, entry, api)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: HubinetOpsConfigEntry
) -> bool:
    """Unload a Hubinet Ops config entry."""

    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Reserve an explicit migration hook for future 0.5 config entry versions."""

    return entry.version == 1
