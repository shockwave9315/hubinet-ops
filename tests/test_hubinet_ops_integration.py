from __future__ import annotations

import ast
from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("homeassistant", reason="isolated HA test dependencies not installed")

from homeassistant.components.diagnostics import REDACTED
from homeassistant.config_entries import (
    SOURCE_REAUTH,
    SOURCE_RECONFIGURE,
    ConfigEntryState,
)
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.update_coordinator import UpdateFailed

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hubinet_ops.api import (
    BackendInformation,
    DetailStatus,
    HubinetOpsApi,
    HubinetOpsCannotConnect,
    HubinetOpsInvalidAuth,
    HubinetOpsSnapshot,
    InventorySourceSnapshot,
    LifecycleState,
    NodeAvailability,
    NodeSnapshot,
    ObservationalContinuity,
    PresenceState,
    ResourceSnapshot,
    ResourceStateLevel,
    ResourceType,
    SecurityContinuity,
    SourceContext,
    SourceFreshness,
    SourceHealth,
    SourceHealthOrigin,
)
from custom_components.hubinet_ops.const import (
    CONF_API_TOKEN,
    CONF_BASE_URL,
    CONF_VERIFY_TLS,
    DATA_API_FACTORY,
    DOMAIN,
)
from custom_components.hubinet_ops.coordinator import (
    node_registry_key,
    resource_device_info,
    resource_registry_key,
    source_registry_key,
)
from custom_components.hubinet_ops.diagnostics import (
    async_get_config_entry_diagnostics,
)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations, socket_enabled):
    """Load custom integrations; fake transports never perform network I/O."""

    yield


BACKEND_ID = "6a172b5d-d820-4cac-904f-dfb17d42163e"
OTHER_BACKEND_ID = "2b5d3b3b-e4b9-412a-851a-11bc4e839aa7"
SOURCE_ID = "cfe64f8e-2529-4692-9c23-526479961dbc"
SOURCE_B_ID = "44e24b73-f593-4625-a182-2e2db9541688"
NODE_A = "811d7ea4-470f-42d4-aa06-d2c5c9249a1e"
NODE_B = "6f1c0770-6ca6-4c20-ab56-8cb645f63ee3"
RESOURCE_VM = "3a6d0ac4-f859-438d-9f96-9d21dba641f1"
RESOURCE_CT = "b50f157b-d2fb-4fff-9497-42c5c239ef49"
RESOURCE_TEST = "58fa8094-8a8b-4e70-9cf3-f8fd727d85ea"
RESOURCE_ADDED = "c5321ec5-7259-421a-94ab-195a9c5e5d81"
BASE_URL = "https://ops.example.test"
API_TOKEN = "phase-zero-test-token-not-a-secret"
ENTRY_DATA = {
    CONF_BASE_URL: BASE_URL,
    CONF_API_TOKEN: API_TOKEN,
    CONF_VERIFY_TLS: True,
}


def backend_information(
    *, backend_instance_id: str = BACKEND_ID
) -> BackendInformation:
    return BackendInformation(
        backend_instance_id=backend_instance_id,
        name="Hubinet Ops Test",
        version="0.5.0.dev0",
        api_version="0.5-draft",
    )


def source_context(
    *,
    revision: int = 3,
    transport_trust_revision: int = 2,
    endpoint_id: str = "7b784024-62d8-4f3e-bb63-af9fe65fcc8e",
    canonical_transport_locator: str = "https://pve.example.test:8006",
    canonicalization_contract_version: int = 1,
) -> SourceContext:
    return SourceContext(
        source_config_revision=revision,
        endpoint_id=endpoint_id,
        canonical_transport_locator=canonical_transport_locator,
        canonicalization_contract_version=canonicalization_contract_version,
        transport_trust_revision=transport_trust_revision,
    )


def source(
    *,
    inventory_source_id: str = SOURCE_ID,
    name: str = "Test Proxmox",
    provider_kind: str = "proxmox",
    health: SourceHealth = SourceHealth.HEALTHY,
    freshness: SourceFreshness = SourceFreshness.FRESH,
    health_origin: SourceHealthOrigin = SourceHealthOrigin.DISCOVERY_RUN,
    health_reason: str = "authoritative_inventory_commit",
    last_issued_run_sequence: int = 5,
    latest_completed_run_sequence: int | None = 5,
    latest_completed_outcome: str | None = "success",
    last_health_run_sequence: int | None = 5,
    last_run_health_outcome: str | None = "success",
    last_committed_run_sequence: int | None = 5,
    current_context: SourceContext | None = None,
    committed_context: SourceContext | None = None,
    facts: dict[str, Any] | None = None,
) -> InventorySourceSnapshot:
    has_commit = last_committed_run_sequence is not None
    context = current_context or source_context()
    return InventorySourceSnapshot(
        inventory_source_id=inventory_source_id,
        name=name,
        provider_kind=provider_kind,
        health=health,
        freshness=freshness,
        health_origin=health_origin,
        health_reason=health_reason,
        last_issued_run_sequence=last_issued_run_sequence,
        latest_completed_run_sequence=latest_completed_run_sequence,
        latest_completed_outcome=latest_completed_outcome,
        last_health_run_sequence=last_health_run_sequence,
        last_run_health_outcome=last_run_health_outcome,
        last_committed_run_sequence=last_committed_run_sequence,
        last_successful_observed_at=(
            "2026-08-08T11:59:30+00:00" if has_commit else None
        ),
        freshness_reference_at=(
            "2026-08-08T11:59:00+00:00" if has_commit else None
        ),
        freshness_valid_until=(
            "2026-08-08T12:04:00+00:00" if has_commit else None
        ),
        current_context=context,
        committed_context=(committed_context or context) if has_commit else None,
        facts=facts or {},
    )


def unavailable_source() -> InventorySourceSnapshot:
    return source(
        health=SourceHealth.SOURCE_UNAVAILABLE,
        freshness=SourceFreshness.STALE,
        health_reason="active_endpoint_timeout",
        last_issued_run_sequence=6,
        latest_completed_run_sequence=6,
        latest_completed_outcome="source_unavailable",
        last_health_run_sequence=6,
        last_run_health_outcome="source_unavailable",
    )


def node(
    node_id: str = NODE_A,
    *,
    inventory_source_id: str = SOURCE_ID,
    name: str = "pve-a",
    available: bool = True,
    facts: dict[str, Any] | None = None,
) -> NodeSnapshot:
    return NodeSnapshot(
        node_id=node_id,
        inventory_source_id=inventory_source_id,
        name=name,
        status="online" if available else "offline",
        available=available,
        facts=facts or {},
    )


def resource(
    resource_id: str,
    resource_type: ResourceType,
    vmid: int,
    name: str,
    *,
    active_binding_id: str | None = None,
    locator_generation: int = 1,
    resource_continuity_revision: int = 1,
    current_node_id: str | None = NODE_A,
    last_known_node_id: str | None = None,
    presence: PresenceState = PresenceState.PRESENT,
    lifecycle: LifecycleState = LifecycleState.ACTIVE,
    observational_continuity: ObservationalContinuity = (
        ObservationalContinuity.CONSISTENT
    ),
    security_continuity: SecurityContinuity = SecurityContinuity.UNVERIFIED,
    detail_status: DetailStatus = DetailStatus.OK,
    node_availability: NodeAvailability = NodeAvailability.AVAILABLE,
    status: str = "running",
    retained_policy: dict[str, Any] | None = None,
    effective_policy: dict[str, Any] | None = None,
    policy_applicable: bool = False,
    effective_capabilities: frozenset[str] = frozenset(),
    state: dict[str, Any] | None = None,
    termination_reason: str | None = None,
    successor_resource_id: str | None = None,
) -> ResourceSnapshot:
    if active_binding_id is None and presence in {
        PresenceState.PRESENT,
        PresenceState.MISSING,
    }:
        active_binding_id = f"binding-{resource_id}"
    return ResourceSnapshot(
        resource_id=resource_id,
        inventory_source_id=SOURCE_ID,
        active_binding_id=active_binding_id,
        resource_type=resource_type,
        vmid=vmid,
        locator_generation=locator_generation,
        resource_continuity_revision=resource_continuity_revision,
        name=name,
        status=status,
        current_node_id=current_node_id,
        last_known_node_id=last_known_node_id,
        presence=presence,
        lifecycle=lifecycle,
        observational_continuity=observational_continuity,
        security_continuity=security_continuity,
        detail_status=detail_status,
        node_availability=node_availability,
        state_level=ResourceStateLevel.OBSERVED,
        retained_policy=retained_policy or {},
        effective_policy=effective_policy or {},
        policy_applicable=policy_applicable,
        effective_capabilities=effective_capabilities,
        state=state or {},
        termination_reason=termination_reason,
        successor_resource_id=successor_resource_id,
    )


