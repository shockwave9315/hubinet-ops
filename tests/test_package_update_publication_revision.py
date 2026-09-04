"""Regression: package-update job mutations advance ``published_state_revision``.

GitHub review, PR #74, ``app/inventory/publication.py``: the published
resource snapshot carries a ``package_update_job`` summary that changes when
a job is issued, its checkpoint advances, snapshot confirmation changes,
mutation state changes, health state/outcome changes, rollback state
changes, or a job reaches a terminal state -- but none of those authority
mutations used to call ``_bump_global_revisions``. Home Assistant's
``validate_transition()`` requires that whenever
``published_state_revision`` is unchanged the complete published view is
unchanged too (see ``custom_components/hubinet_ops/contract/transition_validation.py``),
so a stale revision made every successor snapshot published during an update
look like a rejected mutable view, and the coordinator went
stale/unavailable for the whole duration of an update.

This file drives the durable authority path directly, one checkpoint at a
time (no worker, no host orchestration), so each transition's revision bump
-- or deliberate absence of one -- can be observed in isolation, then feeds
the resulting sequence of published views through the REAL Home Assistant
``validate_transition()`` to prove the fix closes the contract violation end
to end, not just that some counter increased.
"""

from __future__ import annotations

from pathlib import Path

from app.inventory import (
    HealthProbeOutcome,
    InventoryPublication,
    PackageUpdateExecutionOutcome,
    PackageUpdateJobStatus,
)
from app.inventory.mutation_completion import PackageMutationPostState

from tests.test_inventory_publication import contract_snapshot
from tests.test_package_update_execution_gate import _ready_job
from tests.test_package_update_health import _observations
from tests.test_package_update_job_authority import _approved_system, _issue
from tests.test_package_update_mutation import ARBITRARY_EVIDENCE_DIGEST, job_packages
from tests.test_package_update_snapshot_safety import UPID, _canonical


def _view(store, authority):
    return InventoryPublication(store, authority).read()


def _post_state(job) -> PackageMutationPostState:
    return PackageMutationPostState(
        pre_installed={
            (package.package_name, package.architecture): package.installed_version
            for package in job.packages
        },
        post_installed={
            (package.package_name, package.architecture): package.candidate_version
            for package in job.packages
        },
        post_unfinished=(),
    )


def test_publication_revision_advances_with_every_published_job_transition(
    tmp_path: Path,
) -> None:
    """Issuance through success: every published transition bumps the revision,
    and every consecutive pair of published views is a legal HA successor.
    """

    clock, store, authority, resource, scan, approval = _approved_system(tmp_path)

    views = [_view(store, authority)]
    assert (
        views[0].resources[0]["package_update_job"]["state"] == "not_started"
    )

    # 1. Issuance: the summary springs into existence.
    job = _issue(authority, resource, approval)
    views.append(_view(store, authority))
    assert views[-1].resources[0]["package_update_job"]["checkpoint"] == "issued"

    # 2. Preflight passed.
    authority.record_package_update_preflight_passed(job.job_id)
    views.append(_view(store, authority))

    # 3. Write-ahead snapshot intent.
    job = authority.record_package_update_snapshot_intent(job.job_id)
    views.append(_view(store, authority))

    identity = authority.package_update_snapshot_identity(job.job_id)
    ownership = authority.package_update_snapshot_ownership(job.job_id)

    # Recording the observed PVE task UPID is durable evidence, but it is
    # not part of the published summary (`snapshot_task_upid` is never
    # published) -- see the negative control below for the direct proof
    # this specific transition does not needlessly bump. It is still
    # appended here so the sequence includes a same-revision successor.
    authority.record_package_update_snapshot_task(job.job_id, UPID)
    views.append(_view(store, authority))

    # 4. Snapshot confirmed.
    job = authority.confirm_package_update_snapshot(
        job.job_id, _canonical(ownership, identity)
    )
    views.append(_view(store, authority))

    # 5. Package mutation armed.
    outcome, arm, job = authority.arm_package_update_mutation(
        job.job_id, job_packages(job), prepared_evidence_digest=ARBITRARY_EVIDENCE_DIGEST
    )
    assert outcome is PackageUpdateExecutionOutcome.MATCHED
    views.append(_view(store, authority))

    # 6. Package mutation completed.
    job = authority.complete_package_update_mutation(job.job_id, _post_state(job))
    views.append(_view(store, authority))

    # 7. Health evaluation started.
    job = authority.start_package_update_health(job.job_id)
    views.append(_view(store, authority))

    # 8. Health evaluation completed -- PASSED, job terminalizes SUCCEEDED.
    observations = _observations(
        job, [HealthProbeOutcome.PASSED] * len(job.health_probes)
    )
    job = authority.complete_package_update_health(job.job_id, observations)
    views.append(_view(store, authority))
    assert job.status is PackageUpdateJobStatus.SUCCEEDED

    # Every transition that changed the published job summary strictly
    # advanced the revision -- except the snapshot-task recording, which
    # changes nothing published and is asserted unchanged explicitly.
    revisions = [view.published_state_revision for view in views]
    for index in range(1, len(revisions)):
        if index == 4:  # the snapshot-task-UPID view, appended above
            assert revisions[index] == revisions[index - 1], (
                "recording the observed PVE task UPID must not bump the "
                "published revision: it changes no published field"
            )
            continue
        assert revisions[index] > revisions[index - 1], (
            f"published transition {index} did not advance "
            "published_state_revision"
        )

    # And every one of those views -- publication N alongside publication
    # N+1 -- is accepted by the real Home Assistant successor contract.
    # `validate_transition` would reject ANY of these pairs as a mutable
    # view under one revision if the fix above were missing.
    previous = None
    for view in views:
        current = contract_snapshot(view)
        if previous is not None:
            current.validate_revision_successor(previous)
        previous = current


