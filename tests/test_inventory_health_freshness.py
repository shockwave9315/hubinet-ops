from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.inventory import (
    AuthorityConflict,
    BaselineCompleteness,
    BaselineMode,
    DetailReadStatus,
    DiscoveredNode,
    DiscoveredResource,
    InventoryAuthority,
    InventoryAuthorityStore,
    NormalizedDiscoverySnapshot,
    SourceAvailability,
)


START = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.value = START

    def __call__(self) -> datetime:
        return self.value


def make_authority(tmp_path: Path, *, duration: int = 300, authority_type=InventoryAuthority):
    clock = Clock()
    store = InventoryAuthorityStore(tmp_path / "authority.db", now=clock)
    authority = authority_type(store, now=clock)
    source = authority.create_inventory_source(
        provider_kind="proxmox",
        display_name="Primary",
        credential_reference="secret://inventory/primary",
        transport_locator="https://pve.example:8006",
        freshness_duration_seconds=duration,
    )
    return clock, store, authority, source.source.inventory_source_id


def snapshot_for(
    store,
    run,
    *,
    observed_at: str | None = None,
    node_observed_at: tuple[str, ...] | None = None,
    resource_observed_at: tuple[str, ...] | None = None,
):
    source_id = run.inventory_source_id
    observed = observed_at or START.isoformat()
    node_times = node_observed_at if node_observed_at is not None else (observed,)
    resource_times = (
        resource_observed_at if resource_observed_at is not None else (observed,)
    )
    node_names = tuple(f"pve-{chr(ord('a') + index)}" for index in range(len(node_times)))
    return NormalizedDiscoverySnapshot(
        run_id=run.run_id,
        discovery_run_sequence=run.discovery_run_sequence,
        inventory_source_id=source_id,
        expected_source_config_revision=run.expected_source_config_revision,
        endpoint_id=run.expected_endpoint_id,
        canonical_transport_locator=run.expected_canonical_transport_locator,
        canonicalization_contract_version=run.expected_canonicalization_contract_version,
        expected_transport_trust_revision=run.expected_transport_trust_revision,
        provider_contract_version=run.provider_contract_version,
        observed_at=observed,
        source_facts={"release": "9.0"},
        source_availability=SourceAvailability.AVAILABLE,
        baseline_completeness=BaselineCompleteness.COMPLETE,
        baseline_mode=BaselineMode.CLUSTER,
        acl_topology_hash_before="acl",
        acl_topology_hash_after="acl",
        permission_snapshot_hash_before="perm",
        permission_snapshot_hash_after="perm",
        permission_coverage_complete=True,
        boundary_consistent=True,
        covered_nodes=node_names,
        failed_baseline_scopes=(),
        detail_summary={
            "ok_count": len(resource_times),
            "temporarily_unavailable_count": 0,
            "error_count": 0,
        },
        failed_detail_scopes=(),
        nodes=tuple(
            DiscoveredNode(node_name, "online", True, node_time, {})
            for node_name, node_time in zip(node_names, node_times, strict=True)
        ),
        resources=tuple(
            DiscoveredResource(
                source_id,
                100 + index,
                "qemu",
                f"guest-{index}",
                "running",
                node_names[index % len(node_names)] if node_names else None,
                resource_time,
                DetailReadStatus.OK,
                {"memory": 1024},
            )
            for index, resource_time in enumerate(resource_times)
        ),
    )


def successful_run(store, authority, source_id, *, observed_at: str | None = None):
    run = authority.issue_discovery_run(source_id, 1)
    authority.mark_discovery_run_running(source_id, run.run_id)
    authority.finalize_successful_discovery_run(
        source_id, run.run_id, snapshot_for(store, run, observed_at=observed_at)
    )
    return store.discovery_run(run.run_id)


def test_successful_completion_is_write_once_and_persists_restart(tmp_path: Path) -> None:
    _, store, authority, source_id = make_authority(tmp_path)
    completed = successful_run(store, authority, source_id)
    state = store.source_state(source_id)
    assert completed.lifecycle.value == "completed"
    assert completed.provider_outcome == "success"
    assert state.source.active_discovery_run_id is None
    assert state.source.last_committed_run_sequence == completed.discovery_run_sequence
    assert state.runtime_health.latest_completed_outcome == "success"
    store.close()
    reopened = InventoryAuthorityStore(tmp_path / "authority.db")
    assert reopened.discovery_run(completed.run_id) == completed
    assert reopened.source_state(source_id).runtime_health.last_successful_observed_at == START.isoformat()