def absent_resource(
    resource_id: str,
    presence: PresenceState,
    *,
    vmid: int = 777,
    generation: int = 1,
    resource_continuity_revision: int = 1,
    successor_resource_id: str | None = None,
) -> ResourceSnapshot:
    terminal = presence in {
        PresenceState.CONFIRMED_REMOVED,
        PresenceState.NOT_CURRENT,
    }
    return resource(
        resource_id,
        ResourceType.LXC,
        vmid,
        "Retained Container",
        active_binding_id=None if terminal else f"binding-{resource_id}",
        locator_generation=generation,
        resource_continuity_revision=resource_continuity_revision,
        current_node_id=None,
        last_known_node_id=NODE_A,
        presence=presence,
        lifecycle=(LifecycleState.RETIRED if terminal else LifecycleState.QUARANTINED),
        observational_continuity=(
            ObservationalContinuity.REPLACED
            if presence is PresenceState.NOT_CURRENT
            else ObservationalContinuity.UNCERTAIN
        ),
        detail_status=DetailStatus.NOT_APPLICABLE,
        node_availability=NodeAvailability.NOT_APPLICABLE,
        status="unknown",
        termination_reason=(
            "replaced"
            if presence is PresenceState.NOT_CURRENT
            else "confirmed_removed"
            if presence is PresenceState.CONFIRMED_REMOVED
            else None
        ),
        successor_resource_id=successor_resource_id,
    )


INITIAL_RESOURCES = (
    resource(RESOURCE_VM, ResourceType.QEMU, 100, "Home Assistant"),
    resource(RESOURCE_CT, ResourceType.LXC, 101, "Cloudflared"),
    resource(RESOURCE_TEST, ResourceType.LXC, 666, "Test Container"),
)


def snapshot(
    resources: Iterable[ResourceSnapshot],
    *,
    sources: tuple[InventorySourceSnapshot, ...] | None = None,
    nodes: tuple[NodeSnapshot, ...] | None = None,
    backend_instance_id: str = BACKEND_ID,
    inventory_revision: int = 10,
    published_state_revision: int = 20,
    published_at: str = "2026-08-08T12:00:00+00:00",
) -> HubinetOpsSnapshot:
    return HubinetOpsSnapshot(
        backend=backend_information(backend_instance_id=backend_instance_id),
        sources=sources if sources is not None else (source(),),
        nodes=nodes if nodes is not None else (node(),),
        resources=tuple(resources),
        inventory_revision=inventory_revision,
        published_state_revision=published_state_revision,
        published_at=published_at,
    )


class FakeTransport:
    def __init__(
        self,
        snapshots: Iterable[HubinetOpsSnapshot] = (),
        *,
        validation_error: Exception | None = None,
        validation_backend_instance_id: str = BACKEND_ID,
    ) -> None:
        self._snapshots = list(snapshots)
        self._index = 0
        self.validation_error = validation_error
        self.validation_backend_instance_id = validation_backend_instance_id
        self.validate_calls = 0
        self.snapshot_calls = 0

    async def validate_connection(self) -> BackendInformation:
        self.validate_calls += 1
        if self.validation_error is not None:
            raise self.validation_error
        return backend_information(
            backend_instance_id=self.validation_backend_instance_id
        )

    async def fetch_backend_information(self) -> BackendInformation:
        return backend_information()

    async def fetch_resource_snapshot(self) -> HubinetOpsSnapshot:
        self.snapshot_calls += 1
        if not self._snapshots:
            raise HubinetOpsCannotConnect("no fake snapshot")
        selected = self._snapshots[min(self._index, len(self._snapshots) - 1)]
        self._index += 1
        return selected


class FakeApiFactory:
    def __init__(self, transport: FakeTransport) -> None:
        self.transport = transport
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self, *, base_url: str, api_token: str, verify_tls: bool
    ) -> HubinetOpsApi:
        self.calls.append(
            {
                "base_url": base_url,
                "api_token": api_token,
                "verify_tls": verify_tls,
            }
        )
        return HubinetOpsApi(
            base_url=base_url,
            api_token=api_token,
            verify_tls=verify_tls,
            transport=self.transport,
        )


def install_factory(hass: HomeAssistant, transport: FakeTransport) -> FakeApiFactory:
    factory = FakeApiFactory(transport)
    hass.data.setdefault(DOMAIN, {})[DATA_API_FACTORY] = factory
    return factory


async def setup_entry(
    hass: HomeAssistant, transport: FakeTransport
) -> MockConfigEntry:
    install_factory(hass, transport)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Hubinet Ops Test",
        data=ENTRY_DATA,
        unique_id=BACKEND_ID,
        version=1,
        minor_version=1,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def registry_unique_ids(
    hass: HomeAssistant, entry: MockConfigEntry, key: str
) -> set[str]:
    return {
        item.unique_id
        for item in er.async_entries_for_config_entry(
            er.async_get(hass), entry.entry_id
        )
        if key in item.unique_id
    }


def resource_entity_states(
    hass: HomeAssistant, entry: MockConfigEntry, resource_id: str
) -> dict[str, str]:
    key = resource_registry_key(BACKEND_ID, resource_id)
    prefix = f"{key}:"
    states: dict[str, str] = {}
    for item in er.async_entries_for_config_entry(
        er.async_get(hass), entry.entry_id
    ):
        if not item.unique_id.startswith(prefix):
            continue
        state = hass.states.get(item.entity_id)
        assert state is not None
        states[item.unique_id.removeprefix(prefix)] = state.state
    return states


@pytest.mark.asyncio
async def test_config_flow_binds_exact_backend_instance(hass: HomeAssistant) -> None:
    transport = FakeTransport([snapshot(())])
    factory = install_factory(hass, transport)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "user"},
        data={**ENTRY_DATA, CONF_BASE_URL: f"{BASE_URL}/"},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == ENTRY_DATA
    assert transport.validate_calls == 1
    assert all(call == ENTRY_DATA for call in factory.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (HubinetOpsInvalidAuth("denied"), "invalid_auth"),
        (HubinetOpsCannotConnect("offline"), "cannot_connect"),
    ],
)
async def test_config_flow_connection_errors(
    hass: HomeAssistant, error: Exception, expected: str
) -> None:
    install_factory(hass, FakeTransport(validation_error=error))
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}, data=ENTRY_DATA
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": expected}


@pytest.mark.asyncio
async def test_reauth_preserves_backend_identity(hass: HomeAssistant) -> None:
    transport = FakeTransport()
    install_factory(hass, transport)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Hubinet Ops Test",
        data=ENTRY_DATA,
        unique_id=BACKEND_ID,
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id},
        data=entry.data,
    )
    assert result["step_id"] == "reauth_confirm"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_TOKEN: "replacement-token"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.unique_id == BACKEND_ID
    assert entry.data[CONF_API_TOKEN] == "replacement-token"


@pytest.mark.asyncio
async def test_reauth_rejects_wrong_backend_instance(hass: HomeAssistant) -> None:
    install_factory(
        hass,
        FakeTransport(validation_backend_instance_id=OTHER_BACKEND_ID),
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Hubinet Ops Test",
        data=ENTRY_DATA,
        unique_id=BACKEND_ID,
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id},
        data=entry.data,
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_TOKEN: "foreign-backend-token"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert result["errors"] == {"base": "wrong_instance"}
    assert entry.unique_id == BACKEND_ID
    assert entry.data == ENTRY_DATA


@pytest.mark.asyncio
async def test_reconfigure_preserves_backend_identity(hass: HomeAssistant) -> None:
    install_factory(hass, FakeTransport())
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Hubinet Ops Test",
        data=ENTRY_DATA,
        unique_id=BACKEND_ID,
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
    )
    assert result["step_id"] == "reconfigure"
    replacement = {
        CONF_BASE_URL: "https://new-ops.example.test/",
        CONF_API_TOKEN: "replacement-token",
        CONF_VERIFY_TLS: False,
    }
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], replacement
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.unique_id == BACKEND_ID
    assert entry.data == {
        **replacement,
        CONF_BASE_URL: "https://new-ops.example.test",
    }


@pytest.mark.asyncio
async def test_reconfigure_rejects_wrong_backend_instance(
    hass: HomeAssistant,
) -> None:
    install_factory(
        hass,
        FakeTransport(validation_backend_instance_id=OTHER_BACKEND_ID),
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Hubinet Ops Test",
        data=ENTRY_DATA,
        unique_id=BACKEND_ID,
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_BASE_URL: "https://foreign-ops.example.test",
            CONF_API_TOKEN: "foreign-backend-token",
            CONF_VERIFY_TLS: True,
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"
    assert result["errors"] == {"base": "wrong_instance"}
    assert entry.unique_id == BACKEND_ID
    assert entry.data == ENTRY_DATA


@pytest.mark.asyncio
async def test_backend_instance_mismatch_preserves_previous_view_and_registry(
    hass: HomeAssistant,
) -> None:
    foreign = HubinetOpsSnapshot(
        backend=backend_information(backend_instance_id=OTHER_BACKEND_ID),
        sources=(source(),),
        nodes=(node(NODE_B, name="pve-b"),),
        resources=(),
        inventory_revision=11,
        published_state_revision=21,
        published_at="2026-08-08T12:01:00+00:00",
    )
    entry = await setup_entry(
        hass, FakeTransport([snapshot(INITIAL_RESOURCES), foreign])
    )
    coordinator = entry.runtime_data
    previous = coordinator.data
    known = (
        coordinator.known_sources.copy(),
        coordinator.known_nodes.copy(),
        coordinator.known_resources.copy(),
    )
    callback_events: list[Any] = []
    coordinator.new_sources_callbacks.append(callback_events.extend)
    coordinator.new_nodes_callbacks.append(callback_events.extend)
    coordinator.new_resources_callbacks.append(callback_events.extend)

    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.last_update_success is False
    assert isinstance(coordinator.last_exception, UpdateFailed)
    assert coordinator.last_exception.translation_key == "wrong_instance"
    assert coordinator.data is previous
    assert coordinator.data.backend.backend_instance_id == BACKEND_ID
    assert known == (
        coordinator.known_sources,
        coordinator.known_nodes,
        coordinator.known_resources,
    )
    assert callback_events == []
    registry = dr.async_get(hass)
    assert registry.async_get_device(
        {(DOMAIN, node_registry_key(OTHER_BACKEND_ID, NODE_B))}
    ) is None


@pytest.mark.asyncio
async def test_first_refresh_rejects_foreign_backend_before_device_changes(
    hass: HomeAssistant,
) -> None:
    foreign = snapshot((), backend_instance_id=OTHER_BACKEND_ID)
    install_factory(hass, FakeTransport([foreign]))
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Hubinet Ops Test",
        data=ENTRY_DATA,
        unique_id=BACKEND_ID,
    )
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id) == []


