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
    PackageUpdateHealthOutcome,
    PackageUpdateJobEvent,
    PackageUpdateJobState,
    PackageUpdateJobSummary,
    PackageUpdateJobView,
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

# Pre-ACK side-effect timeout contract -- the authority writer-wait budget
# shared by every route below that can, before its own ACK, wait to become
# the authority store's one SQLite writer (`BEGIN IMMEDIATE`). One product
# update's maintenance-fence acquisition, a workload package-update job's
# own issuance, and arming a same-job rollback all take that SAME lock --
# see `app/inventory/product_update_fence.py` -- so a client deadline too
# short to outlast a legitimate holder is not a per-route coincidence, it
# is one contract every one of these routes shares.
#
# `_AUTHORITY_WRITER_WAIT_BUDGET_SECONDS_MIRROR` mirrors the backend's own
# `AUTHORITY_WRITER_WAIT_BUDGET_SECONDS` (see `app/inventory/contention_
# policy.py`) rather than importing it -- this integration ships and runs
# independently of the backend package. Built from two named
# sub-components, mirroring the backend's own `MAX_HOST_CRITICAL_SECTION_
# SECONDS`/`WRITER_SCHEDULING_MARGIN_SECONDS` individually rather than one
# opaque summed literal: the worst-case bounded host critical section that
# may hold the writer lock (95s), and the scheduling margin on top of it
# (10s). A regression test (tests/test_hubinet_ops_transport_http.py)
# asserts this mirror still equals the real backend constant, so drift
# fails a test instead of silently reopening the P1s this closes.
_MAX_HOST_CRITICAL_SECTION_SECONDS_MIRROR = 95
_WRITER_SCHEDULING_MARGIN_SECONDS_MIRROR = 10
_AUTHORITY_WRITER_WAIT_BUDGET_SECONDS_MIRROR = (
    _MAX_HOST_CRITICAL_SECTION_SECONDS_MIRROR + _WRITER_SCHEDULING_MARGIN_SECONDS_MIRROR
)

# PR #74 review finding 3, corrected -- a dedicated, longer timeout for the
# explicit rollback request ONLY.
#
# The backend's rollback route is synchronous, and durable, before it ever
# returns 202, in exactly this order: it resolves the active job, then
# performs a FRESH read-only canonical PVE snapshot listing
# (`inspect_job_snapshot_state`, bounded by the backend's own
# `PACKAGE_UPDATE_SNAPSHOT_TIMEOUT_SECONDS` = 60s host-control ceiling --
# `_PACKAGE_UPDATE_SNAPSHOT_TIMEOUT_SECONDS_MIRROR` below), and only THEN
# calls `arm_package_update_rollback`, which is an ordinary authority
# writer transaction and can itself legitimately wait the FULL
# `_AUTHORITY_WRITER_WAIT_BUDGET_SECONDS_MIRROR` for the writer lock before
# it durably commits `rollback_may_have_started` and the route acknowledges.
# Those two waits are sequential, not alternatives, so the route's real
# worst-case pre-ACK budget is their SUM: a client timeout that only covers
# one of them (the previous `135s`, pinned against the inspection ceiling
# alone) can still time out while the backend is legitimately still waiting
# to arm the rollback -- reporting failure to the operator for a
# destructive rollback that durably proceeds anyway once the backend
# obtains the lock. See transport_http.py's own `rollback_package_update`.
#
# The `+15s` margin is the SAME bounded margin `_REQUEST_TIMEOUT` already
# gives every other request for ordinary HTTP/TLS/network overhead on top
# of the backend's own processing ceiling -- not a second, independent
# guess at network latency. `_ROLLBACK_ROUTE_PROCESSING_MARGIN_SECONDS`
# covers the route's own small bounded pre-ACK DB work outside the two
# waits above (the job/identity/ownership lookups, never a host round
# trip).
#
# Deliberately NOT a global increase: every other ordinary package-update
# request (fetch/resume) keeps `_REQUEST_TIMEOUT`'s existing 15s bound, and
# this constant exists to cover exactly this route's documented backend
# contract. START has its own dedicated, separately derived timeout below
# for the same underlying writer-wait reason -- see `_START_REQUEST_
# TIMEOUT` -- but does not share the additional snapshot-inspection wait
# rollback has, so the two constants are deliberately not equal.
_PACKAGE_UPDATE_SNAPSHOT_TIMEOUT_SECONDS_MIRROR = 60
_ROLLBACK_ROUTE_PROCESSING_MARGIN_SECONDS = 5
_ROLLBACK_REQUEST_TIMEOUT = aiohttp.ClientTimeout(
    total=_PACKAGE_UPDATE_SNAPSHOT_TIMEOUT_SECONDS_MIRROR
    + _AUTHORITY_WRITER_WAIT_BUDGET_SECONDS_MIRROR
    + _ROLLBACK_ROUTE_PROCESSING_MARGIN_SECONDS
    + 15
)

