"""Concrete read-only HTTP transport connecting Home Assistant to Hubinet Ops.

See ``ARCHITECTURE.md``.

This transport talks only to the Hubinet Ops R0 backend -- it never
connects directly to Proxmox, and it structurally cannot: the published/
HTTP/HA contract has no PVE-credential-shaped field anywhere, so
there is nothing here to read, store, or forward even by mistake. It uses
Home Assistant's own shared ``aiohttp`` client session
(``homeassistant.helpers.aiohttp_client.async_get_clientsession``), never a
bespoke HTTP client, matching the pattern the vendored ``proxmoxve``
integration itself uses.
"""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    BackendInformation,
    DetailStatus,
    HubinetOpsApi,
    HubinetOpsApiFactory,
    HubinetOpsCannotConnect,
    HubinetOpsInvalidAuth,
    HubinetOpsInvalidResponse,
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

_LOGGER = logging.getLogger(__name__)

# Bounded, never unbounded (mirrors the production PVE transport's own
# posture). Tunable value, not an architecture decision.
_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15)

_BACKEND_ROUTE = "/r0/v1/backend"
_SNAPSHOT_ROUTE = "/r0/v1/snapshot"


def _backend_information(payload: Mapping[str, Any]) -> BackendInformation:
    try:
        return BackendInformation(
            backend_instance_id=str(payload["backend_instance_id"]),
            name=str(payload["name"]),
            version=str(payload["version"]),
            api_version=str(payload["api_version"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise HubinetOpsInvalidResponse(f"malformed backend information: {exc}") from exc


def _source_context(payload: Mapping[str, Any] | None) -> SourceContext | None:
    if payload is None:
        return None
    return SourceContext(
        source_config_revision=int(payload["source_config_revision"]),
        endpoint_id=str(payload["endpoint_id"]),
        canonical_transport_locator=str(payload["canonical_transport_locator"]),
        canonicalization_contract_version=int(payload["canonicalization_contract_version"]),
        transport_trust_revision=int(payload["transport_trust_revision"]),
    )


def _source_snapshot(payload: Mapping[str, Any]) -> InventorySourceSnapshot:
    return InventorySourceSnapshot(
        inventory_source_id=str(payload["inventory_source_id"]),
        name=str(payload["name"]),
        provider_kind=str(payload["provider_kind"]),
        health=SourceHealth(payload["health"]),
        freshness=SourceFreshness(payload["freshness"]),
        health_origin=SourceHealthOrigin(payload["health_origin"]),
        health_reason=str(payload["health_reason"]),
        last_issued_run_sequence=int(payload["last_issued_run_sequence"]),
        latest_completed_run_sequence=payload["latest_completed_run_sequence"],
        latest_completed_outcome=payload["latest_completed_outcome"],
        last_health_run_sequence=payload["last_health_run_sequence"],
        last_run_health_outcome=payload["last_run_health_outcome"],
        last_committed_run_sequence=payload["last_committed_run_sequence"],
        last_successful_observed_at=payload["last_successful_observed_at"],
        freshness_reference_at=payload["freshness_reference_at"],
        freshness_valid_until=payload["freshness_valid_until"],
        current_context=_source_context(payload["current_context"]),
        committed_context=_source_context(payload.get("committed_context")),
        facts=payload.get("facts") or {},
    )


def _node_snapshot(payload: Mapping[str, Any]) -> NodeSnapshot:
    return NodeSnapshot(
        node_id=str(payload["node_id"]),
        inventory_source_id=str(payload["inventory_source_id"]),
        name=str(payload["name"]),
        status=str(payload["status"]),
        available=bool(payload["available"]),
        facts=payload.get("facts") or {},
    )


def _resource_snapshot(payload: Mapping[str, Any]) -> ResourceSnapshot:
    return ResourceSnapshot(
        resource_id=str(payload["resource_id"]),
        inventory_source_id=str(payload["inventory_source_id"]),
        active_binding_id=payload["active_binding_id"],
        resource_type=ResourceType(payload["resource_type"]),
        vmid=int(payload["vmid"]),
        locator_generation=int(payload["locator_generation"]),
        resource_continuity_revision=int(payload["resource_continuity_revision"]),
        name=str(payload["name"]),
        status=str(payload["status"]),
        current_node_id=payload["current_node_id"],
        last_known_node_id=payload["last_known_node_id"],
        presence=PresenceState(payload["presence"]),
        lifecycle=LifecycleState(payload["lifecycle"]),
        observational_continuity=ObservationalContinuity(payload["observational_continuity"]),
        security_continuity=SecurityContinuity(payload["security_continuity"]),
        detail_status=DetailStatus(payload["detail_status"]),
        node_availability=NodeAvailability(payload["node_availability"]),
        state_level=ResourceStateLevel(
            payload.get("state_level", ResourceStateLevel.DISCOVERED.value)
        ),
        retained_policy=payload.get("retained_policy") or {},
        effective_policy=payload.get("effective_policy") or {},
        policy_applicable=bool(payload.get("policy_applicable", False)),
        suspended_reason=payload.get("suspended_reason"),
        # JSON has no frozenset type -- explicit conversion (mismatch 2).
        effective_capabilities=frozenset(payload.get("effective_capabilities") or ()),
        state=payload.get("state") or {},
        termination_reason=payload.get("termination_reason"),
        successor_resource_id=payload.get("successor_resource_id"),
    )


def _snapshot_from_payload(payload: Mapping[str, Any]) -> HubinetOpsSnapshot:
    return HubinetOpsSnapshot(
        backend=_backend_information(payload["backend"]),
        sources=tuple(_source_snapshot(item) for item in payload["sources"]),
        nodes=tuple(_node_snapshot(item) for item in payload["nodes"]),
        resources=tuple(_resource_snapshot(item) for item in payload["resources"]),
        inventory_revision=int(payload["inventory_revision"]),
        published_state_revision=int(payload["published_state_revision"]),
        published_at=str(payload["published_at"]),
    )


class HttpHubinetOpsTransport:
    """Read-only HTTP transport implementing the ``HubinetOpsTransport`` Protocol.

    Reads backend/snapshot state only -- no write methods exist, and none
    may be added even for convenience.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        base_url: str,
        api_token: str,
        verify_tls: bool,
    ) -> None:
        self._session = async_get_clientsession(hass, verify_ssl=verify_tls)
        self._base_url = base_url.rstrip("/")
        self._api_token = api_token

    async def _get(self, path: str) -> Any:
        url = f"{self._base_url}{path}"
        headers = {"Authorization": f"Bearer {self._api_token}"}
        try:
            async with self._session.get(
                url, headers=headers, timeout=_REQUEST_TIMEOUT
            ) as response:
                if response.status in (401, 403):
                    raise HubinetOpsInvalidAuth(
                        "Hubinet Ops backend rejected the bearer token"
                    )
                if response.status != 200:
                    raise HubinetOpsCannotConnect(
                        f"Hubinet Ops backend returned HTTP {response.status}"
                    )
                try:
                    return await response.json()
                except (aiohttp.ContentTypeError, ValueError) as exc:
                    raise HubinetOpsInvalidResponse(
                        "Hubinet Ops backend returned a non-JSON body"
                    ) from exc
        except TimeoutError as exc:
            raise HubinetOpsCannotConnect("Hubinet Ops backend request timed out") from exc
        except aiohttp.ClientConnectorError as exc:
            raise HubinetOpsCannotConnect(
                f"cannot connect to Hubinet Ops backend: {exc}"
            ) from exc
        except aiohttp.ClientError as exc:
            raise HubinetOpsCannotConnect(f"Hubinet Ops backend request failed: {exc}") from exc

    async def validate_connection(self) -> BackendInformation:
        payload = await self._get(_BACKEND_ROUTE)
        return _backend_information(payload)

    async def fetch_backend_information(self) -> BackendInformation:
        payload = await self._get(_BACKEND_ROUTE)
        return _backend_information(payload)

    async def fetch_resource_snapshot(self) -> HubinetOpsSnapshot:
        payload = await self._get(_SNAPSHOT_ROUTE)
        try:
            return _snapshot_from_payload(payload)
        except (KeyError, TypeError, ValueError) as exc:
            raise HubinetOpsInvalidResponse(f"malformed Hubinet Ops snapshot: {exc}") from exc


def http_api_factory(hass: HomeAssistant) -> HubinetOpsApiFactory:
    """Return a ``HubinetOpsApiFactory`` bound to this ``hass`` instance.

    The ``HubinetOpsApiFactory`` Protocol itself carries no ``hass``
    parameter (config flow/setup call it with only ``base_url``/
    ``api_token``/``verify_tls``), so ``hass`` is captured here via
    closure at registration time instead.
    """

    def _factory(*, base_url: str, api_token: str, verify_tls: bool) -> HubinetOpsApi:
        return HubinetOpsApi(
            base_url=base_url,
            api_token=api_token,
            verify_tls=verify_tls,
            transport=HttpHubinetOpsTransport(
                hass, base_url=base_url, api_token=api_token, verify_tls=verify_tls
            ),
        )

    return _factory
