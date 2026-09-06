"""Stage 4 (v20): the health-contract Repair as a fixable discovery flow.

Human1 defect A's issue (an approved plan stuck on an unconfigured health
contract) is raised exactly as before -- see test_hubinet_ops_integration.py.
These tests cover what changed: the issue is now `is_fixable=True`, and
fixing it drives `discover -> render -> confirm -> declare` entirely through
native Home Assistant Repairs, with zero YAML, zero Developer Tools, and zero
manually-typed probe rows. Discovery is ephemeral throughout: nothing this
flow does persists a candidate on Home Assistant's side, and nothing reaches
the backend as a declared contract until the operator explicitly submits the
confirm step.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("homeassistant", reason="isolated HA test dependencies not installed")

from homeassistant.components.repairs import repairs_flow_manager
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.setup import async_setup_component

from custom_components.hubinet_ops.api import (
    HealthContractStatus,
    HealthDiscoveryAdapter,
    HealthDiscoveryCandidate,
    HealthDiscoveryRecommendationBasis,
    HealthDiscoveryResult,
    HealthDiscoveryRoleHint,
    HealthDiscoveryStatus,
    HealthProbeKind,
    HubinetOpsCannotConnect,
    PackagePlanApprovalStatus,
)
from custom_components.hubinet_ops.const import DOMAIN
from custom_components.hubinet_ops.repairs import _candidate_field

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


def _docker_candidate(**overrides) -> HealthDiscoveryCandidate:
    fields = {
        "adapter": HealthDiscoveryAdapter.DOCKER,
        "kind": HealthProbeKind.DOCKER_CONTAINER_HEALTHY,
        "target": "weatherhub-redis-1",
        "observed_state": "running",
        "origin": None,
        "role_hint": HealthDiscoveryRoleHint.WORKLOAD_CANDIDATE,
        "recommended": True,
        "rationale": "docker healthcheck reports healthy",
    }
    fields.update(overrides)
    return HealthDiscoveryCandidate(**fields)


def _systemd_candidate(**overrides) -> HealthDiscoveryCandidate:
    fields = {
        "adapter": HealthDiscoveryAdapter.SYSTEMD,
        "kind": HealthProbeKind.SYSTEMD_UNIT_ACTIVE,
        "target": "weatherhub.service",
        "observed_state": "active",
        "origin": None,
        "role_hint": HealthDiscoveryRoleHint.WORKLOAD_CANDIDATE,
        "recommended": False,
        "rationale": "the only enabled non-platform unit",
    }
    fields.update(overrides)
    return HealthDiscoveryCandidate(**fields)


def _guest_operational_candidate(**overrides) -> HealthDiscoveryCandidate:
    fields = {
        "adapter": HealthDiscoveryAdapter.GUEST,
        "kind": HealthProbeKind.GUEST_OPERATIONAL,
        "target": None,
        "observed_state": "unknown",
        "origin": None,
        "role_hint": HealthDiscoveryRoleHint.WORKLOAD_CANDIDATE,
        "recommended": True,
        "rationale": "guest_fallback",
    }
    fields.update(overrides)
    return HealthDiscoveryCandidate(**fields)


async def _setup_blocked_resource(
    hass: HomeAssistant, **transport_kwargs
) -> tuple[FakeTransport, str]:
    """Bring up one entry with the exact approved/unconfigured dead end.

    Returns ``(transport, issue_id)``.
    """

    planned = exact_plan_resource(approved=True)
    assert planned.resource_id == RESOURCE_CT
    assert planned.package_plan_approval.status is PackagePlanApprovalStatus.APPROVED
    assert planned.health_contract.status is HealthContractStatus.UNCONFIGURED

    transport = FakeTransport([snapshot((planned,))], **transport_kwargs)
    await setup_entry(hass, transport)
    issues = _health_contract_repair_ids(hass)
    assert len(issues) == 1
    return transport, next(iter(issues))


async def _init_fix_flow(hass: HomeAssistant, issue_id: str):
    assert await async_setup_component(hass, "repairs", {})
    flow_manager = repairs_flow_manager(hass)
    assert flow_manager is not None
    return flow_manager, await flow_manager.async_init(
        DOMAIN, data={"issue_id": issue_id}
    )


@pytest.mark.asyncio
async def test_fix_flow_declares_the_recommended_docker_candidate(
    hass: HomeAssistant,
) -> None:
    candidate = _docker_candidate()
    transport, issue_id = await _setup_blocked_resource(
        hass,
        health_discovery_results={
            RESOURCE_CT: HealthDiscoveryResult(
                resource_id=RESOURCE_CT,
                status=HealthDiscoveryStatus.OK,
                candidates=(candidate,),
                recommendation_basis=HealthDiscoveryRecommendationBasis.DOCKER_HEALTHCHECK,
            )
        },
    )

    flow_manager, result = await _init_fix_flow(hass, issue_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "confirm"
    assert transport.health_discovery_reads == [RESOURCE_CT]

    field = _candidate_field(candidate)
    # The recommended candidate is pre-selected -- submitting the schema's
    # own defaults unchanged must be enough to declare it.
    result = await flow_manager.async_configure(result["flow_id"], {field: True})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    written_resource_id, written_probes, written_revision = transport.health_contract_writes[0]
    assert written_resource_id == RESOURCE_CT
    assert written_revision is None
    assert len(written_probes) == 1
    assert written_probes[0].kind is HealthProbeKind.DOCKER_CONTAINER_HEALTHY
    assert written_probes[0].target == "weatherhub-redis-1"

    # The issue clears itself: fixing it is exactly the same as an operator
    # declaring the contract by hand.
    assert _health_contract_repair_ids(hass) == set()


@pytest.mark.asyncio
async def test_fix_flow_lets_the_operator_deselect_a_non_recommended_candidate(
    hass: HomeAssistant,
) -> None:
    """Two containers are running; only one has a Docker healthcheck.

    Both are shown, only the healthchecked one is pre-selected -- and the
    operator's own unchanged submission must leave the other one out.
    """

    healthchecked = _docker_candidate()
    running_only = _docker_candidate(
        kind=HealthProbeKind.DOCKER_CONTAINER_RUNNING,
        target="weatherhub-nginx-1",
        recommended=False,
        rationale="running, no healthcheck configured",
    )
    transport, issue_id = await _setup_blocked_resource(
        hass,
        health_discovery_results={
            RESOURCE_CT: HealthDiscoveryResult(
                resource_id=RESOURCE_CT,
                status=HealthDiscoveryStatus.OK,
                candidates=(healthchecked, running_only),
                recommendation_basis=HealthDiscoveryRecommendationBasis.DOCKER_HEALTHCHECK,
            )
        },
    )

    flow_manager, result = await _init_fix_flow(hass, issue_id)
    result = await flow_manager.async_configure(
        result["flow_id"], {_candidate_field(healthchecked): True}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    _, written_probes, _ = transport.health_contract_writes[0]
    assert len(written_probes) == 1
    assert written_probes[0].target == "weatherhub-redis-1"


@pytest.mark.asyncio
async def test_fix_flow_requires_at_least_one_selection(hass: HomeAssistant) -> None:
    candidate = _docker_candidate()
    transport, issue_id = await _setup_blocked_resource(
        hass,
        health_discovery_results={
            RESOURCE_CT: HealthDiscoveryResult(
                resource_id=RESOURCE_CT,
                status=HealthDiscoveryStatus.OK,
                candidates=(candidate,),
                recommendation_basis=HealthDiscoveryRecommendationBasis.DOCKER_HEALTHCHECK,
            )
        },
    )

    flow_manager, result = await _init_fix_flow(hass, issue_id)
    result = await flow_manager.async_configure(
        result["flow_id"], {_candidate_field(candidate): False}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "confirm"
    assert result["errors"] == {"base": "no_candidates_selected"}
    assert transport.health_contract_writes == []
    # Nothing was declared -- the issue must still be open.
    assert len(_health_contract_repair_ids(hass)) == 1


@pytest.mark.asyncio
async def test_fix_flow_writes_the_guest_fallback_with_a_null_target(
    hass: HomeAssistant,
) -> None:
    """The one rule that must never slip, end to end: a targetless fallback
    probe reaches the backend as `target: null`, never a faked target."""

    candidate = _guest_operational_candidate()
    transport, issue_id = await _setup_blocked_resource(
        hass,
        health_discovery_results={
            RESOURCE_CT: HealthDiscoveryResult(
                resource_id=RESOURCE_CT,
                status=HealthDiscoveryStatus.NO_CANDIDATES,
                candidates=(candidate,),
                recommendation_basis=HealthDiscoveryRecommendationBasis.GUEST_FALLBACK,
            )
        },
    )

    flow_manager, result = await _init_fix_flow(hass, issue_id)
    result = await flow_manager.async_configure(
        result["flow_id"], {_candidate_field(candidate): True}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    _, written_probes, _ = transport.health_contract_writes[0]
    assert len(written_probes) == 1
    assert written_probes[0].kind is HealthProbeKind.GUEST_OPERATIONAL
    assert written_probes[0].target is None


@pytest.mark.asyncio
async def test_fix_flow_lets_the_operator_choose_among_ambiguous_candidates(
    hass: HomeAssistant,
) -> None:
    first = _systemd_candidate(target="weatherhub-api.service")
    second = _systemd_candidate(target="weatherhub-worker.service")
    transport, issue_id = await _setup_blocked_resource(
        hass,
        health_discovery_results={
            RESOURCE_CT: HealthDiscoveryResult(
                resource_id=RESOURCE_CT,
                status=HealthDiscoveryStatus.AMBIGUOUS_CANDIDATES,
                candidates=(first, second),
                recommendation_basis=None,
            )
        },
    )

    flow_manager, result = await _init_fix_flow(hass, issue_id)
    # Neither is pre-selected -- the operator must pick.
    result = await flow_manager.async_configure(
        result["flow_id"],
        {_candidate_field(first): True, _candidate_field(second): False},
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    _, written_probes, _ = transport.health_contract_writes[0]
    assert len(written_probes) == 1
    assert written_probes[0].target == "weatherhub-api.service"


@pytest.mark.parametrize(
    "status",
    [
        HealthDiscoveryStatus.GUEST_UNAVAILABLE,
        HealthDiscoveryStatus.UNDECIDABLE,
        HealthDiscoveryStatus.TOO_MANY_CANDIDATES,
    ],
)
@pytest.mark.asyncio
async def test_fix_flow_aborts_on_undecided_discovery(
    hass: HomeAssistant, status: HealthDiscoveryStatus
) -> None:
    """Discovery uncertainty must never be rendered as if it were a choice --
    the flow aborts and leaves the issue open, exactly as before Stage 4."""

    transport, issue_id = await _setup_blocked_resource(
        hass,
        health_discovery_results={
            RESOURCE_CT: HealthDiscoveryResult(
                resource_id=RESOURCE_CT, status=status, candidates=(), recommendation_basis=None
            )
        },
    )

    _, result = await _init_fix_flow(hass, issue_id)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == status.value
    assert transport.health_contract_writes == []
    assert len(_health_contract_repair_ids(hass)) == 1


@pytest.mark.asyncio
async def test_fix_flow_aborts_when_discovery_itself_fails(hass: HomeAssistant) -> None:
    transport, issue_id = await _setup_blocked_resource(
        hass, health_discovery_error=HubinetOpsCannotConnect("backend unreachable")
    )

    _, result = await _init_fix_flow(hass, issue_id)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "discovery_failed"
    assert len(_health_contract_repair_ids(hass)) == 1


def test_fix_flow_translations_are_structural() -> None:
    """Every abort reason and error the flow can return, and the confirm
    step itself, must resolve in both English and Polish -- mirroring the
    existing HUMAN1-ERROR-I18N-01 discipline for exceptions."""

    integration_root = Path(__file__).parents[1] / "custom_components" / "hubinet_ops"
    strings = json.loads((integration_root / "strings.json").read_text())
    english = json.loads((integration_root / "translations" / "en.json").read_text())
    polish = json.loads((integration_root / "translations" / "pl.json").read_text())

    assert strings["issues"] == english["issues"]
    assert set(strings["issues"]) == set(polish["issues"])

    fix_flow = strings["issues"]["health_contract_unconfigured"]["fix_flow"]
    polish_fix_flow = polish["issues"]["health_contract_unconfigured"]["fix_flow"]
    assert set(fix_flow["abort"]) == set(polish_fix_flow["abort"])
    assert set(fix_flow["error"]) == set(polish_fix_flow["error"])
    assert set(fix_flow["step"]) == set(polish_fix_flow["step"])
    for section in ("abort", "error"):
        for key, message in polish_fix_flow[section].items():
            assert isinstance(message, str) and message

    # Every abort reason `async_step_init` can actually return must resolve.
    expected_abort_reasons = {
        "entry_not_loaded",
        "resource_not_found",
        "discovery_failed",
        "guest_unavailable",
        "undecidable",
        "too_many_candidates",
        "no_candidates_discovered",
    }
    assert expected_abort_reasons <= set(fix_flow["abort"])
    assert {"no_candidates_selected", "declare_failed"} <= set(fix_flow["error"])
