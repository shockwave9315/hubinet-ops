"""Dark bounded SSH transport for job-bound healthcheck evaluation.

**Not production-reachable and not deployed.** No production configuration,
key, or `authorized_keys` entry exists for this channel:
`app/inventory_runtime.py` never constructs it, and neither bootstrap nor the
product updater installs its helper or key. It is instantiated only by
hermetic tests in this stage.

It is a separate, purpose-specific client from the scan, snapshot, execution,
mutation, and rollback transports, and deliberately does not resurrect the
removed generic `app/host_control.py`: pinned known-hosts trust, strict typed
JSON, bounded request and response sizes, bounded timeouts, no password
authentication, no agent or port forwarding, no interactive shell, and no
arbitrary remote command.

Every field of the request is a typed authority fact assembled by
`InventoryAuthority.package_update_health_request`. There is no VMID, node,
contract, probe, kind, or target parameter a caller may choose, and no probe
that is not one this job froze at issuance.

**This transport is READ-ONLY on both sides**, which is why it exposes one
operation and carries no operation identity, no journal binding, and no seal:
there is no at-most-once property to protect, so repeating a call is safe and
inventing a destructive-operation journal would add a failure mode without
adding a guarantee.
"""

from __future__ import annotations

from collections.abc import Mapping
import json
from pathlib import Path
import re
from typing import Any

from app.inventory import (
    HealthProbeKind,
    HealthProbeOutcome,
    MAX_HEALTH_PROBES,
    MAX_HEALTH_PROBE_TARGET_LENGTH,
    PackageUpdateHealthRequest,
)
from app.package_scan_host_control import (
    BoundedProcessResult,
    ProcessRunner,
    _bounded_process_runner,
)
from app.package_update_health import (
    HOST_PROBE_REASONS,
    HostHealthResult,
    HostProbeResult,
    PackageUpdateHealthError,
    expected_health_host_probes,
)


_HOST_RE = re.compile(r"[A-Za-z0-9_.:-]+")
_USER_RE = re.compile(r"[A-Za-z0-9_.-]+")
_NODE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,62}")
_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_FINGERPRINT_RE = re.compile(r"[0-9a-f]{64}")

#: Generous enough for 32 probes with maximum-length targets plus the fixed
#: envelope, and no more. A request that does not fit is refused here rather
#: than truncated on the wire.
_MAX_REQUEST_BYTES = 32 * 1024

#: The whole operation surface. One typed, read-only operation.
_OPERATIONS = ("evaluate_health_contract",)

_OUTCOMES = {
    "passed": HealthProbeOutcome.PASSED,
    "failed": HealthProbeOutcome.FAILED,
    "unknown": HealthProbeOutcome.UNKNOWN,
}


