#!/usr/bin/env python3
"""Forced-command PVE boundary for Hubinet's job-owned pre-update snapshots.

**Not deployed.** Neither `deploy/bootstrap-proxmox-0.5.sh` nor
`deploy/update-proxmox-0.5.sh` installs this file, its forced-command
`authorized_keys` entry, or any key for it, and no additional PVE privilege is
provisioned for it. It is a separate file and a separate logical privilege
boundary from `deploy/hubinet-package-scan-helper.py`, which stays scan-only
and is never extended into a mutation helper.

Scope is deliberately minimal:

- LXC only.
- Exactly two typed operations: `inspect_job_snapshot_state` and
  `ensure_pre_update_snapshot_submitted`.
- No arbitrary shell, no command string, no caller-supplied argv, no generic
  action dispatcher. Every `pvesh` argv is built from this file's own fixed
  constants plus validated typed fields.
- **No snapshot delete operation, and no rollback submission.**

`ensure_pre_update_snapshot_submitted` is submission-only: it never polls a
PVE task to completion. It journals the submission, invokes `pvesh create`
at most once, records the task identity the instant PVE returns one, and
returns immediately. The backend is expected to hold no lock of its own
across this call (see `app/package_update_snapshot.py`), and it must not hold
one across task completion either, so the helper never blocks here for PVE's
own async task to finish. `inspect_job_snapshot_state` reads the journaled
task's status exactly once, synchronously, alongside a fresh canonical
listing -- fast, bounded, and repeatable from the caller's own bounded retry
loop -- so completion is observed by the caller polling this cheap read
operation, never by the mutating one blocking internally.

## Durable operation journal

The backend receiving a UPID is not something this helper may rely on: the
caller can die between PVE accepting the mutation and the answer being
recorded anywhere durable. So each snapshot mutation is journaled here, on the
PVE host, keyed by the operation identity the backend derives deterministically
from immutable job identity.

```text
intent      request recorded; submission NOT yet attempted   -> may submit
submitted   submission attempt has begun                     -> NEVER resubmit
task_known  PVE returned a UPID                              -> caller polls that task
terminal    outcome recorded                                 -> replay answer
```

Every response -- from either operation -- reports this exact phase as a
typed `submission_state` field, read straight from the journal rather than
inferred from canonical PVE state or an error string. The caller uses it, not
canonical evidence, to decide whether a NEW submission is still permitted:
`absent` (no journal record at all) and `intent` are the only phases that
permit one; everything else means some submission attempt may already have
crossed PVE's door, and no second one may ever be issued for this operation
identity.

`intent -> submitted` is an atomic rename that is fsynced before the
subprocess is launched, so a journal still reading `intent` proves submission
was never reached. `submitted` without a UPID is the genuinely uncertain
window: this helper then inspects canonical state and may recover a success
only on strict evidence (the exact snapshot, the exact ownership metadata,
complete, and the container not locked by an in-flight operation). Otherwise
it answers `uncertain`. It never submits a second snapshot request because a
caller restarted.

**This journal is the host-side record of record for submission.** Every
failure answer therefore carries a typed `error.submission`:

- `not_submitted` — the journal was at `intent` for this exact operation
  identity when the failure happened, which proves the submission subprocess
  was never launched for it. Only this lets the backend safely terminalize a
  job that is already past its write-ahead uncertainty checkpoint, so it is
  emitted from that durable phase alone, never inferred from an error name, a
  canonical absence, a lock, a timeout, or a transport failure.
- `may_have_been_submitted` — the default for everything else, including an
  unreadable or corrupt journal, a lease held by another invocation, and every
  failure at or after the `submitted` transition.

Destruction of this journal by something outside Hubinet is out of the
product's threat model (see `AGENTS.md`); it is ordinary durable state on the
PVE host, not a defence against an administrator deleting it.

Mutations are serialized per VMID with a kernel `flock`.

## Verified PVE semantics this file depends on

- `POST /nodes/{node}/lxc/{vmid}/snapshot` returns a UPID immediately
  (`fork_worker('vzsnapshot', ...)`); `pvesh create` prints that UPID rather
  than waiting, so the task identity can be journaled before polling.
- `pvesh` resolves the endpoint's own `proxyto => node` by running
  `pvesh --noproxy` on the owning cluster member over PVE's existing root
  SSH trust, so no per-node Hubinet credential is needed here.
- `GET /nodes/{node}/tasks/{upid}/status` gives `status` in
  `running`/`stopped` plus an optional `exitstatus`; PVE's own rule treats
  `OK` and `WARNINGS: <n>` as non-errors.
- `GET /nodes/{node}/lxc/{vmid}/snapshot` includes PVE's synthetic `current`
  pseudo-entry and carries `snapstate` for unfinished snapshots.
- `pve-snapshot-name` is `pve-configid` (`/^[a-z][a-z0-9_-]+$/i`) with
  maxLength 40; `current` and `vzdump` are reserved.
- `GET /nodes/{node}/lxc/{vmid}/config` exposes `lock`, including `snapshot`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import subprocess
import sys
import time
import uuid
from typing import Any


MAX_REQUEST_BYTES = 8192
MAX_COMMAND_OUTPUT_BYTES = 4 * 1024 * 1024
COMMAND_TIMEOUT_SECONDS = 120.0

JOURNAL_DIRECTORY = Path("/var/lib/hubinet-ops/snapshot-operations")

NODE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,62}")
#: PVE `pve-configid`, which `pve-snapshot-name` builds on, plus maxLength 40.
SNAPSHOT_NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{1,39}")
PVE_RESERVED_SNAPSHOT_NAMES = frozenset({"current", "vzdump"})
UPID_RE = re.compile(
    r"UPID:(?:[a-zA-Z0-9](?:[a-zA-Z0-9\-]*[a-zA-Z0-9])?)"
    r":[0-9A-Fa-f]{8}:[0-9A-Fa-f]{8,9}:[0-9A-Fa-f]{8}"
    r":[^:\s/]+:[^:\s/]*:[^:\s/]+:"
)

SNAPSHOT_NAME_PREFIX = "hubinet-preupd-"
SNAPSHOT_METADATA_PROTOCOL = 1
SNAPSHOT_METADATA_MARKER = "hubinet-ops-snapshot-v1"
SNAPSHOT_KIND_PRE_UPDATE = "pre_update"
SNAPSHOT_DESCRIPTION_HEADLINE = (
    "Hubinet Ops job-owned pre-update snapshot - do not delete manually"
)
_NAME_DOMAIN = b"hubinet-ops/package-update/pre-update-snapshot-name/v1"
_OPERATION_DOMAIN = b"hubinet-ops/package-update/snapshot-operation-id/v1"

#: Container config locks that prove some PVE operation is still in flight.
_IN_FLIGHT_LOCKS = frozenset({"snapshot", "snapshot-delete", "rollback"})

OPERATIONS = ("inspect_job_snapshot_state", "ensure_pre_update_snapshot_submitted")

_OWNERSHIP_FIELDS = (
    "job_id",
    "resource_id",
    "resource_continuity_revision",
    "inventory_source_id",
    "backend_instance_id",
)


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    timed_out: bool = False
    output_exceeded: bool = False


Runner = Callable[[tuple[str, ...], float, int], CommandResult]


class RequestError(ValueError):
    """The request did not have the exact expected typed shape."""


#: What this host can PROVE about whether a snapshot mutation was submitted
#: for one operation identity. This is the only thing that may ever let the
#: backend terminalize a job that is already past its write-ahead uncertainty
#: checkpoint, so it is derived from the durable journal, never from an error
#: name, a canonical absence, a timeout, or a transport failure.
SUBMISSION_NOT_SUBMITTED = "not_submitted"
SUBMISSION_UNKNOWN = "may_have_been_submitted"


class SnapshotError(RuntimeError):
    """A snapshot operation failed.

    ``submission`` defaults to ``may_have_been_submitted``: unless a caller
    positively proves otherwise from the durable journal, every failure must
    be treated as one that may have left a PVE mutation in flight.
    """

    def __init__(
        self,
        classification: str,
        message: str,
        *,
        submission: str = SUBMISSION_UNKNOWN,
    ) -> None:
        super().__init__(message)
        self.classification = classification
        self.message = message
        self.submission = submission


def _run_bounded(
    argv: tuple[str, ...], timeout: float, max_output: int
) -> CommandResult:
    process = subprocess.Popen(  # noqa: S603 - every argv shape is fixed below
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        close_fds=True,
    )
    assert process.stdout is not None
    assert process.stderr is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    output = {"stdout": bytearray(), "stderr": bytearray()}
    started = time.monotonic()
    timed_out = False
    exceeded = False
    try:
        while selector.get_map():
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                timed_out = True
                process.kill()
                break
            for key, _ in selector.select(min(remaining, 0.2)):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                output[key.data].extend(chunk)
                if len(output["stdout"]) + len(output["stderr"]) > max_output:
                    exceeded = True
                    process.kill()
                    break
            if exceeded:
                break
    finally:
        selector.close()
        process.wait(timeout=5)
    return CommandResult(
        process.returncode,
        bytes(output["stdout"][: max_output + 1]),
        bytes(output["stderr"][: max_output + 1]),
        timed_out,
        exceeded,
    )


# ---------------------------------------------------------------------------
# Typed request validation
# ---------------------------------------------------------------------------


def _canonical_uuid(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise RequestError(f"{field} must be a canonical UUID")
    try:
        parsed = uuid.UUID(value)
    except ValueError as exc:
        raise RequestError(f"{field} must be a canonical UUID") from exc
    if parsed.int == 0 or str(parsed) != value:
        raise RequestError(f"{field} must be a canonical UUID")
    return value


def _positive_integer(value: Any, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise RequestError(f"{field} must be a positive integer")
    return value


def _canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(
        dict(payload), ensure_ascii=True, sort_keys=True, separators=(",", ":")
    )


def derive_snapshot_identity(
    *,
    backend_instance_id: str,
    job_id: str,
    resource_id: str,
    resource_continuity_revision: int,
) -> tuple[str, str]:
    """Independently re-derive ``(operation_id, snapshot_name)``.

    Deliberately duplicated from the backend's own derivation
    (`app/inventory/snapshot_identity.py`) rather than trusted across the
    privilege boundary: this helper accepts a request's snapshot name only if
    it equals what the request's own identity fields derive here, so it can
    never be talked into creating an arbitrarily named snapshot.
    """

    payload = _canonical_json(
        {
            "backend_instance_id": backend_instance_id,
            "job_id": job_id,
            "resource_id": resource_id,
            "resource_continuity_revision": resource_continuity_revision,
        }
    ).encode("ascii")
    name_digest = hashlib.sha256(_NAME_DOMAIN + b"\n" + payload).hexdigest()
    snapshot_name = SNAPSHOT_NAME_PREFIX + name_digest[:24]
    operation_digest = hashlib.sha256(_OPERATION_DOMAIN + b"\n" + payload).digest()
    operation_id = str(uuid.UUID(bytes=operation_digest[:16], version=5))
    return operation_id, snapshot_name


def build_snapshot_description(ownership: Mapping[str, Any]) -> str:
    """Build the snapshot description from validated typed fields only.

    No caller-supplied free text ever reaches a `pvesh` argv: the description
    is constructed here from this file's fixed grammar.
    """

    payload = _canonical_json(
        {
            "protocol": SNAPSHOT_METADATA_PROTOCOL,
            "kind": SNAPSHOT_KIND_PRE_UPDATE,
            "job_id": ownership["job_id"],
            "resource_id": ownership["resource_id"],
            "resource_continuity_revision": ownership[
                "resource_continuity_revision"
            ],
            "inventory_source_id": ownership["inventory_source_id"],
            "backend_instance_id": ownership["backend_instance_id"],
        }
    )
    return f"{SNAPSHOT_DESCRIPTION_HEADLINE}\n{SNAPSHOT_METADATA_MARKER} {payload}"


def parse_snapshot_description(description: Any) -> dict[str, Any] | None:
    """Strictly parse Hubinet ownership metadata out of a PVE description.

    Returns ``None`` for a description that makes no Hubinet claim at all -- a
    foreign or manual snapshot. Raises :class:`RequestError` when it *does*
    look like a Hubinet snapshot but does not parse into exactly one
    well-formed claim, so the caller fails closed instead of silently
    skipping it.
    """

    if not isinstance(description, str) or "hubinet-ops-snapshot" not in description:
        return None
    marker_lines = [
        line.strip()
        for line in description.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        if line.strip().startswith(SNAPSHOT_METADATA_MARKER)
    ]
    if len(marker_lines) != 1:
        raise RequestError("snapshot metadata is not exactly one marker line")
    remainder = marker_lines[0][len(SNAPSHOT_METADATA_MARKER):]
    if not remainder.startswith(" "):
        raise RequestError("snapshot marker line is malformed")
    try:
        payload = json.loads(remainder.strip())
    except ValueError as exc:
        raise RequestError("snapshot metadata is not valid JSON") from exc
    expected = {"protocol", "kind", *_OWNERSHIP_FIELDS}
    if not isinstance(payload, dict) or set(payload) != expected:
        raise RequestError("snapshot metadata does not have the exact shape")
    if payload["protocol"] != SNAPSHOT_METADATA_PROTOCOL:
        raise RequestError("snapshot metadata protocol is unsupported")
    if payload["kind"] != SNAPSHOT_KIND_PRE_UPDATE:
        raise RequestError("snapshot metadata kind is not pre-update")
    for field in ("job_id", "resource_id", "inventory_source_id", "backend_instance_id"):
        _canonical_uuid(payload[field], field)
    _positive_integer(
        payload["resource_continuity_revision"], "resource_continuity_revision"
    )
    return payload


def validate_request(payload: Any) -> dict[str, Any]:
    """Validate one typed snapshot request independently of the backend."""

    if not isinstance(payload, Mapping) or set(payload) != {
        "request_version",
        "operation",
        "target",
        "operation_identity",
        "ownership",
    }:
        raise RequestError("request must have the exact snapshot-operation shape")
    if payload["request_version"] != 1:
        raise RequestError("unknown host-control request version")
    operation = payload["operation"]
    if operation not in OPERATIONS:
        raise RequestError("unknown host-control operation")

    target = payload["target"]
    if not isinstance(target, Mapping) or set(target) != {"vmid", "expected_node"}:
        raise RequestError("target must have the exact snapshot-operation shape")
    vmid = target["vmid"]
    if type(vmid) is not int or not 100 <= vmid <= 999_999_999:
        raise RequestError("vmid must be a valid PVE integer VMID")
    expected_node = target["expected_node"]
    if not isinstance(expected_node, str) or not NODE_RE.fullmatch(expected_node):
        raise RequestError("expected_node is invalid")

    identity = payload["operation_identity"]
    if not isinstance(identity, Mapping) or set(identity) != {
        "snapshot_operation_id",
        "snapshot_name",
    }:
        raise RequestError("operation_identity must have the exact shape")
    snapshot_operation_id = _canonical_uuid(
        identity["snapshot_operation_id"], "snapshot_operation_id"
    )
    snapshot_name = identity["snapshot_name"]
    if (
        not isinstance(snapshot_name, str)
        or not SNAPSHOT_NAME_RE.fullmatch(snapshot_name)
        or snapshot_name.lower() in PVE_RESERVED_SNAPSHOT_NAMES
    ):
        raise RequestError("snapshot_name is not a valid PVE snapshot name")

    raw_ownership = payload["ownership"]
    if not isinstance(raw_ownership, Mapping) or set(raw_ownership) != set(
        _OWNERSHIP_FIELDS
    ):
        raise RequestError("ownership must have the exact shape")
    ownership = {
        "job_id": _canonical_uuid(raw_ownership["job_id"], "job_id"),
        "resource_id": _canonical_uuid(raw_ownership["resource_id"], "resource_id"),
        "resource_continuity_revision": _positive_integer(
            raw_ownership["resource_continuity_revision"],
            "resource_continuity_revision",
        ),
        "inventory_source_id": _canonical_uuid(
            raw_ownership["inventory_source_id"], "inventory_source_id"
        ),
        "backend_instance_id": _canonical_uuid(
            raw_ownership["backend_instance_id"], "backend_instance_id"
        ),
    }

    # The backend does not get to choose the identity: it must equal what
    # these immutable ownership facts derive here.
    derived_operation_id, derived_name = derive_snapshot_identity(
        backend_instance_id=ownership["backend_instance_id"],
        job_id=ownership["job_id"],
        resource_id=ownership["resource_id"],
        resource_continuity_revision=ownership["resource_continuity_revision"],
    )
    if (
        derived_operation_id != snapshot_operation_id
        or derived_name != snapshot_name
    ):
        raise RequestError(
            "operation identity does not match its own ownership derivation"
        )

    return {
        "operation": operation,
        "vmid": vmid,
        "expected_node": expected_node,
        "snapshot_operation_id": snapshot_operation_id,
        "snapshot_name": snapshot_name,
        "ownership": ownership,
    }


def request_fingerprint(request: Mapping[str, Any]) -> str:
    """Canonical hash of the exact request this operation identity commits to."""

    return hashlib.sha256(
        _canonical_json(
            {
                "vmid": request["vmid"],
                "expected_node": request["expected_node"],
                "snapshot_operation_id": request["snapshot_operation_id"],
                "snapshot_name": request["snapshot_name"],
                "ownership": dict(request["ownership"]),
            }
        ).encode("ascii")
    ).hexdigest()


# ---------------------------------------------------------------------------
# Durable per-operation journal
# ---------------------------------------------------------------------------


JOURNAL_PHASES = ("intent", "submitted", "task_known", "terminal")


class OperationJournal:
    """Atomic, fsynced, per-operation request journal on the PVE host."""

    def __init__(self, directory: Path = JOURNAL_DIRECTORY) -> None:
        self._directory = Path(directory)

    @property
    def directory(self) -> Path:
        return self._directory

    def _path(self, snapshot_operation_id: str) -> Path:
        # The id is a validated canonical UUID, so this never escapes.
        return self._directory / f"op-{snapshot_operation_id}.json"

    def ensure_directory(self) -> None:
        self._directory.mkdir(parents=True, exist_ok=True, mode=0o700)

    def read(self, snapshot_operation_id: str) -> dict[str, Any] | None:
        path = self._path(snapshot_operation_id)
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise SnapshotError(
                "journal_unreadable", "snapshot operation journal is unreadable"
            ) from exc
        try:
            record = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise SnapshotError(
                "journal_corrupt", "snapshot operation journal is corrupt"
            ) from exc
        if (
            not isinstance(record, dict)
            or record.get("journal_version") != 1
            or record.get("snapshot_operation_id") != snapshot_operation_id
            or record.get("phase") not in JOURNAL_PHASES
            or not isinstance(record.get("request_fingerprint"), str)
        ):
            raise SnapshotError(
                "journal_corrupt", "snapshot operation journal is corrupt"
            )
        # A phase is only usable with the facts that phase promises. A
        # 'task_known' record without a decodable task identity must never
        # degrade into a record that looks safe to submit from.
        if record["phase"] == "task_known" and not (
            isinstance(record.get("task_upid"), str)
            and UPID_RE.fullmatch(record["task_upid"])
        ):
            raise SnapshotError(
                "journal_corrupt",
                "snapshot operation journal records a task without its identity",
            )
        if record["phase"] == "terminal" and record.get("outcome") not in (
            "completed",
            "failed",
        ):
            raise SnapshotError(
                "journal_corrupt",
                "snapshot operation journal records a terminal phase without an outcome",
            )
        return record

    def write(self, record: Mapping[str, Any]) -> None:
        """Atomically replace the journal and fsync data, entry, and directory."""

        self.ensure_directory()
        path = self._path(str(record["snapshot_operation_id"]))
        temporary = path.with_name(path.name + ".tmp")
        payload = _canonical_json(record).encode("ascii")
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
        )
        try:
            os.write(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, path)
        directory_descriptor = os.open(self._directory, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)


class VmidMutationLock:
    """Kernel `flock` serializing snapshot mutation per VMID."""

    def __init__(self, vmid: int, directory: Path = JOURNAL_DIRECTORY) -> None:
        self._path = Path(directory) / f"vmid-{int(vmid)}.lock"
        self._descriptor: int | None = None

    def __enter__(self) -> VmidMutationLock:
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._descriptor = os.open(
            self._path, os.O_WRONLY | os.O_CREAT | os.O_CLOEXEC, 0o600
        )
        try:
            fcntl.flock(self._descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(self._descriptor)
            self._descriptor = None
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise SnapshotError(
                    "operation_in_progress",
                    "another snapshot operation holds this guest's lease",
                ) from exc
            raise
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._descriptor is not None:
            fcntl.flock(self._descriptor, fcntl.LOCK_UN)
            os.close(self._descriptor)
            self._descriptor = None


# ---------------------------------------------------------------------------
# Bounded PVE reads
# ---------------------------------------------------------------------------


def _command(
    runner: Runner, argv: tuple[str, ...], *, max_output: int = MAX_COMMAND_OUTPUT_BYTES
) -> CommandResult:
    result = runner(argv, COMMAND_TIMEOUT_SECONDS, max_output)
    if result.timed_out:
        raise SnapshotError("timeout", "PVE command timed out")
    if result.output_exceeded:
        raise SnapshotError("execution_failed", "PVE command output exceeded its bound")
    return result


def _json_command(
    runner: Runner, argv: tuple[str, ...], what: str, *, max_output: int
) -> Any:
    result = _command(runner, argv, max_output=max_output)
    if result.returncode != 0:
        raise SnapshotError("execution_failed", f"could not read {what}")
    try:
        return json.loads(result.stdout.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise SnapshotError("execution_failed", f"{what} was malformed") from exc


def revalidate_live_target(runner: Runner, vmid: int, expected_node: str) -> None:
    """Independently re-read live PVE state immediately before any mutation.

    The backend's `resource_id`, binding, and continuity revision are backend
    authority facts PVE itself does not know, so they stay context carried into
    ownership metadata. What PVE *can* prove is checked here, and a VMID alone
    is never permission.
    """

    rows = _json_command(
        runner,
        ("pvesh", "get", "/cluster/resources", "--type", "vm", "--output-format", "json"),
        "current PVE target state",
        max_output=4 * 1024 * 1024,
    )
    if not isinstance(rows, list):
        raise SnapshotError("execution_failed", "current PVE target state was malformed")
    matches = [row for row in rows if isinstance(row, Mapping) and row.get("vmid") == vmid]
    if len(matches) != 1:
        raise SnapshotError("guest_unavailable", "guest is missing or ambiguous")
    row = matches[0]
    if row.get("type") != "lxc":
        raise SnapshotError(
            "unsupported_resource_type", "current PVE resource is not an LXC guest"
        )
    if row.get("node") != expected_node:
        raise SnapshotError("stale_target", "guest node changed after job issuance")
    if row.get("status") not in ("running", "stopped"):
        raise SnapshotError("guest_unavailable", "guest status is not snapshot-ready")


def read_container_lock(
    runner: Runner, vmid: int, expected_node: str
) -> str | None:
    """Return the container's current PVE config lock, if any."""

    config = _json_command(
        runner,
        (
            "pvesh", "get", f"/nodes/{expected_node}/lxc/{vmid}/config",
            "--output-format", "json",
        ),
        "current PVE container configuration",
        max_output=1 * 1024 * 1024,
    )
    if not isinstance(config, Mapping):
        raise SnapshotError(
            "execution_failed", "current PVE container configuration was malformed"
        )
    lock = config.get("lock")
    if lock is None:
        return None
    if not isinstance(lock, str):
        raise SnapshotError(
            "execution_failed", "current PVE container lock was malformed"
        )
    return lock


