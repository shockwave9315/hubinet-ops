#!/usr/bin/env python3
"""Forced-command PVE boundary for Hubinet's health-evaluation operations.

**Deployed.** `deploy/lib/bootstrap-update-boundaries.sh` and
`deploy/update-proxmox-0.5.sh` install this file as one of the five
package-update forced-command boundaries (snapshot, plan simulation,
mutation, rollback, health), each behind its own dedicated key and its own
root-owned forced command. It requires no PVE API privilege beyond the
audit-only pair the product already has: it uses host-local `pct exec`, not a
PVE mutation endpoint.

It exposes exactly ONE typed operation: `evaluate_health_contract`, the
durable job-bound verdict path. It is READ-ONLY. It cannot create, delete,
start, stop, snapshot, roll back, upgrade, install, or remove anything, and
there is no path through it that accepts remote command text.

There is deliberately no workload-DISCOVERY operation here. v0.5 has exactly
TWO health contract shapes -- the built-in `guest_operational` baseline, or an
explicit advanced `systemd_unit_active` contract -- and they never mix (see
`validate_request` below). Docker-specific package-update health probes
(`docker_container_running`, `docker_container_healthy`) are not part of v0.5
Hubinet Ops package-update health: they were removed end-to-end, not merely
hidden. Nothing here infers what a guest runs; absence of a workload observer
is not proof of workload absence.

## Two evaluation shapes, never mixed

**The `guest_operational` baseline is a single one-shot execution.** Exactly
one probe, no target: the exact current resource/locator/node context is
revalidated through the existing trusted boundary, then `pct exec <vmid> --
/bin/true` runs exactly ONCE. Exit 0 is a DECISIVE PASS immediately -- no
second confirmation round, no sleep, and no `MAX_ROUND_SPAN_SECONDS` or
`MIN_DECISIVE_ROUND` gate of any kind. A stopped/unavailable guest, a command
timeout, or any host/transport/context uncertainty is UNKNOWN, and stays
UNKNOWN after exactly that one attempt: no automatic retry, no settling
window, no automatic rollback. See `_evaluate_guest_operational_once` and
`ARCHITECTURE.md`, "Job-bound healthcheck execution -- baseline
independence".

**An explicit advanced `systemd_unit_active` contract keeps bounded
settling.** A single `evaluate_health_contract` request may take up to the
backend's requested settling deadline (`DEFAULT_SETTLING_DEADLINE_SECONDS`,
180s by product default; the backend states its policy on the wire, and this
file validates and clamps it against its own hard ceilings before using it --
see below) to answer, because it owns the ENTIRE bounded settling window
described in `ARCHITECTURE.md`, "Job-bound healthcheck execution", not one
instantaneous sample. A declared unit restarts systemd's own state machine on
an ordinary package-triggered restart, and a normal successful update must
not durably fail, or require a manual re-run, merely because that restart has
not finished settling yet.

So, for this contract shape only, the file repeatedly observes the COMPLETE
frozen probe set in bounded ROUNDS, each one batched into ONE `systemctl show`
call -- never one guest command per probe -- until either:

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
transport headroom, and that headroom is now a real bound rather than an
approximate one: ONE absolute monotonic deadline is established in
`handle_request` from the clamped policy BEFORE any subprocess runs, and the
prologue (`_local_node`, the first `revalidate_live_target`) spends that same
budget instead of an unconditional per-command allowance added on top of it
(PR #80 review MINOR-1). The whole call is therefore bounded by the requested
deadline plus `_TRANSPORT_RETURN_MARGIN_SECONDS`, never by
"prologue + deadline". This same absolute deadline also bounds the
`guest_operational` baseline's one command -- it is what the command's own
timeout is computed from -- but the baseline never sleeps or loops within it.

## Why the systemd command is what it is

Every argv below is fixed, and every one was verified against the real tools
rather than assumed. A probe TARGET is data supplied by the operator through
the contract API; it becomes one argv element and never command text, never a
format string, never a template, and never a shell fragment. Shell quoting is
not used as a security mechanism anywhere in this file -- there is no shell.

`systemctl is-active <pattern>` is unusable: verified against systemd 257, it
expands glob patterns and exits 0 if ANY matching unit is active, and an
explicit `--` end-of-options marker does not stop that expansion.
`systemctl is-active 'ssh*'` prints four lines and succeeds. A probe built on
it could pass because some other unit is up, which is exactly the false PASS
this stage refuses to be capable of.

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
    "guest_operational",
)

#: The one kind whose probes carry no target at all -- never a faked one.
_GUEST_OPERATIONAL_KIND = "guest_operational"
_SYSTEMD_UNIT_ACTIVE_KIND = "systemd_unit_active"

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
#: settling systemd, and a same-round coincidence is not enough evidence
#: either way. This bound applies ONLY to the advanced `systemd_unit_active`
#: settling loop below -- the `guest_operational` baseline never enters it at
#: all (see `_evaluate_guest_operational_once`).
MIN_DECISIVE_ROUND = 2
#: A hard structural ceiling on rounds attempted, independent of the wall
#: clock deadline above (defends against a clock that misbehaves).
MAX_SETTLING_ROUNDS = 40
#: At most this many guest commands (`pct exec` invocations) in ONE round of
#: the advanced systemd settling loop, whatever the probe count: one batched
#: `systemctl show` covering every frozen unit -- never one command per
#: probe. There is exactly one family left since Docker health probes and the
#: `guest_operational` baseline's own one-shot check were removed from this
#: loop (v0.5 health scope reduction).
MAX_COMMANDS_PER_ROUND = 1
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
#: The smallest timeout this file will ever actually GRANT a command -- not
#: a floor applied when less is left. Once what remains of the settling
#: budget (after `_TRANSPORT_RETURN_MARGIN_SECONDS`) would compute a smaller
#: value than this, `_next_command_timeout` returns ``None`` instead: a
#: `subprocess` timeout of (near-)zero is not a meaningful bound, and
#: artificially flooring it up to this value would be extending the real
#: deadline while still claiming to enforce it (PR #80 review finding 1).
#: The caller reports the family/probe UNKNOWN/unresolved and never starts
#: the command at all.
_MIN_COMMAND_TIMEOUT_SECONDS = 0.05

Clock = Callable[[], float]
Sleeper = Callable[[float], None]


def _next_command_timeout(
    absolute_deadline: float, *, monotonic: Clock
) -> float | None:
    """The timeout for exactly ONE subprocess invocation about to begin,
    computed FRESH by reading the clock right now -- never a value computed
    before an earlier subprocess in the same call chain consumed real
    wall-clock time.

    PR #80 review finding 1: a single ``remaining_budget`` float computed
    once per round (or once per family) and then reused across several
    sequential subprocess calls -- most concretely, live-target revalidation
    followed by the actual guest command inside one ``_run_guest_command``
    call -- let one logical command path consume roughly TWICE its intended
    share of the settling budget, because the second call still believed the
    budget the first call started with. There is exactly one absolute
    monotonic deadline for an evaluation; this function is the ONLY place
    that may convert "how much of it is left" into a per-command timeout, and it must be called again, immediately, before
    every single subprocess this file issues.

    Returns ``None`` when there is no longer enough real budget left to
    safely start a command at all. The caller must then stop -- report the
    family/probe controlled UNKNOWN/unresolved -- rather than launch one
    with an artificially floored positive timeout that could not reflect a
    genuine remaining budget: a positive floor here would be extending the
    deadline in substance while still claiming to enforce it.
    """

    remaining = absolute_deadline - monotonic()
    usable = remaining - _TRANSPORT_RETURN_MARGIN_SECONDS
    if usable < _MIN_COMMAND_TIMEOUT_SECONDS:
        return None
    return min(COMMAND_TIMEOUT_SECONDS, usable)

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

    # The built-in baseline and an explicit advanced contract never mix
    # (PRODUCT.md, "What healthy means"): a contract is EITHER exactly one
    # `guest_operational` probe OR one-or-more `systemd_unit_active` probes.
    # The backend's own domain validation and SQL schema already refuse to
    # store anything else, but this boundary re-proves it independently
    # rather than trusting the caller -- defense in depth, and what lets the
    # two evaluation shapes below stay structurally distinct rather than one
    # dispatcher silently guessing which rules apply from the probe list.
    kinds = {str(probe["kind"]) for probe in probes}
    if kinds == {_GUEST_OPERATIONAL_KIND}:
        if len(probes) != 1:
            raise RequestError(
                "a guest_operational health contract must declare exactly one probe"
            )
        contract_kind = _GUEST_OPERATIONAL_KIND
    elif kinds == {_SYSTEMD_UNIT_ACTIVE_KIND}:
        contract_kind = _SYSTEMD_UNIT_ACTIVE_KIND
    else:
        raise RequestError(
            "a health contract may not combine guest_operational with any "
            "other probe"
        )

    return {
        "vmid": vmid,
        "expected_node": expected_node,
        "ownership": normalized_ownership,
        "revision": revision,
        "fingerprint": fingerprint,
        "probes": probes,
        "contract_kind": contract_kind,
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


def _require_prologue_timeout(absolute_deadline: float, *, monotonic: Clock) -> float:
    """The timeout for one PROLOGUE subprocess, from the SAME absolute
    deadline the rest of the operation spends.

    PR #80 review MINOR-1: `_local_node` and the operation's first
    `revalidate_live_target` used to run at the unconditional
    ``COMMAND_TIMEOUT_SECONDS`` *before* any deadline existed, so a bounded
    operation's real wall clock was "prologue + deadline", not "deadline".
    One absolute monotonic deadline now governs the whole operation, and the
    prologue is inside it like every other subprocess. Budget exhaustion
    here is fail-closed -- never a verdict, never positive absence.
    """

    timeout = _next_command_timeout(absolute_deadline, monotonic=monotonic)
    if timeout is None:
        raise HealthError(
            "execution_failed",
            "the bounded budget was exhausted before the guest could be read",
        )
    return timeout


def _local_node(
    runner: Runner, *, absolute_deadline: float, monotonic: Clock
) -> str:
    """Ask this PVE node's own trusted local state who it is.

    Bounded by whatever is ACTUALLY left of the one absolute deadline, read
    fresh immediately before the subprocess starts -- never the fixed
    per-command allowance alone.
    """

    result = _command(
        runner,
        ("pvesh", "get", "/cluster/status", "--output-format", "json"),
        timeout=_require_prologue_timeout(absolute_deadline, monotonic=monotonic),
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
    exceed the remaining evaluation budget) -- see `_next_command_timeout`.
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
    absolute_deadline: float,
    monotonic: Clock,
    data_arguments: Sequence[str] = (),
    max_output: int = MAX_COMMAND_OUTPUT_BYTES,
) -> CommandResult:
    """Run one fixed ``pct exec`` shape on the node that currently holds it.

    **This dispatcher owns the live-target invariant**, exactly as the
    mutation helper's does: every single guest command -- one batched
    systemd call per round, or the baseline's one fixed liveness check,
    never one call per probe -- is preceded here by its own fresh
    :func:`revalidate_live_target`, so no caller can amortize one check
    across two rounds and send a later one to a replacement guest.

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

    PR #80 review finding 1: the revalidation call and the actual guest
    command are two SEPARATE subprocess invocations, and each gets its OWN
    timeout computed FRESH, right before it starts
    (`_next_command_timeout(absolute_deadline, monotonic=monotonic)`) --
    never one value computed once and reused for both, which used to let one
    logical command path consume roughly twice its intended share of the
    settling budget. Either computation returning ``None`` (no real budget
    left to safely start a command at all) stops here with
    `ProbeUnknown("settling_budget_exhausted")` rather than launching a
    command with an artificially floored timeout.
    """

    revalidation_timeout = _next_command_timeout(absolute_deadline, monotonic=monotonic)
    if revalidation_timeout is None:
        raise ProbeUnknown("settling_budget_exhausted")
    revalidate_live_target(runner, vmid, expected_node, timeout=revalidation_timeout)

    command_timeout = _next_command_timeout(absolute_deadline, monotonic=monotonic)
    if command_timeout is None:
        raise ProbeUnknown("settling_budget_exhausted")
    inner = ("pct", "exec", str(vmid), "--", *tail)
    if expected_node == local_node:
        result = _command(runner, inner, timeout=command_timeout, max_output=max_output)
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
        # The constants around it -- the fixed `systemctl`/`pct exec` argv
        # this file owns -- are quoted normally. Their content is fixed and
        # reviewed; the caller's is not, and only the caller's is subject to
        # this rule.
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
        result = _command(runner, argv, timeout=command_timeout, max_output=max_output)
    if result.returncode == 255:
        raise ProbeUnknown("guest_unavailable")
    return result


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


def _require_probe_target_string(target: object) -> str:
    """A kind that HAS a target must actually carry a string one.

    Never ``str(target)``: PR #80 review MINOR-2 -- coercing a legitimately
    absent (``None``) target into the literal string ``"None"`` is exactly
    the faked target the frozen design forbids, and it silently made one
    kind's structural classification depend on another kind's charset.
    """

    if not isinstance(target, str):
        raise ProbeUnknown("probe_target_not_exact")
    return target


def _structural_probe_outcome(
    kind: str, target: str | None
) -> tuple[str, str] | None:
    """A fixed, round-independent (outcome, reason) if the target can never
    settle, else ``None`` meaning "ask the guest".

    Only ever called for the advanced `systemd_unit_active` settling loop:
    `validate_request` already refuses a contract mixing `guest_operational`
    with anything else, and the baseline's one-shot evaluation
    (`_evaluate_guest_operational_once`) never enters this loop at all. Exact
    kind branching, never an ``else:`` catch-all that would make an
    unrecognised kind look valid.
    """

    try:
        if kind == "systemd_unit_active":
            _require_exact_systemd_unit(_require_probe_target_string(target))
        else:  # pragma: no cover - `validate_request` refuses unknown kinds
            # Never guessed at, and never silently validated as some other
            # kind's grammar: an unrecognised kind is an answer this file
            # cannot produce, which is UNKNOWN.
            return "unknown", "malformed_output"
    except ProbeUnknown as exc:
        return "unknown", exc.reason
    return None


# ---------------------------------------------------------------------------
# One batched round of the advanced systemd settling loop: at most one
# `systemctl show`, whatever the probe count.
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
    absolute_deadline: float,
    monotonic: Clock,
) -> dict[str, tuple[str, str]]:
    """One batched ``systemctl show`` covering every requested unit.

    Returns ``{target: (outcome, reason)}`` for exactly the requested
    targets, mapped BY POSITION -- never by the returned ``Id``, because
    verified alias behaviour (``ssh.service``/``sshd.service``) means two
    distinct requested targets can report the identical ``Id``.

    ``absolute_deadline``/``monotonic`` are the settling window's single
    reference clock; the guest command this issues gets a timeout computed
    fresh, immediately before it starts, from what is ACTUALLY left of it.
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
            absolute_deadline=absolute_deadline,
            monotonic=monotonic,
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


