"""Native Home Assistant actions for update-plan review and health contracts.

Every action here changes authority *metadata* or reads it. None of them
starts an update job, mutates a package, or evaluates a health probe -- the
backend has no endpoint that could, and this module deliberately has no way
to ask for one.
"""

from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Any

from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
import voluptuous as vol

from .api import (
    HealthProbe,
    HealthProbeKind,
    HubinetOpsApiError,
    HubinetOpsHealthContractUnconfigured,
    PackageScanStatus,
)
from .const import (
    DATA_COORDINATORS,
    DATA_SERVICES_REGISTERED,
    DOMAIN,
    SERVICE_APPROVE_UPDATE_PLAN,
    SERVICE_CLEAR_HEALTH_CONTRACT,
    SERVICE_SET_HEALTH_CONTRACT,
    SERVICE_VIEW_HEALTH_CONTRACT,
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
ATTR_PROBES = "probes"
ATTR_KIND = "kind"
ATTR_TARGET = "target"
ATTR_EXPECTED_REVISION = "expected_revision"

#: Mirrors the backend contract bound. Validating it here means an obviously
#: malformed contract is refused in Home Assistant with a readable message
#: instead of only at the HTTP boundary.
MAX_HEALTH_PROBES = 32
MAX_HEALTH_PROBE_TARGET_LENGTH = 200

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


def _probe_target(value: Any) -> str:
    """Accept one bounded, whitespace-free probe target and nothing else.

    A target is DATA for a fixed argv operation, never command text, so there
    is no shell metacharacter question to answer here -- what matters is that
    it stays one bounded opaque argument.
    """

    if not isinstance(value, str) or not value:
        raise vol.Invalid("health probe target must be a non-empty string")
    if len(value) > MAX_HEALTH_PROBE_TARGET_LENGTH:
        raise vol.Invalid("health probe target is too long")
    if any(character.isspace() for character in value):
        raise vol.Invalid("health probe target must not contain whitespace")
    return value


_PROBE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_KIND): vol.In([kind.value for kind in HealthProbeKind]),
        vol.Required(ATTR_TARGET): _probe_target,
    }
)
_VIEW_HEALTH_CONTRACT_SCHEMA = vol.Schema({vol.Required(ATTR_DEVICE_ID): str})
_SET_HEALTH_CONTRACT_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): str,
        # At least one probe: an empty contract is not "nothing to check",
        # it is a malformed contract, and the operator who wants no contract
        # clears it instead.
        vol.Required(ATTR_PROBES): vol.All(
            [_PROBE_SCHEMA], vol.Length(min=1, max=MAX_HEALTH_PROBES)
        ),
        vol.Optional(ATTR_EXPECTED_REVISION): vol.All(int, vol.Range(min=0)),
    }
)
_CLEAR_HEALTH_CONTRACT_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): str,
        vol.Optional(ATTR_EXPECTED_REVISION): vol.All(int, vol.Range(min=0)),
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
                "architecture": package.architecture,
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


def _health_contract_response(
    resource_id: str, resource_name: str, contract: Any
) -> dict[str, Any]:
    """Render one contract, or an explicit unconfigured state.

    ``probes`` is ``None`` when unconfigured -- never ``[]``. An empty list
    would read as "a contract every workload satisfies", and no configured
    contract is exactly the state in which nothing about this workload's
    health has been declared.
    """

    if contract is None:
        return {
            "resource_id": resource_id,
            "resource_name": resource_name,
            "status": "unconfigured",
            "revision": None,
            "fingerprint": None,
            "created_at": None,
            "updated_at": None,
            "probes": None,
        }
    return {
        "resource_id": resource_id,
        "resource_name": resource_name,
        "status": contract.status.value,
        "revision": contract.revision,
        "fingerprint": contract.fingerprint,
        "created_at": contract.created_at,
        "updated_at": contract.updated_at,
        "probes": [
            {ATTR_KIND: probe.kind.value, ATTR_TARGET: probe.target}
            for probe in contract.probes
        ],
    }


