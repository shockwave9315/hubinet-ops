from __future__ import annotations

import hashlib
import importlib
import json
from pathlib import Path
import threading
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import validate_config
from app.database import Database
from app.executor import ExecutorError
from app.host_control import HostControlError
from app.service import OpsService
from app.stabilization import Stabilizer
from tests.test_lifecycle_snapshots import (
    CompatibleExecutor,
    FakeHostControl,
    run_queued,
    settings,
)
from tests.test_v042_snapshot_consistency import (
    PreUpdateHost,
    SnapshotUpdateExecutor,
    _approved_update,
)
from tests.test_service import FakeClock


def _owned(
    vmid: int,
    suffix: str,
    *,
    kind: str = "manual",
    created_at: str,
) -> dict[str, Any]:
    short = "pre" if kind == "pre-update" else "manual"
    name = f"hubinet-ops-{vmid}-{short}-{suffix}"
    return {
        "name": name,
        "created_at": created_at,
        "kind": kind,
        "owned_by_hubinet_ops": True,
        "rollback_eligible": True,
        "delete_eligible": True,
        "source_job_id": hashlib.sha256(name.encode("utf-8")).hexdigest()[:32],
    }


def _record_snapshot_source(
    db: Database,
    snapshot: dict[str, Any],
    *,
    operation_type: str | None = None,
    vmid: int | None = None,
    snapshot_name: str | None = None,
) -> dict[str, Any]:
    resolved_vmid = int(vmid if vmid is not None else snapshot["name"].split("-")[2])
    resolved_operation = operation_type or (
        "update" if snapshot.get("kind") == "pre-update" else "snapshot_create"
    )
    resolved_name = snapshot_name or str(snapshot["name"])
    digest = hashlib.sha256(
        (resolved_name + resolved_operation + str(resolved_vmid)).encode("utf-8")
    ).hexdigest()
    source_id = f"ownership{digest[:23]}"
    result = (
        {"source_job_id": snapshot.get("source_job_id")}
        if resolved_operation in {"snapshot_create", "snapshot_create_ram"}
        else {"pre_update_snapshot": True}
    )
    now = "2026-07-29T12:00:00+00:00"
    with db._lock, db._connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO jobs "
            "(id,request_id,operation_type,plan_id,vmid,container_name,status,stage,"
            "progress,snapshot_name,result,error,created_at,updated_at) "
            "VALUES(?,?,?,NULL,?,?,'success','completed',100,?,?,NULL,?,?)",
            (
                source_id,
                f"ownership-{digest[:32]}",
                resolved_operation,
                resolved_vmid,
                f"ct-{resolved_vmid}",
                resolved_name,
                json.dumps(result, sort_keys=True),
                now,
                now,
            ),
        )
    if resolved_operation == "update":
        db.update_job(
            source_id,
            status="running",
            stage="snapshot",
            progress=24,
        )
        db.record_pre_update_snapshot_proof(
            source_id,
            resolved_vmid,
            resolved_name,
            str(snapshot["source_job_id"]),
        )
        db.update_job(source_id, status="success", stage="completed", progress=100)
    return db.get_job(source_id)


def _record_snapshot_sources(
    db: Database,
    snapshots: list[dict[str, Any]],
) -> None:
    for snapshot in snapshots:
        if snapshot.get("owned_by_hubinet_ops") is True:
            _record_snapshot_source(db, snapshot)


def test_name_only_and_unrecorded_snapshot_candidates_remain_uncertain(
    tmp_path: Path,
) -> None:
    cfg = settings(tmp_path)
    db = Database(cfg.db_path)
    host = FakeHostControl()
    name_only = _owned(
        106,
        "20260729T120000Z",
        created_at="2026-07-29T12:00:00+00:00",
    )
    name_only.update(
        owned_by_hubinet_ops=False,
        source_job_id=None,
    )
    missing_record = _owned(
        106,
        "20260728T120000Z",
        created_at="2026-07-28T12:00:00+00:00",
    )
    host.snapshots = [name_only, missing_record]
    service = OpsService(cfg, db, CompatibleExecutor(), host_control=host)

    listing = service.list_snapshots(106)

    assert all(
        snapshot["owned_by_hubinet_ops"] is False
        and snapshot["ownership_status"] == "uncertain"
        and snapshot["delete_eligible"] is False
        and snapshot["protected"] is True
        for snapshot in listing["snapshots"]
    )
    with pytest.raises(ValueError, match="does not exist"):
        service.queue_snapshot_action(
            106,
            "delete",
            name_only["name"],
            "reject-name-only-delete-0001",
        )
    service.queue_snapshot_prune(106, "oldest", "skip-uncertain-oldest-0001")
    terminal = run_queued(service, db)
    assert terminal["result"]["deleted"] == []
    assert not any(call[0] == "snapshot_delete" for call in host.calls)


