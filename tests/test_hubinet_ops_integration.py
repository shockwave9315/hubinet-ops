from __future__ import annotations

import ast
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("homeassistant", reason="isolated HA test dependencies not installed")

from homeassistant.components.diagnostics import REDACTED
from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_RECONFIGURE
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.update_coordinator import UpdateFailed

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hubinet_ops.api import (
    BackendInformation,
    HubinetOpsApi,
    HubinetOpsCannotConnect,
    HubinetOpsInvalidAuth,
    HubinetOpsSnapshot,
    NodeSnapshot,
    PresenceState,
    ResourceIdentity,
    ResourceSnapshot,
    ResourceStateLevel,
    ResourceType,
)
from custom_components.hubinet_ops.const import (
    CONF_API_TOKEN,
    CONF_BASE_URL,
    CONF_VERIFY_TLS,
    DATA_API_FACTORY,
    DOMAIN,
)
from custom_components.hubinet_ops.coordinator import resource_device_info
from custom_components.hubinet_ops.diagnostics import (
    async_get_config_entry_diagnostics,
)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations, socket_enabled):
    """Load custom integrations; no test transport performs network I/O.

    ``socket_enabled`` is needed because the Windows asyncio event loop creates an
    internal local socketpair. All Hubinet Ops communication still uses FakeTransport.
    """

    yield

INSTANCE_ID = "6a172b5d-d820-4cac-904f-dfb17d42163e"
BASE_URL = "https://ops.example.test"
API_TOKEN = "phase-zero-test-token-not-a-secret"
ENTRY_DATA = {
    CONF_BASE_URL: BASE_URL,
    CONF_API_TOKEN: API_TOKEN,
    CONF_VERIFY_TLS: True,
}


def backend_information() -> BackendInformation:
    return BackendInformation(
        instance_id=INSTANCE_ID,
        name="Hubinet Ops Test",
        version="0.5.0.dev0",
        api_version="0.5-draft",
    )


def resource(
    resource_type: ResourceType,
    vmid: int,
    name: str,
    *,
    node_id: str = "pve-a",
    state: dict[str, Any] | None = None,
) -> ResourceSnapshot:
    return ResourceSnapshot(
        identity=ResourceIdentity(INSTANCE_ID, resource_type, vmid),
        name=name,
        node_id=node_id,
        status="running",
        state_level=ResourceStateLevel.OBSERVED,
        policy={"managed": False},
        capabilities=frozenset(),
        state=state or {},
    )


def snapshot(
    resources: Iterable[ResourceSnapshot],
    *,
    nodes: tuple[NodeSnapshot, ...] | None = None,
) -> HubinetOpsSnapshot:
    return HubinetOpsSnapshot(
        backend=backend_information(),
        nodes=(
            nodes
            if nodes is not None
            else (
                NodeSnapshot(
                    instance_id=INSTANCE_ID,
                    node_id="pve-a",
                    name="pve-a",
                    status="online",
                ),
            )
        ),
        resources=tuple(resources),
        generated_at="2026-08-08T12:00:00+00:00",
    )


INITIAL_RESOURCES = (
    resource(ResourceType.QEMU, 100, "Home Assistant"),
    resource(ResourceType.LXC, 101, "Cloudflared"),
    resource(ResourceType.LXC, 666, "Test Container"),
)


class FakeTransport:
    def __init__(
        self,
        snapshots: Iterable[HubinetOpsSnapshot] = (),
        *,
        validation_error: Exception | None = None,
    ) -> None:
        self._snapshots = list(snapshots)
        self._index = 0
        self.validation_error = validation_error
        self.validate_calls = 0
        self.info_calls = 0
        self.snapshot_calls = 0

    async def validate_connection(self) -> BackendInformation:
        self.validate_calls += 1
        if self.validation_error is not None:
            raise self.validation_error
        return backend_information()

    async def fetch_backend_information(self) -> BackendInformation:
        self.info_calls += 1
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
        unique_id=INSTANCE_ID,
        version=1,
        minor_version=1,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


@pytest.mark.asyncio
async def test_config_flow_happy_path(hass: HomeAssistant) -> None:
    transport = FakeTransport([snapshot(())])
    factory = install_factory(hass, transport)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "user"},
        data={**ENTRY_DATA, CONF_BASE_URL: f"{BASE_URL}/"},
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Hubinet Ops Test"
    assert result["data"] == ENTRY_DATA
    assert transport.validate_calls == 1
    assert factory.calls
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
async def test_reauth(hass: HomeAssistant) -> None:
    install_factory(hass, FakeTransport())
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=ENTRY_DATA,
        unique_id=INSTANCE_ID,
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
    assert entry.data[CONF_API_TOKEN] == "replacement-token"


