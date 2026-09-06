"""Native Home Assistant Repairs for a discoverable health-contract gap.

Human1 live defect A: an operator who reviews and approves a real package
plan reaches a disabled **Start approved update** button, presses
**View health contract**, and correctly learns the resource is
*unconfigured* -- but the native per-resource device workflow has no
discoverable continuation from there. The only existing way to declare one is
the ``hubinet_ops.set_health_contract`` action, which nothing in the device
workflow points to; an operator without a manual pointer to it, or to
Developer Tools -> Actions, has no supported next step.

Repairs (Settings -> Repairs, and the notification bell) is the one HA-native
surface visible from anywhere in the UI without reading source code or
guessing a service/action name, so this module raises one bounded, typed
issue per resource that is genuinely blocked on it: an approved plan with an
unconfigured health contract, which is exactly the dead end Human1 observed.

Stage 4 (v20) turns this issue **fixable**: the fix flow calls the backend's
ephemeral candidate-discovery read (Docker/systemd inspection lives entirely
on the backend; this module never touches a container or a unit file), shows
what it found, and requires the operator to explicitly confirm -- with every
non-recommended candidate unchecked by default -- before anything is
written. Declaring what "healthy" means for a workload stays an explicit
operator decision exactly as PRODUCT.md requires: discovery only narrows
what is *shown*, it never chooses on the operator's behalf, and the write
itself goes through the exact same `async_replace_health_contract` mutation
the `set_health_contract` action already uses. That action, reachable from
this device's **Actions** tab or Developer Tools -> Actions, remains a
supported alternative path for an operator who would rather type the
contract by hand; the issue's own description still names it.

This grants no authority and stores nothing durable of its own: every issue
here is recomputed from the same already-fetched, already-validated snapshot
on every coordinator refresh, never persisted state of its own, and
disappears the moment the resource stops qualifying (contract configured,
approval no longer approved, or the resource itself is gone) -- or the moment
the fix flow below successfully declares one.
"""

from __future__ import annotations

from homeassistant.components.repairs import RepairsFlow, RepairsFlowResult
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
import voluptuous as vol

from .api import (
    HealthContractStatus,
    HealthDiscoveryCandidate,
    HealthDiscoveryStatus,
    HealthProbe,
    HealthProbeKind,
    HubinetOpsApiError,
    HubinetOpsSnapshot,
    PackagePlanApprovalStatus,
    ResourceType,
)
from .const import DATA_COORDINATORS, DOMAIN

#: Stable prefix so a resource's issue id can never collide with an issue
#: raised for an unrelated reason, and so it is trivially recognizable in the
#: repairs UI/registry as this one family.
_ISSUE_PREFIX = "health_contract_unconfigured"

#: Discovery statuses that carry no usable candidate at all -- the fix flow
#: aborts rather than showing an empty or meaningless form. The operator's
#: only path forward on any of these is the manual `set_health_contract`
#: action, exactly as before Stage 4.
_UNDECIDED_DISCOVERY_STATUSES = frozenset(
    {
        HealthDiscoveryStatus.GUEST_UNAVAILABLE,
        HealthDiscoveryStatus.UNDECIDABLE,
        HealthDiscoveryStatus.TOO_MANY_CANDIDATES,
    }
)


def _issue_id(entry_id: str, resource_id: str) -> str:
    return f"{_ISSUE_PREFIX}::{entry_id}::{resource_id}"


def _resource_display_name(resource) -> str:
    """Mirror ``coordinator.resource_device_name`` without importing it.

    ``coordinator.py`` imports this module to drive the sync below, so
    importing back from it would be circular; this is the exact same
    deliberate, independently-tested duplication this codebase already uses
    for other cross-module invariants (e.g. `_RESUME_CAPABLE_CHECKPOINTS`).
    """

    prefix = "VM" if resource.resource_type is ResourceType.QEMU else "CT"
    return f"{prefix}{resource.vmid} {resource.name}"


def async_sync_health_contract_repairs(
    hass: HomeAssistant,
    *,
    entry_id: str,
    snapshot: HubinetOpsSnapshot,
    previously_raised_resource_ids: frozenset[str],
) -> frozenset[str]:
    """Raise or clear the "approved but unconfigured" repair per resource.

    Called once per coordinator refresh with the just-validated snapshot.
    Returns the resource ids that currently carry the issue; the caller must
    remember this and pass it back as ``previously_raised_resource_ids`` on
    the next call so a resource that stops qualifying (contract configured,
    approval no longer approved, resource retired/removed) gets its issue
    cleared rather than left behind forever.
    """

    currently_blocked: set[str] = set()
    for resource in snapshot.resources:
        if resource.resource_type is not ResourceType.LXC:
            continue
        if (
            resource.package_plan_approval.status
            is PackagePlanApprovalStatus.APPROVED
            and resource.health_contract.status is HealthContractStatus.UNCONFIGURED
        ):
            currently_blocked.add(resource.resource_id)
            ir.async_create_issue(
                hass,
                DOMAIN,
                _issue_id(entry_id, resource.resource_id),
                is_fixable=True,
                severity=ir.IssueSeverity.WARNING,
                translation_key=_ISSUE_PREFIX,
                translation_placeholders={"name": _resource_display_name(resource)},
                data={"entry_id": entry_id, "resource_id": resource.resource_id},
            )

    for resource_id in previously_raised_resource_ids - currently_blocked:
        ir.async_delete_issue(hass, DOMAIN, _issue_id(entry_id, resource_id))

    return frozenset(currently_blocked)


