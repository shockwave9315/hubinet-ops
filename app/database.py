from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .security import bounded_json, sanitize_data, sanitize_text
from .state import JOB_STAGES, normalize_state


class Database:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS plans (
                    id TEXT PRIMARY KEY,
                    vmid INTEGER NOT NULL,
                    container_name TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    status TEXT NOT NULL,
                    risk TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    approved_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_plans_status ON plans(status);
                CREATE INDEX IF NOT EXISTS idx_plans_vmid ON plans(vmid);

                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    plan_id TEXT NOT NULL,
                    vmid INTEGER NOT NULL,
                    container_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    progress INTEGER NOT NULL DEFAULT 0,
                    snapshot_name TEXT,
                    result TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(plan_id) REFERENCES plans(id)
                );

                CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
                CREATE INDEX IF NOT EXISTS idx_jobs_vmid ON jobs(vmid);

                CREATE TABLE IF NOT EXISTS container_states (
                    vmid INTEGER PRIMARY KEY,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS job_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    vmid INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    level TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    progress INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    message TEXT NOT NULL,
                    details_json TEXT NOT NULL DEFAULT '{}',
                    FOREIGN KEY(job_id) REFERENCES jobs(id)
                );

                CREATE INDEX IF NOT EXISTS idx_job_events_job_created
                    ON job_events(job_id, created_at DESC, id DESC);
                CREATE INDEX IF NOT EXISTS idx_job_events_vmid_created
                    ON job_events(vmid, created_at DESC, id DESC);
                """
            )
            job_columns = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
            if "progress" not in job_columns:
                conn.execute("ALTER TABLE jobs ADD COLUMN progress INTEGER NOT NULL DEFAULT 0")
            self._migrate_container_states(conn)
            conn.execute("PRAGMA user_version=201")
            # Po restarcie nie udajemy, że przerwane zadanie dalej działa.
            now = utc_now()
            conn.execute(
                "UPDATE jobs SET status='interrupted', stage='failed', progress=100, updated_at=? "
                "WHERE status IN ('queued','running')",
                (now,),
            )
            conn.execute(
                "UPDATE plans SET status='interrupted' WHERE status='approved' "
                "AND id IN (SELECT plan_id FROM jobs WHERE status='interrupted')"
            )

    def _migrate_container_states(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute("SELECT vmid, payload FROM container_states").fetchall()
        for row in rows:
            try:
                old = json.loads(row["payload"])
                migrated = normalize_state(old)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if migrated != old:
                conn.execute(
                    "UPDATE container_states SET payload=? WHERE vmid=?",
                    (json.dumps(migrated, ensure_ascii=False), row["vmid"]),
                )

    def create_plan(
        self,
        *,
        vmid: int,
        container_name: str,
        fingerprint: str,
        risk: str,
        payload: dict[str, Any],
        ttl_minutes: int,
    ) -> dict[str, Any]:
        plan_id = uuid.uuid4().hex
        created = datetime.now(UTC)
        expires = created + timedelta(minutes=ttl_minutes)
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE plans SET status='superseded' WHERE vmid=? AND fingerprint<>? "
                "AND status='waiting_approval'",
                (vmid, fingerprint),
            )
            conn.execute(
                "INSERT INTO plans(id, vmid, container_name, fingerprint, status, risk, payload, created_at, expires_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    plan_id,
                    vmid,
                    container_name,
                    fingerprint,
                    "waiting_approval",
                    risk,
                    json.dumps(payload, ensure_ascii=False),
                    created.isoformat(),
                    expires.isoformat(),
                ),
            )
        return self.get_plan(plan_id)

    def find_active_plan(self, vmid: int, fingerprint: str | None = None) -> dict[str, Any] | None:
        query = (
            "SELECT * FROM plans WHERE vmid=? "
            "AND status IN ('waiting_approval','approved')"
        )
        args: list[Any] = [vmid]
        if fingerprint is not None:
            query += " AND fingerprint=?"
            args.append(fingerprint)
        query += " ORDER BY created_at DESC LIMIT 1"
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE plans SET status='expired' "
                "WHERE status='waiting_approval' AND expires_at<=?",
                (utc_now(),),
            )
            row = conn.execute(query, args).fetchone()
        return _decode_plan(row) if row else None

    def get_plan(self, plan_id: str) -> dict[str, Any]:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
        if not row:
            raise KeyError(plan_id)
        return _decode_plan(row)

    def list_plans(self, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        query = "SELECT * FROM plans"
        args: list[Any] = []
        if status:
            query += " WHERE status=?"
            args.append(status)
        query += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        with self._lock, self._connect() as conn:
            rows = conn.execute(query, args).fetchall()
        return [_decode_plan(row) for row in rows]

    def approve_plan(self, plan_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        now = datetime.now(UTC)
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
            if not row:
                conn.execute("ROLLBACK")
                raise KeyError(plan_id)
            plan = _decode_plan(row)
            if plan["status"] != "waiting_approval":
                conn.execute("ROLLBACK")
                raise ValueError(f"Plan status is {plan['status']}, not waiting_approval")
            if datetime.fromisoformat(plan["expires_at"]) <= now:
                conn.execute("UPDATE plans SET status='expired' WHERE id=?", (plan_id,))
                conn.execute("COMMIT")
                raise ValueError("Plan expired")

            active_job = conn.execute(
                "SELECT id FROM jobs WHERE vmid=? AND status IN ('queued','running') LIMIT 1",
                (plan["vmid"],),
            ).fetchone()
            if active_job:
                conn.execute("ROLLBACK")
                raise ValueError("Another job is already queued or running for this VMID")

            job_id = uuid.uuid4().hex
            now_iso = now.isoformat()
            conn.execute(
                "UPDATE plans SET status='approved', approved_at=? WHERE id=?",
                (now_iso, plan_id),
            )
            conn.execute(
                "INSERT INTO jobs(id, plan_id, vmid, container_name, status, stage, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    job_id,
                    plan_id,
                    plan["vmid"],
                    plan["container_name"],
                    "queued",
                    "queued",
                    now_iso,
                    now_iso,
                ),
            )
            conn.execute("COMMIT")
        return self.get_plan(plan_id), self.get_job(job_id)

    def update_plan_status(self, plan_id: str, status: str) -> dict[str, Any]:
        allowed = {
            "waiting_approval", "approved", "rejected", "expired", "superseded",
            "completed", "blocked", "failed", "rolled_back", "recovered", "interrupted"
        }
        if status not in allowed:
            raise ValueError("Invalid plan status")
        with self._lock, self._connect() as conn:
            cur = conn.execute("UPDATE plans SET status=? WHERE id=?", (status, plan_id))
            if cur.rowcount == 0:
                raise KeyError(plan_id)
        return self.get_plan(plan_id)

    def invalidate_active_plans(self, vmid: int, status: str = "superseded") -> None:
        if status not in {"superseded", "expired", "rejected"}:
            raise ValueError("Invalid terminal plan status")
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE plans SET status=? WHERE vmid=? AND status='waiting_approval'",
                (status, vmid),
            )

    def reject_plan(self, plan_id: str) -> dict[str, Any]:
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
            if not row:
                conn.execute("ROLLBACK")
                raise KeyError(plan_id)
            plan = _decode_plan(row)
            if plan["status"] != "waiting_approval":
                conn.execute("ROLLBACK")
                raise ValueError(f"Plan status is {plan['status']}, not waiting_approval")
            conn.execute("UPDATE plans SET status='rejected' WHERE id=?", (plan_id,))
            conn.execute("COMMIT")
        return self.get_plan(plan_id)

    def next_queued_job(self) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM jobs WHERE status='queued' ORDER BY created_at ASC LIMIT 1"
            ).fetchone()
            if not row:
                conn.execute("COMMIT")
                return None
            now = utc_now()
            conn.execute(
                "UPDATE jobs SET status='running', stage='preflight', progress=5, updated_at=? WHERE id=?",
                (now, row["id"]),
            )
            conn.execute("COMMIT")
        return self.get_job(row["id"])

    def get_job(self, job_id: str) -> dict[str, Any]:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise KeyError(job_id)
        return _decode_job(row)

    def get_active_job(self, vmid: int) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE vmid=? AND status IN ('queued','running') "
                "ORDER BY created_at DESC LIMIT 1",
                (vmid,),
            ).fetchone()
        return _decode_job(row) if row else None

    def get_latest_job(self, vmid: int) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE vmid=? ORDER BY created_at DESC LIMIT 1", (vmid,)
            ).fetchone()
        return _decode_job(row) if row else None

    def list_jobs(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_decode_job(row) for row in rows]

    def active_job_count(self) -> int:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM jobs WHERE status IN ('queued','running')"
            ).fetchone()
        return int(row["count"])

    def create_manual_rollback_job(self, source_job_id: str) -> dict[str, Any]:
        return self.create_followup_job(source_job_id, stage="rollback", progress=1)

    def create_followup_job(
        self,
        source_job_id: str,
        *,
        stage: str,
        progress: int,
    ) -> dict[str, Any]:
        source = self.get_job(source_job_id)
        job_id = uuid.uuid4().hex
        now = utc_now()
        with self._lock, self._connect() as conn:
            if conn.execute(
                "SELECT 1 FROM jobs WHERE vmid=? AND status IN ('queued','running')",
                (source["vmid"],),
            ).fetchone():
                raise ValueError("Another job is already active for this VMID")
            conn.execute(
                "INSERT INTO jobs(id,plan_id,vmid,container_name,status,stage,progress,snapshot_name,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    job_id,
                    source["plan_id"],
                    source["vmid"],
                    source["container_name"],
                    "running",
                    stage,
                    max(0, min(99, int(progress))),
                    source.get("snapshot_name"),
                    now,
                    now,
                ),
            )
        return self.get_job(job_id)

    def update_job(self, job_id: str, **fields: Any) -> dict[str, Any]:
        allowed = {"status", "stage", "progress", "snapshot_name", "result", "error"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"Unknown job fields: {unknown}")
        fields["updated_at"] = utc_now()
        assignments = ", ".join(f"{key}=?" for key in fields)
        values = []
        for key, value in fields.items():
            if key == "result" and isinstance(value, (dict, list)):
                value = bounded_json(value)
            elif key == "error" and value is not None:
                value = sanitize_text(value, limit=2000)
            values.append(value)
        values.append(job_id)
        with self._lock, self._connect() as conn:
            conn.execute(f"UPDATE jobs SET {assignments} WHERE id=?", values)
        return self.get_job(job_id)

    def upsert_container_state(self, vmid: int, payload: dict[str, Any]) -> dict[str, Any]:
        updated_at = utc_now()
        payload = normalize_state(payload)
        payload["updated_at"] = updated_at
        raw = bounded_json(payload, limit=256_000)
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO container_states(vmid, payload, updated_at) VALUES(?,?,?) "
                "ON CONFLICT(vmid) DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at",
                (vmid, raw, updated_at),
            )
        return payload

    def get_container_state(self, vmid: int) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM container_states WHERE vmid=?", (vmid,)
            ).fetchone()
        return normalize_state(json.loads(row["payload"])) if row else None

    def list_container_states(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT payload FROM container_states ORDER BY vmid ASC"
            ).fetchall()
        return [normalize_state(json.loads(row["payload"])) for row in rows]

    def insert_job_event(
        self,
        *,
        job_id: str,
        vmid: int,
        level: str,
        stage: str,
        progress: int,
        event_type: str,
        message: str,
        details: dict[str, Any] | None = None,
        terminal: bool = False,
    ) -> dict[str, Any]:
        safe_level = level if level in {"debug", "info", "warning", "error"} else "info"
        requested_stage = sanitize_text(stage, limit=64) or "idle"
        safe_stage = requested_stage if requested_stage in JOB_STAGES else "idle"
        safe_type = sanitize_text(event_type, limit=64) or "event"
        safe_message = sanitize_text(message, limit=1000)
        requested = max(0, min(100, int(progress)))
        if requested == 100 and not terminal:
            requested = 99
        created = utc_now()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            job = conn.execute("SELECT progress FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not job:
                conn.execute("ROLLBACK")
                raise KeyError(job_id)
            actual = max(int(job["progress"] or 0), requested)
            conn.execute(
                "INSERT INTO job_events(job_id,vmid,created_at,level,stage,progress,event_type,message,details_json) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    job_id,
                    int(vmid),
                    created,
                    safe_level,
                    safe_stage,
                    actual,
                    safe_type,
                    safe_message,
                    bounded_json(details or {}, limit=16_000),
                ),
            )
            event_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            conn.execute(
                "UPDATE jobs SET stage=?, progress=?, updated_at=? WHERE id=?",
                (safe_stage, actual, created, job_id),
            )
            conn.execute("COMMIT")
        return self.get_job_event(event_id)

    def get_job_event(self, event_id: int) -> dict[str, Any]:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM job_events WHERE id=?", (event_id,)).fetchone()
        if not row:
            raise KeyError(event_id)
        return _decode_event(row)

    def list_job_events(self, job_id: str, limit: int = 50) -> list[dict[str, Any]]:
        bounded = min(max(int(limit), 1), 200)
        with self._lock, self._connect() as conn:
            if not conn.execute("SELECT 1 FROM jobs WHERE id=?", (job_id,)).fetchone():
                raise KeyError(job_id)
            rows = conn.execute(
                "SELECT * FROM job_events WHERE job_id=? ORDER BY created_at DESC,id DESC LIMIT ?",
                (job_id, bounded),
            ).fetchall()
        return [_decode_event(row) for row in reversed(rows)]

    def list_container_events(self, vmid: int, limit: int = 50) -> list[dict[str, Any]]:
        bounded = min(max(int(limit), 1), 200)
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM job_events WHERE vmid=? ORDER BY created_at DESC,id DESC LIMIT ?",
                (int(vmid), bounded),
            ).fetchall()
        return [_decode_event(row) for row in reversed(rows)]


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _decode_plan(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["payload"] = json.loads(item["payload"])
    return item


def _decode_job(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    if item.get("result"):
        try:
            item["result"] = json.loads(item["result"])
        except json.JSONDecodeError:
            pass
    return item


def _decode_event(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    try:
        item["details"] = sanitize_data(json.loads(item.pop("details_json")))
    except (json.JSONDecodeError, TypeError):
        item["details"] = {}
        item.pop("details_json", None)
    return item
