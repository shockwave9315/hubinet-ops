"""Job-bound healthcheck execution: freezing, evaluation, verdicts, recovery.

Covers the last missing half of the update lifecycle -- the boundary between a
package mutation that was proven complete and a job that may finally be called
successful:

- the health contract generation a job FREEZES at issuance, and the refusal to
  issue a job for a resource that has not declared one;
- pre-mutation drift: an edited, cleared, or clear-and-recreated contract makes
  the old job stale and forbids the real package mutation;
- post-mutation immutability: the same edits change nothing about what the job
  must satisfy;
- the ALL-OF aggregation, and the three genuinely different answers PASS, FAIL
  and UNKNOWN;
- the ONE legal success transition, and the SQL that makes every other route to
  `succeeded` unstorable;
- same-job rollback from the health branch, without pretending health passed;
- the fixed argv of all three probe kinds against a fake guest, including every
  way an option-like, glob-like, or ambiguous target must fail to produce a
  PASS.

Nothing here runs a real `pvesh`, `pct`, `ssh`, `systemctl`, `docker`, or PVE
operation. The host boundary is the actual dark helper module driven by a fake
guest, with a JSON round trip through the real transport parser.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
from types import ModuleType
import uuid

import pytest

from app.inventory import (
    AuthorityConflict,
    HealthOutcome,
    HealthProbeKind,
    HealthProbeObservation,
    HealthProbeOutcome,
    InventoryAuthorityStore,
    PackageUpdateCheckpoint,
    PackageUpdateEventType,
    PackageUpdateJobStatus,
    ResourceHealthProbe,
    aggregate_health_outcome,
)
from app.package_update_health import (
    HealthStageStatus,
    HostHealthResult,
    HostProbeResult,
    PackageUpdateHealthError,
    PackageUpdateHealthOrchestrator,
    validate_host_health_result,
)
from tests.test_package_update_job_authority import (
    HEALTH_PROBES,
    _approved_system,
    _issue,
)
from tests.test_package_update_snapshot_safety import _canonical


ROOT = Path(__file__).resolve().parents[1]
HELPER_PATH = ROOT / "deploy" / "hubinet-package-health-helper.py"

NODE = "pve-a"


def _load_helper() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "hubinet_package_health_helper", HELPER_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


helper = _load_helper()


# ===========================================================================
# Fixtures: a job driven all the way to a proven package mutation
# ===========================================================================


def _mutated_job(tmp_path: Path, *, health_probes=None):
    """One package-update job at exactly ``mutation_completed``.

    That is the boundary health execution lives on: the package mutation is
    independently proven, and nothing has yet said whether the workload the
    operator cares about actually came back.

    The mutation checkpoints are reached through direct authority SQL rather
    than by driving the real mutation orchestrator, deliberately: this file
    tests the HEALTH boundary, and `tests/test_package_update_mutation.py`
    already proves how a job legitimately gets here. Every health transition
    under test is exercised through its real authority method.
    """

    clock, store, authority, resource, scan, approval = _approved_system(
        tmp_path, health_probes=health_probes
    )
    job = _issue(authority, resource, approval)
    authority.record_package_update_preflight_passed(job.job_id)
    job = authority.record_package_update_snapshot_intent(job.job_id)
    identity = authority.package_update_snapshot_identity(job.job_id)
    ownership = authority.package_update_snapshot_ownership(job.job_id)
    job = authority.confirm_package_update_snapshot(
        job.job_id, _canonical(ownership, identity)
    )
    _force_mutation_completed(store, job.job_id)
    job = authority.package_update_job(job.job_id)
    assert job.checkpoint is PackageUpdateCheckpoint.MUTATION_COMPLETED
    assert job.status is PackageUpdateJobStatus.ACTIVE
    return clock, store, authority, resource, scan, approval, job


def _force_mutation_completed(store, job_id: str) -> None:
    with store._transaction() as connection:
        connection.execute(
            "UPDATE package_update_jobs "
            "SET checkpoint='mutation_may_have_started', "
            "mutation_operation_id=?, mutation_may_have_started_at=?, "
            "accepted_prepared_evidence_digest=? WHERE job_id=?",
            (str(uuid.uuid4()), "2026-02-01T00:00:00+00:00", "a" * 64, job_id),
        )
        connection.execute(
            "UPDATE package_update_jobs SET checkpoint='mutation_completed', "
            "mutation_completed_at=? WHERE job_id=?",
            ("2026-02-01T00:01:00+00:00", job_id),
        )


def _observations(job, outcomes, reasons=None):
    """Build one typed observation per frozen probe, in canonical order."""

    default = {
        HealthProbeOutcome.PASSED: {
            HealthProbeKind.SYSTEMD_UNIT_ACTIVE: "unit_active",
            HealthProbeKind.DOCKER_CONTAINER_RUNNING: "container_running",
            HealthProbeKind.DOCKER_CONTAINER_HEALTHY: "container_healthy",
        },
        HealthProbeOutcome.FAILED: {
            HealthProbeKind.SYSTEMD_UNIT_ACTIVE: "unit_not_active",
            HealthProbeKind.DOCKER_CONTAINER_RUNNING: "container_not_running",
            HealthProbeKind.DOCKER_CONTAINER_HEALTHY: "container_unhealthy",
        },
        HealthProbeOutcome.UNKNOWN: {
            HealthProbeKind.SYSTEMD_UNIT_ACTIVE: "command_failed",
            HealthProbeKind.DOCKER_CONTAINER_RUNNING: "docker_daemon_unavailable",
            HealthProbeKind.DOCKER_CONTAINER_HEALTHY: "docker_daemon_unavailable",
        },
    }
    return tuple(
        HealthProbeObservation(
            probe_index=probe.probe_index,
            kind=probe.kind,
            target=probe.target,
            outcome=outcome,
            reason=(
                reasons[index]
                if reasons is not None
                else default[outcome][probe.kind]
            ),
        )
        for index, (probe, outcome) in enumerate(zip(job.health_probes, outcomes))
    )


def _repair_out_of_band(store, *statements) -> None:
    """Rebuild durable rows the way only a direct-SQL repair could.

    The triggers make every one of these writes impossible through ordinary
    SQL, which is the point: they are dropped for exactly this connection so
    the test can reconstruct a row set the schema forbids, and prove the READ
    path catches it anyway. Per `AGENTS.md` the administrator holding this
    connection is trusted; what is being tested is that Hubinet still refuses
    to make a false statement about their database.
    """

    connection = sqlite3.connect(store.path, isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("BEGIN IMMEDIATE")
        for name in (
            "package_update_job_health_probe_update_immutable",
            "package_update_job_health_probe_delete_immutable",
            "package_update_job_health_probe_insert_during_issuance",
        ):
            connection.execute(f"DROP TRIGGER {name}")
        for statement, parameters in zip(statements[::2], statements[1::2]):
            connection.execute(statement, parameters)
        connection.commit()
    finally:
        connection.close()


def _assert_sql_rejected(store, statement, parameters) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        with store._transaction() as connection:
            connection.execute(statement, parameters)


# ===========================================================================
# 1. Issuance freezes the exact contract generation
# ===========================================================================


def test_issuance_freezes_the_exact_current_contract_generation(
    tmp_path: Path,
) -> None:
    clock, store, authority, resource, scan, approval = _approved_system(tmp_path)
    contract = authority.resource_health_contract(resource.resource_id)

    job = _issue(authority, resource, approval)

    assert job.health_contract_revision == contract.revision
    assert job.health_contract_fingerprint == contract.fingerprint
    assert job.health_contract_probe_count == len(contract.probes) == 2
    assert [
        (probe.probe_index, probe.kind, probe.target) for probe in job.health_probes
    ] == [
        (index, probe.kind, probe.target)
        for index, probe in enumerate(contract.probes)
    ]
    # No verdict, no results, and no health lifecycle facts at issuance.
    assert job.health_started_at is None
    assert job.health_completed_at is None
    assert job.health_outcome is None
    assert job.health_probe_results == ()

    events = store.list_package_update_job_events(job.job_id)
    issued = events[0]
    assert issued.event_type is PackageUpdateEventType.JOB_ISSUED
    assert issued.details["health_contract_revision"] == contract.revision
    assert issued.details["health_contract_fingerprint"] == contract.fingerprint

    # It survives a reopen exactly as every other frozen job fact does.
    path = store.path
    store.close()
    reopened = InventoryAuthorityStore(path, now=clock)
    assert reopened.package_update_job(job.job_id) == job


def test_issuance_refuses_a_resource_with_no_health_contract(
    tmp_path: Path,
) -> None:
    """Absence is not health, so it cannot be the criterion for a job."""

    _, _, authority, resource, _, approval = _approved_system(tmp_path)
    authority.clear_resource_health_contract(resource.resource_id)

    with pytest.raises(AuthorityConflict, match="declared resource health contract"):
        _issue(authority, resource, approval)


def test_a_cleared_contract_leaves_no_job_and_no_orphaned_probe_rows(
    tmp_path: Path,
) -> None:
    _, store, authority, resource, _, approval = _approved_system(tmp_path)
    authority.clear_resource_health_contract(resource.resource_id)

    with pytest.raises(AuthorityConflict):
        _issue(authority, resource, approval)

    counts = store.record_counts()
    assert counts["package_update_jobs"] == 0
    assert counts["package_update_job_health_probes"] == 0


def test_a_replacement_at_the_same_vmid_inherits_no_contract_and_cannot_issue(
    tmp_path: Path,
) -> None:
    """A VMID-reused replacement is a different resource, so it has no
    contract of its own and cannot borrow its predecessor's."""

    from tests.test_package_update_snapshot_safety import (
        _break_incarnation_continuity_at_the_same_locator,
    )

    _, store, authority, resource, scan, approval = _approved_system(tmp_path)
    _break_incarnation_continuity_at_the_same_locator(store, authority)

    # The declaration the predecessor made is not the replacement's to use,
    # and the replacement has made none of its own.
    with pytest.raises(AuthorityConflict):
        _issue(authority, resource, approval)