@pytest.mark.asyncio
async def test_reconfigure(hass: HomeAssistant) -> None:
    install_factory(hass, FakeTransport())
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=ENTRY_DATA,
        unique_id=INSTANCE_ID,
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
    assert entry.data == {**replacement, CONF_BASE_URL: replacement[CONF_BASE_URL][:-1]}


@pytest.mark.asyncio
async def test_coordinator_fetches_one_logical_snapshot(hass: HomeAssistant) -> None:
    transport = FakeTransport([snapshot(INITIAL_RESOURCES)])
    entry = await setup_entry(hass, transport)
    coordinator = entry.runtime_data
    assert transport.snapshot_calls == 1
    assert len(coordinator.data.nodes) == 1
    assert len(coordinator.data.resources) == 3


@pytest.mark.asyncio
async def test_dynamic_node_creation(hass: HomeAssistant) -> None:
    entry = await setup_entry(hass, FakeTransport([snapshot(INITIAL_RESOURCES)]))
    registry = dr.async_get(hass)
    devices = dr.async_entries_for_config_entry(registry, entry.entry_id)
    by_identifier = {
        next(iter(device.identifiers))[1]: device for device in devices
    }
    node_key = f"{INSTANCE_ID}:node:pve-a"
    assert by_identifier[node_key].name == "Node pve-a"


@pytest.mark.asyncio
async def test_dynamic_qemu_creation(hass: HomeAssistant) -> None:
    entry = await setup_entry(hass, FakeTransport([snapshot(INITIAL_RESOURCES)]))
    registry = dr.async_get(hass)
    device = registry.async_get_device(
        {(DOMAIN, f"{INSTANCE_ID}:resource:qemu:100")}
    )
    assert device is not None
    assert device.name == "VM100 Home Assistant"


@pytest.mark.asyncio
async def test_dynamic_lxc_creation(hass: HomeAssistant) -> None:
    entry = await setup_entry(hass, FakeTransport([snapshot(INITIAL_RESOURCES)]))
    registry = dr.async_get(hass)
    device = registry.async_get_device(
        {(DOMAIN, f"{INSTANCE_ID}:resource:lxc:666")}
    )
    assert device is not None
    assert device.name == "CT666 Test Container"


@pytest.mark.asyncio
async def test_vm_lxc_devices_use_node_via_device(hass: HomeAssistant) -> None:
    entry = await setup_entry(hass, FakeTransport([snapshot(INITIAL_RESOURCES)]))
    registry = dr.async_get(hass)
    parent = registry.async_get_device({(DOMAIN, f"{INSTANCE_ID}:node:pve-a")})
    vm = registry.async_get_device({(DOMAIN, f"{INSTANCE_ID}:resource:qemu:100")})
    container = registry.async_get_device(
        {(DOMAIN, f"{INSTANCE_ID}:resource:lxc:101")}
    )
    assert parent is not None and vm is not None and container is not None
    assert vm.via_device_id == parent.id
    assert container.via_device_id == parent.id


@pytest.mark.asyncio
async def test_device_info_uses_2026_8_1_via_device_id_contract(
    hass: HomeAssistant,
) -> None:
    entry = await setup_entry(hass, FakeTransport([snapshot(INITIAL_RESOURCES)]))
    registry = dr.async_get(hass)
    parent = registry.async_get_device({(DOMAIN, f"{INSTANCE_ID}:node:pve-a")})
    assert parent is not None

    device_info = resource_device_info(
        hass,
        entry.entry_id,
        INITIAL_RESOURCES[0],
    )
    assert "via_device" not in device_info
    assert device_info["via_device_id"] == parent.id


@pytest.mark.asyncio
async def test_resource_is_added_after_refresh_without_reload(
    hass: HomeAssistant,
) -> None:
    added = resource(ResourceType.LXC, 777, "New Container")
    transport = FakeTransport(
        [snapshot(INITIAL_RESOURCES), snapshot((*INITIAL_RESOURCES, added))]
    )
    entry = await setup_entry(hass, transport)
    coordinator = entry.runtime_data
    await coordinator.async_request_refresh()
    await hass.async_block_till_done()

    registry = dr.async_get(hass)
    identifiers = {
        identifier
        for device in dr.async_entries_for_config_entry(registry, entry.entry_id)
        for identifier in device.identifiers
    }
    assert (DOMAIN, added.identity.registry_key) in identifiers
    entity_entries = er.async_entries_for_config_entry(
        er.async_get(hass), entry.entry_id
    )
    assert {
        entity.unique_id for entity in entity_entries if ":lxc:777:" in entity.unique_id
    } == {
        f"{added.identity.registry_key}:status",
        f"{added.identity.registry_key}:type",
        f"{added.identity.registry_key}:node",
    }


