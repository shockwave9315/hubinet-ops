"""NEXT-C: the execution-time APT plan equality gate.

Covers the missing proof between an approved job's confirmed pre-update
snapshot and (future, unimplemented) package mutation: a fresh
metadata-refreshed execution-time APT simulation must exactly match this
job's frozen approved material before any future stage may mutate packages.

Nothing here performs, or can reach, a real PVE operation, a real APT
mutation, a healthcheck, or a rollback. This stage never advances the
checkpoint past ``snapshot_confirmed``.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType
import uuid

import pytest

from app.inventory import (
    AuthorityConflict,
    InventoryAuthority,
    PackageScanFailure,
    PackageScanPackage,
    PackageUpdateCheckpoint,
    PackageUpdateEventType,
    PackageUpdateExecutionOutcome,
    PackageUpdateJobStatus,
)
from app.package_scan import HostScanFailure
from app.package_scan_host_control import BoundedProcessResult
from app.package_update_execution import (
    ExecutionGateStatus,
    HostExecutionResult,
    expected_execution_host_context,
    run_package_update_execution_gate,
)
from app.package_update_execution_host_control import (
    SshPackageUpdateExecutionHostControl,
)
from tests.test_package_update_job_authority import (
    _add_approved_resource,
    _approved_system,
    _issue,
)
from tests.test_package_update_snapshot_safety import _canonical


ROOT = Path(__file__).resolve().parents[1]
HELPER_PATH = ROOT / "deploy" / "hubinet-package-update-helper.py"


def _load_helper() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "hubinet_package_update_helper", HELPER_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


helper = _load_helper()


SIMULATION = """\
Reading package lists...
Building dependency tree...
Calculating upgrade...
Inst apt [2.6.1] (2.6.2 Debian:12/stable-security [amd64])
Inst zlib1g [1:1.2.13.dfsg-1] (1:1.2.13.dfsg-2 Debian:12/stable-security [amd64])
2 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.
"""


def _simulation_for(packages) -> str:
    lines = [
        f"Inst {p.package_name} [{p.installed_version}] "
        f"({p.candidate_version} Debian:12/stable-security [{p.architecture}])"
        for p in packages
    ]
    lines.append(
        f"{len(packages)} upgraded, 0 newly installed, 0 to remove and 0 not upgraded."
    )
    return "\n".join(lines) + "\n"


def _ready_job(tmp_path: Path, *, packages=None):
    """One package-update job advanced to a confirmed snapshot.

    This is the exact boundary the execution gate lives on: after
    ``snapshot_confirmed``, before any package mutation.
    """

    clock, store, authority, resource, scan, approval = _approved_system(
        tmp_path, packages=packages
    )
    job = _issue(authority, resource, approval)
    authority.record_package_update_preflight_passed(job.job_id)
    job = authority.record_package_update_snapshot_intent(job.job_id)
    identity = authority.package_update_snapshot_identity(job.job_id)
    ownership = authority.package_update_snapshot_ownership(job.job_id)
    job = authority.confirm_package_update_snapshot(
        job.job_id, _canonical(ownership, identity)
    )
    assert job.checkpoint is PackageUpdateCheckpoint.SNAPSHOT_CONFIRMED
    assert job.status is PackageUpdateJobStatus.ACTIVE
    return clock, store, authority, resource, scan, approval, job


class FakeExecutionHostControl:
    """A typed, in-memory stand-in for the dark SSH transport."""

    def __init__(self, *, simulation_stdout=None, os_release='ID=debian\nVERSION_ID="12"\n',
                 raises=None, wrong_context=False, side_effect=None):
        self.simulation_stdout = simulation_stdout
        self.os_release = os_release
        self.raises = raises
        self.wrong_context = wrong_context
        self.side_effect = side_effect
        self.calls = 0

    def simulate_exact_update_plan(self, job) -> HostExecutionResult:
        self.calls += 1
        if self.side_effect is not None:
            self.side_effect(job)
        if self.raises is not None:
            raise self.raises
        context = dict(expected_execution_host_context(job))
        if self.wrong_context:
            context["job_id"] = str(uuid.uuid4())
        return HostExecutionResult(
            context=context,
            os_release=self.os_release,
            simulation_stdout=self.simulation_stdout,
        )


# ---------------------------------------------------------------------------
# 1-7: the equality decision itself (authority.evaluate_package_update_execution_plan)
# ---------------------------------------------------------------------------


def test_exact_match_passes_and_leaves_the_job_completely_untouched(
    tmp_path: Path,
) -> None:
    clock, store, authority, resource, scan, approval, job = _ready_job(tmp_path)
    outcome, decided = authority.evaluate_package_update_execution_plan(
        job.job_id, scan.packages
    )
    assert outcome is PackageUpdateExecutionOutcome.MATCHED
    assert decided.status is PackageUpdateJobStatus.ACTIVE
    assert decided.checkpoint is PackageUpdateCheckpoint.SNAPSHOT_CONFIRMED
    assert decided.mutation_may_have_started_at is None
    assert decided.mutation_completed_at is None
    assert decided.terminalized_at is None
    events = store.list_package_update_job_events(job.job_id)
    assert events[-1].event_type is PackageUpdateEventType.EXECUTION_PLAN_VERIFIED
    assert events[-1].level.value == "info"


@pytest.mark.parametrize(
    "mutate",
    (
        lambda p: p + (PackageScanPackage("curl", "amd64", "7.0", "7.1"),),  # added
        lambda p: p[:-1],  # removed
        lambda p: (
            PackageScanPackage(p[0].package_name, "i386", p[0].installed_version, p[0].candidate_version),
            *p[1:],
        ),  # architecture-only difference
        lambda p: (
            PackageScanPackage(p[0].package_name, p[0].architecture, "9.9.9", p[0].candidate_version),
            *p[1:],
        ),  # installed-version difference
        lambda p: (
            PackageScanPackage(p[0].package_name, p[0].architecture, p[0].installed_version, "9.9.9"),
            *p[1:],
        ),  # candidate-version difference
        lambda p: (),  # zero-plan where the job was non-empty
    ),
    ids=("added", "removed", "architecture", "installed", "candidate", "empty"),
)
def test_every_material_mismatch_dimension_blocks_the_job(
    tmp_path: Path, mutate
) -> None:
    clock, store, authority, resource, scan, approval, job = _ready_job(tmp_path)
    fresh = mutate(scan.packages)
    outcome, decided = authority.evaluate_package_update_execution_plan(job.job_id, fresh)
    assert outcome is PackageUpdateExecutionOutcome.MISMATCHED
    assert decided.status is PackageUpdateJobStatus.BLOCKED
    assert decided.checkpoint is PackageUpdateCheckpoint.SNAPSHOT_CONFIRMED
    assert decided.mutation_may_have_started_at is None
    assert decided.terminalized_at is not None
    assert decided.terminal_reason is not None
    events = store.list_package_update_job_events(job.job_id)
    assert events[-1].event_type is PackageUpdateEventType.EXECUTION_PLAN_MISMATCH
    assert events[-1].level.value == "error"

    # The global destructive slot is released: a second, independent
    # approved job can now be issued.
    other_resource, other_scan, other_approval = _add_approved_resource(store, authority)
    other_job = _issue(authority, other_resource, other_approval)
    assert other_job.status is PackageUpdateJobStatus.ACTIVE


def test_metadata_only_difference_still_matches(tmp_path: Path) -> None:
    # Regression J: origin/description/security differing from the job's
    # frozen rows must never be material.
    clock, store, authority, resource, scan, approval, job = _ready_job(tmp_path)
    relabelled = tuple(
        PackageScanPackage(
            p.package_name,
            p.architecture,
            p.installed_version,
            p.candidate_version,
            origin="a different repository label",
            description="a different description",
            security=None,
        )
        for p in scan.packages
    )
    outcome, decided = authority.evaluate_package_update_execution_plan(
        job.job_id, relabelled
    )
    assert outcome is PackageUpdateExecutionOutcome.MATCHED
    assert decided.checkpoint is PackageUpdateCheckpoint.SNAPSHOT_CONFIRMED


def test_row_order_never_affects_the_decision(tmp_path: Path) -> None:
    # Regression I.
    clock, store, authority, resource, scan, approval, job = _ready_job(tmp_path)
    outcome, decided = authority.evaluate_package_update_execution_plan(
        job.job_id, tuple(reversed(scan.packages))
    )
    assert outcome is PackageUpdateExecutionOutcome.MATCHED


def test_gate_refuses_a_job_that_has_not_reached_snapshot_confirmed(
    tmp_path: Path,
) -> None:
    clock, store, authority, resource, scan, approval = _approved_system(tmp_path)
    job = _issue(authority, resource, approval)
    with pytest.raises(AuthorityConflict, match="not awaiting"):
        authority.evaluate_package_update_execution_plan(job.job_id, scan.packages)


def test_gate_refuses_a_terminal_job_and_never_reopens_it(tmp_path: Path) -> None:
    # Regression 13: the job goes terminal (interrupted at startup, say) for
    # a reason unrelated to this gate; the gate must never resurrect it.
    clock, store, authority, resource, scan, approval, job = _ready_job(tmp_path)
    authority.recover_interrupted_package_update_jobs()
    refreshed = store.package_update_job(job.job_id)
    assert refreshed.status is PackageUpdateJobStatus.INTERRUPTED

    with pytest.raises(AuthorityConflict, match="terminal"):
        authority.evaluate_package_update_execution_plan(job.job_id, scan.packages)
    still = store.package_update_job(job.job_id)
    assert still.status is PackageUpdateJobStatus.INTERRUPTED
    assert still.terminal_reason == refreshed.terminal_reason


def test_second_invocation_after_mismatch_is_never_re_executed(tmp_path: Path) -> None:
    # Regression 15: an idempotent retry against an already-terminal job
    # never re-decides or mutates a package.
    clock, store, authority, resource, scan, approval, job = _ready_job(tmp_path)
    mismatched = scan.packages[:-1]
    authority.evaluate_package_update_execution_plan(job.job_id, mismatched)
    with pytest.raises(AuthorityConflict, match="terminal"):
        authority.evaluate_package_update_execution_plan(job.job_id, scan.packages)


def test_approval_invalidation_covers_architecture_exactly_like_a_version(
    tmp_path: Path,
) -> None:
    """Regression for PRODUCT.md rule 2 / this stage's section 28.

    Changing only architecture must invalidate an approved plan exactly like
    changing a candidate version: the material fingerprint differs, so a
    later scan showing only the architecture changed is no longer the
    approved plan.
    """

    clock, store, authority, resource, scan, approval = _approved_system(tmp_path)
    reforeign = tuple(
        PackageScanPackage(
            p.package_name,
            "i386" if p.architecture != "i386" else "arm64",
            p.installed_version,
            p.candidate_version,
            p.origin,
            p.description,
            p.security,
        )
        for p in scan.packages
    )
    new_run = authority.issue_package_scan(resource.resource_id)
    changed = authority.finalize_successful_package_scan(
        new_run.scan_run_id,
        os_id="debian",
        os_version="12",
        packages=reforeign,
        reboot_required=None,
    )
    assert changed.plan_fingerprint != approval.approved_plan_fingerprint
    with pytest.raises(AuthorityConflict, match="does not match the approved plan"):
        authority.issue_package_update_job(
            resource.resource_id, approval.approval_id, str(uuid.uuid4())
        )


# ---------------------------------------------------------------------------
# 8-11: host/parse failures never match and never mutate
# ---------------------------------------------------------------------------


def test_orchestrator_matches_and_appends_no_mutation_checkpoint(tmp_path: Path) -> None:
    clock, store, authority, resource, scan, approval, job = _ready_job(tmp_path)
    host_control = FakeExecutionHostControl(simulation_stdout=_simulation_for(scan.packages))
    result = run_package_update_execution_gate(authority, job.job_id, host_control)
    assert result.status is ExecutionGateStatus.MATCHED
    assert result.job.checkpoint is PackageUpdateCheckpoint.SNAPSHOT_CONFIRMED
    assert result.job.mutation_may_have_started_at is None
    assert host_control.calls == 1


def test_orchestrator_mismatch_terminalizes_and_releases_the_slot(tmp_path: Path) -> None:
    clock, store, authority, resource, scan, approval, job = _ready_job(tmp_path)
    host_control = FakeExecutionHostControl(
        simulation_stdout=_simulation_for(scan.packages[:-1])
    )
    result = run_package_update_execution_gate(authority, job.job_id, host_control)
    assert result.status is ExecutionGateStatus.MISMATCHED
    assert result.job.status is PackageUpdateJobStatus.BLOCKED
    assert result.job.mutation_may_have_started_at is None


def test_malformed_simulation_fails_closed_never_matches(tmp_path: Path) -> None:
    clock, store, authority, resource, scan, approval, job = _ready_job(tmp_path)
    host_control = FakeExecutionHostControl(simulation_stdout="not a real apt simulation\n")
    result = run_package_update_execution_gate(authority, job.job_id, host_control)
    assert result.status is ExecutionGateStatus.HOST_FAILURE
    assert result.failure_class is PackageScanFailure.MALFORMED_PLAN
    refreshed = store.package_update_job(job.job_id)
    assert refreshed.status is PackageUpdateJobStatus.ACTIVE
    assert refreshed.checkpoint is PackageUpdateCheckpoint.SNAPSHOT_CONFIRMED


def test_apt_busy_fails_closed_never_matches(tmp_path: Path) -> None:
    clock, store, authority, resource, scan, approval, job = _ready_job(tmp_path)
    host_control = FakeExecutionHostControl(
        raises=HostScanFailure(PackageScanFailure.PACKAGE_MANAGER_BUSY, "APT or dpkg is busy")
    )
    result = run_package_update_execution_gate(authority, job.job_id, host_control)
    assert result.status is ExecutionGateStatus.HOST_FAILURE
    assert result.failure_class is PackageScanFailure.PACKAGE_MANAGER_BUSY
    assert store.package_update_job(job.job_id).status is PackageUpdateJobStatus.ACTIVE


def test_metadata_refresh_failure_fails_closed_never_matches(tmp_path: Path) -> None:
    clock, store, authority, resource, scan, approval, job = _ready_job(tmp_path)
    host_control = FakeExecutionHostControl(
        raises=HostScanFailure(
            PackageScanFailure.METADATA_REFRESH_FAILED, "APT metadata refresh failed"
        )
    )
    result = run_package_update_execution_gate(authority, job.job_id, host_control)
    assert result.status is ExecutionGateStatus.HOST_FAILURE
    assert result.failure_class is PackageScanFailure.METADATA_REFRESH_FAILED
    assert store.package_update_job(job.job_id).status is PackageUpdateJobStatus.ACTIVE


def test_host_timeout_is_deterministic_and_never_mutates(tmp_path: Path) -> None:
    clock, store, authority, resource, scan, approval, job = _ready_job(tmp_path)
    host_control = FakeExecutionHostControl(raises=TimeoutError("timed out"))
    first = run_package_update_execution_gate(authority, job.job_id, host_control)
    second = run_package_update_execution_gate(authority, job.job_id, host_control)
    assert first.status is second.status is ExecutionGateStatus.HOST_FAILURE
    assert first.failure_class is second.failure_class is PackageScanFailure.TIMEOUT
    assert store.package_update_job(job.job_id).status is PackageUpdateJobStatus.ACTIVE


def test_stale_host_response_context_is_a_host_failure(tmp_path: Path) -> None:
    clock, store, authority, resource, scan, approval, job = _ready_job(tmp_path)
    host_control = FakeExecutionHostControl(
        simulation_stdout=_simulation_for(scan.packages), wrong_context=True
    )
    result = run_package_update_execution_gate(authority, job.job_id, host_control)
    assert result.status is ExecutionGateStatus.HOST_FAILURE
    assert result.failure_class is PackageScanFailure.STALE_TARGET


# ---------------------------------------------------------------------------
# 12-13: authority/job state changing while the host round trip is "in flight"
# ---------------------------------------------------------------------------


def test_authority_going_stale_during_the_host_round_trip_refuses(tmp_path: Path) -> None:
    clock, store, authority, resource, scan, approval, job = _ready_job(tmp_path)

    def _go_stale(_job) -> None:
        authority.rotate_transport_trust(resource.inventory_source_id)

    host_control = FakeExecutionHostControl(
        simulation_stdout=_simulation_for(scan.packages), side_effect=_go_stale
    )
    result = run_package_update_execution_gate(authority, job.job_id, host_control)
    assert result.status is ExecutionGateStatus.AUTHORITY_STALE
    refreshed = store.package_update_job(job.job_id)
    assert refreshed.status is PackageUpdateJobStatus.ACTIVE
    assert refreshed.checkpoint is PackageUpdateCheckpoint.SNAPSHOT_CONFIRMED


def test_job_going_terminal_during_the_host_round_trip_is_never_overwritten(
    tmp_path: Path,
) -> None:
    clock, store, authority, resource, scan, approval, job = _ready_job(tmp_path)

    def _terminalize(_job) -> None:
        authority.recover_interrupted_package_update_jobs()

    host_control = FakeExecutionHostControl(
        simulation_stdout=_simulation_for(scan.packages[:-1]), side_effect=_terminalize
    )
    result = run_package_update_execution_gate(authority, job.job_id, host_control)
    assert result.status is ExecutionGateStatus.AUTHORITY_STALE
    refreshed = store.package_update_job(job.job_id)
    # Still exactly the interruption the side effect caused -- never
    # overwritten with a "mismatched" terminal reason.
    assert refreshed.status is PackageUpdateJobStatus.INTERRUPTED


# ---------------------------------------------------------------------------
# 14: crash boundary -- a MATCHED pass creates no new state startup must know
# ---------------------------------------------------------------------------


def test_a_matched_pass_is_still_safely_interrupted_by_startup_recovery(
    tmp_path: Path,
) -> None:
    clock, store, authority, resource, scan, approval, job = _ready_job(tmp_path)
    outcome, _ = authority.evaluate_package_update_execution_plan(job.job_id, scan.packages)
    assert outcome is PackageUpdateExecutionOutcome.MATCHED

    recovered = authority.recover_interrupted_package_update_jobs()
    assert job.job_id in recovered
    refreshed = store.package_update_job(job.job_id)
    assert refreshed.status is PackageUpdateJobStatus.INTERRUPTED
    assert refreshed.mutation_may_have_started_at is None


# ---------------------------------------------------------------------------
# The dark host-control transport and forced-command helper.
# ---------------------------------------------------------------------------


def test_ssh_host_control_pins_key_and_sends_the_job_owned_context(
    tmp_path: Path,
) -> None:
    clock, store, authority, resource, scan, approval, job = _ready_job(tmp_path)
    captured = {}

    def runner(argv, input_bytes, timeout, max_output):
        captured.update(argv=argv, input_bytes=input_bytes)
        request = json.loads(input_bytes)
        response = {
            "response_version": 1,
            "ok": True,
            "context": request["context"],
            "os_release": 'ID=debian\nVERSION_ID="12"\n',
            "simulation": {"returncode": 0, "stdout": _simulation_for(scan.packages)},
        }
        return BoundedProcessResult(0, json.dumps(response).encode(), b"")

    client = SshPackageUpdateExecutionHostControl(
        host="192.0.2.11",
        port=22,
        user="hubinet-update-exec",
        private_key_path=tmp_path.resolve() / "id_ed25519",
        known_hosts_path=tmp_path.resolve() / "known_hosts",
        timeout_seconds=900,
        max_result_bytes=8 * 1024 * 1024,
        runner=runner,
    )
    result = client.simulate_exact_update_plan(job)
    assert result.simulation_stdout == _simulation_for(scan.packages)
    argv = captured["argv"]
    assert "StrictHostKeyChecking=yes" in argv
    assert argv[-1] == "hubinet-update-exec@192.0.2.11"
    sent = json.loads(captured["input_bytes"])
    assert sent["operation"] == "simulate_exact_update_plan"
    assert sent["context"] == {
        "job_id": job.job_id,
        "resource_id": job.resource_id,
        "binding_id": job.expected_binding_id,
        "locator_generation": job.expected_locator_generation,
        "resource_continuity_revision": job.expected_resource_continuity_revision,
    }


@pytest.mark.parametrize(
    "result, expected",
    (
        (BoundedProcessResult(-9, b"", b"", timed_out=True), TimeoutError),
        (BoundedProcessResult(-9, b"", b"", output_exceeded=True), HostScanFailure),
        (BoundedProcessResult(255, b"", b"ssh failed"), HostScanFailure),
    ),
)
def test_ssh_host_control_classifies_transport_bounds(
    tmp_path: Path, result: BoundedProcessResult, expected: type[Exception]
) -> None:
    client = SshPackageUpdateExecutionHostControl(
        host="pve-a",
        port=22,
        user="hubinet-update-exec",
        private_key_path=tmp_path.resolve() / "id_ed25519",
        known_hosts_path=tmp_path.resolve() / "known_hosts",
        timeout_seconds=900,
        max_result_bytes=8 * 1024 * 1024,
        runner=lambda *_args: result,
    )
    _, store, authority, resource, scan, approval, job = _ready_job(tmp_path)
    with pytest.raises(expected):
        client.simulate_exact_update_plan(job)


def _request(*, vmid=101, operation="simulate_exact_update_plan", expected_node="pve-a"):
    return {
        "request_version": 1,
        "operation": operation,
        "target": {"vmid": vmid, "expected_node": expected_node},
        "context": {
            "job_id": str(uuid.uuid4()),
            "resource_id": str(uuid.uuid4()),
            "binding_id": str(uuid.uuid4()),
            "locator_generation": 2,
            "resource_continuity_revision": 3,
        },
    }


def test_helper_accepts_only_typed_operation_and_strict_vmid() -> None:
    with pytest.raises(helper.RequestError, match="unknown"):
        helper.validate_request(_request(operation="scan_packages"))
    with pytest.raises(helper.RequestError, match="unknown"):
        helper.validate_request(_request(operation="apt-get upgrade"))
    for malformed in ("101", 0, -1, True, 99, 1_000_000_000):
        with pytest.raises(helper.RequestError, match="vmid"):
            helper.validate_request(_request(vmid=malformed))


def test_helper_never_contains_a_mutating_apt_or_dpkg_argv() -> None:
    text = HELPER_PATH.read_text(encoding="utf-8")
    for forbidden in (
        "apt-get install",
        "apt-get upgrade",
        "apt-get dist-upgrade",
        "apt-get remove",
        "apt-get autoremove",
        "apt full-upgrade",
        "dpkg -i",
        "dpkg --configure",
        '"apt-get", "install"',
        '"apt-get", "upgrade"',
        '"apt-get", "dist-upgrade"',
        '"dpkg"',
    ):
        assert forbidden not in text, forbidden


class FakeHelperRunner:
    def __init__(
        self,
        *,
        resource_type: str = "lxc",
        status: str = "running",
        node: str = "pve-a",
        local_node: str = "pve-a",
        os_release: str = 'ID=debian\nVERSION_ID="12"\n',
        apt_version: str = "apt 2.6.1 (amd64)\n",
        update_returncode: int = 0,
        update_stderr: str = "",
        simulation_returncode: int = 0,
        simulation_stdout: str = SIMULATION,
        simulation_stderr: str = "",
    ) -> None:
        self.resource_type = resource_type
        self.status = status
        self.node = node
        self.local_node = local_node
        self.os_release = os_release
        self.apt_version = apt_version
        self.update_returncode = update_returncode
        self.update_stderr = update_stderr
        self.simulation_returncode = simulation_returncode
        self.simulation_stdout = simulation_stdout
        self.simulation_stderr = simulation_stderr
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, argv, _timeout, _max_output):
        self.calls.append(argv)
        rendered = " ".join(argv)
        if argv[0] == "pvesh" and argv[2] == "/cluster/status":
            rows = [{"type": "node", "name": self.local_node, "local": 1}]
            return helper.CommandResult(0, json.dumps(rows).encode(), b"")
        if argv[0] == "pvesh" and argv[2] == "/cluster/resources":
            rows = [
                {"vmid": 101, "type": self.resource_type, "node": self.node, "status": self.status}
            ]
            return helper.CommandResult(0, json.dumps(rows).encode(), b"")
        if "/etc/os-release" in rendered:
            return helper.CommandResult(0, self.os_release.encode(), b"")
        if "apt-get --version" in rendered:
            return helper.CommandResult(0, self.apt_version.encode(), b"")
        if "update" in rendered and "apt-get" in rendered:
            return helper.CommandResult(
                self.update_returncode, b"", self.update_stderr.encode()
            )
        if "upgrade" in rendered:
            return helper.CommandResult(
                self.simulation_returncode,
                self.simulation_stdout.encode(),
                self.simulation_stderr.encode(),
            )
        raise AssertionError(f"unexpected command shape: {argv!r}")


def test_helper_success_uses_only_fixed_pvesh_and_pct_shapes_and_no_reboot_check() -> None:
    runner = FakeHelperRunner()
    response = helper.handle_request(_request(), runner=runner)
    assert response["ok"] is True
    assert "reboot_required" not in response
    assert all(call[0] in {"pvesh", "pct"} for call in runner.calls)
    assert any(call[-4:] == ("apt-get", "update", "-qq", "--error-on=any") for call in runner.calls)
    assert any(call[-3:] == ("apt-get", "-s", "upgrade") for call in runner.calls)
    assert not any("/var/run/reboot-required" in " ".join(call) for call in runner.calls)


@pytest.mark.parametrize(
    ("runner", "classification"),
    (
        (FakeHelperRunner(status="stopped"), "guest_unavailable"),
        (FakeHelperRunner(resource_type="qemu"), "unsupported_resource_type"),
        (FakeHelperRunner(node="pve-b"), "stale_target"),
        (FakeHelperRunner(os_release='ID=alpine\nVERSION_ID="3.20"\n'), "unsupported_os"),
        (
            FakeHelperRunner(update_returncode=100, update_stderr="repository unavailable"),
            "metadata_refresh_failed",
        ),
        (
            FakeHelperRunner(
                update_returncode=100,
                update_stderr="E: Could not get lock /var/lib/dpkg/lock-frontend",
            ),
            "package_manager_busy",
        ),
        (
            FakeHelperRunner(simulation_returncode=100, simulation_stderr="failed"),
            "simulation_failed",
        ),
    ),
)
def test_helper_classifies_ordinary_failures(runner, classification: str) -> None:
    response = helper.handle_request(_request(), runner=runner)
    assert response["ok"] is False
    assert response["error"]["classification"] == classification
    if classification in {"metadata_refresh_failed", "package_manager_busy"}:
        assert not any("upgrade" in " ".join(call) for call in runner.calls)


def test_helper_end_to_end_matches_the_authority_gate(tmp_path: Path) -> None:
    """The real dark helper's own output round-trips through the real parser
    and produces a MATCHED decision against a job built from the same
    packages -- proving parser sharing end to end, not just in isolation.
    """

    clock, store, authority, resource, scan, approval, job = _ready_job(tmp_path)
    runner = FakeHelperRunner(simulation_stdout=_simulation_for(scan.packages))
    response = helper.handle_request(_request(), runner=runner)
    assert response["ok"] is True

    from app.package_scan import parse_apt_simulation

    fresh = parse_apt_simulation(response["simulation"]["stdout"])
    outcome, decided = authority.evaluate_package_update_execution_plan(job.job_id, fresh)
    assert outcome is PackageUpdateExecutionOutcome.MATCHED
