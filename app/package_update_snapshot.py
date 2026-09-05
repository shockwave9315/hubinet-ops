"""Dark job-owned pre-update snapshot safety for package update jobs.

**Not production-reachable.** Nothing in `app/inventory_runtime.py`, the HTTP
API, the Home Assistant integration, the discovery scheduler, or the package
scan scheduler constructs or calls anything in this module. It exists so the
snapshot half of the update lifecycle can be built and adversarially tested
before it is ever activated, and `tests/test_r0_architecture_regression.py`
proves it stays unreachable.

It contains no APT/package mutation of any kind, and no snapshot deletion.

## What the orchestrator guarantees

```text
revalidate authority
  -> preflight_passed
  -> derive deterministic job-owned snapshot identity
  -> DURABLY COMMIT snapshot_may_have_started   (write-ahead, before any call)
  -> read the durable host operation state (never requires current authority)
  -> if, and only if, that state proves no submission has crossed the door:
       hold current authority through a SHORT critical section and submit
  -> read-only task/canonical polling (current authority not required)
  -> terminal PVE task evidence
  -> fresh canonical PVE snapshot listing
  -> strict same-job ownership match
  -> snapshot_confirmed
```

The write-ahead checkpoint is committed *before* the host-control call so a
crash anywhere after it leaves a durable "a snapshot operation may already
have been submitted" fact. That state is never treated as "definitely no
mutation happened", never replayed, and never silently released.

## The submission critical section

Between the write-ahead commit and an actual new PVE submission there is a
second, narrower race: another Hubinet writer (discovery reconciliation, a
package scan) can invalidate this job's authority *after* it was proved and
*before* the host is asked to submit. Re-proving authority in a separate
transaction from the submission request is a check-then-commit race exactly
like the one the write-ahead checkpoint itself was built to close.

So a NEW submission is only ever attempted from inside
:meth:`InventoryAuthority.execute_snapshot_submission_if_current`, which
re-proves the job's whole current-authority context and calls the host's
submission-only operation *while it still holds the database's writer lock*.
Nothing else may write to this authority store while that section runs, so
nothing can invalidate the job between the final proof and the submission
request it authorizes. That transaction is kept as short as the submission
request itself: the host operation it calls
(`ensure_pre_update_snapshot_submitted`) journals and detaches a fixed local
runner before returning, so the writer lock is held only for one bounded round
trip, never for the physical snapshot operation.

Recovering evidence about an operation that may already have been submitted
is a different thing entirely, and never requires current authority: reading
the durable host operation state (`inspect_job_snapshot_state`) and polling a
known task to completion both happen strictly *outside* this critical
section, after it has already committed and released its writer lock. A
stale incarnation must never cause that evidence to be discarded -- see
`AGENTS.md` and `PRODUCT.md`.

This is deliberately not a claim that PVE and this backend's authority are
proved atomic, and it is deliberately not a claim that a snapshot is proved
to belong to "the same LXC" PVE showed several minutes ago -- see
`ARCHITECTURE.md`. The claim is narrower and exact: Hubinet's own current
authority is held stable through the submission boundary, and the host
independently re-validates the live PVE target immediately before it ever
submits.

Transient `absent`/`intent` journal observations can route a job into the
release path, but never release it: an already-launched helper may not have
reached its host lease yet. The release path instead durably writes
`sealed_not_submitted` through a third typed host operation, under the same
per-VMID lease as submission, while the backend still holds its SQLite writer
transaction. Only that seal releases the job. A delayed helper must obey it;
every failed or unsupported seal stays uncertain and fenced.

## Verified PVE task semantics

Established from current Proxmox VE sources, not from Hubinet 0.4 behaviour:

- The endpoint uses `fork_worker('vzsnapshot', ...)`, but local `pvesh` CLI
  runs the worker synchronously and prints the final UPID only after worker
  output. The host helper therefore detaches it and recovers the exact UPID
  from a crash-safe bounded capture. A returned submission response is never
  evidence that a snapshot exists.
- `GET /nodes/{node}/tasks/{upid}/status` reports `status` in
  `running`/`stopped` plus an optional `exitstatus`. `stopped` alone is not
  success; the exit status decides, using PVE's own rule
  (`PVE::UPID::status_is_error`): `OK` and `WARNINGS: <n>` are non-errors,
  everything else is an error, and an absent status is unknown.
- `GET /nodes/{node}/lxc/{vmid}/snapshot` also returns PVE's synthetic
  `current` pseudo-entry, and carries `snapstate` for snapshots whose
  operation has not finished, even though its declared schema omits it.

Strict fresh canonical evidence is therefore mandatory in every case, and a
terminal successful task is never sufficient on its own. It is not
universally necessary either: when a task was observed it must have reached a
terminal non-error state, but the submitted-without-recorded-UPID recovery
path establishes completion from the host's durable operation-journal state
plus that same canonical evidence, without fabricating a task identity.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
import time
from typing import Any, Protocol

from app.inventory import (
    AuthorityConflict,
    HostSubmissionState,
    InventoryAuthority,
    ObservedSnapshot,
    PackageUpdateCheckpoint,
    PackageUpdateJob,
    PackageUpdateJobStatus,
    PackageUpdateRollbackTarget,
    PackageUpdateSnapshotIdentity,
    SnapshotIdentityError,
    SnapshotOwnership,
    SnapshotSubmissionRefusedBeforeCallback,
    checkpoint_rank,
    parse_snapshot_description,
)


#: PVE's synthetic listing row for "the container as it is right now". It is
#: never a real snapshot and can never be owned, confirmed, or rolled back to.
PVE_CURRENT_PSEUDO_ENTRY = "current"

MAX_CANONICAL_SNAPSHOTS = 512
MAX_SNAPSHOT_DESCRIPTION_BYTES = 8192


class SnapshotOperationOutcome(StrEnum):
    """How a dark host snapshot operation ended, from the caller's side."""

    #: Strict canonical evidence of this job's snapshot was obtained.
    COMPLETED = "completed"
    #: The PVE task terminated in a failure state.
    FAILED = "failed"
    #: The outcome could not be established. Never a licence to resubmit.
    UNCERTAIN = "uncertain"
    #: The host PROVED, from its durable operation journal, that no snapshot
    #: mutation was ever submitted for this operation identity. This is the
    #: only outcome that may release a job which is already past its
    #: write-ahead uncertainty checkpoint, and it is never inferred from a
    #: canonical absence, an error name, a timeout, or a transport failure.
    NOT_SUBMITTED = "not_submitted"


