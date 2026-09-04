"""The one production worker that continues explicitly started update jobs.

This module is the production activation of the update lifecycle, and it is
deliberately a *composition* of the stages PRs #67-#73 already built. It
contains no state machine of its own, no snapshot/mutation/rollback/health
logic, no retry policy, and no compensation policy.

## What it may and may not do

```text
an authenticated operator explicitly starts an update
  -> InventoryAuthority.issue_package_update_job      (durable, ACTIVE @ issued)
  -> this worker is WOKEN                             (a hint, never authority)
  -> it re-reads the durable job and composes:
        snapshot orchestration          -> snapshot_confirmed
        execution-time plan equality    -> matched, nothing durable changed
        mutation orchestration          -> mutation_completed
        health orchestration            -> SUCCEEDED
```

**It never issues a job.** There is no call to
``issue_package_update_job`` anywhere in this file, no timer that could make
one, and no path from a package scan, a discovery cycle, an approval, or a
Home Assistant poll into one. Continuing a job an operator already started is
not auto-update; inventing a job nobody asked for would be, and this worker
structurally cannot.

**It never arms a rollback.** ``arm_package_update_rollback`` appears nowhere
here. The worker only ever enters the rollback stage for a job whose durable
checkpoint is ALREADY ``rollback_may_have_started`` -- a boundary that only an
authenticated operator's explicit rollback request (or a crash after one) can
have committed. A failed health verdict, an unknown health verdict, a failed
mutation, and an uncertain mutation each leave the worker idle with the job
still ACTIVE and rollback-capable, and submit nothing.

**It invents no retry policy.** One wake performs at most one attempt of each
stage the job is actually at. There is no retry count, no backoff, no grace
period, no threshold, and no interval. When a stage says the job remains owned
but has no truthful next automatic step, the worker stops and stays idle for
that job until an operator explicitly asks again.

## Wake-driven, and the durable job is the only authority

The worker sleeps on an in-process :class:`threading.Event`. A wake is a hint
that something *may* have changed; it is never permission to act and never a
belief about what state a job is in. Every cycle re-reads the one globally
active job from the authority database, and every stage transition re-reads
the job again before deciding what to do next. Nothing in this process's
memory grants a mutation.

Durable global single-flight (the ``one_active_package_update_job_globally``
unique index) remains the real concurrency authority. The in-process cycle
lock below only stops this one worker from running two cycles at once; it is
not, and must never be treated as, the thing that stops two mutations.

## Host I/O and the writer lock

Every host round trip happens inside the stage orchestrators, which already
decide which of them may run inside the authority store's short critical
sections and which must not. This worker adds no transaction of its own and
holds no lock across host I/O.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
import logging
import threading

from app.inventory import (
    AuthorityConflict,
    AuthorityNotFound,
    CHECKPOINT_ORDER,
    InventoryAuthority,
    InventoryAuthorityStore,
    PackageUpdateCheckpoint,
    PackageUpdateJob,
    PackageUpdateJobStatus,
    checkpoint_rank,
)
from app.package_update_execution import (
    ExecutionGateStatus,
    PackageUpdateExecutionHostControl,
    run_package_update_execution_gate,
)
from app.package_update_health import (
    HealthStageStatus,
    PackageUpdateHealthError,
    PackageUpdateHealthOrchestrator,
)
from app.package_update_mutation import (
    MutationStageResult,
    MutationStageStatus,
    PackageUpdateMutationError,
    PackageUpdateMutationOrchestrator,
)
from app.package_update_rollback import (
    PackageUpdateRollbackError,
    PackageUpdateRollbackOrchestrator,
    RollbackOperationOutcome,
)
from app.package_update_snapshot import (
    PackageUpdateSnapshotError,
    PackageUpdateSnapshotOrchestrator,
    SnapshotOperationOutcome,
)

_LOGGER = logging.getLogger(__name__)


class PackageUpdateWorkerCycleStatus(StrEnum):
    """How one worker cycle ended."""

    #: No job owns the global slot. Nothing to do, and nothing was touched.
    IDLE = "idle"
    #: The job reached a terminal status during this cycle.
    TERMINAL = "terminal"
    #: The job remains ACTIVE and owned, and there is no truthful next
    #: automatic step. The worker goes idle for it; only an explicit operator
    #: action may ask again.
    STOPPED = "stopped"
    #: Another cycle was already running in this process. Nothing was touched.
    BUSY = "busy"
    #: One cycle raised an unexpected exception. Durable authority facts are
    #: untouched beyond whatever a stage had already truthfully recorded, and
    #: the worker stays alive.
    ERROR = "error"


#: Every reason this worker stops progressing a still-ACTIVE job. Closed on
#: purpose: an unnamed stop is a bug, not a fallback, and a regression test
#: proves nothing here means "retry in N seconds" or "roll back".
PACKAGE_UPDATE_WORKER_STOP_REASONS: frozenset[str] = frozenset(
    {
        # -- snapshot stage -------------------------------------------------
        "snapshot_failed",
        "snapshot_uncertain",
        "snapshot_not_submitted",
        # -- execution-time plan equality gate ------------------------------
        "execution_plan_mismatched",
        "execution_authority_stale",
        "execution_authority_temporarily_unavailable",
        "execution_host_failure",
        "execution_job_not_ready",
        # -- package mutation -----------------------------------------------
        "mutation_running",
        "mutation_terminal_failure",
        "mutation_uncertain",
        "mutation_not_submitted",
        "mutation_mismatched",
        "mutation_authority_stale",
        "mutation_authority_temporarily_unavailable",
        "mutation_host_failure",
        "mutation_job_not_ready",
        # -- health evaluation ----------------------------------------------
        #: A frozen probe was positively proven false. The job stays ACTIVE,
        #: keeps its snapshot, and is rollback-capable. NOTHING is submitted.
        "health_failed",
        #: No verdict could be reached truthfully. Nothing durable was
        #: written; the job stays ACTIVE at `health_started` and an operator
        #: may explicitly ask for another read-only evaluation.
        "health_unknown",
        # -- same-job rollback (only ever entered already-armed) ------------
        "rollback_failed",
        "rollback_uncertain",
        "rollback_not_submitted",
        # -- structural ------------------------------------------------------
        #: The job's durable state changed underneath this cycle (an operator
        #: rollback request winning a race, most typically). Nothing was
        #: forced; the next wake re-reads the new truth.
        "stage_precondition_changed",
        #: The job sits at a durable checkpoint this worker has no truthful
        #: automatic continuation for.
        "no_automatic_continuation",
    }
)

#: A hard bound on stage transitions in one cycle. Each iteration must strictly
#: advance the durable checkpoint or the cycle stops, so this is a structural
#: termination guarantee, not a retry budget: no checkpoint is ever attempted
#: twice within one cycle.
_MAX_STAGE_TRANSITIONS_PER_CYCLE = len(CHECKPOINT_ORDER)

#: Checkpoints from which the snapshot stage is the truthful next step.
_SNAPSHOT_CHECKPOINTS = (
    PackageUpdateCheckpoint.ISSUED,
    PackageUpdateCheckpoint.PREFLIGHT_PASSED,
    PackageUpdateCheckpoint.SNAPSHOT_MAY_HAVE_STARTED,
)


@dataclass(frozen=True, slots=True)
class PackageUpdateWorkerCycle:
    """What one worker cycle observed and did. Bounded, typed, no host output."""

    status: PackageUpdateWorkerCycleStatus
    job_id: str | None = None
    job_status: PackageUpdateJobStatus | None = None
    checkpoint: PackageUpdateCheckpoint | None = None
    stop_reason: str | None = None


class PackageUpdateWorker:
    """One bounded worker that continues EXISTING explicit update jobs.

    Exactly one instance exists per backend process, it owns exactly one
    thread, and it creates no thread pool and no per-resource worker. That is
    affordable precisely because the durable authority already permits only
    one active job globally: a per-resource pool would be concurrency this
    product has deliberately made impossible.
    """

    def __init__(
        self,
        authority: InventoryAuthority,
        store: InventoryAuthorityStore,
        *,
        snapshot: PackageUpdateSnapshotOrchestrator,
        execution_host_control: PackageUpdateExecutionHostControl,
        mutation: PackageUpdateMutationOrchestrator,
        rollback: PackageUpdateRollbackOrchestrator,
        health: PackageUpdateHealthOrchestrator,
        post_update_scan_wake: Callable[[], None] | None = None,
    ) -> None:
        self._authority = authority
        self._store = store
        self._snapshot = snapshot
        self._execution_host_control = execution_host_control
        self._mutation = mutation
        self._rollback = rollback
        self._health = health
        self._post_update_scan_wake = post_update_scan_wake
        self._cycle_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """Start the single worker thread.

        The thread's FIRST action is one recovery cycle: the authority's own
        startup recovery has already terminalized every provably pre-mutation
        job, so whatever still owns the global slot is a durable uncertain
        state one of the stages knows how to re-observe.
        """

        if self.is_running:
            return
        self._stop_event.clear()
        self._wake_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="hubinet-package-update-worker",
            daemon=True,
        )
        self._thread.start()

    def wake(self) -> None:
        """Hint that durable job state may have changed.

        A wake is never authority and never carries a job id, a stage, or a
        checkpoint: the worker re-reads the durable truth itself. Waking an
        already-running cycle is harmless -- the event stays set and the loop
        runs one more cycle, which re-reads state and may do nothing.
        """

        self._wake_event.set()

    def configure_post_update_scan_wake(self, wake: Callable[[], None]) -> None:
        """Connect the independent scan scheduler before this worker starts."""

        if self.is_running:
            raise RuntimeError("post-update scan wake must be configured before start")
        self._post_update_scan_wake = wake

    def stop(self, *, grace_seconds: float = 30.0) -> None:
        """Stop scheduling NEW work and wait, bounded, for the current cycle.

        This deliberately does NOT interrupt a stage that is already inside a
        bounded host operation. Killing a submitted package mutation or PVE
        rollback and then resetting backend authority would be exactly the
        lie this product refuses to tell: the host's own operation journal and
        the durable checkpoints stay the truth, and a cycle that is still
        running when the grace elapses simply keeps its own stage's existing
        timeout and uncertainty semantics.
        """

        self._stop_event.set()
        self._wake_event.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=grace_seconds)

    def _run_loop(self) -> None:
        # One recovery cycle before ever waiting, so a backend that restarted
        # while a job owned an uncertain durable state re-observes it without
        # needing an operator to ask.
        while not self._stop_event.is_set():
            try:
                self.run_once()
            except Exception as exc:  # noqa: BLE001 - never kill the worker
                _LOGGER.error(
                    "package update worker cycle failed unexpectedly: %s",
                    type(exc).__name__,
                )
            if self._stop_event.is_set():
                return
            # Wake-driven, not polled: with nothing to do the worker blocks
            # here indefinitely rather than re-reading the database on a
            # timer. Only an explicit operator action (start, resume,
            # rollback) or shutdown sets this event.
            self._wake_event.wait()
            self._wake_event.clear()

    # ------------------------------------------------------------------
    # One cycle
    # ------------------------------------------------------------------

    def run_once(self) -> PackageUpdateWorkerCycle:
        """Re-read durable state and drive the one active job, if any."""

        if not self._cycle_lock.acquire(blocking=False):
            return PackageUpdateWorkerCycle(
                status=PackageUpdateWorkerCycleStatus.BUSY
            )
        try:
            job = self._store.active_package_update_job()
            if job is None:
                return PackageUpdateWorkerCycle(
                    status=PackageUpdateWorkerCycleStatus.IDLE
                )
            try:
                return self._drive(job.job_id)
            except Exception as exc:  # noqa: BLE001 - bounded, redacted
                # The error boundary. No durable authority fact is written
                # here, no success is synthesized, and the global slot is
                # never cleared merely to regain liveness: whatever a stage
                # already truthfully recorded is the state that survives.
                # Only the exception TYPE is logged -- never its message,
                # never helper stdout/stderr, never a credential.
                _LOGGER.error(
                    "package update worker cycle failed for job %s: %s",
                    job.job_id,
                    type(exc).__name__,
                )
                return PackageUpdateWorkerCycle(
                    status=PackageUpdateWorkerCycleStatus.ERROR,
                    job_id=job.job_id,
                )
        finally:
            self._cycle_lock.release()

    def _drive(self, job_id: str) -> PackageUpdateWorkerCycle:
        """Compose the existing stages for one job until it stops advancing."""

        previous_rank: int | None = None
        for _ in range(_MAX_STAGE_TRANSITIONS_PER_CYCLE):
            try:
                job = self._authority.package_update_job(job_id)
            except AuthorityNotFound:
                return PackageUpdateWorkerCycle(
                    status=PackageUpdateWorkerCycleStatus.IDLE, job_id=job_id
                )
            if job.status is not PackageUpdateJobStatus.ACTIVE:
                return self._terminal(job)

            rank = checkpoint_rank(job.checkpoint)
            if previous_rank is not None and rank <= previous_rank:
                # A stage returned "advance" without advancing the durable
                # checkpoint. Refuse to loop on it rather than inventing a
                # retry: the durable record, not this cycle's optimism, says
                # what happened.
                return self._stopped(job, "no_automatic_continuation")
            previous_rank = rank

            try:
                outcome = self._step(job)
            except (
                PackageUpdateSnapshotError,
                PackageUpdateMutationError,
                PackageUpdateRollbackError,
                PackageUpdateHealthError,
                AuthorityConflict,
            ):
                # A stage refused because the job's durable state moved
                # between this cycle's read and its own re-proof -- an
                # operator's explicit rollback request winning a race is the
                # ordinary case. Nothing is forced; the next wake re-reads.
                return self._stopped(
                    self._reread(job), "stage_precondition_changed"
                )
            if outcome is not None:
                return outcome
        return self._stopped(
            self._reread(self._authority.package_update_job(job_id)),
            "no_automatic_continuation",
        )

    def _step(self, job: PackageUpdateJob) -> PackageUpdateWorkerCycle | None:
        """Run the ONE stage this durable checkpoint calls for.

        Returns ``None`` to mean "the durable checkpoint advanced; re-read and
        continue", or a terminal cycle result.
        """

        checkpoint = job.checkpoint
        if checkpoint in _SNAPSHOT_CHECKPOINTS:
            return self._run_snapshot(job)
        if checkpoint is PackageUpdateCheckpoint.SNAPSHOT_CONFIRMED:
            return self._run_execution_gate_then_mutation(job)
        if checkpoint is PackageUpdateCheckpoint.MUTATION_MAY_HAVE_STARTED:
            return self._run_mutation_recovery(job)
        if checkpoint in (
            PackageUpdateCheckpoint.MUTATION_COMPLETED,
            PackageUpdateCheckpoint.HEALTH_STARTED,
        ):
            return self._run_health(job)
        if checkpoint is PackageUpdateCheckpoint.HEALTH_COMPLETED:
            # Reachable while ACTIVE only with a FAILED verdict -- a passing
            # one is inseparable from `succeeded`, which the ACTIVE guard
            # above already excluded. The job keeps its snapshot and its
            # rollback authority, and this worker submits nothing: PRODUCT.md
            # has made no automatic compensation decision, and inventing one
            # here would be inventing it.
            return self._stopped(job, "health_failed")
        if checkpoint is PackageUpdateCheckpoint.ROLLBACK_MAY_HAVE_STARTED:
            return self._run_rollback(job)
        return self._stopped(job, "no_automatic_continuation")

    # ------------------------------------------------------------------
    # Stages -- composition only
    # ------------------------------------------------------------------

    def _run_snapshot(self, job: PackageUpdateJob) -> PackageUpdateWorkerCycle | None:
        result = self._snapshot.ensure_job_owned_snapshot(job.job_id)
        if result.outcome is SnapshotOperationOutcome.COMPLETED:
            return None
        return self._stopped(
            self._reread(result.job),
            {
                SnapshotOperationOutcome.FAILED: "snapshot_failed",
                SnapshotOperationOutcome.UNCERTAIN: "snapshot_uncertain",
                SnapshotOperationOutcome.NOT_SUBMITTED: "snapshot_not_submitted",
            }[result.outcome],
        )

    def _run_execution_gate_then_mutation(
        self, job: PackageUpdateJob
    ) -> PackageUpdateWorkerCycle | None:
        """Prove exact plan equality FIRST, then run the mutation stage.

        The gate runs even though the mutation stage re-proves exact material
        itself immediately before it arms. That is deliberate and is not
        redundancy to be optimized away: the gate is the cheap, entirely
        non-mutating refusal that keeps a drifted plan from ever reaching the
        stage that owns the real package command, and a successful pass here
        is explicitly NOT a durable permit -- nothing about the job changes,
        and the mutation stage still re-proves everything in the same
        transaction that commits its write-ahead boundary.
        """

        gate = run_package_update_execution_gate(
            self._authority, job.job_id, self._execution_host_control
        )
        if gate.status is not ExecutionGateStatus.MATCHED:
            return self._stopped(
                self._reread(gate.job),
                {
                    ExecutionGateStatus.MISMATCHED: "execution_plan_mismatched",
                    ExecutionGateStatus.AUTHORITY_STALE: "execution_authority_stale",
                    ExecutionGateStatus.AUTHORITY_TEMPORARILY_UNAVAILABLE: (
                        "execution_authority_temporarily_unavailable"
                    ),
                    ExecutionGateStatus.HOST_FAILURE: "execution_host_failure",
                    ExecutionGateStatus.JOB_NOT_READY: "execution_job_not_ready",
                }[gate.status],
            )
        result = self._mutation.execute_job_owned_mutation(job.job_id)
        return self._after_mutation(job, result)

    def _run_mutation_recovery(
        self, job: PackageUpdateJob
    ) -> PackageUpdateWorkerCycle | None:
        """Resolve an already-armed mutation from durable evidence alone.

        Never prepares, never arms, and never submits -- that asymmetry lives
        in the mutation stage itself and is exactly what makes a restart safe.
        """

        result = self._mutation.recover_job_owned_mutation(job.job_id)
        return self._after_mutation(job, result)

    def _after_mutation(
        self, job: PackageUpdateJob, result: MutationStageResult
    ) -> PackageUpdateWorkerCycle | None:
        status = result.status
        if status is MutationStageStatus.COMPLETED:
            return None
        return self._stopped(
            self._reread(result.job if result.job is not None else job),
            {
                MutationStageStatus.RUNNING: "mutation_running",
                MutationStageStatus.TERMINAL_FAILURE: "mutation_terminal_failure",
                MutationStageStatus.UNCERTAIN: "mutation_uncertain",
                MutationStageStatus.NOT_SUBMITTED: "mutation_not_submitted",
                MutationStageStatus.MISMATCHED: "mutation_mismatched",
                MutationStageStatus.AUTHORITY_STALE: "mutation_authority_stale",
                MutationStageStatus.AUTHORITY_TEMPORARILY_UNAVAILABLE: (
                    "mutation_authority_temporarily_unavailable"
                ),
                MutationStageStatus.HOST_FAILURE: "mutation_host_failure",
                MutationStageStatus.JOB_NOT_READY: "mutation_job_not_ready",
            }[status],
        )

    def _run_health(self, job: PackageUpdateJob) -> PackageUpdateWorkerCycle | None:
        """Perform ONE truthful read-only health attempt.

        One wake, one attempt. An UNKNOWN verdict leaves the job ACTIVE at
        `health_started` with its snapshot and rollback authority intact and
        the worker idle for it. There is deliberately no interval retry, no
        backoff, no grace period, no attempt count, and no threshold here:
        PR #73 invented none, and production activation is not the place to
        invent one either. An operator asks again through the explicit resume
        control.
        """

        result = self._health.evaluate_job_health(job.job_id)
        if result.status is HealthStageStatus.PASSED:
            return None
        return self._stopped(
            self._reread(result.job),
            {
                HealthStageStatus.FAILED: "health_failed",
                HealthStageStatus.UNKNOWN: "health_unknown",
            }[result.status],
        )

    def _run_rollback(self, job: PackageUpdateJob) -> PackageUpdateWorkerCycle | None:
        """Continue a rollback an operator already durably requested.

        Only ever entered at ``rollback_may_have_started``, which is a
        boundary this worker cannot commit: it is written by
        ``arm_package_update_rollback``, which this module never calls, from
        the authenticated operator rollback route alone. The empty observation
        sequence is correct and load-bearing rather than a shortcut -- the
        rollback stage re-derives the target from durable authority and does
        not consult a listing for an already-armed job, because re-deciding
        eligibility for a guest that may already be being rolled back would be
        exactly the wrong thing to do.
        """

        if job.checkpoint is not PackageUpdateCheckpoint.ROLLBACK_MAY_HAVE_STARTED:
            return self._stopped(job, "no_automatic_continuation")
        result = self._rollback.roll_back_to_job_snapshot(job.job_id, ())
        if result.outcome is RollbackOperationOutcome.COMPLETED:
            return self._terminal(self._reread(result.job))
        return self._stopped(
            self._reread(result.job),
            {
                RollbackOperationOutcome.FAILED: "rollback_failed",
                RollbackOperationOutcome.UNCERTAIN: "rollback_uncertain",
                RollbackOperationOutcome.NOT_SUBMITTED: "rollback_not_submitted",
            }[result.outcome],
        )

    # ------------------------------------------------------------------
    # Result helpers
    # ------------------------------------------------------------------

    def _reread(self, job: PackageUpdateJob | None) -> PackageUpdateJob | None:
        """Report the job as the DATABASE currently has it, not as a stage saw it."""

        if job is None:
            return None
        try:
            return self._authority.package_update_job(job.job_id)
        except AuthorityNotFound:  # pragma: no cover - defensive
            return job

    def _terminal(self, job: PackageUpdateJob) -> PackageUpdateWorkerCycle:
        if (
            job.status is PackageUpdateJobStatus.SUCCEEDED
            and self._post_update_scan_wake is not None
        ):
            # The durable request already exists in the same transaction that
            # made the job SUCCEEDED.  This call carries no authority and is
            # only a latency hint; failure cannot rewrite successful history.
            try:
                self._post_update_scan_wake()
            except Exception as exc:  # noqa: BLE001 - durable request survives
                _LOGGER.error(
                    "post-update package scan wake failed for job %s: %s",
                    job.job_id,
                    type(exc).__name__,
                )
        return PackageUpdateWorkerCycle(
            status=PackageUpdateWorkerCycleStatus.TERMINAL,
            job_id=job.job_id,
            job_status=job.status,
            checkpoint=job.checkpoint,
        )

    @staticmethod
    def _stopped(
        job: PackageUpdateJob | None, reason: str
    ) -> PackageUpdateWorkerCycle:
        if reason not in PACKAGE_UPDATE_WORKER_STOP_REASONS:  # pragma: no cover
            raise AssertionError("worker stop reason is outside the closed set")
        return PackageUpdateWorkerCycle(
            status=PackageUpdateWorkerCycleStatus.STOPPED,
            job_id=None if job is None else job.job_id,
            job_status=None if job is None else job.status,
            checkpoint=None if job is None else job.checkpoint,
            stop_reason=reason,
        )
