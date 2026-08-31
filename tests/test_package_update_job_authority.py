from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sqlite3
import threading
import uuid

import pytest

from app.inventory import (
    AuthorityConflict,
    InventoryAuthority,
    InventoryAuthorityStore,
    PackageScanFailure,
    PackageScanPackage,
    PackageUpdateCheckpoint,
    PackageUpdateEventType,
    PackageUpdateJobStatus,
)
from tests.test_package_plan_approval import _successful_plan
from tests.test_package_scan_authority import START, _packages, _reconcile, _system


def _approved_system(tmp_path: Path, *, packages=None, resource_type: str = "lxc"):
    clock, store, authority, resource = _system(
        tmp_path, resource_type=resource_type
    )
    if resource_type != "lxc":
        return clock, store, authority, resource, None, None
    scan = _successful_plan(authority, resource.resource_id, packages)
    approval = authority.approve_package_plan(
        resource.resource_id, scan.scan_run_id, scan.plan_fingerprint
    )
    return clock, store, authority, resource, scan, approval


def _add_approved_resource(store, authority):
    source = authority.create_inventory_source(
        provider_kind="proxmox_ve",
        display_name="Secondary",
        credential_reference="secret://pve-secondary",
        transport_locator="https://pve-secondary.example:8006",
    )
    _reconcile(authority, source.source.inventory_source_id)
    resource = next(
        item
        for item in store.list_resources()
        if item.inventory_source_id == source.source.inventory_source_id
    )
    scan = _successful_plan(authority, resource.resource_id)
    approval = authority.approve_package_plan(
        resource.resource_id, scan.scan_run_id, scan.plan_fingerprint
    )
    return resource, scan, approval


def _issue(authority, resource, approval, request_id=None):
    return authority.issue_package_update_job(
        resource.resource_id,
        approval.approval_id,
        request_id or str(uuid.uuid4()),
    )


def test_job_issuance_copies_immutable_authority_and_packages_and_reopens(
    tmp_path: Path,
) -> None:
    clock, store, authority, resource, scan, approval = _approved_system(tmp_path)
    request_id = str(uuid.uuid4())
    published_revision = store.backend_instance().published_state_revision

    job = _issue(authority, resource, approval, request_id)

    source = store.source_state(resource.inventory_source_id)
    assert job.request_id == request_id
    assert job.resource_id == resource.resource_id
    assert job.approval_id == approval.approval_id
    assert job.approval_reviewed_scan_run_id == approval.reviewed_scan_run_id
    assert job.approved_plan_fingerprint == approval.approved_plan_fingerprint
    assert job.approval_approved_at == approval.approved_at
    assert job.current_plan_scan_run_id == scan.scan_run_id
    assert job.inventory_source_id == resource.inventory_source_id
    assert job.committed_source_config_revision == source.source.source_config_revision
    assert job.committed_endpoint_id == source.active_endpoint.endpoint_id
    assert (
        job.committed_canonical_transport_locator
        == source.active_endpoint.canonical_transport_locator
    )
    assert (
        job.committed_canonicalization_contract_version
        == source.active_endpoint.canonicalization_contract_version
    )
    assert (
        job.committed_transport_trust_revision
        == source.active_endpoint.transport_trust_revision
    )
    assert job.expected_resource_type == "lxc"
    assert job.expected_binding_id == resource.active_binding_id
    assert job.expected_locator_generation == resource.locator_generation
    assert (
        job.expected_resource_continuity_revision
        == resource.resource_continuity_revision
    )
    assert job.expected_vmid == resource.vmid
    assert job.expected_node_id == resource.current_node_id
    assert job.expected_node_name == "pve-a"
    assert job.status is PackageUpdateJobStatus.ACTIVE
    assert job.checkpoint is PackageUpdateCheckpoint.ISSUED
    assert store.backend_instance().published_state_revision == published_revision
    assert [package.package_name for package in job.packages] == [
        package.package_name for package in scan.packages
    ]
    assert job.package_count == len(job.packages) == 2

    events = store.list_package_update_job_events(job.job_id)
    assert len(events) == 1
    assert events[0].sequence == 1
    assert events[0].event_type is PackageUpdateEventType.JOB_ISSUED

    path = store.path
    store.close()
    reopened = InventoryAuthorityStore(path, now=clock)
    assert reopened.package_update_job(job.job_id) == job
    assert reopened.list_package_update_job_events(job.job_id) == events


