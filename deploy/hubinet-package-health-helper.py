#!/usr/bin/env python3
"""Forced-command PVE boundary for Hubinet's sole health-evaluation operation.

**Dark and NOT deployed.** No bootstrap or updater path installs this file, no
`authorized_keys` entry exists for it, no key is provisioned, and it requires
no PVE API privilege beyond the audit-only pair the product already has: it
uses host-local `pct exec`, not a PVE mutation endpoint.

It exposes exactly ONE typed operation, `evaluate_health_contract`, and that
operation is READ-ONLY. It cannot create, delete, start, stop, snapshot, roll
back, upgrade, install, or remove anything, and there is no path through it
that accepts remote command text.

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
property block per matched unit, and three things make the requested object
exact:

1. `--` IS honoured here (verified: `systemctl show ... -- --help` reports
   `Id=--help.service` rather than printing usage), so an option-like target
   can never be consumed as an option;
2. the target must match a strict unit-name charset that contains none of
   systemd's glob characters `*`, `?`, `[` -- necessary because a pattern can
   legitimately match exactly ONE unit (verified: `ssh?service` matched only
   `ssh.service`), so "exactly one block" alone is not sufficient;
3. exactly one property block must come back.

`ActiveState=active` is the only PASS. Any other known state is a definitive
FAIL, including `LoadState=not-found`, which systemd reports as a normal
success with `ActiveState=inactive`. An unreadable, empty, or multi-block
answer is UNKNOWN. "The command ran" is never a PASS.

**Docker.** `docker inspect` resolves a container by name OR by ID prefix
(verified), so the returned `.Name` is compared against the requested target:
an ID-prefix resolution reports a different name and is refused rather than
accepted as the named container. `--type container` stops an image of the same
name matching, and `--` is honoured (verified: `-- --help` is treated as a
container name). The `--format` template is a constant owned by this file; no
part of it is built from a request.

`docker inspect` exits 1 for absence and for other failures, and telling them
apart by matching English stderr would be fragile. A fixed daemon oracle
(`docker ps --all --no-trunc --quiet`) runs before the probe. After a failing
inspect, another fixed command lists every complete container name as one JSON
string; only a successful, bounded, well-formed listing that does not contain
the requested exact name proves absence. A timeout, overflow, generic inspect
failure for a name still present, unavailable daemon, or unusable listing is
UNKNOWN.

**`docker_container_healthy` is never downgraded to "running".** It requires
`.State.Running` true AND `.State.Health.Status` exactly `healthy`. A container
with no HEALTHCHECK, or one reporting `starting` or `unhealthy`, is a
definitive FAIL, because the operator specifically demanded Docker health.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
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
#: interpolated, and never extended by a caller. The `<none>` branch is what
#: lets `docker_container_healthy` tell "no HEALTHCHECK configured" apart from
#: a health status, which it must, because the first is a definitive failure
#: of a probe that specifically demanded Docker health.
DOCKER_INSPECT_FORMAT = (
    "{{.Name}}\t{{.State.Running}}\t"
    "{{if .State.Health}}{{.State.Health.Status}}{{else}}<none>{{end}}"
)

#: Kept beside the template above so the flag and the constant it carries are
#: audited as one thing.
DOCKER_INSPECT_FORMAT_FLAG = "--format"

#: Fixed positive absence proof.  Docker 26.1.5 was verified to accept this
#: exact `ps` shape and emit each container's complete `.Names` value as one
#: JSON string.  JSON keeps parsing exact without stderr-language matching.
DOCKER_NAME_LIST_FORMAT = "{{json .Names}}"

#: systemd ActiveState values that are a definitive NOT-active. Anything
#: outside this set and "active" is an answer this helper does not understand,
#: which is UNKNOWN rather than a guess in either direction.
SYSTEMD_INACTIVE_STATES = frozenset(
    {"inactive", "failed", "activating", "deactivating", "reloading", "maintenance"}
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


class _ContainerAbsent(RuntimeError):
    """The daemon answered and reported no such container.

    Internal control flow only. It exists so "definitely absent" can be
    distinguished from "could not tell" by the DAEMON having answered, rather
    than by matching Docker's English error text.
    """


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


def validate_request(payload: Any) -> dict[str, Any]:
    """Accept exactly one request shape, and nothing else.

    Every authority fact arrives typed and is validated here. There is no
    field through which a caller could pass a command, an option, an argv
    fragment, a format template, an environment variable, or a probe kind
    outside the three the product defines.
    """

    if not isinstance(payload, Mapping) or set(payload) != {
        "request_version",
        "operation",
        "target",
        "ownership",
        "health_contract",
    }:
        raise RequestError("request must have the exact health-evaluation shape")
    if (
        payload["request_version"] != 1
        or payload["operation"] != "evaluate_health_contract"
    ):
        raise RequestError("unknown host-control operation")

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
    seen: set[tuple[str, str]] = set()
    for index, raw in enumerate(raw_probes):
        if not isinstance(raw, Mapping) or set(raw) != {"index", "kind", "target"}:
            raise RequestError("a probe must have the exact shape")
        if raw["index"] != index:
            raise RequestError("probes must be canonically indexed from 0")
        kind = raw["kind"]
        if kind not in PROBE_KINDS:
            raise RequestError("unsupported probe kind")
        probe_target = raw["target"]
        if (
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
    }


def _command(
    runner: Runner,
    argv: tuple[str, ...],
    *,
    max_output: int = MAX_COMMAND_OUTPUT_BYTES,
) -> CommandResult:
    return runner(argv, COMMAND_TIMEOUT_SECONDS, max_output)


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


def revalidate_live_target(runner: Runner, vmid: int, expected_node: str) -> None:
    """Independently prove the live PVE facts before touching the guest.

    The backend proves it still names the intended resource INCARNATION; only
    the host can prove the live PVE target. A VMID is an execution locator,
    not an identity: PVE can free one and reuse it at any moment, and a health
    PASS recorded against a replacement guest would be a false statement about
    a workload this job never updated -- read-only or not.
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
    data_argument: str | None = None,
    max_output: int = MAX_COMMAND_OUTPUT_BYTES,
) -> CommandResult:
    """Run one fixed ``pct exec`` shape on the node that currently holds it.

    **This dispatcher owns the live-target invariant**, exactly as the
    mutation helper's does: every single guest command is preceded here by its
    own fresh :func:`revalidate_live_target`, so no caller can amortize one
    check across two commands and send the second to a replacement guest.

    ``tail`` is a fixed argv shape built by this file, with at most one
    element that came from the request -- a probe target that has already
    passed its kind-specific execution-time validation. A non-local guest is
    routed to its expected cluster member over root's existing passwordless
    inter-node SSH trust Proxmox itself provisions, exactly as the scan,
    execution, and mutation helpers do; no new Hubinet credential exists on
    that node. Unlike those helpers, this one routes an element that originated
    outside the file: ``data_argument`` names it, and it is proved shell-inert
    before it may cross that boundary. Every other element is a constant this
    file owns.
    """

    revalidate_live_target(runner, vmid, expected_node)
    inner = ("pct", "exec", str(vmid), "--", *tail)
    if expected_node == local_node:
        result = _command(runner, inner, max_output=max_output)
    else:
        # Routing to another cluster member is the ONE place a command line
        # exists rather than an argv list, because that is what ssh hands the
        # remote login shell.
        #
        # Shell quoting is deliberately NOT the mechanism that makes the
        # caller's target safe here. The kind-specific validation already
        # restricts a target to characters a shell reads as nothing at all,
        # and this makes that a CHECKED property rather than a claim: if the
        # request-derived element would need a quote adding, it is not what
        # this file believes it is, and the probe is reported unevaluable
        # instead of being handed to a shell that might read it.
        #
        # The constants around it -- notably the Docker `--format` template,
        # whose braces this file owns -- are quoted normally. Their content is
        # fixed and reviewed; the caller's is not, and only the caller's is
        # subject to this rule.
        if data_argument is not None and shlex.quote(data_argument) != data_argument:
            raise ProbeUnknown("probe_target_not_exact")
        argv = (
            "ssh",
            "-T",
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=yes",
            f"root@{expected_node}",
            shlex.join(inner),
        )
        result = _command(runner, argv, max_output=max_output)
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
# The three probe kinds. Each returns (outcome, reason) or raises ProbeUnknown.
# ---------------------------------------------------------------------------


