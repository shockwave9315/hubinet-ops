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
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from hubinet_ops_host_control import HostControlError, HostController

VERSION = "0.4.0"
MAX_REQUEST_BYTES = 16 * 1024
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
SNAPSHOT_PATH_RE = re.compile(
    r"^/api/v1/resources/(?P<vmid>[1-9][0-9]{1,5})/snapshots"
    r"(?:/(?P<name>[^/]+)(?:/(?P<operation>rollback))?)?$"
)
ACTION_PATH_RE = re.compile(
    r"^/api/v1/resources/(?P<vmid>[1-9][0-9]{1,5})/"
    r"(?P<action>start|shutdown|reboot|force-stop|self-update)$"
)
TERMINAL_STATUSES = {"succeeded", "failed", "interrupted"}

LOG = logging.getLogger("hubinet-ops-hostd")


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


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
    def __init__(self, path: Path) -> None:
        self.path = path
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
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(vmid, request_id)
                );
                CREATE INDEX IF NOT EXISTS idx_host_jobs_status
                    ON host_jobs(status, created_at);
                """
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

    def queued(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM host_jobs WHERE status='queued' ORDER BY created_at, id"
            ).fetchall()
        return [self._row(row) for row in rows]

    def transition(
        self,
        job_id: str,
        *,
        status: str,
        stage: str,
        progress: int,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE host_jobs SET status=?, stage=?, progress=?, result_json=?, error=?, "
                "updated_at=? WHERE id=?",
                (
                    status,
                    stage,
                    progress,
                    json.dumps(result, sort_keys=True, separators=(",", ":")) if result else None,
                    error,
                    utc_now(),
                    job_id,
                ),
            )
        return self.get(job_id)

    def reconcile(self, controller: HostController) -> list[dict[str, Any]]:
        reconciled: list[dict[str, Any]] = []
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM host_jobs WHERE status IN ('queued','running') ORDER BY created_at"
            ).fetchall()
        for row in rows:
            job = self._row(row)
            result: dict[str, Any] | None = None
            terminal = "interrupted"
            message = "hostd restarted while the operation was in progress"
            if job["operation_type"].startswith("lifecycle_"):
                try:
                    actual = controller.execute("status", job["vmid"])["status"]
                    expected = {
                        "lifecycle_start": "running",
                        "lifecycle_shutdown": "stopped",
                        "lifecycle_force_stop": "stopped",
                    }.get(job["operation_type"])
                    if expected is not None and actual == expected:
                        terminal = "succeeded"
                        message = None
                        result = {"status": actual, "reconciled": True}
                except Exception as exc:  # reconciliation must never repeat the operation
                    message = f"status reconciliation failed: {exc}"
            reconciled.append(
                self.transition(
                    job["id"],
                    status=terminal,
                    stage="complete" if terminal == "succeeded" else "interrupted",
                    progress=100,
                    result=result,
                    error=message,
                )
            )
        return reconciled

    @staticmethod
    def _row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["result"] = json.loads(result.pop("result_json")) if result["result_json"] else None
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
            if job["status"] != "queued":
                return job
            self.store.transition(job_id, status="running", stage="executing", progress=10)
            action = {
                "lifecycle_start": "start",
                "lifecycle_shutdown": "shutdown",
                "lifecycle_reboot": "reboot",
                "lifecycle_force_stop": "force-stop",
                "snapshot_create": "snapshot-create",
                "snapshot_rollback": "snapshot-rollback",
                "snapshot_delete": "snapshot-delete",
                "self_update": "self-update",
            }[job["operation_type"]]
            LOG.info(
                "host job executing id=%s request_id=%s vmid=%s operation=%s",
                job_id,
                job["request_id"],
                job["vmid"],
                job["operation_type"],
            )
            try:
                result = self.controller.execute(
                    action,
                    job["vmid"],
                    job["argument"],
                    source_job_id=job_id,
                )
            except Exception as exc:
                LOG.error("host job failed id=%s vmid=%s operation=%s", job_id, job["vmid"], job["operation_type"])
                return self.store.transition(
                    job_id,
                    status="failed",
                    stage="failed",
                    progress=100,
                    error=str(exc)[:1024],
                )
            LOG.info("host job succeeded id=%s vmid=%s operation=%s", job_id, job["vmid"], job["operation_type"])
            return self.store.transition(
                job_id,
                status="succeeded",
                stage="complete",
                progress=100,
                result=result,
            )


class HostdApplication:
    def __init__(self, config: HostdConfig, store: HostJobStore, runner: HostJobRunner, token: str) -> None:
        if len(token) < 32:
            raise ValueError("HUBINET_OPS_HOSTD_TOKEN must contain at least 32 characters")
        self.config = config
        self.store = store
        self.runner = runner
        self.token = token

    def authorize(self, headers: Any, client_ip: str) -> bool:
        if self.config.client_allowlist and client_ip not in self.config.client_allowlist:
            return False
        provided = headers.get("Authorization", "")
        return hmac.compare_digest(provided, f"Bearer {self.token}")

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
            "snapshot_rollback": "snapshot-rollback",
            "snapshot_delete": "snapshot-delete",
            "self_update": "self-update",
        }[operation_type]
        self.runner.controller.policy.validate(action, vmid, argument)
        job, created = self.store.create(
            vmid=vmid,
            operation_type=operation_type,
            request_id=request_id,
            argument=argument,
        )
        if created:
            self.runner.start(job["id"])
        return job, created


class HostdHandler(BaseHTTPRequestHandler):
    server_version = f"hubinet-ops-hostd/{VERSION}"
    app: HostdApplication

    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path == "/health":
            self._send(HTTPStatus.OK, {"status": "ok", "version": VERSION})
            return
        if not self._authorized():
            return
        if path.startswith("/api/v1/jobs/"):
            try:
                job = self.app.store.get(path.removeprefix("/api/v1/jobs/"))
            except KeyError:
                self._send(HTTPStatus.NOT_FOUND, {"error": "host job not found"})
                return
            self._send(HTTPStatus.OK, job)
            return
        match = SNAPSHOT_PATH_RE.fullmatch(path)
        if match and match.group("name") is None:
            try:
                snapshots = self.app.runner.controller.execute("list-snapshots", int(match.group("vmid")))
            except (HostControlError, ValueError) as exc:
                self._send(HTTPStatus.CONFLICT, {"error": str(exc)})
                return
            self._send(HTTPStatus.OK, snapshots)
            return
        self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if not self._authorized():
            return
        path = urlsplit(self.path).path
        action_match = ACTION_PATH_RE.fullmatch(path)
        snapshot_match = SNAPSHOT_PATH_RE.fullmatch(path)
        try:
            payload = self._body()
            request_id = str(payload.get("request_id") or uuid.uuid4().hex)
            if action_match:
                vmid = int(action_match.group("vmid"))
                operation_type = {
                    "start": "lifecycle_start",
                    "shutdown": "lifecycle_shutdown",
                    "reboot": "lifecycle_reboot",
                    "force-stop": "lifecycle_force_stop",
                    "self-update": "self_update",
                }[action_match.group("action")]
                argument = None
            elif snapshot_match and snapshot_match.group("operation") == "rollback":
                vmid = int(snapshot_match.group("vmid"))
                operation_type = "snapshot_rollback"
                argument = unquote(snapshot_match.group("name"))
            elif snapshot_match and snapshot_match.group("name") is None:
                vmid = int(snapshot_match.group("vmid"))
                operation_type = "snapshot_create"
                argument = str(payload.get("name", ""))
            else:
                self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            job, created = self.app.submit(
                vmid=vmid,
                operation_type=operation_type,
                request_id=request_id,
                argument=argument,
            )
        except ValueError as exc:
            self._send(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        except HostControlError as exc:
            self._send(HTTPStatus.CONFLICT, {"error": str(exc)})
            return
        self._send(HTTPStatus.ACCEPTED if created else HTTPStatus.OK, job)

    def do_DELETE(self) -> None:  # noqa: N802
        if not self._authorized():
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

    def _authorized(self) -> bool:
        if self.app.authorize(self.headers, self.client_address[0]):
            return True
        self._send(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
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
    config = HostdConfig.load(args.config)
    controller = HostController()
    store = HostJobStore(config.database)
    store.reconcile(controller)
    application = HostdApplication(config, store, HostJobRunner(store, controller), token)
    serve(config, application)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
