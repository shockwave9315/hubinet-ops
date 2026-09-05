"""Dark same-job rollback execution for package update jobs.

**Production-reachable.** `app/inventory_runtime.py` builds this orchestrator
and its dedicated SSH host control as part of the package-update worker, and
both bootstrap and the product updater install the matching forced-command
helper and its own key (`deploy/lib/bootstrap-update-boundaries.sh`,
`deploy/lib/update-boundaries.sh`). Reachability is NOT policy: NOTHING calls
a rollback automatically. The worker may never arm one, no scheduler, startup
path, or health verdict triggers one, and
`tests/test_r0_architecture_regression.py` enforces that.

It contains no snapshot deletion, no retention, and no automatic rollback
policy: a caller must ask for one exact job to be rolled back. Health
execution now exists (`app/package_update_health.py`) and a proven health
failure is a legal entry point here, but nothing connects the two: a failed
health verdict leaves the job active and rollback-CAPABLE, and never calls
anything in this module. Deciding *when* compensation should happen
automatically is a policy this product has not made, and inventing one here
would be inventing a product decision (see `PRODUCT.md`, `STATUS.md`).

## What the orchestrator guarantees

```text
job ACTIVE at mutation_may_have_started, mutation_completed, health_started,
  or health_completed with outcome=failed
  -> fresh canonical PVE snapshot listing
  -> exact same-job rollback target (authority-selected, never caller-named)
  -> DURABLY COMMIT rollback_may_have_started  (write-ahead, before any call)
  -> read the durable host operation state (never requires a current context)
  -> if, and only if, that state proves no submission has crossed the door:
       hold the rollback context through a SHORT critical section and submit
  -> record the observed PVE task identity the instant it is known
  -> read-only task polling (outside every lock)
  -> terminal PVE task evidence, by PVE's own success rule
  -> fresh canonical listing proving current parent == this job's snapshot
  -> rollback_completed  ->  status = ROLLED_BACK
```

Every one of those checkpoints is a legal entry point, and that is deliberate.
It is the reason schema v14 exists, and schema v16 extends exactly the same
rule to the health branch: a package mutation that failed, was partial, timed
out, was killed, or simply could not be proven complete never reaches
`mutation_completed`, and a health evaluation that was interrupted or could
not reach a verdict never reaches `health_completed` -- yet those are exactly
the jobs that most need compensating. Requiring an earlier stage's SUCCESS
before allowing compensation would fence out the only guests that need it.
Nothing in this module writes, or reads as permission,
`mutation_completed_at`, `health_completed_at`, or `health_outcome`.

A job whose health verdict PASSED is deliberately not eligible: a passing
verdict and `status=succeeded` are one indivisible durable fact, so such a job
is terminal and is refused before eligibility is even consulted.

## The guest is left STOPPED

Verified upstream: `PVE::AbstractConfig::snapshot_rollback` calls
`__snapshot_rollback_vm_stop`, which for LXC is `PVE::LXC::vm_stop($vmid, 1)`
-- a forced stop -- and the endpoint restarts the container afterwards only
when its own `start` parameter is set. This stage pins `start` to 0 as a
code-owned host-side constant, so a successful rollback always leaves the
guest stopped. Restarting it, and validating it afterwards, are deliberately
separate future work rather than three destructive concerns fused into one
operation.

## Verified PVE rollback semantics

Established from current Proxmox VE sources, not inherited from snapshot
create and not from Hubinet 0.4:

- `POST /nodes/{node}/lxc/{vmid}/snapshot/{snapname}/rollback` is protected,
  takes an optional `start` boolean (default 0), and requires `VM.Snapshot` or
  `VM.Snapshot.Rollback` on `/vms/{vmid}`.
- The endpoint uses `fork_worker('vzrollback', ...)`, but local `pvesh` CLI
  runs it synchronously and emits the final task id only after worker output.
  The host helper detaches that CLI call and recovers the exact UPID from its
  durable bounded capture, so a returned submission response proves nothing.
- `snapshot_rollback` refuses, with these exact upstream conditions: a
  template; a snapshot that does not exist; a snapshot still carrying
  `snapstate` ("unable to rollback to incomplete snapshot"); a config already
  carrying another lock (`check_lock`); and a container still running after
  the forced stop.
- It takes the config lock as `rollback` for the whole operation, replaces
  the current config from the snapshot (`__snapshot_apply_config`), moves
  displaced volumes to `unused`, and sets `parent` to the snapshot name.
- It never deletes its source snapshot.
- Task terminal semantics are PVE's own, identical to snapshot create:
  `status` in `running`/`stopped` plus an optional `exitstatus`, where only
  `OK` and `WARNINGS: <n>` are non-errors.

So `parent == snapshot_name` on PVE's synthetic `current` row is a genuine
post-condition of a successful rollback, and this module requires it -- but
only as one member of a coherent evidence set. It is not unique (two
rollbacks to the same snapshot leave the same value), so it can never
identify one operation on its own, and the source snapshot still existing is
no evidence at all, since upstream guarantees that either way.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
import time
from typing import Protocol

from app.inventory import (
    AuthorityConflict,
    HostRollbackState,
    InventoryAuthority,
    ObservedSnapshot,
    PackageUpdateCheckpoint,
    PackageUpdateJob,
    PackageUpdateJobStatus,
    PackageUpdateRollbackRequest,
    PackageUpdateRollbackTarget,
    RollbackSubmissionRefusedBeforeCallback,
)
from app.package_update_snapshot import SnapshotTaskStatus


class RollbackOperationOutcome(StrEnum):
    """How a dark host rollback operation ended, from the caller's side."""

    #: Terminal non-error PVE task plus fresh canonical evidence of this
    #: job's own snapshot and the post-rollback `parent` post-condition.
    COMPLETED = "completed"
    #: The PVE rollback task terminated in a failure state.
    FAILED = "failed"
    #: The outcome could not be established. Never a licence to resubmit.
    UNCERTAIN = "uncertain"
    #: The host PROVED, from its durable operation journal, that no rollback
    #: was ever submitted for this operation identity. The only outcome that
    #: may release a job already past its write-ahead rollback checkpoint,
    #: and never inferred from a canonical absence, an error name, a timeout,
    #: or a transport failure.
    NOT_SUBMITTED = "not_submitted"