def test_same_plan_newer_scan_preserves_reviewed_provenance_and_copies_current_rows(
    tmp_path: Path,
) -> None:
    _, store, authority, resource, reviewed, approval = _approved_system(tmp_path)
    current_packages = tuple(
        replace(
            package,
            origin="current scan audit origin",
            description="current scan presentation metadata",
        )
        for package in _packages()
    )
    current = _successful_plan(authority, resource.resource_id, current_packages)
    assert current.scan_run_id != reviewed.scan_run_id
    assert current.plan_fingerprint == reviewed.plan_fingerprint

    job = _issue(authority, resource, approval)

    assert job.approval_reviewed_scan_run_id == reviewed.scan_run_id
    assert job.current_plan_scan_run_id == current.scan_run_id
    assert all(
        package.origin == "current scan audit origin" for package in job.packages
    )
    assert store.package_plan_approval(resource.resource_id) == approval


def test_request_id_retry_is_durable_and_precedes_changed_live_authority(
    tmp_path: Path,
) -> None:
    _, store, authority, resource, _, approval = _approved_system(tmp_path)
    request_id = str(uuid.uuid4())
    first = _issue(authority, resource, approval, request_id)
    assert authority.recover_interrupted_package_update_jobs() == (first.job_id,)
    authority.rotate_transport_trust(resource.inventory_source_id)

    retry = _issue(authority, resource, approval, request_id)

    assert retry == store.package_update_job(first.job_id)
    assert retry.status is PackageUpdateJobStatus.INTERRUPTED
    assert len(store.list_package_update_jobs()) == 1
    assert len(retry.packages) == 2
    assert [event.event_type for event in store.list_package_update_job_events(first.job_id)] == [
        PackageUpdateEventType.JOB_ISSUED,
        PackageUpdateEventType.RESTART_INTERRUPTED,
    ]


def test_request_id_collision_fails_for_another_resource_or_approval(
    tmp_path: Path,
) -> None:
    _, store, authority, resource, _, approval = _approved_system(tmp_path)
    request_id = str(uuid.uuid4())
    _issue(authority, resource, approval, request_id)
    other_resource, _, other_approval = _add_approved_resource(store, authority)

    with pytest.raises(AuthorityConflict, match="another package update request"):
        _issue(authority, other_resource, other_approval, request_id)
    with pytest.raises(AuthorityConflict, match="another package update request"):
        authority.issue_package_update_job(
            resource.resource_id, str(uuid.uuid4()), request_id
        )
    assert len(store.list_package_update_jobs()) == 1


def test_global_single_flight_blocks_other_resource_but_recovery_releases_slot(
    tmp_path: Path,
) -> None:
    _, store, authority, resource, _, approval = _approved_system(tmp_path)
    other_resource, _, other_approval = _add_approved_resource(store, authority)
    first = _issue(authority, resource, approval)

    with pytest.raises(AuthorityConflict, match="global slot"):
        _issue(authority, other_resource, other_approval)
    assert _issue(authority, resource, approval, first.request_id) == first

    assert authority.recover_interrupted_package_update_jobs() == (first.job_id,)
    second = _issue(authority, other_resource, other_approval)
    assert second.resource_id == other_resource.resource_id
    assert len(store.list_package_update_jobs()) == 2