@pytest.mark.parametrize(
    ("mismatch", "operation_type", "vmid", "name_suffix"),
    [
        ("vmid", "snapshot_create", 101, ""),
        ("snapshot_name", "snapshot_create", 106, "-different"),
        ("operation_type", "snapshot_delete", 106, ""),
    ],
)
def test_snapshot_candidate_requires_exact_durable_source_contract(
    tmp_path: Path,
    mismatch: str,
    operation_type: str,
    vmid: int,
    name_suffix: str,
) -> None:
    case_dir = tmp_path / mismatch
    case_dir.mkdir()
    cfg = settings(case_dir)
    db = Database(cfg.db_path)
    host = FakeHostControl()
    snapshot = _owned(
        106,
        "20260729T121000Z",
        created_at="2026-07-29T12:10:00+00:00",
    )
    host.snapshots = [snapshot]
    _record_snapshot_source(
        db,
        snapshot,
        operation_type=operation_type,
        vmid=vmid,
        snapshot_name=str(snapshot["name"]) + name_suffix,
    )
    service = OpsService(cfg, db, CompatibleExecutor(), host_control=host)

    modeled = service.list_snapshots(106)["snapshots"][0]

    assert modeled["owned_by_hubinet_ops"] is False
    assert modeled["ownership_status"] == "uncertain"
    assert modeled["delete_eligible"] is False


@pytest.mark.parametrize("operation_type", ["snapshot_create", "snapshot_create_ram"])
def test_manual_snapshot_requires_matching_host_and_backend_job(
    tmp_path: Path,
    operation_type: str,
) -> None:
    case_dir = tmp_path / operation_type
    case_dir.mkdir()
    cfg = settings(case_dir)
    db = Database(cfg.db_path)
    host = FakeHostControl()
    snapshot = _owned(
        106,
        "20260729T122000Z",
        created_at="2026-07-29T12:20:00+00:00",
    )
    host.snapshots = [snapshot]
    source = _record_snapshot_source(
        db,
        snapshot,
        operation_type=operation_type,
    )
    service = OpsService(cfg, db, CompatibleExecutor(), host_control=host)

    modeled = service.list_snapshots(106)["snapshots"][0]

    assert modeled["owned_by_hubinet_ops"] is True
    assert modeled["ownership_status"] == "owned"
    assert modeled["source_job_id"] == source["id"]
    assert modeled["host_source_job_id"] == snapshot["source_job_id"]
    assert modeled["delete_eligible"] is True


def test_pre_update_snapshot_uses_exact_update_job_and_remains_rollback_eligible(
    tmp_path: Path,
) -> None:
    cfg = settings(tmp_path)
    db = Database(cfg.db_path)
    host = FakeHostControl("running")
    snapshot = _owned(
        106,
        "20260729T123000Z",
        kind="pre-update",
        created_at="2026-07-29T12:30:00+00:00",
    )
    host.snapshots = [snapshot]
    source = _record_snapshot_source(db, snapshot)
    db.update_job(source["id"], status="failed")
    service = OpsService(cfg, db, CompatibleExecutor(), host_control=host)

    modeled = service.list_snapshots(106)["snapshots"][0]
    queued = service.queue_snapshot_action(
        106,
        "rollback",
        snapshot["name"],
        "valid-pre-update-rollback-0001",
    )

    assert modeled["owned_by_hubinet_ops"] is True
    assert modeled["rollback_eligible"] is True
    assert queued["snapshot_name"] == snapshot["name"]


def test_pre_update_snapshot_requires_durable_mutation_event_and_prune_skips_it(
    tmp_path: Path,
) -> None:
    cfg = settings(tmp_path)
    db = Database(cfg.db_path)
    host = FakeHostControl("running")
    snapshot = _owned(
        106,
        "20260729T124000Z",
        kind="pre-update",
        created_at="2026-07-29T12:40:00+00:00",
    )
    host.snapshots = [snapshot]
    source, _ = db.create_operation_job(
        vmid=106,
        container_name="ct-106",
        operation_type="update",
        request_id="pre-update-without-proof-0001",
        snapshot_name=snapshot["name"],
    )
    db.update_job(source["id"], status="failed", stage="failed", progress=100)
    service = OpsService(cfg, db, CompatibleExecutor(), host_control=host)

    modeled = service.list_snapshots(106)["snapshots"][0]

    assert modeled["owned_by_hubinet_ops"] is False
    assert modeled["ownership_status"] == "uncertain"
    assert modeled["protected"] is True
    assert modeled["delete_eligible"] is False
    assert modeled["rollback_eligible"] is False
    service.queue_snapshot_prune(106, "oldest", "skip-unproven-pre-update-0001")
    terminal = run_queued(service, db)
    assert terminal["result"]["deleted"] == []
    assert not any(call[0] == "snapshot_delete" for call in host.calls)


