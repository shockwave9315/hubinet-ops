from __future__ import annotations

import hashlib
import json
import logging
import threading
from datetime import UTC, datetime
from typing import Any

import httpx

from .config import Settings
from .database import Database, utc_now
from .executor import Executor, ExecutorError

LOGGER = logging.getLogger("hubinet_ops")

STAGE_PROGRESS = {
    "idle": 0,
    "queued": 5,
    "preflight": 15,
    "snapshot": 30,
    "updating": 55,
    "healthcheck": 80,
    "repairing": 85,
    "rolling_back": 70,
    "rollback_healthcheck": 90,
    "completed": 100,
    "rolled_back": 100,
    "recovered_without_rollback": 100,
    "manual_intervention": 100,
    "interrupted": 100,
}


class OpsService:
    def __init__(self, settings: Settings, db: Database, executor: Executor):
        self.settings = settings
        self.db = db
        self.executor = executor
        self._stop = threading.Event()
        self._scan_lock = threading.Lock()
        self._worker = threading.Thread(target=self._worker_loop, name="ops-worker", daemon=True)
        self._scheduler = threading.Thread(target=self._scheduler_loop, name="ops-scheduler", daemon=True)
        self._telemetry = threading.Thread(target=self._telemetry_loop, name="ops-telemetry", daemon=True)

    def start(self) -> None:
        self._ensure_initial_states()
        self._worker.start()
        self._telemetry.start()
        if bool(self.settings.scheduler.get("enabled", True)):
            self._scheduler.start()

    def stop(self) -> None:
        self._stop.set()
        self._worker.join(timeout=5)
        self._telemetry.join(timeout=5)
        if self._scheduler.is_alive():
            self._scheduler.join(timeout=5)

    def list_containers(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for vmid, cfg in sorted(self.settings.containers.items()):
            item = {
                "vmid": vmid,
                "name": cfg.get("name", f"ct-{vmid}"),
                "enabled": bool(cfg.get("enabled", False)),
                "adapter": cfg.get("adapter", "apt"),
                "criticality": cfg.get("criticality", "medium"),
                "approval_mode": cfg.get("approval_mode", "always"),
                "automatic_rollback": bool(cfg.get("automatic_rollback", False)),
                "dashboard_path": cfg.get("dashboard_path", f"/hubinet-ops/ct-{vmid}"),
            }
            state = self.db.get_container_state(vmid)
            if state:
                item["state"] = state
            result.append(item)
        return result

    def list_states(self) -> dict[str, Any]:
        return {
            "version": "0.2.0",
            "generated_at": utc_now(),
            "containers": {str(item["vmid"]): item for item in self.db.list_container_states()},
        }

    def get_state(self, vmid: int) -> dict[str, Any]:
        self._container(vmid)
        state = self.db.get_container_state(vmid)
        if state is None:
            state = self._base_state(vmid)
            state = self.db.upsert_container_state(vmid, state)
        return state

    def refresh_container(self, vmid: int) -> dict[str, Any]:
        cfg = self._container(vmid)
        state = self.get_state(vmid)
        if not bool(cfg.get("enabled", False)):
            state.update({"status": "disabled", "health": "unknown", "health_score": 0})
            return self.db.upsert_container_state(vmid, self._decorate_state(vmid, state))

        try:
            result = self.executor.run("inspect", vmid, timeout=120)
            inspected = result.get("data", {})
            state.update(inspected)
            state["last_refresh"] = utc_now()
            state["last_error"] = None
        except ExecutorError as exc:
            state.update(
                {
                    "health": "critical",
                    "health_score": 0,
                    "last_error": str(exc),
                    "last_refresh": utc_now(),
                }
            )
        state = self._decorate_state(vmid, state)
        return self.db.upsert_container_state(vmid, state)

    def refresh_all(self) -> list[dict[str, Any]]:
        outcomes = []
        for vmid, cfg in sorted(self.settings.containers.items()):
            if bool(cfg.get("enabled", False)):
                outcomes.append(self.refresh_container(vmid))
        return outcomes

    def scan_all(self) -> list[dict[str, Any]]:
        if not self._scan_lock.acquire(blocking=False):
            return [{"status": "skipped", "reason": "scan_already_running"}]
        try:
            outcomes = []
            for vmid, cfg in sorted(self.settings.containers.items()):
                if not bool(cfg.get("enabled", False)):
                    continue
                outcomes.append(self.scan_container(vmid))
            return outcomes
        finally:
            self._scan_lock.release()

    def scan_container(self, vmid: int) -> dict[str, Any]:
        cfg = self._container(vmid)
        if not bool(cfg.get("enabled", False)):
            return {"vmid": vmid, "status": "disabled"}
        if cfg.get("adapter", "apt") != "apt":
            return {"vmid": vmid, "status": "unsupported_adapter", "adapter": cfg.get("adapter")}

        state = self.get_state(vmid)
        state.update({"status": "scanning", "last_error": None})
        self.db.upsert_container_state(vmid, self._decorate_state(vmid, state))

        try:
            scan = self.executor.run("scan", vmid, timeout=300)
        except ExecutorError as exc:
            state.update(
                {
                    "status": "scan_failed",
                    "last_error": str(exc),
                    "last_scan": utc_now(),
                }
            )
            self.db.upsert_container_state(vmid, self._decorate_state(vmid, state))
            event = {
                "event_type": "scan_failed",
                "vmid": vmid,
                "container": cfg.get("name", f"ct-{vmid}"),
                "dashboard_path": cfg.get("dashboard_path", f"/hubinet-ops/ct-{vmid}"),
                "error": str(exc),
            }
            self._notify_ha(event)
            return {"vmid": vmid, "status": "error", "error": str(exc)}

        data = scan.get("data", {})
        count = int(data.get("pending_count", 0))
        state.update(
            {
                "updates": data,
                "pending_updates": count,
                "last_scan": utc_now(),
                "last_error": None,
            }
        )
        if count <= 0:
            self.db.invalidate_active_plans(vmid)
            state.update(
                {
                    "risk": "none",
                    "active_plan_id": None,
                    "active_plan_status": None,
                }
            )
            state = self._decorate_state(vmid, state)
            self.db.upsert_container_state(vmid, state)
            return {"vmid": vmid, "status": "up_to_date", "data": data}

        fingerprint = str(data.get("fingerprint") or _fingerprint(data))
        active = self.db.find_active_plan(vmid, fingerprint)
        if active:
            state.update(
                {
                    "risk": active["risk"],
                    "active_plan_id": active["id"],
                    "active_plan_status": active["status"],
                }
            )
            self.db.upsert_container_state(vmid, self._decorate_state(vmid, state))
            return {"vmid": vmid, "status": "existing_plan", "plan": active}

        risk = _risk_for(cfg, data)
        ttl = int(self.settings.scheduler.get("approval_ttl_minutes", 1440))
        plan = self.db.create_plan(
            vmid=vmid,
            container_name=str(cfg.get("name", f"ct-{vmid}")),
            fingerprint=fingerprint,
            risk=risk,
            payload=data,
            ttl_minutes=ttl,
        )
        state.update(
            {
                "risk": risk,
                "active_plan_id": plan["id"],
                "active_plan_status": plan["status"],
            }
        )
        self.db.upsert_container_state(vmid, self._decorate_state(vmid, state))
        self._notify_ha(
            {
                "event_type": "approval_required",
                "plan_id": plan["id"],
                "vmid": vmid,
                "container": plan["container_name"],
                "risk": risk,
                "pending_count": count,
                "packages": data.get("packages", [])[:10],
                "expires_at": plan["expires_at"],
                "dashboard_path": cfg.get("dashboard_path", f"/hubinet-ops/ct-{vmid}"),
            }
        )
        return {"vmid": vmid, "status": "plan_created", "plan": plan}

    def approve(self, plan_id: str) -> dict[str, Any]:
        plan, job = self.db.approve_plan(plan_id)
        vmid = int(plan["vmid"])
        state = self.get_state(vmid)
        state.update(
            {
                "active_plan_id": plan_id,
                "active_plan_status": "approved",
                "active_job_id": job["id"],
                "job_status": "queued",
                "job_stage": "queued",
                "job_progress": STAGE_PROGRESS["queued"],
                "last_error": None,
            }
        )
        self.db.upsert_container_state(vmid, self._decorate_state(vmid, state))
        self._notify_ha(
            {
                "event_type": "job_queued",
                "plan_id": plan_id,
                "job_id": job["id"],
                "vmid": vmid,
                "container": plan["container_name"],
                "dashboard_path": self._container(vmid).get("dashboard_path", f"/hubinet-ops/ct-{vmid}"),
            }
        )
        return {"plan": plan, "job": job}

    def reject(self, plan_id: str) -> dict[str, Any]:
        plan = self.db.reject_plan(plan_id)
        vmid = int(plan["vmid"])
        state = self.get_state(vmid)
        state.update(
            {
                "active_plan_id": None,
                "active_plan_status": "rejected",
                "risk": "none",
            }
        )
        self.db.upsert_container_state(vmid, self._decorate_state(vmid, state))
        return {"plan": plan}

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            job = self.db.next_queued_job()
            if not job:
                self._stop.wait(1.0)
                continue
            try:
                self._run_job(job)
            except Exception:
                LOGGER.exception("Unhandled worker failure for job %s", job.get("id"))
                self.db.update_job(
                    job["id"], status="failed", stage="internal_error", error="Unhandled worker error"
                )
                self._set_job_state(job, status="failed", stage="internal_error", error="Unhandled worker error")

    def _run_job(self, job: dict[str, Any]) -> None:
        job_id = job["id"]
        vmid = int(job["vmid"])
        cfg = self._container(vmid)
        container = job["container_name"]
        auto_rollback = bool(cfg.get("automatic_rollback", False))
        snapshot_name = f"ops-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}-{job_id[:6]}"

        self._set_job_state(job, status="running", stage="preflight")
        self._notify_ha(
            {
                "event_type": "job_started",
                "job_id": job_id,
                "vmid": vmid,
                "container": container,
                "dashboard_path": cfg.get("dashboard_path", f"/hubinet-ops/ct-{vmid}"),
            }
        )

        try:
            self.db.update_job(job_id, stage="preflight")
            self._set_job_state(job, status="running", stage="preflight")
            preflight = self.executor.run("preflight", vmid, timeout=300)

            if auto_rollback:
                self.db.update_job(job_id, stage="snapshot", snapshot_name=snapshot_name)
                self._set_job_state(job, status="running", stage="snapshot", snapshot_name=snapshot_name)
                self.executor.run("snapshot", vmid, snapshot_name, timeout=600)

            self.db.update_job(job_id, stage="updating")
            self._set_job_state(job, status="running", stage="updating")
            update = self.executor.run("update", vmid, timeout=3900)

            self.db.update_job(job_id, stage="healthcheck")
            self._set_job_state(job, status="running", stage="healthcheck")
            health = self.executor.run("healthcheck", vmid, timeout=300)
            post_scan = self.executor.run("scan", vmid, timeout=300)

            result = {
                "preflight": preflight,
                "update": update,
                "healthcheck": health,
                "post_scan": post_scan,
            }
            self.db.update_job(job_id, status="success", stage="completed", result=result)
            self.db.update_plan_status(job["plan_id"], "completed")
            state = self.get_state(vmid)
            updates = post_scan.get("data", {})
            state.update(
                {
                    **health.get("data", {}),
                    "updates": updates,
                    "pending_updates": int(updates.get("pending_count", 0)),
                    "active_plan_id": None,
                    "active_plan_status": "completed",
                    "active_job_id": job_id,
                    "job_status": "success",
                    "job_stage": "completed",
                    "job_progress": 100,
                    "last_update": utc_now(),
                    "last_error": None,
                    "snapshot_name": snapshot_name if auto_rollback else None,
                }
            )
            self.db.upsert_container_state(vmid, self._decorate_state(vmid, state))
            self._notify_ha(
                {
                    "event_type": "job_success",
                    "job_id": job_id,
                    "vmid": vmid,
                    "container": container,
                    "snapshot_name": snapshot_name if auto_rollback else None,
                    "reboot_required": bool(update.get("data", {}).get("reboot_required", False)),
                    "dashboard_path": cfg.get("dashboard_path", f"/hubinet-ops/ct-{vmid}"),
                }
            )
        except ExecutorError as exc:
            failed_stage = self.db.get_job(job_id)["stage"]
            LOGGER.error("Job %s failed at %s: %s", job_id, failed_stage, exc)

            if failed_stage in {"preflight", "snapshot"}:
                self.db.update_job(job_id, status="blocked", stage=failed_stage, error=str(exc))
                self.db.update_plan_status(job["plan_id"], "blocked")
                self._set_job_state(job, status="blocked", stage=failed_stage, error=str(exc))
                self._notify_ha(
                    {
                        "event_type": "job_blocked",
                        "job_id": job_id,
                        "vmid": vmid,
                        "container": container,
                        "stage": failed_stage,
                        "error": str(exc),
                        "dashboard_path": cfg.get("dashboard_path", f"/hubinet-ops/ct-{vmid}"),
                    }
                )
            elif auto_rollback:
                self._rollback(
                    job_id,
                    vmid,
                    container,
                    snapshot_name,
                    str(exc),
                    allow_repair=(failed_stage == "healthcheck"),
                )
            else:
                self.db.update_job(job_id, status="failed", stage="manual_intervention", error=str(exc))
                self.db.update_plan_status(job["plan_id"], "failed")
                self._set_job_state(job, status="failed", stage="manual_intervention", error=str(exc))
                self._notify_ha(
                    {
                        "event_type": "manual_intervention_required",
                        "job_id": job_id,
                        "vmid": vmid,
                        "container": container,
                        "error": str(exc),
                        "dashboard_path": cfg.get("dashboard_path", f"/hubinet-ops/ct-{vmid}"),
                    }
                )

    def _rollback(
        self,
        job_id: str,
        vmid: int,
        container: str,
        snapshot_name: str,
        cause: str,
        *,
        allow_repair: bool,
    ) -> None:
        cfg = self._container(vmid)
        job = self.db.get_job(job_id)
        try:
            if allow_repair:
                self.db.update_job(job_id, stage="repairing", error=cause)
                self._set_job_state(job, status="running", stage="repairing", error=cause)
                try:
                    self.executor.run("repair", vmid, timeout=300)
                    health = self.executor.run("healthcheck", vmid, timeout=300)
                    self.db.update_job(job_id, status="recovered", stage="recovered_without_rollback")
                    self.db.update_plan_status(job["plan_id"], "recovered")
                    state = self.get_state(vmid)
                    state.update(
                        {
                            **health.get("data", {}),
                            "job_status": "recovered",
                            "job_stage": "recovered_without_rollback",
                            "job_progress": 100,
                            "last_error": cause,
                        }
                    )
                    self.db.upsert_container_state(vmid, self._decorate_state(vmid, state))
                    self._notify_ha(
                        {
                            "event_type": "job_recovered",
                            "job_id": job_id,
                            "vmid": vmid,
                            "container": container,
                            "original_error": cause,
                            "dashboard_path": cfg.get("dashboard_path", f"/hubinet-ops/ct-{vmid}"),
                        }
                    )
                    return
                except ExecutorError:
                    pass

            self.db.update_job(job_id, stage="rolling_back")
            self._set_job_state(job, status="running", stage="rolling_back", error=cause)
            self.executor.run("rollback", vmid, snapshot_name, timeout=1200)
            self.db.update_job(job_id, stage="rollback_healthcheck")
            self._set_job_state(job, status="running", stage="rollback_healthcheck", error=cause)
            health = self.executor.run("healthcheck", vmid, timeout=300)
            self.db.update_job(
                job_id,
                status="rolled_back",
                stage="rolled_back",
                result={"rollback_healthcheck": health},
                error=cause,
            )
            self.db.update_plan_status(job["plan_id"], "rolled_back")
            state = self.get_state(vmid)
            state.update(
                {
                    **health.get("data", {}),
                    "job_status": "rolled_back",
                    "job_stage": "rolled_back",
                    "job_progress": 100,
                    "last_error": cause,
                    "snapshot_name": snapshot_name,
                }
            )
            self.db.upsert_container_state(vmid, self._decorate_state(vmid, state))
            self._notify_ha(
                {
                    "event_type": "job_rolled_back",
                    "job_id": job_id,
                    "vmid": vmid,
                    "container": container,
                    "snapshot_name": snapshot_name,
                    "original_error": cause,
                    "dashboard_path": cfg.get("dashboard_path", f"/hubinet-ops/ct-{vmid}"),
                }
            )
        except ExecutorError as rollback_exc:
            error = f"Original failure: {cause}; rollback failure: {rollback_exc}"
            self.db.update_job(job_id, status="failed", stage="manual_intervention", error=error)
            self.db.update_plan_status(job["plan_id"], "failed")
            self._set_job_state(job, status="failed", stage="manual_intervention", error=error)
            self._notify_ha(
                {
                    "event_type": "manual_intervention_required",
                    "job_id": job_id,
                    "vmid": vmid,
                    "container": container,
                    "error": error,
                    "dashboard_path": cfg.get("dashboard_path", f"/hubinet-ops/ct-{vmid}"),
                }
            )

    def _scheduler_loop(self) -> None:
        initial = int(self.settings.scheduler.get("initial_scan_delay_seconds", 60))
        if self._stop.wait(initial):
            return
        interval = max(60, int(self.settings.scheduler.get("scan_interval_minutes", 360)) * 60)
        while not self._stop.is_set():
            try:
                self.scan_all()
            except Exception:
                LOGGER.exception("Scheduled scan failed")
            if self._stop.wait(interval):
                return

    def _telemetry_loop(self) -> None:
        initial = int(self.settings.scheduler.get("initial_refresh_delay_seconds", 5))
        if self._stop.wait(initial):
            return
        interval = max(10, int(self.settings.scheduler.get("state_refresh_seconds", 30)))
        while not self._stop.is_set():
            try:
                self.refresh_all()
            except Exception:
                LOGGER.exception("Telemetry refresh failed")
            if self._stop.wait(interval):
                return

    def _notify_ha(self, payload: dict[str, Any]) -> None:
        url = str(self.settings.home_assistant.get("webhook_url", "")).strip()
        if not url:
            return
        timeout = float(self.settings.home_assistant.get("request_timeout_seconds", 10))
        try:
            with httpx.Client(timeout=timeout) as client:
                response = client.post(url, json=payload)
                response.raise_for_status()
        except Exception as exc:
            LOGGER.warning("Home Assistant webhook failed: %s", exc)

    def _ensure_initial_states(self) -> None:
        for vmid in self.settings.containers:
            if self.db.get_container_state(vmid) is None:
                self.db.upsert_container_state(vmid, self._base_state(vmid))

    def _base_state(self, vmid: int) -> dict[str, Any]:
        cfg = self._container(vmid)
        return {
            "vmid": vmid,
            "name": cfg.get("name", f"ct-{vmid}"),
            "enabled": bool(cfg.get("enabled", False)),
            "adapter": cfg.get("adapter", "apt"),
            "criticality": cfg.get("criticality", "medium"),
            "dashboard_path": cfg.get("dashboard_path", f"/hubinet-ops/ct-{vmid}"),
            "status": "initializing" if cfg.get("enabled", False) else "disabled",
            "health": "unknown",
            "health_score": 0,
            "pending_updates": 0,
            "updates": {"pending_count": 0, "packages": []},
            "risk": "none",
            "active_plan_id": None,
            "active_plan_status": None,
            "active_job_id": None,
            "job_status": None,
            "job_stage": "idle",
            "job_progress": 0,
            "last_scan": None,
            "last_refresh": None,
            "last_update": None,
            "last_error": None,
        }

    def _decorate_state(self, vmid: int, state: dict[str, Any]) -> dict[str, Any]:
        cfg = self._container(vmid)
        state.update(
            {
                "vmid": vmid,
                "name": cfg.get("name", f"ct-{vmid}"),
                "enabled": bool(cfg.get("enabled", False)),
                "adapter": cfg.get("adapter", "apt"),
                "criticality": cfg.get("criticality", "medium"),
                "dashboard_path": cfg.get("dashboard_path", f"/hubinet-ops/ct-{vmid}"),
            }
        )
        active_job = self.db.get_active_job(vmid)
        active_plan = self.db.find_active_plan(vmid)
        if active_job:
            state.update(
                {
                    "active_job_id": active_job["id"],
                    "job_status": active_job["status"],
                    "job_stage": active_job["stage"],
                    "job_progress": STAGE_PROGRESS.get(active_job["stage"], 0),
                }
            )
        if active_plan:
            state.update(
                {
                    "active_plan_id": active_plan["id"],
                    "active_plan_status": active_plan["status"],
                    "risk": active_plan["risk"],
                }
            )

        if not state.get("enabled"):
            state["status"] = "disabled"
        elif active_job:
            state["status"] = active_job["stage"]
        elif active_plan and active_plan["status"] == "waiting_approval":
            state["status"] = "waiting_approval"
        elif state.get("last_error") and state.get("health") == "critical":
            state["status"] = "error"
        elif int(state.get("pending_updates", 0) or 0) > 0:
            state["status"] = "update_available"
        elif state.get("health") in {"critical", "degraded"}:
            state["status"] = state.get("health")
        elif state.get("health") == "offline" or state.get("lxc_status") == "stopped":
            state["status"] = "offline"
        elif state.get("health") == "healthy":
            state["status"] = "healthy"
        else:
            state["status"] = state.get("status", "unknown")
        return state

    def _set_job_state(
        self,
        job: dict[str, Any],
        *,
        status: str,
        stage: str,
        error: str | None = None,
        snapshot_name: str | None = None,
    ) -> None:
        vmid = int(job["vmid"])
        state = self.get_state(vmid)
        state.update(
            {
                "active_job_id": job["id"],
                "job_status": status,
                "job_stage": stage,
                "job_progress": STAGE_PROGRESS.get(stage, 0),
                "last_error": error,
            }
        )
        if snapshot_name:
            state["snapshot_name"] = snapshot_name
        self.db.upsert_container_state(vmid, self._decorate_state(vmid, state))

    def _container(self, vmid: int) -> dict[str, Any]:
        try:
            return self.settings.containers[int(vmid)]
        except KeyError as exc:
            raise KeyError(f"Unknown VMID: {vmid}") from exc


def _fingerprint(data: dict[str, Any]) -> str:
    blob = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()


def _risk_for(cfg: dict[str, Any], data: dict[str, Any]) -> str:
    criticality = str(cfg.get("criticality", "medium"))
    packages = {str(p.get("name", "")) for p in data.get("packages", []) if isinstance(p, dict)}
    high_risk = {
        "systemd",
        "libc6",
        "linux-image-amd64",
        "openssh-server",
        "proxmox-ve",
        "docker-ce",
        "containerd.io",
    }
    if criticality in {"critical", "high"} or packages.intersection(high_risk):
        return "high"
    if int(data.get("pending_count", 0)) >= 20:
        return "medium"
    return "low"
