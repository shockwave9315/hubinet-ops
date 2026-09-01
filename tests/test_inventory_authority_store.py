from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import sqlite3
import uuid

import pytest

from app.inventory import (
    AuthorityDatabaseRejected,
    InventoryAuthority,
    InventoryAuthorityStore,
)
from app.inventory.store import AUTHORITY_SCHEMA_MARKER, AUTHORITY_SCHEMA_VERSION


FIXED_NOW = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)


def fixed_now() -> datetime:
    return FIXED_NOW


def test_fresh_authority_database_initializes_one_persistent_backend(
    tmp_path: Path,
) -> None:
    path = tmp_path / "authority.db"
    store = InventoryAuthorityStore(path, now=fixed_now)
    assert AUTHORITY_SCHEMA_VERSION == 12

    backend = store.backend_instance()
    parsed = uuid.UUID(backend.backend_instance_id)
    assert str(parsed) == backend.backend_instance_id
    assert parsed.int != 0
    assert backend.created_at == FIXED_NOW.isoformat()
    assert backend.inventory_revision == 0
    assert backend.published_state_revision == 1
    assert store.record_counts()["backend_instance"] == 1

    with sqlite3.connect(path) as connection:
        marker = connection.execute(
            "SELECT marker, schema_version FROM authority_schema"
        ).fetchone()
        assert marker == (AUTHORITY_SCHEMA_MARKER, AUTHORITY_SCHEMA_VERSION)
        assert connection.execute("PRAGMA user_version").fetchone()[0] == AUTHORITY_SCHEMA_VERSION
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO backend_instance VALUES(2, ?, ?, 0, 1, ?)",
                (str(uuid.uuid4()), FIXED_NOW.isoformat(), FIXED_NOW.isoformat()),
            )


def test_backend_identity_and_revisions_persist_across_reopen(tmp_path: Path) -> None:
    path = tmp_path / "authority.db"
    store = InventoryAuthorityStore(path, now=fixed_now)
    initial = store.backend_instance()
    InventoryAuthority(store, now=fixed_now).create_inventory_source(
        provider_kind="proxmox",
        display_name="Primary",
        credential_reference="secret://inventory/pve-primary",
        transport_locator="https://pve.example:8006",
    )
    changed = store.backend_instance()
    store.close()

    reopened = InventoryAuthorityStore(path, now=fixed_now)
    persisted = reopened.backend_instance()
    assert persisted.backend_instance_id == initial.backend_instance_id
    assert persisted.created_at == initial.created_at
    assert persisted.inventory_revision == changed.inventory_revision == 1
    assert persisted.published_state_revision == changed.published_state_revision == 2


def test_legacy_04_database_is_rejected_without_modification(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE plans (id TEXT PRIMARY KEY);
            CREATE TABLE jobs (id TEXT PRIMARY KEY);
            PRAGMA user_version=400;
            """
        )
    before = path.read_bytes()

    with pytest.raises(AuthorityDatabaseRejected, match="legacy Hubinet Ops 0.4"):
        InventoryAuthorityStore(path, now=fixed_now)

    assert path.read_bytes() == before
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 400
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall() == [("jobs",), ("plans",)]


def test_unknown_nonempty_database_is_rejected_without_modification(
    tmp_path: Path,
) -> None:
    path = tmp_path / "unknown.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE unrelated (value TEXT NOT NULL)")
        connection.execute("INSERT INTO unrelated VALUES ('retained')")
    before = path.read_bytes()

    with pytest.raises(AuthorityDatabaseRejected, match="no recognized"):
        InventoryAuthorityStore(path, now=fixed_now)

    assert path.read_bytes() == before
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT value FROM unrelated").fetchone()[0] == "retained"


@pytest.mark.parametrize("old_version", (1, 2, 3, 4, 5, 6, 7, 8))
def test_older_dormant_authority_schema_is_rejected_without_migration(
    tmp_path: Path, old_version: int
) -> None:
    path = tmp_path / "phase1a.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE authority_schema (singleton INTEGER PRIMARY KEY, marker TEXT, schema_version INTEGER)"
        )
        connection.execute(
            "INSERT INTO authority_schema VALUES(1, ?, ?)",
            (AUTHORITY_SCHEMA_MARKER, old_version),
        )
        connection.execute(f"PRAGMA user_version={old_version}")
    before = path.read_bytes()
    with pytest.raises(AuthorityDatabaseRejected, match="unsupported"):
        InventoryAuthorityStore(path, now=fixed_now)
    assert path.read_bytes() == before


def test_store_connections_enforce_foreign_keys(tmp_path: Path) -> None:
    store = InventoryAuthorityStore(tmp_path / "authority.db", now=fixed_now)
    assert store.foreign_keys_enabled() is True

    with pytest.raises(sqlite3.IntegrityError):
        with store._transaction() as connection:
            connection.execute(
                "INSERT INTO source_endpoints VALUES(?, ?, ?, 1, 'active', 1, ?)",
                (
                    str(uuid.uuid4()),
                    str(uuid.uuid4()),
                    "https://pve.example",
                    FIXED_NOW.isoformat(),
                ),
            )


def test_initial_source_transaction_rolls_back_every_record_and_revision(
    tmp_path: Path,
) -> None:
    class FailingHealthAuthority(InventoryAuthority):
        def _insert_initial_health(self, connection, *, source_id: str) -> None:
            raise RuntimeError("injected health persistence failure")

    store = InventoryAuthorityStore(tmp_path / "authority.db", now=fixed_now)
    before = store.backend_instance()

    with pytest.raises(RuntimeError, match="injected health"):
        FailingHealthAuthority(store, now=fixed_now).create_inventory_source(
            provider_kind="proxmox",
            display_name="Will roll back",
            credential_reference="secret://inventory/failing",
            transport_locator="https://pve.example:8006",
        )

    counts = store.record_counts()
    assert counts["inventory_sources"] == 0
    assert counts["source_endpoints"] == 0
    assert counts["source_runtime_health"] == 0
    assert store.backend_instance() == before