def test_backend_snapshot_proof_confirms_ownership_and_survives_result_update_and_restart(
    tmp_path: Path,
) -> None:
    cfg = settings(tmp_path)
    db = Database(cfg.db_path)
    host = FakeHostControl("running")
    snapshot = _owned(
        106,
        "20260729T125000Z",
        kind="pre-update",
        created_at="2026-07-29T12:50:00+00:00",
    )
    host.snapshots = [snapshot]
    source, _ = db.create_operation_job(
        vmid=106,
        container_name="ct-106",
        operation_type="update",
        request_id="pre-update-mutation-marker-0001",
        snapshot_name=snapshot["name"],
    )
    proven = db.record_pre_update_snapshot_proof(
        source["id"],
        106,
        snapshot["name"],
        str(snapshot["source_job_id"]),
    )
    proof = dict(proven["result"]["snapshot_proof"])

    before_final_event = OpsService(
        cfg, db, CompatibleExecutor(), host_control=host
    )._refresh_snapshot_state(106, required_name=snapshot["name"], required_kind="pre-update")
    terminal = db.update_job(
        source["id"],
        status="success",
        stage="completed",
        progress=100,
        result={
            "verification": {"status": "passed"},
            "snapshot_proof": {
                "version": 1,
                "vmid": 999,
                "snapshot_name": "attacker-controlled",
                "kind": "pre-update",
            },
        },
    )
    restarted = OpsService(cfg, db, CompatibleExecutor(), host_control=host)
    after_restart = restarted.list_snapshots(106)["snapshots"][0]

    assert before_final_event["snapshots"][0]["owned_by_hubinet_ops"] is True
    assert terminal["result"]["snapshot_proof"] == proof
    assert terminal["result"]["verification"] == {"status": "passed"}
    assert after_restart["owned_by_hubinet_ops"] is True
    assert after_restart["source_job_id"] == source["id"]


def test_version_one_snapshot_proof_is_preserved_but_never_authorizes_ownership(
    tmp_path: Path,
) -> None:
    cfg = settings(tmp_path)
    db = Database(cfg.db_path)
    host = FakeHostControl("running")
    snapshot = _owned(
        106,
        "20260729T125300Z",
        kind="pre-update",
        created_at="2026-07-29T12:53:00+00:00",
    )
    host.snapshots = [snapshot]
    source, _ = db.create_operation_job(
        vmid=106,
        container_name="ct-106",
        operation_type="update",
        request_id="legacy-version-one-proof-0001",
        snapshot_name=snapshot["name"],
    )
    version_one = {
        "version": 1,
        "vmid": 106,
        "snapshot_name": snapshot["name"],
        "kind": "pre-update",
    }
    with db._lock, db._connect() as conn:
        conn.execute(
            "UPDATE jobs SET result=? WHERE id=?",
            (json.dumps({"snapshot_proof": version_one}), source["id"]),
        )
    terminal = db.update_job(
        source["id"],
        status="blocked",
        stage="failed",
        progress=100,
        result={"terminal_result": "failed"},
    )
    service = OpsService(cfg, db, CompatibleExecutor(), host_control=host)

    modeled = service.list_snapshots(106)["snapshots"][0]

    assert terminal["result"]["snapshot_proof"] == version_one
    assert modeled["owned_by_hubinet_ops"] is False
    assert modeled["ownership_status"] == "uncertain"
    assert modeled["protected"] is True
    assert modeled["rollback_eligible"] is False
    assert modeled["delete_eligible"] is False
    with pytest.raises(ValueError, match="missing, foreign, or ineligible"):
        service.manual_rollback(106)
    service.queue_snapshot_prune(106, "oldest", "version-one-proof-prune-0001")
    prune = run_queued(service, db)
    assert prune["result"]["deleted"] == []
    assert not any(call[0] == "snapshot_delete" for call in host.calls)


