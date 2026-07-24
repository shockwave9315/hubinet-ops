from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app.database import Database


def create_job(db: Database, vmid: int = 106) -> dict:
    plan = db.create_plan(
        vmid=vmid,
        container_name="weather",
        fingerprint="abc",
        risk="high",
        payload={"pending_count": 1},
        ttl_minutes=60,
    )
    _, job = db.approve_plan(plan["id"])
    return job


def test_plan_approval_creates_queued_job(tmp_path: Path) -> None:
    db = Database(tmp_path / "ops.db")
    job = create_job(db)
    assert job["status"] == "queued"
    assert job["progress"] == 0


def test_reject_plan(tmp_path: Path) -> None:
    db = Database(tmp_path / "ops.db")
    plan = db.create_plan(
        vmid=106,
        container_name="weather",
        fingerprint="xyz",
        risk="high",
        payload={"pending_count": 80},
        ttl_minutes=60,
    )
    assert db.reject_plan(plan["id"])["status"] == "rejected"
    assert db.find_active_plan(106) is None


def test_020_database_migrates_without_data_loss(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE plans (
          id TEXT PRIMARY KEY, vmid INTEGER NOT NULL, container_name TEXT NOT NULL,
          fingerprint TEXT NOT NULL, status TEXT NOT NULL, risk TEXT NOT NULL,
          payload TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
          approved_at TEXT
        );
        CREATE TABLE jobs (
          id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, vmid INTEGER NOT NULL,
          container_name TEXT NOT NULL, status TEXT NOT NULL, stage TEXT NOT NULL,
          snapshot_name TEXT, result TEXT, error TEXT, created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE container_states (vmid INTEGER PRIMARY KEY, payload TEXT NOT NULL, updated_at TEXT NOT NULL);
        INSERT INTO plans VALUES ('plan1',106,'weather','fp','completed','high','{}','2026-01-01','2027-01-01',NULL);
        INSERT INTO jobs VALUES ('job1','plan1',106,'weather','rolled_back','rolled_back','snap1','{}','update failed','2026-01-01','2026-01-02');
        """
    )
    legacy = {
        "vmid": 106,
        "status": "update_available",
        "health": "healthy",
        "pending_updates": 80,
        "job_status": "rolled_back",
        "job_stage": "rolled_back",
    }
    conn.execute(
        "INSERT INTO container_states VALUES (?,?,?)",
        (106, json.dumps(legacy), "2026-01-02"),
    )
    conn.commit()
    conn.close()

    db = Database(path)
    state = db.get_container_state(106)
    assert state is not None
    assert state["health_status"] == "healthy"
    assert state["update_status"] == "update_available"
    assert state["operation_status"] == "rolled_back"
    assert state["last_operation_result"] == "rolled_back"
    assert db.get_plan("plan1")["status"] == "completed"
    assert db.get_job("job1")["snapshot_name"] == "snap1"
    with sqlite3.connect(path) as migrated:
        assert migrated.execute("PRAGMA user_version").fetchone()[0] == 400
        columns = {row[1] for row in migrated.execute("PRAGMA table_info(jobs)")}
        assert {"progress", "request_id", "operation_type"} <= columns

    assert state["resource_type"] == "lxc"
    assert state["adapter"] == "apt"

    # Reopening performs the migration idempotently and preserves history.
    reopened = Database(path)
    assert reopened.get_plan("plan1")["status"] == "completed"
    assert reopened.get_job("job1")["snapshot_name"] == "snap1"
    assert reopened.get_job("job1")["operation_type"] == "update"
    assert reopened.get_job("job1")["request_id"] == "job1"
    assert reopened.get_resource_state(106)["resource_type"] == "lxc"


def test_recovery_event_schema_adds_mutation_started_marker(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy-recovery.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE processed_recovery_events (
                recovery_id TEXT PRIMARY KEY,
                request_id TEXT NOT NULL,
                vmid INTEGER NOT NULL,
                snapshot_name TEXT,
                operation_type TEXT NOT NULL,
                status TEXT NOT NULL,
                result_json TEXT,
                error TEXT,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                processed_at TEXT NOT NULL
            );
            """
        )

    Database(path)

    with sqlite3.connect(path) as conn:
        columns = {
            row[1]
            for row in conn.execute(
                "PRAGMA table_info(processed_recovery_events)"
            )
        }
    assert "mutation_started_at" in columns


def test_events_are_ordered_bounded_monotonic_and_redacted(tmp_path: Path) -> None:
    db = Database(tmp_path / "ops.db")
    job = create_job(db)
    first = db.insert_job_event(
        job_id=job["id"],
        vmid=106,
        level="info",
        stage="updating",
        progress=42,
        event_type="package",
        message="Authorization: Bearer top-secret",
        details={"api_token": "secret", "package": "curl"},
    )
    second = db.insert_job_event(
        job_id=job["id"],
        vmid=106,
        level="info",
        stage="updating",
        progress=20,
        event_type="package",
        message="next",
    )
    assert first["progress"] == 42
    assert second["progress"] == 42
    assert "top-secret" not in first["message"]
    assert first["details"]["api_token"] == "[REDACTED]"
    assert [item["id"] for item in db.list_job_events(job["id"])] == [first["id"], second["id"]]

    for index in range(205):
        db.insert_job_event(
            job_id=job["id"],
            vmid=106,
            level="info",
            stage="updating",
            progress=42,
            event_type="line",
            message=str(index),
        )
    assert len(db.list_job_events(job["id"], 999)) == 200
    assert len(db.list_container_events(106, 10)) == 10


def test_nonterminal_event_cannot_reach_100(tmp_path: Path) -> None:
    db = Database(tmp_path / "ops.db")
    job = create_job(db)
    event = db.insert_job_event(
        job_id=job["id"], vmid=106, level="info", stage="updating", progress=100,
        event_type="bad_progress", message="still running",
    )
    assert event["progress"] == 99


def test_operation_jobs_are_idempotent_and_plan_is_optional(tmp_path: Path) -> None:
    db = Database(tmp_path / "ops.db")
    job, created = db.create_operation_job(
        vmid=106,
        container_name="weather",
        operation_type="lifecycle_reboot",
        request_id="request-12345678",
    )
    same, created_again = db.create_operation_job(
        vmid=106,
        container_name="weather",
        operation_type="lifecycle_reboot",
        request_id="request-12345678",
    )
    assert created is True
    assert created_again is False
    assert same["id"] == job["id"]
    assert job["plan_id"] is None
    assert job["operation_type"] == "lifecycle_reboot"


def test_only_one_destructive_job_is_active_globally(tmp_path: Path) -> None:
    db = Database(tmp_path / "ops.db")
    db.create_operation_job(
        vmid=101,
        container_name="one",
        operation_type="snapshot_create",
        request_id="request-11111111",
    )
    with pytest.raises(ValueError, match="destructive maintenance job"):
        db.create_operation_job(
            vmid=106,
            container_name="two",
            operation_type="lifecycle_reboot",
            request_id="request-22222222",
        )
