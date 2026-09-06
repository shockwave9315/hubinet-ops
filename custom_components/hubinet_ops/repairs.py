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
guessing a service/action name, so this module raises one bounded, typed,
non-fixable issue per resource that is genuinely blocked on it: an approved
plan with an unconfigured health contract, which is exactly the dead end
Human1 observed. It is deliberately **not fixable through a flow**: fixing it
means declaring what "healthy" means for this specific workload, and
PRODUCT.md requires that stay an explicit operator decision, never one this
integration infers or fills in on the operator's behalf. The issue's own
description names the existing `set_health_contract` action and its required
shape, so following it does not require reading source code -- but the
action itself remains the single place a contract is declared, exactly as
before.

This grants no authority and stores nothing durable: every issue here is
recomputed from the same already-fetched, already-validated snapshot on every
coordinator refresh, never persisted state of its own, and disappears the
moment the resource stops qualifying (contract configured, approval no longer
approved, or the resource itself is gone).
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
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=_ISSUE_PREFIX,
                translation_placeholders={"name": _resource_display_name(resource)},
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
