#!/usr/bin/env python3
"""Forced-command PVE boundary for Hubinet's same-job snapshot rollback.

**Not deployed.** Neither `deploy/bootstrap-proxmox-0.5.sh` nor
`deploy/update-proxmox-0.5.sh` installs this file, its forced-command
`authorized_keys` entry, or any key for it, and neither of the two PVE
snapshot privileges upstream accepts for the rollback endpoint is provisioned
anywhere. Production provisions exactly the audit-only pair (`Sys.Audit` plus
the VM audit privilege) and nothing else; the exact privilege tokens are
deliberately not spelled anywhere under `deploy/`, and
`tests/test_r0_architecture_regression.py` enforces that.

It is a separate file and a separate logical privilege boundary from the scan,
snapshot, and mutation helpers. **Combining rollback into the snapshot helper
was considered and rejected**: snapshot create adds a recovery point and is
safe to attempt on a healthy guest, while rollback force-stops a running
container and replaces its volumes and config. Keeping them apart means the
deployed forced-command entry that may one day create snapshots does not also
carry the capability to destroy the current state of a guest, and each
boundary's `authorized_keys` entry says exactly what it can do. The snapshot
helper therefore keeps NO rollback operation, and this file has NO create and
NO delete.

Scope is deliberately minimal:

- LXC only.
- Exactly three typed operations: `inspect_rollback_state`,
  `submit_same_job_rollback`, and the narrow `seal_rollback_never_submitted`
  fence.
- No arbitrary shell, no command string, no caller-supplied argv, no generic
  action dispatcher. Every `pvesh` argv is built from this file's own fixed
  constants plus validated typed fields.
- **No snapshot create, no snapshot delete, no lifecycle control.**
- `start` is pinned to 0 by this file, never by the request. A successful
  rollback always leaves the guest stopped.
- The final preflight proves the target snapshot's strict structured
  OWNERSHIP metadata from a fresh listing, never merely its name. A snapshot
  name is a physical PVE key; the description metadata is the ownership
  proof. Authority proves this when it arms the rollback, but PVE state can
  change afterwards, and this file runs closest in time to the real `pvesh`
  call.

`submit_same_job_rollback` is submission-only: it never polls the PVE task to
completion. It journals the submission, invokes `pvesh create` at most once,
records the task identity the instant PVE returns one, and returns. The
backend holds its own authority-store writer transaction across this call (see
`InventoryAuthority.execute_rollback_submission_if_current`, and the sized
wait policy in `app/inventory/contention_policy.py`), so this call must stay
bounded. `inspect_rollback_state` reads the journaled task's status once,
synchronously, alongside a fresh canonical listing, so completion is observed
by the caller polling that cheap read operation, never by the mutating one
blocking internally.

`inspect_rollback_state` is read-only with respect to PVE and the journal, but
it is serialized against the SAME per-VMID lease the mutating operations use,
acquired non-blocking and released before it returns. Without joining that
lease a concurrent inspection could read `intent` while a live submitter was
about to advance past it and hand the caller stale routing evidence. If the
lease is already held this reports `operation_in_progress` -- never inferred
as `not_submitted`, `absent`, or `intent`.

## Durable operation journal

The backend receiving a UPID is not something this helper may rely on: the
caller can die between PVE accepting the rollback and the answer being
recorded anywhere durable. So each rollback is journaled here, on the PVE
host, keyed by the operation identity the backend derives deterministically
from immutable job identity plus that job's own confirmed snapshot.

```text
intent                request recorded; submission NOT attempted -> may submit
sealed_not_submitted  durable no-future-submit fence             -> NEVER submit
submitted             submission attempt has begun               -> NEVER resubmit
task_known            PVE returned a UPID                        -> caller polls it
terminal              outcome recorded                           -> replay answer
```

Every successful (`ok: true`) response reports this exact phase as a typed
`rollback_state` field, read straight from the journal rather than inferred
from canonical PVE state or an error string. `absent` and `intent` permit a
NEW submission but never release a backend job. Only `sealed_not_submitted` is
a durable release proof; it is written under the same per-VMID lease as
submission, and every delayed helper must obey it.

`intent -> submitted` is an atomic rename fsynced before `pvesh create` runs,
so an observation of `intent` under the lease is transient pre-submission
routing evidence, not a no-future-submit proof.

**`submitted` without a UPID never recovers into success here.** This is a
deliberate difference from the snapshot helper. A snapshot's existence, with
this job's exact ownership metadata, is unique canonical proof that THAT
operation completed. Rollback has no such witness: the source snapshot
survives a rollback either way, and `parent == snapname` is equally true after
any earlier rollback to the same snapshot. So without the exact task identity
there is nothing that distinguishes "this rollback completed" from "some
rollback completed once"; the answer is `uncertain`, and the backend keeps the
job fenced.

Every failure answer carries a typed `error.submission`:

- `not_submitted` -- transient routing evidence when a journal read under the
  lease is still `absent`/`intent`, or the final durable answer when it is
  `sealed_not_submitted`.
- `may_have_been_submitted` -- the default for everything else, including an
  unreadable or corrupt journal, a lease held by another invocation, and every
  failure at or after the `submitted` transition.

Destruction of this journal by something outside Hubinet is out of the
product's threat model (see `AGENTS.md`).

## Verified PVE semantics this file depends on

- `POST /nodes/{node}/lxc/{vmid}/snapshot/{snapname}/rollback` is
  `protected => 1`, `proxyto => 'node'`, takes an optional `start` boolean
  (default 0), and returns the task id; the worker is
  `fork_worker('vzrollback', ...)`, so `pvesh create` prints a UPID rather
  than waiting.
- `PVE::AbstractConfig::snapshot_rollback` refuses a template, a missing
  snapshot, a snapshot still carrying `snapstate`, a config under another
  lock, and a container still running after its own forced stop; it holds the
  config lock as `rollback`, replaces the config from the snapshot, and sets
  `parent` to the snapshot name.
- `PVE::LXC::Config::__snapshot_rollback_vm_stop` is
  `PVE::LXC::vm_stop($vmid, 1)` -- a forced stop -- so a running guest is
  stopped as part of the rollback.
- `PVE::AbstractConfig::check_lock` dies whenever `$conf->{lock}` is truthy.
  It does not accept `backup`, `migrate`, or any other lock type, so ANY
  non-empty config lock is refused here before the `submitted` boundary
  rather than after it.
- a snapshot description carries this product's strict structured ownership
  metadata, and PVE's LXC config parser appends a newline to every
  description line it reads back, so the marker is parsed with normalised
  line framing.
- `GET /nodes/{node}/tasks/{upid}/status` gives `status` in
  `running`/`stopped` plus an optional `exitstatus`; PVE's own rule treats
  `OK` and `WARNINGS: <n>` as non-errors.
- `GET /nodes/{node}/lxc/{vmid}/snapshot` includes PVE's synthetic `current`
  pseudo-entry (carrying `parent`) and carries `snapstate` for unfinished
  snapshots.
- `pvesh` resolves the endpoint's own `proxyto => node` over PVE's existing
  root SSH trust, so no per-node Hubinet credential is needed here.
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

JOURNAL_DIRECTORY = Path("/var/lib/hubinet-ops/rollback-operations")

#: The smallest directory known-durable independent of any concurrent Hubinet
#: activity: `/var/lib`, a standard FHS location from the base OS install. No
#: concurrent-first-use durability barrier is needed for it -- only for the
#: Hubinet-owned levels strictly below it. See `_ensure_durable_directory`.
JOURNAL_DURABILITY_ANCHOR = JOURNAL_DIRECTORY.parent.parent

NODE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,62}")
#: PVE `pve-configid`, which `pve-snapshot-name` builds on, plus maxLength 40.
SNAPSHOT_NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{1,39}")
PVE_RESERVED_SNAPSHOT_NAMES = frozenset({"current", "vzdump"})
UPID_RE = re.compile(
    r"UPID:(?:[a-zA-Z0-9](?:[a-zA-Z0-9\-]*[a-zA-Z0-9])?)"
    r":[0-9A-Fa-f]{8}:[0-9A-Fa-f]{8,9}:[0-9A-Fa-f]{8}"
    r":[^:\s/]+:[^:\s/]*:[^:\s/]+:"
)

#: The snapshot ownership protocol, identical to the one the snapshot helper
#: writes and `app/inventory/snapshot_identity.py` defines. This file parses
#: it independently because each dark helper is standalone on the PVE host
#: and imports nothing from the backend; the FORMAT is shared, not invented
#: a second time.
SNAPSHOT_METADATA_PROTOCOL = 1
SNAPSHOT_METADATA_MARKER = "hubinet-ops-snapshot-v1"
#: Any description containing this token CLAIMS to be Hubinet's. One that
#: claims it but will not parse strictly is reported as malformed and fails
#: closed, so ownership can never be silently ambiguous.
SNAPSHOT_METADATA_TOKEN = "hubinet-ops-snapshot"
SNAPSHOT_KIND_PRE_UPDATE = "pre_update"
_OWNERSHIP_FIELDS = (
    "job_id",
    "resource_id",
    "resource_continuity_revision",
    "inventory_source_id",
    "backend_instance_id",
)

#: PVE's `start` parameter for the rollback endpoint, pinned here as this
#: file's own constant. It is deliberately NOT reachable from the request:
#: rollback, restart, and health validation are three separate destructive
#: concerns, and this stage ships only the first. A successful rollback
#: therefore always leaves the guest stopped.
ROLLBACK_START_AFTER = 0

OPERATIONS = (
    "inspect_rollback_state",
    "submit_same_job_rollback",
    "seal_rollback_never_submitted",
)

JOURNAL_PHASES = (
    "intent",
    "sealed_not_submitted",
    "submitted",
    "task_known",
    "terminal",
)

#: Journal phases from which a NEW submission is still possible.
_PRE_SUBMISSION_PHASES = ("intent",)


class RollbackError(Exception):
    """A typed, classified rollback-boundary failure."""

    def __init__(
        self,
        classification: str,
        message: str,
        *,
        submission: str = "may_have_been_submitted",
    ) -> None:
        super().__init__(message)
        self.classification = classification
        self.submission = submission


class RequestError(ValueError):
    """The request itself is not a well-formed rollback request."""


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    timed_out: bool
    output_exceeded: bool


Runner = Callable[..., CommandResult]


def _run_bounded(
    argv: tuple[str, ...], timeout: float, max_output: int
) -> CommandResult:
    """Run one fixed argv under a single wall-clock deadline.

    Deliberately no stdin: every operation this helper runs takes its whole
    input from its own validated argv, so there is no payload to deliver and
    no blocking `stdin.write()` path to get wrong.
    """

    started = time.monotonic()
    process = subprocess.Popen(  # noqa: S603 - every argv shape is fixed above
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
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        for stream in (process.stdout, process.stderr):
            try:
                stream.close()
            except OSError:
                pass
    return CommandResult(
        returncode=process.returncode if process.returncode is not None else -1,
        stdout=bytes(output["stdout"]),
        stderr=bytes(output["stderr"]),
        timed_out=timed_out,
        output_exceeded=exceeded,
    )


# ---------------------------------------------------------------------------
# Strict request validation
# ---------------------------------------------------------------------------


def _canonical_json(payload: Any) -> str:
    return json.dumps(
        payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    )


def _canonical_uuid(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise RequestError(f"{field} must be a canonical UUID")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise RequestError(f"{field} must be a canonical UUID") from exc
    if parsed.int == 0 or str(parsed) != value:
        raise RequestError(f"{field} must be a canonical non-NIL UUID")
    return value


def _positive_integer(value: Any, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise RequestError(f"{field} must be a positive integer")
    return value


def parse_snapshot_ownership(payload: Any, what: str) -> dict[str, Any]:
    """Strictly validate one structured snapshot-ownership record.

    Shared by the request validator and the PVE description parser, so the
    request's expected ownership and the ownership actually observed on the
    guest are held to exactly the same grammar. Anything else fails closed.
    """

    expected = {"protocol", "kind", *_OWNERSHIP_FIELDS}
    if not isinstance(payload, dict) or set(payload) != expected:
        raise RequestError(f"{what} does not have the exact expected shape")
    if payload["protocol"] != SNAPSHOT_METADATA_PROTOCOL:
        raise RequestError(f"{what} protocol is unsupported")
    if payload["kind"] != SNAPSHOT_KIND_PRE_UPDATE:
        raise RequestError(f"{what} kind is not pre-update")
    for field in (
        "job_id",
        "resource_id",
        "inventory_source_id",
        "backend_instance_id",
    ):
        _canonical_uuid(payload[field], field)
    _positive_integer(
        payload["resource_continuity_revision"], "resource_continuity_revision"
    )
    return {
        "protocol": SNAPSHOT_METADATA_PROTOCOL,
        "kind": SNAPSHOT_KIND_PRE_UPDATE,
        **{field: payload[field] for field in _OWNERSHIP_FIELDS},
    }


def parse_snapshot_description(description: Any) -> dict[str, Any] | None:
    """Strictly parse Hubinet ownership metadata out of a PVE description.

    Returns ``None`` for a description that makes no Hubinet claim at all -- a
    foreign or manual snapshot. Raises :class:`RequestError` when it *does*
    look like a Hubinet snapshot but does not parse into exactly one
    well-formed claim, so a caller fails closed rather than silently skipping
    it.

    PVE's LXC config parser appends a newline to every description line it
    reads back, so a description never round-trips byte-identically; this
    normalises line framing before applying its strict checks, exactly as the
    snapshot helper and `app/inventory/snapshot_identity.py` do.
    """

    if (
        not isinstance(description, str)
        or SNAPSHOT_METADATA_TOKEN not in description
    ):
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
    return parse_snapshot_ownership(payload, "snapshot metadata")


def parse_request(payload: Any) -> dict[str, Any]:
    """Validate one typed rollback request strictly, or refuse it."""

    if not isinstance(payload, dict):
        raise RequestError("request must be a JSON object")
    if set(payload) != {
        "request_version",
        "operation",
        "target",
        "operation_identity",
        "ownership",
        "expected_snapshot_ownership",
    }:
        raise RequestError("request does not have the exact expected shape")
    if payload["request_version"] != 1:
        raise RequestError("unsupported request version")
    operation = payload["operation"]
    if operation not in OPERATIONS:
        raise RequestError("unsupported operation")

    target = payload["target"]
    if not isinstance(target, dict) or set(target) != {"vmid", "expected_node"}:
        raise RequestError("target does not have the exact expected shape")
    vmid = _positive_integer(target["vmid"], "vmid")
    if not 100 <= vmid <= 999_999_999:
        raise RequestError("vmid is outside the supported PVE range")
    expected_node = target["expected_node"]
    if not isinstance(expected_node, str) or not NODE_RE.fullmatch(expected_node):
        raise RequestError("expected_node is invalid")

    identity = payload["operation_identity"]
    if not isinstance(identity, dict) or set(identity) != {
        "rollback_operation_id",
        "snapshot_name",
        "snapshot_operation_id",
    }:
        raise RequestError("operation_identity does not have the exact expected shape")
    rollback_operation_id = _canonical_uuid(
        identity["rollback_operation_id"], "rollback_operation_id"
    )
    snapshot_operation_id = _canonical_uuid(
        identity["snapshot_operation_id"], "snapshot_operation_id"
    )
    snapshot_name = identity["snapshot_name"]
    if not isinstance(snapshot_name, str) or not SNAPSHOT_NAME_RE.fullmatch(
        snapshot_name
    ):
        raise RequestError("snapshot_name is not a valid PVE snapshot name")
    if snapshot_name.lower() in PVE_RESERVED_SNAPSHOT_NAMES:
        # PVE would refuse these itself, but refusing here means `current` can
        # never even be formed into a rollback request.
        raise RequestError("snapshot_name is a PVE-reserved name")

    ownership = payload["ownership"]
    if not isinstance(ownership, dict) or set(ownership) != {
        "job_id",
        "resource_id",
        "resource_continuity_revision",
        "binding_id",
        "locator_generation",
        "backend_instance_id",
    }:
        raise RequestError("ownership does not have the exact expected shape")
    parsed_ownership = {
        "job_id": _canonical_uuid(ownership["job_id"], "job_id"),
        "resource_id": _canonical_uuid(ownership["resource_id"], "resource_id"),
        "resource_continuity_revision": _positive_integer(
            ownership["resource_continuity_revision"], "resource_continuity_revision"
        ),
        "binding_id": _canonical_uuid(ownership["binding_id"], "binding_id"),
        "locator_generation": _positive_integer(
            ownership["locator_generation"], "locator_generation"
        ),
        "backend_instance_id": _canonical_uuid(
            ownership["backend_instance_id"], "backend_instance_id"
        ),
    }
    expected_snapshot_ownership = parse_snapshot_ownership(
        payload["expected_snapshot_ownership"], "expected_snapshot_ownership"
    )
    # The expected ownership must belong to the SAME job and resource
    # incarnation the operation itself does. A request that expects some
    # other job's snapshot metadata is incoherent on its face and is refused
    # before any PVE state is read.
    if (
        expected_snapshot_ownership["job_id"] != parsed_ownership["job_id"]
        or expected_snapshot_ownership["resource_id"]
        != parsed_ownership["resource_id"]
        or expected_snapshot_ownership["resource_continuity_revision"]
        != parsed_ownership["resource_continuity_revision"]
        or expected_snapshot_ownership["backend_instance_id"]
        != parsed_ownership["backend_instance_id"]
    ):
        raise RequestError(
            "expected_snapshot_ownership does not describe this operation's "
            "own job and resource incarnation"
        )
    return {
        "operation": operation,
        "vmid": vmid,
        "expected_node": expected_node,
        "rollback_operation_id": rollback_operation_id,
        "snapshot_operation_id": snapshot_operation_id,
        "snapshot_name": snapshot_name,
        "ownership": parsed_ownership,
        "expected_snapshot_ownership": expected_snapshot_ownership,
    }


def request_fingerprint(request: Mapping[str, Any]) -> str:
    """Bind every material fact of one rollback request into one digest.

    A request differing in ANY of these is a DIFFERENT request and may never
    reuse an existing operation journal, however similar it looks.
    """

    return hashlib.sha256(
        _canonical_json(
            {
                "rollback_operation_id": request["rollback_operation_id"],
                "snapshot_operation_id": request["snapshot_operation_id"],
                "snapshot_name": request["snapshot_name"],
                "vmid": request["vmid"],
                "expected_node": request["expected_node"],
                "ownership": dict(request["ownership"]),
                # Bound in, so an operation journaled against one expected
                # snapshot ownership can never be reused by a request that
                # expects different ownership.
                "expected_snapshot_ownership": dict(
                    request["expected_snapshot_ownership"]
                ),
                "start": ROLLBACK_START_AFTER,
            }
        ).encode("ascii")
    ).hexdigest()


# ---------------------------------------------------------------------------
# Durable journal
# ---------------------------------------------------------------------------


def _write_all(descriptor: int, payload: bytes) -> None:
    """Write every byte of `payload` to `descriptor`, or raise.

    `os.write` may legally write fewer bytes than it was given without that
    being an error. Treating a short write as complete would let the journal
    fsync and rename a truncated JSON payload into place as the durable
    record -- exactly the corruption crash recovery depends on NOT existing.
    """

    view = memoryview(payload)
    offset = 0
    total = len(view)
    while offset < total:
        try:
            written = os.write(descriptor, view[offset:])
        except InterruptedError:
            continue
        if written <= 0:
            raise OSError("journal write made no progress")
        offset += written


def _ensure_durable_directory(
    path: Path, *, mode: int, anchor: Path = JOURNAL_DURABILITY_ANCHOR
) -> None:
    """Create `path`, and any missing parents down to `anchor`, as a
    crash-durable link, proving every level's own barrier on EVERY call.

    `Path.mkdir(parents=True, exist_ok=True)` alone is not enough on this
    host's very first rollback operation, when `JOURNAL_DIRECTORY` (and
    possibly its parent `/var/lib/hubinet-ops`) does not exist yet. `fsync` on
    a directory only makes ENTRIES INSIDE it durable; it says nothing about
    that directory's OWN entry in ITS parent. Without this, the sequence
    "create journal dir -> write+fsync journal record -> rename -> fsync
    journal dir -> submitted -> pvesh rollback -> host power loss" can lose
    the newly-created directory entry itself on reboot, so recovery sees the
    journal directory as absent and seals an operation that may already have
    force-stopped a container and replaced its volumes.

    `is_dir()` being true is NOT proof a level is durable: it is equally true
    the instant after a CONCURRENT first-use caller (a different VMID racing
    this same host's very first two rollback operations) creates it and before
    that caller has performed its own parent-fsync barrier. So every level
    strictly below `anchor` gets its own `mkdir` attempt (tolerating
    `FileExistsError` -- an ordinary race, not an error) followed by an
    unconditional fsync of ITS parent, on every call, regardless of whether
    that level already existed when this call started. `anchor` itself is
    never opened, created, or fsynced.

    `path` is always one of this module's own code-owned absolute literals,
    never request- or caller-controlled, so there is no symlink or path
    traversal surface to defend against here.
    """

    if path != anchor and anchor not in path.parents:
        raise ValueError(f"{path} is not below the trusted anchor {anchor}")

    levels: list[Path] = []
    probe = path
    while probe != anchor:
        levels.append(probe)
        probe = probe.parent
    for directory in reversed(levels):
        try:
            os.mkdir(directory, mode)
        except FileExistsError:
            pass
        parent_descriptor = os.open(directory.parent, os.O_RDONLY)
        try:
            os.fsync(parent_descriptor)
        finally:
            os.close(parent_descriptor)


class OperationJournal:
    """Atomic, fsynced, per-operation rollback journal on the PVE host."""

    def __init__(
        self,
        directory: Path = JOURNAL_DIRECTORY,
        *,
        anchor: Path = JOURNAL_DURABILITY_ANCHOR,
    ) -> None:
        self._directory = Path(directory)
        self._anchor = Path(anchor)

    @property
    def directory(self) -> Path:
        return self._directory

    @property
    def anchor(self) -> Path:
        return self._anchor

    def _path(self, rollback_operation_id: str) -> Path:
        # The id is a validated canonical UUID, so this never escapes.
        return self._directory / f"op-{rollback_operation_id}.json"

    def ensure_directory(self) -> None:
        """Create the journal directory durably.

        Shares its primitive with `VmidRollbackLease.__enter__`, which may
        create this same directory first: whichever runs first still pays for
        its OWN durability barrier, because an already-existing directory is
        not proof the other caller's barrier has completed yet.
        """

        _ensure_durable_directory(self._directory, mode=0o700, anchor=self._anchor)

    def read(self, rollback_operation_id: str) -> dict[str, Any] | None:
        path = self._path(rollback_operation_id)
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise RollbackError(
                "journal_unreadable", "rollback operation journal is unreadable"
            ) from exc
        try:
            record = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise RollbackError(
                "journal_corrupt", "rollback operation journal is corrupt"
            ) from exc
        if (
            not isinstance(record, dict)
            or record.get("journal_version") != 1
            or record.get("rollback_operation_id") != rollback_operation_id
            or record.get("phase") not in JOURNAL_PHASES
            or not isinstance(record.get("request_fingerprint"), str)
            or type(record.get("vmid")) is not int
        ):
            raise RollbackError(
                "journal_corrupt", "rollback operation journal is corrupt"
            )
        phase = record["phase"]
        # A phase is only usable with exactly the facts that phase promises.
        if phase in ("intent", "sealed_not_submitted", "submitted"):
            if (
                record.get("task_upid") is not None
                or record.get("outcome") is not None
            ):
                raise RollbackError(
                    "journal_corrupt",
                    "rollback operation journal phase carries incompatible evidence",
                )
        if phase in ("task_known", "terminal") and not (
            isinstance(record.get("task_upid"), str)
            and UPID_RE.fullmatch(record["task_upid"])
        ):
            raise RollbackError(
                "journal_corrupt",
                "rollback operation journal records a task without its identity",
            )
        if phase == "terminal" and record.get("outcome") not in (
            "completed",
            "failed",
        ):
            raise RollbackError(
                "journal_corrupt",
                "rollback operation journal records a terminal phase with an "
                "outcome this writer contract never produces",
            )
        return record

    def write(self, record: Mapping[str, Any]) -> None:
        """Atomically replace the journal and fsync data, entry, and directory."""

        self.ensure_directory()
        path = self._path(str(record["rollback_operation_id"]))
        temporary = path.with_name(path.name + ".tmp")
        payload = _canonical_json(dict(record)).encode("utf-8")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            try:
                _write_all(descriptor, payload)
                os.fsync(descriptor)
            except BaseException:
                # A short or failed write must never become the durable
                # record: no rename, and the incomplete temp file is removed.
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
                raise
        finally:
            os.close(descriptor)
        os.replace(temporary, path)
        directory_descriptor = os.open(self._directory, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)


class VmidRollbackLease:
    """Kernel `flock` serializing rollback operations per VMID.

    Non-blocking on purpose: a lease held by someone else is EVIDENCE (an
    operation is in flight), never something to wait behind while a backend
    holds its own writer lock.
    """

    def __init__(
        self,
        vmid: int,
        directory: Path = JOURNAL_DIRECTORY,
        *,
        anchor: Path = JOURNAL_DURABILITY_ANCHOR,
    ) -> None:
        self._path = Path(directory) / f"vmid-{int(vmid)}.lock"
        self._anchor = Path(anchor)
        self._descriptor: int | None = None

    def __enter__(self) -> VmidRollbackLease:
        # Same durable-directory primitive as
        # `OperationJournal.ensure_directory` -- this lease is typically
        # acquired before that operation's first journal record exists, and a
        # DIFFERENT VMID's lease may be racing this same first-use directory
        # concurrently; either way this call proves its own barrier.
        _ensure_durable_directory(self._path.parent, mode=0o700, anchor=self._anchor)
        self._descriptor = os.open(
            self._path, os.O_WRONLY | os.O_CREAT | os.O_CLOEXEC, 0o600
        )
        try:
            fcntl.flock(self._descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(self._descriptor)
            self._descriptor = None
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise RollbackError(
                    "operation_in_progress",
                    "another rollback operation holds this guest's lease",
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
        raise RollbackError("timeout", "PVE command timed out")
    if result.output_exceeded:
        raise RollbackError(
            "execution_failed", "PVE command output exceeded its bound"
        )
    return result


def _json_command(
    runner: Runner, argv: tuple[str, ...], what: str, *, max_output: int
) -> Any:
    result = _command(runner, argv, max_output=max_output)
    if result.returncode != 0:
        raise RollbackError("execution_failed", f"could not read {what}")
    try:
        return json.loads(result.stdout.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise RollbackError("execution_failed", f"{what} was malformed") from exc


def revalidate_live_target(runner: Runner, vmid: int, expected_node: str) -> None:
    """Independently re-read live PVE state immediately before any mutation.

    A VMID is an execution locator, never durable identity: PVE can free one
    and reuse it for an unrelated guest at any moment. The backend's
    `resource_id`, binding, and continuity revision are backend authority
    facts PVE itself does not know; what PVE *can* prove is checked here.
    """

    rows = _json_command(
        runner,
        (
            "pvesh",
            "get",
            "/cluster/resources",
            "--type",
            "vm",
            "--output-format",
            "json",
        ),
        "current PVE target state",
        max_output=4 * 1024 * 1024,
    )
    if not isinstance(rows, list):
        raise RollbackError("execution_failed", "current PVE target state was malformed")
    matches = [
        row
        for row in rows
        if isinstance(row, dict) and row.get("vmid") == vmid
    ]
    if len(matches) != 1:
        raise RollbackError(
            "stale_target", "current PVE state does not contain exactly this VMID"
        )
    row = matches[0]
    if str(row.get("type")) != "lxc":
        raise RollbackError(
            "unsupported_resource_type", "current PVE resource is not an LXC guest"
        )
    if str(row.get("node")) != expected_node:
        raise RollbackError(
            "stale_target", "current PVE resource is not on the expected node"
        )


def read_config_lock(runner: Runner, vmid: int, expected_node: str) -> str | None:
    """Return the container's current config `lock`, if any.

    ``None`` means the config carries no ``lock`` key at all -- the only
    state in which upstream `check_lock` permits a rollback. Every other
    outcome is returned or raised rather than normalised away: a non-string
    lock is malformed and fails closed, and an empty string is returned as-is
    so the caller refuses it too. PVE's own config parser does not emit an
    empty lock, so treating one as "unlocked" would be inventing permissive
    semantics for a state that should never occur.
    """

    config = _json_command(
        runner,
        (
            "pvesh",
            "get",
            f"/nodes/{expected_node}/lxc/{vmid}/config",
            "--output-format",
            "json",
        ),
        "current container configuration",
        max_output=1024 * 1024,
    )
    if not isinstance(config, dict):
        raise RollbackError(
            "execution_failed", "current container configuration was malformed"
        )
    lock = config.get("lock")
    if lock is None:
        return None
    if not isinstance(lock, str):
        raise RollbackError(
            "execution_failed", "current container configuration lock was malformed"
        )
    return lock


def read_snapshot_listing(
    runner: Runner, vmid: int, expected_node: str
) -> list[dict[str, Any]]:
    """Read one fresh canonical snapshot listing for this container."""

    rows = _json_command(
        runner,
        (
            "pvesh",
            "get",
            f"/nodes/{expected_node}/lxc/{vmid}/snapshot",
            "--output-format",
            "json",
        ),
        "current snapshot listing",
        max_output=4 * 1024 * 1024,
    )
    if not isinstance(rows, list) or not all(
        isinstance(row, dict) for row in rows
    ):
        raise RollbackError("execution_failed", "current snapshot listing was malformed")
    return rows


def read_task_status(
    runner: Runner, expected_node: str, task_upid: str
) -> dict[str, Any]:
    """Read one bounded PVE task status observation."""

    status = _json_command(
        runner,
        (
            "pvesh",
            "get",
            f"/nodes/{expected_node}/tasks/{task_upid}/status",
            "--output-format",
            "json",
        ),
        "PVE task status",
        max_output=256 * 1024,
    )
    if not isinstance(status, dict):
        raise RollbackError("execution_failed", "PVE task status was malformed")
    return status


def _task_is_terminal_success(status: Mapping[str, Any]) -> bool | None:
    """Apply PVE's own success rule. ``None`` means not terminal or unknown."""

    if status.get("status") != "stopped":
        return None
    exit_status = status.get("exitstatus")
    if not isinstance(exit_status, str) or not exit_status:
        return None
    if exit_status == "OK":
        return True
    prefix = "WARNINGS: "
    if (
        exit_status.startswith(prefix)
        and exit_status[len(prefix):].isdigit()
        and exit_status[len(prefix):].isascii()
    ):
        return True
    if exit_status == "unexpected status":
        return None
    return False


def _require_target_snapshot_is_rollbackable(
    snapshots: list[dict[str, Any]],
    snapshot_name: str,
    expected_ownership: Mapping[str, Any],
) -> None:
    """Prove, from ONE fresh listing, that this exact snapshot may be rolled to.

    Two independent things are proved here, and both must hold before the
    journal may reach `submitted`:

    1. **PVE would accept the operation.** Upstream `snapshot_rollback` dies
       on a missing snapshot and on one still carrying `snapstate`. Refusing
       here means an operation PVE was always going to reject never enters
       the permanently-uncertain window.

    2. **The snapshot is still this job's own.** A snapshot NAME is a
       physical PVE key and is NEVER ownership proof -- the strict structured
       metadata in its description is. Authority proved that when it armed
       the rollback, but PVE state can change afterwards: the same name can
       come to exist carrying absent, malformed, foreign, or another job's
       metadata. Because this check runs on the host, from the listing
       closest in time to the real `pvesh` call, it closes that window rather
       than trusting a proof taken earlier.

    Ambiguity is never resolved in the operation's favour. A Hubinet-looking
    description that will not parse, a second entry claiming this same job
    under a different name, and a duplicate of the target name all fail
    closed, exactly as the backend's own
    `_require_exactly_one_job_owned_snapshot` does.
    """

    matches: list[dict[str, Any]] = []
    for row in snapshots:
        name = row.get("name")
        if name == "current":
            continue
        try:
            ownership = parse_snapshot_description(row.get("description", ""))
        except RequestError as exc:
            # Looks like Hubinet metadata but does not parse. If it is the
            # target itself this is fatal; if it is some OTHER entry it
            # cannot be attributed, so it cannot be ruled out as a second
            # claim on this job either.
            raise RollbackError(
                "snapshot_ownership_malformed",
                "canonical PVE state contains malformed Hubinet snapshot "
                "metadata; job-owned snapshot ownership is ambiguous",
                submission="not_submitted",
            ) from exc
        claims_this_job = (
            ownership is not None
            and ownership["job_id"] == expected_ownership["job_id"]
        )
        if name != snapshot_name:
            if claims_this_job:
                raise RollbackError(
                    "snapshot_ownership_ambiguous",
                    "another snapshot claims this job under a different name",
                    submission="not_submitted",
                )
            continue
        if row.get("snapstate"):
            raise RollbackError(
                "snapshot_incomplete",
                "the target snapshot is incomplete and PVE would refuse to "
                "roll back",
                submission="not_submitted",
            )
        if ownership is None:
            raise RollbackError(
                "snapshot_ownership_absent",
                "the target snapshot carries no Hubinet ownership metadata",
                submission="not_submitted",
            )
        if ownership != dict(expected_ownership):
            raise RollbackError(
                "snapshot_ownership_mismatch",
                "the target snapshot does not carry this job's exact "
                "ownership metadata",
                submission="not_submitted",
            )
        matches.append(row)
    if len(matches) != 1:
        raise RollbackError(
            "snapshot_absent",
            "canonical PVE state does not contain exactly this job's snapshot",
            submission="not_submitted",
        )


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


def _journal_record(
    request: Mapping[str, Any],
    phase: str,
    *,
    task_upid: str | None = None,
    outcome: str | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "journal_version": 1,
        "rollback_operation_id": request["rollback_operation_id"],
        "phase": phase,
        "request_fingerprint": request_fingerprint(request),
        "vmid": request["vmid"],
    }
    if task_upid is not None:
        record["task_upid"] = task_upid
    if outcome is not None:
        record["outcome"] = outcome
    return record


def _require_same_request(
    record: Mapping[str, Any], request: Mapping[str, Any]
) -> None:
    """Refuse to reuse a journal written for a materially different request."""

    if record.get("request_fingerprint") != request_fingerprint(request):
        raise RollbackError(
            "request_mismatch",
            "an operation with this identity was journaled for a different request",
        )
    if record.get("vmid") != request["vmid"]:
        raise RollbackError(
            "request_mismatch",
            "an operation with this identity was journaled for a different VMID",
        )


def _phase_response(
    request: Mapping[str, Any],
    phase: str,
    *,
    outcome: str,
    task_upid: str | None = None,
    task: Mapping[str, Any] | None = None,
    snapshots: list[dict[str, Any]] | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    response: dict[str, Any] = {
        "response_version": 1,
        "ok": True,
        "rollback_operation_id": request["rollback_operation_id"],
        "outcome": outcome,
        "rollback_state": phase,
    }
    if task_upid is not None:
        response["task_upid"] = task_upid
    if task is not None:
        response["task"] = dict(task)
    if snapshots is not None:
        response["snapshots"] = snapshots
    if reason is not None:
        response["reason"] = reason[:500]
    return response


def _resolve_post_submission(
    runner: Runner,
    journal: OperationJournal,
    request: Mapping[str, Any],
    record: Mapping[str, Any],
) -> dict[str, Any]:
    """Report the current truth about an operation that reached `submitted`.

    Never resubmits. With a task identity it reads that exact task once and
    classifies it by PVE's own rule; without one the answer is `uncertain`,
    because rollback has no unique canonical witness of its own (see the
    module docstring).
    """

    phase = record["phase"]
    task_upid = record.get("task_upid")
    if task_upid is None:
        return _phase_response(
            request,
            phase,
            outcome="uncertain",
            reason=(
                "rollback was submitted but no PVE task identity was captured; "
                "canonical state cannot distinguish this rollback from an earlier one"
            ),
        )

    status = read_task_status(runner, request["expected_node"], str(task_upid))
    succeeded = _task_is_terminal_success(status)
    if succeeded is None:
        if status.get("status") == "running":
            return _phase_response(
                request,
                phase,
                outcome="uncertain",
                task_upid=str(task_upid),
                task=status,
                reason="PVE rollback task is still running",
            )
        return _phase_response(
            request,
            phase,
            outcome="uncertain",
            task_upid=str(task_upid),
            task=status,
            reason="PVE rollback task status could not be classified",
        )
    if not succeeded:
        if phase != "terminal":
            journal.write(
                _journal_record(
                    request, "terminal", task_upid=str(task_upid), outcome="failed"
                )
            )
        return _phase_response(
            request,
            "terminal",
            outcome="failed",
            task_upid=str(task_upid),
            task=status,
            reason="PVE rollback task reached a terminal error state",
        )

    snapshots = read_snapshot_listing(
        runner, request["vmid"], request["expected_node"]
    )
    if phase != "terminal":
        journal.write(
            _journal_record(
                request, "terminal", task_upid=str(task_upid), outcome="completed"
            )
        )
    return _phase_response(
        request,
        "terminal",
        outcome="completed",
        task_upid=str(task_upid),
        task=status,
        snapshots=snapshots,
        reason="PVE rollback task reached a terminal non-error state",
    )


def inspect_rollback_state(
    runner: Runner, journal: OperationJournal, request: Mapping[str, Any]
) -> dict[str, Any]:
    """Read current durable and canonical state. Submits nothing, ever."""

    with VmidRollbackLease(request["vmid"], journal.directory, anchor=journal.anchor):
        record = journal.read(request["rollback_operation_id"])
        # `absent` and `intent` are TRANSIENT pre-submission routing evidence,
        # so they report `uncertain`, never `not_submitted`. A helper launched
        # by a backend that then died may not have taken this lease yet, so
        # neither observation may release a job -- only the durable seal may,
        # and only it reports `not_submitted` from this operation.
        if record is None:
            return _phase_response(
                request,
                "absent",
                outcome="uncertain",
                reason="no rollback operation is journaled for this identity",
            )
        _require_same_request(record, request)
        phase = record["phase"]
        if phase == "intent":
            return _phase_response(
                request,
                "intent",
                outcome="uncertain",
                reason="rollback intent is journaled but nothing was submitted",
            )
        if phase == "sealed_not_submitted":
            return _phase_response(
                request,
                "sealed_not_submitted",
                outcome="not_submitted",
                reason="this rollback operation is durably sealed as never submitted",
            )
        return _resolve_post_submission(runner, journal, request, record)


def seal_rollback_never_submitted(
    runner: Runner, journal: OperationJournal, request: Mapping[str, Any]
) -> dict[str, Any]:
    """Durably forbid this exact operation from ever being submitted.

    Performs no PVE reads at all, so a moved or deleted guest does not need to
    exist on the frozen node for this release path to work. The per-VMID lease
    is what orders it against a delayed submitter: if seal wins, that
    submitter reads `sealed_not_submitted` when it finally takes the lease and
    must refuse.
    """

    with VmidRollbackLease(request["vmid"], journal.directory, anchor=journal.anchor):
        record = journal.read(request["rollback_operation_id"])
        if record is not None:
            _require_same_request(record, request)
            phase = record["phase"]
            if phase == "sealed_not_submitted":
                return _phase_response(
                    request,
                    "sealed_not_submitted",
                    outcome="not_submitted",
                    reason="this rollback operation was already durably sealed",
                )
            if phase not in _PRE_SUBMISSION_PHASES:
                # Post-submission. Sealing would be a lie: PVE may already
                # have force-stopped this container.
                raise RollbackError(
                    "already_submitted",
                    "this rollback operation is already past its submission boundary",
                )
        journal.write(_journal_record(request, "sealed_not_submitted"))
        return _phase_response(
            request,
            "sealed_not_submitted",
            outcome="not_submitted",
            reason="this rollback operation is now durably sealed as never submitted",
        )


def submit_same_job_rollback(
    runner: Runner, journal: OperationJournal, request: Mapping[str, Any]
) -> dict[str, Any]:
    """Submit, or reattach to, this exact job's rollback. At most once.

    Submission-only: it never waits for PVE's asynchronous `vzrollback` task.
    """

    with VmidRollbackLease(request["vmid"], journal.directory, anchor=journal.anchor):
        record = journal.read(request["rollback_operation_id"])
        if record is not None:
            _require_same_request(record, request)
            phase = record["phase"]
            if phase == "sealed_not_submitted":
                # The durable no-future-submit fence. A delayed helper that
                # was launched before the seal must obey it.
                raise RollbackError(
                    "sealed_not_submitted",
                    "this rollback operation is durably sealed as never submitted",
                    submission="not_submitted",
                )
            if phase != "intent":
                # Already submitted. NEVER resubmit -- report the truth.
                return _resolve_post_submission(runner, journal, request, record)

        # Pre-flight, all of it BEFORE the journal reaches `submitted`, so a
        # refusal here leaves the operation releasable rather than
        # permanently uncertain.
        revalidate_live_target(runner, request["vmid"], request["expected_node"])
        # ANY config lock refuses, not a curated list of "interesting" ones.
        # Upstream `PVE::AbstractConfig::check_lock` dies whenever
        # `$conf->{lock}` is truthy -- it does not accept `backup`,
        # `migrate`, or any other lock type. Treating only snapshot-family
        # locks as blockers let a legitimately locked container reach the
        # durable `submitted` record, after which PVE refuses the rollback
        # and the operation can no longer be sealed or retried: the job would
        # hold the one global destructive slot for a refusal that was
        # observable beforehand.
        lock = read_config_lock(runner, request["vmid"], request["expected_node"])
        if lock is not None:
            raise RollbackError(
                "operation_in_progress",
                f"the container configuration is locked ({lock or 'empty'}) "
                "and PVE would refuse to roll back",
                submission="not_submitted",
            )
        snapshots = read_snapshot_listing(
            runner, request["vmid"], request["expected_node"]
        )
        _require_target_snapshot_is_rollbackable(
            snapshots,
            request["snapshot_name"],
            request["expected_snapshot_ownership"],
        )

        if record is None:
            journal.write(_journal_record(request, "intent"))

        # The uncertainty boundary. `submitted` is fsynced BEFORE `pvesh
        # create` runs, so a crash between the two leaves a durable record
        # that is never resubmitted from.
        journal.write(_journal_record(request, "submitted"))

        # Fresh revalidation immediately before the one real mutation: the
        # reads above cost wall-clock time in which PVE could have freed and
        # reused this VMID.
        revalidate_live_target(runner, request["vmid"], request["expected_node"])
        result = _command(
            runner,
            (
                "pvesh",
                "create",
                f"/nodes/{request['expected_node']}/lxc/{request['vmid']}"
                f"/snapshot/{request['snapshot_name']}/rollback",
                "--start",
                str(ROLLBACK_START_AFTER),
                "--output-format",
                "json",
            ),
            max_output=256 * 1024,
        )
        if result.returncode != 0:
            # PVE refused AFTER the durable `submitted` record exists. That
            # record stays: this operation is never retried, and the backend
            # keeps the job fenced. It is deliberately NOT sealed -- the
            # pre-submission release contract no longer applies.
            raise RollbackError(
                "execution_failed", "PVE refused or failed the rollback submission"
            )
        upid = _extract_upid(result.stdout)
        if upid is None:
            return _phase_response(
                request,
                "submitted",
                outcome="uncertain",
                reason="PVE accepted the rollback but returned no task identity",
            )
        journal.write(_journal_record(request, "task_known", task_upid=upid))
        return _phase_response(
            request,
            "task_known",
            outcome="uncertain",
            task_upid=upid,
            reason="rollback submitted; PVE task identity captured",
        )


def _extract_upid(stdout: bytes) -> str | None:
    """Pull the UPID out of `pvesh create`'s answer, strictly."""

    try:
        text = stdout.decode("utf-8")
    except UnicodeDecodeError:
        return None
    candidate: Any = None
    try:
        parsed = json.loads(text)
    except ValueError:
        candidate = text.strip()
    else:
        candidate = parsed if isinstance(parsed, str) else None
    if not isinstance(candidate, str):
        return None
    candidate = candidate.strip().strip('"')
    if not UPID_RE.fullmatch(candidate) or len(candidate) > 300:
        return None
    return candidate


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def handle(
    payload: Any,
    *,
    runner: Runner = _run_bounded,
    journal: OperationJournal | None = None,
) -> dict[str, Any]:
    """Dispatch one validated request. The ONLY dispatcher in this file."""

    request = parse_request(payload)
    operation_journal = journal if journal is not None else OperationJournal()
    if request["operation"] == "inspect_rollback_state":
        return inspect_rollback_state(runner, operation_journal, request)
    if request["operation"] == "seal_rollback_never_submitted":
        return seal_rollback_never_submitted(runner, operation_journal, request)
    if request["operation"] == "submit_same_job_rollback":
        return submit_same_job_rollback(runner, operation_journal, request)
    # Unreachable: parse_request already refuses anything else.
    raise RequestError("unsupported operation")


def main() -> int:
    raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    if len(raw) > MAX_REQUEST_BYTES:
        _emit(
            {
                "response_version": 1,
                "ok": False,
                "rollback_operation_id": None,
                "error": {
                    "classification": "request_too_large",
                    "message": "request exceeded its structural bound",
                },
            }
        )
        return 1
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        _emit(
            {
                "response_version": 1,
                "ok": False,
                "rollback_operation_id": None,
                "error": {
                    "classification": "malformed_request",
                    "message": "request was not valid JSON",
                },
            }
        )
        return 1

    operation_id: str | None = None
    try:
        request = parse_request(payload)
        operation_id = request["rollback_operation_id"]
    except RequestError as exc:
        _emit(
            {
                "response_version": 1,
                "ok": False,
                "rollback_operation_id": None,
                "error": {
                    "classification": "malformed_request",
                    "message": str(exc)[:500],
                },
            }
        )
        return 1

    try:
        _emit(handle(payload))
    except RollbackError as exc:
        _emit(
            {
                "response_version": 1,
                "ok": False,
                "rollback_operation_id": operation_id,
                "error": {
                    "classification": exc.classification,
                    "message": str(exc)[:500],
                    "submission": exc.submission,
                },
            }
        )
        return 1
    except Exception:  # noqa: BLE001 - never leak a traceback across the boundary
        _emit(
            {
                "response_version": 1,
                "ok": False,
                "rollback_operation_id": operation_id,
                "error": {
                    "classification": "execution_failed",
                    "message": "rollback boundary failed",
                    "submission": "may_have_been_submitted",
                },
            }
        )
        return 1
    return 0


def _emit(payload: Mapping[str, Any]) -> None:
    sys.stdout.write(_canonical_json(dict(payload)))
    sys.stdout.write("\n")
    sys.stdout.flush()


if __name__ == "__main__":
    raise SystemExit(main())
