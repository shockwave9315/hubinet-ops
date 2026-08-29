"""Automatic single-worker scheduler for current Debian/Ubuntu LXC scans."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import threading

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
        self._settings_lock = threading.Lock()
        self._cycle_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def interval_seconds(self) -> int:
        with self._settings_lock:
            return self._interval_seconds

    def configure_interval_seconds(self, interval_seconds: int) -> None:
        """Controlled-input seam; this stage has no HTTP/HA writer for it."""

        validated = validate_package_scan_interval_seconds(interval_seconds)
        with self._settings_lock:
            self._interval_seconds = validated
        self._wake_event.set()

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.is_running:
            return
        self._stop_event.clear()
        self._wake_event.clear()
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
        while not self._stop_event.is_set():
            try:
                self.run_once()
            except Exception:  # noqa: BLE001 - never silently kill the scheduler
                _LOGGER.exception("package scan scheduler cycle failed")
            self._wake_event.wait(self.interval_seconds)
            self._wake_event.clear()

    def run_once(self) -> tuple[PackageScanCycleOutcome, ...]:
        if not self._cycle_lock.acquire(blocking=False):
            return ()
        outcomes: list[PackageScanCycleOutcome] = []
        try:
            resources = tuple(
                resource
                for resource in self._store.list_resources()
                if resource.resource_type == "lxc"
                and resource.presence == "present"
                and resource.lifecycle == "active"
                and resource.active_binding_id is not None
            )
            for resource in resources:
                run = None
                try:
                    run = self._authority.issue_package_scan(resource.resource_id)
                    completed = run_package_scan(
                        self._authority, run, self._host_control
                    )
                    outcomes.append(
                        PackageScanCycleOutcome(
                            resource.resource_id,
                            completed.outcome.value if completed.outcome else "unknown",
                            run.scan_run_id,
                        )
                    )
                except AuthorityConflict:
                    outcomes.append(
                        PackageScanCycleOutcome(resource.resource_id, "conflict", None)
                    )
                except Exception:  # noqa: BLE001 - preserve latest-attempt unknown semantics
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
                                error_message="package scan scheduler execution failed",
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