@pytest.mark.asyncio
async def test_devices_and_entities_are_keyed_by_backend_resource_id(
    hass: HomeAssistant,
) -> None:
    entry = await setup_entry(hass, FakeTransport([snapshot(INITIAL_RESOURCES)]))
    registry = dr.async_get(hass)
    source_device = registry.async_get_device(
        {(DOMAIN, source_registry_key(BACKEND_ID, SOURCE_ID))}
    )
    node_device = registry.async_get_device(
        {(DOMAIN, node_registry_key(BACKEND_ID, NODE_A))}
    )
    resource_device = registry.async_get_device(
        {(DOMAIN, resource_registry_key(BACKEND_ID, RESOURCE_CT))}
    )
    assert source_device is not None and node_device is not None
    assert resource_device is not None
    assert node_device.via_device_id == source_device.id
    assert resource_device.via_device_id == node_device.id
    key = resource_registry_key(BACKEND_ID, RESOURCE_CT)
    assert registry_unique_ids(hass, entry, key) == {
        f"{key}:{description}"
        for description in {
            "status",
            "type",
            "node",
            "presence",
            "detail_status",
            "lifecycle",
            "observational_continuity",
            "security_continuity",
        }
    }


@pytest.mark.asyncio
async def test_rename_preserves_identity_and_updates_device_name(
    hass: HomeAssistant,
) -> None:
    renamed = replace(INITIAL_RESOURCES[1], name="Cloudflared Renamed")
    second = snapshot(
        (INITIAL_RESOURCES[0], renamed, INITIAL_RESOURCES[2]),
        inventory_revision=11,
        published_state_revision=21,
        published_at="2026-08-08T12:01:00+00:00",
    )
    entry = await setup_entry(
        hass, FakeTransport([snapshot(INITIAL_RESOURCES), second])
    )
    key = resource_registry_key(BACKEND_ID, RESOURCE_CT)
    before = registry_unique_ids(hass, entry, key)
    await entry.runtime_data.async_request_refresh()
    await hass.async_block_till_done()
    assert registry_unique_ids(hass, entry, key) == before
    device = dr.async_get(hass).async_get_device({(DOMAIN, key)})
    assert device is not None and device.name == "CT101 Cloudflared Renamed"


@pytest.mark.asyncio
async def test_node_migration_preserves_identity_and_updates_via_device(
    hass: HomeAssistant,
) -> None:
    moved = replace(
        INITIAL_RESOURCES[1],
        current_node_id=NODE_B,
        resource_continuity_revision=1,
    )
    second = snapshot(
        (INITIAL_RESOURCES[0], moved, INITIAL_RESOURCES[2]),
        nodes=(node(), node(NODE_B, name="pve-b")),
        inventory_revision=11,
        published_state_revision=21,
        published_at="2026-08-08T12:01:00+00:00",
    )
    entry = await setup_entry(
        hass, FakeTransport([snapshot(INITIAL_RESOURCES), second])
    )
    key = resource_registry_key(BACKEND_ID, RESOURCE_CT)
    before = registry_unique_ids(hass, entry, key)
    await entry.runtime_data.async_request_refresh()
    await hass.async_block_till_done()
    registry = dr.async_get(hass)
    child = registry.async_get_device({(DOMAIN, key)})
    parent = registry.async_get_device(
        {(DOMAIN, node_registry_key(BACKEND_ID, NODE_B))}
    )
    assert child is not None and parent is not None
    assert child.via_device_id == parent.id
    assert registry_unique_ids(hass, entry, key) == before


@pytest.mark.asyncio
async def test_unresolved_node_without_history_clears_via_device_id(
    hass: HomeAssistant,
) -> None:
    unresolved = replace(
        INITIAL_RESOURCES[1],
        current_node_id=None,
        last_known_node_id=None,
        node_availability=NodeAvailability.UNRESOLVED,
    )
    second = snapshot(
        (unresolved,),
        inventory_revision=11,
        published_state_revision=21,
        published_at="2026-08-08T12:01:00+00:00",
    )
    entry = await setup_entry(
        hass,
        FakeTransport([snapshot((INITIAL_RESOURCES[1],)), second]),
    )
    registry = dr.async_get(hass)
    key = resource_registry_key(BACKEND_ID, RESOURCE_CT)
    child = registry.async_get_device({(DOMAIN, key)})
    parent = registry.async_get_device(
        {(DOMAIN, node_registry_key(BACKEND_ID, NODE_A))}
    )
    assert child is not None and parent is not None
    original_device_id = child.id
    assert child.via_device_id == parent.id

    await entry.runtime_data.async_request_refresh()
    await hass.async_block_till_done()

    child = registry.async_get_device({(DOMAIN, key)})
    assert child is not None
    assert child.id == original_device_id
    assert child.via_device_id is None


@pytest.mark.asyncio
async def test_unresolved_node_retains_last_known_via_device_id(
    hass: HomeAssistant,
) -> None:
    unresolved = replace(
        INITIAL_RESOURCES[1],
        current_node_id=None,
        last_known_node_id=NODE_A,
        node_availability=NodeAvailability.UNRESOLVED,
    )
    second = snapshot(
        (unresolved,),
        inventory_revision=11,
        published_state_revision=21,
        published_at="2026-08-08T12:01:00+00:00",
    )
    entry = await setup_entry(
        hass,
        FakeTransport([snapshot((INITIAL_RESOURCES[1],)), second]),
    )
    registry = dr.async_get(hass)
    parent = registry.async_get_device(
        {(DOMAIN, node_registry_key(BACKEND_ID, NODE_A))}
    )
    assert parent is not None

    await entry.runtime_data.async_request_refresh()
    await hass.async_block_till_done()

    child = registry.async_get_device(
        {(DOMAIN, resource_registry_key(BACKEND_ID, RESOURCE_CT))}
    )
    assert child is not None
    assert child.via_device_id == parent.id


@pytest.mark.asyncio
async def test_retained_and_successor_generations_share_vmid_without_collision(
    hass: HomeAssistant,
) -> None:
    old_id = "dc38061a-af9b-4a65-96a7-b012e07a459c"
    successor_id = "c4176c22-660a-4484-b8eb-e8390e9a44c6"
    old = absent_resource(
        old_id,
        PresenceState.NOT_CURRENT,
        vmid=101,
        generation=4,
        successor_resource_id=successor_id,
    )
    successor = resource(
        successor_id,
        ResourceType.LXC,
        101,
        "Replacement Container",
        locator_generation=5,
    )
    view = snapshot((successor, old))
    assert view.current_resources_by_locator[(SOURCE_ID, 101)] is successor
    assert set(view.resources_by_id) == {old_id, successor_id}

    entry = await setup_entry(hass, FakeTransport([view]))
    registry = dr.async_get(hass)
    old_key = resource_registry_key(BACKEND_ID, old_id)
    successor_key = resource_registry_key(BACKEND_ID, successor_id)
    old_device = registry.async_get_device({(DOMAIN, old_key)})
    successor_device = registry.async_get_device({(DOMAIN, successor_key)})
    assert old_device is not None and successor_device is not None
    assert old_device.id != successor_device.id
    assert registry_unique_ids(hass, entry, old_key).isdisjoint(
        registry_unique_ids(hass, entry, successor_key)
    )
    assert set(resource_entity_states(hass, entry, old_id).values()) == {
        STATE_UNAVAILABLE
    }
    assert all(
        state != STATE_UNAVAILABLE
        for state in resource_entity_states(hass, entry, successor_id).values()
    )
    assert old_device.via_device_id is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "retained",
    [
        absent_resource(
            RESOURCE_CT,
            PresenceState.MISSING,
            resource_continuity_revision=2,
        ),
        absent_resource(
            RESOURCE_CT,
            PresenceState.CONFIRMED_REMOVED,
            resource_continuity_revision=2,
        ),
    ],
    ids=("missing", "confirmed-removed"),
)
async def test_absent_resource_transition_retains_all_entities_unavailable(
    hass: HomeAssistant,
    retained: ResourceSnapshot,
) -> None:
    first = snapshot((INITIAL_RESOURCES[1],))
    second = snapshot(
        (retained,),
        inventory_revision=11,
        published_state_revision=21,
        published_at="2026-08-08T12:01:00+00:00",
    )
    entry = await setup_entry(hass, FakeTransport([first, second]))
    before = registry_unique_ids(
        hass, entry, resource_registry_key(BACKEND_ID, RESOURCE_CT)
    )

    await entry.runtime_data.async_request_refresh()
    await hass.async_block_till_done()

    after = registry_unique_ids(
        hass, entry, resource_registry_key(BACKEND_ID, RESOURCE_CT)
    )
    assert after == before
    states = resource_entity_states(hass, entry, RESOURCE_CT)
    assert set(states) == {
        "status",
        "type",
        "node",
        "presence",
        "detail_status",
        "lifecycle",
        "observational_continuity",
        "security_continuity",
    }
    assert set(states.values()) == {STATE_UNAVAILABLE}


