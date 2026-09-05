"""Production activation: the explicit operator lifecycle, end to end.

This is the stage where an operator action can, for the first time, drive the
already-built internal stages all the way through a real package mutation. So
what these tests exercise is the COMPOSITION and its boundary, not the stages
themselves -- each of those already has its own adversarial suite, and this
file deliberately does not re-litigate them.

What is proved here:

- the happy path: explicit start -> issued -> snapshot -> execution-time plan
  equality -> mutation -> health PASS -> SUCCEEDED;
- every stop condition leaves the job ACTIVE, fenced, and idle, with nothing
  submitted and nothing fabricated;
- **no automatic rollback**, from a failed mutation, an unproven mutation, a
  FAILED health verdict, or an UNKNOWN one;
- **no retry policy**: one wake, one attempt, then the worker waits to be
  asked;
- the explicit rollback control, and the exact point its intent becomes
  durable -- before the operator is told it was accepted;
- restart at every durable uncertain checkpoint, with no duplicate
  destructive submission and no false success;
- the API/worker races the durable global single-flight has to settle.

Nothing here contacts a real Proxmox, LXC, SSH, systemd, or Docker. The
snapshot and health boundaries are typed in-memory fakes; the mutation and
rollback boundaries are the REAL dark helpers driven over a JSON round trip
through the production transports' own request builders and response parsers,
reused from the stage suites that already prove them.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import sqlite3
from threading import Barrier
import uuid

from fastapi.testclient import TestClient
import pytest

from app.inventory import (
    AuthorityConflict,
    HealthOutcome,
    HealthProbeKind,
    HealthProbeOutcome,
    InventoryAuthority,
    InventoryAuthorityStore,
    InventoryPublication,
    HostSubmissionState,
    ObservedSnapshot,
    PackageScanFailure,
    PackageUpdateCheckpoint,
    PackageUpdateIssuanceRefused,
    PackageUpdateJobStatus,
    ProductUpdateFenceError,
    product_update_fence_path,
)
from app.package_scan import HostScanFailure, HostScanResult, expected_host_context
from app.package_scan_scheduler import PackageScanScheduler
from app.inventory_runtime import PackageUpdateRuntime, create_read_only_app
from app.inventory_runtime_config import parse_r0_runtime_config
from app.package_update_health import (
    HostHealthResult,
    HostProbeResult,
    PackageUpdateHealthOrchestrator,
)
from app.package_update_mutation import PackageUpdateMutationOrchestrator
from app.package_update_rollback import PackageUpdateRollbackOrchestrator
from app.package_update_snapshot import (
    HostSnapshotResult,
    PackageUpdateSnapshotOrchestrator,
    SnapshotOperationOutcome,
    SnapshotTaskState,
    SnapshotTaskStatus,
)
from app.package_update_worker import (
    PACKAGE_UPDATE_WORKER_STOP_REASONS,
    PackageUpdateWorker,
    PackageUpdateWorkerCycleStatus,
)

from tests.test_package_update_execution_gate import (
    FakeExecutionHostControl,
    _inventory_for,
    _simulation_for,
)
from tests.test_package_update_job_authority import (
    HEALTH_PROBES,
    _add_approved_resource,
    _approved_system,
    _issue,
)
from tests.test_package_update_mutation import (
    FakeGuest,
    HelperBackedHostControl as MutationHostControl,
    _FakeMonotonic,
)
from tests.test_package_update_rollback import (
    FakePve,
    HelperBackedHostControl as RollbackHostControl,
)
from tests.test_package_update_snapshot_safety import (
    _break_incarnation_continuity_at_the_same_locator,
    _canonical,
    _current_entry,
    _foreign_entry,
)


UPID = "UPID:pve-a:0000A1B2:00C3D4E5:66000000:vzsnapshot:110:root@pam:"


# ===========================================================================
# Typed fakes for the two boundaries whose real helpers need a live PVE
# ===========================================================================


class ScriptedSnapshotHostControl:
    """A snapshot boundary that behaves like a healthy host by default.

    Mirrors the real host's journal-driven contract closely enough for the
    orchestrator's read-then-submit split to run truthfully: it reports
    ``absent`` until something is submitted, then keeps reporting whatever
    that submission produced -- exactly as a durable journal would.
    """

    def __init__(self, ownership, identity, journal, *, outcome=None):
        self._ownership = ownership
        self._identity = identity
        self._outcome = outcome or SnapshotOperationOutcome.COMPLETED
        #: Stands in for the host's own durable operation journal, and is
        #: shared across a simulated backend restart for exactly that reason:
        #: a real host does not forget that a submission crossed its door
        #: because the backend died, and a fake that did would make "never
        #: resubmit after a restart" unprovable rather than proven.
        self._journal = journal
        self.submit_calls = 0
        self.inspect_calls = 0
        self.seal_calls = 0
        #: What a fresh canonical listing reports. The rollback route reads
        #: this through the read-only inspection, so tests can model a listing
        #: that does not prove this job's snapshot.
        self.listing = None

    @property
    def _submitted(self):
        return self._journal.get("submitted")

    def _canonical_listing(self):
        if self.listing is not None:
            return self.listing
        return _canonical(self._ownership, self._identity)

    def _result(self, outcome):
        return HostSnapshotResult(
            outcome=outcome,
            snapshot_operation_id=self._identity.snapshot_operation_id,
            task_upid=UPID,
            task=SnapshotTaskStatus(
                upid=UPID, terminal=True, state=SnapshotTaskState.OK
            ),
            snapshots=self._canonical_listing(),
            submission_state=(
                HostSubmissionState.TERMINAL
                if outcome is SnapshotOperationOutcome.COMPLETED
                else HostSubmissionState.SUBMITTED
            ),
        )

    def ensure_pre_update_snapshot_submitted(self, **kwargs):
        self.submit_calls += 1
        outcome = self._outcome
        self._journal["submitted"] = self._result(outcome)
        return self._journal["submitted"]

    def inspect_job_snapshot_state(self, **kwargs):
        self.inspect_calls += 1
        if self._submitted is not None:
            return replace(self._submitted, snapshots=self._canonical_listing())
        return HostSnapshotResult(
            outcome=SnapshotOperationOutcome.UNCERTAIN,
            snapshot_operation_id=kwargs["snapshot_operation_id"],
            submission_state=HostSubmissionState.ABSENT,
            snapshots=self._canonical_listing(),
        )

    def seal_operation_never_submitted(self, **kwargs):
        self.seal_calls += 1
        return HostSnapshotResult(
            outcome=SnapshotOperationOutcome.NOT_SUBMITTED,
            snapshot_operation_id=kwargs["snapshot_operation_id"],
            submission_state=HostSubmissionState.SEALED_NOT_SUBMITTED,
            reason="host durably sealed this snapshot operation before submission",
        )


class ScriptedHealthHostControl:
    """A read-only health boundary with a scripted verdict per attempt."""

    def __init__(self, *outcomes, before_evaluate=None):
        #: One entry per attempt: "passed", "failed", "unknown", or an
        #: exception instance to raise.
        self._outcomes = list(outcomes) or ["passed"]
        self._before_evaluate = before_evaluate
        self.calls = 0

    def evaluate_health_contract(self, request):
        self.calls += 1
        if self._before_evaluate is not None:
            before_evaluate, self._before_evaluate = self._before_evaluate, None
            before_evaluate()
        outcome = self._outcomes.pop(0) if self._outcomes else "passed"
        if isinstance(outcome, BaseException):
            raise outcome
        if outcome == "unknown":
            # A whole-request failure the orchestrator classifies as UNKNOWN:
            # nothing was proven true and nothing was proven false.
            return HostHealthResult(
                contract_revision=request.health_contract_revision,
                contract_fingerprint=request.health_contract_fingerprint,
                probes=(),
                reason="guest_unavailable",
            )
        probes = tuple(
            HostProbeResult(
                probe_index=index,
                kind=probe.kind,
                target=probe.target,
                outcome=(
                    HealthProbeOutcome.PASSED
                    if outcome == "passed"
                    else HealthProbeOutcome.FAILED
                ),
                reason=_reason_for(probe.kind, outcome),
            )
            for index, probe in enumerate(request.probes)
        )
        return HostHealthResult(
            contract_revision=request.health_contract_revision,
            contract_fingerprint=request.health_contract_fingerprint,
            probes=probes,
        )


def _reason_for(kind: HealthProbeKind, outcome: str) -> str:
    if outcome == "passed":
        return {
            HealthProbeKind.SYSTEMD_UNIT_ACTIVE: "unit_active",
            HealthProbeKind.DOCKER_CONTAINER_RUNNING: "container_running",
            HealthProbeKind.DOCKER_CONTAINER_HEALTHY: "container_healthy",
        }[kind]
    return {
        HealthProbeKind.SYSTEMD_UNIT_ACTIVE: "unit_not_active",
        HealthProbeKind.DOCKER_CONTAINER_RUNNING: "container_not_running",
        HealthProbeKind.DOCKER_CONTAINER_HEALTHY: "container_unhealthy",
    }[kind]


# ===========================================================================
# The production composition under test
# ===========================================================================


class ProductionSystem:
    """One authority, one worker, five typed boundaries, no real host."""

    def __init__(self, tmp_path: Path, *, health=None, packages=None):
        (
            self.clock,
            self.store,
            self.authority,
            self.resource,
            self.scan,
            self.approval,
        ) = _approved_system(tmp_path, packages=packages)
        self.tmp_path = tmp_path
        self.snapshot_host = None
        self.execution_host = FakeExecutionHostControl(
            simulation_stdout=_simulation_for(self.scan.packages),
            installed_inventory=_inventory_for(self.scan.packages),
        )
        self.health_host = ScriptedHealthHostControl(*(health or ["passed"]))
        # The mutation and rollback boundaries are the REAL dark helpers, and
        # both need the job's own VMID and snapshot identity, so they are
        # built in `bind_snapshot_host` once a job exists.
        self.guest = None
        self.mutation_host = None
        self.pve = None
        self.rollback_host = None
        self.worker = None

    # -- lifecycle -----------------------------------------------------

    def build_worker(self) -> PackageUpdateWorker:
        """Build the worker against the current authority object.

        Called again after a simulated restart so the worker is rebuilt on
        the reopened store, exactly as a fresh process would build it.
        """

        self.worker = PackageUpdateWorker(
            self.authority,
            self.store,
            snapshot=PackageUpdateSnapshotOrchestrator(
                self.authority,
                self._snapshot_host(),
                sleep=lambda _s: None,
                monotonic=_FakeMonotonic(5.0),
                task_poll_timeout_seconds=30.0,
                task_poll_interval_seconds=5.0,
            ),
            execution_host_control=self.execution_host,
            mutation=PackageUpdateMutationOrchestrator(
                self.authority,
                self.mutation_host,
                sleep=lambda _s: None,
                monotonic=_FakeMonotonic(5.0),
                poll_timeout_seconds=30.0,
                poll_interval_seconds=5.0,
            ),
            rollback=PackageUpdateRollbackOrchestrator(
                self.authority,
                self.rollback_host,
                sleep=lambda _s: None,
                monotonic=_FakeMonotonic(5.0),
                task_poll_timeout_seconds=30.0,
                task_poll_interval_seconds=5.0,
            ),
            health=PackageUpdateHealthOrchestrator(self.authority, self.health_host),
        )
        return self.worker

    def _snapshot_host(self):
        if self.snapshot_host is None:
            raise AssertionError("a job must be issued before the snapshot boundary")
        return self.snapshot_host

    def bind_hosts(self, job_id: str, **kwargs) -> None:
        """Derive this job's durable identities and build every boundary.

        Everything here is derived from the job the authority just issued --
        the snapshot identity, the ownership metadata, the VMID -- so the
        fakes stand exactly where the real host would, answering about the
        exact operation this backend derived.
        """

        job = self.store.package_update_job(job_id)
        self.identity = self.authority.package_update_snapshot_identity(job_id)
        self.ownership = self.authority.package_update_snapshot_ownership(job_id)
        self._snapshot_journal: dict = {}
        self.snapshot_host = ScriptedSnapshotHostControl(
            self.ownership, self.identity, self._snapshot_journal, **kwargs
        )
        self.guest = FakeGuest(approved=job.packages, vmid=job.expected_vmid)
        self.guest.simulated = job.packages
        self.mutation_host = MutationHostControl(
            self.guest, self.tmp_path / "mutation-journal"
        )
        self.pve = FakePve(
            vmid=job.expected_vmid,
            snapshot_name=self.identity.snapshot_name,
            ownership=self.ownership,
        )
        self.rollback_host = RollbackHostControl(
            self.pve, self.tmp_path / "rollback-journal"
        )

    def complete_pve_rollback(self) -> None:
        """Drive the fake PVE to the state a finished rollback leaves.

        Upstream sets the `current` pseudo-entry's parent to the snapshot in
        its second locked phase, and the source snapshot itself survives --
        which is exactly why the source surviving is treated as no evidence
        at all by the completion proof.
        """

        self.pve.current_parent = self.identity.snapshot_name
        self.pve.task_status = "stopped"
        self.pve.task_exitstatus = "OK"

    def restart(self) -> None:
        """Simulate a backend restart: reopen the store, re-run recovery.

        The exact production order -- the authority's own startup recovery
        first, then a rebuilt worker inspecting whatever still owns the
        global slot.
        """

        path = self.store.path
        self.store.close()
        self.store = InventoryAuthorityStore(path, now=self.clock)
        self.authority = InventoryAuthority(self.store, now=self.clock)
        self.authority.recover_interrupted_package_update_jobs()
        if self.snapshot_host is not None:
            # Re-derive against the new authority object. The identity is
            # deterministic, so the SAME operation is re-observed rather than
            # a second one being started.
            self.snapshot_host = ScriptedSnapshotHostControl(
                self.ownership,
                self.identity,
                self._snapshot_journal,
                outcome=self.snapshot_host._outcome,
            )
        self.build_worker()

    # -- convenience ---------------------------------------------------

    def job(self, job_id: str):
        return self.store.package_update_job(job_id)

    def events(self, job_id: str):
        return [
            event.event_type.value
            for event in self.store.list_package_update_job_events(job_id, limit=200)
        ]


def _system(tmp_path: Path, **kwargs) -> ProductionSystem:
    return ProductionSystem(tmp_path, **kwargs)


def _start(system: ProductionSystem):
    """Issue a job the way the production route does, and script its host."""

    job = _issue(system.authority, system.resource, system.approval)
    system.bind_hosts(job.job_id)
    system.build_worker()
    return job


# ===========================================================================
# A. HAPPY PATH
# ===========================================================================


def test_explicit_start_drives_the_whole_lifecycle_to_succeeded(
    tmp_path: Path,
) -> None:
    """One operator action, five stages, one durable success.

    Every checkpoint on the way is a durable authority fact, and the last one
    is reachable only through a proven passing verdict against the contract
    the job froze at issuance.
    """

    system = _system(tmp_path)
    job = _start(system)
    assert job.checkpoint is PackageUpdateCheckpoint.ISSUED

    cycle = system.worker.run_once()

    assert cycle.status is PackageUpdateWorkerCycleStatus.TERMINAL
    final = system.job(job.job_id)
    assert final.status is PackageUpdateJobStatus.SUCCEEDED
    assert final.checkpoint is PackageUpdateCheckpoint.HEALTH_COMPLETED
    assert final.health_outcome is HealthOutcome.PASSED
    # Each stage really ran, in order, and left its own durable evidence.
    assert final.snapshot_confirmed_at is not None
    assert final.mutation_completed_at is not None
    assert final.health_completed_at is not None
    assert system.execution_host.calls == 1
    assert system.health_host.calls == 1
    # Every frozen probe carries its own proven result row.
    assert len(final.health_probe_results) == len(HEALTH_PROBES)
    assert all(
        result.outcome is HealthProbeOutcome.PASSED
        for result in final.health_probe_results
    )
    # Nothing rolled anything back on the way to success.
    assert final.rollback_operation_id is None
    assert "submit_same_job_rollback" not in system.rollback_host.calls


class _PostUpdateScanHost:
    def __init__(self, *, packages=(), failure: PackageScanFailure | None = None):
        self.packages = tuple(packages)
        self.failure = failure
        self.calls = 0

    def scan_packages(self, run):
        self.calls += 1
        if self.failure is not None:
            raise HostScanFailure(self.failure, "fresh post-update scan failed")
        return HostScanResult(
            context=expected_host_context(run),
            os_release='ID=debian\nVERSION_ID="12"\n',
            native_architecture="amd64\n",
            installed_inventory=_inventory_for(self.packages),
            simulation_stdout=_simulation_for(self.packages),
            reboot_required=None,
        )


@pytest.mark.parametrize(
    ("mode", "expected_status", "expected_pending"),
    [
        ("zero", "success", 0),
        ("nonzero", "success", 2),
        ("unknown", "failed", None),
    ],
)
def test_success_requests_one_real_scan_and_publishes_its_actual_result(
    tmp_path: Path,
    mode: str,
    expected_status: str,
    expected_pending: int | None,
) -> None:
    system = _system(tmp_path)
    job = _start(system)
    wakes: list[str] = []
    system.worker.configure_post_update_scan_wake(lambda: wakes.append("wake"))

    assert system.worker.run_once().status is PackageUpdateWorkerCycleStatus.TERMINAL
    assert wakes == ["wake"]
    assert system.authority.pending_post_update_package_scans() == (
        (job.job_id, job.resource_id),
    )
    assert "post_update_scan_requested" in system.events(job.job_id)
    before_scan = next(
        resource
        for resource in InventoryPublication(
            system.store, system.authority
        ).read().resources
        if resource["resource_id"] == job.resource_id
    )["package_scan"]
    assert before_scan["post_update_scan_pending"] is True

    packages = system.scan.packages[:2] if mode == "nonzero" else ()
    host = _PostUpdateScanHost(
        packages=packages,
        failure=(
            PackageScanFailure.METADATA_REFRESH_FAILED
            if mode == "unknown"
            else None
        ),
    )
    scheduler = PackageScanScheduler(
        system.authority,
        system.store,
        host,
        interval_seconds=21_600,
        initial_delay_seconds=0,
    )

    outcomes = scheduler.run_post_update_once()

    assert len(outcomes) == 1
    assert outcomes[0].resource_id == job.resource_id
    assert host.calls == 1
    assert system.authority.pending_post_update_package_scans() == ()
    latest = system.store.list_package_scan_runs(job.resource_id)[-1]
    assert latest.outcome is not None and latest.outcome.value == expected_status
    assert latest.pending_count == expected_pending
    published = next(
        resource
        for resource in InventoryPublication(
            system.store, system.authority
        ).read().resources
        if resource["resource_id"] == job.resource_id
    )["package_scan"]
    assert published["status"] == expected_status
    assert published["pending_count"] == expected_pending
    assert published["post_update_scan_pending"] is False
    assert (
        system.authority.package_update_job(job.job_id).status
        is PackageUpdateJobStatus.SUCCEEDED
    )

    assert system.worker.run_once().status is PackageUpdateWorkerCycleStatus.IDLE
    InventoryPublication(system.store, system.authority).read()
    assert scheduler.run_post_update_once() == ()
    assert system.authority.pending_post_update_package_scans() == ()
    assert wakes == ["wake"]
    assert host.calls == 1


def test_restart_before_post_update_scan_claim_preserves_one_request(
    tmp_path: Path,
) -> None:
    system = _system(tmp_path)
    job = _start(system)
    assert system.worker.run_once().status is PackageUpdateWorkerCycleStatus.TERMINAL

    path = system.store.path
    system.store.close()
    store = InventoryAuthorityStore(path, now=system.clock)
    authority = InventoryAuthority(store, now=system.clock)
    assert authority.pending_post_update_package_scans() == (
        (job.job_id, job.resource_id),
    )

    host = _PostUpdateScanHost()
    scheduler = PackageScanScheduler(
        authority,
        store,
        host,
        interval_seconds=21_600,
        initial_delay_seconds=0,
    )
    outcomes = scheduler.run_post_update_once()
    assert len(outcomes) == 1
    assert host.calls == 1
    assert authority.pending_post_update_package_scans() == ()
    assert scheduler.run_post_update_once() == ()
    store.close()


def test_restart_after_post_update_scan_claim_resumes_same_run_once(
    tmp_path: Path,
) -> None:
    system = _system(tmp_path)
    job = _start(system)
    assert system.worker.run_once().status is PackageUpdateWorkerCycleStatus.TERMINAL

    claimed = system.authority.issue_post_update_package_scan(job.job_id)
    assert claimed.lifecycle.value == "running"
    pending = next(
        resource
        for resource in InventoryPublication(
            system.store, system.authority
        ).read().resources
        if resource["resource_id"] == job.resource_id
    )["package_scan"]
    assert pending["post_update_scan_pending"] is True

    path = system.store.path
    system.store.close()
    store = InventoryAuthorityStore(path, now=system.clock)
    authority = InventoryAuthority(store, now=system.clock)
    assert authority.recover_interrupted_package_scans() == ()
    assert authority.pending_post_update_package_scans() == (
        (job.job_id, job.resource_id),
    )

    host = _PostUpdateScanHost()
    scheduler = PackageScanScheduler(
        authority,
        store,
        host,
        interval_seconds=21_600,
        initial_delay_seconds=0,
    )
    outcomes = scheduler.run_post_update_once()
    assert len(outcomes) == 1
    assert outcomes[0].scan_run_id == claimed.scan_run_id
    assert authority.pending_post_update_package_scans() == ()
    assert scheduler.run_post_update_once() == ()
    assert host.calls == 1
    latest = store.list_package_scan_runs(job.resource_id)[-1]
    assert latest.scan_run_id == claimed.scan_run_id
    assert latest.outcome is not None and latest.outcome.value == "success"
    assert latest.pending_count == 0
    published = next(
        resource
        for resource in InventoryPublication(store, authority).read().resources
        if resource["resource_id"] == job.resource_id
    )["package_scan"]
    assert published["status"] == "success"
    assert published["pending_count"] == 0
    assert published["post_update_scan_pending"] is False
    store.close()


def test_v18_post_update_link_accepts_only_same_resource_running_once(
    tmp_path: Path,
) -> None:
    system = _system(tmp_path)
    job = _start(system)
    assert system.worker.run_once().status is PackageUpdateWorkerCycleStatus.TERMINAL

    terminal = system.authority.issue_package_scan(job.resource_id)
    system.authority.finalize_successful_package_scan(
        terminal.scan_run_id,
        os_id="debian",
        os_version="12",
        packages=(),
        reboot_required=None,
    )
    with pytest.raises(sqlite3.IntegrityError, match="link is invalid"):
        with system.store._transaction() as connection:
            connection.execute(
                "UPDATE package_update_post_scan_requests SET scan_run_id=? "
                "WHERE job_id=?",
                (terminal.scan_run_id, job.job_id),
            )

    other_resource, _, _ = _add_approved_resource(system.store, system.authority)
    wrong_resource = system.authority.issue_package_scan(other_resource.resource_id)
    with pytest.raises(sqlite3.IntegrityError, match="link is invalid"):
        with system.store._transaction() as connection:
            connection.execute(
                "UPDATE package_update_post_scan_requests SET scan_run_id=? "
                "WHERE job_id=?",
                (wrong_resource.scan_run_id, job.job_id),
            )

    running = system.authority.issue_package_scan(job.resource_id)
    with system.store._transaction() as connection:
        connection.execute(
            "UPDATE package_update_post_scan_requests SET scan_run_id=? WHERE job_id=?",
            (running.scan_run_id, job.job_id),
        )
    with pytest.raises(sqlite3.IntegrityError, match="link is invalid"):
        with system.store._transaction() as connection:
            connection.execute(
                "UPDATE package_update_post_scan_requests SET scan_run_id=NULL "
                "WHERE job_id=?",
                (job.job_id,),
            )
    with pytest.raises(sqlite3.IntegrityError, match="link is invalid"):
        with system.store._transaction() as connection:
            connection.execute(
                "UPDATE package_update_post_scan_requests SET scan_run_id=? "
                "WHERE job_id=?",
                (str(uuid.uuid4()), job.job_id),
            )


def test_periodic_cycle_does_not_duplicate_post_update_resource(tmp_path: Path) -> None:
    system = _system(tmp_path)
    job = _start(system)
    assert system.worker.run_once().status is PackageUpdateWorkerCycleStatus.TERMINAL
    host = _PostUpdateScanHost()
    scheduler = PackageScanScheduler(
        system.authority,
        system.store,
        host,
        interval_seconds=21_600,
        initial_delay_seconds=0,
    )
    outcomes = scheduler.run_once()
    assert [outcome.resource_id for outcome in outcomes] == [job.resource_id]
    assert host.calls == 1


def test_vmid_reuse_cannot_satisfy_an_old_resource_post_update_request(
    tmp_path: Path,
) -> None:
    system = _system(tmp_path)
    job = _start(system)
    assert system.worker.run_once().status is PackageUpdateWorkerCycleStatus.TERMINAL
    _break_incarnation_continuity_at_the_same_locator(system.store, system.authority)
    host = _PostUpdateScanHost()
    scheduler = PackageScanScheduler(
        system.authority,
        system.store,
        host,
        interval_seconds=21_600,
        initial_delay_seconds=0,
    )
    outcomes = scheduler.run_post_update_once()
    assert len(outcomes) == 1
    assert outcomes[0].status == "conflict"
    assert host.calls == 0
    assert system.authority.pending_post_update_package_scans() == (
        (job.job_id, job.resource_id),
    )


def test_the_execution_gate_runs_before_the_mutation_stage(tmp_path: Path) -> None:
    """The gate is not skipped because mutation re-proves material itself.

    A drifted plan must be refused by the cheap, entirely non-mutating check
    before it can reach the stage that owns the real package command.
    """

    system = _system(tmp_path)
    job = _start(system)
    order: list[str] = []
    system.execution_host.side_effect = lambda _job: order.append("gate")
    original = system.mutation_host.prepare_exact_package_mutation

    def _record(request):
        order.append("mutation")
        return original(request)

    system.mutation_host.prepare_exact_package_mutation = _record
    system.worker.run_once()

    assert order[0] == "gate"
    assert "mutation" in order


# ===========================================================================
# B. PLAN DRIFT
# ===========================================================================


def test_plan_drift_blocks_the_job_with_zero_package_mutation(
    tmp_path: Path,
) -> None:
    """`PRODUCT.md` rule 2, at the production boundary.

    The gate sees fresh material that no longer equals the approved rows,
    terminalizes the job `blocked`, and the worker stops there. The mutation
    boundary is never armed and the real package command is never submitted.
    """

    system = _system(tmp_path)
    job = _start(system)
    # One approved package silently gone from the fresh simulation.
    system.execution_host.simulation_stdout = _simulation_for(system.scan.packages[:-1])

    cycle = system.worker.run_once()

    assert cycle.status is PackageUpdateWorkerCycleStatus.STOPPED
    assert cycle.stop_reason == "execution_plan_mismatched"
    final = system.job(job.job_id)
    assert final.status is PackageUpdateJobStatus.BLOCKED
    assert final.mutation_may_have_started_at is None
    assert final.mutation_operation_id is None
    assert system.mutation_host.calls == []
    # The snapshot it already took is retained, and confers no rollback
    # authority: a pre-mutation job never gains one merely by owning a
    # snapshot.
    assert final.snapshot_confirmed_at is not None
    assert final.rollback_operation_id is None


def test_a_blocked_pre_mutation_job_can_never_be_rolled_back(tmp_path: Path) -> None:
    """A retained snapshot is not authorization, and there is no fallback."""

    system = _system(tmp_path)
    job = _start(system)
    system.execution_host.simulation_stdout = _simulation_for(system.scan.packages[:-1])
    system.worker.run_once()

    from app.inventory import AuthorityConflict

    with pytest.raises(AuthorityConflict):
        system.authority.arm_package_update_rollback(
            job.job_id, _canonical(system.ownership, system.identity)
        )


# ===========================================================================
# C. SNAPSHOT UNCERTAINTY
# ===========================================================================


def test_snapshot_uncertainty_stays_fenced_and_never_reaches_mutation(
    tmp_path: Path,
) -> None:
    """An unproven snapshot is never a licence to mutate packages.

    The job stays ACTIVE at its write-ahead uncertainty checkpoint, keeps the
    one global destructive slot, and the worker stops. Nothing about the
    execution gate or the mutation stage runs.
    """

    system = _system(tmp_path)
    job = _issue(system.authority, system.resource, system.approval)
    system.bind_hosts(job.job_id, outcome=SnapshotOperationOutcome.UNCERTAIN)
    system.build_worker()

    cycle = system.worker.run_once()

    assert cycle.status is PackageUpdateWorkerCycleStatus.STOPPED
    assert cycle.stop_reason == "snapshot_uncertain"
    final = system.job(job.job_id)
    assert final.status is PackageUpdateJobStatus.ACTIVE
    assert final.checkpoint is PackageUpdateCheckpoint.SNAPSHOT_MAY_HAVE_STARTED
    assert system.execution_host.calls == 0
    assert system.mutation_host.calls == []
    assert final.mutation_may_have_started_at is None


# ===========================================================================
# D. MUTATION FAILURE / UNKNOWN
# ===========================================================================


def test_a_failed_mutation_keeps_ownership_and_never_rolls_back(
    tmp_path: Path,
) -> None:
    """A package command that failed is exactly the job that needs a snapshot.

    It stays ACTIVE at `mutation_may_have_started`, keeps its confirmed
    snapshot and the global slot, reaches no health success, and -- the point
    of this test -- causes ZERO rollback submissions.
    """

    system = _system(tmp_path)
    job = _start(system)
    system.guest.mutation_exit_code = 100

    cycle = system.worker.run_once()

    assert cycle.status is PackageUpdateWorkerCycleStatus.STOPPED
    assert cycle.stop_reason == "mutation_terminal_failure"
    final = system.job(job.job_id)
    assert final.status is PackageUpdateJobStatus.ACTIVE
    assert final.checkpoint is PackageUpdateCheckpoint.MUTATION_MAY_HAVE_STARTED
    assert final.mutation_completed_at is None
    assert final.snapshot_confirmed_at is not None
    assert final.health_started_at is None
    assert final.health_outcome is None
    assert system.health_host.calls == 0
    assert "submit_same_job_rollback" not in system.rollback_host.calls
    assert final.rollback_may_have_started_at is None


def test_an_unproven_mutation_stays_uncertain_and_never_rolls_back(
    tmp_path: Path,
) -> None:
    """A mutation still running is durably uncertain, never failed, never retried."""

    system = _system(tmp_path)
    job = _start(system)
    system.mutation_host.runner_mode = "hold"

    cycle = system.worker.run_once()

    assert cycle.stop_reason in {"mutation_running", "mutation_uncertain"}
    final = system.job(job.job_id)
    assert final.status is PackageUpdateJobStatus.ACTIVE
    assert final.checkpoint is PackageUpdateCheckpoint.MUTATION_MAY_HAVE_STARTED
    assert final.mutation_completed_at is None
    assert "submit_same_job_rollback" not in system.rollback_host.calls
    system.mutation_host.release_held_runner()


# ===========================================================================
# E. HEALTH FAIL AND UNKNOWN
# ===========================================================================


def test_a_failed_health_verdict_leaves_the_job_rollback_capable_and_idle(
    tmp_path: Path,
) -> None:
    """A proven health failure reports and stops. It compensates nothing.

    The job stays ACTIVE with a definitive FAILED verdict, keeps its snapshot,
    and becomes rollback-CAPABLE -- but the worker submits nothing, because
    this product has made no automatic compensation decision.
    """

    system = _system(tmp_path, health=["failed"])
    job = _start(system)

    cycle = system.worker.run_once()

    assert cycle.status is PackageUpdateWorkerCycleStatus.STOPPED
    assert cycle.stop_reason == "health_failed"
    final = system.job(job.job_id)
    assert final.status is PackageUpdateJobStatus.ACTIVE
    assert final.checkpoint is PackageUpdateCheckpoint.HEALTH_COMPLETED
    assert final.health_outcome is HealthOutcome.FAILED
    assert final.mutation_completed_at is not None
    assert final.snapshot_confirmed_at is not None
    # Zero rollback submissions, and no durable rollback intent.
    assert final.rollback_may_have_started_at is None
    assert final.rollback_operation_id is None
    assert system.rollback_host.calls == []
    assert system.authority.pending_post_update_package_scans() == ()

    # And a further wake changes nothing: it is not a retry loop.
    again = system.worker.run_once()
    assert again.stop_reason == "health_failed"
    assert system.health_host.calls == 1
    assert system.rollback_host.calls == []
    assert system.authority.pending_post_update_package_scans() == ()


def test_an_unknown_health_verdict_writes_no_verdict_and_never_retries(
    tmp_path: Path,
) -> None:
    """UNKNOWN is never success, never durable, and never auto-retried.

    The job stays ACTIVE at `health_started` with its snapshot and rollback
    authority intact. A second wake performs no second attempt on its own --
    production liveness for this state is an operator invoking `resume`.
    """

    system = _system(tmp_path, health=["unknown"])
    job = _start(system)

    cycle = system.worker.run_once()

    assert cycle.stop_reason == "health_unknown"
    final = system.job(job.job_id)
    assert final.status is PackageUpdateJobStatus.ACTIVE
    assert final.checkpoint is PackageUpdateCheckpoint.HEALTH_STARTED
    assert final.health_completed_at is None
    assert final.health_outcome is None
    assert final.health_probe_results == ()
    assert system.health_host.calls == 1
    assert system.rollback_host.calls == []
    assert system.authority.pending_post_update_package_scans() == ()


def test_an_explicit_resume_re_evaluates_read_only_health_and_can_succeed(
    tmp_path: Path,
) -> None:
    """The operator asks again; the read-only evaluation simply runs again.

    Resuming is safe here for one structural reason: health execution reads
    and changes nothing. That is why this stage has no journal, no
    write-ahead checkpoint, and no at-most-once fence -- and why the
    re-evaluation is a continuation rather than a second submission.
    """

    system = _system(tmp_path, health=["unknown", "passed"])
    job = _start(system)
    assert system.worker.run_once().stop_reason == "health_unknown"

    # Exactly what the resume route does: wake the worker, which re-reads the
    # durable checkpoint and continues where that is safe.
    cycle = system.worker.run_once()

    assert cycle.status is PackageUpdateWorkerCycleStatus.TERMINAL
    final = system.job(job.job_id)
    assert final.status is PackageUpdateJobStatus.SUCCEEDED
    assert final.health_outcome is HealthOutcome.PASSED
    assert system.health_host.calls == 2
    # One health evaluation per wake -- never a loop inside one wake.
    assert system.mutation_host.calls.count("execute_exact_package_mutation") == 1


# ===========================================================================
# F. EXPLICIT ROLLBACK
# ===========================================================================


def _failed_health_job(tmp_path: Path):
    system = _system(tmp_path, health=["failed"])
    job = _start(system)
    system.worker.run_once()
    assert system.job(job.job_id).health_outcome is HealthOutcome.FAILED
    return system, job


def test_explicit_rollback_makes_its_intent_durable_then_rolls_back(
    tmp_path: Path,
) -> None:
    """The operator asks; the boundary is durable; only then does it run.

    `arm_package_update_rollback` is the write-ahead commit, and it happens
    before anything is submitted to PVE. Once it has, a crash leaves a state
    startup recovery understands rather than a request nobody recorded.
    """

    system, job = _failed_health_job(tmp_path)

    armed = system.authority.arm_package_update_rollback(
        job.job_id, _canonical(system.ownership, system.identity)
    )
    assert armed.checkpoint is PackageUpdateCheckpoint.ROLLBACK_MAY_HAVE_STARTED
    assert armed.rollback_operation_id is not None
    assert armed.rollback_may_have_started_at is not None
    # Durable BEFORE any submission: the host has not been asked anything.
    assert system.rollback_host.calls == []

    system.complete_pve_rollback()
    cycle = system.worker.run_once()

    assert cycle.status is PackageUpdateWorkerCycleStatus.TERMINAL
    final = system.job(job.job_id)
    assert final.status is PackageUpdateJobStatus.ROLLED_BACK
    assert final.checkpoint is PackageUpdateCheckpoint.ROLLBACK_COMPLETED
    assert final.rollback_completed_at is not None
    # A rolled-back update is never a successful one.
    assert final.status is not PackageUpdateJobStatus.SUCCEEDED
    assert final.health_outcome is HealthOutcome.FAILED


def test_rollback_targets_only_this_jobs_own_snapshot(tmp_path: Path) -> None:
    """No caller names a snapshot, and there is no "latest snapshot" fallback.

    A fresh canonical listing that does not contain this job's own snapshot
    refuses, even though it contains a perfectly good older one.
    """

    system, job = _failed_health_job(tmp_path)
    from app.inventory import AuthorityConflict

    someone_elses = (
        _current_entry(),
        _foreign_entry(),
        ObservedSnapshot(name="pre-update-older", description="an older snapshot"),
    )
    with pytest.raises(AuthorityConflict):
        system.authority.arm_package_update_rollback(job.job_id, someone_elses)

    assert system.job(job.job_id).rollback_may_have_started_at is None
    assert system.rollback_host.calls == []


def test_a_second_rollback_request_never_submits_a_second_rollback(
    tmp_path: Path,
) -> None:
    """Re-arming re-derives and re-proves the SAME operation identity."""

    system, job = _failed_health_job(tmp_path)
    listing = _canonical(system.ownership, system.identity)

    first = system.authority.arm_package_update_rollback(job.job_id, listing)
    second = system.authority.arm_package_update_rollback(job.job_id, listing)

    assert second.rollback_operation_id == first.rollback_operation_id
    assert second.rollback_may_have_started_at == first.rollback_may_have_started_at


def test_a_succeeded_job_is_refused_a_rollback(tmp_path: Path) -> None:
    """A passing verdict and `succeeded` are one fact; terminal is terminal."""

    system = _system(tmp_path)
    job = _start(system)
    system.worker.run_once()
    assert system.job(job.job_id).status is PackageUpdateJobStatus.SUCCEEDED

    from app.inventory import AuthorityConflict

    with pytest.raises(AuthorityConflict):
        system.authority.arm_package_update_rollback(
            job.job_id, _canonical(system.ownership, system.identity)
        )
    assert system.rollback_host.calls == []


# ===========================================================================
# G. RESTART MATRIX
# ===========================================================================


def test_restart_before_the_worker_sees_a_newly_issued_job(tmp_path: Path) -> None:
    """Existing pre-mutation interruption semantics, unchanged and preserved.

    A job that was issued but never reached its write-ahead snapshot boundary
    is provably pre-mutation, so a restart terminalizes it `interrupted` and
    frees the global slot. That is a truthful answer, not a weakened one: it
    never runs on its own afterwards, and the operator simply asks again.
    """

    system = _system(tmp_path)
    job = _issue(system.authority, system.resource, system.approval)
    system.bind_hosts(job.job_id)
    system.build_worker()

    system.restart()

    final = system.job(job.job_id)
    assert final.status is PackageUpdateJobStatus.INTERRUPTED
    assert final.checkpoint is PackageUpdateCheckpoint.ISSUED
    cycle = system.worker.run_once()
    assert cycle.status is PackageUpdateWorkerCycleStatus.IDLE
    assert system.snapshot_host.submit_calls == 0


def test_restart_at_snapshot_may_have_started_reattaches_and_never_resubmits(
    tmp_path: Path,
) -> None:
    """The uncertain snapshot boundary survives a restart intact."""

    system = _system(tmp_path)
    job = _issue(system.authority, system.resource, system.approval)
    system.bind_hosts(job.job_id, outcome=SnapshotOperationOutcome.UNCERTAIN)
    system.build_worker()
    system.worker.run_once()
    assert (
        system.job(job.job_id).checkpoint
        is PackageUpdateCheckpoint.SNAPSHOT_MAY_HAVE_STARTED
    )

    before = system.job(job.job_id)
    system.restart()
    rebuilt = system.snapshot_host
    assert system.job(job.job_id).status is PackageUpdateJobStatus.ACTIVE
    system.worker.run_once()

    after = system.job(job.job_id)
    # Reattached, not replayed. The host's own journal still says a
    # submission crossed its door, so the recovering backend re-OBSERVES that
    # exact operation and never submits a second one.
    assert rebuilt.submit_calls == 0
    assert rebuilt.inspect_calls >= 1
    assert after.snapshot_operation_id == before.snapshot_operation_id
    assert after.status is PackageUpdateJobStatus.ACTIVE
    assert after.mutation_may_have_started_at is None


def test_restart_at_mutation_may_have_started_recovers_and_never_resubmits(
    tmp_path: Path,
) -> None:
    """A restart mid-mutation observes; it never submits a second command.

    The mutation stage's asymmetry is what makes this safe: an invocation
    that finds a job already armed is recovery-only. The host's own journal
    stays at `submitted`, which is durably uncertain and never retried.
    """

    system = _system(tmp_path)
    job = _start(system)
    system.mutation_host.runner_mode = "hold"
    system.worker.run_once()
    assert (
        system.job(job.job_id).checkpoint
        is PackageUpdateCheckpoint.MUTATION_MAY_HAVE_STARTED
    )
    submissions = system.mutation_host.calls.count("execute_exact_package_mutation")
    assert submissions == 1

    system.restart()
    system.worker.run_once()

    assert (
        system.mutation_host.calls.count("execute_exact_package_mutation") == submissions
    )
    final = system.job(job.job_id)
    assert final.status is PackageUpdateJobStatus.ACTIVE
    assert final.mutation_completed_at is None
    assert final.health_outcome is None
    system.mutation_host.release_held_runner()


def test_restart_when_the_mutation_answer_was_lost_never_double_submits(
    tmp_path: Path,
) -> None:
    """A lost SSH answer is uncertainty, never permission to try again."""

    system = _system(tmp_path)
    job = _start(system)
    system.mutation_host.drop_response.add("execute_exact_package_mutation")
    system.mutation_host.runner_mode = "hold"

    system.worker.run_once()
    submissions = system.mutation_host.calls.count("execute_exact_package_mutation")
    assert submissions == 1
    assert (
        system.job(job.job_id).checkpoint
        is PackageUpdateCheckpoint.MUTATION_MAY_HAVE_STARTED
    )

    system.restart()
    system.mutation_host.drop_response.clear()
    system.worker.run_once()

    assert (
        system.mutation_host.calls.count("execute_exact_package_mutation") == submissions
    )
    system.mutation_host.release_held_runner()


def test_restart_at_mutation_completed_continues_into_health_only(
    tmp_path: Path,
) -> None:
    """A proven mutation is not a success; the frozen contract still decides."""

    system = _system(tmp_path, health=["unknown", "passed"])
    job = _start(system)
    system.worker.run_once()
    assert system.job(job.job_id).checkpoint is PackageUpdateCheckpoint.HEALTH_STARTED

    system.restart()
    assert system.job(job.job_id).status is PackageUpdateJobStatus.ACTIVE
    system.worker.run_once()

    final = system.job(job.job_id)
    assert final.status is PackageUpdateJobStatus.SUCCEEDED
    assert final.health_outcome is HealthOutcome.PASSED
    # The package command was never re-submitted to reach that success.
    assert system.mutation_host.calls.count("execute_exact_package_mutation") == 1


def test_restart_at_health_started_never_marks_the_job_succeeded(
    tmp_path: Path,
) -> None:
    """Restarting is not evidence about a workload."""

    system = _system(tmp_path, health=["unknown"])
    job = _start(system)
    system.worker.run_once()
    assert system.job(job.job_id).checkpoint is PackageUpdateCheckpoint.HEALTH_STARTED

    system.restart()

    recovered = system.job(job.job_id)
    assert recovered.status is PackageUpdateJobStatus.ACTIVE
    assert recovered.health_outcome is None
    assert recovered.health_completed_at is None


def test_restart_at_a_failed_health_verdict_still_never_rolls_back(
    tmp_path: Path,
) -> None:
    """A restart is not an operator asking for compensation."""

    system, job = _failed_health_job(tmp_path)

    system.restart()
    cycle = system.worker.run_once()

    assert cycle.stop_reason == "health_failed"
    final = system.job(job.job_id)
    assert final.status is PackageUpdateJobStatus.ACTIVE
    assert final.rollback_may_have_started_at is None
    assert system.rollback_host.calls == []


def test_restart_at_rollback_may_have_started_reattaches_and_never_resubmits(
    tmp_path: Path,
) -> None:
    """A rollback may be force-stopping the guest right now.

    So the job stays ACTIVE and fenced across the restart with its durable
    operation identity, and the recovering backend RE-OBSERVES that exact
    operation rather than submitting a second destructive rollback.
    """

    system, job = _failed_health_job(tmp_path)
    system.authority.arm_package_update_rollback(
        job.job_id, _canonical(system.ownership, system.identity)
    )
    armed = system.job(job.job_id)

    system.restart()

    recovered = system.job(job.job_id)
    assert recovered.status is PackageUpdateJobStatus.ACTIVE
    assert recovered.checkpoint is PackageUpdateCheckpoint.ROLLBACK_MAY_HAVE_STARTED
    assert recovered.rollback_operation_id == armed.rollback_operation_id

    system.complete_pve_rollback()
    system.worker.run_once()

    assert system.rollback_host.calls.count("submit_same_job_rollback") == 1
    assert system.job(job.job_id).status is PackageUpdateJobStatus.ROLLED_BACK


def test_restart_when_the_rollback_answer_was_lost_never_double_submits(
    tmp_path: Path,
) -> None:
    """A submitted rollback whose answer was lost is never resubmitted."""

    system, job = _failed_health_job(tmp_path)
    system.authority.arm_package_update_rollback(
        job.job_id, _canonical(system.ownership, system.identity)
    )
    system.rollback_host.drop_response.add("submit_same_job_rollback")
    system.worker.run_once()
    assert system.rollback_host.calls.count("submit_same_job_rollback") == 1

    system.restart()
    system.rollback_host.drop_response.clear()
    system.worker.run_once()

    assert system.rollback_host.calls.count("submit_same_job_rollback") == 1
    final = system.job(job.job_id)
    assert final.status is not PackageUpdateJobStatus.SUCCEEDED


# ===========================================================================
# H. THE WORKER'S OWN ERROR BOUNDARY AND SHUTDOWN
# ===========================================================================


def test_an_unexpected_stage_exception_never_kills_the_worker(
    tmp_path: Path, caplog
) -> None:
    """One bad cycle is logged, bounded and redacted, and life goes on.

    The job's durable facts are untouched beyond whatever a stage already
    truthfully recorded, no success is synthesized, and the global slot is
    never cleared merely to regain liveness.
    """

    import logging

    system = _system(tmp_path)
    job = _start(system)

    class _Exploded(RuntimeError):
        pass

    def _explode(_job):
        raise _Exploded("host control exploded with secret-token-value inside")

    system.execution_host.side_effect = _explode

    with caplog.at_level(logging.DEBUG):
        cycle = system.worker.run_once()

    # The gate classifies a host-side explosion as a host failure rather than
    # letting it escape, so the job is left fenced either way.
    assert cycle.status in {
        PackageUpdateWorkerCycleStatus.STOPPED,
        PackageUpdateWorkerCycleStatus.ERROR,
    }
    final = system.job(job.job_id)
    assert final.status is PackageUpdateJobStatus.ACTIVE
    assert final.mutation_may_have_started_at is None
    rendered = "\n".join(record.getMessage() for record in caplog.records)
    assert "secret-token-value" not in rendered

    # The worker is still usable.
    system.execution_host.side_effect = None
    assert system.worker.run_once().status is PackageUpdateWorkerCycleStatus.TERMINAL


def test_a_second_concurrent_cycle_is_refused_rather_than_racing(
    tmp_path: Path,
) -> None:
    """The in-process cycle lock stops two cycles; durable authority stops two jobs."""

    system = _system(tmp_path)
    _start(system)
    barrier = Barrier(2)
    observed: list[PackageUpdateWorkerCycleStatus] = []

    def _slow_gate(_job):
        barrier.wait(timeout=5)

    system.execution_host.side_effect = _slow_gate

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(system.worker.run_once)
        barrier.wait(timeout=5)
        second = pool.submit(system.worker.run_once)
        observed.append(second.result().status)
        observed.append(first.result().status)

    assert PackageUpdateWorkerCycleStatus.BUSY in observed
    # Exactly one real package command, whatever the threads did.
    assert system.mutation_host.calls.count("execute_exact_package_mutation") == 1


def test_shutdown_stops_scheduling_without_pretending_anything_undone(
    tmp_path: Path,
) -> None:
    """Stopping the worker changes no durable fact about a host operation."""

    system = _system(tmp_path)
    job = _start(system)
    system.mutation_host.runner_mode = "hold"
    system.worker.run_once()
    before = system.job(job.job_id)

    system.worker.start()
    system.worker.stop(grace_seconds=5.0)

    after = system.job(job.job_id)
    assert after.checkpoint == before.checkpoint
    assert after.status == before.status
    assert after.mutation_operation_id == before.mutation_operation_id
    assert after.mutation_completed_at is None
    system.mutation_host.release_held_runner()


def test_every_stop_reason_the_worker_can_report_is_in_the_closed_set() -> None:
    """An unnamed stop is a bug, not a fallback."""

    assert isinstance(PACKAGE_UPDATE_WORKER_STOP_REASONS, frozenset)
    assert {"health_failed", "health_unknown"} <= PACKAGE_UPDATE_WORKER_STOP_REASONS


# ===========================================================================
# I. THE OPERATOR API: the only production route to a package mutation
# ===========================================================================


BEARER = "a" * 32
_API_ENV = {
    "TEST_PVE_TOKEN": "root@pam!hubinet-ops=00000000-0000-0000-0000-000000000000",
    "TEST_API_TOKEN": BEARER,
}


def _api_config(db_path):
    """A config whose source matches the one the seeded authority holds."""

    return parse_r0_runtime_config(
        {
            "source": {
                "display_name": "Primary",
                "provider_kind": "proxmox_ve",
                "pve_endpoint": "https://pve.example:8006",
                "freshness_duration_seconds": 300,
                "credential_reference": "secret://pve",
                "pve_token_env": "TEST_PVE_TOKEN",
                "tls": {"verify": True, "ca_bundle_path": None},
            },
            "runtime": {
                "authority_db_path": str(db_path),
                "api_token_env": "TEST_API_TOKEN",
            },
        },
        env=_API_ENV,
    )


class ApiSystem:
    """The real routes and the real worker, over fake host boundaries.

    Built the way the composition root builds them: the seam hands the
    factory this app's OWN authority and store, so nothing here talks to a
    second database or a second worker.
    """

    def __init__(self, tmp_path: Path, *, health=None, activated: bool = True):
        seed = ProductionSystem(tmp_path, health=health)
        self.resource_id = seed.resource.resource_id
        db_path = seed.store.path
        seed.store.close()
        self.seed = seed
        self.built: dict = {}

        def _factory(authority, store):
            if not activated:
                return None
            seed.authority = authority
            seed.store = store
            self.built["system"] = seed
            return PackageUpdateRuntime(
                worker=_DeferredWorker(seed),
                snapshot_host_control=_DeferredSnapshotHost(seed),
            )

        # The same fake clock the seeded authority used. Without it the
        # app's own startup would materialize a freshness expiry against real
        # wall-clock time and refuse the approval as stale -- a real
        # behaviour, but not the one under test here.
        self.app = create_read_only_app(
            _api_config(db_path),
            start_scheduler=False,
            now=seed.clock,
            package_update_runtime_factory=_factory,
        )
        self.client = TestClient(self.app)
        self.store = self.app.state.store
        self.authority = self.app.state.authority

    def close(self) -> None:
        self.client.close()
        self.app.state.package_scan_scheduler.stop()
        self.app.state.scheduler.stop()
        self.app.state.store.close()

    # -- HTTP ----------------------------------------------------------

    def post(self, path: str, **kwargs):
        return self.client.post(
            path, headers={"Authorization": f"Bearer {BEARER}"}, **kwargs
        )

    def get(self, path: str, **kwargs):
        return self.client.get(
            path, headers={"Authorization": f"Bearer {BEARER}"}, **kwargs
        )

    def start(self, request_id: str | None = None):
        return self.post(
            f"/r0/v1/resources/{self.resource_id}/package-update",
            json={"request_id": request_id or str(uuid.uuid4())},
        )

    def bind(self, job_id: str, **kwargs) -> None:
        self.seed.bind_hosts(job_id, **kwargs)
        self.seed.build_worker()

    def run_worker(self):
        return self.seed.worker.run_once()


class _DeferredWorker:
    """Forwards `wake()` to whatever worker the fixture has built.

    The routes only ever call `wake()` on the worker, which is exactly the
    contract this stands in for: a wake is a hint, so a test that wants to
    observe the effect runs one cycle explicitly.
    """

    def __init__(self, system: ProductionSystem) -> None:
        self._system = system
        self.wakes = 0

    def wake(self) -> None:
        self.wakes += 1

    def configure_post_update_scan_wake(self, wake) -> None:
        self.post_update_scan_wake = wake

    def start(self) -> None:  # pragma: no cover - the app may start it
        pass

    def stop(self, *, grace_seconds: float = 30.0) -> None:
        pass


class _DeferredSnapshotHost:
    """Forwards read-only inspection to the fixture's scripted boundary."""

    def __init__(self, system: ProductionSystem) -> None:
        self._system = system

    def inspect_job_snapshot_state(self, **kwargs):
        return self._system.snapshot_host.inspect_job_snapshot_state(**kwargs)

    def ensure_pre_update_snapshot_submitted(self, **kwargs):  # pragma: no cover
        raise AssertionError("the rollback route must never submit a snapshot")

    def seal_operation_never_submitted(self, **kwargs):  # pragma: no cover
        raise AssertionError("the rollback route must never seal an operation")