def list_snapshots(
    runner: Runner, vmid: int, expected_node: str
) -> list[dict[str, Any]]:
    """Read the canonical snapshot listing for one LXC guest."""

    rows = _json_command(
        runner,
        (
            "pvesh", "get", f"/nodes/{expected_node}/lxc/{vmid}/snapshot",
            "--output-format", "json",
        ),
        "current PVE snapshot listing",
        max_output=2 * 1024 * 1024,
    )
    if not isinstance(rows, list) or len(rows) > 512:
        raise SnapshotError(
            "execution_failed", "current PVE snapshot listing was malformed"
        )
    listing: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping) or not isinstance(row.get("name"), str):
            raise SnapshotError(
                "execution_failed", "current PVE snapshot listing was malformed"
            )
        entry: dict[str, Any] = {
            "name": row["name"],
            "description": row.get("description") or "",
        }
        if isinstance(row.get("snaptime"), int):
            entry["snaptime"] = row["snaptime"]
        if isinstance(row.get("parent"), str):
            entry["parent"] = row["parent"]
        if isinstance(row.get("snapstate"), str) and row["snapstate"]:
            entry["snapstate"] = row["snapstate"]
        listing.append(entry)
    return listing


def read_task_status(
    runner: Runner, expected_node: str, upid: str
) -> dict[str, Any]:
    """Read one PVE task's bounded status."""

    if not UPID_RE.fullmatch(upid):
        raise SnapshotError("execution_failed", "PVE task identity was malformed")
    status = _json_command(
        runner,
        (
            "pvesh", "get", f"/nodes/{expected_node}/tasks/{upid}/status",
            "--output-format", "json",
        ),
        "PVE task status",
        max_output=256 * 1024,
    )
    if not isinstance(status, Mapping) or status.get("status") not in (
        "running",
        "stopped",
    ):
        raise SnapshotError("execution_failed", "PVE task status was malformed")
    result: dict[str, Any] = {"upid": upid, "status": status["status"]}
    exit_status = status.get("exitstatus")
    if isinstance(exit_status, str) and exit_status:
        result["exitstatus"] = exit_status[:200]
    return result