def test_request_id_retry_returns_the_original_frozen_generation(
    tmp_path: Path,
) -> None:
    """Idempotent retry returns the ORIGINAL job with its ORIGINAL contract,
    even after the operator replaced the live contract in between."""

    _, _, authority, resource, _, approval = _approved_system(tmp_path)
    request_id = str(uuid.uuid4())
    first = _issue(authority, resource, approval, request_id)

    authority.replace_resource_health_contract(
        resource.resource_id,
        (
            ResourceHealthProbe(
                kind=HealthProbeKind.SYSTEMD_UNIT_ACTIVE, target="other.service"
            ),
        ),
    )
    retried = _issue(authority, resource, approval, request_id)

    assert retried == first
    assert retried.health_contract_revision == first.health_contract_revision
    assert retried.health_probes == first.health_probes


def test_the_frozen_contract_is_immutable_in_sql(tmp_path: Path) -> None:
    _, store, authority, resource, _, approval = _approved_system(tmp_path)
    job = _issue(authority, resource, approval)

    for column, value in (
        ("health_contract_revision", 99),
        ("health_contract_fingerprint", "b" * 64),
        ("health_contract_probe_count", 1),
    ):
        _assert_sql_rejected(
            store,
            f"UPDATE package_update_jobs SET {column}=? WHERE job_id=?",
            (value, job.job_id),
        )
    _assert_sql_rejected(
        store,
        "UPDATE package_update_job_health_probes SET target=? WHERE job_id=?",
        ("elsewhere.service", job.job_id),
    )
    _assert_sql_rejected(
        store,
        "DELETE FROM package_update_job_health_probes WHERE job_id=?",
        (job.job_id,),
    )
    _assert_sql_rejected(
        store,
        "INSERT INTO package_update_job_health_probes("
        "job_id, probe_index, kind, target) VALUES(?, ?, ?, ?)",
        (job.job_id, 2, "systemd_unit_active", "extra.service"),
    )


def test_a_job_cannot_be_stored_without_a_frozen_contract(tmp_path: Path) -> None:
    """The three frozen columns are NOT NULL, so "issued with no criterion"
    is unstorable rather than merely unreachable."""

    _, store, authority, resource, _, approval = _approved_system(tmp_path)
    job = _issue(authority, resource, approval)
    for column in (
        "health_contract_revision",
        "health_contract_fingerprint",
        "health_contract_probe_count",
    ):
        _assert_sql_rejected(
            store,
            f"UPDATE package_update_jobs SET {column}=NULL WHERE job_id=?",
            (job.job_id,),
        )


# ===========================================================================
# 2. Contract changes during a job (the §26 matrix)
# ===========================================================================


def _mutation_is_still_permitted(authority, job_id) -> bool:
    """Does the pre-mutation current-authority proof still hold?

    This is the exact gate every pre-mutation transition shares, so proving
    it here proves the real package mutation is forbidden too.
    """

    try:
        authority.revalidate_package_update_job(job_id)
    except AuthorityConflict:
        return False
    return True


def test_A_a_changed_contract_before_mutation_makes_the_job_stale(
    tmp_path: Path,
) -> None:
    _, _, authority, resource, _, approval = _approved_system(tmp_path)
    job = _issue(authority, resource, approval)
    assert _mutation_is_still_permitted(authority, job.job_id)

    authority.replace_resource_health_contract(
        resource.resource_id,
        (
            ResourceHealthProbe(
                kind=HealthProbeKind.SYSTEMD_UNIT_ACTIVE, target="changed.service"
            ),
        ),
    )

    assert not _mutation_is_still_permitted(authority, job.job_id)


def test_B_a_cleared_contract_before_mutation_forbids_mutation(
    tmp_path: Path,
) -> None:
    _, _, authority, resource, _, approval = _approved_system(tmp_path)
    job = _issue(authority, resource, approval)

    authority.clear_resource_health_contract(resource.resource_id)

    assert not _mutation_is_still_permitted(authority, job.job_id)


def test_C_identical_material_recreated_is_a_new_generation_and_stale(
    tmp_path: Path,
) -> None:
    """The reason schema v15 never reuses a revision.

    Clearing and re-declaring byte-identical probes leaves the FINGERPRINT
    unchanged, so a fingerprint-only comparison would call this job current.
    The revision moved, and that is a new generation of the operator's
    declaration -- not a continuation of the one this job froze.
    """

    _, _, authority, resource, _, approval = _approved_system(tmp_path)
    job = _issue(authority, resource, approval)
    before = authority.resource_health_contract(resource.resource_id)

    authority.clear_resource_health_contract(resource.resource_id)
    after = authority.replace_resource_health_contract(
        resource.resource_id, HEALTH_PROBES
    )

    assert after.fingerprint == before.fingerprint == job.health_contract_fingerprint
    assert after.revision == before.revision + 1
    assert not _mutation_is_still_permitted(authority, job.job_id)


def test_D_a_changed_contract_after_mutation_changes_nothing(
    tmp_path: Path,
) -> None:
    _, _, authority, _, _, _, job = _mutated_job(tmp_path)
    frozen = job.health_probes

    authority.replace_resource_health_contract(
        job.resource_id,
        (
            ResourceHealthProbe(
                kind=HealthProbeKind.SYSTEMD_UNIT_ACTIVE, target="moved.service"
            ),
        ),
    )

    started = authority.start_package_update_health(job.job_id)
    assert started.checkpoint is PackageUpdateCheckpoint.HEALTH_STARTED
    assert started.health_probes == frozen
    assert started.health_contract_revision == job.health_contract_revision

    decided = authority.complete_package_update_health(
        job.job_id,
        _observations(started, (HealthProbeOutcome.PASSED,) * len(frozen)),
    )
    assert decided.status is PackageUpdateJobStatus.SUCCEEDED


def test_E_a_cleared_contract_after_mutation_still_evaluates_the_frozen_one(
    tmp_path: Path,
) -> None:
    _, _, authority, _, _, _, job = _mutated_job(tmp_path)
    frozen = job.health_probes

    authority.clear_resource_health_contract(job.resource_id)
    assert authority.resource_health_contract(job.resource_id) is None

    started = authority.start_package_update_health(job.job_id)
    assert started.health_probes == frozen
    decided = authority.complete_package_update_health(
        job.job_id,
        _observations(started, (HealthProbeOutcome.PASSED,) * len(frozen)),
    )
    assert decided.status is PackageUpdateJobStatus.SUCCEEDED
    assert decided.health_outcome is HealthOutcome.PASSED


# ===========================================================================
# 3. The ALL-OF aggregation
# ===========================================================================


@pytest.mark.parametrize(
    ("outcomes", "expected"),
    (
        ((HealthProbeOutcome.PASSED,), HealthOutcome.PASSED),
        (
            (HealthProbeOutcome.PASSED, HealthProbeOutcome.PASSED),
            HealthOutcome.PASSED,
        ),
        (
            (HealthProbeOutcome.FAILED, HealthProbeOutcome.PASSED),
            HealthOutcome.FAILED,
        ),
        (
            (HealthProbeOutcome.FAILED, HealthProbeOutcome.UNKNOWN),
            HealthOutcome.FAILED,
        ),
        (
            (HealthProbeOutcome.PASSED, HealthProbeOutcome.UNKNOWN),
            HealthOutcome.UNKNOWN,
        ),
        (
            (HealthProbeOutcome.UNKNOWN, HealthProbeOutcome.UNKNOWN),
            HealthOutcome.UNKNOWN,
        ),
    ),
)
def test_all_of_aggregation(outcomes, expected) -> None:
    assert aggregate_health_outcome(outcomes) is expected


def test_an_empty_probe_set_is_never_a_pass() -> None:
    """"Zero required things all held" is exactly the false pass this
    product refuses to be able to make."""

    from app.inventory import AuthorityInvariantError

    with pytest.raises(AuthorityInvariantError):
        aggregate_health_outcome(())


def test_a_deterministic_failure_beside_an_unknown_is_still_a_failure(
    tmp_path: Path,
) -> None:
    _, _, authority, _, _, _, job = _mutated_job(tmp_path)
    started = authority.start_package_update_health(job.job_id)

    decided = authority.complete_package_update_health(
        job.job_id,
        _observations(
            started, (HealthProbeOutcome.FAILED, HealthProbeOutcome.UNKNOWN)
        ),
    )

    assert decided.health_outcome is HealthOutcome.FAILED
    assert decided.status is PackageUpdateJobStatus.ACTIVE
    assert [result.outcome for result in decided.health_probe_results] == [
        HealthProbeOutcome.FAILED,
        HealthProbeOutcome.UNKNOWN,
    ]


