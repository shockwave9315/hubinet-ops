"""Data coordinator for the authoritative Hubinet Ops published snapshot.

Portions of the coordinator/callback structure were adapted from Home Assistant
Core's ``proxmoxve`` integration and changed for Hubinet Ops. Upstream is
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
    InventorySourceSnapshot,
    NodeSnapshot,
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
    MODEL_SOURCE,
)

_LOGGER = logging.getLogger(__name__)

type HubinetOpsConfigEntry = ConfigEntry[HubinetOpsCoordinator]


def source_registry_key(backend_instance_id: str, inventory_source_id: str) -> str:
    """Serialize one backend-owned source identity for Home Assistant."""

    return f"{backend_instance_id}:source:{inventory_source_id}"


def node_registry_key(backend_instance_id: str, node_id: str) -> str:
    """Serialize one backend-owned node identity for Home Assistant."""

    return f"{backend_instance_id}:node:{node_id}"


def resource_registry_key(backend_instance_id: str, resource_id: str) -> str:
    """Serialize one opaque backend resource identity for Home Assistant."""

    return f"{backend_instance_id}:resource:{resource_id}"


def source_identifier(
    backend_instance_id: str, source: InventorySourceSnapshot
) -> tuple[str, str]:
    """Return the Device Registry identifier for an inventory source."""

    return DOMAIN, source_registry_key(
        backend_instance_id, source.inventory_source_id
    )


def node_identifier(
    backend_instance_id: str, node: NodeSnapshot
) -> tuple[str, str]:
    """Return the Device Registry identifier for a backend node."""

    return DOMAIN, node_registry_key(backend_instance_id, node.node_id)


def resource_identifier(
    backend_instance_id: str, resource: ResourceSnapshot
) -> tuple[str, str]:
    """Return the Device Registry identifier for a backend resource."""

    return DOMAIN, resource_registry_key(backend_instance_id, resource.resource_id)


def _parent_device_id(
    hass: HomeAssistant,
    config_entry_id: str,
    identifier: tuple[str, str],
) -> str:
    """Resolve a validated parent already registered for this snapshot."""

    device_id = dr.async_get_device_id_by_identifier(
        hass,
        identifier,
        config_entry_id=config_entry_id,
    )
    if device_id is None:
        raise ValueError("validated parent device is absent from Device Registry")
    return device_id


def source_device_info(
    backend_instance_id: str, source: InventorySourceSnapshot
) -> dr.DeviceInfo:
    """Build Device Registry information for one read-only source."""

    return dr.DeviceInfo(
        identifiers={source_identifier(backend_instance_id, source)},
        manufacturer=MANUFACTURER,
        model=MODEL_SOURCE,
        name=f"Source {source.name}",
    )


def node_device_info(
    hass: HomeAssistant,
    config_entry_id: str,
    backend_instance_id: str,
    node: NodeSnapshot,
) -> dr.DeviceInfo:
    """Build Device Registry information for a source-namespaced node."""

    source_id = _parent_device_id(
        hass,
        config_entry_id,
        (
            DOMAIN,
            source_registry_key(backend_instance_id, node.inventory_source_id),
        ),
    )
    return dr.DeviceInfo(
        identifiers={node_identifier(backend_instance_id, node)},
        manufacturer=MANUFACTURER,
        model=MODEL_NODE,
        name=f"Node {node.name}",
        via_device_id=source_id,
    )


def resource_device_name(resource: ResourceSnapshot) -> str:
    """Return the display name without using it as identity."""

    prefix = "VM" if resource.resource_type is ResourceType.QEMU else "CT"
    return f"{prefix}{resource.vmid} {resource.name}"


def resource_device_info(
    hass: HomeAssistant,
    config_entry_id: str,
    backend_instance_id: str,
    resource: ResourceSnapshot,
) -> dr.DeviceInfo:
    """Build DeviceInfo with opaque resource identity and validated topology."""

    device_info = dr.DeviceInfo(
        identifiers={resource_identifier(backend_instance_id, resource)},
        manufacturer=MANUFACTURER,
        model=MODEL_QEMU if resource.resource_type is ResourceType.QEMU else MODEL_LXC,
        name=resource_device_name(resource),
        via_device_id=None,
    )
    relation_node_id = resource.relation_node_id
    if relation_node_id is not None:
        device_info["via_device_id"] = _parent_device_id(
            hass,
            config_entry_id,
            (DOMAIN, node_registry_key(backend_instance_id, relation_node_id)),
        )
    return device_info


class HubinetOpsCoordinator(DataUpdateCoordinator[HubinetOpsSnapshot]):
    """Fetch and publish one authoritative backend view without reconciliation."""

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
        self.known_sources: set[str] = set()
        self.known_nodes: set[str] = set()
        self.known_resources: set[str] = set()
        self.new_sources_callbacks: list[
            Callable[[list[InventorySourceSnapshot]], None]
        ] = []
        self.new_nodes_callbacks: list[Callable[[list[NodeSnapshot]], None]] = []
        self.new_resources_callbacks: list[
            Callable[[list[ResourceSnapshot]], None]
        ] = []

    async def _async_update_data(self) -> HubinetOpsSnapshot:
        """Fetch one authoritative immutable Hubinet Ops snapshot."""

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

        if incoming.backend.backend_instance_id != self.config_entry.unique_id:
            # This remains before revision checks, registry writes, callbacks, and
            # publication so a foreign backend can never affect the bound entry.
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="wrong_instance",
            )

        previous = getattr(self, "data", None)
        if previous is not None:
            try:
                incoming.validate_revision_successor(previous)
            except ValueError as err:
                raise UpdateFailed(
                    translation_domain=DOMAIN,
                    translation_key="invalid_snapshot",
                ) from err

        self._async_publish_inventory(incoming)
        return incoming

    def _async_publish_inventory(self, data: HubinetOpsSnapshot) -> None:
        """Synchronize device relationships and notify platforms of additions."""

        backend_instance_id = data.backend.backend_instance_id
        device_registry = dr.async_get(self.hass)
        for source in data.sources:
            device_registry.async_get_or_create(
                config_entry_id=self.config_entry.entry_id,
                **source_device_info(backend_instance_id, source),
            )
        for node in data.nodes:
            device_registry.async_get_or_create(
                config_entry_id=self.config_entry.entry_id,
                **node_device_info(
                    self.hass,
                    self.config_entry.entry_id,
                    backend_instance_id,
                    node,
                ),
            )
        for resource in data.resources:
            device_registry.async_get_or_create(
                config_entry_id=self.config_entry.entry_id,
                **resource_device_info(
                    self.hass,
                    self.config_entry.entry_id,
                    backend_instance_id,
                    resource,
                ),
            )

        current_sources = {
            source.inventory_source_id for source in data.sources
        }
        new_source_ids = current_sources - self.known_sources
        self.known_sources.update(current_sources)
        if new_source_ids:
            new_sources = [
                source
                for source in data.sources
                if source.inventory_source_id in new_source_ids
            ]
            for notify in tuple(self.new_sources_callbacks):
                notify(new_sources)

        current_nodes = {node.node_id for node in data.nodes}
        new_node_ids = current_nodes - self.known_nodes
        self.known_nodes.update(current_nodes)
        if new_node_ids:
            new_nodes = [node for node in data.nodes if node.node_id in new_node_ids]
            for notify in tuple(self.new_nodes_callbacks):
                notify(new_nodes)

        current_resources = {resource.resource_id for resource in data.resources}
        new_resource_ids = current_resources - self.known_resources
        self.known_resources.update(current_resources)
        if new_resource_ids:
            new_resources = [
                resource
                for resource in data.resources
                if resource.resource_id in new_resource_ids
            ]
            for notify in tuple(self.new_resources_callbacks):
                notify(new_resources)
