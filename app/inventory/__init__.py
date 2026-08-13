"""Hubinet Ops 0.5 persistent inventory authority core."""

from .authority import InventoryAuthority
from .canonicalization import (
    CANONICALIZATION_CONTRACT_VERSION,
    canonicalize_transport_locator,
)
from .models import (
    AuthorityConflict,
    AuthorityDatabaseRejected,
    AuthorityError,
    AuthorityInvariantError,
    AuthorityNotFound,
    BackendInstance,
    DiscoveryRun,
    DiscoveryRunLifecycle,
    EndpointLifecycle,
    InventorySource,
    InventorySourceState,
    PersistentSourceFreshness,
    PersistentSourceHealth,
    PersistentSourceHealthOrigin,
    SourceEndpoint,
    SourceRuntimeHealth,
)
from .store import InventoryAuthorityStore

__all__ = [
    "AuthorityConflict",
    "AuthorityDatabaseRejected",
    "AuthorityError",
    "AuthorityInvariantError",
    "AuthorityNotFound",
    "BackendInstance",
    "CANONICALIZATION_CONTRACT_VERSION",
    "DiscoveryRun",
    "DiscoveryRunLifecycle",
    "EndpointLifecycle",
    "InventoryAuthority",
    "InventoryAuthorityStore",
    "InventorySource",
    "InventorySourceState",
    "PersistentSourceFreshness",
    "PersistentSourceHealth",
    "PersistentSourceHealthOrigin",
    "SourceEndpoint",
    "SourceRuntimeHealth",
    "canonicalize_transport_locator",
]
