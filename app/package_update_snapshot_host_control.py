"""Dark bounded SSH transport for job-owned pre-update snapshot operations.

**Production-reachable and deployed.** `app/inventory_runtime.py` constructs
exactly one of these for the package-update worker, and both bootstrap and
the product updater install its forced-command helper together with a key
dedicated to this boundary alone
(`deploy/lib/bootstrap-update-boundaries.sh`,
`deploy/lib/update-boundaries.sh`). Deploying the channel broadens no PVE API
privilege: the deployed identity stays `Sys.Audit` plus `VM.Audit`.

It is a separate, purpose-specific client from `app/package_scan_host_control.py`
and deliberately does not resurrect the removed generic `app/host_control.py`:
pinned known-hosts trust, strict typed JSON, bounded request and response
sizes, bounded timeouts, no password authentication, no agent or port
forwarding, no interactive shell, and no arbitrary remote command.
"""

from __future__ import annotations

from collections.abc import Mapping
import json
from pathlib import Path
import re
from typing import Any

from app.inventory import (
    MAX_SNAPSHOT_HOST_TIMEOUT_SECONDS,
    ObservedSnapshot,
    SnapshotOwnership,
)
from app.package_scan_host_control import (
    BoundedProcessResult,
    ProcessRunner,
    _bounded_process_runner,
)
from app.package_update_snapshot import (
    HostSnapshotResult,
    HostSubmissionState,
    PackageUpdateSnapshotError,
    SnapshotEvidenceError,
    SnapshotOperationOutcome,
    classify_task_status,
    parse_canonical_snapshot_listing,
)


_HOST_RE = re.compile(r"[A-Za-z0-9_.:-]+")
_USER_RE = re.compile(r"[A-Za-z0-9_.-]+")
_NODE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,62}")
_SNAPSHOT_NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{1,39}")
_MAX_REQUEST_BYTES = 8192

#: Outcomes the dark helper may report, mapped onto the orchestrator's typed
#: vocabulary.
#:
#: ``absent`` is only produced by the read-only ``inspect_job_snapshot_state``
#: operation, and it maps to UNCERTAIN, never FAILED. A canonical absence is
#: an *observation*, not proof that an already-submitted PVE snapshot
#: operation terminated: the detached host runner may still be running and
#: about to create the snapshot. Only a terminal failed PVE task (which the
#: helper reports as ``failed``, with its own canonical evidence attached) may
#: reach the FAILED branch, and only the explicit durable ``not_submitted``
#: proof may release a job that was never submitted at all.
_OUTCOMES = {
    "completed": SnapshotOperationOutcome.COMPLETED,
    "failed": SnapshotOperationOutcome.FAILED,
    "uncertain": SnapshotOperationOutcome.UNCERTAIN,
    "absent": SnapshotOperationOutcome.UNCERTAIN,
    "not_submitted": SnapshotOperationOutcome.NOT_SUBMITTED,
}

#: The one token that may downgrade a host failure from "unknown" to "proved
#: nothing was submitted". Matched exactly: an absent, unknown, or malformed
#: value stays uncertain, so an older helper that does not report submission
#: proof can never accidentally release a fenced job.
_HELPER_NOT_SUBMITTED = "not_submitted"

#: The helper's exact classification for "the per-VMID lease is already
#: held", which is precisely what this operation's own detached destructive
#: runner does for the whole of its physical `pvesh`. Matched exactly against
#: the typed classification and never against reason text, so an unknown or
#: malformed value stays plain UNKNOWN. It proves nothing on its own -- see
#: `HostSnapshotResult.host_operation_in_progress`.
_HELPER_OPERATION_IN_PROGRESS = "operation_in_progress"


