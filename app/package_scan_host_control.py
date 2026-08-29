"""Bounded SSH transport for the sole package-scan host-control operation."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import selectors
import subprocess
import time
from typing import Any

from app.inventory import PackageScanFailure, PackageScanRun
from app.package_scan import HostScanFailure, HostScanResult, expected_host_context


_HOST_RE = re.compile(r"[A-Za-z0-9_.:-]+")
_USER_RE = re.compile(r"[A-Za-z0-9_.-]+")
_MAX_REQUEST_BYTES = 4096


@dataclass(frozen=True, slots=True)
class BoundedProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    timed_out: bool = False
    output_exceeded: bool = False


ProcessRunner = Callable[[tuple[str, ...], bytes, float, int], BoundedProcessResult]


def _bounded_process_runner(
    argv: tuple[str, ...], input_bytes: bytes, timeout: float, max_output: int
) -> BoundedProcessResult:
    process = subprocess.Popen(  # noqa: S603 - argv is fixed/validated and never a shell
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        close_fds=True,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None
    process.stdin.write(input_bytes)
    process.stdin.close()
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
    return BoundedProcessResult(
        process.returncode,
        bytes(output["stdout"][: max_output + 1]),
        bytes(output["stderr"][: max_output + 1]),
        timed_out,
        exceeded,
    )


class SshPackageScanHostControl:
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
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 3600:
            raise ValueError("host-control timeout is invalid")
        if type(max_result_bytes) is not int or not 1024 <= max_result_bytes <= 16 * 1024 * 1024:
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

    def scan_packages(self, run: PackageScanRun) -> HostScanResult:
        context = expected_host_context(run)
        request = {
            "request_version": 1,
            "operation": "scan_packages",
            "target": {
                "vmid": run.expected_vmid,
                "expected_node": run.expected_node_name,
            },
            "context": context,
        }
        encoded = json.dumps(
            request, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        if len(encoded) > _MAX_REQUEST_BYTES:
            raise HostScanFailure(
                PackageScanFailure.EXECUTION_FAILED,
                "host-control request exceeded its structural bound",
            )
        argv = (
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
            f"{self._user}@{self._host}",
        )
        result = self._runner(
            argv, encoded, float(self._timeout_seconds), self._max_result_bytes
        )
        if result.timed_out:
            raise TimeoutError("host-control request timed out")
        if result.output_exceeded:
            raise HostScanFailure(
                PackageScanFailure.EXECUTION_FAILED,
                "host-control result exceeded its configured bound",
            )
        if result.returncode != 0 and not result.stdout:
            raise HostScanFailure(
                PackageScanFailure.EXECUTION_FAILED,
                "host-control SSH execution failed",
            )
        try:
            payload = json.loads(result.stdout.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise HostScanFailure(
                PackageScanFailure.EXECUTION_FAILED,
                "host-control returned a malformed response",
            ) from exc
        return _parse_response(payload, context)


def _parse_response(
    payload: Any, expected_context: Mapping[str, Any]
) -> HostScanResult:
    if not isinstance(payload, Mapping) or payload.get("response_version") != 1:
        raise HostScanFailure(
            PackageScanFailure.EXECUTION_FAILED,
            "host-control returned an unsupported response",
        )
    context = payload.get("context")
    if not isinstance(context, Mapping) or dict(context) != dict(expected_context):
        raise HostScanFailure(
            PackageScanFailure.STALE_TARGET,
            "host-control response context does not match the scan request",
        )
    if payload.get("ok") is not True:
        error = payload.get("error")
        if not isinstance(error, Mapping):
            raise HostScanFailure(
                PackageScanFailure.EXECUTION_FAILED,
                "host-control returned an unclassified failure",
            )
        try:
            failure = PackageScanFailure(str(error["classification"]))
        except (KeyError, ValueError) as exc:
            raise HostScanFailure(
                PackageScanFailure.EXECUTION_FAILED,
                "host-control returned an unknown failure classification",
            ) from exc
        message = str(error.get("message") or "package scan failed")[:500]
        os_data = payload.get("os")
        os_id = os_version = None
        if isinstance(os_data, Mapping):
            os_id = str(os_data.get("id") or "")[:100] or None
            os_version = str(os_data.get("version") or "")[:200] or None
        raise HostScanFailure(failure, message, os_id, os_version)
    os_release = payload.get("os_release")
    simulation = payload.get("simulation")
    reboot_required = payload.get("reboot_required")
    if (
        not isinstance(os_release, str)
        or not isinstance(simulation, Mapping)
        or type(simulation.get("returncode")) is not int
        or not isinstance(simulation.get("stdout"), str)
        or simulation["returncode"] != 0
        or reboot_required not in {True, None}
    ):
        raise HostScanFailure(
            PackageScanFailure.EXECUTION_FAILED,
            "host-control returned malformed successful scan evidence",
        )
    return HostScanResult(
        context=dict(context),
        os_release=os_release,
        simulation_stdout=simulation["stdout"],
        reboot_required=reboot_required,
    )
