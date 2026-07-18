from pathlib import Path

from app.database import Database


def test_plan_approval_creates_job(tmp_path: Path) -> None:
    db = Database(tmp_path / "ops.db")
    plan = db.create_plan(
        vmid=101,
        container_name="cloudflared",
        fingerprint="abc",
        risk="low",
        payload={"pending_count": 1},
        ttl_minutes=60,
    )
    approved, job = db.approve_plan(plan["id"])
    assert approved["status"] == "approved"
    assert job["status"] == "queued"
    assert job["vmid"] == 101


def test_reject_plan(tmp_path: Path) -> None:
    db = Database(tmp_path / "ops.db")
    plan = db.create_plan(
        vmid=106,
        container_name="pogoda",
        fingerprint="xyz",
        risk="high",
        payload={"pending_count": 80},
        ttl_minutes=60,
    )
    rejected = db.reject_plan(plan["id"])
    assert rejected["status"] == "rejected"
    assert db.find_active_plan(106) is None


def test_container_state_roundtrip(tmp_path: Path) -> None:
    db = Database(tmp_path / "ops.db")
    written = db.upsert_container_state(
        106,
        {
            "vmid": 106,
            "status": "healthy",
            "docker": {"healthy": 3, "total": 3},
        },
    )
    loaded = db.get_container_state(106)
    assert loaded == written
    assert loaded["docker"]["healthy"] == 3