def _task_is_error(status: Mapping[str, Any]) -> bool:
    """Apply PVE's own `status_is_error` rule to a terminal task."""

    exit_status = status.get("exitstatus")
    if not isinstance(exit_status, str) or not exit_status:
        return True
    if exit_status == "OK":
        return False
    return not (
        exit_status.startswith("WARNINGS: ")
        and exit_status[len("WARNINGS: "):].isdigit()
    )


# ---------------------------------------------------------------------------
# Owned-snapshot evidence
# ---------------------------------------------------------------------------


def _owned_snapshot_evidence(
    listing: list[dict[str, Any]], request: Mapping[str, Any]
) -> str:
    """Classify canonical evidence for this job's snapshot.

    Returns ``present`` (exact complete snapshot with exact ownership),
    ``incomplete``, ``ambiguous``, or ``absent``. Anything Hubinet-looking
    that will not parse is ambiguous, never silently skipped.
    """

    expected_name = request["snapshot_name"]
    ownership = dict(request["ownership"])
    matches = 0
    for entry in listing:
        name = entry["name"]
        if name == "current":
            continue
        try:
            parsed = parse_snapshot_description(entry.get("description"))
        except RequestError:
            # A Hubinet-looking snapshot that will not parse cannot be
            # attributed, so it cannot be ruled out as a claim on this job.
            return "ambiguous"
        claims_job = parsed is not None and parsed["job_id"] == ownership["job_id"]
        if name != expected_name:
            if claims_job:
                return "ambiguous"
            continue
        if parsed is None:
            return "ambiguous"
        if entry.get("snapstate"):
            return "incomplete"
        if {field: parsed[field] for field in _OWNERSHIP_FIELDS} != ownership:
            return "ambiguous"
        matches += 1
    if matches == 1:
        return "present"
    if matches > 1:
        return "ambiguous"
    return "absent"


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


