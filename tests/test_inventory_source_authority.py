from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import sqlite3
import uuid

import pytest

from app.inventory import (
    CANONICALIZATION_CONTRACT_VERSION,
    InventoryAuthority,
    InventoryAuthorityStore,
    PersistentSourceFreshness,
    PersistentSourceHealth,
    PersistentSourceHealthOrigin,
    canonicalize_transport_locator,
)


FIXED_NOW = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)


def fixed_now() -> datetime:
    return FIXED_NOW


def create_authority(tmp_path: Path) -> tuple[InventoryAuthorityStore, InventoryAuthority]:
    store = InventoryAuthorityStore(tmp_path / "authority.db", now=fixed_now)
    return store, InventoryAuthority(store, now=fixed_now)


def test_canonicalization_equates_dns_case_default_port_and_root() -> None:
    assert canonicalize_transport_locator("https://PVE.EXAMPLE/") == (
        canonicalize_transport_locator("https://pve.example:443")
    )
    assert canonicalize_transport_locator("HTTPS://PVE.EXAMPLE:443/") == (
        "https://pve.example"
    )


def test_canonicalization_preserves_nondefault_port_without_magic_pve_port() -> None:
    assert canonicalize_transport_locator("https://pve.example:8006/") == (
        "https://pve.example:8006"
    )
    assert canonicalize_transport_locator("https://pve.example") == (
        "https://pve.example"
    )


def test_canonicalization_normalizes_ipv6_representation() -> None:
    assert canonicalize_transport_locator(
        "https://[2001:0DB8:0000:0000:0000:0000:0000:0001]:443/"
    ) == "https://[2001:db8::1]"


@pytest.mark.parametrize(
    ("locator", "canonical"),
    (
        ("https://pve1.example.test", "https://pve1.example.test"),
        ("https://123.example.test", "https://123.example.test"),
        ("https://node-127.example.test", "https://node-127.example.test"),
        ("https://127.0.0.1", "https://127.0.0.1"),
        ("https://192.0.2.10:8006", "https://192.0.2.10:8006"),
    ),
)
def test_canonicalization_accepts_numeric_dns_labels_and_canonical_ipv4(
    locator: str, canonical: str
) -> None:
    assert canonicalize_transport_locator(locator) == canonical


@pytest.mark.parametrize(
    "locator",
    (
        "http://pve.example",
        "https://user@pve.example",
        "https://pve.example?view=all",
        "https://pve.example?",
        "https://pve.example#fragment",
        "https://pve.example#",
        "https://pve.example/proxy/path",
        "https:///missing-host",
        "https://pve.example:not-a-port",
        "https://pve.example:0",
        "https://pve.example:",
        "https://2001:db8::1",
        "https://[v1.example]",
        "https://127.000.000.001",
        "https://127.1",
        "https://127.0.1",
        "https://2130706433",
        "https://0x7f000001",
        "https://017700000001",
        "pve.example:8006",
        "https://pve..example",
        " https://pve.example",
    ),
)
def test_canonicalization_rejects_unsupported_or_ambiguous_inputs(
    locator: str,
) -> None:
    with pytest.raises(ValueError):
        canonicalize_transport_locator(locator)


def test_initial_source_creation_is_complete_and_revisioned(tmp_path: Path) -> None:
    store, authority = create_authority(tmp_path)
    before = store.backend_instance()
    state = authority.create_inventory_source(
        provider_kind="proxmox",
        display_name="Primary PVE",
        credential_reference="secret://inventory/pve-primary",
        transport_locator="https://PVE.EXAMPLE:8006/",
    )
    after = store.backend_instance()

    for generated_id in (
        state.source.inventory_source_id,
        state.active_endpoint.endpoint_id,
    ):
        parsed = uuid.UUID(generated_id)
        assert parsed.int != 0
        assert str(parsed) == generated_id

    assert state.source.backend_instance_id == after.backend_instance_id
    assert state.source.provider_kind == "proxmox"
    assert state.source.display_name == "Primary PVE"
    assert state.source.credential_reference == "secret://inventory/pve-primary"
    assert state.source.source_config_revision == 1
    assert state.source.last_issued_run_sequence == 0
    assert state.source.last_committed_run_sequence is None
    assert state.source.active_discovery_run_id is None

    assert state.active_endpoint.inventory_source_id == state.source.inventory_source_id
    assert state.active_endpoint.lifecycle.value == "active"
    assert state.active_endpoint.canonical_transport_locator == (
        "https://pve.example:8006"
    )
    assert state.active_endpoint.canonicalization_contract_version == (
        CANONICALIZATION_CONTRACT_VERSION
    )
    assert state.active_endpoint.transport_trust_revision == 1

    health = state.runtime_health
    assert health.health is PersistentSourceHealth.NOT_YET_OBSERVED
    assert health.freshness is PersistentSourceFreshness.NOT_YET_OBSERVED
    assert health.health_origin is PersistentSourceHealthOrigin.INITIAL
    assert health.health_reason == ""
    assert health.latest_completed_run_sequence is None
    assert health.latest_completed_outcome is None
    assert health.last_health_run_sequence is None
    assert health.last_run_health_outcome is None
    assert health.last_successful_observed_at is None
    assert health.freshness_reference_at is None
    assert health.freshness_valid_until is None
    assert health.committed_source_config_revision is None
    assert health.committed_endpoint_id is None

    assert after.inventory_revision == before.inventory_revision + 1
    assert after.published_state_revision == before.published_state_revision + 1


