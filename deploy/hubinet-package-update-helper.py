#!/usr/bin/env python3
"""Dark forced-command PVE boundary for the execution-time APT plan gate.

**Not deployed.** Bootstrap and the product updater never install this file,
never provision an `authorized_keys` forced-command entry for it, and never
grant it or the identity that would run it any PVE privilege. It exists in
this repository only so hermetic tests can exercise the execution-time plan
equality gate end to end; see `app/package_update_execution_host_control.py`
and `ARCHITECTURE.md`, "Execution-time plan equality".

It is a separate file and a separate logical privilege boundary from
`deploy/hubinet-package-scan-helper.py`, which stays the only helper
production ever deploys and remains scan-only. Keeping this one dark and
distinct means NEXT-C cannot accidentally make job execution
production-reachable merely by extending an already-deployed boundary, and
the future mutation stage will need a deliberately stronger execution
boundary of its own anyway.

Exposes exactly ONE typed, non-mutating operation: `simulate_exact_update_plan`
-- a fixed APT metadata refresh (`apt-get update -qq --error-on=any`) plus a
fixed upgrade simulation (`apt-get -s upgrade`), against the caller's own
frozen expected VMID/node, re-validated live immediately before each guest
command. No install, upgrade, dist-upgrade, remove, autoremove, or dpkg
configure/install ever appears in this file's argv, and none may be added to
it: the boundary intentionally has no capability to mutate a workload.
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


MAX_REQUEST_BYTES = 4096
MAX_COMMAND_OUTPUT_BYTES = 6 * 1024 * 1024
COMMAND_TIMEOUT_SECONDS = 300.0
NODE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,62}")
APT_VERSION_RE = re.compile(
    r"apt ([0-9]+)\.([0-9]+)\.([0-9]+)"
    r"(?:[~+.-][A-Za-z0-9.+:~-]*)? \([A-Za-z0-9_-]+\)"
)
MINIMUM_APT_VERSION = (2, 1, 16)
BUSY_PATTERNS = (
    "could not get lock",
    "unable to acquire the dpkg frontend lock",
    "is another process using it",
    "could not open lock file",
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


class ExecutionError(RuntimeError):
    def __init__(self, classification: str, message: str) -> None:
        super().__init__(message)
        self.classification = classification
        self.message = message


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
    if not isinstance(payload, Mapping) or set(payload) != {
        "request_version",
        "operation",
        "target",
        "context",
    }:
        raise RequestError("request must have the exact execution-plan shape")
    if (
        payload["request_version"] != 1
        or payload["operation"] != "simulate_exact_update_plan"
    ):
        raise RequestError("unknown host-control operation")
    target = payload["target"]
    context = payload["context"]
    if not isinstance(target, Mapping) or set(target) != {"vmid", "expected_node"}:
        raise RequestError("target must have the exact execution-plan shape")
    if not isinstance(context, Mapping) or set(context) != {
        "job_id",
        "resource_id",
        "binding_id",
        "locator_generation",
        "resource_continuity_revision",
    }:
        raise RequestError("context must have the exact execution-plan shape")
    vmid = target["vmid"]
    if type(vmid) is not int or not 100 <= vmid <= 999_999_999:
        raise RequestError("vmid must be a valid PVE integer VMID")
    expected_node = target["expected_node"]
    if not isinstance(expected_node, str) or not NODE_RE.fullmatch(expected_node):
        raise RequestError("expected_node is invalid")
    normalized_context = {
        "job_id": _canonical_uuid(context["job_id"], "job_id"),
        "resource_id": _canonical_uuid(context["resource_id"], "resource_id"),
        "binding_id": _canonical_uuid(context["binding_id"], "binding_id"),
        "locator_generation": _positive_integer(
            context["locator_generation"], "locator_generation"
        ),
        "resource_continuity_revision": _positive_integer(
            context["resource_continuity_revision"],
            "resource_continuity_revision",
        ),
    }
    return {
        "vmid": vmid,
        "expected_node": expected_node,
        "context": normalized_context,
    }


def _command(
    runner: Runner, argv: tuple[str, ...], *, max_output: int = MAX_COMMAND_OUTPUT_BYTES
) -> CommandResult:
    result = runner(argv, COMMAND_TIMEOUT_SECONDS, max_output)
    if result.timed_out:
        raise ExecutionError("timeout", "package update execution command timed out")
    if result.output_exceeded:
        raise ExecutionError(
            "execution_failed",
            "package update execution command output exceeded its bound",
        )
    return result


def _local_node(runner: Runner) -> str:
    """Ask this PVE node's own trusted local state who it is.

    Uses the same authoritative `/cluster/status` source as the scan helper
    and the backend's own discovery provider -- no new trust source.
    """

    result = _command(
        runner,
        ("pvesh", "get", "/cluster/status", "--output-format", "json"),
        max_output=1 * 1024 * 1024,
    )
    if result.returncode != 0:
        raise ExecutionError("execution_failed", "could not read local PVE cluster status")
    try:
        rows = json.loads(result.stdout.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ExecutionError(
            "execution_failed", "local PVE cluster status was malformed"
        ) from exc
    if not isinstance(rows, list):
        raise ExecutionError("execution_failed", "local PVE cluster status was malformed")
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
        raise ExecutionError("execution_failed", "local PVE node identity is ambiguous")
    return local_nodes[0]


def _run_guest_command(
    runner: Runner,
    vmid: int,
    expected_node: str,
    local_node: str,
    tail: tuple[str, ...],
    *,
    max_output: int = MAX_COMMAND_OUTPUT_BYTES,
) -> CommandResult:
    """Run one fixed ``pct exec`` shape on whichever node currently holds it.

    Identical routing contract to the scan helper's own
    ``_run_guest_command``: ``tail`` is always one of this file's own fixed
    argv shapes, and a non-local guest is routed to its expected cluster
    member over root's existing passwordless inter-node SSH trust Proxmox
    itself provisions -- no new Hubinet credential on that node, and no
    request-provided or arbitrary text ever reaches either command.
    """

    inner = ("pct", "exec", str(vmid), "--", *tail)
    if expected_node == local_node:
        result = _command(runner, inner, max_output=max_output)
    else:
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
        raise ExecutionError(
            "execution_failed",
            "could not execute package update execution command in guest",
        )
    return result


def _current_target(runner: Runner, vmid: int, expected_node: str) -> None:
    result = _command(
        runner,
        ("pvesh", "get", "/cluster/resources", "--type", "vm", "--output-format", "json"),
        max_output=4 * 1024 * 1024,
    )
    if result.returncode != 0:
        raise ExecutionError("execution_failed", "could not read current PVE target state")
    try:
        rows = json.loads(result.stdout.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ExecutionError(
            "execution_failed", "current PVE target state was malformed"
        ) from exc
    if not isinstance(rows, list):
        raise ExecutionError("execution_failed", "current PVE target state was malformed")
    matches = [row for row in rows if isinstance(row, Mapping) and row.get("vmid") == vmid]
    if len(matches) != 1:
        raise ExecutionError("guest_unavailable", "guest is missing or unavailable")
    row = matches[0]
    if row.get("type") != "lxc":
        raise ExecutionError(
            "unsupported_resource_type", "current PVE resource is not an LXC guest"
        )
    if row.get("node") != expected_node:
        raise ExecutionError("stale_target", "guest node changed after job issuance")
    if row.get("status") != "running":
        raise ExecutionError("guest_unavailable", "guest is not running")


def _decode_output(result: CommandResult) -> tuple[str, str]:
    try:
        return result.stdout.decode("utf-8"), result.stderr.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ExecutionError(
            "execution_failed", "package update execution command output was not UTF-8"
        ) from exc


def _parse_os_release(text: str) -> tuple[str, str]:
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ExecutionError("unsupported_os", "guest OS release metadata is malformed")
        key, value = line.split("=", 1)
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or key in values:
            raise ExecutionError("unsupported_os", "guest OS release metadata is malformed")
        try:
            parsed = shlex.split(value, posix=True)
        except ValueError as exc:
            raise ExecutionError(
                "unsupported_os", "guest OS release metadata is malformed"
            ) from exc
        if len(parsed) > 1:
            raise ExecutionError("unsupported_os", "guest OS release metadata is ambiguous")
        values[key] = parsed[0] if parsed else ""
    os_id = values.get("ID", "").lower()
    version = values.get("VERSION_ID") or values.get("VERSION_CODENAME") or ""
    if os_id not in {"debian", "ubuntu"}:
        raise ExecutionError("unsupported_os", "guest operating system is not Debian or Ubuntu")
    if not version:
        raise ExecutionError("unsupported_os", "guest operating system version is unknown")
    return os_id, version


def _parse_apt_version(text: str) -> tuple[int, int, int]:
    lines = text.splitlines()
    if not lines or not (match := APT_VERSION_RE.fullmatch(lines[0])):
        raise ExecutionError("execution_failed", "guest APT version output was malformed")
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch)


def _package_failure(stage: str, stderr: str) -> ExecutionError:
    lowered = stderr.lower()
    if any(pattern in lowered for pattern in BUSY_PATTERNS):
        return ExecutionError("package_manager_busy", "APT or dpkg is busy")
    if stage == "metadata_refresh":
        return ExecutionError("metadata_refresh_failed", "APT metadata refresh failed")
    return ExecutionError("simulation_failed", "APT upgrade simulation failed")


def handle_request(payload: Any, *, runner: Runner = _run_bounded) -> dict[str, Any]:
    request = validate_request(payload)
    vmid = request["vmid"]
    expected_node = request["expected_node"]
    context = request["context"]
    try:
        local_node = _local_node(runner)

        _current_target(runner, vmid, expected_node)
        os_result = _run_guest_command(
            runner, vmid, expected_node, local_node,
            ("env", "LC_ALL=C", "cat", "/etc/os-release"),
            max_output=64 * 1024,
        )
        os_release, _ = _decode_output(os_result)
        if os_result.returncode != 0:
            raise ExecutionError("guest_unavailable", "guest OS release metadata is unavailable")
        _parse_os_release(os_release)

        _current_target(runner, vmid, expected_node)
        apt_version_result = _run_guest_command(
            runner, vmid, expected_node, local_node,
            ("env", "LC_ALL=C", "apt-get", "--version"),
            max_output=64 * 1024,
        )
        apt_version_stdout, _ = _decode_output(apt_version_result)
        if apt_version_result.returncode != 0:
            raise ExecutionError("execution_failed", "could not determine guest APT version")
        if _parse_apt_version(apt_version_stdout) < MINIMUM_APT_VERSION:
            raise ExecutionError(
                "unsupported_os",
                "guest APT version does not support strict metadata refresh",
            )

        # Metadata refresh: execution-time candidate state must be current,
        # not a reuse of the original scan's stale indexes. This writes only
        # APT's own index/cache metadata, which PRODUCT.md's "What package
        # scanning may do" already treats as non-mutating; it never touches
        # a workload package.
        _current_target(runner, vmid, expected_node)
        update = _run_guest_command(
            runner, vmid, expected_node, local_node,
            (
                "env", "LC_ALL=C",
                "DEBIAN_FRONTEND=noninteractive", "apt-get", "update", "-qq",
                "--error-on=any",
            ),
        )
        _, update_stderr = _decode_output(update)
        if update.returncode != 0:
            raise _package_failure("metadata_refresh", update_stderr)

        # Simulation only (`-s`): never a real upgrade. See
        # PRODUCT.md, "What package scanning may do", and
        # ARCHITECTURE.md, "Execution-time plan equality".
        _current_target(runner, vmid, expected_node)
        simulation = _run_guest_command(
            runner, vmid, expected_node, local_node,
            (
                "env", "LC_ALL=C",
                "DEBIAN_FRONTEND=noninteractive", "apt-get", "-s", "upgrade",
            ),
        )
        simulation_stdout, simulation_stderr = _decode_output(simulation)
        if simulation.returncode != 0:
            raise _package_failure("simulation", simulation_stderr)

        # Independent, read-only proof of the guest's currently installed
        # binary packages -- never inferred from APT's own candidate
        # description, and read as close as possible to the simulation
        # above to minimize the ordinary concurrent-package-manager race
        # window between the two. See ARCHITECTURE.md, "Binary package
        # identity".
        _current_target(runner, vmid, expected_node)
        native_arch_result = _run_guest_command(
            runner, vmid, expected_node, local_node,
            ("env", "LC_ALL=C", "dpkg", "--print-architecture"),
            max_output=4096,
        )
        native_architecture, _ = _decode_output(native_arch_result)
        if native_arch_result.returncode != 0:
            raise ExecutionError(
                "execution_failed", "could not determine guest native architecture"
            )

        _current_target(runner, vmid, expected_node)
        inventory_result = _run_guest_command(
            runner, vmid, expected_node, local_node,
            (
                "env", "LC_ALL=C", "dpkg-query", "-W",
                "-f=${Package}\\t${Architecture}\\t${Version}\\t${db:Status-Status}\\n",
            ),
            max_output=32 * 1024 * 1024,
        )
        installed_inventory, _ = _decode_output(inventory_result)
        if inventory_result.returncode != 0:
            raise ExecutionError(
                "execution_failed", "could not read guest installed package inventory"
            )

        return {
            "response_version": 1,
            "ok": True,
            "context": context,
            "os_release": os_release,
            "native_architecture": native_architecture,
            "installed_inventory": installed_inventory,
            "simulation": {"returncode": 0, "stdout": simulation_stdout},
        }
    except ExecutionError as exc:
        return {
            "response_version": 1,
            "ok": False,
            "context": context,
            "error": {
                "classification": exc.classification,
                "message": exc.message[:500],
            },
        }


def main() -> int:
    if os.environ.get("SSH_ORIGINAL_COMMAND"):
        response = {
            "response_version": 1,
            "ok": False,
            "context": {},
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
            sys.stdout.write(json.dumps(response, ensure_ascii=True, separators=(",", ":")))
            return 0 if response.get("ok") is True else 1
        except (UnicodeDecodeError, ValueError, RequestError) as exc:
            error = str(exc)[:500] or "malformed execution-plan request"
    response = {
        "response_version": 1,
        "ok": False,
        "context": {},
        "error": {"classification": "execution_failed", "message": error},
    }
    sys.stdout.write(json.dumps(response, ensure_ascii=True, separators=(",", ":")))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