@pytest.fixture
def api(tmp_path: Path):
    system = ApiSystem(tmp_path)
    try:
        yield system
    finally:
        system.close()


def test_start_requires_authentication(api) -> None:
    """Bearer authentication is required on the one destructive route."""

    response = api.client.post(
        f"/r0/v1/resources/{api.resource_id}/package-update",
        json={"request_id": str(uuid.uuid4())},
    )
    assert response.status_code == 401
    assert api.store.active_package_update_job() is None


def test_start_acknowledges_only_after_the_job_is_durable(api) -> None:
    """202 means a durable job representing THIS request already exists.

    That is the whole contract of the acknowledgement point: the operator is
    never told an update started on the strength of an in-memory intention.
    """

    request_id = str(uuid.uuid4())
    response = api.start(request_id)

    assert response.status_code == 202
    body = response.json()
    assert body["request_id"] == request_id
    assert body["resource_id"] == api.resource_id
    assert body["status"] == "active"
    assert body["checkpoint"] == "issued"
    # Durable before the response, not after the wake.
    durable = api.store.active_package_update_job()
    assert durable is not None and durable.job_id == body["job_id"]


def test_start_rejects_every_field_but_the_request_id(api) -> None:
    """`extra="forbid"` is the fence, and it is a 422 rather than silence."""

    for extra in (
        {"vmid": 110},
        {"node": "pve-a"},
        {"snapshot_name": "pre-update-x"},
        {"packages": ["apt"]},
        {"plan_fingerprint": "0" * 64},
        {"command": "apt-get upgrade"},
        {"argv": ["apt-get", "upgrade"]},
        {"health_probes": []},
        {"operation": "execute_exact_package_mutation"},
    ):
        response = api.post(
            f"/r0/v1/resources/{api.resource_id}/package-update",
            json={"request_id": str(uuid.uuid4()), **extra},
        )
        assert response.status_code == 422, extra
    assert api.store.active_package_update_job() is None