def test_source_display_names_may_duplicate_without_identity_collision(
    tmp_path: Path,
) -> None:
    store, authority = create_authority(tmp_path)
    first = authority.create_inventory_source(
        provider_kind="proxmox",
        display_name="Duplicate label",
        credential_reference="secret://inventory/first",
        transport_locator="https://first.example:8006",
    )
    second = authority.create_inventory_source(
        provider_kind="proxmox",
        display_name="Duplicate label",
        credential_reference="secret://inventory/second",
        transport_locator="https://second.example:8006",
    )

    assert first.source.inventory_source_id != second.source.inventory_source_id
    assert first.active_endpoint.endpoint_id != second.active_endpoint.endpoint_id
    assert len(store.list_source_states()) == 2


def test_display_rename_preserves_identity_config_endpoint_and_active_context(
    tmp_path: Path,
) -> None:
    store, authority = create_authority(tmp_path)
    initial = authority.create_inventory_source(
        provider_kind="proxmox",
        display_name="Before",
        credential_reference="secret://inventory/primary",
        transport_locator="https://pve.example:8006",
    )
    run = authority.issue_discovery_run(initial.source.inventory_source_id, 7)
    before_revision = store.backend_instance()

    renamed = authority.rename_inventory_source(
        initial.source.inventory_source_id, "After"
    )
    after_revision = store.backend_instance()

    assert renamed.source.inventory_source_id == initial.source.inventory_source_id
    assert renamed.source.provider_kind == initial.source.provider_kind
    assert renamed.source.source_config_revision == initial.source.source_config_revision
    assert renamed.source.active_discovery_run_id == run.run_id
    assert renamed.active_endpoint == initial.active_endpoint
    assert run.expected_source_config_revision == renamed.source.source_config_revision
    assert run.expected_endpoint_id == renamed.active_endpoint.endpoint_id
    assert after_revision.inventory_revision == before_revision.inventory_revision + 1
    assert after_revision.published_state_revision == (
        before_revision.published_state_revision + 1
    )


def test_source_and_endpoint_identity_constraints_fail_closed(tmp_path: Path) -> None:
    store, authority = create_authority(tmp_path)
    state = authority.create_inventory_source(
        provider_kind="proxmox",
        display_name="Primary",
        credential_reference="secret://inventory/primary",
        transport_locator="https://pve.example:8006",
    )

    with pytest.raises(sqlite3.IntegrityError, match="source identity"):
        with store._transaction() as connection:
            connection.execute(
                "UPDATE inventory_sources SET provider_kind='replacement' "
                "WHERE inventory_source_id=?",
                (state.source.inventory_source_id,),
            )

    with pytest.raises(sqlite3.IntegrityError, match="endpoint identity"):
        with store._transaction() as connection:
            connection.execute(
                "UPDATE source_endpoints SET canonical_transport_locator=? "
                "WHERE endpoint_id=?",
                ("https://replacement.example:8006", state.active_endpoint.endpoint_id),
            )

    with pytest.raises(sqlite3.IntegrityError):
        with store._transaction() as connection:
            connection.execute(
                "INSERT INTO source_endpoints VALUES(?, ?, ?, 1, 'active', 1, ?)",
                (
                    str(uuid.uuid4()),
                    state.source.inventory_source_id,
                    "https://second.example:8006",
                    FIXED_NOW.isoformat(),
                ),
            )

    with pytest.raises(sqlite3.IntegrityError):
        with store._transaction() as connection:
            connection.execute(
                "INSERT INTO source_endpoints VALUES(?, ?, ?, 1, 'candidate', 1, ?)",
                (
                    str(uuid.uuid4()),
                    state.source.inventory_source_id,
                    state.active_endpoint.canonical_transport_locator,
                    FIXED_NOW.isoformat(),
                ),
            )

    assert store.source_state(state.source.inventory_source_id) == state
