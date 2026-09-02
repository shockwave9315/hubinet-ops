"""Concrete typed HTTP transport connecting Home Assistant to Hubinet Ops.

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
    HealthContractStatus,
    HealthContractSummary,
    HealthProbe,
    HealthProbeKind,
    HubinetOpsApi,
    HubinetOpsApiFactory,
    HubinetOpsCannotConnect,
    HubinetOpsConflict,
    HubinetOpsHealthContractUnconfigured,
    HubinetOpsInvalidAuth,
    HubinetOpsInvalidResponse,
    HubinetOpsSnapshot,
    InventorySourceSnapshot,
    LifecycleState,
    NodeAvailability,
    NodeSnapshot,
    ObservationalContinuity,
    PackageScanError,
    PackageScanOs,
    PackageScanPackage,
    PackageScanSnapshot,
    PackageScanStatus,
    PackagePlanApprovalSnapshot,
    PackagePlanApprovalStatus,
    PresenceState,
    ResourceHealthContract,
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
_PACKAGE_PLAN_APPROVAL_ROUTE = (
    "/r0/v1/resources/{resource_id}/package-plan-approval"
)
_HEALTH_CONTRACT_ROUTE = "/r0/v1/resources/{resource_id}/health-contract"


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


def _package_scan_snapshot(payload: Any) -> PackageScanSnapshot:
    if not isinstance(payload, Mapping):
        # A field that is present but null or otherwise not an object is
        # malformed, not missing -- it must still fail validation. Only a
        # genuinely absent key is handled as the backward-compatibility
        # fallback, by the caller, before this function is ever invoked.
        raise TypeError("package_scan must be an object when present")
    os_payload = payload.get("os")
    error_payload = payload.get("error")
    return PackageScanSnapshot(
        status=PackageScanStatus(payload["status"]),
        scan_run_id=payload.get("scan_run_id"),
        started_at=payload.get("started_at"),
        completed_at=payload.get("completed_at"),
        os=(
            PackageScanOs(
                os_id=str(os_payload["id"]), version=str(os_payload["version"])
            )
            if isinstance(os_payload, Mapping)
            else None
        ),
        pending_count=payload.get("pending_count"),
        plan_fingerprint=payload.get("plan_fingerprint"),
        reboot_required=payload.get("reboot_required"),
        packages=tuple(
            PackageScanPackage(
                name=str(package["name"]),
                architecture=str(package["architecture"]),
                installed_version=str(package["installed_version"]),
                candidate_version=str(package["candidate_version"]),
                origin=package.get("origin"),
                description=package.get("description"),
                security=package.get("security"),
            )
            for package in payload.get("packages", ())
        ),
        error=(
            PackageScanError(
                classification=str(error_payload["classification"]),
                message=str(error_payload["message"]),
            )
            if isinstance(error_payload, Mapping)
            else None
        ),
    )


def _package_plan_approval_snapshot(payload: Any) -> PackagePlanApprovalSnapshot:
    if not isinstance(payload, Mapping):
        raise TypeError("package_plan_approval must be an object when present")
    return PackagePlanApprovalSnapshot(
        status=PackagePlanApprovalStatus(payload["status"]),
        approvable=payload["approvable"],
        approval_id=payload.get("approval_id"),
        reviewed_scan_run_id=payload.get("reviewed_scan_run_id"),
        plan_fingerprint=payload.get("plan_fingerprint"),
        approved_at=payload.get("approved_at"),
    )


def _health_contract_summary(payload: Any) -> HealthContractSummary:
    if not isinstance(payload, Mapping):
        raise TypeError("health_contract must be an object when present")
    return HealthContractSummary(
        status=HealthContractStatus(payload["status"]),
        revision=payload.get("revision"),
        fingerprint=payload.get("fingerprint"),
        probe_count=payload.get("probe_count"),
        updated_at=payload.get("updated_at"),
    )


def _resource_health_contract(
    resource_id: str, payload: Any
) -> ResourceHealthContract:
    """Parse one complete contract document from the health-contract route.

    Only ``configured`` is ever built here: the unconfigured case never
    reaches this function, because the backend reports it as a distinct 404
    that the transport raises as
    ``HubinetOpsHealthContractUnconfigured`` rather than as a contract with
    nothing in it.
    """

    if not isinstance(payload, Mapping):
        raise HubinetOpsInvalidResponse("health contract response is not an object")
    try:
        if payload["resource_id"] != resource_id:
            raise HubinetOpsInvalidResponse(
                "health contract response names a different resource"
            )
        status = HealthContractStatus(payload["status"])
        if status is not HealthContractStatus.CONFIGURED:
            raise HubinetOpsInvalidResponse(
                "health contract response carries material without a contract"
            )
        probes = tuple(
            HealthProbe(
                kind=HealthProbeKind(probe["kind"]), target=str(probe["target"])
            )
            for probe in payload["probes"]
        )
        return ResourceHealthContract(
            resource_id=resource_id,
            status=status,
            revision=int(payload["revision"]),
            fingerprint=str(payload["fingerprint"]),
            created_at=str(payload["created_at"]),
            updated_at=str(payload["updated_at"]),
            probes=probes,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise HubinetOpsInvalidResponse(f"malformed health contract: {exc}") from exc


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
        package_scan=(
            # Backward compatibility: an older 0.5 backend predating
            # package scanning publishes resources with no ``package_scan``
            # key at all. Synthesize the same NOT_SCANNED shape the current
            # backend would publish for an unattempted scan -- but only for
            # a genuinely *missing* key. A present key that is null or
            # otherwise malformed must still fail validation, never fall
            # back to this default.
            PackageScanSnapshot()
            if "package_scan" not in payload
            else _package_scan_snapshot(payload["package_scan"])
        ),
        package_plan_approval=(
            PackagePlanApprovalSnapshot()
            if "package_plan_approval" not in payload
            else _package_plan_approval_snapshot(payload["package_plan_approval"])
        ),
        health_contract=(
            # Backward compatibility with a 0.5 backend predating health
            # contracts, handled exactly like package_scan above: a genuinely
            # MISSING key falls back to the default, while a present-but-
            # malformed value still fails validation. The default is
            # unconfigured for LXC and unsupported for anything else, because
            # "we cannot tell" must never render as a declared contract.
            _default_health_contract_summary(payload)
            if "health_contract" not in payload
            else _health_contract_summary(payload["health_contract"])
        ),
        termination_reason=payload.get("termination_reason"),
        successor_resource_id=payload.get("successor_resource_id"),
    )


def _default_health_contract_summary(
    payload: Mapping[str, Any],
) -> HealthContractSummary:
    return HealthContractSummary(
        status=(
            HealthContractStatus.UNCONFIGURED
            if payload.get("resource_type") == ResourceType.LXC.value
            else HealthContractStatus.UNSUPPORTED
        )
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
    """HTTP transport with reads plus one narrow exact-plan approval write."""

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
                "cannot connect to Hubinet Ops backend"
            ) from exc
        except aiohttp.ClientError as exc:
            raise HubinetOpsCannotConnect("Hubinet Ops backend request failed") from exc

    @staticmethod
    async def _decode(response: aiohttp.ClientResponse) -> Any:
        """Map one health-contract response to the typed error taxonomy.

        The 404 split is the point of this helper: an unconfigured contract
        and a missing resource arrive with the same status code and must not
        be collapsed, because only one of them means "the operator has not
        said what healthy means here".
        """

        if response.status in (401, 403):
            raise HubinetOpsInvalidAuth(
                "Hubinet Ops backend rejected the bearer token"
            )
        if response.status in (404, 409, 422):
            error = ""
            try:
                body = await response.json()
            except (aiohttp.ContentTypeError, ValueError):
                body = None
            if isinstance(body, Mapping) and isinstance(body.get("detail"), Mapping):
                error = str(body["detail"].get("error", ""))
            if error == "contract_unconfigured":
                raise HubinetOpsHealthContractUnconfigured(
                    "resource has no configured health contract"
                )
            raise HubinetOpsConflict(
                f"Hubinet Ops refused the health contract request ({error or response.status})"
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

    async def _health_contract_request(
        self, method: str, resource_id: str, **kwargs: Any
    ) -> Any:
        url = f"{self._base_url}{_HEALTH_CONTRACT_ROUTE.format(resource_id=resource_id)}"
        headers = {"Authorization": f"Bearer {self._api_token}"}
        try:
            async with self._session.request(
                method, url, headers=headers, timeout=_REQUEST_TIMEOUT, **kwargs
            ) as response:
                return await self._decode(response)
        except TimeoutError as exc:
            raise HubinetOpsCannotConnect(
                "Hubinet Ops backend request timed out"
            ) from exc
        except aiohttp.ClientConnectorError as exc:
            raise HubinetOpsCannotConnect(
                "cannot connect to Hubinet Ops backend"
            ) from exc
        except aiohttp.ClientError as exc:
            raise HubinetOpsCannotConnect("Hubinet Ops backend request failed") from exc

    async def _put(self, path: str, payload: Mapping[str, str]) -> Any:
        url = f"{self._base_url}{path}"
        headers = {"Authorization": f"Bearer {self._api_token}"}
        try:
            async with self._session.put(
                url,
                headers=headers,
                json=dict(payload),
                timeout=_REQUEST_TIMEOUT,
            ) as response:
                if response.status in (401, 403):
                    raise HubinetOpsInvalidAuth(
                        "Hubinet Ops backend rejected the bearer token"
                    )
                if response.status == 409:
                    raise HubinetOpsConflict(
                        "the reviewed package plan is no longer current"
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
            raise HubinetOpsCannotConnect(
                "Hubinet Ops backend request timed out"
            ) from exc
        except aiohttp.ClientConnectorError as exc:
            raise HubinetOpsCannotConnect(
                "cannot connect to Hubinet Ops backend"
            ) from exc
        except aiohttp.ClientError as exc:
            raise HubinetOpsCannotConnect("Hubinet Ops backend request failed") from exc

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

    async def approve_package_plan(
        self, resource_id: str, scan_run_id: str, plan_fingerprint: str
    ) -> None:
        payload = await self._put(
            _PACKAGE_PLAN_APPROVAL_ROUTE.format(resource_id=resource_id),
            {
                "scan_run_id": scan_run_id,
                "plan_fingerprint": plan_fingerprint,
            },
        )
        if not isinstance(payload, Mapping) or any(
            payload.get(field) != expected
            for field, expected in (
                ("resource_id", resource_id),
                ("reviewed_scan_run_id", scan_run_id),
                ("plan_fingerprint", plan_fingerprint),
            )
        ):
            raise HubinetOpsInvalidResponse(
                "approval response does not match the reviewed plan reference"
            )

    async def fetch_health_contract(self, resource_id: str) -> ResourceHealthContract:
        payload = await self._health_contract_request("GET", resource_id)
        return _resource_health_contract(resource_id, payload)

    async def replace_health_contract(
        self,
        resource_id: str,
        probes: tuple[HealthProbe, ...],
        expected_revision: int | None,
    ) -> ResourceHealthContract:
        body: dict[str, Any] = {
            "probes": [
                {"kind": probe.kind.value, "target": probe.target}
                for probe in probes
            ]
        }
        if expected_revision is not None:
            body["expected_revision"] = expected_revision
        payload = await self._health_contract_request("PUT", resource_id, json=body)
        return _resource_health_contract(resource_id, payload)

    async def clear_health_contract(
        self, resource_id: str, expected_revision: int | None
    ) -> None:
        params: dict[str, Any] = {}
        if expected_revision is not None:
            params["expected_revision"] = expected_revision
        payload = await self._health_contract_request(
            "DELETE", resource_id, params=params
        )
        if (
            not isinstance(payload, Mapping)
            or payload.get("resource_id") != resource_id
            or payload.get("status") != HealthContractStatus.UNCONFIGURED.value
        ):
            raise HubinetOpsInvalidResponse(
                "clear response does not confirm an unconfigured contract"
            )


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