class PackageUpdateRollbackError(RuntimeError):
    """A dark rollback operation could not be carried out safely."""


@dataclass(frozen=True, slots=True)
class HostRollbackResult:
    """The bounded, typed answer one dark host rollback operation returns."""

    outcome: RollbackOperationOutcome
    rollback_operation_id: str
    #: Present as soon as the host durably knew the PVE task identity.
    task_upid: str | None = None
    task: SnapshotTaskStatus | None = None
    #: A fresh canonical listing. ``None`` means the host could not obtain one.
    snapshots: tuple[ObservedSnapshot, ...] | None = None
    #: Bounded classification/reason text. Never raw PVE logs or command text.
    reason: str | None = None
    #: The host's own durable journal phase for this operation, read directly
    #: off it. ``None`` whenever the host could not report one at all -- an
    #: older host predating this field, a transport failure, or any host-side
    #: error including :attr:`host_operation_in_progress`. The caller must
    #: treat ``None`` exactly like ``SUBMITTED`` -- never a licence to submit.
    rollback_state: HostRollbackState | None = None
    #: The host refused this read because the per-VMID lease was already
    #: held, which is what the detached destructive runner does for the whole
    #: of its physical `pvesh`. INTERNAL CONTROL FLOW ONLY.
    #:
    #: Set from the helper's exact typed error classification, never from
    #: reason text; an absent/unknown/malformed classification leaves it
    #: ``False`` so an older or misbehaving host fails closed.
    #:
    #: It is NOT authority: not success, not failure, not absence, not
    #: submission proof, and it never reaches publication or HA. It may
    #: extend bounded read-only polling ONLY when the orchestrator already
    #: holds durable ``submitted``/``task_known`` evidence for this exact
    #: operation; before that proof exists it is plain UNKNOWN.
    host_operation_in_progress: bool = False