@pytest.mark.asyncio
async def test_replacement_transition_retains_old_entities_unavailable(
    hass: HomeAssistant,
) -> None:
    successor_id = "c4176c22-660a-4484-b8eb-e8390e9a44c6"
    original = replace(
        INITIAL_RESOURCES[1],
        locator_generation=4,
        resource_continuity_revision=8,
    )
    old = absent_resource(
        RESOURCE_CT,
        PresenceState.NOT_CURRENT,
        vmid=101,
        generation=4,
        resource_continuity_revision=9,
        successor_resource_id=successor_id,
    )
    successor = resource(
        successor_id,
        ResourceType.LXC,
        101,
        "Replacement Container",
        locator_generation=5,
    )
    second = snapshot(
        (old, successor),
        inventory_revision=11,
        published_state_revision=21,
        published_at="2026-08-08T12:01:00+00:00",
    )
    entry = await setup_entry(
        hass, FakeTransport([snapshot((original,)), second])
    )
    old_key = resource_registry_key(BACKEND_ID, RESOURCE_CT)
    before = registry_unique_ids(hass, entry, old_key)

    await entry.runtime_data.async_request_refresh()
    await hass.async_block_till_done()

    assert registry_unique_ids(hass, entry, old_key) == before
    assert set(resource_entity_states(hass, entry, RESOURCE_CT).values()) == {
        STATE_UNAVAILABLE
    }
    successor_states = resource_entity_states(hass, entry, successor_id)
    assert successor_states
    assert all(state != STATE_UNAVAILABLE for state in successor_states.values())


@pytest.mark.asyncio
async def test_present_detail_error_keeps_independent_entities_available(
    hass: HomeAssistant,
) -> None:
    detail_error = replace(
        INITIAL_RESOURCES[1],
        detail_status=DetailStatus.ERROR,
    )
    second = snapshot(
        (detail_error,),
        inventory_revision=11,
        published_state_revision=21,
    )
    entry = await setup_entry(
        hass, FakeTransport([snapshot((INITIAL_RESOURCES[1],)), second])
    )
    await entry.runtime_data.async_request_refresh()
    await hass.async_block_till_done()

    states = resource_entity_states(hass, entry, RESOURCE_CT)
    assert states["status"] == STATE_UNAVAILABLE
    assert states["detail_status"] == "error"
    assert states["presence"] == "present"
    assert states["type"] == "lxc"
    assert states["node"] != STATE_UNAVAILABLE


@pytest.mark.asyncio
async def test_present_unavailable_node_only_blocks_node_dependent_entities(
    hass: HomeAssistant,
) -> None:
    unavailable_node = node(available=False)
    node_unavailable = replace(
        INITIAL_RESOURCES[1],
        node_availability=NodeAvailability.UNAVAILABLE,
    )
    second = snapshot(
        (node_unavailable,),
        nodes=(unavailable_node,),
        inventory_revision=11,
        published_state_revision=21,
    )
    entry = await setup_entry(
        hass, FakeTransport([snapshot((INITIAL_RESOURCES[1],)), second])
    )
    await entry.runtime_data.async_request_refresh()
    await hass.async_block_till_done()

    states = resource_entity_states(hass, entry, RESOURCE_CT)
    assert {
        key for key, state in states.items() if state == STATE_UNAVAILABLE
    } == {"status", "node"}
    assert states["presence"] == "present"
    assert states["detail_status"] == "ok"


@pytest.mark.asyncio
async def test_ambiguity_preserves_resource_binding_generation_and_device(
    hass: HomeAssistant,
) -> None:
    original = resource(
        RESOURCE_CT,
        ResourceType.LXC,
        101,
        "Cloudflared",
        locator_generation=7,
        resource_continuity_revision=8,
    )
    ambiguous = replace(
        original,
        lifecycle=LifecycleState.QUARANTINED,
        observational_continuity=ObservationalContinuity.UNCERTAIN,
        resource_continuity_revision=9,
    )
    second = snapshot(
        (ambiguous,),
        inventory_revision=11,
        published_state_revision=21,
        published_at="2026-08-08T12:01:00+00:00",
    )
    entry = await setup_entry(
        hass, FakeTransport([snapshot((original,)), second])
    )
    await entry.runtime_data.async_request_refresh()
    await hass.async_block_till_done()
    current = entry.runtime_data.data.resources_by_id[RESOURCE_CT]
    assert current.resource_id == original.resource_id
    assert current.active_binding_id == original.active_binding_id
    assert current.locator_generation == 7
    assert set(entry.runtime_data.data.resources_by_id) == {RESOURCE_CT}
    key = resource_registry_key(BACKEND_ID, RESOURCE_CT)
    assert dr.async_get(hass).async_get_device({(DOMAIN, key)}) is not None


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        (
            {"active_binding_id": "replacement-binding"},
            "active binding is immutable",
        ),
        ({"vmid": 102}, "resource locator is immutable"),
        ({"locator_generation": 5}, "locator_generation is immutable"),
    ],
)
def test_same_resource_cannot_change_locator_binding_or_generation(
    mutation: dict[str, Any], match: str
) -> None:
    original = resource(
        RESOURCE_CT,
        ResourceType.LXC,
        101,
        "Cloudflared",
        active_binding_id="binding-a",
        locator_generation=4,
        resource_continuity_revision=8,
    )
    incoming = replace(original, **mutation)
    with pytest.raises(ValueError, match=match):
        snapshot(
            (incoming,),
            inventory_revision=11,
            published_state_revision=21,
        ).validate_revision_successor(snapshot((original,)))


@pytest.mark.parametrize("reopened_presence", [PresenceState.PRESENT, PresenceState.MISSING])
def test_terminal_resource_cannot_be_reopened_as_nonterminal(
    reopened_presence: PresenceState,
) -> None:
    terminal = absent_resource(
        RESOURCE_CT,
        PresenceState.CONFIRMED_REMOVED,
        vmid=101,
        generation=4,
        resource_continuity_revision=9,
    )
    if reopened_presence is PresenceState.PRESENT:
        reopened = resource(
            RESOURCE_CT,
            ResourceType.LXC,
            101,
            "Reopened",
            active_binding_id="binding-reopened",
            locator_generation=4,
            resource_continuity_revision=10,
        )
    else:
        reopened = absent_resource(
            RESOURCE_CT,
            PresenceState.MISSING,
            vmid=101,
            generation=4,
            resource_continuity_revision=10,
        )
    with pytest.raises(ValueError, match="terminal resource cannot be reopened"):
        snapshot(
            (reopened,),
            inventory_revision=12,
            published_state_revision=22,
        ).validate_revision_successor(
            snapshot(
                (terminal,),
                inventory_revision=11,
                published_state_revision=21,
            )
        )


@pytest.mark.parametrize(
    "incoming",
    [
        replace(
            resource(
                RESOURCE_CT,
                ResourceType.LXC,
                101,
                "Cloudflared",
                resource_continuity_revision=8,
            ),
            lifecycle=LifecycleState.QUARANTINED,
            observational_continuity=ObservationalContinuity.UNCERTAIN,
        ),
        replace(
            resource(
                RESOURCE_CT,
                ResourceType.LXC,
                101,
                "Cloudflared",
                resource_continuity_revision=8,
                security_continuity=SecurityContinuity.TRUSTED,
            ),
            lifecycle=LifecycleState.QUARANTINED,
            observational_continuity=ObservationalContinuity.UNCERTAIN,
            security_continuity=SecurityContinuity.REVOKED,
        ),
        absent_resource(
            RESOURCE_CT,
            PresenceState.CONFIRMED_REMOVED,
            vmid=101,
            resource_continuity_revision=8,
        ),
    ],
)
def test_security_relevant_transition_requires_newer_continuity_revision(
    incoming: ResourceSnapshot,
) -> None:
    previous_security = (
        SecurityContinuity.TRUSTED
        if incoming.security_continuity is SecurityContinuity.REVOKED
        else SecurityContinuity.UNVERIFIED
    )
    original = resource(
        RESOURCE_CT,
        ResourceType.LXC,
        101,
        "Cloudflared",
        resource_continuity_revision=8,
        security_continuity=previous_security,
    )
    with pytest.raises(ValueError, match="requires a newer"):
        snapshot(
            (incoming,),
            inventory_revision=11,
            published_state_revision=21,
        ).validate_revision_successor(snapshot((original,)))


