"""Native Home Assistant actions for exact package-plan review and approval."""

from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Any

from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
import voluptuous as vol

from .api import HubinetOpsApiError, PackageScanStatus
from .const import (
    DATA_COORDINATORS,
    DATA_SERVICES_REGISTERED,
    DOMAIN,
    SERVICE_APPROVE_UPDATE_PLAN,
    SERVICE_VIEW_UPDATE_PLAN,
)
from .coordinator import (
    HubinetOpsCoordinator,
    resource_device_name,
    resource_registry_key,
)

ATTR_DEVICE_ID = "device_id"
ATTR_RESOURCE_ID = "resource_id"
ATTR_SCAN_RUN_ID = "scan_run_id"
ATTR_PLAN_FINGERPRINT = "plan_fingerprint"

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")

_VIEW_SCHEMA = vol.Schema(
    {vol.Required(ATTR_DEVICE_ID): str}
)
_APPROVE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_RESOURCE_ID): vol.Match(_UUID_RE),
        vol.Required(ATTR_SCAN_RUN_ID): vol.Match(_UUID_RE),
        vol.Required(ATTR_PLAN_FINGERPRINT): vol.Match(_FINGERPRINT_RE),
    }
)


def _coordinator_for_resource(
    hass: HomeAssistant, resource_id: str
) -> HubinetOpsCoordinator:
    coordinators: Mapping[str, HubinetOpsCoordinator] = hass.data.get(DOMAIN, {}).get(
        DATA_COORDINATORS, {}
    )
    matches = [
        coordinator
        for coordinator in coordinators.values()
        if resource_id in coordinator.data.resources_by_id
    ]
    if len(matches) != 1:
        raise HomeAssistantError(
            "resource must belong to exactly one loaded Hubinet Ops backend"
        )
    return matches[0]


def _coordinator_and_resource_for_device(
    hass: HomeAssistant, device_id: str
) -> tuple[HubinetOpsCoordinator, str]:
    """Resolve one selected HA device to exactly one loaded Hubinet resource."""

    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        raise HomeAssistantError("selected Hubinet Ops resource device does not exist")

    coordinators: Mapping[str, HubinetOpsCoordinator] = hass.data.get(DOMAIN, {}).get(
        DATA_COORDINATORS, {}
    )
    matches: list[tuple[HubinetOpsCoordinator, str]] = []
    for coordinator in coordinators.values():
        if coordinator.config_entry.entry_id not in device.config_entries:
            continue
        backend_instance_id = coordinator.data.backend.backend_instance_id
        for resource in coordinator.data.resources:
            if (
                DOMAIN,
                resource_registry_key(backend_instance_id, resource.resource_id),
            ) in device.identifiers:
                matches.append((coordinator, resource.resource_id))

    if len(matches) != 1:
        raise HomeAssistantError(
            "selected device must identify exactly one loaded Hubinet Ops resource"
        )
    return matches[0]


def _approval_response(approval: Any) -> dict[str, Any]:
    return {
        "status": approval.status.value,
        "approvable": approval.approvable,
        "approval_id": approval.approval_id,
        "reviewed_scan_run_id": approval.reviewed_scan_run_id,
        "plan_fingerprint": approval.plan_fingerprint,
        "approved_at": approval.approved_at,
    }


async def _view_update_plan(
    hass: HomeAssistant, call: ServiceCall
) -> ServiceResponse:
    coordinator, resource_id = _coordinator_and_resource_for_device(
        hass, call.data[ATTR_DEVICE_ID]
    )
    try:
        snapshot = await coordinator.api.async_fetch_resource_snapshot()
    except HubinetOpsApiError as exc:
        raise HomeAssistantError("could not read the current Hubinet Ops plan") from exc
    if snapshot.backend.backend_instance_id != coordinator.config_entry.unique_id:
        raise HomeAssistantError("backend identity changed during plan review")
    resource = snapshot.resources_by_id.get(resource_id)
    if resource is None:
        raise HomeAssistantError("resource is absent from the fresh backend snapshot")

    scan = resource.package_scan
    approval = resource.package_plan_approval
    approvable = bool(
        approval.approvable
        and scan.status is PackageScanStatus.SUCCESS
        and scan.scan_run_id is not None
        and scan.plan_fingerprint is not None
    )
    reference = (
        {
            ATTR_RESOURCE_ID: resource.resource_id,
            ATTR_SCAN_RUN_ID: scan.scan_run_id,
            ATTR_PLAN_FINGERPRINT: scan.plan_fingerprint,
        }
        if approvable
        else None
    )
    return {
        "resource_id": resource.resource_id,
        "resource_name": resource_device_name(resource),
        "approvable": approvable,
        "scan_status": scan.status.value,
        "scan_run_id": scan.scan_run_id if approvable else None,
        "plan_fingerprint": scan.plan_fingerprint if approvable else None,
        "pending_count": scan.pending_count,
        "packages": [
            {
                "name": package.name,
                "installed_version": package.installed_version,
                "candidate_version": package.candidate_version,
                "origin": package.origin,
                "security": package.security,
                "description": package.description,
            }
            for package in scan.packages
        ],
        "approval": _approval_response(approval),
        "approval_reference": reference,
    }


async def _approve_update_plan(hass: HomeAssistant, call: ServiceCall) -> None:
    resource_id = call.data[ATTR_RESOURCE_ID]
    scan_run_id = call.data[ATTR_SCAN_RUN_ID]
    plan_fingerprint = call.data[ATTR_PLAN_FINGERPRINT]
    coordinator = _coordinator_for_resource(hass, resource_id)
    try:
        await coordinator.api.async_approve_package_plan(
            resource_id, scan_run_id, plan_fingerprint
        )
    except HubinetOpsApiError as exc:
        raise HomeAssistantError("Hubinet Ops refused the reviewed update plan") from exc
    await coordinator.async_request_refresh()


def async_setup_services(hass: HomeAssistant) -> None:
    """Register the two domain actions exactly once."""

    domain_data = hass.data.setdefault(DOMAIN, {})
    if domain_data.get(DATA_SERVICES_REGISTERED):
        return

    async def view_handler(call: ServiceCall) -> ServiceResponse:
        return await _view_update_plan(hass, call)

    async def approve_handler(call: ServiceCall) -> None:
        await _approve_update_plan(hass, call)

    hass.services.async_register(
        DOMAIN,
        SERVICE_VIEW_UPDATE_PLAN,
        view_handler,
        schema=_VIEW_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_APPROVE_UPDATE_PLAN,
        approve_handler,
        schema=_APPROVE_SCHEMA,
    )
    domain_data[DATA_SERVICES_REGISTERED] = True


def async_unload_services(hass: HomeAssistant) -> None:
    """Remove domain actions after the final config entry unloads."""

    domain_data = hass.data.get(DOMAIN, {})
    if not domain_data.pop(DATA_SERVICES_REGISTERED, False):
        return
    hass.services.async_remove(DOMAIN, SERVICE_VIEW_UPDATE_PLAN)
    hass.services.async_remove(DOMAIN, SERVICE_APPROVE_UPDATE_PLAN)
