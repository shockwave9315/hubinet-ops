"""Fresh SQLite persistence for the Hubinet Ops 0.5 authority namespace."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
import sqlite3
import uuid

from .models import (
    AuthorityDatabaseRejected,
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


AUTHORITY_SCHEMA_MARKER = "hubinet_ops_0_5_authority"
AUTHORITY_SCHEMA_VERSION = 1
BUSY_TIMEOUT_MS = 5_000

_REQUIRED_TABLES = frozenset(
    {
        "authority_schema",
        "backend_instance",
        "inventory_sources",
        "source_endpoints",
        "source_runtime_health",
        "discovery_runs",
    }
)
_REQUIRED_SCHEMA_OBJECTS = _REQUIRED_TABLES | frozenset(
    {
        "one_active_endpoint_per_source",
        "backend_instance_identity_immutable",
        "inventory_source_identity_immutable",
        "source_endpoint_identity_immutable",
        "active_run_must_belong_to_source",
        "discovery_run_issuance_immutable",
        "discovery_run_terminalization_once",
        "discovery_run_release_before_terminalization",
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
                "published_state_revision FROM backend_instance"
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
                "authority database schema objects do not match version 1"
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
                "published_state_revision) VALUES(1, ?, ?, 0, 1)",
                (backend_instance_id, created_at),
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
        provider_contract_version=int(row["provider_contract_version"]),
        lifecycle=DiscoveryRunLifecycle(str(row["lifecycle"])),
        terminalized_at=row["terminalized_at"],
        terminal_reason=row["terminal_reason"],
        completed_at=row["completed_at"],
        provider_outcome=row["provider_outcome"],
        observed_at=row["observed_at"],
        normalized_snapshot_hash=row["normalized_snapshot_hash"],
    )


_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE authority_schema (
        singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
        marker TEXT NOT NULL CHECK(marker = 'hubinet_ops_0_5_authority'),
        schema_version INTEGER NOT NULL CHECK(schema_version = 1)
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
            CHECK(typeof(published_state_revision) = 'integer' AND published_state_revision > 0)
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
            committed_transport_trust_revision IS NULL
        )),
        CHECK((committed_source_config_revision IS NULL AND committed_endpoint_id IS NULL AND
               committed_canonical_transport_locator IS NULL AND
               committed_canonicalization_contract_version IS NULL AND
               committed_transport_trust_revision IS NULL) OR
              (committed_source_config_revision > 0 AND committed_endpoint_id IS NOT NULL AND
               committed_canonical_transport_locator IS NOT NULL AND
               committed_canonicalization_contract_version > 0 AND
               committed_transport_trust_revision > 0))
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
        provider_contract_version INTEGER NOT NULL
            CHECK(typeof(provider_contract_version) = 'integer' AND provider_contract_version > 0),
        lifecycle TEXT NOT NULL CHECK(lifecycle IN ('issued', 'running', 'completed', 'abandoned')),
        terminalized_at TEXT,
        terminal_reason TEXT,
        completed_at TEXT,
        provider_outcome TEXT,
        observed_at TEXT,
        normalized_snapshot_hash TEXT,
        FOREIGN KEY(inventory_source_id) REFERENCES inventory_sources(inventory_source_id),
        FOREIGN KEY(inventory_source_id, expected_endpoint_id)
            REFERENCES source_endpoints(inventory_source_id, endpoint_id),
        UNIQUE(inventory_source_id, discovery_run_sequence),
        CHECK((lifecycle IN ('issued', 'running') AND terminalized_at IS NULL AND
               terminal_reason IS NULL AND completed_at IS NULL AND provider_outcome IS NULL) OR
              (lifecycle = 'abandoned' AND terminalized_at IS NOT NULL AND
               length(trim(terminal_reason)) > 0 AND completed_at IS NULL AND
               provider_outcome IS NULL AND observed_at IS NULL AND
               normalized_snapshot_hash IS NULL) OR
              (lifecycle = 'completed' AND terminalized_at IS NOT NULL AND
               completed_at IS NOT NULL AND length(trim(provider_outcome)) > 0))
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
    BEFORE UPDATE OF endpoint_id, inventory_source_id, canonical_transport_locator,
                     canonicalization_contract_version, created_at
    ON source_endpoints
    BEGIN SELECT RAISE(ABORT, 'endpoint identity and canonical pair are immutable'); END
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
                     expected_transport_trust_revision, provider_contract_version
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
)
