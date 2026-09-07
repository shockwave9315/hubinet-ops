"""A native Home Assistant Repair for the one health state that blocks Start.

Human1 live defect A was an operator who reviewed and approved a real package
plan, reached a disabled **Start approved update** button, pressed
**View health contract**, correctly learned the resource was *unconfigured*,
and had no discoverable continuation from there.

v0.5 removes that dead end at its source rather than papering over it: the
backend provisions the built-in ``guest_operational`` default for every
current package-managed LXC during reconciliation, so a normally managed
resource is already health-configured before its first update and needs no
onboarding click at all. There is deliberately no discovery/candidate flow
here any more -- Home Assistant does not inspect Docker or systemd, does not
rank workloads, and does not decide what "healthy" means. Absence of a
workload observer is not proof of workload absence, so v0.5 does not infer
workload health automatically.

What remains is a narrow, NON-fixable safety net. A resource can still be
genuinely unconfigured -- an operator used the low-level clear API -- and if
that resource also has an approved plan, Start is blocked and the operator
deserves to be told where to fix it: Settings -> Devices & Services ->
Hubinet Ops -> Configure (restore the built-in default), or the
``hubinet_ops.set_health_contract`` action for an explicit advanced contract.
The next successful inventory reconciliation restores the default on its own,
so this issue is normally transient as well as rare.

This grants no authority and stores nothing durable of its own: every issue
here is recomputed from the same already-fetched, already-validated snapshot
on every coordinator refresh, never persisted state of its own, and
disappears the moment the resource stops qualifying (contract configured,
approval no longer approved, or the resource itself is gone).
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .api import (
    HealthContractStatus,
    HubinetOpsSnapshot,
    PackagePlanApprovalStatus,
    ResourceType,
)
from .const import DOMAIN

#: Stable prefix so a resource's issue id can never collide with an issue
#: raised for an unrelated reason, and so it is trivially recognizable in the
#: repairs UI/registry as this one family.
_ISSUE_PREFIX = "health_contract_unconfigured"


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
                # Not fixable from here on purpose: the remedy is a backend
                # product default plus two existing explicit operator
                # surfaces, none of which needs a bespoke flow -- and none of
                # which may become guest inspection.
                is_fixable=False,
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