def test_failed_completion_is_visible_but_does_not_mutate_inventory(tmp_path: Path) -> None:
    _, store, authority, source_id = make_authority(tmp_path)
    run = authority.issue_discovery_run(source_id, 1)
    before = store.backend_instance()
    completed = authority.finalize_failed_discovery_run(
        source_id,
        run.run_id,
        outcome=BaselineCompleteness.SOURCE_UNAVAILABLE,
        reason="transport unavailable",
    )
    after = store.backend_instance()
    state = store.source_state(source_id)
    assert completed.provider_outcome == "source_unavailable"
    assert state.runtime_health.health.value == "source_unavailable"
    assert state.runtime_health.freshness.value == "stale"
    assert after.inventory_revision == before.inventory_revision
    assert after.published_state_revision == before.published_state_revision + 1
    with pytest.raises(AuthorityConflict):
        authority.finalize_failed_discovery_run(
            source_id, run.run_id, outcome=BaselineCompleteness.PARTIAL, reason="late"
        )


def test_fast_success_expires_with_compare_and_swap_timer(tmp_path: Path) -> None:
    clock, store, authority, source_id = make_authority(tmp_path, duration=60)
    completed = successful_run(store, authority, source_id)
    health = store.source_state(source_id).runtime_health
    assert health.freshness.value == "fresh"
    old_revision = store.backend_instance()
    clock.value = START + timedelta(seconds=61)
    assert authority.materialize_due_expiry(
        source_id,
        expected_run_sequence=completed.discovery_run_sequence,
        expected_deadline=health.freshness_valid_until,
    )
    current = store.backend_instance()
    assert current.inventory_revision == old_revision.inventory_revision
    assert current.published_state_revision == old_revision.published_state_revision + 1
    assert store.source_state(source_id).runtime_health.health_origin.value == "time_expiry"
    assert not authority.materialize_due_expiry(source_id)
    assert not authority.source_is_fresh_for_future_mutation(source_id)


def test_future_mutation_freshness_check_fences_expiry_and_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock, store, authority, source_id = make_authority(tmp_path, duration=10)
    successful_run(store, authority, source_id)
    clock.value = START + timedelta(seconds=11)
    split_wrapper_calls = 0
    original_materialize = authority.materialize_due_expiry

    def split_transaction_trap(*args, **kwargs):
        nonlocal split_wrapper_calls
        split_wrapper_calls += 1
        clock.value = START + timedelta(seconds=9)
        result = original_materialize(*args, **kwargs)
        clock.value = START + timedelta(seconds=11)
        return result

    monkeypatch.setattr(authority, "materialize_due_expiry", split_transaction_trap)

    assert not authority.source_is_fresh_for_future_mutation(source_id)
    assert split_wrapper_calls == 0
    assert store.source_state(source_id).runtime_health.freshness.value == "stale"


def test_success_observed_before_deadline_but_committed_after_is_stale_on_arrival(tmp_path: Path) -> None:
    clock, store, authority, source_id = make_authority(tmp_path, duration=30)
    run = authority.issue_discovery_run(source_id, 1)
    clock.value = START + timedelta(seconds=31)
    authority.finalize_successful_discovery_run(source_id, run.run_id, snapshot_for(store, run))
    health = store.source_state(source_id).runtime_health
    assert health.freshness.value == "stale"
    assert health.health_origin.value == "time_expiry"


def test_older_nested_observation_is_stale_on_arrival_without_changing_snapshot_provenance(
    tmp_path: Path,
) -> None:
    clock, store, authority, source_id = make_authority(tmp_path, duration=300)
    run = authority.issue_discovery_run(source_id, 1)
    oldest = START - timedelta(hours=1)
    clock.value = START + timedelta(minutes=1)
    authority.finalize_successful_discovery_run(
        source_id,
        run.run_id,
        snapshot_for(
            store,
            run,
            node_observed_at=(oldest.isoformat(),),
            resource_observed_at=(oldest.isoformat(),),
        ),
    )
    health = store.source_state(source_id).runtime_health
    assert health.last_successful_observed_at == START.isoformat()
    assert health.freshness_reference_at == oldest.isoformat()
    assert health.freshness_valid_until == (oldest + timedelta(seconds=300)).isoformat()
    assert health.freshness.value == "stale"
    assert health.health_origin.value == "time_expiry"
    assert health.health_reason == "freshness_deadline_elapsed_before_commit"
    assert len(store.list_resources(source_id)) == 1
    assert not authority.source_is_fresh_for_future_mutation(source_id)


