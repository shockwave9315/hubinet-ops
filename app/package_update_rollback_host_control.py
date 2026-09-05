"""Dark bounded SSH transport for same-job PVE rollback operations.

**Production-reachable and deployed.** `app/inventory_runtime.py` constructs
exactly one of these for the package-update worker, and both bootstrap and
the product updater install its forced-command helper together with a key
dedicated to this boundary alone
(`deploy/lib/bootstrap-update-boundaries.sh`,
`deploy/lib/update-boundaries.sh`). Deploying the channel broadens no PVE API
privilege: the deployed identity stays `Sys.Audit` plus `VM.Audit`, and a
rollback is still only ever started by an explicit operator request.

It is a separate, purpose-specific client from the scan, snapshot, and
mutation transports, and deliberately does not resurrect the removed generic
`app/host_control.py`: pinned known-hosts trust, strict typed JSON, bounded
request and response sizes, bounded timeouts, no password authentication, no
agent or port forwarding, no interactive shell, and no arbitrary remote
command.

Every field of the request is a typed authority fact assembled by
`InventoryAuthority.package_update_rollback_request`. There is no snapshot
name parameter a caller may choose, and no `start` parameter at all: the
helper pins PVE's `start` to 0 as its own code-owned constant, so a
successful rollback always leaves the guest stopped.
"""

from __future__ import annotations

from collections.abc import Mapping
import json
from pathlib import Path
import re
from typing import Any

from app.inventory import (
    HostRollbackState,
    MAX_ROLLBACK_SUBMISSION_TIMEOUT_SECONDS,
    ObservedSnapshot,
    PackageUpdateRollbackRequest,
    SnapshotOwnership,
)
from app.package_scan_host_control import (
    BoundedProcessResult,
    ProcessRunner,
    _bounded_process_runner,
)
from app.package_update_rollback import (
    HostRollbackResult,
    PackageUpdateRollbackError,
    RollbackOperationOutcome,
)
from app.package_update_snapshot import (
    SnapshotEvidenceError,
    classify_task_status,
    parse_canonical_snapshot_listing,
)


_HOST_RE = re.compile(r"[A-Za-z0-9_.:-]+")
_USER_RE = re.compile(r"[A-Za-z0-9_.-]+")
_NODE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,62}")
_SNAPSHOT_NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{1,39}")
_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_MAX_REQUEST_BYTES = 8192

_OPERATIONS = (
    "submit_same_job_rollback",
    "inspect_rollback_state",
    "seal_rollback_never_submitted",
)

