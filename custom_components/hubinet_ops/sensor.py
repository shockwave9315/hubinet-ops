"""Read-only source, node, and resource sensors for Hubinet Ops.

The dynamic platform callback pattern was adapted from Home Assistant Core's
Apache-2.0 ``proxmoxve`` integration. Availability is deliberately split:
resource devices/entities remain retained, while present-resource current facts
fail closed on source freshness, detail status, or node relation.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, override

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import (
    DetailStatus,
    InventorySourceSnapshot,
    NodeAvailability,
    NodeSnapshot,
    PackageScanStatus,
    PresenceState,
    ResourceSnapshot,
)
from .coordinator import HubinetOpsConfigEntry
from .entity import (
    HubinetOpsNodeEntity,
    HubinetOpsResourceEntity,
    HubinetOpsSourceEntity,
)

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class HubinetOpsSourceSensorDescription(SensorEntityDescription):
    """Describe one source health/freshness diagnostic."""

    value_fn: Callable[[InventorySourceSnapshot], Any]


@dataclass(frozen=True, kw_only=True)
class HubinetOpsNodeSensorDescription(SensorEntityDescription):
    """Describe a current node sensor."""

    value_fn: Callable[[NodeSnapshot], str]


@dataclass(frozen=True, kw_only=True)
class HubinetOpsResourceSensorDescription(SensorEntityDescription):
    """Describe a resource sensor and its required current-state axes."""

    value_fn: Callable[[ResourceSnapshot], Any]
    requires_current_source: bool = False
    requires_detail: bool = False
    requires_node: bool = False
    requires_package_scan_success: bool = False
    requires_package_scan_completed: bool = False
    requires_reboot_known: bool = False


SOURCE_SENSORS = (
    HubinetOpsSourceSensorDescription(
        key="health",
        translation_key="source_health",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda source: source.health.value,
    ),
    HubinetOpsSourceSensorDescription(
        key="freshness",
        translation_key="source_freshness",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda source: source.freshness.value,
    ),
    HubinetOpsSourceSensorDescription(
        key="last_successful_observation",
        translation_key="source_last_successful_observation",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda source: source.last_successful_observed_at,
    ),
    HubinetOpsSourceSensorDescription(
        key="freshness_valid_until",
        translation_key="source_freshness_valid_until",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda source: source.freshness_valid_until,
    ),
)

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
        requires_current_source=True,
        requires_detail=True,
        requires_node=True,
    ),
    HubinetOpsResourceSensorDescription(
        key="type",
        translation_key="resource_type",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda resource: resource.resource_type.value,
    ),
    HubinetOpsResourceSensorDescription(
        key="node",
        translation_key="resource_node",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda resource: resource.relation_node_id,
        requires_current_source=True,
        requires_node=True,
    ),
    HubinetOpsResourceSensorDescription(
        key="presence",
        translation_key="resource_presence",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda resource: resource.presence.value,
    ),
    HubinetOpsResourceSensorDescription(
        key="detail_status",
        translation_key="resource_detail_status",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda resource: resource.detail_status.value,
    ),
    HubinetOpsResourceSensorDescription(
        key="lifecycle",
        translation_key="resource_lifecycle",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda resource: resource.lifecycle.value,
    ),
    HubinetOpsResourceSensorDescription(
        key="observational_continuity",
        translation_key="resource_observational_continuity",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda resource: resource.observational_continuity.value,
    ),
    HubinetOpsResourceSensorDescription(
        key="security_continuity",
        translation_key="resource_security_continuity",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda resource: resource.security_continuity.value,
    ),
    HubinetOpsResourceSensorDescription(
        key="package_scan_status",
        translation_key="resource_package_scan_status",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda resource: resource.package_scan.status.value,
    ),
    HubinetOpsResourceSensorDescription(
        key="pending_updates",
        translation_key="resource_pending_updates",
        value_fn=lambda resource: resource.package_scan.pending_count,
        requires_current_source=True,
        requires_package_scan_success=True,
    ),
    HubinetOpsResourceSensorDescription(
        key="last_package_scan",
        translation_key="resource_last_package_scan",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda resource: resource.package_scan.completed_at,
        requires_package_scan_completed=True,
    ),
    HubinetOpsResourceSensorDescription(
        key="reboot_required",
        translation_key="resource_reboot_required",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda resource: resource.package_scan.reboot_required,
        requires_current_source=True,
        requires_package_scan_success=True,
        requires_reboot_known=True,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HubinetOpsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up current sensors and subscribe for dynamic inventory additions."""

    coordinator = entry.runtime_data

    def add_sources(sources: list[InventorySourceSnapshot]) -> None:
        async_add_entities(
            HubinetOpsSourceSensor(coordinator, description, source)
            for source in sources
            for description in SOURCE_SENSORS
        )

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

    coordinator.new_sources_callbacks.append(add_sources)
    coordinator.new_nodes_callbacks.append(add_nodes)
    coordinator.new_resources_callbacks.append(add_resources)
    entry.async_on_unload(
        lambda: coordinator.new_sources_callbacks.remove(add_sources)
    )
    entry.async_on_unload(lambda: coordinator.new_nodes_callbacks.remove(add_nodes))
    entry.async_on_unload(
        lambda: coordinator.new_resources_callbacks.remove(add_resources)
    )

    add_sources(list(coordinator.data.sources))
    add_nodes(list(coordinator.data.nodes))
    add_resources(list(coordinator.data.resources))