@pytest.mark.asyncio
async def test_rename_preserves_unique_ids(hass: HomeAssistant) -> None:
    renamed = resource(ResourceType.LXC, 101, "Cloudflared Renamed")
    transport = FakeTransport(
        [
            snapshot(INITIAL_RESOURCES),
            snapshot((INITIAL_RESOURCES[0], renamed, INITIAL_RESOURCES[2])),
        ]
    )
    entry = await setup_entry(hass, transport)
    entity_registry = er.async_get(hass)
    before = {
        item.unique_id
        for item in er.async_entries_for_config_entry(entity_registry, entry.entry_id)
        if ":lxc:101:" in item.unique_id
    }
    await entry.runtime_data.async_request_refresh()
    await hass.async_block_till_done()
    after = {
        item.unique_id
        for item in er.async_entries_for_config_entry(entity_registry, entry.entry_id)
        if ":lxc:101:" in item.unique_id
    }
    assert after == before
    device = dr.async_get(hass).async_get_device(
        {(DOMAIN, renamed.identity.registry_key)}
    )
    assert device is not None
    assert device.name == "CT101 Cloudflared Renamed"


@pytest.mark.asyncio
async def test_node_migration_preserves_identity_and_updates_via_device(
    hass: HomeAssistant,
) -> None:
    moved = resource(ResourceType.LXC, 101, "Cloudflared", node_id="pve-b")
    second_nodes = (
        NodeSnapshot(INSTANCE_ID, "pve-a", "pve-a", "online"),
        NodeSnapshot(INSTANCE_ID, "pve-b", "pve-b", "online"),
    )
    transport = FakeTransport(
        [
            snapshot(INITIAL_RESOURCES),
            snapshot((INITIAL_RESOURCES[0], moved, INITIAL_RESOURCES[2]), nodes=second_nodes),
        ]
    )
    entry = await setup_entry(hass, transport)
    await entry.runtime_data.async_request_refresh()
    await hass.async_block_till_done()
    registry = dr.async_get(hass)
    child = registry.async_get_device({(DOMAIN, moved.identity.registry_key)})
    parent = registry.async_get_device({(DOMAIN, f"{INSTANCE_ID}:node:pve-b")})
    assert child is not None and parent is not None
    assert child.via_device_id == parent.id


@pytest.mark.asyncio
async def test_one_missing_refresh_retains_unavailable_resource(
    hass: HomeAssistant,
) -> None:
    transport = FakeTransport(
        [snapshot(INITIAL_RESOURCES), snapshot((INITIAL_RESOURCES[0],))]
    )
    entry = await setup_entry(hass, transport)
    identity = INITIAL_RESOURCES[1].identity
    await entry.runtime_data.async_request_refresh()
    await hass.async_block_till_done()
    retained = entry.runtime_data.data.resources_by_identity[identity]
    assert retained.presence is PresenceState.MISSING
    assert retained.available is False
    assert retained.node_id is None
    assert retained.last_known_node_id == "pve-a"
    assert retained.relation_node_id == "pve-a"
    assert dr.async_get(hass).async_get_device(
        {(DOMAIN, identity.registry_key)}
    ) is not None


def test_present_resource_requires_node_in_same_snapshot() -> None:
    with pytest.raises(
        ValueError,
        match="present resource references a node absent from the same snapshot",
    ):
        snapshot((resource(ResourceType.LXC, 777, "Wrong Node", node_id="pve-b"),))


@pytest.mark.parametrize(
    "presence",
    [
        PresenceState.TEMPORARILY_UNAVAILABLE,
        PresenceState.NODE_UNAVAILABLE,
        PresenceState.MISSING,
        PresenceState.CONFIRMED_REMOVED,
    ],
)
def test_non_present_resource_uses_explicit_last_known_node(
    presence: PresenceState,
) -> None:
    unavailable = ResourceSnapshot(
        identity=ResourceIdentity(INSTANCE_ID, ResourceType.LXC, 777),
        name="Unavailable Container",
        node_id=None,
        last_known_node_id="pve-a",
        status="unknown",
        presence=presence,
    )
    result = snapshot((unavailable,), nodes=())
    stored = result.resources[0]
    assert stored.node_id is None
    assert stored.last_known_node_id == "pve-a"
    assert stored.relation_node_id == "pve-a"
    assert stored.available is False


def test_non_present_resource_rejects_current_node_id() -> None:
    with pytest.raises(ValueError, match="must use last_known_node_id"):
        ResourceSnapshot(
            identity=ResourceIdentity(INSTANCE_ID, ResourceType.LXC, 777),
            name="Missing Container",
            node_id="pve-a",
            last_known_node_id="pve-a",
            status="unknown",
            presence=PresenceState.MISSING,
        )


