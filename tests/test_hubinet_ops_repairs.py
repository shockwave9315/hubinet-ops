"""The health-contract Repair after the v0.5 simplification.

Human1 defect A was an approved plan stuck on an unconfigured health
contract, with no discoverable continuation. v0.5 removes that dead end at
its source: the backend gives every current package-managed LXC a built-in
``guest_operational`` contract, so a normally managed resource never reaches
the state this Repair describes.

What is left is a narrow, NON-fixable safety net for the state that can still
occur (an operator used the low-level clear API). These tests pin exactly
that: the issue is still raised and still cleared correctly, it is NOT
fixable, this module exposes no discovery/candidate flow at all, and the
strings it renders point at the two explicit operator surfaces instead.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("homeassistant", reason="isolated HA test dependencies not installed")

from homeassistant.core import HomeAssistant

from custom_components.hubinet_ops import repairs as repairs_module
from custom_components.hubinet_ops.api import (
    HealthContractStatus,
    PackagePlanApprovalStatus,
)

from tests.test_hubinet_ops_integration import (
    FakeTransport,
    RESOURCE_CT,
    _health_contract_repair_ids,
    exact_plan_resource,
    setup_entry,
    snapshot,
)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations, socket_enabled):
    """Load custom integrations; fake transports never perform network I/O."""

    yield


async def _setup_blocked_resource(
    hass: HomeAssistant, **transport_kwargs
) -> tuple[FakeTransport, str]:
    """Bring up one entry with the approved/unconfigured state.

    Returns ``(transport, issue_id)``.
    """

    planned = exact_plan_resource(approved=True)
    assert planned.resource_id == RESOURCE_CT
    assert planned.package_plan_approval.status is PackagePlanApprovalStatus.APPROVED
    assert planned.health_contract.status is HealthContractStatus.UNCONFIGURED

    transport = FakeTransport([snapshot((planned,))], **transport_kwargs)
    await setup_entry(hass, transport)
    (issue_id,) = _health_contract_repair_ids(hass)
    return transport, issue_id


@pytest.mark.asyncio
async def test_the_repair_is_raised_but_is_not_fixable(hass: HomeAssistant) -> None:
    """The remedy is a backend product default plus two explicit operator
    surfaces -- never a Home-Assistant-side discovery flow."""

    from homeassistant.helpers import issue_registry as ir

    _transport, issue_id = await _setup_blocked_resource(hass)
    issue = ir.async_get(hass).async_get_issue("hubinet_ops", issue_id)

    assert issue is not None
    assert issue.is_fixable is False
    assert issue.severity is ir.IssueSeverity.WARNING
    assert issue.translation_key == "health_contract_unconfigured"
    assert issue.translation_placeholders == {"name": "CT101 Cloudflared"}


def test_the_repairs_module_exposes_no_discovery_or_fix_flow() -> None:
    """Load-bearing: the removed `discover -> render -> confirm -> declare`
    surface must be gone, not merely unreachable.

    Leaving it behind would advertise automatic workload discovery this
    product deliberately does not do -- absence of a workload observer is not
    proof of workload absence, so v0.5 infers no workload health at all.
    """

    assert not hasattr(repairs_module, "async_create_fix_flow")
    assert not hasattr(repairs_module, "HealthContractDiscoveryFixFlow")
    assert not hasattr(repairs_module, "_candidate_field")

    source = Path(repairs_module.__file__).read_text(encoding="utf-8")
    for marker in (
        "async_fetch_health_candidates",
        "HealthDiscovery",
        "UNDECIDED_DISCOVERY_STATUSES",
        "RepairsFlow",
        "docker ps",
        "systemctl",
        "command -v",
    ):
        assert marker not in source, marker


def test_the_issue_translations_are_structural_and_carry_no_fix_flow() -> None:
    """Every string this issue renders must resolve in both English and
    Polish -- mirroring the existing HUMAN1-ERROR-I18N-01 discipline."""

    integration_root = Path(__file__).parents[1] / "custom_components" / "hubinet_ops"
    strings = json.loads((integration_root / "strings.json").read_text(encoding="utf-8"))
    english = json.loads(
        (integration_root / "translations" / "en.json").read_text(encoding="utf-8")
    )
    polish = json.loads(
        (integration_root / "translations" / "pl.json").read_text(encoding="utf-8")
    )

    assert strings["issues"] == english["issues"]
    assert set(strings["issues"]) == set(polish["issues"])

    for catalogue in (strings, polish):
        issue = catalogue["issues"]["health_contract_unconfigured"]
        # A non-fixable issue has no flow to translate, and leaving a dead
        # `fix_flow` block behind would still advertise one in the UI.
        assert "fix_flow" not in issue
        assert set(issue) == {"title", "description"}
        assert "{name}" in issue["title"]
        assert "{name}" in issue["description"]
        # It points at the two explicit surfaces, and never offers to find
        # a workload for the operator. ("inventory discovery" is the PVE
        # resource scan that restores the default -- a different thing.)
        assert "reset_health_contract" in issue["description"]
        assert "set_health_contract" in issue["description"]
        for banned in (
            "candidate",
            "kandydat",
            "discover candidates",
            "Select **Fix**",
        ):
            assert banned not in issue["description"]