def test_concurrent_new_requests_cannot_create_two_active_jobs(tmp_path: Path) -> None:
    clock, store, authority, resource, _, approval = _approved_system(tmp_path)
    other_resource, _, other_approval = _add_approved_resource(store, authority)
    barrier = threading.Barrier(2)
    results = []
    lock = threading.Lock()

    def issue(resource_id, approval_id):
        contender = InventoryAuthority(store, now=clock)
        barrier.wait()
        try:
            result = contender.issue_package_update_job(
                resource_id, approval_id, str(uuid.uuid4())
            )
        except AuthorityConflict as exc:
            result = exc
        with lock:
            results.append(result)

    threads = [
        threading.Thread(
            target=issue, args=(resource.resource_id, approval.approval_id)
        ),
        threading.Thread(
            target=issue,
            args=(other_resource.resource_id, other_approval.approval_id),
        ),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    assert sum(not isinstance(result, Exception) for result in results) == 1
    assert sum(isinstance(result, AuthorityConflict) for result in results) == 1
    assert sum(job.status is PackageUpdateJobStatus.ACTIVE for job in store.list_package_update_jobs()) == 1


def test_issuance_failure_rolls_back_job_packages_event_and_slot(tmp_path: Path) -> None:
    clock, store, authority, resource, _, approval = _approved_system(tmp_path)

    class FailingAuthority(InventoryAuthority):
        def _after_package_update_job_issuance(self, connection, *, job_id):
            raise RuntimeError("injected issuance failure")

    with pytest.raises(RuntimeError, match="injected issuance"):
        _issue(FailingAuthority(store, now=clock), resource, approval)

    counts = store.record_counts()
    assert counts["package_update_jobs"] == 0
    assert counts["package_update_job_packages"] == 0
    assert counts["package_update_job_events"] == 0
    assert _issue(authority, resource, approval).status is PackageUpdateJobStatus.ACTIVE


def test_issuance_requires_current_approval_and_matching_approval_id(
    tmp_path: Path,
) -> None:
    _, store, authority, resource = _system(tmp_path)
    with pytest.raises(AuthorityConflict, match="current package plan approval"):
        authority.issue_package_update_job(
            resource.resource_id, str(uuid.uuid4()), str(uuid.uuid4())
        )
    scan = _successful_plan(authority, resource.resource_id)
    approval = authority.approve_package_plan(
        resource.resource_id, scan.scan_run_id, scan.plan_fingerprint
    )
    with pytest.raises(AuthorityConflict, match="approval_id"):
        authority.issue_package_update_job(
            resource.resource_id, str(uuid.uuid4()), str(uuid.uuid4())
        )
    assert store.package_plan_approval(resource.resource_id) == approval


def test_changed_plan_and_failed_or_interrupted_latest_attempt_fail_issuance(
    tmp_path: Path,
) -> None:
    for case in ("changed", "failed", "interrupted"):
        case_path = tmp_path / case
        case_path.mkdir()
        _, store, authority, resource, _, approval = _approved_system(case_path)
        if case == "changed":
            _successful_plan(
                authority,
                resource.resource_id,
                (PackageScanPackage("apt", "2.6.1", "2.6.9"),),
            )
            match = "does not match"
        else:
            latest = authority.issue_package_scan(resource.resource_id)
            if case == "failed":
                authority.finalize_failed_package_scan(
                    latest.scan_run_id,
                    failure_class=PackageScanFailure.GUEST_UNAVAILABLE,
                    error_message="unavailable",
                )
            else:
                authority.recover_interrupted_package_scans()
            match = "latest package scan"
        with pytest.raises(AuthorityConflict, match=match):
            _issue(authority, resource, approval)
        assert store.list_package_update_jobs() == ()


@pytest.mark.parametrize(
    "mutation",
    (
        lambda clock, authority, resource: setattr(
            clock, "value", START.replace(minute=6)
        ),
        lambda clock, authority, resource: authority.rotate_transport_trust(
            resource.inventory_source_id
        ),
        lambda clock, authority, resource: authority.rotate_credential_reference(
            resource.inventory_source_id, "secret://rotated"
        ),
    ),
)
def test_stale_or_changed_source_context_fails_issuance(
    tmp_path: Path, mutation
) -> None:
    clock, store, authority, resource, _, approval = _approved_system(tmp_path)
    mutation(clock, authority, resource)

    with pytest.raises(AuthorityConflict, match="stale"):
        _issue(authority, resource, approval)
    assert store.list_package_update_jobs() == ()


def test_unhealthy_source_fails_issuance(tmp_path: Path) -> None:
    _, store, authority, resource, _, approval = _approved_system(tmp_path)
    from tests.test_package_scan_authority import _finalize_discovery_failure
    from app.inventory.discovery import BaselineCompleteness

    _finalize_discovery_failure(
        authority, resource.inventory_source_id, BaselineCompleteness.SOURCE_UNAVAILABLE
    )
    with pytest.raises(AuthorityConflict, match="stale"):
        _issue(authority, resource, approval)
    assert store.list_package_update_jobs() == ()


def test_binding_generation_continuity_and_replacement_fail_issuance(
    tmp_path: Path,
) -> None:
    for case in ("binding", "continuity", "replacement"):
        case_path = tmp_path / case
        case_path.mkdir()
        _, store, authority, resource, _, approval = _approved_system(case_path)
        if case == "binding":
            with store._transaction() as connection:
                old = connection.execute(
                    "SELECT * FROM resource_locator_bindings WHERE resource_id=? "
                    "AND valid_to_run_sequence IS NULL",
                    (resource.resource_id,),
                ).fetchone()
                connection.execute(
                    "UPDATE resource_locator_bindings SET valid_to_run_sequence=2, "
                    "closure_reason='test_rebind' WHERE binding_id=?",
                    (old["binding_id"],),
                )
                connection.execute(
                    "INSERT INTO resource_locator_bindings("
                    "binding_id, inventory_source_id, vmid, locator_generation, "
                    "resource_id, valid_from_run_sequence) VALUES(?, ?, ?, ?, ?, 2)",
                    (
                        str(uuid.uuid4()),
                        resource.inventory_source_id,
                        resource.vmid,
                        resource.locator_generation + 1,
                        resource.resource_id,
                    ),
                )
        elif case == "continuity":
            with store._transaction() as connection:
                connection.execute(
                    "UPDATE resource_incarnations SET resource_continuity_revision="
                    "resource_continuity_revision+1 WHERE resource_id=?",
                    (resource.resource_id,),
                )
        else:
            _reconcile(
                authority, resource.inventory_source_id, resource_type="qemu"
            )

        with pytest.raises(AuthorityConflict, match="stale|current executable"):
            _issue(authority, resource, approval)
        assert store.list_package_update_jobs() == ()


def test_unsupported_type_and_empty_exact_plan_fail_issuance(tmp_path: Path) -> None:
    qemu_path = tmp_path / "qemu"
    qemu_path.mkdir()
    _, qemu_store, qemu_authority, qemu, _, _ = _approved_system(
        qemu_path, resource_type="qemu"
    )
    with pytest.raises(AuthorityConflict, match="support LXC"):
        qemu_authority.issue_package_update_job(
            qemu.resource_id, str(uuid.uuid4()), str(uuid.uuid4())
        )
    assert qemu_store.list_package_update_jobs() == ()

    empty_path = tmp_path / "empty"
    empty_path.mkdir()
    _, store, authority, resource, _, approval = _approved_system(
        empty_path, packages=()
    )
    with pytest.raises(AuthorityConflict, match="non-empty"):
        _issue(authority, resource, approval)
    assert store.list_package_update_jobs() == ()


def test_revalidation_allows_unchanged_and_newer_same_exact_plan_without_old_approval(
    tmp_path: Path,
) -> None:
    _, store, authority, resource, _, approval = _approved_system(tmp_path)
    job = _issue(authority, resource, approval)
    assert authority.revalidate_package_update_job(job.job_id) == job

    current = _successful_plan(authority, resource.resource_id)
    replacement = authority.approve_package_plan(
        resource.resource_id, current.scan_run_id, current.plan_fingerprint
    )
    assert replacement.approval_id != job.approval_id
    revalidated = authority.revalidate_package_update_job(job.job_id)
    assert revalidated.job_id == job.job_id
    assert revalidated.approval_id == approval.approval_id
    assert revalidated.approval_reviewed_scan_run_id == approval.reviewed_scan_run_id


def test_revalidation_fails_changed_or_failed_latest_plan(tmp_path: Path) -> None:
    for case in ("changed", "failed"):
        case_path = tmp_path / case
        case_path.mkdir()
        _, _, authority, resource, _, approval = _approved_system(case_path)
        job = _issue(authority, resource, approval)
        if case == "changed":
            _successful_plan(
                authority,
                resource.resource_id,
                (PackageScanPackage("apt", "2.6.1", "2.6.9"),),
            )
        else:
            latest = authority.issue_package_scan(resource.resource_id)
            authority.finalize_failed_package_scan(
                latest.scan_run_id,
                failure_class=PackageScanFailure.GUEST_UNAVAILABLE,
                error_message="unavailable",
            )
        with pytest.raises(AuthorityConflict):
            authority.revalidate_package_update_job(job.job_id)


def test_revalidation_fails_source_and_resource_target_changes(tmp_path: Path) -> None:
    for case in ("source", "continuity", "binding", "status", "node"):
        case_path = tmp_path / case
        case_path.mkdir()
        _, store, authority, resource, _, approval = _approved_system(case_path)
        job = _issue(authority, resource, approval)
        if case == "source":
            authority.rotate_transport_trust(resource.inventory_source_id)
        elif case == "continuity":
            with store._transaction() as connection:
                connection.execute(
                    "UPDATE resource_incarnations SET resource_continuity_revision="
                    "resource_continuity_revision+1 WHERE resource_id=?",
                    (resource.resource_id,),
                )
        elif case == "binding":
            with store._transaction() as connection:
                old = connection.execute(
                    "SELECT * FROM resource_locator_bindings WHERE resource_id=? "
                    "AND valid_to_run_sequence IS NULL",
                    (resource.resource_id,),
                ).fetchone()
                connection.execute(
                    "UPDATE resource_locator_bindings SET valid_to_run_sequence=2, "
                    "closure_reason='test_rebind' WHERE binding_id=?",
                    (old["binding_id"],),
                )
                connection.execute(
                    "INSERT INTO resource_locator_bindings("
                    "binding_id, inventory_source_id, vmid, locator_generation, "
                    "resource_id, valid_from_run_sequence) VALUES(?, ?, ?, ?, ?, 2)",
                    (
                        str(uuid.uuid4()),
                        resource.inventory_source_id,
                        resource.vmid,
                        resource.locator_generation + 1,
                        resource.resource_id,
                    ),
                )
        elif case == "status":
            with store._transaction() as connection:
                connection.execute(
                    "UPDATE resource_incarnations SET status='stopped' "
                    "WHERE resource_id=?",
                    (resource.resource_id,),
                )
        else:
            with store._transaction() as connection:
                new_node_id = str(uuid.uuid4())
                connection.execute(
                    "INSERT INTO inventory_nodes("
                    "node_id, inventory_source_id, external_node_name, status, "
                    "available, facts_json, created_at, updated_at) "
                    "VALUES(?, ?, 'pve-b', 'online', 1, '{}', ?, ?)",
                    (
                        new_node_id,
                        resource.inventory_source_id,
                        START.isoformat(),
                        START.isoformat(),
                    ),
                )
                connection.execute(
                    "UPDATE resource_incarnations SET current_node_id=?, "
                    "last_known_node_id=? WHERE resource_id=?",
                    (new_node_id, new_node_id, resource.resource_id),
                )
        with pytest.raises(AuthorityConflict, match="context"):
            authority.revalidate_package_update_job(job.job_id)


def test_revalidation_compares_current_material_to_immutable_job_material(
    tmp_path: Path,
) -> None:
    _, store, authority, resource, _, approval = _approved_system(tmp_path)
    job = _issue(authority, resource, approval)
    with store._transaction() as connection:
        connection.execute(
            "DROP TRIGGER package_update_job_package_update_immutable"
        )
        connection.execute(
            "UPDATE package_update_job_packages SET candidate_version='tampered' "
            "WHERE job_id=? AND package_index=0",
            (job.job_id,),
        )
    with pytest.raises(AuthorityConflict, match="material"):
        authority.revalidate_package_update_job(job.job_id)


def _assert_sql_rejected(store, statement, params=()):
    with pytest.raises(sqlite3.IntegrityError):
        with store._transaction() as connection:
            connection.execute(statement, params)


def test_sql_enforces_job_package_event_and_scan_immutability(tmp_path: Path) -> None:
    _, store, authority, resource, scan, approval = _approved_system(tmp_path)
    job = _issue(authority, resource, approval)

    _assert_sql_rejected(
        store,
        "UPDATE package_update_jobs SET approval_id=? WHERE job_id=?",
        (str(uuid.uuid4()), job.job_id),
    )
    _assert_sql_rejected(
        store, "DELETE FROM package_update_jobs WHERE job_id=?", (job.job_id,)
    )
    _assert_sql_rejected(
        store,
        "UPDATE package_update_job_packages SET candidate_version='x' "
        "WHERE job_id=? AND package_index=0",
        (job.job_id,),
    )
    _assert_sql_rejected(
        store,
        "DELETE FROM package_update_job_packages WHERE job_id=? AND package_index=0",
        (job.job_id,),
    )
    _assert_sql_rejected(
        store,
        "INSERT INTO package_update_job_packages("
        "job_id, package_index, package_name, installed_version, candidate_version) "
        "VALUES(?, 99, 'later', '1', '2')",
        (job.job_id,),
    )
    _assert_sql_rejected(
        store,
        "UPDATE package_update_job_events SET message='changed' "
        "WHERE job_id=? AND sequence=1",
        (job.job_id,),
    )
    _assert_sql_rejected(
        store,
        "DELETE FROM package_update_job_events WHERE job_id=? AND sequence=1",
        (job.job_id,),
    )
    _assert_sql_rejected(
        store,
        "UPDATE package_scan_packages SET candidate_version='x' "
        "WHERE scan_run_id=? AND package_index=0",
        (scan.scan_run_id,),
    )
    _assert_sql_rejected(
        store,
        "DELETE FROM package_scan_packages WHERE scan_run_id=? AND package_index=0",
        (scan.scan_run_id,),
    )
    _assert_sql_rejected(
        store,
        "INSERT INTO package_scan_packages("
        "scan_run_id, package_index, package_name, installed_version, candidate_version) "
        "VALUES(?, 99, 'later', '1', '2')",
        (scan.scan_run_id,),
    )
    with pytest.raises(ValueError, match="event limit"):
        store.list_package_update_job_events(job.job_id, limit=201)


def test_restart_recovery_is_atomic_idempotent_and_releases_global_slot(
    tmp_path: Path,
) -> None:
    _, store, authority, resource, _, approval = _approved_system(tmp_path)
    other_resource, _, other_approval = _add_approved_resource(store, authority)
    job = _issue(authority, resource, approval)
    published_revision = store.backend_instance().published_state_revision

    assert authority.recover_interrupted_package_update_jobs() == (job.job_id,)
    recovered = store.package_update_job(job.job_id)
    assert recovered.status is PackageUpdateJobStatus.INTERRUPTED
    assert recovered.terminalized_at is not None
    assert "before package mutation began" in recovered.terminal_reason
    assert store.backend_instance().published_state_revision == published_revision
    assert authority.recover_interrupted_package_update_jobs() == ()
    assert len(store.list_package_update_job_events(job.job_id)) == 2

    replacement = _issue(authority, other_resource, other_approval)
    assert replacement.status is PackageUpdateJobStatus.ACTIVE


def test_production_composition_runs_safe_job_recovery_before_schedulers(
    tmp_path: Path,
) -> None:
    _, store, authority, resource, _, approval = _approved_system(tmp_path)
    job = _issue(authority, resource, approval)
    db_path = store.path
    store.close()

    from app.inventory_runtime import create_read_only_app
    from app.inventory_runtime_config import parse_r0_runtime_config

    config = parse_r0_runtime_config(
        {
            "source": {
                "display_name": "Primary",
                "provider_kind": "proxmox_ve",
                "pve_endpoint": "https://pve.example:8006",
                "freshness_duration_seconds": 300,
                "credential_reference": "secret://pve",
                "pve_token_env": "TEST_PVE_TOKEN",
                "tls": {"verify": True, "ca_bundle_path": None},
            },
            "runtime": {
                "authority_db_path": str(db_path),
                "api_token_env": "TEST_API_TOKEN",
            },
        },
        env={
            "TEST_PVE_TOKEN": (
                "root@pam!hubinet-ops="
                "00000000-0000-0000-0000-000000000000"
            ),
            "TEST_API_TOKEN": "a" * 32,
        },
    )
    app = create_read_only_app(config, start_scheduler=False)
    try:
        assert (
            app.state.store.package_update_job(job.job_id).status
            is PackageUpdateJobStatus.INTERRUPTED
        )
        assert len(app.state.store.list_package_update_job_events(job.job_id)) == 2
    finally:
        app.state.package_scan_scheduler.stop()
        app.state.scheduler.stop()
        app.state.store.close()


def test_reserved_mutation_intent_state_is_not_replayed_or_silently_cleared(
    tmp_path: Path,
) -> None:
    _, store, authority, resource, _, approval = _approved_system(tmp_path)
    other_resource, _, other_approval = _add_approved_resource(store, authority)
    job = _issue(authority, resource, approval)
    identity = authority.package_update_snapshot_identity(job.job_id)
    with store._transaction() as connection:
        # Schema v10 makes mutation_may_have_started reachable only from a
        # coherent confirmed-snapshot state, so the fixture has to establish
        # the whole durable prefix rather than only the mutation checkpoint.
        connection.execute(
            "UPDATE package_update_jobs SET checkpoint='mutation_may_have_started', "
            "snapshot_operation_id=?, snapshot_name=?, "
            "snapshot_intent_recorded_at=?, snapshot_confirmed_at=?, "
            "mutation_may_have_started_at=? WHERE job_id=?",
            (
                identity.snapshot_operation_id,
                identity.snapshot_name,
                START.isoformat(),
                START.isoformat(),
                START.isoformat(),
                job.job_id,
            ),
        )

    assert authority.recover_interrupted_package_update_jobs() == ()
    preserved = store.package_update_job(job.job_id)
    assert preserved.status is PackageUpdateJobStatus.ACTIVE
    assert preserved.checkpoint is PackageUpdateCheckpoint.MUTATION_MAY_HAVE_STARTED
    assert preserved.mutation_may_have_started_at == START.isoformat()
    assert len(store.list_package_update_job_events(job.job_id)) == 1
    with pytest.raises(AuthorityConflict, match="global slot"):
        _issue(authority, other_resource, other_approval)


def test_terminal_job_cannot_be_reactivated_or_reterminalized(tmp_path: Path) -> None:
    _, store, authority, resource, _, approval = _approved_system(tmp_path)
    job = _issue(authority, resource, approval)
    authority.recover_interrupted_package_update_jobs()
    _assert_sql_rejected(
        store,
        "UPDATE package_update_jobs SET status='active', terminalized_at=NULL, "
        "terminal_reason=NULL WHERE job_id=?",
        (job.job_id,),
    )
    _assert_sql_rejected(
        store,
        "UPDATE package_update_jobs SET status='failed' WHERE job_id=?",
        (job.job_id,),
    )
