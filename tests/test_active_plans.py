from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.config import Settings
from app.contracts import REQUIRED_APT_ACTIONS
from app.database import Database
from app.service import OpsService


EXECUTOR_HASH = "a" * 64
PROFILE_HASH = "b" * 64


class ContractExecutor:
    def __init__(
        self,
        *,
        fingerprint: str = "approved-fingerprint",
        version: str = "0.4.0",
        actions: set[str] | None = None,
    ) -> None:
        self.fingerprint = fingerprint
        self.version = version
        self.actions = set(actions if actions is not None else REQUIRED_APT_ACTIONS)
        self.calls: list[str] = []

    def run(
        self,
        action: str,
        vmid: int,
        argument: str | None = None,
        timeout: int | None = None,
        on_event: Any = None,
    ) -> dict[str, Any]:
        self.calls.append(action)
        if action == "capabilities":
            return {
                "ok": True,
                "data": {
                    "version": self.version,
                    "protocol_version": 1,
                    "supported_actions": sorted(self.actions),
                    "executor_sha256": EXECUTOR_HASH,
                    "profile_sha256": PROFILE_HASH,
                    "profile_validation_status": "valid",
                },
            }
        if action == "scan":
            return {
                "ok": True,
                "data": {
                    "pending_count": 3,
                    "packages": [{"name": "curl"}],
                    "fingerprint": self.fingerprint,
                },
            }
        return {"ok": True, "data": {}}


def settings(tmp_path: Path, *, approve: bool = True, reject: bool = True) -> Settings:
    return Settings(
        raw={
            "scheduler": {"enabled": False, "approval_ttl_minutes": 60},
            "mqtt": {"enabled": False},
            "home_assistant": {},
            "resources": {
                106: {
                    "resource_type": "lxc",
                    "adapter": "apt",
                    "name": "weather",
                    "enabled": True,
                    "monitoring": {"inspect": True, "update_scan": True},
                    "operator_capabilities": {
                        "refresh": True,
                        "scan": True,
                        "approve": approve,
                        "reject": reject,
                    },
                    "executor_contract": {
                        "executor_sha256": EXECUTOR_HASH,
                        "profile_sha256": PROFILE_HASH,
                    },
                }
            },
        },
        config_path=tmp_path / "config.yaml",
        db_path=tmp_path / "ops.db",
        api_token="t" * 64,
    )


def plan(db: Database, *, ttl: int = 60, fingerprint: str = "approved-fingerprint") -> dict[str, Any]:
    return db.create_plan(
        vmid=106,
        container_name="weather",
        fingerprint=fingerprint,
        risk="high",
        payload={"pending_count": 3, "fingerprint": fingerprint},
        ttl_minutes=ttl,
    )


def test_approve_active_finds_exact_plan_by_vmid_and_queues_idempotent_job(tmp_path: Path) -> None:
    cfg = settings(tmp_path)
    db = Database(cfg.db_path)
    expected = plan(db)
    executor = ContractExecutor()
    service = OpsService(cfg, db, executor)  # type: ignore[arg-type]

    result = service.approve_active(106, "approval-request-0001")

    assert result["plan"]["id"] == expected["id"]
    assert result["job"]["operation_type"] == "update"
    assert result["job"]["request_id"] == "approval-request-0001"
    assert executor.calls == ["capabilities", "scan"]


def test_approve_active_rejects_no_plan_multiple_plans_and_expired_plan(tmp_path: Path) -> None:
    cfg = settings(tmp_path)
    db = Database(cfg.db_path)
    service = OpsService(cfg, db, ContractExecutor())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="no active waiting plan"):
        service.approve_active(106)

    plan(db)
    plan(db)
    with pytest.raises(ValueError, match="multiple active waiting plans"):
        service.approve_active(106)

    other = Database(tmp_path / "expired.db")
    plan(other, ttl=0)
    expired_service = OpsService(cfg, other, ContractExecutor())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="no active waiting plan"):
        expired_service.approve_active(106)


def test_approve_active_rejects_changed_fingerprint_and_policy_block(tmp_path: Path) -> None:
    cfg = settings(tmp_path)
    db = Database(cfg.db_path)
    expected = plan(db)
    service = OpsService(cfg, db, ContractExecutor(fingerprint="changed"))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="fingerprint changed"):
        service.approve_active(106)
    assert db.get_plan(expected["id"])["status"] == "superseded"

    blocked_cfg = settings(tmp_path, approve=False)
    blocked_db = Database(tmp_path / "blocked.db")
    plan(blocked_db)
    blocked = OpsService(blocked_cfg, blocked_db, ContractExecutor())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="blocked by policy"):
        blocked.approve_active(106)


def test_incompatible_executor_blocks_before_snapshot_or_update(tmp_path: Path) -> None:
    cfg = settings(tmp_path)
    db = Database(cfg.db_path)
    plan(db)
    executor = ContractExecutor(
        version="0.2.1",
        actions=set(REQUIRED_APT_ACTIONS) - {"verify"},
    )
    service = OpsService(cfg, db, executor)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match=r"required 0\.4\.0/protocol 1/verify"):
        service.approve_active(106)

    assert executor.calls == ["capabilities"]
    assert "snapshot" not in executor.calls
    assert "update" not in executor.calls
    state = service.get_state(106)
    assert state["executor_compatible"] is False
    assert "verify" in state["executor_missing_actions"]


def test_reject_active_uses_vmid_and_returns_explicit_no_plan_error(tmp_path: Path) -> None:
    cfg = settings(tmp_path)
    db = Database(cfg.db_path)
    expected = plan(db)
    service = OpsService(cfg, db, ContractExecutor())  # type: ignore[arg-type]

    result = service.reject_active(106)

    assert result["plan"]["id"] == expected["id"]
    assert result["plan"]["status"] == "rejected"
    with pytest.raises(ValueError, match="no active waiting plan"):
        service.reject_active(106)