def _response(
    request: Mapping[str, Any],
    outcome: str,
    *,
    task_upid: str | None = None,
    task: Mapping[str, Any] | None = None,
    snapshots: list[dict[str, Any]] | None = None,
    reason: str | None = None,
    submission_state: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "response_version": 1,
        "ok": True,
        "operation": request["operation"],
        "snapshot_operation_id": request["snapshot_operation_id"],
        "outcome": outcome,
    }
    if task_upid is not None:
        payload["task_upid"] = task_upid
    if task is not None:
        payload["task"] = dict(task)
    if snapshots is not None:
        payload["snapshots"] = snapshots
    if reason is not None:
        payload["reason"] = reason[:500]
    if submission_state is not None:
        payload["submission_state"] = submission_state
    return payload


def _inspect(
    runner: Runner, request: Mapping[str, Any], journal: OperationJournal
) -> dict[str, Any]:
    """Read-only: current canonical state plus the journaled submission phase.

    Never mutates the journal and never submits anything. A single bounded
    read of the journaled task (if any) plus a fresh canonical listing is the
    whole of it -- there is no poll-to-completion loop here, so a caller may
    call this as often as it likes, including as its own bounded retry loop,
    without ever holding anything open on either side.
    """

    record = journal.read(request["snapshot_operation_id"])
    if record is not None and record["request_fingerprint"] != request_fingerprint(
        request
    ):
        raise SnapshotError(
            "operation_request_mismatch",
            "this operation identity was journaled with a different request",
        )
    listing = list_snapshots(runner, request["vmid"], request["expected_node"])
    task_upid = record.get("task_upid") if record else None
    task = None
    if isinstance(task_upid, str):
        task = read_task_status(runner, request["expected_node"], task_upid)
    evidence = _owned_snapshot_evidence(listing, request)
    if task is not None and task["status"] == "stopped" and _task_is_error(task):
        # The task PVE ran for this operation terminated in a failure state.
        # This is reported regardless of canonical evidence -- the backend
        # independently re-proves canonical absence before it may ever
        # terminalize a job on this outcome, exactly as it always has.
        outcome = "failed"
    else:
        outcome = {
            "present": "completed",
            "absent": "absent",
            "incomplete": "uncertain",
            "ambiguous": "uncertain",
        }[evidence]
    submission_state = record["phase"] if record is not None else "absent"
    return _response(
        request,
        outcome,
        task_upid=task_upid if isinstance(task_upid, str) else None,
        task=task,
        snapshots=listing,
        reason=f"canonical job-owned snapshot evidence: {evidence}",
        submission_state=submission_state,
    )


