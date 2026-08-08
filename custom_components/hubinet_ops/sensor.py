"""Minimal read-only sensor platform for Hubinet Ops.

The dynamic platform callback pattern was adapted from Home Assistant Core's
Apache-2.0 ``proxmoxve`` integration and reduced to neutral Phase 0 sensors.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import override

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import NodeSnapshot, ResourceSnapshot
from .coordinator import HubinetOpsConfigEntry
from .entity import HubinetOpsNodeEntity, HubinetOpsResourceEntity

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class HubinetOpsNodeSensorDescription(SensorEntityDescription):
    """Describe a neutral node sensor."""

    value_fn: Callable[[NodeSnapshot], str]


@dataclass(frozen=True, kw_only=True)
class HubinetOpsResourceSensorDescription(SensorEntityDescription):
    """Describe a neutral resource sensor."""

    value_fn: Callable[[ResourceSnapshot], str]


NODE_SENSORS = (
    HubinetOpsNodeSensorDescription(
        key="status",
        translation_key="node_status",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda node: node.status,
    ),
)

RESOURCE_SENSORS = (
    HubinetOpsResourceSensorDescription(
        key="status",
        translation_key="resource_status",
        value_fn=lambda resource: resource.status,
    ),
    HubinetOpsResourceSensorDescription(
        key="type",
        translation_key="resource_type",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda resource: resource.identity.resource_type.value,
    ),
    HubinetOpsResourceSensorDescription(
        key="node",
        translation_key="resource_node",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda resource: resource.node_id,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HubinetOpsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up current sensors and subscribe for dynamic inventory additions."""

    coordinator = entry.runtime_data

    def add_nodes(nodes: list[NodeSnapshot]) -> None:
        async_add_entities(
            HubinetOpsNodeSensor(coordinator, description, node)
            for node in nodes
            for description in NODE_SENSORS
        )

    def add_resources(resources: list[ResourceSnapshot]) -> None:
        async_add_entities(
            HubinetOpsResourceSensor(coordinator, description, resource)
            for resource in resources
            for description in RESOURCE_SENSORS
        )

    coordinator.new_nodes_callbacks.append(add_nodes)
    coordinator.new_resources_callbacks.append(add_resources)
    entry.async_on_unload(lambda: coordinator.new_nodes_callbacks.remove(add_nodes))
    entry.async_on_unload(
        lambda: coordinator.new_resources_callbacks.remove(add_resources)
    )

    add_nodes(list(coordinator.data.nodes))
    add_resources(list(coordinator.data.resources))


class HubinetOpsNodeSensor(HubinetOpsNodeEntity, SensorEntity):
    """Read-only status sensor for a backend node."""

    entity_description: HubinetOpsNodeSensorDescription

    @property
    @override
    def native_value(self) -> str:
        node = self.coordinator.data.nodes_by_id[self._node_id]
        return self.entity_description.value_fn(node)


class HubinetOpsResourceSensor(HubinetOpsResourceEntity, SensorEntity):
    """Read-only neutral sensor for one durable resource identity."""

    entity_description: HubinetOpsResourceSensorDescription

    @property
    @override
    def native_value(self) -> str:
        return self.entity_description.value_fn(self.resource)