def _require_exact_systemd_unit(target: str) -> str:
    if not SYSTEMD_UNIT_RE.fullmatch(target) or target.startswith("-"):
        # Contains a glob character, a path separator, whitespace, or
        # something systemd would escape: it does not name one exact unit.
        raise ProbeUnknown("probe_target_not_exact")
    if not target.endswith(SYSTEMD_UNIT_SUFFIXES):
        raise ProbeUnknown("probe_target_not_exact")
    return target


def evaluate_systemd_unit_active(
    runner: Runner, vmid: int, expected_node: str, local_node: str, target: str
) -> tuple[str, str]:
    unit = _require_exact_systemd_unit(target)
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
            # `--` is verified to end option parsing here, so an option-like
            # target is a unit name and never a new option.
            "--",
            unit,
        ),
        data_argument=unit,
        max_output=64 * 1024,
    )
    stdout = _decode(result)
    if result.returncode != 0:
        # systemctl show succeeds even for a unit that does not exist, so a
        # non-zero exit means the command itself could not run -- no systemd
        # in the guest, a broken bus, a permission problem. Never a verdict.
        raise ProbeUnknown("command_failed")
    blocks = [block for block in stdout.strip().split("\n\n") if block.strip()]
    if len(blocks) != 1:
        # More than one block means the target matched more than one unit
        # despite the charset check; zero means the answer was empty. Neither
        # is a statement about the requested unit.
        raise ProbeUnknown("probe_target_ambiguous" if blocks else "malformed_output")
    properties: dict[str, str] = {}
    for line in blocks[0].splitlines():
        if "=" not in line:
            raise ProbeUnknown("malformed_output")
        key, value = line.split("=", 1)
        if key in properties:
            raise ProbeUnknown("malformed_output")
        properties[key] = value
    if set(properties) != {"Id", "LoadState", "ActiveState"}:
        raise ProbeUnknown("malformed_output")
    active_state = properties["ActiveState"]
    if active_state == "active":
        return "passed", "unit_active"
    if active_state in SYSTEMD_INACTIVE_STATES:
        # Includes LoadState=not-found, which systemd reports as a normal
        # ActiveState=inactive: a unit that does not exist is definitively
        # not active, and the operator said it must be.
        return "failed", "unit_not_active"
    raise ProbeUnknown("malformed_output")


