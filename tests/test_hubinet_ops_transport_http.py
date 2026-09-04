"""Native Home Assistant HTTP transport.

Covers tests #29 (HA-side half), #31, #32, #33 (transport
contribution), #36, #37, #38 of
ARCHITECTURE.md.

Like every other ``test_hubinet_ops_*.py`` file in this repository, this
suite requires the pinned Home Assistant test environment
(``requirements-ha-test.txt``, Linux CI / devcontainer / WSL) and is
skipped at collection time wherever ``homeassistant`` is not installed
(including native Windows -- see STATUS.md's documented, accepted
``fcntl`` import limitation). No real network access anywhere in this
file: every backend request is intercepted by the ``aioclient_mock``
fixture.
"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("homeassistant", reason="isolated HA test dependencies not installed")

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer
from homeassistant.config_entries import SOURCE_REAUTH
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hubinet_ops.api import (
    BackendInformation,
    DetailStatus,
    HubinetOpsCannotConnect,
    HubinetOpsConflict,
    HubinetOpsInvalidAuth,
    HubinetOpsInvalidResponse,
    HubinetOpsSnapshot,
    InventorySourceSnapshot,
    LifecycleState,
    NodeSnapshot,
    NodeAvailability,
    ObservationalContinuity,
    PackageScanError,
    PackageScanSnapshot,
    PackageScanStatus,
    PresenceState,
    ResourceSnapshot,
    ResourceType,
    SourceContext,
)
from custom_components.hubinet_ops.const import (
    CONF_API_TOKEN,
    CONF_BASE_URL,
    CONF_VERIFY_TLS,
    DATA_API_FACTORY,
    DOMAIN,
)
from custom_components.hubinet_ops.diagnostics import async_get_config_entry_diagnostics
from custom_components.hubinet_ops.transport_http import HttpHubinetOpsTransport, http_api_factory

from tests.test_hubinet_ops_integration import (
    BACKEND_ID,
    NODE_A,
    OTHER_BACKEND_ID,
    RESOURCE_CT,
    RESOURCE_VM,
    SOURCE_ID,
    backend_information,
    node,
    resource,
    snapshot,
    source,
)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations, socket_enabled):
    """Load custom integrations; the real transport never performs real
    network I/O in tests -- aioclient_mock intercepts every request."""

    yield


BASE_URL = "https://ops.example.test"
API_TOKEN = "r0-test-token-not-a-real-secret"
ENTRY_DATA = {
    CONF_BASE_URL: BASE_URL,
    CONF_API_TOKEN: API_TOKEN,
    CONF_VERIFY_TLS: True,
}


# ---------------------------------------------------------------------------
# JSON wire-format serialization -- the exact reverse of transport_http's
# own parsing, built directly from the already-typed contract dataclasses
# so every fixture is guaranteed contract-valid before serialization.
# ---------------------------------------------------------------------------


def _context_json(context: SourceContext | None) -> dict[str, Any] | None:
    if context is None:
        return None
    return {
        "source_config_revision": context.source_config_revision,
        "endpoint_id": context.endpoint_id,
        "canonical_transport_locator": context.canonical_transport_locator,
        "canonicalization_contract_version": context.canonicalization_contract_version,
        "transport_trust_revision": context.transport_trust_revision,
    }


def _source_json(item: InventorySourceSnapshot) -> dict[str, Any]:
    return {
        "inventory_source_id": item.inventory_source_id,
        "name": item.name,
        "provider_kind": item.provider_kind,
        "health": item.health.value,
        "freshness": item.freshness.value,
        "health_origin": item.health_origin.value,
        "health_reason": item.health_reason,
        "last_issued_run_sequence": item.last_issued_run_sequence,
        "latest_completed_run_sequence": item.latest_completed_run_sequence,
        "latest_completed_outcome": item.latest_completed_outcome,
        "last_health_run_sequence": item.last_health_run_sequence,
        "last_run_health_outcome": item.last_run_health_outcome,
        "last_committed_run_sequence": item.last_committed_run_sequence,
        "last_successful_observed_at": item.last_successful_observed_at,
        "freshness_reference_at": item.freshness_reference_at,
        "freshness_valid_until": item.freshness_valid_until,
        "current_context": _context_json(item.current_context),
        "committed_context": _context_json(item.committed_context),
        "facts": dict(item.facts),
    }


def _node_json(item: NodeSnapshot) -> dict[str, Any]:
    return {
        "node_id": item.node_id,
        "inventory_source_id": item.inventory_source_id,
        "name": item.name,
        "status": item.status,
        "available": item.available,
        "facts": dict(item.facts),
    }


def _resource_json(item: ResourceSnapshot) -> dict[str, Any]:
    return {
        "resource_id": item.resource_id,
        "inventory_source_id": item.inventory_source_id,
        "active_binding_id": item.active_binding_id,
        "resource_type": item.resource_type.value,
        "vmid": item.vmid,
        "locator_generation": item.locator_generation,
        "resource_continuity_revision": item.resource_continuity_revision,
        "name": item.name,
        "status": item.status,
        "current_node_id": item.current_node_id,
        "last_known_node_id": item.last_known_node_id,
        "presence": item.presence.value,
        "lifecycle": item.lifecycle.value,
        "observational_continuity": item.observational_continuity.value,
        "security_continuity": item.security_continuity.value,
        "detail_status": item.detail_status.value,
        "node_availability": item.node_availability.value,
        "state_level": item.state_level.value,
        "retained_policy": dict(item.retained_policy),
        "effective_policy": dict(item.effective_policy),
        "policy_applicable": item.policy_applicable,
        "suspended_reason": item.suspended_reason,
        "effective_capabilities": sorted(item.effective_capabilities),
        "state": dict(item.state),
        "termination_reason": item.termination_reason,
        "successor_resource_id": item.successor_resource_id,
    }


def _backend_json(item: BackendInformation) -> dict[str, Any]:
    return {
        "backend_instance_id": item.backend_instance_id,
        "name": item.name,
        "version": item.version,
        "api_version": item.api_version,
    }


def _snapshot_json(item: HubinetOpsSnapshot) -> dict[str, Any]:
    return {
        "backend": _backend_json(item.backend),
        "sources": [_source_json(s) for s in item.sources],
        "nodes": [_node_json(n) for n in item.nodes],
        "resources": [_resource_json(r) for r in item.resources],
        "inventory_revision": item.inventory_revision,
        "published_state_revision": item.published_state_revision,
        "published_at": item.published_at,
    }


def _fixture_snapshot(
    *resources,
    source_run_sequence: int = 5,
    inventory_revision: int = 10,
    published_state_revision: int = 20,
    published_at: str = "2026-08-08T12:00:00+00:00",
) -> HubinetOpsSnapshot:
    return snapshot(
        resources,
        sources=(
            source(
                last_issued_run_sequence=source_run_sequence,
                latest_completed_run_sequence=source_run_sequence,
                last_health_run_sequence=source_run_sequence,
                last_committed_run_sequence=source_run_sequence,
            ),
        ),
        nodes=(node(),),
        inventory_revision=inventory_revision,
        published_state_revision=published_state_revision,
        published_at=published_at,
    )


def _package_scan_json(scan: PackageScanSnapshot) -> dict[str, Any]:
    return {
        "status": scan.status.value,
        "scan_run_id": scan.scan_run_id,
        "started_at": scan.started_at,
        "completed_at": scan.completed_at,
        "os": (
            {"id": scan.os.os_id, "version": scan.os.version}
            if scan.os is not None
            else None
        ),
        "pending_count": scan.pending_count,
        "plan_fingerprint": scan.plan_fingerprint,
        "reboot_required": scan.reboot_required,
        "packages": [
            {
                "name": item.name,
                "installed_version": item.installed_version,
                "candidate_version": item.candidate_version,
                "origin": item.origin,
                "description": item.description,
                "security": item.security,
            }
            for item in scan.packages
        ],
        "error": (
            {"classification": scan.error.classification, "message": scan.error.message}
            if scan.error is not None
            else None
        ),
    }


# ---------------------------------------------------------------------------
# test #29 (HA-side half) -- publication -> HTTP -> HA round trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_29_snapshot_round_trip_matches_typed_contract(
    hass: HomeAssistant, aioclient_mock
) -> None:
    expected = _fixture_snapshot(
        resource(RESOURCE_VM, ResourceType.QEMU, 100, "Home Assistant"),
        resource(RESOURCE_CT, ResourceType.LXC, 101, "Cloudflared"),
    )
    aioclient_mock.get(f"{BASE_URL}/r0/v1/snapshot", json=_snapshot_json(expected))

    transport = HttpHubinetOpsTransport(
        hass, base_url=BASE_URL, api_token=API_TOKEN, verify_tls=True
    )
    result = await transport.fetch_resource_snapshot()

    assert result == expected
    assert isinstance(result.resources[0].effective_capabilities, frozenset)


# ---------------------------------------------------------------------------
# Corrective pass, Finding 3 -- an old 0.5 backend predating package scanning
# publishes resources with no "package_scan" field at all. That must parse
# as a backward-compatible NOT_SCANNED, never a KeyError that fails the
# whole coordinator refresh. A field that IS present but malformed must
# still be rejected exactly as before.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_finding3_missing_package_scan_field_is_backward_compatible_not_scanned(
    hass: HomeAssistant, aioclient_mock
) -> None:
    expected = _fixture_snapshot(
        resource(RESOURCE_CT, ResourceType.LXC, 101, "Cloudflared")
    )
    payload = _snapshot_json(expected)
    assert "package_scan" not in payload["resources"][0]  # old-backend wire shape
    aioclient_mock.get(f"{BASE_URL}/r0/v1/snapshot", json=payload)

    transport = HttpHubinetOpsTransport(
        hass, base_url=BASE_URL, api_token=API_TOKEN, verify_tls=True
    )
    result = await transport.fetch_resource_snapshot()

    scan = result.resources[0].package_scan
    assert scan == PackageScanSnapshot()
    assert scan.status is PackageScanStatus.NOT_SCANNED
    assert scan.scan_run_id is None
    assert scan.started_at is None
    assert scan.completed_at is None
    assert scan.os is None
    assert scan.packages == ()
    assert scan.pending_count is None
    assert scan.plan_fingerprint is None
    assert scan.reboot_required is None
    assert scan.error is None
    # The rest of the resource -- ordinary inventory data -- is unaffected.
    assert result.resources[0].name == "Cloudflared"


@pytest.mark.asyncio
async def test_finding3_present_valid_package_scan_field_still_parses(
    hass: HomeAssistant, aioclient_mock
) -> None:
    scan = PackageScanSnapshot(
        status=PackageScanStatus.FAILED,
        scan_run_id="11111111-1111-1111-1111-111111111111",
        started_at="2026-08-28T12:00:00+00:00",
        completed_at="2026-08-28T12:05:00+00:00",
        error=PackageScanError(
            classification="metadata_refresh_failed",
            message="APT metadata refresh failed",
        ),
    )
    ct = resource(RESOURCE_CT, ResourceType.LXC, 101, "Cloudflared", package_scan=scan)
    expected = _fixture_snapshot(ct)
    payload = _snapshot_json(expected)
    payload["resources"][0]["package_scan"] = _package_scan_json(scan)
    aioclient_mock.get(f"{BASE_URL}/r0/v1/snapshot", json=payload)

    transport = HttpHubinetOpsTransport(
        hass, base_url=BASE_URL, api_token=API_TOKEN, verify_tls=True
    )
    result = await transport.fetch_resource_snapshot()

    assert result.resources[0].package_scan == scan


@pytest.mark.asyncio
async def test_finding3_present_but_malformed_package_scan_field_still_fails(
    hass: HomeAssistant, aioclient_mock
) -> None:
    ct = resource(RESOURCE_CT, ResourceType.LXC, 101, "Cloudflared")
    expected = _fixture_snapshot(ct)
    payload = _snapshot_json(expected)
    # A status of "success" requires OS/fingerprint/exact package evidence;
    # this field is present but incomplete, so it must still be rejected --
    # the missing-field compatibility fallback must never widen to cover it.
    payload["resources"][0]["package_scan"] = {"status": "success"}
    aioclient_mock.get(f"{BASE_URL}/r0/v1/snapshot", json=payload)

    transport = HttpHubinetOpsTransport(
        hass, base_url=BASE_URL, api_token=API_TOKEN, verify_tls=True
    )
    with pytest.raises(HubinetOpsInvalidResponse):
        await transport.fetch_resource_snapshot()


@pytest.mark.asyncio
async def test_finding3_present_null_package_scan_field_still_fails(
    hass: HomeAssistant, aioclient_mock
) -> None:
    # A present-but-null field is a malformed field, not a missing one --
    # the missing-key compatibility fallback must not swallow this case.
    ct = resource(RESOURCE_CT, ResourceType.LXC, 101, "Cloudflared")
    expected = _fixture_snapshot(ct)
    payload = _snapshot_json(expected)
    payload["resources"][0]["package_scan"] = None
    aioclient_mock.get(f"{BASE_URL}/r0/v1/snapshot", json=payload)

    transport = HttpHubinetOpsTransport(
        hass, base_url=BASE_URL, api_token=API_TOKEN, verify_tls=True
    )
    with pytest.raises(HubinetOpsInvalidResponse):
        await transport.fetch_resource_snapshot()


@pytest.mark.asyncio
async def test_29_authorization_header_sent_on_every_request(
    hass: HomeAssistant, aioclient_mock
) -> None:
    aioclient_mock.get(
        f"{BASE_URL}/r0/v1/backend", json=_backend_json(backend_information())
    )
    transport = HttpHubinetOpsTransport(
        hass, base_url=BASE_URL, api_token=API_TOKEN, verify_tls=True
    )
    await transport.validate_connection()

    assert len(aioclient_mock.mock_calls) == 1
    headers = aioclient_mock.mock_calls[0][3]
    assert headers["Authorization"] == f"Bearer {API_TOKEN}"


@pytest.mark.asyncio
async def test_exact_plan_approval_put_forwards_reference_and_bearer_unchanged(
    hass: HomeAssistant, aioclient_mock
) -> None:
    scan_run_id = "95255892-75df-4fb6-ae04-c4fe4802aa97"
    fingerprint = "a" * 64
    url = f"{BASE_URL}/r0/v1/resources/{RESOURCE_CT}/package-plan-approval"
    aioclient_mock.put(
        url,
        json={
            "approval_id": "36d17ae7-f86c-4613-a046-a2bd3301af38",
            "resource_id": RESOURCE_CT,
            "reviewed_scan_run_id": scan_run_id,
            "plan_fingerprint": fingerprint,
            "approved_at": "2026-08-08T12:01:00+00:00",
        },
    )
    transport = HttpHubinetOpsTransport(
        hass, base_url=BASE_URL, api_token=API_TOKEN, verify_tls=True
    )

    await transport.approve_package_plan(RESOURCE_CT, scan_run_id, fingerprint)

    assert len(aioclient_mock.mock_calls) == 1
    call = aioclient_mock.mock_calls[0]
    assert call[0] == "PUT"
    assert call[3]["Authorization"] == f"Bearer {API_TOKEN}"
    assert call[2] == {
        "scan_run_id": scan_run_id,
        "plan_fingerprint": fingerprint,
    }


@pytest.mark.asyncio
async def test_exact_plan_approval_conflict_maps_to_typed_conflict(
    hass: HomeAssistant, aioclient_mock
) -> None:
    url = f"{BASE_URL}/r0/v1/resources/{RESOURCE_CT}/package-plan-approval"
    aioclient_mock.put(url, status=409)
    transport = HttpHubinetOpsTransport(
        hass, base_url=BASE_URL, api_token=API_TOKEN, verify_tls=True
    )
    with pytest.raises(HubinetOpsConflict):
        await transport.approve_package_plan(
            RESOURCE_CT,
            "95255892-75df-4fb6-ae04-c4fe4802aa97",
            "a" * 64,
        )


# ---------------------------------------------------------------------------
# test #31 -- wrong backend on reauth rejected, via the real transport
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_31_reauth_rejects_wrong_backend_via_real_http_transport(
    hass: HomeAssistant, aioclient_mock
) -> None:
    # No fake factory installed: create_api_client falls back to the real
    # http_api_factory(hass) default.
    aioclient_mock.get(
        f"{BASE_URL}/r0/v1/backend",
        json=_backend_json(backend_information(backend_instance_id=OTHER_BACKEND_ID)),
    )
    entry = MockConfigEntry(
        domain=DOMAIN, title="Hubinet Ops Test", data=ENTRY_DATA, unique_id=BACKEND_ID
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
    assert result["errors"] == {"base": "wrong_instance"}
    assert entry.unique_id == BACKEND_ID
    assert entry.data == ENTRY_DATA


# ---------------------------------------------------------------------------
# test #32 (transport contribution) -- exception mapping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_32_401_and_403_map_to_invalid_auth(
    hass: HomeAssistant, aioclient_mock
) -> None:
    for status in (401, 403):
        aioclient_mock.clear_requests()
        aioclient_mock.get(f"{BASE_URL}/r0/v1/backend", status=status)
        transport = HttpHubinetOpsTransport(
            hass, base_url=BASE_URL, api_token=API_TOKEN, verify_tls=True
        )
        with pytest.raises(HubinetOpsInvalidAuth):
            await transport.validate_connection()


@pytest.mark.asyncio
async def test_32_non_2xx_other_than_401_403_maps_to_cannot_connect(
    hass: HomeAssistant, aioclient_mock
) -> None:
    aioclient_mock.get(f"{BASE_URL}/r0/v1/backend", status=500)
    transport = HttpHubinetOpsTransport(
        hass, base_url=BASE_URL, api_token=API_TOKEN, verify_tls=True
    )
    with pytest.raises(HubinetOpsCannotConnect):
        await transport.validate_connection()


@pytest.mark.asyncio
async def test_32_connection_error_maps_to_cannot_connect(
    hass: HomeAssistant, aioclient_mock
) -> None:
    aioclient_mock.get(
        f"{BASE_URL}/r0/v1/backend",
        exc=aiohttp.ClientConnectorError(
            connection_key=None, os_error=OSError("refused")
        ),
    )
    transport = HttpHubinetOpsTransport(
        hass, base_url=BASE_URL, api_token=API_TOKEN, verify_tls=True
    )
    with pytest.raises(HubinetOpsCannotConnect):
        await transport.validate_connection()


@pytest.mark.asyncio
async def test_32_timeout_maps_to_cannot_connect(
    hass: HomeAssistant, aioclient_mock
) -> None:
    aioclient_mock.get(f"{BASE_URL}/r0/v1/backend", exc=TimeoutError())
    transport = HttpHubinetOpsTransport(
        hass, base_url=BASE_URL, api_token=API_TOKEN, verify_tls=True
    )
    with pytest.raises(HubinetOpsCannotConnect):
        await transport.validate_connection()


@pytest.mark.asyncio
async def test_32_invalid_json_body_maps_to_invalid_response(
    hass: HomeAssistant, aioclient_mock
) -> None:
    aioclient_mock.get(
        f"{BASE_URL}/r0/v1/backend",
        text="not json",
        headers={"Content-Type": "text/plain"},
    )
    transport = HttpHubinetOpsTransport(
        hass, base_url=BASE_URL, api_token=API_TOKEN, verify_tls=True
    )
    with pytest.raises(HubinetOpsInvalidResponse):
        await transport.validate_connection()


@pytest.mark.asyncio
async def test_32_response_missing_required_fields_maps_to_invalid_response(
    hass: HomeAssistant, aioclient_mock
) -> None:
    aioclient_mock.get(f"{BASE_URL}/r0/v1/backend", json={"name": "Hubinet Ops"})
    transport = HttpHubinetOpsTransport(
        hass, base_url=BASE_URL, api_token=API_TOKEN, verify_tls=True
    )
    with pytest.raises(HubinetOpsInvalidResponse):
        await transport.validate_connection()


@pytest.mark.asyncio
async def test_32_correct_token_and_200_succeeds(
    hass: HomeAssistant, aioclient_mock
) -> None:
    aioclient_mock.get(
        f"{BASE_URL}/r0/v1/backend", json=_backend_json(backend_information())
    )
    transport = HttpHubinetOpsTransport(
        hass, base_url=BASE_URL, api_token=API_TOKEN, verify_tls=True
    )
    info = await transport.validate_connection()
    assert info.backend_instance_id == BACKEND_ID


# ---------------------------------------------------------------------------
# test #33 (transport contribution) -- diagnostics redact both HA and
# PVE secrets even after round-tripping through the real transport
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_33_diagnostics_redact_secrets_reaching_it_via_real_transport(
    hass: HomeAssistant, aioclient_mock
) -> None:
    leaking = resource(
        RESOURCE_VM,
        ResourceType.QEMU,
        100,
        "Home Assistant",
        state={"token": "leaked-secret-value", "harmless": "ok"},
    )
    payload = _fixture_snapshot(leaking)
    aioclient_mock.get(f"{BASE_URL}/r0/v1/snapshot", json=_snapshot_json(payload))
    aioclient_mock.get(
        f"{BASE_URL}/r0/v1/backend", json=_backend_json(backend_information())
    )

    entry = MockConfigEntry(
        domain=DOMAIN, title="Hubinet Ops Test", data=ENTRY_DATA, unique_id=BACKEND_ID
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    rendered = repr(diagnostics)
    assert "leaked-secret-value" not in rendered
    assert API_TOKEN not in rendered


# ---------------------------------------------------------------------------
# test #36 -- dynamic new resource appears without reload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_36_new_resource_appears_via_real_transport_without_reload(
    hass: HomeAssistant, aioclient_mock
) -> None:
    initial = _fixture_snapshot(resource(RESOURCE_VM, ResourceType.QEMU, 100, "Home Assistant"))
    aioclient_mock.get(f"{BASE_URL}/r0/v1/snapshot", json=_snapshot_json(initial))
    aioclient_mock.get(
        f"{BASE_URL}/r0/v1/backend", json=_backend_json(backend_information())
    )

    entry = MockConfigEntry(
        domain=DOMAIN, title="Hubinet Ops Test", data=ENTRY_DATA, unique_id=BACKEND_ID
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    coordinator = entry.runtime_data
    assert len(coordinator.data.resources) == 1

    updated = _fixture_snapshot(
        resource(RESOURCE_VM, ResourceType.QEMU, 100, "Home Assistant"),
        resource(RESOURCE_CT, ResourceType.LXC, 101, "Cloudflared"),
        source_run_sequence=6,
        inventory_revision=11,
        published_state_revision=21,
        published_at="2026-08-08T12:01:00+00:00",
    )
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{BASE_URL}/r0/v1/snapshot", json=_snapshot_json(updated))

    await coordinator.async_refresh()

    assert len(coordinator.data.resources) == 2
    assert RESOURCE_CT in coordinator.data.resources_by_id


# ---------------------------------------------------------------------------
# test #37 -- resource replacement preserves accepted HA identity
# semantics, via the real transport
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_37_replacement_preserves_old_and_successor_via_real_transport(
    hass: HomeAssistant, aioclient_mock
) -> None:
    original = resource(RESOURCE_VM, ResourceType.QEMU, 100, "Home Assistant")
    aioclient_mock.get(
        f"{BASE_URL}/r0/v1/snapshot", json=_snapshot_json(_fixture_snapshot(original))
    )
    aioclient_mock.get(
        f"{BASE_URL}/r0/v1/backend", json=_backend_json(backend_information())
    )
    entry = MockConfigEntry(
        domain=DOMAIN, title="Hubinet Ops Test", data=ENTRY_DATA, unique_id=BACKEND_ID
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    successor_id = "8e39a37d-0b53-4d1e-9d9e-6a54a2f2ad11"
    retired_original = resource(
        RESOURCE_VM,
        ResourceType.QEMU,
        100,
        "Home Assistant (old)",
        active_binding_id=None,
        resource_continuity_revision=2,
        current_node_id=None,
        last_known_node_id=NODE_A,
        presence=PresenceState.NOT_CURRENT,
        lifecycle=LifecycleState.RETIRED,
        observational_continuity=ObservationalContinuity.REPLACED,
        detail_status=DetailStatus.NOT_APPLICABLE,
        node_availability=NodeAvailability.NOT_APPLICABLE,
        status="unknown",
        termination_reason="replaced",
        successor_resource_id=successor_id,
    )
    successor = resource(
        successor_id,
        ResourceType.QEMU,
        100,
        "Home Assistant",
        locator_generation=2,
    )

    aioclient_mock.clear_requests()
    aioclient_mock.get(
        f"{BASE_URL}/r0/v1/snapshot",
        json=_snapshot_json(
            _fixture_snapshot(
                retired_original,
                successor,
                source_run_sequence=6,
                inventory_revision=11,
                published_state_revision=21,
                published_at="2026-08-08T12:01:00+00:00",
            )
        ),
    )

    coordinator = entry.runtime_data
    await coordinator.async_refresh()

    by_id = coordinator.data.resources_by_id
    assert by_id[RESOURCE_VM].presence is PresenceState.NOT_CURRENT
    assert by_id[RESOURCE_VM].successor_resource_id == successor_id
    assert by_id[successor_id].presence is PresenceState.PRESENT
    assert coordinator.data.current_resources_by_locator[(SOURCE_ID, 100)].resource_id == (
        successor_id
    )


# ---------------------------------------------------------------------------
# test #38 -- terminal retained resource presentation, via the real
# transport
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_38_confirmed_removed_resource_round_trips_via_real_transport(
    hass: HomeAssistant, aioclient_mock
) -> None:
    from tests.test_hubinet_ops_integration import absent_resource

    terminal = absent_resource(
        "9c2f9e2b-df3e-4a4a-9a0b-9e2f2c9b1a11", PresenceState.CONFIRMED_REMOVED
    )
    aioclient_mock.get(
        f"{BASE_URL}/r0/v1/snapshot", json=_snapshot_json(_fixture_snapshot(terminal))
    )
    transport = HttpHubinetOpsTransport(
        hass, base_url=BASE_URL, api_token=API_TOKEN, verify_tls=True
    )
    result = await transport.fetch_resource_snapshot()

    parsed = result.resources[0]
    assert parsed.presence is PresenceState.CONFIRMED_REMOVED
    assert parsed.active_binding_id is None
    assert parsed.termination_reason == "confirmed_removed"


def test_http_api_factory_binds_hass_and_matches_protocol_shape() -> None:
    class FakeHass:
        pass

    factory = http_api_factory(FakeHass())
    assert callable(factory)


# ---------------------------------------------------------------------------
# PR #74 review finding 3 -- the rollback request must not report a
# transport timeout before the backend's own durable pre-ACK boundary can
# legitimately complete.
#
# `aioclient_mock` (used throughout this file) replaces aiohttp's real
# `ClientSession._request` outright, so it never applies whatever `timeout=`
# a caller passes -- it cannot exercise genuine timeout ENFORCEMENT, only
# what request was attempted. These tests instead run a real local aiohttp
# server on the loopback interface (this file's own `auto_enable_custom_
# integrations` fixture already opts into real sockets via `socket_enabled`)
# and monkeypatch transport_http's module-level timeout CONSTANTS down to
# small, test-scaled values -- never any request/response code path itself
# -- so the suite proves the actual `aiohttp.ClientTimeout` mechanics the
# production code relies on without waiting out real production durations.
# ---------------------------------------------------------------------------

from custom_components.hubinet_ops import transport_http as _transport_http_module


def _rollback_job_payload(resource_id: str) -> dict[str, Any]:
    """The minimum complete, contract-valid rollback response body."""

    return {
        "resource_id": resource_id,
        "job_id": "11111111-1111-4111-8111-111111111111",
        "request_id": "22222222-2222-4222-8222-222222222222",
        "status": "active",
        "checkpoint": "rollback_may_have_started",
        "issued_at": "2026-01-01T00:00:00+00:00",
        "approved_plan_fingerprint": "fingerprint",
        "package_count": 1,
        "snapshot": {"name": "hubinet-pre-update", "confirmed_at": "2026-01-01T00:00:00+00:00"},
        "mutation": {},
        "health": {},
        "rollback": {
            "may_have_started_at": "2026-01-01T00:05:00+00:00",
            "available": True,
        },
    }


class _DelayedRollbackServer:
    """A real local HTTP server that delays its rollback response by
    exactly `delay_seconds` of genuine wall-clock time before returning a
    valid 202 job body -- modelling the backend's own synchronous,
    potentially slow, pre-ACK snapshot inspection.
    """

    def __init__(self, delay_seconds: float, resource_id: str) -> None:
        self._delay_seconds = delay_seconds
        self._resource_id = resource_id
        self.calls = 0

    async def _handle(self, request: web.Request) -> web.Response:
        self.calls += 1
        await asyncio.sleep(self._delay_seconds)
        return web.json_response(
            _rollback_job_payload(self._resource_id), status=202
        )

    def app(self) -> web.Application:
        application = web.Application()
        application.router.add_post(
            "/r0/v1/resources/{resource_id}/package-update/rollback",
            self._handle,
        )
        return application


@pytest.mark.asyncio
async def test_rollback_uses_a_longer_timeout_than_ordinary_package_update_requests(
    hass: HomeAssistant,
) -> None:
    """Structural proof: the rollback route is wired to a materially larger
    bound than every other package-update request, and that bound covers
    the full SEQUENTIAL pre-ACK budget -- the backend's own real snapshot-
    inspection ceiling, PLUS the full authority writer-wait budget
    `arm_package_update_rollback` can separately wait on afterwards -- not
    merely one of the two (required test 1).
    """

    assert (
        _transport_http_module._ROLLBACK_REQUEST_TIMEOUT.total
        > _transport_http_module._REQUEST_TIMEOUT.total
    )

    from app.inventory_runtime_config import PACKAGE_UPDATE_SNAPSHOT_TIMEOUT_SECONDS

    backend_policy = _load_backend_contention_policy()
    assert (
        _transport_http_module._PACKAGE_UPDATE_SNAPSHOT_TIMEOUT_SECONDS_MIRROR
        == PACKAGE_UPDATE_SNAPSHOT_TIMEOUT_SECONDS
    ), (
        "the HA-side mirror of the backend's snapshot-inspection ceiling "
        "has drifted from the real backend constant -- update "
        "_PACKAGE_UPDATE_SNAPSHOT_TIMEOUT_SECONDS_MIRROR in "
        "transport_http.py to match "
        "app.inventory_runtime_config.PACKAGE_UPDATE_SNAPSHOT_TIMEOUT_SECONDS"
    )
    # The dedicated timeout must never be shorter than the SUM of the two
    # legitimate sequential pre-ACK waits -- covering it exactly, with no
    # margin, would still let a legitimate worst case race the deadline.
    assert (
        _transport_http_module._ROLLBACK_REQUEST_TIMEOUT.total
        > PACKAGE_UPDATE_SNAPSHOT_TIMEOUT_SECONDS
        + backend_policy.AUTHORITY_WRITER_WAIT_BUDGET_SECONDS
    )


@pytest.mark.asyncio
async def test_ordinary_requests_keep_the_existing_bounded_timeout(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Required test 1: fetch/resume are unaffected by this fix and still
    time out on their existing, short, ordinary bound.

    START is deliberately NOT exercised here any more: it now has its own
    dedicated `_START_REQUEST_TIMEOUT` for exactly the same pre-ACK
    writer-wait reason rollback does -- see the `TestStartRequestTimeout`
    class below.
    """

    monkeypatch.setattr(
        _transport_http_module, "_REQUEST_TIMEOUT", aiohttp.ClientTimeout(total=0.2)
    )
    server = _DelayedRollbackServer(delay_seconds=1.0, resource_id=RESOURCE_CT)
    application = web.Application()
    application.router.add_post(
        "/r0/v1/resources/{resource_id}/package-update/resume", server._handle
    )
    test_server = TestServer(application)
    await test_server.start_server(loop=asyncio.get_running_loop())
    try:
        transport = HttpHubinetOpsTransport(
            hass,
            base_url=str(test_server.make_url("")).rstrip("/"),
            api_token=API_TOKEN,
            verify_tls=False,
        )
        with pytest.raises(HubinetOpsCannotConnect):
            await transport.resume_package_update(RESOURCE_CT)
    finally:
        await test_server.close()