class PackageUpdateRollbackHostControl(Protocol):
    """The dark typed host boundary this orchestrator is allowed to use.

    Deliberately narrow: three operations, no snapshot creation, no snapshot
    deletion, no lifecycle control, no generic action dispatcher, and no place
    to pass a command string or a caller-chosen snapshot name.
    """

    def submit_same_job_rollback(
        self, request: PackageUpdateRollbackRequest
    ) -> HostRollbackResult:
        """Submit, or reattach to, this exact job's rollback operation.

        Submission-only: it journals `submitted`, starts one detached fixed
        local `pvesh create`, and returns. Inspect later promotes only an exact
        completed capture to `task_known`, so it is safe to call while the
        backend holds its authority writer lock across it.
        """

    def inspect_rollback_state(
        self, request: PackageUpdateRollbackRequest
    ) -> HostRollbackResult:
        """Read current durable and canonical state without submitting."""

    def seal_rollback_never_submitted(
        self, request: PackageUpdateRollbackRequest
    ) -> HostRollbackResult:
        """Durably forbid this exact operation from ever being submitted."""


@dataclass(frozen=True, slots=True)
class RollbackStageResult:
    """The durable result of one dark rollback stage attempt."""

    outcome: RollbackOperationOutcome
    job: PackageUpdateJob
    reason: str | None = None


#: Bounded polling of one in-flight rollback: a journaled PVE task, or a
#: `submitted` rollback whose detached host capture has not yet yielded the
#: exact UPID. Either still in flight when this elapses stays UNCERTAIN --
#: never failed, never resubmitted. The
#: default is generous because a rollback force-stops the container and then
#: rolls back every volume, which on a large mountpoint is not quick.
DEFAULT_TASK_POLL_TIMEOUT_SECONDS = 1800.0
DEFAULT_TASK_POLL_INTERVAL_SECONDS = 2.0

#: Every checkpoint this orchestrator may be entered at. The four authority
#: entry points plus ``rollback_may_have_started`` itself, which is how a
#: crashed attempt re-enters to reattach rather than resubmit.
#:
#: The health additions carry exactly the same meaning the mutation ones do:
#: a job whose health evaluation was interrupted or could not reach a verdict
#: (``health_started``), and one whose frozen contract was PROVEN to fail
#: (``health_completed``, which is only ever reachable while active with a
#: FAILED verdict), are both jobs that may need compensating. Requiring
#: health success before rollback would fence exactly the guests that need
#: it, in the same way requiring mutation success once did.
_ELIGIBLE_CHECKPOINTS = (
    PackageUpdateCheckpoint.MUTATION_MAY_HAVE_STARTED,
    PackageUpdateCheckpoint.MUTATION_COMPLETED,
    PackageUpdateCheckpoint.HEALTH_STARTED,
    PackageUpdateCheckpoint.HEALTH_COMPLETED,
    PackageUpdateCheckpoint.ROLLBACK_MAY_HAVE_STARTED,
)

#: Journal phases that still permit a NEW submission for this exact operation
#: identity. Everything else -- including ``None``, which only an older host
#: would report -- means a rollback may already have crossed PVE's door.
_SUBMISSION_PERMITTED_STATES = (HostRollbackState.ABSENT, HostRollbackState.INTENT)

#: A release proof is deliberately distinct from permission to submit. A
#: transient absent/intent observation can be invalidated by a delayed helper;
#: only the durable host seal may release the backend's global slot.
_RELEASE_PROVED_STATES = (HostRollbackState.SEALED_NOT_SUBMITTED,)


