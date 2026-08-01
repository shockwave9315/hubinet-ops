from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

import pytest

from app.database import Database
from app.executor import ExecutorError
from app.host_control import HostControlError
from app.service import HostJobOutcome, OpsService, classify_host_job_error
from tests.test_lifecycle_snapshots import (
    CompatibleExecutor,
    FakeHostControl,
    settings as lifecycle_settings,
)
from tests.test_service import docker_state, settings as update_settings
from tests.test_v042_snapshot_consistency import SnapshotUpdateExecutor


REMOTE_JOB_ID = "d" * 32


class FailingUpdateExecutor(SnapshotUpdateExecutor):
    def run(self, action: str, vmid: int, argument=None, timeout=None, on_event=None):
        if action == "update":
            self.actions.append(action)
            raise ExecutorError("update failed after package mutation")
        return super().run(action, vmid, argument, timeout, on_event)


class OutcomeHost(FakeHostControl):
    def __init__(self, mode: str) -> None:
        super().__init__("running")
        self.mode = mode
        self.rollback_posts = 0
        self.rollback_reads = 0

    def execute(self, operation_type: str, vmid: int, request_id: str, **kwargs: Any):
        if operation_type != "snapshot_rollback":
            return super().execute(operation_type, vmid, request_id, **kwargs)
        self.rollback_posts += 1
        observed = {
            "id": REMOTE_JOB_ID,
            "vmid": vmid,
            "request_id": request_id,
            "operation_type": operation_type,
        }
        callback = kwargs.get("on_observed")
        if callback is not None:
            callback(observed)
        if self.mode == "failed":
            raise HostControlError("physical identity mismatch", status="failed")
        if self.mode == "http400":
            raise HostControlError("invalid request", http_status=400)
        if self.mode == "http409":
            raise HostControlError("contract mismatch", http_status=409)
        if self.mode == "http500":
            raise HostControlError("gateway failed", status="unavailable", http_status=500)
        if self.mode == "interrupted":
            raise HostControlError("worker interrupted", status="interrupted")
        if self.mode in {"unavailable", "accepted_success_lost"}:
            self.existing_jobs[request_id] = {
                "id": REMOTE_JOB_ID,
                "status": "succeeded" if self.mode == "accepted_success_lost" else "running",
                "result": {"lxc_status": "running"},
            }
            raise HostControlError("connection reset", status="unavailable")
        return {"lxc_status": "running"}

    def wait_existing_job(self, operation_type: str, vmid: int, request_id: str, **kwargs: Any):
        self.rollback_reads += 1
        if self.mode == "not_found":
            raise HostControlError("missing", status="not_found")
        if self.mode == "unavailable":
            raise HostControlError("poll unavailable", status="unavailable")
        return super().wait_existing_job(operation_type, vmid, request_id, **kwargs)


