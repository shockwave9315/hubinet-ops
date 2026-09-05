"""Single worker with monotonic periodic and durable post-update scan lanes.

Post-update wakes are latency hints only and never reset the absolute periodic
deadline. Interval reconfiguration explicitly re-anchors that deadline from
the configuration instant.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import threading
import time
from collections.abc import Callable

from app.inventory import (
    AuthorityConflict,
    InventoryAuthority,
    InventoryAuthorityStore,
    PackageScanFailure,
    PackageScanLifecycle,
)
from app.inventory_runtime_config import validate_package_scan_interval_seconds
from app.package_scan import PackageScanHostControl, run_package_scan


_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PackageScanCycleOutcome:
    resource_id: str
    status: str
    scan_run_id: str | None


class PackageScanScheduler:
    """One global worker; durable authority supplies per-resource single-flight."""

    def __init__(
        self,
        authority: InventoryAuthority,
        store: InventoryAuthorityStore,
        host_control: PackageScanHostControl,
        *,
        interval_seconds: int,
        initial_delay_seconds: int,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._authority = authority
        self._store = store
        self._host_control = host_control
        self._interval_seconds = validate_package_scan_interval_seconds(
            interval_seconds
        )
        if (
            type(initial_delay_seconds) is not int
            or not 0 <= initial_delay_seconds <= 600
        ):
            raise ValueError("initial package scan delay must be from 0 through 600")
        self._initial_delay_seconds = initial_delay_seconds
        self._monotonic = monotonic
        self._settings_lock = threading.Lock()
        self._post_update_wake_pending = False
        self._interval_generation = 0
        self._cycle_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def interval_seconds(self) -> int:
        with self._settings_lock:
            return self._interval_seconds

    def configure_interval_seconds(self, interval_seconds: int) -> None:
        """Set cadence and re-anchor its deadline from the current instant.

        This controlled-input seam has no HTTP/HA writer. Reconfiguration is
        the one wake that deliberately replaces the current periodic deadline;
        post-update wakes never do.
        """

        validated = validate_package_scan_interval_seconds(interval_seconds)
        with self._settings_lock:
            self._interval_seconds = validated
            self._interval_generation += 1
        self._wake_event.set()

    def wake_for_post_update_scan(self) -> None:
        """Wake only the durable post-update request lane.

        The event is an in-process latency hint.  The request itself was
        committed atomically with SUCCEEDED and is rediscovered after a
        restart even if this wake is lost.
        """

        with self._settings_lock:
            self._post_update_wake_pending = True
        self._wake_event.set()

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.is_running:
            return
        self._stop_event.clear()
        self._wake_event.clear()
        with self._settings_lock:
            self._post_update_wake_pending = False
        self._thread = threading.Thread(
            target=self._run_loop,
            name="hubinet-package-scan-scheduler",
            daemon=True,
        )
        self._thread.start()

    def stop(self, *, grace_seconds: float = 10.0) -> None:
        self._stop_event.set()
        self._wake_event.set()
        if self._thread is not None:
            self._thread.join(timeout=grace_seconds)

    def _run_loop(self) -> None:
        if self._stop_event.wait(self._initial_delay_seconds):
            return
        # Startup handles both the ordinary periodic population and any
        # durable post-update request whose in-memory wake was lost to the
        # prior process exiting.
        try:
            self.run_once()
        except Exception:  # noqa: BLE001 - never silently kill the scheduler
            _LOGGER.exception("package scan scheduler cycle failed")
        with self._settings_lock:
            interval = self._interval_seconds
            interval_generation = self._interval_generation
        next_periodic_deadline = self._monotonic() + interval
        while not self._stop_event.is_set():
            remaining = max(0.0, next_periodic_deadline - self._monotonic())
            self._wake_event.wait(remaining)
            self._wake_event.clear()
            if self._stop_event.is_set():
                return
            with self._settings_lock:
                post_update = self._post_update_wake_pending
                self._post_update_wake_pending = False
                interval = self._interval_seconds
                current_generation = self._interval_generation
            now = self._monotonic()
            if current_generation != interval_generation:
                # Explicit policy: a configured interval starts now. This is
                # independent of the durable post-update request lane.
                interval_generation = current_generation
                next_periodic_deadline = now + interval
            periodic_due = now >= next_periodic_deadline
            try:
                if periodic_due:
                    # run_once handles durable requests first and excludes
                    # those resources from the periodic lane in this cycle.
                    self.run_once()
                elif post_update:
                    self.run_post_update_once()
            except Exception:  # noqa: BLE001 - never silently kill the scheduler
                _LOGGER.exception("package scan scheduler cycle failed")
            finally:
                if periodic_due:
                    # Preserve absolute cadence. Long work or repeated wakes
                    # cannot shift the deadline by a fresh full interval.
                    now = self._monotonic()
                    while next_periodic_deadline <= now:
                        next_periodic_deadline += interval

    def run_once(self) -> tuple[PackageScanCycleOutcome, ...]:
        """Run durable post-update requests, then one ordinary full cycle."""

        return self._run_once(include_periodic=True)

    def run_post_update_once(self) -> tuple[PackageScanCycleOutcome, ...]:
        """Run only successful-job requests; never scan unrelated resources."""

        return self._run_once(include_periodic=False)

    def _run_once(
        self, *, include_periodic: bool
    ) -> tuple[PackageScanCycleOutcome, ...]:
        if not self._cycle_lock.acquire(blocking=False):
            return ()
        outcomes: list[PackageScanCycleOutcome] = []
        try:
            requested_resources: set[str] = set()
            for job_id, resource_id in self._authority.pending_post_update_package_scans():
                requested_resources.add(resource_id)
                run = None
                try:
                    run = self._authority.issue_post_update_package_scan(job_id)
                    self._execute_run(run, outcomes)
                except AuthorityConflict:
                    outcomes.append(
                        PackageScanCycleOutcome(resource_id, "conflict", None)
                    )
                except Exception:  # noqa: BLE001 - preserve latest-attempt unknown semantics
                    _LOGGER.exception(
                        "package scan for resource %s failed unexpectedly",
                        resource_id,
                    )
                    if run is not None:
                        persisted = self._store.package_scan_run(run.scan_run_id)
                        if persisted.lifecycle is PackageScanLifecycle.RUNNING:
                            self._authority.finalize_failed_package_scan(
                                run.scan_run_id,
                                failure_class=PackageScanFailure.EXECUTION_FAILED,
                                error_message="package scan scheduler execution failed",
                            )
                    outcomes.append(
                        PackageScanCycleOutcome(
                            resource_id,
                            "failed",
                            run.scan_run_id if run is not None else None,
                        )
                    )

            if include_periodic:
                resources = tuple(
                    resource
                    for resource in self._store.list_resources()
                    if resource.resource_id not in requested_resources
                    and resource.resource_type == "lxc"
                    and resource.presence == "present"
                    and resource.lifecycle == "active"
                    and resource.active_binding_id is not None
                )
                for resource in resources:
                    run = None
                    try:
                        run = self._authority.issue_package_scan(resource.resource_id)
                        self._execute_run(run, outcomes)
                    except AuthorityConflict:
                        outcomes.append(
                            PackageScanCycleOutcome(
                                resource.resource_id, "conflict", None
                            )
                        )
                    except Exception:  # noqa: BLE001 - latest attempt becomes unknown
                        _LOGGER.exception(
                            "package scan for resource %s failed unexpectedly",
                            resource.resource_id,
                        )
                        if run is not None:
                            persisted = self._store.package_scan_run(run.scan_run_id)
                            if persisted.lifecycle is PackageScanLifecycle.RUNNING:
                                self._authority.finalize_failed_package_scan(
                                    run.scan_run_id,
                                    failure_class=PackageScanFailure.EXECUTION_FAILED,
                                    error_message=(
                                        "package scan scheduler execution failed"
                                    ),
                                )
                        outcomes.append(
                            PackageScanCycleOutcome(
                                resource.resource_id,
                                "failed",
                                run.scan_run_id if run is not None else None,
                            )
                        )
            return tuple(outcomes)
        finally:
            self._cycle_lock.release()

    def _execute_run(self, run, outcomes: list[PackageScanCycleOutcome]) -> None:
        completed = run_package_scan(self._authority, run, self._host_control)
        outcomes.append(
            PackageScanCycleOutcome(
                run.resource_id,
                completed.outcome.value if completed.outcome else "unknown",
                run.scan_run_id,
            )
        )