#: Outcomes the dark helper may report, mapped onto the orchestrator's typed
#: vocabulary. ``absent`` maps to UNCERTAIN, never FAILED: a canonical absence
#: is an observation, not proof that an already-submitted PVE rollback
#: terminated; the detached host runner may still be active.
_OUTCOMES = {
    "completed": RollbackOperationOutcome.COMPLETED,
    "failed": RollbackOperationOutcome.FAILED,
    "uncertain": RollbackOperationOutcome.UNCERTAIN,
    "absent": RollbackOperationOutcome.UNCERTAIN,
    "not_submitted": RollbackOperationOutcome.NOT_SUBMITTED,
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
#: `HostRollbackResult.host_operation_in_progress`.
_HELPER_OPERATION_IN_PROGRESS = "operation_in_progress"


class SshPackageUpdateRollbackHostControl:
    """One bounded typed request per rollback operation, over pinned-key SSH."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        user: str,
        private_key_path: Path,
        known_hosts_path: Path,
        submission_timeout_seconds: int,
        inspection_timeout_seconds: int,
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
            type(submission_timeout_seconds) is not int
            or not 1
            <= submission_timeout_seconds
            <= MAX_ROLLBACK_SUBMISSION_TIMEOUT_SECONDS
        ):
            # This ceiling is deliberate and load-bearing: the submission and
            # seal operations run while the backend holds its authority
            # store's writer lock (see `app.inventory.contention_policy`), so
            # an uncapped timeout here would make ordinary concurrent writer
            # contention effectively uncapped too.
            raise ValueError(
                "rollback submission timeout must be between 1 and "
                f"{MAX_ROLLBACK_SUBMISSION_TIMEOUT_SECONDS} seconds"
            )
        if (
            type(inspection_timeout_seconds) is not int
            or not 1 <= inspection_timeout_seconds <= 3600
        ):
            # The read-only inspection runs strictly OUTSIDE the writer lock
            # and may legitimately take longer than a submission, so it is
            # deliberately not bound by the ceiling above.
            raise ValueError(
                "rollback inspection timeout must be between 1 and 3600 seconds"
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
        self._submission_timeout_seconds = submission_timeout_seconds
        self._inspection_timeout_seconds = inspection_timeout_seconds
        self._max_result_bytes = max_result_bytes
        self._runner = runner

    # -- typed operations ------------------------------------------------

    def submit_same_job_rollback(
        self, request: PackageUpdateRollbackRequest
    ) -> HostRollbackResult:
        return self._request(
            "submit_same_job_rollback",
            request,
            timeout_seconds=self._submission_timeout_seconds,
        )

    def inspect_rollback_state(
        self, request: PackageUpdateRollbackRequest
    ) -> HostRollbackResult:
        return self._request(
            "inspect_rollback_state",
            request,
            timeout_seconds=self._inspection_timeout_seconds,
        )

    def seal_rollback_never_submitted(
        self, request: PackageUpdateRollbackRequest
    ) -> HostRollbackResult:
        return self._request(
            "seal_rollback_never_submitted",
            request,
            timeout_seconds=self._submission_timeout_seconds,
        )

    # -- transport -------------------------------------------------------

    def _request(
        self,
        operation: str,
        request: PackageUpdateRollbackRequest,
        *,
        timeout_seconds: int,
    ) -> HostRollbackResult:
        if operation not in _OPERATIONS:
            raise ValueError("unsupported rollback host-control operation")
        if not isinstance(request, PackageUpdateRollbackRequest):
            raise ValueError("a typed rollback request is required")
        for field in (
            "rollback_operation_id",
            "backend_instance_id",
            "job_id",
            "resource_id",
            "binding_id",
            "snapshot_operation_id",
        ):
            value = getattr(request, field)
            if not isinstance(value, str) or not _UUID_RE.fullmatch(value):
                raise ValueError(f"{field} must be a canonical UUID")
        if type(request.vmid) is not int or not 100 <= request.vmid <= 999_999_999:
            raise ValueError("vmid must be a valid PVE integer VMID")
        if not isinstance(request.expected_node, str) or not _NODE_RE.fullmatch(
            request.expected_node
        ):
            raise ValueError("expected_node is invalid")
        if not isinstance(
            request.snapshot_name, str
        ) or not _SNAPSHOT_NAME_RE.fullmatch(request.snapshot_name):
            raise ValueError("snapshot_name is not a valid PVE snapshot name")
        for field in ("locator_generation", "resource_continuity_revision"):
            value = getattr(request, field)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{field} must be a positive integer")
        expected_ownership = request.expected_snapshot_ownership
        if not isinstance(expected_ownership, SnapshotOwnership):
            raise ValueError("expected snapshot ownership metadata is required")

        payload = {
            "request_version": 1,
            "operation": operation,
            "target": {
                "vmid": request.vmid,
                "expected_node": request.expected_node,
            },
            "operation_identity": {
                "rollback_operation_id": request.rollback_operation_id,
                "snapshot_name": request.snapshot_name,
                "snapshot_operation_id": request.snapshot_operation_id,
            },
            # The strict structured ownership the target snapshot MUST carry
            # in its PVE description. A snapshot name is a physical PVE key
            # and never ownership proof, so the host re-proves this against
            # its own fresh listing immediately before the destructive call.
            # Authority-derived; no caller can choose it.
            "expected_snapshot_ownership": {
                "protocol": expected_ownership.protocol,
                "kind": expected_ownership.kind,
                "job_id": expected_ownership.job_id,
                "resource_id": expected_ownership.resource_id,
                "resource_continuity_revision": (
                    expected_ownership.resource_continuity_revision
                ),
                "inventory_source_id": expected_ownership.inventory_source_id,
                "backend_instance_id": expected_ownership.backend_instance_id,
            },
            # Typed fields only. The helper rebuilds its own request
            # fingerprint from these, so no free text crosses this boundary
            # and a request differing in any of them is a different
            # operation.
            "ownership": {
                "job_id": request.job_id,
                "resource_id": request.resource_id,
                "resource_continuity_revision": (
                    request.resource_continuity_revision
                ),
                "binding_id": request.binding_id,
                "locator_generation": request.locator_generation,
                "backend_instance_id": request.backend_instance_id,
            },
        }
        encoded = json.dumps(
            payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        if len(encoded) > _MAX_REQUEST_BYTES:
            raise PackageUpdateRollbackError(
                "host-control request exceeded its structural bound"
            )
        result = self._runner(
            self._argv(timeout_seconds),
            encoded,
            float(timeout_seconds),
            self._max_result_bytes,
        )
        return self._parse(result, request.rollback_operation_id)

    def _argv(self, timeout_seconds: int) -> tuple[str, ...]:
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
            f"ConnectTimeout={min(30, timeout_seconds)}",
            "-o",
            f"ServerAliveInterval={min(15, max(1, timeout_seconds // 3))}",
            "-o",
            "ServerAliveCountMax=2",
            f"{self._user}@{self._host}",
        )

    def _parse(
        self, result: BoundedProcessResult, rollback_operation_id: str
    ) -> HostRollbackResult:
        # Every transport-level failure is UNCERTAIN, never a failure: a lost
        # answer says nothing about whether PVE ran the rollback, and the
        # caller must not resubmit on it.
        if result.timed_out:
            return self._uncertain(
                rollback_operation_id, "host-control request timed out"
            )
        if result.output_exceeded:
            return self._uncertain(
                rollback_operation_id,
                "host-control result exceeded its configured bound",
            )
        if result.returncode != 0 and not result.stdout:
            return self._uncertain(
                rollback_operation_id, "host-control SSH execution failed"
            )
        try:
            payload = json.loads(result.stdout.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return self._uncertain(
                rollback_operation_id, "host-control returned a malformed response"
            )
        return self._parse_payload(payload, rollback_operation_id)

    def _parse_payload(
        self, payload: Any, rollback_operation_id: str
    ) -> HostRollbackResult:
        if not isinstance(payload, Mapping) or payload.get("response_version") != 1:
            return self._uncertain(
                rollback_operation_id,
                "host-control returned an unsupported response",
            )
        if payload.get("rollback_operation_id") != rollback_operation_id:
            return self._uncertain(
                rollback_operation_id,
                "host-control answered a different rollback operation",
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
                # A journal read under the lease was still pre-submission.
                # Routing evidence for the durable seal attempt, never itself
                # a backend release proof, and keyed off the exact token
                # rather than the classification text.
                return HostRollbackResult(
                    outcome=RollbackOperationOutcome.NOT_SUBMITTED,
                    rollback_operation_id=rollback_operation_id,
                    reason=f"host journal was pre-submission ({classification})",
                )
            return self._uncertain(
                rollback_operation_id,
                f"host-control reported a failure ({classification})",
                host_operation_in_progress=(
                    classification == _HELPER_OPERATION_IN_PROGRESS
                ),
            )
        outcome = _OUTCOMES.get(str(payload.get("outcome")))
        if outcome is None:
            return self._uncertain(
                rollback_operation_id, "host-control returned an unknown outcome"
            )
        task_upid = payload.get("task_upid")
        if task_upid is not None and (
            not isinstance(task_upid, str) or len(task_upid) > 300
        ):
            return self._uncertain(
                rollback_operation_id,
                "host-control returned a malformed task identity",
            )
        task = None
        raw_task = payload.get("task")
        if raw_task is not None:
            try:
                task = classify_task_status(raw_task)
            except SnapshotEvidenceError:
                return self._uncertain(
                    rollback_operation_id,
                    "host-control returned a malformed task status",
                )
        snapshots: tuple[ObservedSnapshot, ...] | None = None
        raw_snapshots = payload.get("snapshots")
        if raw_snapshots is not None:
            try:
                snapshots = parse_canonical_snapshot_listing(raw_snapshots)
            except SnapshotEvidenceError:
                return self._uncertain(
                    rollback_operation_id,
                    "host-control returned a malformed snapshot listing",
                )
        reason = payload.get("reason")
        raw_state = payload.get("rollback_state")
        rollback_state: HostRollbackState | None = None
        if raw_state is not None:
            try:
                rollback_state = HostRollbackState(str(raw_state))
            except ValueError:
                return self._uncertain(
                    rollback_operation_id,
                    "host-control returned an unknown rollback state",
                )
        if not self._response_fields_are_consistent(
            outcome=outcome,
            rollback_state=rollback_state,
            task_upid=task_upid,
            task=task,
            snapshots=snapshots,
        ):
            return self._uncertain(
                rollback_operation_id,
                "host-control returned contradictory rollback operation fields",
            )
        return HostRollbackResult(
            outcome=outcome,
            rollback_operation_id=rollback_operation_id,
            task_upid=task_upid,
            task=task,
            snapshots=snapshots,
            reason=str(reason)[:500] if isinstance(reason, str) else None,
            rollback_state=rollback_state,
        )

    @staticmethod
    def _response_fields_are_consistent(
        *,
        outcome: RollbackOperationOutcome,
        rollback_state: HostRollbackState | None,
        task_upid: str | None,
        task: Any,
        snapshots: tuple[ObservedSnapshot, ...] | None,
    ) -> bool:
        if task is not None and (task_upid is None or task.upid != task_upid):
            return False
        if rollback_state is None:
            return task is None or task_upid is not None
        if rollback_state in (HostRollbackState.ABSENT, HostRollbackState.INTENT):
            return (
                task_upid is None
                and task is None
                and outcome
                in (
                    RollbackOperationOutcome.UNCERTAIN,
                    RollbackOperationOutcome.NOT_SUBMITTED,
                )
            )
        if rollback_state is HostRollbackState.SEALED_NOT_SUBMITTED:
            return (
                outcome is RollbackOperationOutcome.NOT_SUBMITTED
                and task_upid is None
                and task is None
                and snapshots is None
            )
        if rollback_state is HostRollbackState.SUBMITTED:
            # Submitted without a captured UPID is the genuinely uncertain
            # window. It can never be COMPLETED: unlike snapshot create,
            # rollback completion REQUIRES the durable task identity, because
            # canonical state alone (a surviving snapshot, a `parent` value)
            # cannot distinguish this rollback from an earlier one.
            return (
                task_upid is None
                and task is None
                and outcome
                in (
                    RollbackOperationOutcome.UNCERTAIN,
                    RollbackOperationOutcome.FAILED,
                )
            )
        if rollback_state is HostRollbackState.TASK_KNOWN:
            return (
                task_upid is not None
                and outcome is not RollbackOperationOutcome.NOT_SUBMITTED
            )
        if rollback_state is HostRollbackState.TERMINAL:
            return (
                outcome is not RollbackOperationOutcome.NOT_SUBMITTED
                and task_upid is not None
                and (task is None or task.terminal)
            )
        return False

    @staticmethod
    def _uncertain(
        rollback_operation_id: str,
        reason: str,
        *,
        host_operation_in_progress: bool = False,
    ) -> HostRollbackResult:
        return HostRollbackResult(
            outcome=RollbackOperationOutcome.UNCERTAIN,
            rollback_operation_id=rollback_operation_id,
            reason=reason,
            host_operation_in_progress=host_operation_in_progress,
        )
