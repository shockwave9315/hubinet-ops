from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier

import pytest

from app.inventory import (
    AuthorityConflict,
    DiscoveryRunLifecycle,
    InventoryAuthority,
    InventoryAuthorityStore,
)


FIXED_NOW = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)


def fixed_now() -> datetime:
    return FIXED_NOW


def create_source(
    store: InventoryAuthorityStore,
    *,
    name: str = "Primary",
    locator: str = "https://pve.example:8006",
) -> str:
    state = InventoryAuthority(store, now=fixed_now).create_inventory_source(
        provider_kind="proxmox",
        display_name=name,
        credential_reference=f"secret://inventory/{name.lower()}",
        transport_locator=locator,
    )
    return state.source.inventory_source_id


def test_issuance_allocates_sequence_and_exact_context_and_published_revision(
    tmp_path: Path,
) -> None:
    store = InventoryAuthorityStore(tmp_path / "authority.db", now=fixed_now)
    source_id = create_source(store)
    state = store.source_state(source_id)
    before = store.backend_instance()

    run = InventoryAuthority(store, now=fixed_now).issue_discovery_run(source_id, 3)
    current = store.source_state(source_id)
    after = store.backend_instance()

    assert run.discovery_run_sequence == 1
    assert run.lifecycle is DiscoveryRunLifecycle.ISSUED
    assert run.expected_source_config_revision == state.source.source_config_revision
    assert run.expected_endpoint_id == state.active_endpoint.endpoint_id
    assert run.expected_canonical_transport_locator == (
        state.active_endpoint.canonical_transport_locator
    )
    assert run.expected_canonicalization_contract_version == (
        state.active_endpoint.canonicalization_contract_version
    )
    assert run.expected_transport_trust_revision == (
        state.active_endpoint.transport_trust_revision
    )
    assert run.provider_contract_version == 3
    assert current.source.active_discovery_run_id == run.run_id
    assert current.source.last_issued_run_sequence == 1
    assert after.inventory_revision == before.inventory_revision
    assert after.published_state_revision == before.published_state_revision + 1


def test_running_and_abandon_are_exact_one_time_fences_without_fake_observation(
    tmp_path: Path,
) -> None:
    store = InventoryAuthorityStore(tmp_path / "authority.db", now=fixed_now)
    source_id = create_source(store)
    authority = InventoryAuthority(store, now=fixed_now)
    issued = authority.issue_discovery_run(source_id, 1)
    running = authority.mark_discovery_run_running(source_id, issued.run_id)
    abandoned = authority.abandon_discovery_run(
        source_id, running.run_id, reason="restart recovery fence"
    )

    assert running.lifecycle is DiscoveryRunLifecycle.RUNNING
    assert abandoned.lifecycle is DiscoveryRunLifecycle.ABANDONED
    assert abandoned.terminalized_at == FIXED_NOW.isoformat()
    assert abandoned.terminal_reason == "restart recovery fence"
    assert abandoned.completed_at is None
    assert abandoned.provider_outcome is None
    assert abandoned.observed_at is None
    assert abandoned.normalized_snapshot_hash is None
    assert store.source_state(source_id).source.active_discovery_run_id is None
    assert store.source_state(source_id).runtime_health.latest_completed_run_sequence is None


def test_abandoned_sequence_remains_consumed_and_is_never_reused(tmp_path: Path) -> None:
    store = InventoryAuthorityStore(tmp_path / "authority.db", now=fixed_now)
    source_id = create_source(store)
    authority = InventoryAuthority(store, now=fixed_now)
    first = authority.issue_discovery_run(source_id, 1)
    authority.abandon_discovery_run(source_id, first.run_id, reason="cancelled")
    second = authority.issue_discovery_run(source_id, 1)

    assert first.discovery_run_sequence == 1
    assert second.discovery_run_sequence == 2
    assert [run.discovery_run_sequence for run in store.list_discovery_runs(source_id)] == [1, 2]


def test_restart_preserves_active_owner_and_blocks_new_issuance(tmp_path: Path) -> None:
    path = tmp_path / "authority.db"
    store = InventoryAuthorityStore(path, now=fixed_now)
    source_id = create_source(store)
    run = InventoryAuthority(store, now=fixed_now).issue_discovery_run(source_id, 1)
    store.close()

    reopened = InventoryAuthorityStore(path, now=fixed_now)
    state = reopened.source_state(source_id)
    assert state.source.active_discovery_run_id == run.run_id
    assert state.source.last_issued_run_sequence == run.discovery_run_sequence
    with pytest.raises(AuthorityConflict, match="already has an active"):
        InventoryAuthority(reopened, now=fixed_now).issue_discovery_run(source_id, 1)