def _finalize(
    runner: Runner,
    request: Mapping[str, Any],
    journal: OperationJournal,
    record: dict[str, Any],
    outcome: str,
    reason: str,
    task: Mapping[str, Any] | None,
) -> dict[str, Any]:
    listing = list_snapshots(runner, request["vmid"], request["expected_node"])
    if outcome in ("completed", "failed"):
        record = {
            **record,
            "phase": "terminal",
            "outcome": outcome,
            "reason": reason[:500],
        }
        journal.write(record)
    return _response(
        request,
        outcome,
        task_upid=record.get("task_upid"),
        task=task,
        snapshots=listing,
        reason=reason,
        submission_state="terminal",
    )


def _ensure_submitted(
    runner: Runner, request: Mapping[str, Any], journal: OperationJournal
) -> dict[str, Any]:
    fingerprint = request_fingerprint(request)
    with VmidMutationLock(request["vmid"], journal.directory):
        record = journal.read(request["snapshot_operation_id"])
        if record is not None:
            if record["request_fingerprint"] != fingerprint:
                raise SnapshotError(
                    "operation_request_mismatch",
                    "this operation identity was journaled with a different request",
                )
            phase = record["phase"]
            if phase == "terminal":
                # Replay the recorded answer with fresh canonical evidence.
                return _finalize(
                    runner, request, journal, record,
                    str(record.get("outcome", "uncertain")),
                    str(record.get("reason", "replayed journaled outcome")),
                    None,
                )
            if phase == "task_known":
                # NEVER submit and NEVER poll here: the task is already
                # journaled, so the caller observes its completion through
                # the read-only inspect operation's own bounded retries,
                # never by this mutating one blocking internally.
                return _response(
                    request,
                    "uncertain",
                    task_upid=str(record["task_upid"]),
                    submission_state="task_known",
                    reason=(
                        "snapshot submission already recorded; inspect the "
                        "operation to observe task completion"
                    ),
                )
            if phase == "submitted":
                # The genuinely uncertain window. NEVER resubmit here.
                return _recover_submitted_without_task(
                    runner, request, journal, record
                )
            if phase != "intent":
                # Exhaustive by construction: a phase this build does not
                # understand must never fall through into a submission.
                raise SnapshotError(
                    "journal_corrupt",
                    "snapshot operation journal is in an unrecognized phase",
                )
            # phase == "intent": the atomic rename to "submitted" never
            # completed, so submission was provably never attempted.
        else:
            record = {
                "journal_version": 1,
                "snapshot_operation_id": request["snapshot_operation_id"],
                "request_fingerprint": fingerprint,
                "vmid": request["vmid"],
                "expected_node": request["expected_node"],
                "snapshot_name": request["snapshot_name"],
                "phase": "intent",
            }
            journal.write(record)

        # ---------------------------------------------------------------
        # PRE-SUBMISSION WINDOW
        #
        # The durable journal for this operation is at `intent`, and the
        # `intent -> submitted` transition below is an fsynced atomic rename
        # performed strictly BEFORE the submission subprocess is launched.
        # So for the whole of this window this host can prove that no
        # snapshot mutation has ever been submitted for this operation
        # identity, and any failure raised here is reported as such rather
        # than as unresolvable uncertainty.
        #
        # That proof is what lets the backend safely terminalize a job which
        # is already past its write-ahead uncertainty checkpoint. Without it
        # an ordinary pre-flight refusal (a guest that moved node, say) would
        # fence the single global destructive slot forever.
        # ---------------------------------------------------------------
        if record["phase"] != "intent":
            raise SnapshotError(
                "journal_corrupt",
                "snapshot operation left its pre-submission phase unexpectedly",
            )
        try:
            # Live target revalidation immediately before the mutation.
            revalidate_live_target(runner, request["vmid"], request["expected_node"])
            lock = read_container_lock(
                runner, request["vmid"], request["expected_node"]
            )
            # Canonical pre-submission read. Defence in depth on top of the
            # journal: it lets an operation whose result already exists be
            # recognised without issuing any mutation request at all.
            listing = (
                None
                if lock in _IN_FLIGHT_LOCKS
                else list_snapshots(
                    runner, request["vmid"], request["expected_node"]
                )
            )
        except SnapshotError as exc:
            raise SnapshotError(
                exc.classification,
                exc.message,
                submission=SUBMISSION_NOT_SUBMITTED,
            ) from exc

        if lock in _IN_FLIGHT_LOCKS:
            # This operation has not been submitted, but *something* is
            # mutating this guest right now. Refusing is not the same as
            # proving nothing happened, so this stays uncertain.
            return _response(
                request,
                "uncertain",
                reason=f"guest is locked by an in-flight PVE operation ({lock})",
                submission_state="intent",
            )

        assert listing is not None
        evidence = _owned_snapshot_evidence(listing, request)
        if evidence == "present":
            journal.write(
                {
                    **record,
                    "phase": "terminal",
                    "outcome": "completed",
                    "reason": "canonical job-owned snapshot already present",
                }
            )
            return _response(
                request,
                "completed",
                snapshots=listing,
                reason="canonical job-owned snapshot already present",
                submission_state="terminal",
            )
        if evidence != "absent":
            return _response(
                request,
                "uncertain",
                snapshots=listing,
                reason=(
                    "canonical state is not a clean absence for this operation "
                    f"({evidence}); refusing to submit"
                ),
                submission_state="intent",
            )

        record = {**record, "phase": "submitted"}
        journal.write(record)

        description = build_snapshot_description(request["ownership"])
        result = _command(
            runner,
            (
                "pvesh", "create",
                f"/nodes/{request['expected_node']}/lxc/{request['vmid']}/snapshot",
                "--snapname", request["snapshot_name"],
                "--description", description,
                "--output-format", "json",
            ),
            max_output=256 * 1024,
        )
        upid = _extract_upid(result)
        if upid is None:
            return _response(
                request,
                "uncertain",
                reason=(
                    "PVE snapshot submission returned no usable task identity; "
                    "the operation may or may not have started"
                ),
                submission_state="submitted",
            )
        # The task is now durably journaled. Return immediately: polling it
        # to completion is the read-only operation's job, never this one's.
        record = {**record, "phase": "task_known", "task_upid": upid}
        journal.write(record)
        return _response(
            request,
            "uncertain",
            task_upid=upid,
            submission_state="task_known",
            reason=(
                "snapshot submission recorded; inspect the operation to "
                "observe task completion"
            ),
        )