def test_an_unknown_aggregate_is_refused_by_the_definitive_finalizer(
    tmp_path: Path,
) -> None:
    _, store, authority, _, _, _, job = _mutated_job(tmp_path)
    started = authority.start_package_update_health(job.job_id)

    with pytest.raises(AuthorityConflict, match="unknown health outcome"):
        authority.complete_package_update_health(
            job.job_id,
            _observations(
                started, (HealthProbeOutcome.PASSED, HealthProbeOutcome.UNKNOWN)
            ),
        )

    after = authority.package_update_job(job.job_id)
    assert after.checkpoint is PackageUpdateCheckpoint.HEALTH_STARTED
    assert after.health_completed_at is None
    assert after.health_outcome is None
    assert after.health_probe_results == ()
    assert store.record_counts()["package_update_job_health_probe_results"] == 0


# ===========================================================================
# 4. Observations must describe EXACTLY the frozen contract
# ===========================================================================


def _started(tmp_path: Path):
    _, store, authority, resource, scan, approval, job = _mutated_job(tmp_path)
    started = authority.start_package_update_health(job.job_id)
    return store, authority, started


def test_a_missing_observation_is_refused(tmp_path: Path) -> None:
    store, authority, job = _started(tmp_path)
    full = _observations(job, (HealthProbeOutcome.PASSED,) * 2)
    with pytest.raises(AuthorityConflict, match="cover exactly the frozen probe set"):
        authority.complete_package_update_health(job.job_id, full[:1])


def test_an_extra_observation_is_refused(tmp_path: Path) -> None:
    store, authority, job = _started(tmp_path)
    full = _observations(job, (HealthProbeOutcome.PASSED,) * 2)
    extra = full + (
        HealthProbeObservation(
            probe_index=2,
            kind=HealthProbeKind.SYSTEMD_UNIT_ACTIVE,
            target="extra.service",
            outcome=HealthProbeOutcome.PASSED,
            reason="unit_active",
        ),
    )
    with pytest.raises(AuthorityConflict):
        authority.complete_package_update_health(job.job_id, extra)


def test_a_duplicate_observation_is_refused(tmp_path: Path) -> None:
    store, authority, job = _started(tmp_path)
    full = _observations(job, (HealthProbeOutcome.PASSED,) * 2)
    with pytest.raises(AuthorityConflict, match="duplicate probe index"):
        authority.complete_package_update_health(job.job_id, (full[0], full[0]))


def test_a_wrong_target_is_refused(tmp_path: Path) -> None:
    store, authority, job = _started(tmp_path)
    full = list(_observations(job, (HealthProbeOutcome.PASSED,) * 2))
    full[0] = HealthProbeObservation(
        probe_index=full[0].probe_index,
        kind=full[0].kind,
        target="somewhere-else",
        outcome=HealthProbeOutcome.PASSED,
        reason=full[0].reason,
    )
    with pytest.raises(AuthorityConflict, match="does not describe the frozen probe"):
        authority.complete_package_update_health(job.job_id, tuple(full))


def test_a_wrong_kind_is_refused(tmp_path: Path) -> None:
    store, authority, job = _started(tmp_path)
    full = list(_observations(job, (HealthProbeOutcome.PASSED,) * 2))
    full[0] = HealthProbeObservation(
        probe_index=full[0].probe_index,
        kind=HealthProbeKind.DOCKER_CONTAINER_HEALTHY,
        target=full[0].target,
        outcome=HealthProbeOutcome.PASSED,
        reason="container_healthy",
    )
    with pytest.raises(AuthorityConflict, match="does not describe the frozen probe"):
        authority.complete_package_update_health(job.job_id, tuple(full))


def test_an_unbounded_reason_token_is_refused(tmp_path: Path) -> None:
    store, authority, job = _started(tmp_path)
    full = list(_observations(job, (HealthProbeOutcome.PASSED,) * 2))
    full[0] = HealthProbeObservation(
        probe_index=full[0].probe_index,
        kind=full[0].kind,
        target=full[0].target,
        outcome=HealthProbeOutcome.PASSED,
        reason="it printed: Active: active (running) since Tue",
    )
    with pytest.raises(AuthorityConflict, match="bounded token"):
        authority.complete_package_update_health(job.job_id, tuple(full))


# ===========================================================================
# 5. The ONE legal success transition
# ===========================================================================


def test_the_complete_success_path(tmp_path: Path) -> None:
    clock, store, authority, resource, scan, approval, job = _mutated_job(tmp_path)

    started = authority.start_package_update_health(job.job_id)
    assert started.checkpoint is PackageUpdateCheckpoint.HEALTH_STARTED
    assert started.health_started_at is not None
    assert started.status is PackageUpdateJobStatus.ACTIVE

    decided = authority.complete_package_update_health(
        job.job_id, _observations(started, (HealthProbeOutcome.PASSED,) * 2)
    )

    assert decided.checkpoint is PackageUpdateCheckpoint.HEALTH_COMPLETED
    assert decided.health_outcome is HealthOutcome.PASSED
    assert decided.status is PackageUpdateJobStatus.SUCCEEDED
    assert decided.terminalized_at is not None
    assert decided.terminal_reason
    assert [result.outcome for result in decided.health_probe_results] == [
        HealthProbeOutcome.PASSED,
        HealthProbeOutcome.PASSED,
    ]

    events = store.list_package_update_job_events(job.job_id)
    types = [event.event_type for event in events]
    assert PackageUpdateEventType.HEALTH_STARTED in types
    assert PackageUpdateEventType.HEALTH_PASSED in types
    passed = next(
        event
        for event in events
        if event.event_type is PackageUpdateEventType.HEALTH_PASSED
    )
    assert passed.details["failed_probe_indexes"] == []
    assert passed.details["unknown_probe_indexes"] == []


def test_success_releases_the_one_global_active_slot(tmp_path: Path) -> None:
    from tests.test_package_update_job_authority import _add_approved_resource

    _, store, authority, resource, scan, approval, job = _mutated_job(tmp_path)
    other, _, other_approval = _add_approved_resource(store, authority)
    with pytest.raises(AuthorityConflict, match="global slot"):
        _issue(authority, other, other_approval)

    started = authority.start_package_update_health(job.job_id)
    authority.complete_package_update_health(
        job.job_id, _observations(started, (HealthProbeOutcome.PASSED,) * 2)
    )

    assert _issue(authority, other, other_approval).status is (
        PackageUpdateJobStatus.ACTIVE
    )


def test_a_job_cannot_health_complete_twice(tmp_path: Path) -> None:
    _, _, authority, _, _, _, job = _mutated_job(tmp_path)
    started = authority.start_package_update_health(job.job_id)
    authority.complete_package_update_health(
        job.job_id, _observations(started, (HealthProbeOutcome.PASSED,) * 2)
    )

    with pytest.raises(AuthorityConflict):
        authority.complete_package_update_health(
            job.job_id, _observations(started, (HealthProbeOutcome.PASSED,) * 2)
        )


def test_a_later_result_can_never_overwrite_an_accepted_verdict(
    tmp_path: Path,
) -> None:
    _, store, authority, _, _, _, job = _mutated_job(tmp_path)
    started = authority.start_package_update_health(job.job_id)
    authority.complete_package_update_health(
        job.job_id,
        _observations(started, (HealthProbeOutcome.FAILED, HealthProbeOutcome.PASSED)),
    )

    _assert_sql_rejected(
        store,
        "UPDATE package_update_jobs SET health_outcome='passed' WHERE job_id=?",
        (job.job_id,),
    )
    _assert_sql_rejected(
        store,
        "UPDATE package_update_jobs SET health_completed_at=? WHERE job_id=?",
        ("2027-01-01T00:00:00+00:00", job.job_id),
    )
    _assert_sql_rejected(
        store,
        "UPDATE package_update_job_health_probe_results SET outcome='passed' "
        "WHERE job_id=?",
        (job.job_id,),
    )
    _assert_sql_rejected(
        store,
        "DELETE FROM package_update_job_health_probe_results WHERE job_id=?",
        (job.job_id,),
    )


def test_health_start_is_idempotent_and_write_once(tmp_path: Path) -> None:
    _, store, authority, _, _, _, job = _mutated_job(tmp_path)
    first = authority.start_package_update_health(job.job_id)
    again = authority.start_package_update_health(job.job_id)
    assert again.health_started_at == first.health_started_at

    _assert_sql_rejected(
        store,
        "UPDATE package_update_jobs SET health_started_at=? WHERE job_id=?",
        ("2027-01-01T00:00:00+00:00", job.job_id),
    )


# ===========================================================================
# 6. SQL impossibilities
# ===========================================================================


def test_sql_forbids_health_started_without_a_proven_mutation(
    tmp_path: Path,
) -> None:
    _, store, authority, resource, _, approval = _approved_system(tmp_path)
    job = _issue(authority, resource, approval)
    authority.record_package_update_preflight_passed(job.job_id)
    authority.record_package_update_snapshot_intent(job.job_id)
    identity = authority.package_update_snapshot_identity(job.job_id)
    ownership = authority.package_update_snapshot_ownership(job.job_id)
    authority.confirm_package_update_snapshot(
        job.job_id, _canonical(ownership, identity)
    )
    with store._transaction() as connection:
        connection.execute(
            "UPDATE package_update_jobs "
            "SET checkpoint='mutation_may_have_started', "
            "mutation_operation_id=?, mutation_may_have_started_at=?, "
            "accepted_prepared_evidence_digest=? WHERE job_id=?",
            (str(uuid.uuid4()), "2026-02-01T00:00:00+00:00", "a" * 64, job.job_id),
        )

    _assert_sql_rejected(
        store,
        "UPDATE package_update_jobs SET checkpoint='health_started', "
        "health_started_at=? WHERE job_id=?",
        ("2026-02-01T00:05:00+00:00", job.job_id),
    )
    with pytest.raises(AuthorityConflict, match="not ready for health evaluation"):
        authority.start_package_update_health(job.job_id)


