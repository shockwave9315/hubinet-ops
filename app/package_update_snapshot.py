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
  -> dark typed host operation (submit or reattach; never a blind resubmit)
  -> terminal PVE task evidence
  -> fresh canonical PVE snapshot listing
  -> strict same-job ownership match
  -> snapshot_confirmed
```

The write-ahead checkpoint is committed *before* the host-control call so a
crash anywhere after it leaves a durable "a snapshot operation may already
have been submitted" fact. That state is never treated as "definitely no
mutation happened", never replayed, and never silently released.

The one exception is not an inference but a proof: the host's durable
operation journal can show that a failure happened while the operation was
still at `intent`, which means the submission subprocess was never launched
for it. Only that proof releases the job (terminalized `blocked`), so an
ordinary pre-flight refusal on the host does not fence the single global
destructive slot forever. Everything else -- a canonical absence, a lock, a
timeout, a lost SSH answer, an unreadable journal -- stays uncertain.

## Verified PVE task semantics

Established from current Proxmox VE sources, not from Hubinet 0.4 behaviour:

- `POST /nodes/{node}/lxc/{vmid}/snapshot` is asynchronous. It returns a UPID
  immediately (`fork_worker('vzsnapshot', ...)`), so a returned POST is never
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

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from app.inventory import (
    InventoryAuthority,
    ObservedSnapshot,
    PackageUpdateCheckpoint,
    PackageUpdateJob,
    PackageUpdateJobStatus,
    PackageUpdateRollbackTarget,
    SnapshotIdentityError,
    SnapshotOwnership,
    checkpoint_rank,
    looks_like_hubinet_snapshot,
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
    """One bounded observation of a PVE asynchronous task."""

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


class PackageUpdateSnapshotHostControl(Protocol):
    """The dark typed host boundary this orchestrator is allowed to use.

    Deliberately narrow: there is no delete operation, no rollback submission,
    no generic action dispatcher, and no place to pass a command string.
    """

    def create_pre_update_snapshot(
        self,
        *,
        snapshot_operation_id: str,
        snapshot_name: str,
        vmid: int,
        expected_node: str,
        ownership: SnapshotOwnership,
    ) -> HostSnapshotResult:
        """Submit, or reattach to, this exact job's snapshot operation."""

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
            ownership = None
            malformed = True
        if ownership is None and not malformed:
            malformed = looks_like_hubinet_snapshot(description)
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


class PackageUpdateSnapshotOrchestrator:
    """Coordinate authority and one dark host boundary for one snapshot.

    Instantiated only by hermetic tests in this stage. It performs no package
    mutation, no snapshot deletion, and no rollback submission.
    """

    def __init__(
        self,
        authority: InventoryAuthority,
        host_control: PackageUpdateSnapshotHostControl,
    ) -> None:
        self._authority = authority
        self._host_control = host_control

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

        # E. The dark host operation. It either submits exactly once or
        # reattaches to the operation this same identity already started.
        try:
            result = self._host_control.create_pre_update_snapshot(
                snapshot_operation_id=identity.snapshot_operation_id,
                snapshot_name=identity.snapshot_name,
                vmid=job.expected_vmid,
                expected_node=job.expected_node_name,
                ownership=ownership,
            )
        except Exception as exc:  # noqa: BLE001 - any failure here is uncertain
            return self._uncertain(
                job.job_id,
                f"snapshot host operation did not return an outcome: "
                f"{type(exc).__name__}",
            )
        return self._apply_host_result(job.job_id, identity.snapshot_operation_id, result)

    def _apply_host_result(
        self,
        job_id: str,
        snapshot_operation_id: str,
        result: HostSnapshotResult,
    ) -> SnapshotStageResult:
        if (
            not isinstance(result, HostSnapshotResult)
            or result.snapshot_operation_id != snapshot_operation_id
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
            # The host proved nothing was submitted for this operation, so the
            # job can be released rather than fencing the global destructive
            # slot forever. If the durable job record contradicts that proof
            # (a task was observed, so it *was* submitted), authority refuses
            # and the job stays fenced.
            reason = result.reason or "snapshot operation was blocked before submission"
            try:
                job = self._authority.block_package_update_before_snapshot_submission(
                    job_id, reason[:500]
                )
            except Exception:  # noqa: BLE001 - a contradicted proof stays fenced
                return self._uncertain(
                    job_id,
                    "host reported no submission but durable job state "
                    "contradicts it",
                )
            return SnapshotStageResult(
                outcome=SnapshotOperationOutcome.NOT_SUBMITTED,
                job=job,
                reason=reason,
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
                job = self._authority.fail_package_update_snapshot(
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
                job=job,
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
            job = self._authority.confirm_package_update_snapshot(
                job_id, result.snapshots
            )
        except Exception as exc:  # noqa: BLE001 - never confirm on ambiguity
            return self._uncertain(
                job_id,
                f"canonical snapshot confirmation failed closed: {type(exc).__name__}",
            )
        return SnapshotStageResult(
            outcome=SnapshotOperationOutcome.COMPLETED, job=job
        )

    def _uncertain(self, job_id: str, reason: str) -> SnapshotStageResult:
        job = self._authority.record_package_update_snapshot_uncertain(
            job_id, reason[:500]
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