class CountingStabilizer:
    def __init__(self) -> None:
        self.calls = 0

    def wait(self, **_kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        return {
            "health_status": "healthy",
            "docker": {"required_healthy": 3, "required_total": 3},
        }


def _case(tmp_path: Path, mode: str) -> tuple[OpsService, Database, OutcomeHost, dict[str, Any]]:
    cfg = update_settings(tmp_path)
    cfg.raw["containers"][106]["pre_update_snapshot"] = True
    cfg.raw["containers"][106]["automatic_rollback"] = True
    cfg.raw["containers"][106]["operator_capabilities"].update(
        {"snapshot_create": True, "snapshot_list": True, "snapshot_delete": True}
    )
    db = Database(cfg.db_path)
    host = OutcomeHost(mode)
    executor = FailingUpdateExecutor([docker_state(3), docker_state(3), docker_state(3)])
    service = OpsService(cfg, db, executor, host_control=host)
    plan = service.scan_container(106)["plan"]
    job = service.approve(plan["id"])["job"]
    return service, db, host, job


@pytest.mark.parametrize("mode", ["unavailable", "http500", "interrupted"])
def test_automatic_rollback_ambiguous_submit_stays_active_without_terminal_event(
    tmp_path: Path,
    mode: str,
) -> None:
    service, db, host, job = _case(tmp_path, mode)
    service._run_job(db.get_job(job["id"]))
    active = db.get_job(job["id"])
    contract = active["result"]["automatic_rollback"]
    assert active["status"] in {"queued", "running"}
    assert contract["phase"] == "outcome_unknown"
    assert contract["request_id"] == f"automatic-rollback-{job['id']}"
    assert host.rollback_posts == 1
    assert db.get_plan(str(job["plan_id"]))["status"] == "approved"
    assert not any(
        str(event.get("event_type") or "").startswith("job_")
        and event.get("event_type") != "job_started"
        for event in db.list_job_events(job["id"])
    )
    assert service.get_state(106)["operation_status"] == "reconciliation_required"


def test_automatic_rollback_reconciliation_is_read_only_and_warning_is_once(
    tmp_path: Path,
) -> None:
    service, db, host, job = _case(tmp_path, "unavailable")
    service._run_job(db.get_job(job["id"]))
    service._reconcile_startup_jobs()
    service._reconcile_startup_jobs()
    assert host.rollback_posts == 1
    assert host.rollback_reads == 2
    warnings = [
        event for event in db.list_job_events(job["id"], limit=200)
        if event["event_type"] == "automatic_rollback_outcome_unknown"
    ]
    assert len(warnings) == 1
    assert db.get_job(job["id"])["status"] in {"queued", "running"}


def test_automatic_rollback_success_lost_is_completed_after_read_only_restart(
    tmp_path: Path,
) -> None:
    service, db, host, job = _case(tmp_path, "accepted_success_lost")
    stabilizer = CountingStabilizer()
    service.stabilizer = stabilizer  # type: ignore[assignment]
    service._run_job(db.get_job(job["id"]))
    assert db.get_job(job["id"])["result"]["automatic_rollback"]["phase"] == "outcome_unknown"
    restarted = OpsService(
        service.settings,
        db,
        service.executor,
        host_control=host,
        stabilizer=stabilizer,  # type: ignore[arg-type]
    )
    restarted._reconcile_startup_jobs()
    restarted._reconcile_startup_jobs()
    terminal = db.get_job(job["id"])
    assert terminal["status"] == "rolled_back"
    assert terminal["result"]["automatic_rollback"]["phase"] == "completed"
    assert host.rollback_posts == 1
    assert stabilizer.calls == 1
    terminal_events = [
        event for event in db.list_job_events(job["id"])
        if event.get("event_type") == "job_rolled_back"
    ]
    assert len(terminal_events) == 1


@pytest.mark.parametrize("mode", ["failed", "http400", "http409"])
def test_automatic_rollback_definitive_rejection_terminalizes(
    tmp_path: Path,
    mode: str,
) -> None:
    service, db, host, job = _case(tmp_path, mode)
    service._run_job(db.get_job(job["id"]))
    terminal = db.get_job(job["id"])
    assert terminal["status"] == "failed"
    assert terminal["result"]["automatic_rollback"]["phase"] == "definitive_failed"
    assert db.get_plan(str(job["plan_id"]))["status"] == "failed"
    assert host.rollback_posts == 1


def test_automatic_rollback_contract_and_proof_survive_generic_result_updates(
    tmp_path: Path,
) -> None:
    service, db, _host, job = _case(tmp_path, "unavailable")
    service._run_job(db.get_job(job["id"]))
    before = db.get_job(job["id"])["result"]
    db.update_job(
        job["id"],
        result={
            "automatic_rollback": {"version": 999, "phase": "completed"},
            "snapshot_proof": {"version": 999},
            "executor": "spoofed",
        },
    )
    after = db.get_job(job["id"])["result"]
    assert after["automatic_rollback"] == before["automatic_rollback"]
    assert after["snapshot_proof"] == before["snapshot_proof"]
    assert after["executor"] == "spoofed"


def test_outcome_unknown_global_lock_blocks_mutations_but_allows_listing(
    tmp_path: Path,
) -> None:
    service, db, host, job = _case(tmp_path, "unavailable")
    service._run_job(db.get_job(job["id"]))
    listing = service.list_snapshots(106)
    assert isinstance(listing["snapshots"], list)
    with pytest.raises(ValueError, match="Another destructive maintenance job is active"):
        service.queue_snapshot_create(106, "blocked-create-0001")
    with pytest.raises(ValueError, match="Another destructive maintenance job is active"):
        service.queue_snapshot_prune(106, "oldest", "blocked-prune-0001")
    assert host.rollback_posts == 1


def test_sibling_lifecycle_submit_loss_stays_active_and_never_posts_twice(
    tmp_path: Path,
) -> None:
    class UnavailableLifecycleHost(FakeHostControl):
        def __init__(self) -> None:
            super().__init__("running")
            self.posts = 0

        def execute(self, operation_type: str, vmid: int, request_id: str, **kwargs: Any):
            self.posts += 1
            raise HostControlError("connection reset", status="unavailable")

    cfg = lifecycle_settings(tmp_path)
    db = Database(cfg.db_path)
    host = UnavailableLifecycleHost()
    service = OpsService(cfg, db, CompatibleExecutor(), host_control=host)
    queued = service.queue_lifecycle(106, "reboot", "lifecycle-unknown-0001")
    running = db.next_queued_job()
    assert running is not None
    service._run_job(running)
    service._reconcile_startup_jobs()
    service._reconcile_startup_jobs()
    active = db.get_job(queued["id"])
    assert active["status"] == "running"
    assert active["stage"] in {"host_outcome_unknown", "host_reconciliation"}
    assert host.posts == 1
    assert service.get_state(106)["operation_status"] == "reconciliation_required"


def test_not_found_after_automatic_submit_is_read_only_and_never_resubmits(
    tmp_path: Path,
) -> None:
    service, db, host, job = _case(tmp_path, "unavailable")
    service._run_job(db.get_job(job["id"]))
    host.mode = "not_found"
    service._reconcile_startup_jobs()
    assert host.rollback_posts == 1
    assert host.rollback_reads == 1
    assert db.get_job(job["id"])["result"]["automatic_rollback"]["phase"] == "outcome_unknown"


def test_automatic_rollback_contract_preserves_exact_identity_and_host_job_id(
    tmp_path: Path,
) -> None:
    service, db, _host, job = _case(tmp_path, "unavailable")
    service._run_job(db.get_job(job["id"]))
    result = db.get_job(job["id"])["result"]
    contract = result["automatic_rollback"]
    proof = result["snapshot_proof"]
    assert contract["host_job_id"] == REMOTE_JOB_ID
    assert contract["expected_snapshot_identity"] == {
        "version": 1,
        "vmid": proof["vmid"],
        "snapshot_name": proof["snapshot_name"],
        "kind": proof["kind"],
        "host_source_job_id": proof["host_source_job_id"],
        "pve_snaptime": proof["pve_snaptime"],
    }


def test_restart_after_remote_succeeded_before_stabilization_runs_it_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, db, host, job = _case(tmp_path, "accepted_success_lost")
    service._run_job(db.get_job(job["id"]))
    original_finish = service._finish_automatic_rollback

    def crash_before_stabilization(*_args: Any, **_kwargs: Any) -> None:
        raise SystemExit("crash before stabilization")

    monkeypatch.setattr(service, "_finish_automatic_rollback", crash_before_stabilization)
    with pytest.raises(SystemExit, match="before stabilization"):
        service._reconcile_startup_jobs()
    assert db.get_job(job["id"])["result"]["automatic_rollback"]["phase"] == "remote_succeeded"
    monkeypatch.setattr(service, "_finish_automatic_rollback", original_finish)
    stabilizer = CountingStabilizer()
    service.stabilizer = stabilizer  # type: ignore[assignment]
    service._reconcile_startup_jobs()
    service._reconcile_startup_jobs()
    assert stabilizer.calls == 1
    assert db.get_job(job["id"])["status"] == "rolled_back"


def test_restart_during_stabilization_does_not_run_stabilization_twice(
    tmp_path: Path,
) -> None:
    service, db, host, job = _case(tmp_path, "accepted_success_lost")
    service._run_job(db.get_job(job["id"]))

    class CrashingStabilizer:
        def __init__(self) -> None:
            self.calls = 0

        def wait(self, **_kwargs: Any) -> dict[str, Any]:
            self.calls += 1
            raise SystemExit("crash during stabilization")

    crashing = CrashingStabilizer()
    service.stabilizer = crashing  # type: ignore[assignment]
    with pytest.raises(SystemExit, match="during stabilization"):
        service._reconcile_startup_jobs()
    assert db.get_job(job["id"])["result"]["automatic_rollback"]["phase"] == "stabilizing"
    replacement = CountingStabilizer()
    restarted = OpsService(
        service.settings, db, service.executor, host_control=host,
        stabilizer=replacement,  # type: ignore[arg-type]
    )
    restarted._reconcile_startup_jobs()
    assert crashing.calls == 1
    assert replacement.calls == 0
    assert db.get_job(job["id"])["status"] in {"queued", "running"}


@pytest.mark.parametrize(
    "malformed",
    [None, {"version": 99}, {"version": 1, "phase": "submitting"}],
    ids=["missing", "unsupported-version", "missing-identity"],
)
def test_missing_or_malformed_automatic_contract_remains_active_fail_closed(
    tmp_path: Path,
    malformed: dict[str, Any] | None,
) -> None:
    service, db, _host, job = _case(tmp_path, "unavailable")
    service._run_job(db.get_job(job["id"]))
    current = db.get_job(job["id"])
    result = dict(current["result"])
    if malformed is None:
        result.pop("automatic_rollback", None)
    else:
        result["automatic_rollback"] = malformed
    with db._lock, db._connect() as conn:  # test-only corruption of durable state
        conn.execute(
            "UPDATE jobs SET result=?,stage='rollback' WHERE id=?",
            (json.dumps(result), str(job["id"])),
        )
    service._reconcile_startup_jobs()
    active = db.get_job(job["id"])
    assert active["status"] in {"queued", "running"}
    assert db.get_plan(str(job["plan_id"]))["status"] == "approved"
    assert active["result"]["snapshot_proof"]["version"] == 3
    assert active["result"]["automatic_rollback_reconciliation_error"]
    assert service.get_state(106)["operation_status"] == "reconciliation_required"


@dataclass(frozen=True)
class FailureScenario:
    scenario: str
    operation: str
    phase: str
    fault: str
    expected: HostJobOutcome
    expected_state: str
    remote_actions: int
    test_name: str
    error: HostControlError
    submit_started: bool


def _matrix() -> list[FailureScenario]:
    operations = [
        "automatic_rollback", "manual_rollback", "snapshot_delete",
        "snapshot_prune", "retention_delete", "snapshot_create",
        "snapshot_create_ram", "pre_update_create", "lifecycle_start",
        "lifecycle_shutdown", "lifecycle_reboot", "lifecycle_force_stop",
        "self_update", "offline_restore", "offline_force_stop",
    ]
    phases = [
        "before_local_job", "after_local_job", "prepared", "before_network_submit",
        "submitting", "post_accepted_response_lost", "remote_queued", "remote_running",
        "physical_mutation_running", "remote_succeeded_result_lost", "remote_failed",
        "remote_interrupted", "final_physical_refresh", "stabilization",
        "local_terminalization",
    ]
    faults = [
        ("backend_crash", HostControlError("backend crash", status="unavailable"), False),
        ("hostd_restart", HostControlError("hostd restart", status="unavailable"), False),
        ("backend_restart", HostControlError("backend restart", status="unavailable"), False),
        ("timeout", HostControlError("timeout", status="unavailable"), False),
        ("connection_reset", HostControlError("reset", status="unavailable"), False),
        ("http_400", HostControlError("bad request", http_status=400), True),
        ("http_409", HostControlError("collision", http_status=409), True),
        ("http_500", HostControlError("server error", status="unavailable", http_status=500), False),
        ("unavailable", HostControlError("unavailable", status="unavailable"), False),
        ("not_found", HostControlError("missing", status="not_found"), False),
        ("interrupted", HostControlError("interrupted", status="interrupted"), False),
        ("terminal_failed", HostControlError("failed", status="failed"), True),
        ("contract_mismatch", HostControlError("mismatch", status="contract_mismatch"), True),
        ("request_id_collision", HostControlError("collision", http_status=409), True),
        ("physical_identity_mismatch", HostControlError("identity", status="failed"), True),
        ("snapshot_absent", HostControlError("absent", status="failed"), True),
        ("snapshot_replaced", HostControlError("replaced", status="failed"), True),
        ("duplicate_listing", HostControlError("duplicate", status="failed"), True),
        ("repeated_reconciliation", HostControlError("poll unavailable", status="unavailable"), False),
        ("parallel_destructive_attempt", HostControlError("active job", http_status=409), True),
    ]
    cases: list[FailureScenario] = []
    for index in range(60):
        operation = operations[index % len(operations)]
        phase = phases[index % len(phases)]
        fault, error, intrinsically_definitive = faults[index % len(faults)]
        submit_started = phases.index(phase) >= phases.index("submitting")
        expected = (
            HostJobOutcome.DEFINITIVE_FAILURE
            if intrinsically_definitive or not submit_started
            else HostJobOutcome.OUTCOME_UNKNOWN
        )
        expected_state = (
            "no_local_job"
            if phase == "before_local_job"
            else "prepared"
            if not submit_started
            else "terminal_failed"
            if expected is HostJobOutcome.DEFINITIVE_FAILURE
            else "active/reconciliation_required"
        )
        cases.append(
            FailureScenario(
                f"M{index + 1:02d}-{operation}-{phase}-{fault}",
                operation,
                phase,
                fault,
                expected,
                expected_state,
                1 if submit_started else 0,
                "test_failure_matrix_classifies_50_named_host_scenarios",
                error,
                submit_started,
            )
        )
    return cases


FAILURE_MATRIX = _matrix()


@pytest.mark.parametrize("case", FAILURE_MATRIX, ids=lambda case: case.scenario)
def test_failure_matrix_classifies_50_named_host_scenarios(case: FailureScenario) -> None:
    assert len(FAILURE_MATRIX) >= 50
    assert classify_host_job_error(
        case.error, submit_started=case.submit_started
    ) is case.expected
    assert case.expected_state in {
        "no_local_job", "prepared", "active/reconciliation_required", "terminal_failed"
    }
    assert case.remote_actions in {0, 1}