def _require_exact_docker_name(target: str) -> str:
    if not DOCKER_NAME_RE.fullmatch(target) or target.startswith("-"):
        raise ProbeUnknown("probe_target_not_exact")
    return target


def _docker_daemon_answered(
    runner: Runner, vmid: int, expected_node: str, local_node: str
) -> bool:
    """Fixed, argument-less proof that the guest's Docker daemon answered.

    `docker ps` requires the daemon, takes nothing from the request, and exits
    0 only when the daemon responded. This initial oracle proves Docker is
    reachable before evaluation; it does not classify a later non-zero
    `docker inspect` as absence. Definitive absence requires the separate
    bounded exact-name proof in `_docker_exact_name_is_absent`.
    """

    try:
        result = _run_guest_command(
            runner,
            vmid,
            expected_node,
            local_node,
            ("env", "LC_ALL=C", "docker", "ps", "--all", "--no-trunc", "--quiet"),
            max_output=1024 * 1024,
        )
    except ProbeUnknown:
        return False
    return (
        not result.timed_out and not result.output_exceeded and result.returncode == 0
    )


def _inspect_container(
    runner: Runner, vmid: int, expected_node: str, local_node: str, name: str
) -> tuple[str, bool, str]:
    """Return ``(name, running, health)`` for exactly the requested container."""

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
            # Stops an image of the same name from being inspected instead.
            "--type",
            "container",
            DOCKER_INSPECT_FORMAT_FLAG,
            DOCKER_INSPECT_FORMAT,
            # Verified to end option parsing, so an option-like container name
            # is a name and never a new option.
            "--",
            name,
        ),
        data_argument=name,
        max_output=64 * 1024,
    )
    # A real timeout kills the process, so it normally carries BOTH
    # `timed_out=True` and a negative return code.  Execution bounds must win
    # before any return-code interpretation or absence proof.
    if result.timed_out:
        raise ProbeUnknown("command_timed_out")
    if result.output_exceeded:
        raise ProbeUnknown("malformed_output")
    if result.returncode != 0:
        # A live daemon does not make every inspect error mean absence.  Only
        # a separate exact-name inventory may prove the name is not present.
        if _docker_exact_name_is_absent(
            runner, vmid, expected_node, local_node, name
        ):
            raise _ContainerAbsent()
        raise ProbeUnknown("command_failed")
    stdout = _decode(result)
    lines = [line for line in stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        raise ProbeUnknown("malformed_output")
    fields = lines[0].split("\t")
    if len(fields) != 3:
        raise ProbeUnknown("malformed_output")
    observed_name, running, health = fields
    # `docker inspect` also resolves a container by ID PREFIX, and reports the
    # container's real name with a leading '/'. Requiring an exact match is
    # what stops a hex-looking target passing because it happened to prefix
    # some other container's id.
    if observed_name != f"/{name}":
        raise ProbeUnknown("probe_target_not_exact")
    if running not in ("true", "false"):
        raise ProbeUnknown("malformed_output")
    return observed_name, running == "true", health


def _docker_exact_name_is_absent(
    runner: Runner, vmid: int, expected_node: str, local_node: str, name: str
) -> bool:
    """Positively prove an exact container name is absent from a live daemon.

    The argv and format are fixed.  Every listed name is decoded as one JSON
    string and compared for exact equality.  An unavailable, timed-out,
    overflowing, non-zero, or malformed listing is UNKNOWN, never absence.
    """

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
    )
    stdout = _decode(result)
    if result.returncode != 0:
        raise ProbeUnknown("docker_daemon_unavailable")
    names: list[str] = []
    for line in stdout.splitlines():
        try:
            listed = json.loads(line)
        except (TypeError, ValueError) as exc:
            raise ProbeUnknown("malformed_output") from exc
        if not isinstance(listed, str) or not listed:
            raise ProbeUnknown("malformed_output")
        names.append(listed)
    return name not in names


