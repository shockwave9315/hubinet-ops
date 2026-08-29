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
    AuthorityDatabaseRejected,
    AuthorityInvariantError,
    AuthorityNotFound,
    BackendInstance,
    DiscoveryRun,
    DiscoveryRunLifecycle,
    EndpointLifecycle,
    InventorySource,
    InventorySourceState,
    InventoryNode,
    PackageScanFailure,
    PackageScanLifecycle,
    PackageScanOutcome,
    PackageScanPackage,
    PackageScanRun,
    PackagePlanApproval,
    PersistentSourceFreshness,
    PersistentSourceHealth,
    PersistentSourceHealthOrigin,
    ResourceTermination,
    SourceEndpoint,
    SourceRuntimeHealth,
    ResourceIncarnation,
    ResourceLocatorBinding,
)


AUTHORITY_SCHEMA_MARKER = "hubinet_ops_0_5_authority"
AUTHORITY_SCHEMA_VERSION = 8
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
        "package_scan_runs",
        "package_scan_packages",
        "package_plan_approvals",
        "canonicalization_migrations",
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
        "resource_termination_immutable",
        "resource_termination_no_delete",
        "one_active_package_scan_per_resource",
        "package_scan_issuance_immutable",
        "package_scan_terminalization_once",
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

    def resource_termination(self, resource_id: str) -> ResourceTermination | None:
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM resource_terminations WHERE resource_id=?",
                (resource_id,),
            ).fetchone()
        return _resource_termination(row) if row is not None else None

    def package_scan_run(self, scan_run_id: str) -> PackageScanRun:
        with self._read_transaction() as connection:
            row = connection.execute(
                "SELECT * FROM package_scan_runs WHERE scan_run_id=?",
                (scan_run_id,),
            ).fetchone()
            if row is None:
                raise AuthorityNotFound("package scan run does not exist")
            return _package_scan_run(connection, row)

    def list_package_scan_runs(
        self, resource_id: str | None = None
    ) -> tuple[PackageScanRun, ...]:
        with self._read_transaction() as connection:
            if resource_id is None:
                rows = connection.execute(
                    "SELECT * FROM package_scan_runs "
                    "ORDER BY resource_id, attempt_sequence"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM package_scan_runs WHERE resource_id=? "
                    "ORDER BY attempt_sequence",
                    (resource_id,),
                ).fetchall()
            return tuple(_package_scan_run(connection, row) for row in rows)

    def package_plan_approval(self, resource_id: str) -> PackagePlanApproval | None:
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM package_plan_approvals WHERE resource_id=?",
                (resource_id,),
            ).fetchone()
        return _package_plan_approval(row) if row is not None else None

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
        created_at=str(row["created_at"]),
    )


def _package_scan_run(
    connection: sqlite3.Connection, row: sqlite3.Row
) -> PackageScanRun:
    package_rows = connection.execute(
        "SELECT * FROM package_scan_packages WHERE scan_run_id=? "
        "ORDER BY package_index",
        (str(row["scan_run_id"]),),
    ).fetchall()
    packages = tuple(
        PackageScanPackage(
            package_name=str(package["package_name"]),
            installed_version=str(package["installed_version"]),
            candidate_version=str(package["candidate_version"]),
            origin=package["origin"],
            description=package["description"],
            security=_optional_sqlite_boolean(package["security"], "security"),
        )
        for package in package_rows
    )
    lifecycle = PackageScanLifecycle(str(row["lifecycle"]))
    outcome = (
        PackageScanOutcome(str(row["outcome"]))
        if row["outcome"] is not None
        else None
    )
    failure = (
        PackageScanFailure(str(row["failure_class"]))
        if row["failure_class"] is not None
        else None
    )
    pending = int(row["pending_count"]) if row["pending_count"] is not None else None
    if outcome is PackageScanOutcome.SUCCESS and pending != len(packages):
        raise AuthorityInvariantError(
            "successful package scan pending count does not match package rows"
        )
    return PackageScanRun(
        scan_run_id=str(row["scan_run_id"]),
        resource_id=str(row["resource_id"]),
        inventory_source_id=str(row["inventory_source_id"]),
        committed_source_config_revision=int(
            row["committed_source_config_revision"]
        ),
        committed_endpoint_id=str(row["committed_endpoint_id"]),
        committed_canonical_transport_locator=str(
            row["committed_canonical_transport_locator"]
        ),
        committed_canonicalization_contract_version=int(
            row["committed_canonicalization_contract_version"]
        ),
        committed_transport_trust_revision=int(
            row["committed_transport_trust_revision"]
        ),
        provider_contract_version=int(row["provider_contract_version"]),
        attempt_sequence=int(row["attempt_sequence"]),
        expected_binding_id=str(row["expected_binding_id"]),
        expected_locator_generation=int(row["expected_locator_generation"]),
        expected_resource_continuity_revision=int(
            row["expected_resource_continuity_revision"]
        ),
        expected_vmid=int(row["expected_vmid"]),
        expected_node_id=str(row["expected_node_id"]),
        expected_node_name=str(row["expected_node_name"]),
        started_at=str(row["started_at"]),
        lifecycle=lifecycle,
        completed_at=row["completed_at"],
        outcome=outcome,
        failure_class=failure,
        error_message=row["error_message"],
        os_id=row["os_id"],
        os_version=row["os_version"],
        pending_count=pending,
        plan_fingerprint=row["plan_fingerprint"],
        reboot_required=_optional_sqlite_boolean(
            row["reboot_required"], "reboot_required"
        ),
        packages=packages,
    )


