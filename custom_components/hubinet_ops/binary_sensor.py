"""Truthful binary operator facts for Hubinet Ops resources."""

from __future__ import annotations

from typing import override

from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import PresenceState, ResourceSnapshot, ResourceType
from .coordinator import HubinetOpsConfigEntry
from .entity import HubinetOpsResourceEntity

PARALLEL_UPDATES = 0

ROLLBACK_AVAILABLE = BinarySensorEntityDescription(
    key="rollback_available",
    translation_key="rollback_available",
    icon="mdi:backup-restore",
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HubinetOpsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data

    def add_resources(resources: list[ResourceSnapshot]) -> None:
        async_add_entities(
            HubinetOpsRollbackAvailableBinarySensor(
                coordinator, ROLLBACK_AVAILABLE, resource
            )
            for resource in resources
            if resource.resource_type is ResourceType.LXC
        )

    coordinator.new_resources_callbacks.append(add_resources)
    entry.async_on_unload(
        lambda: coordinator.new_resources_callbacks.remove(add_resources)
    )
    add_resources(list(coordinator.data.resources))


class HubinetOpsRollbackAvailableBinarySensor(
    HubinetOpsResourceEntity, BinarySensorEntity
):
    """Whether backend authority currently accepts explicit same-job rollback."""

    @property
    @override
    def available(self) -> bool:
        return (
            super().available
            and self.resource.resource_type is ResourceType.LXC
            and self.resource.presence is PresenceState.PRESENT
        )

    @property
    @override
    def is_on(self) -> bool:
        return self.resource.operator_capabilities.can_rollback_update