class SnapshotTaskState(StrEnum):
    """Classification of one PVE task status observation."""

    RUNNING = "running"
    OK = "ok"
    WARNING = "warning"
    ERROR = "error"
    UNKNOWN = "unknown"


class PackageUpdateSnapshotError(RuntimeError):
    """A dark snapshot operation could not be carried out safely."""


class SnapshotEvidenceError(PackageUpdateSnapshotError):
    """Host-supplied snapshot evidence was malformed or out of contract."""


@dataclass(frozen=True, slots=True)
class SnapshotTaskStatus:
    """One bounded observation of the exact PVE task attributed by UPID."""

    upid: str
    terminal: bool
    state: SnapshotTaskState

    @property
    def succeeded(self) -> bool:
        """Whether PVE's own rule treats this terminal task as a non-error."""

        return self.terminal and self.state in (
            SnapshotTaskState.OK,
            SnapshotTaskState.WARNING,
        )


@dataclass(frozen=True, slots=True)
class HostSnapshotResult:
    """The bounded, typed answer one dark host snapshot operation returns."""

    outcome: SnapshotOperationOutcome
    snapshot_operation_id: str
    #: Present as soon as the host durably knew the PVE task identity.
    task_upid: str | None = None
    task: SnapshotTaskStatus | None = None
    #: A fresh canonical listing. ``None`` means the host could not obtain one.
    snapshots: tuple[ObservedSnapshot, ...] | None = None
    #: Bounded classification/reason text. Never raw PVE logs or command text.
    reason: str | None = None
    #: The host's own durable journal phase for this operation, read
    #: directly off it. ``None`` only for an older host that predates this
    #: field, which the caller must treat exactly like ``SUBMITTED`` --
    #: never a licence to submit, and never something to poll.
    submission_state: HostSubmissionState | None = None


class PackageUpdateSnapshotHostControl(Protocol):
    """The dark typed host boundary this orchestrator is allowed to use.

    Deliberately narrow: there is no delete operation, no rollback submission,
    no generic action dispatcher, and no place to pass a command string.
    """

    def ensure_pre_update_snapshot_submitted(
        self,
        *,
        snapshot_operation_id: str,
        snapshot_name: str,
        vmid: int,
        expected_node: str,
        ownership: SnapshotOwnership,
    ) -> HostSnapshotResult:
        """Submit, or reattach to, this exact job's snapshot operation.

        Submission-only: this call never polls a PVE task to completion. It
        returns promptly once the operation has crossed (or is already past)
        its submission boundary, so it is safe to call while this backend
        holds its own authority write lock across it.
        """

    def inspect_job_snapshot_state(
        self,
        *,
        snapshot_operation_id: str,
        snapshot_name: str,
        vmid: int,
        expected_node: str,
        ownership: SnapshotOwnership,
    ) -> HostSnapshotResult:
        """Read current canonical state without submitting anything."""

    def seal_operation_never_submitted(
        self,
        *,
        snapshot_operation_id: str,
        snapshot_name: str,
        vmid: int,
        expected_node: str,
        ownership: SnapshotOwnership,
    ) -> HostSnapshotResult:
        """Durably forbid this exact operation from ever being submitted."""


