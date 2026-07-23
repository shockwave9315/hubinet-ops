from __future__ import annotations

import hashlib
import json
import logging
import math
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Callable

import httpx

from .config import Settings
from .contracts import (
    EXECUTOR_PROTOCOL_VERSION,
    EXECUTOR_VERSION,
    evaluate_executor_contract,
    parse_owned_snapshot_name,
)
from .database import Database, utc_now
from .executor import Executor, ExecutorError
from .host_control import HostControlClient, HostControlError
from .mqtt import MqttTelemetry, VERSION
from .security import sanitize_data, sanitize_text
from .stabilization import StabilizationPolicy, Stabilizer
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
        self._reconcile_startup_jobs()
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
            "automatic_rollback": bool(cfg.get("automatic_rollback", False)),
            "manual_rollback_allowed": bool(cfg.get("manual_rollback_allowed", False)),
            "manual_snapshot_restore_allowed": bool(
                cfg.get("manual_snapshot_restore_allowed", False)
            ),
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
        try:
            inspected = _executor_data(self.executor.run("inspect", vmid, timeout=120))
            # Inspect may take long enough for a job to reach a terminal state.
            # Re-read the latest DB state after I/O so telemetry cannot resurrect
            # stale operation/plan/job fields captured before that transition.
            state = self.get_state(vmid)
            state.update(inspected)
            state["health_status"] = inspected.get(
                "health_status",
                inspected.get("health", "unknown"),
            )
            if inspected.get("runtime_status") == "running" or inspected.get("lxc_status") == "running":
                state["intentional_shutdown"] = False
                state["lifecycle_health_pending"] = False
                if state.get("lifecycle_status") != "running":
                    state["expected_lxc_status"] = None
            state["last_refresh"] = utc_now()
            if state.get("last_operation_result") is None:
                state["last_error"] = None
        except ExecutorError as exc:
            state = self.get_state(vmid)
            state.update(
                {
                    "health_status": "offline",
                    "health_score": 0,
                    "last_error": sanitize_text(exc, limit=2000),
                    "last_refresh": utc_now(),
                }
            )
        saved = self._save_state(vmid, state)
        self._observe_health(vmid, str(saved.get("health_status", "unknown")))
        return saved

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
                        and existing.get("operation_type") in {"update", "self_update"}
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
        state.update(
            {
                "active_plan_id": None,
                "active_plan_status": "rejected",
                "risk": "none",
                "operation_status": "idle",
                "job_stage": "idle",
                "job_progress": 0,
            }
        )
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
            if self._plan_type(plan) != "self_update":
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
            for key in ("fingerprint", "release_id", "version")
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

    def _require_compatible_executor(self, vmid: int) -> dict[str, Any]:
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
        self._save_state(vmid, state)
        if not compatibility.compatible:
            installed = compatibility.version or "unknown"
            reasons = "; ".join(compatibility.reasons)
            if executor_error:
                reasons = f"{executor_error}; {reasons}".strip("; ")
            raise ExecutorError(
                f"Executor CT{vmid} is incompatible: required {EXECUTOR_VERSION}/"
                f"protocol {EXECUTOR_PROTOCOL_VERSION}/verify with configured hashes; "
                f"installed {installed}. {reasons}"
            )
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
            self._require_compatible_executor(vmid)
            if not bool(cfg.get("manual_rollback_allowed", False)):
                raise ValueError("Manual rollback is not allowed by resource policy")
            if self.db.get_active_job(vmid) is not None:
                raise ValueError("Another job is already active for this resource")
            source = self.db.get_latest_job(vmid)
            if source is None or not source.get("snapshot_name"):
                raise ValueError("No rollback snapshot is available")
            if source["status"] not in {"failed", "blocked", "interrupted"}:
                raise ValueError("Rollback is only allowed after a failed operation")
            job = self.db.create_manual_rollback_job(source["id"])
            self.db.update_job(job["id"], status="running", stage="rollback", progress=1)
            self._rollback(job, str(source.get("error") or "Manual rollback requested"))
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
        if cfg.get("adapter") == "apt":
            self._require_compatible_executor(vmid)
        elif vmid != 110 or self.host_control is None:
            raise ValueError("CT110 lifecycle requires independent PVE host control")
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
        if cfg.get("resource_type") != "lxc":
            raise ValueError("Snapshots are supported only for LXC resources")
        self._require_capability(vmid, "snapshot_list")
        if self.host_control is not None:
            snapshots = self.host_control.list_snapshots(vmid)
        elif cfg.get("adapter") == "apt":
            snapshots = list(
                _executor_data(self.executor.run("list-snapshots", vmid, timeout=60)).get(
                    "snapshots", []
                )
            )
        else:
            raise ValueError("CT110 snapshots require independent PVE host control")
        snapshots = [
            dict(item) for item in snapshots
            if isinstance(item, dict)
        ]
        snapshots.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        owned = [item for item in snapshots if item.get("owned_by_hubinet_ops") is True]
        latest = owned[0] if owned else {}
        state = self.get_state(vmid)
        state.update(
            {
                "snapshot_count": len(owned),
                "latest_snapshot_name": latest.get("name"),
                "latest_snapshot_at": latest.get("created_at"),
                "latest_snapshot_kind": latest.get("kind"),
            }
        )
        self._save_state(vmid, state)
        return {"snapshots": snapshots, "latest": latest or None}

    def queue_snapshot_create(
        self,
        vmid: int,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        lock = self._scan_locks[vmid]
        if not lock.acquire(blocking=False):
            raise ValueError("A scan or manual operation is active for this resource")
        try:
            return self._queue_snapshot_create_locked(vmid, request_id)
        finally:
            lock.release()

    def _queue_snapshot_create_locked(
        self,
        vmid: int,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        cfg = self._resource(vmid)
        self._require_capability(vmid, "snapshot_create")
        if cfg.get("adapter") == "apt":
            self._require_compatible_executor(vmid)
        elif vmid != 110 or self.host_control is None:
            raise ValueError("CT110 snapshots require independent PVE host control")
        stamp = self._now().astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
        name = f"hubinet-ops-{vmid}-manual-{stamp}"
        job, _ = self.db.create_operation_job(
            vmid=vmid,
            container_name=str(cfg.get("name", f"ct-{vmid}")),
            operation_type="snapshot_create",
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
        self._require_capability(vmid, capability)
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
        if cfg.get("adapter") == "apt":
            self._require_compatible_executor(vmid)
        elif vmid != 110 or self.host_control is None:
            raise ValueError("CT110 snapshots require independent PVE host control")
        job, _ = self.db.create_operation_job(
            vmid=vmid,
            container_name=str(cfg.get("name", f"ct-{vmid}")),
            operation_type=operation_type,
            request_id=request_id or uuid.uuid4().hex,
            snapshot_name=str(selected["name"]),
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
            raise ValueError("Self-update is supported only for CT110")
        if self.host_control is None:
            raise ValueError("CT110 self-update requires independent PVE host control")
        if self.db.get_active_job(vmid) is not None:
            raise ValueError("Another job is already active for this resource")
        release = self._read_self_update_release(vmid)
        fingerprint = str(release["fingerprint"])
        active = self.db.find_active_plan(vmid, fingerprint)
        if active is not None:
            if self._plan_type(active) != "self_update":
                raise ValueError("Resolve the active plan before CT110 self-update")
            if active.get("status") != "waiting_approval":
                raise ValueError("Approved self-update plan is already pending execution")
            status = "existing_plan"
        else:
            other = self.db.find_active_plan(vmid)
            if other is not None:
                raise ValueError("Resolve the active plan before CT110 self-update")
            payload = {
                "plan_type": "self_update",
                "version": str(release["version"]),
                "release_id": str(release["release_id"]),
                "fingerprint": fingerprint,
                "file_count": release.get("file_count"),
                "total_bytes": release.get("total_bytes"),
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
        try:
            release = self.host_control.inspect_self_update_release(vmid)
        except HostControlError as exc:
            raise ValueError(f"Cannot inspect staged self-update release: {exc}") from exc
        required = ("version", "release_id", "fingerprint")
        if not all(isinstance(release.get(key), str) and release[key] for key in required):
            raise ValueError("Staged self-update release identity is invalid")
        fingerprint = str(release["fingerprint"])
        if len(fingerprint) != 64 or any(
            character not in "0123456789abcdef" for character in fingerprint
        ):
            raise ValueError("Staged self-update release fingerprint is invalid")
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
            succeeded = False
            result: dict[str, Any] | None = None
            error: Exception | None = None
            try:
                if operation_type in {
                    "lifecycle_start", "lifecycle_shutdown", "lifecycle_force_stop"
                }:
                    status = self._host_status(int(job["vmid"]))
                    actual = str(status.get("lxc_status") or status.get("runtime_status"))
                    expected = "running" if operation_type == "lifecycle_start" else "stopped"
                    succeeded = actual == expected
                    result = {"runtime_status": actual, "reconciled": True}
                elif operation_type in {"snapshot_create", "snapshot_delete"}:
                    listing = self.list_snapshots(int(job["vmid"]))
                    exists = any(
                        item.get("name") == job.get("snapshot_name")
                        for item in listing["snapshots"]
                    )
                    succeeded = exists if operation_type == "snapshot_create" else not exists
                    result = {"snapshot_exists": exists, "reconciled": True}
                elif operation_type == "self_update":
                    if self.host_control is None:
                        raise ValueError(
                            "CT110 self-update reconciliation requires PVE host control"
                        )
                    release = self._approved_self_update_release(job)
                    result = self.host_control.execute(
                        "self_update",
                        int(job["vmid"]),
                        str(job["request_id"]),
                        release_fingerprint=str(release["fingerprint"]),
                    )
                    succeeded = True
            except Exception as exc:
                error = exc
                LOGGER.warning("Startup reconciliation failed for job %s: %s", job["id"], exc)
            if succeeded:
                self.db.update_job(job["id"], result=result or {})
                self._terminal(job, "success", "success", None)
            elif operation_type == "self_update" and isinstance(error, HostControlError):
                host_error = error
                if host_error.result:
                    self.db.update_job(job["id"], result=host_error.result)
                self._terminal(
                    job,
                    "interrupted" if host_error.status == "interrupted" else "failed",
                    "failed",
                    str(host_error),
                )
            else:
                self._terminal(
                    job,
                    "interrupted",
                    "failed",
                    "Agent restarted during the operation; it was not replayed",
                )

    def _run_job(self, job: dict[str, Any]) -> None:
        if str(job.get("operation_type") or "update") != "update":
            self._run_operation_job(job)
            return
        vmid = int(job["vmid"])
        cfg = self._resource(vmid)
        policy = StabilizationPolicy.from_config(cfg.get("stabilization"))
        auto_rollback = bool(cfg.get("automatic_rollback", False))
        snapshot = (
            f"hubinet-ops-{vmid}-pre-update-"
            f"{self._now().astimezone(UTC).strftime('%Y%m%dT%H%M%SZ')}"
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
            if auto_rollback:
                self.db.update_job(job["id"], snapshot_name=snapshot)
                emit(
                    stage="snapshot",
                    progress=20,
                    event_type="snapshot_started",
                    message="Creating rollback snapshot",
                )
                self._execute("snapshot", vmid, 600, emit, snapshot)
                self._enforce_snapshot_retention(vmid, job)
                emit(
                    stage="snapshot",
                    progress=25,
                    event_type="snapshot_created",
                    message="Rollback snapshot created",
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
                    "snapshot_name": snapshot if auto_rollback else None,
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
            self._terminal(job, "success", "success", None)
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
            "snapshot_rollback": "snapshot_rollback",
            "snapshot_delete": "snapshot_deleting",
            "retry_healthcheck": "healthcheck",
            "self_update": "self_updating",
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
            else:
                result = self._execute_host_operation(job)
            if operation_type == "snapshot_create":
                self._enforce_snapshot_retention(vmid, job)
            self.db.update_job(job["id"], result=result)
            state = self.get_state(vmid)
            if operation_type.startswith("lifecycle_"):
                runtime = str(result.get("lxc_status") or result.get("runtime_status") or "unknown")
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
                self.list_snapshots(vmid)
            elif operation_type == "retry_healthcheck":
                state.update(result)
                state["health_status"] = result.get(
                    "health_status", result.get("health", "healthy")
                )
                state["last_refresh"] = self._utc_second_timestamp()
                self._save_state(vmid, state)
            else:
                self._save_state(vmid, state)
            self._terminal(job, "success", "success", None)
        except (ExecutorError, HostControlError, ValueError) as exc:
            if isinstance(exc, HostControlError) and exc.result:
                self.db.update_job(job["id"], result=exc.result)
            state = self.get_state(vmid)
            if operation_type == "retry_healthcheck" and isinstance(exc, ExecutorError):
                if exc.data:
                    state.update(exc.data)
                    state["health_status"] = exc.data.get(
                        "health_status", exc.data.get("health", "critical")
                    )
            if operation_type.startswith("lifecycle_"):
                state.update(
                    {
                        "lifecycle_status": "failed",
                        "lifecycle_finished_at": self._utc_second_timestamp(),
                        "lifecycle_error": sanitize_text(exc, limit=2000),
                    }
                )
            if operation_type.startswith("snapshot_"):
                state["snapshot_operation_status"] = "failed"
            self._save_state(vmid, state)
            self._terminal(job, "failed", "failed", str(exc))

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

    def _execute_host_operation(self, job: dict[str, Any]) -> dict[str, Any]:
        operation_type = str(job["operation_type"])
        vmid = int(job["vmid"])
        snapshot_name = job.get("snapshot_name")
        if self.host_control is not None:
            if operation_type == "self_update":
                release = self._validate_self_update_plan(job)
                return self.host_control.execute(
                    operation_type,
                    vmid,
                    str(job["request_id"]),
                    release_fingerprint=str(release["fingerprint"]),
                )
            return self.host_control.execute(
                operation_type,
                vmid,
                str(job["request_id"]),
                snapshot_name=str(snapshot_name) if snapshot_name else None,
            )
        cfg = self._resource(vmid)
        if cfg.get("adapter") != "apt":
            raise ValueError("Independent PVE host control is required")
        action = {
            "lifecycle_start": "start",
            "lifecycle_shutdown": "shutdown",
            "lifecycle_reboot": "reboot",
            "lifecycle_force_stop": "force-stop",
            "snapshot_create": "snapshot-create",
            "snapshot_rollback": "snapshot-rollback",
            "snapshot_delete": "snapshot-delete",
        }.get(operation_type)
        if action is None:
            raise ValueError("Operation requires independent PVE host control")
        return _executor_data(
            self.executor.run(
                action,
                vmid,
                str(snapshot_name) if snapshot_name else None,
                timeout=1800,
            )
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

    def _raw_snapshot_list(self, vmid: int) -> list[dict[str, Any]]:
        if self.host_control is not None:
            return self.host_control.list_snapshots(vmid)
        return list(
            _executor_data(self.executor.run("list-snapshots", vmid, timeout=60)).get(
                "snapshots", []
            )
        )

    def _enforce_snapshot_retention(self, vmid: int, job: dict[str, Any]) -> None:
        retention = max(1, int(self._resource(vmid).get("snapshot_retention", 5)))
        snapshots = [
            dict(item)
            for item in self._raw_snapshot_list(vmid)
            if isinstance(item, dict) and item.get("owned_by_hubinet_ops") is True
        ]
        snapshots.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        protected = self.db.rollback_source_snapshots(vmid)
        protected.add(str(job.get("snapshot_name") or ""))
        for index, snapshot in enumerate(snapshots[retention:], start=retention):
            name = str(snapshot.get("name") or "")
            if not name or name in protected or not snapshot.get("delete_eligible"):
                continue
            if self.host_control is not None:
                self.host_control.execute(
                    "snapshot_delete",
                    vmid,
                    f"retention-{str(job['id'])[:32]}-{index}",
                    snapshot_name=name,
                )
            else:
                self.executor.run("snapshot-delete", vmid, name, timeout=300)

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
            emit(
                stage="rollback",
                progress=88,
                level="warning",
                event_type="rollback_started",
                message="Rollback started",
            )
            self._execute("rollback", vmid, 1200, emit, snapshot)
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
            self._terminal(job, "rolled_back", "rolled_back", cause)
            self._notify_ha(self._notification("job_rolled_back", vmid))
        except ExecutorError as rollback_error:
            error = f"Original failure: {cause}; rollback failure: {rollback_error}"
            self.db.update_plan_status(job["plan_id"], "failed")
            self._terminal(job, "failed", "manual_intervention", error)
            self._notify_ha(
                self._notification(
                    "manual_intervention_required",
                    vmid,
                    error=error,
                )
            )

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
        operation = {
            "success": "success",
            "rolled_back": "rolled_back",
            "manual_intervention": "manual_intervention",
            "failed": "failed",
        }[result]
        event = self.db.insert_job_event(
            job_id=job["id"],
            vmid=int(job["vmid"]),
            level="error" if result in {"failed", "manual_intervention"} else "info",
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
                "active_job_id": None,
                "last_job_id": job["id"],
                "operation_type": job.get("operation_type", "update"),
                "operation_status": operation,
                "job_stage": event["stage"],
                "job_progress": 100,
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
            emit(
                stage=action_stage,
                progress=_safe_int(
                    item.get("progress"),
                    STAGE_PROGRESS.get(action_stage, 0),
                ),
                level=str(item.get("level", "info")),
                event_type=str(item.get("event_type", "executor_event")),
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

    def _save_state(self, vmid: int, state: dict[str, Any]) -> dict[str, Any]:
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
            state.update(
                {
                    "active_job_id": active_job["id"],
                    "operation_type": active_job.get("operation_type", "update"),
                    "operation_status": "running",
                    "job_stage": active_job["stage"],
                    "job_progress": active_job.get("progress", 0),
                }
            )
        if cfg.get("adapter") == "agent_self":
            agent_summary = self._agent_state()
            agent_summary.pop("last_refresh", None)
            state.update(agent_summary)
            state["mqtt_availability"] = self.mqtt.availability
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