def test_version_one_snapshot_proof_cannot_authorize_automatic_rollback(
    tmp_path: Path,
) -> None:
    cfg = settings(tmp_path)
    db = Database(cfg.db_path)
    host = FakeHostControl("running")
    snapshot = _owned(
        106,
        "20260729T125330Z",
        kind="pre-update",
        created_at="2026-07-29T12:53:30+00:00",
    )
    host.snapshots = [snapshot]
    plan = db.create_plan(
        vmid=106,
        container_name="ct-106",
        fingerprint="version-one-automatic-rollback",
        risk="high",
        payload={"pending_count": 1},
        ttl_minutes=60,
    )
    _, source = db.approve_plan(plan["id"])
    db.update_job(
        source["id"],
        status="running",
        stage="updating",
        snapshot_name=snapshot["name"],
    )
    version_one = {
        "version": 1,
        "vmid": 106,
        "snapshot_name": snapshot["name"],
        "kind": "pre-update",
    }
    with db._lock, db._connect() as conn:
        conn.execute(
            "UPDATE jobs SET result=? WHERE id=?",
            (json.dumps({"snapshot_proof": version_one}), source["id"]),
        )
    executor = CompatibleExecutor()
    service = OpsService(cfg, db, executor, host_control=host)

    service._rollback(db.get_job(source["id"]), "simulated update failure")

    terminal = db.get_job(source["id"])
    assert terminal["status"] == "failed"
    assert terminal["result"]["snapshot_proof"] == version_one
    assert "rollback" not in executor.calls
    assert not any(call[0] == "snapshot_rollback" for call in host.calls)


@pytest.mark.parametrize(
    "replacement_source_job_id",
    ["b" * 32, ""],
    ids=["different-source", "empty-source"],
)
def test_stale_pre_update_proof_never_authorizes_recreated_snapshot(
    tmp_path: Path,
    replacement_source_job_id: str,
) -> None:
    cfg = settings(tmp_path)
    db = Database(cfg.db_path)
    host = FakeHostControl("running")
    snapshot = _owned(
        106,
        "20260729T125400Z",
        kind="pre-update",
        created_at="2026-07-29T12:54:00+00:00",
    )
    original_source_job_id = "a" * 32
    snapshot["source_job_id"] = original_source_job_id
    host.snapshots = [snapshot]
    source, _ = db.create_operation_job(
        vmid=106,
        container_name="ct-106",
        operation_type="update",
        request_id="stale-pre-update-proof-0001",
        snapshot_name=snapshot["name"],
    )
    db.record_pre_update_snapshot_proof(
        source["id"],
        106,
        snapshot["name"],
        original_source_job_id,
    )
    db.update_job(source["id"], status="blocked", stage="failed", progress=100)
    service = OpsService(cfg, db, CompatibleExecutor(), host_control=host)
    assert service.list_snapshots(106)["snapshots"][0]["owned_by_hubinet_ops"] is True

    replacement = dict(snapshot)
    replacement["source_job_id"] = replacement_source_job_id
    host.snapshots = [replacement]
    modeled = service.list_snapshots(106)["snapshots"][0]

    assert replacement_source_job_id != original_source_job_id
    assert modeled["owned_by_hubinet_ops"] is False
    assert modeled["ownership_status"] == "uncertain"
    assert modeled["rollback_eligible"] is False
    assert modeled["delete_eligible"] is False
    with pytest.raises(ValueError, match="missing, foreign, or ineligible"):
        service.manual_rollback(106)
    service.queue_snapshot_prune(106, "oldest", "stale-proof-prune-0001")
    prune = run_queued(service, db)
    assert prune["result"]["deleted"] == []
    assert not any(call[0] == "snapshot_delete" for call in host.calls)


