#!/usr/bin/env python3
"""Minimal durable PVE control plane for Hubinet Ops.

The daemon intentionally uses only the Python standard library.  It exposes
typed operations, never command text, and delegates all PVE validation and
execution to ``hubinet_ops_host_control``.
"""
from __future__ import annotations

import argparse
import hmac
import json
import logging
import os
import re
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from hubinet_ops_host_control import HostControlError, HostController
from hubinet_ops_release import ReleaseError, read_marker, remove_marker, write_marker

VERSION = "0.4.1"
MAX_REQUEST_BYTES = 16 * 1024
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
SNAPSHOT_PATH_RE = re.compile(
    r"^/api/v1/resources/(?P<vmid>[1-9][0-9]{1,5})/snapshots"
    r"(?:/(?P<name>[^/]+)(?:/(?P<operation>restore|rollback))?)?$"
)
ACTION_PATH_RE = re.compile(
    r"^/api/v1/resources/(?P<vmid>[1-9][0-9]{1,5})/"
    r"(?P<action>start|shutdown|reboot|force-stop|self-update)$"
)
OFFLINE_RESTORE_PATH_RE = re.compile(
    r"^/api/v1/resources/(?P<vmid>[1-9][0-9]{1,5})/snapshots/"
    r"(?P<name>[^/]+)/offline-restore$"
)
OFFLINE_FORCE_STOP_PATH_RE = re.compile(
    r"^/api/v1/resources/(?P<vmid>[1-9][0-9]{1,5})/offline-force-stop$"
)
RECOVERY_EVENT_PATH_RE = re.compile(
    r"^/api/v1/recovery-events/(?P<recovery_id>[a-f0-9]{32})/ack$"
)
STATUS_PATH_RE = re.compile(
    r"^/api/v1/resources/(?P<vmid>[1-9][0-9]{1,5})/status$"
)
JOB_BY_REQUEST_PATH_RE = re.compile(
    r"^/api/v1/jobs/by-request/(?P<vmid>[^/]+)/(?P<request_id>[^/]+)$"
)
SELF_UPDATE_RELEASE_PATH_RE = re.compile(
    r"^/api/v1/resources/(?P<vmid>[1-9][0-9]{1,5})/self-update/release$"
)
TERMINAL_STATUSES = {"succeeded", "failed", "interrupted"}
SELF_UPDATE_LAUNCH_TIMEOUT_SECONDS = 600

LOG = logging.getLogger("hubinet-ops-hostd")


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _runtime_status(payload: Any) -> str:
    if not isinstance(payload, dict):
        raise HostControlError("status response must be an object")
    value = payload.get("lxc_status")
    if value in {None, ""}:
        value = payload.get("runtime_status")
    status = str(value or "").strip().lower()
    if status not in {"running", "stopped"}:
        raise HostControlError("status response has no valid LXC runtime state")
    return status


