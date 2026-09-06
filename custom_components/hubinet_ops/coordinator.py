"""Data coordinator for the authoritative Hubinet Ops published snapshot.

Portions of the coordinator/callback structure were adapted from Home Assistant
Core's ``proxmoxve`` integration and changed for Hubinet Ops. Upstream is
licensed under Apache-2.0; see ``NOTICE.md`` and the vendored license.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
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
    HubinetOpsOperatorAvailabilityUnsupported,
    HubinetOpsSnapshot,
    InventorySourceSnapshot,
    NodeSnapshot,
    OperatorAvailabilityView,
    OperatorCapabilities,
    ResourceOperatorAvailability,
    ResourceSnapshot,
    ResourceType,
    validate_operator_availability,
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


@dataclass(frozen=True, slots=True)
class ReviewedUpdatePlanReference:
    """Ephemeral UI memory of one exact plan the operator reviewed."""

    backend_instance_id: str
    resource_id: str
    scan_run_id: str
    plan_fingerprint: str


def _all_false_operator_availability(
    incoming: HubinetOpsSnapshot,
) -> OperatorAvailabilityView:
    """Build the conservative fallback view for HUMAN1-AVAIL-COMPAT-01.

    Used ONLY when the transport raises the typed, definite
    ``HubinetOpsOperatorAvailabilityUnsupported`` -- never for an ordinary
    failure. Every capability is false for every resource in the just-fetched
    snapshot: no authority is invented, and because this view is derived from
    ``incoming`` itself, its ``authority_published_state_revision`` is
    trivially aligned and can never trigger the revision-race handling below.
    """

    return OperatorAvailabilityView(
        backend_instance_id=incoming.backend.backend_instance_id,
        authority_published_state_revision=incoming.published_state_revision,
        resources=tuple(
            ResourceOperatorAvailability(
                resource_id=resource_id, capabilities=OperatorCapabilities()
            )
            for resource_id in incoming.resources_by_id
        ),
    )


def _availability_revision_race(
    availability: OperatorAvailabilityView, incoming: HubinetOpsSnapshot
) -> bool:
    """Return whether a mismatch is ONLY an ordinary revision race.

    Bound to the exact shape of HUMAN1-AVAIL-RACE-01: the two reads named the
    same backend and the same resource set, and disagree only on the
    revision number an intervening, legitimate backend write could explain.
    A backend identity mismatch or a resource-set mismatch is a structural
    inconsistency, never a race, and this deliberately returns ``False`` for
    those so the caller fails closed on the first attempt instead of
    retrying them.
    """

    return (
        availability.backend_instance_id == incoming.backend.backend_instance_id
        and set(availability.resources_by_id) == set(incoming.resources_by_id)
        and availability.authority_published_state_revision
        != incoming.published_state_revision
    )


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
        # UX state only. It is never persisted and grants no backend
        # authority. Reloading Home Assistant intentionally empties it.
        self.reviewed_update_plans: dict[str, ReviewedUpdatePlanReference] = {}
        self.operator_availability: OperatorAvailabilityView

    def operator_capabilities(self, resource_id: str) -> OperatorCapabilities:
        """Return backend-owned point-in-time availability for one resource."""

        return self.operator_availability.resources_by_id[resource_id]

    def remember_reviewed_update_plan(
        self, reference: ReviewedUpdatePlanReference
    ) -> None:
        self.reviewed_update_plans[reference.resource_id] = reference
        self.async_update_listeners()

    def forget_reviewed_update_plan(self, resource_id: str) -> None:
        if self.reviewed_update_plans.pop(resource_id, None) is not None:
            self.async_update_listeners()

    def reviewed_update_plan(
        self, resource_id: str
    ) -> ReviewedUpdatePlanReference | None:
        return self.reviewed_update_plans.get(resource_id)

    def _invalidate_stale_plan_reviews(self, incoming: HubinetOpsSnapshot) -> None:
        """Discard UX references contradicted by a newer backend view."""

        stale: list[str] = []
        for resource_id, reference in self.reviewed_update_plans.items():
            resource = incoming.resources_by_id.get(resource_id)
            if (
                incoming.backend.backend_instance_id
                != reference.backend_instance_id
                or resource is None
                or not self.operator_capabilities(
                    resource_id
                ).can_approve_update_plan
                or resource.package_scan.scan_run_id != reference.scan_run_id
                or resource.package_scan.plan_fingerprint
                != reference.plan_fingerprint
            ):
                stale.append(resource_id)
        for resource_id in stale:
            self.reviewed_update_plans.pop(resource_id, None)

    #: HUMAN1-AVAIL-RACE-01's exact bound: the first read plus exactly one
    #: retry of the COMPLETE snapshot+availability pair. Never unbounded,
    #: never a loop -- a persistent mismatch on the second attempt fails
    #: closed exactly like it always did.
    _MAX_COHERENCE_ATTEMPTS = 2

    async def _fetch_snapshot(
        self, previous: HubinetOpsSnapshot | None
    ) -> HubinetOpsSnapshot:
        """Fetch and validate one fresh snapshot against the prior one."""

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

        if previous is not None:
            try:
                incoming.validate_revision_successor(previous)
            except ValueError as err:
                raise UpdateFailed(
                    translation_domain=DOMAIN,
                    translation_key="invalid_snapshot",
                ) from err
        return incoming

    async def _fetch_availability(
        self, incoming: HubinetOpsSnapshot
    ) -> OperatorAvailabilityView:
        """Fetch volatile availability, applying only the one typed fallback.

        HUMAN1-AVAIL-COMPAT-01: a definite "this route does not exist"
        result from a backend that predates Human1 publication falls back to
        an all-false view aligned to ``incoming``. Every other failure --
        auth, connection, timeout, 5xx, malformed body -- remains fail-closed
        and is never treated as that one compatibility case.
        """

        try:
            return await self.api.async_fetch_operator_availability()
        except HubinetOpsOperatorAvailabilityUnsupported:
            return _all_false_operator_availability(incoming)
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

    async def _fetch_coherent_pair(
        self, previous: HubinetOpsSnapshot | None
    ) -> tuple[HubinetOpsSnapshot, OperatorAvailabilityView]:
        """Fetch one mutually coherent snapshot/availability pair.

        HUMAN1-AVAIL-RACE-01: an ordinary backend write can land between the
        two independently correct HTTP reads below and make them disagree
        only on revision. That gets exactly one bounded self-heal: refetch
        the COMPLETE pair once. A backend-identity or resource-set mismatch
        is never treated as this race and fails closed immediately, and a
        revision mismatch still present after the retry fails closed too.
        """

        for attempt in range(1, self._MAX_COHERENCE_ATTEMPTS + 1):
            incoming = await self._fetch_snapshot(previous)
            availability = await self._fetch_availability(incoming)

            if _availability_revision_race(availability, incoming):
                if attempt < self._MAX_COHERENCE_ATTEMPTS:
                    continue
                raise UpdateFailed(
                    translation_domain=DOMAIN,
                    translation_key="invalid_snapshot",
                )

            try:
                validate_operator_availability(availability, incoming)
            except ValueError as err:
                raise UpdateFailed(
                    translation_domain=DOMAIN,
                    translation_key="invalid_snapshot",
                ) from err

            return incoming, availability

        raise AssertionError(  # pragma: no cover - loop always returns or raises
            "coherence attempt bound exhausted without a terminal outcome"
        )

    async def _async_update_data(self) -> HubinetOpsSnapshot:
        """Fetch one authoritative immutable Hubinet Ops snapshot."""

        previous = getattr(self, "data", None)
        incoming, availability = await self._fetch_coherent_pair(previous)

        self.operator_availability = availability
        self._invalidate_stale_plan_reviews(incoming)
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