@pytest.mark.parametrize(
    ("event_type", "details"),
    [
        ("snapshot_created", None),
        ("snapshot_created", {"snapshot_name": "exact"}),
        ("snapshot_mutation_succeeded", {"snapshot_name": "exact"}),
    ],
)
def test_historical_snapshot_events_never_authorize_legacy_snapshot(
    tmp_path: Path,
    event_type: str,
    details: dict[str, Any] | None,
) -> None:
    cfg = settings(tmp_path)
    db = Database(cfg.db_path)
    host = FakeHostControl("running")
    snapshot = _owned(
        106,
        "20260729T125500Z",
        kind="pre-update",
        created_at="2026-07-29T12:55:00+00:00",
    )
    host.snapshots = [snapshot]
    source, _ = db.create_operation_job(
        vmid=106,
        container_name="ct-106",
        operation_type="update",
        request_id="invalid-pre-update-proof-details-0001",
        snapshot_name=snapshot["name"],
    )
    resolved_details = (
        {"snapshot_name": snapshot["name"]}
        if details == {"snapshot_name": "exact"}
        else details
    )
    db.insert_job_event(
        job_id=source["id"],
        vmid=106,
        level="info",
        stage="snapshot",
        progress=24,
        event_type=event_type,
        message="Historical snapshot event",
        details=resolved_details,
    )
    source = db.update_job(
        source["id"], status="failed", stage="failed", progress=100
    )
    before = dict(source)
    service = OpsService(cfg, db, CompatibleExecutor(), host_control=host)

    modeled = service.list_snapshots(106)["snapshots"][0]

    assert modeled["owned_by_hubinet_ops"] is False
    assert modeled["ownership_status"] == "uncertain"
    assert modeled["protected"] is True
    assert modeled["delete_eligible"] is False
    assert modeled["rollback_eligible"] is False
    with pytest.raises(ValueError, match="missing, foreign, or ineligible"):
        service.manual_rollback(106)
    assert db.get_job(source["id"]) == before
    service.queue_snapshot_prune(106, "oldest", "skip-historical-event-0001")
    prune = run_queued(service, db)
    assert prune["result"]["deleted"] == []
    assert not any(call[0] == "snapshot_delete" for call in host.calls)


def test_generic_job_result_cannot_create_snapshot_proof(
    tmp_path: Path,
) -> None:
    cfg = settings(tmp_path)
    db = Database(cfg.db_path)
    host = FakeHostControl("running")
    snapshot = _owned(
        106,
        "20260729T125900Z",
        kind="pre-update",
        created_at="2026-07-29T12:59:00+00:00",
    )
    host.snapshots = [snapshot]
    source, _ = db.create_operation_job(
        vmid=106,
        container_name="ct-106",
        operation_type="update",
        request_id="spoofed-result-snapshot-proof-0001",
        snapshot_name=snapshot["name"],
    )
    updated = db.update_job(
        source["id"],
        result={
            "snapshot_proof": {
                "version": 1,
                "vmid": 106,
                "snapshot_name": snapshot["name"],
                "kind": "pre-update",
            },
            "executor_data": "untrusted",
        },
    )

    modeled = OpsService(
        cfg, db, CompatibleExecutor(), host_control=host
    ).list_snapshots(106)["snapshots"][0]

    assert updated["result"] == {"executor_data": "untrusted"}
    assert modeled["owned_by_hubinet_ops"] is False
    assert modeled["ownership_status"] == "uncertain"


def test_malformed_active_update_result_blocks_proof_persistence_fail_closed(
    tmp_path: Path,
) -> None:
    cfg = settings(tmp_path)
    db = Database(cfg.db_path)
    host = FakeHostControl("running")
    snapshot = _owned(
        106,
        "20260729T130100Z",
        kind="pre-update",
        created_at="2026-07-29T13:01:00+00:00",
    )
    host.snapshots = [snapshot]
    source, _ = db.create_operation_job(
        vmid=106,
        container_name="ct-106",
        operation_type="update",
        request_id="malformed-active-update-result-0001",
        snapshot_name=snapshot["name"],
    )
    db.update_job(source["id"], result="{malformed-json")

    with pytest.raises(ValueError, match="result is malformed"):
        db.record_pre_update_snapshot_proof(
            source["id"],
            106,
            snapshot["name"],
            str(snapshot["source_job_id"]),
        )

    modeled = OpsService(
        cfg, db, CompatibleExecutor(), host_control=host
    ).list_snapshots(106)["snapshots"][0]
    assert modeled["owned_by_hubinet_ops"] is False
    assert modeled["protected"] is True
    assert modeled["delete_eligible"] is False


@pytest.mark.parametrize(
    "malformed_result",
    ["legacy-text", ["source_job_id", "wrong"], 7, None, "{not-json"],
)
def test_malformed_manual_snapshot_result_is_uncertain_and_never_pruned(
    tmp_path: Path,
    malformed_result: Any,
) -> None:
    cfg = settings(tmp_path)
    db = Database(cfg.db_path)
    host = FakeHostControl()
    snapshot = _owned(
        106,
        "20260729T130000Z",
        created_at="2026-07-29T13:00:00+00:00",
    )
    host.snapshots = [snapshot]
    source, _ = db.create_operation_job(
        vmid=106,
        container_name="ct-106",
        operation_type="snapshot_create",
        request_id="malformed-manual-source-0001",
        snapshot_name=snapshot["name"],
    )
    db.update_job(source["id"], status="success", result=malformed_result)
    service = OpsService(cfg, db, CompatibleExecutor(), host_control=host)

    modeled = service.list_snapshots(106)["snapshots"][0]

    assert modeled["owned_by_hubinet_ops"] is False
    assert modeled["ownership_status"] == "uncertain"
    assert modeled["protected"] is True
    assert modeled["delete_eligible"] is False
    assert modeled["rollback_eligible"] is False
    service.queue_snapshot_prune(106, "oldest", "skip-malformed-manual-0001")
    terminal = run_queued(service, db)
    assert terminal["result"]["deleted"] == []
    assert not any(call[0] == "snapshot_delete" for call in host.calls)


