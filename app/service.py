from __future__ import annotations

import hashlib
import json
import logging
import math
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any, Callable

import httpx

from .config import Settings
from .contracts import (
    EXECUTOR_PROTOCOL_VERSION,
    EXECUTOR_VERSION,
    SNAPSHOT_PRUNE_DELETED_HISTORY_LIMIT,
    SNAPSHOT_PRUNE_STATE_VERSION,
    evaluate_executor_contract,
    parse_owned_snapshot_name,
)
from .database import Database, utc_now
from .executor import Executor, ExecutorError
from .host_control import HostControlClient, HostControlError
from .mqtt import MqttTelemetry, VERSION
from .security import sanitize_data, sanitize_text
from .stabilization import (
    StabilizationInterrupted,
    StabilizationPolicy,
    Stabilizer,
)
from .state import normalize_state
from .time_utils import parse_utc_timestamp

LOGGER = logging.getLogger("hubinet_ops")

STAGE_PROGRESS = {
    "idle": 0,
    "scanning": 2,
    "preflight": 10,
    "snapshot": 20,
    "updating": 30,
    "waiting_services": 75,
    "healthcheck": 80,
    "verifying": 97,
    "repair": 85,
    "rollback": 88,
    "rollback_wait": 90,
    "rollback_healthcheck": 92,
    "completed": 100,
    "failed": 100,
}
TERMINAL_JOB_STATUSES = {
    "blocked",
    "failed",
    "interrupted",
    "recovered",
    "rolled_back",
    "success",
}
HOST_CONTROL_OPERATION_TYPES = {
    "lifecycle_start",
    "lifecycle_shutdown",
    "lifecycle_reboot",
    "lifecycle_force_stop",
    "snapshot_create",
    "snapshot_create_ram",
    "snapshot_rollback",
    "snapshot_delete",
    "self_update",
    "ct110_system_update",
}
BACKEND_ONLY_EVENT_TYPES = {
    "snapshot_created",
    "snapshot_mutation_succeeded",
}


class HostJobOutcome(str, Enum):
    DEFINITIVE_FAILURE = "definitive_failure"
    OUTCOME_UNKNOWN = "outcome_unknown"
    REMOTE_SUCCEEDED = "remote_succeeded"


def classify_host_job_error(
    error: HostControlError | ValueError,
    *,
    submit_started: bool,
) -> HostJobOutcome:
    """Classify a typed host job without confusing transport loss with failure."""
    if isinstance(error, ValueError):
        return HostJobOutcome.DEFINITIVE_FAILURE
    if error.status in {"failed", "contract_mismatch"}:
        return HostJobOutcome.DEFINITIVE_FAILURE
    if error.http_status in {400, 409, 422}:
        return HostJobOutcome.DEFINITIVE_FAILURE
    if (
        not submit_started
        and error.http_status is not None
        and 400 <= error.http_status < 500
        and error.http_status not in {408, 429}
    ):
        return HostJobOutcome.DEFINITIVE_FAILURE
    if submit_started:
        return HostJobOutcome.OUTCOME_UNKNOWN
    return HostJobOutcome.DEFINITIVE_FAILURE


class ConflictError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        required_action: str | None = None,
    ) -> None:
        super().__init__(sanitize_text(message, limit=1000))
        self.code = sanitize_text(code, limit=100)
        self.required_action = (
            sanitize_text(required_action, limit=1000) if required_action else None
        )

    def detail(self) -> dict[str, str]:
        result = {"code": self.code, "message": str(self)}
        if self.required_action:
            result["required_action"] = self.required_action
        return result


class SnapshotPruneOutcomeUnknown(RuntimeError):
    """Keep a prune job active when a child deletion cannot be reconciled safely."""


class HostOperationOutcomeUnknown(RuntimeError):
    """A durable host mutation may have happened and must only be reconciled."""