def test_sql_forbids_a_health_completion_without_a_health_start(
    tmp_path: Path,
) -> None:
    _, store, authority, _, _, _, job = _mutated_job(tmp_path)
    _assert_sql_rejected(
        store,
        "UPDATE package_update_jobs SET checkpoint='health_completed', "
        "health_completed_at=?, health_outcome='failed' WHERE job_id=?",
        ("2026-02-01T00:05:00+00:00", job.job_id),
    )


def test_sql_forbids_succeeded_without_a_passing_health_verdict(
    tmp_path: Path,
) -> None:
    """No package command exit code, mutation completion, guest
    reachability, or absence of failures may produce SUCCEEDED on its own."""

    _, store, authority, _, _, _, job = _mutated_job(tmp_path)

    # A proven mutation alone is not success.
    _assert_sql_rejected(
        store,
        "UPDATE package_update_jobs SET status='succeeded', terminalized_at=?, "
        "terminal_reason='apt exited zero' WHERE job_id=?",
        ("2026-02-01T00:05:00+00:00", job.job_id),
    )

    started = authority.start_package_update_health(job.job_id)
    # A started evaluation with no verdict is not success either.
    _assert_sql_rejected(
        store,
        "UPDATE package_update_jobs SET status='succeeded', terminalized_at=?, "
        "terminal_reason='health was started' WHERE job_id=?",
        ("2026-02-01T00:05:00+00:00", job.job_id),
    )
    # And neither is a FAILED verdict.
    authority.complete_package_update_health(
        job.job_id,
        _observations(started, (HealthProbeOutcome.FAILED, HealthProbeOutcome.PASSED)),
    )
    _assert_sql_rejected(
        store,
        "UPDATE package_update_jobs SET status='succeeded', terminalized_at=?, "
        "terminal_reason='close enough' WHERE job_id=?",
        ("2026-02-01T00:05:00+00:00", job.job_id),
    )


def test_sql_forbids_a_passing_verdict_with_missing_or_failing_results(
    tmp_path: Path,
) -> None:
    _, store, authority, _, _, _, job = _mutated_job(tmp_path)
    authority.start_package_update_health(job.job_id)

    # No result rows at all.
    _assert_sql_rejected(
        store,
        "UPDATE package_update_jobs SET checkpoint='health_completed', "
        "health_completed_at=?, health_outcome='passed', status='succeeded', "
        "terminalized_at=?, terminal_reason='no evidence' WHERE job_id=?",
        (
            "2026-02-01T00:05:00+00:00",
            "2026-02-01T00:05:00+00:00",
            job.job_id,
        ),
    )

    # One passing result for a two-probe contract: still not every probe.
    with store._transaction() as connection:
        connection.execute(
            "INSERT INTO package_update_job_health_probe_results("
            "job_id, probe_index, outcome, checked_at, reason) "
            "VALUES(?, 0, 'passed', ?, 'unit_active')",
            (job.job_id, "2026-02-01T00:05:00+00:00"),
        )
    _assert_sql_rejected(
        store,
        "UPDATE package_update_jobs SET checkpoint='health_completed', "
        "health_completed_at=?, health_outcome='passed', status='succeeded', "
        "terminalized_at=?, terminal_reason='half proven' WHERE job_id=?",
        (
            "2026-02-01T00:05:00+00:00",
            "2026-02-01T00:05:00+00:00",
            job.job_id,
        ),
    )

    # A complete set containing a non-pass is likewise not a pass.
    with store._transaction() as connection:
        connection.execute(
            "INSERT INTO package_update_job_health_probe_results("
            "job_id, probe_index, outcome, checked_at, reason) "
            "VALUES(?, 1, 'unknown', ?, 'command_failed')",
            (job.job_id, "2026-02-01T00:05:00+00:00"),
        )
    _assert_sql_rejected(
        store,
        "UPDATE package_update_jobs SET checkpoint='health_completed', "
        "health_completed_at=?, health_outcome='passed', status='succeeded', "
        "terminalized_at=?, terminal_reason='absence of failure' WHERE job_id=?",
        (
            "2026-02-01T00:05:00+00:00",
            "2026-02-01T00:05:00+00:00",
            job.job_id,
        ),
    )


def test_sql_forbids_a_failing_verdict_with_no_proven_failure(
    tmp_path: Path,
) -> None:
    _, store, authority, _, _, _, job = _mutated_job(tmp_path)
    authority.start_package_update_health(job.job_id)
    with store._transaction() as connection:
        connection.executemany(
            "INSERT INTO package_update_job_health_probe_results("
            "job_id, probe_index, outcome, checked_at, reason) "
            "VALUES(?, ?, 'unknown', ?, 'command_failed')",
            [
                (job.job_id, 0, "2026-02-01T00:05:00+00:00"),
                (job.job_id, 1, "2026-02-01T00:05:00+00:00"),
            ],
        )
    _assert_sql_rejected(
        store,
        "UPDATE package_update_jobs SET checkpoint='health_completed', "
        "health_completed_at=?, health_outcome='failed' WHERE job_id=?",
        ("2026-02-01T00:05:00+00:00", job.job_id),
    )


def test_sql_forbids_a_result_row_before_health_started(tmp_path: Path) -> None:
    _, store, authority, _, _, _, job = _mutated_job(tmp_path)
    _assert_sql_rejected(
        store,
        "INSERT INTO package_update_job_health_probe_results("
        "job_id, probe_index, outcome, checked_at, reason) "
        "VALUES(?, 0, 'passed', ?, 'unit_active')",
        (job.job_id, "2026-02-01T00:05:00+00:00"),
    )


def test_sql_forbids_a_result_row_for_an_unfrozen_probe(tmp_path: Path) -> None:
    _, store, authority, _, _, _, job = _mutated_job(tmp_path)
    authority.start_package_update_health(job.job_id)
    _assert_sql_rejected(
        store,
        "INSERT INTO package_update_job_health_probe_results("
        "job_id, probe_index, outcome, checked_at, reason) "
        "VALUES(?, 7, 'passed', ?, 'unit_active')",
        (job.job_id, "2026-02-01T00:05:00+00:00"),
    )


def test_sql_still_lets_an_uncertain_mutation_reach_rollback_without_health(
    tmp_path: Path,
) -> None:
    """The v14 lesson, re-proved after v16 added two ranks above it."""

    _, store, authority, resource, _, approval = _approved_system(tmp_path)
    job = _issue(authority, resource, approval)
    authority.record_package_update_preflight_passed(job.job_id)
    authority.record_package_update_snapshot_intent(job.job_id)
    identity = authority.package_update_snapshot_identity(job.job_id)
    ownership = authority.package_update_snapshot_ownership(job.job_id)
    authority.confirm_package_update_snapshot(
        job.job_id, _canonical(ownership, identity)
    )
    with store._transaction() as connection:
        connection.execute(
            "UPDATE package_update_jobs "
            "SET checkpoint='mutation_may_have_started', "
            "mutation_operation_id=?, mutation_may_have_started_at=?, "
            "accepted_prepared_evidence_digest=? WHERE job_id=?",
            (str(uuid.uuid4()), "2026-02-01T00:00:00+00:00", "a" * 64, job.job_id),
        )
        # Rank 9 from rank 5, with mutation_completed_at, health_started_at,
        # health_completed_at and health_outcome ALL still NULL.
        connection.execute(
            "UPDATE package_update_jobs "
            "SET checkpoint='rollback_may_have_started', "
            "rollback_operation_id=?, rollback_may_have_started_at=? "
            "WHERE job_id=?",
            (str(uuid.uuid4()), "2026-02-01T00:02:00+00:00", job.job_id),
        )

    after = authority.package_update_job(job.job_id)
    assert after.checkpoint is PackageUpdateCheckpoint.ROLLBACK_MAY_HAVE_STARTED
    assert after.mutation_completed_at is None
    assert after.health_started_at is None
    assert after.health_completed_at is None
    assert after.health_outcome is None


# ===========================================================================
# 7. Read-path coherence
# ===========================================================================


def test_the_read_path_refuses_an_incoherent_frozen_probe_set(
    tmp_path: Path,
) -> None:
    """A direct-SQL repair that rebuilt an inconsistent row set is caught on
    read rather than silently understating what the operator required."""

    from app.inventory import AuthorityInvariantError

    _, store, authority, resource, _, approval = _approved_system(tmp_path)
    job = _issue(authority, resource, approval)
    _repair_out_of_band(
        store,
        "DELETE FROM package_update_job_health_probes "
        "WHERE job_id=? AND probe_index=1",
        (job.job_id,),
    )

    with pytest.raises(AuthorityInvariantError, match="probe count"):
        authority.package_update_job(job.job_id)


def test_the_read_path_refuses_probes_that_do_not_match_the_fingerprint(
    tmp_path: Path,
) -> None:
    from app.inventory import AuthorityInvariantError

    _, store, authority, resource, _, approval = _approved_system(tmp_path)
    job = _issue(authority, resource, approval)
    _repair_out_of_band(
        store,
        "DELETE FROM package_update_job_health_probes "
        "WHERE job_id=? AND probe_index=0",
        (job.job_id,),
        "INSERT INTO package_update_job_health_probes("
        "job_id, probe_index, kind, target) "
        "VALUES(?, 0, 'systemd_unit_active', 'swapped.service')",
        (job.job_id,),
    )

    with pytest.raises(AuthorityInvariantError, match="fingerprint"):
        authority.package_update_job(job.job_id)


