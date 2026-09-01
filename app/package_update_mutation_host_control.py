"""Dark bounded SSH transport for crash-safe real package mutation.

**Not production-reachable and not deployed.** No production configuration,
key, or `authorized_keys` entry exists for this channel: `app/inventory_runtime.py`
never constructs it, and neither bootstrap nor the product updater installs
its helper or key. It is instantiated only by hermetic tests in this stage.

It is a separate, purpose-specific client and logical privilege boundary
from every other typed host-control transport in this repository: it targets
the not-deployed `deploy/hubinet-package-mutation-helper.py` forced-command
boundary and nothing else. It reuses the same bounded-process runner every
other typed host-control transport uses -- one implementation of "run a
fixed argv with a deadline and a byte cap", not a fourth one -- and the same
pinned-key, no-password, no-forwarding, no-arbitrary-command SSH posture.

**Two deliberate timeout budgets.** The backend calls
`execute_exact_package_mutation` and `seal_mutation_never_submitted` while it
still holds the authority store's one writer lock, so both are bounded by
`submission_timeout_seconds`, whose ceiling
(`contention_policy.MAX_PACKAGE_MUTATION_SUBMISSION_TIMEOUT_SECONDS`) is
enforced in the constructor, before any SSH or subprocess execution, and is
sized so an ordinary concurrent writer's wait budget can never be exhausted
by a healthy one of them. Neither operation waits for the package command:
the helper journals `submitted` and detaches its runner. The read-only
`prepare_exact_package_mutation` and `inspect_package_mutation_state` run
strictly outside that lock and use the longer `timeout_seconds` instead,
because an APT metadata refresh plus simulation can legitimately take
minutes -- which is exactly why they may never happen inside the lock.
"""

from __future__ import annotations

from collections.abc import Mapping
import json
from pathlib import Path
import re
from typing import Any

from app.inventory import (
    HostMutationState,
    PackageScanFailure,
    PackageUpdateMutationRequest,
)
from app.inventory.contention_policy import (
    MAX_PACKAGE_MUTATION_SUBMISSION_TIMEOUT_SECONDS,
)
from app.package_scan import HostScanFailure
from app.package_scan_host_control import ProcessRunner, _bounded_process_runner
from app.package_update_mutation import HostMutationEvidence, HostMutationResult


_HOST_RE = re.compile(r"[A-Za-z0-9_.:-]+")
_USER_RE = re.compile(r"[A-Za-z0-9_.-]+")
_FINGERPRINT_RE = re.compile(r"[0-9a-f]{64}")
_MAX_REQUEST_BYTES = 1024 * 1024

#: Host error classifications that map onto the shared package-scan failure
#: vocabulary. Everything else the helper can report is a mutation-specific
#: condition the orchestrator must treat as uncertainty, never as a clean
#: pre-mutation failure, so it is classified as EXECUTION_FAILED and the
#: helper's own bounded message is preserved verbatim.
_FAILURE_CLASSES = {
    failure.value: failure for failure in PackageScanFailure
}