def test_malformed_newer_candidate_does_not_hide_valid_manual_source(
    tmp_path: Path,
) -> None:
    cfg = settings(tmp_path)
    db = Database(cfg.db_path)
    host = FakeHostControl()
    snapshot = _owned(
        106,
        "20260729T131000Z",
        created_at="2026-07-29T13:10:00+00:00",
    )
    host.snapshots = [snapshot]
    valid, _ = db.create_operation_job(
        vmid=106,
        container_name="ct-106",
        operation_type="snapshot_create",
        request_id="valid-older-manual-source-0001",
        snapshot_name=snapshot["name"],
    )
    db.update_job(
        valid["id"],
        status="success",
        result={"source_job_id": snapshot["source_job_id"]},
    )
    malformed, _ = db.create_operation_job(
        vmid=106,
        container_name="ct-106",
        operation_type="snapshot_create",
        request_id="malformed-newer-manual-source-0001",
        snapshot_name=snapshot["name"],
    )
    db.update_job(malformed["id"], status="success", result="legacy-text")
    service = OpsService(cfg, db, CompatibleExecutor(), host_control=host)

    modeled = service.list_snapshots(106)["snapshots"][0]

    assert modeled["owned_by_hubinet_ops"] is True
    assert modeled["source_job_id"] == valid["id"]
    assert modeled["delete_eligible"] is True


def test_snapshot_list_exposes_complete_managed_model_and_protection(
    tmp_path: Path,
) -> None:
    cfg = settings(tmp_path)
    db = Database(cfg.db_path)
    host = FakeHostControl()
    protected = _owned(
        106,
        "20260728T120000Z",
        created_at="2026-07-28T12:00:00+00:00",
    )
    foreign = {
        "name": "operator-backup",
        "created_at": "2026-07-27T12:00:00+00:00",
        "owned_by_hubinet_ops": False,
        "delete_eligible": True,
    }
    host.snapshots = [protected, foreign]
    source = _record_snapshot_source(db, protected)
    failed, _ = db.create_operation_job(
        vmid=106,
        container_name="ct-106",
        operation_type="snapshot_delete",
        request_id="failed-snapshot-source-0001",
        snapshot_name=protected["name"],
    )
    db.update_job(failed["id"], status="failed")
    service = OpsService(cfg, db, CompatibleExecutor(), host_control=host)

    listing = service.list_snapshots(106)

    managed = listing["snapshots"][0]
    assert managed["physical_name"] == protected["name"]
    assert managed["logical_type"] == "manual"
    assert managed["vmid"] == 106
    assert isinstance(managed["age_seconds"], int)
    assert managed["protected"] is True
    assert managed["protection_reason"] == "manual_rollback_source"
    assert managed["source_job_id"] == source["id"]
    assert managed["owned_by_hubinet_ops"] is True
    assert listing["snapshots"][1]["ownership_status"] == "foreign"


def test_retention_keeps_exact_count_and_never_deletes_foreign_or_protected(
    tmp_path: Path,
) -> None:
    cfg = settings(tmp_path)
    cfg.raw["resources"][106]["snapshot_retention_count"] = 2
    cfg.raw["resources"][106].pop("snapshot_retention", None)
    db = Database(cfg.db_path)
    host = FakeHostControl()
    snapshots = [
        _owned(106, "20260729T120000Z", created_at="2026-07-29T12:00:00+00:00"),
        _owned(106, "20260728T120000Z", created_at="2026-07-28T12:00:00+00:00"),
        _owned(106, "20260727T120000Z", created_at="2026-07-27T12:00:00+00:00"),
        _owned(106, "20260726T120000Z", created_at="2026-07-26T12:00:00+00:00"),
    ]
    foreign = {
        "name": "foreign-snapshot",
        "created_at": "2026-07-20T12:00:00+00:00",
        "owned_by_hubinet_ops": False,
        "delete_eligible": True,
    }
    host.snapshots = snapshots + [foreign]
    _record_snapshot_sources(db, snapshots)
    protected, _ = db.create_operation_job(
        vmid=106,
        container_name="ct-106",
        operation_type="snapshot_delete",
        request_id="protected-failed-snapshot-0001",
        snapshot_name=snapshots[-1]["name"],
    )
    db.update_job(protected["id"], status="failed")
    current, _ = db.create_operation_job(
        vmid=106,
        container_name="ct-106",
        operation_type="snapshot_create",
        request_id="retention-current-job-0001",
        snapshot_name=snapshots[0]["name"],
    )
    service = OpsService(cfg, db, CompatibleExecutor(), host_control=host)

    service._terminal_with_snapshot_retention(
        current,
        job_status="success",
        result="success",
        error=None,
    )

    remaining = {item["name"] for item in host.snapshots}
    assert snapshots[0]["name"] in remaining
    assert snapshots[1]["name"] in remaining
    assert snapshots[2]["name"] not in remaining
    assert snapshots[3]["name"] in remaining
    assert foreign["name"] in remaining