class SshPackageUpdateHealthHostControl:
    """One bounded typed request per health evaluation, over pinned-key SSH."""

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
            # This evaluation runs strictly OUTSIDE the authority store's
            # writer transactions -- it is read-only, so it needs no critical
            # section -- and is therefore deliberately not bound by the
            # snapshot/rollback submission ceilings in
            # `app.inventory.contention_policy`. It is still bounded.
            raise ValueError(
                "health evaluation timeout must be between 1 and 3600 seconds"
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

    def evaluate_health_contract(
        self, request: PackageUpdateHealthRequest
    ) -> HostHealthResult:
        return self._request("evaluate_health_contract", request)

    # -- transport -------------------------------------------------------

    def _request(
        self, operation: str, request: PackageUpdateHealthRequest
    ) -> HostHealthResult:
        if operation not in _OPERATIONS:
            raise ValueError("unsupported health host-control operation")
        if not isinstance(request, PackageUpdateHealthRequest):
            raise ValueError("a typed health request is required")
        for field in ("job_id", "backend_instance_id", "resource_id", "binding_id"):
            value = getattr(request, field)
            if not isinstance(value, str) or not _UUID_RE.fullmatch(value):
                raise ValueError(f"{field} must be a canonical UUID")
        if type(request.vmid) is not int or not 100 <= request.vmid <= 999_999_999:
            raise ValueError("vmid must be a valid PVE integer VMID")
        if not isinstance(request.expected_node, str) or not _NODE_RE.fullmatch(
            request.expected_node
        ):
            raise ValueError("expected_node is invalid")
        for field in (
            "locator_generation",
            "resource_continuity_revision",
            "health_contract_revision",
        ):
            value = getattr(request, field)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{field} must be a positive integer")
        if not isinstance(
            request.health_contract_fingerprint, str
        ) or not _FINGERPRINT_RE.fullmatch(request.health_contract_fingerprint):
            raise ValueError("health_contract_fingerprint is invalid")
        probes = expected_health_host_probes(request.probes)
        if not 1 <= len(probes) <= MAX_HEALTH_PROBES:
            raise ValueError("health request probe set is out of bounds")
        for index, probe in enumerate(probes):
            if probe["index"] != index:
                raise ValueError("health request probes are not canonically indexed")
            if probe["kind"] not in {kind.value for kind in HealthProbeKind}:
                raise ValueError("health request carries an unsupported probe kind")
            target = probe["target"]
            if (
                not isinstance(target, str)
                or not 1 <= len(target) <= MAX_HEALTH_PROBE_TARGET_LENGTH
            ):
                raise ValueError("health request carries an invalid probe target")

        payload = {
            "request_version": 1,
            "operation": operation,
            "target": {
                "vmid": request.vmid,
                "expected_node": request.expected_node,
            },
            # Typed fields only. The helper validates all of them and echoes
            # the contract identity back, so no free text crosses this
            # boundary in either direction.
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
            # The exact frozen contract generation, and the exact frozen
            # probe set. A target is DATA here and stays data: the helper
            # builds fixed argv around it and never interpolates it into
            # command text, a template, or a shell fragment.
            "health_contract": {
                "revision": request.health_contract_revision,
                "fingerprint": request.health_contract_fingerprint,
                "probes": list(probes),
            },
        }
        encoded = json.dumps(
            payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        if len(encoded) > _MAX_REQUEST_BYTES:
            raise PackageUpdateHealthError(
                "host-control request exceeded its structural bound"
            )
        result = self._runner(
            self._argv(self._timeout_seconds),
            encoded,
            float(self._timeout_seconds),
            self._max_result_bytes,
        )
        return self._parse(result, request)

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
        self, result: BoundedProcessResult, request: PackageUpdateHealthRequest
    ) -> HostHealthResult:
        # Every transport-level failure raises rather than returning a
        # result. The orchestrator turns that into UNKNOWN, which is the only
        # truthful reading of "the answer never arrived": it is not a pass,
        # it is not a failure, and re-reading is safe.
        if result.timed_out:
            raise PackageUpdateHealthError("host-control request timed out")
        if result.output_exceeded:
            raise PackageUpdateHealthError(
                "host-control result exceeded its configured bound"
            )
        if result.returncode != 0 and not result.stdout:
            raise PackageUpdateHealthError("host-control SSH execution failed")
        try:
            payload = json.loads(result.stdout.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise PackageUpdateHealthError(
                "host-control returned a malformed response"
            ) from exc
        return self._parse_payload(payload, request)

    def _parse_payload(
        self, payload: Any, request: PackageUpdateHealthRequest
    ) -> HostHealthResult:
        if not isinstance(payload, Mapping) or payload.get("response_version") != 1:
            raise PackageUpdateHealthError(
                "host-control returned an unsupported response"
            )
        if payload.get("job_id") != request.job_id:
            raise PackageUpdateHealthError(
                "host-control answered a different package update job"
            )
        if payload.get("ok") is not True:
            error = payload.get("error")
            classification = "unclassified"
            if isinstance(error, Mapping):
                classification = str(error.get("classification") or "unclassified")[
                    :100
                ]
            raise PackageUpdateHealthError(
                f"host-control reported a failure ({classification})"
            )
        contract = payload.get("health_contract")
        if not isinstance(contract, Mapping):
            raise PackageUpdateHealthError(
                "host-control returned no health contract identity"
            )
        revision = contract.get("revision")
        fingerprint = contract.get("fingerprint")
        if type(revision) is not int or not isinstance(fingerprint, str):
            raise PackageUpdateHealthError(
                "host-control returned a malformed health contract identity"
            )
        raw_probes = payload.get("probes")
        if not isinstance(raw_probes, list) or not raw_probes:
            raise PackageUpdateHealthError(
                "host-control returned no probe results"
            )
        if len(raw_probes) > MAX_HEALTH_PROBES:
            raise PackageUpdateHealthError(
                "host-control returned more probe results than a contract may hold"
            )
        probes: list[HostProbeResult] = []
        for raw in raw_probes:
            probes.append(self._parse_probe(raw))
        return HostHealthResult(
            contract_revision=revision,
            contract_fingerprint=fingerprint,
            probes=tuple(probes),
            reason=(
                str(payload["reason"])[:100]
                if isinstance(payload.get("reason"), str)
                else None
            ),
        )

    @staticmethod
    def _parse_probe(raw: Any) -> HostProbeResult:
        if not isinstance(raw, Mapping) or set(raw) != {
            "index",
            "kind",
            "target",
            "outcome",
            "reason",
        }:
            raise PackageUpdateHealthError(
                "host-control returned a malformed probe result"
            )
        index = raw["index"]
        if type(index) is not int or not 0 <= index < MAX_HEALTH_PROBES:
            raise PackageUpdateHealthError(
                "host-control returned an out-of-range probe index"
            )
        try:
            kind = HealthProbeKind(str(raw["kind"]))
        except ValueError as exc:
            raise PackageUpdateHealthError(
                "host-control returned an unsupported probe kind"
            ) from exc
        target = raw["target"]
        if (
            not isinstance(target, str)
            or not 1 <= len(target) <= MAX_HEALTH_PROBE_TARGET_LENGTH
        ):
            raise PackageUpdateHealthError(
                "host-control returned an out-of-bounds probe target"
            )
        outcome = _OUTCOMES.get(str(raw["outcome"]))
        if outcome is None:
            raise PackageUpdateHealthError(
                "host-control returned an unknown probe outcome"
            )
        reason = raw["reason"]
        if not isinstance(reason, str) or reason not in HOST_PROBE_REASONS:
            raise PackageUpdateHealthError(
                "host-control returned a probe reason outside its bounded taxonomy"
            )
        return HostProbeResult(
            probe_index=index,
            kind=kind,
            target=target,
            outcome=outcome,
            reason=reason,
        )