def _parse_timestamp(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


@dataclass(frozen=True)
class HostdConfig:
    bind: str
    port: int
    database: Path
    client_allowlist: frozenset[str]

    @classmethod
    def load(cls, path: Path) -> "HostdConfig":
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("hostd configuration must be a JSON object")
        bind = str(raw.get("bind", "")).strip()
        if not bind:
            raise ValueError("hostd bind address is required")
        port = int(raw.get("port", 8741))
        if not 1 <= port <= 65535:
            raise ValueError("hostd port must be between 1 and 65535")
        allowlist = raw.get("client_allowlist", [])
        if not isinstance(allowlist, list) or not all(isinstance(v, str) for v in allowlist):
            raise ValueError("client_allowlist must be a list of IP addresses")
        return cls(
            bind=bind,
            port=port,
            database=Path(str(raw.get("database", "/var/lib/hubinet-ops-hostd/jobs.db"))),
            client_allowlist=frozenset(allowlist),
        )


class HostJobStore:
    def __init__(
        self,
        path: Path,
        self_update_results: Path | None = None,
    ) -> None:
        self.path = path
        self.self_update_results = (
            self_update_results or path.parent / "self-update-results"
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _init_schema(self) -> None:
        with self._lock, self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS host_jobs (
                    id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL,
                    vmid INTEGER NOT NULL,
                    operation_type TEXT NOT NULL,
                    argument TEXT,
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    progress INTEGER NOT NULL,
                    result_json TEXT,
                    error TEXT,
                    launching_started_at TEXT,
                    launch_deadline_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(vmid, request_id)
                );
                CREATE INDEX IF NOT EXISTS idx_host_jobs_status
                    ON host_jobs(status, created_at);
                CREATE TABLE IF NOT EXISTS recovery_events (
                    recovery_id TEXT PRIMARY KEY,
                    host_job_id TEXT NOT NULL UNIQUE,
                    request_id TEXT NOT NULL,
                    vmid INTEGER NOT NULL,
                    snapshot_name TEXT,
                    operation_type TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    mutation_started_at TEXT,
                    status TEXT NOT NULL,
                    result_json TEXT,
                    error TEXT,
                    completed_at TEXT,
                    acknowledged_at TEXT,
                    FOREIGN KEY(host_job_id) REFERENCES host_jobs(id)
                );
                CREATE INDEX IF NOT EXISTS idx_recovery_events_unacknowledged
                    ON recovery_events(acknowledged_at, started_at);
                """
            )
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(host_jobs)").fetchall()
            }
            for name in ("launching_started_at", "launch_deadline_at"):
                if name not in columns:
                    connection.execute(f"ALTER TABLE host_jobs ADD COLUMN {name} TEXT")
            recovery_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(recovery_events)"
                ).fetchall()
            }
            if "mutation_started_at" not in recovery_columns:
                connection.execute(
                    "ALTER TABLE recovery_events ADD COLUMN mutation_started_at TEXT"
                )

    def create(
        self,
        *,
        vmid: int,
        operation_type: str,
        request_id: str,
        argument: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        if not REQUEST_ID_RE.fullmatch(request_id):
            raise ValueError("invalid request_id")
        now = utc_now()
        with self._lock, self._connect() as connection:
            existing = connection.execute(
                "SELECT * FROM host_jobs WHERE vmid=? AND request_id=?",
                (vmid, request_id),
            ).fetchone()
            if existing is not None:
                job = self._row(existing)
                if job["operation_type"] != operation_type or job["argument"] != argument:
                    raise ValueError("request_id was already used for another operation")
                return job, False
            active = connection.execute(
                "SELECT id FROM host_jobs WHERE status IN ('queued','running') LIMIT 1"
            ).fetchone()
            if active is not None:
                raise HostControlError("another destructive host job is active")
            job_id = uuid.uuid4().hex
            connection.execute(
                "INSERT INTO host_jobs "
                "(id, request_id, vmid, operation_type, argument, status, stage, progress, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'queued', 'queued', 0, ?, ?)",
                (job_id, request_id, vmid, operation_type, argument, now, now),
            )
            return self.get(job_id), True

    def get(self, job_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM host_jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return self._row(row)

    def get_by_request_id(self, vmid: int, request_id: str) -> dict[str, Any]:
        if isinstance(vmid, bool) or not 1 <= int(vmid) <= 999999:
            raise ValueError("invalid vmid")
        if not REQUEST_ID_RE.fullmatch(str(request_id)):
            raise ValueError("invalid request_id")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM host_jobs WHERE vmid=? AND request_id=?",
                (int(vmid), str(request_id)),
            ).fetchone()
        if row is None:
            raise KeyError((int(vmid), str(request_id)))
        return self._row(row)

    def create_recovery(
        self,
        *,
        vmid: int,
        operation_type: str,
        request_id: str,
        argument: str | None,
    ) -> tuple[dict[str, Any], bool]:
        if operation_type not in {
            "offline_snapshot_restore",
            "offline_force_stop",
        }:
            raise ValueError("invalid recovery operation")
        if not REQUEST_ID_RE.fullmatch(request_id):
            raise ValueError("invalid request_id")
        now = utc_now()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM host_jobs WHERE vmid=? AND request_id=?",
                (vmid, request_id),
            ).fetchone()
            if existing is not None:
                job = self._row(existing)
                if job["operation_type"] != operation_type or job["argument"] != argument:
                    connection.execute("ROLLBACK")
                    raise ValueError("request_id was already used for another operation")
                recovery = connection.execute(
                    "SELECT recovery_id FROM recovery_events WHERE host_job_id=?",
                    (job["id"],),
                ).fetchone()
                if recovery is None:
                    connection.execute("ROLLBACK")
                    raise HostControlError("recovery marker is missing for existing job")
                connection.execute("COMMIT")
                return job, False
            if connection.execute(
                "SELECT id FROM host_jobs WHERE status IN ('queued','running') LIMIT 1"
            ).fetchone():
                connection.execute("ROLLBACK")
                raise HostControlError("another destructive host job is active")
            job_id = uuid.uuid4().hex
            recovery_id = uuid.uuid4().hex
            connection.execute(
                "INSERT INTO host_jobs "
                "(id, request_id, vmid, operation_type, argument, status, stage, progress, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'queued', 'queued', 0, ?, ?)",
                (job_id, request_id, vmid, operation_type, argument, now, now),
            )
            connection.execute(
                "INSERT INTO recovery_events "
                "(recovery_id,host_job_id,request_id,vmid,snapshot_name,operation_type,"
                "started_at,status) VALUES(?,?,?,?,?,?,?,'queued')",
                (
                    recovery_id,
                    job_id,
                    request_id,
                    vmid,
                    argument if operation_type == "offline_snapshot_restore" else None,
                    operation_type,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM host_jobs WHERE id=?",
                (job_id,),
            ).fetchone()
            connection.execute("COMMIT")
        if row is None:
            raise HostControlError("failed to persist recovery host job")
        return self._row(row), True

    def list_unacknowledged_recovery_events(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM recovery_events WHERE acknowledged_at IS NULL "
                "ORDER BY started_at, recovery_id"
            ).fetchall()
        return [self._recovery_row(row) for row in rows]

    def has_recovery_event(self, host_job_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM recovery_events WHERE host_job_id=?",
                (host_job_id,),
            ).fetchone()
        return row is not None

    def acknowledge_recovery_event(self, recovery_id: str) -> dict[str, Any]:
        if not re.fullmatch(r"[a-f0-9]{32}", recovery_id):
            raise ValueError("invalid recovery_id")
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM recovery_events WHERE recovery_id=?",
                (recovery_id,),
            ).fetchone()
            if row is None:
                raise KeyError(recovery_id)
            if row["status"] not in TERMINAL_STATUSES:
                raise HostControlError("active recovery event cannot be acknowledged")
            if row["acknowledged_at"] is None:
                connection.execute(
                    "UPDATE recovery_events SET acknowledged_at=? WHERE recovery_id=?",
                    (utc_now(), recovery_id),
                )
            persisted = connection.execute(
                "SELECT * FROM recovery_events WHERE recovery_id=?",
                (recovery_id,),
            ).fetchone()
        if persisted is None:
            raise KeyError(recovery_id)
        return self._recovery_row(persisted)

    def _sync_recovery_event(self, job: dict[str, Any]) -> None:
        completed_at = utc_now() if job["status"] in TERMINAL_STATUSES else None
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE recovery_events SET status=?, result_json=?, error=?, "
                "completed_at=COALESCE(completed_at, ?) WHERE host_job_id=?",
                (
                    job["status"],
                    json.dumps(job["result"], sort_keys=True, separators=(",", ":"))
                    if job.get("result") is not None
                    else None,
                    job.get("error"),
                    completed_at,
                    job["id"],
                ),
            )

    def queued(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM host_jobs WHERE status='queued' ORDER BY created_at, id"
            ).fetchall()
        return [self._row(row) for row in rows]

    def begin_execution(self, job_id: str) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "UPDATE host_jobs SET status='running', stage='executing', progress=10, "
                "updated_at=? WHERE id=? AND status='queued'",
                (utc_now(), job_id),
            )
        if cursor.rowcount != 1:
            current = self.get(job_id)
            LOG.warning(
                "host job execution start skipped because job is no longer queued "
                "id=%s existing_status=%s",
                job_id,
                current["status"],
            )
            return current
        persisted = self.get(job_id)
        self._sync_recovery_event(persisted)
        return persisted

    def mark_recovery_mutation_started(self, job_id: str) -> dict[str, Any]:
        mutation_started_at = utc_now()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE recovery_events "
                "SET mutation_started_at=COALESCE(mutation_started_at, ?) "
                "WHERE host_job_id=? AND status='running'",
                (mutation_started_at, job_id),
            )
            if cursor.rowcount != 1:
                connection.execute("ROLLBACK")
                raise HostControlError(
                    "active recovery event is missing before destructive execution"
                )
            persisted = connection.execute(
                "SELECT * FROM recovery_events WHERE host_job_id=?",
                (job_id,),
            ).fetchone()
            connection.execute("COMMIT")
        if persisted is None:
            raise HostControlError(
                "failed to persist recovery mutation-attempt marker"
            )
        return self._recovery_row(persisted)

    def transition_from_active(
        self,
        job_id: str,
        *,
        status: str,
        stage: str,
        progress: int,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        if status not in TERMINAL_STATUSES:
            raise ValueError("transition_from_active requires a terminal status")
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "UPDATE host_jobs SET status=?, stage=?, progress=?, result_json=?, error=?, "
                "updated_at=? WHERE id=? AND status IN ('queued','running')",
                (
                    status,
                    stage,
                    progress,
                    json.dumps(result, sort_keys=True, separators=(",", ":"))
                    if result
                    else None,
                    error,
                    utc_now(),
                    job_id,
                ),
            )
        if cursor.rowcount != 1:
            current = self.get(job_id)
            LOG.warning(
                "host job transition skipped because job is no longer active "
                "id=%s requested_status=%s existing_status=%s",
                job_id,
                status,
                current["status"],
            )
            return current
        persisted = self.get(job_id)
        self._sync_recovery_event(persisted)
        return persisted

    def begin_self_update_launch(
        self,
        job_id: str,
        *,
        timeout_seconds: int = SELF_UPDATE_LAUNCH_TIMEOUT_SECONDS,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        current = self.get(job_id)
        if current["operation_type"] != "self_update" or current["status"] != "queued":
            raise HostControlError("self-update job is not queued for launch")
        started = (now or datetime.now(UTC)).replace(microsecond=0)
        deadline = started + timedelta(seconds=timeout_seconds)
        started_at = started.isoformat()
        deadline_at = deadline.isoformat()
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "UPDATE host_jobs SET status='running', stage='launching', progress=5, "
                "launching_started_at=?, launch_deadline_at=?, updated_at=? "
                "WHERE id=? AND status='queued' AND operation_type='self_update'",
                (started_at, deadline_at, utc_now(), job_id),
            )
            if cursor.rowcount != 1:
                raise HostControlError("self-update job could not enter launching state")
        try:
            write_marker(
                self.self_update_results,
                job_id,
                {
                    "job_id": job_id,
                    "status": "launching",
                    "fingerprint": str(current["argument"]),
                    "started_at": started_at,
                    "deadline_at": deadline_at,
                    "exit_code": None,
                    "error": None,
                },
            )
        except (OSError, ReleaseError) as exc:
            self.transition_from_active(
                job_id,
                status="failed",
                stage="failed",
                progress=100,
                error=f"failed to persist self-update launch marker: {exc}",
            )
            raise HostControlError(
                "failed to persist self-update launch marker"
            ) from exc
        return self.get(job_id)

    def reconcile_startup(self, controller: HostController) -> list[dict[str, Any]]:
        reconciled: list[dict[str, Any]] = []
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM host_jobs WHERE status IN ('queued','running') ORDER BY created_at"
            ).fetchall()
        for row in rows:
            reconciled.append(
                self.reconcile_startup_job(controller, self._row(row)["id"])
            )
        return reconciled

    def reconcile_startup_job(
        self,
        controller: HostController,
        job_id: str,
    ) -> dict[str, Any]:
        job = self.get(job_id)
        if job["status"] in TERMINAL_STATUSES:
            return job
        if job["operation_type"] == "self_update":
            return self.refresh_self_update_result(job_id)

        result: dict[str, Any] | None = None
        terminal = "interrupted"
        message = "hostd restarted while the operation was in progress; outcome is unknown"
        if job["operation_type"] == "lifecycle_reboot":
            message = (
                "hostd restarted during lifecycle_reboot; running state cannot prove "
                "that the reboot completed"
            )
        elif job["operation_type"] in {
            "lifecycle_start",
            "lifecycle_shutdown",
            "lifecycle_force_stop",
        }:
            try:
                payload = controller.execute("status", job["vmid"])
                actual = _runtime_status(payload)
                expected = (
                    "running"
                    if job["operation_type"] == "lifecycle_start"
                    else "stopped"
                )
                if actual == expected:
                    terminal = "succeeded"
                    message = None
                    result = {"runtime_status": actual, "reconciled": True}
                else:
                    message = (
                        f"lifecycle reconciliation observed {actual}, expected {expected}; "
                        "outcome is unknown"
                    )
            except Exception as exc:  # reconciliation must never repeat the operation
                message = f"status reconciliation failed: {exc}"
        return self.transition_from_active(
            job["id"],
            status=terminal,
            stage="complete" if terminal == "succeeded" else "interrupted",
            progress=100,
            result=result,
            error=message,
        )

    def refresh_self_update_result(self, job_id: str) -> dict[str, Any]:
        job = self.get(job_id)
        if job["operation_type"] != "self_update":
            raise HostControlError(
                "supervisor marker refresh is supported only for self-update jobs"
            )
        if job["status"] in TERMINAL_STATUSES:
            return job
        terminal = "interrupted"
        stage = "interrupted"
        result: dict[str, Any] | None = None
        message = "self-update supervisor result is missing; rollout outcome is unknown"
        remove = False
        try:
            marker = read_marker(self.self_update_results, str(job["id"]))
            if marker is None:
                launch_deadline = _parse_timestamp(job.get("launch_deadline_at"))
                if launch_deadline is not None and launch_deadline > datetime.now(UTC):
                    return job
                message = (
                    "self-update supervisor launch marker did not appear before "
                    "the launch deadline; rollout outcome is unknown"
                )
            elif marker.get("fingerprint") != job.get("argument"):
                message = (
                    "self-update supervisor fingerprint does not match the approved release; "
                    "rollout outcome is unknown"
                )
                remove = True
            elif marker.get("status") == "launching":
                deadline = _parse_timestamp(marker.get("deadline_at"))
                if deadline is not None and deadline > datetime.now(UTC):
                    return job
                message = (
                    "self-update supervisor did not finish launching before "
                    "its deadline; rollout outcome is unknown"
                )
                remove = True
            elif marker.get("status") == "running":
                deadline = _parse_timestamp(marker.get("deadline_at"))
                if deadline is not None and deadline > datetime.now(UTC):
                    return job
                message = (
                    "self-update supervisor did not publish a terminal result before "
                    "its deadline; rollout outcome is unknown"
                )
                remove = True
            elif marker.get("status") == "succeeded":
                terminal = "succeeded"
                stage = "complete"
                message = None
                result = {**marker, "supervisor_result_refreshed": True}
                remove = True
            elif marker.get("status") == "failed":
                terminal = "failed"
                stage = "failed"
                exit_code = int(marker["exit_code"])
                message = (
                    f"self-update supervisor failed with exit code {exit_code}: "
                    f"{str(marker.get('error') or 'rollout failed')[:4096]}"
                )
                result = {**marker, "supervisor_result_refreshed": True}
                remove = True
        except ReleaseError as exc:
            message = f"{exc}; rollout outcome is unknown"
            remove = True

        persisted = self.transition_from_active(
            job["id"],
            status=terminal,
            stage=stage,
            progress=100,
            result=result,
            error=message,
        )
        if remove:
            try:
                remove_marker(self.self_update_results, str(job["id"]))
            except OSError as exc:
                LOG.warning(
                    "terminal self-update marker cleanup failed job=%s: %s",
                    job["id"],
                    exc,
                )
        return persisted

    @staticmethod
    def _row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["result"] = json.loads(result.pop("result_json")) if result["result_json"] else None
        return result

    @staticmethod
    def _recovery_row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["result"] = (
            json.loads(result.pop("result_json")) if result["result_json"] else None
        )
        return result


class HostJobRunner:
    def __init__(self, store: HostJobStore, controller: HostController) -> None:
        self.store = store
        self.controller = controller
        self._lock = threading.Lock()

    def start(self, job_id: str) -> None:
        thread = threading.Thread(target=self.run, args=(job_id,), daemon=True)
        thread.start()

    def run(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self.store.get(job_id)
            if job["operation_type"] == "self_update":
                if job["status"] != "running" or job["stage"] != "launching":
                    return job
            elif job["status"] != "queued":
                return job
            else:
                started = self.store.begin_execution(job_id)
                if started["status"] != "running" or started["stage"] != "executing":
                    return started
            action = {
                "lifecycle_start": "start",
                "lifecycle_shutdown": "shutdown",
                "lifecycle_reboot": "reboot",
                "lifecycle_force_stop": "force-stop",
                "snapshot_create": "snapshot-create",
                "snapshot_create_ram": "snapshot-create-ram",
                "snapshot_rollback": "snapshot-rollback",
                "snapshot_delete": "snapshot-delete",
                "self_update": "self-update",
                "offline_snapshot_restore": "snapshot-rollback",
                "offline_force_stop": "force-stop",
            }[job["operation_type"]]
            LOG.info(
                "host job executing id=%s request_id=%s vmid=%s operation=%s",
                job_id,
                job["request_id"],
                job["vmid"],
                job["operation_type"],
            )
            try:
                if job["operation_type"] in {
                    "offline_snapshot_restore",
                    "offline_force_stop",
                }:
                    self.store.mark_recovery_mutation_started(job_id)
                result = self.controller.execute(
                    action,
                    job["vmid"],
                    job["argument"],
                    source_job_id=job_id,
                )
            except Exception as exc:
                LOG.error("host job failed id=%s vmid=%s operation=%s", job_id, job["vmid"], job["operation_type"])
                if job["operation_type"] == "self_update":
                    current = self.store.get(job_id)
                    if current["status"] in TERMINAL_STATUSES:
                        return current
                    try:
                        marker = read_marker(
                            self.store.self_update_results,
                            str(job_id),
                        )
                    except ReleaseError:
                        marker = {"status": "invalid"}
                    if marker is not None:
                        refreshed = self.store.refresh_self_update_result(job_id)
                        if refreshed["status"] in TERMINAL_STATUSES:
                            return refreshed
                failed = self.store.transition_from_active(
                    job_id,
                    status="failed",
                    stage="failed",
                    progress=100,
                    error=str(exc)[:1024],
                )
                if job["operation_type"] == "self_update":
                    try:
                        remove_marker(self.store.self_update_results, str(job_id))
                    except OSError as cleanup_error:
                        LOG.warning(
                            "failed self-update marker cleanup failed job=%s: %s",
                            job_id,
                            cleanup_error,
                        )
                return failed
            if job["operation_type"] == "self_update":
                return self.store.refresh_self_update_result(job_id)
            LOG.info("host job succeeded id=%s vmid=%s operation=%s", job_id, job["vmid"], job["operation_type"])
            return self.store.transition_from_active(
                job_id,
                status="succeeded",
                stage="complete",
                progress=100,
                result=result,
            )


class HostdApplication:
    def __init__(
        self,
        config: HostdConfig,
        store: HostJobStore,
        runner: HostJobRunner,
        token: str,
        backend_token: str,
        update_token: str,
        recovery_token: str,
    ) -> None:
        tokens = {
            "general": ("HUBINET_OPS_HOSTD_TOKEN", token),
            "backend": ("HUBINET_OPS_HOSTD_BACKEND_TOKEN", backend_token),
            "self_update": ("HUBINET_OPS_HOSTD_UPDATE_TOKEN", update_token),
            "recovery": ("HUBINET_OPS_HOSTD_RECOVERY_TOKEN", recovery_token),
        }
        for environment_name, value in tokens.values():
            if len(value) < 32:
                raise ValueError(f"{environment_name} must contain at least 32 characters")
        values = [value for _, value in tokens.values()]
        if any(
            hmac.compare_digest(left, right)
            for index, left in enumerate(values)
            for right in values[index + 1 :]
        ):
            raise ValueError("Hostd bearer tokens for separate scopes must differ")
        self.config = config
        self.store = store
        self.runner = runner
        self.tokens = {scope: value for scope, (_, value) in tokens.items()}
        self._submit_lock = threading.Lock()

    def authentication_scope(self, headers: Any, client_ip: str) -> str | None:
        if self.config.client_allowlist and client_ip not in self.config.client_allowlist:
            return None
        provided = headers.get("Authorization", "")
        for scope, token in self.tokens.items():
            if hmac.compare_digest(provided, f"Bearer {token}"):
                return scope
        return None

    def submit(
        self,
        *,
        vmid: int,
        operation_type: str,
        request_id: str,
        argument: str | None,
    ) -> tuple[dict[str, Any], bool]:
        # Validate through the same implementation used by the forced-command wrapper.
        action = {
            "lifecycle_start": "start",
            "lifecycle_shutdown": "shutdown",
            "lifecycle_reboot": "reboot",
            "lifecycle_force_stop": "force-stop",
            "snapshot_create": "snapshot-create",
            "snapshot_create_ram": "snapshot-create-ram",
            "snapshot_rollback": "snapshot-rollback",
            "snapshot_delete": "snapshot-delete",
            "self_update": "self-update",
        }[operation_type]
        self.runner.controller.policy.validate(action, vmid, argument)
        with self._submit_lock:
            job, created = self.store.create(
                vmid=vmid,
                operation_type=operation_type,
                request_id=request_id,
                argument=argument,
            )
            if created:
                if operation_type == "self_update":
                    job = self.store.begin_self_update_launch(job["id"])
                self.runner.start(job["id"])
        return job, created

    def submit_recovery(
        self,
        *,
        vmid: int,
        operation_type: str,
        request_id: str,
        argument: str | None,
    ) -> tuple[dict[str, Any], bool]:
        if vmid != 110:
            raise HostControlError("offline recovery is restricted to CT110")
        action = {
            "offline_snapshot_restore": "snapshot-rollback",
            "offline_force_stop": "force-stop",
        }[operation_type]
        self.runner.controller.policy.validate(action, vmid, argument)
        with self._submit_lock:
            try:
                existing = self.store.get_by_request_id(vmid, request_id)
            except KeyError:
                existing = None
            if existing is not None:
                if (
                    existing["operation_type"] != operation_type
                    or existing["argument"] != argument
                ):
                    raise ValueError(
                        "request_id was already used for another operation"
                    )
                if not self.store.has_recovery_event(str(existing["id"])):
                    raise HostControlError("recovery marker is missing")
                return existing, False
            if operation_type == "offline_snapshot_restore":
                status = self.runner.controller.execute("status", vmid)
                if _runtime_status(status) != "stopped":
                    raise HostControlError(
                        "offline snapshot restore requires CT110 runtime to be stopped"
                    )
                snapshots = self.runner.controller.execute("list-snapshots", vmid)
                values = snapshots.get("snapshots") if isinstance(snapshots, dict) else None
                snapshot = next(
                    (
                        item
                        for item in values or []
                        if isinstance(item, dict) and item.get("name") == argument
                    ),
                    None,
                )
                if (
                    snapshot is None
                    or snapshot.get("owned_by_hubinet_ops") is not True
                    or snapshot.get("rollback_eligible") is not True
                ):
                    raise HostControlError(
                        "offline restore requires an owned rollback-eligible snapshot"
                    )
            job, created = self.store.create_recovery(
                vmid=vmid,
                operation_type=operation_type,
                request_id=request_id,
                argument=argument,
            )
            if created:
                self.runner.start(job["id"])
        return job, created

    def get_job(self, job_id: str) -> dict[str, Any]:
        job = self.store.get(job_id)
        if (
            job["operation_type"] == "self_update"
            and job["status"] not in TERMINAL_STATUSES
        ):
            return self.store.refresh_self_update_result(job_id)
        return job

    def find_job_by_request_id(
        self,
        vmid: int,
        request_id: str,
    ) -> dict[str, Any]:
        job = self.store.get_by_request_id(vmid, request_id)
        if (
            job["operation_type"] == "self_update"
            and job["status"] not in TERMINAL_STATUSES
        ):
            return self.store.refresh_self_update_result(str(job["id"]))
        return job

    def list_recovery_events(self) -> dict[str, Any]:
        return {"events": self.store.list_unacknowledged_recovery_events()}

    def acknowledge_recovery_event(self, recovery_id: str) -> dict[str, Any]:
        return self.store.acknowledge_recovery_event(recovery_id)


class HostdHandler(BaseHTTPRequestHandler):
    server_version = f"hubinet-ops-hostd/{VERSION}"
    app: HostdApplication

    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path == "/health":
            self._send(HTTPStatus.OK, {"status": "ok", "version": VERSION})
            return
        if path == "/api/v1/recovery-events":
            if not self._authorized({"backend"}):
                return
            self._send(HTTPStatus.OK, self.app.list_recovery_events())
            return
        lookup_match = JOB_BY_REQUEST_PATH_RE.fullmatch(path)
        if lookup_match:
            if not self._authorized({"general", "backend"}):
                return
            raw_vmid = lookup_match.group("vmid")
            request_id = lookup_match.group("request_id")
            if (
                not re.fullmatch(r"[1-9][0-9]{0,5}", raw_vmid)
                or not REQUEST_ID_RE.fullmatch(request_id)
            ):
                self._send(HTTPStatus.BAD_REQUEST, {"error": "invalid job lookup"})
                return
            try:
                job = self.app.find_job_by_request_id(
                    int(raw_vmid),
                    request_id,
                )
            except KeyError:
                self._send(HTTPStatus.NOT_FOUND, {"error": "host job not found"})
                return
            except ValueError as exc:
                self._send(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            self._send(HTTPStatus.OK, job)
            return
        if path.startswith("/api/v1/jobs/"):
            if not self._authorized({"general", "backend"}):
                return
            try:
                job = self.app.get_job(path.removeprefix("/api/v1/jobs/"))
            except KeyError:
                self._send(HTTPStatus.NOT_FOUND, {"error": "host job not found"})
                return
            self._send(HTTPStatus.OK, job)
            return
        status_match = STATUS_PATH_RE.fullmatch(path)
        if status_match:
            if not self._authorized({"general", "backend"}):
                return
            try:
                result = self.app.runner.controller.execute(
                    "status", int(status_match.group("vmid"))
                )
            except (HostControlError, ValueError) as exc:
                self._send(HTTPStatus.CONFLICT, {"error": str(exc)})
                return
            self._send(HTTPStatus.OK, result)
            return
        release_match = SELF_UPDATE_RELEASE_PATH_RE.fullmatch(path)
        if release_match:
            if not self._authorized({"backend", "self_update"}):
                return
            try:
                result = self.app.runner.controller.execute(
                    "self-update-release", int(release_match.group("vmid"))
                )
            except (HostControlError, ValueError) as exc:
                message = str(exc)
                self._send(
                    HTTPStatus.CONFLICT,
                    {
                        "error": message,
                        "code": (
                            "staged_release_missing"
                            if message == "No approved Hubinet Ops release is staged"
                            else "staged_release_invalid"
                        ),
                    },
                )
                return
            self._send(HTTPStatus.OK, result)
            return
        match = SNAPSHOT_PATH_RE.fullmatch(path)
        if match and match.group("name") is None:
            if not self._authorized({"general", "backend"}):
                return
            try:
                snapshots = self.app.runner.controller.execute("list-snapshots", int(match.group("vmid")))
            except (HostControlError, ValueError) as exc:
                self._send(HTTPStatus.CONFLICT, {"error": str(exc)})
                return
            self._send(HTTPStatus.OK, snapshots)
            return
        if self._authorized({"general", "backend", "self_update", "recovery"}):
            self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        action_match = ACTION_PATH_RE.fullmatch(path)
        snapshot_match = SNAPSHOT_PATH_RE.fullmatch(path)
        offline_restore_match = OFFLINE_RESTORE_PATH_RE.fullmatch(path)
        offline_force_stop_match = OFFLINE_FORCE_STOP_PATH_RE.fullmatch(path)
        recovery_event_match = RECOVERY_EVENT_PATH_RE.fullmatch(path)
        if recovery_event_match:
            if not self._authorized({"backend"}):
                return
            try:
                event = self.app.acknowledge_recovery_event(
                    recovery_event_match.group("recovery_id")
                )
            except KeyError:
                self._send(HTTPStatus.NOT_FOUND, {"error": "recovery event not found"})
                return
            except (ValueError, HostControlError) as exc:
                self._send(HTTPStatus.CONFLICT, {"error": str(exc)})
                return
            self._send(HTTPStatus.OK, event)
            return
        required_scopes = {"recovery"} if (
            offline_restore_match or offline_force_stop_match
        ) else (
            {"self_update"}
            if action_match and action_match.group("action") == "self-update"
            else {"general", "backend"}
            if action_match and action_match.group("action") == "start"
            else {"backend"}
        )
        if not self._authorized(required_scopes):
            return
        try:
            payload = self._body()
            request_id = str(payload.get("request_id") or uuid.uuid4().hex)
            if offline_restore_match:
                if payload.get("confirm") != "RESTORE_CT110_OFFLINE":
                    raise ValueError(
                        "offline restore requires confirm=RESTORE_CT110_OFFLINE"
                    )
                job, created = self.app.submit_recovery(
                    vmid=int(offline_restore_match.group("vmid")),
                    operation_type="offline_snapshot_restore",
                    request_id=request_id,
                    argument=unquote(offline_restore_match.group("name")),
                )
            elif offline_force_stop_match:
                if payload.get("confirm") != "FORCE_STOP_CT110_RECOVERY":
                    raise ValueError(
                        "offline force-stop requires confirm=FORCE_STOP_CT110_RECOVERY"
                    )
                job, created = self.app.submit_recovery(
                    vmid=int(offline_force_stop_match.group("vmid")),
                    operation_type="offline_force_stop",
                    request_id=request_id,
                    argument=None,
                )
            elif action_match:
                vmid = int(action_match.group("vmid"))
                operation_type = {
                    "start": "lifecycle_start",
                    "shutdown": "lifecycle_shutdown",
                    "reboot": "lifecycle_reboot",
                    "force-stop": "lifecycle_force_stop",
                    "self-update": "self_update",
                }[action_match.group("action")]
                argument = (
                    str(payload.get("fingerprint") or "")
                    if operation_type == "self_update"
                    else None
                )
                job, created = self.app.submit(
                    vmid=vmid,
                    operation_type=operation_type,
                    request_id=request_id,
                    argument=argument,
                )
            elif snapshot_match and snapshot_match.group("operation") in {
                "restore",
                "rollback",
            }:
                vmid = int(snapshot_match.group("vmid"))
                operation_type = "snapshot_rollback"
                argument = unquote(snapshot_match.group("name"))
                job, created = self.app.submit(
                    vmid=vmid,
                    operation_type=operation_type,
                    request_id=request_id,
                    argument=argument,
                )
            elif snapshot_match and snapshot_match.group("name") is None:
                vmid = int(snapshot_match.group("vmid"))
                include_ram = payload.get("include_ram", False)
                if not isinstance(include_ram, bool):
                    raise ValueError("include_ram must be a boolean")
                operation_type = (
                    "snapshot_create_ram" if include_ram else "snapshot_create"
                )
                argument = str(payload.get("name", ""))
                job, created = self.app.submit(
                    vmid=vmid,
                    operation_type=operation_type,
                    request_id=request_id,
                    argument=argument,
                )
            else:
                self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
        except ValueError as exc:
            self._send(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        except HostControlError as exc:
            self._send(HTTPStatus.CONFLICT, {"error": str(exc)})
            return
        self._send(HTTPStatus.ACCEPTED if created else HTTPStatus.OK, job)

    def do_DELETE(self) -> None:  # noqa: N802
        if not self._authorized({"backend"}):
            return
        match = SNAPSHOT_PATH_RE.fullmatch(urlsplit(self.path).path)
        if not match or match.group("name") is None or match.group("operation") is not None:
            self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        try:
            payload = self._body()
            job, created = self.app.submit(
                vmid=int(match.group("vmid")),
                operation_type="snapshot_delete",
                request_id=str(payload.get("request_id") or uuid.uuid4().hex),
                argument=unquote(match.group("name")),
            )
        except ValueError as exc:
            self._send(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        except HostControlError as exc:
            self._send(HTTPStatus.CONFLICT, {"error": str(exc)})
            return
        self._send(HTTPStatus.ACCEPTED if created else HTTPStatus.OK, job)

    def _authorized(self, allowed_scopes: set[str]) -> bool:
        scope = self.app.authentication_scope(self.headers, self.client_address[0])
        if scope in allowed_scopes:
            return True
        self._send(
            HTTPStatus.FORBIDDEN if scope is not None else HTTPStatus.UNAUTHORIZED,
            {"error": "forbidden" if scope is not None else "unauthorized"},
        )
        return False

    def _body(self) -> dict[str, Any]:
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ValueError("invalid Content-Length") from exc
        if length < 0 or length > MAX_REQUEST_BYTES:
            raise ValueError("request body is too large")
        raw = self.rfile.read(length)
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("request body must be valid JSON") from exc
        if not isinstance(parsed, dict):
            raise ValueError("request body must be a JSON object")
        return parsed

    def _send(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        LOG.info("client=%s %s", self.client_address[0], format % args)


def serve(config: HostdConfig, application: HostdApplication) -> None:
    handler = type("ConfiguredHostdHandler", (HostdHandler,), {"app": application})
    server = ThreadingHTTPServer((config.bind, config.port), handler)
    LOG.info("hostd started bind=%s port=%s", config.bind, config.port)
    server.serve_forever()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path("/etc/hubinet-ops/hostd.json"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    token = os.environ.get("HUBINET_OPS_HOSTD_TOKEN", "")
    backend_token = os.environ.get("HUBINET_OPS_HOSTD_BACKEND_TOKEN", "")
    update_token = os.environ.get("HUBINET_OPS_HOSTD_UPDATE_TOKEN", "")
    recovery_token = os.environ.get("HUBINET_OPS_HOSTD_RECOVERY_TOKEN", "")
    config = HostdConfig.load(args.config)
    os.environ["HUBINET_OPS_HOSTD_DATABASE"] = str(config.database)
    controller = HostController()
    store = HostJobStore(config.database)
    os.environ["HUBINET_OPS_SELF_UPDATE_RESULTS"] = str(store.self_update_results)
    store.reconcile_startup(controller)
    application = HostdApplication(
        config,
        store,
        HostJobRunner(store, controller),
        token,
        backend_token,
        update_token,
        recovery_token,
    )
    serve(config, application)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
