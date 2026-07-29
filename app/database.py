from __future__ import annotations

import json
import re
import sqlite3
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .contracts import (
    JOB_OPERATION_TYPES,
    REQUEST_ID_RE,
    parse_owned_snapshot_name,
)
from .security import bounded_json, sanitize_data, sanitize_text
from .state import JOB_STAGES, normalize_state
from .time_utils import parse_utc_timestamp


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
                    request_id TEXT NOT NULL,
                    operation_type TEXT NOT NULL,
                    plan_id TEXT,
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
                    FOREIGN KEY(plan_id) REFERENCES plans(id),
                    UNIQUE(vmid, request_id)
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

                CREATE TABLE IF NOT EXISTS processed_recovery_events (
                    recovery_id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL,
                    vmid INTEGER NOT NULL,
                    snapshot_name TEXT,
                    operation_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result_json TEXT,
                    error TEXT,
                    started_at TEXT NOT NULL,
                    mutation_started_at TEXT,
                    completed_at TEXT,
                    processed_at TEXT NOT NULL
                );
                """
            )
            self._migrate_jobs(conn)
            recovery_columns = {
                str(row["name"])
                for row in conn.execute(
                    "PRAGMA table_info(processed_recovery_events)"
                ).fetchall()
            }
            if "mutation_started_at" not in recovery_columns:
                conn.execute(
                    "ALTER TABLE processed_recovery_events "
                    "ADD COLUMN mutation_started_at TEXT"
                )
            self._migrate_container_states(conn)
            conn.execute("PRAGMA user_version=400")

    def _migrate_jobs(self, conn: sqlite3.Connection) -> None:
        columns = list(conn.execute("PRAGMA table_info(jobs)"))
        names = {row["name"] for row in columns}
        plan_column = next((row for row in columns if row["name"] == "plan_id"), None)
        if (
            {"request_id", "operation_type", "progress"} <= names
            and plan_column is not None
            and plan_column["notnull"] == 0
        ):
            return
        jobs = [dict(row) for row in conn.execute("SELECT * FROM jobs")]
        events = [dict(row) for row in conn.execute("SELECT * FROM job_events")]
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.executescript(
            """
            DROP TABLE job_events;
            DROP TABLE jobs;
            CREATE TABLE jobs (
                id TEXT PRIMARY KEY,
                request_id TEXT NOT NULL,
                operation_type TEXT NOT NULL,
                plan_id TEXT,
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
                FOREIGN KEY(plan_id) REFERENCES plans(id),
                UNIQUE(vmid, request_id)
            );
            CREATE INDEX idx_jobs_status ON jobs(status);
            CREATE INDEX idx_jobs_vmid ON jobs(vmid);
            CREATE TABLE job_events (
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
            CREATE INDEX idx_job_events_job_created
                ON job_events(job_id, created_at DESC, id DESC);
            CREATE INDEX idx_job_events_vmid_created
                ON job_events(vmid, created_at DESC, id DESC);
            """
        )
        for job in jobs:
            conn.execute(
                "INSERT INTO jobs "
                "(id,request_id,operation_type,plan_id,vmid,container_name,status,stage,progress,"
                "snapshot_name,result,error,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    job["id"], job.get("request_id") or job["id"],
                    job.get("operation_type") or "update", job.get("plan_id"),
                    job["vmid"], job["container_name"], job["status"], job["stage"],
                    int(job.get("progress") or 0), job.get("snapshot_name"),
                    job.get("result"), job.get("error"), job["created_at"], job["updated_at"],
                ),
            )
        for event in events:
            conn.execute(
                "INSERT INTO job_events "
                "(id,job_id,vmid,created_at,level,stage,progress,event_type,message,details_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                tuple(
                    event[key]
                    for key in (
                        "id", "job_id", "vmid", "created_at", "level", "stage",
                        "progress", "event_type", "message", "details_json"
                    )
                ),
            )
        conn.execute("PRAGMA foreign_keys=ON")

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

    def waiting_plans(self, vmid: int) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE plans SET status='expired' WHERE vmid=? "
                "AND status='waiting_approval' AND expires_at<=?",
                (vmid, utc_now()),
            )
            rows = conn.execute(
                "SELECT * FROM plans WHERE vmid=? AND status='waiting_approval' "
                "ORDER BY created_at DESC, id DESC",
                (vmid,),
            ).fetchall()
        return [_decode_plan(row) for row in rows]

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

    def approve_plan(
        self,
        plan_id: str,
        *,
        request_id: str | None = None,
        operation_type: str = "update",
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if operation_type not in {"update", "self_update"}:
            raise ValueError("Invalid approved plan operation")
        now = datetime.now(UTC)
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
            if not row:
                conn.execute("ROLLBACK")
                raise KeyError(plan_id)
            plan = _decode_plan(row)
            request_id = request_id or uuid.uuid4().hex
            if not REQUEST_ID_RE.fullmatch(request_id):
                conn.execute("ROLLBACK")
                raise ValueError("Invalid request_id")
            existing = conn.execute(
                "SELECT * FROM jobs WHERE vmid=? AND request_id=?",
                (plan["vmid"], request_id),
            ).fetchone()
            if existing is not None:
                job = _decode_job(existing)
                if (
                    job["operation_type"] != operation_type
                    or job["plan_id"] != plan_id
                ):
                    conn.execute("ROLLBACK")
                    raise ValueError("request_id was already used for another operation")
                conn.execute("COMMIT")
                return plan, job
            if plan["status"] != "waiting_approval":
                conn.execute("ROLLBACK")
                raise ValueError(f"Plan status is {plan['status']}, not waiting_approval")
            expires_at = parse_utc_timestamp(plan.get("expires_at"))
            if expires_at is None or expires_at <= now:
                conn.execute("UPDATE plans SET status='expired' WHERE id=?", (plan_id,))
                conn.execute("COMMIT")
                raise ValueError("Plan expired")

            active_job = conn.execute(
                "SELECT id FROM jobs WHERE status IN ('queued','running') LIMIT 1"
            ).fetchone()
            if active_job:
                conn.execute("ROLLBACK")
                raise ValueError("Another destructive maintenance job is active")

            job_id = uuid.uuid4().hex
            now_iso = now.isoformat()
            conn.execute(
                "UPDATE plans SET status='approved', approved_at=? WHERE id=?",
                (now_iso, plan_id),
            )
            conn.execute(
                "INSERT INTO jobs(id,request_id,operation_type,plan_id,vmid,container_name,status,stage,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    job_id,
                    request_id,
                    operation_type,
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
            stage = "preflight" if row["operation_type"] == "update" else "queued"
            progress = 5 if row["operation_type"] == "update" else 1
            conn.execute(
                "UPDATE jobs SET status='running', stage=?, progress=?, updated_at=? WHERE id=?",
                (stage, progress, now, row["id"]),
            )
            conn.execute("COMMIT")
        return self.get_job(row["id"])

    def get_job(self, job_id: str) -> dict[str, Any]:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise KeyError(job_id)
        return _decode_job(row)

    def get_job_by_request_id(
        self,
        vmid: int,
        request_id: str,
    ) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE vmid=? AND request_id=?",
                (int(vmid), str(request_id)),
            ).fetchone()
        return _decode_job(row) if row else None

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

    def rollback_source_snapshots(self, vmid: int) -> set[str]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT snapshot_name FROM jobs WHERE vmid=? AND snapshot_name IS NOT NULL "
                "AND status IN ('failed','blocked','interrupted')",
                (vmid,),
            ).fetchall()
        return {str(row["snapshot_name"]) for row in rows if row["snapshot_name"]}

    def list_jobs(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_decode_job(row) for row in rows]

    def active_jobs(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE status IN ('queued','running') "
                "ORDER BY created_at, id"
            ).fetchall()
        return [_decode_job(row) for row in rows]

    def active_job_count(self) -> int:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM jobs WHERE status IN ('queued','running')"
            ).fetchone()
        return int(row["count"])

    def create_operation_job(
        self,
        *,
        vmid: int,
        container_name: str,
        operation_type: str,
        request_id: str,
        plan_id: str | None = None,
        snapshot_name: str | None = None,
        require_no_active_plan: bool = False,
    ) -> tuple[dict[str, Any], bool]:
        if operation_type not in JOB_OPERATION_TYPES:
            raise ValueError("Invalid operation_type")
        if not REQUEST_ID_RE.fullmatch(str(request_id)):
            raise ValueError("Invalid request_id")
        now = utc_now()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM jobs WHERE vmid=? AND request_id=?",
                (vmid, request_id),
            ).fetchone()
            if existing is not None:
                job = _decode_job(existing)
                if (
                    job["operation_type"] != operation_type
                    or job.get("plan_id") != plan_id
                    or job.get("snapshot_name") != snapshot_name
                ):
                    conn.execute("ROLLBACK")
                    raise ValueError("request_id was already used for another operation")
                conn.execute("COMMIT")
                return job, False
            if require_no_active_plan:
                conn.execute(
                    "UPDATE plans SET status='expired' "
                    "WHERE status='waiting_approval' AND expires_at<=?",
                    (now,),
                )
                if conn.execute(
                    "SELECT 1 FROM plans "
                    "WHERE status IN ('waiting_approval','approved') LIMIT 1"
                ).fetchone():
                    conn.execute("ROLLBACK")
                    raise ValueError(
                        "Resolve the active update plan before snapshot restore"
                    )
            if conn.execute(
                "SELECT 1 FROM jobs WHERE status IN ('queued','running') LIMIT 1"
            ).fetchone():
                conn.execute("ROLLBACK")
                raise ValueError("Another destructive maintenance job is active")
            job_id = uuid.uuid4().hex
            conn.execute(
                "INSERT INTO jobs "
                "(id,request_id,operation_type,plan_id,vmid,container_name,status,stage,progress,"
                "snapshot_name,created_at,updated_at) VALUES(?,?,?,?,?,?,'queued','queued',0,?,?,?)",
                (
                    job_id, request_id, operation_type, plan_id, vmid, container_name,
                    snapshot_name, now, now,
                ),
            )
            conn.execute("COMMIT")
        return self.get_job(job_id), True

    def apply_recovery_event(
        self,
        event: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        recovery_id = str(event.get("recovery_id") or "")
        request_id = str(event.get("request_id") or "")
        operation_type = str(event.get("operation_type") or "")
        status = str(event.get("status") or "")
        try:
            vmid = int(event.get("vmid"))
        except (TypeError, ValueError) as exc:
            raise ValueError("Recovery event has invalid VMID") from exc
        if (
            not re.fullmatch(r"[a-f0-9]{32}", recovery_id)
            or not REQUEST_ID_RE.fullmatch(request_id)
            or vmid != 110
            or operation_type not in {
                "offline_snapshot_restore",
                "offline_force_stop",
            }
            or status not in {"succeeded", "failed", "interrupted"}
        ):
            raise ValueError("Recovery event contract is invalid")
        snapshot_name = (
            str(event.get("snapshot_name") or "")
            if operation_type == "offline_snapshot_restore"
            else None
        )
        if (
            operation_type == "offline_snapshot_restore"
            and parse_owned_snapshot_name(str(snapshot_name), vmid=110) is None
        ):
            raise ValueError("Offline restore recovery event has invalid snapshot")
        started_at = str(event.get("started_at") or "")
        mutation_started_at = (
            str(event.get("mutation_started_at") or "") or None
        )
        completed_at = str(event.get("completed_at") or "") or None
        if parse_utc_timestamp(started_at) is None or (
            mutation_started_at is not None
            and parse_utc_timestamp(mutation_started_at) is None
        ) or (
            completed_at is not None and parse_utc_timestamp(completed_at) is None
        ):
            raise ValueError("Recovery event has invalid timestamps")
        result = event.get("result")
        result_payload = dict(result) if isinstance(result, dict) else {}
        error = sanitize_text(event.get("error"), limit=2000) if event.get("error") else None
        processed_at = utc_now()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM processed_recovery_events WHERE recovery_id=?",
                (recovery_id,),
            ).fetchone()
            if existing is not None:
                persisted_existing = self._decode_recovery_event(existing)
                expected_contract = {
                    "request_id": request_id,
                    "vmid": vmid,
                    "snapshot_name": snapshot_name,
                    "operation_type": operation_type,
                    "status": status,
                    "mutation_started_at": mutation_started_at,
                }
                if any(
                    persisted_existing.get(key) != value
                    for key, value in expected_contract.items()
                ):
                    conn.execute("ROLLBACK")
                    raise ValueError(
                        "Recovery event contract changed after local persistence"
                    )
                conn.execute("COMMIT")
                return persisted_existing, False
            restore_state_is_untrusted = (
                operation_type == "offline_snapshot_restore"
                and (
                    status == "succeeded"
                    or (
                        mutation_started_at is not None
                        and status in {"failed", "interrupted"}
                    )
                )
            )
            if restore_state_is_untrusted:
                conn.execute(
                    "UPDATE plans SET status='superseded' "
                    "WHERE status='waiting_approval'"
                )
                conn.execute(
                    "UPDATE plans SET status='recovered' WHERE status='approved'"
                )
                recovery_result = bounded_json(
                    {
                        "recovery_id": recovery_id,
                        "snapshot_name": snapshot_name,
                        "recovery_status": status,
                        "recovery_error": error,
                        "mutation_started_at": mutation_started_at,
                        "backend_state_invalidated": True,
                        "recovered": status == "succeeded",
                    }
                )
                conn.execute(
                    "UPDATE jobs SET status='interrupted', stage='failed', progress=100, "
                    "result=?, error=?, updated_at=? "
                    "WHERE status IN ('queued','running')",
                    (
                        recovery_result,
                        sanitize_text(
                            f"Superseded by offline recovery {recovery_id} "
                            f"from snapshot {snapshot_name}",
                            limit=2000,
                        ),
                        processed_at,
                    ),
                )
                rows = conn.execute(
                    "SELECT vmid,payload FROM container_states"
                ).fetchall()
                for row in rows:
                    try:
                        state = json.loads(row["payload"])
                    except (TypeError, json.JSONDecodeError):
                        state = {}
                    if not isinstance(state, dict):
                        state = {}
                    state.update(
                        {
                            "active_plan_id": None,
                            "active_plan_status": None,
                            "active_job_id": None,
                        }
                    )
                    if int(row["vmid"]) == 110:
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
                                "operation_status": "unknown",
                                "last_operation_result": "interrupted",
                                "last_offline_recovery_id": recovery_id,
                                "last_offline_recovery_snapshot": snapshot_name,
                                "last_offline_recovery_at": completed_at or processed_at,
                                "last_offline_recovery_status": status,
                                "last_offline_recovery_error": error,
                                "last_offline_recovery_mutation_started_at": (
                                    mutation_started_at
                                ),
                            }
                        )
                    normalized = normalize_state(state)
                    conn.execute(
                        "UPDATE container_states SET payload=?,updated_at=? WHERE vmid=?",
                        (
                            bounded_json(normalized, limit=256_000),
                            processed_at,
                            int(row["vmid"]),
                        ),
                    )
            conn.execute(
                "INSERT INTO processed_recovery_events "
                "(recovery_id,request_id,vmid,snapshot_name,operation_type,status,"
                "result_json,error,started_at,mutation_started_at,completed_at,processed_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    recovery_id,
                    request_id,
                    vmid,
                    snapshot_name,
                    operation_type,
                    status,
                    bounded_json(result_payload),
                    error,
                    started_at,
                    mutation_started_at,
                    completed_at,
                    processed_at,
                ),
            )
            persisted = conn.execute(
                "SELECT * FROM processed_recovery_events WHERE recovery_id=?",
                (recovery_id,),
            ).fetchone()
            conn.execute("COMMIT")
        if persisted is None:
            raise RuntimeError("Failed to persist recovery event")
        return self._decode_recovery_event(persisted), True

    def get_processed_recovery_event(self, recovery_id: str) -> dict[str, Any]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM processed_recovery_events WHERE recovery_id=?",
                (recovery_id,),
            ).fetchone()
        if row is None:
            raise KeyError(recovery_id)
        return self._decode_recovery_event(row)

    @staticmethod
    def _decode_recovery_event(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        value["result"] = (
            json.loads(value.pop("result_json")) if value["result_json"] else {}
        )
        return value

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
        request_id = uuid.uuid4().hex
        now = utc_now()
        with self._lock, self._connect() as conn:
            if conn.execute(
                "SELECT 1 FROM jobs WHERE status IN ('queued','running') LIMIT 1"
            ).fetchone():
                raise ValueError("Another destructive maintenance job is active")
            conn.execute(
                "INSERT INTO jobs(id,request_id,operation_type,plan_id,vmid,container_name,status,stage,progress,snapshot_name,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    job_id,
                    request_id,
                    "snapshot_rollback" if stage == "rollback" else "retry_healthcheck",
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

    def terminalize_job_with_snapshot_prune(
        self,
        source_job_id: str,
        *,
        source_status: str,
        terminal_result: str,
        error: str | None,
        prune_request_id: str,
        prune_result: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        """Atomically finish a source operation and enqueue its retention follow-up."""
        if source_status not in {
            "blocked",
            "failed",
            "interrupted",
            "recovered",
            "rolled_back",
            "success",
        }:
            raise ValueError("Invalid terminal source status")
        if terminal_result not in {
            "failed",
            "interrupted",
            "manual_intervention",
            "rolled_back",
            "success",
        }:
            raise ValueError("Invalid terminal result")
        if not REQUEST_ID_RE.fullmatch(prune_request_id):
            raise ValueError("Invalid snapshot prune request_id")
        now = utc_now()
        prune_id = uuid.uuid4().hex
        event_level = (
            "error"
            if terminal_result in {"failed", "manual_intervention"}
            else "warning"
            if terminal_result == "interrupted"
            else "info"
        )
        event_stage = (
            "completed"
            if terminal_result in {"success", "rolled_back"}
            else "failed"
        )
        event_message = sanitize_text(
            error or f"Job finished: {terminal_result}",
            limit=1000,
        )
        safe_error = sanitize_text(error, limit=2000) if error else None
        raw_prune_result = bounded_json(prune_result)
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            source = conn.execute(
                "SELECT * FROM jobs WHERE id=?",
                (source_job_id,),
            ).fetchone()
            if source is None:
                conn.execute("ROLLBACK")
                raise KeyError(source_job_id)
            if str(source["status"]) not in {"queued", "running"}:
                conn.execute("ROLLBACK")
                raise ValueError("Source job is already terminal")
            other_active = conn.execute(
                "SELECT 1 FROM jobs "
                "WHERE status IN ('queued','running') AND id<>? LIMIT 1",
                (source_job_id,),
            ).fetchone()
            if other_active is not None:
                conn.execute("ROLLBACK")
                raise ValueError("Another destructive maintenance job is active")
            existing = conn.execute(
                "SELECT * FROM jobs WHERE vmid=? AND request_id=?",
                (int(source["vmid"]), prune_request_id),
            ).fetchone()
            if existing is not None:
                conn.execute("ROLLBACK")
                raise ValueError("Snapshot prune handoff request_id already exists")
            conn.execute(
                "UPDATE jobs SET status=?,stage=?,progress=100,error=?,updated_at=? "
                "WHERE id=?",
                (
                    source_status,
                    event_stage,
                    safe_error,
                    now,
                    source_job_id,
                ),
            )
            conn.execute(
                "INSERT INTO jobs "
                "(id,request_id,operation_type,plan_id,vmid,container_name,status,"
                "stage,progress,snapshot_name,result,created_at,updated_at) "
                "VALUES(?,?, 'snapshot_prune',NULL,?,?,'queued','queued',0,"
                "'retention',?,?,?)",
                (
                    prune_id,
                    prune_request_id,
                    int(source["vmid"]),
                    str(source["container_name"]),
                    raw_prune_result,
                    now,
                    now,
                ),
            )
            conn.execute(
                "INSERT INTO job_events"
                "(job_id,vmid,created_at,level,stage,progress,event_type,message,"
                "details_json) VALUES(?,?,?,?,?,100,?,?,?)",
                (
                    source_job_id,
                    int(source["vmid"]),
                    now,
                    event_level,
                    event_stage,
                    f"job_{terminal_result}",
                    event_message,
                    bounded_json(
                        {
                            "snapshot_prune_job_id": prune_id,
                            "snapshot_prune_request_id": prune_request_id,
                        },
                        limit=16_000,
                    ),
                ),
            )
            event_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            conn.execute("COMMIT")
        return (
            self.get_job(source_job_id),
            self.get_job(prune_id),
            self.get_job_event(event_id),
        )

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

    def upsert_resource_state(self, vmid: int, payload: dict[str, Any]) -> dict[str, Any]:
        """Canonical 0.3 state API; the existing table remains migration-safe."""
        return self.upsert_container_state(vmid, payload)

    def get_container_state(self, vmid: int) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM container_states WHERE vmid=?", (vmid,)
            ).fetchone()
        return normalize_state(json.loads(row["payload"])) if row else None

    def get_resource_state(self, vmid: int) -> dict[str, Any] | None:
        return self.get_container_state(vmid)

    def list_container_states(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT payload FROM container_states ORDER BY vmid ASC"
            ).fetchall()
        return [normalize_state(json.loads(row["payload"])) for row in rows]

    def list_resource_states(self) -> list[dict[str, Any]]:
        return self.list_container_states()

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

    def list_resource_events(self, vmid: int, limit: int = 50) -> list[dict[str, Any]]:
        return self.list_container_events(vmid, limit)


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
