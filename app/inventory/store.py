"""Fresh SQLite persistence for the Hubinet Ops 0.5 authority namespace."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
import json
from pathlib import Path
import sqlite3
import uuid

from .models import (
    AttestationEvidenceTier,
    AttestationOperation,
    AttestationOutcome,
    AuthorityDatabaseRejected,
    AuthorityInvariantError,
    AuthorityNotFound,
    BackendInstance,
    CandidateAttestationBinding,
    ConfirmedRemovalResult,
    DiscoveryRun,
    DiscoveryRunLifecycle,
    EndpointLifecycle,
    InventorySource,
    InventorySourceState,
    InventoryNode,
    PersistentSourceFreshness,
    PersistentSourceHealth,
    PersistentSourceHealthOrigin,
    ResourceAbsenceAttestation,
    ResourceAbsencePointer,
    ResourceRemovalAuthority,
    ResourceTermination,
    SourceAttestationEvent,
    SourceAttestationRelationshipGate,
    SourceAttestationState,
    SourceAttestationStatus,
    TierTwoEvaluationStatus,
    SourceEndpoint,
    SourceRuntimeHealth,
    ResourceIncarnation,
    ResourceLocatorBinding,
)


AUTHORITY_SCHEMA_MARKER = "hubinet_ops_0_5_authority"
AUTHORITY_SCHEMA_VERSION = 5
BUSY_TIMEOUT_MS = 5_000

_REQUIRED_TABLES = frozenset(
    {
        "authority_schema",
        "backend_instance",
        "inventory_sources",
        "source_endpoints",
        "source_runtime_health",
        "discovery_runs",
        "inventory_nodes",
        "resource_incarnations",
        "resource_locator_bindings",
        "resource_terminations",
        "canonicalization_migrations",
        "source_attestation_state",
        "source_attestation_events",
        "candidate_attestation_bindings",
        "resource_absence_pointers",
        "resource_removal_authorities",
        "resource_absence_attestations",
    }
)
_REQUIRED_SCHEMA_OBJECTS = _REQUIRED_TABLES | frozenset(
    {
        "one_active_endpoint_per_source",
        "backend_instance_identity_immutable",
        "inventory_source_identity_immutable",
        "source_endpoint_identity_immutable",
        "source_endpoint_canonical_pair_migration_only",
        "active_run_must_belong_to_source",
        "discovery_run_issuance_immutable",
        "discovery_run_terminalization_once",
        "discovery_run_release_before_terminalization",
        "discovery_run_identity_per_source",
        "one_active_binding_per_locator",
        "one_active_binding_per_resource",
        "resource_identity_immutable",
        "binding_identity_immutable",
        "source_attestation_state_identity_immutable",
        "source_attestation_epoch_monotonic",
        "source_attestation_state_no_delete",
        "source_attestation_events_immutable",
        "source_attestation_events_no_delete",
        "candidate_attestation_binding_immutable",
        "candidate_attestation_binding_no_delete",
        "candidate_attestation_binding_requires_matching_accepted_candidate_event",
        "candidate_attestation_binding_requires_live_eligible_source",
        "source_attestation_security_context_requires_epoch_bump",
        "resource_absence_pointer_identity_immutable",
        "resource_absence_pointer_requires_successful_witness_run",
        "resource_absence_pointer_requires_successful_witness_run_update",
        "resource_removal_authority_immutable",
        "resource_removal_authority_no_delete",
        "resource_absence_attestation_immutable",
        "resource_absence_attestation_no_delete",
        "resource_absence_attestation_requires_matching_removal_authority",
        "resource_termination_confirmed_removed_requires_matching_evidence",
        "resource_termination_immutable",
        "resource_termination_no_delete",
        "resource_removal_authority_requires_matching_binding_provenance",
        "resource_removal_authority_requires_matching_witness_provenance",
    }
)
_LEGACY_TABLES = frozenset({"plans", "jobs", "container_states", "job_events"})


class InventoryAuthorityStore:
    """Own connections, schema validation, and read models for one 0.5 DB."""

    def __init__(
        self,
        path: Path,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.path = Path(path)
        self._now = now or (lambda: datetime.now(UTC))
        self._closed = False
        self._open_or_initialize()

    def close(self) -> None:
        """Prevent this store handle from opening further connections."""

        self._closed = True

    def backend_instance(self) -> BackendInstance:
        with self._read_connection() as connection:
            rows = connection.execute(
                "SELECT backend_instance_id, created_at, inventory_revision, "
                "published_state_revision, published_at FROM backend_instance"
            ).fetchall()
        if len(rows) != 1:
            raise AuthorityInvariantError(
                "authority database must contain exactly one backend instance"
            )
        return _backend_instance(rows[0])

    def source_state(self, inventory_source_id: str) -> InventorySourceState:
        with self._read_transaction() as connection:
            return self._source_state_from_connection(
                connection, inventory_source_id
            )

    def list_source_states(self) -> tuple[InventorySourceState, ...]:
        with self._read_transaction() as connection:
            source_ids = [
                str(row["inventory_source_id"])
                for row in connection.execute(
                    "SELECT inventory_source_id FROM inventory_sources "
                    "ORDER BY created_at, inventory_source_id"
                ).fetchall()
            ]
            return tuple(
                self._source_state_from_connection(connection, source_id)
                for source_id in source_ids
            )

    def discovery_run(self, run_id: str) -> DiscoveryRun:
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM discovery_runs WHERE run_id=?", (run_id,)
            ).fetchone()
        if row is None:
            raise AuthorityNotFound("discovery run does not exist")
        return _discovery_run(row)

    def list_discovery_runs(
        self, inventory_source_id: str | None = None
    ) -> tuple[DiscoveryRun, ...]:
        with self._read_connection() as connection:
            if inventory_source_id is None:
                rows = connection.execute(
                    "SELECT * FROM discovery_runs "
                    "ORDER BY inventory_source_id, discovery_run_sequence"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM discovery_runs WHERE inventory_source_id=? "
                    "ORDER BY discovery_run_sequence",
                    (inventory_source_id,),
                ).fetchall()
        return tuple(_discovery_run(row) for row in rows)

    def list_nodes(
        self, inventory_source_id: str | None = None
    ) -> tuple[InventoryNode, ...]:
        with self._read_connection() as connection:
            if inventory_source_id is None:
                rows = connection.execute(
                    "SELECT * FROM inventory_nodes ORDER BY inventory_source_id, external_node_name"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM inventory_nodes WHERE inventory_source_id=? "
                    "ORDER BY external_node_name",
                    (inventory_source_id,),
                ).fetchall()
        return tuple(_inventory_node(row) for row in rows)

    def list_resources(
        self, inventory_source_id: str | None = None
    ) -> tuple[ResourceIncarnation, ...]:
        with self._read_connection() as connection:
            query = (
                "SELECT r.*, b.binding_id AS active_binding_id, "
                "COALESCE(b.locator_generation, (SELECT MAX(history.locator_generation) "
                "FROM resource_locator_bindings history WHERE history.resource_id=r.resource_id)) "
                "AS locator_generation FROM resource_incarnations r "
                "LEFT JOIN resource_locator_bindings b ON b.resource_id=r.resource_id "
                "AND b.valid_to_run_sequence IS NULL"
            )
            params: tuple[object, ...] = ()
            if inventory_source_id is not None:
                query += " WHERE r.inventory_source_id=?"
                params = (inventory_source_id,)
            query += " ORDER BY r.inventory_source_id, r.vmid, locator_generation"
            rows = connection.execute(query, params).fetchall()
        return tuple(_resource_incarnation(row) for row in rows)

    def list_bindings(
        self, inventory_source_id: str | None = None
    ) -> tuple[ResourceLocatorBinding, ...]:
        with self._read_connection() as connection:
            if inventory_source_id is None:
                rows = connection.execute(
                    "SELECT * FROM resource_locator_bindings "
                    "ORDER BY inventory_source_id, vmid, locator_generation"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM resource_locator_bindings WHERE inventory_source_id=? "
                    "ORDER BY vmid, locator_generation",
                    (inventory_source_id,),
                ).fetchall()
        return tuple(_resource_locator_binding(row) for row in rows)

    def attestation_state(self, inventory_source_id: str) -> SourceAttestationState:
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM source_attestation_state WHERE inventory_source_id=?",
                (inventory_source_id,),
            ).fetchone()
        if row is None:
            raise AuthorityNotFound("inventory source does not exist")
        return _source_attestation_state(row)

    def attestation_event(self, event_id: str) -> SourceAttestationEvent:
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM source_attestation_events WHERE event_id=?",
                (event_id,),
            ).fetchone()
        if row is None:
            raise AuthorityNotFound("source attestation event does not exist")
        return _source_attestation_event(row)

    def list_attestation_events(
        self, inventory_source_id: str | None = None
    ) -> tuple[SourceAttestationEvent, ...]:
        with self._read_connection() as connection:
            if inventory_source_id is None:
                rows = connection.execute(
                    "SELECT * FROM source_attestation_events "
                    "ORDER BY inventory_source_id, attempted_at, event_id"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM source_attestation_events WHERE inventory_source_id=? "
                    "ORDER BY attempted_at, event_id",
                    (inventory_source_id,),
                ).fetchall()
        return tuple(_source_attestation_event(row) for row in rows)

    def list_candidate_attestation_bindings(
        self,
        inventory_source_id: str | None = None,
        *,
        endpoint_id: str | None = None,
    ) -> tuple[CandidateAttestationBinding, ...]:
        with self._read_connection() as connection:
            clauses: list[str] = []
            params: list[object] = []
            if inventory_source_id is not None:
                clauses.append("inventory_source_id=?")
                params.append(inventory_source_id)
            if endpoint_id is not None:
                clauses.append("endpoint_id=?")
                params.append(endpoint_id)
            query = "SELECT * FROM candidate_attestation_bindings"
            if clauses:
                query += " WHERE " + " AND ".join(clauses)
            query += " ORDER BY inventory_source_id, endpoint_id, matched_at, binding_id"
            rows = connection.execute(query, tuple(params)).fetchall()
        return tuple(_candidate_attestation_binding(row) for row in rows)

    def resource_absence_pointer(self, resource_id: str) -> ResourceAbsencePointer | None:
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM resource_absence_pointers WHERE resource_id=?",
                (resource_id,),
            ).fetchone()
        return _resource_absence_pointer(row) if row is not None else None

    def list_resource_absence_pointers(
        self, inventory_source_id: str | None = None
    ) -> tuple[ResourceAbsencePointer, ...]:
        with self._read_connection() as connection:
            if inventory_source_id is None:
                rows = connection.execute(
                    "SELECT * FROM resource_absence_pointers ORDER BY inventory_source_id, resource_id"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM resource_absence_pointers WHERE inventory_source_id=? "
                    "ORDER BY resource_id",
                    (inventory_source_id,),
                ).fetchall()
        return tuple(_resource_absence_pointer(row) for row in rows)

    def resource_removal_authority(self, evidence_id: str) -> ResourceRemovalAuthority:
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM resource_removal_authorities WHERE evidence_id=?",
                (evidence_id,),
            ).fetchone()
        if row is None:
            raise AuthorityNotFound("Class-C removal authority evidence does not exist")
        return _resource_removal_authority(row)

    def resource_absence_attestation(self, evidence_id: str) -> ResourceAbsenceAttestation:
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM resource_absence_attestations WHERE evidence_id=?",
                (evidence_id,),
            ).fetchone()
        if row is None:
            raise AuthorityNotFound("operator absence-attestation evidence does not exist")
        return _resource_absence_attestation(row)

    def resource_termination(self, resource_id: str) -> ResourceTermination | None:
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM resource_terminations WHERE resource_id=?",
                (resource_id,),
            ).fetchone()
        return _resource_termination(row) if row is not None else None

    def confirmed_removal_result(self, decision_id: str) -> ConfirmedRemovalResult:
        with self._read_connection() as connection:
            authority_row = connection.execute(
                "SELECT * FROM resource_removal_authorities WHERE decision_id=?",
                (decision_id,),
            ).fetchone()
            attestation_row = connection.execute(
                "SELECT * FROM resource_absence_attestations WHERE decision_id=?",
                (decision_id,),
            ).fetchone()
        if authority_row is None or attestation_row is None:
            raise AuthorityNotFound("Class-C decision does not exist")
        termination = self.resource_termination(str(authority_row["resource_id"]))
        if termination is None:
            raise AuthorityInvariantError(
                "accepted Class-C decision must have a linked termination record"
            )
        return ConfirmedRemovalResult(
            decision_id=decision_id,
            resource_id=str(authority_row["resource_id"]),
            removal_authority=_resource_removal_authority(authority_row),
            absence_attestation=_resource_absence_attestation(attestation_row),
            termination=termination,
        )

    def record_counts(self) -> dict[str, int]:
        """Return bounded schema diagnostics without exposing SQL execution."""

        with self._read_transaction() as connection:
            return {
                table: int(
                    connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                )
                for table in sorted(_REQUIRED_TABLES)
            }

    def foreign_keys_enabled(self) -> bool:
        with self._read_connection() as connection:
            return bool(connection.execute("PRAGMA foreign_keys").fetchone()[0])

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def _read_connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _read_transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _source_state_from_connection(
        connection: sqlite3.Connection, inventory_source_id: str
    ) -> InventorySourceState:
        source_row = connection.execute(
            "SELECT * FROM inventory_sources WHERE inventory_source_id=?",
            (inventory_source_id,),
        ).fetchone()
        if source_row is None:
            raise AuthorityNotFound("inventory source does not exist")
        endpoint_rows = connection.execute(
            "SELECT * FROM source_endpoints "
            "WHERE inventory_source_id=? AND lifecycle='active'",
            (inventory_source_id,),
        ).fetchall()
        health_row = connection.execute(
            "SELECT * FROM source_runtime_health WHERE inventory_source_id=?",
            (inventory_source_id,),
        ).fetchone()
        if len(endpoint_rows) != 1 or health_row is None:
            raise AuthorityInvariantError(
                "inventory source must have exactly one active endpoint and runtime health record"
            )
        return InventorySourceState(
            source=_inventory_source(source_row),
            active_endpoint=_source_endpoint(endpoint_rows[0]),
            runtime_health=_source_runtime_health(health_row),
        )

    def _connect(self) -> sqlite3.Connection:
        if self._closed:
            raise AuthorityInvariantError("authority store is closed")
        connection = sqlite3.connect(
            self.path,
            timeout=BUSY_TIMEOUT_MS / 1_000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _open_or_initialize(self) -> None:
        fresh = not self.path.exists() or self.path.stat().st_size == 0
        if fresh:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._initialize_fresh_database()
        else:
            self._reject_unrecognized_existing_database()

        with self._connect() as connection:
            journal_mode = str(
                connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            ).lower()
            if journal_mode != "wal":
                raise AuthorityInvariantError("authority database requires WAL mode")
            self._validate_schema(connection)

    def _reject_unrecognized_existing_database(self) -> None:
        uri = f"{self.path.resolve().as_uri()}?mode=ro"
        try:
            with sqlite3.connect(uri, uri=True) as connection:
                connection.row_factory = sqlite3.Row
                user_version = int(
                    connection.execute("PRAGMA user_version").fetchone()[0]
                )
                objects = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master "
                        "WHERE type IN ('table', 'index', 'trigger') "
                        "AND name NOT LIKE 'sqlite_%'"
                    ).fetchall()
                }
                if "authority_schema" not in objects:
                    if user_version == 400 or objects & _LEGACY_TABLES:
                        raise AuthorityDatabaseRejected(
                            "legacy Hubinet Ops 0.4 database is not a 0.5 authority database"
                        )
                    raise AuthorityDatabaseRejected(
                        "non-empty database has no recognized 0.5 authority marker"
                    )
                marker_rows = connection.execute(
                    "SELECT marker, schema_version FROM authority_schema"
                ).fetchall()
                backend_rows = connection.execute(
                    "SELECT backend_instance_id FROM backend_instance"
                ).fetchall() if "backend_instance" in objects else []
        except AuthorityDatabaseRejected:
            raise
        except sqlite3.DatabaseError as exc:
            raise AuthorityDatabaseRejected(
                "existing database is not a valid 0.5 authority database"
            ) from exc

        if len(marker_rows) != 1:
            raise AuthorityDatabaseRejected("authority schema marker is invalid")
        marker = marker_rows[0]
        if (
            marker["marker"] != AUTHORITY_SCHEMA_MARKER
            or type(marker["schema_version"]) is not int
            or marker["schema_version"] != AUTHORITY_SCHEMA_VERSION
            or user_version != AUTHORITY_SCHEMA_VERSION
        ):
            raise AuthorityDatabaseRejected(
                "authority schema marker or version is unsupported"
            )
        if objects != _REQUIRED_SCHEMA_OBJECTS:
            raise AuthorityDatabaseRejected(
                "authority database schema objects do not match version "
                f"{AUTHORITY_SCHEMA_VERSION}"
            )
        if len(backend_rows) != 1 or not _is_canonical_uuid(
            backend_rows[0]["backend_instance_id"]
        ):
            raise AuthorityDatabaseRejected(
                "authority database backend identity is invalid"
            )

    def _initialize_fresh_database(self) -> None:
        backend_instance_id = _new_uuid()
        created_at = _timestamp(self._now())
        connection = sqlite3.connect(
            self.path,
            timeout=BUSY_TIMEOUT_MS / 1_000,
            isolation_level=None,
        )
        try:
            connection.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("BEGIN IMMEDIATE")
            for statement in _SCHEMA_STATEMENTS:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO authority_schema(singleton, marker, schema_version) "
                "VALUES(1, ?, ?)",
                (AUTHORITY_SCHEMA_MARKER, AUTHORITY_SCHEMA_VERSION),
            )
            connection.execute(
                "INSERT INTO backend_instance("
                "singleton, backend_instance_id, created_at, inventory_revision, "
                "published_state_revision, published_at) VALUES(1, ?, ?, 0, 1, ?)",
                (backend_instance_id, created_at, created_at),
            )
            connection.execute(f"PRAGMA user_version={AUTHORITY_SCHEMA_VERSION}")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _validate_schema(self, connection: sqlite3.Connection) -> None:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if not _REQUIRED_TABLES <= tables:
            raise AuthorityDatabaseRejected(
                "authority database is missing required schema tables"
            )
        marker_rows = connection.execute(
            "SELECT marker, schema_version FROM authority_schema"
        ).fetchall()
        if len(marker_rows) != 1:
            raise AuthorityDatabaseRejected("authority schema marker is invalid")
        marker = marker_rows[0]
        if (
            marker["marker"] != AUTHORITY_SCHEMA_MARKER
            or marker["schema_version"] != AUTHORITY_SCHEMA_VERSION
            or connection.execute("PRAGMA user_version").fetchone()[0]
            != AUTHORITY_SCHEMA_VERSION
        ):
            raise AuthorityDatabaseRejected(
                "authority schema marker or version is unsupported"
            )
        if len(connection.execute("SELECT 1 FROM backend_instance").fetchall()) != 1:
            raise AuthorityDatabaseRejected(
                "authority database must contain exactly one backend instance"
            )
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise AuthorityDatabaseRejected("authority database foreign keys are invalid")


def _new_uuid() -> str:
    value = uuid.uuid4()
    if value.int == 0:
        raise AuthorityInvariantError("backend UUID generator returned NIL")
    return str(value)


def _is_canonical_uuid(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        return False
    return parsed.int != 0 and str(parsed) == value


def _timestamp(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("authority clock must return a timezone-aware datetime")
    return value.astimezone(UTC).isoformat()


def _backend_instance(row: sqlite3.Row) -> BackendInstance:
    return BackendInstance(
        backend_instance_id=str(row["backend_instance_id"]),
        created_at=str(row["created_at"]),
        inventory_revision=int(row["inventory_revision"]),
        published_state_revision=int(row["published_state_revision"]),
        published_at=str(row["published_at"]),
    )


def _inventory_source(row: sqlite3.Row) -> InventorySource:
    return InventorySource(
        inventory_source_id=str(row["inventory_source_id"]),
        backend_instance_id=str(row["backend_instance_id"]),
        provider_kind=str(row["provider_kind"]),
        display_name=str(row["display_name"]),
        credential_reference=str(row["credential_reference"]),
        created_at=str(row["created_at"]),
        source_config_revision=int(row["source_config_revision"]),
        last_issued_run_sequence=int(row["last_issued_run_sequence"]),
        last_committed_run_sequence=(
            int(row["last_committed_run_sequence"])
            if row["last_committed_run_sequence"] is not None
            else None
        ),
        active_discovery_run_id=(
            str(row["active_discovery_run_id"])
            if row["active_discovery_run_id"] is not None
            else None
        ),
        provider_contract_version=int(row["provider_contract_version"]),
        freshness_duration_seconds=int(row["freshness_duration_seconds"]),
        facts=json.loads(str(row["facts_json"])),
    )


def _source_endpoint(row: sqlite3.Row) -> SourceEndpoint:
    return SourceEndpoint(
        endpoint_id=str(row["endpoint_id"]),
        inventory_source_id=str(row["inventory_source_id"]),
        canonical_transport_locator=str(row["canonical_transport_locator"]),
        canonicalization_contract_version=int(
            row["canonicalization_contract_version"]
        ),
        lifecycle=EndpointLifecycle(str(row["lifecycle"])),
        transport_trust_revision=int(row["transport_trust_revision"]),
        created_at=str(row["created_at"]),
    )


def _source_runtime_health(row: sqlite3.Row) -> SourceRuntimeHealth:
    optional_ints = {
        name: int(row[name]) if row[name] is not None else None
        for name in (
            "latest_completed_run_sequence",
            "last_health_run_sequence",
            "committed_source_config_revision",
            "committed_canonicalization_contract_version",
            "committed_transport_trust_revision",
            "committed_source_attestation_epoch",
        )
    }
    return SourceRuntimeHealth(
        inventory_source_id=str(row["inventory_source_id"]),
        health=PersistentSourceHealth(str(row["health"])),
        freshness=PersistentSourceFreshness(str(row["freshness"])),
        health_origin=PersistentSourceHealthOrigin(str(row["health_origin"])),
        health_reason=str(row["health_reason"]),
        latest_completed_run_sequence=optional_ints[
            "latest_completed_run_sequence"
        ],
        latest_completed_outcome=row["latest_completed_outcome"],
        last_health_run_sequence=optional_ints["last_health_run_sequence"],
        last_run_health_outcome=row["last_run_health_outcome"],
        last_successful_observed_at=row["last_successful_observed_at"],
        freshness_reference_at=row["freshness_reference_at"],
        freshness_valid_until=row["freshness_valid_until"],
        committed_source_config_revision=optional_ints[
            "committed_source_config_revision"
        ],
        committed_endpoint_id=row["committed_endpoint_id"],
        committed_canonical_transport_locator=row[
            "committed_canonical_transport_locator"
        ],
        committed_canonicalization_contract_version=optional_ints[
            "committed_canonicalization_contract_version"
        ],
        committed_transport_trust_revision=optional_ints[
            "committed_transport_trust_revision"
        ],
        committed_source_attestation_epoch=optional_ints[
            "committed_source_attestation_epoch"
        ],
    )


def _discovery_run(row: sqlite3.Row) -> DiscoveryRun:
    return DiscoveryRun(
        run_id=str(row["run_id"]),
        inventory_source_id=str(row["inventory_source_id"]),
        discovery_run_sequence=int(row["discovery_run_sequence"]),
        issued_at=str(row["issued_at"]),
        expected_source_config_revision=int(row["expected_source_config_revision"]),
        expected_endpoint_id=str(row["expected_endpoint_id"]),
        expected_canonical_transport_locator=str(
            row["expected_canonical_transport_locator"]
        ),
        expected_canonicalization_contract_version=int(
            row["expected_canonicalization_contract_version"]
        ),
        expected_transport_trust_revision=int(
            row["expected_transport_trust_revision"]
        ),
        expected_source_attestation_epoch=int(
            row["expected_source_attestation_epoch"]
        ),
        provider_contract_version=int(row["provider_contract_version"]),
        lifecycle=DiscoveryRunLifecycle(str(row["lifecycle"])),
        terminalized_at=row["terminalized_at"],
        terminal_reason=row["terminal_reason"],
        completed_at=row["completed_at"],
        provider_outcome=row["provider_outcome"],
        observed_at=row["observed_at"],
        normalized_snapshot_hash=row["normalized_snapshot_hash"],
        baseline_completeness=row["baseline_completeness"],
        source_availability=row["source_availability"],
        baseline_mode=row["baseline_mode"],
        permission_coverage_complete=_optional_sqlite_boolean(
            row["permission_coverage_complete"], "permission_coverage_complete"
        ),
        boundary_consistent=_optional_sqlite_boolean(
            row["boundary_consistent"], "boundary_consistent"
        ),
        covered_nodes=_optional_completion_collection(row, "covered_nodes_json"),
        failed_baseline_scopes=_optional_completion_collection(
            row, "failed_baseline_scopes_json"
        ),
        acl_topology_hash_before=row["acl_topology_hash_before"],
        acl_topology_hash_after=row["acl_topology_hash_after"],
        permission_snapshot_hash_before=row["permission_snapshot_hash_before"],
        permission_snapshot_hash_after=row["permission_snapshot_hash_after"],
        detail_ok_count=(int(row["detail_ok_count"]) if row["detail_ok_count"] is not None else None),
        detail_temporarily_unavailable_count=(int(row["detail_temporarily_unavailable_count"]) if row["detail_temporarily_unavailable_count"] is not None else None),
        detail_error_count=(int(row["detail_error_count"]) if row["detail_error_count"] is not None else None),
        failed_detail_scopes=_optional_completion_collection(
            row, "failed_detail_scopes_json"
        ),
        completion_source_config_revision=(
            int(row["completion_source_config_revision"])
            if row["completion_source_config_revision"] is not None
            else None
        ),
        completion_endpoint_id=row["completion_endpoint_id"],
        completion_canonical_transport_locator=row[
            "completion_canonical_transport_locator"
        ],
        completion_canonicalization_contract_version=(
            int(row["completion_canonicalization_contract_version"])
            if row["completion_canonicalization_contract_version"] is not None
            else None
        ),
        completion_transport_trust_revision=(
            int(row["completion_transport_trust_revision"])
            if row["completion_transport_trust_revision"] is not None
            else None
        ),
        completion_source_attestation_epoch=(
            int(row["completion_source_attestation_epoch"])
            if row["completion_source_attestation_epoch"] is not None
            else None
        ),
    )


def _optional_sqlite_boolean(value: object, field: str) -> bool | None:
    if value is None:
        return None
    if type(value) is not int or value not in {0, 1}:
        raise AuthorityInvariantError(f"discovery run {field} is malformed")
    return bool(value)


def _optional_completion_collection(
    row: sqlite3.Row, field: str
) -> tuple[str, ...] | None:
    raw = row[field]
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise AuthorityInvariantError(f"discovery run {field} is malformed")
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise AuthorityInvariantError(
            f"discovery run {field} is malformed"
        ) from exc
    if (
        not isinstance(value, list)
        or any(
            not isinstance(item, str)
            or not item
            or item != item.strip()
            for item in value
        )
        or len(set(value)) != len(value)
        or json.dumps(value, ensure_ascii=True, separators=(",", ":")) != raw
    ):
        raise AuthorityInvariantError(f"discovery run {field} is malformed")
    return tuple(value)


def _inventory_node(row: sqlite3.Row) -> InventoryNode:
    return InventoryNode(
        node_id=str(row["node_id"]),
        inventory_source_id=str(row["inventory_source_id"]),
        external_node_name=str(row["external_node_name"]),
        status=str(row["status"]),
        available=bool(row["available"]),
        facts=json.loads(str(row["facts_json"])),
    )


def _resource_incarnation(row: sqlite3.Row) -> ResourceIncarnation:
    generation = row["locator_generation"]
    if generation is None:
        raise AuthorityInvariantError("resource incarnation has no locator history")
    return ResourceIncarnation(
        resource_id=str(row["resource_id"]),
        inventory_source_id=str(row["inventory_source_id"]),
        resource_type=str(row["resource_type"]),
        vmid=int(row["vmid"]),
        resource_continuity_revision=int(row["resource_continuity_revision"]),
        name=str(row["name"]),
        status=str(row["status"]),
        current_node_id=row["current_node_id"],
        last_known_node_id=row["last_known_node_id"],
        presence=str(row["presence"]),
        lifecycle=str(row["lifecycle"]),
        observational_continuity=str(row["observational_continuity"]),
        security_continuity=str(row["security_continuity"]),
        detail_status=str(row["detail_status"]),
        node_availability=str(row["node_availability"]),
        state_level=str(row["state_level"]),
        active_binding_id=row["active_binding_id"],
        locator_generation=int(generation),
        facts=json.loads(str(row["facts_json"])),
        termination_reason=row["termination_reason"],
        successor_resource_id=row["successor_resource_id"],
    )


def _resource_locator_binding(row: sqlite3.Row) -> ResourceLocatorBinding:
    return ResourceLocatorBinding(
        binding_id=str(row["binding_id"]),
        inventory_source_id=str(row["inventory_source_id"]),
        vmid=int(row["vmid"]),
        locator_generation=int(row["locator_generation"]),
        resource_id=str(row["resource_id"]),
        valid_from_run_sequence=int(row["valid_from_run_sequence"]),
        valid_to_run_sequence=(int(row["valid_to_run_sequence"]) if row["valid_to_run_sequence"] is not None else None),
        closure_reason=row["closure_reason"],
    )


def _resource_absence_pointer(row: sqlite3.Row) -> ResourceAbsencePointer:
    return ResourceAbsencePointer(
        resource_id=str(row["resource_id"]),
        inventory_source_id=str(row["inventory_source_id"]),
        witness_run_id=str(row["witness_run_id"]),
        witness_discovery_run_sequence=int(row["witness_discovery_run_sequence"]),
        updated_at=str(row["updated_at"]),
    )


def _resource_removal_authority(row: sqlite3.Row) -> ResourceRemovalAuthority:
    return ResourceRemovalAuthority(
        evidence_id=str(row["evidence_id"]),
        decision_id=str(row["decision_id"]),
        inventory_source_id=str(row["inventory_source_id"]),
        resource_id=str(row["resource_id"]),
        binding_id=str(row["binding_id"]),
        vmid=int(row["vmid"]),
        locator_generation=int(row["locator_generation"]),
        resource_continuity_revision=int(row["resource_continuity_revision"]),
        witness_run_id=str(row["witness_run_id"]),
        witness_discovery_run_sequence=int(row["witness_discovery_run_sequence"]),
        source_config_revision=int(row["source_config_revision"]),
        endpoint_id=str(row["endpoint_id"]),
        canonical_transport_locator=str(row["canonical_transport_locator"]),
        canonicalization_contract_version=int(row["canonicalization_contract_version"]),
        transport_trust_revision=int(row["transport_trust_revision"]),
        source_attestation_epoch=int(row["source_attestation_epoch"]),
        actor=str(row["actor"]),
        decided_at=str(row["decided_at"]),
        reason=str(row["reason"]),
    )


def _resource_absence_attestation(row: sqlite3.Row) -> ResourceAbsenceAttestation:
    return ResourceAbsenceAttestation(
        evidence_id=str(row["evidence_id"]),
        decision_id=str(row["decision_id"]),
        inventory_source_id=str(row["inventory_source_id"]),
        resource_id=str(row["resource_id"]),
        binding_id=str(row["binding_id"]),
        vmid=int(row["vmid"]),
        locator_generation=int(row["locator_generation"]),
        resource_continuity_revision=int(row["resource_continuity_revision"]),
        witness_run_id=str(row["witness_run_id"]),
        witness_discovery_run_sequence=int(row["witness_discovery_run_sequence"]),
        source_config_revision=int(row["source_config_revision"]),
        endpoint_id=str(row["endpoint_id"]),
        canonical_transport_locator=str(row["canonical_transport_locator"]),
        canonicalization_contract_version=int(row["canonicalization_contract_version"]),
        transport_trust_revision=int(row["transport_trust_revision"]),
        source_attestation_epoch=int(row["source_attestation_epoch"]),
        actor=str(row["actor"]),
        decided_at=str(row["decided_at"]),
        reason=str(row["reason"]),
    )


def _resource_termination(row: sqlite3.Row) -> ResourceTermination:
    return ResourceTermination(
        resource_id=str(row["resource_id"]),
        inventory_source_id=str(row["inventory_source_id"]),
        binding_id=str(row["binding_id"]),
        locator_generation=int(row["locator_generation"]),
        reason=str(row["reason"]),
        successor_resource_id=(
            str(row["successor_resource_id"])
            if row["successor_resource_id"] is not None
            else None
        ),
        run_sequence=int(row["run_sequence"]),
        class_c_decision_id=(
            str(row["class_c_decision_id"])
            if row["class_c_decision_id"] is not None
            else None
        ),
        created_at=str(row["created_at"]),
    )


def _source_attestation_state(row: sqlite3.Row) -> SourceAttestationState:
    evidence_tier = row["evidence_tier"]
    tier2_evaluation = row["tier2_evaluation"]
    return SourceAttestationState(
        inventory_source_id=str(row["inventory_source_id"]),
        attestation_status=SourceAttestationStatus(str(row["attestation_status"])),
        source_attestation_epoch=int(row["source_attestation_epoch"]),
        anchor_kind=row["anchor_kind"],
        anchor_value=row["anchor_value"],
        evidence_tier=(
            AttestationEvidenceTier(str(evidence_tier))
            if evidence_tier is not None
            else None
        ),
        tier2_evaluation=(
            TierTwoEvaluationStatus(str(tier2_evaluation))
            if tier2_evaluation is not None
            else None
        ),
        relationship_gate=SourceAttestationRelationshipGate(
            str(row["relationship_gate"])
        ),
        accepted_at=row["accepted_at"],
        accepted_by=row["accepted_by"],
        evaluated_endpoint_id=row["evaluated_endpoint_id"],
    )


def _source_attestation_event(row: sqlite3.Row) -> SourceAttestationEvent:
    evidence_tier = row["evidence_tier"]
    return SourceAttestationEvent(
        event_id=str(row["event_id"]),
        inventory_source_id=str(row["inventory_source_id"]),
        target_endpoint_id=str(row["target_endpoint_id"]),
        operation=AttestationOperation(str(row["operation"])),
        actor=str(row["actor"]),
        attempted_at=str(row["attempted_at"]),
        expected_source_config_revision=int(row["expected_source_config_revision"]),
        expected_endpoint_id=str(row["expected_endpoint_id"]),
        expected_canonical_transport_locator=str(
            row["expected_canonical_transport_locator"]
        ),
        expected_canonicalization_contract_version=int(
            row["expected_canonicalization_contract_version"]
        ),
        expected_transport_trust_revision=int(
            row["expected_transport_trust_revision"]
        ),
        expected_source_attestation_epoch=int(
            row["expected_source_attestation_epoch"]
        ),
        expected_relationship_gate=SourceAttestationRelationshipGate(
            str(row["expected_relationship_gate"])
        ),
        outcome=AttestationOutcome(str(row["outcome"])),
        evidence_tier=(
            AttestationEvidenceTier(str(evidence_tier))
            if evidence_tier is not None
            else None
        ),
        tier2_evaluation=(
            TierTwoEvaluationStatus(str(row["tier2_evaluation"]))
            if row["tier2_evaluation"] is not None
            else None
        ),
        asserted_anchor_kind=row["asserted_anchor_kind"],
        asserted_anchor_value=row["asserted_anchor_value"],
        endpoint_lifecycle_at_check=row["endpoint_lifecycle_at_check"],
        previous_epoch=int(row["previous_epoch"]),
        resulting_epoch=(
            int(row["resulting_epoch"]) if row["resulting_epoch"] is not None else None
        ),
        resulting_relationship_gate=(
            SourceAttestationRelationshipGate(str(row["resulting_relationship_gate"]))
            if row["resulting_relationship_gate"] is not None
            else None
        ),
        reason=str(row["reason"]),
    )


def _candidate_attestation_binding(row: sqlite3.Row) -> CandidateAttestationBinding:
    return CandidateAttestationBinding(
        binding_id=str(row["binding_id"]),
        inventory_source_id=str(row["inventory_source_id"]),
        endpoint_id=str(row["endpoint_id"]),
        source_attestation_epoch=int(row["source_attestation_epoch"]),
        evidence_tier=AttestationEvidenceTier(str(row["evidence_tier"])),
        tier2_evaluation=TierTwoEvaluationStatus(str(row["tier2_evaluation"])),
        endpoint_lifecycle_at_check=str(row["endpoint_lifecycle_at_check"]),
        canonical_transport_locator=str(row["canonical_transport_locator"]),
        canonicalization_contract_version=int(
            row["canonicalization_contract_version"]
        ),
        transport_trust_revision=int(row["transport_trust_revision"]),
        matched_at=str(row["matched_at"]),
        created_by=str(row["created_by"]),
        event_id=str(row["event_id"]),
    )


_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE authority_schema (
        singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
        marker TEXT NOT NULL CHECK(marker = 'hubinet_ops_0_5_authority'),
        schema_version INTEGER NOT NULL CHECK(schema_version = 5)
    )
    """,
    """
    CREATE TABLE backend_instance (
        singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
        backend_instance_id TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL,
        inventory_revision INTEGER NOT NULL
            CHECK(typeof(inventory_revision) = 'integer' AND inventory_revision >= 0),
        published_state_revision INTEGER NOT NULL
            CHECK(typeof(published_state_revision) = 'integer' AND published_state_revision > 0),
        published_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE inventory_sources (
        inventory_source_id TEXT PRIMARY KEY,
        backend_instance_id TEXT NOT NULL,
        provider_kind TEXT NOT NULL CHECK(length(trim(provider_kind)) > 0),
        display_name TEXT NOT NULL CHECK(length(trim(display_name)) > 0),
        credential_reference TEXT NOT NULL CHECK(length(trim(credential_reference)) > 0),
        created_at TEXT NOT NULL,
        source_config_revision INTEGER NOT NULL DEFAULT 1
            CHECK(typeof(source_config_revision) = 'integer' AND source_config_revision > 0),
        last_issued_run_sequence INTEGER NOT NULL DEFAULT 0
            CHECK(typeof(last_issued_run_sequence) = 'integer' AND last_issued_run_sequence >= 0),
        last_committed_run_sequence INTEGER
            CHECK(last_committed_run_sequence IS NULL OR
                  (typeof(last_committed_run_sequence) = 'integer' AND last_committed_run_sequence > 0)),
        active_discovery_run_id TEXT,
        provider_contract_version INTEGER NOT NULL DEFAULT 1
            CHECK(typeof(provider_contract_version) = 'integer' AND provider_contract_version > 0),
        freshness_duration_seconds INTEGER NOT NULL DEFAULT 300
            CHECK(typeof(freshness_duration_seconds) = 'integer' AND freshness_duration_seconds > 0),
        facts_json TEXT NOT NULL DEFAULT '{}',
        FOREIGN KEY(backend_instance_id) REFERENCES backend_instance(backend_instance_id),
        FOREIGN KEY(active_discovery_run_id) REFERENCES discovery_runs(run_id)
            DEFERRABLE INITIALLY DEFERRED
    )
    """,
    """
    CREATE TABLE source_endpoints (
        endpoint_id TEXT PRIMARY KEY,
        inventory_source_id TEXT NOT NULL,
        canonical_transport_locator TEXT NOT NULL,
        canonicalization_contract_version INTEGER NOT NULL
            CHECK(typeof(canonicalization_contract_version) = 'integer' AND
                  canonicalization_contract_version > 0),
        lifecycle TEXT NOT NULL
            CHECK(lifecycle IN ('active', 'candidate', 'inactive', 'retired')),
        transport_trust_revision INTEGER NOT NULL
            CHECK(typeof(transport_trust_revision) = 'integer' AND transport_trust_revision > 0),
        created_at TEXT NOT NULL,
        FOREIGN KEY(inventory_source_id) REFERENCES inventory_sources(inventory_source_id),
        UNIQUE(inventory_source_id, canonical_transport_locator),
        UNIQUE(inventory_source_id, endpoint_id)
    )
    """,
    """
    CREATE UNIQUE INDEX one_active_endpoint_per_source
    ON source_endpoints(inventory_source_id) WHERE lifecycle = 'active'
    """,
    """
    CREATE TABLE source_runtime_health (
        inventory_source_id TEXT PRIMARY KEY,
        health TEXT NOT NULL CHECK(health IN (
            'healthy', 'source_unavailable', 'degraded',
            'configuration_error', 'not_yet_observed')),
        freshness TEXT NOT NULL CHECK(freshness IN ('fresh', 'stale', 'not_yet_observed')),
        health_origin TEXT NOT NULL CHECK(health_origin IN (
            'discovery_run', 'controlled_context_transition', 'time_expiry', 'initial')),
        health_reason TEXT NOT NULL,
        latest_completed_run_sequence INTEGER,
        latest_completed_outcome TEXT,
        last_health_run_sequence INTEGER,
        last_run_health_outcome TEXT,
        last_successful_observed_at TEXT,
        freshness_reference_at TEXT,
        freshness_valid_until TEXT,
        committed_source_config_revision INTEGER,
        committed_endpoint_id TEXT,
        committed_canonical_transport_locator TEXT,
        committed_canonicalization_contract_version INTEGER,
        committed_transport_trust_revision INTEGER,
        committed_source_attestation_epoch INTEGER CHECK(
            committed_source_attestation_epoch IS NULL OR
            (typeof(committed_source_attestation_epoch) = 'integer' AND
             committed_source_attestation_epoch >= 0)),
        FOREIGN KEY(inventory_source_id) REFERENCES inventory_sources(inventory_source_id),
        FOREIGN KEY(inventory_source_id, committed_endpoint_id)
            REFERENCES source_endpoints(inventory_source_id, endpoint_id),
        CHECK((latest_completed_run_sequence IS NULL) = (latest_completed_outcome IS NULL)),
        CHECK((last_health_run_sequence IS NULL) = (last_run_health_outcome IS NULL)),
        CHECK(health_origin != 'initial' OR (
            health = 'not_yet_observed' AND freshness = 'not_yet_observed' AND
            latest_completed_run_sequence IS NULL AND last_health_run_sequence IS NULL AND
            last_successful_observed_at IS NULL AND freshness_reference_at IS NULL AND
            freshness_valid_until IS NULL AND committed_source_config_revision IS NULL AND
            committed_endpoint_id IS NULL AND committed_canonical_transport_locator IS NULL AND
            committed_canonicalization_contract_version IS NULL AND
            committed_transport_trust_revision IS NULL AND
            committed_source_attestation_epoch IS NULL
        )),
        CHECK((committed_source_config_revision IS NULL AND committed_endpoint_id IS NULL AND
               committed_canonical_transport_locator IS NULL AND
               committed_canonicalization_contract_version IS NULL AND
               committed_transport_trust_revision IS NULL AND
               committed_source_attestation_epoch IS NULL) OR
              (committed_source_config_revision > 0 AND committed_endpoint_id IS NOT NULL AND
               committed_canonical_transport_locator IS NOT NULL AND
               committed_canonicalization_contract_version > 0 AND
               committed_transport_trust_revision > 0 AND
               committed_source_attestation_epoch >= 0))
    )
    """,
    """
    CREATE TABLE discovery_runs (
        run_id TEXT PRIMARY KEY,
        inventory_source_id TEXT NOT NULL,
        discovery_run_sequence INTEGER NOT NULL
            CHECK(typeof(discovery_run_sequence) = 'integer' AND discovery_run_sequence > 0),
        issued_at TEXT NOT NULL,
        expected_source_config_revision INTEGER NOT NULL
            CHECK(typeof(expected_source_config_revision) = 'integer' AND
                  expected_source_config_revision > 0),
        expected_endpoint_id TEXT NOT NULL,
        expected_canonical_transport_locator TEXT NOT NULL,
        expected_canonicalization_contract_version INTEGER NOT NULL
            CHECK(typeof(expected_canonicalization_contract_version) = 'integer' AND
                  expected_canonicalization_contract_version > 0),
        expected_transport_trust_revision INTEGER NOT NULL
            CHECK(typeof(expected_transport_trust_revision) = 'integer' AND
                  expected_transport_trust_revision > 0),
        expected_source_attestation_epoch INTEGER NOT NULL DEFAULT 0
            CHECK(typeof(expected_source_attestation_epoch) = 'integer' AND
                  expected_source_attestation_epoch >= 0),
        provider_contract_version INTEGER NOT NULL
            CHECK(typeof(provider_contract_version) = 'integer' AND provider_contract_version > 0),
        lifecycle TEXT NOT NULL CHECK(lifecycle IN ('issued', 'running', 'completed', 'abandoned')),
        terminalized_at TEXT,
        terminal_reason TEXT,
        completed_at TEXT,
        provider_outcome TEXT CHECK(provider_outcome IS NULL OR provider_outcome IN (
            'success', 'partial', 'configuration_error', 'source_unavailable', 'invalid')),
        observed_at TEXT,
        normalized_snapshot_hash TEXT,
        baseline_completeness TEXT CHECK(baseline_completeness IS NULL OR baseline_completeness IN (
            'complete', 'partial', 'configuration_error', 'source_unavailable', 'invalid')),
        source_availability TEXT CHECK(source_availability IS NULL OR source_availability IN (
            'available', 'unavailable')),
        baseline_mode TEXT CHECK(baseline_mode IS NULL OR baseline_mode IN ('cluster', 'standalone')),
        permission_coverage_complete INTEGER CHECK(
            permission_coverage_complete IS NULL OR permission_coverage_complete IN (0, 1)),
        boundary_consistent INTEGER CHECK(
            boundary_consistent IS NULL OR boundary_consistent IN (0, 1)),
        covered_nodes_json TEXT CHECK(
            covered_nodes_json IS NULL OR
            (json_valid(covered_nodes_json) AND json_type(covered_nodes_json) = 'array')),
        failed_baseline_scopes_json TEXT CHECK(
            failed_baseline_scopes_json IS NULL OR
            (json_valid(failed_baseline_scopes_json) AND
             json_type(failed_baseline_scopes_json) = 'array')),
        acl_topology_hash_before TEXT,
        acl_topology_hash_after TEXT,
        permission_snapshot_hash_before TEXT,
        permission_snapshot_hash_after TEXT,
        detail_ok_count INTEGER CHECK(
            detail_ok_count IS NULL OR
            (typeof(detail_ok_count) = 'integer' AND detail_ok_count >= 0)),
        detail_temporarily_unavailable_count INTEGER CHECK(
            detail_temporarily_unavailable_count IS NULL OR
            (typeof(detail_temporarily_unavailable_count) = 'integer' AND
             detail_temporarily_unavailable_count >= 0)),
        detail_error_count INTEGER CHECK(
            detail_error_count IS NULL OR
            (typeof(detail_error_count) = 'integer' AND detail_error_count >= 0)),
        failed_detail_scopes_json TEXT CHECK(
            failed_detail_scopes_json IS NULL OR
            (json_valid(failed_detail_scopes_json) AND
             json_type(failed_detail_scopes_json) = 'array')),
        completion_source_config_revision INTEGER CHECK(
            completion_source_config_revision IS NULL OR
            (typeof(completion_source_config_revision) = 'integer' AND
             completion_source_config_revision > 0)),
        completion_endpoint_id TEXT,
        completion_canonical_transport_locator TEXT,
        completion_canonicalization_contract_version INTEGER CHECK(
            completion_canonicalization_contract_version IS NULL OR
            (typeof(completion_canonicalization_contract_version) = 'integer' AND
             completion_canonicalization_contract_version > 0)),
        completion_transport_trust_revision INTEGER CHECK(
            completion_transport_trust_revision IS NULL OR
            (typeof(completion_transport_trust_revision) = 'integer' AND
             completion_transport_trust_revision > 0)),
        completion_source_attestation_epoch INTEGER CHECK(
            completion_source_attestation_epoch IS NULL OR
            (typeof(completion_source_attestation_epoch) = 'integer' AND
             completion_source_attestation_epoch >= 0)),
        FOREIGN KEY(inventory_source_id) REFERENCES inventory_sources(inventory_source_id),
        FOREIGN KEY(inventory_source_id, expected_endpoint_id)
            REFERENCES source_endpoints(inventory_source_id, endpoint_id),
        UNIQUE(inventory_source_id, discovery_run_sequence),
        CHECK((lifecycle IN ('issued', 'running') AND terminalized_at IS NULL AND
               terminal_reason IS NULL AND completed_at IS NULL AND provider_outcome IS NULL) OR
              (lifecycle = 'abandoned' AND terminalized_at IS NOT NULL AND
               length(trim(terminal_reason)) > 0 AND completed_at IS NULL AND
               provider_outcome IS NULL AND observed_at IS NULL AND
               normalized_snapshot_hash IS NULL AND baseline_completeness IS NULL) OR
              (lifecycle = 'completed' AND terminalized_at IS NOT NULL AND
               completed_at IS NOT NULL AND length(trim(provider_outcome)) > 0)),
        CHECK(lifecycle = 'completed' OR (
            observed_at IS NULL AND normalized_snapshot_hash IS NULL AND
            baseline_completeness IS NULL AND
            source_availability IS NULL AND baseline_mode IS NULL AND
            permission_coverage_complete IS NULL AND boundary_consistent IS NULL AND
            covered_nodes_json IS NULL AND failed_baseline_scopes_json IS NULL AND
            acl_topology_hash_before IS NULL AND acl_topology_hash_after IS NULL AND
            permission_snapshot_hash_before IS NULL AND permission_snapshot_hash_after IS NULL AND
            detail_ok_count IS NULL AND detail_temporarily_unavailable_count IS NULL AND
            detail_error_count IS NULL AND failed_detail_scopes_json IS NULL AND
            completion_source_config_revision IS NULL AND completion_endpoint_id IS NULL AND
            completion_canonical_transport_locator IS NULL AND
            completion_canonicalization_contract_version IS NULL AND
            completion_transport_trust_revision IS NULL AND
            completion_source_attestation_epoch IS NULL)),
        CHECK((detail_ok_count IS NULL AND detail_temporarily_unavailable_count IS NULL AND
               detail_error_count IS NULL) OR
              (detail_ok_count IS NOT NULL AND
               detail_temporarily_unavailable_count IS NOT NULL AND
               detail_error_count IS NOT NULL)),
        CHECK((completion_source_config_revision IS NULL AND completion_endpoint_id IS NULL AND
               completion_canonical_transport_locator IS NULL AND
               completion_canonicalization_contract_version IS NULL AND
               completion_transport_trust_revision IS NULL AND
               completion_source_attestation_epoch IS NULL) OR
              (completion_source_config_revision IS NOT NULL AND
               length(trim(completion_endpoint_id)) > 0 AND
               length(trim(completion_canonical_transport_locator)) > 0 AND
               completion_canonicalization_contract_version IS NOT NULL AND
               completion_transport_trust_revision IS NOT NULL AND
               completion_source_attestation_epoch IS NOT NULL)),
        CHECK(lifecycle != 'completed' OR baseline_completeness IS NOT NULL),
        CHECK(lifecycle != 'completed' OR provider_outcome = 'success' OR
              length(trim(terminal_reason)) > 0),
        CHECK(provider_outcome != 'success' OR (
            terminal_reason IS NULL AND baseline_completeness = 'complete' AND
            source_availability = 'available' AND baseline_mode IS NOT NULL AND
            permission_coverage_complete = 1 AND boundary_consistent = 1 AND
            covered_nodes_json IS NOT NULL AND covered_nodes_json != '[]' AND
            failed_baseline_scopes_json = '[]' AND
            acl_topology_hash_before IS NOT NULL AND acl_topology_hash_after IS NOT NULL AND
            permission_snapshot_hash_before IS NOT NULL AND
            permission_snapshot_hash_after IS NOT NULL AND
            normalized_snapshot_hash IS NOT NULL AND detail_ok_count IS NOT NULL AND
            failed_detail_scopes_json IS NOT NULL AND
            completion_source_config_revision IS NOT NULL))
    )
    """,
    """
    CREATE UNIQUE INDEX discovery_run_identity_per_source
    ON discovery_runs(inventory_source_id, run_id)
    """,
    """
    CREATE TABLE inventory_nodes (
        node_id TEXT PRIMARY KEY,
        inventory_source_id TEXT NOT NULL,
        external_node_name TEXT NOT NULL,
        status TEXT NOT NULL,
        available INTEGER NOT NULL CHECK(available IN (0, 1)),
        facts_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(inventory_source_id) REFERENCES inventory_sources(inventory_source_id),
        UNIQUE(inventory_source_id, external_node_name),
        UNIQUE(inventory_source_id, node_id)
    )
    """,
    """
    CREATE TABLE resource_incarnations (
        resource_id TEXT PRIMARY KEY,
        inventory_source_id TEXT NOT NULL,
        resource_type TEXT NOT NULL CHECK(resource_type IN ('qemu', 'lxc')),
        vmid INTEGER NOT NULL CHECK(typeof(vmid) = 'integer' AND vmid > 0),
        resource_continuity_revision INTEGER NOT NULL
            CHECK(typeof(resource_continuity_revision) = 'integer' AND resource_continuity_revision > 0),
        name TEXT NOT NULL,
        status TEXT NOT NULL,
        current_node_id TEXT,
        last_known_node_id TEXT,
        presence TEXT NOT NULL CHECK(presence IN ('present', 'missing', 'confirmed_removed', 'not_current')),
        lifecycle TEXT NOT NULL CHECK(lifecycle IN ('active', 'quarantined', 'retired')),
        observational_continuity TEXT NOT NULL CHECK(observational_continuity IN ('consistent', 'uncertain', 'replaced')),
        security_continuity TEXT NOT NULL CHECK(security_continuity IN ('unverified', 'trusted', 'revoked')),
        detail_status TEXT NOT NULL CHECK(detail_status IN ('ok', 'temporarily_unavailable', 'error', 'not_applicable')),
        node_availability TEXT NOT NULL CHECK(node_availability IN ('available', 'unavailable', 'unresolved', 'not_applicable')),
        state_level TEXT NOT NULL DEFAULT 'discovered' CHECK(state_level IN ('discovered', 'observed', 'managed', 'maintenance', 'break_glass')),
        facts_json TEXT NOT NULL,
        termination_reason TEXT,
        successor_resource_id TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(inventory_source_id) REFERENCES inventory_sources(inventory_source_id),
        FOREIGN KEY(inventory_source_id, current_node_id) REFERENCES inventory_nodes(inventory_source_id, node_id),
        FOREIGN KEY(inventory_source_id, last_known_node_id) REFERENCES inventory_nodes(inventory_source_id, node_id),
        FOREIGN KEY(successor_resource_id) REFERENCES resource_incarnations(resource_id)
            DEFERRABLE INITIALLY DEFERRED,
        UNIQUE(inventory_source_id, resource_id),
        CHECK((presence = 'present' AND detail_status != 'not_applicable' AND node_availability != 'not_applicable') OR
              (presence != 'present' AND detail_status = 'not_applicable' AND node_availability = 'not_applicable'))
    )
    """,
    """
    CREATE TABLE resource_locator_bindings (
        binding_id TEXT PRIMARY KEY,
        inventory_source_id TEXT NOT NULL,
        vmid INTEGER NOT NULL CHECK(typeof(vmid) = 'integer' AND vmid > 0),
        locator_generation INTEGER NOT NULL CHECK(typeof(locator_generation) = 'integer' AND locator_generation > 0),
        resource_id TEXT NOT NULL,
        valid_from_run_sequence INTEGER NOT NULL CHECK(valid_from_run_sequence > 0),
        valid_to_run_sequence INTEGER,
        closure_reason TEXT,
        FOREIGN KEY(inventory_source_id, resource_id) REFERENCES resource_incarnations(inventory_source_id, resource_id),
        UNIQUE(inventory_source_id, vmid, locator_generation),
        UNIQUE(inventory_source_id, binding_id),
        CHECK((valid_to_run_sequence IS NULL AND closure_reason IS NULL) OR
              (valid_to_run_sequence IS NOT NULL AND valid_to_run_sequence >= valid_from_run_sequence AND length(trim(closure_reason)) > 0))
    )
    """,
    """
    CREATE UNIQUE INDEX one_active_binding_per_locator
    ON resource_locator_bindings(inventory_source_id, vmid)
    WHERE valid_to_run_sequence IS NULL
    """,
    """
    CREATE UNIQUE INDEX one_active_binding_per_resource
    ON resource_locator_bindings(resource_id)
    WHERE valid_to_run_sequence IS NULL
    """,
    """
    CREATE TABLE resource_terminations (
        resource_id TEXT PRIMARY KEY,
        inventory_source_id TEXT NOT NULL,
        binding_id TEXT NOT NULL,
        locator_generation INTEGER NOT NULL,
        reason TEXT NOT NULL CHECK(reason IN ('replaced', 'confirmed_removed')),
        successor_resource_id TEXT,
        run_sequence INTEGER NOT NULL CHECK(typeof(run_sequence) = 'integer' AND run_sequence > 0),
        class_c_decision_id TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY(resource_id) REFERENCES resource_incarnations(resource_id),
        FOREIGN KEY(binding_id) REFERENCES resource_locator_bindings(binding_id),
        FOREIGN KEY(successor_resource_id) REFERENCES resource_incarnations(resource_id)
            DEFERRABLE INITIALLY DEFERRED,
        FOREIGN KEY(class_c_decision_id) REFERENCES resource_removal_authorities(decision_id),
        CHECK(
            (reason = 'confirmed_removed' AND successor_resource_id IS NULL
             AND class_c_decision_id IS NOT NULL)
            OR
            (reason = 'replaced' AND successor_resource_id IS NOT NULL
             AND class_c_decision_id IS NULL)
        )
    )
    """,
    """
    CREATE TABLE canonicalization_migrations (
        migration_id TEXT NOT NULL,
        inventory_source_id TEXT NOT NULL,
        endpoint_id TEXT NOT NULL,
        old_contract_version INTEGER NOT NULL,
        old_canonical_locator TEXT NOT NULL,
        new_contract_version INTEGER NOT NULL,
        new_canonical_locator TEXT NOT NULL,
        migrated_at TEXT NOT NULL,
        PRIMARY KEY(migration_id, endpoint_id),
        FOREIGN KEY(inventory_source_id, endpoint_id) REFERENCES source_endpoints(inventory_source_id, endpoint_id)
    )
    """,
    """
    CREATE TABLE source_attestation_state (
        inventory_source_id TEXT PRIMARY KEY,
        attestation_status TEXT NOT NULL
            CHECK(attestation_status IN ('not_yet_attested', 'attested')),
        source_attestation_epoch INTEGER NOT NULL DEFAULT 0
            CHECK(typeof(source_attestation_epoch) = 'integer' AND source_attestation_epoch >= 0),
        anchor_kind TEXT CHECK(anchor_kind IS NULL OR anchor_kind = 'pve_root_ca_sha256_fingerprint'),
        anchor_value TEXT,
        evidence_tier TEXT CHECK(evidence_tier IS NULL OR evidence_tier IN ('tier_1', 'tier_2')),
        tier2_evaluation TEXT CHECK(tier2_evaluation IS NULL OR tier2_evaluation IN (
            'not_evaluated', 'failed', 'verified')),
        relationship_gate TEXT NOT NULL DEFAULT 'clear' CHECK(relationship_gate IN (
            'clear', 'mismatch_pending_reattestation')),
        accepted_at TEXT,
        accepted_by TEXT,
        evaluated_endpoint_id TEXT,
        FOREIGN KEY(inventory_source_id) REFERENCES inventory_sources(inventory_source_id),
        FOREIGN KEY(inventory_source_id, evaluated_endpoint_id)
            REFERENCES source_endpoints(inventory_source_id, endpoint_id),
        CHECK(
            (attestation_status = 'not_yet_attested' AND anchor_kind IS NULL AND
             anchor_value IS NULL AND evidence_tier IS NULL AND tier2_evaluation IS NULL AND
             relationship_gate = 'clear' AND
             accepted_at IS NULL AND accepted_by IS NULL AND evaluated_endpoint_id IS NULL)
            OR
            (attestation_status = 'attested' AND source_attestation_epoch >= 1 AND
             anchor_kind IS NOT NULL AND anchor_value IS NOT NULL AND
             evidence_tier IS NOT NULL AND tier2_evaluation IS NOT NULL AND
             accepted_at IS NOT NULL AND
             accepted_by IS NOT NULL AND evaluated_endpoint_id IS NOT NULL)
        ),
        CHECK(tier2_evaluation != 'verified' OR evidence_tier = 'tier_2')
    )
    """,
    """
    CREATE TABLE source_attestation_events (
        event_id TEXT PRIMARY KEY,
        inventory_source_id TEXT NOT NULL,
        target_endpoint_id TEXT NOT NULL,
        operation TEXT NOT NULL CHECK(operation IN (
            'enrollment', 'reattestation', 'revocation', 'candidate_check')),
        actor TEXT NOT NULL CHECK(length(trim(actor)) > 0),
        attempted_at TEXT NOT NULL,
        expected_source_config_revision INTEGER NOT NULL
            CHECK(typeof(expected_source_config_revision) = 'integer' AND
                  expected_source_config_revision > 0),
        expected_endpoint_id TEXT NOT NULL,
        expected_canonical_transport_locator TEXT NOT NULL,
        expected_canonicalization_contract_version INTEGER NOT NULL
            CHECK(typeof(expected_canonicalization_contract_version) = 'integer' AND
                  expected_canonicalization_contract_version > 0),
        expected_transport_trust_revision INTEGER NOT NULL
            CHECK(typeof(expected_transport_trust_revision) = 'integer' AND
                  expected_transport_trust_revision > 0),
        expected_source_attestation_epoch INTEGER NOT NULL
            CHECK(typeof(expected_source_attestation_epoch) = 'integer' AND
                  expected_source_attestation_epoch >= 0),
        expected_relationship_gate TEXT NOT NULL CHECK(expected_relationship_gate IN (
            'clear', 'mismatch_pending_reattestation')),
        outcome TEXT NOT NULL CHECK(outcome IN (
            'match', 'mismatch', 'unavailable', 'malformed', 'stale_cas', 'accepted', 'rejected')),
        evidence_tier TEXT CHECK(evidence_tier IS NULL OR evidence_tier IN ('tier_1', 'tier_2')),
        tier2_evaluation TEXT CHECK(tier2_evaluation IS NULL OR tier2_evaluation IN (
            'not_evaluated', 'failed', 'verified')),
        asserted_anchor_kind TEXT,
        asserted_anchor_value TEXT,
        endpoint_lifecycle_at_check TEXT,
        previous_epoch INTEGER NOT NULL
            CHECK(typeof(previous_epoch) = 'integer' AND previous_epoch >= 0),
        resulting_epoch INTEGER
            CHECK(resulting_epoch IS NULL OR
                  (typeof(resulting_epoch) = 'integer' AND resulting_epoch >= previous_epoch)),
        resulting_relationship_gate TEXT
            CHECK(resulting_relationship_gate IS NULL OR resulting_relationship_gate IN (
                'clear', 'mismatch_pending_reattestation')),
        reason TEXT NOT NULL CHECK(length(trim(reason)) > 0),
        FOREIGN KEY(inventory_source_id) REFERENCES inventory_sources(inventory_source_id),
        FOREIGN KEY(inventory_source_id, target_endpoint_id)
            REFERENCES source_endpoints(inventory_source_id, endpoint_id),
        FOREIGN KEY(inventory_source_id, expected_endpoint_id)
            REFERENCES source_endpoints(inventory_source_id, endpoint_id),
        CHECK(tier2_evaluation != 'verified' OR evidence_tier = 'tier_2')
    )
    """,
    """
    CREATE TABLE candidate_attestation_bindings (
        binding_id TEXT PRIMARY KEY,
        inventory_source_id TEXT NOT NULL,
        endpoint_id TEXT NOT NULL,
        source_attestation_epoch INTEGER NOT NULL
            CHECK(typeof(source_attestation_epoch) = 'integer' AND source_attestation_epoch >= 1),
        evidence_tier TEXT NOT NULL CHECK(evidence_tier IN ('tier_1', 'tier_2')),
        tier2_evaluation TEXT NOT NULL CHECK(tier2_evaluation IN (
            'not_evaluated', 'failed', 'verified')),
        endpoint_lifecycle_at_check TEXT NOT NULL
            CHECK(length(trim(endpoint_lifecycle_at_check)) > 0),
        canonical_transport_locator TEXT NOT NULL,
        canonicalization_contract_version INTEGER NOT NULL
            CHECK(typeof(canonicalization_contract_version) = 'integer' AND
                  canonicalization_contract_version > 0),
        transport_trust_revision INTEGER NOT NULL
            CHECK(typeof(transport_trust_revision) = 'integer' AND transport_trust_revision > 0),
        matched_at TEXT NOT NULL,
        created_by TEXT NOT NULL CHECK(length(trim(created_by)) > 0),
        event_id TEXT NOT NULL,
        FOREIGN KEY(inventory_source_id) REFERENCES inventory_sources(inventory_source_id),
        FOREIGN KEY(inventory_source_id, endpoint_id)
            REFERENCES source_endpoints(inventory_source_id, endpoint_id),
        FOREIGN KEY(event_id) REFERENCES source_attestation_events(event_id),
        CHECK(tier2_evaluation != 'verified' OR evidence_tier = 'tier_2')
    )
    """,
    """
    CREATE TABLE resource_absence_pointers (
        resource_id TEXT PRIMARY KEY,
        inventory_source_id TEXT NOT NULL,
        witness_run_id TEXT NOT NULL,
        witness_discovery_run_sequence INTEGER NOT NULL
            CHECK(typeof(witness_discovery_run_sequence) = 'integer' AND
                  witness_discovery_run_sequence > 0),
        updated_at TEXT NOT NULL,
        FOREIGN KEY(inventory_source_id, resource_id)
            REFERENCES resource_incarnations(inventory_source_id, resource_id),
        FOREIGN KEY(inventory_source_id, witness_run_id)
            REFERENCES discovery_runs(inventory_source_id, run_id)
    )
    """,
    """
    CREATE TABLE resource_removal_authorities (
        evidence_id TEXT PRIMARY KEY,
        decision_id TEXT NOT NULL UNIQUE,
        inventory_source_id TEXT NOT NULL,
        resource_id TEXT NOT NULL,
        binding_id TEXT NOT NULL,
        vmid INTEGER NOT NULL CHECK(typeof(vmid) = 'integer' AND vmid > 0),
        locator_generation INTEGER NOT NULL
            CHECK(typeof(locator_generation) = 'integer' AND locator_generation > 0),
        resource_continuity_revision INTEGER NOT NULL
            CHECK(typeof(resource_continuity_revision) = 'integer' AND
                  resource_continuity_revision > 0),
        witness_run_id TEXT NOT NULL,
        witness_discovery_run_sequence INTEGER NOT NULL
            CHECK(typeof(witness_discovery_run_sequence) = 'integer' AND
                  witness_discovery_run_sequence > 0),
        source_config_revision INTEGER NOT NULL
            CHECK(typeof(source_config_revision) = 'integer' AND source_config_revision > 0),
        endpoint_id TEXT NOT NULL,
        canonical_transport_locator TEXT NOT NULL,
        canonicalization_contract_version INTEGER NOT NULL
            CHECK(typeof(canonicalization_contract_version) = 'integer' AND
                  canonicalization_contract_version > 0),
        transport_trust_revision INTEGER NOT NULL
            CHECK(typeof(transport_trust_revision) = 'integer' AND transport_trust_revision > 0),
        source_attestation_epoch INTEGER NOT NULL
            CHECK(typeof(source_attestation_epoch) = 'integer' AND source_attestation_epoch >= 1),
        actor TEXT NOT NULL CHECK(length(trim(actor)) > 0),
        decided_at TEXT NOT NULL,
        reason TEXT NOT NULL CHECK(length(trim(reason)) > 0),
        FOREIGN KEY(inventory_source_id, resource_id)
            REFERENCES resource_incarnations(inventory_source_id, resource_id),
        FOREIGN KEY(inventory_source_id, binding_id)
            REFERENCES resource_locator_bindings(inventory_source_id, binding_id),
        FOREIGN KEY(inventory_source_id, witness_run_id)
            REFERENCES discovery_runs(inventory_source_id, run_id),
        FOREIGN KEY(inventory_source_id, endpoint_id)
            REFERENCES source_endpoints(inventory_source_id, endpoint_id)
    )
    """,
    """
    CREATE TABLE resource_absence_attestations (
        evidence_id TEXT PRIMARY KEY,
        decision_id TEXT NOT NULL UNIQUE,
        inventory_source_id TEXT NOT NULL,
        resource_id TEXT NOT NULL,
        binding_id TEXT NOT NULL,
        vmid INTEGER NOT NULL CHECK(typeof(vmid) = 'integer' AND vmid > 0),
        locator_generation INTEGER NOT NULL
            CHECK(typeof(locator_generation) = 'integer' AND locator_generation > 0),
        resource_continuity_revision INTEGER NOT NULL
            CHECK(typeof(resource_continuity_revision) = 'integer' AND
                  resource_continuity_revision > 0),
        witness_run_id TEXT NOT NULL,
        witness_discovery_run_sequence INTEGER NOT NULL
            CHECK(typeof(witness_discovery_run_sequence) = 'integer' AND
                  witness_discovery_run_sequence > 0),
        source_config_revision INTEGER NOT NULL
            CHECK(typeof(source_config_revision) = 'integer' AND source_config_revision > 0),
        endpoint_id TEXT NOT NULL,
        canonical_transport_locator TEXT NOT NULL,
        canonicalization_contract_version INTEGER NOT NULL
            CHECK(typeof(canonicalization_contract_version) = 'integer' AND
                  canonicalization_contract_version > 0),
        transport_trust_revision INTEGER NOT NULL
            CHECK(typeof(transport_trust_revision) = 'integer' AND transport_trust_revision > 0),
        source_attestation_epoch INTEGER NOT NULL
            CHECK(typeof(source_attestation_epoch) = 'integer' AND source_attestation_epoch >= 1),
        actor TEXT NOT NULL CHECK(length(trim(actor)) > 0),
        decided_at TEXT NOT NULL,
        reason TEXT NOT NULL CHECK(length(trim(reason)) > 0),
        FOREIGN KEY(decision_id) REFERENCES resource_removal_authorities(decision_id),
        FOREIGN KEY(inventory_source_id, resource_id)
            REFERENCES resource_incarnations(inventory_source_id, resource_id),
        FOREIGN KEY(inventory_source_id, binding_id)
            REFERENCES resource_locator_bindings(inventory_source_id, binding_id),
        FOREIGN KEY(inventory_source_id, witness_run_id)
            REFERENCES discovery_runs(inventory_source_id, run_id),
        FOREIGN KEY(inventory_source_id, endpoint_id)
            REFERENCES source_endpoints(inventory_source_id, endpoint_id)
    )
    """,
    """
    CREATE TRIGGER backend_instance_identity_immutable
    BEFORE UPDATE OF backend_instance_id, created_at ON backend_instance
    BEGIN SELECT RAISE(ABORT, 'backend instance identity is immutable'); END
    """,
    """
    CREATE TRIGGER inventory_source_identity_immutable
    BEFORE UPDATE OF inventory_source_id, backend_instance_id, provider_kind, created_at
    ON inventory_sources
    BEGIN SELECT RAISE(ABORT, 'inventory source identity is immutable'); END
    """,
    """
    CREATE TRIGGER source_endpoint_identity_immutable
    BEFORE UPDATE OF endpoint_id, inventory_source_id, created_at
    ON source_endpoints
    BEGIN SELECT RAISE(ABORT, 'endpoint identity is immutable'); END
    """,
    """
    CREATE TRIGGER source_endpoint_canonical_pair_migration_only
    BEFORE UPDATE OF canonical_transport_locator, canonicalization_contract_version
    ON source_endpoints
    WHEN NOT EXISTS (
        SELECT 1 FROM canonicalization_migrations m
        WHERE m.inventory_source_id=OLD.inventory_source_id
          AND m.endpoint_id=OLD.endpoint_id
          AND m.old_contract_version=OLD.canonicalization_contract_version
          AND m.old_canonical_locator=OLD.canonical_transport_locator
          AND m.new_contract_version=NEW.canonicalization_contract_version
          AND m.new_canonical_locator=NEW.canonical_transport_locator
    )
    BEGIN SELECT RAISE(ABORT, 'endpoint identity canonical pair requires migration provenance'); END
    """,
    """
    CREATE TRIGGER resource_identity_immutable
    BEFORE UPDATE OF resource_id, inventory_source_id, resource_type, vmid, created_at
    ON resource_incarnations
    BEGIN SELECT RAISE(ABORT, 'resource incarnation identity is immutable'); END
    """,
    """
    CREATE TRIGGER binding_identity_immutable
    BEFORE UPDATE OF binding_id, inventory_source_id, vmid, locator_generation,
                     resource_id, valid_from_run_sequence
    ON resource_locator_bindings
    BEGIN SELECT RAISE(ABORT, 'locator binding identity is immutable'); END
    """,
    """
    CREATE TRIGGER active_run_must_belong_to_source
    BEFORE UPDATE OF active_discovery_run_id ON inventory_sources
    WHEN NEW.active_discovery_run_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM discovery_runs
        WHERE run_id = NEW.active_discovery_run_id
          AND inventory_source_id = NEW.inventory_source_id
          AND lifecycle IN ('issued', 'running')
    )
    BEGIN SELECT RAISE(ABORT, 'active discovery run must be a nonterminal run of this source'); END
    """,
    """
    CREATE TRIGGER discovery_run_issuance_immutable
    BEFORE UPDATE OF run_id, inventory_source_id, discovery_run_sequence, issued_at,
                     expected_source_config_revision, expected_endpoint_id,
                     expected_canonical_transport_locator,
                     expected_canonicalization_contract_version,
                     expected_transport_trust_revision, expected_source_attestation_epoch,
                     provider_contract_version
    ON discovery_runs
    BEGIN SELECT RAISE(ABORT, 'discovery run issuance fields are immutable'); END
    """,
    """
    CREATE TRIGGER discovery_run_terminalization_once
    BEFORE UPDATE ON discovery_runs
    WHEN OLD.lifecycle IN ('completed', 'abandoned')
    BEGIN SELECT RAISE(ABORT, 'discovery run is already terminal'); END
    """,
    """
    CREATE TRIGGER discovery_run_release_before_terminalization
    BEFORE UPDATE OF lifecycle ON discovery_runs
    WHEN NEW.lifecycle IN ('completed', 'abandoned') AND EXISTS (
        SELECT 1 FROM inventory_sources WHERE active_discovery_run_id = OLD.run_id
    )
    BEGIN SELECT RAISE(ABORT, 'active ownership must be released in the terminal transaction'); END
    """,
    """
    CREATE TRIGGER source_attestation_state_identity_immutable
    BEFORE UPDATE OF inventory_source_id ON source_attestation_state
    BEGIN SELECT RAISE(ABORT, 'source attestation state identity is immutable'); END
    """,
    """
    CREATE TRIGGER source_attestation_epoch_monotonic
    BEFORE UPDATE OF source_attestation_epoch ON source_attestation_state
    WHEN NEW.source_attestation_epoch < OLD.source_attestation_epoch
    BEGIN SELECT RAISE(ABORT, 'source attestation epoch must never decrease'); END
    """,
    """
    CREATE TRIGGER source_attestation_events_immutable
    BEFORE UPDATE ON source_attestation_events
    BEGIN SELECT RAISE(ABORT, 'source attestation events are immutable audit records'); END
    """,
    """
    CREATE TRIGGER candidate_attestation_binding_immutable
    BEFORE UPDATE ON candidate_attestation_bindings
    BEGIN SELECT RAISE(ABORT, 'candidate attestation bindings are immutable audit records'); END
    """,
    """
    CREATE TRIGGER source_attestation_state_no_delete
    BEFORE DELETE ON source_attestation_state
    BEGIN SELECT RAISE(ABORT, 'source attestation state is mandatory and cannot be deleted'); END
    """,
    """
    CREATE TRIGGER source_attestation_events_no_delete
    BEFORE DELETE ON source_attestation_events
    BEGIN SELECT RAISE(ABORT, 'source attestation events are immutable audit records'); END
    """,
    """
    CREATE TRIGGER candidate_attestation_binding_no_delete
    BEFORE DELETE ON candidate_attestation_bindings
    BEGIN SELECT RAISE(ABORT, 'candidate attestation bindings are immutable audit records'); END
    """,
    """
    CREATE TRIGGER candidate_attestation_binding_requires_matching_accepted_candidate_event
    BEFORE INSERT ON candidate_attestation_bindings
    WHEN NOT EXISTS (
        SELECT 1 FROM source_attestation_events e
        WHERE e.event_id = NEW.event_id
          AND e.inventory_source_id = NEW.inventory_source_id
          AND e.target_endpoint_id = NEW.endpoint_id
          AND e.expected_endpoint_id = NEW.endpoint_id
          AND e.operation = 'candidate_check'
          AND e.outcome = 'accepted'
          AND e.expected_source_attestation_epoch = NEW.source_attestation_epoch
          AND e.previous_epoch = NEW.source_attestation_epoch
          AND e.resulting_epoch IS NULL
          AND e.expected_relationship_gate = 'clear'
          AND e.resulting_relationship_gate IS NULL
          AND e.evidence_tier = NEW.evidence_tier
          AND e.tier2_evaluation = NEW.tier2_evaluation
          AND e.endpoint_lifecycle_at_check = NEW.endpoint_lifecycle_at_check
          AND e.expected_canonical_transport_locator = NEW.canonical_transport_locator
          AND e.expected_canonicalization_contract_version = NEW.canonicalization_contract_version
          AND e.expected_transport_trust_revision = NEW.transport_trust_revision
          AND e.actor = NEW.created_by
          AND e.attempted_at = NEW.matched_at
    )
    BEGIN SELECT RAISE(ABORT,
        'candidate attestation binding must originate from a matching accepted candidate_check event'
    ); END
    """,
    """
    CREATE TRIGGER candidate_attestation_binding_requires_live_eligible_source
    BEFORE INSERT ON candidate_attestation_bindings
    WHEN NOT EXISTS (
        SELECT 1 FROM source_attestation_state s
        JOIN source_attestation_events e ON e.event_id = NEW.event_id
        WHERE s.inventory_source_id = NEW.inventory_source_id
          AND s.attestation_status = 'attested'
          AND s.source_attestation_epoch = NEW.source_attestation_epoch
          AND s.relationship_gate = 'clear'
          AND s.anchor_kind = e.asserted_anchor_kind
          AND s.anchor_value = e.asserted_anchor_value
    )
    BEGIN SELECT RAISE(ABORT,
        'candidate attestation binding requires the source to be currently eligible under the exact binding epoch'
    ); END
    """,
    """
    CREATE TRIGGER source_attestation_security_context_requires_epoch_bump
    BEFORE UPDATE OF attestation_status, anchor_kind, anchor_value ON source_attestation_state
    WHEN NEW.source_attestation_epoch = OLD.source_attestation_epoch
      AND (NEW.attestation_status IS NOT OLD.attestation_status
           OR NEW.anchor_kind IS NOT OLD.anchor_kind
           OR NEW.anchor_value IS NOT OLD.anchor_value)
    BEGIN SELECT RAISE(ABORT,
        'source attestation security-context change requires an epoch bump'
    ); END
    """,
    """
    CREATE TRIGGER resource_absence_pointer_identity_immutable
    BEFORE UPDATE OF resource_id, inventory_source_id ON resource_absence_pointers
    BEGIN SELECT RAISE(ABORT, 'sampled-absence pointer identity is immutable'); END
    """,
    """
    CREATE TRIGGER resource_absence_pointer_requires_successful_witness_run
    BEFORE INSERT ON resource_absence_pointers
    WHEN NOT EXISTS (
        SELECT 1 FROM discovery_runs r
        WHERE r.run_id = NEW.witness_run_id
          AND r.inventory_source_id = NEW.inventory_source_id
          AND r.discovery_run_sequence = NEW.witness_discovery_run_sequence
          AND (
            (r.lifecycle = 'completed' AND r.provider_outcome = 'success'
             AND r.baseline_completeness = 'complete')
            OR
            (r.lifecycle IN ('issued', 'running') AND EXISTS (
                SELECT 1 FROM inventory_sources s
                WHERE s.inventory_source_id = r.inventory_source_id
                  AND s.active_discovery_run_id = r.run_id
            ))
          )
    )
    BEGIN SELECT RAISE(ABORT,
        'sampled-absence pointer must reference a successful complete witness run'
    ); END
    """,
    """
    CREATE TRIGGER resource_absence_pointer_requires_successful_witness_run_update
    BEFORE UPDATE OF witness_run_id, witness_discovery_run_sequence ON resource_absence_pointers
    WHEN NOT EXISTS (
        SELECT 1 FROM discovery_runs r
        WHERE r.run_id = NEW.witness_run_id
          AND r.inventory_source_id = NEW.inventory_source_id
          AND r.discovery_run_sequence = NEW.witness_discovery_run_sequence
          AND (
            (r.lifecycle = 'completed' AND r.provider_outcome = 'success'
             AND r.baseline_completeness = 'complete')
            OR
            (r.lifecycle IN ('issued', 'running') AND EXISTS (
                SELECT 1 FROM inventory_sources s
                WHERE s.inventory_source_id = r.inventory_source_id
                  AND s.active_discovery_run_id = r.run_id
            ))
          )
    )
    BEGIN SELECT RAISE(ABORT,
        'sampled-absence pointer must reference a successful complete witness run'
    ); END
    """,
    """
    CREATE TRIGGER resource_removal_authority_immutable
    BEFORE UPDATE ON resource_removal_authorities
    BEGIN SELECT RAISE(ABORT, 'Class-C removal authority evidence is immutable'); END
    """,
    """
    CREATE TRIGGER resource_removal_authority_no_delete
    BEFORE DELETE ON resource_removal_authorities
    BEGIN SELECT RAISE(ABORT, 'Class-C removal authority evidence cannot be deleted'); END
    """,
    """
    CREATE TRIGGER resource_absence_attestation_immutable
    BEFORE UPDATE ON resource_absence_attestations
    BEGIN SELECT RAISE(ABORT, 'operator absence-attestation evidence is immutable'); END
    """,
    """
    CREATE TRIGGER resource_absence_attestation_no_delete
    BEFORE DELETE ON resource_absence_attestations
    BEGIN SELECT RAISE(ABORT, 'operator absence-attestation evidence cannot be deleted'); END
    """,
    """
    CREATE TRIGGER resource_absence_attestation_requires_matching_removal_authority
    BEFORE INSERT ON resource_absence_attestations
    WHEN NOT EXISTS (
        SELECT 1 FROM resource_removal_authorities a
        WHERE a.decision_id = NEW.decision_id
          AND a.inventory_source_id = NEW.inventory_source_id
          AND a.resource_id = NEW.resource_id
          AND a.binding_id = NEW.binding_id
          AND a.vmid = NEW.vmid
          AND a.locator_generation = NEW.locator_generation
          AND a.resource_continuity_revision = NEW.resource_continuity_revision
          AND a.witness_run_id = NEW.witness_run_id
          AND a.witness_discovery_run_sequence = NEW.witness_discovery_run_sequence
          AND a.source_config_revision = NEW.source_config_revision
          AND a.endpoint_id = NEW.endpoint_id
          AND a.canonical_transport_locator = NEW.canonical_transport_locator
          AND a.canonicalization_contract_version = NEW.canonicalization_contract_version
          AND a.transport_trust_revision = NEW.transport_trust_revision
          AND a.source_attestation_epoch = NEW.source_attestation_epoch
          AND a.actor = NEW.actor
          AND a.decided_at = NEW.decided_at
          AND a.reason = NEW.reason
    )
    BEGIN SELECT RAISE(ABORT,
        'absence attestation must match its removal-authority decision field-for-field'
    ); END
    """,
    """
    CREATE TRIGGER resource_termination_confirmed_removed_requires_matching_evidence
    BEFORE INSERT ON resource_terminations
    WHEN NEW.reason = 'confirmed_removed' AND NOT EXISTS (
        SELECT 1 FROM resource_removal_authorities a
        JOIN resource_absence_attestations b ON b.decision_id = a.decision_id
        WHERE a.decision_id = NEW.class_c_decision_id
          AND a.inventory_source_id = NEW.inventory_source_id
          AND a.resource_id = NEW.resource_id
          AND a.binding_id = NEW.binding_id
          AND a.locator_generation = NEW.locator_generation
          AND a.witness_discovery_run_sequence = NEW.run_sequence
    )
    BEGIN SELECT RAISE(ABORT,
        'confirmed_removed termination must match its Class-C decision evidence'
    ); END
    """,
    """
    CREATE TRIGGER resource_termination_immutable
    BEFORE UPDATE ON resource_terminations
    BEGIN SELECT RAISE(ABORT,
        'resource terminations are immutable retained terminal/tombstone records'
    ); END
    """,
    """
    CREATE TRIGGER resource_termination_no_delete
    BEFORE DELETE ON resource_terminations
    BEGIN SELECT RAISE(ABORT,
        'resource terminations are immutable retained terminal/tombstone records'
    ); END
    """,
    """
    CREATE TRIGGER resource_removal_authority_requires_matching_binding_provenance
    BEFORE INSERT ON resource_removal_authorities
    WHEN NOT EXISTS (
        SELECT 1 FROM resource_locator_bindings b
        WHERE b.binding_id = NEW.binding_id
          AND b.inventory_source_id = NEW.inventory_source_id
          AND b.resource_id = NEW.resource_id
          AND b.vmid = NEW.vmid
          AND b.locator_generation = NEW.locator_generation
    )
    BEGIN SELECT RAISE(ABORT,
        'removal authority binding provenance does not match one real binding tuple'
    ); END
    """,
    """
    CREATE TRIGGER resource_removal_authority_requires_matching_witness_provenance
    BEFORE INSERT ON resource_removal_authorities
    WHEN NOT EXISTS (
        SELECT 1 FROM discovery_runs r
        WHERE r.run_id = NEW.witness_run_id
          AND r.inventory_source_id = NEW.inventory_source_id
          AND r.discovery_run_sequence = NEW.witness_discovery_run_sequence
    )
    BEGIN SELECT RAISE(ABORT,
        'removal authority witness provenance does not match one real discovery run'
    ); END
    """,
)
