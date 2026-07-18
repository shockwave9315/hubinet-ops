from __future__ import annotations

from pathlib import Path

path = Path("app/service.py")
text = path.read_text(encoding="utf-8")

replacements = [
    (
        '''            state["last_refresh"] = utc_now()\n            state["last_error"] = None\n''',
        '''            state["last_refresh"] = utc_now()\n            # A successful telemetry refresh clears a transient health error, but\n            # must not erase the reason recorded for the last completed operation.\n            if state.get("last_operation_result") is None:\n                state["last_error"] = None\n''',
    ),
    (
        '''    def approve(self, plan_id: str) -> dict[str, Any]:\n        plan, job = self.db.approve_plan(plan_id)\n        vmid = int(plan["vmid"])\n        state = self.get_state(vmid)\n        state.update(\n            {\n                "active_plan_id": plan_id,\n                "active_plan_status": "approved",\n                "active_job_id": job["id"],\n                "operation_status": "running",\n                "job_stage": "preflight",\n                "job_progress": 1,\n                "last_error": None,\n            }\n        )\n        self._save_state(vmid, state)\n        self._notify_ha(self._notification("job_queued", vmid))\n        return {"plan": plan, "job": job}\n''',
        '''    def approve(self, plan_id: str) -> dict[str, Any]:\n        candidate = self.db.get_plan(plan_id)\n        vmid = int(candidate["vmid"])\n        lock = self._scan_locks[vmid]\n        if not lock.acquire(blocking=False):\n            raise ValueError(\n                "A scan is running for this container; retry approval after it finishes"\n            )\n        try:\n            plan, job = self.db.approve_plan(plan_id)\n            state = self.get_state(vmid)\n            state.update(\n                {\n                    "active_plan_id": plan_id,\n                    "active_plan_status": "approved",\n                    "active_job_id": job["id"],\n                    "operation_status": "running",\n                    "job_stage": "preflight",\n                    "job_progress": 1,\n                    "last_error": None,\n                }\n            )\n            self._save_state(vmid, state)\n            self._notify_ha(self._notification("job_queued", vmid))\n            return {"plan": plan, "job": job}\n        finally:\n            lock.release()\n''',
    ),
    (
        '''        self.db.update_job(\n            job["id"],\n            status=job_status,\n            stage=event["stage"],\n            progress=100,\n            error=error,\n        )\n        state = self.get_state(int(job["vmid"]))\n        state.update(\n            {\n                "active_job_id": job["id"],\n                "operation_status": operation,\n                "job_stage": event["stage"],\n                "job_progress": 100,\n                "last_operation_result": result,\n                "last_error": (\n                    sanitize_text(error, limit=2000)\n                    if error\n                    else state.get("last_error")\n                ),\n                "last_job_event": event,\n            }\n        )\n''',
        '''        self.db.update_job(\n            job["id"],\n            status=job_status,\n            stage=event["stage"],\n            progress=100,\n            error=error,\n        )\n        plan = self.db.get_plan(job["plan_id"])\n        plan_status = str(plan["status"])\n        if plan_status == "approved":\n            plan_status = {\n                "success": "completed",\n                "rolled_back": "rolled_back",\n                "blocked": "blocked",\n            }.get(job_status, "failed")\n            self.db.update_plan_status(job["plan_id"], plan_status)\n        state = self.get_state(int(job["vmid"]))\n        state.update(\n            {\n                "active_plan_id": None,\n                "active_plan_status": plan_status,\n                "active_job_id": job["id"],\n                "operation_status": operation,\n                "job_stage": event["stage"],\n                "job_progress": 100,\n                "last_operation_result": result,\n                "last_error": (\n                    sanitize_text(error, limit=2000)\n                    if error\n                    else state.get("last_error")\n                ),\n                "last_job_event": event,\n            }\n        )\n''',
    ),
]

for old, new in replacements:
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"Expected exactly one patch target, found {count}")
    text = text.replace(old, new)

path.write_text(text, encoding="utf-8")