# Pre-ACK side-effect timeout contract -- the START request's own dedicated
# timeout.
#
# `start_package_update`'s POST is synchronous and durable before it ever
# returns 202: the backend's route calls `issue_package_update_job`, which
# can, before it answers, wait for the SAME shared authority writer lock
# described above. A workload host-control critical section (or another
# package-update job's own submission step) legitimately holding that lock
# is not a bug; a client deadline too short to outlast it is: the operator
# would be told START failed while the backend, moments later, durably
# issues the job anyway and wakes the worker regardless. Unlike rollback,
# START has no separate read-only inspection before it, so its budget is
# the writer-wait budget alone plus its own small bounded margin.
#
# `_START_ROUTE_PROCESSING_MARGIN_SECONDS` covers the route's own small
# bounded pre-ACK DB work (a `request_id` lookup, a fence-file read, a
# handful of `SELECT`s and one `INSERT`, never a host round trip), and the
# final `+15s` is the same ordinary HTTP/TLS/loopback margin used above.
_START_ROUTE_PROCESSING_MARGIN_SECONDS = 5
_START_REQUEST_TIMEOUT = aiohttp.ClientTimeout(
    total=_AUTHORITY_WRITER_WAIT_BUDGET_SECONDS_MIRROR
    + _START_ROUTE_PROCESSING_MARGIN_SECONDS
    + 15
)

_BACKEND_ROUTE = "/r0/v1/backend"
_SNAPSHOT_ROUTE = "/r0/v1/snapshot"
_PACKAGE_PLAN_APPROVAL_ROUTE = (
    "/r0/v1/resources/{resource_id}/package-plan-approval"
)
_HEALTH_CONTRACT_ROUTE = "/r0/v1/resources/{resource_id}/health-contract"
_PACKAGE_UPDATE_ROUTE = "/r0/v1/resources/{resource_id}/package-update"
_PACKAGE_UPDATE_RESUME_ROUTE = _PACKAGE_UPDATE_ROUTE + "/resume"
_PACKAGE_UPDATE_ROLLBACK_ROUTE = _PACKAGE_UPDATE_ROUTE + "/rollback"


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
        post_update_scan_pending=payload["post_update_scan_pending"],
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


def _package_update_job_summary(payload: Any) -> PackageUpdateJobSummary:
    if not isinstance(payload, Mapping):
        raise TypeError("package_update_job must be an object when present")
    outcome = payload.get("health_outcome")
    return PackageUpdateJobSummary(
        state=PackageUpdateJobState(payload["state"]),
        job_id=payload.get("job_id"),
        checkpoint=payload.get("checkpoint"),
        issued_at=payload.get("issued_at"),
        health_outcome=(
            None if outcome is None else PackageUpdateHealthOutcome(outcome)
        ),
        snapshot_confirmed_at=payload.get("snapshot_confirmed_at"),
        mutation_completed_at=payload.get("mutation_completed_at"),
        rollback_completed_at=payload.get("rollback_completed_at"),
        terminalized_at=payload.get("terminalized_at"),
        terminal_reason=payload.get("terminal_reason"),
    )


def _default_package_update_job_summary(
    payload: Mapping[str, Any],
) -> PackageUpdateJobSummary:
    """The fallback for a backend predating production update activation.

    Exactly the same discipline as `package_scan` and `health_contract`: a
    genuinely MISSING key falls back, a present-but-malformed value still
    fails validation. The fallback is `not_started` for LXC and `unsupported`
    otherwise -- never anything that could read as a completed update.
    """

    return PackageUpdateJobSummary(
        state=(
            PackageUpdateJobState.NOT_STARTED
            if payload.get("resource_type") == ResourceType.LXC.value
            else PackageUpdateJobState.UNSUPPORTED
        )
    )


