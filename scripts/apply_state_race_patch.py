from __future__ import annotations

from pathlib import Path

service_path = Path("app/service.py")
service = service_path.read_text(encoding="utf-8")
old_refresh = '''    def refresh_container(self, vmid: int) -> dict[str, Any]:
        cfg = self._container(vmid)
        state = self.get_state(vmid)
        if not bool(cfg.get("enabled", False)):
            state.update({"health_status": "unknown", "health_score": 0})
            return self._save_state(vmid, state)
        try:
            inspected = self.executor.run("inspect", vmid, timeout=120).get("data", {})
            state.update(inspected)
            state["health_status"] = inspected.get(
                "health_status",
                inspected.get("health", "unknown"),
            )
            state["last_refresh"] = utc_now()
            # A successful telemetry refresh clears a transient health error, but
            # must not erase the reason recorded for the last completed operation.
            if state.get("last_operation_result") is None:
                state["last_error"] = None
        except ExecutorError as exc:
            state.update(
                {
                    "health_status": "offline",
                    "health_score": 0,
                    "last_error": sanitize_text(exc, limit=2000),
                    "last_refresh": utc_now(),
                }
            )
        return self._save_state(vmid, state)
'''
new_refresh = '''    def refresh_container(self, vmid: int) -> dict[str, Any]:
        cfg = self._container(vmid)
        if not bool(cfg.get("enabled", False)):
            state = self.get_state(vmid)
            state.update({"health_status": "unknown", "health_score": 0})
            return self._save_state(vmid, state)
        try:
            inspected = self.executor.run("inspect", vmid, timeout=120).get("data", {})
            # Inspect may take long enough for a job to reach a terminal state.
            # Re-read the latest DB state after I/O so telemetry cannot resurrect
            # stale operation/plan/job fields captured before that transition.
            state = self.get_state(vmid)
            state.update(inspected)
            state["health_status"] = inspected.get(
                "health_status",
                inspected.get("health", "unknown"),
            )
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
        return self._save_state(vmid, state)
'''
if service.count(old_refresh) != 1:
    raise SystemExit("refresh_container patch target not unique")
service_path.write_text(service.replace(old_refresh, new_refresh), encoding="utf-8")

db_path = Path("app/database.py")
database = db_path.read_text(encoding="utf-8")
old_lookup = '''        with self._lock, self._connect() as conn:
            row = conn.execute(query, args).fetchone()
        return _decode_plan(row) if row else None
'''
new_lookup = '''        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE plans SET status='expired' "
                "WHERE status='waiting_approval' AND expires_at<=?",
                (utc_now(),),
            )
            row = conn.execute(query, args).fetchone()
        return _decode_plan(row) if row else None
'''
if database.count(old_lookup) != 1:
    raise SystemExit("find_active_plan patch target not unique")
db_path.write_text(database.replace(old_lookup, new_lookup), encoding="utf-8")