def test_the_read_path_refuses_results_without_a_verdict(tmp_path: Path) -> None:
    from app.inventory import AuthorityInvariantError

    _, store, authority, _, _, _, job = _mutated_job(tmp_path)
    authority.start_package_update_health(job.job_id)
    with store._transaction() as connection:
        connection.execute(
            "INSERT INTO package_update_job_health_probe_results("
            "job_id, probe_index, outcome, checked_at, reason) "
            "VALUES(?, 0, 'passed', ?, 'unit_active')",
            (job.job_id, "2026-02-01T00:05:00+00:00"),
        )

    with pytest.raises(AuthorityInvariantError, match="without a durable health verdict"):
        authority.package_update_job(job.job_id)


# ===========================================================================
# 8. Failure, and same-job rollback from the health branch
# ===========================================================================


def test_a_failing_verdict_keeps_the_job_active_and_rollback_capable(
    tmp_path: Path,
) -> None:
    _, store, authority, _, _, _, job = _mutated_job(tmp_path)
    started = authority.start_package_update_health(job.job_id)

    decided = authority.complete_package_update_health(
        job.job_id,
        _observations(started, (HealthProbeOutcome.FAILED, HealthProbeOutcome.PASSED)),
    )

    assert decided.checkpoint is PackageUpdateCheckpoint.HEALTH_COMPLETED
    assert decided.health_outcome is HealthOutcome.FAILED
    assert decided.status is PackageUpdateJobStatus.ACTIVE
    assert decided.terminalized_at is None
    # The job keeps everything a compensation stage needs.
    assert decided.snapshot_confirmed_at is not None
    assert decided.snapshot_name is not None
    assert authority.package_update_rollback_identity(job.job_id)

    events = store.list_package_update_job_events(job.job_id)
    failed = next(
        event
        for event in events
        if event.event_type is PackageUpdateEventType.HEALTH_FAILED
    )
    assert failed.details["failed_probe_indexes"] == [0]
    assert failed.details["unknown_probe_indexes"] == []


@pytest.mark.parametrize(
    "arrive_at",
    (
        "mutation_may_have_started",
        "mutation_completed",
        "health_started",
        "health_completed_failed",
    ),
)
def test_every_legal_rollback_entry_point_can_arm(
    tmp_path: Path, arrive_at: str
) -> None:
    """All four entry points, and none of them fabricates an earlier success.

    The two health entries are v16's version of the v14 lesson: a health
    evaluation that never reached a verdict, and one that reached a FAILING
    verdict, are both jobs that may need compensating -- so neither may be
    required to look like a success first.
    """

    _, store, authority, resource, scan, approval = _approved_system(tmp_path)
    job = _issue(authority, resource, approval)
    authority.record_package_update_preflight_passed(job.job_id)
    authority.record_package_update_snapshot_intent(job.job_id)
    identity = authority.package_update_snapshot_identity(job.job_id)
    ownership = authority.package_update_snapshot_ownership(job.job_id)
    authority.confirm_package_update_snapshot(
        job.job_id, _canonical(ownership, identity)
    )
    with store._transaction() as connection:
        connection.execute(
            "UPDATE package_update_jobs "
            "SET checkpoint='mutation_may_have_started', "
            "mutation_operation_id=?, mutation_may_have_started_at=?, "
            "accepted_prepared_evidence_digest=? WHERE job_id=?",
            (str(uuid.uuid4()), "2026-02-01T00:00:00+00:00", "a" * 64, job.job_id),
        )
    if arrive_at != "mutation_may_have_started":
        with store._transaction() as connection:
            connection.execute(
                "UPDATE package_update_jobs SET checkpoint='mutation_completed', "
                "mutation_completed_at=? WHERE job_id=?",
                ("2026-02-01T00:01:00+00:00", job.job_id),
            )
    if arrive_at in ("health_started", "health_completed_failed"):
        started = authority.start_package_update_health(job.job_id)
        if arrive_at == "health_completed_failed":
            authority.complete_package_update_health(
                job.job_id,
                _observations(
                    started,
                    (HealthProbeOutcome.FAILED, HealthProbeOutcome.PASSED),
                ),
            )

    armed = authority.arm_package_update_rollback(
        job.job_id, _canonical(ownership, identity)
    )

    assert armed.checkpoint is PackageUpdateCheckpoint.ROLLBACK_MAY_HAVE_STARTED
    assert armed.status is PackageUpdateJobStatus.ACTIVE
    assert armed.rollback_operation_id is not None
    if arrive_at == "mutation_may_have_started":
        # Nothing fabricated a mutation completion this job never had.
        assert armed.mutation_completed_at is None
    if arrive_at in ("mutation_may_have_started", "mutation_completed"):
        assert armed.health_started_at is None
    if arrive_at == "health_started":
        # Nor a health verdict it never reached.
        assert armed.health_completed_at is None
        assert armed.health_outcome is None
    if arrive_at == "health_completed_failed":
        assert armed.health_outcome is HealthOutcome.FAILED

    event = store.list_package_update_job_events(job.job_id)[-1]
    assert event.event_type is PackageUpdateEventType.ROLLBACK_MAY_HAVE_STARTED
    assert event.details["entered_from_checkpoint"] == (
        "health_completed"
        if arrive_at == "health_completed_failed"
        else arrive_at
    )


def test_rollback_is_refused_after_a_successful_health_verdict(
    tmp_path: Path,
) -> None:
    _, _, authority, _, _, _, job = _mutated_job(tmp_path)
    identity = authority.package_update_snapshot_identity(job.job_id)
    ownership = authority.package_update_snapshot_ownership(job.job_id)
    started = authority.start_package_update_health(job.job_id)
    succeeded = authority.complete_package_update_health(
        job.job_id, _observations(started, (HealthProbeOutcome.PASSED,) * 2)
    )
    assert succeeded.status is PackageUpdateJobStatus.SUCCEEDED

    with pytest.raises(AuthorityConflict, match="terminal"):
        authority.arm_package_update_rollback(
            job.job_id, _canonical(ownership, identity)
        )


def test_health_transitions_are_refused_once_a_rollback_is_armed(
    tmp_path: Path,
) -> None:
    """A late health result never overwrites a rollback that moved the job on."""

    _, _, authority, _, _, _, job = _mutated_job(tmp_path)
    identity = authority.package_update_snapshot_identity(job.job_id)
    ownership = authority.package_update_snapshot_ownership(job.job_id)
    started = authority.start_package_update_health(job.job_id)
    authority.arm_package_update_rollback(
        job.job_id, _canonical(ownership, identity)
    )

    with pytest.raises(AuthorityConflict, match="not inside a health evaluation"):
        authority.complete_package_update_health(
            job.job_id, _observations(started, (HealthProbeOutcome.PASSED,) * 2)
        )
    with pytest.raises(AuthorityConflict):
        authority.start_package_update_health(job.job_id)


# ===========================================================================
# 9. Restart and retry
# ===========================================================================


def test_startup_recovery_leaves_a_health_started_job_active_and_fenced(
    tmp_path: Path,
) -> None:
    _, _, authority, _, _, _, job = _mutated_job(tmp_path)
    authority.start_package_update_health(job.job_id)

    assert authority.recover_interrupted_package_update_jobs() == ()

    after = authority.package_update_job(job.job_id)
    assert after.status is PackageUpdateJobStatus.ACTIVE
    assert after.checkpoint is PackageUpdateCheckpoint.HEALTH_STARTED
    # A restart is never evidence about the workload.
    assert after.health_completed_at is None
    assert after.health_outcome is None


def test_startup_recovery_leaves_a_failed_health_job_active_and_rollback_capable(
    tmp_path: Path,
) -> None:
    _, _, authority, _, _, _, job = _mutated_job(tmp_path)
    started = authority.start_package_update_health(job.job_id)
    authority.complete_package_update_health(
        job.job_id,
        _observations(started, (HealthProbeOutcome.FAILED, HealthProbeOutcome.PASSED)),
    )

    assert authority.recover_interrupted_package_update_jobs() == ()

    after = authority.package_update_job(job.job_id)
    assert after.status is PackageUpdateJobStatus.ACTIVE
    assert after.checkpoint is PackageUpdateCheckpoint.HEALTH_COMPLETED
    assert authority.package_update_rollback_identity(job.job_id)


def test_a_succeeded_job_is_terminal_and_never_re_evaluated(
    tmp_path: Path,
) -> None:
    _, _, authority, _, _, _, job = _mutated_job(tmp_path)
    started = authority.start_package_update_health(job.job_id)
    authority.complete_package_update_health(
        job.job_id, _observations(started, (HealthProbeOutcome.PASSED,) * 2)
    )

    assert authority.recover_interrupted_package_update_jobs() == ()
    with pytest.raises(AuthorityConflict, match="terminal"):
        authority.start_package_update_health(job.job_id)


# ===========================================================================
# 10. The orchestrator, over a typed fake host
# ===========================================================================