def test_snapshot_task_recording_does_not_bump_publication_revision(
    tmp_path: Path,
) -> None:
    """A narrow negative control for the bounded-audit requirement.

    ``record_package_update_snapshot_task`` and
    ``record_package_update_snapshot_uncertain`` write durable evidence
    inside the same active checkpoint, but neither changes any field the
    published ``package_update_job`` summary carries (job_id, checkpoint,
    issued_at, health_outcome, snapshot_confirmed_at, mutation_completed_at,
    rollback_completed_at, terminalized_at, terminal_reason). Bumping the
    published revision for either would be exactly the "internal
    event-log-only change" the finding says must NOT bump it.
    """

    clock, store, authority, resource, scan, approval, job = _ready_job_at_snapshot_intent(
        tmp_path
    )
    before = _view(store, authority)

    authority.record_package_update_snapshot_task(job.job_id, UPID)
    after_task = _view(store, authority)
    assert after_task.published_state_revision == before.published_state_revision
    assert after_task.resources == before.resources

    authority.record_package_update_snapshot_uncertain(job.job_id, "transport timed out")
    after_uncertain = _view(store, authority)
    assert (
        after_uncertain.published_state_revision == before.published_state_revision
    )
    assert after_uncertain.resources == before.resources


def _ready_job_at_snapshot_intent(tmp_path: Path):
    """One issued job advanced to the write-ahead snapshot intent."""

    clock, store, authority, resource, scan, approval = _approved_system(tmp_path)
    job = _issue(authority, resource, approval)
    authority.record_package_update_preflight_passed(job.job_id)
    job = authority.record_package_update_snapshot_intent(job.job_id)
    return clock, store, authority, resource, scan, approval, job


def test_execution_plan_mismatch_blocks_the_job_and_bumps_the_revision(
    tmp_path: Path,
) -> None:
    """A terminal ``blocked`` transition is a published change too."""

    from app.inventory import PackageScanPackage

    clock, store, authority, resource, scan, approval, job = _ready_job(tmp_path)
    before = _view(store, authority)

    outcome, job = authority.evaluate_package_update_execution_plan(
        job.job_id,
        (
            PackageScanPackage(
                package_name="some-other-package",
                architecture="amd64",
                installed_version="1",
                candidate_version="2",
            ),
        ),
    )

    assert outcome is PackageUpdateExecutionOutcome.MISMATCHED
    assert job.status is PackageUpdateJobStatus.BLOCKED
    after = _view(store, authority)
    assert after.published_state_revision > before.published_state_revision

    current = contract_snapshot(after)
    current.validate_revision_successor(contract_snapshot(before))


def test_restart_recovery_interruption_bumps_the_revision(tmp_path: Path) -> None:
    """Startup recovery terminalizing a pre-mutation job is a published change."""

    clock, store, authority, resource, scan, approval = _approved_system(tmp_path)
    job = _issue(authority, resource, approval)
    before = _view(store, authority)

    recovered = authority.recover_interrupted_package_update_jobs()

    assert recovered == (job.job_id,)
    after = _view(store, authority)
    assert after.published_state_revision > before.published_state_revision
    assert (
        after.resources[0]["package_update_job"]["state"]
        == PackageUpdateJobStatus.INTERRUPTED.value
    )

    current = contract_snapshot(after)
    current.validate_revision_successor(contract_snapshot(before))