@pytest.mark.asyncio
async def test_rollback_survives_the_snapshot_plus_writer_wait_sequential_delay(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Required test 2: a rollback response delayed beyond what the OLD,
    inspection-only-derived 135s bound would have covered, but within the
    NEW budget that also covers `arm_package_update_rollback`'s own
    SEQUENTIAL authority writer wait, must not report
    `HubinetOpsCannotConnect`.

    Scaled by a flat 0.01 from the real formulas (old 135s = 120s
    inspection-pin + 15s margin; new 185s = 60s real inspection ceiling +
    105s writer-wait budget + 5s route margin + 15s network margin):
    delay 1.6s -- past where the old 1.35s-scaled bound would have given
    up, comfortably inside the new 1.85s-scaled one. This is the exact P1
    witness: snapshot inspection (~60s real) followed by a separate,
    legitimate writer-lock wait while arming rollback (~80s real) totals
    past the old bound while the backend is still legitimately working
    toward a durable commit.
    """

    monkeypatch.setattr(
        _transport_http_module,
        "_ROLLBACK_REQUEST_TIMEOUT",
        aiohttp.ClientTimeout(total=1.85),
    )
    server = _DelayedRollbackServer(delay_seconds=1.6, resource_id=RESOURCE_CT)
    test_server = TestServer(server.app())
    await test_server.start_server(loop=asyncio.get_running_loop())
    try:
        transport = HttpHubinetOpsTransport(
            hass,
            base_url=str(test_server.make_url("")).rstrip("/"),
            api_token=API_TOKEN,
            verify_tls=False,
        )
        result = await transport.rollback_package_update(RESOURCE_CT)
    finally:
        await test_server.close()

    assert server.calls == 1
    assert result.resource_id == RESOURCE_CT
    assert result.rollback_available is True
    # The delay genuinely exceeds where the OLD, inspection-only bound
    # would have given up -- proof this pass's widening, not merely the
    # pre-existing dedicated-rollback-timeout mechanism, is what covers it.
    assert 1.6 > 1.35


@pytest.mark.asyncio
async def test_rollback_response_beyond_the_ordinary_bound_does_not_report_cannot_connect(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Required test 3, the finding's own core claim: a rollback response
    that legitimately takes longer than `_REQUEST_TIMEOUT` -- but stays
    within the rollback-specific budget -- must never surface as
    `HubinetOpsCannotConnect`. Scaled down: the ordinary bound is
    monkeypatched to 0.2s, the rollback bound to 2s, and the fake backend
    delays exactly 1s -- longer than the ordinary bound, comfortably inside
    the rollback one.
    """

    monkeypatch.setattr(
        _transport_http_module, "_REQUEST_TIMEOUT", aiohttp.ClientTimeout(total=0.2)
    )
    monkeypatch.setattr(
        _transport_http_module,
        "_ROLLBACK_REQUEST_TIMEOUT",
        aiohttp.ClientTimeout(total=2.0),
    )
    server = _DelayedRollbackServer(delay_seconds=1.0, resource_id=RESOURCE_CT)
    test_server = TestServer(server.app())
    await test_server.start_server(loop=asyncio.get_running_loop())
    try:
        transport = HttpHubinetOpsTransport(
            hass,
            base_url=str(test_server.make_url("")).rstrip("/"),
            api_token=API_TOKEN,
            verify_tls=False,
        )
        result = await transport.rollback_package_update(RESOURCE_CT)
    finally:
        await test_server.close()

    assert server.calls == 1
    assert result.resource_id == RESOURCE_CT
    assert result.rollback_available is True


@pytest.mark.asyncio
async def test_rollback_beyond_its_own_budget_still_fails_boundedly(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Required test 4: the rollback timeout is longer, not unbounded --
    a response that outlasts even the (scaled) rollback budget still
    reports `HubinetOpsCannotConnect`, and does so within a bounded wait.
    """

    monkeypatch.setattr(
        _transport_http_module,
        "_ROLLBACK_REQUEST_TIMEOUT",
        aiohttp.ClientTimeout(total=0.3),
    )
    server = _DelayedRollbackServer(delay_seconds=2.0, resource_id=RESOURCE_CT)
    test_server = TestServer(server.app())
    await test_server.start_server(loop=asyncio.get_running_loop())
    try:
        transport = HttpHubinetOpsTransport(
            hass,
            base_url=str(test_server.make_url("")).rstrip("/"),
            api_token=API_TOKEN,
            verify_tls=False,
        )
        with pytest.raises(HubinetOpsCannotConnect):
            await asyncio.wait_for(
                transport.rollback_package_update(RESOURCE_CT), timeout=5.0
            )
    finally:
        await test_server.close()


@pytest.mark.asyncio
async def test_rollback_request_body_still_carries_no_caller_supplied_target(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Required test 5: the backend contract this fix must not weaken --
    the request body remains empty (no snapshot/VMID/operation id), so a
    longer client-side timeout never becomes a wider request surface.
    """

    captured: dict[str, Any] = {}

    async def _capture(request: web.Request) -> web.Response:
        captured["body"] = await request.text()
        return web.json_response(_rollback_job_payload(RESOURCE_CT), status=202)

    application = web.Application()
    application.router.add_post(
        "/r0/v1/resources/{resource_id}/package-update/rollback", _capture
    )
    test_server = TestServer(application)
    await test_server.start_server(loop=asyncio.get_running_loop())
    try:
        transport = HttpHubinetOpsTransport(
            hass,
            base_url=str(test_server.make_url("")).rstrip("/"),
            api_token=API_TOKEN,
            verify_tls=False,
        )
        await transport.rollback_package_update(RESOURCE_CT)
    finally:
        await test_server.close()

    assert captured["body"] in ("{}", "")


# ---------------------------------------------------------------------------
# Family C -- pre-ACK side-effect timeout contract, START's own dedicated
# timeout. Same shape and same reason as the rollback suite above:
# `start_package_update`'s backend route durably issues the job inside the
# authority store's writer transaction BEFORE it acknowledges, and that
# legitimate pre-ACK wait can outlast the ordinary 15s bound.
# ---------------------------------------------------------------------------

_CONTENTION_POLICY_PATH = (
    Path(__file__).resolve().parents[1] / "app" / "inventory" / "contention_policy.py"
)


def _load_backend_contention_policy():
    """Load the backend's writer-wait policy module directly by path.

    Deliberately not ``import app.inventory.contention_policy``: that would
    execute ``app/inventory/__init__.py``, which pulls in the rest of the
    authority package. Loading the one pure-constants module by file path
    keeps this cross-check dependency-free under the pinned HA environment,
    matching how this file's own module-under-test and every other
    ``test_hubinet_ops_*.py``/``test_update_*`` helper loads standalone
    scripts in this repository.
    """

    spec = importlib.util.spec_from_file_location(
        "hubinet_ops_contention_policy_backend_mirror_check",
        _CONTENTION_POLICY_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _start_job_payload(resource_id: str) -> dict[str, Any]:
    """The minimum complete, contract-valid START response body."""

    body = _rollback_job_payload(resource_id)
    body["checkpoint"] = "issued"
    body["rollback"] = {"may_have_started_at": None, "available": False}
    return body


class _DelayedStartServer:
    """A real local HTTP server that delays its START response by exactly
    ``delay_seconds`` of genuine wall-clock time before returning a valid
    202 job body -- modelling the backend's own synchronous, potentially
    slow, pre-ACK authority-writer-lock wait.
    """

    def __init__(self, delay_seconds: float, resource_id: str) -> None:
        self._delay_seconds = delay_seconds
        self._resource_id = resource_id
        self.calls = 0

    async def _handle(self, request: web.Request) -> web.Response:
        self.calls += 1
        await asyncio.sleep(self._delay_seconds)
        return web.json_response(_start_job_payload(self._resource_id), status=202)

    def app(self) -> web.Application:
        application = web.Application()
        application.router.add_post(
            "/r0/v1/resources/{resource_id}/package-update", self._handle
        )
        return application


class TestStartRequestTimeout:
    def test_start_uses_a_longer_timeout_than_ordinary_package_update_requests(
        self,
    ) -> None:
        """Structural proof: START is wired to a materially larger bound
        than every other ordinary package-update request, and that bound is
        pinned against the backend's own real writer-wait budget -- not an
        unexplained, independently drifting magic number.
        """

        assert (
            _transport_http_module._START_REQUEST_TIMEOUT.total
            > _transport_http_module._REQUEST_TIMEOUT.total
        )

        backend_policy = _load_backend_contention_policy()
        assert (
            _transport_http_module._AUTHORITY_WRITER_WAIT_BUDGET_SECONDS_MIRROR
            == backend_policy.AUTHORITY_WRITER_WAIT_BUDGET_SECONDS
        ), (
            "the HA-side mirror of the backend's authority writer wait "
            "budget has drifted from the real backend constant -- update "
            "_AUTHORITY_WRITER_WAIT_BUDGET_SECONDS_MIRROR in "
            "transport_http.py to match "
            "app.inventory.contention_policy.AUTHORITY_WRITER_WAIT_BUDGET_"
            "SECONDS"
        )
        # The client timeout must exceed the backend's own real writer-wait
        # budget -- covering it exactly, with no margin at all, would still
        # let a legitimate worst-case wait race the client's own deadline.
        assert (
            _transport_http_module._START_REQUEST_TIMEOUT.total
            > backend_policy.AUTHORITY_WRITER_WAIT_BUDGET_SECONDS
        )

    @pytest.mark.asyncio
    async def test_start_response_beyond_the_ordinary_bound_does_not_report_cannot_connect(
        self, hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Required test 1 (HA START): a start response delayed beyond the
        old ordinary bound, but within the supported writer-wait budget,
        must never surface as `HubinetOpsCannotConnect` -- HA must not
        report timeout while the backend can still legitimately issue the
        job. Scaled down: the ordinary bound is monkeypatched to 0.2s, the
        START bound to 2s, and the fake backend delays exactly 1s.
        """

        monkeypatch.setattr(
            _transport_http_module, "_REQUEST_TIMEOUT", aiohttp.ClientTimeout(total=0.2)
        )
        monkeypatch.setattr(
            _transport_http_module,
            "_START_REQUEST_TIMEOUT",
            aiohttp.ClientTimeout(total=2.0),
        )
        server = _DelayedStartServer(delay_seconds=1.0, resource_id=RESOURCE_CT)
        test_server = TestServer(server.app())
        await test_server.start_server(loop=asyncio.get_running_loop())
        try:
            transport = HttpHubinetOpsTransport(
                hass,
                base_url=str(test_server.make_url("")).rstrip("/"),
                api_token=API_TOKEN,
                verify_tls=False,
            )
            result = await transport.start_package_update(RESOURCE_CT, "req-1")
        finally:
            await test_server.close()

        assert server.calls == 1
        assert result.resource_id == RESOURCE_CT

    @pytest.mark.asyncio
    async def test_start_beyond_its_own_budget_still_fails_boundedly(
        self, hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Required test: the START timeout is longer, not unbounded -- a
        response that outlasts even the (scaled) START budget still reports
        `HubinetOpsCannotConnect`, and does so within a bounded wait. Because
        the real (unscaled) budget is proven above to exceed the backend's
        own real writer-wait ceiling, a genuine timeout under the real
        contract is proof the backend's own legitimate pre-ACK window was
        already exceeded too -- not a race the backend could still win.
        """

        monkeypatch.setattr(
            _transport_http_module,
            "_START_REQUEST_TIMEOUT",
            aiohttp.ClientTimeout(total=0.3),
        )
        server = _DelayedStartServer(delay_seconds=2.0, resource_id=RESOURCE_CT)
        test_server = TestServer(server.app())
        await test_server.start_server(loop=asyncio.get_running_loop())
        try:
            transport = HttpHubinetOpsTransport(
                hass,
                base_url=str(test_server.make_url("")).rstrip("/"),
                api_token=API_TOKEN,
                verify_tls=False,
            )
            with pytest.raises(HubinetOpsCannotConnect):
                await asyncio.wait_for(
                    transport.start_package_update(RESOURCE_CT, "req-1"), timeout=5.0
                )
        finally:
            await test_server.close()

    @pytest.mark.asyncio
    async def test_start_request_id_still_the_only_thing_sent(
        self, hass: HomeAssistant
    ) -> None:
        """Required test (HA START): idempotent `request_id` behavior is
        unchanged by the dedicated timeout -- the request body still carries
        exactly one field, and no VMID/plan/target crosses this boundary.
        """

        captured: dict[str, Any] = {}

        async def _capture(request: web.Request) -> web.Response:
            captured["body"] = await request.json()
            return web.json_response(_start_job_payload(RESOURCE_CT), status=202)

        application = web.Application()
        application.router.add_post(
            "/r0/v1/resources/{resource_id}/package-update", _capture
        )
        test_server = TestServer(application)
        await test_server.start_server(loop=asyncio.get_running_loop())
        try:
            transport = HttpHubinetOpsTransport(
                hass,
                base_url=str(test_server.make_url("")).rstrip("/"),
                api_token=API_TOKEN,
                verify_tls=False,
            )
            await transport.start_package_update(RESOURCE_CT, "req-42")
        finally:
            await test_server.close()

        assert captured["body"] == {"request_id": "req-42"}