@pytest.mark.asyncio
async def test_last_known_node_absent_on_initial_snapshot_is_not_invented(
    hass: HomeAssistant,
) -> None:
    missing = ResourceSnapshot(
        identity=ResourceIdentity(INSTANCE_ID, ResourceType.LXC, 777),
        name="Missing Container",
        node_id=None,
        last_known_node_id="retired-node",
        status="unknown",
        presence=PresenceState.MISSING,
    )
    await setup_entry(hass, FakeTransport([snapshot((missing,), nodes=())]))
    device = dr.async_get(hass).async_get_device(
        {(DOMAIN, missing.identity.registry_key)}
    )
    assert device is not None
    assert device.via_device_id is None


def test_confirmed_removed_is_not_downgraded_when_later_absent() -> None:
    removed = ResourceSnapshot(
        identity=ResourceIdentity(INSTANCE_ID, ResourceType.LXC, 777),
        name="Removed Container",
        node_id=None,
        last_known_node_id="pve-a",
        status="removed",
        presence=PresenceState.CONFIRMED_REMOVED,
    )
    previous = snapshot((removed,))
    current = snapshot(())

    reconciled = current.preserving_unconfirmed_missing(previous)
    retained = reconciled.resources_by_identity[removed.identity]
    assert retained.presence is PresenceState.CONFIRMED_REMOVED
    assert retained.last_known_node_id == "pve-a"


def test_snapshot_mappings_are_deeply_immutable() -> None:
    mutable_state = {
        "nested": {"values": [1, {"flag": True}]},
    }
    frozen = resource(
        ResourceType.LXC,
        777,
        "Immutable Container",
        state=mutable_state,
    )
    mutable_state["nested"]["values"].append(2)

    nested = frozen.state["nested"]
    assert nested["values"] == (1, {"flag": True})
    with pytest.raises(TypeError):
        nested["new"] = "mutation"  # type: ignore[index]
    with pytest.raises(TypeError, match="JSON-like"):
        resource(
            ResourceType.LXC,
            778,
            "Mutable Payload",
            state={"payload": bytearray(b"mutable")},
        )


@pytest.mark.asyncio
async def test_diagnostics_redact_credentials(hass: HomeAssistant) -> None:
    sensitive_resource = resource(
        ResourceType.LXC,
        666,
        "Test Container",
        state={
            "authorization": "Bearer deeply-secret",
            "headers": {"Authorization": "Bearer nested-secret"},
            "events": [{"authorization": "Bearer sequence-secret"}],
        },
    )
    entry = await setup_entry(hass, FakeTransport([snapshot((sensitive_resource,))]))
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    config_data = diagnostics["config_entry"]["data"]
    assert config_data[CONF_API_TOKEN] == REDACTED
    assert config_data[CONF_BASE_URL] == REDACTED
    state = diagnostics["snapshot"]["resources"][0]["state"]
    assert state["authorization"] == REDACTED
    assert state["headers"] == REDACTED
    assert state["events"][0]["authorization"] == REDACTED
    assert API_TOKEN not in repr(diagnostics)
    assert "deeply-secret" not in repr(diagnostics)
    assert "nested-secret" not in repr(diagnostics)
    assert "sequence-secret" not in repr(diagnostics)


def integration_python_sources() -> list[Path]:
    return sorted(Path("custom_components/hubinet_ops").glob("*.py"))


def test_client_has_no_direct_proxmox_dependency_or_mutation() -> None:
    combined = "\n".join(
        path.read_text(encoding="utf-8") for path in integration_python_sources()
    ).lower()
    assert "proxmoxer" not in combined
    assert "proxmoxapi" not in combined
    assert "press_action" not in combined
    assert "via_device=" not in combined
    assert ".status.start" not in combined
    assert ".snapshot.post" not in combined
    assert "app.mqtt" not in combined
    assert "app.ha_entities" not in combined
    assert "home-assistant/packages" not in combined


def test_production_integration_has_no_hardcoded_current_vmids() -> None:
    forbidden = set(range(100, 111))
    occurrences: list[tuple[Path, int, int]] = []
    for path in integration_python_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        occurrences.extend(
            (path, node.lineno, node.value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and type(node.value) is int
            and node.value in forbidden
        )
    assert occurrences == []


def test_resource_identity_rejects_non_integer_vmid() -> None:
    with pytest.raises(ValueError, match="positive integer"):
        ResourceIdentity(INSTANCE_ID, ResourceType.LXC, 1.5)  # type: ignore[arg-type]