@pytest.mark.parametrize(
    (
        "snapshot_time",
        "node_times",
        "resource_times",
        "commit_time",
        "expected_reference",
        "expected_freshness",
    ),
    (
        (
            START,
            (START + timedelta(minutes=1),),
            (START + timedelta(minutes=1),),
            START + timedelta(minutes=1),
            START,
            "fresh",
        ),
        (
            START,
            (START - timedelta(hours=1),),
            (START - timedelta(minutes=30),),
            START,
            START - timedelta(hours=1),
            "stale",
        ),
        (
            START,
            (START - timedelta(minutes=30),),
            (START - timedelta(hours=1, minutes=15),),
            START,
            START - timedelta(hours=1, minutes=15),
            "stale",
        ),
        (
            START,
            (START - timedelta(minutes=10), START - timedelta(minutes=40)),
            (START - timedelta(minutes=20), START - timedelta(minutes=50)),
            START,
            START - timedelta(minutes=50),
            "stale",
        ),
        (START, (START,), (START,), START, START, "fresh"),
        (
            START,
            (START - timedelta(minutes=2),),
            (START - timedelta(minutes=1),),
            START + timedelta(minutes=1),
            START - timedelta(minutes=2),
            "fresh",
        ),
    ),
)
def test_freshness_reference_is_oldest_relevant_observation(
    tmp_path: Path,
    snapshot_time: datetime,
    node_times: tuple[datetime, ...],
    resource_times: tuple[datetime, ...],
    commit_time: datetime,
    expected_reference: datetime,
    expected_freshness: str,
) -> None:
    clock, store, authority, source_id = make_authority(tmp_path, duration=300)
    run = authority.issue_discovery_run(source_id, 1)
    clock.value = commit_time
    authority.finalize_successful_discovery_run(
        source_id,
        run.run_id,
        snapshot_for(
            store,
            run,
            observed_at=snapshot_time.isoformat(),
            node_observed_at=tuple(value.isoformat() for value in node_times),
            resource_observed_at=tuple(value.isoformat() for value in resource_times),
        ),
    )
    health = store.source_state(source_id).runtime_health
    assert health.last_successful_observed_at == snapshot_time.isoformat()
    assert health.freshness_reference_at == expected_reference.isoformat()
    assert health.freshness_valid_until == (
        expected_reference + timedelta(seconds=300)
    ).isoformat()
    assert health.freshness.value == expected_freshness


@pytest.mark.parametrize(
    ("timestamp_kwargs", "error"),
    (
        (
            {"node_observed_at": ("not-a-timestamp",)},
            "node observed_at must be an ISO timestamp",
        ),
        (
            {"resource_observed_at": (START.replace(tzinfo=None).isoformat(),)},
            "resource observed_at must be timezone-aware",
        ),
    ),
)
def test_malformed_nested_observation_rolls_back_success_atomically(
    tmp_path: Path,
    timestamp_kwargs: dict[str, tuple[str, ...]],
    error: str,
) -> None:
    _, store, authority, source_id = make_authority(tmp_path)
    run = authority.issue_discovery_run(source_id, 1)
    before = store.backend_instance()
    with pytest.raises(ValueError, match=error):
        authority.finalize_successful_discovery_run(
            source_id,
            run.run_id,
            snapshot_for(store, run, **timestamp_kwargs),
        )
    assert store.list_resources(source_id) == ()
    assert store.source_state(source_id).source.active_discovery_run_id == run.run_id
    assert store.discovery_run(run.run_id).lifecycle.value == "issued"
    assert store.backend_instance() == before