# ---------------------------------------------------------------------------
# Canonical PVE snapshot listing
# ---------------------------------------------------------------------------


def classify_task_status(payload: Any) -> SnapshotTaskStatus:
    """Classify one ``/nodes/{node}/tasks/{upid}/status`` response.

    Applies PVE's own success rule rather than inventing one: a task is
    terminal when ``status`` is ``stopped``, and a terminal task is a
    non-error only when its ``exitstatus`` is ``OK`` or ``WARNINGS: <n>``. A
    terminal task with no exit status at all is ``UNKNOWN``, never success.
    """

    if not isinstance(payload, Mapping):
        raise SnapshotEvidenceError("PVE task status was malformed")
    upid = payload.get("upid")
    status = payload.get("status")
    if not isinstance(upid, str) or not upid or status not in ("running", "stopped"):
        raise SnapshotEvidenceError("PVE task status was malformed")
    if status == "running":
        return SnapshotTaskStatus(
            upid=upid, terminal=False, state=SnapshotTaskState.RUNNING
        )
    exit_status = payload.get("exitstatus")
    if exit_status is None or not isinstance(exit_status, str) or not exit_status:
        state = SnapshotTaskState.UNKNOWN
    elif exit_status == "OK":
        state = SnapshotTaskState.OK
    elif _is_warning_exit_status(exit_status):
        state = SnapshotTaskState.WARNING
    elif exit_status == "unexpected status":
        state = SnapshotTaskState.UNKNOWN
    else:
        state = SnapshotTaskState.ERROR
    return SnapshotTaskStatus(upid=upid, terminal=True, state=state)


def _is_warning_exit_status(value: str) -> bool:
    prefix = "WARNINGS: "
    return (
        value.startswith(prefix)
        and value[len(prefix):].isdigit()
        and value[len(prefix):].isascii()
    )


def parse_canonical_snapshot_listing(payload: Any) -> tuple[ObservedSnapshot, ...]:
    """Parse one ``GET /nodes/{node}/lxc/{vmid}/snapshot`` response strictly.

    Preserves exactly the evidence ownership decisions need: the name, the
    description, whether the row is PVE's ``current`` pseudo-entry, whether
    PVE still reports a ``snapstate`` (an unfinished snapshot operation), and
    the strictly parsed Hubinet ownership metadata. A Hubinet-looking snapshot
    whose metadata does not parse is preserved as ``ownership_malformed``
    rather than dropped, so it can never silently disappear from an ownership
    decision.
    """

    if isinstance(payload, (str, bytes)) or not isinstance(payload, Sequence):
        raise SnapshotEvidenceError("PVE snapshot listing was malformed")
    rows = tuple(payload)
    if len(rows) > MAX_CANONICAL_SNAPSHOTS:
        raise SnapshotEvidenceError("PVE snapshot listing exceeded its bound")
    observed: list[ObservedSnapshot] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            raise SnapshotEvidenceError("PVE snapshot listing entry was malformed")
        name = row.get("name")
        if not isinstance(name, str) or not name or len(name) > 128:
            raise SnapshotEvidenceError("PVE snapshot entry name was malformed")
        if name in seen:
            raise SnapshotEvidenceError("PVE snapshot listing repeated a name")
        seen.add(name)
        description = row.get("description", "")
        if not isinstance(description, str):
            raise SnapshotEvidenceError("PVE snapshot description was malformed")
        if len(description.encode("utf-8", "surrogatepass")) > (
            MAX_SNAPSHOT_DESCRIPTION_BYTES
        ):
            raise SnapshotEvidenceError("PVE snapshot description exceeded its bound")
        snaptime = row.get("snaptime")
        if snaptime is not None and type(snaptime) is not int:
            raise SnapshotEvidenceError("PVE snapshot snaptime was malformed")
        parent = row.get("parent")
        if parent is not None and not isinstance(parent, str):
            raise SnapshotEvidenceError("PVE snapshot parent was malformed")
        snapstate = row.get("snapstate")
        if snapstate is not None and not isinstance(snapstate, str):
            raise SnapshotEvidenceError("PVE snapshot snapstate was malformed")
        is_current = name == PVE_CURRENT_PSEUDO_ENTRY
        ownership: SnapshotOwnership | None = None
        malformed = False
        try:
            ownership = parse_snapshot_description(description)
        except SnapshotIdentityError:
            # Looks like a Hubinet snapshot but its metadata does not parse.
            ownership = None
            malformed = True
        # No further check is needed here: `parse_snapshot_description`
        # returns ``None`` (rather than raising) only when the description
        # makes no Hubinet ownership claim at all -- i.e. exactly when
        # `looks_like_hubinet_snapshot` is already false -- so `ownership is
        # None` without `malformed` can never itself be an ambiguous claim.
        observed.append(
            ObservedSnapshot(
                name=name,
                description=description,
                is_current_pseudo_entry=is_current,
                # PVE reports snaptime 0 when it does not know one, and its
                # `current` pseudo-entry carries none at all.
                incomplete=bool(snapstate),
                snaptime=snaptime if snaptime else None,
                parent=parent,
                # The pseudo-entry's canned "You are here!" text is PVE's, and
                # an ownership claim on it would be nonsense.
                ownership=None if is_current else ownership,
                ownership_malformed=False if is_current else malformed,
            )
        )
    return tuple(observed)


