"""Typed read-only contract for the Hubinet Ops backend.

This module intentionally does not define provisional HTTP endpoint paths.  Phase 0
uses an injected transport so the backend 0.5 API can be finalized independently.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Protocol, Self


class HubinetOpsApiError(RuntimeError):
    """Base exception raised by the Hubinet Ops API contract."""


class HubinetOpsInvalidAuth(HubinetOpsApiError):
    """The Hubinet Ops backend rejected the bearer token."""


class HubinetOpsCannotConnect(HubinetOpsApiError):
    """The Hubinet Ops backend could not be reached."""


class HubinetOpsInvalidResponse(HubinetOpsApiError):
    """The Hubinet Ops backend returned data outside the typed contract."""


class ResourceType(StrEnum):
    """Resource types exposed by Hubinet Ops inventory."""

    QEMU = "qemu"
    LXC = "lxc"


class ResourceStateLevel(StrEnum):
    """Backend-owned policy/state level for a resource."""

    DISCOVERED = "discovered"
    OBSERVED = "observed"
    MANAGED = "managed"
    MAINTENANCE = "maintenance"
    BREAK_GLASS = "break_glass"


class PresenceState(StrEnum):
    """Backend reconciliation state for inventory presence."""

    PRESENT = "present"
    TEMPORARILY_UNAVAILABLE = "temporarily_unavailable"
    NODE_UNAVAILABLE = "node_unavailable"
    MISSING = "missing"
    CONFIRMED_REMOVED = "confirmed_removed"


def _deep_freeze(value: Any) -> Any:
    """Recursively freeze one JSON-like backend snapshot value."""

    if value is None or type(value) in {str, int, float, bool}:
        return value
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("snapshot mapping keys must be strings")
        return MappingProxyType(
            {key: _deep_freeze(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_deep_freeze(item) for item in value)
    raise TypeError(
        f"snapshot values must be JSON-like, got {type(value).__name__}"
    )


def _immutable_mapping(value: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """Return a recursively immutable copy of a JSON-like mapping."""

    frozen = _deep_freeze(value or {})
    assert isinstance(frozen, Mapping)
    return frozen


@dataclass(frozen=True, slots=True)
class BackendInformation:
    """Stable identity and version information for one backend instance."""

    instance_id: str
    name: str
    version: str
    api_version: str

    def __post_init__(self) -> None:
        if not self.instance_id.strip():
            raise ValueError("instance_id must not be empty")


@dataclass(frozen=True, slots=True, order=True)
class ResourceIdentity:
    """Durable resource identity independent from name and current node."""

    instance_id: str
    resource_type: ResourceType
    vmid: int

    def __post_init__(self) -> None:
        if not self.instance_id.strip():
            raise ValueError("instance_id must not be empty")
        if type(self.vmid) is not int or self.vmid <= 0:
            raise ValueError("vmid must be a positive integer")

    @property
    def registry_key(self) -> str:
        """Return the deterministic HA serialization of this identity."""

        return f"{self.instance_id}:resource:{self.resource_type.value}:{self.vmid}"


@dataclass(frozen=True, slots=True)
class NodeSnapshot:
    """One node observation supplied by the backend snapshot."""

    instance_id: str
    node_id: str
    name: str
    status: str
    available: bool = True
    facts: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        if not self.instance_id.strip() or not self.node_id.strip():
            raise ValueError("node identity fields must not be empty")
        object.__setattr__(self, "facts", _immutable_mapping(self.facts))

    @property
    def registry_key(self) -> str:
        """Return the deterministic HA serialization of this node identity."""

        return f"{self.instance_id}:node:{self.node_id}"


@dataclass(frozen=True, slots=True)
class ResourceSnapshot:
    """One backend-owned resource observation and effective policy view."""

    identity: ResourceIdentity
    name: str
    node_id: str | None
    status: str
    last_known_node_id: str | None = None
    state_level: ResourceStateLevel = ResourceStateLevel.DISCOVERED
    policy: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    capabilities: frozenset[str] = field(default_factory=frozenset)
    available: bool = True
    presence: PresenceState = PresenceState.PRESENT
    state: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        if self.presence is PresenceState.PRESENT:
            if not isinstance(self.node_id, str) or not self.node_id.strip():
                raise ValueError("present resource must have a current node_id")
            if self.last_known_node_id is not None:
                raise ValueError(
                    "present resource must not also define last_known_node_id"
                )
        else:
            if self.node_id is not None:
                raise ValueError(
                    "non-present resource must use last_known_node_id, not node_id"
                )
            if (
                not isinstance(self.last_known_node_id, str)
                or not self.last_known_node_id.strip()
            ):
                raise ValueError(
                    "non-present resource must have a last_known_node_id"
                )
        object.__setattr__(self, "policy", _immutable_mapping(self.policy))
        object.__setattr__(self, "state", _immutable_mapping(self.state))
        object.__setattr__(self, "capabilities", frozenset(self.capabilities))
        if self.presence is not PresenceState.PRESENT:
            object.__setattr__(self, "available", False)

    @property
    def relation_node_id(self) -> str:
        """Return the current or explicit last-known node for HA topology."""

        if self.presence is PresenceState.PRESENT:
            assert self.node_id is not None
            return self.node_id
        assert self.last_known_node_id is not None
        return self.last_known_node_id

    def as_missing(self) -> Self:
        """Preserve identity and last observation after an unexplained absence."""

        if self.presence is PresenceState.CONFIRMED_REMOVED:
            return self
        return replace(
            self,
            node_id=None,
            last_known_node_id=self.relation_node_id,
            available=False,
            presence=PresenceState.MISSING,
        )


@dataclass(frozen=True, slots=True)
class HubinetOpsSnapshot:
    """One logical, internally consistent backend state snapshot."""

    backend: BackendInformation
    nodes: tuple[NodeSnapshot, ...]
    resources: tuple[ResourceSnapshot, ...]
    generated_at: str | None = None

    def __post_init__(self) -> None:
        node_keys = {(node.instance_id, node.node_id) for node in self.nodes}
        if len(node_keys) != len(self.nodes):
            raise ValueError("snapshot contains duplicate node identities")
        identities = {resource.identity for resource in self.resources}
        if len(identities) != len(self.resources):
            raise ValueError("snapshot contains duplicate resource identities")
        if any(node.instance_id != self.backend.instance_id for node in self.nodes):
            raise ValueError("node belongs to a different backend instance")
        if any(
            resource.identity.instance_id != self.backend.instance_id
            for resource in self.resources
        ):
            raise ValueError("resource belongs to a different backend instance")
        invalid_present_nodes = [
            resource.identity
            for resource in self.resources
            if resource.presence is PresenceState.PRESENT
            and (resource.identity.instance_id, resource.node_id) not in node_keys
        ]
        if invalid_present_nodes:
            raise ValueError(
                "present resource references a node absent from the same snapshot"
            )

    @property
    def nodes_by_id(self) -> dict[str, NodeSnapshot]:
        """Return nodes indexed by the backend-stable node ID."""

        return {node.node_id: node for node in self.nodes}

    @property
    def resources_by_identity(self) -> dict[ResourceIdentity, ResourceSnapshot]:
        """Return resources indexed by durable identity."""

        return {resource.identity: resource for resource in self.resources}

    def preserving_unconfirmed_missing(
        self, previous: HubinetOpsSnapshot | None
    ) -> HubinetOpsSnapshot:
        """Keep resources absent once until the backend owns a removal decision."""

        if previous is None or previous.backend.instance_id != self.backend.instance_id:
            return self
        current = self.resources_by_identity
        retained = tuple(
            old.as_missing()
            for identity, old in previous.resources_by_identity.items()
            if identity not in current
        )
        if not retained:
            return self
        return replace(self, resources=(*self.resources, *retained))


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
            "Hubinet Ops 0.5 backend transport is not configured in Phase 0"
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
    """Create the fail-closed Phase 0 client without inventing HTTP endpoints."""

    return HubinetOpsApi(
        base_url=base_url,
        api_token=api_token,
        verify_tls=verify_tls,
        transport=_UnconfiguredPhaseZeroTransport(),
    )