def test_retention_zero_disables_automatic_pruning(tmp_path: Path) -> None:
    cfg = settings(tmp_path)
    cfg.raw["resources"][106]["snapshot_retention_count"] = 0
    cfg.raw["resources"][106].pop("snapshot_retention", None)
    db = Database(cfg.db_path)
    host = FakeHostControl()
    host.snapshots = [
        _owned(106, "20260729T120000Z", created_at="2026-07-29T12:00:00+00:00"),
        _owned(106, "20260728T120000Z", created_at="2026-07-28T12:00:00+00:00"),
    ]
    job, _ = db.create_operation_job(
        vmid=106,
        container_name="ct-106",
        operation_type="snapshot_create",
        request_id="retention-disabled-job-0001",
        snapshot_name=host.snapshots[0]["name"],
    )
    service = OpsService(cfg, db, CompatibleExecutor(), host_control=host)

    service._terminal_with_snapshot_retention(
        job,
        job_status="success",
        result="success",
        error=None,
    )

    assert len(host.snapshots) == 2
    assert not any(call[0] == "snapshot_delete" for call in host.calls)


@pytest.mark.parametrize("value", [-1, 101, 1.5, "3"])
def test_snapshot_retention_count_validation_rejects_invalid_values(
    value: Any,
) -> None:
    config = {
        "resources": {
            106: {
                "enabled": True,
                "resource_type": "lxc",
                "adapter": "apt",
                "monitoring": {"inspect": True, "update_scan": True},
                "operator_capabilities": {"snapshot_list": True},
                "snapshot_retention_count": value,
                "executor_contract": {
                    "executor_sha256": "a" * 64,
                    "profile_sha256": "b" * 64,
                },
            }
        }
    }
    with pytest.raises(RuntimeError, match="snapshot_retention_count"):
        validate_config(config)


def test_bulk_delete_requires_exact_confirmation_and_rechecks_protection(
    tmp_path: Path,
) -> None:
    cfg = settings(tmp_path)
    db = Database(cfg.db_path)
    host = FakeHostControl()
    newest = _owned(106, "20260729T120000Z", created_at="2026-07-29T12:00:00+00:00")
    protected = _owned(106, "20260728T120000Z", created_at="2026-07-28T12:00:00+00:00")
    oldest = _owned(106, "20260727T120000Z", created_at="2026-07-27T12:00:00+00:00")
    host.snapshots = [newest, protected, oldest]
    _record_snapshot_sources(db, host.snapshots)
    failed, _ = db.create_operation_job(
        vmid=106,
        container_name="ct-106",
        operation_type="snapshot_delete",
        request_id="bulk-protected-source-0001",
        snapshot_name=protected["name"],
    )
    db.update_job(failed["id"], status="failed")
    service = OpsService(cfg, db, CompatibleExecutor(), host_control=host)

    with pytest.raises(ValueError, match="Exact confirmation"):
        service.queue_snapshot_prune(
            106,
            "all_unprotected",
            "bulk-delete-wrong-confirm-0001",
            confirmation="yes",
        )
    queued = service.queue_snapshot_prune(
        106,
        "all_unprotected",
        "bulk-delete-confirmed-0001",
        confirmation="DELETE_ALL_UNPROTECTED",
    )
    terminal = run_queued(service, db)

    assert terminal["status"] == "success"
    assert set(terminal["result"]["deleted"]) == {newest["name"], oldest["name"]}
    assert {item["name"] for item in host.snapshots} == {protected["name"]}
    assert queued["operation_type"] == "snapshot_prune"