#: The one fixed, code-owned, read-only guest liveness operation
#: `guest_operational` performs. An absolute path (never resolved through a
#: guest `PATH`), and never built from, or carrying, anything the operator
#: supplied -- there is no argument at all. `/bin/true` is part of every
#: supported Debian/Ubuntu LXC's base `coreutils` install.
GUEST_OPERATIONAL_COMMAND: tuple[str, ...] = ("/bin/true",)


def _evaluate_guest_operational_once(
    runner: Runner,
    vmid: int,
    expected_node: str,
    local_node: str,
    *,
    absolute_deadline: float,
    monotonic: Clock,
) -> tuple[str, str]:
    """The built-in baseline's ONE-SHOT evaluation. No settling, no retry.

    Called exactly once per `evaluate_health_contract` attempt, and never
    from the settling loop below: the frozen architecture requires the
    `guest_operational` baseline to be independent of the advanced settling
    machinery entirely (`ARCHITECTURE.md`, "Job-bound healthcheck execution
    -- baseline independence"). A single successful execution is DECISIVE
    PASS immediately.

    PASS requires the exact current resource context to have just
    revalidated (already proven by `_run_guest_command`'s own invariant)
    AND the fixed operation to exit successfully. Anything else this file
    cannot positively prove otherwise is UNKNOWN -- there is deliberately no
    FAIL outcome here: infrastructure uncertainty about a guest this stage
    cannot positively prove down is never turned into a failure of the
    workload the operator declared (there IS no workload declared; this
    kind names none). No sleep, no second confirmation round, and no
    `MAX_ROUND_SPAN_SECONDS`/`MIN_DECISIVE_ROUND` gate applies here at all --
    those bound the ADVANCED systemd settling loop only.
    """

    try:
        result = _run_guest_command(
            runner,
            vmid,
            expected_node,
            local_node,
            GUEST_OPERATIONAL_COMMAND,
            max_output=1024,
            absolute_deadline=absolute_deadline,
            monotonic=monotonic,
        )
    except (ProbeUnknown, HealthError) as exc:
        return "unknown", _guest_family_reason(exc)
    if result.timed_out:
        return "unknown", "command_timed_out"
    if result.output_exceeded:
        return "unknown", "malformed_output"
    if result.returncode == 0:
        return "passed", "guest_operational_confirmed"
    return "unknown", "command_failed"