class FakeHealthHostControl:
    """A typed, in-memory stand-in for the dark SSH transport."""

    def __init__(self, *, outcomes=None, raises=None, mutate=None, side_effect=None):
        self.outcomes = outcomes
        self.raises = raises
        self.mutate = mutate
        self.side_effect = side_effect
        self.calls = 0

    def evaluate_health_contract(self, request):
        self.calls += 1
        if self.side_effect is not None:
            self.side_effect(request)
        if self.raises is not None:
            raise self.raises
        outcomes = self.outcomes or (
            (HealthProbeOutcome.PASSED,) * len(request.probes)
        )
        reasons = {
            HealthProbeOutcome.PASSED: {
                HealthProbeKind.SYSTEMD_UNIT_ACTIVE: "unit_active",
                HealthProbeKind.DOCKER_CONTAINER_RUNNING: "container_running",
                HealthProbeKind.DOCKER_CONTAINER_HEALTHY: "container_healthy",
            },
            HealthProbeOutcome.FAILED: {
                HealthProbeKind.SYSTEMD_UNIT_ACTIVE: "unit_not_active",
                HealthProbeKind.DOCKER_CONTAINER_RUNNING: "container_absent",
                HealthProbeKind.DOCKER_CONTAINER_HEALTHY: "container_unhealthy",
            },
            HealthProbeOutcome.UNKNOWN: {
                HealthProbeKind.SYSTEMD_UNIT_ACTIVE: "command_timed_out",
                HealthProbeKind.DOCKER_CONTAINER_RUNNING: "docker_daemon_unavailable",
                HealthProbeKind.DOCKER_CONTAINER_HEALTHY: "docker_daemon_unavailable",
            },
        }
        result = HostHealthResult(
            contract_revision=request.health_contract_revision,
            contract_fingerprint=request.health_contract_fingerprint,
            probes=tuple(
                HostProbeResult(
                    probe_index=probe.probe_index,
                    kind=probe.kind,
                    target=probe.target,
                    outcome=outcome,
                    reason=reasons[outcome][probe.kind],
                )
                for probe, outcome in zip(request.probes, outcomes)
            ),
        )
        if self.mutate is not None:
            result = self.mutate(result)
        return result


class RecordingRollbackHostControl:
    """Fails the test if health execution ever reaches the rollback boundary."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def submit_same_job_rollback(self, request):
        self.calls.append("submit")
        raise AssertionError("health execution must never submit a rollback")

    def inspect_rollback_state(self, request):
        self.calls.append("inspect")
        raise AssertionError("health execution must never inspect a rollback")

    def seal_rollback_never_submitted(self, request):
        self.calls.append("seal")
        raise AssertionError("health execution must never seal a rollback")


def test_the_orchestrator_drives_the_full_success_path(tmp_path: Path) -> None:
    _, store, authority, _, _, _, job = _mutated_job(tmp_path)
    host = FakeHealthHostControl()
    orchestrator = PackageUpdateHealthOrchestrator(authority, host)

    result = orchestrator.evaluate_job_health(job.job_id)

    assert result.status is HealthStageStatus.PASSED
    assert result.job.status is PackageUpdateJobStatus.SUCCEEDED
    assert result.job.checkpoint is PackageUpdateCheckpoint.HEALTH_COMPLETED
    assert host.calls == 1


def test_the_orchestrator_reports_a_failure_and_calls_no_rollback(
    tmp_path: Path,
) -> None:
    """The whole no-auto-policy rule, as an executable regression.

    A proven health failure makes ZERO calls into the rollback host control,
    and nothing anywhere arms, submits, or seals a rollback. Compensation
    stays something an operator asks for.
    """

    _, store, authority, _, _, _, job = _mutated_job(tmp_path)
    rollback_host = RecordingRollbackHostControl()
    host = FakeHealthHostControl(
        outcomes=(HealthProbeOutcome.FAILED, HealthProbeOutcome.PASSED)
    )
    orchestrator = PackageUpdateHealthOrchestrator(authority, host)

    result = orchestrator.evaluate_job_health(job.job_id)

    assert result.status is HealthStageStatus.FAILED
    assert result.job.status is PackageUpdateJobStatus.ACTIVE
    assert result.job.health_outcome is HealthOutcome.FAILED
    assert rollback_host.calls == []
    assert result.job.rollback_operation_id is None
    assert result.job.rollback_may_have_started_at is None
    events = store.list_package_update_job_events(job.job_id)
    assert not any(
        event.event_type
        in (
            PackageUpdateEventType.ROLLBACK_MAY_HAVE_STARTED,
            PackageUpdateEventType.ROLLBACK_SUBMITTED,
        )
        for event in events
    )


def test_a_failed_health_job_can_then_be_rolled_back_normally(
    tmp_path: Path,
) -> None:
    """The capability is retained; it just is not exercised automatically."""

    _, store, authority, _, _, _, job = _mutated_job(tmp_path)
    identity = authority.package_update_snapshot_identity(job.job_id)
    ownership = authority.package_update_snapshot_ownership(job.job_id)
    orchestrator = PackageUpdateHealthOrchestrator(
        authority,
        FakeHealthHostControl(
            outcomes=(HealthProbeOutcome.FAILED, HealthProbeOutcome.PASSED)
        ),
    )
    assert orchestrator.evaluate_job_health(job.job_id).status is (
        HealthStageStatus.FAILED
    )

    # An operator now asks for the rollback, explicitly.
    armed = authority.arm_package_update_rollback(
        job.job_id, _canonical(ownership, identity)
    )
    assert armed.checkpoint is PackageUpdateCheckpoint.ROLLBACK_MAY_HAVE_STARTED
    assert armed.health_outcome is HealthOutcome.FAILED


@pytest.mark.parametrize(
    "kind",
    ("transport", "timeout", "malformed"),
)
def test_a_lost_or_malformed_host_answer_is_unknown_and_retryable(
    tmp_path: Path, kind: str
) -> None:
    _, store, authority, _, _, _, job = _mutated_job(tmp_path)
    if kind == "transport":
        host = FakeHealthHostControl(raises=OSError("ssh died"))
    elif kind == "timeout":
        host = FakeHealthHostControl(
            raises=PackageUpdateHealthError("host-control request timed out")
        )
    else:
        host = FakeHealthHostControl(
            mutate=lambda result: HostHealthResult(
                contract_revision=result.contract_revision + 1,
                contract_fingerprint=result.contract_fingerprint,
                probes=result.probes,
            )
        )
    orchestrator = PackageUpdateHealthOrchestrator(authority, host)

    result = orchestrator.evaluate_job_health(job.job_id)

    assert result.status is HealthStageStatus.UNKNOWN
    assert result.job.status is PackageUpdateJobStatus.ACTIVE
    assert result.job.checkpoint is PackageUpdateCheckpoint.HEALTH_STARTED
    assert result.job.health_completed_at is None
    assert result.job.health_outcome is None
    assert result.job.health_probe_results == ()
    assert result.job.snapshot_confirmed_at is not None
    events = store.list_package_update_job_events(job.job_id)
    assert events[-1].event_type is PackageUpdateEventType.HEALTH_OUTCOME_UNKNOWN

    # Read-only, so it is simply safe to run again -- and it then succeeds.
    retried = PackageUpdateHealthOrchestrator(
        authority, FakeHealthHostControl()
    ).evaluate_job_health(job.job_id)
    assert retried.status is HealthStageStatus.PASSED
    assert retried.job.status is PackageUpdateJobStatus.SUCCEEDED


def test_an_unknown_probe_beside_passes_is_never_success(tmp_path: Path) -> None:
    _, _, authority, _, _, _, job = _mutated_job(tmp_path)
    host = FakeHealthHostControl(
        outcomes=(HealthProbeOutcome.PASSED, HealthProbeOutcome.UNKNOWN)
    )
    orchestrator = PackageUpdateHealthOrchestrator(authority, host)

    result = orchestrator.evaluate_job_health(job.job_id)

    assert result.status is HealthStageStatus.UNKNOWN
    assert result.job.status is PackageUpdateJobStatus.ACTIVE
    assert result.job.health_outcome is None


def test_a_resource_replaced_during_the_host_call_is_never_accepted(
    tmp_path: Path,
) -> None:
    """A PASS about a REPLACEMENT guest would be a false statement about this
    job's workload, read-only probes or not."""

    from tests.test_package_update_snapshot_safety import (
        _break_incarnation_continuity_at_the_same_locator,
    )

    _, store, authority, resource, _, _, job = _mutated_job(tmp_path)

    def replace_guest(request):
        _break_incarnation_continuity_at_the_same_locator(store, authority)

    host = FakeHealthHostControl(side_effect=replace_guest)
    orchestrator = PackageUpdateHealthOrchestrator(authority, host)

    result = orchestrator.evaluate_job_health(job.job_id)

    assert result.status is HealthStageStatus.UNKNOWN
    assert result.job.health_outcome is None
    assert result.job.status is PackageUpdateJobStatus.ACTIVE
    assert result.job.checkpoint is PackageUpdateCheckpoint.HEALTH_STARTED


def test_a_stale_resource_context_is_refused_before_any_host_call(
    tmp_path: Path,
) -> None:
    from tests.test_package_update_snapshot_safety import (
        _break_incarnation_continuity_at_the_same_locator,
    )

    _, store, authority, resource, _, _, job = _mutated_job(tmp_path)
    _break_incarnation_continuity_at_the_same_locator(store, authority)
    host = FakeHealthHostControl()

    result = PackageUpdateHealthOrchestrator(
        authority, host
    ).evaluate_job_health(job.job_id)

    assert result.status is HealthStageStatus.UNKNOWN
    assert host.calls == 0


# ===========================================================================
# 11. Strict host-response validation
# ===========================================================================


