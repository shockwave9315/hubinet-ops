"""The dark execution-time APT plan equality gate for one package-update job.

This is NEXT-C: it fills the missing proof between an approved job's
confirmed pre-update snapshot and (future, unimplemented) package mutation --

    snapshot_confirmed
      -> fresh execution-time APT metadata refresh + simulation
      -> canonical material plan (the SAME parser package scanning uses)
      -> atomic authority equality decision against the job's frozen rows

and performs ZERO package mutation of its own. See ``ARCHITECTURE.md``,
"Execution-time plan equality", and ``PRODUCT.md`` rule 2.

Nothing on the production HTTP, Home Assistant, scheduler, bootstrap, or
updater path calls this module. It exists only for hermetic tests today,
exactly like ``app/package_update_snapshot.py``. A successful equality
result is evidence for this one invocation, never a durable "safe to
mutate" flag: it changes nothing about the job's checkpoint, and any future
mutation stage must re-run this exact gate immediately before it mutates,
not trust an earlier pass.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from app.inventory import (
    AuthorityConflict,
    AuthorityNotFound,
    InventoryAuthority,
    PackageScanFailure,
    PackageUpdateCheckpoint,
    PackageUpdateExecutionOutcome,
    PackageUpdateJob,
    PackageUpdateJobStatus,
)
from app.package_scan import (
    HostScanFailure,
    PackageScanParseError,
    parse_apt_simulation,
    parse_os_release,
)


@dataclass(frozen=True, slots=True)
class HostExecutionResult:
    context: Mapping[str, Any]
    os_release: str
    simulation_stdout: str


class PackageUpdateExecutionHostControl(Protocol):
    def simulate_exact_update_plan(self, job: PackageUpdateJob) -> HostExecutionResult:
        """Execute the one allowed typed host-control operation.

        Non-mutating: a fixed APT metadata refresh plus an upgrade
        simulation only, against the job's own frozen expected VMID/node,
        exactly like package scanning's host-control operation. Never
        installs, upgrades, removes, or configures a workload package.
        """


class ExecutionGateStatus(StrEnum):
    """The typed outcome of one execution-gate invocation."""

    #: Fresh execution-time material exactly matched the job's frozen rows.
    #: The job is untouched: still ACTIVE at ``snapshot_confirmed``.
    MATCHED = "matched"
    #: Fresh execution-time material differs from the job's frozen rows in
    #: some material way. The job was terminalized as ``blocked``.
    MISMATCHED = "mismatched"
    #: The job is not currently active at exactly ``snapshot_confirmed`` --
    #: it either has not reached this gate yet, or has already gone
    #: terminal for some other reason. Nothing was touched.
    JOB_NOT_READY = "job_not_ready"
    #: Current job/source/resource/approval authority did not hold, either
    #: before the host was ever called (the cheap pre-check) or when the
    #: equality decision was proved. Zero mutation; the job was not
    #: terminalized by this alone.
    AUTHORITY_STALE = "authority_stale"
    #: The host round trip itself failed (busy, timeout, transport failure,
    #: unsupported OS/resource, or a malformed/ambiguous simulation). Never
    #: treated as an empty plan and never as a match.
    HOST_FAILURE = "host_failure"


@dataclass(frozen=True, slots=True)
class ExecutionGateOutcome:
    status: ExecutionGateStatus
    job: PackageUpdateJob | None = None
    failure_class: PackageScanFailure | None = None
    message: str | None = None


def expected_execution_host_context(job: PackageUpdateJob) -> dict[str, Any]:
    return {
        "job_id": job.job_id,
        "resource_id": job.resource_id,
        "binding_id": job.expected_binding_id,
        "locator_generation": job.expected_locator_generation,
        "resource_continuity_revision": job.expected_resource_continuity_revision,
    }


def run_package_update_execution_gate(
    authority: InventoryAuthority,
    job_id: str,
    host_control: PackageUpdateExecutionHostControl,
) -> ExecutionGateOutcome:
    """Run one full execution-time equality gate for one package-update job.

    Host I/O (metadata refresh plus simulation, which may legitimately take
    minutes) runs entirely outside any authority-store transaction. The
    only database writes happen inside
    :meth:`InventoryAuthority.evaluate_package_update_execution_plan`'s own
    single short writer transaction, which never wraps host I/O -- see
    ``ARCHITECTURE.md``, "SQLite writer-contention policy".
    """

    try:
        job = authority.package_update_job(job_id)
    except AuthorityNotFound:
        return ExecutionGateOutcome(
            status=ExecutionGateStatus.JOB_NOT_READY,
            message="package update job does not exist",
        )
    if (
        job.status is not PackageUpdateJobStatus.ACTIVE
        or job.checkpoint is not PackageUpdateCheckpoint.SNAPSHOT_CONFIRMED
    ):
        return ExecutionGateOutcome(
            status=ExecutionGateStatus.JOB_NOT_READY,
            job=job,
            message="package update job is not awaiting the execution-time plan gate",
        )

    # Optional cheap current-authority check before spending a (potentially
    # multi-minute) host round trip on a job that is already known stale.
    # This is an optimization only: the authority compare below re-proves
    # current authority again, in the same transaction as the decision it
    # authorizes, regardless of what happens here.
    try:
        job = authority.revalidate_package_update_job(job_id)
    except AuthorityNotFound:
        return ExecutionGateOutcome(
            status=ExecutionGateStatus.JOB_NOT_READY,
            message="package update job does not exist",
        )
    except AuthorityConflict as exc:
        return ExecutionGateOutcome(
            status=ExecutionGateStatus.AUTHORITY_STALE, job=job, message=str(exc)
        )

    try:
        result = host_control.simulate_exact_update_plan(job)
        if dict(result.context) != expected_execution_host_context(job):
            raise HostScanFailure(
                PackageScanFailure.STALE_TARGET,
                "host-control response context does not match the execution request",
            )
        # Parsed with the exact same canonical parser package scanning uses
        # -- one implementation, never a second independent one -- so
        # execution-time evidence and scan-time evidence can never drift.
        parse_os_release(result.os_release)
        fresh_packages = parse_apt_simulation(result.simulation_stdout)
    except HostScanFailure as exc:
        return ExecutionGateOutcome(
            status=ExecutionGateStatus.HOST_FAILURE,
            job=job,
            failure_class=exc.failure_class,
            message=exc.message,
        )
    except PackageScanParseError as exc:
        return ExecutionGateOutcome(
            status=ExecutionGateStatus.HOST_FAILURE,
            job=job,
            failure_class=PackageScanFailure.MALFORMED_PLAN,
            message=str(exc),
        )
    except TimeoutError:
        return ExecutionGateOutcome(
            status=ExecutionGateStatus.HOST_FAILURE,
            job=job,
            failure_class=PackageScanFailure.TIMEOUT,
            message="package update execution host-control request timed out",
        )
    except Exception:  # noqa: BLE001 - classify without publishing raw exception detail
        return ExecutionGateOutcome(
            status=ExecutionGateStatus.HOST_FAILURE,
            job=job,
            failure_class=PackageScanFailure.EXECUTION_FAILED,
            message="package update execution host-control failed",
        )

    try:
        outcome, decided_job = authority.evaluate_package_update_execution_plan(
            job_id, fresh_packages
        )
    except AuthorityNotFound:
        return ExecutionGateOutcome(
            status=ExecutionGateStatus.JOB_NOT_READY,
            message="package update job does not exist",
        )
    except AuthorityConflict as exc:
        # The pre-host-call checks above already proved the job was ACTIVE
        # at snapshot_confirmed with current authority. Reaching a conflict
        # here means one of those facts changed concurrently while the
        # (potentially multi-minute) host round trip was in flight --
        # either the job went terminal for some other reason, or resource/
        # source authority went stale. Either way nothing was mutated and
        # this decision was refused, never silently treated as a match.
        return ExecutionGateOutcome(
            status=ExecutionGateStatus.AUTHORITY_STALE, job=job, message=str(exc)
        )

    if outcome is PackageUpdateExecutionOutcome.MATCHED:
        return ExecutionGateOutcome(status=ExecutionGateStatus.MATCHED, job=decided_job)
    return ExecutionGateOutcome(status=ExecutionGateStatus.MISMATCHED, job=decided_job)
