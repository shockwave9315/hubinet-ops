"""Coordinator entity bases for Hubinet Ops.

The general CoordinatorEntity/DeviceInfo pattern was adapted from Home Assistant
Core's Apache-2.0 ``proxmoxve`` integration and substantially changed.
"""

from __future__ import annotations

from typing import override

from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import NodeSnapshot, ResourceIdentity, ResourceSnapshot
from .coordinator import (
    HubinetOpsCoordinator,
    node_device_info,
    resource_device_info,
)


class HubinetOpsCoordinatorEntity(CoordinatorEntity[HubinetOpsCoordinator]):
    """Base for read-only entities backed by the single coordinator."""

    _attr_has_entity_name = True


class HubinetOpsNodeEntity(HubinetOpsCoordinatorEntity):
    """Entity attached to a node identity."""

    def __init__(
        self,
        coordinator: HubinetOpsCoordinator,
        entity_description: EntityDescription,
        node: NodeSnapshot,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = entity_description
        self._node_id = node.node_id
        self._attr_device_info = node_device_info(node)
        self._attr_unique_id = (
            f"{node.instance_id}:node:{node.node_id}:{entity_description.key}"
        )

    @property
    @override
    def available(self) -> bool:
        node = self.coordinator.data.nodes_by_id.get(self._node_id)
        return super().available and node is not None and node.available


class HubinetOpsResourceEntity(HubinetOpsCoordinatorEntity):
    """Entity attached to durable ResourceIdentity, never to a name or node."""

    def __init__(
        self,
        coordinator: HubinetOpsCoordinator,
        entity_description: EntityDescription,
        resource: ResourceSnapshot,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = entity_description
        identity: ResourceIdentity = resource.identity
        self.identity = identity
        self._attr_device_info = resource_device_info(resource)
        self._attr_unique_id = f"{identity.registry_key}:{entity_description.key}"

    @property
    @override
    def available(self) -> bool:
        resource = self.coordinator.data.resources_by_identity.get(self.identity)
        return super().available and resource is not None and resource.available

    @property
    def resource(self):
        """Return the latest observation for this durable identity."""

        return self.coordinator.data.resources_by_identity[self.identity]