def test_repeating_a_request_id_returns_the_same_job(api) -> None:
    """UUID idempotency, unchanged: a retry is not a second update."""

    request_id = str(uuid.uuid4())
    first = api.start(request_id)
    second = api.start(request_id)

    assert first.status_code == 202 and second.status_code == 202
    assert first.json()["job_id"] == second.json()["job_id"]
    assert len(api.store.list_package_update_jobs()) == 1


def test_a_second_start_is_refused_by_the_durable_global_single_flight(api) -> None:
    """One global destructive slot, and the authority owns it."""

    first = api.start()
    assert first.status_code == 202

    second = api.start()

    assert second.status_code == 409
    assert second.json()["detail"]["error"] == "another_job_active"
    assert api.store.active_package_update_job().job_id == first.json()["job_id"]


def test_two_simultaneous_starts_produce_exactly_one_job(api) -> None:
    """Whatever the threads do, the durable slot settles it."""

    barrier = Barrier(2)

    def _start_one():
        barrier.wait(timeout=5)
        return api.start()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [future.result() for future in [pool.submit(_start_one) for _ in range(2)]]

    codes = sorted(response.status_code for response in results)
    assert codes == [202, 409]
    assert len(api.store.list_package_update_jobs()) == 1


def test_start_without_an_approval_is_a_named_refusal(tmp_path: Path) -> None:
    """The taxonomy an operator needs: which precondition actually failed."""

    system = ApiSystem(tmp_path)
    try:
        with system.store._transaction() as connection:
            connection.execute("DELETE FROM package_plan_approvals")
        response = system.start()
        assert response.status_code == 409
        assert response.json()["detail"]["error"] == "no_current_approval"
        assert system.store.active_package_update_job() is None
    finally:
        system.close()