def _package_plan_approval(row: sqlite3.Row) -> PackagePlanApproval:
    return PackagePlanApproval(
        approval_id=str(row["approval_id"]),
        resource_id=str(row["resource_id"]),
        reviewed_scan_run_id=str(row["reviewed_scan_run_id"]),
        approved_plan_fingerprint=str(row["approved_plan_fingerprint"]),
        approved_at=str(row["approved_at"]),
    )


_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE authority_schema (
        singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
        marker TEXT NOT NULL CHECK(marker = 'hubinet_ops_0_5_authority'),
        schema_version INTEGER NOT NULL CHECK(schema_version = 8)
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
            completion_transport_trust_revision IS NULL)),
        CHECK((detail_ok_count IS NULL AND detail_temporarily_unavailable_count IS NULL AND
               detail_error_count IS NULL) OR
              (detail_ok_count IS NOT NULL AND
               detail_temporarily_unavailable_count IS NOT NULL AND
               detail_error_count IS NOT NULL)),
        CHECK((completion_source_config_revision IS NULL AND completion_endpoint_id IS NULL AND
               completion_canonical_transport_locator IS NULL AND
               completion_canonicalization_contract_version IS NULL AND
               completion_transport_trust_revision IS NULL) OR
              (completion_source_config_revision IS NOT NULL AND
               length(trim(completion_endpoint_id)) > 0 AND
               length(trim(completion_canonical_transport_locator)) > 0 AND
               completion_canonicalization_contract_version IS NOT NULL AND
               completion_transport_trust_revision IS NOT NULL)),
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
        presence TEXT NOT NULL CHECK(presence IN ('present', 'missing', 'not_current')),
        lifecycle TEXT NOT NULL CHECK(lifecycle IN ('active', 'quarantined', 'retired')),
        observational_continuity TEXT NOT NULL CHECK(observational_continuity IN ('consistent', 'uncertain', 'replaced')),
        security_continuity TEXT NOT NULL CHECK(security_continuity = 'unverified'),
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
        reason TEXT NOT NULL CHECK(reason = 'replaced'),
        successor_resource_id TEXT NOT NULL,
        run_sequence INTEGER NOT NULL CHECK(typeof(run_sequence) = 'integer' AND run_sequence > 0),
        created_at TEXT NOT NULL,
        FOREIGN KEY(resource_id) REFERENCES resource_incarnations(resource_id),
        FOREIGN KEY(binding_id) REFERENCES resource_locator_bindings(binding_id),
        FOREIGN KEY(successor_resource_id) REFERENCES resource_incarnations(resource_id)
            DEFERRABLE INITIALLY DEFERRED
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
    CREATE TABLE package_scan_runs (
        scan_run_id TEXT PRIMARY KEY,
        resource_id TEXT NOT NULL,
        inventory_source_id TEXT NOT NULL,
        committed_source_config_revision INTEGER NOT NULL
            CHECK(typeof(committed_source_config_revision) = 'integer' AND
                  committed_source_config_revision > 0),
        committed_endpoint_id TEXT NOT NULL,
        committed_canonical_transport_locator TEXT NOT NULL,
        committed_canonicalization_contract_version INTEGER NOT NULL
            CHECK(typeof(committed_canonicalization_contract_version) = 'integer' AND
                  committed_canonicalization_contract_version > 0),
        committed_transport_trust_revision INTEGER NOT NULL
            CHECK(typeof(committed_transport_trust_revision) = 'integer' AND
                  committed_transport_trust_revision > 0),
        provider_contract_version INTEGER NOT NULL
            CHECK(typeof(provider_contract_version) = 'integer' AND
                  provider_contract_version > 0),
        attempt_sequence INTEGER NOT NULL
            CHECK(typeof(attempt_sequence) = 'integer' AND attempt_sequence > 0),
        expected_binding_id TEXT NOT NULL,
        expected_locator_generation INTEGER NOT NULL
            CHECK(typeof(expected_locator_generation) = 'integer' AND
                  expected_locator_generation > 0),
        expected_resource_continuity_revision INTEGER NOT NULL
            CHECK(typeof(expected_resource_continuity_revision) = 'integer' AND
                  expected_resource_continuity_revision > 0),
        expected_vmid INTEGER NOT NULL
            CHECK(typeof(expected_vmid) = 'integer' AND expected_vmid > 0),
        expected_node_id TEXT NOT NULL,
        expected_node_name TEXT NOT NULL CHECK(length(trim(expected_node_name)) > 0),
        started_at TEXT NOT NULL,
        lifecycle TEXT NOT NULL CHECK(lifecycle IN ('running', 'completed')),
        completed_at TEXT,
        outcome TEXT CHECK(outcome IS NULL OR outcome IN ('success', 'failed', 'interrupted')),
        failure_class TEXT CHECK(failure_class IS NULL OR failure_class IN (
            'guest_unavailable', 'unsupported_resource_type', 'unsupported_os',
            'package_manager_busy', 'metadata_refresh_failed', 'simulation_failed',
            'timeout', 'malformed_plan', 'stale_target', 'execution_failed')),
        error_message TEXT CHECK(error_message IS NULL OR length(error_message) <= 500),
        os_id TEXT,
        os_version TEXT,
        pending_count INTEGER CHECK(pending_count IS NULL OR
            (typeof(pending_count) = 'integer' AND pending_count >= 0)),
        plan_fingerprint TEXT CHECK(plan_fingerprint IS NULL OR length(plan_fingerprint) = 64),
        reboot_required INTEGER CHECK(reboot_required IS NULL OR reboot_required = 1),
        FOREIGN KEY(inventory_source_id, resource_id)
            REFERENCES resource_incarnations(inventory_source_id, resource_id),
        FOREIGN KEY(expected_binding_id) REFERENCES resource_locator_bindings(binding_id),
        FOREIGN KEY(inventory_source_id, expected_node_id)
            REFERENCES inventory_nodes(inventory_source_id, node_id),
        UNIQUE(resource_id, attempt_sequence),
        CHECK((lifecycle = 'running' AND completed_at IS NULL AND outcome IS NULL AND
               failure_class IS NULL AND error_message IS NULL AND os_id IS NULL AND
               os_version IS NULL AND pending_count IS NULL AND plan_fingerprint IS NULL AND
               reboot_required IS NULL) OR
              (lifecycle = 'completed' AND completed_at IS NOT NULL AND outcome IS NOT NULL)),
        CHECK(outcome != 'success' OR (
            failure_class IS NULL AND error_message IS NULL AND os_id IS NOT NULL AND
            os_version IS NOT NULL AND pending_count IS NOT NULL AND
            plan_fingerprint IS NOT NULL)),
        CHECK(outcome NOT IN ('failed', 'interrupted') OR (
            failure_class IS NOT NULL AND error_message IS NOT NULL AND
            pending_count IS NULL AND plan_fingerprint IS NULL AND reboot_required IS NULL))
    )
    """,
    """
    CREATE UNIQUE INDEX one_active_package_scan_per_resource
    ON package_scan_runs(resource_id) WHERE lifecycle = 'running'
    """,
    """
    CREATE TABLE package_scan_packages (
        scan_run_id TEXT NOT NULL,
        package_index INTEGER NOT NULL
            CHECK(typeof(package_index) = 'integer' AND package_index >= 0),
        package_name TEXT NOT NULL CHECK(length(trim(package_name)) > 0),
        installed_version TEXT NOT NULL CHECK(length(trim(installed_version)) > 0),
        candidate_version TEXT NOT NULL CHECK(length(trim(candidate_version)) > 0),
        origin TEXT CHECK(origin IS NULL OR length(origin) <= 500),
        description TEXT CHECK(description IS NULL OR length(description) <= 500),
        security INTEGER CHECK(security IS NULL OR security = 1),
        PRIMARY KEY(scan_run_id, package_index),
        UNIQUE(scan_run_id, package_name),
        FOREIGN KEY(scan_run_id) REFERENCES package_scan_runs(scan_run_id)
    )
    """,
    """
    CREATE TABLE package_plan_approvals (
        resource_id TEXT PRIMARY KEY,
        approval_id TEXT NOT NULL UNIQUE,
        reviewed_scan_run_id TEXT NOT NULL,
        approved_plan_fingerprint TEXT NOT NULL
            CHECK(length(approved_plan_fingerprint) = 64),
        approved_at TEXT NOT NULL,
        FOREIGN KEY(resource_id) REFERENCES resource_incarnations(resource_id),
        FOREIGN KEY(reviewed_scan_run_id) REFERENCES package_scan_runs(scan_run_id)
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
                     expected_transport_trust_revision,
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
    CREATE TRIGGER package_scan_issuance_immutable
    BEFORE UPDATE OF scan_run_id, resource_id, inventory_source_id,
                     committed_source_config_revision, committed_endpoint_id,
                     committed_canonical_transport_locator,
                     committed_canonicalization_contract_version,
                     committed_transport_trust_revision,
                     provider_contract_version,
                     attempt_sequence, expected_binding_id,
                     expected_locator_generation,
                     expected_resource_continuity_revision, expected_vmid,
                     expected_node_id, expected_node_name, started_at
    ON package_scan_runs
    BEGIN SELECT RAISE(ABORT, 'package scan issuance fields are immutable'); END
    """,
    """
    CREATE TRIGGER package_scan_terminalization_once
    BEFORE UPDATE ON package_scan_runs
    WHEN OLD.lifecycle = 'completed'
    BEGIN SELECT RAISE(ABORT, 'package scan run is already terminal'); END
    """,
)