class HubinetOpsSourceSensor(HubinetOpsSourceEntity, SensorEntity):
    """Read-only fixed health/freshness sensor for an inventory source."""

    entity_description: HubinetOpsSourceSensorDescription

    @property
    @override
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.source)

    @property
    @override
    def extra_state_attributes(self) -> Mapping[str, Any]:
        source = self.source
        return {
            "health_origin": source.health_origin.value,
            "health_reason": source.health_reason,
            "last_issued_run_sequence": source.last_issued_run_sequence,
            "latest_completed_run_sequence": source.latest_completed_run_sequence,
            "latest_completed_outcome": source.latest_completed_outcome,
            "last_health_run_sequence": source.last_health_run_sequence,
            "last_run_health_outcome": source.last_run_health_outcome,
            "last_committed_run_sequence": source.last_committed_run_sequence,
            "freshness_reference_at": source.freshness_reference_at,
            "source_config_revision": source.current_context.source_config_revision,
            "transport_trust_revision": source.current_context.transport_trust_revision,
        }


class HubinetOpsNodeSensor(HubinetOpsNodeEntity, SensorEntity):
    """Read-only current status sensor for a backend node."""

    entity_description: HubinetOpsNodeSensorDescription

    @property
    @override
    def native_value(self) -> str:
        node = self.coordinator.data.nodes_by_id[self._node_id]
        return self.entity_description.value_fn(node)


class HubinetOpsResourceSensor(HubinetOpsResourceEntity, SensorEntity):
    """Read-only sensor for one opaque backend resource identity."""

    entity_description: HubinetOpsResourceSensorDescription

    @property
    @override
    def available(self) -> bool:
        if not super().available:
            return False
        resource = self.resource
        if resource.presence is not PresenceState.PRESENT:
            return False
        source = self.coordinator.data.sources_by_id[resource.inventory_source_id]
        description = self.entity_description
        if description.requires_current_source and not source.current_facts_available:
            return False
        if description.requires_detail and (
            resource.presence is not PresenceState.PRESENT
            or resource.detail_status is not DetailStatus.OK
        ):
            return False
        if description.requires_node and (
            resource.presence is not PresenceState.PRESENT
            or resource.current_node_id is None
            or resource.node_availability is not NodeAvailability.AVAILABLE
        ):
            return False
        package_scan = resource.package_scan
        if (
            description.requires_package_scan_success
            and package_scan.status is not PackageScanStatus.SUCCESS
        ):
            return False
        if description.requires_package_scan_completed and package_scan.completed_at is None:
            return False
        if description.requires_reboot_known and package_scan.reboot_required is None:
            return False
        return True

    @property
    @override
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.resource)

    @property
    @override
    def extra_state_attributes(self) -> Mapping[str, Any]:
        resource = self.resource
        source = self.coordinator.data.sources_by_id[resource.inventory_source_id]
        return {
            "inventory_source_id": resource.inventory_source_id,
            "resource_id": resource.resource_id,
            "vmid": resource.vmid,
            "locator_generation": resource.locator_generation,
            "resource_continuity_revision": resource.resource_continuity_revision,
            "presence": resource.presence.value,
            "detail_status": resource.detail_status.value,
            "node_availability": resource.node_availability.value,
            "source_health": source.health.value,
            "source_freshness": source.freshness.value,
            "package_scan_status": resource.package_scan.status.value,
            "package_scan_run_id": resource.package_scan.scan_run_id,
            "package_scan_completed_at": resource.package_scan.completed_at,
            "package_plan_fingerprint": resource.package_scan.plan_fingerprint,
        }