def test_start_without_a_health_contract_is_a_named_refusal(tmp_path: Path) -> None:
    """Absence is not health, so it is not a job either."""

    system = ApiSystem(tmp_path)
    try:
        system.authority.clear_resource_health_contract(system.resource_id)
        response = system.start()
        assert response.status_code == 409
        assert (
            response.json()["detail"]["error"] == "health_contract_unconfigured"
        )
        assert system.store.active_package_update_job() is None
    finally:
        system.close()


def test_readback_reports_bounded_typed_facts_and_no_raw_output(api) -> None:
    """What an operator needs to see, and nothing an operator must not."""

    started = api.start().json()
    api.bind(started["job_id"])
    api.run_worker()

    response = api.get(f"/r0/v1/resources/{api.resource_id}/package-update")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "succeeded"
    assert body["health"]["outcome"] == "passed"
    assert body["snapshot"]["name"] is not None
    assert body["mutation"]["completed_at"] is not None
    assert body["rollback"]["available"] is False
    assert isinstance(body["events"], list) and body["events"]
    rendered = response.text
    for forbidden in ("stdout", "stderr", "apt-get", BEARER, "PRIVATE KEY"):
        assert forbidden not in rendered, forbidden
    # No package rows and no per-probe results in the readback.
    assert "packages" not in body
    assert "health_probe_results" not in body


