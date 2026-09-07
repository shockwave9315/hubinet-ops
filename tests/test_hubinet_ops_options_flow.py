"""Native health maintenance after the v0.5 simplification.

Settings -> Devices & Services -> Hubinet Ops -> Configure is now a narrow,
two-step flow: pick an LXC, see what it currently declares, and optionally
restore the Hubinet Ops built-in default. It is deliberately NOT part of the
normal update flow -- a managed LXC already carries the backend's built-in
``guest_operational`` contract before its first update, so nothing here has
to be visited to reach **Start**.

The load-bearing part of these tests is what the flow can no longer do: there
is no discovery step, no candidate rendering, no adapter/role/recommendation
vocabulary, and no path through which Home Assistant decides what "healthy"
means for a guest. Absence of a workload observer is not proof of workload
absence, so v0.5 infers no workload health at all; an advanced Docker/systemd
contract stays an explicit `set_health_contract` decision.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

pytest.importorskip("homeassistant", reason="isolated HA test dependencies not installed")

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.hubinet_ops import config_flow as config_flow_module
from custom_components.hubinet_ops.api import (
    HealthContractStatus,
    HealthContractSummary,
    HealthProbe,
    HealthProbeKind,
    HubinetOpsCannotConnect,
    HubinetOpsConflict,
    OperatorCapabilities,
    ResourceHealthContract,
)

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


def _advanced_contract(*, revision: int = 3) -> ResourceHealthContract:
    """An explicitly declared advanced systemd contract."""

    return ResourceHealthContract(
        resource_id=RESOURCE_CT,
        status=HealthContractStatus.CONFIGURED,
        revision=revision,
        fingerprint="a" * 64,
        created_at="2026-08-08T11:00:00+00:00",
        updated_at="2026-08-08T11:30:00+00:00",
        probes=(
            HealthProbe(
                kind=HealthProbeKind.SYSTEMD_UNIT_ACTIVE, target="mariadb.service"
            ),
        ),
    )


async def _open_for_ct(hass: HomeAssistant, transport) -> tuple:
    """Open the flow for RESOURCE_CT.

    GitHub review P2 #2: the init selector now only offers an LXC the
    backend currently says `can_configure_health_contract` for, so every
    fixture routed through here needs that capability published for
    RESOURCE_CT -- `setdefault` so a test that deliberately sets its own
    capabilities map is never overridden.
    """

    transport.operator_capabilities.setdefault(
        RESOURCE_CT, OperatorCapabilities(can_configure_health_contract=True)
    )
    entry = await setup_entry(hass, transport)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"resource_id": RESOURCE_CT}
    )
    return entry, result


@pytest.mark.asyncio
async def test_options_flow_lists_lxc_resources_only(hass: HomeAssistant) -> None:
    """Both current, configurable LXCs are offered; the QEMU is excluded."""

    entry = await setup_entry(
        hass,
        FakeTransport(
            [snapshot(INITIAL_RESOURCES)],
            operator_capabilities={
                RESOURCE_CT: OperatorCapabilities(can_configure_health_contract=True),
                RESOURCE_TEST: OperatorCapabilities(can_configure_health_contract=True),
            },
        ),
    )
    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    selectable = result["data_schema"].schema["resource_id"].container
    # RESOURCE_VM is QEMU, excluded; both current, configurable LXCs are
    # offered.
    assert RESOURCE_VM not in selectable
    assert RESOURCE_CT in selectable
    assert RESOURCE_TEST in selectable


@pytest.mark.asyncio
async def test_a_retained_non_current_lxc_is_not_offered(hass: HomeAssistant) -> None:
    """GitHub review P2 #2's exact witness: a failed/partial discovery can
    retain a historical/missing/uncertain LXC in the published snapshot, and
    the backend correctly refuses `can_configure_health_contract` for it. HA
    must not offer a resource the backend would then refuse
    `async_fetch_health_contract`/`async_reset_health_contract` for --
    RESOURCE_TEST is a real LXC in the snapshot, but the backend publishes no
    configure capability for it, so it must not appear in the selector."""

    entry = await setup_entry(
        hass,
        FakeTransport(
            [snapshot(INITIAL_RESOURCES)],
            operator_capabilities={
                RESOURCE_CT: OperatorCapabilities(can_configure_health_contract=True),
                # RESOURCE_TEST deliberately omitted: defaults to
                # OperatorCapabilities(), i.e. every capability False,
                # exactly like a retained/non-current resource.
            },
        ),
    )
    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    selectable = result["data_schema"].schema["resource_id"].container
    assert RESOURCE_CT in selectable
    assert RESOURCE_TEST not in selectable
    assert RESOURCE_VM not in selectable


@pytest.mark.asyncio
async def test_no_eligible_lxc_produces_a_truthful_abort(hass: HomeAssistant) -> None:
    """Every LXC present is currently non-configurable (e.g. all retained/
    non-current) -- the flow must abort truthfully rather than offer a
    selector the backend would refuse every option of."""

    entry = await setup_entry(hass, FakeTransport([snapshot(INITIAL_RESOURCES)]))
    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_lxc_resources"


@pytest.mark.asyncio
async def test_a_selection_that_loses_eligibility_while_the_form_is_open_is_reprompted(
    hass: HomeAssistant,
) -> None:
    """GitHub review P2 #1's exact TOCTOU witness: two LXCs are eligible when
    the init form is rendered, the operator selects one, a coordinator
    refresh happens in the background WHILE that already-rendered form still
    sits open and strips that exact resource's
    `can_configure_health_contract` -- the OTHER LXC stays eligible, so the
    freshly rebuilt `resources` mapping is not empty, it just no longer
    contains the submitted id. The flow must re-render the same step
    truthfully with an error, never raise a raw `KeyError` from indexing a
    capability-filtered mapping with a stale selection -- and the operator
    must be able to pick the still-eligible resource right away."""

    transport = FakeTransport(
        [snapshot(INITIAL_RESOURCES)],
        operator_capabilities={
            RESOURCE_CT: OperatorCapabilities(can_configure_health_contract=True),
            RESOURCE_TEST: OperatorCapabilities(can_configure_health_contract=True),
        },
    )
    entry = await setup_entry(hass, transport)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    selectable = result["data_schema"].schema["resource_id"].container
    assert RESOURCE_CT in selectable
    assert RESOURCE_TEST in selectable

    # The background coordinator refresh the already-open form cannot see:
    # RESOURCE_CT loses the capability the about-to-be-submitted selection
    # depends on, while RESOURCE_TEST remains eligible.
    transport.operator_capabilities[RESOURCE_CT] = OperatorCapabilities()
    await entry.runtime_data.async_request_refresh()
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"resource_id": RESOURCE_CT}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    assert result["errors"] == {"base": "resource_no_longer_eligible"}
    reoffered = result["data_schema"].schema["resource_id"].container
    assert RESOURCE_CT not in reoffered
    assert RESOURCE_TEST in reoffered

    # The still-eligible resource can be selected cleanly, in the same flow.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"resource_id": RESOURCE_TEST}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reset_confirm"


@pytest.mark.asyncio
async def test_the_flow_shows_the_current_contract_and_writes_nothing(
    hass: HomeAssistant,
) -> None:
    """Reading is never a mutation, and the form is reached in ONE step --
    no discover/render/confirm detour."""

    transport = FakeTransport(
        [snapshot((INITIAL_RESOURCES[0], _configured_ct_resource(), INITIAL_RESOURCES[2]))],
        health_contracts={RESOURCE_CT: _advanced_contract()},
    )
    _entry, result = await _open_for_ct(hass, transport)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reset_confirm"
    assert "mariadb.service" in result["description_placeholders"]["current"]
    assert transport.health_contract_writes == []
    assert transport.health_contract_resets == []
    assert transport.health_contract_clears == []


@pytest.mark.asyncio
async def test_an_unconfigured_resource_reaches_the_same_single_form(
    hass: HomeAssistant,
) -> None:
    """Even the state the old flow treated as an onboarding dead end is just
    "nothing declared yet" now -- one form, one confirm, done."""

    transport = FakeTransport([snapshot(INITIAL_RESOURCES)])
    _entry, result = await _open_for_ct(hass, transport)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reset_confirm"
    assert "unconfigured" in result["description_placeholders"]["current"]


@pytest.mark.asyncio
async def test_reset_restores_the_default_with_the_read_revision(
    hass: HomeAssistant,
) -> None:
    """The backend owns the default: this flow sends the resource and its
    compare-and-set revision, and no probe of its own."""

    transport = FakeTransport(
        [
            snapshot(
                (INITIAL_RESOURCES[0], _configured_ct_resource(), INITIAL_RESOURCES[2])
            ),
            snapshot(
                (INITIAL_RESOURCES[0], _configured_ct_resource(revision=4), INITIAL_RESOURCES[2]),
                inventory_revision=11,
                published_state_revision=21,
            ),
        ],
        health_contracts={RESOURCE_CT: _advanced_contract()},
    )
    _entry, result = await _open_for_ct(hass, transport)

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"confirm": True}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert transport.health_contract_resets == [(RESOURCE_CT, 3)]
    assert transport.health_contract_writes == []
    assert transport.health_contract_clears == []
    assert transport.health_contracts[RESOURCE_CT].probes == (
        HealthProbe(kind=HealthProbeKind.GUEST_OPERATIONAL, target=None),
    )


@pytest.mark.asyncio
async def test_reset_from_unconfigured_asserts_revision_zero(
    hass: HomeAssistant,
) -> None:
    """`0` is the backend's own "there is no contract yet" assertion, never
    "no opinion" -- so an unconfigured reset is still a compare-and-set."""

    transport = FakeTransport([snapshot(INITIAL_RESOURCES), snapshot(INITIAL_RESOURCES)])
    _entry, result = await _open_for_ct(hass, transport)

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"confirm": True}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert transport.health_contract_resets == [(RESOURCE_CT, 0)]


@pytest.mark.asyncio
async def test_an_unconfirmed_reset_writes_nothing(hass: HomeAssistant) -> None:
    transport = FakeTransport(
        [snapshot((INITIAL_RESOURCES[0], _configured_ct_resource(), INITIAL_RESOURCES[2]))],
        health_contracts={RESOURCE_CT: _advanced_contract()},
    )
    _entry, result = await _open_for_ct(hass, transport)

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"confirm": False}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "reset_not_confirmed"}
    assert transport.health_contract_resets == []
    # The advanced contract an operator declared is still exactly there.
    assert transport.health_contracts[RESOURCE_CT] == _advanced_contract()


@pytest.mark.asyncio
async def test_a_revision_race_fails_closed_and_never_overwrites(
    hass: HomeAssistant,
) -> None:
    """A concurrent change is refused and re-read, never retried blindly."""

    transport = FakeTransport(
        [snapshot((INITIAL_RESOURCES[0], _configured_ct_resource(), INITIAL_RESOURCES[2]))],
        health_contracts={RESOURCE_CT: _advanced_contract()},
    )
    _entry, result = await _open_for_ct(hass, transport)
    # Another writer changes the contract while this form is open.
    transport.health_contract_error = HubinetOpsConflict("revision conflict")

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"confirm": True}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "revision_changed"}
    assert transport.health_contract_resets == [(RESOURCE_CT, 3)]
    assert transport.health_contracts[RESOURCE_CT] == _advanced_contract()


@pytest.mark.asyncio
async def test_a_failed_reset_is_reported_and_changes_nothing(
    hass: HomeAssistant,
) -> None:
    transport = FakeTransport(
        [snapshot((INITIAL_RESOURCES[0], _configured_ct_resource(), INITIAL_RESOURCES[2]))],
        health_contracts={RESOURCE_CT: _advanced_contract()},
    )
    _entry, result = await _open_for_ct(hass, transport)
    transport.health_contract_error = HubinetOpsCannotConnect("backend down")

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"confirm": True}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "reset_failed"}
    assert transport.health_contracts[RESOURCE_CT] == _advanced_contract()


@pytest.mark.asyncio
async def test_an_unreadable_contract_aborts_rather_than_guessing(
    hass: HomeAssistant,
) -> None:
    transport = FakeTransport(
        [snapshot(INITIAL_RESOURCES)],
        health_contract_error=HubinetOpsCannotConnect("backend down"),
    )
    _entry, result = await _open_for_ct(hass, transport)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "health_contract_read_failed"
    assert transport.health_contract_resets == []


def test_the_options_flow_carries_no_workload_discovery_surface() -> None:
    """Load-bearing: this operator surface must never grow back into guest
    inspection.

    It also may not name Docker or systemd COMMANDS. Advanced explicit probes
    still run those -- in the job-bound backend health helper, which is the
    only place they belong.
    """

    flow = config_flow_module.HubinetOpsOptionsFlow
    assert not hasattr(flow, "async_step_discover")
    assert not hasattr(flow, "async_step_clear_confirm")
    assert not hasattr(flow, "_candidates")
    assert not hasattr(config_flow_module, "_probe_field")

    source = Path(config_flow_module.__file__).read_text(encoding="utf-8")
    for marker in (
        "async_fetch_health_candidates",
        "HealthDiscovery",
        "UNDECIDED_DISCOVERY_STATUSES",
        "recommended",
        "docker ps",
        "systemctl",
        "command -v",
    ):
        assert marker not in source, marker