@pytest.mark.parametrize("ambiguous_presence", [PresenceState.PRESENT, PresenceState.MISSING])
def test_ambiguity_and_missing_keep_exact_binding_and_generation(
    ambiguous_presence: PresenceState,
) -> None:
    original = resource(
        RESOURCE_CT,
        ResourceType.LXC,
        101,
        "Cloudflared",
        active_binding_id="binding-a",
        locator_generation=4,
        resource_continuity_revision=8,
    )
    ambiguous = replace(
        original,
        presence=ambiguous_presence,
        lifecycle=LifecycleState.QUARANTINED,
        observational_continuity=ObservationalContinuity.UNCERTAIN,
        detail_status=(
            DetailStatus.OK
            if ambiguous_presence is PresenceState.PRESENT
            else DetailStatus.NOT_APPLICABLE
        ),
        current_node_id=(NODE_A if ambiguous_presence is PresenceState.PRESENT else None),
        last_known_node_id=(None if ambiguous_presence is PresenceState.PRESENT else NODE_A),
        node_availability=(
            NodeAvailability.AVAILABLE
            if ambiguous_presence is PresenceState.PRESENT
            else NodeAvailability.NOT_APPLICABLE
        ),
        resource_continuity_revision=9,
    )
    incoming = snapshot(
        (ambiguous,),
        inventory_revision=11,
        published_state_revision=21,
    )
    incoming.validate_revision_successor(snapshot((original,)))
    assert ambiguous.active_binding_id == original.active_binding_id
    assert ambiguous.locator_generation == original.locator_generation


def test_accepted_terminal_closure_keeps_incarnation_locator() -> None:
    original = resource(
        RESOURCE_CT,
        ResourceType.LXC,
        101,
        "Cloudflared",
        active_binding_id="binding-a",
        locator_generation=4,
        resource_continuity_revision=8,
    )
    terminal = absent_resource(
        RESOURCE_CT,
        PresenceState.CONFIRMED_REMOVED,
        vmid=101,
        generation=4,
        resource_continuity_revision=9,
    )
    snapshot(
        (terminal,),
        inventory_revision=11,
        published_state_revision=21,
    ).validate_revision_successor(snapshot((original,)))
    assert terminal.active_binding_id is None


def test_continuity_revision_may_advance_without_visible_axis_change() -> None:
    original = resource(
        RESOURCE_CT,
        ResourceType.LXC,
        101,
        "Cloudflared",
        resource_continuity_revision=8,
    )
    revised = replace(original, resource_continuity_revision=11)
    snapshot(
        (revised,),
        inventory_revision=11,
        published_state_revision=21,
    ).validate_revision_successor(snapshot((original,)))


def test_direct_replacement_closes_old_and_uses_separate_successor_resource() -> None:
    old_id = "dc38061a-af9b-4a65-96a7-b012e07a459c"
    successor_id = "c4176c22-660a-4484-b8eb-e8390e9a44c6"
    original = resource(
        old_id,
        ResourceType.LXC,
        101,
        "Old Container",
        active_binding_id="binding-old",
        locator_generation=4,
        resource_continuity_revision=8,
    )
    old_terminal = absent_resource(
        old_id,
        PresenceState.NOT_CURRENT,
        vmid=101,
        generation=4,
        resource_continuity_revision=9,
        successor_resource_id=successor_id,
    )
    successor = resource(
        successor_id,
        ResourceType.LXC,
        101,
        "Successor",
        active_binding_id="binding-successor",
        locator_generation=5,
    )
    incoming = snapshot(
        (old_terminal, successor),
        inventory_revision=11,
        published_state_revision=21,
    )
    incoming.validate_revision_successor(snapshot((original,)))
    assert incoming.current_resources_by_locator[(SOURCE_ID, 101)] is successor


@pytest.mark.parametrize(
    "detail_status",
    [DetailStatus.TEMPORARILY_UNAVAILABLE, DetailStatus.ERROR],
)
def test_present_locator_accepts_independent_detail_failure(
    detail_status: DetailStatus,
) -> None:
    item = resource(
        RESOURCE_ADDED,
        ResourceType.LXC,
        777,
        "Detail Failure",
        detail_status=detail_status,
    )
    assert snapshot((item,)).resources[0].presence is PresenceState.PRESENT


def test_present_locator_accepts_unavailable_or_unresolved_node_relation() -> None:
    unavailable = resource(
        RESOURCE_ADDED,
        ResourceType.LXC,
        777,
        "Unavailable Node",
        node_availability=NodeAvailability.UNAVAILABLE,
    )
    view = snapshot((unavailable,), nodes=(node(available=False),))
    assert view.resources[0].presence is PresenceState.PRESENT

    unresolved = replace(
        unavailable,
        current_node_id=None,
        last_known_node_id=NODE_A,
        node_availability=NodeAvailability.UNRESOLVED,
    )
    assert snapshot((unresolved,)).resources[0].resource_id == RESOURCE_ADDED


@pytest.mark.parametrize(
    "presence",
    [
        PresenceState.MISSING,
        PresenceState.CONFIRMED_REMOVED,
        PresenceState.NOT_CURRENT,
    ],
)
def test_absent_and_terminal_states_require_not_applicable_detail(
    presence: PresenceState,
) -> None:
    successor_id = RESOURCE_ADDED if presence is PresenceState.NOT_CURRENT else None
    old = absent_resource(
        RESOURCE_CT,
        presence,
        successor_resource_id=successor_id,
    )
    resources = (
        old,
        resource(RESOURCE_ADDED, ResourceType.LXC, 777, "Successor", locator_generation=2),
    ) if successor_id else (old,)
    assert snapshot(resources).resources[0].detail_status is DetailStatus.NOT_APPLICABLE


@pytest.mark.parametrize(
    "mutation,match",
    [
        ({"detail_status": DetailStatus.NOT_APPLICABLE}, "current detail status"),
        (
            {
                "presence": PresenceState.MISSING,
                "lifecycle": LifecycleState.QUARANTINED,
                "observational_continuity": ObservationalContinuity.UNCERTAIN,
                "current_node_id": None,
                "last_known_node_id": NODE_A,
                "node_availability": NodeAvailability.NOT_APPLICABLE,
            },
            "require detail_status=not_applicable",
        ),
        (
            {
                "lifecycle": LifecycleState.RETIRED,
                "observational_continuity": ObservationalContinuity.CONSISTENT,
            },
            "canonical state matrix",
        ),
        (
            {
                "current_node_id": None,
                "last_known_node_id": NODE_A,
                "node_availability": NodeAvailability.AVAILABLE,
            },
            "unresolved current node relation",
        ),
    ],
)
def test_resource_validator_rejects_invalid_axis_combinations(
    mutation: dict[str, Any], match: str
) -> None:
    valid = resource(
        RESOURCE_ADDED, ResourceType.LXC, 777, "Invalid Candidate"
    )
    with pytest.raises(ValueError, match=match):
        replace(valid, **mutation)


def test_snapshot_rejects_dangling_current_and_last_known_nodes() -> None:
    dangling_current = resource(
        RESOURCE_ADDED,
        ResourceType.LXC,
        777,
        "Dangling",
        current_node_id=NODE_B,
    )
    with pytest.raises(ValueError, match="node absent"):
        snapshot((dangling_current,))

    dangling_last = absent_resource(RESOURCE_ADDED, PresenceState.MISSING)
    with pytest.raises(ValueError, match="node absent"):
        snapshot((replace(dangling_last, last_known_node_id=NODE_B),))


@pytest.mark.asyncio
async def test_backend_reachable_with_unavailable_source_keeps_resource_presence(
    hass: HomeAssistant,
) -> None:
    first = snapshot((INITIAL_RESOURCES[1],))
    second = snapshot(
        (INITIAL_RESOURCES[1],),
        sources=(unavailable_source(),),
        inventory_revision=10,
        published_state_revision=21,
        published_at="2026-08-08T12:01:00+00:00",
    )
    entry = await setup_entry(hass, FakeTransport([first, second]))
    await entry.runtime_data.async_request_refresh()
    await hass.async_block_till_done()
    current = entry.runtime_data.data.resources_by_id[RESOURCE_CT]
    assert current.presence is PresenceState.PRESENT
    assert entry.runtime_data.last_update_success is True

    entity_registry = er.async_get(hass)
    source_key = source_registry_key(BACKEND_ID, SOURCE_ID)
    source_health = next(
        item
        for item in er.async_entries_for_config_entry(entity_registry, entry.entry_id)
        if item.unique_id == f"{source_key}:health"
    )
    resource_key = resource_registry_key(BACKEND_ID, RESOURCE_CT)
    runtime_status = next(
        item
        for item in er.async_entries_for_config_entry(entity_registry, entry.entry_id)
        if item.unique_id == f"{resource_key}:status"
    )
    presence_entity = next(
        item
        for item in er.async_entries_for_config_entry(entity_registry, entry.entry_id)
        if item.unique_id == f"{resource_key}:presence"
    )
    assert hass.states.get(source_health.entity_id).state == "source_unavailable"
    assert hass.states.get(runtime_status.entity_id).state == STATE_UNAVAILABLE
    assert hass.states.get(presence_entity.entity_id).state == "present"


