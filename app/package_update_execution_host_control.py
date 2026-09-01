"""Dark bounded SSH transport for the execution-time APT plan equality gate.

**Not production-reachable and not deployed.** No production configuration,
key, or `authorized_keys` entry exists for this channel: `app/inventory_runtime.py`
never constructs it, and neither bootstrap nor the product updater installs
its helper or key. It is instantiated only by hermetic tests in this stage.

It is a separate, purpose-specific client and logical privilege boundary
from `app/package_scan_host_control.py`: it targets the not-deployed
`deploy/hubinet-package-update-helper.py` forced-command boundary, never
`deploy/hubinet-package-scan-helper.py`. It reuses the same bounded-process
runner every other typed host-control transport uses -- one implementation
of "run a fixed argv with a deadline and a byte cap", not a third one -- and
the same pinned-key, no-password, no-forwarding, no-arbitrary-command SSH
posture as the snapshot transport.
"""

from __future__ import annotations

from collections.abc import Mapping
import json
from pathlib import Path
import re
from typing import Any

from app.inventory import PackageScanFailure, PackageUpdateJob
from app.package_scan import HostScanFailure
from app.package_scan_host_control import ProcessRunner, _bounded_process_runner
from app.package_update_execution import HostExecutionResult, expected_execution_host_context


_HOST_RE = re.compile(r"[A-Za-z0-9_.:-]+")
_USER_RE = re.compile(r"[A-Za-z0-9_.-]+")
_MAX_REQUEST_BYTES = 4096


class SshPackageUpdateExecutionHostControl:
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

    def simulate_exact_update_plan(self, job: PackageUpdateJob) -> HostExecutionResult:
        context = expected_execution_host_context(job)
        request = {
            "request_version": 1,
            "operation": "simulate_exact_update_plan",
            "target": {
                "vmid": job.expected_vmid,
                "expected_node": job.expected_node_name,
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
) -> HostExecutionResult:
    if not isinstance(payload, Mapping) or payload.get("response_version") != 1:
        raise HostScanFailure(
            PackageScanFailure.EXECUTION_FAILED,
            "host-control returned an unsupported response",
        )
    context = payload.get("context")
    if not isinstance(context, Mapping) or dict(context) != dict(expected_context):
        raise HostScanFailure(
            PackageScanFailure.STALE_TARGET,
            "host-control response context does not match the execution request",
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
        message = str(error.get("message") or "package update execution failed")[:500]
        raise HostScanFailure(failure, message)
    os_release = payload.get("os_release")
    simulation = payload.get("simulation")
    if (
        not isinstance(os_release, str)
        or not isinstance(simulation, Mapping)
        or type(simulation.get("returncode")) is not int
        or not isinstance(simulation.get("stdout"), str)
        or simulation["returncode"] != 0
    ):
        raise HostScanFailure(
            PackageScanFailure.EXECUTION_FAILED,
            "host-control returned malformed successful execution evidence",
        )
    return HostExecutionResult(
        context=dict(context),
        os_release=os_release,
        simulation_stdout=simulation["stdout"],
    )