def test_same_source_concurrent_issuance_has_exactly_one_winner(tmp_path: Path) -> None:
    path = tmp_path / "authority.db"
    setup_store = InventoryAuthorityStore(path, now=fixed_now)
    source_id = create_source(setup_store)
    setup_store.close()
    barrier = Barrier(2)

    def issue() -> tuple[str, str]:
        store = InventoryAuthorityStore(path, now=fixed_now)
        authority = InventoryAuthority(store, now=fixed_now)
        barrier.wait()
        try:
            run = authority.issue_discovery_run(source_id, 1)
            return "success", run.run_id
        except AuthorityConflict as exc:
            return "conflict", str(exc)
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: issue(), range(2)))

    assert [kind for kind, _ in results].count("success") == 1
    assert [kind for kind, _ in results].count("conflict") == 1
    verified = InventoryAuthorityStore(path, now=fixed_now)
    runs = verified.list_discovery_runs(source_id)
    state = verified.source_state(source_id)
    assert len(runs) == 1
    assert state.source.active_discovery_run_id == runs[0].run_id
    assert state.source.last_issued_run_sequence == runs[0].discovery_run_sequence == 1


def test_different_sources_may_own_runs_concurrently(tmp_path: Path) -> None:
    path = tmp_path / "authority.db"
    setup = InventoryAuthorityStore(path, now=fixed_now)
    source_a = create_source(setup, name="A", locator="https://a.example:8006")
    source_b = create_source(setup, name="B", locator="https://b.example:8006")
    before = setup.backend_instance()
    setup.close()
    barrier = Barrier(2)

    def issue(source_id: str) -> str:
        store = InventoryAuthorityStore(path, now=fixed_now)
        barrier.wait()
        try:
            return InventoryAuthority(store, now=fixed_now).issue_discovery_run(
                source_id, 1
            ).run_id
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        run_ids = list(executor.map(issue, (source_a, source_b)))

    verified = InventoryAuthorityStore(path, now=fixed_now)
    assert verified.source_state(source_a).source.active_discovery_run_id in run_ids
    assert verified.source_state(source_b).source.active_discovery_run_id in run_ids
    after = verified.backend_instance()
    assert after.inventory_revision == before.inventory_revision
    assert after.published_state_revision == before.published_state_revision + 2


def test_wrong_run_cannot_release_another_sources_owner(tmp_path: Path) -> None:
    store = InventoryAuthorityStore(tmp_path / "authority.db", now=fixed_now)
    source_a = create_source(store, name="A", locator="https://a.example:8006")
    source_b = create_source(store, name="B", locator="https://b.example:8006")
    authority = InventoryAuthority(store, now=fixed_now)
    run_a = authority.issue_discovery_run(source_a, 1)
    run_b = authority.issue_discovery_run(source_b, 1)

    with pytest.raises(AuthorityConflict, match="belongs to another source"):
        authority.abandon_discovery_run(source_a, run_b.run_id, reason="wrong owner")

    assert store.source_state(source_a).source.active_discovery_run_id == run_a.run_id
    assert store.source_state(source_b).source.active_discovery_run_id == run_b.run_id


def test_second_terminalization_and_late_worker_are_rejected_without_state_change(
    tmp_path: Path,
) -> None:
    store = InventoryAuthorityStore(tmp_path / "authority.db", now=fixed_now)
    source_id = create_source(store)
    authority = InventoryAuthority(store, now=fixed_now)
    run = authority.issue_discovery_run(source_id, 1)
    abandoned = authority.abandon_discovery_run(source_id, run.run_id, reason="fenced")
    before = store.backend_instance()

    with pytest.raises(AuthorityConflict):
        authority.abandon_discovery_run(source_id, run.run_id, reason="again")
    with pytest.raises(AuthorityConflict):
        authority.mark_discovery_run_running(source_id, run.run_id)

    assert store.discovery_run(run.run_id) == abandoned
    assert store.backend_instance() == before
    with pytest.raises(FrozenInstanceError):
        abandoned.provider_outcome = "success"  # type: ignore[misc]


def test_issuance_transaction_rolls_back_sequence_run_owner_and_revision(
    tmp_path: Path,
) -> None:
    class FailingClaimAuthority(InventoryAuthority):
        def _claim_active_run(self, connection, *, source_id: str, run_id: str) -> None:
            raise RuntimeError("injected owner claim failure")

    store = InventoryAuthorityStore(tmp_path / "authority.db", now=fixed_now)
    source_id = create_source(store)
    before = store.backend_instance()

    with pytest.raises(RuntimeError, match="injected owner claim"):
        FailingClaimAuthority(store, now=fixed_now).issue_discovery_run(source_id, 1)

    state = store.source_state(source_id)
    assert state.source.last_issued_run_sequence == 0
    assert state.source.active_discovery_run_id is None
    assert store.list_discovery_runs(source_id) == ()
    assert store.backend_instance() == before


def test_contract_sensitive_integer_rejects_bool_without_state_change(
    tmp_path: Path,
) -> None:
    store = InventoryAuthorityStore(tmp_path / "authority.db", now=fixed_now)
    source_id = create_source(store)
    before = store.backend_instance()

    with pytest.raises(ValueError, match="positive integer"):
        InventoryAuthority(store, now=fixed_now).issue_discovery_run(source_id, True)

    assert store.source_state(source_id).source.last_issued_run_sequence == 0
    assert store.list_discovery_runs(source_id) == ()
    assert store.backend_instance() == before