def _run_one_systemd_round(
    runner: Runner,
    vmid: int,
    expected_node: str,
    local_node: str,
    probes: Sequence[dict[str, Any]],
    budget: _RoundBudget,
    *,
    absolute_deadline: float,
    monotonic: Clock,
) -> dict[int, tuple[str, str]]:
    """Observe EVERY still-live frozen systemd probe in one batched round.

    Only ever called for an advanced `systemd_unit_active` contract:
    `validate_request` refuses a contract mixing `guest_operational` with
    anything else, so every probe here is `systemd_unit_active`.
    """

    targets = [str(p["target"]) for p in probes]
    by_target = _systemd_round(
        runner,
        vmid,
        expected_node,
        local_node,
        targets,
        budget,
        absolute_deadline=absolute_deadline,
        monotonic=monotonic,
    )
    return {
        int(probe["index"]): by_target[str(probe["target"])] for probe in probes
    }


# ---------------------------------------------------------------------------
# Bounded settling for the advanced systemd contract: repeat full rounds
# until a decisive verdict or a bound. The guest_operational baseline never
# enters this loop at all -- see `_evaluate_guest_operational_once`.
# ---------------------------------------------------------------------------


def evaluate_health_contract_settling(
    runner: Runner,
    vmid: int,
    expected_node: str,
    local_node: str,
    probes: Sequence[dict[str, Any]],
    *,
    absolute_deadline: float,
    observation_interval_seconds: float = DEFAULT_OBSERVATION_INTERVAL_SECONDS,
    monotonic: Clock = time.monotonic,
    sleep: Sleeper = time.sleep,
) -> dict[str, Any]:
    """Drive one bounded settling window over an advanced systemd contract.

    Called ONLY for a `systemd_unit_active` contract -- `validate_request`
    refuses a contract mixing `guest_operational` with anything else, and
    `handle_request` never calls this function at all for the baseline (it
    calls `_evaluate_guest_operational_once` instead, exactly once, with no
    settling). Every probe in ``probes`` here is therefore
    `systemd_unit_active`.

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

    ``absolute_deadline`` is the ONE monotonic deadline for the whole
    evaluation, established by `handle_request` from the caller's
    ALREADY-CLAMPED policy (`_validate_settling_policy`) BEFORE the
    prologue (`_local_node`, the first `revalidate_live_target`) runs. This
    function never starts a fresh window of its own: whatever the prologue
    already spent is spent (PR #80 review MINOR-1).
    ``observation_interval_seconds`` is likewise already clamped -- this
    function trusts both as given, exactly as it trusts every other
    already-validated field in ``probes``.
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
        outcome = _structural_probe_outcome(str(probe["kind"]), probe["target"])
        if outcome is None:
            live_probes.append(probe)
        else:
            structural[int(probe["index"])] = outcome

    start = monotonic()
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
        round_start = monotonic()
        fresh = _run_one_systemd_round(
            runner,
            vmid,
            expected_node,
            local_node,
            live_probes,
            budget,
            absolute_deadline=absolute_deadline,
            monotonic=monotonic,
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
        round_start = monotonic()
        fresh = _run_one_systemd_round(
            runner,
            vmid,
            expected_node,
            local_node,
            live_probes,
            budget,
            absolute_deadline=absolute_deadline,
            monotonic=monotonic,
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


def handle_request(
    payload: Any,
    *,
    runner: Runner = _run_bounded,
    monotonic: Clock = time.monotonic,
    sleep: Sleeper = time.sleep,
) -> dict[str, Any]:
    # ONE operation. There is no dispatch and no second request shape: every
    # payload goes straight to the health-evaluation validation below, which
    # is what produces the bootstrap/updater acceptance marker ("request must
    # have the exact health-evaluation shape") for anything malformed.
    request = validate_request(payload)
    vmid = request["vmid"]
    expected_node = request["expected_node"]
    job_id = request["ownership"]["job_id"]
    # ONE absolute monotonic deadline for the WHOLE evaluation, established
    # from the already-clamped backend policy BEFORE the prologue, so the
    # local-node read and the first live-target revalidation are inside the
    # settling budget rather than added on top of it (PR #80 review
    # MINOR-1). `evaluate_health_contract_settling` continues spending THIS
    # deadline; it never starts a fresh one.
    absolute_deadline = monotonic() + request["settling_deadline_seconds"]
    try:
        local_node = _local_node(
            runner, absolute_deadline=absolute_deadline, monotonic=monotonic
        )
        revalidate_live_target(
            runner,
            vmid,
            expected_node,
            timeout=_require_prologue_timeout(
                absolute_deadline, monotonic=monotonic
            ),
        )
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

    if request["contract_kind"] == _GUEST_OPERATIONAL_KIND:
        # The built-in baseline: ONE execution, no sleep, no second
        # confirmation round, and no settling-loop bound of any kind applies
        # -- see `_evaluate_guest_operational_once` and ARCHITECTURE.md,
        # "Job-bound healthcheck execution -- baseline independence". A
        # successful execution is DECISIVE immediately; anything else is
        # UNKNOWN after exactly this one attempt.
        attempt_start = monotonic()
        outcome, reason = _evaluate_guest_operational_once(
            runner,
            vmid,
            expected_node,
            local_node,
            absolute_deadline=absolute_deadline,
            monotonic=monotonic,
        )
        last_round_span_ms = int((monotonic() - attempt_start) * 1000)
        settled = {
            "probes": {0: (outcome, reason)},
            "rounds": 1,
            "settled_seconds": monotonic() - attempt_start,
            "last_round_span_ms": last_round_span_ms,
            "decisive": outcome == "passed",
        }
    else:
        settled = evaluate_health_contract_settling(
            runner,
            vmid,
            expected_node,
            local_node,
            request["probes"],
            absolute_deadline=absolute_deadline,
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