class SshPackageUpdateSnapshotHostControl:
    """One bounded typed request per snapshot operation, over pinned-key SSH."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        user: str,
        private_key_path: Path,
        known_hosts_path: Path,
        timeout_seconds: int,
        max_result_bytes: int,
        runner: ProcessRunner = _bounded_process_runner,
    ) -> None:
        if (
            not isinstance(host, str)
            or not _HOST_RE.fullmatch(host)
            or host.startswith("-")
        ):
            raise ValueError("host-control host is invalid")
        if (
            not isinstance(user, str)
            or not _USER_RE.fullmatch(user)
            or user.startswith("-")
        ):
            raise ValueError("host-control user is invalid")
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("host-control port is invalid")
        if (
            type(timeout_seconds) is not int
            or not 1 <= timeout_seconds <= MAX_SNAPSHOT_HOST_TIMEOUT_SECONDS
        ):
            # This bound is deliberate, not the historical 3600s ceiling: a
            # snapshot host critical section holds the authority store's
            # writer lock for its whole duration (see
            # `app.inventory.contention_policy`), so an uncapped timeout here
            # would make ordinary concurrent writer contention effectively
            # uncapped too, whatever the store's own wait budget says.
            raise ValueError(
                "host-control timeout must be between 1 and "
                f"{MAX_SNAPSHOT_HOST_TIMEOUT_SECONDS} seconds"
            )
        if (
            type(max_result_bytes) is not int
            or not 1024 <= max_result_bytes <= 16 * 1024 * 1024
        ):
            raise ValueError("host-control result bound is invalid")
        key_path = Path(private_key_path)
        hosts_path = Path(known_hosts_path)
        if not key_path.is_absolute() or not hosts_path.is_absolute():
            raise ValueError("host-control trust paths must be absolute")
        self._host = host
        self._port = port
        self._user = user
        self._private_key_path = key_path
        self._known_hosts_path = hosts_path
        self._timeout_seconds = timeout_seconds
        self._max_result_bytes = max_result_bytes
        self._runner = runner

    # -- typed operations ------------------------------------------------

    def ensure_pre_update_snapshot_submitted(
        self,
        *,
        snapshot_operation_id: str,
        snapshot_name: str,
        vmid: int,
        expected_node: str,
        ownership: SnapshotOwnership,
    ) -> HostSnapshotResult:
        return self._request(
            "ensure_pre_update_snapshot_submitted",
            snapshot_operation_id=snapshot_operation_id,
            snapshot_name=snapshot_name,
            vmid=vmid,
            expected_node=expected_node,
            ownership=ownership,
        )

    def inspect_job_snapshot_state(
        self,
        *,
        snapshot_operation_id: str,
        snapshot_name: str,
        vmid: int,
        expected_node: str,
        ownership: SnapshotOwnership,
    ) -> HostSnapshotResult:
        return self._request(
            "inspect_job_snapshot_state",
            snapshot_operation_id=snapshot_operation_id,
            snapshot_name=snapshot_name,
            vmid=vmid,
            expected_node=expected_node,
            ownership=ownership,
        )

    def seal_operation_never_submitted(
        self,
        *,
        snapshot_operation_id: str,
        snapshot_name: str,
        vmid: int,
        expected_node: str,
        ownership: SnapshotOwnership,
    ) -> HostSnapshotResult:
        return self._request(
            "seal_operation_never_submitted",
            snapshot_operation_id=snapshot_operation_id,
            snapshot_name=snapshot_name,
            vmid=vmid,
            expected_node=expected_node,
            ownership=ownership,
        )

    # -- transport -------------------------------------------------------

    def _request(
        self,
        operation: str,
        *,
        snapshot_operation_id: str,
        snapshot_name: str,
        vmid: int,
        expected_node: str,
        ownership: SnapshotOwnership,
    ) -> HostSnapshotResult:
        if operation not in (
            "ensure_pre_update_snapshot_submitted",
            "inspect_job_snapshot_state",
            "seal_operation_never_submitted",
        ):
            raise ValueError("unsupported snapshot host-control operation")
        if type(vmid) is not int or not 100 <= vmid <= 999_999_999:
            raise ValueError("vmid must be a valid PVE integer VMID")
        if not isinstance(expected_node, str) or not _NODE_RE.fullmatch(expected_node):
            raise ValueError("expected_node is invalid")
        if not isinstance(snapshot_name, str) or not _SNAPSHOT_NAME_RE.fullmatch(
            snapshot_name
        ):
            raise ValueError("snapshot_name is not a valid PVE snapshot name")
        if not isinstance(ownership, SnapshotOwnership):
            raise ValueError("ownership metadata is required")

        request = {
            "request_version": 1,
            "operation": operation,
            "target": {"vmid": vmid, "expected_node": expected_node},
            "operation_identity": {
                "snapshot_operation_id": snapshot_operation_id,
                "snapshot_name": snapshot_name,
            },
            # Typed fields only. The helper builds the snapshot description
            # from these itself, so no free text ever crosses this boundary.
            "ownership": {
                "job_id": ownership.job_id,
                "resource_id": ownership.resource_id,
                "resource_continuity_revision": ownership.resource_continuity_revision,
                "inventory_source_id": ownership.inventory_source_id,
                "backend_instance_id": ownership.backend_instance_id,
            },
        }
        encoded = json.dumps(
            request, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        if len(encoded) > _MAX_REQUEST_BYTES:
            raise PackageUpdateSnapshotError(
                "host-control request exceeded its structural bound"
            )
        result = self._runner(
            self._argv(), encoded, float(self._timeout_seconds), self._max_result_bytes
        )
        return self._parse(result, snapshot_operation_id)

    def _argv(self) -> tuple[str, ...]:
        return (
            "ssh",
            "-T",
            "-p",
            str(self._port),
            "-i",
            str(self._private_key_path),
            "-o",
            "BatchMode=yes",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={self._known_hosts_path}",
            "-o",
            "PasswordAuthentication=no",
            "-o",
            "KbdInteractiveAuthentication=no",
            "-o",
            "ForwardAgent=no",
            "-o",
            "ClearAllForwardings=yes",
            "-o",
            f"ConnectTimeout={min(30, self._timeout_seconds)}",
            "-o",
            f"ServerAliveInterval={min(15, max(1, self._timeout_seconds // 3))}",
            "-o",
            "ServerAliveCountMax=2",
            f"{self._user}@{self._host}",
        )

    def _parse(
        self, result: BoundedProcessResult, snapshot_operation_id: str
    ) -> HostSnapshotResult:
        # Every transport-level failure is an UNCERTAIN outcome, never a
        # failure: a lost answer says nothing about whether PVE ran the
        # mutation, and the caller must not resubmit on it.
        if result.timed_out:
            return self._uncertain(
                snapshot_operation_id, "host-control request timed out"
            )
        if result.output_exceeded:
            return self._uncertain(
                snapshot_operation_id,
                "host-control result exceeded its configured bound",
            )
        if result.returncode != 0 and not result.stdout:
            return self._uncertain(
                snapshot_operation_id, "host-control SSH execution failed"
            )
        try:
            payload = json.loads(result.stdout.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return self._uncertain(
                snapshot_operation_id, "host-control returned a malformed response"
            )
        return self._parse_payload(payload, snapshot_operation_id)

    def _parse_payload(
        self, payload: Any, snapshot_operation_id: str
    ) -> HostSnapshotResult:
        if not isinstance(payload, Mapping) or payload.get("response_version") != 1:
            return self._uncertain(
                snapshot_operation_id, "host-control returned an unsupported response"
            )
        if payload.get("snapshot_operation_id") != snapshot_operation_id:
            return self._uncertain(
                snapshot_operation_id,
                "host-control answered a different snapshot operation",
            )
        if payload.get("ok") is not True:
            error = payload.get("error")
            classification = "unclassified"
            submission = None
            if isinstance(error, Mapping):
                classification = str(error.get("classification") or "unclassified")[
                    :100
                ]
                submission = error.get("submission")
            if submission == _HELPER_NOT_SUBMITTED:
                # The helper read a pre-submission journal phase. This is
                # routing evidence for the durable seal attempt, never itself
                # a backend release proof. It is keyed off the exact token,
                # never `classification`; the same classification after
                # submission stays uncertain.
                return HostSnapshotResult(
                    outcome=SnapshotOperationOutcome.NOT_SUBMITTED,
                    snapshot_operation_id=snapshot_operation_id,
                    reason=(
                        "host journal was pre-submission "
                        f"({classification})"
                    ),
                )
            return self._uncertain(
                snapshot_operation_id,
                f"host-control reported a failure ({classification})",
                host_operation_in_progress=(
                    classification == _HELPER_OPERATION_IN_PROGRESS
                ),
            )
        outcome = _OUTCOMES.get(str(payload.get("outcome")))
        if outcome is None:
            return self._uncertain(
                snapshot_operation_id, "host-control returned an unknown outcome"
            )
        task_upid = payload.get("task_upid")
        if task_upid is not None and (
            not isinstance(task_upid, str) or len(task_upid) > 300
        ):
            return self._uncertain(
                snapshot_operation_id, "host-control returned a malformed task identity"
            )
        task = None
        raw_task = payload.get("task")
        if raw_task is not None:
            try:
                task = classify_task_status(raw_task)
            except SnapshotEvidenceError:
                return self._uncertain(
                    snapshot_operation_id,
                    "host-control returned a malformed task status",
                )
        snapshots: tuple[ObservedSnapshot, ...] | None = None
        raw_snapshots = payload.get("snapshots")
        if raw_snapshots is not None:
            try:
                snapshots = parse_canonical_snapshot_listing(raw_snapshots)
            except SnapshotEvidenceError:
                return self._uncertain(
                    snapshot_operation_id,
                    "host-control returned a malformed snapshot listing",
                )
        reason = payload.get("reason")
        raw_submission_state = payload.get("submission_state")
        submission_state: HostSubmissionState | None = None
        if raw_submission_state is not None:
            try:
                submission_state = HostSubmissionState(str(raw_submission_state))
            except ValueError:
                return self._uncertain(
                    snapshot_operation_id,
                    "host-control returned an unknown submission state",
                )
        if not self._response_fields_are_consistent(
            outcome=outcome,
            submission_state=submission_state,
            task_upid=task_upid,
            task=task,
            snapshots=snapshots,
        ):
            return self._uncertain(
                snapshot_operation_id,
                "host-control returned contradictory snapshot operation fields",
            )
        return HostSnapshotResult(
            outcome=outcome,
            snapshot_operation_id=snapshot_operation_id,
            task_upid=task_upid,
            task=task,
            snapshots=snapshots,
            reason=str(reason)[:500] if isinstance(reason, str) else None,
            submission_state=submission_state,
        )

    @staticmethod
    def _response_fields_are_consistent(
        *,
        outcome: SnapshotOperationOutcome,
        submission_state: HostSubmissionState | None,
        task_upid: str | None,
        task: Any,
        snapshots: tuple[ObservedSnapshot, ...] | None,
    ) -> bool:
        if task is not None and (task_upid is None or task.upid != task_upid):
            return False
        if submission_state is None:
            return task is None or task_upid is not None
        if submission_state in (
            HostSubmissionState.ABSENT,
            HostSubmissionState.INTENT,
        ):
            return (
                task_upid is None
                and task is None
                and outcome
                in (
                    SnapshotOperationOutcome.UNCERTAIN,
                    SnapshotOperationOutcome.NOT_SUBMITTED,
                )
            )
        if submission_state is HostSubmissionState.SEALED_NOT_SUBMITTED:
            return (
                outcome is SnapshotOperationOutcome.NOT_SUBMITTED
                and task_upid is None
                and task is None
                and snapshots is None
            )
        if submission_state is HostSubmissionState.SUBMITTED:
            return (
                task_upid is None
                and task is None
                and outcome
                in (
                    SnapshotOperationOutcome.UNCERTAIN,
                    SnapshotOperationOutcome.COMPLETED,
                )
            )
        if submission_state is HostSubmissionState.TASK_KNOWN:
            return (
                task_upid is not None
                and outcome is not SnapshotOperationOutcome.NOT_SUBMITTED
            )
        if submission_state is HostSubmissionState.TERMINAL:
            return (
                outcome is not SnapshotOperationOutcome.NOT_SUBMITTED
                and (task is None or task.terminal)
            )
        return False

    @staticmethod
    def _uncertain(
        snapshot_operation_id: str,
        reason: str,
        *,
        host_operation_in_progress: bool = False,
    ) -> HostSnapshotResult:
        return HostSnapshotResult(
            outcome=SnapshotOperationOutcome.UNCERTAIN,
            snapshot_operation_id=snapshot_operation_id,
            reason=reason,
            host_operation_in_progress=host_operation_in_progress,
        )