def test_the_active_job_witness_answers_the_product_updater(api) -> None:
    """The updater's fence reads this, and it must be exact."""

    idle = api.get("/r0/v1/package-update/active").json()
    assert idle == {"active": False, "job": None}

    started = api.start().json()
    busy = api.get("/r0/v1/package-update/active").json()
    assert busy["active"] is True
    assert busy["job"]["job_id"] == started["job_id"]
    assert busy["job"]["checkpoint"] == "issued"


def test_resume_wakes_the_worker_without_resubmitting_anything(api) -> None:
    """Resume is "re-read the checkpoint", never "send the command again"."""

    started = api.start().json()
    api.bind(started["job_id"])
    api.seed.health_host._outcomes = ["unknown", "passed"]
    api.run_worker()
    assert api.store.package_update_job(started["job_id"]).checkpoint is (
        PackageUpdateCheckpoint.HEALTH_STARTED
    )
    submissions = api.seed.mutation_host.calls.count("execute_exact_package_mutation")

    response = api.post(
        f"/r0/v1/resources/{api.resource_id}/package-update/resume", json={}
    )

    assert response.status_code == 202
    assert response.json()["checkpoint"] == "health_started"
    # The route itself submits nothing; it wakes the worker.
    assert (
        api.seed.mutation_host.calls.count("execute_exact_package_mutation")
        == submissions
    )
    api.run_worker()
    assert (
        api.store.package_update_job(started["job_id"]).status
        is PackageUpdateJobStatus.SUCCEEDED
    )