def _extract_upid(result: CommandResult) -> str | None:
    if result.returncode != 0:
        return None
    try:
        text = result.stdout.decode("utf-8").strip()
    except UnicodeDecodeError:
        return None
    if not text:
        return None
    try:
        decoded = json.loads(text)
    except ValueError:
        decoded = text
    if not isinstance(decoded, str) or not UPID_RE.fullmatch(decoded):
        return None
    return decoded


def _recover_submitted_without_task(
    runner: Runner,
    request: Mapping[str, Any],
    journal: OperationJournal,
    record: dict[str, Any],
) -> dict[str, Any]:
    """Decide a submitted-but-untracked operation without ever resubmitting.

    Recovery to success needs strict evidence that the operation is over and
    that its exact result is present: the guest carries no in-flight lock, and
    the canonical listing holds exactly this job's complete snapshot with
    exactly its ownership metadata. Anything else is UNCERTAIN.
    """

    lock = read_container_lock(runner, request["vmid"], request["expected_node"])
    listing = list_snapshots(runner, request["vmid"], request["expected_node"])
    evidence = _owned_snapshot_evidence(listing, request)
    if lock in _IN_FLIGHT_LOCKS:
        return _response(
            request,
            "uncertain",
            snapshots=listing,
            reason=(
                "a snapshot request was submitted for this operation and the "
                f"guest is still locked ({lock})"
            ),
            submission_state="submitted",
        )
    if evidence == "present":
        journal.write(
            {
                **record,
                "phase": "terminal",
                "outcome": "completed",
                "reason": "recovered: canonical job-owned snapshot present",
            }
        )
        return _response(
            request,
            "completed",
            snapshots=listing,
            reason="recovered: canonical job-owned snapshot present",
            submission_state="terminal",
        )
    return _response(
        request,
        "uncertain",
        snapshots=listing,
        reason=(
            "a snapshot request was submitted for this operation but its task "
            f"identity was never recorded (canonical evidence: {evidence})"
        ),
        submission_state="submitted",
    )