def test_future_freshness_reference_commits_last_known_inventory_but_never_fresh(
    tmp_path: Path,
) -> None:
    clock, store, authority, source_id = make_authority(tmp_path, duration=300)
    run = authority.issue_discovery_run(source_id, 1)
    future_observation = START + timedelta(hours=1)
    authority.finalize_successful_discovery_run(
        source_id,
        run.run_id,
        snapshot_for(store, run, observed_at=future_observation.isoformat()),
    )

    state = store.source_state(source_id)
    completed = store.discovery_run(run.run_id)
    health = state.runtime_health
    assert len(store.list_resources(source_id)) == 1
    assert completed.lifecycle.value == "completed"
    assert completed.provider_outcome == "success"
    assert state.source.last_committed_run_sequence == run.discovery_run_sequence
    assert health.last_successful_observed_at == future_observation.isoformat()
    assert health.freshness_reference_at == future_observation.isoformat()
    assert health.freshness_valid_until == (
        future_observation + timedelta(seconds=300)
    ).isoformat()
    assert health.health.value == "healthy"
    assert health.freshness.value == "stale"
    assert health.health_origin.value == "discovery_run"
    assert health.health_reason == "clock_anomaly_future_observation"
    assert not authority.source_is_fresh_for_future_mutation(source_id)

    before_catch_up = store.backend_instance()
    clock.value = future_observation + timedelta(minutes=1)
    assert not authority.source_is_fresh_for_future_mutation(source_id)
    assert store.backend_instance() == before_catch_up


def test_backward_clock_before_finalization_fails_closed_without_later_revival(
    tmp_path: Path,
) -> None:
    clock, store, authority, source_id = make_authority(tmp_path, duration=300)
    run = authority.issue_discovery_run(source_id, 1)
    clock.value = START - timedelta(minutes=10)
    authority.finalize_successful_discovery_run(
        source_id,
        run.run_id,
        snapshot_for(store, run, observed_at=START.isoformat()),
    )
    health = store.source_state(source_id).runtime_health
    assert health.freshness_reference_at == START.isoformat()
    assert health.freshness_valid_until == (START + timedelta(seconds=300)).isoformat()
    assert health.freshness.value == "stale"
    assert health.health_origin.value == "discovery_run"
    assert health.health_reason == "clock_anomaly_future_observation"

    clock.value = START + timedelta(minutes=1)
    assert not authority.source_is_fresh_for_future_mutation(source_id)
    assert store.source_state(source_id).runtime_health == health


def test_newer_success_replaces_expired_anchor_and_old_timer_is_noop(tmp_path: Path) -> None:
    clock, store, authority, source_id = make_authority(tmp_path, duration=60)
    first = successful_run(store, authority, source_id)
    old_deadline = store.source_state(source_id).runtime_health.freshness_valid_until
    clock.value = START + timedelta(seconds=70)
    second = successful_run(store, authority, source_id, observed_at=clock.value.isoformat())
    before = store.backend_instance()
    assert not authority.materialize_due_expiry(
        source_id,
        expected_run_sequence=first.discovery_run_sequence,
        expected_deadline=old_deadline,
    )
    assert store.backend_instance() == before
    health = store.source_state(source_id).runtime_health
    assert health.last_health_run_sequence == second.discovery_run_sequence
    assert health.freshness.value == "fresh"


@pytest.mark.parametrize("changed_field", ["config", "trust"])
def test_late_old_context_worker_is_audited_but_cannot_commit(tmp_path: Path, changed_field: str) -> None:
    _, store, authority, source_id = make_authority(tmp_path)
    run = authority.issue_discovery_run(source_id, 1)
    # Simulate an independently serialized context commit to exercise finalization's
    # defense-in-depth CAS; public transition methods reject while this run owns source.
    with store._transaction() as connection:
        if changed_field == "config":
            connection.execute(
                "UPDATE inventory_sources SET source_config_revision=source_config_revision+1 "
                "WHERE inventory_source_id=?",
                (source_id,),
            )
        else:
            connection.execute(
                "UPDATE source_endpoints SET transport_trust_revision=transport_trust_revision+1 "
                "WHERE inventory_source_id=?",
                (source_id,),
            )
        authority._mark_controlled_context_transition(
            connection, source_id, f"test_{changed_field}_transition"
        )
    before = store.backend_instance()
    with pytest.raises(AuthorityConflict, match="context changed"):
        authority.finalize_successful_discovery_run(source_id, run.run_id, snapshot_for(store, run))
    assert store.list_resources(source_id) == ()
    assert store.discovery_run(run.run_id).provider_outcome == "invalid"
    assert store.source_state(source_id).source.active_discovery_run_id is None
    assert store.backend_instance().inventory_revision == before.inventory_revision