def test_source_contract_carries_fixed_provenance_and_initial_semantics() -> None:
    healthy = source()
    assert healthy.current_context == healthy.committed_context
    assert healthy.freshness_reference_at is not None
    assert healthy.freshness_valid_until is not None

    initial = source(
        health=SourceHealth.NOT_YET_OBSERVED,
        freshness=SourceFreshness.NOT_YET_OBSERVED,
        health_origin=SourceHealthOrigin.INITIAL,
        health_reason="",
        last_issued_run_sequence=0,
        latest_completed_run_sequence=None,
        latest_completed_outcome=None,
        last_health_run_sequence=None,
        last_run_health_outcome=None,
        last_committed_run_sequence=None,
    )
    assert initial.committed_context is None
    assert initial.freshness_valid_until is None

    with pytest.raises(ValueError, match="healthy authoritative discovery commit"):
        replace(healthy, freshness=SourceFreshness.FRESH, health=SourceHealth.DEGRADED)

    with pytest.raises(ValueError, match="healthy authoritative discovery commit"):
        source(
            last_issued_run_sequence=6,
            latest_completed_run_sequence=6,
            last_health_run_sequence=6,
            last_committed_run_sequence=5,
        )

    later_audit_only_completion = source(
        last_issued_run_sequence=6,
        latest_completed_run_sequence=6,
        latest_completed_outcome="audit_only_inapplicable",
        last_health_run_sequence=5,
        last_committed_run_sequence=5,
    )
    assert later_audit_only_completion.freshness is SourceFreshness.FRESH


@pytest.mark.parametrize(
    ("incoming_source", "match"),
    [
        (
            source(
                latest_completed_run_sequence=None,
                latest_completed_outcome=None,
            ),
            "latest_completed_run_sequence cannot be cleared",
        ),
        (
            source(
                health=SourceHealth.DEGRADED,
                freshness=SourceFreshness.STALE,
                health_origin=SourceHealthOrigin.CONTROLLED_CONTEXT_TRANSITION,
                health_reason="provenance_erased",
                last_health_run_sequence=None,
                last_run_health_outcome=None,
                last_committed_run_sequence=None,
            ),
            "last_health_run_sequence cannot be cleared",
        ),
        (
            source(
                health=SourceHealth.SOURCE_UNAVAILABLE,
                freshness=SourceFreshness.STALE,
                health_reason="newer_failure_without_commit_provenance",
                last_issued_run_sequence=6,
                latest_completed_run_sequence=6,
                latest_completed_outcome="source_unavailable",
                last_health_run_sequence=6,
                last_run_health_outcome="source_unavailable",
                last_committed_run_sequence=None,
            ),
            "last_committed_run_sequence cannot be cleared",
        ),
    ],
)
def test_source_durable_sequence_cannot_return_to_unset(
    incoming_source: InventorySourceSnapshot,
    match: str,
) -> None:
    incoming = snapshot(
        (),
        sources=(incoming_source,),
        published_state_revision=21,
    )
    with pytest.raises(ValueError, match=match):
        incoming.validate_revision_successor(snapshot((), sources=(source(),)))


@pytest.mark.parametrize(
    ("incoming_source", "match"),
    [
        (
            replace(source(), latest_completed_outcome="rewritten_completion"),
            "latest completed outcome is immutable",
        ),
        (
            replace(source(), last_run_health_outcome="rewritten_health"),
            "last run health outcome is immutable",
        ),
        (
            source(
                current_context=source_context(revision=4),
                committed_context=source_context(revision=4),
            ),
            "successful commit provenance is immutable",
        ),
    ],
)
def test_same_source_sequence_cannot_rewrite_provenance(
    incoming_source: InventorySourceSnapshot,
    match: str,
) -> None:
    incoming = snapshot(
        (),
        sources=(incoming_source,),
        published_state_revision=21,
    )
    with pytest.raises(ValueError, match=match):
        incoming.validate_revision_successor(snapshot((), sources=(source(),)))


@pytest.mark.parametrize(
    ("field_name", "changed_value"),
    [
        ("last_successful_observed_at", "2026-08-08T12:00:30+00:00"),
        ("freshness_reference_at", "2026-08-08T12:00:00+00:00"),
        ("freshness_valid_until", "2026-08-08T12:05:00+00:00"),
    ],
)
def test_same_committed_sequence_cannot_rewrite_fixed_timestamps(
    field_name: str,
    changed_value: str,
) -> None:
    incoming_source = replace(source(), **{field_name: changed_value})
    incoming = snapshot(
        (),
        sources=(incoming_source,),
        published_state_revision=21,
    )
    with pytest.raises(ValueError, match="successful commit provenance is immutable"):
        incoming.validate_revision_successor(snapshot((), sources=(source(),)))


def test_higher_source_sequences_may_publish_new_provenance() -> None:
    previous = snapshot((), sources=(source(),))

    higher_completion = source(
        last_issued_run_sequence=6,
        latest_completed_run_sequence=6,
        latest_completed_outcome="audit_only_inapplicable",
        last_health_run_sequence=5,
        last_committed_run_sequence=5,
    )
    snapshot(
        (),
        sources=(higher_completion,),
        published_state_revision=21,
    ).validate_revision_successor(previous)

    higher_health = source(
        health=SourceHealth.SOURCE_UNAVAILABLE,
        freshness=SourceFreshness.STALE,
        health_reason="newer_failed_health",
        last_issued_run_sequence=6,
        latest_completed_run_sequence=6,
        latest_completed_outcome="source_unavailable",
        last_health_run_sequence=6,
        last_run_health_outcome="source_unavailable",
        last_committed_run_sequence=5,
    )
    snapshot(
        (),
        sources=(higher_health,),
        published_state_revision=21,
    ).validate_revision_successor(previous)

    higher_commit = replace(
        source(
            last_issued_run_sequence=6,
            latest_completed_run_sequence=6,
            last_health_run_sequence=6,
            last_committed_run_sequence=6,
            current_context=source_context(revision=4),
        ),
        last_successful_observed_at="2026-08-08T12:01:30+00:00",
        freshness_reference_at="2026-08-08T12:01:00+00:00",
        freshness_valid_until="2026-08-08T12:06:00+00:00",
    )
    snapshot(
        (),
        sources=(higher_commit,),
        inventory_revision=11,
        published_state_revision=21,
    ).validate_revision_successor(previous)


def test_health_only_expiry_retains_exact_committed_provenance() -> None:
    committed = source()
    expired = replace(
        committed,
        health=SourceHealth.DEGRADED,
        freshness=SourceFreshness.STALE,
        health_origin=SourceHealthOrigin.TIME_EXPIRY,
        health_reason="freshness_deadline_expired",
    )
    snapshot(
        (),
        sources=(expired,),
        published_state_revision=21,
    ).validate_revision_successor(snapshot((), sources=(committed,)))
    assert expired.committed_context == committed.committed_context
    assert expired.freshness_valid_until == committed.freshness_valid_until


@pytest.mark.parametrize(
    ("previous_source", "incoming_source", "match"),
    [
        (
            source(current_context=source_context(revision=4)),
            source(current_context=source_context(revision=3)),
            "source_config_revision",
        ),
        (
            source(
                current_context=source_context(transport_trust_revision=3)
            ),
            source(
                current_context=source_context(transport_trust_revision=2)
            ),
            "transport_trust_revision",
        ),
        (
            source(last_issued_run_sequence=6),
            source(last_issued_run_sequence=5),
            "last_issued_run_sequence",
        ),
        (
            source(
                health=SourceHealth.SOURCE_UNAVAILABLE,
                freshness=SourceFreshness.STALE,
                health_reason="audit_only_completion",
                last_issued_run_sequence=7,
                latest_completed_run_sequence=6,
                latest_completed_outcome="audit_only",
                last_health_run_sequence=5,
                last_run_health_outcome="success",
                last_committed_run_sequence=5,
            ),
            source(
                health=SourceHealth.SOURCE_UNAVAILABLE,
                freshness=SourceFreshness.STALE,
                health_reason="older_audit_completion",
                last_issued_run_sequence=8,
                latest_completed_run_sequence=5,
                latest_completed_outcome="success",
                last_health_run_sequence=5,
                last_run_health_outcome="success",
                last_committed_run_sequence=5,
            ),
            "latest_completed_run_sequence",
        ),
        (
            source(
                health=SourceHealth.SOURCE_UNAVAILABLE,
                freshness=SourceFreshness.STALE,
                health_reason="newer_failed_health",
                last_issued_run_sequence=7,
                latest_completed_run_sequence=7,
                latest_completed_outcome="source_unavailable",
                last_health_run_sequence=6,
                last_run_health_outcome="source_unavailable",
                last_committed_run_sequence=5,
            ),
            source(
                health=SourceHealth.SOURCE_UNAVAILABLE,
                freshness=SourceFreshness.STALE,
                health_reason="older_failed_health",
                last_issued_run_sequence=8,
                latest_completed_run_sequence=8,
                latest_completed_outcome="source_unavailable",
                last_health_run_sequence=5,
                last_run_health_outcome="source_unavailable",
                last_committed_run_sequence=5,
            ),
            "last_health_run_sequence",
        ),
        (
            source(
                last_issued_run_sequence=6,
                latest_completed_run_sequence=6,
                last_health_run_sequence=6,
                last_committed_run_sequence=6,
            ),
            source(
                health=SourceHealth.SOURCE_UNAVAILABLE,
                freshness=SourceFreshness.STALE,
                health_reason="newer_failure_retains_older_commit",
                last_issued_run_sequence=7,
                latest_completed_run_sequence=7,
                latest_completed_outcome="source_unavailable",
                last_health_run_sequence=7,
                last_run_health_outcome="source_unavailable",
                last_committed_run_sequence=5,
            ),
            "last_committed_run_sequence",
        ),
    ],
)
def test_source_monotonic_provenance_rejects_rollbacks(
    previous_source: InventorySourceSnapshot,
    incoming_source: InventorySourceSnapshot,
    match: str,
) -> None:
    previous = snapshot((), sources=(previous_source,))
    incoming = snapshot(
        (),
        sources=(incoming_source,),
        published_state_revision=21,
        published_at="2026-08-08T12:01:00+00:00",
    )
    with pytest.raises(ValueError, match=match):
        incoming.validate_revision_successor(previous)


