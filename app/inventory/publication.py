"""Consistent dormant publication boundary for the 0.5 inventory authority."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
from types import MappingProxyType
from typing import Any

from .authority import InventoryAuthority
from .store import InventoryAuthorityStore


def _freeze(value: Any) -> Any:
    if value is None or type(value) in {str, int, float, bool}:
        return value
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    raise TypeError("published authority values must be JSON-like")


@dataclass(frozen=True, slots=True)
class PublishedInventoryView:
    """One deeply immutable logical view from a single SQLite read transaction."""

    backend: Mapping[str, Any]
    sources: tuple[Mapping[str, Any], ...]
    nodes: tuple[Mapping[str, Any], ...]
    resources: tuple[Mapping[str, Any], ...]
    inventory_revision: int
    published_state_revision: int
    published_at: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "backend", _freeze(self.backend))
        object.__setattr__(self, "sources", tuple(_freeze(item) for item in self.sources))
        object.__setattr__(self, "nodes", tuple(_freeze(item) for item in self.nodes))
        object.__setattr__(self, "resources", tuple(_freeze(item) for item in self.resources))


class InventoryPublication:
    def __init__(
        self,
        store: InventoryAuthorityStore,
        authority: InventoryAuthority,
        *,
        backend_name: str = "Hubinet Ops",
        backend_version: str = "0.5",
        api_version: str = "0.5",
    ) -> None:
        self._store = store
        self._authority = authority
        self._backend_name = backend_name
        self._backend_version = backend_version
        self._api_version = api_version

    def read(self) -> PublishedInventoryView:
        """Materialize expiry and assemble one view in the same transaction."""

        with self._store._transaction() as connection:
            decision_time = self._authority._authority_decision_time()
            source_ids = tuple(
                str(row["inventory_source_id"])
                for row in connection.execute(
                    "SELECT inventory_source_id FROM inventory_sources "
                    "ORDER BY inventory_source_id"
                ).fetchall()
            )
            for source_id in source_ids:
                self._authority._materialize_due_expiry_in_transaction(
                    connection, source_id, now=decision_time
                )
            backend_rows = connection.execute("SELECT * FROM backend_instance").fetchall()
            if len(backend_rows) != 1:
                raise RuntimeError("authority database must contain one backend instance")
            backend = backend_rows[0]
            source_rows = connection.execute(
                "SELECT s.*, h.*, e.endpoint_id, e.canonical_transport_locator, "
                "e.canonicalization_contract_version, e.transport_trust_revision "
                "FROM inventory_sources s JOIN source_runtime_health h USING(inventory_source_id) "
                "JOIN source_endpoints e USING(inventory_source_id) "
                "WHERE e.lifecycle='active' ORDER BY s.inventory_source_id"
            ).fetchall()
            node_rows = connection.execute(
                "SELECT * FROM inventory_nodes ORDER BY inventory_source_id, node_id"
            ).fetchall()
            resource_rows = connection.execute(
                "SELECT r.*, active.binding_id AS active_binding_id, "
                "COALESCE(active.locator_generation, history.locator_generation) AS locator_generation "
                "FROM resource_incarnations r "
                "LEFT JOIN resource_locator_bindings active ON active.resource_id=r.resource_id "
                "AND active.valid_to_run_sequence IS NULL "
                "JOIN resource_locator_bindings history ON history.resource_id=r.resource_id "
                "AND history.locator_generation=(SELECT MAX(h2.locator_generation) "
                "FROM resource_locator_bindings h2 WHERE h2.resource_id=r.resource_id) "
                "ORDER BY r.inventory_source_id, r.vmid, locator_generation"
            ).fetchall()

            sources = tuple(self._source(row) for row in source_rows)
            nodes = tuple(self._node(row) for row in node_rows)
            resources = tuple(self._resource(row) for row in resource_rows)
            return PublishedInventoryView(
                backend={
                    "backend_instance_id": str(backend["backend_instance_id"]),
                    "name": self._backend_name,
                    "version": self._backend_version,
                    "api_version": self._api_version,
                },
                sources=sources,
                nodes=nodes,
                resources=resources,
                inventory_revision=int(backend["inventory_revision"]),
                published_state_revision=int(backend["published_state_revision"]),
                published_at=str(backend["published_at"]),
            )

    @staticmethod
    def _source(row) -> dict[str, Any]:
        committed = None
        if row["committed_source_config_revision"] is not None:
            committed = {
                "source_config_revision": int(row["committed_source_config_revision"]),
                "endpoint_id": str(row["committed_endpoint_id"]),
                "canonical_transport_locator": str(row["committed_canonical_transport_locator"]),
                "canonicalization_contract_version": int(row["committed_canonicalization_contract_version"]),
                "transport_trust_revision": int(row["committed_transport_trust_revision"]),
            }
        return {
            "inventory_source_id": str(row["inventory_source_id"]),
            "name": str(row["display_name"]),
            "provider_kind": str(row["provider_kind"]),
            "health": str(row["health"]),
            "freshness": str(row["freshness"]),
            "health_origin": str(row["health_origin"]),
            "health_reason": str(row["health_reason"]),
            "last_issued_run_sequence": int(row["last_issued_run_sequence"]),
            "latest_completed_run_sequence": row["latest_completed_run_sequence"],
            "latest_completed_outcome": row["latest_completed_outcome"],
            "last_health_run_sequence": row["last_health_run_sequence"],
            "last_run_health_outcome": row["last_run_health_outcome"],
            "last_committed_run_sequence": row["last_committed_run_sequence"],
            "last_successful_observed_at": row["last_successful_observed_at"],
            "freshness_reference_at": row["freshness_reference_at"],
            "freshness_valid_until": row["freshness_valid_until"],
            "current_context": {
                "source_config_revision": int(row["source_config_revision"]),
                "endpoint_id": str(row["endpoint_id"]),
                "canonical_transport_locator": str(row["canonical_transport_locator"]),
                "canonicalization_contract_version": int(row["canonicalization_contract_version"]),
                "transport_trust_revision": int(row["transport_trust_revision"]),
            },
            "committed_context": committed,
            "facts": json.loads(str(row["facts_json"])),
        }

    @staticmethod
    def _node(row) -> dict[str, Any]:
        return {
            "node_id": str(row["node_id"]),
            "inventory_source_id": str(row["inventory_source_id"]),
            "name": str(row["external_node_name"]),
            "status": str(row["status"]),
            "available": bool(row["available"]),
            "facts": json.loads(str(row["facts_json"])),
        }

    @staticmethod
    def _resource(row) -> dict[str, Any]:
        return {
            "resource_id": str(row["resource_id"]),
            "inventory_source_id": str(row["inventory_source_id"]),
            "active_binding_id": row["active_binding_id"],
            "resource_type": str(row["resource_type"]),
            "vmid": int(row["vmid"]),
            "locator_generation": int(row["locator_generation"]),
            "resource_continuity_revision": int(row["resource_continuity_revision"]),
            "name": str(row["name"]),
            "status": str(row["status"]),
            "current_node_id": row["current_node_id"],
            "last_known_node_id": row["last_known_node_id"],
            "presence": str(row["presence"]),
            "lifecycle": str(row["lifecycle"]),
            "observational_continuity": str(row["observational_continuity"]),
            "security_continuity": str(row["security_continuity"]),
            "detail_status": str(row["detail_status"]),
            "node_availability": str(row["node_availability"]),
            "state_level": str(row["state_level"]),
            "retained_policy": {},
            "effective_policy": {},
            "policy_applicable": False,
            "suspended_reason": None,
            "effective_capabilities": (),
            "state": json.loads(str(row["facts_json"])),
            "termination_reason": row["termination_reason"],
            "successor_resource_id": row["successor_resource_id"],
        }