def test_context_transition_is_controlled_and_serialized_with_run_owner(tmp_path: Path) -> None:
    _, store, authority, source_id = make_authority(tmp_path)
    run = authority.issue_discovery_run(source_id, 1)
    with pytest.raises(AuthorityConflict, match="waits for active run"):
        authority.rotate_credential_reference(source_id, "secret://inventory/rotated")
    authority.abandon_discovery_run(source_id, run.run_id, reason="operator fence")
    before = store.source_state(source_id)
    after = authority.rotate_credential_reference(source_id, "secret://inventory/rotated")
    assert after.source.source_config_revision == before.source.source_config_revision + 1
    assert after.runtime_health.health_origin.value == "controlled_context_transition"
    assert after.runtime_health.latest_completed_run_sequence is None


def test_success_transaction_rolls_back_reconciliation_on_internal_failure(tmp_path: Path) -> None:
    class FailingAuthority(InventoryAuthority):
        def _after_reconciliation(self, connection, snapshot):
            raise RuntimeError("injected after reconciliation")

    _, store, authority, source_id = make_authority(tmp_path, authority_type=FailingAuthority)
    run = authority.issue_discovery_run(source_id, 1)
    before = store.backend_instance()
    with pytest.raises(RuntimeError, match="injected"):
        authority.finalize_successful_discovery_run(source_id, run.run_id, snapshot_for(store, run))
    assert store.list_resources(source_id) == ()
    assert store.discovery_run(run.run_id).lifecycle.value == "issued"
    assert store.source_state(source_id).source.active_discovery_run_id == run.run_id
    assert store.backend_instance() == before


def test_success_transaction_rolls_back_health_before_owner_release(tmp_path: Path) -> None:
    class FailingAfterHealth(InventoryAuthority):
        def _after_health_update(self, connection, snapshot):
            raise RuntimeError("injected after health")

    _, store, authority, source_id = make_authority(tmp_path, authority_type=FailingAfterHealth)
    run = authority.issue_discovery_run(source_id, 1)
    before = store.source_state(source_id)
    with pytest.raises(RuntimeError, match="injected after health"):
        authority.finalize_successful_discovery_run(source_id, run.run_id, snapshot_for(store, run))
    after = store.source_state(source_id)
    assert after.runtime_health == before.runtime_health
    assert after.source.active_discovery_run_id == run.run_id
    assert store.discovery_run(run.run_id).lifecycle.value == "issued"


def test_canonicalization_namespace_migration_preserves_endpoint_identity(tmp_path: Path) -> None:
    clock = Clock()
    store = InventoryAuthorityStore(tmp_path / "authority.db", now=clock)
    authority = InventoryAuthority(
        store,
        now=clock,
        _test_migration_contracts={2: lambda locator: locator.replace("pve.example", "PVE.EXAMPLE") + "/"},
    )
    source = authority.create_inventory_source(
        provider_kind="proxmox",
        display_name="Primary",
        credential_reference="secret://inventory/primary",
        transport_locator="https://pve.example:8006",
    )
    source_id = source.source.inventory_source_id
    endpoint_id = source.active_endpoint.endpoint_id
    migrated = authority.migrate_canonicalization_contract(source_id, 2)
    assert migrated.active_endpoint.endpoint_id == endpoint_id
    assert migrated.active_endpoint.canonicalization_contract_version == 2
    assert migrated.active_endpoint.canonical_transport_locator == "https://PVE.EXAMPLE:8006/"
    assert migrated.source.source_config_revision == 2
    with store._read_transaction() as connection:
        row = connection.execute("SELECT * FROM canonicalization_migrations").fetchone()
    assert row["endpoint_id"] == endpoint_id


def test_canonicalization_migration_collision_rolls_back_atomically(tmp_path: Path) -> None:
    clock, store, base, source_id = make_authority(tmp_path)
    state = store.source_state(source_id)
    with store._transaction() as connection:
        connection.execute(
            "INSERT INTO source_endpoints VALUES(?, ?, ?, 1, 'retired', 1, ?)",
            (
                "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                source_id,
                "https://retired.example:8006",
                START.isoformat(),
            ),
        )
    authority = InventoryAuthority(
        store, now=clock, _test_migration_contracts={2: lambda locator: "https://same.example"}
    )
    with pytest.raises(AuthorityConflict, match="collision"):
        authority.migrate_canonicalization_contract(source_id, 2)
    after = store.source_state(source_id)
    assert after.active_endpoint == state.active_endpoint
    with store._read_transaction() as connection:
        assert connection.execute("SELECT COUNT(*) FROM canonicalization_migrations").fetchone()[0] == 0
