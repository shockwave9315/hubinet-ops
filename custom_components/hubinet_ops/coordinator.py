"""Data coordinator for the Hubinet Ops integration.

Portions of the coordinator/callback structure were adapted from Home Assistant
Core's ``proxmoxve`` integration and changed for Hubinet Ops.  Upstream is
licensed under Apache-2.0; see ``NOTICE.md`` and the vendored license.
"""

from __future__ import annotations

from collections.abc import Callable
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    HubinetOpsApi,
    HubinetOpsApiError,
    HubinetOpsInvalidAuth,
    HubinetOpsSnapshot,
    NodeSnapshot,
    ResourceIdentity,
    ResourceSnapshot,
    ResourceType,
)
from .const import (
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    MANUFACTURER,
    MODEL_LXC,
    MODEL_NODE,
    MODEL_QEMU,
)

_LOGGER = logging.getLogger(__name__)

type HubinetOpsConfigEntry = ConfigEntry[HubinetOpsCoordinator]


def node_identifier(node: NodeSnapshot) -> tuple[str, str]:
    """Return the Device Registry identifier for a backend node."""

    return DOMAIN, node.registry_key


def resource_identifier(identity: ResourceIdentity) -> tuple[str, str]:
    """Return the Device Registry identifier for a backend resource."""

    return DOMAIN, identity.registry_key


def node_device_info(node: NodeSnapshot) -> dr.DeviceInfo:
    """Build Device Registry information for a Hubinet Ops node."""

    return dr.DeviceInfo(
        identifiers={node_identifier(node)},
        manufacturer=MANUFACTURER,
        model=MODEL_NODE,
        name=f"Node {node.name}",
    )


def resource_device_name(resource: ResourceSnapshot) -> str:
    """Return the display name without using it as identity."""

    prefix = "VM" if resource.identity.resource_type is ResourceType.QEMU else "CT"
    return f"{prefix}{resource.identity.vmid} {resource.name}"


def resource_device_info(resource: ResourceSnapshot) -> dr.DeviceInfo:
    """Build Device Registry information using current node as a relation only."""

    node_key = f"{resource.identity.instance_id}:node:{resource.node_id}"
    model = (
        MODEL_QEMU
        if resource.identity.resource_type is ResourceType.QEMU
        else MODEL_LXC
    )
    return dr.DeviceInfo(
        identifiers={resource_identifier(resource.identity)},
        manufacturer=MANUFACTURER,
        model=model,
        name=resource_device_name(resource),
        via_device=(DOMAIN, node_key),
    )


class HubinetOpsCoordinator(DataUpdateCoordinator[HubinetOpsSnapshot]):
    """Fetch one logical backend snapshot and publish dynamic additions."""

    config_entry: HubinetOpsConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: HubinetOpsConfigEntry,
        api: HubinetOpsApi,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=DOMAIN,
            update_interval=DEFAULT_UPDATE_INTERVAL,
        )
        self.api = api
        self.known_nodes: set[tuple[str, str]] = set()
        self.known_resources: set[ResourceIdentity] = set()
        self.new_nodes_callbacks: list[Callable[[list[NodeSnapshot]], None]] = []
        self.new_resources_callbacks: list[
            Callable[[list[ResourceSnapshot]], None]
        ] = []

    async def _async_update_data(self) -> HubinetOpsSnapshot:
        """Fetch and reconcile one read-only Hubinet Ops snapshot."""

        try:
            incoming = await self.api.async_fetch_resource_snapshot()
        except HubinetOpsInvalidAuth as err:
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN,
                translation_key="invalid_auth",
            ) from err
        except HubinetOpsApiError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="cannot_connect",
            ) from err

        previous = self.data
        data = incoming.preserving_unconfirmed_missing(previous)
        self._async_publish_inventory(data)
        return data

    def _async_publish_inventory(self, data: HubinetOpsSnapshot) -> None:
        """Synchronize device relationships and notify platforms of additions."""

        device_registry = dr.async_get(self.hass)
        for node in data.nodes:
            device_registry.async_get_or_create(
                config_entry_id=self.config_entry.entry_id,
                **node_device_info(node),
            )
        for resource in data.resources:
            device_registry.async_get_or_create(
                config_entry_id=self.config_entry.entry_id,
                **resource_device_info(resource),
            )

        current_nodes = {(node.instance_id, node.node_id) for node in data.nodes}
        new_node_ids = current_nodes - self.known_nodes
        self.known_nodes.update(current_nodes)
        if new_node_ids:
            new_nodes = [
                node
                for node in data.nodes
                if (node.instance_id, node.node_id) in new_node_ids
            ]
            for notify in tuple(self.new_nodes_callbacks):
                notify(new_nodes)

        current_resources = {resource.identity for resource in data.resources}
        new_resource_ids = current_resources - self.known_resources
        self.known_resources.update(current_resources)
        if new_resource_ids:
            new_resources = [
                resource
                for resource in data.resources
                if resource.identity in new_resource_ids
            ]
            for notify in tuple(self.new_resources_callbacks):
                notify(new_resources)
