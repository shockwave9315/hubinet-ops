"""Typed API surface and compatibility facade for Hubinet Ops."""

from __future__ import annotations

from typing import Protocol

from .contract import (
    BackendInformation,
    DetailStatus,
    HealthContractStatus,
    HealthContractSummary,
    HealthProbe,
    HealthProbeKind,
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


#: How many trailing job events an explicit readback asks for by default.
#: Bounded on both sides: the backend caps what it will return, and this is
#: what Home Assistant asks for when nobody says otherwise.
DEFAULT_PACKAGE_UPDATE_EVENTS = 20


class HubinetOpsApiError(RuntimeError):
    """Base exception raised by the Hubinet Ops API contract."""


class HubinetOpsInvalidAuth(HubinetOpsApiError):
    """The Hubinet Ops backend rejected the bearer token."""


class HubinetOpsCannotConnect(HubinetOpsApiError):
    """The Hubinet Ops backend could not be reached."""


class HubinetOpsInvalidResponse(HubinetOpsApiError):
    """The Hubinet Ops backend returned data outside the typed contract."""


class HubinetOpsConflict(HubinetOpsApiError):
    """The backend refused a stale or mismatched exact plan reference."""


class HubinetOpsHealthContractUnconfigured(HubinetOpsApiError):
    """The resource exists and is current, but declares no health contract.

    A distinct exception rather than an empty result, deliberately: this is
    the one answer a caller must never be able to mistake for "nothing needs
    checking, so it is healthy". Callers that legitimately want to *display*
    the unconfigured state catch this and say so explicitly.
    """


class HubinetOpsTransport(Protocol):
    """Typed backend transport with one exact-plan approval mutation."""

    async def validate_connection(self) -> BackendInformation:
        """Authenticate and validate the backend identity."""

    async def fetch_backend_information(self) -> BackendInformation:
        """Fetch backend identity and version information."""

    async def fetch_resource_snapshot(self) -> HubinetOpsSnapshot:
        """Fetch one logical inventory/state/policy snapshot."""

    async def approve_package_plan(
        self, resource_id: str, scan_run_id: str, plan_fingerprint: str
    ) -> None:
        """Approve the caller-supplied exact reviewed plan reference."""

    async def fetch_health_contract(self, resource_id: str) -> ResourceHealthContract:
        """Read one exact resource's complete declared health contract."""

    async def replace_health_contract(
        self,
        resource_id: str,
        probes: tuple[HealthProbe, ...],
        expected_revision: int | None,
    ) -> ResourceHealthContract:
        """Install one complete health contract for one exact resource."""

    async def clear_health_contract(
        self, resource_id: str, expected_revision: int | None
    ) -> None:
        """Clear one exact resource's health contract."""

    async def start_package_update(
        self, resource_id: str, request_id: str
    ) -> PackageUpdateJobView:
        """Explicitly start the currently approved update for one resource."""

    async def fetch_package_update(
        self, resource_id: str, events: int
    ) -> PackageUpdateJobView:
        """Read one resource's current/latest job and bounded event tail."""

    async def resume_package_update(self, resource_id: str) -> PackageUpdateJobView:
        """Ask the backend worker to re-enter an existing recoverable job."""

    async def rollback_package_update(self, resource_id: str) -> PackageUpdateJobView:
        """Explicitly roll one resource back to its own job's snapshot."""


class HubinetOpsApi:
    """Typed Hubinet Ops client independent from a concrete transport."""

    def __init__(
        self,
        *,
        base_url: str,
        api_token: str,
        verify_tls: bool,
        transport: HubinetOpsTransport,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._api_token = api_token
        self.verify_tls = verify_tls
        self._transport = transport

    async def async_validate_connection(self) -> BackendInformation:
        """Validate authentication and return stable backend information."""

        return await self._transport.validate_connection()

    async def async_fetch_backend_information(self) -> BackendInformation:
        """Fetch backend information."""

        return await self._transport.fetch_backend_information()

    async def async_fetch_resource_snapshot(self) -> HubinetOpsSnapshot:
        """Fetch one logical snapshot for the coordinator."""

        return await self._transport.fetch_resource_snapshot()

    async def async_approve_package_plan(
        self, resource_id: str, scan_run_id: str, plan_fingerprint: str
    ) -> None:
        """Forward one exact reviewed plan reference unchanged."""

        await self._transport.approve_package_plan(
            resource_id, scan_run_id, plan_fingerprint
        )

    async def async_fetch_health_contract(
        self, resource_id: str
    ) -> ResourceHealthContract:
        """Read one resource's declared health contract."""

        return await self._transport.fetch_health_contract(resource_id)

    async def async_replace_health_contract(
        self,
        resource_id: str,
        probes: tuple[HealthProbe, ...],
        expected_revision: int | None = None,
    ) -> ResourceHealthContract:
        """Forward one complete declared contract unchanged."""

        return await self._transport.replace_health_contract(
            resource_id, probes, expected_revision
        )

    async def async_clear_health_contract(
        self, resource_id: str, expected_revision: int | None = None
    ) -> None:
        """Make one resource's health contract unconfigured."""

        await self._transport.clear_health_contract(resource_id, expected_revision)

    async def async_start_package_update(
        self, resource_id: str, request_id: str
    ) -> PackageUpdateJobView:
        """Forward one explicit operator start request unchanged."""

        return await self._transport.start_package_update(resource_id, request_id)

    async def async_fetch_package_update(
        self, resource_id: str, events: int = DEFAULT_PACKAGE_UPDATE_EVENTS
    ) -> PackageUpdateJobView:
        """Read one resource's package-update job."""

        return await self._transport.fetch_package_update(resource_id, events)

    async def async_resume_package_update(
        self, resource_id: str
    ) -> PackageUpdateJobView:
        """Forward one explicit operator resume request unchanged."""

        return await self._transport.resume_package_update(resource_id)

    async def async_rollback_package_update(
        self, resource_id: str
    ) -> PackageUpdateJobView:
        """Forward one explicit operator same-job rollback request unchanged."""

        return await self._transport.rollback_package_update(resource_id)


class HubinetOpsApiFactory(Protocol):
    """Factory boundary used by config flow, setup and fake transports."""

    def __call__(
        self, *, base_url: str, api_token: str, verify_tls: bool
    ) -> HubinetOpsApi:
        """Create a client bound only to the Hubinet Ops backend."""


class _UnconfiguredPhaseZeroTransport:
    """Fail closed until the backend 0.5 HTTP contract is finalized."""

    @staticmethod
    def _error() -> HubinetOpsCannotConnect:
        return HubinetOpsCannotConnect(
            "Hubinet Ops backend transport is not configured"
        )

    async def validate_connection(self) -> BackendInformation:
        raise self._error()

    async def fetch_backend_information(self) -> BackendInformation:
        raise self._error()

    async def fetch_resource_snapshot(self) -> HubinetOpsSnapshot:
        raise self._error()

    async def approve_package_plan(
        self, resource_id: str, scan_run_id: str, plan_fingerprint: str
    ) -> None:
        raise self._error()

    async def fetch_health_contract(self, resource_id: str) -> ResourceHealthContract:
        raise self._error()

    async def replace_health_contract(
        self,
        resource_id: str,
        probes: tuple[HealthProbe, ...],
        expected_revision: int | None,
    ) -> ResourceHealthContract:
        raise self._error()

    async def clear_health_contract(
        self, resource_id: str, expected_revision: int | None
    ) -> None:
        raise self._error()

    async def start_package_update(
        self, resource_id: str, request_id: str
    ) -> PackageUpdateJobView:
        raise self._error()

    async def fetch_package_update(
        self, resource_id: str, events: int
    ) -> PackageUpdateJobView:
        raise self._error()

    async def resume_package_update(self, resource_id: str) -> PackageUpdateJobView:
        raise self._error()

    async def rollback_package_update(self, resource_id: str) -> PackageUpdateJobView:
        raise self._error()


def phase_zero_api_factory(
    *, base_url: str, api_token: str, verify_tls: bool
) -> HubinetOpsApi:
    """Create the fail-closed client without inventing HTTP endpoints."""

    return HubinetOpsApi(
        base_url=base_url,
        api_token=api_token,
        verify_tls=verify_tls,
        transport=_UnconfiguredPhaseZeroTransport(),
    )