def handle_request(
    payload: Any,
    *,
    runner: Runner = _run_bounded,
    journal: OperationJournal | None = None,
) -> dict[str, Any]:
    request = validate_request(payload)
    operation_journal = journal or OperationJournal()
    try:
        if request["operation"] == "inspect_job_snapshot_state":
            return _inspect(runner, request, operation_journal)
        return _ensure_submitted(runner, request, operation_journal)
    except SnapshotError as exc:
        return {
            "response_version": 1,
            "ok": False,
            "operation": request["operation"],
            "snapshot_operation_id": request["snapshot_operation_id"],
            "error": {
                "classification": exc.classification,
                "message": exc.message[:500],
                "submission": exc.submission,
            },
        }


def main() -> int:
    if os.environ.get("SSH_ORIGINAL_COMMAND"):
        response = {
            "response_version": 1,
            "ok": False,
            "operation": None,
            "snapshot_operation_id": None,
            "error": {
                "classification": "execution_failed",
                "message": "remote command text is not accepted",
            },
        }
        sys.stdout.write(json.dumps(response, separators=(",", ":")))
        return 2
    raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    if len(raw) > MAX_REQUEST_BYTES:
        error = "request exceeded its structural bound"
    else:
        try:
            payload = json.loads(raw.decode("utf-8"))
            response = handle_request(payload)
            sys.stdout.write(
                json.dumps(response, ensure_ascii=True, separators=(",", ":"))
            )
            return 0 if response.get("ok") is True else 1
        except (UnicodeDecodeError, ValueError, RequestError) as exc:
            error = str(exc)[:500] or "malformed snapshot-operation request"
    response = {
        "response_version": 1,
        "ok": False,
        "operation": None,
        "snapshot_operation_id": None,
        "error": {"classification": "execution_failed", "message": error},
    }
    sys.stdout.write(json.dumps(response, ensure_ascii=True, separators=(",", ":")))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