def _package_update_job_view(
    resource_id: str, payload: Any
) -> PackageUpdateJobView:
    """Parse one complete job document from an explicit operator action.

    The backend's body nests snapshot/mutation/health/rollback facts; this
    flattens exactly the fields the integration is contracted to show and
    ignores nothing silently -- a missing required field raises rather than
    defaulting, because a job rendered with an invented field is a job
    described untruthfully.
    """

    if not isinstance(payload, Mapping):
        raise HubinetOpsInvalidResponse("package update response is not an object")
    try:
        if payload["resource_id"] != resource_id:
            raise HubinetOpsInvalidResponse(
                "package update response names a different resource"
            )
        snapshot = payload["snapshot"]
        mutation = payload["mutation"]
        health = payload["health"]
        rollback = payload["rollback"]
        outcome = health.get("outcome")
        return PackageUpdateJobView(
            job_id=str(payload["job_id"]),
            request_id=str(payload["request_id"]),
            resource_id=resource_id,
            status=PackageUpdateJobState(payload["status"]),
            checkpoint=str(payload["checkpoint"]),
            issued_at=str(payload["issued_at"]),
            approved_plan_fingerprint=str(payload["approved_plan_fingerprint"]),
            package_count=int(payload["package_count"]),
            snapshot_name=snapshot.get("name"),
            snapshot_confirmed_at=snapshot.get("confirmed_at"),
            mutation_may_have_started_at=mutation.get("may_have_started_at"),
            mutation_completed_at=mutation.get("completed_at"),
            health_contract_revision=health.get("contract_revision"),
            health_started_at=health.get("started_at"),
            health_completed_at=health.get("completed_at"),
            health_outcome=(
                None if outcome is None else PackageUpdateHealthOutcome(outcome)
            ),
            rollback_may_have_started_at=rollback.get("may_have_started_at"),
            rollback_completed_at=rollback.get("completed_at"),
            rollback_available=bool(rollback["available"]),
            terminalized_at=payload.get("terminalized_at"),
            terminal_reason=payload.get("terminal_reason"),
            events=tuple(
                PackageUpdateJobEvent(
                    sequence=int(event["sequence"]),
                    created_at=str(event["created_at"]),
                    level=str(event["level"]),
                    stage=str(event["stage"]),
                    event_type=str(event["event_type"]),
                    message=str(event["message"]),
                )
                for event in payload.get("events", ())
            ),
        )
    except HubinetOpsInvalidResponse:
        raise
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise HubinetOpsInvalidResponse(
            f"malformed package update job: {exc}"
        ) from exc


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
        package_update_job=(
            _default_package_update_job_summary(payload)
            if "package_update_job" not in payload
            else _package_update_job_summary(payload["package_update_job"])
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

    # ------------------------------------------------------------------
    # Explicit operator update controls.
    #
    # Every one of these is invoked because a person asked for it. None of
    # them is reachable from the coordinator's polling path, and the start
    # call carries exactly one caller-controlled value -- a request id.
    # ------------------------------------------------------------------

    async def _package_update_request(
        self,
        method: str,
        path: str,
        *,
        timeout: aiohttp.ClientTimeout | None = None,
        **kwargs: Any,
    ) -> Any:
        # Looked up by module global at call time rather than bound as a
        # default parameter value, deliberately: a default value would be
        # captured once at function-definition time, which is fine in
        # production (the module-level constants never change after
        # import) but makes the ordinary bound un-patchable by a test that
        # wants to prove the timeout mechanics themselves without waiting
        # out the real production duration.
        effective_timeout = timeout if timeout is not None else _REQUEST_TIMEOUT
        url = f"{self._base_url}{path}"
        headers = {"Authorization": f"Bearer {self._api_token}"}
        try:
            async with self._session.request(
                method, url, headers=headers, timeout=effective_timeout, **kwargs
            ) as response:
                return await self._decode_package_update(response)
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

    @staticmethod
    async def _decode_package_update(response: aiohttp.ClientResponse) -> Any:
        """Map one package-update response to the typed error taxonomy.

        The backend names its own refusal; this preserves that name in the
        raised message so an operator sees "no_current_approval" rather than
        "409". It never re-decides anything: an unrecognized error name is
        still surfaced, not swallowed or reinterpreted.
        """

        if response.status in (401, 403):
            raise HubinetOpsInvalidAuth(
                "Hubinet Ops backend rejected the bearer token"
            )
        if response.status in (400, 404, 409, 422, 503):
            error = ""
            try:
                body = await response.json()
            except (aiohttp.ContentTypeError, ValueError):
                body = None
            if isinstance(body, Mapping) and isinstance(body.get("detail"), Mapping):
                error = str(body["detail"].get("error", ""))
            raise HubinetOpsConflict(
                "Hubinet Ops refused the package update request "
                f"({error or response.status})"
            )
        if response.status not in (200, 202):
            raise HubinetOpsCannotConnect(
                f"Hubinet Ops backend returned HTTP {response.status}"
            )
        try:
            return await response.json()
        except (aiohttp.ContentTypeError, ValueError) as exc:
            raise HubinetOpsInvalidResponse(
                "Hubinet Ops backend returned a non-JSON body"
            ) from exc

    async def start_package_update(
        self, resource_id: str, request_id: str
    ) -> PackageUpdateJobView:
        """Explicitly start the currently approved update for one resource.

        ``request_id`` is the only thing this method sends. There is no
        parameter here for a VMID, a package, a version, a snapshot, a probe,
        or a command, and adding one would be adding a way for Home Assistant
        to decide something the backend authority owns.

        Uses `_START_REQUEST_TIMEOUT`, not the ordinary shared
        `_REQUEST_TIMEOUT`: this request's backend handler durably issues the
        job inside the authority store's writer transaction BEFORE it
        acknowledges (see this module's own `_START_REQUEST_TIMEOUT`
        docstring), and that legitimate pre-ACK wait can outlast the 15s
        bound every other ordinary package-update request uses.
        """

        payload = await self._package_update_request(
            "POST",
            _PACKAGE_UPDATE_ROUTE.format(resource_id=resource_id),
            timeout=_START_REQUEST_TIMEOUT,
            json={"request_id": request_id},
        )
        return _package_update_job_view(resource_id, payload)

    async def fetch_package_update(
        self, resource_id: str, events: int
    ) -> PackageUpdateJobView:
        """Read one resource's current/latest job and a bounded event tail."""

        payload = await self._package_update_request(
            "GET",
            _PACKAGE_UPDATE_ROUTE.format(resource_id=resource_id),
            params={"events": str(events)},
        )
        return _package_update_job_view(resource_id, payload)

    async def resume_package_update(
        self, resource_id: str
    ) -> PackageUpdateJobView:
        """Ask the backend worker to re-enter an existing recoverable job."""

        payload = await self._package_update_request(
            "POST",
            _PACKAGE_UPDATE_RESUME_ROUTE.format(resource_id=resource_id),
            json={},
        )
        return _package_update_job_view(resource_id, payload)

    async def rollback_package_update(
        self, resource_id: str
    ) -> PackageUpdateJobView:
        """Explicitly roll one resource back to its own job's snapshot.

        The operator selects a RESOURCE. No snapshot name, snapshot id, VMID,
        node, operation id, or rollback target crosses this boundary, and the
        backend resolves the one applicable active job itself.

        Uses `_ROLLBACK_REQUEST_TIMEOUT`, not the ordinary shared
        `_REQUEST_TIMEOUT`: this request's backend handler performs a fresh,
        durable, read-only PVE snapshot inspection BEFORE it durably arms
        rollback and returns 202 (see this module's own `_ROLLBACK_REQUEST_
        TIMEOUT` docstring), and that legitimate pre-ACK work can outlast the
        15s bound every other package-update request uses.
        """

        payload = await self._package_update_request(
            "POST",
            _PACKAGE_UPDATE_ROLLBACK_ROUTE.format(resource_id=resource_id),
            timeout=_ROLLBACK_REQUEST_TIMEOUT,
            json={},
        )
        return _package_update_job_view(resource_id, payload)

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