def _host_result(job, **overrides):
    base = {
        "contract_revision": job.health_contract_revision,
        "contract_fingerprint": job.health_contract_fingerprint,
        "probes": tuple(
            HostProbeResult(
                probe_index=probe.probe_index,
                kind=probe.kind,
                target=probe.target,
                outcome=HealthProbeOutcome.PASSED,
                reason=(
                    "unit_active"
                    if probe.kind is HealthProbeKind.SYSTEMD_UNIT_ACTIVE
                    else "container_running"
                ),
            )
            for probe in job.health_probes
        ),
    }
    base.update(overrides)
    return HostHealthResult(**base)


def test_a_valid_host_answer_becomes_typed_observations(tmp_path: Path) -> None:
    _, _, _, _, _, _, job = _mutated_job(tmp_path)
    observations = validate_host_health_result(job, _host_result(job))
    assert [obs.probe_index for obs in observations] == [0, 1]
    assert all(obs.outcome is HealthProbeOutcome.PASSED for obs in observations)


@pytest.mark.parametrize(
    ("overrides", "match"),
    (
        ({"contract_revision": 99}, "different health contract revision"),
        ({"contract_fingerprint": "c" * 64}, "different health contract fingerprint"),
    ),
)
def test_an_answer_about_another_contract_generation_is_rejected(
    tmp_path: Path, overrides, match
) -> None:
    _, _, _, _, _, _, job = _mutated_job(tmp_path)
    with pytest.raises(PackageUpdateHealthError, match=match):
        validate_host_health_result(job, _host_result(job, **overrides))


def test_a_short_probe_set_is_rejected(tmp_path: Path) -> None:
    _, _, _, _, _, _, job = _mutated_job(tmp_path)
    full = _host_result(job)
    with pytest.raises(PackageUpdateHealthError, match="different number of probe"):
        validate_host_health_result(
            job, _host_result(job, probes=full.probes[:1])
        )


def test_a_duplicated_probe_result_is_rejected(tmp_path: Path) -> None:
    _, _, _, _, _, _, job = _mutated_job(tmp_path)
    full = _host_result(job)
    with pytest.raises(PackageUpdateHealthError, match="duplicate probe result"):
        validate_host_health_result(
            job, _host_result(job, probes=(full.probes[0], full.probes[0]))
        )


def test_a_probe_result_about_another_target_is_rejected(tmp_path: Path) -> None:
    _, _, _, _, _, _, job = _mutated_job(tmp_path)
    full = _host_result(job)
    swapped = (
        HostProbeResult(
            probe_index=0,
            kind=full.probes[0].kind,
            target="somewhere-else",
            outcome=HealthProbeOutcome.PASSED,
            reason=full.probes[0].reason,
        ),
        full.probes[1],
    )
    with pytest.raises(PackageUpdateHealthError, match="different probe target"):
        validate_host_health_result(job, _host_result(job, probes=swapped))


def test_a_probe_result_about_another_kind_is_rejected(tmp_path: Path) -> None:
    _, _, _, _, _, _, job = _mutated_job(tmp_path)
    full = _host_result(job)
    swapped = (
        HostProbeResult(
            probe_index=0,
            kind=HealthProbeKind.DOCKER_CONTAINER_HEALTHY,
            target=full.probes[0].target,
            outcome=HealthProbeOutcome.PASSED,
            reason="container_healthy",
        ),
        full.probes[1],
    )
    with pytest.raises(PackageUpdateHealthError, match="different probe kind"):
        validate_host_health_result(job, _host_result(job, probes=swapped))


def test_a_reason_that_contradicts_its_outcome_is_rejected(tmp_path: Path) -> None:
    """A host claiming PASS with `container_absent` is contradicting itself,
    and a self-contradictory answer is not evidence."""

    _, _, _, _, _, _, job = _mutated_job(tmp_path)
    full = _host_result(job)
    contradictory = (
        full.probes[0],
        HostProbeResult(
            probe_index=1,
            kind=full.probes[1].kind,
            target=full.probes[1].target,
            outcome=HealthProbeOutcome.PASSED,
            reason="container_absent",
        ),
    )
    with pytest.raises(PackageUpdateHealthError, match="contradicts its own outcome"):
        validate_host_health_result(job, _host_result(job, probes=contradictory))


def test_a_reason_impossible_for_that_probe_kind_is_rejected(
    tmp_path: Path,
) -> None:
    _, _, _, _, _, _, job = _mutated_job(tmp_path)
    full = _host_result(job)
    # Probe 0 is the Docker probe (canonical order is by (kind, target)), so
    # a systemd reason on it is describing something never looked at.
    assert full.probes[0].kind is HealthProbeKind.DOCKER_CONTAINER_RUNNING
    impossible = (
        HostProbeResult(
            probe_index=0,
            kind=full.probes[0].kind,
            target=full.probes[0].target,
            outcome=HealthProbeOutcome.PASSED,
            reason="unit_active",
        ),
        full.probes[1],
    )
    with pytest.raises(PackageUpdateHealthError, match="impossible for that probe kind"):
        validate_host_health_result(job, _host_result(job, probes=impossible))


def test_an_unbounded_host_reason_is_rejected(tmp_path: Path) -> None:
    _, _, _, _, _, _, job = _mutated_job(tmp_path)
    full = _host_result(job)
    leaky = (
        HostProbeResult(
            probe_index=0,
            kind=full.probes[0].kind,
            target=full.probes[0].target,
            outcome=HealthProbeOutcome.PASSED,
            reason="Active: active (running) since Tue 2026-02-01",
        ),
        full.probes[1],
    )
    with pytest.raises(PackageUpdateHealthError, match="bounded taxonomy"):
        validate_host_health_result(job, _host_result(job, probes=leaky))


def test_sql_forbids_inserting_a_job_row_that_already_claims_a_verdict(
    tmp_path: Path,
) -> None:
    """The last way a statement could express a success it cannot back up:
    writing the whole row at once instead of transitioning into it."""

    _, store, authority, resource, _, approval = _approved_system(tmp_path)
    template = _issue(authority, resource, approval)
    columns = (
        "job_id, request_id, issued_at, resource_id, approval_id, "
        "approval_reviewed_scan_run_id, approved_plan_fingerprint, "
        "approval_approved_at, current_plan_scan_run_id, inventory_source_id, "
        "committed_source_config_revision, committed_endpoint_id, "
        "committed_canonical_transport_locator, "
        "committed_canonicalization_contract_version, "
        "committed_transport_trust_revision, provider_contract_version, "
        "expected_resource_type, expected_binding_id, expected_locator_generation, "
        "expected_resource_continuity_revision, expected_vmid, expected_node_id, "
        "expected_node_name, package_count, health_contract_revision, "
        "health_contract_fingerprint, health_contract_probe_count, status, "
        "checkpoint, mutation_operation_id, mutation_may_have_started_at, "
        "accepted_prepared_evidence_digest, mutation_completed_at, "
        "health_started_at, health_completed_at, health_outcome, "
        "terminalized_at, terminal_reason"
    )
    forged = (
        str(uuid.uuid4()),
        str(uuid.uuid4()),
        template.issued_at,
        template.resource_id,
        template.approval_id,
        template.approval_reviewed_scan_run_id,
        template.approved_plan_fingerprint,
        template.approval_approved_at,
        template.current_plan_scan_run_id,
        template.inventory_source_id,
        template.committed_source_config_revision,
        template.committed_endpoint_id,
        template.committed_canonical_transport_locator,
        template.committed_canonicalization_contract_version,
        template.committed_transport_trust_revision,
        template.provider_contract_version,
        template.expected_resource_type,
        template.expected_binding_id,
        template.expected_locator_generation,
        template.expected_resource_continuity_revision,
        template.expected_vmid,
        template.expected_node_id,
        template.expected_node_name,
        template.package_count,
        template.health_contract_revision,
        template.health_contract_fingerprint,
        template.health_contract_probe_count,
        "succeeded",
        "health_completed",
        str(uuid.uuid4()),
        "2026-02-01T00:00:00+00:00",
        "a" * 64,
        "2026-02-01T00:01:00+00:00",
        "2026-02-01T00:02:00+00:00",
        "2026-02-01T00:03:00+00:00",
        "passed",
        "2026-02-01T00:03:00+00:00",
        "forged",
    )
    _assert_sql_rejected(
        store,
        f"INSERT INTO package_update_jobs({columns}) "
        f"VALUES({', '.join('?' * len(forged))})",
        forged,
    )


def test_the_unknown_event_names_the_probe_that_could_not_be_evaluated(
    tmp_path: Path,
) -> None:
    """"The Docker daemon did not answer" is what an operator needs to see,
    not a generic "something went wrong"."""

    _, store, authority, _, _, _, job = _mutated_job(tmp_path)
    host = FakeHealthHostControl(
        outcomes=(HealthProbeOutcome.UNKNOWN, HealthProbeOutcome.PASSED)
    )

    result = PackageUpdateHealthOrchestrator(
        authority, host
    ).evaluate_job_health(job.job_id)

    assert result.status is HealthStageStatus.UNKNOWN
    event = store.list_package_update_job_events(job.job_id)[-1]
    assert event.event_type is PackageUpdateEventType.HEALTH_OUTCOME_UNKNOWN
    assert event.details["reason"] == "docker_daemon_unavailable"


