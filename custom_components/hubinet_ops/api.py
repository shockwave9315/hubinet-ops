"""Read-only API surface and compatibility facade for Hubinet Ops."""

from __future__ import annotations

from typing import Protocol

from .contract import (
    BackendInformation,
    DetailStatus,
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


class HubinetOpsApiError(RuntimeError):
    """Base exception raised by the Hubinet Ops API contract."""


class HubinetOpsInvalidAuth(HubinetOpsApiError):
    """The Hubinet Ops backend rejected the bearer token."""


class HubinetOpsCannotConnect(HubinetOpsApiError):
    """The Hubinet Ops backend could not be reached."""


class HubinetOpsInvalidResponse(HubinetOpsApiError):
    """The Hubinet Ops backend returned data outside the typed contract."""


class HubinetOpsTransport(Protocol):
    """Read-only transport implemented by the future backend API adapter."""

    async def validate_connection(self) -> BackendInformation:
        """Authenticate and validate the backend identity."""

    async def fetch_backend_information(self) -> BackendInformation:
        """Fetch backend identity and version information."""

    async def fetch_resource_snapshot(self) -> HubinetOpsSnapshot:
        """Fetch one logical inventory/state/policy snapshot."""


class HubinetOpsApi:
    """Read-only Hubinet Ops client independent from a concrete transport."""

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
