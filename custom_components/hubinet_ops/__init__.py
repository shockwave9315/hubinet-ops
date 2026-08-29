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

from .api import HubinetOpsApi, HubinetOpsApiFactory
from .const import (
    CONF_API_TOKEN,
    CONF_BASE_URL,
    CONF_VERIFY_TLS,
    DATA_API_FACTORY,
    DATA_COORDINATORS,
    DOMAIN,
)
from .coordinator import HubinetOpsConfigEntry, HubinetOpsCoordinator
from .services import async_setup_services, async_unload_services
from .transport_http import http_api_factory

PLATFORMS = [Platform.SENSOR]


def create_api_client(
    hass: HomeAssistant, data: Mapping[str, Any]
) -> HubinetOpsApi:
    """Create a backend-only client through the configured transport factory.

    Defaults to the real R0 HTTP transport (``http_api_factory``, bound to
    this ``hass`` instance) -- the injection seam at
    ``hass.data[DOMAIN][DATA_API_FACTORY]`` remains exactly as before for
    tests/fakes to override explicitly.
    """

    domain_data = hass.data.setdefault(DOMAIN, {})
    factory: HubinetOpsApiFactory = domain_data.get(DATA_API_FACTORY) or http_api_factory(
        hass
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

    # Only reflect this entry in global Hubinet state (the loaded-coordinator
    # map and the domain services) once platform forwarding has actually
    # succeeded. Sensor setup needs only entry.runtime_data, so nothing
    # requires these globals to exist beforehand -- and a failure below must
    # not leave a stale coordinator or callable approval services behind for
    # an entry that never finished loading.
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    hass.data.setdefault(DOMAIN, {}).setdefault(DATA_COORDINATORS, {})[
        entry.entry_id
    ] = coordinator
    async_setup_services(hass)
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: HubinetOpsConfigEntry
) -> bool:
    """Unload a Hubinet Ops config entry."""

    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unloaded:
        return False
    coordinators = hass.data.get(DOMAIN, {}).get(DATA_COORDINATORS, {})
    coordinators.pop(entry.entry_id, None)
    if not coordinators:
        async_unload_services(hass)
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Reserve an explicit migration hook for future 0.5 config entry versions."""

    return entry.version == 1
