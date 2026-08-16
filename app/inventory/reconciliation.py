"""Transactional resource/node reconciliation for authoritative read-only inventory."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping, Sequence
import json
import sqlite3
from typing import Callable

from .discovery import BaselineCompleteness, NormalizedDiscoverySnapshot


@dataclass(frozen=True, slots=True)
class ReconciliationSummary:
    created_resources: int = 0
    updated_resources: int = 0
    missing_resources: int = 0
    replacements: int = 0


class InventoryReconciler:
    """Apply one complete normalized snapshot inside the caller's transaction."""

    def __init__(self, *, new_uuid: Callable[[], str]) -> None:
        self._new_uuid = new_uuid

    def reconcile(
        self,
        connection: sqlite3.Connection,
        snapshot: NormalizedDiscoverySnapshot,
        *,
        committed_at: str,
    ) -> ReconciliationSummary:
        if snapshot.baseline_completeness is not BaselineCompleteness.COMPLETE:
            raise ValueError("only a complete normalized baseline may reconcile inventory")

        node_ids = self._reconcile_nodes(connection, snapshot, committed_at)
        connection.execute(
            "UPDATE inventory_sources SET facts_json=? WHERE inventory_source_id=?",
            (_json(snapshot.source_facts), snapshot.inventory_source_id),
        )

        active_rows = {
            int(row["vmid"]): row
            for row in connection.execute(
                "SELECT r.*, b.binding_id, b.locator_generation "
                "FROM resource_incarnations r JOIN resource_locator_bindings b "
                "ON b.resource_id=r.resource_id AND b.valid_to_run_sequence IS NULL "
                "WHERE r.inventory_source_id=?",
                (snapshot.inventory_source_id,),
            ).fetchall()
        }
        observed_vmids: set[int] = set()
        counts = {"created": 0, "updated": 0, "missing": 0, "replaced": 0}

        for observed in snapshot.resources:
            observed_vmids.add(observed.vmid)
            old = active_rows.get(observed.vmid)
            node_id = node_ids.get(observed.current_node_name)
            node_available = (
                next(
                    node.available
                    for node in snapshot.nodes
                    if node.external_node_name == observed.current_node_name
                )
                if observed.current_node_name is not None
                else None
            )
            availability = (
                "available" if node_available is True else
                "unavailable" if node_available is False else
                "unresolved"
            )
            if old is None:
                self._create_resource(
                    connection, snapshot, observed, node_id, availability, committed_at
                )
                counts["created"] += 1
                continue
            if str(old["resource_type"]) != observed.resource_type:
                self._replace_resource(
                    connection, snapshot, observed, old, node_id, availability, committed_at
                )
                counts["replaced"] += 1
                continue

            returning_from_gap = str(old["presence"]) == "missing"
            security = str(old["security_continuity"])
            if returning_from_gap and security == "trusted":
                security = "revoked"
            last_known_node_id = (
                None
                if node_id is not None
                else old["current_node_id"] or old["last_known_node_id"]
            )
            connection.execute(
                "UPDATE resource_incarnations SET name=?, status=?, current_node_id=?, "
                "last_known_node_id=?, presence='present', lifecycle=?, "
                "observational_continuity=?, security_continuity=?, detail_status=?, "
                "node_availability=?, facts_json=?, resource_continuity_revision="
                "resource_continuity_revision+?, updated_at=? WHERE resource_id=?",
                (
                    observed.name,
                    observed.status,
                    node_id,
                    last_known_node_id,
                    "quarantined" if returning_from_gap else str(old["lifecycle"]),
                    "uncertain" if returning_from_gap else str(old["observational_continuity"]),
                    security,
                    observed.detail_status.value,
                    availability,
                    _json(observed.facts),
                    int(returning_from_gap),
                    committed_at,
                    str(old["resource_id"]),
                ),
            )
            if returning_from_gap:
                # ADR 0004 §12 retention shape A: the slot is present again,
                # so any retained sampled-absence provenance is no longer
                # current-eligible. Clearing it is a routine, non-security
                # side effect of ordinary reconciliation, not a removal/
                # absence decision of any kind (§3 item 4/5/6/7 unchanged).
                self._clear_absence_pointer(connection, str(old["resource_id"]))
            counts["updated"] += 1

        for vmid, old in active_rows.items():
            if vmid in observed_vmids:
                continue
            resource_id = str(old["resource_id"])
            # ADR 0004 §12 retention shape A: this exact authoritative
            # successful complete-baseline run sampled the known slot
            # absent -- durably record/reconfirm that fact atomically with
            # this same reconciliation commit, even when the resource was
            # already missing and no other field below needs to change.
            # This is machine consistency evidence only (§11); it grants no
            # authority and performs no identity/binding/revision mutation.
            self._upsert_absence_pointer(
                connection,
                resource_id=resource_id,
                inventory_source_id=snapshot.inventory_source_id,
                witness_run_id=snapshot.run_id,
                witness_discovery_run_sequence=snapshot.discovery_run_sequence,
                updated_at=committed_at,
            )
            if str(old["presence"]) == "missing":
                continue
            security = "revoked" if str(old["security_continuity"]) == "trusted" else str(old["security_continuity"])
            last_known = old["current_node_id"] or old["last_known_node_id"]
            connection.execute(
                "UPDATE resource_incarnations SET presence='missing', lifecycle='quarantined', "
                "observational_continuity='uncertain', security_continuity=?, "
                "detail_status='not_applicable', node_availability='not_applicable', "
                "current_node_id=NULL, last_known_node_id=?, resource_continuity_revision="
                "resource_continuity_revision+1, updated_at=? WHERE resource_id=?",
                (security, last_known, committed_at, resource_id),
            )
            counts["missing"] += 1

        return ReconciliationSummary(
            created_resources=counts["created"],
            updated_resources=counts["updated"],
            missing_resources=counts["missing"],
            replacements=counts["replaced"],
        )

    def _reconcile_nodes(
        self,
        connection: sqlite3.Connection,
        snapshot: NormalizedDiscoverySnapshot,
        committed_at: str,
    ) -> dict[str, str]:
        existing = {
            str(row["external_node_name"]): str(row["node_id"])
            for row in connection.execute(
                "SELECT node_id, external_node_name FROM inventory_nodes "
                "WHERE inventory_source_id=?",
                (snapshot.inventory_source_id,),
            ).fetchall()
        }
        result: dict[str, str] = {}
        observed_names: set[str] = set()
        for node in snapshot.nodes:
            observed_names.add(node.external_node_name)
            node_id = existing.get(node.external_node_name) or self._new_uuid()
            result[node.external_node_name] = node_id
            connection.execute(
                "INSERT INTO inventory_nodes(node_id, inventory_source_id, external_node_name, "
                "status, available, facts_json, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(inventory_source_id, external_node_name) DO UPDATE SET "
                "status=excluded.status, available=excluded.available, facts_json=excluded.facts_json, "
                "updated_at=excluded.updated_at",
                (
                    node_id,
                    snapshot.inventory_source_id,
                    node.external_node_name,
                    node.status,
                    int(node.available),
                    _json(node.facts),
                    committed_at,
                    committed_at,
                ),
            )
        for name in set(existing) - observed_names:
            connection.execute(
                "UPDATE inventory_nodes SET status='unavailable', available=0, updated_at=? "
                "WHERE inventory_source_id=? AND external_node_name=?",
                (committed_at, snapshot.inventory_source_id, name),
            )
        return result

    def _create_resource(self, connection, snapshot, observed, node_id, availability, committed_at) -> None:
        generation = int(
            connection.execute(
                "SELECT COALESCE(MAX(locator_generation), 0) FROM resource_locator_bindings "
                "WHERE inventory_source_id=? AND vmid=?",
                (snapshot.inventory_source_id, observed.vmid),
            ).fetchone()[0]
        ) + 1
        resource_id = self._new_uuid()
        binding_id = self._new_uuid()
        connection.execute(
            "INSERT INTO resource_incarnations(resource_id, inventory_source_id, resource_type, vmid, "
            "resource_continuity_revision, name, status, current_node_id, last_known_node_id, presence, "
            "lifecycle, observational_continuity, security_continuity, detail_status, node_availability, "
            "state_level, facts_json, termination_reason, successor_resource_id, created_at, updated_at) "
            "VALUES(?, ?, ?, ?, 1, ?, ?, ?, NULL, 'present', 'active', 'consistent', 'unverified', ?, ?, "
            "'discovered', ?, NULL, NULL, ?, ?)",
            (resource_id, snapshot.inventory_source_id, observed.resource_type, observed.vmid, observed.name,
             observed.status, node_id, observed.detail_status.value, availability, _json(observed.facts),
             committed_at, committed_at),
        )
        connection.execute(
            "INSERT INTO resource_locator_bindings(binding_id, inventory_source_id, vmid, locator_generation, "
            "resource_id, valid_from_run_sequence, valid_to_run_sequence, closure_reason) "
            "VALUES(?, ?, ?, ?, ?, ?, NULL, NULL)",
            (binding_id, snapshot.inventory_source_id, observed.vmid, generation, resource_id, snapshot.discovery_run_sequence),
        )

    def _replace_resource(self, connection, snapshot, observed, old, node_id, availability, committed_at) -> None:
        old_resource_id = str(old["resource_id"])
        old_binding_id = str(old["binding_id"])
        old_generation = int(old["locator_generation"])
        # ADR 0004 §12 retention shape A: a replaced old resource is
        # terminal (not_current/retired) and must not retain an eligible
        # current sampled-absence pointer, whether or not it was missing
        # immediately beforehand.
        self._clear_absence_pointer(connection, old_resource_id)
        successor_id = self._new_uuid()
        successor_binding_id = self._new_uuid()
        old_security = "revoked" if str(old["security_continuity"]) == "trusted" else str(old["security_continuity"])
        last_known = old["current_node_id"] or old["last_known_node_id"]
        connection.execute(
            "UPDATE resource_locator_bindings SET valid_to_run_sequence=?, closure_reason='replaced' "
            "WHERE binding_id=? AND valid_to_run_sequence IS NULL",
            (snapshot.discovery_run_sequence, old_binding_id),
        )
        connection.execute(
            "UPDATE resource_incarnations SET presence='not_current', lifecycle='retired', "
            "observational_continuity='replaced', security_continuity=?, detail_status='not_applicable', "
            "node_availability='not_applicable', current_node_id=NULL, last_known_node_id=?, "
            "resource_continuity_revision=resource_continuity_revision+1, termination_reason='replaced', "
            "successor_resource_id=?, updated_at=? WHERE resource_id=?",
            (old_security, last_known, successor_id, committed_at, old_resource_id),
        )
        connection.execute(
            "INSERT INTO resource_incarnations(resource_id, inventory_source_id, resource_type, vmid, "
            "resource_continuity_revision, name, status, current_node_id, last_known_node_id, presence, "
            "lifecycle, observational_continuity, security_continuity, detail_status, node_availability, "
            "state_level, facts_json, termination_reason, successor_resource_id, created_at, updated_at) "
            "VALUES(?, ?, ?, ?, 1, ?, ?, ?, NULL, 'present', 'active', 'consistent', 'unverified', ?, ?, "
            "'discovered', ?, NULL, NULL, ?, ?)",
            (successor_id, snapshot.inventory_source_id, observed.resource_type, observed.vmid, observed.name,
             observed.status, node_id, observed.detail_status.value, availability, _json(observed.facts),
             committed_at, committed_at),
        )
        connection.execute(
            "INSERT INTO resource_locator_bindings VALUES(?, ?, ?, ?, ?, ?, NULL, NULL)",
            (successor_binding_id, snapshot.inventory_source_id, observed.vmid, old_generation + 1,
             successor_id, snapshot.discovery_run_sequence),
        )
        connection.execute(
            "INSERT INTO resource_terminations("
            "resource_id, inventory_source_id, binding_id, locator_generation, reason, "
            "successor_resource_id, run_sequence, class_c_decision_id, created_at) "
            "VALUES(?, ?, ?, ?, 'replaced', ?, ?, NULL, ?)",
            (old_resource_id, snapshot.inventory_source_id, old_binding_id, old_generation,
             successor_id, snapshot.discovery_run_sequence, committed_at),
        )

    @staticmethod
    def _upsert_absence_pointer(
        connection: sqlite3.Connection,
        *,
        resource_id: str,
        inventory_source_id: str,
        witness_run_id: str,
        witness_discovery_run_sequence: int,
        updated_at: str,
    ) -> None:
        """ADR 0004 §12 retention shape A: overwrite, never append.

        A single bounded row per resource; only ever written by an
        authoritative successful complete-baseline reconciliation commit
        (never by a partial/failed/stale run, which never reaches this
        method at all), and carries no identity/binding/revision/authority
        side effect of its own.
        """

        connection.execute(
            "INSERT INTO resource_absence_pointers("
            "resource_id, inventory_source_id, witness_run_id, "
            "witness_discovery_run_sequence, updated_at) "
            "VALUES(?, ?, ?, ?, ?) "
            "ON CONFLICT(resource_id) DO UPDATE SET "
            "witness_run_id=excluded.witness_run_id, "
            "witness_discovery_run_sequence=excluded.witness_discovery_run_sequence, "
            "updated_at=excluded.updated_at",
            (
                resource_id,
                inventory_source_id,
                witness_run_id,
                witness_discovery_run_sequence,
                updated_at,
            ),
        )

    @staticmethod
    def _clear_absence_pointer(connection: sqlite3.Connection, resource_id: str) -> None:
        """Delete (never merely mark) retention-shape-A provenance that is
        no longer current-eligible -- the slot is present again or the old
        resource has become terminal via replacement (ADR 0004 §12)."""

        connection.execute(
            "DELETE FROM resource_absence_pointers WHERE resource_id=?", (resource_id,)
        )


def _json(value: object) -> str:
    return json.dumps(_plain(value), sort_keys=True, separators=(",", ":"))


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_plain(item) for item in value]
    return value
