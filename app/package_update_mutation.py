"""Dark crash-safe real package mutation for one package update job.

**Not production-reachable.** Nothing in `app/inventory_runtime.py`, the HTTP
API, the Home Assistant integration, the discovery scheduler, or the package
scan scheduler constructs or calls anything in this module, and
`tests/test_r0_architecture_regression.py` proves it stays that way. It
exists so the mutation half of the update lifecycle can be built and
adversarially tested before it is ever activated.

This module performs no package mutation itself. It coordinates authority
and one dark host boundary
(`deploy/hubinet-package-mutation-helper.py`) which owns the only real
workload package command in this product.

## What the orchestrator guarantees

```text
ACTIVE @ snapshot_confirmed
  -> host PREPARE          (read-only: metadata refresh, SIMULATION, dpkg
                            identity; returns an evidence digest and writes
                            NO durable host state)
  -> canonical material    (the SAME parser package scanning uses)
  -> ONE authority transaction:
        re-prove ACTIVE @ snapshot_confirmed
        re-prove current authority     (stale -> released, blocked)
        exact complete-set equality vs the job's IMMUTABLE frozen rows
        COMMIT checkpoint = mutation_may_have_started      (write-ahead)
                          + accepted_prepared_evidence_digest
  -> short submission critical section: re-prove current authority and the
     accepted digest and, while still holding the authority writer lock, ask
     the host to EXECUTE
  -> host journals `intent` bound to that accepted digest, then durably
     journals `submitted` BEFORE launching anything, then hands the real
     package command to a detached runner and returns
  -> read-only polling, entirely outside any transaction
  -> terminal host evidence
  -> independent dpkg completion proof
  -> mutation_completed
```

## Preparation is read-only in the durable sense too

The host PREPARE writes no journal record. It runs strictly BEFORE the
write-ahead arming transaction, so anything durable it created would be
mutation-operation state for an operation that may never be armed -- and,
being immutable once written, would convert every ordinary pre-arm transient
(a package scan still RUNNING, a lost PREPARE response, a backend that died
before arming) into an operation identity that could not be prepared again
until a backend restart interrupted the job. Preparation is therefore
repeatable by construction, and the host journal's first record is created by
the submit-capable EXECUTE path, from the digest this arming transaction
accepted. Nothing about at-most-once weakens: the host still writes and
fsyncs `submitted` under its per-VMID lease before any package command can
be launched, and an armed job with no host record is still resolved by
durably SEALING that absence, never by inferring anything from it.

## Two boundaries, and why both exist

`mutation_may_have_started` is a write-ahead checkpoint committed durably
BEFORE any real package command can be sent. Once a job reaches it, nothing
may ever infer that no workload package changed. It is deliberately not
"mark safe, mutate later": the exact equality proof and the checkpoint share
one transaction, because proving in one transaction and committing in
another is a check-then-commit race.

The submission critical section closes the second, narrower race the
checkpoint alone leaves open: another Hubinet writer (discovery
reconciliation, a package scan) can invalidate this job's resource
incarnation AFTER authority was proved and BEFORE the host is actually asked
to mutate. So the final proof and the submission it authorizes share one
transaction too. The host call it makes never waits for the package command
-- the host journals `submitted` and detaches -- so that writer lock is held
for one bounded round trip, never for an upgrade.

## Only the invocation that proved it may submit

A real package mutation is submitted ONLY by the same invocation that just
prepared the fresh execution-time evidence and armed the job with it. Every
later invocation -- a retry, a restart recovery, a concurrent racer -- can
observe, seal, or complete, and can never submit. That makes "no blind
resubmission after a crash, timeout, or lost response" structural rather
than a judgment call at each recovery branch, and it is enforced twice over:
here, and by the host, which binds the accepted digest into its journal's
first record and then refuses to execute for any caller presenting a
different one.

## Failure is never release

Once the write-ahead checkpoint exists, an `apt-get` failure, a lost
response, a timeout, a restart, a running operation, an unreadable
post-state, or any ambiguity leaves the job ACTIVE at
`mutation_may_have_started`: still owning the one global destructive slot,
still owning its confirmed pre-update snapshot, still holding rollback
authority, with truthful durable evidence appended. Packages may be partly
changed, and pretending otherwise would strand a half-upgraded guest with no
owner. The ONLY release is the host's durable `sealed_not_submitted` proof,
which says exactly that no mutation happened and none ever can.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
import time
from typing import Protocol

from app.inventory import (
    AuthorityConflict,
    AuthorityNotFound,
    HostMutationState,
    InventoryAuthority,
    MutationSubmissionRefusedBeforeCallback,
    PackageMutationArmOutcome,
    PackageMutationEvidenceNotAccepted,
    PackageScanFailure,
    PackageUpdateCheckpoint,
    PackageUpdateExecutionOutcome,
    PackageUpdateJob,
    PackageUpdateJobStatus,
    PackageUpdateMutationRequest,
    checkpoint_rank,
)
from app.inventory.authority import (
    PackageUpdateExecutionAuthorityTemporarilyUnavailable,
)
from app.inventory.mutation_completion import PackageMutationPostState
from app.package_scan import (
    HostScanFailure,
    PackageScanParseError,
    parse_apt_simulation,
    parse_installed_inventory_state,
    parse_native_architecture,
    parse_os_release,
)


#: Bounded polling of one submitted mutation. An operation still running when
#: this elapses stays UNCERTAIN and fenced -- never failed, and never a
#: licence to resubmit.
DEFAULT_MUTATION_POLL_TIMEOUT_SECONDS = 5400.0
DEFAULT_MUTATION_POLL_INTERVAL_SECONDS = 5.0

#: Host journal states from which a NEW mutation may still be submitted, and
#: which a durable host seal may therefore fence. `ABSENT` is the ordinary
#: one -- the backend arms BEFORE calling the host, so a backend that died
#: between arming and executing leaves exactly this. Everything else means a
#: package command may already have run.
_SUBMISSION_PERMITTED_STATES = (
    HostMutationState.ABSENT,
    HostMutationState.INTENT,
)


class PackageUpdateMutationError(RuntimeError):
    """A dark package mutation operation could not be carried out safely."""


@dataclass(frozen=True, slots=True)
class HostMutationEvidence:
    """The durable result one mutation operation reached on the host.

    Raw evidence, deliberately not a verdict: the exit code says what the
    command did, and the two independent dpkg readings are what the backend
    actually proves completion from.
    """

    exit_code: int
    timed_out: bool
    pre_native_architecture: str
    pre_installed_inventory: str
    post_native_architecture: str
    post_installed_inventory: str
    output_tail: str = ""


@dataclass(frozen=True, slots=True)
class HostMutationResult:
    """The bounded, typed answer one dark host mutation operation returns."""

    mutation_operation_id: str
    state: HostMutationState
    #: Whether the host observes a live runner holding this guest's lease.
    running: bool = False
    reason: str | None = None
    #: Read-only preparation evidence. Present only for a successful prepare.
    os_release: str | None = None
    native_architecture: str | None = None
    installed_inventory: str | None = None
    simulation_stdout: str | None = None
    prepared_evidence_digest: str | None = None
    #: Durable terminal evidence. Present only in a terminal state.
    evidence: HostMutationEvidence | None = None


class PackageUpdateMutationHostControl(Protocol):
    """The dark typed host boundary this orchestrator is allowed to use.

    Deliberately narrow: four typed operations, no generic dispatcher, no
    place to pass a command string, argv, option, package name, or version,
    and no rollback, snapshot, or delete of any kind.
    """

    def prepare_exact_package_mutation(
        self, request: PackageUpdateMutationRequest
    ) -> HostMutationResult:
        """Produce fresh execution-time evidence. Non-mutating, and durable-free.

        It changes no workload package AND writes no host journal record, so
        an attempt that never gets armed leaves the operation identity
        untouched and the next attempt simply prepares again.
        """

    def execute_exact_package_mutation(
        self,
        request: PackageUpdateMutationRequest,
        *,
        prepared_evidence_digest: str,
    ) -> HostMutationResult:
        """Cross this operation's submission boundary at most once.

        Submission-only: it returns once the host has durably journaled
        `submitted` and detached the runner, so it never waits for the
        package command and is safe to call inside the backend's own bounded
        writer critical section.
        """

    def seal_mutation_never_submitted(
        self, request: PackageUpdateMutationRequest
    ) -> HostMutationResult:
        """Durably forbid this exact operation from ever mutating packages."""

    def inspect_package_mutation_state(
        self, request: PackageUpdateMutationRequest
    ) -> HostMutationResult:
        """Read the durable operation state. Submits nothing, seals nothing."""


class MutationStageStatus(StrEnum):
    """The typed outcome of one package mutation stage attempt."""

    #: The exact approved mutation is durably complete and independently
    #: proven from dpkg's own post-state. The job is ACTIVE at
    #: `mutation_completed` -- NOT succeeded; the healthcheck has not run.
    COMPLETED = "completed"
    #: A mutation is running on the host right now. The job stays ACTIVE and
    #: fenced; query again later. Never resubmitted.
    RUNNING = "running"
    #: The host durably proved no package mutation was ever submitted for
    #: this operation identity, and none ever can be. The job was released
    #: as `blocked`: snapshot retained, no rollback authority fabricated.
    NOT_SUBMITTED = "not_submitted"
    #: The real package command reached a failed terminal result. Packages
    #: may be partly changed, so the job stays ACTIVE, fenced, and owning
    #: the global destructive slot for the later rollback stage.
    TERMINAL_FAILURE = "terminal_failure"
    #: The outcome could not be established. Identical ownership semantics to
    #: TERMINAL_FAILURE, and never a licence to resubmit.
    UNCERTAIN = "uncertain"
    #: Fresh execution-time material no longer matches the job's frozen
    #: approved rows. Nothing was mutated; the job is `blocked`.
    MISMATCHED = "mismatched"
    #: Current authority was proven stale before arming. Nothing was
    #: mutated; the job is `blocked` and the global slot released.
    AUTHORITY_STALE = "authority_stale"
    #: A newer package scan is still running, so authority cannot be decided.
    #: Nothing touched; retry after it completes.
    AUTHORITY_TEMPORARILY_UNAVAILABLE = "authority_temporarily_unavailable"
    #: The job is not at a checkpoint this stage acts on. Nothing touched.
    JOB_NOT_READY = "job_not_ready"
    #: A read-only host round trip failed before anything was armed. Nothing
    #: was mutated and nothing durable changed.
    HOST_FAILURE = "host_failure"


@dataclass(frozen=True, slots=True)
class MutationStageResult:
    status: MutationStageStatus
    job: PackageUpdateJob | None = None
    failure_class: PackageScanFailure | None = None
    reason: str | None = None


class PackageUpdateMutationOrchestrator:
    """Coordinate authority and one dark host boundary for one mutation.

    Instantiated only by hermetic tests in this stage. It performs no package
    mutation of its own, no snapshot operation, and no rollback.
    """

    def __init__(
        self,
        authority: InventoryAuthority,
        host_control: PackageUpdateMutationHostControl,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        poll_timeout_seconds: float = DEFAULT_MUTATION_POLL_TIMEOUT_SECONDS,
        poll_interval_seconds: float = DEFAULT_MUTATION_POLL_INTERVAL_SECONDS,
    ) -> None:
        self._authority = authority
        self._host_control = host_control
        self._monotonic = monotonic
        self._sleep = sleep
        self._poll_timeout_seconds = poll_timeout_seconds
        self._poll_interval_seconds = poll_interval_seconds

    # ------------------------------------------------------------------
    # Public surface
    # ------------------------------------------------------------------

    def execute_job_owned_mutation(self, job_id: str) -> MutationStageResult:
        """Drive one job from a confirmed snapshot to a proven mutation.

        Re-entrant by design, and asymmetric on purpose: an invocation that
        finds the job still at `snapshot_confirmed` proves fresh material,
        arms the write-ahead boundary, and may submit exactly once; an
        invocation that finds it already armed is a RECOVERY and may never
        submit (see :meth:`recover_job_owned_mutation`).
        """

        try:
            job = self._authority.package_update_job(job_id)
        except AuthorityNotFound:
            return MutationStageResult(
                status=MutationStageStatus.JOB_NOT_READY,
                reason="package update job does not exist",
            )
        if job.status is not PackageUpdateJobStatus.ACTIVE:
            return MutationStageResult(
                status=MutationStageStatus.JOB_NOT_READY,
                job=job,
                reason="package update job is terminal",
            )
        rank = checkpoint_rank(job.checkpoint)
        if rank > checkpoint_rank(PackageUpdateCheckpoint.MUTATION_COMPLETED):
            return MutationStageResult(
                status=MutationStageStatus.JOB_NOT_READY,
                job=job,
                reason="package update job has advanced past the mutation stage",
            )
        if job.checkpoint is PackageUpdateCheckpoint.MUTATION_COMPLETED:
            return MutationStageResult(status=MutationStageStatus.COMPLETED, job=job)
        if job.checkpoint is PackageUpdateCheckpoint.MUTATION_MAY_HAVE_STARTED:
            # Already past the uncertainty boundary: recovery only.
            return self.recover_job_owned_mutation(job_id)
        if job.checkpoint is not PackageUpdateCheckpoint.SNAPSHOT_CONFIRMED:
            return MutationStageResult(
                status=MutationStageStatus.JOB_NOT_READY,
                job=job,
                reason="package update job has no confirmed pre-update snapshot",
            )
        return self._prove_arm_and_submit(job)

    def recover_job_owned_mutation(self, job_id: str) -> MutationStageResult:
        """Resolve an already-armed job from durable evidence alone.

        Never prepares, never arms, and NEVER submits. Every branch either
        keeps the job ACTIVE and fenced, completes it from proven post-state,
        or -- only on the host's durable `sealed_not_submitted` proof --
        releases it as blocked.
        """

        try:
            job = self._authority.package_update_job(job_id)
        except AuthorityNotFound:
            return MutationStageResult(
                status=MutationStageStatus.JOB_NOT_READY,
                reason="package update job does not exist",
            )
        if job.status is not PackageUpdateJobStatus.ACTIVE:
            return MutationStageResult(
                status=MutationStageStatus.JOB_NOT_READY,
                job=job,
                reason="package update job is terminal",
            )
        if job.checkpoint is PackageUpdateCheckpoint.MUTATION_COMPLETED:
            return MutationStageResult(status=MutationStageStatus.COMPLETED, job=job)
        if job.checkpoint is not PackageUpdateCheckpoint.MUTATION_MAY_HAVE_STARTED:
            return MutationStageResult(
                status=MutationStageStatus.JOB_NOT_READY,
                job=job,
                reason="package update job is not inside a package mutation",
            )
        request = self._authority.package_update_mutation_request(job.job_id)
        # A pure read of evidence that may already exist. It never requires
        # current authority: recovering evidence about an operation that may
        # already have mutated packages must never be discarded merely
        # because this backend's inventory context moved on.
        result = self._inspect(request)
        return self._resolve(job, request, result, allow_seal=True)

    # ------------------------------------------------------------------
    # The submitting path
    # ------------------------------------------------------------------

    def _prove_arm_and_submit(self, job: PackageUpdateJob) -> MutationStageResult:
        request = self._authority.package_update_mutation_request(job.job_id)

        # A. Fresh execution-time proof material, read-only, entirely outside
        #    any authority transaction. This is the exact plan gate re-run
        #    immediately before mutating -- never a trusted earlier pass.
        try:
            prepared = self._host_control.prepare_exact_package_mutation(request)
        except HostScanFailure as exc:
            return MutationStageResult(
                status=MutationStageStatus.HOST_FAILURE,
                job=job,
                failure_class=exc.failure_class,
                reason=exc.message,
            )
        except TimeoutError:
            return MutationStageResult(
                status=MutationStageStatus.HOST_FAILURE,
                job=job,
                failure_class=PackageScanFailure.TIMEOUT,
                reason="package mutation preparation timed out",
            )
        except Exception:  # noqa: BLE001 - classify without leaking detail
            return MutationStageResult(
                status=MutationStageStatus.HOST_FAILURE,
                job=job,
                failure_class=PackageScanFailure.EXECUTION_FAILED,
                reason="package mutation preparation failed",
            )
        prepared = self._validated(prepared, request.mutation_operation_id)
        if prepared.state is not HostMutationState.ABSENT:
            # Preparation is read-only and creates no durable host state, so
            # `absent` is the only state a preparable operation can be in.
            # Anything else means the host already has durable state for
            # this identity while the backend has not armed one -- which
            # cannot happen on the ordinary path, because the host's first
            # record is written by an EXECUTE that only an armed job can
            # send. Nothing is mutated, nothing is armed, and the evidence
            # is surfaced rather than reasoned around.
            return MutationStageResult(
                status=MutationStageStatus.HOST_FAILURE,
                job=job,
                failure_class=PackageScanFailure.EXECUTION_FAILED,
                reason=(
                    "host package mutation evidence is past preparation while "
                    f"the job is not armed ({prepared.state.value})"
                ),
            )
        if (
            prepared.prepared_evidence_digest is None
            or prepared.simulation_stdout is None
            or prepared.native_architecture is None
            or prepared.installed_inventory is None
            or prepared.os_release is None
        ):
            return MutationStageResult(
                status=MutationStageStatus.HOST_FAILURE,
                job=job,
                failure_class=PackageScanFailure.EXECUTION_FAILED,
                reason="host returned malformed package mutation preparation",
            )

        # B. Canonical material, parsed with the SAME parser package scanning
        #    uses -- one implementation, never a second one.
        try:
            parse_os_release(prepared.os_release)
            fresh_packages = parse_apt_simulation(
                prepared.simulation_stdout,
                native_architecture=prepared.native_architecture,
                installed_inventory=prepared.installed_inventory,
            )
        except PackageScanParseError as exc:
            return MutationStageResult(
                status=MutationStageStatus.HOST_FAILURE,
                job=job,
                failure_class=PackageScanFailure.MALFORMED_PLAN,
                reason=str(exc),
            )

        # C. The write-ahead uncertainty boundary. Exact equality, the
        #    accepted evidence digest, and the checkpoint commit in ONE
        #    transaction.
        digest = prepared.prepared_evidence_digest
        try:
            outcome, armed, job = self._authority.arm_package_update_mutation(
                job.job_id, fresh_packages, prepared_evidence_digest=digest
            )
        except PackageUpdateExecutionAuthorityTemporarilyUnavailable as exc:
            return MutationStageResult(
                status=MutationStageStatus.AUTHORITY_TEMPORARILY_UNAVAILABLE,
                job=job,
                reason=str(exc),
            )
        except AuthorityConflict as exc:
            # The job moved off this checkpoint, or went terminal, while the
            # read-only host round trip was in flight. Never overwritten,
            # never reopened, and nothing was mutated.
            return MutationStageResult(
                status=MutationStageStatus.JOB_NOT_READY, job=job, reason=str(exc)
            )
        if outcome is PackageUpdateExecutionOutcome.MISMATCHED:
            return MutationStageResult(status=MutationStageStatus.MISMATCHED, job=job)
        if outcome is PackageUpdateExecutionOutcome.AUTHORITY_STALE:
            return MutationStageResult(
                status=MutationStageStatus.AUTHORITY_STALE, job=job
            )
        if armed is not PackageMutationArmOutcome.ARMED_NOW:
            # Some other invocation committed the accepted evidence for this
            # job. Deriving the same deterministic operation identity is not
            # permission to submit -- only carrying the digest authority
            # actually accepted is, and this invocation does not. It becomes
            # recovery-only, which can observe, complete, or seal, but never
            # submit.
            return self.recover_job_owned_mutation(job.job_id)

        # D. The submission critical section. This is the ONLY place in the
        #    product that may cause a real package command to run.
        try:
            submitted = self._authority.execute_package_mutation_submission_if_current(
                job.job_id,
                lambda: self._host_control.execute_exact_package_mutation(
                    request, prepared_evidence_digest=digest
                ),
                prepared_evidence_digest=digest,
            )
        except PackageMutationEvidenceNotAccepted as exc:
            # Structurally guaranteed zero callbacks, but NEVER routed to the
            # seal: the invocation whose evidence authority did accept may be
            # mutating right now. Stay armed, stay fenced, resolve from
            # durable evidence alone.
            return self._uncertain(job, str(exc))
        except MutationSubmissionRefusedBeforeCallback:
            # Structurally guaranteed: current authority proved false BEFORE
            # the callback ran, so the host was never asked to mutate. That
            # alone never authorizes a mutation, and must never fence the
            # global slot forever either -- route it to the durable seal.
            return self._resolve_pre_mutation_block(job, request)
        except Exception as exc:  # noqa: BLE001 - an unreturned submission is unknown
            return self._uncertain(
                job,
                "package mutation submission did not return an outcome: "
                f"{type(exc).__name__}",
            )
        submitted = self._validated(submitted, request.mutation_operation_id)
        if submitted.state is HostMutationState.SUBMITTED:
            job = self._record_submitted(job)
        return self._resolve(job, request, submitted, allow_seal=True)

    # ------------------------------------------------------------------
    # Evidence resolution
    # ------------------------------------------------------------------

    def _resolve(
        self,
        job: PackageUpdateJob,
        request: PackageUpdateMutationRequest,
        result: HostMutationResult,
        *,
        allow_seal: bool,
    ) -> MutationStageResult:
        if result.state in _SUBMISSION_PERMITTED_STATES:
            # Transient pre-submission routing evidence ONLY. A helper
            # launched by a backend that then died may not have taken its
            # host lease yet, so this can never itself release the job.
            if allow_seal:
                return self._resolve_pre_mutation_block(job, request)
            return self._uncertain(
                job,
                result.reason
                or "package mutation was not durably sealed before submission",
            )
        if result.state is HostMutationState.SEALED_NOT_SUBMITTED:
            # A prior attempt may have durably sealed the host and then died
            # before committing the backend block. Re-enter the idempotent
            # seal under the writer lock to finish that transition.
            if allow_seal:
                return self._resolve_pre_mutation_block(job, request)
            return self._uncertain(
                job, result.reason or "package mutation seal was not applied"
            )

        if result.state is HostMutationState.SUBMITTED:
            result = self._poll_until_terminal(request, result)

        if result.state is HostMutationState.SUBMITTED:
            if result.running:
                return MutationStageResult(
                    status=MutationStageStatus.RUNNING,
                    job=job,
                    reason=result.reason or "package mutation is still running",
                )
            return self._uncertain(
                job,
                result.reason
                or (
                    "package mutation was submitted but reached no terminal "
                    "result; its outcome is unknown"
                ),
            )

        if result.state is HostMutationState.TERMINAL_FAILURE:
            return self._terminal_failure(job, result)

        if result.state is HostMutationState.TERMINAL_SUCCESS:
            return self._complete(job, result)

        return self._uncertain(
            job, result.reason or "package mutation state is not recognized"
        )

    def _complete(
        self, job: PackageUpdateJob, result: HostMutationResult
    ) -> MutationStageResult:
        """Prove the mutation completed, independently of the exit code."""

        evidence = result.evidence
        if evidence is None:
            return self._uncertain(
                job, "host reported a terminal mutation without its evidence"
            )
        try:
            post_state = self._parsed_post_state(evidence)
        except PackageScanParseError as exc:
            return self._uncertain(
                job, f"package mutation post-state evidence is malformed: {exc}"
            )
        try:
            completed = self._authority.complete_package_update_mutation(
                job.job_id, post_state
            )
        except AuthorityConflict as exc:
            # The completion proof did not hold, or the job moved. Either
            # way the job keeps its snapshot, its global slot, and its
            # rollback authority, and nothing is retried.
            return self._uncertain(job, str(exc))
        return MutationStageResult(
            status=MutationStageStatus.COMPLETED, job=completed
        )

    @staticmethod
    def _parsed_post_state(
        evidence: HostMutationEvidence,
    ) -> PackageMutationPostState:
        """Parse both independent dpkg readings with the canonical parser.

        The pre-mutation reading must itself be clean: a guest that was
        already mid-transaction before the mutation cannot support any exact
        statement about what this mutation did.
        """

        parse_native_architecture(evidence.pre_native_architecture)
        parse_native_architecture(evidence.post_native_architecture)
        pre = parse_installed_inventory_state(evidence.pre_installed_inventory)
        post = parse_installed_inventory_state(evidence.post_installed_inventory)
        if pre.unfinished:
            raise PackageScanParseError(
                "guest already had unfinished dpkg package state before the "
                "mutation"
            )
        return PackageMutationPostState(
            pre_installed=pre.installed,
            post_installed=post.installed,
            post_unfinished=post.unfinished,
        )

    def _terminal_failure(
        self, job: PackageUpdateJob, result: HostMutationResult
    ) -> MutationStageResult:
        evidence = result.evidence
        detail = "package mutation command reached a failed terminal result"
        if evidence is not None:
            detail = (
                "package mutation command reached a failed terminal result "
                f"(exit_code={evidence.exit_code}, timed_out={evidence.timed_out})"
            )
        try:
            job = self._authority.record_package_update_mutation_terminal_failure(
                job.job_id, detail
            )
        except AuthorityConflict:
            job = self._authority.package_update_job(job.job_id)
        return MutationStageResult(
            status=MutationStageStatus.TERMINAL_FAILURE, job=job, reason=detail
        )

    def _resolve_pre_mutation_block(
        self, job: PackageUpdateJob, request: PackageUpdateMutationRequest
    ) -> MutationStageResult:
        """Decide, and durably apply, a pre-mutation release.

        The seal is taken FRESH inside the authority transaction that would
        apply it -- never an earlier observation -- so it is serialized
        against every submission critical section by the authority writer
        lock, and against every delayed helper by the host's own per-VMID
        lease. Anything other than a durable `sealed_not_submitted` is
        recovered through the ordinary evidence pipeline exactly as if this
        call had never happened: never released, and never resubmitted.
        """

        def _seal() -> tuple[HostMutationState | None, str, HostMutationResult]:
            try:
                fresh = self._host_control.seal_mutation_never_submitted(request)
            except Exception as exc:  # noqa: BLE001 - an unreturned seal is unknown
                fresh = HostMutationResult(
                    mutation_operation_id=request.mutation_operation_id,
                    state=HostMutationState.SUBMITTED,
                    reason=(
                        "package mutation seal did not return an outcome: "
                        f"{type(exc).__name__}"
                    ),
                )
            fresh = self._validated(fresh, request.mutation_operation_id)
            proved = fresh.state is HostMutationState.SEALED_NOT_SUBMITTED
            reason = (
                "host durably proved this package mutation was never submitted "
                "and can never be submitted; no workload package changed"
                if proved
                else "host did not durably seal this package mutation"
            )
            return fresh.state, reason, fresh

        try:
            blocked, fresh = self._authority.resolve_pre_mutation_block(
                job.job_id, _seal
            )
        except Exception:  # noqa: BLE001 - a contradicted proof stays fenced
            return self._uncertain(
                job,
                "a durable host seal proved this mutation was never submitted, "
                "but durable job state contradicts it",
            )
        if blocked:
            return MutationStageResult(
                status=MutationStageStatus.NOT_SUBMITTED,
                job=self._authority.package_update_job(job.job_id),
                reason=fresh.reason,
            )
        # The seal decision is attempted exactly once per orchestration path.
        # Ordinary evidence handling may fence or recover the job, but must
        # never recurse into another seal.
        return self._resolve(job, request, fresh, allow_seal=False)

    # ------------------------------------------------------------------
    # Bounded host reads
    # ------------------------------------------------------------------

    def _inspect(
        self, request: PackageUpdateMutationRequest
    ) -> HostMutationResult:
        """One bounded, read-only look at the host's durable operation state."""

        try:
            inspected = self._host_control.inspect_package_mutation_state(request)
        except Exception as exc:  # noqa: BLE001 - a failed read is uncertain
            return HostMutationResult(
                mutation_operation_id=request.mutation_operation_id,
                # An unreadable host is NEVER routed into the seal path: it
                # proves nothing about whether a mutation was submitted.
                state=HostMutationState.SUBMITTED,
                reason=(
                    "package mutation host inspection did not return an "
                    f"outcome: {type(exc).__name__}"
                ),
            )
        return self._validated(inspected, request.mutation_operation_id)

    def _poll_until_terminal(
        self, request: PackageUpdateMutationRequest, result: HostMutationResult
    ) -> HostMutationResult:
        """Bounded-poll a submitted mutation, purely through read-only reads.

        Never opens a database transaction and never resubmits anything. A
        submitted operation whose runner is no longer alive cannot become
        more terminal by waiting, so that case returns immediately as the
        uncertainty it is rather than burning the whole budget.
        """

        if result.state is not HostMutationState.SUBMITTED or not result.running:
            return result
        deadline = self._monotonic() + self._poll_timeout_seconds
        while True:
            if self._monotonic() >= deadline:
                return result
            self._sleep(self._poll_interval_seconds)
            result = self._inspect(request)
            if result.state is not HostMutationState.SUBMITTED or not result.running:
                return result

    @staticmethod
    def _validated(
        result: HostMutationResult, mutation_operation_id: str
    ) -> HostMutationResult:
        if (
            not isinstance(result, HostMutationResult)
            or result.mutation_operation_id != mutation_operation_id
            or not isinstance(result.state, HostMutationState)
        ):
            return HostMutationResult(
                mutation_operation_id=mutation_operation_id,
                # A host answering about a different operation proves nothing
                # about this one, so it is uncertain, never releasable.
                state=HostMutationState.SUBMITTED,
                reason="host answered about a different package mutation operation",
            )
        return result

    # ------------------------------------------------------------------
    # Durable, non-terminal evidence
    # ------------------------------------------------------------------

    def _record_submitted(self, job: PackageUpdateJob) -> PackageUpdateJob:
        try:
            return self._authority.record_package_update_mutation_submitted(
                job.job_id,
                "host durably journaled this package mutation before launching it",
            )
        except AuthorityConflict:
            return self._authority.package_update_job(job.job_id)

    def _uncertain(self, job: PackageUpdateJob, reason: str) -> MutationStageResult:
        """Durably record uncertainty, tolerating concurrent resolution.

        A concurrent, compliant invocation can complete or release the job
        between whatever observed the need for this call and this durable
        write. That invocation's own transaction already proved and committed
        the job's real outcome, so it is authoritative: this call must never
        undo, rewrite, or reopen it. Re-read the durable job and return a
        typed, fenced result describing that instead.
        """

        try:
            updated = self._authority.record_package_update_mutation_uncertain(
                job.job_id, reason[:500]
            )
        except AuthorityConflict:
            updated = self._authority.package_update_job(job.job_id)
            still_eligible = (
                updated.status is PackageUpdateJobStatus.ACTIVE
                and updated.checkpoint
                is PackageUpdateCheckpoint.MUTATION_MAY_HAVE_STARTED
            )
            if still_eligible:
                raise
            return MutationStageResult(
                status=MutationStageStatus.UNCERTAIN,
                job=updated,
                reason=(
                    "a concurrent invocation already resolved this job before "
                    "this uncertainty could be durably recorded; the job's own "
                    "current state is authoritative and was left untouched"
                ),
            )
        return MutationStageResult(
            status=MutationStageStatus.UNCERTAIN, job=updated, reason=reason
        )