class OpsService:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        executor: Executor,
        mqtt: MqttTelemetry | None = None,
        stabilizer: Stabilizer | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] | None = None,
        host_control: HostControlClient | None = None,
    ):
        self.settings = settings
        self.db = db
        self.executor = executor
        self.mqtt = mqtt or MqttTelemetry({"enabled": False}, settings.resources)
        self._stop = threading.Event()
        self._monotonic = monotonic
        self._now = now or (lambda: datetime.now(UTC))
        self._last_full_refresh: str | None = None
        self._last_resource_refresh: str | None = None
        self._resource_refresh_sequence = 0
        self._last_published_agent_state: dict[str, Any] | None = None
        self._agent_publish_context = threading.local()
        self._agent_publish_lock = threading.RLock()
        self.stabilizer = stabilizer or Stabilizer(executor, self._stop)
        self.host_control = host_control
        self._scan_all_lock = threading.Lock()
        self._scan_locks = {vmid: threading.Lock() for vmid in settings.resources}
        self._recovery_lock = threading.RLock()
        self._recovery_wakeup = threading.Event()
        self._recovery_due: dict[int, float] = {}
        self._observed_health: dict[int, str] = {}
        self._worker = threading.Thread(target=self._worker_loop, name="ops-worker", daemon=True)
        self._scheduler = threading.Thread(
            target=self._scheduler_loop,
            name="ops-scheduler",
            daemon=True,
        )
        self._telemetry = threading.Thread(
            target=self._telemetry_loop,
            name="ops-telemetry",
            daemon=True,
        )
        self._recovery_worker = threading.Thread(
            target=self._recovery_loop,
            name="ops-recovery-scan",
            daemon=True,
        )
        self.mqtt.set_state_provider(self._mqtt_snapshot)

    def start(self) -> None:
        self._consume_offline_recovery_events()
        self._reconcile_startup_jobs()
        self.reconcile_snapshot_proofs()
        self._ensure_initial_states()
        self.mqtt.start()
        self._worker.start()
        self._telemetry.start()
        self._recovery_worker.start()
        monitoring_scheduler = self.settings.monitoring_scheduler
        if bool(
            monitoring_scheduler.get(
                "enabled",
                self.settings.scheduler.get("enabled", False),
            )
        ):
            self._scheduler.start()

    def stop(self) -> None:
        self._stop.set()
        self._recovery_wakeup.set()
        self._worker.join(timeout=5)
        self._telemetry.join(timeout=5)
        self._recovery_worker.join(timeout=5)
        if self._scheduler.is_alive():
            self._scheduler.join(timeout=5)
        self.mqtt.stop()

    def list_containers(self) -> list[dict[str, Any]]:
        return [
            item for item in self.list_resources()
            if item["resource_type"] == "lxc"
        ]

    def list_resources(self) -> list[dict[str, Any]]:
        return [
            self._resource_item(vmid, cfg)
            for vmid, cfg in sorted(self.settings.resources.items())
        ]

    def get_resource(self, vmid: int) -> dict[str, Any]:
        normalized_vmid = int(vmid)
        return self._resource_item(normalized_vmid, self._resource(normalized_vmid))

    def _resource_item(self, vmid: int, cfg: dict[str, Any]) -> dict[str, Any]:
        item = {
            "vmid": vmid,
            "resource_type": cfg.get("resource_type", "lxc"),
            "name": cfg.get("name", f"resource-{vmid}"),
            "display_name": cfg.get("display_name", cfg.get("name", f"resource-{vmid}")),
            "enabled": bool(cfg.get("enabled", False)),
            "adapter": cfg.get("adapter", "apt"),
            "criticality": cfg.get("criticality", "medium"),
            "ip_address": cfg.get("ip_address"),
            "guest_agent": bool(cfg.get("guest_agent", False)),
            "approval_mode": cfg.get("approval_mode", "always"),
            "pre_update_snapshot": bool(cfg.get("pre_update_snapshot", False)),
            "automatic_rollback": bool(cfg.get("automatic_rollback", False)),
            "manual_rollback_allowed": bool(cfg.get("manual_rollback_allowed", False)),
            "manual_snapshot_restore_allowed": bool(
                cfg.get("manual_snapshot_restore_allowed", False)
            ),
            "snapshot_retention_count": self._snapshot_retention_count(vmid),
            "dashboard_path": cfg.get("dashboard_path", f"/hubinet-ops/ct-{vmid}"),
            "operator_capabilities": self._capabilities(vmid),
            "monitoring": self._monitoring(vmid),
            "state": self.db.get_resource_state(vmid) or {},
        }
        return item

    def list_states(self) -> dict[str, Any]:
        resources = {
            str(item["vmid"]): item for item in self.db.list_resource_states()
        }
        return {
            "version": VERSION,
            "generated_at": utc_now(),
            "resources": resources,
            "containers": {
                vmid: item
                for vmid, item in resources.items()
                if item.get("resource_type", "lxc") == "lxc"
            },
        }
    def get_state(self, vmid: int) -> dict[str, Any]:
        self._resource(vmid)
        state = self.db.get_resource_state(vmid)
        if state is None:
            state = self._save_state(vmid, self._base_state(vmid))
        return state

    def refresh_container(self, vmid: int, *, operator: bool = False) -> dict[str, Any]:
        self._suppress_agent_publication()
        try:
            return self._refresh_container(vmid, operator=operator)
        finally:
            self._resume_agent_publication()
            with self._agent_publish_lock:
                self._resource_refresh_sequence += 1
                self._last_resource_refresh = self._utc_second_timestamp()
            self._publish_agent_state_if_changed()

    def _refresh_container(self, vmid: int, *, operator: bool = False) -> dict[str, Any]:
        cfg = self._resource(vmid)
        if operator:
            self._require_capability(vmid, "refresh")
        elif not self._monitoring(vmid)["inspect"]:
            return self.get_state(vmid)
        if not bool(cfg.get("enabled", False)):
            state = self.get_state(vmid)
            state.update({"health_status": "unknown", "health_score": 0})
            saved = self._save_state(vmid, state)
            self._observe_health(vmid, str(saved.get("health_status", "unknown")))
            return saved
        host_runtime = "unknown"
        host_runtime_error: str | None = None
        if str(cfg.get("resource_type") or "lxc") == "lxc":
            try:
                host_status = self._host_status(vmid)
                host_runtime = str(
                    host_status.get("lxc_status")
                    or host_status.get("runtime_status")
                    or host_status.get("status")
                    or "unknown"
                )
            except ValueError as exc:
                host_runtime_error = sanitize_text(exc, limit=2000)
                LOGGER.warning(
                    "PVE runtime probe failed during refresh CT%s: %s",
                    vmid,
                    host_runtime_error,
                )
        executor_contract_error: str | None = None
        if (
            str(cfg.get("resource_type") or "lxc") == "lxc"
            and str(cfg.get("adapter") or "apt") == "apt"
        ):
            try:
                self._require_compatible_executor(vmid, publish_state=False)
            except ExecutorError as exc:
                executor_contract_error = sanitize_text(exc, limit=2000)
                LOGGER.warning(
                    "Executor compatibility probe failed during refresh CT%s: %s",
                    vmid,
                    executor_contract_error,
                )
        try:
            inspected = _executor_data(self.executor.run("inspect", vmid, timeout=120))
            # Inspect may take long enough for a job to reach a terminal state.
            # Re-read the latest DB state after I/O so telemetry cannot resurrect
            # stale operation/plan/job fields captured before that transition.
            state = self.get_state(vmid)
            state.update(inspected)
            if vmid == 110 and cfg.get("adapter") == "agent_self":
                installed_version = str(
                    inspected.get("agent_version") or VERSION
                )
                state["application_current_version"] = installed_version
                if state.get("application_latest_version") == installed_version:
                    state["application_release_check_status"] = "up_to_date"
            if str(cfg.get("resource_type") or "lxc") == "lxc":
                state["lxc_status"] = host_runtime
                state["runtime_status"] = host_runtime
            state["health_status"] = inspected.get(
                "health_status",
                inspected.get("health", "unknown"),
            )
            if host_runtime == "running":
                state["intentional_shutdown"] = False
                state["lifecycle_health_pending"] = False
                if state.get("lifecycle_status") != "running":
                    state["expected_lxc_status"] = None
            state["last_refresh"] = utc_now()
            preserve_operation_error = self._has_terminal_operation_error(state)
            if executor_contract_error is not None and not preserve_operation_error:
                state["last_error"] = executor_contract_error
            elif host_runtime_error is not None and not preserve_operation_error:
                state["last_error"] = host_runtime_error
            elif not preserve_operation_error:
                state["last_error"] = None
        except ExecutorError as exc:
            state = self.get_state(vmid)
            guest_error = sanitize_text(exc, limit=2000)
            if str(cfg.get("resource_type") or "lxc") == "lxc":
                health = (
                    "offline"
                    if host_runtime == "stopped"
                    else "degraded"
                    if host_runtime == "running"
                    else "unknown"
                )
            else:
                health = "offline"
            state.update({"health_status": health, "health_score": 0})
            if str(cfg.get("resource_type") or "lxc") == "lxc":
                state["lxc_status"] = host_runtime
                state["runtime_status"] = host_runtime
            state["last_refresh"] = utc_now()
            if not self._has_terminal_operation_error(state):
                state["last_error"] = (
                    executor_contract_error
                    or host_runtime_error
                    or guest_error
                )
        saved = self._save_state(vmid, state)
        self._observe_health(vmid, str(saved.get("health_status", "unknown")))
        if operator and self._capabilities(vmid).get("snapshot_list", False):
            try:
                self._refresh_snapshot_state(vmid)
            except (ExecutorError, HostControlError, ValueError) as exc:
                # The primary resource observation is still useful.  The
                # snapshot helper preserves the previous canonical snapshot
                # model and marks it stale instead of inventing a deletion.
                LOGGER.warning(
                    "Snapshot refresh failed during operator refresh for %s: %s",
                    vmid,
                    exc,
                )
            saved = self.get_state(vmid)
        return saved

    @staticmethod
    def _has_terminal_operation_error(state: dict[str, Any]) -> bool:
        """Keep errors that explain an explicitly unsuccessful operation outcome."""

        return state.get("last_operation_result") in {
            "failed",
            "interrupted",
            "rolled_back",
            "manual_intervention",
        }

    def refresh_all(self, *, operator: bool = False) -> list[dict[str, Any]]:
        completed = False
        self._suppress_agent_publication()
        try:
            refreshed = [
                self.refresh_container(vmid, operator=operator)
                for vmid, cfg in sorted(self.settings.resources.items())
                if bool(cfg.get("enabled", False))
                and (
                    self._capabilities(vmid)["refresh"]
                    if operator
                    else self._monitoring(vmid)["inspect"]
                )
            ]
            completed = True
            return refreshed
        finally:
            self._resume_agent_publication()
            if completed:
                with self._agent_publish_lock:
                    self._last_full_refresh = self._utc_second_timestamp()
                self._publish_agent_state_if_changed()

    def scan_all(self, *, operator: bool = True) -> list[dict[str, Any]]:
        if not self._scan_all_lock.acquire(blocking=False):
            return [{"status": "skipped", "reason": "scan_all_already_running"}]
        try:
            return [
                self.scan_container(vmid, operator=operator)
                for vmid, cfg in sorted(self.settings.resources.items())
                if bool(cfg.get("enabled", False))
                and (
                    self._capabilities(vmid)["scan"]
                    if operator
                    else self._monitoring(vmid)["update_scan"]
                )
            ]
        finally:
            self._scan_all_lock.release()

    def scan_container(
        self,
        vmid: int,
        *,
        operator: bool = True,
        source: str = "operator",
    ) -> dict[str, Any]:
        cfg = self._resource(vmid)
        if operator:
            self._require_capability(vmid, "scan")
        elif not self._monitoring(vmid)["update_scan"]:
            return {"vmid": vmid, "status": "skipped", "reason": "monitoring_scan_disabled"}
        lock = self._scan_locks[vmid]
        if not lock.acquire(blocking=False):
            if operator:
                raise ValueError("scan_already_running")
            return {"vmid": vmid, "status": "skipped", "reason": "scan_already_running"}
        try:
            active_job = self.db.get_active_job(vmid)
            if active_job is not None:
                if operator:
                    raise ValueError("job_active")
                return {
                    "vmid": vmid,
                    "status": "skipped",
                    "reason": "job_active",
                    "job_id": active_job["id"],
                }
            if self.get_state(vmid).get("lifecycle_status") == "running":
                if operator:
                    raise ValueError("lifecycle_active")
                return {
                    "vmid": vmid,
                    "status": "skipped",
                    "reason": "lifecycle_active",
                }
            return self._scan_container_locked(vmid, cfg, source=source)
        finally:
            lock.release()

    def _scan_container_locked(
        self,
        vmid: int,
        cfg: dict[str, Any],
        *,
        source: str = "operator",
    ) -> dict[str, Any]:
        if not bool(cfg.get("enabled", False)):
            return {"vmid": vmid, "status": "disabled"}
        if (
            vmid == 110
            and cfg.get("adapter") == "agent_self"
            and cfg.get("resource_type", "lxc") == "lxc"
        ):
            return self._scan_ct110_system_locked(vmid, cfg, source=source)
        if cfg.get("adapter", "apt") != "apt" or cfg.get("resource_type", "lxc") != "lxc":
            return {
                "vmid": vmid,
                "status": "unsupported_adapter",
                "adapter": cfg.get("adapter"),
            }

        state = self.get_state(vmid)
        prior_operation = state["operation_status"]
        prior_stage = state["job_stage"]
        state.update(
            {
                "update_status": "scanning",
                "job_stage": "scanning",
            }
        )
        if state.get("last_operation_result") is None:
            state["last_error"] = None
        self._save_state(vmid, state)
        try:
            data = _executor_data(self.executor.run("scan", vmid, timeout=700))
        except ExecutorError as exc:
            state = self.get_state(vmid)
            state.update(
                {
                    "update_status": "unknown",
                    "job_stage": prior_stage,
                    "last_error": sanitize_text(exc, limit=2000),
                    "last_scan": utc_now(),
                    "operation_status": prior_operation,
                }
            )
            self._save_state(vmid, state)
            self._notify_ha(self._notification("scan_failed", vmid, error=str(exc)))
            return {
                "vmid": vmid,
                "status": "error",
                "error": sanitize_text(exc, limit=2000),
            }

        count = max(0, int(data.get("pending_count", 0) or 0))
        data = dict(data)
        data["packages"] = list(data.get("packages") or [])[:200]
        state = self.get_state(vmid)
        state.update(
            {
                "updates": data,
                "pending_updates": count,
                "update_status": "update_available" if count else "up_to_date",
                "job_stage": prior_stage,
                "last_scan": utc_now(),
                "operation_status": prior_operation,
            }
        )
        if count == 0:
            self.db.invalidate_active_plans(vmid)
            state.update(
                {
                    "risk": "none",
                    "active_plan_id": None,
                    "active_plan_status": None,
                }
            )
            self._save_state(vmid, state)
            return {"vmid": vmid, "status": "up_to_date", "data": data, "source": source}

        # Observation-only resources report available updates but never create a
        # plan that cannot be approved by policy.
        if not self._capabilities(vmid)["approve"]:
            self.db.invalidate_active_plans(vmid)
            state.update(
                {
                    "risk": _risk_for(cfg, data),
                    "active_plan_id": None,
                    "active_plan_status": None,
                    "operation_status": "idle",
                    "job_stage": "idle",
                }
            )
            self._save_state(vmid, state)
            return {
                "vmid": vmid,
                "status": "updates_observed",
                "data": data,
                "source": source,
            }

        fingerprint = str(data.get("fingerprint") or _fingerprint(data))
        active = self.db.find_active_plan(vmid, fingerprint)
        if active is None:
            active = self.db.create_plan(
                vmid=vmid,
                container_name=str(cfg.get("name", f"ct-{vmid}")),
                fingerprint=fingerprint,
                risk=_risk_for(cfg, data),
                payload=data,
                ttl_minutes=int(
                    self.settings.scheduler.get("approval_ttl_minutes", 1440)
                ),
            )
            status = "plan_created"
            self._notify_ha(
                self._notification(
                    "approval_required",
                    vmid,
                    pending_count=count,
                    risk=active["risk"],
                )
            )
        else:
            status = "existing_plan"
        state.update(
            {
                "risk": active["risk"],
                "active_plan_id": active["id"],
                "active_plan_status": active["status"],
                "operation_status": "waiting_approval",
                "job_stage": "idle",
            }
        )
        self._save_state(vmid, state)
        return {"vmid": vmid, "status": status, "plan": active, "source": source}

    def _scan_ct110_system_locked(
        self,
        vmid: int,
        cfg: dict[str, Any],
        *,
        source: str,
    ) -> dict[str, Any]:
        host = self._require_host_control("CT110 system update scan")
        state = self.get_state(vmid)
        prior_operation = state["operation_status"]
        prior_stage = state["job_stage"]
        state.update(
            {
                "system_update_status": "scanning",
                "job_stage": "system_scanning",
            }
        )
        self._save_state(vmid, state)
        try:
            data = self._validate_ct110_system_scan(host.scan_ct110_system(vmid))
        except (HostControlError, ValueError) as exc:
            state = self.get_state(vmid)
            state.update(
                {
                    "system_update_status": "unknown",
                    "system_last_scan": self._utc_second_timestamp(),
                    "system_last_error": sanitize_text(exc, limit=2000),
                    "job_stage": prior_stage,
                    "operation_status": prior_operation,
                }
            )
            self._save_state(vmid, state)
            return {
                "vmid": vmid,
                "status": "error",
                "error": sanitize_text(exc, limit=2000),
            }

        count = int(data["pending_count"])
        fingerprint = str(data["fingerprint"])
        payload = {**data, "plan_type": "ct110_system_update"}
        state = self.get_state(vmid)
        state.update(
            {
                "system_updates": data,
                "system_pending_updates": count,
                "system_security_updates": data["security_updates_count"],
                "system_package_names": ", ".join(
                    item["name"] for item in data["packages"]
                )[:255] or None,
                "system_update_status": (
                    "update_available" if count else "up_to_date"
                ),
                "system_last_scan": data["scanned_at"],
                "system_last_error": None,
                "job_stage": prior_stage,
                "operation_status": prior_operation,
            }
        )
        if count == 0:
            active = self.db.find_active_plan(vmid)
            if active is not None and self._plan_type(active) == "ct110_system_update":
                self.db.update_plan_status(str(active["id"]), "superseded")
            state.update(
                {
                    "system_active_plan_id": None,
                    "system_active_plan_status": None,
                }
            )
            self._save_state(vmid, state)
            return {
                "vmid": vmid,
                "status": "up_to_date",
                "data": data,
                "source": source,
            }
        if not self._capabilities(vmid)["approve"]:
            active = self.db.find_active_plan(vmid)
            if active is not None and self._plan_type(active) == "ct110_system_update":
                self.db.update_plan_status(str(active["id"]), "superseded")
            state.update(
                {
                    "system_active_plan_id": None,
                    "system_active_plan_status": None,
                    "operation_status": "idle",
                    "job_stage": "idle",
                }
            )
            self._save_state(vmid, state)
            return {
                "vmid": vmid,
                "status": "updates_observed",
                "plan_created": False,
                "data": data,
                "source": source,
            }

        active = self.db.find_active_plan(vmid, fingerprint)
        if active is not None and self._plan_type(active) != "ct110_system_update":
            raise ValueError("Resolve the active application release plan first")
        if active is None:
            other = self.db.find_active_plan(vmid)
            if other is not None:
                raise ValueError("Resolve the active CT110 plan before system scan")
            active = self.db.create_plan(
                vmid=vmid,
                container_name=str(cfg.get("name", "hubinet-ops")),
                fingerprint=fingerprint,
                risk=_risk_for(cfg, data),
                payload=payload,
                ttl_minutes=int(
                    self.settings.scheduler.get("approval_ttl_minutes", 1440)
                ),
            )
            status = "plan_created"
        else:
            status = "existing_plan"
        state.update(
            {
                "system_active_plan_id": active["id"],
                "system_active_plan_status": active["status"],
                "operation_status": "waiting_approval",
            }
        )
        self._save_state(vmid, state)
        return {
            "vmid": vmid,
            "status": status,
            "plan": active,
            "source": source,
        }

    @staticmethod
    def _validate_ct110_system_scan(raw: Any) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise ValueError("CT110 system scan result must be an object")
        packages = raw.get("packages")
        if not isinstance(packages, list) or len(packages) > 2000:
            raise ValueError("CT110 system scan package list is invalid")
        normalized: list[dict[str, Any]] = []
        for item in packages:
            if not isinstance(item, dict) or set(item) - {
                "name", "current", "target", "security"
            }:
                raise ValueError("CT110 system scan package entry is invalid")
            name = str(item.get("name") or "")
            current = str(item.get("current") or "")
            target = str(item.get("target") or "")
            security = item.get("security")
            if (
                not name
                or len(name) > 255
                or not current
                or len(current) > 255
                or not target
                or len(target) > 255
                or security not in {True, False, None}
            ):
                raise ValueError("CT110 system scan package identity is invalid")
            normalized.append(
                {
                    "name": name,
                    "current": current,
                    "target": target,
                    "security": security,
                }
            )
        if len({item["name"] for item in normalized}) != len(normalized):
            raise ValueError("CT110 system scan contains duplicate packages")
        pending = raw.get("pending_count")
        if isinstance(pending, bool) or pending != len(normalized):
            raise ValueError("CT110 system scan package count is invalid")
        stable = json.dumps(
            normalized, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        fingerprint = hashlib.sha256(stable).hexdigest()
        if raw.get("fingerprint") != fingerprint:
            raise ValueError("CT110 system scan fingerprint is invalid")
        scanned_at = parse_utc_timestamp(raw.get("scanned_at"))
        if scanned_at is None:
            raise ValueError("CT110 system scan timestamp is invalid")
        security_count = sum(item["security"] is True for item in normalized)
        if raw.get("security_updates_count") != security_count:
            raise ValueError("CT110 security update count is invalid")
        reboot = raw.get("reboot_required")
        if reboot not in {True, False, None}:
            raise ValueError("CT110 reboot-required state is invalid")
        return {
            "pending_count": len(normalized),
            "packages": normalized,
            "fingerprint": fingerprint,
            "scanned_at": scanned_at.isoformat(),
            "security_updates_count": security_count,
            "reboot_required": reboot,
        }

    def approve(self, plan_id: str) -> dict[str, Any]:
        candidate = self.db.get_plan(plan_id)
        vmid = int(candidate["vmid"])
        self._require_capability(vmid, "approve")
        lock = self._scan_locks[vmid]
        if not lock.acquire(blocking=False):
            raise ValueError(
                "A scan is running for this container; retry approval after it finishes"
            )
        try:
            if self._plan_type(candidate) == "self_update":
                return self._approve_self_update_plan(candidate)
            if self._plan_type(candidate) == "ct110_system_update":
                return self._approve_ct110_system_plan(candidate)
            plan, job = self.db.approve_plan(plan_id)
            return self._publish_approved_plan(plan, job)
        finally:
            lock.release()

    def approve_active(self, vmid: int, request_id: str | None = None) -> dict[str, Any]:
        self._resource(vmid)
        self._require_capability(vmid, "approve")
        lock = self._scan_locks[vmid]
        if not lock.acquire(blocking=False):
            raise ValueError("A scan is active for this resource")
        try:
            if request_id:
                existing = self.db.get_job_by_request_id(vmid, request_id)
                if existing is not None:
                    if (
                        existing.get("plan_id")
                        and existing.get("operation_type")
                        in {"update", "self_update", "ct110_system_update"}
                    ):
                        return {
                            "plan": self.db.get_plan(str(existing["plan_id"])),
                            "job": existing,
                        }
                    raise ValueError(
                        "request_id was already used for another operation"
                    )
            plan = self._single_waiting_plan(vmid)
            if self._plan_type(plan) == "self_update":
                return self._approve_self_update_plan(plan, request_id)
            if self._plan_type(plan) == "ct110_system_update":
                return self._approve_ct110_system_plan(plan, request_id)
            self._require_compatible_executor(vmid)
            scan = _executor_data(self.executor.run("scan", vmid, timeout=700))
            pending = max(0, int(scan.get("pending_count", 0) or 0))
            fingerprint = str(scan.get("fingerprint") or _fingerprint(scan))
            if pending <= 0 or fingerprint != str(plan["fingerprint"]):
                self.db.update_plan_status(plan["id"], "superseded")
                raise ValueError(
                    "Active plan fingerprint changed; run a new update scan before approval"
                )
            plan, job = self.db.approve_plan(
                plan["id"],
                request_id=request_id or uuid.uuid4().hex,
            )
            return self._publish_approved_plan(plan, job)
        except ExecutorError as exc:
            raise ValueError(str(exc)) from exc
        finally:
            lock.release()

    def reject(self, plan_id: str) -> dict[str, Any]:
        candidate = self.db.get_plan(plan_id)
        self._require_capability(int(candidate["vmid"]), "reject")
        plan = self.db.reject_plan(plan_id)
        vmid = int(plan["vmid"])
        state = self.get_state(vmid)

        updates = {
            "active_plan_id": None,
            "active_plan_status": "rejected",
            "risk": "none",
            "operation_status": "idle",
            "job_stage": "idle",
            "job_progress": 0,
        }

        plan_type = self._plan_type(plan)
        if plan_type == "ct110_system_update":
            updates.update({
                "system_active_plan_id": None,
                "system_active_plan_status": None,
            })

        state.update(updates)
        self._save_state(vmid, state)
        return {"plan": plan}

    def reject_active(self, vmid: int) -> dict[str, Any]:
        self._resource(vmid)
        self._require_capability(vmid, "reject")
        lock = self._scan_locks[vmid]
        if not lock.acquire(blocking=False):
            raise ValueError("A scan is active for this resource")
        try:
            if self.db.get_active_job(vmid) is not None:
                raise ValueError("Another job is already active for this resource")
            plan = self._single_waiting_plan(vmid)
            if self._plan_type(plan) not in {"self_update", "ct110_system_update"}:
                self._require_compatible_executor(vmid)
            return self.reject(plan["id"])
        finally:
            lock.release()

    def _single_waiting_plan(self, vmid: int) -> dict[str, Any]:
        plans = self.db.waiting_plans(vmid)
        if not plans:
            raise ValueError(f"Resource {vmid} has no active waiting plan")
        if len(plans) != 1:
            raise ValueError(f"Resource {vmid} has multiple active waiting plans")
        return plans[0]

    @staticmethod
    def _plan_type(plan: dict[str, Any]) -> str:
        payload = plan.get("payload")
        if not isinstance(payload, dict):
            return "update"
        return str(payload.get("plan_type") or "update")

    def _approve_self_update_plan(
        self,
        plan: dict[str, Any],
        request_id: str | None = None,
    ) -> dict[str, Any]:
        if int(plan["vmid"]) != 110 or self._plan_type(plan) != "self_update":
            raise ValueError("Plan is not a CT110 self-update plan")
        release = self._read_self_update_release(110)
        payload = dict(plan.get("payload") or {})
        if any(
            str(release.get(key) or "") != str(payload.get(key) or "")
            for key in ("fingerprint", "release_id", "version", "bundle_sha256")
        ):
            self.db.update_plan_status(plan["id"], "superseded")
            raise ValueError(
                "Active self-update plan fingerprint changed; create and approve a new plan"
            )
        plan, job = self.db.approve_plan(
            plan["id"],
            request_id=request_id or uuid.uuid4().hex,
            operation_type="self_update",
        )
        published = self._publish_approved_plan(plan, job)
        state = self.get_state(110)
        state.update(
            {
                "application_download_status": "pending",
                "application_validation_status": "pending",
                "application_deployment_status": "queued",
                "application_last_error": None,
            }
        )
        self._save_state(110, state)
        return published

    def _approve_ct110_system_plan(
        self,
        plan: dict[str, Any],
        request_id: str | None = None,
    ) -> dict[str, Any]:
        if int(plan["vmid"]) != 110 or self._plan_type(plan) != "ct110_system_update":
            raise ValueError("Plan is not a CT110 system-update plan")
        host = self._require_host_control("CT110 system update approval")
        current = self._validate_ct110_system_scan(host.scan_ct110_system(110))
        if (
            int(current["pending_count"]) <= 0
            or str(current["fingerprint"]) != str(plan["fingerprint"])
        ):
            self.db.update_plan_status(str(plan["id"]), "superseded")
            raise ValueError(
                "CT110 system update fingerprint changed; run a new system scan"
            )
        plan, job = self.db.approve_plan(
            str(plan["id"]),
            request_id=request_id or uuid.uuid4().hex,
            operation_type="ct110_system_update",
        )
        state = self.get_state(110)
        state.update(
            {
                "system_active_plan_id": plan["id"],
                "system_active_plan_status": "approved",
                "system_update_status": "queued",
            }
        )
        self._save_state(110, state)
        return self._publish_approved_plan(plan, job)

    def _publish_approved_plan(
        self,
        plan: dict[str, Any],
        job: dict[str, Any],
    ) -> dict[str, Any]:
        vmid = int(plan["vmid"])
        state = self.get_state(vmid)
        state.update(
            {
                "active_plan_id": plan["id"],
                "active_plan_status": "approved",
                "active_job_id": job["id"],
                "operation_type": job.get("operation_type", "update"),
                "operation_status": "running",
                "job_stage": (
                    "preflight"
                    if job.get("operation_type", "update") == "update"
                    else "queued"
                ),
                "job_progress": 1,
                "last_operation_result": None,
                "last_error": None,
            }
        )
        self._save_state(vmid, state)
        self._notify_ha(self._notification("job_queued", vmid))
        return {"plan": plan, "job": job}

    def _require_compatible_executor(
        self,
        vmid: int,
        *,
        publish_state: bool = True,
    ) -> dict[str, Any]:
        cfg = self._resource(vmid)
        if cfg.get("adapter", "apt") != "apt":
            raise ExecutorError(f"Resource {vmid} does not use a managed APT executor")
        contract_cfg = cfg.get("executor_contract") or {}
        try:
            payload = _executor_data(self.executor.run("capabilities", vmid, timeout=60))
        except ExecutorError as exc:
            payload = {}
            executor_error = str(exc)
        else:
            executor_error = None
        compatibility = evaluate_executor_contract(
            payload,
            expected_executor_sha256=str(contract_cfg.get("executor_sha256") or ""),
            expected_profile_sha256=str(contract_cfg.get("profile_sha256") or ""),
        )
        state = self.get_state(vmid)
        state.update(compatibility.state_fields())
        state["executor_last_checked_at"] = self._utc_second_timestamp()
        if not compatibility.compatible:
            installed = compatibility.version or "unknown"
            reasons = "; ".join(compatibility.reasons)
            if executor_error:
                reasons = f"{executor_error}; {reasons}".strip("; ")
            message = (
                f"Executor CT{vmid} is incompatible: required {EXECUTOR_VERSION}/"
                f"protocol {EXECUTOR_PROTOCOL_VERSION}/verify with configured hashes; "
                f"installed {installed}. {reasons}"
            )
            state["executor_contract_error"] = message
            self._save_state(vmid, state, publish=publish_state)
            raise ExecutorError(message)
        state["executor_contract_error"] = None
        self._save_state(vmid, state, publish=publish_state)
        return payload

    def retry_healthcheck(self, vmid: int) -> dict[str, Any]:
        cfg = self._resource(vmid)
        self._require_capability(vmid, "retry_healthcheck")
        lock = self._scan_locks[vmid]
        if not lock.acquire(blocking=False):
            raise ValueError("Another scan or manual operation is active for this resource")
        try:
            if self.db.get_active_job(vmid) is not None:
                raise ValueError("Another job is already active for this resource")
            latest = self.db.get_latest_job(vmid)
            if latest is None:
                raise ValueError("No job is available for retry")
            if latest.get("status") not in {"failed", "blocked", "interrupted"}:
                raise ValueError("Healthcheck retry is only allowed after a failed operation")
            job = self.db.create_followup_job(latest["id"], stage="healthcheck", progress=80)
            policy = StabilizationPolicy.from_config(cfg.get("stabilization"))
            emit = self._emitter(job)
            try:
                health = self.stabilizer.wait(
                    vmid=vmid,
                    phase="update",
                    timeout_seconds=policy.repair_timeout_seconds,
                    policy=policy,
                    emit=emit,
                    initial_grace=False,
                )
                state = self.get_state(vmid)
                state.update(health)
                state["health_status"] = health.get(
                    "health_status",
                    health.get("health", "healthy"),
                )
                state["last_refresh"] = utc_now()
                self._save_state(vmid, state)
                self._terminal(job, "success", "success", None)
            except ExecutorError as exc:
                state = self.get_state(vmid)
                if exc.data:
                    state.update(exc.data)
                    state["health_status"] = exc.data.get(
                        "health_status",
                        exc.data.get("health", "critical"),
                    )
                self._save_state(vmid, state)
                self._terminal(job, "failed", "manual_intervention", str(exc))
            return self.get_state(vmid)
        finally:
            lock.release()

    def queue_retry_healthcheck(
        self,
        vmid: int,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        lock = self._scan_locks[vmid]
        if not lock.acquire(blocking=False):
            raise ValueError("A scan or manual operation is active for this resource")
        try:
            return self._queue_retry_healthcheck_locked(vmid, request_id)
        finally:
            lock.release()

    def _queue_retry_healthcheck_locked(
        self,
        vmid: int,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        cfg = self._resource(vmid)
        self._require_capability(vmid, "retry_healthcheck")
        self._require_compatible_executor(vmid)
        resolved_request_id = request_id or uuid.uuid4().hex
        existing = self.db.get_job_by_request_id(vmid, resolved_request_id)
        if existing is not None:
            if existing.get("operation_type") != "retry_healthcheck":
                raise ValueError("request_id was already used for another operation")
            return existing
        latest = self.db.get_latest_job(vmid)
        if latest is None:
            raise ValueError("No job is available for retry")
        if latest.get("status") not in {"failed", "blocked", "interrupted"}:
            raise ValueError("Healthcheck retry is only allowed after a failed operation")
        job, _ = self.db.create_operation_job(
            vmid=vmid,
            container_name=str(cfg.get("name", f"ct-{vmid}")),
            operation_type="retry_healthcheck",
            request_id=resolved_request_id,
            plan_id=latest.get("plan_id"),
            snapshot_name=latest.get("snapshot_name"),
        )
        self._mark_job_queued(vmid, job)
        return job

    def manual_rollback(self, vmid: int) -> dict[str, Any]:
        cfg = self._resource(vmid)
        self._require_capability(vmid, "rollback")
        lock = self._scan_locks[vmid]
        if not lock.acquire(blocking=False):
            raise ValueError("Another scan or manual operation is active for this resource")
        try:
            if not bool(cfg.get("manual_rollback_allowed", False)):
                raise ValueError("Manual rollback is not allowed by resource policy")
            host_control = self._require_host_control("Manual rollback")
            if self.db.get_active_job(vmid) is not None:
                raise ValueError("Another job is already active for this resource")
            if self.db.find_active_plan(vmid) is not None:
                raise ValueError("Resolve the active update plan before rollback")
            source = self.db.get_latest_job(vmid)
            if (
                source is None
                or int(source.get("vmid") or 0) != vmid
                or str(source.get("operation_type") or "") != "update"
                or not source.get("snapshot_name")
            ):
                raise ValueError("No rollback snapshot is available")
            if source["status"] not in {"failed", "blocked", "interrupted"}:
                raise ValueError("Rollback is only allowed after a failed operation")
            snapshot_name = str(source["snapshot_name"])
            parsed = parse_owned_snapshot_name(snapshot_name, vmid=vmid)
            if parsed is None or parsed.get("kind") != "pre-update":
                raise ValueError(
                    "Recorded rollback snapshot is missing, foreign, or ineligible"
                )
            refreshed = self._refresh_snapshot_state(vmid)
            selected = next(
                (
                    item
                    for item in refreshed["snapshots"]
                    if str(item.get("name") or "") == snapshot_name
                ),
                None,
            )
            if (
                selected is None
                or selected.get("owned_by_hubinet_ops") is not True
                or selected.get("rollback_eligible") is not True
                or str(selected.get("source_job_id") or "") != str(source["id"])
                or int(selected.get("vmid") or 0) != vmid
                or str(selected.get("kind") or "") != "pre-update"
            ):
                raise ValueError(
                    "Recorded rollback snapshot is missing, foreign, or ineligible"
                )
            host_status = self._host_status(vmid)
            runtime = str(
                host_status.get("lxc_status")
                or host_status.get("runtime_status")
                or "unknown"
            )
            if runtime not in {"running", "stopped"}:
                raise ValueError(
                    f"Cannot establish PVE runtime before rollback: {runtime}"
                )
            job = self.db.create_manual_rollback_job(
                source["id"],
                expected_snapshot_identity=self._snapshot_identity(
                    vmid,
                    selected,
                    expected_name=snapshot_name,
                    expected_kind="pre-update",
                ),
            )
            self.db.update_job(job["id"], status="running", stage="rollback", progress=1)
            self._run_operation_job(job)
            return self.db.get_job(job["id"])
        finally:
            lock.release()

    def queue_lifecycle(
        self,
        vmid: int,
        action: str,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        lock = self._scan_locks[vmid]
        if not lock.acquire(blocking=False):
            raise ValueError("A scan or manual operation is active for this resource")
        try:
            return self._queue_lifecycle_locked(vmid, action, request_id)
        finally:
            lock.release()

    def _queue_lifecycle_locked(
        self,
        vmid: int,
        action: str,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        operation_type = {
            "start": "lifecycle_start",
            "shutdown": "lifecycle_shutdown",
            "reboot": "lifecycle_reboot",
            "force-stop": "lifecycle_force_stop",
        }.get(action)
        if operation_type is None:
            raise ValueError("Unsupported lifecycle action")
        capability = "force_stop" if action == "force-stop" else action
        cfg = self._resource(vmid)
        if cfg.get("resource_type") != "lxc":
            raise ValueError("Lifecycle is supported only for LXC resources")
        self._require_capability(vmid, capability)
        self._require_host_control("Lifecycle")
        if self.db.find_active_plan(vmid) is not None and action != "start":
            raise ValueError("Resolve the active update plan before lifecycle control")
        status = self._host_status(vmid)
        runtime = str(status.get("lxc_status") or status.get("runtime_status") or "unknown")
        if action == "start" and runtime != "stopped":
            raise ValueError(f"Start requires stopped runtime, got {runtime}")
        if action != "start" and runtime != "running":
            raise ValueError(f"{action} requires running runtime, got {runtime}")
        job, _ = self.db.create_operation_job(
            vmid=vmid,
            container_name=str(cfg.get("name", f"ct-{vmid}")),
            operation_type=operation_type,
            request_id=request_id or uuid.uuid4().hex,
        )
        self._mark_job_queued(vmid, job)
        return job

    def list_snapshots(self, vmid: int) -> dict[str, Any]:
        cfg = self._resource(vmid)
        if cfg.get("resource_type") not in {"lxc", "qemu"}:
            raise ValueError("Snapshots are not supported for this resource type")
        self._require_capability(vmid, "snapshot_list")
        return self._refresh_snapshot_state(vmid)

    def reconcile_snapshot_proofs(self) -> list[dict[str, Any]]:
        """Recover only proofs backed by an exact backend→hostd→PVE chain.

        The method performs read-only host operations.  It never creates,
        deletes, restores, retains, or otherwise mutates a physical snapshot.
        """
        host = self.host_control
        if host is None or not hasattr(host, "find_job_by_request_id"):
            return []
        reconciled: list[dict[str, Any]] = []
        for vmid, cfg in sorted(self.settings.resources.items()):
            if (
                not bool(cfg.get("enabled", False))
                or not self._capabilities(vmid).get("snapshot_list", False)
            ):
                continue
            try:
                snapshots = self._raw_snapshot_list(vmid)
            except (ExecutorError, HostControlError, ValueError) as exc:
                LOGGER.warning(
                    "Snapshot proof reconciliation list failed for %s: %s",
                    vmid,
                    exc,
                )
                continue
            for raw in snapshots:
                if not isinstance(raw, dict):
                    continue
                name = str(raw.get("name") or "")
                parsed = parse_owned_snapshot_name(name, vmid=vmid)
                host_job_id = str(raw.get("source_job_id") or "")
                pve_snaptime = raw.get("pve_snaptime")
                if (
                    raw.get("owned_by_hubinet_ops") is not True
                    or parsed is None
                    or parsed.get("kind") != "pre-update"
                    or not self._valid_host_source_job_id(host_job_id)
                    or isinstance(pve_snaptime, bool)
                    or not isinstance(pve_snaptime, int)
                    or pve_snaptime <= 0
                ):
                    continue
                candidates: list[tuple[dict[str, Any], dict[str, Any]]] = []
                for candidate in self.db.find_snapshot_jobs(vmid, name):
                    result = candidate.get("result")
                    contract = (
                        result.get("pre_update_snapshot_create")
                        if isinstance(result, dict)
                        else None
                    )
                    if (
                        candidate.get("operation_type") == "update"
                        and candidate.get("status")
                        in {"failed", "blocked", "interrupted"}
                        and isinstance(contract, dict)
                        and contract.get("version") == 1
                        and contract.get("request_id")
                        == f"pre-update-snapshot-{candidate['id']}"
                        and contract.get("snapshot_name") == name
                        and contract.get("phase")
                        in {"definitive_failed", "outcome_unknown"}
                        and contract.get("host_job_id") in {None, host_job_id}
                        and not self.db.has_snapshot_proof(
                            str(candidate["id"]),
                            vmid,
                            name,
                            host_job_id,
                            pve_snaptime,
                        )
                    ):
                        candidates.append((candidate, contract))
                if len(candidates) != 1:
                    continue
                candidate, contract = candidates[0]
                request_id = str(contract["request_id"])
                try:
                    remote = host.find_job_by_request_id(vmid, request_id)
                except (HostControlError, ValueError):
                    continue
                if not isinstance(remote, dict):
                    continue
                remote_vmid = remote.get("vmid")
                if (
                    remote.get("id") != host_job_id
                    or remote.get("request_id") != request_id
                    or isinstance(remote_vmid, bool)
                    or remote_vmid != vmid
                    or remote.get("operation_type") != "snapshot_create"
                    or remote.get("argument") != name
                    or remote.get("status") not in {"succeeded", "failed"}
                ):
                    continue
                if remote.get("status") == "succeeded":
                    remote_result = remote.get("result")
                    if (
                        not isinstance(remote_result, dict)
                        or remote_result.get("name") != name
                        or remote_result.get("kind") != "pre-update"
                        or remote_result.get("source_job_id") != host_job_id
                        or remote_result.get("pve_snaptime") != pve_snaptime
                    ):
                        continue
                try:
                    self.db.reconcile_pre_update_snapshot_proof(
                        str(candidate["id"]),
                        vmid,
                        name,
                        host_job_id,
                        pve_snaptime,
                        request_id,
                    )
                except (KeyError, ValueError):
                    continue
                existing_events = self.db.list_job_events(
                    str(candidate["id"]),
                    limit=200,
                )
                if not any(
                    event.get("event_type") == "snapshot_proof_reconciled"
                    for event in existing_events
                ):
                    self.db.insert_job_event(
                        job_id=str(candidate["id"]),
                        vmid=vmid,
                        level="warning",
                        stage="failed",
                        progress=100,
                        event_type="snapshot_proof_reconciled",
                        message=(
                            "Recovered snapshot proof from the exact durable host job; "
                            "the failed update outcome was preserved"
                        ),
                        details={
                            "snapshot_name": name,
                            "host_job_id": host_job_id,
                            "request_id": request_id,
                            "pve_snaptime": pve_snaptime,
                        },
                    )
                reconciled.append(
                    {
                        "vmid": vmid,
                        "snapshot_name": name,
                        "backend_job_id": str(candidate["id"]),
                        "host_job_id": host_job_id,
                        "status": "reconciled",
                    }
                )
        return reconciled

    def _refresh_snapshot_state(
        self,
        vmid: int,
        *,
        job: dict[str, Any] | None = None,
        required_name: str | None = None,
        required_kind: str | None = None,
        attempts: int = 3,
    ) -> dict[str, Any]:
        last_error: ExecutorError | HostControlError | ValueError | None = None
        snapshots: list[dict[str, Any]] = []
        for _attempt in range(max(1, min(int(attempts), 5))):
            try:
                snapshots = self._raw_snapshot_list(vmid)
                break
            except (ExecutorError, HostControlError, ValueError) as exc:
                last_error = exc
        else:
            warning = sanitize_text(
                f"Snapshot refresh failed: {last_error}",
                limit=2000,
            )
            state = self.get_state(vmid)
            state.update(
                {
                    "snapshot_state_stale": True,
                    "snapshot_refresh_required": True,
                    "snapshot_refresh_warning": warning,
                }
            )
            self._save_state(vmid, state)
            if job is not None:
                event = self.db.insert_job_event(
                    job_id=str(job["id"]),
                    vmid=vmid,
                    level="warning",
                    stage=str(self.db.get_job(str(job["id"])).get("stage") or "snapshot"),
                    progress=int(self.db.get_job(str(job["id"])).get("progress") or 0),
                    event_type="snapshot_refresh_failed",
                    message=warning,
                    details={"refresh_required": True, "attempts": max(1, min(int(attempts), 5))},
                )
                self.mqtt.publish_event(vmid, event)
            raise ValueError(warning) from last_error
        snapshots = self._managed_snapshot_model(
            vmid,
            [dict(item) for item in snapshots if isinstance(item, dict)],
        )
        snapshots.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        owned = [item for item in snapshots if item.get("owned_by_hubinet_ops") is True]
        unproven = [
            item
            for item in snapshots
            if item.get("ownership_status") == "host_owned_unproven"
        ]
        latest = owned[0] if owned else {}
        state = self.get_state(vmid)
        state.update(
            {
                "snapshot_count": len(owned),
                "snapshot_unproven_count": len(unproven),
                "latest_unproven_snapshot_name": (
                    unproven[0].get("name") if unproven else None
                ),
                "latest_snapshot_name": latest.get("name"),
                "latest_snapshot_at": latest.get("created_at"),
                "latest_snapshot_kind": latest.get("kind"),
                "snapshot_state_stale": False,
                "snapshot_refresh_required": False,
                "snapshot_refresh_warning": None,
                "snapshot_refreshed_at": self._utc_second_timestamp(),
                "managed_snapshots": owned,
                "unproven_snapshots": unproven,
            }
        )
        self._save_state(vmid, state)
        if required_name is not None:
            confirmed = next(
                (
                    item
                    for item in owned
                    if str(item.get("name") or "") == required_name
                ),
                None,
            )
            parsed = parse_owned_snapshot_name(required_name, vmid=vmid)
            if (
                confirmed is None
                or parsed is None
                or confirmed.get("owned_by_hubinet_ops") is not True
                or str(confirmed.get("kind") or parsed.get("kind") or "") != str(required_kind or "")
            ):
                warning = (
                    f"Created snapshot {required_name} could not be confirmed "
                    f"as an owned VMID {vmid} {required_kind or ''} snapshot"
                ).strip()
                state = self.get_state(vmid)
                state.update(
                    {
                        "snapshot_state_stale": True,
                        "snapshot_refresh_required": True,
                        "snapshot_refresh_warning": warning,
                    }
                )
                self._save_state(vmid, state)
                if job is not None:
                    event = self.db.insert_job_event(
                        job_id=str(job["id"]),
                        vmid=vmid,
                        level="error",
                        stage="snapshot",
                        progress=int(self.db.get_job(str(job["id"])).get("progress") or 0),
                        event_type="snapshot_confirmation_failed",
                        message=warning,
                        details={"snapshot_name": required_name, "kind": required_kind},
                    )
                    self.mqtt.publish_event(vmid, event)
                raise ValueError(warning)
        return {"snapshots": snapshots, "latest": latest or None}

    def _managed_snapshot_model(
        self,
        vmid: int,
        snapshots: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        jobs_by_snapshot = {
            str(item.get("name") or ""): self.db.find_snapshot_jobs(
                vmid,
                str(item.get("name") or ""),
            )
            for item in snapshots
            if str(item.get("name") or "")
        }
        jobs = [
            job
            for snapshot_jobs in jobs_by_snapshot.values()
            for job in snapshot_jobs
        ]
        plans = [
            plan for plan in self.db.list_plans(limit=500)
            if int(plan.get("vmid") or 0) == vmid
            and str(plan.get("status") or "") in {"waiting_approval", "approved"}
        ]
        active_job_names = {
            str(job["snapshot_name"]): job
            for job in jobs
            if str(job.get("status") or "") in {"queued", "running"}
        }
        rollback_source_names = self.db.rollback_source_snapshots(vmid)
        active_plan_names: dict[str, dict[str, Any]] = {}
        for plan in plans:
            payload = dict(plan.get("payload") or {})
            name = str(payload.get("snapshot_name") or "")
            if name:
                active_plan_names[name] = plan
        now = self._now().astimezone(UTC)
        modeled: list[dict[str, Any]] = []
        for raw in snapshots:
            item = dict(raw)
            name = str(item.get("name") or "")
            parsed = parse_owned_snapshot_name(name, vmid=vmid)
            snapshot_jobs = jobs_by_snapshot.get(name, [])
            host_source_job_id = str(item.get("source_job_id") or "") or None
            pve_snaptime = item.get("pve_snaptime")
            host_owned = (
                item.get("owned_by_hubinet_ops") is True
                and parsed is not None
                and isinstance(pve_snaptime, int)
                and not isinstance(pve_snaptime, bool)
                and pve_snaptime > 0
                and host_source_job_id is not None
                and self._valid_host_source_job_id(host_source_job_id)
            )
            durable_source: dict[str, Any] | None = None
            if host_owned:
                if parsed["kind"] == "manual" and host_source_job_id is not None:
                    durable_source = next(
                        (
                            candidate
                            for candidate in snapshot_jobs
                            if str(candidate.get("operation_type") or "")
                            in {"snapshot_create", "snapshot_create_ram"}
                            and str(
                                (
                                    candidate["result"].get("source_job_id")
                                    if isinstance(candidate.get("result"), dict)
                                    else None
                                )
                                or ""
                            )
                            == host_source_job_id
                            and isinstance(candidate.get("result"), dict)
                            and candidate["result"].get("pve_snaptime")
                            == pve_snaptime
                            and candidate["result"].get("name") == name
                            and candidate["result"].get("kind") == "manual"
                        ),
                        None,
                    )
                elif parsed["kind"] == "pre-update":
                    durable_source = next(
                        (
                            candidate
                            for candidate in snapshot_jobs
                            if str(candidate.get("operation_type") or "") == "update"
                            and self.db.has_snapshot_proof(
                                str(candidate["id"]),
                                vmid,
                                name,
                                host_source_job_id,
                                pve_snaptime,
                            )
                        ),
                        None,
                    )
            owned = durable_source is not None
            reasons: list[str] = []
            related_job = (
                active_job_names.get(name)
                or durable_source
                or (snapshot_jobs[0] if snapshot_jobs else None)
            )
            related_plan = active_plan_names.get(name)
            if name in active_job_names:
                reasons.append("active_job")
            if name in active_plan_names:
                reasons.append("active_plan")
            if name in rollback_source_names:
                reasons.append("manual_rollback_source")
            if not owned:
                reasons.append(
                    "foreign_snapshot"
                    if parsed is None
                    else "backend_proof_missing"
                    if host_owned
                    else "ownership_uncertain"
                )
            created = parse_utc_timestamp(item.get("created_at"))
            age_seconds = (
                max(0, int((now - created.astimezone(UTC)).total_seconds()))
                if created is not None
                else None
            )
            kind = (
                str(item.get("kind") or parsed.get("kind") or "")
                if parsed is not None
                else str(item.get("kind") or "unknown")
            )
            item.update(
                {
                    "physical_name": name,
                    "logical_type": kind,
                    "kind": kind,
                    "vmid": vmid,
                    "age_seconds": age_seconds,
                    "protected": bool(reasons),
                    "protection_reason": reasons[0] if reasons else None,
                    "protection_reasons": reasons,
                    "source_job_id": (
                        str(related_job["id"]) if related_job is not None else None
                    ),
                    "host_source_job_id": host_source_job_id,
                    "pve_snaptime": pve_snaptime,
                    "host_owned": host_owned,
                    "backend_proven": owned,
                    "source_plan_id": (
                        str(related_plan["id"])
                        if related_plan is not None
                        else str(related_job.get("plan_id") or "") or None
                        if related_job is not None
                        else None
                    ),
                    "owned_by_hubinet_ops": owned,
                    "rollback_eligible": (
                        owned and item.get("rollback_eligible") is True
                    ),
                    "delete_eligible": (
                        owned and item.get("delete_eligible") is True
                    ),
                    "ownership_status": (
                        "managed"
                        if owned
                        else "host_owned_unproven"
                        if host_owned
                        else "foreign"
                        if parsed is None
                        else "uncertain"
                    ),
                }
            )
            modeled.append(item)
        return modeled

    def _snapshot_retention_count(self, vmid: int) -> int:
        cfg = self._resource(vmid)
        capabilities = self._capabilities(vmid)
        snapshots_enabled = any(
            bool(capabilities.get(name, False))
            for name in ("snapshot_create", "snapshot_list", "snapshot_delete")
        ) or bool(cfg.get("pre_update_snapshot", False))
        value = cfg.get(
            "snapshot_retention_count",
            cfg.get("snapshot_retention", 3 if snapshots_enabled else 0),
        )
        return max(0, min(int(value), 100))

    def queue_snapshot_create(
        self,
        vmid: int,
        request_id: str | None = None,
        *,
        include_ram: bool = False,
    ) -> dict[str, Any]:
        lock = self._scan_locks[vmid]
        if not lock.acquire(blocking=False):
            raise ValueError("A scan or manual operation is active for this resource")
        try:
            return self._queue_snapshot_create_locked(
                vmid,
                request_id,
                include_ram=include_ram,
            )
        finally:
            lock.release()

    def _queue_snapshot_create_locked(
        self,
        vmid: int,
        request_id: str | None = None,
        *,
        include_ram: bool = False,
    ) -> dict[str, Any]:
        cfg = self._resource(vmid)
        resource_type = str(cfg.get("resource_type") or "")
        if resource_type not in {"lxc", "qemu"}:
            raise ValueError("Snapshots are not supported for this resource type")
        if not isinstance(include_ram, bool):
            raise ValueError("include_ram must be a boolean")
        if include_ram and resource_type != "qemu":
            raise ValueError("include_ram is supported only for QEMU snapshots")
        self._require_capability(vmid, "snapshot_create")
        self._require_host_control("Snapshot creation")
        stamp = self._now().astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
        name = _snapshot_name(vmid, "manual", stamp)
        job, _ = self.db.create_operation_job(
            vmid=vmid,
            container_name=str(cfg.get("name", f"ct-{vmid}")),
            operation_type=(
                "snapshot_create_ram"
                if include_ram
                else "snapshot_create"
            ),
            request_id=request_id or uuid.uuid4().hex,
            snapshot_name=name,
        )
        self._mark_job_queued(vmid, job)
        return job

    def queue_snapshot_action(
        self,
        vmid: int,
        action: str,
        name: str | None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        lock = self._scan_locks[vmid]
        if not lock.acquire(blocking=False):
            raise ValueError("A scan or manual operation is active for this resource")
        try:
            return self._queue_snapshot_action_locked(vmid, action, name, request_id)
        finally:
            lock.release()

    def queue_snapshot_prune(
        self,
        vmid: int,
        mode: str,
        request_id: str | None = None,
        *,
        confirmation: str | None = None,
    ) -> dict[str, Any]:
        if mode not in {"oldest", "all_unprotected"}:
            raise ValueError("Unsupported snapshot pruning mode")
        if mode == "all_unprotected" and confirmation != "DELETE_ALL_UNPROTECTED":
            raise ValueError("Exact confirmation DELETE_ALL_UNPROTECTED is required")
        cfg = self._resource(vmid)
        self._require_capability(vmid, "snapshot_delete")
        self._require_capability(vmid, "snapshot_list")
        self._require_host_control("Snapshot pruning")
        resolved_request_id = request_id or uuid.uuid4().hex
        existing = self.db.get_job_by_request_id(vmid, resolved_request_id)
        if existing is not None:
            result = existing.get("result")
            if (
                existing.get("operation_type") != "snapshot_prune"
                or existing.get("snapshot_name") != mode
                or (
                    isinstance(result, dict)
                    and result.get("mode") not in {None, mode}
                )
            ):
                raise ValueError("request_id was already used for another operation")
            return existing
        if self.db.active_job_count() > 0:
            raise ValueError("Another destructive maintenance job is active")
        listing = self._refresh_snapshot_state(vmid)
        precheck_state = {
            "mode": mode,
            "retention_target": None,
            "deleted_count": 0,
        }
        candidate = self._select_snapshot_prune_candidate(precheck_state, listing)
        if candidate is None:
            return {
                "status": "nothing_to_delete",
                "mode": mode,
                "deleted_count": 0,
            }
        candidate_name = str(candidate.get("name") or "")
        initial_identity = self._snapshot_identity(
            vmid,
            candidate,
            expected_name=candidate_name,
        )
        job, created = self.db.create_snapshot_prune_job(
            vmid=vmid,
            container_name=str(cfg.get("name", f"resource-{vmid}")),
            request_id=resolved_request_id,
            mode=mode,
            retention_target=None,
            source_job_id=None,
            initial_snapshot_identity=initial_identity,
        )
        if created:
            self._mark_job_queued(vmid, job)
        return job

    def _queue_snapshot_action_locked(
        self,
        vmid: int,
        action: str,
        name: str | None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        operation_type = {
            "rollback": "snapshot_rollback",
            "delete": "snapshot_delete",
        }.get(action)
        if operation_type is None:
            raise ValueError("Unsupported snapshot action")
        capability = "snapshot_rollback" if action == "rollback" else "snapshot_delete"
        cfg = self._resource(vmid)
        if cfg.get("resource_type") not in {"lxc", "qemu"}:
            raise ValueError("Snapshots are not supported for this resource type")
        self._require_capability(vmid, capability)
        self._require_host_control("Snapshot operation")
        if action == "rollback" and not bool(
            cfg.get("manual_snapshot_restore_allowed", False)
        ):
            raise ValueError("Explicit snapshot restore is not allowed by resource policy")
        listing = self.list_snapshots(vmid)
        snapshots = [item for item in listing["snapshots"] if item.get("owned_by_hubinet_ops")]
        if name in {None, "latest"}:
            eligible_key = "rollback_eligible" if action == "rollback" else "delete_eligible"
            selected = next((item for item in snapshots if item.get(eligible_key)), None)
        else:
            selected = next((item for item in snapshots if item.get("name") == name), None)
        if selected is None or parse_owned_snapshot_name(str(selected.get("name")), vmid=vmid) is None:
            raise ValueError("Hubinet Ops snapshot does not exist")
        if action == "rollback" and self.db.find_active_plan(vmid) is not None:
            raise ValueError("Resolve the active update plan before snapshot restore")
        if not bool(selected.get(f"{action}_eligible")):
            raise ValueError(f"Snapshot is not {action} eligible")
        if action == "delete" and bool(selected.get("protected")):
            raise ValueError(
                f"Snapshot is protected: {selected.get('protection_reason') or 'policy'}"
            )
        job, _ = self.db.create_operation_job(
            vmid=vmid,
            container_name=str(cfg.get("name", f"ct-{vmid}")),
            operation_type=operation_type,
            request_id=request_id or uuid.uuid4().hex,
            snapshot_name=str(selected["name"]),
            result={
                "expected_snapshot_identity": self._snapshot_identity(
                    vmid,
                    selected,
                    expected_name=str(selected["name"]),
                )
            },
            require_no_active_plan=action == "rollback",
        )
        self._mark_job_queued(vmid, job)
        return job

    def create_self_update_plan(self, vmid: int) -> dict[str, Any]:
        lock = self._scan_locks[vmid]
        if not lock.acquire(blocking=False):
            raise ValueError("A scan or manual operation is active for this resource")
        try:
            return self._create_self_update_plan_locked(vmid)
        finally:
            lock.release()

    def _create_self_update_plan_locked(self, vmid: int) -> dict[str, Any]:
        cfg = self._resource(vmid)
        self._require_capability(vmid, "self_update")
        if vmid != 110 or cfg.get("adapter") != "agent_self":
            raise ConflictError(
                "self_update_not_supported",
                "Self-update is supported only for CT110",
            )
        if self.host_control is None:
            raise ConflictError(
                "host_control_unavailable",
                "CT110 self-update requires independent PVE host control",
                required_action="Restore the independent PVE host control service.",
            )
        if self.db.get_active_job(vmid) is not None:
            raise ConflictError(
                "active_job_conflict",
                "Another job is already active for this resource",
                required_action="Wait for the active job to finish.",
            )
        release = self._read_self_update_release(vmid)
        if release["status"] in {"up_to_date", "no_release_published"}:
            state = self.get_state(vmid)
            state.update(
                {
                    "application_release_check_status": release["status"],
                    "application_current_version": release.get(
                        "current_version", VERSION
                    ),
                    "application_latest_version": release.get("latest_version"),
                    "application_last_check": self._utc_second_timestamp(),
                    "application_last_error": None,
                    "operation_status": "idle",
                }
            )
            self._save_state(vmid, state)
            return dict(release)
        if not self._capabilities(vmid)["approve"]:
            active = self.db.find_active_plan(vmid)
            if active is not None and self._plan_type(active) == "self_update":
                self.db.update_plan_status(str(active["id"]), "superseded")
            state = self.get_state(vmid)
            state.update(
                {
                    "active_plan_id": None,
                    "active_plan_status": None,
                    "operation_status": "idle",
                    "job_stage": "idle",
                    "job_progress": 0,
                    "application_release_check_status": "update_available",
                    "application_current_version": release.get("current_version", VERSION),
                    "application_latest_version": release["version"],
                    "application_release_tag": release.get("tag"),
                    "application_release_commit": release.get("commit_sha"),
                    "application_release_published_at": release.get("published_at"),
                    "application_last_check": self._utc_second_timestamp(),
                    "application_last_error": None,
                }
            )
            self._save_state(vmid, state)
            return dict(release, plan_created=False)

        fingerprint = str(release["fingerprint"])
        active = self.db.find_active_plan(vmid, fingerprint)
        if active is not None:
            if self._plan_type(active) != "self_update":
                raise ConflictError(
                    "active_plan_conflict",
                    "Resolve the active plan before CT110 self-update",
                    required_action="Resolve the active update plan.",
                )
            if active.get("status") != "waiting_approval":
                raise ConflictError(
                    "approved_plan_pending",
                    "Approved self-update plan is already pending execution",
                    required_action="Wait for the approved plan to finish.",
                )
            status = "existing_plan"
        else:
            other = self.db.find_active_plan(vmid)
            if other is not None:
                raise ConflictError(
                    "active_plan_conflict",
                    "Resolve the active plan before CT110 self-update",
                    required_action="Resolve the active update plan.",
                )
            payload = {
                "plan_type": "self_update",
                "version": str(release["version"]),
                "release_id": str(release["release_id"]),
                "fingerprint": fingerprint,
                "file_count": release.get("file_count"),
                "total_bytes": release.get("total_bytes", release.get("size")),
                "tag": release.get("tag"),
                "commit_sha": release.get("commit_sha"),
                "published_at": release.get("published_at"),
                "artifact_verification": release.get("artifact_verification"),
                "bundle_sha256": release.get("bundle_sha256"),
            }
            active = self.db.create_plan(
                vmid=vmid,
                container_name=str(cfg.get("name", "hubinet-ops")),
                fingerprint=fingerprint,
                risk="high",
                payload=payload,
                ttl_minutes=int(
                    self.settings.scheduler.get("approval_ttl_minutes", 1440)
                ),
            )
            status = "plan_created"
            self._notify_ha(
                self._notification(
                    "approval_required",
                    vmid,
                    release_id=payload["release_id"],
                    release_version=payload["version"],
                    risk="high",
                )
            )
        state = self.get_state(vmid)
        state.update(
            {
                "risk": "high",
                "active_plan_id": active["id"],
                "active_plan_status": active["status"],
                "operation_status": "waiting_approval",
                "job_stage": "idle",
                "job_progress": 0,
                "self_update_release_id": release["release_id"],
                "self_update_release_version": release["version"],
                "self_update_release_fingerprint": fingerprint,
                "application_release_check_status": "update_available",
                "application_current_version": release.get(
                    "current_version", VERSION
                ),
                "application_latest_version": release["version"],
                "application_release_tag": release.get("tag"),
                "application_release_commit": release.get("commit_sha"),
                "application_release_published_at": release.get("published_at"),
                "application_download_status": "not_started",
                "application_validation_status": release.get(
                    "artifact_verification", "not_downloaded"
                ),
                "application_last_check": self._utc_second_timestamp(),
                "application_last_error": None,
            }
        )
        self._save_state(vmid, state)
        return {
            "vmid": vmid,
            "status": status,
            "plan": active,
            "release": release,
        }

    def _read_self_update_release(self, vmid: int) -> dict[str, Any]:
        if self.host_control is None:
            raise ValueError("CT110 self-update requires independent PVE host control")
        release = self.host_control.check_application_release(vmid)
        status = str(release.get("status") or "")
        if status in {"up_to_date", "no_release_published"}:
            return dict(release)
        if status != "update_available":
            raise ConflictError(
                "application_release_status_invalid",
                "Application release check returned an invalid status",
            )
        release = {
            **release,
            "version": str(release.get("latest_version") or ""),
            "release_id": (
                f"hubinet-ops-{release.get('latest_version')}-"
                f"{str(release.get('fingerprint') or '')[:16]}"
            ),
        }
        required = ("version", "release_id", "fingerprint")
        if not all(isinstance(release.get(key), str) and release[key] for key in required):
            raise ConflictError(
                "staged_release_identity_invalid",
                "Staged self-update release identity is invalid",
                required_action="Restage a release with complete identity metadata.",
            )
        fingerprint = str(release["fingerprint"])
        if len(fingerprint) != 64 or any(
            character not in "0123456789abcdef" for character in fingerprint
        ):
            raise ConflictError(
                "staged_release_fingerprint_invalid",
                "Staged self-update release fingerprint is invalid",
                required_action="Restage and validate the release fingerprint.",
            )
        return dict(release)

    def _validate_self_update_plan(self, job: dict[str, Any]) -> dict[str, Any]:
        if not job.get("plan_id"):
            raise ValueError("Self-update job has no approved plan")
        plan = self.db.get_plan(str(job["plan_id"]))
        if plan.get("status") != "approved" or self._plan_type(plan) != "self_update":
            raise ValueError(
                f"Self-update plan status is {plan.get('status')}, not approved"
            )
        release = self._read_self_update_release(int(job["vmid"]))
        payload = dict(plan.get("payload") or {})
        if any(
            str(release.get(key) or "") != str(payload.get(key) or "")
            for key in ("fingerprint", "release_id", "version")
        ):
            self.db.update_plan_status(plan["id"], "superseded")
            raise ValueError(
                "Approved self-update release changed before rollout; no changes were made"
            )
        return release

    def _mark_job_queued(self, vmid: int, job: dict[str, Any]) -> None:
        state = self.get_state(vmid)
        state.update(
            {
                "active_job_id": job["id"],
                "operation_type": job["operation_type"],
                "operation_status": "running",
                "job_stage": "queued",
                "job_progress": 0,
                "last_operation_result": None,
                "last_error": None,
            }
        )
        self._save_state(vmid, state)

    def _require_host_control(self, operation: str) -> HostControlClient:
        if self.host_control is None:
            raise ValueError(
                f"{operation} requires independent PVE host control"
            )
        return self.host_control

    def _host_status(self, vmid: int) -> dict[str, Any]:
        try:
            if self.host_control is not None:
                return self.host_control.status(vmid)
            return _executor_data(self.executor.run("status", vmid, timeout=30))
        except (ExecutorError, HostControlError) as exc:
            raise ValueError(f"Cannot read current LXC state: {exc}") from exc

    def lifecycle_container(self, vmid: int, action: str) -> dict[str, Any]:
        if action not in {"start", "shutdown", "reboot"}:
            raise ValueError("Unsupported lifecycle action")
        cfg = self._resource(vmid)
        if cfg.get("resource_type", "lxc") != "lxc" or cfg.get("adapter", "apt") != "apt":
            raise ValueError("Lifecycle is not supported by this resource adapter")
        self._require_capability(vmid, action)
        lock = self._scan_locks[vmid]
        if not lock.acquire(blocking=False):
            raise ValueError("Another scan or lifecycle operation is active for this resource")
        started_at = self._now().isoformat()
        try:
            if self.db.get_active_job(vmid) is not None:
                raise ValueError("Another job is already active for this resource")
            state = self.get_state(vmid)
            if state.get("lifecycle_status") == "running":
                raise ValueError("Another lifecycle operation is already active")
            if state.get("update_status") == "scanning":
                raise ValueError("An update scan is already active")
            if self.db.find_active_plan(vmid) is not None:
                raise ValueError("An active update plan must be resolved before lifecycle control")

            try:
                status_result = self.executor.run("status", vmid, timeout=30)
                status_data = _executor_data(status_result)
                lxc_status = str(
                    status_data.get("lxc_status")
                    or status_data.get("runtime_status")
                    or status_data.get("status", "unknown")
                )
            except ExecutorError as exc:
                raise ValueError(f"Cannot read current LXC state: {exc}") from exc
            if action == "start" and lxc_status != "stopped":
                raise ValueError(f"Start requires a stopped container; current state is {lxc_status}")
            if action in {"shutdown", "reboot"} and lxc_status != "running":
                raise ValueError(
                    f"{action.capitalize()} requires a running container; current state is {lxc_status}"
                )

            stage = {
                "start": "starting",
                "shutdown": "shutting_down",
                "reboot": "rebooting",
            }[action]
            expected_lxc = "stopped" if action == "shutdown" else "running"
            state.update(
                {
                    "lifecycle_action": action,
                    "lifecycle_status": "running",
                    "lifecycle_started_at": started_at,
                    "lifecycle_finished_at": None,
                    "lifecycle_error": None,
                    "operation_status": "running",
                    "job_stage": stage,
                    "job_progress": 10,
                    "last_operation_result": None,
                    "active_job_id": None,
                    "expected_lxc_status": expected_lxc,
                    "intentional_shutdown": False,
                    "lifecycle_health_pending": action in {"start", "reboot"},
                }
            )
            self._save_state(vmid, state)
            try:
                self.executor.run(action, vmid, timeout=180)
                verified = self.executor.run("status", vmid, timeout=30)
                verified_data = _executor_data(verified)
                final_lxc = str(
                    verified_data.get("lxc_status")
                    or verified_data.get("runtime_status")
                    or verified_data.get("status", "unknown")
                )
                expected = "stopped" if action == "shutdown" else "running"
                if final_lxc != expected:
                    raise ExecutorError(
                        f"Lifecycle verification expected {expected}, got {final_lxc}"
                    )
            except ExecutorError as exc:
                state = self.get_state(vmid)
                state.update(
                    {
                        "lifecycle_status": "failed",
                        "lifecycle_finished_at": self._now().isoformat(),
                        "lifecycle_error": sanitize_text(exc, limit=2000),
                        "operation_status": "failed",
                        "job_stage": "failed",
                        "job_progress": 100,
                        "last_operation_result": "failed",
                        "last_error": sanitize_text(exc, limit=2000),
                        "expected_lxc_status": None,
                        "intentional_shutdown": False,
                        "lifecycle_health_pending": False,
                    }
                )
                self._save_state(vmid, state)
                self._notify_ha(
                    self._notification(
                        "lifecycle_failed",
                        vmid,
                        action=action,
                        error=str(exc),
                    )
                )
                raise ValueError(str(exc)) from exc

            state = self.get_state(vmid)
            terminal_at = self._now()
            health_pending = action in {"start", "reboot"}
            state.update(
                {
                    "lxc_status": final_lxc,
                    "health_status": "offline" if final_lxc == "stopped" else "unknown",
                    "lifecycle_status": "success",
                    "lifecycle_finished_at": terminal_at.isoformat(),
                    "lifecycle_error": None,
                    "operation_status": "success",
                    "job_stage": "completed",
                    "job_progress": 100,
                    "last_operation_result": "success",
                    "last_error": None,
                    "expected_lxc_status": expected_lxc,
                    "intentional_shutdown": action == "shutdown",
                    "lifecycle_health_pending": health_pending,
                    "last_terminal_event": f"lifecycle_{action}",
                    "last_terminal_at": terminal_at.isoformat(),
                    "recovery_notification_suppressed_until": (
                        (terminal_at + timedelta(seconds=180)).isoformat()
                        if health_pending
                        else state.get("recovery_notification_suppressed_until")
                    ),
                }
            )
            saved = self._save_state(vmid, state)
            self._notify_ha(
                self._notification(
                    "lifecycle_success",
                    vmid,
                    action=action,
                    lxc_status=final_lxc,
                    health_pending=health_pending,
                )
            )
            return saved
        finally:
            lock.release()

    def _observe_health(self, vmid: int, health: str) -> None:
        previous = self._observed_health.get(vmid)
        self._observed_health[vmid] = health
        if previous is None:
            return
        if health != "healthy":
            try:
                self._cancel_recovery_scan(vmid, "health_changed")
            except Exception as exc:
                LOGGER.exception("Failed to cancel recovery scan for resource %s", vmid)
                self._record_recovery_failure(vmid, exc, status="failed")
            return
        if previous in {"offline", "critical", "degraded"}:
            try:
                self._schedule_recovery_scan(vmid)
            except Exception as exc:
                LOGGER.exception("Failed to schedule recovery scan for resource %s", vmid)
                self._record_recovery_failure(vmid, exc, status="failed")

    def _recovery_settings(self, vmid: int) -> tuple[bool, int, int]:
        raw = self._resource(vmid).get("recovery_scan") or {}
        if not isinstance(raw, dict):
            raise TypeError("recovery_scan must be an object")
        delay = max(1, _safe_int(raw.get("delay_seconds"), 90))
        cooldown_default = max(900, delay)
        cooldown = max(delay, _safe_int(raw.get("cooldown_seconds"), cooldown_default))
        return bool(raw.get("enabled", False)), delay, cooldown

    def _schedule_recovery_scan(self, vmid: int) -> None:
        enabled, delay, _ = self._recovery_settings(vmid)
        if not enabled or not self._monitoring(vmid)["update_scan"]:
            return
        with self._recovery_lock:
            self._recovery_due[vmid] = self._monotonic() + delay
        state = self.get_state(vmid)
        state.update(
            {
                "recovery_scan_status": "scheduled",
                "recovery_scan_due_at": (self._now() + timedelta(seconds=delay)).isoformat(),
                "last_recovery_scan_result": None,
            }
        )
        self._save_state(vmid, state)
        self._recovery_wakeup.set()

    def _cancel_recovery_scan(self, vmid: int, reason: str) -> None:
        with self._recovery_lock:
            existed = self._recovery_due.pop(vmid, None) is not None
        if not existed:
            return
        state = self.get_state(vmid)
        state.update(
            {
                "recovery_scan_status": "cancelled",
                "recovery_scan_due_at": None,
                "last_recovery_scan_result": reason,
            }
        )
        self._save_state(vmid, state)
        self._recovery_wakeup.set()

    def _recovery_loop(self) -> None:
        while not self._stop.is_set():
            try:
                now_monotonic = self._monotonic()
                if not math.isfinite(now_monotonic):
                    raise ValueError("monotonic clock must be finite")
                self._run_due_recovery_scans(now_monotonic)
                with self._recovery_lock:
                    deadlines = [
                        float(value)
                        for value in self._recovery_due.values()
                        if isinstance(value, (int, float))
                        and not isinstance(value, bool)
                        and math.isfinite(float(value))
                    ]
                next_due = min(deadlines, default=None)
                timeout = (
                    60.0
                    if next_due is None
                    else max(0.0, next_due - self._monotonic())
                )
            except Exception:
                LOGGER.exception("Unhandled recovery-loop iteration failure")
                timeout = 1.0
            try:
                self._recovery_wakeup.wait(min(timeout, 60.0))
                self._recovery_wakeup.clear()
            except Exception:
                LOGGER.exception("Recovery-loop wait failed")

    def _run_due_recovery_scans(self, now_monotonic: float) -> None:
        due: list[int] = []
        invalid: list[tuple[int, Exception]] = []
        with self._recovery_lock:
            for raw_vmid, deadline in list(self._recovery_due.items()):
                try:
                    vmid = int(raw_vmid)
                    if isinstance(deadline, bool):
                        raise TypeError("recovery deadline must be numeric")
                    parsed_deadline = float(deadline)
                    if not math.isfinite(parsed_deadline):
                        raise ValueError("recovery deadline must be finite")
                    if parsed_deadline <= now_monotonic:
                        due.append(vmid)
                except (TypeError, ValueError, OverflowError) as exc:
                    invalid.append((_safe_int(raw_vmid, 0), exc))
                    self._recovery_due.pop(raw_vmid, None)
            for vmid in due:
                self._recovery_due.pop(vmid, None)
        for vmid, exc in invalid:
            LOGGER.warning("Discarding malformed recovery deadline for resource %s: %s", vmid, exc)
            if vmid in self.settings.resources:
                self._record_recovery_failure(vmid, exc, status="failed")
        for vmid in sorted(due):
            if self._stop.is_set():
                return
            try:
                self._run_recovery_scan(vmid)
            except Exception as exc:
                LOGGER.exception("Unhandled recovery scan failure for resource %s", vmid)
                self._record_recovery_failure(vmid, exc, status="failed")

    def _record_recovery_failure(
        self,
        vmid: int,
        error: object,
        *,
        status: str,
    ) -> None:
        try:
            state = self.get_state(vmid)
            state.update(
                {
                    "recovery_scan_status": status,
                    "recovery_scan_due_at": None,
                    "last_recovery_scan_result": sanitize_text(error, limit=500),
                }
            )
            self._save_state(vmid, state)
        except Exception:
            LOGGER.exception("Failed to persist recovery failure for resource %s", vmid)

    def _run_recovery_scan(self, vmid: int) -> None:
        enabled, _, cooldown = self._recovery_settings(vmid)
        state = self.get_state(vmid)
        reason: str | None = None
        if not enabled:
            reason = "disabled"
        elif not self._monitoring(vmid)["update_scan"]:
            reason = "monitoring_scan_disabled"
        elif state.get("health_status") != "healthy" or state.get("lxc_status") != "running":
            reason = "container_not_healthy_running"
        elif self.db.get_active_job(vmid) is not None:
            reason = "job_active"
        elif state.get("update_status") == "scanning":
            reason = "scan_active"
        elif state.get("lifecycle_status") == "running":
            reason = "lifecycle_active"
        elif self.db.find_active_plan(vmid) is not None:
            reason = "plan_active"
        last_scan = state.get("last_recovery_scan")
        if reason is None and last_scan:
            parsed_last_scan = parse_utc_timestamp(last_scan)
            parsed_now = parse_utc_timestamp(self._now())
            if parsed_last_scan is None or parsed_now is None:
                reason = "invalid_previous_recovery_timestamp"
            elif (parsed_now - parsed_last_scan).total_seconds() < cooldown:
                reason = "cooldown_active"
        if reason is not None:
            state.update(
                {
                    "recovery_scan_status": "blocked",
                    "recovery_scan_due_at": None,
                    "last_recovery_scan_result": reason,
                }
            )
            self._save_state(vmid, state)
            return

        state.update(
            {
                "recovery_scan_status": "running",
                "recovery_scan_due_at": None,
            }
        )
        self._save_state(vmid, state)
        try:
            result = self.scan_container(vmid, operator=False, source="recovery")
            result_status = str(result.get("status", "unknown"))
            final_status = "completed" if result_status not in {"error", "skipped"} else "failed"
        except Exception as exc:
            LOGGER.warning(
                "Recovery scan failed for resource %s: %s",
                vmid,
                sanitize_text(exc, limit=500),
            )
            result_status = sanitize_text(exc, limit=500)
            final_status = "failed"
        state = self.get_state(vmid)
        state.update(
            {
                "recovery_scan_status": final_status,
                "last_recovery_scan": self._now().isoformat(),
                "last_recovery_scan_result": result_status,
                "recovery_scan_due_at": None,
            }
        )
        self._save_state(vmid, state)

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            job = self.db.next_queued_job()
            if not job:
                self._stop.wait(1)
                continue
            try:
                self._run_job(job)
            except Exception:
                LOGGER.exception("Unhandled worker failure for job %s", job.get("id"))
                self._handle_unhandled_worker_failure(job)

    def _handle_unhandled_worker_failure(self, job: dict[str, Any]) -> None:
        try:
            current = self.db.get_job(str(job["id"]))
            if str(current.get("status")) in TERMINAL_JOB_STATUSES:
                LOGGER.warning(
                    "Worker exception occurred after terminal job %s (%s); "
                    "terminal state is preserved",
                    job.get("id"),
                    current.get("status"),
                )
                return
            result = current.get("result")
            automatic = (
                result.get("automatic_rollback") if isinstance(result, dict) else None
            )
            pre_update_create = (
                result.get("pre_update_snapshot_create")
                if isinstance(result, dict)
                else None
            )
            if isinstance(automatic, dict) and automatic.get("phase") in {
                "submitting", "remote_observed", "outcome_unknown",
                "remote_succeeded", "stabilizing", "stabilized",
            }:
                self._hold_automatic_rollback_unknown(
                    current,
                    dict(automatic),
                    "Unhandled worker error after automatic rollback submit boundary",
                )
                return
            if isinstance(pre_update_create, dict) and pre_update_create.get("phase") in {
                "submitting", "remote_observed", "outcome_unknown",
                "remote_succeeded", "confirming",
            }:
                self._hold_embedded_host_unknown(
                    current,
                    "pre_update_snapshot_create",
                    "Unhandled worker error after pre-update snapshot submit boundary",
                )
                return
            if (
                str(current.get("operation_type") or "") in HOST_CONTROL_OPERATION_TYPES
                and str(current.get("stage") or "") in {
                    "host_submitting", "host_remote_observed",
                    "host_outcome_unknown", "host_remote_succeeded",
                }
            ):
                if str(current.get("stage")) == "host_remote_succeeded":
                    LOGGER.warning(
                        "Worker error after durable remote success for job %s; "
                        "startup will finalize without another POST",
                        current["id"],
                    )
                    return
                self._hold_host_operation_unknown(
                    current,
                    "Unhandled worker error after host submit boundary",
                )
                return
            self._terminal(
                current,
                "failed",
                "manual_intervention",
                "Unhandled worker error",
            )
        except Exception:
            LOGGER.exception("Failed to persist terminal state for job %s", job.get("id"))

    def _reconcile_startup_jobs(self) -> None:
        for job in self.db.active_jobs():
            operation_type = str(job.get("operation_type") or "update")
            result = job.get("result")
            pre_update_create = (
                result.get("pre_update_snapshot_create")
                if isinstance(result, dict)
                else None
            )
            if (
                operation_type == "update"
                and isinstance(pre_update_create, dict)
                and pre_update_create.get("phase") != "completed"
            ):
                # The embedded create state machine decides whether prepared may
                # submit or submitting+ may only perform read-only reconciliation.
                if pre_update_create.get("phase") in {
                    "prepared", "submitting", "remote_observed", "outcome_unknown",
                    "remote_succeeded", "confirming",
                }:
                    self._run_job(job)
                else:
                    self._hold_embedded_host_unknown(
                        job,
                        "pre_update_snapshot_create",
                        "Backend restarted after host snapshot success but before "
                        "durable physical confirmation",
                    )
                continue
            if (
                operation_type == "update"
                and str(job.get("stage") or "")
                in {"rollback", "rollback_wait", "rollback_healthcheck"}
            ):
                self._reattach_automatic_rollback(job)
                continue
            if operation_type == "snapshot_prune":
                self._reconcile_snapshot_prune(job)
                continue
            if (
                self.host_control is not None
                and operation_type in HOST_CONTROL_OPERATION_TYPES
            ):
                initial_host_stages = {
                    "starting", "shutting_down", "rebooting", "force_stopping",
                    "snapshot_creating", "snapshot_rollback", "snapshot_deleting",
                    "self_updating",
                }
                if (
                    str(job.get("stage") or "") == "queued"
                    or (
                        str(job.get("stage") or "") in initial_host_stages
                        and int(job.get("progress") or 0) <= 5
                    )
                ):
                    self._reattach_host_control_job(
                        job, allow_submit_if_not_found=True
                    )
                else:
                    self._reattach_host_control_job(job)
                continue
            succeeded = False
            result: dict[str, Any] | None = None
            try:
                if operation_type in {
                    "lifecycle_start", "lifecycle_shutdown", "lifecycle_force_stop"
                }:
                    status = self._host_status(int(job["vmid"]))
                    actual = str(status.get("lxc_status") or status.get("runtime_status"))
                    expected = "running" if operation_type == "lifecycle_start" else "stopped"
                    succeeded = actual == expected
                    result = {"runtime_status": actual, "reconciled": True}
                elif operation_type in {
                    "snapshot_create",
                    "snapshot_create_ram",
                    "snapshot_delete",
                }:
                    listing = self.list_snapshots(int(job["vmid"]))
                    exists = any(
                        item.get("name") == job.get("snapshot_name")
                        for item in listing["snapshots"]
                    )
                    succeeded = (
                        exists
                        if operation_type in {"snapshot_create", "snapshot_create_ram"}
                        else not exists
                    )
                    result = {"snapshot_exists": exists, "reconciled": True}
            except Exception as exc:
                LOGGER.warning("Startup reconciliation failed for job %s: %s", job["id"], exc)
            if succeeded:
                self.db.update_job(job["id"], result=result or {})
                self._terminal(job, "success", "success", None)
            else:
                self._terminal(
                    job,
                    "interrupted",
                    "failed",
                    "Agent restarted during the operation; it was not replayed",
                )

    def _reconcile_snapshot_prune(self, job: dict[str, Any]) -> None:
        emit = self._emitter(job)
        try:
            result = self._execute_snapshot_prune(job, emit, reconciling=True)
            self._finalize_operation_success(
                self.db.get_job(str(job["id"])),
                result,
                enforce_snapshot_retention=False,
            )
        except SnapshotPruneOutcomeUnknown as exc:
            LOGGER.warning(
                "Snapshot prune job %s remains active pending manual reconciliation: %s",
                job["id"],
                exc,
            )
        except (ExecutorError, HostControlError, ValueError) as exc:
            self._record_snapshot_prune_failure(job, exc)
            self._finalize_operation_failure(
                self.db.get_job(str(job["id"])),
                exc,
            )

    def _consume_offline_recovery_events(self) -> None:
        if self.host_control is None:
            return
        try:
            events = self.host_control.list_recovery_events()
        except HostControlError as exc:
            LOGGER.error(
                "Read-only offline recovery event lookup blocks startup: %s",
                exc,
            )
            raise
        for event in events:
            if str(event.get("status") or "") not in {
                "succeeded",
                "failed",
                "interrupted",
            }:
                continue
            recovery_id = str(event.get("recovery_id") or "")
            try:
                persisted, created = self.db.apply_recovery_event(event)
                if created:
                    LOGGER.warning(
                        "Applied offline recovery event recovery_id=%s vmid=%s "
                        "operation=%s status=%s",
                        recovery_id,
                        persisted["vmid"],
                        persisted["operation_type"],
                        persisted["status"],
                    )
                self.host_control.acknowledge_recovery_event(recovery_id)
            except HostControlError as exc:
                LOGGER.warning(
                    "Offline recovery event remains unacknowledged recovery_id=%s: %s",
                    recovery_id,
                    exc,
                )
            except ValueError:
                LOGGER.exception(
                    "Invalid offline recovery event blocks startup recovery_id=%s",
                    recovery_id,
                )
                raise

    def _reattach_host_control_job(
        self,
        job: dict[str, Any],
        *,
        allow_submit_if_not_found: bool = False,
    ) -> None:
        operation_type = str(job["operation_type"])
        snapshot_name = (
            str(job["snapshot_name"])
            if operation_type.startswith("snapshot_") and job.get("snapshot_name")
            else None
        )
        release_fingerprint: str | None = None
        system_update_fingerprint: str | None = None
        try:
            if str(job.get("stage") or "") == "host_remote_succeeded":
                result = job.get("result")
                self._finalize_operation_success(
                    job,
                    dict(result) if isinstance(result, dict) else {},
                    enforce_snapshot_retention=operation_type
                    in {"snapshot_create", "snapshot_create_ram"},
                )
                return
            if operation_type == "self_update":
                release = self._approved_self_update_release(job)
                release_fingerprint = str(release["fingerprint"])
            elif operation_type == "ct110_system_update":
                plan = self._approved_ct110_system_plan(job)
                system_update_fingerprint = str(plan["fingerprint"])
            identity = (
                self._expected_snapshot_identity_from_job(job)
                if operation_type in {"snapshot_rollback", "snapshot_delete"}
                else None
            )
            wait_kwargs: dict[str, Any] = {
                "snapshot_name": snapshot_name,
                "snapshot_kind": (str(identity["kind"]) if identity else None),
                "expected_source_job_id": (
                    str(identity["host_source_job_id"]) if identity else None
                ),
                "expected_pve_snaptime": (
                    int(identity["pve_snaptime"]) if identity else None
                ),
                "release_fingerprint": release_fingerprint,
                "on_observed": lambda remote: self.db.update_job(
                    str(job["id"]), stage="host_remote_observed"
                ),
            }
            if system_update_fingerprint is not None:
                wait_kwargs["system_update_fingerprint"] = system_update_fingerprint
            result = self.host_control.wait_existing_job(
                operation_type,
                int(job["vmid"]),
                str(job["request_id"]),
                **wait_kwargs,
            )
            self.db.update_job(
                str(job["id"]), result=result, stage="host_remote_succeeded"
            )
            self._finalize_operation_success(
                job,
                result,
                enforce_snapshot_retention=operation_type
                in {"snapshot_create", "snapshot_create_ram"},
            )
        except (HostControlError, ValueError) as exc:
            LOGGER.warning(
                "Read-only host job reattachment failed for local job %s: %s",
                job["id"],
                exc,
            )
            if (
                allow_submit_if_not_found
                and isinstance(exc, HostControlError)
                and exc.status == "not_found"
            ):
                self._run_operation_job(job)
                return
            outcome = classify_host_job_error(
                exc, submit_started=not allow_submit_if_not_found
            )
            if outcome is HostJobOutcome.DEFINITIVE_FAILURE:
                self._finalize_operation_failure(job, exc)
                return
            if isinstance(exc, HostControlError) and exc.result:
                self.db.update_job(str(job["id"]), result=exc.result)
            self._hold_host_operation_unknown(job, str(exc))

    def _run_job(self, job: dict[str, Any]) -> None:
        if str(job.get("operation_type") or "update") != "update":
            self._run_operation_job(job)
            return
        vmid = int(job["vmid"])
        cfg = self._resource(vmid)
        policy = StabilizationPolicy.from_config(cfg.get("stabilization"))
        auto_rollback = bool(cfg.get("automatic_rollback", False))
        pre_update_snapshot = bool(cfg.get("pre_update_snapshot", False))
        snapshot = str(job.get("snapshot_name") or "") or _snapshot_name(
            vmid,
            "pre-update",
            self._now().astimezone(UTC).strftime("%Y%m%dT%H%M%SZ"),
        )
        emit = self._emitter(job)
        emit(
            stage="preflight",
            progress=5,
            event_type="job_started",
            message="Update job started",
        )
        try:
            executor_contract = self._require_compatible_executor(vmid)
            if (
                auto_rollback
                and executor_contract.get("profile_validation_status")
                == "insufficient_health_contract"
            ):
                auto_rollback = False
                emit(
                    stage="preflight",
                    progress=6,
                    level="warning",
                    event_type="automatic_rollback_disabled",
                    message="Automatic rollback disabled: profile health contract is insufficient",
                )
            preflight = self._execute("preflight", vmid, 700, emit)
            self._validate_approved_plan(job, preflight)
            preflight_updates = dict(_executor_data(preflight).get("updates") or {})
            emit(
                stage="preflight",
                progress=15,
                event_type="preflight_passed",
                message="Preflight passed and approved package plan is unchanged",
            )
            emit(
                stage="preflight",
                progress=16,
                event_type="package_lists_refreshed",
                message="Package lists refreshed and update plan revalidated",
                details={
                    "pending_count": preflight_updates.get("pending_count")
                },
            )
            if pre_update_snapshot:
                self.db.update_job(job["id"], snapshot_name=snapshot)
                emit(
                    stage="snapshot",
                    progress=20,
                    event_type="snapshot_started",
                    message="Creating rollback snapshot" if auto_rollback else "Creating pre-update safety snapshot",
                )
                contract = self._pre_update_create_contract(job, required=False)
                if contract is not None and contract.get("phase") == "completed":
                    snapshot_identity = dict(contract["snapshot_identity"])
                else:
                    snapshot_identity = self._continue_pre_update_snapshot_create(
                        job, snapshot
                    )
                    contract = self._pre_update_create_contract(job)
                    contract["phase"] = "confirming"
                    contract["last_error"] = None
                    self.db.persist_pre_update_create_contract(
                        str(job["id"]), contract
                    )
                try:
                    self._confirm_physical_pre_update_snapshot(
                        vmid,
                        snapshot,
                        str(snapshot_identity["host_source_job_id"]),
                        int(snapshot_identity["pve_snaptime"]),
                    )
                except (ExecutorError, HostControlError, ValueError) as exc:
                    warning = sanitize_text(
                        f"Created snapshot {snapshot} could not be physically confirmed: {exc}",
                        limit=2000,
                    )
                    state = self.get_state(vmid)
                    state.update(
                        {
                            "snapshot_state_stale": True,
                            "snapshot_refresh_required": True,
                            "snapshot_refresh_warning": warning,
                        }
                    )
                    self._save_state(vmid, state)
                    event = self.db.insert_job_event(
                        job_id=str(job["id"]),
                        vmid=vmid,
                        level="error",
                        stage="snapshot",
                        progress=int(
                            self.db.get_job(str(job["id"])).get("progress") or 0
                        ),
                        event_type="snapshot_confirmation_failed",
                        message=warning,
                        details={"snapshot_name": snapshot, "kind": "pre-update"},
                    )
                    self.mqtt.publish_event(vmid, event)
                    raise ExecutorError(warning) from exc
                try:
                    self.db.record_pre_update_snapshot_proof(
                        str(job["id"]),
                        vmid,
                        snapshot,
                        str(snapshot_identity["host_source_job_id"]),
                        int(snapshot_identity["pve_snaptime"]),
                    )
                except Exception as exc:
                    raise ExecutorError(
                        f"Failed to persist pre-update snapshot proof: {exc}"
                    ) from exc
                try:
                    self._refresh_snapshot_state(
                        vmid,
                        job=job,
                        required_name=snapshot,
                        required_kind="pre-update",
                    )
                except ValueError as exc:
                    raise ExecutorError(str(exc)) from exc
                emit(
                    stage="snapshot",
                    progress=24,
                    event_type="snapshot_mutation_succeeded",
                    message="Pre-update snapshot mutation completed",
                    details={"snapshot_name": snapshot},
                )
                emit(
                    stage="snapshot",
                    progress=25,
                    event_type="snapshot_created",
                    message="Rollback snapshot created" if auto_rollback else "Pre-update safety snapshot created",
                )
                contract = self._pre_update_create_contract(job)
                if contract.get("phase") != "completed":
                    contract["phase"] = "completed"
                    contract["last_error"] = None
                    self.db.persist_pre_update_create_contract(
                        str(job["id"]), contract
                    )
            emit(
                stage="updating",
                progress=30,
                event_type="update_started",
                message="Package update started",
            )
            update = self._execute("update", vmid, 4500, emit)
            emit(
                stage="waiting_services",
                progress=75,
                event_type="waiting_for_services",
                message="Waiting for systemd and Docker to stabilize",
            )
            try:
                health = self.stabilizer.wait(
                    vmid=vmid,
                    phase="update",
                    timeout_seconds=policy.post_update_timeout_seconds,
                    policy=policy,
                    emit=emit,
                )
            except ExecutorError as health_error:
                health = self._repair_or_raise(job, health_error, policy, emit)
            emit(
                stage="healthcheck",
                progress=96,
                event_type="healthcheck_passed",
                message="Post-update service stabilization passed",
            )

            emit(
                stage="verifying",
                progress=97,
                event_type="verification_started",
                message="Final package and service verification started",
            )
            state = self.get_state(vmid)
            state.update(
                {
                    "verification_status": "running",
                    "verification_error": None,
                }
            )
            self._save_state(vmid, state)
            try:
                verification = self._execute("verify", vmid, 700, emit)
            except ExecutorError as exc:
                failed_verification = dict(exc.data or {})
                state = self.get_state(vmid)
                state.update(failed_verification)
                state.update(
                    {
                        "verification_status": "failed",
                        "last_verification": self._now().isoformat(),
                        "verification_error": sanitize_text(exc, limit=2000),
                    }
                )
                self._save_state(vmid, state)
                raise
            verification_data = _executor_data(verification)
            updates = dict(verification_data.get("updates") or {})
            updates["packages"] = list(updates.get("packages") or [])[:200]
            final_apt_scan_ok = bool(
                verification_data.get("final_apt_scan_ok", True)
            )
            pending = (
                max(0, int(updates.get("pending_count", 0) or 0))
                if final_apt_scan_ok
                else None
            )
            if not final_apt_scan_ok:
                updates["pending_count"] = None
            verification_warning = (
                sanitize_text(
                    verification_data.get("verification_warning"),
                    limit=2000,
                )
                if verification_data.get("verification_warning")
                else None
            )
            packages_updated = max(
                0,
                int(_executor_data(update).get("package_total", 0) or 0),
            )
            raw_reboot_required = verification_data.get("reboot_required")
            reboot_required = (
                raw_reboot_required
                if isinstance(raw_reboot_required, bool)
                else None
            )
            verification_status = (
                "warning"
                if reboot_required is True or bool(pending) or not final_apt_scan_ok
                else "passed"
            )
            docker = dict(verification_data.get("docker") or health.get("docker") or {})
            docker_healthy = max(0, int(docker.get("required_healthy", 0) or 0))
            docker_total = max(0, int(docker.get("required_total", 0) or 0))
            emit(
                stage="verifying",
                progress=99,
                level="warning" if verification_status == "warning" else "info",
                event_type=f"verification_{verification_status}",
                message="Final verification completed",
                details={
                    "packages_remaining_count": pending,
                    "reboot_required": reboot_required,
                    "docker_required_healthy": docker_healthy,
                    "docker_required_total": docker_total,
                },
            )
            result = {
                "preflight": preflight,
                "update": update,
                "healthcheck": health,
                "verification": verification,
            }
            self.db.update_job(job["id"], result=result)
            self.db.update_plan_status(job["plan_id"], "completed")
            state = self.get_state(vmid)
            state.update(health)
            state.update(
                {
                    "health_status": health.get(
                        "health_status",
                        health.get("health", "healthy"),
                    ),
                    "updates": updates,
                    "pending_updates": pending,
                    "update_status": (
                        "unknown"
                        if not final_apt_scan_ok
                        else "update_available"
                        if pending
                        else "up_to_date"
                    ),
                    "active_plan_id": None,
                    "active_plan_status": "completed",
                    "active_job_id": job["id"],
                    "last_update": utc_now(),
                    "last_error": None,
                    "snapshot_name": snapshot if pre_update_snapshot else None,
                    "verification_status": verification_status,
                    "last_verification": self._now().isoformat(),
                    "apt_check_ok": bool(verification_data.get("apt_check_ok", False)),
                    "dpkg_audit_ok": bool(verification_data.get("dpkg_audit_ok", False)),
                    "reboot_required": reboot_required,
                    "packages_updated_count": packages_updated,
                    "packages_remaining_count": pending,
                    "docker_required_healthy": docker_healthy,
                    "docker_required_total": docker_total,
                    "verification_error": verification_warning,
                }
            )
            self._save_state(vmid, state)
            self._terminal_with_snapshot_retention(
                self.db.get_job(job["id"]),
                job_status="success",
                result="success",
                error=None,
            )
            if final_apt_scan_ok and pending is not None and pending > 0:
                self._create_followup_plan(vmid, cfg, updates)
            duration = self._best_effort_duration(job)
            self._notify_ha(
                self._notification(
                    "job_success",
                    vmid,
                    packages_updated_count=packages_updated,
                    packages_remaining_count=pending,
                    reboot_required=reboot_required,
                    apt_check_ok=bool(verification_data.get("apt_check_ok", False)),
                    dpkg_audit_ok=bool(verification_data.get("dpkg_audit_ok", False)),
                    docker_required_healthy=docker_healthy,
                    docker_required_total=docker_total,
                    verification_status=verification_status,
                    verification_warning=verification_warning,
                    duration_seconds=duration,
                )
            )
        except HostOperationOutcomeUnknown as exc:
            LOGGER.warning("Update job %s remains active: %s", job["id"], exc)
            return
        except ExecutorError as exc:
            current_job = self.db.get_job(job["id"])
            if str(current_job.get("status")) in TERMINAL_JOB_STATUSES:
                LOGGER.warning(
                    "Executor-style exception occurred after terminal job %s (%s); "
                    "terminal state is preserved",
                    job.get("id"),
                    current_job.get("status"),
                )
                return
            failed_stage = current_job["stage"]
            LOGGER.error("Job %s failed at %s: %s", job["id"], failed_stage, exc)
            if failed_stage in {"preflight", "snapshot"}:
                self.db.update_plan_status(job["plan_id"], "blocked")
                self._terminal(job, "blocked", "failed", str(exc))
                self._notify_ha(
                    self._notification("job_blocked", vmid, error=str(exc))
                )
            elif auto_rollback:
                self._rollback(job, str(exc))
            else:
                self.db.update_plan_status(job["plan_id"], "failed")
                self._terminal(job, "failed", "manual_intervention", str(exc))
                self._notify_ha(
                    self._notification(
                        "manual_intervention_required",
                        vmid,
                        error=str(exc),
                    )
                )

    def _run_operation_job(self, job: dict[str, Any]) -> None:
        vmid = int(job["vmid"])
        operation_type = str(job["operation_type"])
        emit = self._emitter(job)
        stage = {
            "lifecycle_start": "starting",
            "lifecycle_shutdown": "shutting_down",
            "lifecycle_reboot": "rebooting",
            "lifecycle_force_stop": "force_stopping",
            "snapshot_create": "snapshot_creating",
            "snapshot_create_ram": "snapshot_creating",
            "snapshot_rollback": "snapshot_rollback",
            "snapshot_delete": "snapshot_deleting",
            "snapshot_prune": "snapshot_pruning",
            "retry_healthcheck": "healthcheck",
            "self_update": "self_updating",
            "ct110_system_update": "system_updating",
        }.get(operation_type, "executing")
        emit(
            stage=stage,
            progress=5,
            event_type="operation_started",
            message=f"Operation {operation_type} started",
        )
        try:
            if operation_type == "retry_healthcheck":
                result = self._execute_retry_healthcheck(job, emit)
            elif operation_type == "snapshot_prune":
                result = self._execute_snapshot_prune(job, emit)
            else:
                self.db.update_job(str(job["id"]), stage="host_submitting")
                result = self._execute_host_operation(job)
                self.db.update_job(
                    str(job["id"]), result=result, stage="host_remote_succeeded"
                )
            self._finalize_operation_success(job, result)
        except SnapshotPruneOutcomeUnknown as exc:
            LOGGER.warning(
                "Snapshot prune job %s remains active because its child outcome "
                "is unknown: %s",
                job["id"],
                exc,
            )
        except (ExecutorError, HostControlError, ValueError) as exc:
            if operation_type == "snapshot_prune":
                self._record_snapshot_prune_failure(job, exc)
            submit_started = str(
                self.db.get_job(str(job["id"])).get("stage") or ""
            ) in {
                "host_submitting", "host_remote_observed",
                "host_outcome_unknown", "host_remote_succeeded",
            }
            if (
                isinstance(exc, HostControlError)
                and classify_host_job_error(exc, submit_started=submit_started)
                is HostJobOutcome.OUTCOME_UNKNOWN
            ):
                if exc.result:
                    self.db.update_job(str(job["id"]), result=exc.result)
                self._hold_host_operation_unknown(job, str(exc))
            else:
                self._finalize_operation_failure(job, exc)

    def _hold_host_operation_unknown(
        self,
        job: dict[str, Any],
        error: str,
    ) -> None:
        current = self.db.update_job(
            str(job["id"]),
            stage="host_outcome_unknown",
            error=sanitize_text(error, limit=2000),
        )
        events = self.db.list_job_events(str(job["id"]), limit=200)
        if not any(event.get("event_type") == "host_operation_outcome_unknown" for event in events):
            event = self.db.insert_job_event(
                job_id=str(job["id"]),
                vmid=int(job["vmid"]),
                level="warning",
                stage="host_reconciliation",
                progress=int(current.get("progress") or 5),
                event_type="host_operation_outcome_unknown",
                message=(
                    "Host operation outcome is unknown; read-only reconciliation "
                    "is required and destructive operations remain blocked"
                ),
                details={"request_id": job.get("request_id"), "reconciliation_required": True},
            )
            self.mqtt.publish_event(int(job["vmid"]), event)
        state = self.get_state(int(job["vmid"]))
        state.update(
            {
                "active_job_id": str(job["id"]),
                "operation_status": "reconciliation_required",
                "last_error": sanitize_text(error, limit=2000),
            }
        )
        self._save_state(int(job["vmid"]), state)

    def _finalize_operation_success(
        self,
        job: dict[str, Any],
        result: dict[str, Any],
        *,
        enforce_snapshot_retention: bool = True,
    ) -> None:
        vmid = int(job["vmid"])
        operation_type = str(job["operation_type"])
        if operation_type == "ct110_system_update":
            result = self._validate_ct110_system_result(job, result)
        elif operation_type == "self_update":
            result = self._validate_self_update_result(job, result)
        self.db.update_job(job["id"], result=result)
        state = self.get_state(vmid)
        snapshot_refreshed = False
        if operation_type.startswith("lifecycle_"):
            runtime = str(
                result.get("lxc_status")
                or result.get("runtime_status")
                or "unknown"
            )
            state.update(
                {
                    "lxc_status": runtime,
                    "runtime_status": runtime,
                    "lifecycle_action": operation_type.removeprefix("lifecycle_"),
                    "lifecycle_status": "success",
                    "lifecycle_finished_at": self._utc_second_timestamp(),
                    "lifecycle_error": None,
                }
            )
        if operation_type.startswith("snapshot_"):
            state["snapshot_operation_status"] = "success"
            if operation_type == "snapshot_rollback":
                state.update(
                    {
                        "verification_status": "unknown",
                        "last_verification": None,
                        "apt_check_ok": None,
                        "dpkg_audit_ok": None,
                        "packages_remaining_count": None,
                        "pending_updates": None,
                        "update_status": "unknown",
                        "updates": {"pending_count": None, "packages": []},
                    }
                )
            self._save_state(vmid, state)
            try:
                self._refresh_snapshot_state(
                    vmid,
                    job=job,
                    required_name=(
                        str(job.get("snapshot_name") or "")
                        if operation_type
                        in {"snapshot_create", "snapshot_create_ram"}
                        else None
                    ),
                    required_kind=(
                        "manual"
                        if operation_type
                        in {"snapshot_create", "snapshot_create_ram"}
                        else None
                    ),
                )
                snapshot_refreshed = True
            except (ExecutorError, HostControlError, ValueError) as exc:
                LOGGER.warning(
                    "Read-only snapshot refresh failed after completed job %s: %s",
                    job["id"],
                    exc,
                )
            if (
                operation_type == "snapshot_rollback"
                and str(self._resource(vmid).get("adapter") or "apt") == "apt"
            ):
                try:
                    self._require_compatible_executor(vmid)
                except ExecutorError as exc:
                    # A restored snapshot may legitimately predate the managed
                    # executor. Hostd already completed the rollback; record the
                    # drift without rewriting the successful destructive outcome.
                    LOGGER.warning(
                        "Executor drift detected after successful snapshot rollback "
                        "for CT%s: %s",
                        vmid,
                        exc,
                    )
        elif operation_type == "ct110_system_update":
            verification = dict(result["verification"])
            updates = dict(verification["updates"])
            state.update(
                {
                    "system_update_status": (
                        "update_available"
                        if int(updates["pending_count"]) > 0
                        else "up_to_date"
                    ),
                    "system_updates": updates,
                    "system_pending_updates": int(updates["pending_count"]),
                    "system_security_updates": sum(
                        item.get("security") is True
                        for item in updates.get("packages", [])
                        if isinstance(item, dict)
                    ),
                    "system_package_names": ", ".join(
                        str(item.get("name") or "")
                        for item in updates.get("packages", [])
                        if isinstance(item, dict) and item.get("name")
                    )[:255] or None,
                    "system_active_plan_id": None,
                    "system_active_plan_status": "completed",
                    "system_last_update": self._utc_second_timestamp(),
                    "system_last_verification": self._utc_second_timestamp(),
                    "system_apt_check_ok": True,
                    "system_dpkg_audit_ok": True,
                    "system_service_active": True,
                    "system_health_endpoint_ok": True,
                    "system_reboot_required": verification["reboot_required"],
                    "system_last_error": None,
                    "snapshot_name": result["snapshot_proof"]["snapshot_name"],
                }
            )
            if job.get("plan_id"):
                self.db.update_plan_status(str(job["plan_id"]), "completed")
            self._save_state(vmid, state)
        elif operation_type == "self_update":
            state.update(
                {
                    "application_release_check_status": "up_to_date",
                    "application_current_version": result["version"],
                    "application_latest_version": result["version"],
                    "application_release_tag": result["tag"],
                    "application_release_commit": result["commit_sha"],
                    "application_download_status": "downloaded",
                    "application_validation_status": "verified",
                    "application_deployment_status": "success",
                    "application_last_deployment": self._utc_second_timestamp(),
                    "application_last_result": "success",
                    "application_last_error": None,
                    "active_plan_status": "completed",
                }
            )
            if job.get("plan_id"):
                self.db.update_plan_status(str(job["plan_id"]), "completed")
            self._save_state(vmid, state)
        elif operation_type == "retry_healthcheck":
            state.update(result)
            state["health_status"] = result.get(
                "health_status", result.get("health", "healthy")
            )
            state["last_refresh"] = self._utc_second_timestamp()
            self._save_state(vmid, state)
        else:
            self._save_state(vmid, state)
        if (
            operation_type in {"snapshot_create", "snapshot_create_ram"}
            and enforce_snapshot_retention
            and snapshot_refreshed
        ):
            self._terminal_with_snapshot_retention(
                self.db.get_job(str(job["id"])),
                job_status="success",
                result="success",
                error=None,
            )
        else:
            self._terminal(job, "success", "success", None)

    def _finalize_operation_failure(
        self,
        job: dict[str, Any],
        error: ExecutorError | HostControlError | ValueError,
        *,
        job_status: str = "failed",
        terminal_result: str = "failed",
    ) -> None:
        vmid = int(job["vmid"])
        operation_type = str(job["operation_type"])
        if isinstance(error, HostControlError) and error.result:
            self.db.update_job(job["id"], result=error.result)
        state = self.get_state(vmid)
        if operation_type == "retry_healthcheck" and isinstance(error, ExecutorError):
            if error.data:
                state.update(error.data)
                state["health_status"] = error.data.get(
                    "health_status", error.data.get("health", "critical")
                )
        if operation_type.startswith("lifecycle_"):
            state.update(
                {
                    "lifecycle_status": (
                        "unknown" if job_status == "interrupted" else "failed"
                    ),
                    "lifecycle_finished_at": self._utc_second_timestamp(),
                    "lifecycle_error": sanitize_text(error, limit=2000),
                }
            )
        if operation_type.startswith("snapshot_"):
            state["snapshot_operation_status"] = (
                "unknown" if job_status == "interrupted" else "failed"
            )
        if operation_type == "ct110_system_update":
            state.update(
                {
                    "system_update_status": (
                        "outcome_unknown"
                        if job_status == "interrupted"
                        else "manual_intervention"
                    ),
                    "system_active_plan_status": (
                        "interrupted" if job_status == "interrupted" else "failed"
                    ),
                    "system_last_error": sanitize_text(error, limit=2000),
                }
            )
            if job.get("plan_id"):
                current_plan = self.db.get_plan(str(job["plan_id"]))
                if current_plan.get("status") == "approved":
                    self.db.update_plan_status(
                        str(job["plan_id"]),
                        "interrupted" if job_status == "interrupted" else "failed",
                    )
        if operation_type == "self_update":
            state.update(
                {
                    "application_deployment_status": (
                        "outcome_unknown"
                        if job_status == "interrupted"
                        else "failed"
                    ),
                    "application_last_result": terminal_result,
                    "application_last_error": sanitize_text(error, limit=2000),
                    "active_plan_status": (
                        "interrupted" if job_status == "interrupted" else "failed"
                    ),
                }
            )
            if job.get("plan_id"):
                current_plan = self.db.get_plan(str(job["plan_id"]))
                if current_plan.get("status") == "approved":
                    self.db.update_plan_status(
                        str(job["plan_id"]),
                        "interrupted" if job_status == "interrupted" else "failed",
                    )
        self._save_state(vmid, state)
        self._terminal(job, job_status, terminal_result, str(error))

    def _validate_self_update_result(
        self,
        job: dict[str, Any],
        raw: Any,
    ) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise ValueError("Application rollout result must be an object")
        plan = self._approved_self_update_release(job)
        required = {
            "version": str(plan.get("version") or ""),
            "release_id": str(plan.get("release_id") or ""),
            "fingerprint": str(plan.get("fingerprint") or ""),
            "tag": str(plan.get("tag") or ""),
            "commit_sha": str(plan.get("commit_sha") or ""),
        }
        if any(str(raw.get(key) or "") != value for key, value in required.items()):
            raise ValueError("Application rollout result identity mismatch")
        exit_code = raw.get("exit_code")
        if isinstance(exit_code, bool) or exit_code != 0:
            raise ValueError("Application rollout did not report a successful exit code")
        if raw.get("artifact_verification") != "verified":
            raise ValueError("Application rollout artifact was not verified")
        validated = dict(raw)
        for key, value in required.items():
            validated.setdefault(key, value)
        return validated

    def _validate_ct110_system_result(
        self,
        job: dict[str, Any],
        raw: Any,
    ) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise ValueError("CT110 system update result must be an object")
        plan = self._approved_ct110_system_plan(job)
        if raw.get("plan_fingerprint") != plan["fingerprint"]:
            raise ValueError("CT110 system update result fingerprint mismatch")
        proof = raw.get("snapshot_proof")
        if not isinstance(proof, dict):
            raise ValueError("CT110 system update has no physical snapshot proof")
        name = str(proof.get("snapshot_name") or "")
        parsed = parse_owned_snapshot_name(name, vmid=110)
        source = str(proof.get("host_source_job_id") or "")
        snaptime = proof.get("pve_snaptime")
        if (
            proof.get("version") != 3
            or proof.get("vmid") != 110
            or parsed is None
            or parsed.get("kind") != "pre-update"
            or proof.get("kind") != "pre-update"
            or len(source) != 32
            or any(character not in "0123456789abcdef" for character in source)
            or isinstance(snaptime, bool)
            or not isinstance(snaptime, int)
            or snaptime <= 0
            or proof.get("physically_confirmed") is not True
        ):
            raise ValueError("CT110 system update snapshot proof is invalid")
        verification = raw.get("verification")
        if not isinstance(verification, dict):
            raise ValueError("CT110 system update verification is missing")
        for key in (
            "apt_check_ok",
            "dpkg_audit_ok",
            "service_active",
            "health_endpoint_ok",
        ):
            if verification.get(key) is not True:
                raise ValueError(f"CT110 system update verification failed: {key}")
        reboot = verification.get("reboot_required")
        if reboot not in {True, False}:
            raise ValueError("CT110 system reboot-required result is invalid")
        updates = self._validate_ct110_system_scan(
            {
                **dict(verification.get("updates") or {}),
                "scanned_at": self._utc_second_timestamp(),
                "security_updates_count": sum(
                    item.get("security") is True
                    for item in dict(verification.get("updates") or {}).get(
                        "packages", []
                    )
                    if isinstance(item, dict)
                ),
                "reboot_required": reboot,
            }
        )
        normalized = dict(raw)
        normalized["snapshot_proof"] = dict(proof)
        normalized["verification"] = {
            **verification,
            "updates": updates,
            "reboot_required": reboot,
        }
        return normalized

    def _execute_retry_healthcheck(
        self,
        job: dict[str, Any],
        emit: Callable[..., None],
    ) -> dict[str, Any]:
        cfg = self._resource(int(job["vmid"]))
        policy = StabilizationPolicy.from_config(cfg.get("stabilization"))
        return self.stabilizer.wait(
            vmid=int(job["vmid"]),
            phase="update",
            timeout_seconds=policy.repair_timeout_seconds,
            policy=policy,
            emit=emit,
            initial_grace=False,
        )

    def _execute_snapshot_prune(
        self,
        job: dict[str, Any],
        emit: Callable[..., None],
        *,
        reconciling: bool = False,
    ) -> dict[str, Any]:
        vmid = int(job["vmid"])
        state = self._load_snapshot_prune_state(job)
        mode = str(state["mode"])
        while str(state.get("phase")) != "completed":
            current = state.get("current")
            if isinstance(current, dict):
                try:
                    self._resume_snapshot_prune_child(
                        job,
                        state,
                        reconciling=reconciling,
                    )
                except HostControlError as exc:
                    if not self._is_definitive_snapshot_prune_failure(exc):
                        self._hold_snapshot_prune_unknown(job, state, exc)
                    raise
                reconciling = False
                state = self._load_snapshot_prune_state(
                    self.db.get_job(str(job["id"]))
                )
                continue

            state["phase"] = "selecting"
            self._persist_snapshot_prune_state(job, state)
            listing = self._refresh_snapshot_state(vmid, job=job)
            candidate = self._select_snapshot_prune_candidate(state, listing)
            if candidate is None:
                state["phase"] = "refreshing"
                self._persist_snapshot_prune_state(job, state)
                self._refresh_snapshot_state(vmid, job=job)
                state["phase"] = "completed"
                self._persist_snapshot_prune_state(job, state)
                break
            name = str(candidate["name"])
            state["current"] = {
                "snapshot_name": name,
                "expected_snapshot_identity": self._snapshot_identity(
                    vmid,
                    candidate,
                    expected_name=name,
                ),
                "request_id": self._snapshot_prune_child_request_id(job, name),
                "phase": "prepared",
            }
            state["phase"] = "child_prepared"
            self._persist_snapshot_prune_state(job, state)
            reconciling = False

        deleted = [str(name) for name in state.get("deleted", [])]
        deleted_count = int(state["deleted_count"])
        emit(
            stage="snapshot_pruning",
            progress=95,
            event_type="snapshot_pruning_completed",
            message="Managed snapshot pruning completed",
            details={
                "deleted": deleted,
                "deleted_count": deleted_count,
                "deleted_history_truncated": bool(
                    state["deleted_history_truncated"]
                ),
                "mode": mode,
            },
        )
        return dict(state)

    def _load_snapshot_prune_state(
        self,
        job: dict[str, Any],
    ) -> dict[str, Any]:
        raw = job.get("result")
        if not isinstance(raw, dict):
            raise SnapshotPruneOutcomeUnknown(
                "Snapshot prune job has no persisted durable contract"
            )
        state = dict(raw)
        if state.get("prune_version") in {1, 2}:
            legacy_deleted = state.get("deleted")
            if not isinstance(legacy_deleted, list):
                raise SnapshotPruneOutcomeUnknown(
                    "Legacy snapshot prune job has invalid deletion history"
                )
            if state.get("current") is not None:
                raise SnapshotPruneOutcomeUnknown(
                    "Legacy active snapshot prune has no durable physical identity"
                )
            state["prune_version"] = SNAPSHOT_PRUNE_STATE_VERSION
            state["deleted_count"] = len(legacy_deleted)
            state["deleted"] = legacy_deleted[
                -SNAPSHOT_PRUNE_DELETED_HISTORY_LIMIT:
            ]
            state["deleted_history_truncated"] = (
                len(legacy_deleted) > len(state["deleted"])
            )
            self._persist_snapshot_prune_state(job, state)
        elif state.get("prune_version") != SNAPSHOT_PRUNE_STATE_VERSION:
            raise SnapshotPruneOutcomeUnknown(
                "Snapshot prune job has an unsupported durable state version"
            )
        mode = str(state.get("mode") or "")
        if mode not in {"all_unprotected", "oldest", "retention"}:
            raise ValueError("Snapshot pruning job has an invalid persisted mode")
        if str(job.get("snapshot_name") or "") != mode:
            raise ValueError("Snapshot pruning mode changed after persistence")
        target = state.get("retention_target")
        if mode == "retention":
            if not isinstance(target, int) or isinstance(target, bool) or not 0 <= target <= 100:
                raise ValueError("Snapshot pruning retention target is invalid")
            if not state.get("source_job_id"):
                raise ValueError("Snapshot pruning source job is invalid")
        elif target is not None:
            raise ValueError("Manual snapshot pruning has an unexpected retention target")
        elif state.get("source_job_id") is not None:
            raise ValueError("Manual snapshot pruning has an unexpected source job")
        deleted = state.get("deleted")
        if (
            not isinstance(deleted, list)
            or len(deleted) > SNAPSHOT_PRUNE_DELETED_HISTORY_LIMIT
        ):
            raise ValueError("Snapshot pruning deleted list is invalid")
        for name in deleted:
            if parse_owned_snapshot_name(str(name), vmid=int(job["vmid"])) is None:
                raise ValueError("Snapshot pruning deleted list contains an invalid snapshot")
        deleted_count = state.get("deleted_count")
        if (
            isinstance(deleted_count, bool)
            or not isinstance(deleted_count, int)
            or deleted_count < len(deleted)
        ):
            raise ValueError("Snapshot pruning deleted count is invalid")
        history_truncated = state.get("deleted_history_truncated")
        if (
            not isinstance(history_truncated, bool)
            or history_truncated != (deleted_count > len(deleted))
        ):
            raise ValueError("Snapshot pruning deletion history metadata is invalid")
        if str(state.get("phase") or "") not in {
            "selecting",
            "child_prepared",
            "child_submitted",
            "child_remote_succeeded",
            "unknown",
            "refreshing",
            "completed",
        }:
            raise ValueError("Snapshot pruning phase is invalid")
        current = state.get("current")
        if current is not None:
            if not isinstance(current, dict):
                raise ValueError("Snapshot pruning current child is invalid")
            name = str(current.get("snapshot_name") or "")
            request_id = str(current.get("request_id") or "")
            if (
                parse_owned_snapshot_name(name, vmid=int(job["vmid"])) is None
                or request_id != self._snapshot_prune_child_request_id(job, name)
                or str(current.get("phase") or "")
                not in {"prepared", "submitted", "remote_succeeded", "unknown"}
            ):
                raise ValueError("Snapshot pruning current child contract is invalid")
            identity = current.get("expected_snapshot_identity")
            if not isinstance(identity, dict):
                raise SnapshotPruneOutcomeUnknown(
                    "Snapshot pruning child has no durable expected identity"
                )
            self._expected_snapshot_identity_from_job(
                {
                    **job,
                    "snapshot_name": name,
                    "result": {"expected_snapshot_identity": identity},
                }
            )
        return state

    def _persist_snapshot_prune_state(
        self,
        job: dict[str, Any],
        state: dict[str, Any],
    ) -> None:
        self.db.update_job(str(job["id"]), result=state)

    @staticmethod
    def _snapshot_prune_child_request_id(
        job: dict[str, Any],
        snapshot_name: str,
    ) -> str:
        digest = hashlib.sha256(snapshot_name.encode("utf-8")).hexdigest()[:20]
        return f"prune-{str(job['id'])[:32]}-{digest}"

    def _select_snapshot_prune_candidate(
        self,
        state: dict[str, Any],
        listing: dict[str, Any],
    ) -> dict[str, Any] | None:
        managed = [
            item
            for item in listing["snapshots"]
            if item.get("owned_by_hubinet_ops") is True
        ]
        mode = str(state["mode"])
        if mode == "retention":
            target = int(state["retention_target"])
            pool = managed[target:]
        elif mode == "oldest":
            if int(state["deleted_count"]) > 0:
                return None
            pool = managed
        else:
            pool = managed
        return next(
            (
                item
                for item in reversed(pool)
                if item.get("owned_by_hubinet_ops") is True
                and item.get("protected") is not True
                and item.get("delete_eligible") is True
            ),
            None,
        )

    def _resume_snapshot_prune_child(
        self,
        job: dict[str, Any],
        state: dict[str, Any],
        *,
        reconciling: bool,
    ) -> None:
        vmid = int(job["vmid"])
        current = dict(state["current"])
        name = str(current["snapshot_name"])
        request_id = str(current["request_id"])
        identity = self._expected_snapshot_identity_from_job(
            {
                **job,
                "snapshot_name": name,
                "result": {
                    "expected_snapshot_identity": current.get(
                        "expected_snapshot_identity"
                    )
                },
            }
        )
        host_control = self._require_host_control("Snapshot pruning")
        child_phase = str(current["phase"])
        remote_succeeded = child_phase == "remote_succeeded"

        if not remote_succeeded and child_phase in {"submitted", "unknown"}:
            try:
                host_control.wait_existing_job(
                    "snapshot_delete",
                    vmid,
                    request_id,
                    snapshot_name=name,
                    snapshot_kind=str(identity["kind"]),
                    expected_source_job_id=str(identity["host_source_job_id"]),
                    expected_pve_snaptime=int(identity["pve_snaptime"]),
                )
                remote_succeeded = True
            except HostControlError as exc:
                # Once submitted was persisted, even not_found cannot prove that
                # the original DELETE never reached hostd. Reconciliation is
                # read-only and must never issue a duplicate POST/DELETE.
                raise
        elif not remote_succeeded:
            listing = self._refresh_snapshot_state(vmid, job=job)
            target = next(
                (
                    item
                    for item in listing["snapshots"]
                    if str(item.get("name") or "") == name
                ),
                None,
            )
            if target is None:
                # The exact prechecked target disappeared before this worker
                # crossed the host submission boundary.  Complete as an
                # explicit no-op; never select or delete a replacement.
                state["current"] = None
                state["phase"] = "completed"
                state["status"] = "target_disappeared"
                self._persist_snapshot_prune_state(job, state)
                return
            else:
                self._validate_snapshot_prune_target(vmid, name, target)
                current["phase"] = "submitted"
                state["current"] = current
                state["phase"] = "child_submitted"
                self._persist_snapshot_prune_state(job, state)
                host_control.execute(
                    "snapshot_delete",
                    vmid,
                    request_id,
                    snapshot_name=name,
                    snapshot_kind=str(identity["kind"]),
                    expected_source_job_id=str(identity["host_source_job_id"]),
                    expected_pve_snaptime=int(identity["pve_snaptime"]),
                )
                remote_succeeded = True

        if remote_succeeded:
            current["phase"] = "remote_succeeded"
            state["current"] = current
            state["phase"] = "child_remote_succeeded"
            self._persist_snapshot_prune_state(job, state)
            try:
                listing = self._refresh_snapshot_state(vmid, job=job)
            except (ExecutorError, HostControlError, ValueError) as refresh_error:
                self._hold_snapshot_prune_unknown(job, state, refresh_error)
            if any(
                str(item.get("name") or "") == name
                for item in listing["snapshots"]
            ):
                self._hold_snapshot_prune_unknown(
                    job,
                    state,
                    HostControlError(
                        "Snapshot delete reported success but the snapshot is still present",
                        status="contract_mismatch",
                    ),
                )
            deleted = [str(item) for item in state.get("deleted", [])]
            deleted.append(name)
            state["deleted_count"] = int(state["deleted_count"]) + 1
            state["deleted"] = deleted[-SNAPSHOT_PRUNE_DELETED_HISTORY_LIMIT:]
            state["deleted_history_truncated"] = (
                int(state["deleted_count"]) > len(state["deleted"])
            )
            state["current"] = None
            state["phase"] = "selecting"
            self._persist_snapshot_prune_state(job, state)

    @staticmethod
    def _is_definitive_snapshot_prune_failure(error: HostControlError) -> bool:
        return (
            classify_host_job_error(error, submit_started=True)
            is HostJobOutcome.DEFINITIVE_FAILURE
        )

    @staticmethod
    def _validate_snapshot_prune_target(
        vmid: int,
        name: str,
        target: dict[str, Any],
    ) -> None:
        if (
            parse_owned_snapshot_name(name, vmid=vmid) is None
            or int(target.get("vmid") or 0) != vmid
            or target.get("owned_by_hubinet_ops") is not True
            or target.get("protected") is True
            or target.get("delete_eligible") is not True
        ):
            raise ValueError("Snapshot is no longer eligible for managed pruning")

    def _hold_snapshot_prune_unknown(
        self,
        job: dict[str, Any],
        state: dict[str, Any],
        error: ExecutorError | HostControlError | ValueError,
    ) -> None:
        current = state.get("current")
        if isinstance(current, dict):
            current = dict(current)
            current["phase"] = "unknown"
            state["current"] = current
        state["phase"] = "unknown"
        state["reconciliation_error"] = sanitize_text(error, limit=1000)
        self._persist_snapshot_prune_state(job, state)
        current_job = self.db.get_job(str(job["id"]))
        event = self.db.insert_job_event(
            job_id=str(job["id"]),
            vmid=int(job["vmid"]),
            level="warning",
            stage="snapshot_pruning",
            progress=int(current_job.get("progress") or 0),
            event_type="snapshot_pruning_outcome_unknown",
            message=(
                "Snapshot deletion outcome is unknown; the durable prune job "
                "remains active and blocks destructive operations"
            ),
            details={
                "snapshot_name": (
                    str(current.get("snapshot_name")) if isinstance(current, dict) else None
                ),
                "manual_intervention_required": True,
            },
        )
        self.mqtt.publish_event(int(job["vmid"]), event)
        raise SnapshotPruneOutcomeUnknown(str(error)) from error

    def _record_snapshot_prune_failure(
        self,
        job: dict[str, Any],
        error: ExecutorError | HostControlError | ValueError,
    ) -> None:
        current = self.db.get_job(str(job["id"]))
        prune_state = (
            dict(current["result"])
            if isinstance(current.get("result"), dict)
            else {}
        )
        event = self.db.insert_job_event(
            job_id=str(job["id"]),
            vmid=int(job["vmid"]),
            level="warning",
            stage="snapshot_pruning",
            progress=int(current.get("progress") or 0),
            event_type="snapshot_pruning_failed",
            message=sanitize_text(
                f"Managed snapshot pruning failed: {error}",
                limit=1000,
            ),
            details={
                "source_job_id": prune_state.get("source_job_id"),
                "source_result_preserved": True,
            },
        )
        self.mqtt.publish_event(int(job["vmid"]), event)

    def _execute_host_operation(
        self,
        job: dict[str, Any],
        *,
        on_observed: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        operation_type = str(job["operation_type"])
        vmid = int(job["vmid"])
        snapshot_name = job.get("snapshot_name")
        host_control = self._require_host_control("Host operation")
        if operation_type == "self_update":
            release = self._validate_self_update_plan(job)
            kwargs: dict[str, Any] = {
                "release_fingerprint": str(release["fingerprint"]),
            }
            if on_observed is not None:
                kwargs["on_observed"] = on_observed
            return host_control.execute(
                operation_type,
                vmid,
                str(job["request_id"]),
                **kwargs,
            )
        if operation_type == "ct110_system_update":
            plan = self._approved_ct110_system_plan(job)
            kwargs = {
                "system_update_fingerprint": str(plan["fingerprint"]),
            }
            if on_observed is not None:
                kwargs["on_observed"] = on_observed
            return host_control.execute(
                operation_type,
                vmid,
                str(job["request_id"]),
                **kwargs,
            )
        identity = (
            self._expected_snapshot_identity_from_job(job)
            if operation_type in {"snapshot_rollback", "snapshot_delete"}
            else None
        )
        identity_kwargs = (
            {
                "snapshot_kind": str(identity["kind"]),
                "expected_source_job_id": str(identity["host_source_job_id"]),
                "expected_pve_snaptime": int(identity["pve_snaptime"]),
            }
            if identity
            else {}
        )
        if on_observed is not None:
            identity_kwargs["on_observed"] = on_observed
        return host_control.execute(
            operation_type,
            vmid,
            str(job["request_id"]),
            snapshot_name=str(snapshot_name) if snapshot_name else None,
            **identity_kwargs,
        )

    def _approved_self_update_release(self, job: dict[str, Any]) -> dict[str, Any]:
        if not job.get("plan_id"):
            raise ValueError("Self-update job has no approved plan")
        plan = self.db.get_plan(str(job["plan_id"]))
        if plan.get("status") != "approved" or self._plan_type(plan) != "self_update":
            raise ValueError(
                f"Self-update plan status is {plan.get('status')}, not approved"
            )
        payload = dict(plan.get("payload") or {})
        fingerprint = str(payload.get("fingerprint") or "")
        if len(fingerprint) != 64:
            raise ValueError("Approved self-update plan has an invalid fingerprint")
        return payload

    def _approved_ct110_system_plan(self, job: dict[str, Any]) -> dict[str, Any]:
        if int(job.get("vmid") or 0) != 110 or not job.get("plan_id"):
            raise ValueError("CT110 system update job has no approved plan")
        plan = self.db.get_plan(str(job["plan_id"]))
        if (
            plan.get("status") != "approved"
            or self._plan_type(plan) != "ct110_system_update"
        ):
            raise ValueError(
                f"CT110 system update plan status is {plan.get('status')}, not approved"
            )
        fingerprint = str(plan.get("fingerprint") or "")
        if len(fingerprint) != 64 or any(
            character not in "0123456789abcdef" for character in fingerprint
        ):
            raise ValueError("Approved CT110 system plan has an invalid fingerprint")
        return plan

    def _raw_snapshot_list(self, vmid: int) -> list[dict[str, Any]]:
        if self.host_control is not None:
            return self.host_control.list_snapshots(vmid)
        return list(
            _executor_data(
                self.executor.run("list-snapshots", vmid, timeout=60)
            ).get("snapshots", [])
        )

    def _pre_update_create_contract(
        self,
        job: dict[str, Any],
        *,
        required: bool = True,
    ) -> dict[str, Any] | None:
        current = self.db.get_job(str(job["id"]))
        result = current.get("result")
        contract = (
            result.get("pre_update_snapshot_create")
            if isinstance(result, dict)
            else None
        )
        if contract is None and not required:
            return None
        if not isinstance(contract, dict):
            raise ValueError("Update job has no durable pre-update create contract")
        if (
            contract.get("version") != 1
            or contract.get("request_id") != f"pre-update-snapshot-{current['id']}"
            or contract.get("snapshot_name") != current.get("snapshot_name")
            or contract.get("phase") not in {
                "prepared", "submitting", "remote_observed", "outcome_unknown",
                "remote_succeeded", "confirming", "completed", "definitive_failed",
            }
        ):
            raise ValueError("Pre-update create durable contract is malformed")
        return dict(contract)

    def _observe_pre_update_create(
        self,
        job: dict[str, Any],
        remote: dict[str, Any],
    ) -> None:
        contract = self._pre_update_create_contract(job)
        host_job_id = str(remote.get("id") or "")
        if not self._valid_host_source_job_id(host_job_id):
            raise HostControlError(
                "Host snapshot create returned an invalid job ID",
                status="contract_mismatch",
            )
        contract.update(
            {"phase": "remote_observed", "host_job_id": host_job_id, "last_error": None}
        )
        self.db.persist_pre_update_create_contract(str(job["id"]), contract)

    def _continue_pre_update_snapshot_create(
        self,
        job: dict[str, Any],
        snapshot_name: str,
    ) -> dict[str, Any]:
        vmid = int(job["vmid"])
        contract = self._pre_update_create_contract(job, required=False)
        if contract is None:
            contract = {
                "version": 1,
                "request_id": f"pre-update-snapshot-{job['id']}",
                "phase": "prepared",
                "snapshot_name": snapshot_name,
                "host_job_id": None,
                "snapshot_identity": None,
                "last_error": None,
            }
            self.db.persist_pre_update_create_contract(str(job["id"]), contract)
        try:
            host = self._require_host_control("Pre-update snapshot creation")
            if contract["phase"] == "prepared":
                contract["phase"] = "submitting"
                self.db.persist_pre_update_create_contract(str(job["id"]), contract)
                host_result = host.execute(
                    "snapshot_create",
                    vmid,
                    str(contract["request_id"]),
                    snapshot_name=snapshot_name,
                    on_observed=lambda remote: self._observe_pre_update_create(
                        job,
                        remote,
                    ),
                )
            elif contract["phase"] in {"submitting", "remote_observed", "outcome_unknown"}:
                host_result = host.wait_existing_job(
                    "snapshot_create",
                    vmid,
                    str(contract["request_id"]),
                    snapshot_name=snapshot_name,
                    on_observed=lambda remote: self._observe_pre_update_create(
                        job,
                        remote,
                    ),
                )
            elif contract["phase"] in {"remote_succeeded", "confirming"}:
                identity = contract.get("snapshot_identity")
                if not isinstance(identity, dict):
                    raise ValueError("Pre-update create result identity is missing")
                return dict(identity)
            else:
                raise ValueError("Pre-update create cannot continue from terminal phase")
            identity = self._pre_update_snapshot_identity(
                host_result, vmid=vmid, snapshot_name=snapshot_name
            )
            contract = self._pre_update_create_contract(job)
            contract.update(
                {
                    "phase": "remote_succeeded",
                    "snapshot_identity": identity,
                    "last_error": None,
                }
            )
            self.db.persist_pre_update_create_contract(str(job["id"]), contract)
            return identity
        except (HostControlError, ValueError) as exc:
            contract = self._pre_update_create_contract(job)
            if (
                classify_host_job_error(exc, submit_started=contract["phase"] != "prepared")
                is HostJobOutcome.DEFINITIVE_FAILURE
            ):
                contract.update({"phase": "definitive_failed", "last_error": str(exc)})
                self.db.persist_pre_update_create_contract(str(job["id"]), contract)
                raise ExecutorError(
                    f"Pre-update snapshot host operation failed: {exc}"
                ) from exc
            contract.update({"phase": "outcome_unknown", "last_error": str(exc)})
            self.db.persist_pre_update_create_contract(str(job["id"]), contract)
            self._hold_embedded_host_unknown(job, "pre_update_snapshot_create", str(exc))
            raise HostOperationOutcomeUnknown(str(exc)) from exc

    def _hold_embedded_host_unknown(
        self,
        job: dict[str, Any],
        contract_name: str,
        error: str,
    ) -> None:
        events = self.db.list_job_events(str(job["id"]), limit=200)
        event_type = f"{contract_name}_outcome_unknown"
        if not any(event.get("event_type") == event_type for event in events):
            event = self.db.insert_job_event(
                job_id=str(job["id"]),
                vmid=int(job["vmid"]),
                level="warning",
                stage=(
                    "rollback"
                    if contract_name.startswith("automatic_rollback")
                    else "snapshot"
                ),
                progress=int(self.db.get_job(str(job["id"])).get("progress") or 20),
                event_type=event_type,
                message=(
                    "Embedded host operation outcome is unknown; read-only reconciliation "
                    "is required and destructive operations remain blocked"
                ),
                details={"reconciliation_required": True},
            )
            self.mqtt.publish_event(int(job["vmid"]), event)
        state = self.get_state(int(job["vmid"]))
        state.update(
            {
                "active_job_id": str(job["id"]),
                "operation_status": "reconciliation_required",
                "last_error": sanitize_text(error, limit=2000),
            }
        )
        self._save_state(int(job["vmid"]), state)

    def _confirm_physical_pre_update_snapshot(
        self,
        vmid: int,
        snapshot_name: str,
        host_source_job_id: str,
        pve_snaptime: int,
    ) -> dict[str, Any]:
        """Confirm an exact host-owned snapshot without consulting durable proof."""
        try:
            snapshots = self._raw_snapshot_list(vmid)
        except (AttributeError, TypeError) as exc:
            raise ValueError("Physical snapshot listing is malformed") from exc
        if not isinstance(snapshots, list) or any(
            not isinstance(item, dict) for item in snapshots
        ):
            raise ValueError("Physical snapshot listing is malformed")
        matches = [
            item
            for item in snapshots
            if isinstance(item.get("name"), str)
            and item["name"] == snapshot_name
        ]
        if len(matches) != 1:
            raise ValueError(
                "Physical snapshot listing must contain exactly one matching snapshot"
            )
        snapshot = dict(matches[0])
        parsed = parse_owned_snapshot_name(snapshot_name, vmid=vmid)
        if parsed is None or parsed.get("kind") != "pre-update":
            raise ValueError("Physical snapshot name is not an exact pre-update name")
        if snapshot.get("kind") != "pre-update":
            raise ValueError("Physical snapshot kind is not pre-update")
        if snapshot.get("owned_by_hubinet_ops") is not True:
            raise ValueError("Physical snapshot is foreign or ownership is uncertain")
        physical_source_job_id = str(snapshot.get("source_job_id") or "")
        if not self._valid_host_source_job_id(physical_source_job_id):
            raise ValueError("Physical snapshot source job ID is missing or malformed")
        if physical_source_job_id != host_source_job_id:
            raise ValueError("Physical snapshot source job ID does not match host result")
        if snapshot.get("pve_snaptime") != pve_snaptime:
            raise ValueError("Physical snapshot PVE snaptime does not match host result")
        if (
            "ownership_status" in snapshot
            and snapshot.get("ownership_status") != "owned"
        ):
            raise ValueError("Physical snapshot ownership status is not owned")
        if "vmid" in snapshot:
            raw_vmid = snapshot.get("vmid")
            if isinstance(raw_vmid, bool):
                raise ValueError("Physical snapshot VMID is malformed")
            try:
                physical_vmid = int(raw_vmid)
            except (TypeError, ValueError) as exc:
                raise ValueError("Physical snapshot VMID is malformed") from exc
            if physical_vmid != int(vmid):
                raise ValueError("Physical snapshot VMID conflicts with the resource")
        return snapshot

    @classmethod
    def _snapshot_identity(
        cls,
        vmid: int,
        snapshot: dict[str, Any],
        *,
        expected_name: str | None = None,
        expected_kind: str | None = None,
    ) -> dict[str, Any]:
        name = str(snapshot.get("name") or "")
        parsed = parse_owned_snapshot_name(name, vmid=vmid)
        kind = str(snapshot.get("kind") or "")
        source_job_id = str(snapshot.get("host_source_job_id") or snapshot.get("source_job_id") or "")
        snaptime = snapshot.get("pve_snaptime")
        if (
            parsed is None
            or kind != parsed.get("kind")
            or (expected_name is not None and name != expected_name)
            or (expected_kind is not None and kind != expected_kind)
            or snapshot.get("owned_by_hubinet_ops") is not True
            or not cls._valid_host_source_job_id(source_job_id)
            or isinstance(snaptime, bool)
            or not isinstance(snaptime, int)
            or snaptime <= 0
        ):
            raise ValueError("Snapshot has no valid physical identity")
        return {
            "version": 1,
            "vmid": int(vmid),
            "snapshot_name": name,
            "kind": kind,
            "host_source_job_id": source_job_id,
            "pve_snaptime": snaptime,
        }

    @classmethod
    def _expected_snapshot_identity_from_job(
        cls,
        job: dict[str, Any],
    ) -> dict[str, Any]:
        result = job.get("result")
        identity = (
            result.get("expected_snapshot_identity")
            if isinstance(result, dict)
            else None
        )
        if not isinstance(identity, dict):
            raise ValueError("Snapshot operation has no durable expected identity")
        modeled = {
            "name": identity.get("snapshot_name"),
            "kind": identity.get("kind"),
            "source_job_id": identity.get("host_source_job_id"),
            "pve_snaptime": identity.get("pve_snaptime"),
            "owned_by_hubinet_ops": True,
        }
        validated = cls._snapshot_identity(
            int(job["vmid"]),
            modeled,
            expected_name=str(job.get("snapshot_name") or ""),
        )
        if identity.get("version") != 1 or identity.get("vmid") != int(job["vmid"]):
            raise ValueError("Snapshot operation expected identity is malformed")
        return validated

    @staticmethod
    def _valid_host_source_job_id(value: str) -> bool:
        return (
            len(value) == 32
            and all(char in "0123456789abcdef" for char in value)
        )

    @classmethod
    def _pre_update_snapshot_identity(
        cls,
        result: dict[str, Any],
        *,
        vmid: int,
        snapshot_name: str,
    ) -> dict[str, Any]:
        if not isinstance(result, dict):
            raise ValueError("Host snapshot result is malformed")
        if str(result.get("name") or "") != snapshot_name:
            raise ValueError("Host snapshot result name does not match the request")
        if str(result.get("kind") or "") != "pre-update":
            raise ValueError("Host snapshot result kind is not pre-update")
        if "vmid" in result:
            result_vmid = result.get("vmid")
            if isinstance(result_vmid, bool):
                raise ValueError("Host snapshot result VMID is malformed")
            try:
                parsed_vmid = int(result_vmid)
            except (TypeError, ValueError) as exc:
                raise ValueError("Host snapshot result VMID is malformed") from exc
            if parsed_vmid != int(vmid):
                raise ValueError("Host snapshot result VMID does not match the request")
        source_job_id = str(result.get("source_job_id") or "")
        if not cls._valid_host_source_job_id(source_job_id):
            raise ValueError("Host snapshot result source job ID is missing or malformed")
        snaptime = result.get("pve_snaptime")
        if (
            isinstance(snaptime, bool)
            or not isinstance(snaptime, int)
            or snaptime <= 0
        ):
            raise ValueError("Host snapshot result PVE snaptime is missing or malformed")
        return {
            "version": 1,
            "vmid": int(vmid),
            "snapshot_name": snapshot_name,
            "kind": "pre-update",
            "host_source_job_id": source_job_id,
            "pve_snaptime": snaptime,
        }

    def _validate_approved_plan(
        self,
        job: dict[str, Any],
        preflight: dict[str, Any],
    ) -> None:
        plan = self.db.get_plan(job["plan_id"])
        updates = dict(_executor_data(preflight).get("updates") or {})
        pending = max(0, int(updates.get("pending_count", 0) or 0))
        fingerprint = str(updates.get("fingerprint") or _fingerprint(updates))
        if pending <= 0:
            raise ExecutorError(
                "Approved update plan is no longer valid: no updates remain; scan again",
                data={"updates": updates},
            )
        if fingerprint != str(plan["fingerprint"]):
            raise ExecutorError(
                "Approved update plan changed after approval; scan and approve the new plan",
                data={
                    "approved_fingerprint": plan["fingerprint"],
                    "current_fingerprint": fingerprint,
                    "updates": updates,
                },
            )

    def _repair_or_raise(
        self,
        job: dict[str, Any],
        cause: ExecutorError,
        policy: StabilizationPolicy,
        emit: Callable[..., None],
    ) -> dict[str, Any]:
        cfg = self._resource(int(job["vmid"]))
        actions = list(cfg.get("repair_actions") or [])
        emit(
            stage="healthcheck",
            progress=84,
            level="warning",
            event_type="stabilization_timeout",
            message=str(cause),
            details=cause.data,
        )
        if not actions:
            raise cause
        emit(
            stage="repair",
            progress=85,
            event_type="repair_started",
            message="Configured repair actions started",
        )
        self._execute(
            "repair",
            int(job["vmid"]),
            max(900, int(policy.repair_timeout_seconds)),
            emit,
        )
        return self.stabilizer.wait(
            vmid=int(job["vmid"]),
            phase="repair",
            timeout_seconds=policy.repair_timeout_seconds,
            policy=policy,
            emit=emit,
            initial_grace=False,
        )

    def _create_followup_plan(
        self,
        vmid: int,
        cfg: dict[str, Any],
        updates: dict[str, Any],
    ) -> dict[str, Any]:
        fingerprint = str(updates.get("fingerprint") or _fingerprint(updates))
        active = self.db.find_active_plan(vmid, fingerprint)
        created = active is None
        if active is None:
            active = self.db.create_plan(
                vmid=vmid,
                container_name=str(cfg.get("name", f"ct-{vmid}")),
                fingerprint=fingerprint,
                risk=_risk_for(cfg, updates),
                payload=updates,
                ttl_minutes=int(self.settings.scheduler.get("approval_ttl_minutes", 1440)),
            )
        state = self.get_state(vmid)
        state.update(
            {
                "risk": active["risk"],
                "active_plan_id": active["id"],
                "active_plan_status": active["status"],
                "operation_status": "waiting_approval",
                "job_stage": "idle",
                "job_progress": 0,
            }
        )
        self._save_state(vmid, state)
        if created:
            self._notify_ha(
                self._notification(
                    "approval_required",
                    vmid,
                    pending_count=max(0, int(updates.get("pending_count", 0) or 0)),
                    risk=active["risk"],
                )
            )
        return active

    def _rollback(self, job: dict[str, Any], cause: str) -> None:
        vmid = int(job["vmid"])
        cfg = self._resource(vmid)
        policy = StabilizationPolicy.from_config(cfg.get("stabilization"))
        snapshot = str(self.db.get_job(job["id"]).get("snapshot_name") or "")
        if not snapshot:
            self.db.update_plan_status(job["plan_id"], "failed")
            self._terminal(
                job,
                "failed",
                "manual_intervention",
                f"{cause}; rollback snapshot is missing",
            )
            return
        emit = self._emitter(job)
        try:
            try:
                self._refresh_snapshot_state(
                    vmid,
                    job=job,
                    required_name=snapshot,
                    required_kind="pre-update",
                )
            except (HostControlError, ValueError) as exc:
                raise ExecutorError(
                    f"Rollback snapshot ownership could not be confirmed: {exc}"
                ) from exc
            emit(
                stage="rollback",
                progress=88,
                level="warning",
                event_type="rollback_started",
                message="Rollback started",
            )
            refreshed = self._refresh_snapshot_state(
                vmid,
                job=job,
                required_name=snapshot,
                required_kind="pre-update",
            )
            selected = next(
                item
                for item in refreshed["snapshots"]
                if item.get("name") == snapshot
            )
            identity = self._snapshot_identity(
                vmid,
                selected,
                expected_name=snapshot,
                expected_kind="pre-update",
            )
            proof = self._pre_update_identity_from_job(job)
            if identity != proof:
                raise ExecutorError("Rollback snapshot physical identity changed")
            contract = {
                "version": 1,
                "request_id": f"automatic-rollback-{job['id']}",
                "phase": "prepared",
                "snapshot_name": snapshot,
                "expected_snapshot_identity": identity,
                "host_job_id": None,
                "last_error": None,
            }
            self.db.persist_automatic_rollback_contract(str(job["id"]), contract)
            self._continue_automatic_rollback(job, cause, emit, policy)
        except (ExecutorError, HostControlError, ValueError) as rollback_error:
            try:
                durable = self._automatic_rollback_contract(job)
            except ValueError:
                durable = None
            if isinstance(durable, dict) and durable.get("phase") == "stabilizing":
                if isinstance(rollback_error, StabilizationInterrupted) or (
                    isinstance(rollback_error, ExecutorError) and self._stop.is_set()
                ):
                    self._hold_automatic_rollback_unknown(
                        job, durable, str(rollback_error)
                    )
                    return
                if isinstance(rollback_error, ExecutorError):
                    self._transition_automatic_rollback(
                        job,
                        durable,
                        "definitive_failed",
                        last_error=str(rollback_error),
                    )
                    self._fail_automatic_rollback(job, cause, rollback_error)
                    return
            if isinstance(durable, dict) and durable.get("phase") in {
                "submitting", "remote_observed", "outcome_unknown",
                "remote_succeeded", "stabilizing", "stabilized",
            }:
                self._hold_automatic_rollback_unknown(
                    job, durable, str(rollback_error)
                )
                return
            self._fail_automatic_rollback(job, cause, rollback_error)

    def _automatic_rollback_contract(self, job: dict[str, Any]) -> dict[str, Any]:
        current = self.db.get_job(str(job["id"]))
        result = current.get("result")
        contract = result.get("automatic_rollback") if isinstance(result, dict) else None
        if not isinstance(contract, dict):
            raise ValueError("Update job has no durable automatic rollback contract")
        identity = self._pre_update_identity_from_job(current)
        if (
            contract.get("version") != 1
            or contract.get("request_id") != f"automatic-rollback-{current['id']}"
            or contract.get("snapshot_name") != current.get("snapshot_name")
            or contract.get("expected_snapshot_identity") != identity
            or contract.get("phase") not in {
                "prepared", "submitting", "remote_observed", "outcome_unknown",
                "remote_succeeded", "stabilizing", "stabilized", "completed",
                "definitive_failed",
            }
        ):
            raise ValueError("Automatic rollback durable contract is malformed")
        return dict(contract)

    def _transition_automatic_rollback(
        self,
        job: dict[str, Any],
        contract: dict[str, Any],
        phase: str,
        *,
        host_job_id: str | None = None,
        last_error: str | None = None,
        stabilization_result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        updated = dict(contract)
        updated["phase"] = phase
        if host_job_id is not None:
            updated["host_job_id"] = host_job_id
        updated["last_error"] = (
            sanitize_text(last_error, limit=1000) if last_error else None
        )
        if stabilization_result is not None:
            updated["stabilization_result"] = dict(stabilization_result)
        persisted = self.db.persist_automatic_rollback_contract(
            str(job["id"]), updated
        )
        return dict(persisted["result"]["automatic_rollback"])

    def _observe_automatic_rollback(
        self,
        job: dict[str, Any],
        remote: dict[str, Any],
    ) -> None:
        contract = self._automatic_rollback_contract(job)
        host_job_id = str(remote.get("id") or "")
        if not self._valid_host_source_job_id(host_job_id):
            raise HostControlError(
                "Host control lookup returned an invalid job ID",
                status="contract_mismatch",
            )
        self._transition_automatic_rollback(
            job, contract, "remote_observed", host_job_id=host_job_id
        )

    def _continue_automatic_rollback(
        self,
        job: dict[str, Any],
        cause: str,
        emit: Callable[..., None],
        policy: StabilizationPolicy,
    ) -> None:
        contract = self._automatic_rollback_contract(job)
        phase = str(contract["phase"])
        identity = dict(contract["expected_snapshot_identity"])
        try:
            if phase == "prepared":
                host = self._require_host_control("Automatic rollback")
                contract = self._transition_automatic_rollback(
                    job, contract, "submitting"
                )
                result = host.execute(
                    "snapshot_rollback",
                    int(job["vmid"]),
                    str(contract["request_id"]),
                    snapshot_name=str(identity["snapshot_name"]),
                    snapshot_kind=str(identity["kind"]),
                    expected_source_job_id=str(identity["host_source_job_id"]),
                    expected_pve_snaptime=int(identity["pve_snaptime"]),
                    on_observed=lambda remote: self._observe_automatic_rollback(
                        job, remote
                    ),
                )
            elif phase in {"submitting", "remote_observed", "outcome_unknown"}:
                host = self._require_host_control("Automatic rollback")
                result = host.wait_existing_job(
                    "snapshot_rollback",
                    int(job["vmid"]),
                    str(contract["request_id"]),
                    snapshot_name=str(identity["snapshot_name"]),
                    snapshot_kind=str(identity["kind"]),
                    expected_source_job_id=str(identity["host_source_job_id"]),
                    expected_pve_snaptime=int(identity["pve_snaptime"]),
                    on_observed=lambda remote: self._observe_automatic_rollback(
                        job, remote
                    ),
                )
            elif phase in {
                "remote_succeeded", "stabilizing", "stabilized", "completed"
            }:
                result = {}
            else:
                return
            contract = self._automatic_rollback_contract(job)
            if str(contract["phase"]) not in {
                "remote_succeeded", "stabilizing", "stabilized", "completed"
            }:
                contract = self._transition_automatic_rollback(
                    job, contract, "remote_succeeded"
                )
            self._finish_automatic_rollback(job, cause, emit, policy)
        except HostControlError as exc:
            contract = self._automatic_rollback_contract(job)
            outcome = classify_host_job_error(exc, submit_started=True)
            if outcome is HostJobOutcome.OUTCOME_UNKNOWN:
                self._hold_automatic_rollback_unknown(job, contract, str(exc))
                return
            self._transition_automatic_rollback(
                job, contract, "definitive_failed", last_error=str(exc)
            )
            self._fail_automatic_rollback(job, cause, exc)

    def _hold_automatic_rollback_unknown(
        self,
        job: dict[str, Any],
        contract: dict[str, Any],
        error: str,
    ) -> None:
        if str(contract.get("phase")) in {"remote_succeeded", "stabilizing", "stabilized"}:
            contract = self._transition_automatic_rollback(
                job, contract, str(contract["phase"]), last_error=error
            )
        else:
            contract = self._transition_automatic_rollback(
                job, contract, "outcome_unknown", last_error=error
            )
        existing = self.db.list_job_events(str(job["id"]), limit=200)
        if not any(
            event.get("event_type") == "automatic_rollback_outcome_unknown"
            for event in existing
        ):
            event = self.db.insert_job_event(
                job_id=str(job["id"]),
                vmid=int(job["vmid"]),
                level="warning",
                stage="rollback",
                progress=int(self.db.get_job(str(job["id"])).get("progress") or 88),
                event_type="automatic_rollback_outcome_unknown",
                message=(
                    "Automatic rollback outcome is unknown; read-only reconciliation "
                    "is required and destructive operations remain blocked"
                ),
                details={"request_id": contract["request_id"], "reconciliation_required": True},
            )
            self.mqtt.publish_event(int(job["vmid"]), event)
        state = self.get_state(int(job["vmid"]))
        state.update(
            {
                "operation_status": "reconciliation_required",
                "last_error": sanitize_text(error, limit=2000),
                "active_job_id": str(job["id"]),
            }
        )
        self._save_state(int(job["vmid"]), state)

    def _fail_automatic_rollback(
        self,
        job: dict[str, Any],
        cause: str,
        rollback_error: Exception,
    ) -> None:
        error = f"Original failure: {cause}; rollback failure: {rollback_error}"
        self.db.update_plan_status(str(job["plan_id"]), "failed")
        self._terminal(job, "failed", "manual_intervention", error)
        self._notify_ha(
            self._notification(
                "manual_intervention_required", int(job["vmid"]), error=error
            )
        )

    def _finish_automatic_rollback(
        self,
        job: dict[str, Any],
        cause: str,
        emit: Callable[..., None],
        policy: StabilizationPolicy,
    ) -> None:
        vmid = int(job["vmid"])
        snapshot = str(self.db.get_job(job["id"]).get("snapshot_name") or "")
        try:
            contract = self._automatic_rollback_contract(job)
            if str(contract["phase"]) in {"remote_succeeded", "stabilizing"}:
                contract = self._transition_automatic_rollback(
                    job, contract, "stabilizing"
                )
                emit(
                    stage="rollback_wait",
                    progress=90,
                    event_type="rollback_wait",
                    message="Waiting for LXC, systemd, and Docker after rollback",
                )
                health = self.stabilizer.wait(
                    vmid=vmid,
                    phase="rollback",
                    timeout_seconds=policy.post_rollback_timeout_seconds,
                    policy=policy,
                    emit=emit,
                )
                contract = self._transition_automatic_rollback(
                    job,
                    contract,
                    "stabilized",
                    stabilization_result=health,
                )
            elif str(contract["phase"]) == "stabilized":
                health = dict(contract.get("stabilization_result") or {})
                if not health:
                    raise ValueError("Automatic rollback stabilization result is missing")
            elif str(contract["phase"]) == "completed":
                self._terminal(job, "rolled_back", "rolled_back", cause)
                self._notify_ha(self._notification("job_rolled_back", vmid))
                return
            else:
                return
            emit(
                stage="rollback_healthcheck",
                progress=98,
                event_type="rollback_healthcheck_passed",
                message="Rollback service stabilization passed",
            )
            self.db.update_job(
                job["id"],
                result={"rollback_healthcheck": health},
                error=cause,
            )
            self.db.update_plan_status(job["plan_id"], "rolled_back")
            state = self.get_state(vmid)
            state.update(health)
            state.update(
                {
                    "health_status": health.get(
                        "health_status",
                        health.get("health", "healthy"),
                    ),
                    "snapshot_name": snapshot,
                    "last_error": cause,
                    "active_job_id": job["id"],
                    "verification_status": "unknown",
                    "last_verification": None,
                    "apt_check_ok": None,
                    "dpkg_audit_ok": None,
                    "packages_remaining_count": None,
                    "pending_updates": None,
                    "update_status": "unknown",
                    "updates": {"pending_count": None, "packages": []},
                }
            )
            self._save_state(vmid, state)
            try:
                self._refresh_snapshot_state(vmid, job=job)
            except (ExecutorError, HostControlError, ValueError) as exc:
                LOGGER.warning(
                    "Read-only snapshot refresh failed after automatic rollback job %s: %s",
                    job["id"],
                    exc,
                )
            contract = self._transition_automatic_rollback(
                job, contract, "completed", stabilization_result=health
            )
            self._terminal(job, "rolled_back", "rolled_back", cause)
            self._notify_ha(self._notification("job_rolled_back", vmid))
        except (ExecutorError, ValueError):
            raise

    def _pre_update_identity_from_job(self, job: dict[str, Any]) -> dict[str, Any]:
        current = self.db.get_job(str(job["id"]))
        result = current.get("result")
        proof = result.get("snapshot_proof") if isinstance(result, dict) else None
        if not isinstance(proof, dict) or proof.get("version") != 3:
            raise ValueError("Update job has no current snapshot proof")
        identity = {
            "version": 1,
            "vmid": proof.get("vmid"),
            "snapshot_name": proof.get("snapshot_name"),
            "kind": proof.get("kind"),
            "host_source_job_id": proof.get("host_source_job_id"),
            "pve_snaptime": proof.get("pve_snaptime"),
        }
        validated = self._snapshot_identity(
            int(current["vmid"]),
            {
                "name": identity["snapshot_name"],
                "kind": identity["kind"],
                "host_source_job_id": identity["host_source_job_id"],
                "pve_snaptime": identity["pve_snaptime"],
                "owned_by_hubinet_ops": True,
            },
            expected_name=str(current.get("snapshot_name") or ""),
            expected_kind="pre-update",
        )
        if identity.get("vmid") != int(current["vmid"]):
            raise ValueError("Update snapshot proof VMID changed")
        return validated

    def _reattach_automatic_rollback(self, job: dict[str, Any]) -> None:
        vmid = int(job["vmid"])
        cause = str(job.get("error") or "Update failed")
        try:
            self._continue_automatic_rollback(
                job,
                cause,
                self._emitter(job),
                StabilizationPolicy.from_config(
                    self._resource(vmid).get("stabilization")
                ),
            )
        except (ExecutorError, HostControlError, ValueError) as exc:
            LOGGER.error("Automatic rollback reconciliation failed closed: %s", exc)
            try:
                contract = self._automatic_rollback_contract(job)
            except ValueError:
                current = self.db.get_job(str(job["id"]))
                result = dict(current.get("result") or {})
                result["automatic_rollback_reconciliation_error"] = sanitize_text(
                    exc, limit=1000
                )
                self.db.update_job(
                    str(job["id"]), result=result, error=str(exc)
                )
                self._hold_embedded_host_unknown(
                    job, "automatic_rollback_contract", str(exc)
                )
                return
            if str(contract.get("phase")) == "stabilizing":
                if isinstance(exc, StabilizationInterrupted) or (
                    isinstance(exc, ExecutorError) and self._stop.is_set()
                ):
                    self._hold_automatic_rollback_unknown(job, contract, str(exc))
                    return
                if isinstance(exc, ExecutorError):
                    self._transition_automatic_rollback(
                        job,
                        contract,
                        "definitive_failed",
                        last_error=str(exc),
                    )
                    self._fail_automatic_rollback(job, cause, exc)
                    return
            self._hold_automatic_rollback_unknown(job, contract, str(exc))

    def _terminal(
        self,
        job: dict[str, Any],
        job_status: str,
        result: str,
        error: str | None,
    ) -> None:
        current = self.db.get_job(str(job["id"]))
        if str(current.get("status")) in TERMINAL_JOB_STATUSES:
            LOGGER.warning(
                "Ignoring duplicate terminal transition for job %s already in %s",
                job.get("id"),
                current.get("status"),
            )
            return
        job = current
        event = self.db.insert_job_event(
            job_id=job["id"],
            vmid=int(job["vmid"]),
            level=(
                "error"
                if result in {"failed", "manual_intervention"}
                else "warning"
                if result == "interrupted"
                else "info"
            ),
            stage="completed" if result in {"success", "rolled_back"} else "failed",
            progress=100,
            event_type=f"job_{result}",
            message=error or f"Job finished: {result}",
            terminal=True,
        )
        self.db.update_job(
            job["id"],
            status=job_status,
            stage=event["stage"],
            progress=100,
            error=error,
        )
        self._publish_terminal_transition(
            job,
            job_status=job_status,
            result=result,
            error=error,
            event=event,
        )

    def _terminal_with_snapshot_retention(
        self,
        job: dict[str, Any],
        *,
        job_status: str,
        result: str,
        error: str | None,
    ) -> dict[str, Any]:
        retention_target = self._snapshot_retention_count(int(job["vmid"]))
        if retention_target == 0:
            self._terminal(job, job_status, result, error)
            return self.db.get_job(str(job["id"]))
        prune_request_id = f"retention-{str(job['id'])}"
        source, prune, event, created = self.db.terminalize_job_with_snapshot_prune(
            str(job["id"]),
            source_status=job_status,
            terminal_result=result,
            error=error,
            prune_request_id=prune_request_id,
            retention_target=retention_target,
        )
        if not created:
            return prune
        self._publish_terminal_transition(
            source,
            job_status=job_status,
            result=result,
            error=error,
            event=event,
            next_job=prune,
        )
        self.mqtt.publish_job(int(prune["vmid"]), prune, force=True)
        self._run_operation_job(self.db.next_queued_job() or prune)
        return prune

    def _publish_terminal_transition(
        self,
        job: dict[str, Any],
        *,
        job_status: str,
        result: str,
        error: str | None,
        event: dict[str, Any],
        next_job: dict[str, Any] | None = None,
    ) -> None:
        operation = {
            "success": "success",
            "rolled_back": "rolled_back",
            "manual_intervention": "manual_intervention",
            "failed": "failed",
            "interrupted": "unknown",
        }[result]
        plan_status: str | None = None
        if job.get("plan_id"):
            plan = self.db.get_plan(job["plan_id"])
            plan_status = str(plan["status"])
        if plan_status == "approved":
            plan_status = {
                "success": "completed",
                "rolled_back": "rolled_back",
                "blocked": "blocked",
                "interrupted": "interrupted",
            }.get(job_status, "failed")
            self.db.update_plan_status(job["plan_id"], plan_status)
        state = self.get_state(int(job["vmid"]))
        terminal_at = self._now()
        suppress_recovery = result in {"success", "rolled_back"}
        state.update(
            {
                "active_job_id": (
                    str(next_job["id"]) if next_job is not None else None
                ),
                "last_job_id": job["id"],
                "operation_type": (
                    str(next_job["operation_type"])
                    if next_job is not None
                    else job.get("operation_type", "update")
                ),
                "operation_status": (
                    str(next_job["status"]) if next_job is not None else operation
                ),
                "job_stage": (
                    str(next_job["stage"]) if next_job is not None else event["stage"]
                ),
                "job_progress": (
                    int(next_job["progress"]) if next_job is not None else 100
                ),
                "last_operation_result": result,
                "last_error": (
                    sanitize_text(error, limit=2000)
                    if error
                    else state.get("last_error")
                ),
                "last_job_event": event,
                "last_terminal_event": f"job_{result}",
                "last_terminal_at": terminal_at.isoformat(),
                "recovery_notification_suppressed_until": (
                    (terminal_at + timedelta(seconds=180)).isoformat()
                    if suppress_recovery
                    else state.get("recovery_notification_suppressed_until")
                ),
            }
        )
        if job.get("plan_id"):
            state["active_plan_id"] = None
            state["active_plan_status"] = plan_status
        self._save_state(int(job["vmid"]), state)
        self.mqtt.publish_event(int(job["vmid"]), event)
        self.mqtt.publish_job(
            int(job["vmid"]),
            self.db.get_job(job["id"]),
            force=True,
        )

    def _best_effort_duration(self, job: dict[str, Any]) -> int | None:
        created_at = parse_utc_timestamp(job.get("created_at"))
        finished_at = parse_utc_timestamp(self._now())
        if created_at is None or finished_at is None:
            LOGGER.warning(
                "Cannot calculate duration for job %s: malformed timestamp",
                job.get("id"),
            )
            return None
        try:
            return max(0, int((finished_at - created_at).total_seconds()))
        except (TypeError, ValueError, OverflowError):
            LOGGER.warning(
                "Cannot calculate duration for job %s: incompatible timestamp",
                job.get("id"),
            )
            return None

    def _emitter(self, job: dict[str, Any]) -> Callable[..., None]:
        def emit(
            *,
            stage: str,
            progress: int,
            event_type: str,
            message: str,
            level: str = "info",
            details: dict[str, Any] | None = None,
        ) -> None:
            event = self.db.insert_job_event(
                job_id=job["id"],
                vmid=int(job["vmid"]),
                level=level,
                stage=stage,
                progress=progress,
                event_type=event_type,
                message=message,
                details=details,
            )
            state = self.get_state(int(job["vmid"]))
            state.update(
                {
                    "active_job_id": job["id"],
                    "operation_status": "running",
                    "job_stage": event["stage"],
                    "job_progress": event["progress"],
                    "last_operation_result": None,
                    "last_error": None,
                    "last_job_event": event,
                }
            )
            self._save_state(int(job["vmid"]), state)
            self.mqtt.publish_event(int(job["vmid"]), event)
            self.mqtt.publish_job(
                int(job["vmid"]),
                self.db.get_job(job["id"]),
            )

        return emit

    def _execute(
        self,
        action: str,
        vmid: int,
        timeout: int,
        emit: Callable[..., None],
        argument: str | None = None,
    ) -> dict[str, Any]:
        action_stage = {
            "preflight": "preflight",
            "snapshot": "snapshot",
            "update": "updating",
            "repair": "repair",
            "rollback": "rollback",
            "verify": "verifying",
        }.get(action, "idle")

        def on_event(item: dict[str, Any]) -> None:
            raw_details = item.get("details")
            event_type = str(item.get("event_type", "executor_event"))
            if event_type in BACKEND_ONLY_EVENT_TYPES:
                event_type = f"executor_{event_type}"
            emit(
                stage=action_stage,
                progress=_safe_int(
                    item.get("progress"),
                    STAGE_PROGRESS.get(action_stage, 0),
                ),
                level=str(item.get("level", "info")),
                event_type=event_type,
                message=str(item.get("message", "")),
                details=raw_details if isinstance(raw_details, dict) else {},
            )

        # Do not retry on TypeError. Retrying a destructive update/rollback action can
        # execute it twice. The real Executor and all supported fakes accept on_event.
        return self.executor.run(
            action,
            vmid,
            argument,
            timeout,
            on_event=on_event,
        )

    def _save_state(
        self,
        vmid: int,
        state: dict[str, Any],
        *,
        publish: bool = True,
    ) -> dict[str, Any]:
        state = normalize_state(self._decorate_state(vmid, state))
        events = self.db.list_container_events(vmid, 50)
        state["recent_job_events"] = events
        state["last_job_event"] = events[-1] if events else state.get("last_job_event")
        docker = dict(state.get("docker") or {})
        required = list(docker.get("required") or [])
        by_name = {
            str(item.get("name")): item
            for item in (docker.get("containers") or [])
            if isinstance(item, dict)
        }
        require_health = bool(docker.get("require_health", True))
        docker["required_total"] = len(required)
        docker["required_healthy"] = sum(
            1
            for name in required
            if by_name.get(str(name), {}).get("running")
            and (
                not require_health
                or by_name[str(name)].get("health") == "healthy"
            )
        )
        state["docker"] = docker
        saved = self.db.upsert_resource_state(vmid, state)
        if publish:
            self.mqtt.publish_resource_state(vmid, saved)
        self._publish_agent_state_if_changed()
        return saved

    def _decorate_state(self, vmid: int, state: dict[str, Any]) -> dict[str, Any]:
        cfg = self._resource(vmid)
        state.update(
            {
                "vmid": vmid,
                "resource_type": cfg.get("resource_type", "lxc"),
                "name": cfg.get("name", f"resource-{vmid}"),
                "display_name": cfg.get("display_name", cfg.get("name", f"resource-{vmid}")),
                "enabled": bool(cfg.get("enabled", False)),
                "adapter": cfg.get("adapter", "apt"),
                "criticality": cfg.get("criticality", "medium"),
                "primary_ip_address": cfg.get("ip_address"),
                "dashboard_path": cfg.get(
                    "dashboard_path",
                    f"/hubinet-ops/ct-{vmid}",
                ),
                "rollback_allowed": bool(
                    cfg.get("manual_rollback_allowed", False)
                ) and self._capabilities(vmid)["rollback"],
                "snapshot_restore_allowed": bool(
                    cfg.get("manual_snapshot_restore_allowed", False)
                ) and self._capabilities(vmid)["snapshot_rollback"],
                "operator_capabilities": self._capabilities(vmid),
                "monitoring": self._monitoring(vmid),
                "recovery_scan_enabled": bool(
                    (cfg.get("recovery_scan") or {}).get("enabled", False)
                ),
            }
        )
        active_job = self.db.get_active_job(vmid)
        if active_job:
            active_result = active_job.get("result")
            automatic = (
                active_result.get("automatic_rollback")
                if isinstance(active_result, dict)
                else None
            )
            pre_update_create = (
                active_result.get("pre_update_snapshot_create")
                if isinstance(active_result, dict)
                else None
            )
            reconciliation_required = (
                str(active_job.get("stage") or "") in {
                    "host_outcome_unknown", "host_reconciliation"
                }
                or (
                    isinstance(automatic, dict)
                    and automatic.get("phase") in {"outcome_unknown", "stabilizing"}
                )
                or (
                    isinstance(pre_update_create, dict)
                    and pre_update_create.get("phase") in {
                        "outcome_unknown", "remote_succeeded", "confirming"
                    }
                )
                or (
                    isinstance(active_result, dict)
                    and bool(active_result.get("automatic_rollback_reconciliation_error"))
                )
            )
            state.update(
                {
                    "active_job_id": active_job["id"],
                    "operation_type": active_job.get("operation_type", "update"),
                    "operation_status": (
                        "reconciliation_required"
                        if reconciliation_required
                        else "running"
                    ),
                    "job_stage": active_job["stage"],
                    "job_progress": active_job.get("progress", 0),
                }
            )
        if cfg.get("adapter") == "agent_self":
            agent_summary = self._agent_state()
            agent_summary.pop("last_refresh", None)
            state.update(agent_summary)
            state["mqtt_availability"] = self.mqtt.availability
            if active_job:
                state.update(
                    {
                        "active_job_id": active_job["id"],
                        "operation_type": active_job.get("operation_type", "update"),
                        "operation_status": (
                            "reconciliation_required"
                            if reconciliation_required
                            else "running"
                        ),
                        "job_stage": active_job["stage"],
                        "job_progress": active_job.get("progress", 0),
                    }
                )
        return state

    def _base_state(self, vmid: int) -> dict[str, Any]:
        cfg = self._resource(vmid)
        apt = cfg.get("adapter", "apt") == "apt"
        return normalize_state(
            {
                "vmid": vmid,
                "resource_type": cfg.get("resource_type", "lxc"),
                "adapter": cfg.get("adapter", "apt"),
                "health_status": "unknown",
                "health_score": 0,
                "update_status": "unknown",
                "operation_status": "idle",
                "job_stage": "idle",
                "job_progress": 0,
                "last_operation_result": None,
                "pending_updates": 0 if apt else None,
                "updates": {"pending_count": 0 if apt else None, "packages": []},
                "operator_capabilities": self._capabilities(vmid),
                "snapshot_restore_allowed": bool(
                    cfg.get("manual_snapshot_restore_allowed", False)
                ) and self._capabilities(vmid)["snapshot_rollback"],
                "monitoring": self._monitoring(vmid),
                "lifecycle_status": "idle",
                "verification_status": "unknown",
                "recovery_scan_enabled": bool(
                    (cfg.get("recovery_scan") or {}).get("enabled", False)
                ),
                "recovery_scan_status": (
                    "idle"
                    if bool(
                        (cfg.get("recovery_scan") or {}).get("enabled", False)
                    )
                    else "disabled"
                ),
            }
        )

    def _ensure_initial_states(self) -> None:
        self._suppress_agent_publication()
        try:
            for vmid in self.settings.resources:
                state = self.db.get_resource_state(vmid) or self._base_state(vmid)
                if state.get("lifecycle_status") == "running":
                    state.update(
                        {
                            "lifecycle_status": "failed",
                            "lifecycle_finished_at": self._now().isoformat(),
                            "lifecycle_error": "Agent restarted during lifecycle operation",
                            "operation_status": "failed",
                            "job_stage": "failed",
                            "job_progress": 100,
                            "last_operation_result": "failed",
                            "last_error": "Agent restarted during lifecycle operation",
                            "expected_lxc_status": None,
                            "intentional_shutdown": False,
                            "lifecycle_health_pending": False,
                        }
                    )
                if state.get("recovery_scan_status") in {"scheduled", "running"}:
                    state.update(
                        {
                            "recovery_scan_status": "cancelled",
                            "recovery_scan_due_at": None,
                            "last_recovery_scan_result": "agent_restarted",
                        }
                    )
                if state.get("update_status") == "scanning":
                    state["update_status"] = "unknown"
                    if state.get("job_stage") == "scanning":
                        state["job_stage"] = "idle"
                latest = self.db.get_latest_job(vmid)
                if latest and latest.get("status") == "interrupted":
                    state.update(
                        {
                            "active_job_id": None,
                            "last_job_id": latest["id"],
                            "operation_status": "failed",
                            "job_stage": "failed",
                            "job_progress": 100,
                            "last_operation_result": "failed",
                            "last_error": latest.get("error")
                            or "Agent restarted while this job was active",
                        }
                    )
                self._save_state(vmid, state)
        finally:
            self._resume_agent_publication()
            self._publish_agent_state_if_changed()

    def _scheduler_loop(self) -> None:
        scheduler = self.settings.monitoring_scheduler
        if self._stop.wait(
            int(
                scheduler.get(
                    "initial_scan_delay_seconds",
                    self.settings.scheduler.get("initial_scan_delay_seconds", 60),
                )
            )
        ):
            return
        interval = max(
            60,
            int(
                scheduler.get(
                    "scan_interval_minutes",
                    self.settings.scheduler.get("scan_interval_minutes", 360),
                )
            )
            * 60,
        )
        while not self._stop.is_set():
            try:
                self.scan_all(operator=False)
            except Exception:
                LOGGER.exception("Scheduled scan failed")
            self._stop.wait(interval)

    def _telemetry_loop(self) -> None:
        if self._stop.wait(
            int(self.settings.scheduler.get("initial_refresh_delay_seconds", 5))
        ):
            return
        interval = max(
            10,
            int(self.settings.scheduler.get("state_refresh_seconds", 30)),
        )
        while not self._stop.is_set():
            try:
                self.refresh_all(operator=False)
            except Exception:
                LOGGER.exception("Telemetry refresh failed")
            self._stop.wait(interval)

    def _notification(self, event_type: str, vmid: int, **extra: Any) -> dict[str, Any]:
        cfg = self._resource(vmid)
        return sanitize_data(
            {
                "event_type": event_type,
                "vmid": vmid,
                "container": cfg.get("name", f"resource-{vmid}"),
                "dashboard_path": cfg.get(
                    "dashboard_path",
                    f"/hubinet-ops/ct-{vmid}",
                ),
                **extra,
            }
        )

    def _notify_ha(self, payload: dict[str, Any]) -> None:
        url = str(self.settings.home_assistant.get("webhook_url", "")).strip()
        if not url:
            return
        try:
            with httpx.Client(
                timeout=float(
                    self.settings.home_assistant.get(
                        "request_timeout_seconds",
                        10,
                    )
                )
            ) as client:
                client.post(url, json=payload).raise_for_status()
        except Exception:
            LOGGER.warning("Home Assistant webhook delivery failed")

    def _utc_second_timestamp(self) -> str:
        current = self._now()
        if current.tzinfo is None or current.utcoffset() is None:
            current = current.replace(tzinfo=UTC)
        return current.astimezone(UTC).replace(microsecond=0).isoformat()

    def _suppress_agent_publication(self) -> None:
        depth = int(getattr(self._agent_publish_context, "depth", 0))
        self._agent_publish_context.depth = depth + 1

    def _resume_agent_publication(self) -> None:
        depth = int(getattr(self._agent_publish_context, "depth", 0))
        if depth <= 0:
            raise RuntimeError("Agent publication suppression is not active")
        self._agent_publish_context.depth = depth - 1

    def _publish_agent_state_if_changed(self) -> bool:
        if int(getattr(self._agent_publish_context, "depth", 0)):
            return False
        with self._agent_publish_lock:
            state = self._agent_state()
            if state == self._last_published_agent_state:
                return False
            self.mqtt.publish_agent_state(state)
            self._last_published_agent_state = dict(state)
            return True

    def _agent_state(self) -> dict[str, Any]:
        with self._agent_publish_lock:
            return {
                "version": VERSION,
                "configured_resource_count": len(self.settings.resources),
                "configured_lxc_count": len(self.settings.containers),
                "configured_qemu_count": sum(
                    1
                    for cfg in self.settings.resources.values()
                    if cfg.get("resource_type") == "qemu"
                ),
                "configured_container_count": len(self.settings.containers),
                "active_job_count": self.db.active_job_count(),
                "mqtt_availability": self.mqtt.availability,
                "last_refresh": self._last_full_refresh,
                "last_resource_refresh": self._last_resource_refresh,
                "resource_refresh_sequence": self._resource_refresh_sequence,
            }

    def _mqtt_snapshot(
        self,
    ) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
        jobs = [
            job
            for vmid in self.settings.resources
            if (job := self.db.get_latest_job(vmid)) is not None
        ]
        return self._agent_state(), self.db.list_resource_states(), jobs

    def _resource(self, vmid: int) -> dict[str, Any]:
        try:
            return self.settings.resources[int(vmid)]
        except KeyError as exc:
            raise KeyError(f"Unknown VMID: {vmid}") from exc

    def _container(self, vmid: int) -> dict[str, Any]:
        """Compatibility helper for callers that require an LXC resource."""
        resource = self._resource(vmid)
        if resource.get("resource_type", "lxc") != "lxc":
            raise KeyError(f"VMID {vmid} is not an LXC resource")
        return resource

    def _capabilities(self, vmid: int) -> dict[str, bool]:
        cfg = self._resource(vmid)
        configured = cfg.get("operator_capabilities") or {}
        names = (
            "refresh",
            "scan",
            "approve",
            "reject",
            "retry_healthcheck",
            "rollback",
            "start",
            "shutdown",
            "reboot",
            "force_stop",
            "snapshot_create",
            "snapshot_list",
            "snapshot_rollback",
            "snapshot_delete",
            "self_update",
        )
        return {
            name: bool(configured.get(name, False))
            for name in names
        }

    def _require_capability(self, vmid: int, capability: str) -> None:
        if not self._capabilities(vmid).get(capability, False):
            raise ValueError(
                f"Operator action {capability} is blocked by policy for resource {vmid}"
            )

    def _monitoring(self, vmid: int) -> dict[str, bool]:
        configured = self._resource(vmid).get("monitoring") or {}
        return {
            "inspect": bool(configured.get("inspect", True)),
            "update_scan": bool(configured.get("update_scan", False)),
        }


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _executor_data(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {}
    data = result.get("data")
    return dict(data) if isinstance(data, dict) else {}


def _snapshot_name(vmid: int, kind: str, stamp: str) -> str:
    physical_kind = "pre" if kind == "pre-update" else "manual"
    name = f"hubinet-ops-{int(vmid)}-{physical_kind}-{stamp}"
    if len(name) > 40 and physical_kind == "manual":
        name = f"hubinet-ops-{int(vmid)}-man-{stamp}"
    if len(name) > 40:
        raise ValueError("Generated snapshot name exceeds the PVE 40 character limit")
    return name


def _fingerprint(data: dict[str, Any]) -> str:
    # Match the managed executor contract: only the ordered package plan is
    # fingerprinted. Volatile fields such as scanned_at must not invalidate approval.
    packages = list(data.get("packages") or [])
    blob = json.dumps(packages, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()


def _risk_for(cfg: dict[str, Any], data: dict[str, Any]) -> str:
    packages = {
        str(item.get("name", "")).split(":", 1)[0]
        for item in data.get("packages", [])
        if isinstance(item, dict)
    }
    high_risk = {
        "systemd",
        "libc6",
        "linux-image-amd64",
        "openssh-server",
        "proxmox-ve",
        "docker-ce",
        "docker-ce-cli",
        "containerd.io",
    }
    if (
        str(cfg.get("criticality", "medium")) in {"critical", "high"}
        or packages & high_risk
    ):
        return "high"
    return "medium" if int(data.get("pending_count", 0)) >= 20 else "low"
