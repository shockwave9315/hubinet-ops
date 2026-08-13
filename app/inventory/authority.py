"""Typed mutation boundary for Hubinet Ops 0.5 persistent authority."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
import sqlite3
import uuid

from .canonicalization import (
    CANONICALIZATION_CONTRACT_VERSION,
    canonicalize_transport_locator,
)
from .models import (
    AuthorityConflict,
    AuthorityInvariantError,
    AuthorityNotFound,
    DiscoveryRun,
    DiscoveryRunLifecycle,
    InventorySourceState,
)
from .store import InventoryAuthorityStore


class InventoryAuthority:
    """Expose only explicit authority-changing Phase 1A operations."""

    def __init__(
        self,
        store: InventoryAuthorityStore,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._now = now or (lambda: datetime.now(UTC))

    def create_inventory_source(
        self,
        *,
        provider_kind: str,
        display_name: str,
        credential_reference: str,
        transport_locator: str,
    ) -> InventorySourceState:
        """Atomically establish one source, active endpoint, and initial health."""

        provider_kind = _require_text(provider_kind, "provider_kind", max_length=100)
        display_name = _require_text(display_name, "display_name", max_length=300)
        credential_reference = _require_text(
            credential_reference, "credential_reference", max_length=500
        )
        canonical_locator = canonicalize_transport_locator(transport_locator)
        source_id = _new_uuid()
        endpoint_id = _new_uuid()
        created_at = _timestamp(self._now())

        with self._store._transaction() as connection:
            backend = connection.execute(
                "SELECT backend_instance_id FROM backend_instance"
            ).fetchall()
            if len(backend) != 1:
                raise AuthorityInvariantError(
                    "authority database must contain exactly one backend instance"
                )
            connection.execute(
                "INSERT INTO inventory_sources("
                "inventory_source_id, backend_instance_id, provider_kind, display_name, "
                "credential_reference, created_at, source_config_revision, "
                "last_issued_run_sequence, last_committed_run_sequence, "
                "active_discovery_run_id) VALUES(?, ?, ?, ?, ?, ?, 1, 0, NULL, NULL)",
                (
                    source_id,
                    str(backend[0]["backend_instance_id"]),
                    provider_kind,
                    display_name,
                    credential_reference,
                    created_at,
                ),
            )
            self._insert_initial_endpoint(
                connection,
                source_id=source_id,
                endpoint_id=endpoint_id,
                canonical_locator=canonical_locator,
                created_at=created_at,
            )
            self._insert_initial_health(connection, source_id=source_id)
            self._bump_global_revisions(
                connection, inventory_changed=True, published_changed=True
            )

        return self._store.source_state(source_id)

    def rename_inventory_source(
        self, inventory_source_id: str, display_name: str
    ) -> InventorySourceState:
        """Change presentation only, preserving source configuration and run context."""

        source_id = _require_uuid(inventory_source_id, "inventory_source_id")
        name = _require_text(display_name, "display_name", max_length=300)
        with self._store._transaction() as connection:
            row = self._require_source_row(connection, source_id)
            if str(row["display_name"]) != name:
                connection.execute(
                    "UPDATE inventory_sources SET display_name=? "
                    "WHERE inventory_source_id=?",
                    (name, source_id),
                )
                self._bump_global_revisions(
                    connection, inventory_changed=True, published_changed=True
                )
        return self._store.source_state(source_id)

    def issue_discovery_run(
        self,
        inventory_source_id: str,
        provider_contract_version: int,
    ) -> DiscoveryRun:
        """Durably allocate and own one per-source run before provider I/O."""

        source_id = _require_uuid(inventory_source_id, "inventory_source_id")
        contract_version = _require_positive_integer(
            provider_contract_version, "provider_contract_version"
        )
        run_id = _new_uuid()
        issued_at = _timestamp(self._now())

        with self._store._transaction() as connection:
            source = self._require_source_row(connection, source_id)
            if source["active_discovery_run_id"] is not None:
                raise AuthorityConflict(
                    "inventory source already has an active discovery run"
                )
            endpoint_rows = connection.execute(
                "SELECT * FROM source_endpoints "
                "WHERE inventory_source_id=? AND lifecycle='active'",
                (source_id,),
            ).fetchall()
            if len(endpoint_rows) != 1:
                raise AuthorityInvariantError(
                    "inventory source must have exactly one active endpoint"
                )
            endpoint = endpoint_rows[0]
            old_sequence = int(source["last_issued_run_sequence"])
            new_sequence = old_sequence + 1
            updated = connection.execute(
                "UPDATE inventory_sources SET last_issued_run_sequence=? "
                "WHERE inventory_source_id=? AND last_issued_run_sequence=? "
                "AND active_discovery_run_id IS NULL",
                (new_sequence, source_id, old_sequence),
            )
            if updated.rowcount != 1:
                raise AuthorityConflict("discovery run issuance lost source ownership")
            connection.execute(
                "INSERT INTO discovery_runs("
                "run_id, inventory_source_id, discovery_run_sequence, issued_at, "
                "expected_source_config_revision, expected_endpoint_id, "
                "expected_canonical_transport_locator, "
                "expected_canonicalization_contract_version, "
                "expected_transport_trust_revision, provider_contract_version, lifecycle, "
                "terminalized_at, terminal_reason, completed_at, provider_outcome, "
                "observed_at, normalized_snapshot_hash) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'issued', "
                "NULL, NULL, NULL, NULL, NULL, NULL)",
                (
                    run_id,
                    source_id,
                    new_sequence,
                    issued_at,
                    int(source["source_config_revision"]),
                    str(endpoint["endpoint_id"]),
                    str(endpoint["canonical_transport_locator"]),
                    int(endpoint["canonicalization_contract_version"]),
                    int(endpoint["transport_trust_revision"]),
                    contract_version,
                ),
            )
            self._claim_active_run(connection, source_id=source_id, run_id=run_id)
            self._bump_global_revisions(
                connection, inventory_changed=False, published_changed=True
            )

        return self._store.discovery_run(run_id)

    def mark_discovery_run_running(
        self, inventory_source_id: str, run_id: str
    ) -> DiscoveryRun:
        """Move the exact issued owner to running without changing its context."""

        source_id = _require_uuid(inventory_source_id, "inventory_source_id")
        canonical_run_id = _require_uuid(run_id, "run_id")
        with self._store._transaction() as connection:
            source = self._require_source_row(connection, source_id)
            run = self._require_run_row(connection, canonical_run_id)
            self._require_exact_run_owner(source, run, source_id, canonical_run_id)
            if run["lifecycle"] != DiscoveryRunLifecycle.ISSUED.value:
                raise AuthorityConflict("only an issued discovery run can begin running")
            connection.execute(
                "UPDATE discovery_runs SET lifecycle='running' WHERE run_id=?",
                (canonical_run_id,),
            )
            self._bump_global_revisions(
                connection, inventory_changed=False, published_changed=True
            )
        return self._store.discovery_run(canonical_run_id)

    def abandon_discovery_run(
        self,
        inventory_source_id: str,
        run_id: str,
        *,
        reason: str,
    ) -> DiscoveryRun:
        """One-time fence and release of the exact active incomplete run."""

        source_id = _require_uuid(inventory_source_id, "inventory_source_id")
        canonical_run_id = _require_uuid(run_id, "run_id")
        terminal_reason = _require_text(reason, "reason", max_length=500)
        terminalized_at = _timestamp(self._now())

        with self._store._transaction() as connection:
            source = self._require_source_row(connection, source_id)
            run = self._require_run_row(connection, canonical_run_id)
            self._require_exact_run_owner(source, run, source_id, canonical_run_id)
            if run["lifecycle"] not in {
                DiscoveryRunLifecycle.ISSUED.value,
                DiscoveryRunLifecycle.RUNNING.value,
            }:
                raise AuthorityConflict("discovery run is already terminal")

            released = connection.execute(
                "UPDATE inventory_sources SET active_discovery_run_id=NULL "
                "WHERE inventory_source_id=? AND active_discovery_run_id=?",
                (source_id, canonical_run_id),
            )
            if released.rowcount != 1:
                raise AuthorityConflict("discovery run no longer owns the source")
            self._terminalize_abandoned_run(
                connection,
                run_id=canonical_run_id,
                terminalized_at=terminalized_at,
                reason=terminal_reason,
            )
            self._bump_global_revisions(
                connection, inventory_changed=False, published_changed=True
            )

        return self._store.discovery_run(canonical_run_id)

    def _insert_initial_endpoint(
        self,
        connection: sqlite3.Connection,
        *,
        source_id: str,
        endpoint_id: str,
        canonical_locator: str,
        created_at: str,
    ) -> None:
        connection.execute(
            "INSERT INTO source_endpoints("
            "endpoint_id, inventory_source_id, canonical_transport_locator, "
            "canonicalization_contract_version, lifecycle, "
            "transport_trust_revision, created_at) "
            "VALUES(?, ?, ?, ?, 'active', 1, ?)",
            (
                endpoint_id,
                source_id,
                canonical_locator,
                CANONICALIZATION_CONTRACT_VERSION,
                created_at,
            ),
        )

    def _insert_initial_health(
        self, connection: sqlite3.Connection, *, source_id: str
    ) -> None:
        connection.execute(
            "INSERT INTO source_runtime_health("
            "inventory_source_id, health, freshness, health_origin, health_reason, "
            "latest_completed_run_sequence, latest_completed_outcome, "
            "last_health_run_sequence, last_run_health_outcome, "
            "last_successful_observed_at, freshness_reference_at, "
            "freshness_valid_until, committed_source_config_revision, "
            "committed_endpoint_id, committed_canonical_transport_locator, "
            "committed_canonicalization_contract_version, "
            "committed_transport_trust_revision) "
            "VALUES(?, 'not_yet_observed', 'not_yet_observed', 'initial', '', "
            "NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL)",
            (source_id,),
        )

    def _claim_active_run(
        self, connection: sqlite3.Connection, *, source_id: str, run_id: str
    ) -> None:
        claimed = connection.execute(
            "UPDATE inventory_sources SET active_discovery_run_id=? "
            "WHERE inventory_source_id=? AND active_discovery_run_id IS NULL",
            (run_id, source_id),
        )
        if claimed.rowcount != 1:
            raise AuthorityConflict("discovery run could not claim source ownership")

    def _terminalize_abandoned_run(
        self,
        connection: sqlite3.Connection,
        *,
        run_id: str,
        terminalized_at: str,
        reason: str,
    ) -> None:
        updated = connection.execute(
            "UPDATE discovery_runs SET lifecycle='abandoned', terminalized_at=?, "
            "terminal_reason=? WHERE run_id=? AND lifecycle IN ('issued', 'running')",
            (terminalized_at, reason, run_id),
        )
        if updated.rowcount != 1:
            raise AuthorityConflict("discovery run could not be abandoned")

    @staticmethod
    def _bump_global_revisions(
        connection: sqlite3.Connection,
        *,
        inventory_changed: bool,
        published_changed: bool,
    ) -> None:
        if inventory_changed and not published_changed:
            raise AuthorityInvariantError(
                "an inventory change must also change the published revision"
            )
        updated = connection.execute(
            "UPDATE backend_instance SET "
            "inventory_revision=inventory_revision+?, "
            "published_state_revision=published_state_revision+? WHERE singleton=1",
            (int(inventory_changed), int(published_changed)),
        )
        if updated.rowcount != 1:
            raise AuthorityInvariantError(
                "global revisions require exactly one backend instance"
            )

    @staticmethod
    def _require_source_row(
        connection: sqlite3.Connection, source_id: str
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM inventory_sources WHERE inventory_source_id=?",
            (source_id,),
        ).fetchone()
        if row is None:
            raise AuthorityNotFound("inventory source does not exist")
        return row

    @staticmethod
    def _require_run_row(
        connection: sqlite3.Connection, run_id: str
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM discovery_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if row is None:
            raise AuthorityNotFound("discovery run does not exist")
        return row

    @staticmethod
    def _require_exact_run_owner(
        source: sqlite3.Row,
        run: sqlite3.Row,
        source_id: str,
        run_id: str,
    ) -> None:
        if run["inventory_source_id"] != source_id:
            raise AuthorityConflict("discovery run belongs to another source")
        if source["active_discovery_run_id"] != run_id:
            raise AuthorityConflict("discovery run is not the active source owner")


def _new_uuid() -> str:
    value = str(uuid.uuid4())
    if value == str(uuid.UUID(int=0)):
        raise AuthorityInvariantError("backend UUID generator returned NIL")
    return value


def _require_uuid(value: str, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a canonical UUID")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"{field_name} must be a canonical UUID") from exc
    if parsed.int == 0 or str(parsed) != value:
        raise ValueError(
            f"{field_name} must be a canonical lowercase hyphenated non-NIL UUID"
        )
    return value


def _require_positive_integer(value: int, field_name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


def _require_text(value: str, field_name: str, *, max_length: int) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > max_length
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise ValueError(f"{field_name} must be bounded non-empty text")
    return value


def _timestamp(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("authority clock must return a timezone-aware datetime")
    return value.astimezone(UTC).isoformat()
