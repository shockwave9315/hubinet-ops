#!/usr/bin/env python3
"""Forced-command PVE boundary for Hubinet's sole health-evaluation operation.

**Deployed.** `deploy/lib/bootstrap-update-boundaries.sh` and
`deploy/update-proxmox-0.5.sh` install this file as one of the five
package-update forced-command boundaries (snapshot, plan simulation,
mutation, rollback, health), each behind its own dedicated key and its own
root-owned forced command. It requires no PVE API privilege beyond the
audit-only pair the product already has: it uses host-local `pct exec`, not a
PVE mutation endpoint.

It exposes exactly ONE typed operation, `evaluate_health_contract`, and that
operation is READ-ONLY. It cannot create, delete, start, stop, snapshot, roll
back, upgrade, install, or remove anything, and there is no path through it
that accepts remote command text.

## Bounded health settling, inside this one call

A single `evaluate_health_contract` request may take up to the backend's
requested settling deadline (`DEFAULT_SETTLING_DEADLINE_SECONDS`, 180s by
product default; the backend states its policy on the wire, and this file
validates and clamps it against its own hard ceilings before using it -- see
below) to answer, because it owns the ENTIRE bounded settling window
described in `ARCHITECTURE.md`, "Job-bound healthcheck execution", not one
instantaneous sample. Every declared probe restarts Docker's or systemd's own
state machine on an ordinary package-triggered restart, and a normal
successful update must not durably fail, or require a manual re-run, merely
because that restart has not finished settling yet.

So this file repeatedly observes the COMPLETE frozen probe set in bounded
ROUNDS, each one batched per family (at most one `docker ps`, one
`docker inspect`, and one `systemctl show` per round -- never one guest
command per probe), until either:

- a ROUND is decisive (every probe resolved with no transient state, the
  round itself completed within `MAX_ROUND_SPAN_SECONDS`, and this is at
  least the `MIN_DECISIVE_ROUND`-th round) and every probe passed, or one
  failed definitively -- both cases terminate immediately with that verdict;
- the bounded deadline, round count, or guest-command ceiling is reached
  first, in which case the answer is UNKNOWN, carrying only the LAST COMPLETE
  round's evidence -- evidence from different rounds is never merged into one
  verdict.

`PACKAGE_UPDATE_HEALTH_TIMEOUT_SECONDS = 300` on the backend side
(`app/inventory_runtime_config.py`) already gives this settling window its
transport headroom: 180s of settling plus per-command allowance and margin.

## Why the commands are what they are

Every argv below is fixed, and every one was verified against the real tools
rather than assumed. A probe TARGET is data supplied by the operator through
the contract API; it becomes one argv element and never command text, never a
format string, never a template, and never a shell fragment. Shell quoting is
not used as a security mechanism anywhere in this file -- there is no shell.

**systemd.** `systemctl is-active <pattern>` is unusable: verified against
systemd 257, it expands glob patterns and exits 0 if ANY matching unit is
active, and an explicit `--` end-of-options marker does not stop that
expansion. `systemctl is-active 'ssh*'` prints four lines and succeeds. A
probe built on it could pass because some other unit is up, which is exactly
the false PASS this stage refuses to be capable of.

So the operation is `systemctl show`, which prints one blank-line-separated
property block per matched unit, and this file requests every frozen systemd
target in ONE such call:

1. `--` IS honoured here (verified: `systemctl show ... -- --help` reports
   `Id=--help.service` rather than printing usage), so an option-like target
   can never be consumed as an option;
2. every target must match a strict unit-name charset that contains none of
   systemd's glob characters `*`, `?`, `[` -- necessary because a pattern can
   legitimately match exactly ONE unit (verified: `ssh?service` matched only
   `ssh.service`), so "one block per requested unit" alone is not sufficient;
3. blocks are mapped back to targets BY POSITION, never by the returned `Id`:
   verified that `ssh.service` and `sshd.service` are aliases of the SAME
   unit and both report `Id=ssh.service`, so two distinct requested targets
   can legitimately share one `Id`, and only the batched call's own
   request-order answers "which target is this block about".
4. `ActiveState=active` is the only PASS. `activating`/`deactivating`/
   `reloading`, and an `inactive`/`failed` unit that still carries a
   non-empty `Job` (verified: `systemctl show --property=Job` is empty while
   idle and numeric mid-transition), are all UNKNOWN this round -- systemd's
   own transient states, not a workload verdict. An `inactive`/`failed` unit
   with an EMPTY `Job`, `maintenance`, and `LoadState=not-found` (which
   systemd reports as a normal success with `ActiveState=inactive`) are
   definitive FAILs. An unreadable, empty, or wrong-count answer is UNKNOWN.
   "The command ran" is never a PASS.

**Docker.** Every frozen Docker target -- for BOTH probe kinds together -- is
inspected in ONE batched `docker inspect` call. `docker inspect` resolves a
container by name OR by ID prefix (verified), so the returned `.Name` is
compared against the requested target and matched BY NAME, never by position
or ID prefix: verified that a missing target among several does not shift the
others, and does not stop the present ones from still being printed. `--type
container` stops an image of the same name matching, and `--` is honoured
(verified: `-- --help` is treated as a container name). The `--format`
template is a constant owned by this file; no part of it is built from a
request.

A fixed daemon oracle (`docker ps --all --no-trunc --format json .Names`,
verified to emit one JSON string per existing container regardless of state)
runs FIRST, once per round, for every family member: it is both the daemon
liveness proof and the complete, positively-proven container-name universe.
A requested target absent from that universe is definitively absent; a
target present in it that the batched inspect still could not read cleanly is
UNKNOWN, never absence, because the daemon already proved a moment earlier
that the name exists.

**`docker_container_healthy` is never downgraded to "running".** It requires
`.State.Status` exactly `running` AND `.State.Health.Status` exactly
`healthy`. A container with no HEALTHCHECK, or one reporting `unhealthy`, is
a definitive FAIL, because the operator specifically demanded Docker health.
`starting` is different: Docker's own documented state machine enters it
automatically after every container (re)start and can only leave it for
`healthy` or `unhealthy` once `--health-start-period` and the first probe
elapse, so it is not a workload verdict at all -- it is the daemon saying "no
verdict yet". Live Human1 evidence proved this the hard way: a package update
that legitimately restarts Docker/containerd restarts every container's
health state machine too, and the guest was observed and reported `healthy`
again within seconds, after this helper had already durably recorded a FAIL
under the OLD, unbatched, un-settled design. So `starting` is UNKNOWN, not
FAILED, and this file's bounded settling loop exists precisely so an ordinary
restart resolves it automatically rather than needing a manual re-run.

**The full transient family, not only `starting`.** `.State.Status` values
`created` (not started yet), `restarting`, and `removing` are, symmetrically,
Docker's own transient lifecycle states for EITHER Docker probe kind: none of
them is a workload verdict, and every one of them normally resolves within
seconds. `exited`, `dead`, and `paused` are definitive FAILs for both kinds --
none of the three means "running", and continuing to wait for them would be
mistaking a settled failure for one still in flight.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import json
import os
import re
import selectors
import shlex
import subprocess
import sys
import time
import uuid
from typing import Any


MAX_REQUEST_BYTES = 32 * 1024
MAX_COMMAND_OUTPUT_BYTES = 256 * 1024
COMMAND_TIMEOUT_SECONDS = 60.0
MAX_PROBES = 32
MAX_PROBE_TARGET_LENGTH = 200
NODE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,62}")
FINGERPRINT_RE = re.compile(r"[0-9a-f]{64}")

PROBE_KINDS = (
    "systemd_unit_active",
    "docker_container_running",
    "docker_container_healthy",
    "guest_operational",
)

#: The one kind whose probes carry no target at all -- never a faked one.
_GUEST_OPERATIONAL_KIND = "guest_operational"

# ---------------------------------------------------------------------------
# Bounded health settling -- backend-owned TIMING POLICY, never HA-owned.
# See ARCHITECTURE.md, "Job-bound healthcheck execution".
#
# The backend states its settling policy on the wire (`settling_policy` in
# the request), and this file VALIDATES AND CLAMPS it against its own
# code-owned hard ceilings before using it -- "backend owns timing policy,
# privileged helper enforces hard ceilings" (frozen architecture). The
# product default the backend actually sends is, and stays,
# `DEFAULT_SETTLING_DEADLINE_SECONDS` / `DEFAULT_OBSERVATION_INTERVAL_
# SECONDS` below; nothing about accepting a typed policy field loosens that
# default, and Home Assistant never supplies or chooses either value -- it
# has no path to this boundary at all.
# ---------------------------------------------------------------------------

#: The backend's own product-policy default -- what it actually sends today,
#: and the value every existing behavioural guarantee in this file (and its
#: docstring above) describes.
DEFAULT_SETTLING_DEADLINE_SECONDS = 180.0
#: Never below this, however the backend's policy is configured: a window
#: this short could never even reach `MIN_DECISIVE_ROUND` at the default
#: observation interval, defeating the whole point of bounded settling.
MIN_SETTLING_DEADLINE_SECONDS = 30.0
#: Never above this. `PACKAGE_UPDATE_HEALTH_TIMEOUT_SECONDS = 300` on the
#: backend side is the OUTER SSH transport timeout; this hard ceiling keeps
#: settling comfortably inside it even after per-command allowance and the
#: transport-return margin, so a compromised or buggy backend cannot request
#: a deadline this file would let the outer transport kill mid-response.
MAX_SETTLING_DEADLINE_SECONDS = 200.0
#: The backend's own product-policy default observation interval.
DEFAULT_OBSERVATION_INTERVAL_SECONDS = 5.0
#: Never below this -- a busier interval than the product default buys
#: nothing (every round is still batched per family) and multiplies guest
#: command load for no benefit.
MIN_OBSERVATION_INTERVAL_SECONDS = 1.0
#: Never above this -- an interval this coarse could exhaust
#: `MAX_SETTLING_ROUNDS` long before the deadline, or leave a genuinely
#: fast-settling workload waiting far longer than necessary.
MAX_OBSERVATION_INTERVAL_SECONDS = 30.0
#: A round whose own guest commands took longer than this to answer is never
#: decisive, whichever way it points: a slow round may be describing state
#: that has already moved on again by the time it is read.
MAX_ROUND_SPAN_SECONDS = 15.0
#: No verdict -- PASS or FAIL -- may ever be reached before this round index.
#: Round 1 runs immediately after a package mutation that may itself still be
#: settling Docker/systemd, and a same-round coincidence is not enough
#: evidence either way.
MIN_DECISIVE_ROUND = 2
#: A hard structural ceiling on rounds attempted, independent of the wall
#: clock deadline above (defends against a clock that misbehaves).
MAX_SETTLING_ROUNDS = 40
#: At most this many guest commands (`pct exec` invocations) in ONE round,
#: whatever the probe count: one batched `docker ps`, one batched
#: `docker inspect`, one batched `systemctl show`, and (v20) one fixed
#: `guest_operational` check -- one command per family, never one per probe.
MAX_COMMANDS_PER_ROUND = 4
#: A hard ceiling on guest commands across the WHOLE settling window.
MAX_GUEST_COMMANDS = 160

#: Reserved off every clamped command timeout so this file can still finish
#: classifying a round and assemble its own bounded JSON response before the
#: OUTER SSH transport timeout (`PACKAGE_UPDATE_HEALTH_TIMEOUT_SECONDS`,
#: 300s) would kill it. This is what keeps "helper settling deadline = 180s"
#: from ever becoming "actual helper runtime = 280s+, killed mid-command by
#: the transport" -- every command this file issues is bounded by what is
#: ACTUALLY left of the 180s settling budget, never by the fixed 60s
#: allowance alone.
_TRANSPORT_RETURN_MARGIN_SECONDS = 2.0
#: A command is never issued with a timeout below this, however little
#: settling budget remains -- a command still gets a chance to answer
#: instantly rather than being skipped outright, and `subprocess` timeouts
#: of exactly zero are not a meaningful bound.
_MIN_COMMAND_TIMEOUT_SECONDS = 0.05

Clock = Callable[[], float]
Sleeper = Callable[[float], None]


def _clamped_command_timeout(remaining_budget: float) -> float:
    """The timeout for ONE guest/host command, bounded by what is ACTUALLY
    left of the settling window -- never by `COMMAND_TIMEOUT_SECONDS` alone.

    Frozen rule: never start an operation whose own bounded timeout could
    exceed the remaining health-evaluation budget. A command that would only
    get a sliver of time left still gets `_MIN_COMMAND_TIMEOUT_SECONDS`
    rather than nothing -- it may simply time out quickly and honestly,
    which this file already treats as a truthful UNKNOWN.
    """

    return max(
        _MIN_COMMAND_TIMEOUT_SECONDS,
        min(
            COMMAND_TIMEOUT_SECONDS,
            remaining_budget - _TRANSPORT_RETURN_MARGIN_SECONDS,
        ),
    )

#: Execution-time systemd unit-name validation. Deliberately the SMALLEST
#: restriction that makes the requested object unambiguous, and every part of
#: it earns its place:
#:
#: - the charset excludes systemd's glob characters `*`, `?` and `[`, so the
#:   target can never be a pattern that matches some other active unit;
#: - it excludes `/`, whitespace, and everything systemd would have to escape,
#:   so the name systemd resolves is the name the operator wrote;
#: - a leading `-` is refused as well as guarded by `--`, so nothing depends
#:   on a single mechanism.
SYSTEMD_UNIT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9:_.@-]{0,199}")

#: An explicit unit-type suffix is REQUIRED. `systemctl show nginx` silently
#: resolves to `nginx.service`, and quietly broadening `nginx` into
#: `nginx.service` would be deciding, on the operator's behalf, which of
#: several possible objects the contract meant. A target that does not say is
#: reported UNKNOWN with `probe_target_not_exact`, never guessed at.
SYSTEMD_UNIT_SUFFIXES = (
    ".service",
    ".socket",
    ".target",
    ".timer",
    ".mount",
    ".automount",
    ".path",
    ".slice",
    ".scope",
    ".device",
    ".swap",
)

#: Execution-time Docker container-name validation, matching upstream's own
#: name grammar. It excludes the leading `/` `docker inspect` reports, and
#: excludes anything that could be read as an option or a path.
DOCKER_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}")

#: A CONSTANT owned by this file. Never built from a request, never
#: interpolated, and never extended by a caller. `.Name` is required to map a
#: batched answer back onto its exact requested target, never by position and
#: never by ID prefix. The `<none>` branches are what let `docker_container_
#: healthy` tell "no HEALTHCHECK configured" apart from a health status, and
#: what makes an absent `.State.Health` block (a container Docker has not yet
#: computed health for) distinguishable from a real value.
DOCKER_INSPECT_FORMAT = (
    "{{.Name}}\t{{.State.Status}}\t{{.State.Restarting}}\t"
    "{{if .State.Health}}{{.State.Health.Status}}{{else}}<none>{{end}}"
)

#: Kept beside the template above so the flag and the constant it carries are
#: audited as one thing.
DOCKER_INSPECT_FORMAT_FLAG = "--format"

#: Fixed positive absence proof.  Docker 26.1.5 was verified to accept this
#: exact `ps` shape and emit each container's complete `.Names` value as one
#: JSON string, for every container regardless of state -- the daemon
#: liveness oracle and the complete existing-name universe in one call.
DOCKER_NAME_LIST_FORMAT = "{{json .Names}}"

#: `.State.Status` values that mean the container is not, and is not about to
#: become, healthy or running: none of the three means "running", and none of
#: them resolves itself the way `created`/`restarting`/`removing` do.
DOCKER_DEFINITIVE_FAIL_STATUSES = frozenset({"exited", "dead", "paused"})
#: `.State.Status` values that are Docker's own transient lifecycle, entered
#: automatically and expected to resolve on their own within seconds.
DOCKER_TRANSIENT_STATUSES = frozenset({"created", "restarting", "removing"})
_DOCKER_TRANSIENT_REASONS = {
    "created": "container_not_started_yet",
    "restarting": "container_restarting",
    "removing": "container_removing",
}

#: systemd ActiveState values whose meaning depends on `Job`: a pending job
#: means the unit is mid-transition (UNKNOWN, `unit_job_pending`); an empty
#: one means it has genuinely settled at rest (definitive FAIL).
SYSTEMD_JOB_DEPENDENT_STATES = frozenset({"inactive", "failed"})
#: ActiveState values that are systemd's own transient job states,
#: unconditionally -- `Job` is not consulted for these because systemd
#: reports them only while a job is already in flight.
_SYSTEMD_TRANSIENT_REASONS = {
    "activating": "unit_activating",
    "deactivating": "unit_deactivating",
    "reloading": "unit_reloading",
}
#: Every ActiveState value this helper can interpret at all. Anything else is
#: an answer this helper does not understand, which is UNKNOWN rather than a
#: guess in either direction.
SYSTEMD_KNOWN_ACTIVE_STATES = (
    frozenset({"active", "maintenance"})
    | SYSTEMD_JOB_DEPENDENT_STATES
    | frozenset(_SYSTEMD_TRANSIENT_REASONS)
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
    pass


class HealthError(RuntimeError):
    """The whole evaluation could not be carried out."""

    def __init__(self, classification: str, message: str) -> None:
        super().__init__(message)
        self.classification = classification
        self.message = message


class ProbeUnknown(RuntimeError):
    """ONE probe could not be evaluated truthfully. Never a pass or a fail."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


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