def test_delete_oldest_removes_only_one_unprotected_snapshot(tmp_path: Path) -> None:
    cfg = settings(tmp_path)
    db = Database(cfg.db_path)
    host = FakeHostControl()
    host.snapshots = [
        _owned(106, "20260729T120000Z", created_at="2026-07-29T12:00:00+00:00"),
        _owned(106, "20260728T120000Z", created_at="2026-07-28T12:00:00+00:00"),
        _owned(106, "20260727T120000Z", created_at="2026-07-27T12:00:00+00:00"),
    ]
    _record_snapshot_sources(db, host.snapshots)
    service = OpsService(cfg, db, CompatibleExecutor(), host_control=host)

    service.queue_snapshot_prune(106, "oldest", "delete-oldest-managed-0001")
    terminal = run_queued(service, db)

    assert terminal["result"]["deleted"] == [
        "hubinet-ops-106-manual-20260727T120000Z"
    ]
    assert len(host.snapshots) == 2


def test_snapshot_api_returns_full_list_and_exposes_safe_bulk_operations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = settings(tmp_path)
    db = Database(cfg.db_path)
    host = FakeHostControl()
    host.snapshots = [
        _owned(106, "20260729T120000Z", created_at="2026-07-29T12:00:00+00:00")
    ]
    _record_snapshot_sources(db, host.snapshots)
    import_config = tmp_path / "import-config.yaml"
    import_config.write_text(
        "scheduler:\n  enabled: false\ncontainers:\n  106:\n    enabled: true\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HUBINET_OPS_CONFIG", str(import_config))
    monkeypatch.setenv("HUBINET_OPS_DB", str(tmp_path / "import.db"))
    monkeypatch.setenv("HUBINET_OPS_API_TOKEN", "i" * 64)
    main = importlib.import_module("app.main")
    client = TestClient(
        main.create_app(
            cfg,
            database=db,
            executor=CompatibleExecutor(),
            host_control=host,
        )
    )
    headers = {"Authorization": f"Bearer {cfg.api_token}"}

    listing = client.get("/api/v1/resources/106/snapshots", headers=headers)
    wrong = client.post(
        "/api/v1/resources/106/snapshots/delete-unprotected",
        headers=headers,
        json={
            "request_id": "api-bulk-wrong-confirm-0001",
            "confirm": "DELETE",
        },
    )
    queued = client.post(
        "/api/v1/resources/106/snapshots/delete-oldest",
        headers=headers,
        json={"request_id": "api-delete-oldest-0001"},
    )

    assert listing.status_code == 200
    assert listing.json()["snapshots"][0]["physical_name"] == host.snapshots[0]["name"]
    assert wrong.status_code == 409
    assert "DELETE_ALL_UNPROTECTED" in wrong.json()["detail"]
    assert queued.status_code == 200
    assert queued.json()["operation_type"] == "snapshot_prune"


def test_pruning_failure_is_warning_and_does_not_change_successful_update(
    tmp_path: Path,
) -> None:
    class PruningFailureHost(PreUpdateHost):
        def execute(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            if args[0] == "snapshot_delete":
                raise HostControlError("pruning delete failed", status="failed")
            return super().execute(*args, **kwargs)

    executor = SnapshotUpdateExecutor()
    host = PruningFailureHost()
    host.snapshots = [
        _owned(106, "20260728T120000Z", created_at="2026-07-28T12:00:00+00:00"),
        _owned(106, "20260727T120000Z", created_at="2026-07-27T12:00:00+00:00"),
        _owned(106, "20260726T120000Z", created_at="2026-07-26T12:00:00+00:00"),
    ]
    service, db, job = _approved_update(tmp_path, executor, host)
    _record_snapshot_sources(db, host.snapshots)
    clock = FakeClock()
    service.stabilizer = Stabilizer(
        executor,
        threading.Event(),
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    service.settings.raw["containers"][106]["snapshot_retention_count"] = 1
    service.settings.raw["containers"][106].pop("snapshot_retention", None)

    service._run_job(db.get_job(job["id"]))

    assert db.get_job(job["id"])["status"] == "success"
    prune = next(
        item
        for item in db.list_jobs()
        if item["operation_type"] == "snapshot_prune"
    )
    assert prune["status"] == "failed"
    assert any(
        event["event_type"] == "job_failed"
        for event in db.list_job_events(prune["id"])
    )
    assert any(
        event["event_type"] == "snapshot_pruning_failed"
        and event["level"] == "warning"
        for event in db.list_job_events(prune["id"])
    )