class SshPackageUpdateMutationHostControl:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        user: str,
        private_key_path: Path,
        known_hosts_path: Path,
        timeout_seconds: int,
        submission_timeout_seconds: int,
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
        if (
            type(submission_timeout_seconds) is not int
            or not 1
            <= submission_timeout_seconds
            <= MAX_PACKAGE_MUTATION_SUBMISSION_TIMEOUT_SECONDS
        ):
            # Enforced before any SSH or subprocess execution: a submission
            # round trip long enough to legitimately exhaust the authority
            # store's writer wait budget must be impossible to configure.
            raise ValueError("host-control submission timeout is invalid")
        if (
            type(max_result_bytes) is not int
            or not 1024 <= max_result_bytes <= 64 * 1024 * 1024
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
        self._submission_timeout_seconds = submission_timeout_seconds
        self._max_result_bytes = max_result_bytes
        self._runner = runner

    # ------------------------------------------------------------------
    # The four typed operations
    # ------------------------------------------------------------------

    def prepare_exact_package_mutation(
        self, request: PackageUpdateMutationRequest
    ) -> HostMutationResult:
        return self._call(
            request, "prepare_exact_package_mutation", self._timeout_seconds
        )

    def execute_exact_package_mutation(
        self,
        request: PackageUpdateMutationRequest,
        *,
        prepared_evidence_digest: str,
    ) -> HostMutationResult:
        if not isinstance(
            prepared_evidence_digest, str
        ) or not _FINGERPRINT_RE.fullmatch(prepared_evidence_digest):
            raise HostScanFailure(
                PackageScanFailure.EXECUTION_FAILED,
                "prepared evidence digest is invalid",
            )
        return self._call(
            request,
            "execute_exact_package_mutation",
            self._submission_timeout_seconds,
            prepared_evidence_digest=prepared_evidence_digest,
        )

    def seal_mutation_never_submitted(
        self, request: PackageUpdateMutationRequest
    ) -> HostMutationResult:
        return self._call(
            request,
            "seal_mutation_never_submitted",
            self._submission_timeout_seconds,
        )

    def inspect_package_mutation_state(
        self, request: PackageUpdateMutationRequest
    ) -> HostMutationResult:
        return self._call(
            request, "inspect_package_mutation_state", self._timeout_seconds
        )

    # ------------------------------------------------------------------
    # One bounded round trip
    # ------------------------------------------------------------------

    def _call(
        self,
        request: PackageUpdateMutationRequest,
        operation: str,
        timeout_seconds: int,
        *,
        prepared_evidence_digest: str | None = None,
    ) -> HostMutationResult:
        payload = build_host_request(
            request, operation, prepared_evidence_digest=prepared_evidence_digest
        )
        encoded = json.dumps(
            payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
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
            argv, encoded, float(timeout_seconds), self._max_result_bytes
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
            decoded = json.loads(result.stdout.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise HostScanFailure(
                PackageScanFailure.EXECUTION_FAILED,
                "host-control returned a malformed response",
            ) from exc
        return parse_host_response(decoded, payload)


def build_host_request(
    request: PackageUpdateMutationRequest,
    operation: str,
    *,
    prepared_evidence_digest: str | None = None,
) -> dict[str, Any]:
    """Render the exact typed request the dark mutation boundary accepts.

    Structural only: every value is an authority fact carried through
    unchanged. The approved package material travels so the host can REFUSE a
    mutation whose starting state drifted -- it never becomes argv, an
    option, or command text.
    """

    return {
        "request_version": 1,
        "operation": operation,
        "target": {
            "vmid": request.vmid,
            "expected_node": request.expected_node,
        },
        "context": {
            "backend_instance_id": request.backend_instance_id,
            "job_id": request.job_id,
            "resource_id": request.resource_id,
            "binding_id": request.binding_id,
            "locator_generation": request.locator_generation,
            "resource_continuity_revision": request.resource_continuity_revision,
        },
        "operation_identity": {
            "mutation_operation_id": request.mutation_operation_id,
            "plan_fingerprint": request.plan_fingerprint,
        },
        "expected_packages": [
            {
                "package_name": package_name,
                "architecture": architecture,
                "installed_version": installed_version,
                "candidate_version": candidate_version,
            }
            for package_name, architecture, installed_version, candidate_version in (
                request.packages
            )
        ],
        "prepared_evidence_digest": prepared_evidence_digest,
    }


def parse_host_response(
    payload: Any, request_payload: Mapping[str, Any]
) -> HostMutationResult:
    """Parse one bounded host response strictly, or fail closed.

    The response must answer the exact request that was sent: same context,
    same operation identity. A response that does not is never attributed to
    this operation.
    """

    if not isinstance(payload, Mapping) or payload.get("response_version") != 1:
        raise HostScanFailure(
            PackageScanFailure.EXECUTION_FAILED,
            "host-control returned an unsupported response",
        )
    expected_context = dict(request_payload["context"])
    context = payload.get("context")
    if not isinstance(context, Mapping) or dict(context) != expected_context:
        raise HostScanFailure(
            PackageScanFailure.STALE_TARGET,
            "host-control response context does not match the mutation request",
        )
    expected_operation_id = request_payload["operation_identity"][
        "mutation_operation_id"
    ]
    if payload.get("mutation_operation_id") != expected_operation_id:
        raise HostScanFailure(
            PackageScanFailure.STALE_TARGET,
            "host-control answered about a different package mutation operation",
        )

    if payload.get("ok") is not True:
        error = payload.get("error")
        if not isinstance(error, Mapping):
            raise HostScanFailure(
                PackageScanFailure.EXECUTION_FAILED,
                "host-control returned an unclassified failure",
            )
        classification = str(error.get("classification", ""))
        failure = _FAILURE_CLASSES.get(
            classification, PackageScanFailure.EXECUTION_FAILED
        )
        message = str(error.get("message") or "package mutation failed")[:500]
        raise HostScanFailure(failure, message)

    try:
        state = HostMutationState(str(payload.get("operation_state")))
    except ValueError as exc:
        raise HostScanFailure(
            PackageScanFailure.EXECUTION_FAILED,
            "host-control returned an unknown package mutation state",
        ) from exc
    running = payload.get("running")
    if not isinstance(running, bool):
        raise HostScanFailure(
            PackageScanFailure.EXECUTION_FAILED,
            "host-control returned a malformed liveness signal",
        )
    reason = payload.get("reason")
    if reason is not None and not isinstance(reason, str):
        raise HostScanFailure(
            PackageScanFailure.EXECUTION_FAILED,
            "host-control returned a malformed reason",
        )
    evidence = payload.get("evidence")
    if evidence is not None and not isinstance(evidence, Mapping):
        raise HostScanFailure(
            PackageScanFailure.EXECUTION_FAILED,
            "host-control returned malformed package mutation evidence",
        )

    if state is HostMutationState.INTENT and evidence is not None:
        return _parse_prepared(state, running, reason, evidence, expected_operation_id)
    if (
        state
        in (
            HostMutationState.TERMINAL_SUCCESS,
            HostMutationState.TERMINAL_FAILURE,
        )
        and evidence is not None
    ):
        return _parse_terminal(state, running, reason, evidence, expected_operation_id)
    if state in (
        HostMutationState.TERMINAL_SUCCESS,
        HostMutationState.TERMINAL_FAILURE,
    ):
        raise HostScanFailure(
            PackageScanFailure.EXECUTION_FAILED,
            "host-control reported a terminal mutation without its evidence",
        )
    return HostMutationResult(
        mutation_operation_id=expected_operation_id,
        state=state,
        running=running,
        reason=reason,
    )


def _require_str(evidence: Mapping[str, Any], field: str) -> str:
    value = evidence.get(field)
    if not isinstance(value, str):
        raise HostScanFailure(
            PackageScanFailure.EXECUTION_FAILED,
            "host-control returned malformed package mutation evidence",
        )
    return value


def _parse_prepared(
    state: HostMutationState,
    running: bool,
    reason: str | None,
    evidence: Mapping[str, Any],
    mutation_operation_id: str,
) -> HostMutationResult:
    digest = _require_str(evidence, "prepared_evidence_digest")
    if not _FINGERPRINT_RE.fullmatch(digest):
        raise HostScanFailure(
            PackageScanFailure.EXECUTION_FAILED,
            "host-control returned a malformed prepared evidence digest",
        )
    return HostMutationResult(
        mutation_operation_id=mutation_operation_id,
        state=state,
        running=running,
        reason=reason,
        os_release=_require_str(evidence, "os_release"),
        native_architecture=_require_str(evidence, "native_architecture"),
        installed_inventory=_require_str(evidence, "installed_inventory"),
        simulation_stdout=_require_str(evidence, "simulation_stdout"),
        prepared_evidence_digest=digest,
    )


def _parse_terminal(
    state: HostMutationState,
    running: bool,
    reason: str | None,
    evidence: Mapping[str, Any],
    mutation_operation_id: str,
) -> HostMutationResult:
    exit_code = evidence.get("exit_code")
    timed_out = evidence.get("timed_out")
    if type(exit_code) is not int or not isinstance(timed_out, bool):
        raise HostScanFailure(
            PackageScanFailure.EXECUTION_FAILED,
            "host-control returned malformed package mutation evidence",
        )
    output_tail = evidence.get("output_tail", "")
    if not isinstance(output_tail, str):
        raise HostScanFailure(
            PackageScanFailure.EXECUTION_FAILED,
            "host-control returned malformed package mutation evidence",
        )
    return HostMutationResult(
        mutation_operation_id=mutation_operation_id,
        state=state,
        running=running,
        reason=reason,
        evidence=HostMutationEvidence(
            exit_code=exit_code,
            timed_out=timed_out,
            pre_native_architecture=_require_str(
                evidence, "pre_native_architecture"
            ),
            pre_installed_inventory=_require_str(
                evidence, "pre_installed_inventory"
            ),
            post_native_architecture=_require_str(
                evidence, "post_native_architecture"
            ),
            post_installed_inventory=_require_str(
                evidence, "post_installed_inventory"
            ),
            output_tail=output_tail,
        ),
    )