def _canonical_uuid(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise RequestError(f"{field_name} must be a canonical UUID")
    try:
        parsed = uuid.UUID(value)
    except ValueError as exc:
        raise RequestError(f"{field_name} must be a canonical UUID") from exc
    if parsed.int == 0 or str(parsed) != value:
        raise RequestError(f"{field_name} must be a canonical UUID")
    return value


def _positive_integer(value: Any, field_name: str) -> int:
    if type(value) is not int or value <= 0:
        raise RequestError(f"{field_name} must be a positive integer")
    return value


def _clamp(value: float, *, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def _validate_settling_policy(raw: Any) -> tuple[float, float]:
    """Backend-stated timing policy, validated and CLAMPED against this
    file's own hard ceilings -- "backend owns timing policy, privileged
    helper enforces hard ceilings" (frozen architecture). A caller cannot
    request an arbitrarily large (or small) deadline or interval: whatever
    is asked for is silently bounded to what this file will actually permit,
    never rejected outright for being merely out of range, so an otherwise
    coherent evaluation is not refused wholesale over a policy value.
    """

    if not isinstance(raw, Mapping) or set(raw) != {
        "deadline_seconds",
        "observation_interval_seconds",
    }:
        raise RequestError("settling_policy must have the exact shape")
    deadline = raw["deadline_seconds"]
    interval = raw["observation_interval_seconds"]
    if type(deadline) not in (int, float) or isinstance(deadline, bool):
        raise RequestError("settling_policy.deadline_seconds must be numeric")
    if type(interval) not in (int, float) or isinstance(interval, bool):
        raise RequestError(
            "settling_policy.observation_interval_seconds must be numeric"
        )
    if deadline <= 0 or interval <= 0:
        raise RequestError("settling_policy values must be positive")
    return (
        _clamp(
            float(deadline),
            minimum=MIN_SETTLING_DEADLINE_SECONDS,
            maximum=MAX_SETTLING_DEADLINE_SECONDS,
        ),
        _clamp(
            float(interval),
            minimum=MIN_OBSERVATION_INTERVAL_SECONDS,
            maximum=MAX_OBSERVATION_INTERVAL_SECONDS,
        ),
    )


def validate_request(payload: Any) -> dict[str, Any]:
    """Accept exactly one request shape, and nothing else.

    Every authority fact arrives typed and is validated here. There is no
    field through which a caller could pass a command, an option, an argv
    fragment, a format template, an environment variable, or a probe kind
    outside the three the product defines. This exact top-level shape, and
    the exact "request must have the exact health-evaluation shape" message
    below, are a bootstrap/updater acceptance marker: `deploy/lib/bootstrap-
    update-boundaries.sh`, `deploy/lib/update-boundaries.sh`, and
    `tests/_bootstrap_fake_pve.py` all assert this literal string to prove
    the deployed forced command is genuinely this helper -- that stays
    byte-identical even though the valid request shape below now ALSO
    requires ``settling_policy`` (PR #80 review 2.5: the backend states its
    timing policy on the wire rather than it living only inside this file),
    because a payload missing every required top-level key still fails this
    exact check with this exact message regardless of what that key set is.
    """

    if not isinstance(payload, Mapping) or set(payload) != {
        "request_version",
        "operation",
        "target",
        "ownership",
        "health_contract",
        "settling_policy",
    }:
        raise RequestError("request must have the exact health-evaluation shape")
    if (
        payload["request_version"] != 1
        or payload["operation"] != "evaluate_health_contract"
    ):
        raise RequestError("unknown host-control operation")
    settling_deadline_seconds, observation_interval_seconds = (
        _validate_settling_policy(payload["settling_policy"])
    )

    target = payload["target"]
    if not isinstance(target, Mapping) or set(target) != {"vmid", "expected_node"}:
        raise RequestError("target must have the exact health-evaluation shape")
    vmid = target["vmid"]
    if type(vmid) is not int or not 100 <= vmid <= 999_999_999:
        raise RequestError("vmid must be a valid PVE integer VMID")
    expected_node = target["expected_node"]
    if not isinstance(expected_node, str) or not NODE_RE.fullmatch(expected_node):
        raise RequestError("expected_node is invalid")

    ownership = payload["ownership"]
    if not isinstance(ownership, Mapping) or set(ownership) != {
        "job_id",
        "resource_id",
        "resource_continuity_revision",
        "binding_id",
        "locator_generation",
        "backend_instance_id",
    }:
        raise RequestError("ownership must have the exact health-evaluation shape")
    normalized_ownership = {
        "job_id": _canonical_uuid(ownership["job_id"], "job_id"),
        "resource_id": _canonical_uuid(ownership["resource_id"], "resource_id"),
        "binding_id": _canonical_uuid(ownership["binding_id"], "binding_id"),
        "locator_generation": _positive_integer(
            ownership["locator_generation"], "locator_generation"
        ),
        "resource_continuity_revision": _positive_integer(
            ownership["resource_continuity_revision"],
            "resource_continuity_revision",
        ),
        "backend_instance_id": _canonical_uuid(
            ownership["backend_instance_id"], "backend_instance_id"
        ),
    }

    contract = payload["health_contract"]
    if not isinstance(contract, Mapping) or set(contract) != {
        "revision",
        "fingerprint",
        "probes",
    }:
        raise RequestError("health_contract must have the exact shape")
    revision = _positive_integer(contract["revision"], "health contract revision")
    fingerprint = contract["fingerprint"]
    if not isinstance(fingerprint, str) or not FINGERPRINT_RE.fullmatch(fingerprint):
        raise RequestError("health contract fingerprint is invalid")
    raw_probes = contract["probes"]
    if not isinstance(raw_probes, list) or not 1 <= len(raw_probes) <= MAX_PROBES:
        raise RequestError("a health contract declares 1 to 32 probes")
    probes: list[dict[str, Any]] = []
    seen: set[tuple[str, str | None]] = set()
    for index, raw in enumerate(raw_probes):
        if not isinstance(raw, Mapping) or set(raw) != {"index", "kind", "target"}:
            raise RequestError("a probe must have the exact shape")
        if raw["index"] != index:
            raise RequestError("probes must be canonically indexed from 0")
        kind = raw["kind"]
        if kind not in PROBE_KINDS:
            raise RequestError("unsupported probe kind")
        probe_target = raw["target"]
        if kind == _GUEST_OPERATIONAL_KIND:
            if probe_target is not None:
                raise RequestError(
                    "a guest_operational probe must not carry a target"
                )
        elif (
            not isinstance(probe_target, str)
            or not 1 <= len(probe_target) <= MAX_PROBE_TARGET_LENGTH
        ):
            raise RequestError("probe target is out of bounds")
        identity = (kind, probe_target)
        if identity in seen:
            raise RequestError("a health contract may not repeat a probe")
        seen.add(identity)
        probes.append({"index": index, "kind": kind, "target": probe_target})

    return {
        "vmid": vmid,
        "expected_node": expected_node,
        "ownership": normalized_ownership,
        "revision": revision,
        "fingerprint": fingerprint,
        "probes": probes,
        "settling_deadline_seconds": settling_deadline_seconds,
        "observation_interval_seconds": observation_interval_seconds,
    }


def _command(
    runner: Runner,
    argv: tuple[str, ...],
    *,
    timeout: float = COMMAND_TIMEOUT_SECONDS,
    max_output: int = MAX_COMMAND_OUTPUT_BYTES,
) -> CommandResult:
    return runner(argv, timeout, max_output)


def _local_node(runner: Runner) -> str:
    """Ask this PVE node's own trusted local state who it is."""

    result = _command(
        runner,
        ("pvesh", "get", "/cluster/status", "--output-format", "json"),
        max_output=1 * 1024 * 1024,
    )
    if result.timed_out or result.output_exceeded or result.returncode != 0:
        raise HealthError("execution_failed", "could not read local PVE cluster status")
    try:
        rows = json.loads(result.stdout.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise HealthError(
            "execution_failed", "local PVE cluster status was malformed"
        ) from exc
    if not isinstance(rows, list):
        raise HealthError("execution_failed", "local PVE cluster status was malformed")
    local_nodes = [
        row.get("name")
        for row in rows
        if isinstance(row, Mapping)
        and row.get("type") == "node"
        and row.get("local") in (1, True)
    ]
    if (
        len(local_nodes) != 1
        or not isinstance(local_nodes[0], str)
        or not NODE_RE.fullmatch(local_nodes[0])
    ):
        raise HealthError("execution_failed", "local PVE node identity is ambiguous")
    return local_nodes[0]


def revalidate_live_target(
    runner: Runner,
    vmid: int,
    expected_node: str,
    *,
    timeout: float = COMMAND_TIMEOUT_SECONDS,
) -> None:
    """Independently prove the live PVE facts before touching the guest.

    The backend proves it still names the intended resource INCARNATION; only
    the host can prove the live PVE target. A VMID is an execution locator,
    not an identity: PVE can free one and reuse it at any moment, and a health
    verdict recorded against a replacement guest would be a false statement
    about a workload this job never updated -- read-only or not.

    ``timeout`` is bounded by the caller to whatever settling budget actually
    remains (frozen rule: never start an operation whose own timeout could
    exceed the remaining evaluation budget) -- see `_clamped_command_timeout`.
    """

    result = _command(
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
        timeout=timeout,
        max_output=4 * 1024 * 1024,
    )
    if result.timed_out or result.output_exceeded or result.returncode != 0:
        raise HealthError("execution_failed", "could not read current PVE target state")
    try:
        rows = json.loads(result.stdout.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise HealthError(
            "execution_failed", "current PVE target state was malformed"
        ) from exc
    if not isinstance(rows, list):
        raise HealthError("execution_failed", "current PVE target state was malformed")
    matches = [
        row for row in rows if isinstance(row, Mapping) and row.get("vmid") == vmid
    ]
    if len(matches) != 1:
        raise HealthError("guest_unavailable", "guest is missing or unavailable")
    row = matches[0]
    if row.get("type") != "lxc":
        raise HealthError(
            "unsupported_resource_type", "current PVE resource is not an LXC guest"
        )
    if row.get("node") != expected_node:
        raise HealthError("stale_target", "guest node changed during health evaluation")
    if row.get("status") != "running":
        raise HealthError("guest_unavailable", "guest is not running")


def _run_guest_command(
    runner: Runner,
    vmid: int,
    expected_node: str,
    local_node: str,
    tail: tuple[str, ...],
    *,
    data_arguments: Sequence[str] = (),
    max_output: int = MAX_COMMAND_OUTPUT_BYTES,
    timeout: float = COMMAND_TIMEOUT_SECONDS,
) -> CommandResult:
    """Run one fixed ``pct exec`` shape on the node that currently holds it.

    **This dispatcher owns the live-target invariant**, exactly as the
    mutation helper's does: every single guest command -- one batched Docker
    or systemd call per round, never one call per probe -- is preceded here
    by its own fresh :func:`revalidate_live_target`, so no caller can amortize
    one check across two rounds and send a later one to a replacement guest.

    ``tail`` is a fixed argv shape built by this file, with zero or more
    elements that came from the request -- probe targets that have already
    passed their kind-specific execution-time validation. A non-local guest
    is routed to its expected cluster member over root's existing
    passwordless inter-node SSH trust Proxmox itself provisions, exactly as
    the scan, execution, and mutation helpers do; no new Hubinet credential
    exists on that node. Unlike those helpers, this one routes elements that
    originated outside the file: ``data_arguments`` names them, and each is
    proved shell-inert before it may cross that boundary. Every other element
    is a constant this file owns.

    ``timeout`` bounds BOTH the revalidation call and the guest command
    itself -- the caller has already clamped it to what remains of the
    bounded settling budget (`_clamped_command_timeout`), so neither can run
    long enough by itself to blow through that budget.
    """

    revalidate_live_target(runner, vmid, expected_node, timeout=timeout)
    inner = ("pct", "exec", str(vmid), "--", *tail)
    if expected_node == local_node:
        result = _command(runner, inner, timeout=timeout, max_output=max_output)
    else:
        # Routing to another cluster member is the ONE place a command line
        # exists rather than an argv list, because that is what ssh hands the
        # remote login shell.
        #
        # Shell quoting is deliberately NOT the mechanism that makes the
        # caller's targets safe here. The kind-specific validation already
        # restricts a target to characters a shell reads as nothing at all,
        # and this makes that a CHECKED property rather than a claim: if any
        # request-derived element would need a quote adding, it is not what
        # this file believes it is, and the whole batched command is reported
        # unevaluable instead of being handed to a shell that might read it.
        #
        # The constants around it -- notably the Docker `--format` template,
        # whose braces this file owns -- are quoted normally. Their content is
        # fixed and reviewed; the caller's is not, and only the caller's is
        # subject to this rule.
        for data_argument in data_arguments:
            if shlex.quote(data_argument) != data_argument:
                raise ProbeUnknown("probe_target_not_exact")
        argv = (
            "ssh",
            "-T",
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=yes",
            f"root@{expected_node}",
            shlex.join(inner),
        )
        result = _command(runner, argv, timeout=timeout, max_output=max_output)
    if result.returncode == 255:
        raise ProbeUnknown("guest_unavailable")
    return result


def _decode(result: CommandResult) -> str:
    if result.timed_out:
        raise ProbeUnknown("command_timed_out")
    if result.output_exceeded:
        raise ProbeUnknown("malformed_output")
    try:
        return result.stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProbeUnknown("malformed_output") from exc


# ---------------------------------------------------------------------------
# Structural, host-independent target validation. Computed ONCE, never
# per-round: a target's charset either names one exact object or it never
# will, no matter how many times the guest is asked.
# ---------------------------------------------------------------------------


def _require_exact_systemd_unit(target: str) -> str:
    if not SYSTEMD_UNIT_RE.fullmatch(target) or target.startswith("-"):
        raise ProbeUnknown("probe_target_not_exact")
    if not target.endswith(SYSTEMD_UNIT_SUFFIXES):
        raise ProbeUnknown("probe_target_not_exact")
    return target


def _require_exact_docker_name(target: str) -> str:
    if not DOCKER_NAME_RE.fullmatch(target) or target.startswith("-"):
        raise ProbeUnknown("probe_target_not_exact")
    return target


def _guest_family_reason(exc: ProbeUnknown | HealthError) -> str:
    """Map a whole-family guest-command failure onto its bounded reason.

    ``HealthError`` here means the LIVE-TARGET REVALIDATION that precedes
    every guest command failed -- the guest went away, moved node, or
    stopped mid-round. That is never a failure of the workload the operator
    declared, so it is reported exactly like any other unevaluable family,
    never raised past this round.
    """

    if isinstance(exc, ProbeUnknown):
        return exc.reason
    return (
        "guest_unavailable"
        if exc.classification in ("guest_unavailable", "stale_target")
        else "command_failed"
    )


def _structural_probe_outcome(kind: str, target: str) -> tuple[str, str] | None:
    """A fixed, round-independent (outcome, reason) if the target can never
    settle, else ``None`` meaning "ask the guest"."""

    try:
        if kind == "systemd_unit_active":
            _require_exact_systemd_unit(target)
        else:
            _require_exact_docker_name(target)
    except ProbeUnknown as exc:
        return "unknown", exc.reason
    return None


# ---------------------------------------------------------------------------
# One batched round: at most one systemd command and at most two Docker
# commands, whatever the probe count.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _RoundBudget:
    """Bounded guest-command accounting across the WHOLE settling window.

    A plain counter, not an enforcement point: the settling loop is what
    stops issuing new rounds once ``used >= MAX_GUEST_COMMANDS`` (checked
    between rounds, never mid-round), so this never needs to raise out of a
    round already in progress.
    """

    used: int = 0

    def spend(self) -> None:
        self.used += 1


def _systemd_round(
    runner: Runner,
    vmid: int,
    expected_node: str,
    local_node: str,
    targets: Sequence[str],
    budget: _RoundBudget,
    *,
    remaining_budget: float,
) -> dict[str, tuple[str, str]]:
    """One batched ``systemctl show`` covering every requested unit.

    Returns ``{target: (outcome, reason)}`` for exactly the requested
    targets, mapped BY POSITION -- never by the returned ``Id``, because
    verified alias behaviour (``ssh.service``/``sshd.service``) means two
    distinct requested targets can report the identical ``Id``.

    ``remaining_budget`` is what is ACTUALLY left of the bounded settling
    deadline; the guest command this issues is clamped to it and can never
    run long enough by itself to blow through that deadline.
    """

    if not targets:
        return {}
    budget.spend()
    try:
        result = _run_guest_command(
            runner,
            vmid,
            expected_node,
            local_node,
            (
                "env",
                "LC_ALL=C",
                "systemctl",
                "show",
                "--no-pager",
                "--property=Id",
                "--property=LoadState",
                "--property=ActiveState",
                "--property=Job",
                "--",
                *targets,
            ),
            data_arguments=targets,
            max_output=64 * 1024 * max(1, len(targets)),
            timeout=_clamped_command_timeout(remaining_budget),
        )
    except (ProbeUnknown, HealthError) as exc:
        reason = _guest_family_reason(exc)
        return {target: ("unknown", reason) for target in targets}
    if result.timed_out:
        return {target: ("unknown", "command_timed_out") for target in targets}
    if result.output_exceeded:
        return {target: ("unknown", "malformed_output") for target in targets}
    if result.returncode != 0:
        # systemctl show succeeds even for a unit that does not exist, so a
        # non-zero exit means the command itself could not run -- no systemd
        # in the guest, a broken bus, a permission problem. Never a verdict.
        return {target: ("unknown", "command_failed") for target in targets}
    try:
        stdout = result.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return {target: ("unknown", "malformed_output") for target in targets}
    blocks = [block for block in stdout.strip().split("\n\n") if block.strip()]
    if len(blocks) != len(targets):
        # A mismatched block count means the batched answer cannot be
        # trusted to map positionally at all -- never guess which target a
        # stray or missing block belongs to.
        reason = "probe_target_ambiguous" if len(blocks) > len(targets) else (
            "malformed_output"
        )
        return {target: ("unknown", reason) for target in targets}
    outcomes: dict[str, tuple[str, str]] = {}
    for target, block in zip(targets, blocks, strict=True):
        properties: dict[str, str] = {}
        malformed = False
        for line in block.splitlines():
            if "=" not in line:
                malformed = True
                break
            key, value = line.split("=", 1)
            if key in properties:
                malformed = True
                break
            properties[key] = value
        if malformed or set(properties) != {"Id", "LoadState", "ActiveState", "Job"}:
            outcomes[target] = ("unknown", "malformed_output")
            continue
        outcomes[target] = _classify_systemd_active_state(
            properties["ActiveState"], properties["Job"]
        )
    return outcomes


def _classify_systemd_active_state(active_state: str, job: str) -> tuple[str, str]:
    if active_state == "active":
        return "passed", "unit_active"
    if active_state in _SYSTEMD_TRANSIENT_REASONS:
        return "unknown", _SYSTEMD_TRANSIENT_REASONS[active_state]
    if active_state in SYSTEMD_JOB_DEPENDENT_STATES:
        if job.strip():
            return "unknown", "unit_job_pending"
        # Includes LoadState=not-found, which systemd reports as a normal
        # ActiveState=inactive: a unit that does not exist is definitively
        # not active, and the operator said it must be.
        return "failed", "unit_not_active"
    if active_state == "maintenance":
        return "failed", "unit_not_active"
    return "unknown", "malformed_output"


def _docker_daemon_names(
    runner: Runner,
    vmid: int,
    expected_node: str,
    local_node: str,
    budget: _RoundBudget,
    *,
    remaining_budget: float,
) -> frozenset[str] | tuple[str, str]:
    """The fixed daemon oracle: every existing container name, or a family
    (outcome, reason) if the daemon could not be read this round."""

    budget.spend()
    try:
        result = _run_guest_command(
            runner,
            vmid,
            expected_node,
            local_node,
            (
                "env",
                "LC_ALL=C",
                "docker",
                "ps",
                "--all",
                "--no-trunc",
                "--format",
                DOCKER_NAME_LIST_FORMAT,
            ),
            max_output=1024 * 1024,
            timeout=_clamped_command_timeout(remaining_budget),
        )
    except (ProbeUnknown, HealthError) as exc:
        return "unknown", _guest_family_reason(exc)
    if result.timed_out:
        return "unknown", "command_timed_out"
    if result.output_exceeded:
        return "unknown", "malformed_output"
    if result.returncode != 0:
        return "unknown", "docker_daemon_unavailable"
    try:
        stdout = result.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return "unknown", "malformed_output"
    names: list[str] = []
    for line in stdout.splitlines():
        try:
            listed = json.loads(line)
        except (TypeError, ValueError):
            return "unknown", "malformed_output"
        if not isinstance(listed, str) or not listed:
            return "unknown", "malformed_output"
        names.append(listed)
    return frozenset(names)


def _docker_inspect_batch(
    runner: Runner,
    vmid: int,
    expected_node: str,
    local_node: str,
    targets: Sequence[str],
    budget: _RoundBudget,
    *,
    remaining_budget: float,
) -> dict[str, tuple[str, str, str]] | tuple[str, str]:
    """One batched ``docker inspect``. Returns ``{name: (status, restarting,
    health)}`` for every target it could read, mapped BY NAME -- never by
    position, because a missing target among several does not shift the
    others -- or a family (outcome, reason) if the command itself failed."""

    if not targets:
        return {}
    budget.spend()
    try:
        result = _run_guest_command(
            runner,
            vmid,
            expected_node,
            local_node,
            (
                "env",
                "LC_ALL=C",
                "docker",
                "inspect",
                "--type",
                "container",
                DOCKER_INSPECT_FORMAT_FLAG,
                DOCKER_INSPECT_FORMAT,
                "--",
                *targets,
            ),
            data_arguments=targets,
            max_output=64 * 1024 * max(1, len(targets)),
            timeout=_clamped_command_timeout(remaining_budget),
        )
    except (ProbeUnknown, HealthError) as exc:
        return "unknown", _guest_family_reason(exc)
    if result.timed_out:
        return "unknown", "command_timed_out"
    if result.output_exceeded:
        return "unknown", "malformed_output"
    try:
        stdout = result.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return "unknown", "malformed_output"
    parsed: dict[str, tuple[str, str, str]] = {}
    for line in stdout.splitlines():
        if not line.strip():
            continue
        fields = line.split("\t")
        if len(fields) != 4:
            continue
        name, status, restarting, health = fields
        if not name.startswith("/"):
            continue
        if restarting not in ("true", "false"):
            continue
        # `docker inspect` reports the container's own name with a leading
        # '/'. Requiring the stripped form to be a target we asked about is
        # what stops a hex-looking target passing because it happened to
        # prefix some OTHER container's id under ID-prefix resolution.
        parsed[name[1:]] = (status, restarting, health)
    if result.returncode != 0 and not parsed:
        return "unknown", "command_failed"
    return parsed


def _classify_docker_container(
    kind: str, status: str, restarting: str, health: str
) -> tuple[str, str]:
    if restarting == "true" or status == "restarting":
        return "unknown", "container_restarting"
    if status in DOCKER_TRANSIENT_STATUSES:
        return "unknown", _DOCKER_TRANSIENT_REASONS[status]
    if status in DOCKER_DEFINITIVE_FAIL_STATUSES:
        return "failed", "container_not_running"
    if status != "running":
        return "unknown", "malformed_output"
    if kind == "docker_container_running":
        return "passed", "container_running"
    # docker_container_healthy: running is necessary but not sufficient.
    if health == "healthy":
        return "passed", "container_healthy"
    if health == "unhealthy":
        return "failed", "container_unhealthy"
    if health == "starting":
        return "unknown", "container_health_starting"
    if health == "<none>":
        return "failed", "container_has_no_healthcheck"
    return "unknown", "malformed_output"


def _docker_round(
    runner: Runner,
    vmid: int,
    expected_node: str,
    local_node: str,
    probes: Sequence[dict[str, Any]],
    budget: _RoundBudget,
    *,
    remaining_budget: float,
) -> dict[int, tuple[str, str]]:
    """One round's worth of Docker probes: at most one ``docker ps`` and one
    ``docker inspect``, batched across every Docker target regardless of how
    many probes -- of either Docker kind -- request it."""

    if not probes:
        return {}
    targets = sorted({str(probe["target"]) for probe in probes})
    daemon_names = _docker_daemon_names(
        runner,
        vmid,
        expected_node,
        local_node,
        budget,
        remaining_budget=remaining_budget,
    )
    if isinstance(daemon_names, tuple):
        outcome, reason = daemon_names
        return {probe["index"]: (outcome, reason) for probe in probes}
    present = [target for target in targets if target in daemon_names]
    absent = {target for target in targets if target not in daemon_names}
    inspected = _docker_inspect_batch(
        runner,
        vmid,
        expected_node,
        local_node,
        present,
        budget,
        remaining_budget=remaining_budget,
    )
    if isinstance(inspected, tuple):
        family_outcome, family_reason = inspected
        inspected_by_name: dict[str, tuple[str, str, str]] = {}
    else:
        family_outcome = family_reason = None
        inspected_by_name = inspected
    results: dict[int, tuple[str, str]] = {}
    for probe in probes:
        target = str(probe["target"])
        index = int(probe["index"])
        if target in absent:
            results[index] = ("failed", "container_absent")
            continue
        record = inspected_by_name.get(target)
        if record is None:
            if family_outcome is not None:
                results[index] = (family_outcome, family_reason or "command_failed")
            else:
                # The daemon oracle proved this name exists a moment ago, but
                # the batched inspect could not read it cleanly this round --
                # a race or a transient glitch, never absence.
                results[index] = ("unknown", "command_failed")
            continue
        status, restarting, health = record
        results[index] = _classify_docker_container(
            str(probe["kind"]), status, restarting, health
        )
    return results


#: The one fixed, code-owned, read-only guest liveness operation
#: `guest_operational` performs. An absolute path (never resolved through a
#: guest `PATH`), and never built from, or carrying, anything the operator
#: supplied -- there is no argument at all. `/bin/true` is part of every
#: supported Debian/Ubuntu LXC's base `coreutils` install.
GUEST_OPERATIONAL_COMMAND: tuple[str, ...] = ("/bin/true",)


def _guest_operational_round(
    runner: Runner,
    vmid: int,
    expected_node: str,
    local_node: str,
    probes: Sequence[dict[str, Any]],
    budget: _RoundBudget,
    *,
    remaining_budget: float,
) -> dict[int, tuple[str, str]]:
    """The one `guest_operational` probe, if the contract declares one.

    PASS requires the exact current resource context to have just
    revalidated (already proven by `_run_guest_command`'s own invariant)
    AND the fixed operation to exit successfully. Anything else this file
    cannot positively prove otherwise is UNKNOWN -- there is deliberately no
    FAIL outcome here: infrastructure uncertainty about a guest this stage
    cannot positively prove down is never turned into a failure of the
    workload the operator declared (there IS no workload declared; this
    kind names none).
    """

    if not probes:
        return {}
    budget.spend()
    try:
        result = _run_guest_command(
            runner,
            vmid,
            expected_node,
            local_node,
            GUEST_OPERATIONAL_COMMAND,
            max_output=1024,
            timeout=_clamped_command_timeout(remaining_budget),
        )
    except (ProbeUnknown, HealthError) as exc:
        reason = _guest_family_reason(exc)
        return {int(probe["index"]): ("unknown", reason) for probe in probes}
    if result.timed_out:
        outcome = ("unknown", "command_timed_out")
    elif result.output_exceeded:
        outcome = ("unknown", "malformed_output")
    elif result.returncode == 0:
        outcome = ("passed", "guest_operational_confirmed")
    else:
        outcome = ("unknown", "command_failed")
    return {int(probe["index"]): outcome for probe in probes}


def _run_one_round(
    runner: Runner,
    vmid: int,
    expected_node: str,
    local_node: str,
    probes: Sequence[dict[str, Any]],
    budget: _RoundBudget,
    *,
    remaining_budget: float,
) -> dict[int, tuple[str, str]]:
    """Observe EVERY still-live frozen probe in one bounded batched round."""

    systemd_probes = [p for p in probes if p["kind"] == "systemd_unit_active"]
    guest_probes = [p for p in probes if p["kind"] == _GUEST_OPERATIONAL_KIND]
    docker_probes = [
        p
        for p in probes
        if p["kind"] not in ("systemd_unit_active", _GUEST_OPERATIONAL_KIND)
    ]

    results: dict[int, tuple[str, str]] = {}
    if systemd_probes:
        targets = [str(p["target"]) for p in systemd_probes]
        by_target = _systemd_round(
            runner,
            vmid,
            expected_node,
            local_node,
            targets,
            budget,
            remaining_budget=remaining_budget,
        )
        for probe in systemd_probes:
            results[int(probe["index"])] = by_target[str(probe["target"])]
    if docker_probes:
        results.update(
            _docker_round(
                runner,
                vmid,
                expected_node,
                local_node,
                docker_probes,
                budget,
                remaining_budget=remaining_budget,
            )
        )
    if guest_probes:
        results.update(
            _guest_operational_round(
                runner,
                vmid,
                expected_node,
                local_node,
                guest_probes,
                budget,
                remaining_budget=remaining_budget,
            )
        )
    return results


# ---------------------------------------------------------------------------
# Bounded settling: repeat full rounds until a decisive verdict or a bound.
# ---------------------------------------------------------------------------


def evaluate_health_contract_settling(
    runner: Runner,
    vmid: int,
    expected_node: str,
    local_node: str,
    probes: Sequence[dict[str, Any]],
    *,
    settling_deadline_seconds: float = DEFAULT_SETTLING_DEADLINE_SECONDS,
    observation_interval_seconds: float = DEFAULT_OBSERVATION_INTERVAL_SECONDS,
    monotonic: Clock = time.monotonic,
    sleep: Sleeper = time.sleep,
) -> dict[str, Any]:
    """Drive one bounded settling window over the complete frozen probe set.

    Returns a dict with ``probes`` (index -> (outcome, reason)), ``rounds``,
    ``settled_seconds``, ``last_round_span_ms``, and ``decisive`` (bool).
    Every terminal decision -- PASS, FAIL, or a deadline/bound UNKNOWN -- uses
    exactly ONE round's complete observation set; evidence from different
    rounds is never merged to manufacture a verdict (the required regression:
    an earlier PASS contributes no terminal authority once a later round
    observes FAIL).

    ``decisive`` is the explicit, typed carrier of "may this be aggregated
    into a durable verdict at all" -- the caller (`handle_request`) echoes it
    on the wire as ``evaluation_status``, and the backend orchestrator
    (`app/package_update_health.py`) must check it BEFORE ever aggregating
    probe outcomes into PASSED/FAILED. Inferring decisiveness a second time
    from the probe outcomes alone -- in the backend, or in Home Assistant --
    would silently let a non-decisive round's last observation (e.g. one
    probe FAILED, one still transient, because the deadline hit mid-round)
    become a false durable verdict.

    ``settling_deadline_seconds``/``observation_interval_seconds`` are the
    caller's ALREADY-CLAMPED policy (`_validate_settling_policy`) -- this
    function trusts them as given rather than re-clamping, exactly as it
    trusts every other already-validated field in ``probes``.
    """

    # Structural target problems are round-independent: fixed forever, and
    # never worth spending a guest command on. A decisive round requires
    # EVERY probe resolved with no transient state; a structurally broken
    # target is unconditionally "unknown" every time it is looked at, so no
    # amount of waiting can ever make a round containing one decisive. There
    # is therefore nothing to gain by holding the whole evaluation open for
    # the settling deadline -- it returns immediately, spending no sleep and
    # no guest command on the probe(s) that can never resolve.
    structural: dict[int, tuple[str, str]] = {}
    live_probes: list[dict[str, Any]] = []
    for probe in probes:
        outcome = _structural_probe_outcome(str(probe["kind"]), str(probe["target"]))
        if outcome is None:
            live_probes.append(probe)
        else:
            structural[int(probe["index"])] = outcome

    start = monotonic()
    absolute_deadline = start + settling_deadline_seconds
    budget = _RoundBudget()

    if not live_probes:
        # Every probe is structural (or the contract is somehow empty of
        # live probes): immediate, zero guest commands, never decisive.
        return {
            "probes": dict(structural),
            "rounds": 0,
            "settled_seconds": monotonic() - start,
            "last_round_span_ms": 0,
            "decisive": False,
        }

    if structural:
        # A decisive round is unreachable BY CONSTRUCTION (see above), so
        # continuing to loop for the OTHER, otherwise-live probes cannot
        # ever produce a verdict either -- one honest observation round for
        # them, then stop immediately rather than waiting out the deadline
        # for something time cannot resolve.
        remaining = max(0.0, absolute_deadline - monotonic())
        round_start = monotonic()
        fresh = _run_one_round(
            runner,
            vmid,
            expected_node,
            local_node,
            live_probes,
            budget,
            remaining_budget=remaining,
        )
        round_span_ms = int((monotonic() - round_start) * 1000)
        return {
            "probes": {**structural, **fresh},
            "rounds": 1,
            "settled_seconds": monotonic() - start,
            "last_round_span_ms": round_span_ms,
            "decisive": False,
        }

    round_index = 0
    last_results: dict[int, tuple[str, str]] = {}
    last_round_span_ms = 0
    decisive = False

    while True:
        # Never start a new round once the absolute deadline has passed --
        # checked BEFORE any work for this round, including immediately
        # after a sleep. Round 1 always runs regardless (there is otherwise
        # no observation to report at all).
        if round_index > 0 and monotonic() >= absolute_deadline:
            break
        round_index += 1
        remaining = max(0.0, absolute_deadline - monotonic())
        round_start = monotonic()
        fresh = _run_one_round(
            runner,
            vmid,
            expected_node,
            local_node,
            live_probes,
            budget,
            remaining_budget=remaining,
        )
        round_span = monotonic() - round_start
        last_round_span_ms = int(round_span * 1000)
        last_results = fresh

        any_transient = any(outcome == "unknown" for outcome, _ in last_results.values())
        decisive = (
            round_span <= MAX_ROUND_SPAN_SECONDS
            and round_index >= MIN_DECISIVE_ROUND
            and not any_transient
        )
        if decisive:
            break
        if round_index >= MAX_SETTLING_ROUNDS or budget.used >= MAX_GUEST_COMMANDS:
            break
        remaining_after = absolute_deadline - monotonic()
        if remaining_after <= 0:
            break
        sleep(min(observation_interval_seconds, remaining_after))

    return {
        "probes": last_results,
        "rounds": round_index,
        "settled_seconds": monotonic() - start,
        "last_round_span_ms": last_round_span_ms,
        "decisive": decisive,
    }


# ---------------------------------------------------------------------------
# discover_health_candidates -- the SECOND typed read-only operation on this
# SAME dedicated boundary/helper/key (v20, post-Human1 Stage 3B). Ephemeral:
# creates no authority, persists nothing, and accepts no operator-supplied
# workload name, command, or selector -- only the resource's own current
# typed context. Supported adapters: docker, systemd, guest (a fallback, not
# a fourth workload adapter).
# ---------------------------------------------------------------------------

MAX_DOCKER_DISCOVERY_NAMES = 64
MAX_SYSTEMD_DISCOVERY_UNITS = 128
MAX_DISCOVERY_CANDIDATES = 128
MAX_RECOMMENDED_PROBES = 32
MAX_DISCOVERY_RESPONSE_BYTES = 64 * 1024
DISCOVERY_DEADLINE_SECONDS = 60.0

_DOCKER_DISCOVERY_INSPECT_FORMAT = (
    "{{.Name}}\t{{.State.Status}}\t"
    "{{if .Config.Healthcheck}}healthcheck{{else}}none{{end}}"
)

#: Exact code-owned exclusions -- never a distro-specific deny-list grown ad
#: hoc. A PACKAGE_UNIT origin never demotes a candidate by itself; only
#: these small, structural, name-based rules do.
_SYSTEMD_RUNTIME_UNITS = frozenset(
    {"docker.service", "containerd.service", "podman.service", "cri-o.service"}
)
_SYSTEMD_PLATFORM_UNITS = frozenset(
    {
        "ssh.service",
        "sshd.service",
        "cron.service",
        "dbus.service",
        "rsyslog.service",
        "qemu-guest-agent.service",
        "unattended-upgrades.service",
    }
)
_SYSTEMD_PLATFORM_PREFIXES = ("systemd-", "getty@", "getty.", "serial-getty@")

#: `list-unit-files` states this discovery considers as candidate SOURCES.
#: `static`/`masked`/`alias`/`generated`/`transient` are not operator-facing
#: enable/disable choices and are excluded -- an "alias" entry in particular
#: would only ever re-list a unit already reachable under its canonical name.
_SYSTEMD_DISCOVERY_UNIT_FILE_STATES = frozenset(
    {"enabled", "enabled-runtime", "disabled"}
)


def _systemd_discovery_role_hint(unit: str) -> str:
    if unit in _SYSTEMD_RUNTIME_UNITS:
        return "runtime"
    if unit in _SYSTEMD_PLATFORM_UNITS or unit.startswith(_SYSTEMD_PLATFORM_PREFIXES):
        return "platform"
    return "workload_candidate"


def _systemd_discovery_origin(fragment_path: str) -> str:
    if not fragment_path:
        return "unknown_origin"
    if fragment_path.startswith(
        ("/etc/systemd/system/", "/usr/local/lib/systemd/system/")
    ):
        return "local_unit"
    if fragment_path.startswith(("/lib/systemd/system/", "/usr/lib/systemd/system/")):
        return "package_unit"
    if fragment_path.startswith("/run/systemd/"):
        return "generated"
    return "unknown_origin"


def _discover_docker_candidates(
    runner: Runner,
    vmid: int,
    expected_node: str,
    local_node: str,
    *,
    remaining_budget: float,
) -> tuple[list[dict[str, Any]], str | None]:
    """Returns ``(candidates, undecided_status)``. ``undecided_status`` is
    ``None`` on a truthful, complete read (zero candidates included, since a
    guest can legitimately have no Docker containers at all); otherwise one
    of the bounded discovery statuses this family could not get past.
    """

    try:
        result = _run_guest_command(
            runner,
            vmid,
            expected_node,
            local_node,
            (
                "env",
                "LC_ALL=C",
                "docker",
                "ps",
                "--all",
                "--no-trunc",
                "--format",
                DOCKER_NAME_LIST_FORMAT,
            ),
            max_output=1024 * 1024,
            timeout=_clamped_command_timeout(remaining_budget),
        )
    except (ProbeUnknown, HealthError):
        return [], "undecidable"
    if result.timed_out or result.output_exceeded or result.returncode != 0:
        # Never read "the daemon did not answer" as "there is no Docker" --
        # that would be exactly the uncertainty-as-absence the frozen
        # architecture forbids.
        return [], "undecidable"
    names: list[str] = []
    try:
        stdout = result.stdout.decode("utf-8")
        for line in stdout.splitlines():
            if not line.strip():
                continue
            listed = json.loads(line)
            if not isinstance(listed, str) or not listed:
                return [], "undecidable"
            names.append(listed)
    except (UnicodeDecodeError, ValueError):
        return [], "undecidable"
    if not names:
        return [], None
    if len(names) > MAX_DOCKER_DISCOVERY_NAMES:
        return [], "too_many_candidates"

    try:
        inspected = _run_guest_command(
            runner,
            vmid,
            expected_node,
            local_node,
            (
                "env",
                "LC_ALL=C",
                "docker",
                "inspect",
                "--type",
                "container",
                "--format",
                _DOCKER_DISCOVERY_INSPECT_FORMAT,
                "--",
                *names,
            ),
            data_arguments=names,
            max_output=64 * 1024 * len(names),
            timeout=_clamped_command_timeout(remaining_budget),
        )
    except (ProbeUnknown, HealthError):
        return [], "undecidable"
    if inspected.timed_out or inspected.output_exceeded:
        return [], "undecidable"

    candidates: list[dict[str, Any]] = []
    try:
        stdout = inspected.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return [], "undecidable"
    for line in stdout.splitlines():
        if not line.strip():
            continue
        fields = line.split("\t")
        if len(fields) != 3 or not fields[0].startswith("/"):
            continue
        name, status, healthcheck = fields[0][1:], fields[1], fields[2]
        if name not in names:
            continue
        has_healthcheck = healthcheck == "healthcheck"
        candidates.append(
            {
                "adapter": "docker",
                "kind": (
                    "docker_container_healthy"
                    if has_healthcheck
                    else "docker_container_running"
                ),
                "target": name,
                "observed_state": status if status else "unknown",
                "origin": None,
                "role_hint": "workload_candidate",
                "recommended": False,
                "rationale": (
                    "docker_healthcheck_present"
                    if has_healthcheck
                    else "docker_container_exists"
                ),
            }
        )
    return candidates, None


def _discover_systemd_candidates(
    runner: Runner,
    vmid: int,
    expected_node: str,
    local_node: str,
    *,
    remaining_budget: float,
) -> tuple[list[dict[str, Any]], str | None]:
    """Returns ``(candidates, undecided_status)``, exactly like the Docker
    family above."""

    try:
        listed = _run_guest_command(
            runner,
            vmid,
            expected_node,
            local_node,
            (
                "env",
                "LC_ALL=C",
                "systemctl",
                "list-unit-files",
                "--type=service",
                "--no-legend",
                "--no-pager",
            ),
            max_output=1024 * 1024,
            timeout=_clamped_command_timeout(remaining_budget),
        )
        failed = _run_guest_command(
            runner,
            vmid,
            expected_node,
            local_node,
            (
                "env",
                "LC_ALL=C",
                "systemctl",
                "list-units",
                "--all",
                "--type=service",
                "--state=failed",
                "--no-legend",
                "--no-pager",
            ),
            max_output=1024 * 1024,
            timeout=_clamped_command_timeout(remaining_budget),
        )
    except (ProbeUnknown, HealthError):
        return [], "undecidable"
    if listed.timed_out or listed.output_exceeded or listed.returncode != 0:
        return [], "undecidable"
    if failed.timed_out or failed.output_exceeded or failed.returncode != 0:
        return [], "undecidable"

    units: list[str] = []
    seen: set[str] = set()
    try:
        for line in listed.stdout.decode("utf-8").splitlines():
            parts = line.split()
            if len(parts) < 2:
                continue
            unit, state = parts[0], parts[1]
            if (
                state in _SYSTEMD_DISCOVERY_UNIT_FILE_STATES
                and SYSTEMD_UNIT_RE.fullmatch(unit)
                and unit.endswith(SYSTEMD_UNIT_SUFFIXES)
                and "@" not in unit
                and unit not in seen
            ):
                units.append(unit)
                seen.add(unit)
        for line in failed.stdout.decode("utf-8").splitlines():
            parts = line.split()
            if not parts:
                continue
            unit = parts[0].lstrip("●").strip()
            if (
                SYSTEMD_UNIT_RE.fullmatch(unit)
                and unit.endswith(SYSTEMD_UNIT_SUFFIXES)
                and "@" not in unit
                and unit not in seen
            ):
                units.append(unit)
                seen.add(unit)
    except UnicodeDecodeError:
        return [], "undecidable"

    if not units:
        return [], None
    if len(units) > MAX_SYSTEMD_DISCOVERY_UNITS:
        return [], "too_many_candidates"

    try:
        result = _run_guest_command(
            runner,
            vmid,
            expected_node,
            local_node,
            (
                "env",
                "LC_ALL=C",
                "systemctl",
                "show",
                "--no-pager",
                "--property=Id",
                "--property=LoadState",
                "--property=ActiveState",
                "--property=UnitFileState",
                "--property=FragmentPath",
                "--",
                *units,
            ),
            data_arguments=units,
            max_output=64 * 1024 * len(units),
            timeout=_clamped_command_timeout(remaining_budget),
        )
    except (ProbeUnknown, HealthError):
        return [], "undecidable"
    if result.timed_out or result.output_exceeded:
        return [], "undecidable"
    try:
        stdout = result.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return [], "undecidable"
    blocks = [block for block in stdout.strip().split("\n\n") if block.strip()]
    if len(blocks) != len(units):
        return [], "undecidable"

    candidates: list[dict[str, Any]] = []
    for unit, block in zip(units, blocks, strict=True):
        properties: dict[str, str] = {}
        for line in block.splitlines():
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            properties.setdefault(key, value)
        if properties.get("LoadState") != "loaded":
            continue
        fragment_path = properties.get("FragmentPath", "")
        origin = _systemd_discovery_origin(fragment_path)
        # The requested unit and the answer's own Id can differ for an
        # alias (verified: ssh.service/sshd.service share one Id) -- report
        # it under the name actually requested, but flag the origin.
        if properties.get("Id") and properties["Id"] != unit:
            origin = "alias"
        candidates.append(
            {
                "adapter": "systemd",
                "kind": "systemd_unit_active",
                "target": unit,
                "observed_state": properties.get("ActiveState", "unknown"),
                "origin": origin,
                "role_hint": _systemd_discovery_role_hint(unit),
                "recommended": False,
                "rationale": "systemd_unit_file_present",
            }
        )
    return candidates, None


def discover_health_candidates(
    runner: Runner,
    vmid: int,
    expected_node: str,
    local_node: str,
    *,
    monotonic: Clock = time.monotonic,
) -> dict[str, Any]:
    """One bounded, ephemeral candidate-discovery read. Never persists
    anything; the caller (the backend) is the only place authority for a
    health contract can ever be created, and only via an explicit operator
    confirmation through the existing typed health-contract mutation.
    """

    start = monotonic()
    deadline = start + DISCOVERY_DEADLINE_SECONDS

    docker_candidates, docker_status = _discover_docker_candidates(
        runner,
        vmid,
        expected_node,
        local_node,
        remaining_budget=max(0.0, deadline - monotonic()),
    )
    systemd_candidates, systemd_status = _discover_systemd_candidates(
        runner,
        vmid,
        expected_node,
        local_node,
        remaining_budget=max(0.0, deadline - monotonic()),
    )

    # CRITICAL frozen rule: discovery uncertainty in EITHER family must
    # never be read as "no workload exists" -- it stops here, undecided,
    # and never reaches the guest-fallback recommendation below.
    if docker_status is not None or systemd_status is not None:
        status = next(s for s in (docker_status, systemd_status) if s is not None)
        return {"status": status, "candidates": [], "recommendation_basis": None}

    candidates = docker_candidates + systemd_candidates
    if len(candidates) > MAX_DISCOVERY_CANDIDATES:
        return {
            "status": "too_many_candidates",
            "candidates": [],
            "recommendation_basis": None,
        }

    # Recommendation: backend-owned priority order, computed here (never by
    # Home Assistant, never re-derived from a separate "current health"
    # read -- current unhealthy state must never suppress candidacy).
    docker_healthchecked = [
        c for c in docker_candidates if c["kind"] == "docker_container_healthy"
    ]
    recommendation_basis: str | None = None
    if docker_healthchecked:
        for candidate in docker_healthchecked:
            candidate["recommended"] = True
        recommendation_basis = "docker_healthcheck"
    elif docker_candidates:
        for candidate in docker_candidates:
            candidate["recommended"] = True
        recommendation_basis = "docker_running"
    else:
        workload_candidates = [
            c for c in systemd_candidates if c["role_hint"] == "workload_candidate"
        ]
        if len(workload_candidates) == 1:
            workload_candidates[0]["recommended"] = True
            recommendation_basis = "single_systemd_candidate"
        elif len(workload_candidates) > 1:
            return {
                "status": "ambiguous_candidates",
                "candidates": candidates[:MAX_DISCOVERY_CANDIDATES],
                "recommendation_basis": None,
            }
        else:
            # Positively completed discovery, both families, with NO
            # meaningful supported workload candidate -- Docker or systemd
            # -- found at all. The ONLY circumstance in which the guest
            # fallback may be recommended. Any platform/runtime systemd
            # units still found stay in the response for operator
            # visibility; they are not thrown away, just never recommended.
            candidates.append(
                {
                    "adapter": "guest",
                    "kind": "guest_operational",
                    "target": None,
                    "observed_state": "unknown",
                    "origin": None,
                    "role_hint": "workload_candidate",
                    "recommended": True,
                    "rationale": "guest_fallback",
                }
            )
            recommendation_basis = "guest_fallback"

    recommended_count = sum(1 for c in candidates if c["recommended"])
    if recommended_count > MAX_RECOMMENDED_PROBES:
        # Never silently truncate an ALL-OF recommended set.
        for candidate in candidates:
            candidate["recommended"] = False
        return {
            "status": "too_many_candidates",
            "candidates": candidates[:MAX_DISCOVERY_CANDIDATES],
            "recommendation_basis": None,
        }

    if recommendation_basis == "guest_fallback":
        status = "no_candidates"
    elif candidates:
        status = "ok"
    else:
        status = "no_candidates"
    return {
        "status": status,
        "candidates": candidates[:MAX_DISCOVERY_CANDIDATES],
        "recommendation_basis": recommendation_basis,
    }


def validate_discover_request(payload: Any) -> dict[str, Any]:
    """Accept exactly one request shape for the discovery operation.

    No ``job_id``, no operator-supplied workload name, no command, no
    selector string -- only the resource's own current typed context.
    """

    if not isinstance(payload, Mapping) or set(payload) != {
        "request_version",
        "operation",
        "target",
        "ownership",
    }:
        raise RequestError("request must have the exact discovery shape")
    if (
        payload["request_version"] != 1
        or payload["operation"] != "discover_health_candidates"
    ):
        raise RequestError("unknown host-control operation")

    target = payload["target"]
    if not isinstance(target, Mapping) or set(target) != {"vmid", "expected_node"}:
        raise RequestError("target must have the exact discovery shape")
    vmid = target["vmid"]
    if type(vmid) is not int or not 100 <= vmid <= 999_999_999:
        raise RequestError("vmid must be a valid PVE integer VMID")
    expected_node = target["expected_node"]
    if not isinstance(expected_node, str) or not NODE_RE.fullmatch(expected_node):
        raise RequestError("expected_node is invalid")

    ownership = payload["ownership"]
    if not isinstance(ownership, Mapping) or set(ownership) != {
        "resource_id",
        "binding_id",
        "locator_generation",
        "resource_continuity_revision",
        "backend_instance_id",
    }:
        raise RequestError("ownership must have the exact discovery shape")
    normalized_ownership = {
        "resource_id": _canonical_uuid(ownership["resource_id"], "resource_id"),
        "binding_id": _canonical_uuid(ownership["binding_id"], "binding_id"),
        "locator_generation": _positive_integer(
            ownership["locator_generation"], "locator_generation"
        ),
        "resource_continuity_revision": _positive_integer(
            ownership["resource_continuity_revision"],
            "resource_continuity_revision",
        ),
        "backend_instance_id": _canonical_uuid(
            ownership["backend_instance_id"], "backend_instance_id"
        ),
    }
    return {
        "vmid": vmid,
        "expected_node": expected_node,
        "ownership": normalized_ownership,
    }


def handle_discover_request(
    payload: Any,
    *,
    runner: Runner = _run_bounded,
    monotonic: Clock = time.monotonic,
) -> dict[str, Any]:
    request = validate_discover_request(payload)
    vmid = request["vmid"]
    expected_node = request["expected_node"]
    resource_id = request["ownership"]["resource_id"]
    try:
        local_node = _local_node(runner)
        revalidate_live_target(runner, vmid, expected_node)
    except HealthError as exc:
        classification = (
            "guest_unavailable"
            if exc.classification in ("guest_unavailable", "stale_target")
            else "undecidable"
        )
        return {
            "response_version": 1,
            "ok": True,
            "resource_id": resource_id,
            "discovery_status": classification,
            "candidates": [],
            "recommendation_basis": None,
        }
    discovered = discover_health_candidates(
        runner, vmid, expected_node, local_node, monotonic=monotonic
    )
    return {
        "response_version": 1,
        "ok": True,
        "resource_id": resource_id,
        "discovery_status": discovered["status"],
        "candidates": discovered["candidates"],
        "recommendation_basis": discovered["recommendation_basis"],
    }


def handle_request(
    payload: Any,
    *,
    runner: Runner = _run_bounded,
    monotonic: Clock = time.monotonic,
    sleep: Sleeper = time.sleep,
) -> dict[str, Any]:
    # Dispatch on operation, but ONLY for the exact new operation name --
    # anything else (including a missing/malformed `operation`, or an
    # entirely empty payload) falls straight through to the EXISTING
    # evaluate-request validation below, so the byte-identical bootstrap/
    # updater acceptance marker ("request must have the exact health-
    # evaluation shape") is produced for every payload it was ever produced
    # for before this operation existed.
    if isinstance(payload, Mapping) and payload.get("operation") == (
        "discover_health_candidates"
    ):
        return handle_discover_request(payload, runner=runner, monotonic=monotonic)

    request = validate_request(payload)
    vmid = request["vmid"]
    expected_node = request["expected_node"]
    job_id = request["ownership"]["job_id"]
    try:
        local_node = _local_node(runner)
        revalidate_live_target(runner, vmid, expected_node)
    except HealthError as exc:
        return {
            "response_version": 1,
            "ok": False,
            "job_id": job_id,
            "error": {
                "classification": exc.classification,
                "message": exc.message[:500],
            },
        }

    settled = evaluate_health_contract_settling(
        runner,
        vmid,
        expected_node,
        local_node,
        request["probes"],
        settling_deadline_seconds=request["settling_deadline_seconds"],
        observation_interval_seconds=request["observation_interval_seconds"],
        monotonic=monotonic,
        sleep=sleep,
    )
    by_index: dict[int, tuple[str, str]] = settled["probes"]
    probes: list[dict[str, Any]] = []
    for probe in request["probes"]:
        outcome, reason = by_index[probe["index"]]
        probes.append(
            {
                "index": probe["index"],
                "kind": probe["kind"],
                "target": probe["target"],
                "outcome": outcome,
                "reason": reason,
            }
        )
    return {
        "response_version": 1,
        "ok": True,
        "job_id": job_id,
        # Echoed back verbatim so the backend can prove this answer is about
        # the exact frozen contract generation it asked about.
        "health_contract": {
            "revision": request["revision"],
            "fingerprint": request["fingerprint"],
        },
        "probes": probes,
        # The explicit, typed carrier of "may this be aggregated into a
        # durable verdict at all". The backend MUST check this before ever
        # calling `aggregate_health_outcome` -- never re-infer decisiveness
        # from the probe outcomes alone, in the backend or in Home Assistant.
        "evaluation_status": "decisive" if settled["decisive"] else "unresolved",
        # Bounded settling metadata: never guest output, never a command, and
        # never more than three small integers. Persisted by the backend only
        # alongside a truthful UNKNOWN, never merged into a verdict's proof.
        "settling": {
            "rounds": settled["rounds"],
            "settled_seconds": round(float(settled["settled_seconds"]), 3),
            "last_round_span_ms": settled["last_round_span_ms"],
        },
    }


def main() -> int:
    if os.environ.get("SSH_ORIGINAL_COMMAND"):
        response = {
            "response_version": 1,
            "ok": False,
            "job_id": None,
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
            error = str(exc)[:500] or "malformed health-evaluation request"
    response = {
        "response_version": 1,
        "ok": False,
        "job_id": None,
        "error": {"classification": "execution_failed", "message": error},
    }
    sys.stdout.write(json.dumps(response, ensure_ascii=True, separators=(",", ":")))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