# ---------------------------------------------------------------------------
# Dark orchestration
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SnapshotStageResult:
    """The durable result of one dark snapshot stage attempt."""

    outcome: SnapshotOperationOutcome
    job: PackageUpdateJob
    reason: str | None = None


#: Bounded polling of one in-flight snapshot operation: a journaled PVE task,
#: or a `submitted` operation whose detached host capture has not yet yielded
#: the exact UPID. Either still in flight when this elapses stays UNCERTAIN --
#: never failed, and never a licence to resubmit.
DEFAULT_TASK_POLL_TIMEOUT_SECONDS = 900.0
DEFAULT_TASK_POLL_INTERVAL_SECONDS = 2.0

#: Journal phases that still permit a NEW submission for this exact operation
#: identity. Everything else -- including ``None``, which only an older host
#: that predates this field would ever report -- means some submission
#: attempt may already have crossed PVE's door.
_SUBMISSION_PERMITTED_STATES = (HostSubmissionState.ABSENT, HostSubmissionState.INTENT)

#: A release proof is deliberately distinct from permission to submit. A
#: transient absent/intent observation can be invalidated by a delayed helper;
#: only the durable host seal may release the backend's global slot.
_RELEASE_PROVED_STATES = (HostSubmissionState.SEALED_NOT_SUBMITTED,)