async def _view_health_contract(
    hass: HomeAssistant, call: ServiceCall
) -> ServiceResponse:
    coordinator, resource_id = _coordinator_and_resource_for_device(
        hass, call.data[ATTR_DEVICE_ID]
    )
    name = _resource_display_name(coordinator, resource_id)
    try:
        contract = await coordinator.api.async_fetch_health_contract(resource_id)
    except HubinetOpsHealthContractUnconfigured:
        # Not an error to the operator: it is the answer. Surfacing it as a
        # failure would hide the single most important fact this action can
        # report -- that nobody has said what healthy means here.
        return _health_contract_response(resource_id, name, None)
    except HubinetOpsApiError as exc:
        raise HomeAssistantError(
            "could not read the Hubinet Ops health contract"
        ) from exc
    return _health_contract_response(resource_id, name, contract)


async def _set_health_contract(
    hass: HomeAssistant, call: ServiceCall
) -> ServiceResponse:
    coordinator, resource_id = _coordinator_and_resource_for_device(
        hass, call.data[ATTR_DEVICE_ID]
    )
    name = _resource_display_name(coordinator, resource_id)
    probes = tuple(
        HealthProbe(kind=HealthProbeKind(probe[ATTR_KIND]), target=probe[ATTR_TARGET])
        for probe in call.data[ATTR_PROBES]
    )
    try:
        contract = await coordinator.api.async_replace_health_contract(
            resource_id, probes, call.data.get(ATTR_EXPECTED_REVISION)
        )
    except HubinetOpsApiError as exc:
        raise HomeAssistantError(
            "Hubinet Ops refused the declared health contract"
        ) from exc
    await coordinator.async_request_refresh()
    return _health_contract_response(resource_id, name, contract)


async def _clear_health_contract(
    hass: HomeAssistant, call: ServiceCall
) -> ServiceResponse:
    coordinator, resource_id = _coordinator_and_resource_for_device(
        hass, call.data[ATTR_DEVICE_ID]
    )
    name = _resource_display_name(coordinator, resource_id)
    try:
        await coordinator.api.async_clear_health_contract(
            resource_id, call.data.get(ATTR_EXPECTED_REVISION)
        )
    except HubinetOpsApiError as exc:
        raise HomeAssistantError(
            "Hubinet Ops refused to clear the health contract"
        ) from exc
    await coordinator.async_request_refresh()
    return _health_contract_response(resource_id, name, None)


def _resource_display_name(
    coordinator: HubinetOpsCoordinator, resource_id: str
) -> str:
    resource = coordinator.data.resources_by_id.get(resource_id)
    return resource_device_name(resource) if resource is not None else resource_id


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
    """Register the domain actions exactly once."""

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

    async def view_health_contract_handler(call: ServiceCall) -> ServiceResponse:
        return await _view_health_contract(hass, call)

    async def set_health_contract_handler(call: ServiceCall) -> ServiceResponse:
        return await _set_health_contract(hass, call)

    async def clear_health_contract_handler(call: ServiceCall) -> ServiceResponse:
        return await _clear_health_contract(hass, call)

    for service, handler, schema in (
        (
            SERVICE_VIEW_HEALTH_CONTRACT,
            view_health_contract_handler,
            _VIEW_HEALTH_CONTRACT_SCHEMA,
        ),
        (
            SERVICE_SET_HEALTH_CONTRACT,
            set_health_contract_handler,
            _SET_HEALTH_CONTRACT_SCHEMA,
        ),
        (
            SERVICE_CLEAR_HEALTH_CONTRACT,
            clear_health_contract_handler,
            _CLEAR_HEALTH_CONTRACT_SCHEMA,
        ),
    ):
        hass.services.async_register(
            DOMAIN,
            service,
            handler,
            schema=schema,
            supports_response=SupportsResponse.OPTIONAL,
        )
    domain_data[DATA_SERVICES_REGISTERED] = True


def async_unload_services(hass: HomeAssistant) -> None:
    """Remove domain actions after the final config entry unloads."""

    domain_data = hass.data.get(DOMAIN, {})
    if not domain_data.pop(DATA_SERVICES_REGISTERED, False):
        return
    for service in (
        SERVICE_VIEW_UPDATE_PLAN,
        SERVICE_APPROVE_UPDATE_PLAN,
        SERVICE_VIEW_HEALTH_CONTRACT,
        SERVICE_SET_HEALTH_CONTRACT,
        SERVICE_CLEAR_HEALTH_CONTRACT,
    ):
        hass.services.async_remove(DOMAIN, service)