def test_a_job_that_moved_on_mid_attempt_still_reports_no_verdict(
    tmp_path: Path,
) -> None:
    """An operator arming a rollback while an evaluation was in flight is not
    an error: this attempt simply produced no verdict."""

    _, _, authority, _, _, _, job = _mutated_job(tmp_path)
    identity = authority.package_update_snapshot_identity(job.job_id)
    ownership = authority.package_update_snapshot_ownership(job.job_id)

    def arm_rollback_mid_flight(request):
        authority.arm_package_update_rollback(
            job.job_id, _canonical(ownership, identity)
        )
        raise OSError("ssh died")

    result = PackageUpdateHealthOrchestrator(
        authority, FakeHealthHostControl(side_effect=arm_rollback_mid_flight)
    ).evaluate_job_health(job.job_id)

    assert result.status is HealthStageStatus.UNKNOWN
    assert result.job.checkpoint is PackageUpdateCheckpoint.ROLLBACK_MAY_HAVE_STARTED
    assert result.job.health_outcome is None


# ===========================================================================
# 12. End to end: the orchestrator over the REAL dark helper
# ===========================================================================
#
# Everything above tests one seam. These join all of them: authority ->
# request assembly -> the real SSH transport's JSON encoding -> the real
# helper module -> a fake guest -> the real response parser -> validation ->
# aggregation -> the durable verdict. Nothing here runs a real `pvesh`,
# `pct`, `ssh`, `systemctl`, or `docker`.


def _end_to_end(tmp_path: Path, configure=None, *, probes=None):
    from tests.test_package_health_helper import FakeGuest, helper as real_helper
    from app.package_scan_host_control import BoundedProcessResult
    from app.package_update_health_host_control import (
        SshPackageUpdateHealthHostControl,
    )

    _, store, authority, resource, scan, approval, job = _mutated_job(
        tmp_path, health_probes=probes
    )
    request = authority.package_update_health_request(job.job_id)
    guest = FakeGuest()
    guest.vmid = request.vmid
    guest.node = guest.current_node = request.expected_node
    if configure is not None:
        configure(guest)

    def runner(argv, stdin, timeout, max_bytes):
        payload = json.loads(stdin.decode("utf-8"))
        response = real_helper.handle_request(payload, runner=guest)
        return BoundedProcessResult(
            returncode=0 if response.get("ok") else 1,
            stdout=json.dumps(response).encode("utf-8"),
            stderr=b"",
            timed_out=False,
            output_exceeded=False,
        )

    transport = SshPackageUpdateHealthHostControl(
        host="pve.example.internal",
        port=22,
        user="hubinet-health",
        private_key_path=Path("/etc/hubinet-ops/health.key"),
        known_hosts_path=Path("/etc/hubinet-ops/health.known_hosts"),
        timeout_seconds=60,
        max_result_bytes=64 * 1024,
        runner=runner,
    )
    orchestrator = PackageUpdateHealthOrchestrator(authority, transport)
    return store, authority, guest, orchestrator.evaluate_job_health(job.job_id)


def test_end_to_end_a_healthy_workload_succeeds(tmp_path: Path) -> None:
    store, authority, guest, result = _end_to_end(tmp_path)

    assert result.status is HealthStageStatus.PASSED
    assert result.job.status is PackageUpdateJobStatus.SUCCEEDED
    assert result.job.health_outcome is HealthOutcome.PASSED
    assert [r.reason for r in result.job.health_probe_results] == [
        "container_running",
        "unit_active",
    ]


def test_end_to_end_a_stopped_unit_fails_and_keeps_rollback_authority(
    tmp_path: Path,
) -> None:
    def stop_the_unit(guest):
        guest.units["nginx.service"] = ("loaded", "failed")

    store, authority, guest, result = _end_to_end(tmp_path, stop_the_unit)

    assert result.status is HealthStageStatus.FAILED
    assert result.job.status is PackageUpdateJobStatus.ACTIVE
    assert result.job.health_outcome is HealthOutcome.FAILED
    assert [
        (r.outcome, r.reason) for r in result.job.health_probe_results
    ] == [
        (HealthProbeOutcome.PASSED, "container_running"),
        (HealthProbeOutcome.FAILED, "unit_not_active"),
    ]
    assert authority.package_update_rollback_identity(result.job.job_id)


def test_end_to_end_a_guest_that_is_not_running_is_unknown(tmp_path: Path) -> None:
    def stop_the_guest(guest):
        guest.running = False

    store, authority, guest, result = _end_to_end(tmp_path, stop_the_guest)

    assert result.status is HealthStageStatus.UNKNOWN
    assert result.job.status is PackageUpdateJobStatus.ACTIVE
    assert result.job.health_outcome is None
    assert result.job.health_probe_results == ()
    # The durable event says what actually happened, not "something failed".
    event = store.list_package_update_job_events(result.job.job_id)[-1]
    assert event.details["reason"] == "guest_unavailable"


def test_end_to_end_a_guest_that_moved_node_is_unknown(tmp_path: Path) -> None:
    def move_the_guest(guest):
        guest.current_node = "pve-b"

    store, authority, guest, result = _end_to_end(tmp_path, move_the_guest)

    assert result.status is HealthStageStatus.UNKNOWN
    assert result.job.health_outcome is None
    event = store.list_package_update_job_events(result.job.job_id)[-1]
    assert event.details["reason"] == "resource_context_changed"


def test_end_to_end_an_unavailable_docker_daemon_is_unknown(tmp_path: Path) -> None:
    def stop_docker(guest):
        guest.docker_daemon_up = False

    store, authority, guest, result = _end_to_end(tmp_path, stop_docker)

    assert result.status is HealthStageStatus.UNKNOWN
    assert result.job.health_outcome is None
    event = store.list_package_update_job_events(result.job.job_id)[-1]
    assert event.details["reason"] == "docker_daemon_unavailable"


def test_end_to_end_a_missing_container_fails_because_the_daemon_answered(
    tmp_path: Path,
) -> None:
    def remove_the_container(guest):
        guest.containers.clear()

    store, authority, guest, result = _end_to_end(tmp_path, remove_the_container)

    assert result.status is HealthStageStatus.FAILED
    assert result.job.health_outcome is HealthOutcome.FAILED
    assert [r.reason for r in result.job.health_probe_results][0] == "container_absent"


def test_end_to_end_a_glob_target_is_unknown_and_never_passes(
    tmp_path: Path,
) -> None:
    """The end-to-end version of the false PASS this design exists to stop.

    `nginx*` would match the active `nginx.service` through systemd's own
    pattern expansion. It must not produce a successful update job.
    """

    store, authority, guest, result = _end_to_end(
        tmp_path,
        probes=(
            ResourceHealthProbe(
                kind=HealthProbeKind.SYSTEMD_UNIT_ACTIVE, target="nginx*"
            ),
        ),
    )

    assert result.status is HealthStageStatus.UNKNOWN
    assert result.job.status is PackageUpdateJobStatus.ACTIVE
    assert result.job.health_outcome is None
    event = store.list_package_update_job_events(result.job.job_id)[-1]
    assert event.details["reason"] == "probe_target_not_exact"


def test_end_to_end_docker_health_is_required_when_it_was_asked_for(
    tmp_path: Path,
) -> None:
    def make_it_merely_running(guest):
        guest.containers["web"] = (True, "unhealthy")

    store, authority, guest, result = _end_to_end(
        tmp_path,
        make_it_merely_running,
        probes=(
            ResourceHealthProbe(
                kind=HealthProbeKind.DOCKER_CONTAINER_HEALTHY, target="web"
            ),
        ),
    )

    assert result.status is HealthStageStatus.FAILED
    assert [r.reason for r in result.job.health_probe_results] == [
        "container_unhealthy"
    ]


def test_contract_drift_mid_snapshot_cannot_starve_the_global_slot(
    tmp_path: Path,
) -> None:
    """Drift must not wedge the one global destructive slot.

    A contract edited while a snapshot operation is in flight makes the job
    stale, and staleness must route through the SAME established resolver
    plan drift does: the proven snapshot is retained, the job terminalizes
    `blocked` without gaining rollback authority, and the slot is freed. An
    obsolete job whose only way out is a backend restart is precisely what
    that resolver exists to prevent.
    """

    from tests.test_package_update_job_authority import _add_approved_resource

    _, store, authority, resource, _, approval = _approved_system(tmp_path)
    job = _issue(authority, resource, approval)
    authority.record_package_update_preflight_passed(job.job_id)
    authority.record_package_update_snapshot_intent(job.job_id)
    identity = authority.package_update_snapshot_identity(job.job_id)
    ownership = authority.package_update_snapshot_ownership(job.job_id)

    authority.replace_resource_health_contract(
        resource.resource_id,
        (
            ResourceHealthProbe(
                kind=HealthProbeKind.SYSTEMD_UNIT_ACTIVE, target="new.service"
            ),
        ),
    )

    with pytest.raises(AuthorityConflict, match="newer generation"):
        authority.confirm_package_update_snapshot(
            job.job_id, _canonical(ownership, identity)
        )

    blocked, after = (
        authority.block_package_update_after_snapshot_success_with_stale_authority(
            job.job_id, _canonical(ownership, identity)
        )
    )
    assert blocked is True
    assert after.status is PackageUpdateJobStatus.BLOCKED
    # The snapshot is retained, and retention is not rollback authority.
    assert after.snapshot_name is not None
    assert after.snapshot_confirmed_at is None
    assert after.rollback_operation_id is None

    other, _, other_approval = _add_approved_resource(store, authority)
    assert _issue(authority, other, other_approval).status is (
        PackageUpdateJobStatus.ACTIVE
    )