def async_clear_health_contract_repairs(
    hass: HomeAssistant, *, entry_id: str, resource_ids: frozenset[str]
) -> None:
    """Remove every repair issue this entry currently owns, e.g. on unload."""

    for resource_id in resource_ids:
        ir.async_delete_issue(hass, DOMAIN, _issue_id(entry_id, resource_id))


def _candidate_field(candidate: HealthDiscoveryCandidate) -> str:
    """One stable, readable form-field name per candidate identity.

    Mirrors the backend's own duplicate-identity rule, `(kind, target)`: the
    only candidate kind this can ever collide on is `guest_operational`,
    which a resource can have at most one of -- exactly the one case handled
    with a fixed literal instead of a target.
    """

    if candidate.kind is HealthProbeKind.GUEST_OPERATIONAL:
        return "probe__guest_operational"
    return f"probe__{candidate.kind.value}__{candidate.target}"


def _candidate_description_line(candidate: HealthDiscoveryCandidate) -> str:
    target = candidate.target if candidate.target is not None else (
        "(no target -- guest-level fallback)"
    )
    flag = "recommended" if candidate.recommended else "not recommended"
    return f"- **{candidate.kind.value}** `{target}` -- {flag}: {candidate.rationale}"


class HealthContractDiscoveryFixFlow(RepairsFlow):
    """Discover -> render -> confirm -> declare. Never automatic.

    Every candidate this flow can possibly write was returned by the
    backend's own ephemeral discovery read for this exact resource, and the
    operator explicitly selected it in the confirm step below -- discovery
    only narrows what is shown, it never chooses on the operator's behalf,
    and nothing reaches the backend as a declared contract until the
    operator submits that form. This flow persists nothing of its own; it
    only ever calls the same typed mutation the `set_health_contract` action
    already uses.
    """

    _candidates: tuple[HealthDiscoveryCandidate, ...] = ()
    _resource_name: str = ""

    def _entry_and_resource_id(self) -> tuple[str, str] | None:
        data = self.data
        if not isinstance(data, dict):
            return None
        entry_id = data.get("entry_id")
        resource_id = data.get("resource_id")
        if not isinstance(entry_id, str) or not isinstance(resource_id, str):
            return None
        return entry_id, resource_id

    async def async_step_init(
        self, user_input: dict[str, bool] | None = None
    ) -> RepairsFlowResult:
        identity = self._entry_and_resource_id()
        if identity is None:
            return self.async_abort(reason="resource_not_found")
        entry_id, resource_id = identity

        coordinators = self.hass.data.get(DOMAIN, {}).get(DATA_COORDINATORS, {})
        coordinator = coordinators.get(entry_id)
        if coordinator is None:
            return self.async_abort(reason="entry_not_loaded")

        resource = coordinator.data.resources_by_id.get(resource_id)
        if resource is None:
            return self.async_abort(reason="resource_not_found")
        self._resource_name = _resource_display_name(resource)

        try:
            result = await coordinator.api.async_fetch_health_candidates(resource_id)
        except HubinetOpsApiError:
            return self.async_abort(reason="discovery_failed")

        if result.status in _UNDECIDED_DISCOVERY_STATUSES:
            return self.async_abort(reason=result.status.value)
        if not result.candidates:
            return self.async_abort(reason="no_candidates_discovered")

        self._candidates = result.candidates
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, bool] | None = None
    ) -> RepairsFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            selected = tuple(
                candidate
                for candidate in self._candidates
                if user_input.get(_candidate_field(candidate))
            )
            if not selected:
                errors["base"] = "no_candidates_selected"
            else:
                identity = self._entry_and_resource_id()
                assert identity is not None  # proven in async_step_init
                entry_id, resource_id = identity
                coordinator = self.hass.data[DOMAIN][DATA_COORDINATORS][entry_id]
                probes = tuple(
                    HealthProbe(kind=candidate.kind, target=candidate.target)
                    for candidate in selected
                )
                try:
                    await coordinator.api.async_replace_health_contract(
                        resource_id, probes, None
                    )
                except HubinetOpsApiError:
                    errors["base"] = "declare_failed"
                else:
                    await coordinator.async_request_refresh()
                    return self.async_create_entry(data={})

        schema = vol.Schema(
            {
                vol.Optional(
                    _candidate_field(candidate), default=candidate.recommended
                ): bool
                for candidate in self._candidates
            }
        )
        candidates_text = "\n".join(
            _candidate_description_line(candidate) for candidate in self._candidates
        )
        return self.async_show_form(
            step_id="confirm",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "name": self._resource_name,
                "candidates": candidates_text,
            },
        )


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, str | int | float | None] | None,
) -> RepairsFlow:
    """Create the discovery fix flow for one blocked resource's issue.

    `issue_id` is not parsed here: the flow manager attaches the issue's own
    `data` (`entry_id`/`resource_id`) to the returned flow right after this
    call returns, and the flow reads it from there in `async_step_init` --
    the same identity `async_sync_health_contract_repairs` recorded when it
    raised this issue.
    """

    return HealthContractDiscoveryFixFlow()