def test_provider_kind_is_immutable_for_existing_source() -> None:
    incoming = snapshot(
        (),
        sources=(replace(source(), provider_kind="other-provider"),),
        inventory_revision=11,
        published_state_revision=21,
    )
    with pytest.raises(ValueError, match="provider_kind is immutable"):
        incoming.validate_revision_successor(snapshot((), sources=(source(),)))


@pytest.mark.parametrize(
    ("incoming_context", "match"),
    [
        (
            source_context(endpoint_id="different-endpoint"),
            "endpoint_id is immutable",
        ),
        (
            source_context(
                canonical_transport_locator="https://other-pve.example.test:8006"
            ),
            "canonical transport locator is immutable",
        ),
        (
            source_context(canonicalization_contract_version=2),
            "canonicalization migration requires a newer",
        ),
    ],
)
def test_unrevisioned_current_route_change_is_rejected(
    incoming_context: SourceContext,
    match: str,
) -> None:
    committed_context = source_context()
    controlled_transition = source(
        health=SourceHealth.DEGRADED,
        freshness=SourceFreshness.STALE,
        health_origin=SourceHealthOrigin.CONTROLLED_CONTEXT_TRANSITION,
        health_reason="active_route_changed",
        current_context=incoming_context,
        committed_context=committed_context,
    )
    incoming = snapshot(
        (),
        sources=(controlled_transition,),
        published_state_revision=21,
    )
    with pytest.raises(ValueError, match=match):
        incoming.validate_revision_successor(
            snapshot((), sources=(source(current_context=committed_context),))
        )


def test_active_endpoint_is_immutable_even_with_newer_source_config_revision() -> None:
    previous_context = source_context(
        endpoint_id="endpoint-a",
        canonical_transport_locator="https://pve-a.example.test:8006",
    )
    incoming_context = source_context(
        revision=4,
        endpoint_id="endpoint-b",
        canonical_transport_locator="https://pve-a.example.test:8006",
    )
    controlled_transition = source(
        health=SourceHealth.DEGRADED,
        freshness=SourceFreshness.STALE,
        health_origin=SourceHealthOrigin.CONTROLLED_CONTEXT_TRANSITION,
        health_reason="active_endpoint_replacement_attempt",
        current_context=incoming_context,
        committed_context=previous_context,
    )
    incoming = snapshot(
        (),
        sources=(controlled_transition,),
        published_state_revision=21,
    )
    with pytest.raises(ValueError, match="endpoint_id is immutable"):
        incoming.validate_revision_successor(
            snapshot((), sources=(source(current_context=previous_context),))
        )


def test_canonical_locator_cannot_change_within_same_contract_version() -> None:
    previous_context = source_context(
        endpoint_id="endpoint-a",
        canonical_transport_locator="https://pve-a.example.test:8006",
    )
    incoming_context = source_context(
        revision=4,
        endpoint_id="endpoint-a",
        canonical_transport_locator="https://pve-alias.example.test:8006",
    )
    controlled_transition = source(
        health=SourceHealth.DEGRADED,
        freshness=SourceFreshness.STALE,
        health_origin=SourceHealthOrigin.CONTROLLED_CONTEXT_TRANSITION,
        health_reason="canonical_locator_rewrite_attempt",
        current_context=incoming_context,
        committed_context=previous_context,
    )
    incoming = snapshot(
        (),
        sources=(controlled_transition,),
        published_state_revision=21,
    )
    with pytest.raises(ValueError, match="canonical transport locator is immutable"):
        incoming.validate_revision_successor(
            snapshot((), sources=(source(current_context=previous_context),))
        )


def test_source_config_revision_may_rise_without_route_change() -> None:
    previous_context = source_context(revision=3)
    current_context = source_context(revision=4)
    controlled_transition = source(
        health=SourceHealth.DEGRADED,
        freshness=SourceFreshness.STALE,
        health_origin=SourceHealthOrigin.CONTROLLED_CONTEXT_TRANSITION,
        health_reason="credential_configuration_changed",
        current_context=current_context,
        committed_context=previous_context,
    )
    snapshot(
        (),
        sources=(controlled_transition,),
        published_state_revision=21,
    ).validate_revision_successor(
        snapshot((), sources=(source(current_context=previous_context),))
    )


def test_transport_trust_revision_may_rise_for_same_endpoint() -> None:
    previous_context = source_context(transport_trust_revision=2)
    current_context = source_context(transport_trust_revision=3)
    controlled_transition = source(
        health=SourceHealth.DEGRADED,
        freshness=SourceFreshness.STALE,
        health_origin=SourceHealthOrigin.CONTROLLED_CONTEXT_TRANSITION,
        health_reason="transport_trust_changed",
        current_context=current_context,
        committed_context=previous_context,
    )
    snapshot(
        (),
        sources=(controlled_transition,),
        published_state_revision=21,
    ).validate_revision_successor(
        snapshot((), sources=(source(current_context=previous_context),))
    )


def test_controlled_canonicalization_migration_may_change_locator() -> None:
    previous_context = source_context(
        revision=3,
        endpoint_id="endpoint-a",
        canonical_transport_locator="https://PVE.EXAMPLE.test:8006/",
        canonicalization_contract_version=1,
    )
    migrated_context = source_context(
        revision=4,
        endpoint_id="endpoint-a",
        canonical_transport_locator="https://pve.example.test:8006",
        canonicalization_contract_version=2,
    )
    controlled_transition = source(
        health=SourceHealth.DEGRADED,
        freshness=SourceFreshness.STALE,
        health_origin=SourceHealthOrigin.CONTROLLED_CONTEXT_TRANSITION,
        health_reason="canonicalization_contract_migrated",
        current_context=migrated_context,
        committed_context=previous_context,
    )
    snapshot(
        (),
        sources=(controlled_transition,),
        published_state_revision=21,
    ).validate_revision_successor(
        snapshot((), sources=(source(current_context=previous_context),))
    )


def test_node_identity_cannot_move_between_inventory_sources() -> None:
    previous = snapshot(
        (),
        sources=(source(),),
        nodes=(node(),),
    )
    incoming = snapshot(
        (),
        sources=(
            source(),
            source(
                inventory_source_id=SOURCE_B_ID,
                current_context=source_context(
                    endpoint_id="9e2ef36f-f6db-4e23-93fe-85ad573682f5"
                ),
            ),
        ),
        nodes=(node(inventory_source_id=SOURCE_B_ID),),
        inventory_revision=11,
        published_state_revision=21,
    )
    with pytest.raises(ValueError, match="node identity cannot move between sources"):
        incoming.validate_revision_successor(previous)


def test_node_display_and_runtime_facts_may_change_within_same_source() -> None:
    original = node(facts={"cpu": 0.1})
    updated = replace(
        original,
        name="pve-a-renamed",
        status="maintenance",
        available=False,
        facts={"cpu": 0.4, "maintenance": True},
    )
    incoming = snapshot(
        (),
        sources=(source(),),
        nodes=(updated,),
        inventory_revision=11,
        published_state_revision=21,
    )
    incoming.validate_revision_successor(
        snapshot((), sources=(source(),), nodes=(original,))
    )


def test_published_revisions_are_monotonic_and_one_revision_is_immutable() -> None:
    first = snapshot((INITIAL_RESOURCES[1],))
    changed_same_revision = snapshot(
        (replace(INITIAL_RESOURCES[1], name="Changed"),)
    )
    with pytest.raises(ValueError, match="one immutable view"):
        changed_same_revision.validate_revision_successor(first)
    with pytest.raises(ValueError, match="must not regress"):
        replace(first, published_state_revision=19).validate_revision_successor(first)
    with pytest.raises(ValueError, match="must not regress"):
        replace(first, inventory_revision=9, published_state_revision=21).validate_revision_successor(first)


def test_snapshot_mappings_are_deeply_immutable() -> None:
    mutable = {"nested": {"values": [1, {"flag": True}]}}
    frozen = resource(
        RESOURCE_ADDED,
        ResourceType.LXC,
        777,
        "Immutable",
        state=mutable,
        retained_policy=mutable,
    )
    mutable["nested"]["values"].append(2)
    assert frozen.state["nested"]["values"] == (1, {"flag": True})
    with pytest.raises(TypeError):
        frozen.state["new"] = "mutation"  # type: ignore[index]
    view = snapshot((frozen,))
    with pytest.raises(TypeError):
        view.resources_by_id[RESOURCE_CT] = frozen  # type: ignore[index]
    with pytest.raises(TypeError, match="JSON-like"):
        replace(frozen, state={"payload": bytearray(b"mutable")})


