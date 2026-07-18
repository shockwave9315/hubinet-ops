from __future__ import annotations

from pathlib import Path

path = Path("app/service.py")
text = path.read_text(encoding="utf-8")

old_retry = '''    def retry_healthcheck(self, vmid: int) -> dict[str, Any]:
        cfg = self._container(vmid)
        if self.db.get_active_job(vmid) is not None:
            raise ValueError("Another job is already active for this container")
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
'''
new_retry = '''    def retry_healthcheck(self, vmid: int) -> dict[str, Any]:
        cfg = self._container(vmid)
        lock = self._scan_locks[vmid]
        if not lock.acquire(blocking=False):
            raise ValueError("Another scan or manual operation is active for this container")
        try:
            if self.db.get_active_job(vmid) is not None:
                raise ValueError("Another job is already active for this container")
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
'''
if text.count(old_retry) != 1:
    raise SystemExit("retry_healthcheck patch target not unique")
text = text.replace(old_retry, new_retry)

old_rollback = '''    def manual_rollback(self, vmid: int) -> dict[str, Any]:
        cfg = self._container(vmid)
        if not bool(cfg.get("manual_rollback_allowed", False)):
            raise ValueError("Manual rollback is not allowed by container policy")
        source = self.db.get_latest_job(vmid)
        if source is None or not source.get("snapshot_name"):
            raise ValueError("No rollback snapshot is available")
        if source["status"] not in {"failed", "blocked", "interrupted"}:
            raise ValueError("Rollback is only allowed after a failed operation")
        job = self.db.create_manual_rollback_job(source["id"])
        self.db.update_job(job["id"], status="running", stage="rollback", progress=1)
        self._rollback(job, str(source.get("error") or "Manual rollback requested"))
        return self.db.get_job(job["id"])
'''
new_rollback = '''    def manual_rollback(self, vmid: int) -> dict[str, Any]:
        cfg = self._container(vmid)
        lock = self._scan_locks[vmid]
        if not lock.acquire(blocking=False):
            raise ValueError("Another scan or manual operation is active for this container")
        try:
            if not bool(cfg.get("manual_rollback_allowed", False)):
                raise ValueError("Manual rollback is not allowed by container policy")
            if self.db.get_active_job(vmid) is not None:
                raise ValueError("Another job is already active for this container")
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
'''
if text.count(old_rollback) != 1:
    raise SystemExit("manual_rollback patch target not unique")
text = text.replace(old_rollback, new_rollback)

path.write_text(text, encoding="utf-8")
