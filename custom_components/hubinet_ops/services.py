"""Native Home Assistant actions for the operator update lifecycle.

Two families live here. The review/configuration actions
(``view_update_plan``, ``approve_update_plan``, ``view_health_contract``,
``set_health_contract``, ``clear_health_contract``) read or change authority
*metadata* and change no workload. The execution actions (``start_update``,
``view_update_job``, ``resume_update``, ``rollback_update``) are explicit
operator controls over the production update lifecycle.

Every one of them runs because a person invoked it. **None of them is
reachable from the coordinator.** The coordinator polls a published snapshot
and nothing else; it has no reference to any function in this module, and an
update can therefore never begin as a side effect of Home Assistant
refreshing.

The device selector is the existing dynamic resource-device model, unchanged.
There is no second resource-selection system here. Update-execution actions
carry no VMID, node, package, version, snapshot, probe, command, argv, or
helper operation: ``start_update`` sends one generated ``request_id`` and
``rollback_update`` sends nothing at all, because the backend resolves the
rest from durable authority.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
import uuid

from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
import voluptuous as vol

from .api import (
    DEFAULT_PACKAGE_UPDATE_EVENTS,
    HealthProbe,
    HealthProbeKind,
    HubinetOpsApiError,
    HubinetOpsHealthContractUnconfigured,
    PackageScanStatus,
    PackageUpdateJobView,
)
from .const import (
    DATA_COORDINATORS,
    DATA_SERVICES_REGISTERED,
    DOMAIN,
    SERVICE_APPROVE_UPDATE_PLAN,
    SERVICE_CLEAR_HEALTH_CONTRACT,
    SERVICE_RESUME_UPDATE,
    SERVICE_ROLLBACK_UPDATE,
    SERVICE_SET_HEALTH_CONTRACT,
    SERVICE_START_UPDATE,
    SERVICE_VIEW_HEALTH_CONTRACT,
    SERVICE_VIEW_UPDATE_JOB,
    SERVICE_VIEW_UPDATE_PLAN,
)
from .coordinator import (
    HubinetOpsCoordinator,
    ReviewedUpdatePlanReference,
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
ATTR_EVENTS = "events"

#: Mirrors the backend contract bound. Validating it here means an obviously
#: malformed contract is refused in Home Assistant with a readable message
#: instead of only at the HTTP boundary.
MAX_HEALTH_PROBES = 32
MAX_HEALTH_PROBE_TARGET_LENGTH = 200

_VIEW_SCHEMA = vol.Schema(
    {vol.Required(ATTR_DEVICE_ID): str}
)
_APPROVE_SCHEMA = vol.Schema({vol.Required(ATTR_DEVICE_ID): str})


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


def _require_probe_target_matches_kind(probe: dict[str, Any]) -> dict[str, Any]:
    """``target`` is required for every kind except one.

    ``guest_operational`` (v20) names no container or unit -- it must not
    carry a target at all, never a faked one (``"guest"``, a VMID string).
    """

    kind = probe[ATTR_KIND]
    target = probe.get(ATTR_TARGET)
    if kind == HealthProbeKind.GUEST_OPERATIONAL.value:
        if target is not None:
            raise vol.Invalid(
                "a guest_operational health probe must not carry a target"
            )
    elif target is None:
        raise vol.Invalid("a target is required for this probe kind")
    return probe


_PROBE_SCHEMA = vol.All(
    vol.Schema(
        {
            vol.Required(ATTR_KIND): vol.In(
                [kind.value for kind in HealthProbeKind]
            ),
            vol.Optional(ATTR_TARGET, default=None): vol.Any(None, _probe_target),
        }
    ),
    _require_probe_target_matches_kind,
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

# The explicit operator update controls. `device_id` is the ONLY field on
# three of the four, and the fourth adds a bounded event count. There is
# deliberately nowhere here to name a VMID, a package, a snapshot, a probe,
# or a command: a schema that accepted one would be a schema through which
# Home Assistant could decide something the backend authority owns.
_START_UPDATE_SCHEMA = vol.Schema({vol.Required(ATTR_DEVICE_ID): str})
_VIEW_UPDATE_JOB_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): str,
        vol.Optional(ATTR_EVENTS): vol.All(int, vol.Range(min=0, max=200)),
    }
)
_RESUME_UPDATE_SCHEMA = vol.Schema({vol.Required(ATTR_DEVICE_ID): str})
_ROLLBACK_UPDATE_SCHEMA = vol.Schema({vol.Required(ATTR_DEVICE_ID): str})


def _coordinator_and_resource_for_device(
    hass: HomeAssistant, device_id: str
) -> tuple[HubinetOpsCoordinator, str]:
    """Resolve one selected HA device to exactly one loaded Hubinet resource."""

    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        raise HomeAssistantError(
            "selected Hubinet Ops resource device does not exist",
            translation_domain=DOMAIN,
            translation_key="device_not_found",
        )

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
            "selected device must identify exactly one loaded Hubinet Ops resource",
            translation_domain=DOMAIN,
            translation_key="device_not_unique_resource",
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


async def async_review_update_plan(
    coordinator: HubinetOpsCoordinator, resource_id: str
) -> ServiceResponse:
    """Fresh-read and remember exactly the plan the operator reviewed."""

    try:
        snapshot = await coordinator.api.async_fetch_resource_snapshot()
    except HubinetOpsApiError as exc:
        raise HomeAssistantError(
            "could not read the current Hubinet Ops plan",
            translation_domain=DOMAIN,
            translation_key="review_failed",
        ) from exc
    if snapshot.backend.backend_instance_id != coordinator.config_entry.unique_id:
        raise HomeAssistantError(
            "backend identity changed during plan review",
            translation_domain=DOMAIN,
            translation_key="backend_changed_during_review",
        )
    resource = snapshot.resources_by_id.get(resource_id)
    if resource is None:
        raise HomeAssistantError(
            "resource is absent from the fresh backend snapshot",
            translation_domain=DOMAIN,
            translation_key="resource_absent_during_review",
        )

    scan = resource.package_scan
    approval = resource.package_plan_approval
    approvable = bool(
        approval.approvable
        and scan.status is PackageScanStatus.SUCCESS
        and scan.pending_count is not None
        and scan.pending_count > 0
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
    if reference is None:
        coordinator.forget_reviewed_update_plan(resource_id)
    else:
        coordinator.remember_reviewed_update_plan(
            ReviewedUpdatePlanReference(
                backend_instance_id=snapshot.backend.backend_instance_id,
                resource_id=resource.resource_id,
                scan_run_id=scan.scan_run_id,
                plan_fingerprint=scan.plan_fingerprint,
            )
        )
    return {
        "resource_id": resource.resource_id,
        "resource_name": resource_device_name(resource),
        "approvable": approvable,
        "scan_status": scan.status.value,
        "scan_run_id": scan.scan_run_id if approvable else None,
        "plan_fingerprint": scan.plan_fingerprint if approvable else None,
        "pending_count": scan.pending_count,
        "post_update_scan_pending": scan.post_update_scan_pending,
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


async def _view_update_plan(
    hass: HomeAssistant, call: ServiceCall
) -> ServiceResponse:
    coordinator, resource_id = _coordinator_and_resource_for_device(
        hass, call.data[ATTR_DEVICE_ID]
    )
    return await async_review_update_plan(coordinator, resource_id)


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


async def async_view_health_contract(
    coordinator: HubinetOpsCoordinator, resource_id: str
) -> ServiceResponse:
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
            "could not read the Hubinet Ops health contract",
            translation_domain=DOMAIN,
            translation_key="health_contract_read_failed",
        ) from exc
    return _health_contract_response(resource_id, name, contract)


async def _view_health_contract(
    hass: HomeAssistant, call: ServiceCall
) -> ServiceResponse:
    coordinator, resource_id = _coordinator_and_resource_for_device(
        hass, call.data[ATTR_DEVICE_ID]
    )
    return await async_view_health_contract(coordinator, resource_id)


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
            "Hubinet Ops refused the declared health contract",
            translation_domain=DOMAIN,
            translation_key="health_contract_set_refused",
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
            "Hubinet Ops refused to clear the health contract",
            translation_domain=DOMAIN,
            translation_key="health_contract_clear_refused",
        ) from exc
    await coordinator.async_request_refresh()
    return _health_contract_response(resource_id, name, None)


def _resource_display_name(
    coordinator: HubinetOpsCoordinator, resource_id: str
) -> str:
    resource = coordinator.data.resources_by_id.get(resource_id)
    return resource_device_name(resource) if resource is not None else resource_id


async def async_approve_reviewed_update_plan(
    coordinator: HubinetOpsCoordinator, resource_id: str
) -> None:
    """Approve only the exact fresh plan remembered by a prior review."""

    reference = coordinator.reviewed_update_plan(resource_id)
    if reference is None:
        raise HomeAssistantError(
            "review the current update plan before approving it",
            translation_domain=DOMAIN,
            translation_key="approve_without_review",
        )

    try:
        fresh = await coordinator.api.async_fetch_resource_snapshot()
    except HubinetOpsApiError as exc:
        coordinator.forget_reviewed_update_plan(resource_id)
        raise HomeAssistantError(
            "could not revalidate the reviewed Hubinet Ops plan",
            translation_domain=DOMAIN,
            translation_key="approve_revalidation_failed",
        ) from exc
    resource = fresh.resources_by_id.get(resource_id)
    scan = None if resource is None else resource.package_scan
    if (
        fresh.backend.backend_instance_id != reference.backend_instance_id
        or fresh.backend.backend_instance_id != coordinator.config_entry.unique_id
        or resource is None
        or not resource.package_plan_approval.approvable
        or scan.status is not PackageScanStatus.SUCCESS
        or scan.pending_count is None
        or scan.pending_count < 1
        or scan.scan_run_id != reference.scan_run_id
        or scan.plan_fingerprint != reference.plan_fingerprint
    ):
        coordinator.forget_reviewed_update_plan(resource_id)
        raise HomeAssistantError(
            "the exact reviewed plan changed; review the update plan again",
            translation_domain=DOMAIN,
            translation_key="plan_changed",
        )

    # Consume the UI reference before the potentially uncertain network
    # mutation. A timeout must never turn a second press into an automatic
    # replay of an approval whose result HA could not observe.
    coordinator.forget_reviewed_update_plan(resource_id)
    try:
        await coordinator.api.async_approve_package_plan(
            resource_id, reference.scan_run_id, reference.plan_fingerprint
        )
    except HubinetOpsApiError as exc:
        raise HomeAssistantError(
            "Hubinet Ops refused the reviewed update plan",
            translation_domain=DOMAIN,
            translation_key="approve_refused",
        ) from exc
    await coordinator.async_request_refresh()


async def _approve_update_plan(hass: HomeAssistant, call: ServiceCall) -> None:
    coordinator, resource_id = _coordinator_and_resource_for_device(
        hass, call.data[ATTR_DEVICE_ID]
    )
    await async_approve_reviewed_update_plan(coordinator, resource_id)


# ---------------------------------------------------------------------------
# Explicit operator update controls.
# ---------------------------------------------------------------------------


def _job_response(
    resource_id: str, resource_name: str, job: PackageUpdateJobView
) -> dict[str, Any]:
    """Render one job as an action response.

    Response data, never entity state. Bounded to durable authority facts: no
    helper output, no PVE task log, no command text, and no package rows.
    ``health_outcome`` is ``None`` when no definitive verdict has been
    recorded, and ``None`` is emphatically not a pass. ``health_probes`` IS
    included (post-Human1 correction): each frozen probe's kind, target,
    outcome, checked-at, and bounded reason token, so a FAILED or UNKNOWN
    health result is actionable from Home Assistant without shell/SQLite
    access. It stays empty until a definitive verdict exists.
    """

    return {
        "resource_id": resource_id,
        "resource_name": resource_name,
        "job_id": job.job_id,
        "request_id": job.request_id,
        "status": job.status.value,
        "checkpoint": job.checkpoint,
        "issued_at": job.issued_at,
        "plan_fingerprint": job.approved_plan_fingerprint,
        "package_count": job.package_count,
        "snapshot_name": job.snapshot_name,
        "snapshot_confirmed_at": job.snapshot_confirmed_at,
        "mutation_may_have_started_at": job.mutation_may_have_started_at,
        "mutation_completed_at": job.mutation_completed_at,
        "health_contract_revision": job.health_contract_revision,
        "health_started_at": job.health_started_at,
        "health_completed_at": job.health_completed_at,
        "health_outcome": (
            None if job.health_outcome is None else job.health_outcome.value
        ),
        "health_evidence": job.health_evidence,
        "rollback_may_have_started_at": job.rollback_may_have_started_at,
        "rollback_completed_at": job.rollback_completed_at,
        "rollback_available": job.rollback_available,
        "terminalized_at": job.terminalized_at,
        "terminal_reason": job.terminal_reason,
        "events": [
            {
                "sequence": event.sequence,
                "created_at": event.created_at,
                "level": event.level,
                "stage": event.stage,
                "event_type": event.event_type,
                "message": event.message,
            }
            for event in job.events
        ],
        "health_probes": [
            {
                "index": probe.probe_index,
                "kind": probe.kind.value,
                "target": probe.target,
                "outcome": probe.outcome.value,
                "checked_at": probe.checked_at,
                "reason": probe.reason,
                "definitive": probe.definitive,
            }
            for probe in job.health_probes
        ],
    }


async def async_start_update(
    coordinator: HubinetOpsCoordinator, resource_id: str
) -> ServiceResponse:
    """Explicitly start the currently approved update for one resource.

    The ``request_id`` is generated HERE, once per invocation, and is the only
    caller-controlled value the whole lifecycle accepts. Home Assistant does
    not choose what gets installed: the backend resolves the resource's own
    current durable approval, and a plan that drifted since it was approved
    is refused there rather than negotiated here.
    """

    name = _resource_display_name(coordinator, resource_id)
    try:
        job = await coordinator.api.async_start_package_update(
            resource_id, str(uuid.uuid4())
        )
    except HubinetOpsApiError as exc:
        raise HomeAssistantError(
            "Hubinet Ops refused to start the package update",
            translation_domain=DOMAIN,
            translation_key="start_refused",
        ) from exc
    await coordinator.async_request_refresh()
    return _job_response(resource_id, name, job)


async def _start_update(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    coordinator, resource_id = _coordinator_and_resource_for_device(
        hass, call.data[ATTR_DEVICE_ID]
    )
    return await async_start_update(coordinator, resource_id)


async def async_view_update_job(
    coordinator: HubinetOpsCoordinator,
    resource_id: str,
    events: int = DEFAULT_PACKAGE_UPDATE_EVENTS,
) -> ServiceResponse:
    name = _resource_display_name(coordinator, resource_id)
    try:
        job = await coordinator.api.async_fetch_package_update(
            resource_id, events
        )
    except HubinetOpsApiError as exc:
        raise HomeAssistantError(
            "could not read the Hubinet Ops package update job",
            translation_domain=DOMAIN,
            translation_key="view_job_failed",
        ) from exc
    return _job_response(resource_id, name, job)


async def _view_update_job(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    coordinator, resource_id = _coordinator_and_resource_for_device(
        hass, call.data[ATTR_DEVICE_ID]
    )
    return await async_view_update_job(
        coordinator,
        resource_id,
        call.data.get(ATTR_EVENTS, DEFAULT_PACKAGE_UPDATE_EVENTS),
    )


async def async_resume_update(
    coordinator: HubinetOpsCoordinator, resource_id: str
) -> ServiceResponse:
    """Ask the backend to re-enter an existing recoverable job.

    Never "run the update again": the backend re-reads the durable checkpoint
    and invokes only the existing safe continuation semantics for it.
    """

    name = _resource_display_name(coordinator, resource_id)
    try:
        job = await coordinator.api.async_resume_package_update(resource_id)
    except HubinetOpsApiError as exc:
        raise HomeAssistantError(
            "Hubinet Ops refused to resume the package update job",
            translation_domain=DOMAIN,
            translation_key="resume_refused",
        ) from exc
    await coordinator.async_request_refresh()
    return _job_response(resource_id, name, job)


async def _resume_update(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    coordinator, resource_id = _coordinator_and_resource_for_device(
        hass, call.data[ATTR_DEVICE_ID]
    )
    return await async_resume_update(coordinator, resource_id)


async def async_rollback_update(
    coordinator: HubinetOpsCoordinator, resource_id: str
) -> ServiceResponse:
    """Explicitly roll one resource back to its own job's snapshot.

    The operator selects a resource. No snapshot is named here, and none can
    be: the backend resolves the one applicable active job and reuses the
    same-job rollback contract, which derives the target from durable
    authority and refuses if a fresh canonical listing does not prove exactly
    one snapshot owned by that job.
    """

    name = _resource_display_name(coordinator, resource_id)
    try:
        job = await coordinator.api.async_rollback_package_update(resource_id)
    except HubinetOpsApiError as exc:
        raise HomeAssistantError(
            "Hubinet Ops refused the same-job rollback request",
            translation_domain=DOMAIN,
            translation_key="rollback_refused",
        ) from exc
    await coordinator.async_request_refresh()
    return _job_response(resource_id, name, job)


async def _rollback_update(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    coordinator, resource_id = _coordinator_and_resource_for_device(
        hass, call.data[ATTR_DEVICE_ID]
    )
    return await async_rollback_update(coordinator, resource_id)


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

    async def start_update_handler(call: ServiceCall) -> ServiceResponse:
        return await _start_update(hass, call)

    async def view_update_job_handler(call: ServiceCall) -> ServiceResponse:
        return await _view_update_job(hass, call)

    async def resume_update_handler(call: ServiceCall) -> ServiceResponse:
        return await _resume_update(hass, call)

    async def rollback_update_handler(call: ServiceCall) -> ServiceResponse:
        return await _rollback_update(hass, call)

    # Response-capable, because every one of them answers with the job it
    # acted on -- an operator who starts, resumes, or rolls back an update
    # needs to see what the backend actually did, in the same call.
    for service, handler, schema in (
        (SERVICE_START_UPDATE, start_update_handler, _START_UPDATE_SCHEMA),
        (SERVICE_VIEW_UPDATE_JOB, view_update_job_handler, _VIEW_UPDATE_JOB_SCHEMA),
        (SERVICE_RESUME_UPDATE, resume_update_handler, _RESUME_UPDATE_SCHEMA),
        (SERVICE_ROLLBACK_UPDATE, rollback_update_handler, _ROLLBACK_UPDATE_SCHEMA),
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
        SERVICE_START_UPDATE,
        SERVICE_VIEW_UPDATE_JOB,
        SERVICE_RESUME_UPDATE,
        SERVICE_ROLLBACK_UPDATE,
    ):
        hass.services.async_remove(DOMAIN, service)