def test_resume_is_refused_when_no_active_job_exists(api) -> None:
    response = api.post(
        f"/r0/v1/resources/{api.resource_id}/package-update/resume", json={}
    )
    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "no_active_package_update_job"


def test_rollback_is_durable_before_the_operator_is_told_it_was_accepted(
    tmp_path: Path,
) -> None:
    """The crash rule, at the HTTP boundary.

    By the time the 202 is written, `rollback_may_have_started` is committed.
    A crash immediately afterwards therefore leaves a durable state startup
    recovery understands, and never a request nobody recorded.
    """

    system = ApiSystem(tmp_path, health=["failed"])
    try:
        started = system.start().json()
        system.bind(started["job_id"])
        system.run_worker()
        assert system.store.package_update_job(started["job_id"]).health_outcome is (
            HealthOutcome.FAILED
        )

        response = system.post(
            f"/r0/v1/resources/{system.resource_id}/package-update/rollback", json={}
        )

        assert response.status_code == 202
        durable = system.store.package_update_job(started["job_id"])
        assert durable.checkpoint is PackageUpdateCheckpoint.ROLLBACK_MAY_HAVE_STARTED
        assert durable.rollback_operation_id is not None
        # The route armed the boundary and woke the worker; it submitted
        # nothing to PVE itself.
        assert system.seed.rollback_host.calls == []

        system.seed.complete_pve_rollback()
        system.run_worker()
        assert (
            system.store.package_update_job(started["job_id"]).status
            is PackageUpdateJobStatus.ROLLED_BACK
        )
    finally:
        system.close()


def test_rollback_accepts_no_snapshot_or_target_from_the_caller(
    tmp_path: Path,
) -> None:
    """The operator selects a RESOURCE. There is nothing else to select."""

    system = ApiSystem(tmp_path, health=["failed"])
    try:
        started = system.start().json()
        system.bind(started["job_id"])
        system.run_worker()

        for extra in (
            {"snapshot_name": "pre-update-x"},
            {"snapshot_id": "abc"},
            {"vmid": 110},
            {"node": "pve-a"},
            {"operation_id": "abc"},
            {"rollback_target": "latest"},
        ):
            response = system.post(
                f"/r0/v1/resources/{system.resource_id}/package-update/rollback",
                json=extra,
            )
            assert response.status_code == 422, extra
        assert (
            system.store.package_update_job(started["job_id"]).rollback_operation_id
            is None
        )
    finally:
        system.close()


def test_rollback_is_refused_for_a_pre_mutation_job(tmp_path: Path) -> None:
    """A retained snapshot is never rollback authority."""

    system = ApiSystem(tmp_path)
    try:
        started = system.start().json()
        system.bind(started["job_id"], outcome=SnapshotOperationOutcome.UNCERTAIN)
        system.run_worker()
        job = system.store.package_update_job(started["job_id"])
        assert job.checkpoint is PackageUpdateCheckpoint.SNAPSHOT_MAY_HAVE_STARTED

        response = system.post(
            f"/r0/v1/resources/{system.resource_id}/package-update/rollback", json={}
        )

        assert response.status_code == 409
        assert (
            system.store.package_update_job(started["job_id"]).rollback_operation_id
            is None
        )
    finally:
        system.close()


def test_the_update_routes_report_not_activated_when_the_switch_is_off(
    tmp_path: Path,
) -> None:
    """An installation that has not activated the lifecycle serves no control.

    The read-only routes stay available on purpose: such an installation may
    still own a durable job, and reporting nothing about it would be
    reporting a false absence.
    """

    system = ApiSystem(tmp_path, activated=False)
    try:
        base = f"/r0/v1/resources/{system.resource_id}/package-update"
        for path, body in (
            (base, {"request_id": str(uuid.uuid4())}),
            (f"{base}/resume", {}),
            (f"{base}/rollback", {}),
        ):
            response = system.post(path, json=body)
            assert response.status_code == 503, path
            assert (
                response.json()["detail"]["error"] == "package_update_not_activated"
            )
        assert system.get("/r0/v1/package-update/active").status_code == 200
        assert system.store.active_package_update_job() is None
    finally:
        system.close()


# ===========================================================================
# J. THE EXCLUSIVE PRODUCT-UPDATE MAINTENANCE FENCE
#
# A Hubinet PRODUCT update replaces the backend and its privileged helpers; a
# WORKLOAD update mutates packages through those helpers. The two must never
# overlap, and a poll cannot make them exclusive -- between any "is a job
# active?" answer and the updater's first mutation an operator may
# legitimately start one, and a later poll only moves that window.
#
# So both sides take the SAME authority writer lock, and the fence is durable
# before acquisition commits. These tests pin that: exactly one side wins,
# never both, and the fence survives the process restart a product update
# performs.
# ===========================================================================


def _fence_path(system) -> Path:
    return product_update_fence_path(system.store.path)


def test_acquiring_the_fence_refuses_while_a_workload_job_is_active(
    tmp_path: Path,
) -> None:
    """Witness A. An ACTIVE job refuses the product updater outright.

    And it refuses at acquisition -- which the updater performs immediately
    before its first mutation -- so nothing has been staged, stopped, or
    replaced by the time it is told no.
    """

    system = _system(tmp_path)
    job = _start(system)
    assert system.job(job.job_id).status is PackageUpdateJobStatus.ACTIVE

    with pytest.raises(AuthorityConflict) as refusal:
        system.authority.acquire_product_update_maintenance_fence("run-a")

    assert "ACTIVE" in str(refusal.value)
    assert job.job_id in str(refusal.value)
    # Refused means NOT taken: no fence exists to strand workload updates.
    assert not _fence_path(system).exists()
    assert system.authority.product_update_maintenance_fence() is None


def test_a_held_fence_refuses_every_new_workload_start(tmp_path: Path) -> None:
    """Witness C/D. Once the fence is held, `start_update` fails closed.

    This is the whole point of the primitive, and it is what covers both the
    "between U2 and U4" window and the Phase U5 window in which the target
    backend is already running while product-update acceptance is not yet
    terminal.
    """

    system = _system(tmp_path)
    fence = system.authority.acquire_product_update_maintenance_fence("run-a")
    assert fence.holder == "run-a"

    with pytest.raises(PackageUpdateIssuanceRefused) as refusal:
        _issue(system.authority, system.resource, system.approval)

    assert refusal.value.reason == "product_update_in_progress"
    assert "run-a" in str(refusal.value)
    assert system.store.active_package_update_job() is None
    # Nothing about the workload authority was touched by refusing.
    assert system.store.list_package_update_jobs() == ()


def test_the_fence_survives_a_backend_restart(tmp_path: Path) -> None:
    """Witness G. The product update restarts the backend; the fence persists.

    A newly started target backend -- a different process, possibly a
    different build, possibly against a freshly reset authority database --
    must still refuse workload starts while product-update acceptance is in
    progress. That is why the durable fact is a file beside the database
    rather than in-process state.
    """

    system = _system(tmp_path)
    system.authority.acquire_product_update_maintenance_fence("run-a")

    # Exactly what a product update does in its Step 10: a different process
    # opens the same installation. No job exists, so nothing else is rebuilt.
    path = system.store.path
    system.store.close()
    system.store = InventoryAuthorityStore(path, now=system.clock)
    system.authority = InventoryAuthority(system.store, now=system.clock)
    system.authority.recover_interrupted_package_update_jobs()

    held = system.authority.product_update_maintenance_fence()
    assert held is not None and held.holder == "run-a"
    with pytest.raises(PackageUpdateIssuanceRefused) as refusal:
        _issue(system.authority, system.resource, system.approval)
    assert refusal.value.reason == "product_update_in_progress"


def test_releasing_the_fence_restores_ordinary_workload_semantics(
    tmp_path: Path,
) -> None:
    """Witnesses E and F. Release -- success or proven rollback -- re-opens it.

    Release is a plain filesystem removal, exactly as the product updater
    performs it at a terminal point. It needs no atomicity: removing a fence
    only ever widens what is permitted.
    """

    system = _system(tmp_path)
    system.authority.acquire_product_update_maintenance_fence("run-a")
    with pytest.raises(PackageUpdateIssuanceRefused):
        _issue(system.authority, system.resource, system.approval)

    _fence_path(system).unlink()

    assert system.authority.product_update_maintenance_fence() is None
    job = _issue(system.authority, system.resource, system.approval)
    assert job.status is PackageUpdateJobStatus.ACTIVE