class PackageUpdateSnapshotOrchestrator:
    """Coordinate authority and one dark host boundary for one snapshot.

    Instantiated only by hermetic tests in this stage. It performs no package
    mutation, no snapshot deletion, and no rollback submission.

    A NEW PVE submission is only ever attempted from inside
    :meth:`InventoryAuthority.execute_snapshot_submission_if_current`, which
    holds this backend's own authority write lock across exactly one bounded
    submission-only host call. Recovering evidence about an operation that may
    already have been submitted -- reading the host's durable journal state,
    and polling a known task to completion -- never requires that lock, and
    happens entirely outside it: see the module docstring's "submission
    critical section" section.
    """

    def __init__(
        self,
        authority: InventoryAuthority,
        host_control: PackageUpdateSnapshotHostControl,
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

    def ensure_job_owned_snapshot(self, job_id: str) -> SnapshotStageResult:
        """Drive one job from ``issued`` to a confirmed job-owned snapshot.

        Re-entrant by design. A job already sitting at the write-ahead
        uncertainty boundary after a crash skips straight to the host
        boundary, which reattaches to the operation this same deterministic
        identity already started rather than submitting a second one.
        """

        job = self._authority.package_update_job(job_id)
        if job.status is not PackageUpdateJobStatus.ACTIVE:
            raise PackageUpdateSnapshotError(
                "package update job is terminal"
            )
        confirmed_rank = checkpoint_rank(PackageUpdateCheckpoint.SNAPSHOT_CONFIRMED)
        current_rank = checkpoint_rank(job.checkpoint)
        if current_rank == confirmed_rank:
            return SnapshotStageResult(
                outcome=SnapshotOperationOutcome.COMPLETED, job=job
            )
        if current_rank > confirmed_rank:
            # Past this stage's remit entirely. Never touch a job that may
            # already have mutated packages.
            raise PackageUpdateSnapshotError(
                "package update job has advanced past the snapshot stage"
            )

        if job.checkpoint is not PackageUpdateCheckpoint.SNAPSHOT_MAY_HAVE_STARTED:
            # A. Revalidate the job's whole current authority context, and B.
            # record preflight. Both refuse a stale job while nothing durable
            # about a snapshot exists yet.
            job = self._authority.record_package_update_preflight_passed(job_id)
            # C + D. Derive the deterministic identity and durably commit the
            # write-ahead intent. Everything after this line must assume a PVE
            # snapshot mutation may already have been submitted.
            job = self._authority.record_package_update_snapshot_intent(job.job_id)

        # Both halves are pure reads over immutable job identity, so a
        # restarted attempt derives exactly the same operation.
        identity = self._authority.package_update_snapshot_identity(job.job_id)
        ownership = self._authority.package_update_snapshot_ownership(job.job_id)

        # E1. READ the durable host operation state first. This is a pure
        # read of evidence that may already exist, so it never requires
        # current authority -- a stale incarnation must never cause it to be
        # discarded (see AGENTS.md, "current authority is not required
        # merely to recover evidence").
        result = self._read_inspection(job, identity, ownership)

        if result.submission_state in _RELEASE_PROVED_STATES:
            # A prior attempt may have durably sealed the host and then died
            # before committing the backend block. Re-enter the idempotent
            # seal under the backend writer lock to finish that transition.
            return self._resolve_pre_submission_block(job, identity, ownership)

        if result.outcome is SnapshotOperationOutcome.NOT_SUBMITTED:
            # A journal-backed transient pre-submit observation is routing
            # evidence only. Seal before deciding whether the backend slot can
            # be released; do not retry the PVE read through submission.
            return self._resolve_pre_submission_block(job, identity, ownership)

        if result.submission_state in _SUBMISSION_PERMITTED_STATES:
            # E2. The host PROVED, from its own durable journal, that no
            # submission has ever crossed the door for this exact operation
            # identity, so a NEW one is still possible. This is the only
            # branch that may mutate PVE, and the whole of it runs inside the
            # short authority critical section: current authority is
            # re-proved and held stable through the submission boundary, and
            # the submission-only host call it invokes never polls a PVE task
            # to completion, so the write lock is held only for one bounded
            # round trip.
            try:
                result = self._authority.execute_snapshot_submission_if_current(
                    job.job_id,
                    lambda: self._host_control.ensure_pre_update_snapshot_submitted(
                        snapshot_operation_id=identity.snapshot_operation_id,
                        snapshot_name=identity.snapshot_name,
                        vmid=job.expected_vmid,
                        expected_node=job.expected_node_name,
                        ownership=ownership,
                    ),
                )
            except SnapshotSubmissionRefusedBeforeCallback:
                # This is the ONE case structurally guaranteed to mean
                # current authority refused BEFORE the callback ever ran: the
                # host was never asked to submit anything. That alone must
                # never authorize a submission, but it must also never be
                # allowed to fence this job's global slot forever purely
                # because Hubinet's own authority context moved on -- see
                # _resolve_pre_submission_block. A generic AuthorityConflict
                # (terminal job, wrong checkpoint, or any other lifecycle
                # conflict) says nothing about whether the host was called
                # and must NOT be routed here -- it falls through to the
                # ordinary uncertain path below instead.
                return self._resolve_pre_submission_block(job, identity, ownership)
            except Exception as exc:  # noqa: BLE001 - any failure here is uncertain
                return self._uncertain(
                    job.job_id,
                    f"snapshot host submission did not return an outcome: "
                    f"{type(exc).__name__}",
                )
            result = self._validated(result, identity.snapshot_operation_id)

        return self._finish(job, identity, ownership, result)

    def _finish(
        self,
        job: PackageUpdateJob,
        identity: PackageUpdateSnapshotIdentity,
        ownership: SnapshotOwnership,
        result: HostSnapshotResult,
        *,
        allow_pre_submission_seal: bool = True,
    ) -> SnapshotStageResult:
        # F. Task polling and canonical recovery happen entirely outside the
        # authority critical section: every iteration here is a bounded,
        # read-only host inspection, never a resubmission and never a held
        # database writer lock.
        result = self._poll_until_resolved(job, identity, ownership, result)
        return self._apply_host_result(
            job,
            identity,
            ownership,
            result,
            allow_pre_submission_seal=allow_pre_submission_seal,
        )

    def _resolve_pre_submission_block(
        self,
        job: PackageUpdateJob,
        identity: PackageUpdateSnapshotIdentity,
        ownership: SnapshotOwnership,
    ) -> SnapshotStageResult:
        """Decide, and durably apply, a pre-submission release.

        Reached when transient journal evidence says submission may still be
        preventable, or when a prior attempt already reports the durable seal.
        Neither ``absent`` nor ``intent`` may terminalize the job: a helper
        launched by a dead backend may still be waiting to take its host
        lease and submit afterward.

        Both are closed the same way, inside
        :meth:`InventoryAuthority.resolve_pre_submission_block`: a FRESH host
        seal -- never an earlier observation -- is written while that method's
        transaction still owns the authority-store writer lock. The seal and
        every submitter use the same per-VMID host lease. If seal wins, the
        delayed submitter must refuse; if submit wins, seal observes a
        post-submission phase and refuses to release the job.

        Anything the seal shows other than ``sealed_not_submitted`` -- a
        submission already in flight, a terminal outcome, or the seal itself
        failing -- is recovered through the ordinary
        evidence pipeline exactly as if this call had never happened: never
        released as unsubmitted, and never resubmitted.
        """

        def _seal() -> tuple[HostSubmissionState | None, str, HostSnapshotResult]:
            try:
                fresh = self._host_control.seal_operation_never_submitted(
                    snapshot_operation_id=identity.snapshot_operation_id,
                    snapshot_name=identity.snapshot_name,
                    vmid=job.expected_vmid,
                    expected_node=job.expected_node_name,
                    ownership=ownership,
                )
            except Exception as exc:  # noqa: BLE001 - an unreturned seal is unknown
                fresh = HostSnapshotResult(
                    outcome=SnapshotOperationOutcome.UNCERTAIN,
                    snapshot_operation_id=identity.snapshot_operation_id,
                    reason=(
                        "snapshot host seal did not return an outcome: "
                        f"{type(exc).__name__}"
                    ),
                )
            fresh = self._validated(fresh, identity.snapshot_operation_id)
            proved_not_submitted = fresh.submission_state in _RELEASE_PROVED_STATES
            reason = (
                "host durably sealed this snapshot operation before submission"
                if proved_not_submitted
                else "host did not durably seal this snapshot operation before "
                "submission"
            )
            return fresh.submission_state, reason, fresh

        try:
            blocked, fresh = self._authority.resolve_pre_submission_block(
                job.job_id, _seal
            )
        except Exception:  # noqa: BLE001 - a contradicted proof stays fenced
            return self._uncertain(
                job.job_id,
                "a durable host seal proved this operation was never submitted, "
                "but durable job state contradicts it",
            )

        if blocked:
            return SnapshotStageResult(
                outcome=SnapshotOperationOutcome.NOT_SUBMITTED,
                job=self._authority.package_update_job(job.job_id),
                reason=fresh.reason,
            )
        # The seal decision is attempted exactly once per orchestration path.
        # If it did not prove the durable phase, ordinary evidence handling
        # may fence/recover the job but must not recurse into another seal.
        return self._finish(
            job, identity, ownership, fresh, allow_pre_submission_seal=False
        )

    def _read_inspection(
        self,
        job: PackageUpdateJob,
        identity: PackageUpdateSnapshotIdentity,
        ownership: SnapshotOwnership,
    ) -> HostSnapshotResult:
        """One bounded, read-only look at the host's durable operation state."""

        try:
            inspected = self._host_control.inspect_job_snapshot_state(
                snapshot_operation_id=identity.snapshot_operation_id,
                snapshot_name=identity.snapshot_name,
                vmid=job.expected_vmid,
                expected_node=job.expected_node_name,
                ownership=ownership,
            )
        except Exception as exc:  # noqa: BLE001 - a failed read is uncertain
            return HostSnapshotResult(
                outcome=SnapshotOperationOutcome.UNCERTAIN,
                snapshot_operation_id=identity.snapshot_operation_id,
                reason=(
                    "snapshot host inspection did not return an outcome: "
                    f"{type(exc).__name__}"
                ),
            )
        return self._validated(inspected, identity.snapshot_operation_id)

    @staticmethod
    def _validated(
        result: HostSnapshotResult, snapshot_operation_id: str
    ) -> HostSnapshotResult:
        if (
            not isinstance(result, HostSnapshotResult)
            or result.snapshot_operation_id != snapshot_operation_id
        ):
            return HostSnapshotResult(
                outcome=SnapshotOperationOutcome.UNCERTAIN,
                snapshot_operation_id=snapshot_operation_id,
                reason="snapshot host operation answered a different operation",
            )
        return result

    def _poll_until_resolved(
        self,
        job: PackageUpdateJob,
        identity: PackageUpdateSnapshotIdentity,
        ownership: SnapshotOwnership,
        result: HostSnapshotResult,
    ) -> HostSnapshotResult:
        """Bounded-poll one in-flight operation purely through read-only inspection.

        Never opens a database transaction and never resubmits anything: it
        only re-reads the host's durable state until the operation the
        submission boundary already crossed for reaches a terminal outcome or
        this bound elapses.

        TWO states are in-flight and therefore pollable, both strictly
        read-only:

        ``submitted`` without a task identity is one of them. It used to be
        skipped, on the reasoning that repeating an identical read could not
        change it -- that reasoning is now false. Submission hands the
        physical `pvesh` to a DETACHED host child and returns immediately, so
        the very first response routinely reports ``submitted`` before the
        durable capture exists at all. That child later writes a crash-safe
        completion marker, and a subsequent inspect promotes it to
        ``task_known`` with the exact UPID. Refusing to look again would end
        the worker cycle as ``snapshot_uncertain`` and demand an operator
        RESUME purely to notice a capture that had already landed.

        ``task_known`` whose task is not yet terminal is the other, exactly as
        before. The durable journal phase alone is not a fresh signal there:
        it stays ``task_known`` forever once a task identity is captured,
        whatever the live PVE task later does. So once a read observes the
        task ITSELF has reached a terminal PVE state (``running`` vs
        ``stopped``, never inferred from anything else), further polling
        cannot make that same task "more terminal" and the loop stops.

        Everything else -- a terminal, sealed, malformed, or otherwise
        non-uncertain result -- stops immediately and is decided by the
        existing strict rules, unchanged: the current canonical evidence
        decides completed/failed/uncertain, a canonical absence is never
        turned into failure, and nothing here ever resubmits.
        """

        def _pending(candidate: HostSnapshotResult) -> bool:
            if candidate.outcome is not SnapshotOperationOutcome.UNCERTAIN:
                return False
            if candidate.submission_state is HostSubmissionState.SUBMITTED:
                # The detached capture may still land and yield the exact
                # UPID. Looking again is a read; it never resubmits.
                return True
            if candidate.submission_state is not HostSubmissionState.TASK_KNOWN:
                return False
            if candidate.task is not None and candidate.task.terminal:
                return False
            return True

        if not _pending(result):
            return result
        deadline = self._monotonic() + self._task_poll_timeout_seconds
        while True:
            result = self._read_inspection(job, identity, ownership)
            if not _pending(result):
                return result
            if self._monotonic() >= deadline:
                return result
            self._sleep(self._task_poll_interval_seconds)

    def _apply_host_result(
        self,
        job: PackageUpdateJob,
        identity: PackageUpdateSnapshotIdentity,
        ownership: SnapshotOwnership,
        result: HostSnapshotResult,
        *,
        allow_pre_submission_seal: bool = True,
    ) -> SnapshotStageResult:
        job_id = job.job_id
        if (
            not isinstance(result, HostSnapshotResult)
            or result.snapshot_operation_id != identity.snapshot_operation_id
        ):
            return self._uncertain(
                job_id, "snapshot host operation answered a different operation"
            )

        # Persist the task identity as soon as it is known, whatever the
        # outcome: it is the only thing that lets a later attempt reattach
        # instead of guessing.
        if result.task_upid is not None:
            try:
                self._authority.record_package_update_snapshot_task(
                    job_id, result.task_upid
                )
            except Exception:  # noqa: BLE001 - a conflicting task is uncertainty
                return self._uncertain(
                    job_id, "observed PVE snapshot task conflicts with the durable one"
                )

        if result.outcome is SnapshotOperationOutcome.NOT_SUBMITTED:
            if allow_pre_submission_seal:
                # A transient absent/intent observation only routes into the
                # seal attempt. It is never itself a release proof.
                return self._resolve_pre_submission_block(job, identity, ownership)
            return self._uncertain(
                job_id,
                result.reason
                or "snapshot operation was not durably sealed before submission",
            )

        if result.outcome is SnapshotOperationOutcome.UNCERTAIN:
            return self._uncertain(
                job_id, result.reason or "snapshot operation outcome is uncertain"
            )

        if result.outcome is SnapshotOperationOutcome.FAILED:
            if result.snapshots is None:
                return self._uncertain(
                    job_id,
                    "snapshot task failed without canonical evidence of absence",
                )
            try:
                job_after = self._authority.fail_package_update_snapshot(
                    job_id,
                    result.reason or "PVE snapshot task failed",
                    result.snapshots,
                )
            except Exception:  # noqa: BLE001 - ambiguous absence stays uncertain
                return self._uncertain(
                    job_id,
                    "snapshot task failed but canonical state does not prove absence",
                )
            return SnapshotStageResult(
                outcome=SnapshotOperationOutcome.FAILED,
                job=job_after,
                reason=result.reason,
            )

        # F. A completed operation still has to prove itself.
        #
        # When a PVE task was observed it must have reached a terminal
        # non-error state: a running or failed task never confirms. When no
        # task was observed the host is asserting canonical-evidence recovery
        # (its journal proved no task identity was ever captured), and the
        # canonical listing is then the whole proof.
        #
        # Either way the canonical listing is mandatory and is re-checked
        # strictly below, so a successful task can never skip confirmation.
        if result.task is not None and not result.task.succeeded:
            return self._uncertain(
                job_id,
                "snapshot operation reported completion without a successful task",
            )
        if result.snapshots is None:
            return self._uncertain(
                job_id, "snapshot operation completed without a canonical listing"
            )
        # G. Strict same-job confirmation, which revalidates current authority
        # once more before the job may reach snapshot_confirmed.
        try:
            job_after = self._authority.confirm_package_update_snapshot(
                job_id, result.snapshots
            )
        except AuthorityConflict:
            # Confirmation deliberately retains its current-authority bar:
            # that is what grants rollback authority. If exact canonical
            # success is independently re-proved while current authority is
            # stale, retain the snapshot but terminalize without confirming.
            try:
                blocked, job_after = (
                    self._authority.block_package_update_after_snapshot_success_with_stale_authority(
                        job_id, result.snapshots
                    )
                )
            except Exception as exc:  # noqa: BLE001 - ambiguity stays fenced
                return self._uncertain(
                    job_id,
                    "canonical snapshot confirmation failed closed: "
                    f"{type(exc).__name__}",
                )
            if blocked:
                return SnapshotStageResult(
                    outcome=SnapshotOperationOutcome.COMPLETED,
                    job=job_after,
                    reason=(
                        "snapshot exists but current package authority became "
                        "stale; retained but not authorized for rollback"
                    ),
                )
            # Authority became current again before the independent resolver
            # ran. Retry normal confirmation once; never recurse.
            try:
                job_after = self._authority.confirm_package_update_snapshot(
                    job_id, result.snapshots
                )
            except Exception as exc:  # noqa: BLE001 - bounded retry only
                return self._uncertain(
                    job_id,
                    "canonical snapshot confirmation failed closed after "
                    f"authority became current: {type(exc).__name__}",
                )
        except Exception as exc:  # noqa: BLE001 - never confirm on ambiguity
            return self._uncertain(
                job_id,
                f"canonical snapshot confirmation failed closed: {type(exc).__name__}",
            )
        return SnapshotStageResult(
            outcome=SnapshotOperationOutcome.COMPLETED, job=job_after
        )

    def _uncertain(self, job_id: str, reason: str) -> SnapshotStageResult:
        """Durably record uncertainty, tolerating concurrent terminalization.

        A concurrent, compliant invocation of this same orchestrator can
        terminalize (or otherwise advance) the job between whatever observed
        the need for this call and this durable write reaching its own
        transaction -- for example it already won the pre-submission seal, or
        already completed submission. `record_package_update_snapshot_uncertain`
        then refuses with `AuthorityConflict`, because there is nothing left
        that this call may legally append. That other invocation's own
        transaction already proved and committed the job's real outcome, so
        it is authoritative: this call must never attempt to undo, rewrite, or
        reopen it, and must never raise a raw concurrency exception out of the
        public orchestration surface solely because uncertainty could no
        longer be appended. Re-read the durable job and return a typed,
        fenced result describing that instead, performing no further
        mutation. If the re-read shows the job unexpectedly still eligible,
        the conflict was a genuine invariant violation, not this race, and
        stays fail-closed by re-raising.
        """

        try:
            job = self._authority.record_package_update_snapshot_uncertain(
                job_id, reason[:500]
            )
        except AuthorityConflict:
            job = self._authority.package_update_job(job_id)
            still_eligible = (
                job.status is PackageUpdateJobStatus.ACTIVE
                and job.checkpoint is PackageUpdateCheckpoint.SNAPSHOT_MAY_HAVE_STARTED
            )
            if still_eligible:
                raise
            return SnapshotStageResult(
                outcome=SnapshotOperationOutcome.UNCERTAIN,
                job=job,
                reason=(
                    "a concurrent invocation already resolved this job before "
                    "this uncertainty could be durably recorded; the job's own "
                    "current state is authoritative and was left untouched"
                ),
            )
        return SnapshotStageResult(
            outcome=SnapshotOperationOutcome.UNCERTAIN, job=job, reason=reason
        )

    def select_rollback_target(
        self, job_id: str, observed: Sequence[ObservedSnapshot]
    ) -> PackageUpdateRollbackTarget:
        """Authorize the one snapshot this exact job may roll back to.

        Rollback *submission* is deliberately not implemented in this stage;
        see `ARCHITECTURE.md`. This is the authorization contract only.
        """

        return self._authority.select_package_update_rollback_target(
            job_id, observed
        )
