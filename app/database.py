from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


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
                """
            )
            # Po restarcie nie udajemy, że przerwane zadanie dalej działa.
            now = utc_now()
            conn.execute(
                "UPDATE jobs SET status='interrupted', stage='interrupted', updated_at=? "
                "WHERE status IN ('queued','running')",
                (now,),
            )
            conn.execute(
                "UPDATE plans SET status='interrupted' WHERE status='approved' "
                "AND id IN (SELECT plan_id FROM jobs WHERE status='interrupted')"
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
                "UPDATE jobs SET status='running', stage='preflight', updated_at=? WHERE id=?",
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

    def update_job(self, job_id: str, **fields: Any) -> dict[str, Any]:
        allowed = {"status", "stage", "snapshot_name", "result", "error"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"Unknown job fields: {unknown}")
        fields["updated_at"] = utc_now()
        assignments = ", ".join(f"{key}=?" for key in fields)
        values = []
        for key, value in fields.items():
            if key == "result" and isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False)
            values.append(value)
        values.append(job_id)
        with self._lock, self._connect() as conn:
            conn.execute(f"UPDATE jobs SET {assignments} WHERE id=?", values)
        return self.get_job(job_id)

    def upsert_container_state(self, vmid: int, payload: dict[str, Any]) -> dict[str, Any]:
        updated_at = utc_now()
        payload = dict(payload)
        payload["updated_at"] = updated_at
        raw = json.dumps(payload, ensure_ascii=False)
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
        return json.loads(row["payload"]) if row else None

    def list_container_states(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT payload FROM container_states ORDER BY vmid ASC"
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]


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