def test_exactly_one_of_fence_acquisition_and_workload_start_can_win(
    tmp_path: Path,
) -> None:
    """Witness B, the race itself. Never both.

    Both sides take the authority store's single `BEGIN IMMEDIATE` writer
    lock, and acquisition makes the fence durable inside that critical
    section. So whichever enters first wins and the other observes its
    result -- there is no interleaving that produces both a durable ACTIVE
    job and a held fence.

    Run repeatedly with a barrier so the two transactions genuinely contend
    rather than happening to serialize.
    """

    for attempt in range(12):
        system = _system(tmp_path / f"race-{attempt}")
        barrier = Barrier(2)

        def _acquire():
            barrier.wait(timeout=5)
            try:
                return ("fence", system.authority.acquire_product_update_maintenance_fence("run-a"))
            except AuthorityConflict as exc:
                return ("fence_refused", exc)

        def _start_job():
            barrier.wait(timeout=5)
            try:
                return ("job", _issue(system.authority, system.resource, system.approval))
            except AuthorityConflict as exc:
                return ("job_refused", exc)

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = dict(
                future.result()
                for future in (pool.submit(_acquire), pool.submit(_start_job))
            )

        fence_held = system.authority.product_update_maintenance_fence() is not None
        job_active = system.store.active_package_update_job() is not None

        # The invariant, stated as directly as it can be stated.
        assert not (fence_held and job_active), (
            f"attempt {attempt}: a product update fenced the installation "
            "while a workload job was ACTIVE"
        )
        # And exactly one side succeeded -- neither is allowed to vanish.
        assert fence_held != job_active, (attempt, sorted(outcomes))
        if fence_held:
            assert "job_refused" in outcomes
            assert outcomes["job_refused"].reason == "product_update_in_progress"
        else:
            assert "fence_refused" in outcomes
            assert "ACTIVE" in str(outcomes["fence_refused"])


def test_re_acquiring_is_idempotent_for_the_same_run_and_refused_for_another(
    tmp_path: Path,
) -> None:
    """A run that re-enters through its own recovery must not deadlock itself.

    And a DIFFERENT product update must never silently steal a fence: one at
    a time, and a fence left by a crashed run is released by that run's own
    recovery.
    """

    system = _system(tmp_path)
    first = system.authority.acquire_product_update_maintenance_fence("run-a")
    again = system.authority.acquire_product_update_maintenance_fence("run-a")
    assert again == first

    with pytest.raises(AuthorityConflict) as refusal:
        system.authority.acquire_product_update_maintenance_fence("run-b")
    assert "run-a" in str(refusal.value)
    assert system.authority.product_update_maintenance_fence().holder == "run-a"


def test_a_corrupt_fence_file_fails_closed_rather_than_opening_issuance(
    tmp_path: Path,
) -> None:
    """The positive control for the fence's own hard-fail path.

    A fence file that exists but cannot be read truthfully is treated as a
    held fence, not an absent one: the alternative is letting a corrupt byte
    on disk re-open workload issuance during a product update.
    """

    system = _system(tmp_path)
    system.authority.acquire_product_update_maintenance_fence("run-a")
    _fence_path(system).write_text("not json at all", encoding="utf-8")

    with pytest.raises(ProductUpdateFenceError):
        system.authority.product_update_maintenance_fence()
    with pytest.raises(ProductUpdateFenceError):
        _issue(system.authority, system.resource, system.approval)
    assert system.store.active_package_update_job() is None


def test_the_fence_does_not_change_workload_semantics_when_absent(
    tmp_path: Path,
) -> None:
    """The legal positive control: no product update, nothing changes.

    An ordinary start, an ordinary full lifecycle, and an ordinary explicit
    rollback all behave exactly as they did before the fence existed.
    """

    system = _system(tmp_path, health=["failed"])
    assert system.authority.product_update_maintenance_fence() is None
    job = _start(system)
    assert system.worker.run_once().stop_reason == "health_failed"
    system.authority.arm_package_update_rollback(
        job.job_id, _canonical(system.ownership, system.identity)
    )
    system.complete_pve_rollback()
    system.worker.run_once()
    assert system.job(job.job_id).status is PackageUpdateJobStatus.ROLLED_BACK
    assert not _fence_path(system).exists()


def test_the_fence_never_blocks_an_idempotent_replay_of_an_existing_request(
    tmp_path: Path,
) -> None:
    """The fence refuses NEW workload jobs, which a replay is not.

    A request_id whose job already exists returns that job, exactly as it
    always did. Refusing here would report a job that demonstrably exists as
    though it had never been created.
    """

    system = _system(tmp_path)
    request_id = str(uuid.uuid4())
    job = _issue(system.authority, system.resource, system.approval, request_id)
    system.bind_hosts(job.job_id)
    system.build_worker()
    system.worker.run_once()
    assert system.job(job.job_id).status is PackageUpdateJobStatus.SUCCEEDED

    system.authority.acquire_product_update_maintenance_fence("run-a")

    replay = _issue(system.authority, system.resource, system.approval, request_id)
    assert replay.job_id == job.job_id


def test_the_fence_route_is_authenticated_and_takes_only_a_holder(
    api,
) -> None:
    """The HTTP surface: bearer-authenticated, one opaque field, no workload."""

    unauthenticated = api.client.post(
        "/r0/v1/package-update/maintenance-fence", json={"holder": "run-a"}
    )
    assert unauthenticated.status_code == 401

    for extra in (
        {"resource_id": "x"},
        {"vmid": 110},
        {"job_id": "x"},
        {"snapshot_name": "x"},
        {"command": "apt-get upgrade"},
    ):
        response = api.post(
            "/r0/v1/package-update/maintenance-fence",
            json={"holder": "run-a", **extra},
        )
        assert response.status_code == 422, extra

    acquired = api.post(
        "/r0/v1/package-update/maintenance-fence", json={"holder": "run-a"}
    )
    assert acquired.status_code == 200
    assert acquired.json()["holder"] == "run-a"
    assert api.get("/r0/v1/package-update/maintenance-fence").json()["held"] is True

    # And from that moment the operator start control fails closed.
    refused = api.start()
    assert refused.status_code == 503
    assert refused.json()["detail"]["error"] == "product_update_in_progress"
    assert api.store.active_package_update_job() is None


def test_the_fence_route_refuses_while_a_workload_job_is_active(api) -> None:
    """Witness A at the HTTP boundary, as the updater actually sees it."""

    started = api.start()
    assert started.status_code == 202

    response = api.post(
        "/r0/v1/package-update/maintenance-fence", json={"holder": "run-a"}
    )

    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "product_update_fence_unavailable"
    assert api.get("/r0/v1/package-update/maintenance-fence").json()["held"] is False


# ===========================================================================
# F2 POST-SUCCESS REFRESH: A RUNNING REFRESH MUST NOT ERASE THE LAST REAL
# TERMINAL RESULT.
#
# The refresh a successful update requests is published INDEPENDENTLY through
# `post_update_scan_pending`. While it is still running, the resource keeps
# showing the last real terminal 0/N/UNKNOWN observation it actually has --
# publishing `scanning` there would blank `pending_count`, the plan
# fingerprint and the whole package list, which is precisely the state an
# operator has just been told the update changed.
# ===========================================================================


def _published_scan(store, authority, resource_id: str):
    return next(
        resource
        for resource in InventoryPublication(store, authority).read().resources
        if resource["resource_id"] == resource_id
    )["package_scan"]


def _terminalize_a_fresh_scan(system, resource_id: str, *, packages=None, failure=None):
    """Give the resource one more REAL terminal scan before the refresh."""

    run = system.authority.issue_package_scan(resource_id)
    if failure is not None:
        return system.authority.finalize_failed_package_scan(
            run.scan_run_id,
            failure_class=failure,
            error_message="scan failed for this test",
        )
    return system.authority.finalize_successful_package_scan(
        run.scan_run_id,
        os_id="debian",
        os_version="12",
        packages=tuple(packages or ()),
        reboot_required=None,
    )


def _claim_post_update_refresh(system, job_id: str):
    claimed = system.authority.issue_post_update_package_scan(job_id)
    assert claimed.lifecycle.value == "running"
    return claimed


def _succeed_while_an_ordinary_scan_is_running(
    system,
    job_id: str,
    *,
    previous: str = "approved",
):
    state = {}

    def prepare_publication_race() -> None:
        if previous == "zero":
            state["terminal"] = _terminalize_a_fresh_scan(
                system, system.resource.resource_id, packages=()
            )
        elif previous == "failed":
            state["terminal"] = _terminalize_a_fresh_scan(
                system,
                system.resource.resource_id,
                failure=PackageScanFailure.METADATA_REFRESH_FAILED,
            )
        else:
            assert previous == "approved"
            state["terminal"] = system.scan
        state["running"] = system.authority.issue_package_scan(
            system.resource.resource_id
        )

    system.health_host._before_evaluate = prepare_publication_race
    assert system.worker.run_once().status is PackageUpdateWorkerCycleStatus.TERMINAL
    with system.store._read_transaction() as connection:
        request = connection.execute(
            "SELECT scan_run_id FROM package_update_post_scan_requests WHERE job_id=?",
            (job_id,),
        ).fetchone()
    assert request is not None
    assert request["scan_run_id"] is None
    return state["terminal"], state["running"]


def test_an_unclaimed_refresh_retains_the_previous_success_with_packages(
    tmp_path: Path,
) -> None:
    """An ordinary scan already RUNNING cannot erase the terminal result
    when health PASS creates a still-unclaimed post-update refresh request."""

    system = _system(tmp_path)
    job = _start(system)
    previous, ordinary_running = _succeed_while_an_ordinary_scan_is_running(
        system, job.job_id
    )

    published = _published_scan(system.store, system.authority, job.resource_id)
    assert published["scan_run_id"] == previous.scan_run_id
    assert published["scan_run_id"] != ordinary_running.scan_run_id
    assert published["status"] == "success"
    assert published["pending_count"] == len(previous.packages)
    assert published["packages"] == tuple(
        {
            "name": package.package_name,
            "architecture": package.architecture,
            "installed_version": package.installed_version,
            "candidate_version": package.candidate_version,
            "origin": package.origin,
            "description": package.description,
            "security": package.security,
        }
        for package in previous.packages
    )
    assert published["plan_fingerprint"] == previous.plan_fingerprint
    assert published["post_update_scan_pending"] is True


def test_an_unclaimed_refresh_retains_the_previous_zero_success(
    tmp_path: Path,
) -> None:
    system = _system(tmp_path)
    job = _start(system)
    previous, ordinary_running = _succeed_while_an_ordinary_scan_is_running(
        system, job.job_id, previous="zero"
    )

    published = _published_scan(system.store, system.authority, job.resource_id)
    assert published["scan_run_id"] == previous.scan_run_id
    assert published["scan_run_id"] != ordinary_running.scan_run_id
    assert published["status"] == "success"
    assert published["pending_count"] == 0
    assert published["packages"] == ()
    assert published["plan_fingerprint"] == previous.plan_fingerprint
    assert published["post_update_scan_pending"] is True


def test_an_unclaimed_refresh_retains_the_previous_unknown(
    tmp_path: Path,
) -> None:
    system = _system(tmp_path)
    job = _start(system)
    previous, ordinary_running = _succeed_while_an_ordinary_scan_is_running(
        system, job.job_id, previous="failed"
    )

    published = _published_scan(system.store, system.authority, job.resource_id)
    assert published["scan_run_id"] == previous.scan_run_id
    assert published["scan_run_id"] != ordinary_running.scan_run_id
    assert published["status"] == "failed"
    assert published["pending_count"] is None
    assert published["packages"] == ()
    assert published["plan_fingerprint"] is None
    assert published["error"]["classification"] == "metadata_refresh_failed"
    assert published["post_update_scan_pending"] is True


