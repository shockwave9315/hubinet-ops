"""Coordinator entity bases for Hubinet Ops.

The general CoordinatorEntity/DeviceInfo pattern was adapted from Home Assistant
Core's Apache-2.0 ``proxmoxve`` integration and substantially changed.
"""

from __future__ import annotations

from typing import override

from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import InventorySourceSnapshot, NodeSnapshot, ResourceSnapshot
from .coordinator import (
    HubinetOpsCoordinator,
    node_device_info,
    node_registry_key,
    resource_device_info,
    resource_registry_key,
    source_device_info,
    source_registry_key,
)


class HubinetOpsCoordinatorEntity(CoordinatorEntity[HubinetOpsCoordinator]):
    """Base for read-only entities backed by the single coordinator."""

    _attr_has_entity_name = True


class HubinetOpsSourceEntity(HubinetOpsCoordinatorEntity):
    """Entity attached to a backend-owned inventory source identity."""

    def __init__(
        self,
        coordinator: HubinetOpsCoordinator,
        entity_description: EntityDescription,
        source: InventorySourceSnapshot,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = entity_description
        self._source_id = source.inventory_source_id
        backend_instance_id = coordinator.data.backend.backend_instance_id
        self._attr_device_info = source_device_info(backend_instance_id, source)
        self._attr_unique_id = (
            f"{source_registry_key(backend_instance_id, self._source_id)}:"
            f"{entity_description.key}"
        )

    @property
    @override
    def available(self) -> bool:
        return (
            super().available
            and self._source_id in self.coordinator.data.sources_by_id
        )

    @property
    def source(self) -> InventorySourceSnapshot:
        """Return the latest fixed source view."""

        return self.coordinator.data.sources_by_id[self._source_id]


class HubinetOpsNodeEntity(HubinetOpsCoordinatorEntity):
    """Entity attached to a backend-owned node identity."""

    def __init__(
        self,
        coordinator: HubinetOpsCoordinator,
        entity_description: EntityDescription,
        node: NodeSnapshot,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = entity_description
        self._node_id = node.node_id
        backend_instance_id = coordinator.data.backend.backend_instance_id
        self._attr_device_info = node_device_info(
            coordinator.hass,
            coordinator.config_entry.entry_id,
            backend_instance_id,
            node,
        )
        self._attr_unique_id = (
            f"{node_registry_key(backend_instance_id, node.node_id)}:"
            f"{entity_description.key}"
        )

    @property
    @override
    def available(self) -> bool:
        node = self.coordinator.data.nodes_by_id.get(self._node_id)
        if node is None:
            return False
        source = self.coordinator.data.sources_by_id[node.inventory_source_id]
        return super().available and source.current_facts_available and node.available


class HubinetOpsResourceEntity(HubinetOpsCoordinatorEntity):
    """Entity attached to opaque resource_id, never to VMID, name, type, or node."""

    def __init__(
        self,
        coordinator: HubinetOpsCoordinator,
        entity_description: EntityDescription,
        resource: ResourceSnapshot,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = entity_description
        self.resource_id = resource.resource_id
        backend_instance_id = coordinator.data.backend.backend_instance_id
        self._attr_device_info = resource_device_info(
            coordinator.hass,
            coordinator.config_entry.entry_id,
            backend_instance_id,
            resource,
        )
        self._attr_unique_id = (
            f"{resource_registry_key(backend_instance_id, resource.resource_id)}:"
            f"{entity_description.key}"
        )

    @property
    @override
    def available(self) -> bool:
        return (
            super().available
            and self.resource_id in self.coordinator.data.resources_by_id
        )

    @property
    def resource(self) -> ResourceSnapshot:
        """Return the latest authoritative view for this resource incarnation."""

        return self.coordinator.data.resources_by_id[self.resource_id]
