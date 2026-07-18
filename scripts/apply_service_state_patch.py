from __future__ import annotations

from pathlib import Path

path = Path("app/service.py")
text = path.read_text(encoding="utf-8")

replacements = [
    (
        '''        state.update(\n            {\n                "update_status": "scanning",\n                "job_stage": "scanning",\n                "last_error": None,\n            }\n        )\n''',
        '''        state.update(\n            {\n                "update_status": "scanning",\n                "job_stage": "scanning",\n            }\n        )\n        if state.get("last_operation_result") is None:\n            state["last_error"] = None\n''',
    ),
    (
        '''                "last_scan": utc_now(),\n                "last_error": None,\n                "operation_status": prior_operation,\n''',
        '''                "last_scan": utc_now(),\n                "operation_status": prior_operation,\n''',
    ),
    (
        '''                    "job_progress": 1,\n                    "last_error": None,\n''',
        '''                    "job_progress": 1,\n                    "last_operation_result": None,\n                    "last_error": None,\n''',
    ),
    (
        '''    def retry_healthcheck(self, vmid: int) -> dict[str, Any]:\n        cfg = self._container(vmid)\n        latest = self.db.get_latest_job(vmid)\n        if latest is None:\n            raise ValueError("No job is available for retry")\n''',
        '''    def retry_healthcheck(self, vmid: int) -> dict[str, Any]:\n        cfg = self._container(vmid)\n        if self.db.get_active_job(vmid) is not None:\n            raise ValueError("Another job is already active for this container")\n        latest = self.db.get_latest_job(vmid)\n        if latest is None:\n            raise ValueError("No job is available for retry")\n        if latest.get("status") not in {"failed", "blocked", "interrupted"}:\n            raise ValueError("Healthcheck retry is only allowed after a failed operation")\n''',
    ),
    (
        '''                    "operation_status": "running",\n                    "job_stage": event["stage"],\n                    "job_progress": event["progress"],\n                    "last_job_event": event,\n''',
        '''                    "operation_status": "running",\n                    "job_stage": event["stage"],\n                    "job_progress": event["progress"],\n                    "last_operation_result": None,\n                    "last_error": None,\n                    "last_job_event": event,\n''',
    ),
]

for old, new in replacements:
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"Expected exactly one patch target, found {count}")
    text = text.replace(old, new)

path.write_text(text, encoding="utf-8")