class PackageUpdateRollbackOrchestrator:
    """Coordinate authority and one dark host boundary for one rollback.

    Instantiated only by hermetic tests in this stage. It performs no package
    mutation, no snapshot creation, and no snapshot deletion.

    A NEW PVE rollback submission is only ever attempted from inside
    :meth:`InventoryAuthority.execute_rollback_submission_if_current`, which
    holds this backend's own authority writer lock across exactly one bounded
    submission-only host call. Recovering evidence about an operation that may
    already have been submitted -- reading the host's durable journal state,
    and polling a known task -- never requires that lock and happens entirely
    outside it.
    """

    def __init__(
        self,
        authority: InventoryAuthority,
        host_control: PackageUpdateRollbackHostControl,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        task_poll_timeout_seconds: float = DEFAULT_TASK_POLL_TIMEOUT_SECONDS,
        task_poll_interval_seconds: float = DEFAULT_TASK_POLL_INTERVAL_SECONDS,
    ) -> None:
        self._authority = authority
        self._host_control = host_control
        self._monotonic = monotonic
        self._sleep = sleep
        self._task_poll_timeout_seconds = task_poll_timeout_seconds
        self._task_poll_interval_seconds = task_poll_interval_seconds

    def select_rollback_target(
        self, job_id: str, observed: Sequence[ObservedSnapshot]
    ) -> PackageUpdateRollbackTarget:
        """Authorize the one snapshot this exact job may roll back to."""

        return self._authority.select_package_update_rollback_target(
            job_id, observed
        )

    def roll_back_to_job_snapshot(
        self, job_id: str, observed: Sequence[ObservedSnapshot]
    ) -> RollbackStageResult:
        """Drive one job's same-job rollback to a durable terminal answer.

        ``observed`` is a FRESH canonical PVE snapshot listing, used only to
        prove the same-job target through authority. There is deliberately no
        snapshot-name parameter anywhere on this surface: the target is a
        durable authority fact, and a caller who supplies a listing that does
        not contain this job's own snapshot gets a refusal, never a different
        snapshot.

        Re-entrant by design. A job already at the write-ahead rollback
        boundary after a crash skips straight to the host boundary, which
        reattaches to the operation this same deterministic identity already
        started rather than submitting a second destructive rollback.
        """

        job = self._authority.package_update_job(job_id)
        if job.status is not PackageUpdateJobStatus.ACTIVE:
            raise PackageUpdateRollbackError("package update job is terminal")
        if job.checkpoint not in _ELIGIBLE_CHECKPOINTS:
            raise PackageUpdateRollbackError(
                "package update job is not eligible for same-job rollback"
            )

        if job.checkpoint is not PackageUpdateCheckpoint.ROLLBACK_MAY_HAVE_STARTED:
            # A. Prove the exact same-job target and durably commit the
            # write-ahead boundary in ONE transaction. Everything after this
            # line must assume a PVE rollback may already have been
            # submitted.
            try:
                job = self._authority.arm_package_update_rollback(job_id, observed)
            except RollbackSubmissionRefusedBeforeCallback as exc:
                # Refused before anything durable about a rollback existed,
                # so there is nothing to seal and nothing to recover: the job
                # keeps its ownership and its snapshot exactly as they were.
                return RollbackStageResult(
                    outcome=RollbackOperationOutcome.NOT_SUBMITTED,
                    job=self._authority.package_update_job(job_id),
                    reason=str(exc),
                )

        request = self._authority.package_update_rollback_request(job.job_id)

        # B. READ the durable host operation state first. A pure read of
        # evidence that may already exist, so it never requires a current
        # context -- a stale incarnation must never cause it to be discarded.
        result = self._read_inspection(request)

        if result.rollback_state in _RELEASE_PROVED_STATES:
            # A prior attempt may have durably sealed the host and then died
            # before committing the backend block. Re-enter the idempotent
            # seal under the backend writer lock to finish that transition.
            return self._resolve_pre_rollback_block(job, request)

        if result.outcome is RollbackOperationOutcome.NOT_SUBMITTED:
            # Journal-backed transient pre-submit observation: routing
            # evidence only. Seal before deciding whether the slot may be
            # released.
            return self._resolve_pre_rollback_block(job, request)

        if result.rollback_state in _SUBMISSION_PERMITTED_STATES:
            # C. The host PROVED, from its own durable journal, that no
            # rollback has ever crossed the door for this exact operation
            # identity. This is the only branch that may mutate PVE, and the
            # whole of it runs inside the short authority critical section.
            try:
                result = self._authority.execute_rollback_submission_if_current(
                    job.job_id,
                    lambda: self._host_control.submit_same_job_rollback(request),
                )
            except RollbackSubmissionRefusedBeforeCallback:
                # Structurally guaranteed to mean the context refused BEFORE
                # the callback ran: the host was never asked to roll back.
                # That must never authorize a submission, and must never
                # fence this job's global slot forever either.
                return self._resolve_pre_rollback_block(job, request)
            except Exception as exc:  # noqa: BLE001 - any failure here is uncertain
                return self._uncertain(
                    job.job_id,
                    "rollback host submission did not return an outcome: "
                    f"{type(exc).__name__}",
                )
            result = self._validated(result, request.rollback_operation_id)

        return self._finish(job, request, result)

    def _finish(
        self,
        job: PackageUpdateJob,
        request: PackageUpdateRollbackRequest,
        result: HostRollbackResult,
        *,
        allow_pre_submission_seal: bool = True,
    ) -> RollbackStageResult:
        # D. The instant a result carries a known PVE task identity, persist
        # it durably -- BEFORE any polling/sleep loop, and in a short
        # transaction that ends immediately. From that point on a failed
        # poll, a lost SSH session, a timeout, or a backend crash can never
        # cost authority a task identity it already durably observed: the
        # host's own journal is not the only place that identity survives.
        early = self._persist_known_task(job.job_id, result)
        if early is not None:
            return early
        # Task polling and canonical recovery happen entirely outside the
        # authority critical section: every iteration is a bounded, read-only
        # host inspection, never a resubmission and never a held writer lock.
        result = self._poll_until_resolved(job, request, result)
        return self._apply_host_result(
            job,
            request,
            result,
            allow_pre_submission_seal=allow_pre_submission_seal,
        )

    def _persist_known_task(
        self, job_id: str, result: HostRollbackResult
    ) -> RollbackStageResult | None:
        """Durably record a known PVE task identity before polling begins.

        Returns ``None`` to let the caller proceed (there was nothing to
        persist, or it persisted cleanly -- including idempotently, if this
        exact UPID was already durable). Returns a terminal UNCERTAIN result
        only when persistence itself fails closed, which happens if and only
        if this result's task identity conflicts with one already recorded --
        never a reason to keep polling.
        """

        if result.task_upid is None:
            return None
        try:
            self._authority.record_package_update_rollback_task(
                job_id, result.task_upid
            )
        except Exception:  # noqa: BLE001 - a conflicting task is uncertainty
            return self._uncertain(
                job_id,
                "observed PVE rollback task conflicts with the durable one",
            )
        return None

    def _resolve_pre_rollback_block(
        self, job: PackageUpdateJob, request: PackageUpdateRollbackRequest
    ) -> RollbackStageResult:
        """Decide, and durably apply, a pre-rollback release.

        Reached when transient journal evidence says submission may still be
        preventable, or when a prior attempt already reports the durable seal.
        Neither ``absent`` nor ``intent`` may terminalize the job: a helper
        launched by a dead backend may still be waiting to take its host lease
        and submit afterward.

        Both are closed the same way, inside
        :meth:`InventoryAuthority.resolve_pre_rollback_block`: a FRESH host
        seal -- never an earlier observation -- written while that method's
        transaction still owns the authority-store writer lock. The seal and
        every submitter use the same per-VMID host lease. If seal wins, the
        delayed submitter must refuse; if submit wins, seal observes a
        post-submission phase and refuses to release the job.
        """

        def _seal() -> tuple[HostRollbackState | None, str, HostRollbackResult]:
            try:
                fresh = self._host_control.seal_rollback_never_submitted(request)
            except Exception as exc:  # noqa: BLE001 - an unreturned seal is unknown
                fresh = HostRollbackResult(
                    outcome=RollbackOperationOutcome.UNCERTAIN,
                    rollback_operation_id=request.rollback_operation_id,
                    reason=(
                        "rollback host seal did not return an outcome: "
                        f"{type(exc).__name__}"
                    ),
                )
            fresh = self._validated(fresh, request.rollback_operation_id)
            proved = fresh.rollback_state in _RELEASE_PROVED_STATES
            reason = (
                "host durably sealed this rollback operation before submission"
                if proved
                else "host did not durably seal this rollback operation before "
                "submission"
            )
            return fresh.rollback_state, reason, fresh

        try:
            blocked, fresh = self._authority.resolve_pre_rollback_block(
                job.job_id, _seal
            )
        except Exception:  # noqa: BLE001 - a contradicted proof stays fenced
            return self._uncertain(
                job.job_id,
                "a durable host seal proved this rollback was never submitted, "
                "but durable job state contradicts it",
            )

        if blocked:
            return RollbackStageResult(
                outcome=RollbackOperationOutcome.NOT_SUBMITTED,
                job=self._authority.package_update_job(job.job_id),
                reason=fresh.reason,
            )
        # The seal decision is attempted exactly once per orchestration path.
        return self._finish(job, request, fresh, allow_pre_submission_seal=False)

    def _read_inspection(
        self, request: PackageUpdateRollbackRequest
    ) -> HostRollbackResult:
        """One bounded, read-only look at the host's durable operation state."""

        try:
            inspected = self._host_control.inspect_rollback_state(request)
        except Exception as exc:  # noqa: BLE001 - a failed read is uncertain
            return HostRollbackResult(
                outcome=RollbackOperationOutcome.UNCERTAIN,
                rollback_operation_id=request.rollback_operation_id,
                reason=(
                    "rollback host inspection did not return an outcome: "
                    f"{type(exc).__name__}"
                ),
            )
        return self._validated(inspected, request.rollback_operation_id)

    @staticmethod
    def _validated(
        result: HostRollbackResult, rollback_operation_id: str
    ) -> HostRollbackResult:
        if (
            not isinstance(result, HostRollbackResult)
            or result.rollback_operation_id != rollback_operation_id
        ):
            return HostRollbackResult(
                outcome=RollbackOperationOutcome.UNCERTAIN,
                rollback_operation_id=rollback_operation_id,
                reason="rollback host operation answered a different operation",
            )
        return result

    def _poll_until_resolved(
        self,
        job: PackageUpdateJob,
        request: PackageUpdateRollbackRequest,
        result: HostRollbackResult,
    ) -> HostRollbackResult:
        """Bounded-poll one in-flight rollback purely through read-only inspection.

        Never opens a database transaction and never resubmits.

        THE PROBLEM THIS SOLVES. Submission hands the physical `pvesh` to a
        DETACHED host grandchild which keeps this operation's per-VMID lease
        for the whole of the physical rollback, and releases it only AFTER
        the durable completion capture is written. So the ordinary production
        sequence is:

        ```text
        submit           -> submitted, no UPID (capture does not exist yet)
        inspect          -> lease still held -> host_operation_in_progress
        inspect          -> lease still held -> host_operation_in_progress
        (pvesh ends, capture written, lease released)
        inspect          -> capture promoted -> task_known + exact UPID
        inspect          -> exact task terminal evidence -> done
        ```

        Neither middle read carries a journal phase -- the helper could not
        read the journal at all -- so a predicate looking only at
        ``rollback_state`` would end the cycle as ``rollback_uncertain`` and
        demand an operator RESUME purely to notice a capture that landed
        moments later.

        THE STATE MACHINE. This loop carries the last DURABLY PROVEN
        in-flight phase for this exact operation across transient unreadable
        windows: ``submitted``/``task_known`` sets the proof
        (``task_known`` never degrades back), a bare
        :attr:`~HostRollbackResult.host_operation_in_progress` KEEPS it and
        keeps polling but proves nothing itself, and anything else clears it
        so a transport failure, an unknown classification, or a malformed
        answer after ``submitted`` fails closed rather than being mistaken
        for the expected lease-busy window.

        None of this weakens rollback's attribution rule. Promotion still
        requires a COMPLETE, exact capture, and rollback NEVER infers success
        from canonical state: a rollback that never yields an exact UPID
        stays uncertain and fenced however long this loop looks.

        ``task_known`` whose task is not yet terminal stays pollable exactly
        as before. Once a read observes the task ITSELF has reached a
        terminal PVE state, further polling cannot make that same task more
        terminal, so it stops: the durable journal phase alone is not a fresh
        signal, since it stays ``task_known`` forever once a task identity is
        captured.
        """

        proven = self._proven_in_flight(result, self._durable_proof(job))
        if not self._pending(result, proven):
            return result
        deadline = self._monotonic() + self._task_poll_timeout_seconds
        while True:
            result = self._read_inspection(request)
            proven = self._proven_in_flight(result, proven)
            if not self._pending(result, proven):
                return result
            if self._monotonic() >= deadline:
                return result
            self._sleep(self._task_poll_interval_seconds)

    @staticmethod
    def _durable_proof(job: PackageUpdateJob) -> HostRollbackState | None:
        """Seed the in-flight proof from authority's own durable record.

        A recorded rollback task UPID is write-once durable evidence that
        THIS job's rollback reached ``task_known``, so a worker that
        restarted mid-flight may keep polling through the lease-busy window
        instead of needing an operator RESUME to re-learn what authority
        already knows. Nothing weaker seeds anything: the write-ahead
        ``rollback_may_have_started`` checkpoint is committed BEFORE
        submission and so proves no submission at all.
        """

        return (
            HostRollbackState.TASK_KNOWN
            if job.rollback_task_upid is not None
            else None
        )

    @staticmethod
    def _proven_in_flight(
        candidate: HostRollbackResult, previous: HostRollbackState | None
    ) -> HostRollbackState | None:
        """Carry the last durably proven in-flight phase across busy reads."""

        state = candidate.rollback_state
        if state is HostRollbackState.TASK_KNOWN:
            return state
        if state is HostRollbackState.SUBMITTED:
            # Never let a re-read downgrade proof the cycle already has.
            return previous if previous is HostRollbackState.TASK_KNOWN else state
        if candidate.host_operation_in_progress:
            # The host could not read its journal; it did not contradict it.
            return previous
        return None

    @staticmethod
    def _pending(
        candidate: HostRollbackResult, proven: HostRollbackState | None
    ) -> bool:
        if candidate.outcome is not RollbackOperationOutcome.UNCERTAIN:
            return False
        if candidate.host_operation_in_progress:
            # Transient read-unavailable. Pollable ONLY on top of durable
            # submission proof this cycle already holds; never on its own.
            return proven is not None
        if candidate.rollback_state is HostRollbackState.SUBMITTED:
            # The detached capture may still land and yield the exact UPID.
            # Looking again is a read; it never resubmits.
            return True
        if candidate.rollback_state is not HostRollbackState.TASK_KNOWN:
            return False
        if candidate.task is not None and candidate.task.terminal:
            return False
        return True

    def _apply_host_result(
        self,
        job: PackageUpdateJob,
        request: PackageUpdateRollbackRequest,
        result: HostRollbackResult,
        *,
        allow_pre_submission_seal: bool = True,
    ) -> RollbackStageResult:
        job_id = job.job_id
        if (
            not isinstance(result, HostRollbackResult)
            or result.rollback_operation_id != request.rollback_operation_id
        ):
            return self._uncertain(
                job_id, "rollback host operation answered a different operation"
            )

        # Idempotent safeguard, not the primary persistence point: `_finish`
        # already durably recorded a known task identity BEFORE polling ran,
        # via `_persist_known_task`. This repeats the same write-once record
        # for whatever the FINAL (post-poll) result carries, which is
        # harmless when it is the same UPID already durable, and still fails
        # closed on a genuine conflict.
        if result.task_upid is not None:
            try:
                self._authority.record_package_update_rollback_task(
                    job_id, result.task_upid
                )
            except Exception:  # noqa: BLE001 - a conflicting task is uncertainty
                return self._uncertain(
                    job_id,
                    "observed PVE rollback task conflicts with the durable one",
                )

        if result.outcome is RollbackOperationOutcome.NOT_SUBMITTED:
            if allow_pre_submission_seal:
                return self._resolve_pre_rollback_block(job, request)
            return self._uncertain(
                job_id,
                result.reason
                or "rollback operation was not durably sealed before submission",
            )

        if result.outcome is RollbackOperationOutcome.UNCERTAIN:
            return self._uncertain(
                job_id, result.reason or "rollback operation outcome is uncertain"
            )

        if result.outcome is RollbackOperationOutcome.FAILED:
            # A terminal failed PVE rollback task is recorded truthfully and
            # keeps the job ACTIVE and fenced. It is never retried: the guest
            # may be stopped, partially rolled back, or still config-locked.
            job_after = self._authority.record_package_update_rollback_terminal_failure(
                job_id, (result.reason or "PVE rollback task failed")[:500]
            )
            return RollbackStageResult(
                outcome=RollbackOperationOutcome.FAILED,
                job=job_after,
                reason=result.reason,
            )

        # E. A completed operation still has to prove itself. A terminal
        # non-error task is necessary but never sufficient: the canonical
        # post-condition is re-proved by authority below.
        if result.task is None or not result.task.succeeded:
            return self._uncertain(
                job_id,
                "rollback operation reported completion without a successful "
                "terminal PVE task",
            )
        if result.snapshots is None:
            return self._uncertain(
                job_id, "rollback operation completed without a canonical listing"
            )
        try:
            job_after = self._authority.complete_package_update_rollback(
                job_id, result.snapshots, task_succeeded=True
            )
        except AuthorityConflict as exc:
            return self._uncertain(
                job_id,
                f"canonical rollback completion failed closed: {exc}",
            )
        except Exception as exc:  # noqa: BLE001 - never complete on ambiguity
            return self._uncertain(
                job_id,
                "canonical rollback completion failed closed: "
                f"{type(exc).__name__}",
            )
        return RollbackStageResult(
            outcome=RollbackOperationOutcome.COMPLETED, job=job_after
        )

    def _uncertain(self, job_id: str, reason: str) -> RollbackStageResult:
        """Durably record uncertainty, tolerating concurrent terminalization.

        A concurrent, compliant invocation of this same orchestrator can
        terminalize (or otherwise advance) the job between whatever observed
        the need for this call and this durable write reaching its own
        transaction. That other invocation's transaction already proved and
        committed the job's real outcome, so it is authoritative: this call
        must never undo, rewrite, or reopen it. If the re-read shows the job
        unexpectedly still eligible, the conflict was a genuine invariant
        violation rather than this race, and stays fail-closed by re-raising.
        """

        try:
            job = self._authority.record_package_update_rollback_uncertain(
                job_id, reason[:500]
            )
        except AuthorityConflict:
            job = self._authority.package_update_job(job_id)
            still_eligible = (
                job.status is PackageUpdateJobStatus.ACTIVE
                and job.checkpoint
                is PackageUpdateCheckpoint.ROLLBACK_MAY_HAVE_STARTED
            )
            if still_eligible:
                raise
            return RollbackStageResult(
                outcome=RollbackOperationOutcome.UNCERTAIN,
                job=job,
                reason=(
                    "a concurrent invocation already resolved this job before "
                    "this uncertainty could be durably recorded; the job's own "
                    "current state is authoritative and was left untouched"
                ),
            )
        return RollbackStageResult(
            outcome=RollbackOperationOutcome.UNCERTAIN, job=job, reason=reason
        )
