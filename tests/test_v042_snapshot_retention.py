from __future__ import annotations

import importlib
from pathlib import Path
import threading
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import validate_config
from app.database import Database
from app.executor import ExecutorError
from app.service import OpsService
from app.stabilization import Stabilizer
from tests.test_lifecycle_snapshots import (
    CompatibleExecutor,
    FakeHostControl,
    run_queued,
    settings,
)
from tests.test_v042_snapshot_consistency import (
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
    return {
        "name": f"hubinet-ops-{vmid}-{short}-{suffix}",
        "created_at": created_at,
        "kind": kind,
        "owned_by_hubinet_ops": True,
        "rollback_eligible": True,
        "delete_eligible": True,
    }


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
    assert managed["source_job_id"] == failed["id"]
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

    service._enforce_snapshot_retention(106, current)

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

    service._enforce_snapshot_retention(106, job)

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
    class PruningFailureExecutor(SnapshotUpdateExecutor):
        def run(self, action: str, vmid: int, argument=None, timeout=None, on_event=None):
            if action == "snapshot-delete":
                self.actions.append(action)
                raise ExecutorError("pruning delete failed")
            return super().run(action, vmid, argument, timeout, on_event)

    executor = PruningFailureExecutor()
    executor.snapshots = [
        _owned(106, "20260728T120000Z", created_at="2026-07-28T12:00:00+00:00"),
        _owned(106, "20260727T120000Z", created_at="2026-07-27T12:00:00+00:00"),
        _owned(106, "20260726T120000Z", created_at="2026-07-26T12:00:00+00:00"),
    ]
    service, db, job = _approved_update(tmp_path, executor)
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
    assert any(
        event["event_type"] == "snapshot_pruning_failed"
        and event["level"] == "warning"
        for event in db.list_job_events(job["id"])
    )