def evaluate_docker_container_running(
    runner: Runner, vmid: int, expected_node: str, local_node: str, target: str
) -> tuple[str, str]:
    name = _require_exact_docker_name(target)
    if not _docker_daemon_answered(runner, vmid, expected_node, local_node):
        raise ProbeUnknown("docker_daemon_unavailable")
    try:
        _, running, _ = _inspect_container(
            runner, vmid, expected_node, local_node, name
        )
    except _ContainerAbsent:
        return "failed", "container_absent"
    if running:
        return "passed", "container_running"
    return "failed", "container_not_running"


def evaluate_docker_container_healthy(
    runner: Runner, vmid: int, expected_node: str, local_node: str, target: str
) -> tuple[str, str]:
    name = _require_exact_docker_name(target)
    if not _docker_daemon_answered(runner, vmid, expected_node, local_node):
        raise ProbeUnknown("docker_daemon_unavailable")
    try:
        _, running, health = _inspect_container(
            runner, vmid, expected_node, local_node, name
        )
    except _ContainerAbsent:
        return "failed", "container_absent"
    if not running:
        return "failed", "container_not_running"
    # The operator specifically demanded Docker HEALTHCHECK health, so none of
    # these may be downgraded to "well, it is running".
    if health == "healthy":
        return "passed", "container_healthy"
    if health == "unhealthy":
        return "failed", "container_unhealthy"
    if health == "starting":
        return "failed", "container_health_starting"
    if health == "<none>":
        return "failed", "container_has_no_healthcheck"
    raise ProbeUnknown("malformed_output")


EVALUATORS: dict[str, Callable[..., tuple[str, str]]] = {
    "systemd_unit_active": evaluate_systemd_unit_active,
    "docker_container_running": evaluate_docker_container_running,
    "docker_container_healthy": evaluate_docker_container_healthy,
}


def handle_request(payload: Any, *, runner: Runner = _run_bounded) -> dict[str, Any]:
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

    probes: list[dict[str, Any]] = []
    for probe in request["probes"]:
        evaluator = EVALUATORS[probe["kind"]]
        try:
            outcome, reason = evaluator(
                runner, vmid, expected_node, local_node, probe["target"]
            )
        except ProbeUnknown as exc:
            outcome, reason = "unknown", exc.reason
        except HealthError as exc:
            # A whole-host problem observed mid-probe (the guest went away,
            # moved node, or stopped). It makes THIS probe unevaluable; it is
            # never a failure of the workload the operator declared.
            outcome, reason = "unknown", (
                "guest_unavailable"
                if exc.classification in ("guest_unavailable", "stale_target")
                else "command_failed"
            )
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
