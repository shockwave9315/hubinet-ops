"""PR #80 review finding 3: native maintenance for an ALREADY-configured
health contract.

The onboarding Repair (tests/test_hubinet_ops_repairs.py) covers the stuck
"approved but unconfigured" dead end. This is the other half: viewing,
re-discovering, editing, replacing, or explicitly clearing a contract that
already exists -- reachable natively from Settings -> Devices & Services ->
Hubinet Ops -> Configure (the integration's own Options flow), never
Developer Tools. Discovery classification stays entirely backend-owned
throughout; this flow only ever renders an already-classified,
already-bounded `HealthDiscoveryResult` it fetched over the existing typed
route, and nothing is written before an explicit submit.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

pytest.importorskip("homeassistant", reason="isolated HA test dependencies not installed")

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.hubinet_ops.api import (
    HealthContractStatus,
    HealthContractSummary,
    HealthDiscoveryAdapter,
    HealthDiscoveryCandidate,
    HealthDiscoveryRecommendationBasis,
    HealthDiscoveryResult,
    HealthDiscoveryRoleHint,
    HealthDiscoveryStatus,
    HealthProbe,
    HealthProbeKind,
    HubinetOpsConflict,
    ResourceHealthContract,
)
from custom_components.hubinet_ops.config_flow import _probe_field

from tests.test_hubinet_ops_integration import (
    INITIAL_RESOURCES,
    FakeTransport,
    RESOURCE_CT,
    RESOURCE_TEST,
    RESOURCE_VM,
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


def _configured_ct_resource(*, revision: int = 3):
    return replace(
        INITIAL_RESOURCES[1],
        health_contract=HealthContractSummary(
            status=HealthContractStatus.CONFIGURED,
            revision=revision,
            fingerprint="a" * 64,
            probe_count=1,
            updated_at="2026-08-08T11:30:00+00:00",
        ),
    )


def _configured_contract(*, revision: int = 3) -> ResourceHealthContract:
    return ResourceHealthContract(
        resource_id=RESOURCE_CT,
        status=HealthContractStatus.CONFIGURED,
        revision=revision,
        fingerprint="a" * 64,
        created_at="2026-08-08T11:00:00+00:00",
        updated_at="2026-08-08T11:30:00+00:00",
        probes=(HealthProbe(kind=HealthProbeKind.SYSTEMD_UNIT_ACTIVE, target="nginx.service"),),
    )


async def _init_options_flow(hass: HomeAssistant, entry_id: str):
    result = await hass.config_entries.options.async_init(entry_id)
    return result


@pytest.mark.asyncio
async def test_options_flow_lists_lxc_resources_only(hass: HomeAssistant) -> None:
    entry = await setup_entry(hass, FakeTransport([snapshot(INITIAL_RESOURCES)]))
    result = await _init_options_flow(hass, entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    selectable = result["data_schema"].schema["resource_id"].container
    # RESOURCE_VM is QEMU, excluded; both LXC resources are offered.
    assert RESOURCE_VM not in selectable
    assert RESOURCE_CT in selectable
    assert RESOURCE_TEST in selectable


@pytest.mark.asyncio
async def test_options_flow_opens_the_menu_for_a_configured_resource(
    hass: HomeAssistant,
) -> None:
    configured = _configured_ct_resource()
    transport = FakeTransport(
        [snapshot((INITIAL_RESOURCES[0], configured, INITIAL_RESOURCES[2]))],
        health_contracts={RESOURCE_CT: _configured_contract()},
    )
    entry = await setup_entry(hass, transport)
    result = await _init_options_flow(hass, entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"resource_id": RESOURCE_CT}
    )
    assert result["type"] is FlowResultType.MENU
    assert set(result["menu_options"]) == {"discover", "clear_confirm"}
    assert transport.health_contract_writes == []
    assert transport.health_contract_clears == []


@pytest.mark.asyncio
async def test_options_flow_goes_straight_to_discover_for_an_unconfigured_resource(
    hass: HomeAssistant,
) -> None:
    """No contract means nothing to view or clear -- straight to discovery,
    exactly like the onboarding Repair."""

    candidate = _docker_candidate()
    transport = FakeTransport(
        [snapshot(INITIAL_RESOURCES)],
        health_discovery_results={
            RESOURCE_CT: HealthDiscoveryResult(
                resource_id=RESOURCE_CT,
                status=HealthDiscoveryStatus.OK,
                candidates=(candidate,),
                recommendation_basis=HealthDiscoveryRecommendationBasis.DOCKER_HEALTHCHECK,
            )
        },
    )
    entry = await setup_entry(hass, transport)
    result = await _init_options_flow(hass, entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"resource_id": RESOURCE_CT}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "discover"
    assert transport.health_discovery_reads == [RESOURCE_CT]
    assert transport.health_contract_writes == []


@pytest.mark.asyncio
async def test_options_flow_discover_step_renders_current_probes_without_writing(
    hass: HomeAssistant,
) -> None:
    configured = _configured_ct_resource()
    candidate = _docker_candidate()
    transport = FakeTransport(
        [snapshot((INITIAL_RESOURCES[0], configured, INITIAL_RESOURCES[2]))],
        health_contracts={RESOURCE_CT: _configured_contract()},
        health_discovery_results={
            RESOURCE_CT: HealthDiscoveryResult(
                resource_id=RESOURCE_CT,
                status=HealthDiscoveryStatus.OK,
                candidates=(candidate,),
                recommendation_basis=HealthDiscoveryRecommendationBasis.DOCKER_HEALTHCHECK,
            )
        },
    )
    entry = await setup_entry(hass, transport)
    result = await _init_options_flow(hass, entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"resource_id": RESOURCE_CT}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "discover"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "discover"
    schema_fields = {str(key) for key in result["data_schema"].schema}
    current_field = _probe_field(HealthProbeKind.SYSTEMD_UNIT_ACTIVE, "nginx.service")
    candidate_field = _probe_field(candidate.kind, candidate.target)
    assert current_field in schema_fields
    assert candidate_field in schema_fields
    # Nothing was written merely by rendering the form -- the recommended
    # candidate and the current probe are pre-selected defaults, not writes.
    assert transport.health_contract_writes == []
    assert transport.health_contract_clears == []


@pytest.mark.asyncio
async def test_options_flow_explicit_replace_succeeds_with_the_read_revision(
    hass: HomeAssistant,
) -> None:
    configured = _configured_ct_resource(revision=5)
    candidate = _docker_candidate()
    transport = FakeTransport(
        [snapshot((INITIAL_RESOURCES[0], configured, INITIAL_RESOURCES[2]))],
        health_contracts={RESOURCE_CT: _configured_contract(revision=5)},
        health_discovery_results={
            RESOURCE_CT: HealthDiscoveryResult(
                resource_id=RESOURCE_CT,
                status=HealthDiscoveryStatus.OK,
                candidates=(candidate,),
                recommendation_basis=HealthDiscoveryRecommendationBasis.DOCKER_HEALTHCHECK,
            )
        },
    )
    entry = await setup_entry(hass, transport)
    result = await _init_options_flow(hass, entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"resource_id": RESOURCE_CT}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "discover"}
    )
    candidate_field = _probe_field(candidate.kind, candidate.target)
    current_field = _probe_field(HealthProbeKind.SYSTEMD_UNIT_ACTIVE, "nginx.service")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {candidate_field: True, current_field: False}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    resource_id, probes, expected_revision = transport.health_contract_writes[0]
    assert resource_id == RESOURCE_CT
    assert expected_revision == 5
    assert len(probes) == 1
    assert probes[0].target == "weatherhub-redis-1"


@pytest.mark.asyncio
async def test_options_flow_revision_race_fails_closed_and_never_overwrites(
    hass: HomeAssistant,
) -> None:
    configured = _configured_ct_resource(revision=5)
    candidate = _docker_candidate()
    transport = FakeTransport(
        [snapshot((INITIAL_RESOURCES[0], configured, INITIAL_RESOURCES[2]))],
        health_contracts={RESOURCE_CT: _configured_contract(revision=5)},
        health_discovery_results={
            RESOURCE_CT: HealthDiscoveryResult(
                resource_id=RESOURCE_CT,
                status=HealthDiscoveryStatus.OK,
                candidates=(candidate,),
                recommendation_basis=HealthDiscoveryRecommendationBasis.DOCKER_HEALTHCHECK,
            )
        },
    )
    entry = await setup_entry(hass, transport)

    original_replace = transport.replace_health_contract
    calls = {"n": 0}

    async def conflicting_once(resource_id, probes, expected_revision):
        calls["n"] += 1
        if calls["n"] == 1:
            raise HubinetOpsConflict("stale revision")
        return await original_replace(resource_id, probes, expected_revision)

    transport.replace_health_contract = conflicting_once

    result = await _init_options_flow(hass, entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"resource_id": RESOURCE_CT}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "discover"}
    )
    candidate_field = _probe_field(candidate.kind, candidate.target)
    current_field = _probe_field(HealthProbeKind.SYSTEMD_UNIT_ACTIVE, "nginx.service")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {candidate_field: True, current_field: False}
    )
    # Refused, not silently overwritten -- the form re-shows with an error.
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "revision_changed"}
    assert transport.health_contract_writes == []

    # Trying again (same selection) now succeeds against the current data.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {candidate_field: True, current_field: False}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert len(transport.health_contract_writes) == 1


@pytest.mark.asyncio
async def test_options_flow_explicit_clear_succeeds(hass: HomeAssistant) -> None:
    configured = _configured_ct_resource(revision=5)
    transport = FakeTransport(
        [snapshot((INITIAL_RESOURCES[0], configured, INITIAL_RESOURCES[2]))],
        health_contracts={RESOURCE_CT: _configured_contract(revision=5)},
    )
    entry = await setup_entry(hass, transport)
    result = await _init_options_flow(hass, entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"resource_id": RESOURCE_CT}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "clear_confirm"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "clear_confirm"

    # Declining confirmation refuses, never clears.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"confirm": False}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "clear_not_confirmed"}
    assert transport.health_contract_clears == []

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"confirm": True}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert transport.health_contract_clears == [(RESOURCE_CT, 5)]