def test_pending_without_a_selected_terminal_scan_is_honestly_not_scanned() -> None:
    """The renderer never fabricates a zero/success fallback.

    Coherent authority cannot create this database state: a post-update
    request requires a SUCCEEDED job, and job issuance requires an approved
    completed scan for this same immutable resource. This direct renderer
    control covers the honest fallback without manufacturing impossible SQL.
    """

    published = InventoryPublication._package_scan(
        {"resource_type": "lxc"},
        None,
        {},
        post_update_scan_pending=True,
    )
    assert published["status"] == "not_scanned"
    assert published["scan_run_id"] is None
    assert published["pending_count"] is None
    assert published["packages"] == ()
    assert published["plan_fingerprint"] is None
    assert published["post_update_scan_pending"] is True


def test_unclaimed_refresh_waits_for_ordinary_single_flight_then_gets_its_own_run(
    tmp_path: Path,
) -> None:
    system = _system(tmp_path)
    job = _start(system)
    _previous, ordinary_running = _succeed_while_an_ordinary_scan_is_running(
        system, job.job_id
    )
    host = _PostUpdateScanHost()
    scheduler = PackageScanScheduler(
        system.authority,
        system.store,
        host,
        interval_seconds=21_600,
        initial_delay_seconds=0,
    )

    conflicted = scheduler.run_post_update_once()
    assert len(conflicted) == 1
    assert conflicted[0].status == "conflict"
    assert conflicted[0].scan_run_id is None
    assert host.calls == 0
    assert system.authority.pending_post_update_package_scans() == (
        (job.job_id, job.resource_id),
    )

    system.authority.finalize_failed_package_scan(
        ordinary_running.scan_run_id,
        failure_class=PackageScanFailure.METADATA_REFRESH_FAILED,
        error_message="ordinary scan failed for this test",
    )
    completed = scheduler.run_post_update_once()
    assert len(completed) == 1
    assert completed[0].status == "success"
    assert completed[0].scan_run_id not in {None, ordinary_running.scan_run_id}
    assert host.calls == 1
    assert system.authority.pending_post_update_package_scans() == ()
    published = _published_scan(system.store, system.authority, job.resource_id)
    assert published["scan_run_id"] == completed[0].scan_run_id
    assert published["status"] == "success"
    assert published["pending_count"] == 0
    assert published["post_update_scan_pending"] is False


def test_restart_terminalizes_unlinked_ordinary_scan_but_preserves_request(
    tmp_path: Path,
) -> None:
    system = _system(tmp_path)
    job = _start(system)
    _previous, ordinary_running = _succeed_while_an_ordinary_scan_is_running(
        system, job.job_id
    )

    path = system.store.path
    system.store.close()
    store = InventoryAuthorityStore(path, now=system.clock)
    authority = InventoryAuthority(store, now=system.clock)
    try:
        assert authority.recover_interrupted_package_scans() == (
            ordinary_running.scan_run_id,
        )
        assert authority.pending_post_update_package_scans() == (
            (job.job_id, job.resource_id),
        )
        published = _published_scan(store, authority, job.resource_id)
        assert published["scan_run_id"] == ordinary_running.scan_run_id
        assert published["status"] == "interrupted"
        assert published["pending_count"] is None
        assert published["packages"] == ()
        assert published["plan_fingerprint"] is None
        assert published["post_update_scan_pending"] is True

        host = _PostUpdateScanHost()
        scheduler = PackageScanScheduler(
            authority,
            store,
            host,
            interval_seconds=21_600,
            initial_delay_seconds=0,
        )
        completed = scheduler.run_post_update_once()
        assert len(completed) == 1
        assert completed[0].scan_run_id not in {
            None,
            ordinary_running.scan_run_id,
        }
        assert host.calls == 1
        assert authority.pending_post_update_package_scans() == ()
    finally:
        store.close()


def test_a_running_post_update_refresh_retains_the_previous_success_with_packages(
    tmp_path: Path,
) -> None:
    """Case 1. Previous terminal SUCCESS with N>0 is retained whole."""

    system = _system(tmp_path)
    job = _start(system)
    assert system.worker.run_once().status is PackageUpdateWorkerCycleStatus.TERMINAL

    before = _published_scan(system.store, system.authority, job.resource_id)
    assert before["status"] == "success"
    assert before["pending_count"] == 2
    assert len(before["packages"]) == 2
    assert before["post_update_scan_pending"] is True

    claimed = _claim_post_update_refresh(system, job.job_id)

    during = _published_scan(system.store, system.authority, job.resource_id)
    assert during["status"] == "success"
    assert during["pending_count"] == 2
    assert during["packages"] == before["packages"]
    assert during["plan_fingerprint"] == before["plan_fingerprint"]
    assert during["post_update_scan_pending"] is True
    # The retained result is the PREVIOUS run, never the running refresh, and
    # header and packages describe one single scan_run_id.
    assert during["scan_run_id"] == before["scan_run_id"]
    assert during["scan_run_id"] != claimed.scan_run_id


def test_a_running_post_update_refresh_retains_a_previous_zero_success(
    tmp_path: Path,
) -> None:
    """Case 2. SUCCESS/0 is a real terminal observation, not an absence."""

    system = _system(tmp_path)
    job = _start(system)
    assert system.worker.run_once().status is PackageUpdateWorkerCycleStatus.TERMINAL
    zero = _terminalize_a_fresh_scan(system, job.resource_id, packages=())

    _claim_post_update_refresh(system, job.job_id)

    during = _published_scan(system.store, system.authority, job.resource_id)
    assert during["status"] == "success"
    assert during["pending_count"] == 0
    assert during["packages"] == ()
    assert during["scan_run_id"] == zero.scan_run_id
    assert during["post_update_scan_pending"] is True


def test_a_running_post_update_refresh_retains_a_previous_unknown(
    tmp_path: Path,
) -> None:
    """Case 3. A real failed/UNKNOWN terminal result is retained as-is --
    never softened into `scanning`, never synthesized into success."""

    system = _system(tmp_path)
    job = _start(system)
    assert system.worker.run_once().status is PackageUpdateWorkerCycleStatus.TERMINAL
    failed = _terminalize_a_fresh_scan(
        system,
        job.resource_id,
        failure=PackageScanFailure.METADATA_REFRESH_FAILED,
    )

    _claim_post_update_refresh(system, job.job_id)

    during = _published_scan(system.store, system.authority, job.resource_id)
    assert during["status"] == "failed"
    assert during["pending_count"] is None
    assert during["packages"] == ()
    assert during["error"]["classification"] == "metadata_refresh_failed"
    assert during["scan_run_id"] == failed.scan_run_id
    assert during["post_update_scan_pending"] is True


def test_the_refreshed_result_replaces_the_retained_one_once_it_terminalizes(
    tmp_path: Path,
) -> None:
    """Cases 4 and 5. The retention lasts exactly as long as the refresh is
    running: whatever it really proves -- success or failure -- is published
    the moment it terminalizes, and pending drops to False."""

    for failure, expected_status, expected_pending in (
        (None, "success", 0),
        (PackageScanFailure.METADATA_REFRESH_FAILED, "failed", None),
    ):
        system = _system(tmp_path / f"case-{expected_status}")
        job = _start(system)
        assert (
            system.worker.run_once().status is PackageUpdateWorkerCycleStatus.TERMINAL
        )
        claimed = _claim_post_update_refresh(system, job.job_id)
        retained = _published_scan(system.store, system.authority, job.resource_id)
        assert retained["pending_count"] == 2
        assert retained["post_update_scan_pending"] is True

        if failure is None:
            system.authority.finalize_successful_package_scan(
                claimed.scan_run_id,
                os_id="debian",
                os_version="12",
                packages=(),
                reboot_required=None,
            )
        else:
            system.authority.finalize_failed_package_scan(
                claimed.scan_run_id,
                failure_class=failure,
                error_message="fresh post-update scan failed",
            )

        published = _published_scan(system.store, system.authority, job.resource_id)
        assert published["status"] == expected_status
        assert published["pending_count"] == expected_pending
        assert published["scan_run_id"] == claimed.scan_run_id
        assert published["post_update_scan_pending"] is False
        system.store.close()


def test_restart_while_the_linked_refresh_runs_still_retains_the_result(
    tmp_path: Path,
) -> None:
    """Case 6. The contract is durable state, not in-process memory."""

    system = _system(tmp_path)
    job = _start(system)
    assert system.worker.run_once().status is PackageUpdateWorkerCycleStatus.TERMINAL
    claimed = _claim_post_update_refresh(system, job.job_id)
    before = _published_scan(system.store, system.authority, job.resource_id)

    path = system.store.path
    system.store.close()
    store = InventoryAuthorityStore(path, now=system.clock)
    authority = InventoryAuthority(store, now=system.clock)
    try:
        assert authority.recover_interrupted_package_scans() == ()
        after = _published_scan(store, authority, job.resource_id)
        assert after["status"] == "success"
        assert after["pending_count"] == 2
        assert after["packages"] == before["packages"]
        assert after["scan_run_id"] == before["scan_run_id"]
        assert after["scan_run_id"] != claimed.scan_run_id
        assert after["post_update_scan_pending"] is True
    finally:
        store.close()


def test_an_ordinary_periodic_running_scan_still_publishes_scanning(
    tmp_path: Path,
) -> None:
    """Case 7. The retention is scoped to the post-update refresh contract.

    With no post-update request occupying this run, an ordinary periodic scan
    keeps its intended `scanning` semantics -- this fix must not silently turn
    every running scan into "show the old numbers".
    """

    system = _system(tmp_path)
    resource, _scan, _approval = _add_approved_resource(system.store, system.authority)

    baseline = _published_scan(system.store, system.authority, resource.resource_id)
    assert baseline["status"] == "success"
    assert baseline["post_update_scan_pending"] is False

    running = system.authority.issue_package_scan(resource.resource_id)

    during = _published_scan(system.store, system.authority, resource.resource_id)
    assert during["status"] == "scanning"
    assert during["pending_count"] is None
    assert during["packages"] == ()
    assert during["plan_fingerprint"] is None
    assert during["scan_run_id"] == running.scan_run_id
    assert during["post_update_scan_pending"] is False


def test_every_published_scan_header_and_package_list_share_one_run_id(
    tmp_path: Path,
) -> None:
    """The 2B invariant, asserted directly against the store.

    Whatever run the header names, the published package list must be exactly
    that run's frozen rows -- never another run's, and never a truncated view
    of one because a newer run happened to be in flight.
    """

    system = _system(tmp_path)
    job = _start(system)
    assert system.worker.run_once().status is PackageUpdateWorkerCycleStatus.TERMINAL
    _claim_post_update_refresh(system, job.job_id)

    for resource in InventoryPublication(
        system.store, system.authority
    ).read().resources:
        scan = resource["package_scan"]
        if scan["scan_run_id"] is None:
            assert scan["packages"] == ()
            continue
        with system.store._transaction() as connection:
            rows = connection.execute(
                "SELECT package_name FROM package_scan_packages "
                "WHERE scan_run_id=? ORDER BY package_index",
                (scan["scan_run_id"],),
            ).fetchall()
        if scan["status"] != "success":
            # Only a successful run publishes packages at all.
            assert scan["packages"] == ()
            continue
        assert [entry["name"] for entry in scan["packages"]] == [
            str(row["package_name"]) for row in rows
        ]
        assert scan["pending_count"] == len(rows)