@pytest.mark.asyncio
async def test_diagnostics_redact_secrets_across_new_source_shape(
    hass: HomeAssistant,
) -> None:
    secrets = {
        "authorization": "Bearer lower-authorization-value",
        "Authorization": "Bearer upper-authorization-value",
        "authorization_header": "Bearer header-authorization-value",
        "bearer_token": "bearer-token-value",
        "mqtt_password": "mqtt-password-value",
        "private_key": "private-key-value",
        "webhook_id": "webhook-id-value",
        "client_secret": "client-secret-value",
        "api_key": "api-key-value",
        "ssh_key": "ssh-key-value",
        "some-service-token": "service-token-value",
        "backend_token": "backend-token-value",
        "update_token": "update-token-value",
        "recovery_token": "recovery-token-value",
        "token_value": "token-value-value",
        "passphrase": "passphrase-value",
        "host_authorization": "Bearer host-authorization-value",
        "ha_ssh_key": "ha-ssh-key-value",
        "webhook_url": "https://hooks.example.test/api/webhook/private-id",
        "credential_object": "credential-object-value",
        "nested_secret": "nested-secret-container-value",
        "private_scan_url": "https://private.example.test/api/v1/scan",
        "private_service_url": "https://private.example.test/api/v1/service",
        "canonical_locator": "https://pve.example.test:8006",
    }
    sensitive_source = source(
        facts={
            "authorization": secrets["authorization"],
            "endpoint": {
                "api_key": secrets["api_key"],
                "documentation_url": "https://docs.example.test/source",
            },
            "service": {"client_secret": secrets["client_secret"]},
            "hubinet_ops_service_url": secrets["private_service_url"],
        }
    )
    sensitive_node = node(
        facts={
            "MQTT_PASSWORD": secrets["mqtt_password"],
            "nested": {"private_key": secrets["private_key"], "memory": 4096},
            "events": (
                {
                    "webhook_id": secrets["webhook_id"],
                    "display": "visible-event",
                },
            ),
            "cpu": 0.25,
        }
    )
    sensitive_resource = resource(
        RESOURCE_TEST,
        ResourceType.LXC,
        666,
        "Sensitive",
        retained_policy={
            "authorization": secrets["authorization"],
            "client_secret": secrets["client_secret"],
            "backend_token": secrets["backend_token"],
            "repository_secrets": {
                "update_token": secrets["update_token"],
                "recovery_token": secrets["recovery_token"],
                "token_value": secrets["token_value"],
                "passphrase": secrets["passphrase"],
                "hubinet_ops_host_recovery_authorization": secrets[
                    "host_authorization"
                ],
                "HUBINET_OPS_HA_SSH_KEY": secrets["ha_ssh_key"],
                "webhook_url": secrets["webhook_url"],
                "credentials": {"value": secrets["credential_object"]},
            },
            "managed": False,
        },
        state={
            "Authorization": secrets["Authorization"],
            "authorization_header": secrets["authorization_header"],
            "headers": {
                "Authorization": secrets["authorization_header"],
                "Content-Type": "application/json",
            },
            "bearer_token": secrets["bearer_token"],
            "api_key": secrets["api_key"],
            "ssh_key": secrets["ssh_key"],
            "webhook_url": secrets["webhook_url"],
            "events": [
                {
                    "some-service-token": secrets["some-service-token"],
                    "availability": "online",
                }
            ],
            "hubinet_ops_scan_url": secrets["private_scan_url"],
            "security": {
                "secrets": {"value": secrets["nested_secret"]},
                "mode": "visible-mode",
            },
            "device_id": "visible-device-id",
            "registry_key": "visible-registry-key",
            "token_id": "visible-token-id",
            "backend_token_env": "VISIBLE_BACKEND_TOKEN_ENV",
            "ssh_key_dir": "/visible/key-directory",
            "secrets_file": "/visible/secrets-file-path",
            "documentation_url": "https://docs.example.test/resource",
            "resource_type": "lxc",
            "vmid": 666,
        },
    )
    entry = await setup_entry(
        hass,
        FakeTransport(
            [
                snapshot(
                    (sensitive_resource,),
                    sources=(sensitive_source,),
                    nodes=(sensitive_node,),
                )
            ]
        ),
    )
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["config_entry"]["data"][CONF_API_TOKEN] == REDACTED
    assert diagnostics["config_entry"]["data"][CONF_BASE_URL] == REDACTED
    source_data = diagnostics["snapshot"]["sources"][0]
    assert source_data["current_context"]["canonical_transport_locator"] == REDACTED
    assert source_data["committed_context"]["canonical_transport_locator"] == REDACTED
    assert source_data["facts"]["authorization"] == REDACTED
    assert source_data["facts"]["endpoint"]["api_key"] == REDACTED
    assert source_data["facts"]["service"]["client_secret"] == REDACTED
    assert source_data["facts"]["hubinet_ops_service_url"] == REDACTED
    assert source_data["facts"]["endpoint"]["documentation_url"].startswith("https://docs")
    node_data = diagnostics["snapshot"]["nodes"][0]
    assert node_data["facts"]["MQTT_PASSWORD"] == REDACTED
    assert node_data["facts"]["nested"]["private_key"] == REDACTED
    assert node_data["facts"]["events"][0]["webhook_id"] == REDACTED
    assert node_data["facts"]["nested"]["memory"] == 4096
    assert node_data["facts"]["events"][0]["display"] == "visible-event"
    assert node_data["facts"]["cpu"] == 0.25
    resource_data = diagnostics["snapshot"]["resources"][0]
    policy = resource_data["retained_policy"]
    assert policy["authorization"] == REDACTED
    assert policy["client_secret"] == REDACTED
    assert policy["managed"] is False
    assert resource_data["retained_policy"]["backend_token"] == REDACTED
    repository_secrets = policy["repository_secrets"]
    assert repository_secrets["update_token"] == REDACTED
    assert repository_secrets["recovery_token"] == REDACTED
    assert repository_secrets["token_value"] == REDACTED
    assert repository_secrets["passphrase"] == REDACTED
    assert repository_secrets["hubinet_ops_host_recovery_authorization"] == REDACTED
    assert repository_secrets["HUBINET_OPS_HA_SSH_KEY"] == REDACTED
    assert repository_secrets["webhook_url"] == REDACTED
    assert repository_secrets["credentials"] == REDACTED
    assert resource_data["state"]["Authorization"] == REDACTED
    assert resource_data["state"]["authorization_header"] == REDACTED
    assert resource_data["state"]["headers"] == REDACTED
    assert resource_data["state"]["bearer_token"] == REDACTED
    assert resource_data["state"]["api_key"] == REDACTED
    assert resource_data["state"]["ssh_key"] == REDACTED
    assert resource_data["state"]["webhook_url"] == REDACTED
    assert resource_data["state"]["events"][0]["some-service-token"] == REDACTED
    assert resource_data["state"]["hubinet_ops_scan_url"] == REDACTED
    assert resource_data["state"]["security"]["secrets"] == REDACTED
    assert resource_data["state"]["security"]["mode"] == "visible-mode"
    for key in (
        "device_id",
        "registry_key",
        "token_id",
        "backend_token_env",
        "ssh_key_dir",
        "secrets_file",
        "documentation_url",
        "resource_type",
        "vmid",
    ):
        assert resource_data["state"][key] == sensitive_resource.state[key]
    assert resource_data["state"]["events"][0]["availability"] == "online"
    diagnostics_repr = repr(diagnostics)
    assert API_TOKEN not in diagnostics_repr
    assert all(value not in diagnostics_repr for value in secrets.values())


def integration_python_sources() -> list[Path]:
    return sorted(Path("custom_components/hubinet_ops").glob("*.py"))


def test_client_has_no_proxmox_mqtt_authority_or_mutation_path() -> None:
    combined = "\n".join(
        path.read_text(encoding="utf-8") for path in integration_python_sources()
    ).lower()
    for forbidden in (
        "proxmoxer",
        "proxmoxapi",
        "press_action",
        "via_device=",
        ".status.start",
        ".snapshot.post",
        "app.mqtt",
        "mqtt discovery",
        "home-assistant/packages",
    ):
        assert forbidden not in combined
    assert "preserving_unconfirmed_missing" not in combined
    assert "resources_by_vmid" not in combined


def test_production_integration_has_no_hardcoded_current_vmids() -> None:
    forbidden = set(range(100, 111))
    occurrences: list[tuple[Path, int, int]] = []
    for path in integration_python_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        occurrences.extend(
            (path, item.lineno, item.value)
            for item in ast.walk(tree)
            if isinstance(item, ast.Constant)
            and type(item.value) is int
            and item.value in forbidden
        )
    assert occurrences == []


def test_resource_locator_rejects_non_integer_vmid() -> None:
    with pytest.raises(ValueError, match="positive integer"):
        resource(
            RESOURCE_ADDED,
            ResourceType.LXC,
            1.5,  # type: ignore[arg-type]
            "Invalid VMID",
        )
