from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


ROOT = Path(__file__).parents[1]
PVE = ROOT / "deploy" / "pve"
sys.path.insert(0, str(PVE))

from hubinet_ops_host_control import (  # noqa: E402
    HostControlError,
    HostController,
    HostPaths,
    HostPolicy,
    owned_snapshot,
    run_forced_command,
)
from hubinet_ops_hostd import (  # noqa: E402
    HostdApplication,
    HostdConfig,
    HostJobRunner,
    HostJobStore,
)
from hubinet_ops_release import (  # noqa: E402
    ReleaseError,
    inspect_staged_release,
    prepare_supervisor,
    read_marker,
    remove_marker,
    run_supervisor,
    verify_supervisor_launch,
    write_marker,
)


def _write(path: Path, value: str) -> Path:
    path.write_text(value, encoding="utf-8")
    return path


def policy(
    tmp_path: Path,
    *,
    snapshot_restore_vmids: str = "101\n102\n103\n104\n105\n106\n107\n108\n109\n110\n",
) -> HostPolicy:
    node = tmp_path / "nodes" / "pve-a"
    node.mkdir(parents=True)
    local = tmp_path / "local"
    try:
        local.symlink_to(node, target_is_directory=True)
    except OSError:
        local = node
    release = tmp_path / "approved-release"
    (release / "deploy").mkdir(parents=True)
    _write(
        release / "deploy" / "upgrade-0.4.0-from-pve.sh",
        "#!/usr/bin/env bash\nexit 0\n",
    )
    _write(tmp_path / "self-update", "#!/usr/bin/env bash\nexit 0\n")
    return HostPolicy(
        HostPaths(
            observation=_write(tmp_path / "observation", "100\n101\n106\n110\n"),
            managed=_write(tmp_path / "managed", "101\n106\n"),
            maintenance=_write(tmp_path / "maintenance", "101\n106\n"),
            lifecycle=_write(tmp_path / "lifecycle", "101\n106\n110\n"),
            host_control=_write(tmp_path / "host-control", "101\n106\n110\n"),
            snapshot_create=_write(
                tmp_path / "snapshot-create",
                "101\n106\n110\n",
            ),
            snapshot_restore=_write(
                tmp_path / "snapshot-restore",
                snapshot_restore_vmids,
            ),
            snapshot_delete=_write(
                tmp_path / "snapshot-delete",
                "101\n106\n110\n",
            ),
            resource_types=_write(
                tmp_path / "types",
                "100 qemu\n101 lxc\n106 lxc\n110 lxc\n",
            ),
            pve_local=local,
            pve_nodes=node.parent,
            self_update=tmp_path / "self-update",
            self_update_release=release,
        )
    )


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.status = {101: "stopped", 106: "running", 110: "running"}
        self.snapshots: list[dict[str, Any]] = [
            {
                "name": "foreign-backup",
                "description": "not managed by Hubinet Ops",
                "snaptime": 1_700_000_000,
            },
            {
                "name": "hubinet-ops-106-manual-20260720T170000Z",
                "description": "hubinet-ops;kind=manual;source_job_id=abc12345",
                "snaptime": 1_721_492_400,
            },
        ]

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(argv), dict(kwargs)))
        if argv[:2] == ["pct", "status"]:
            return subprocess.CompletedProcess(argv, 0, f"status: {self.status[int(argv[2])]}\n", "")
        if argv[:2] == ["pct", "start"]:
            self.status[int(argv[2])] = "running"
        elif argv[:2] in (["pct", "shutdown"], ["pct", "stop"]):
            self.status[int(argv[2])] = "stopped"
        elif argv[:2] == ["pct", "listsnapshot"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps(self.snapshots), "")
        return subprocess.CompletedProcess(argv, 0, "", "")


def test_policy_has_independent_observation_managed_lifecycle_and_snapshot_guards(
    tmp_path: Path,
) -> None:
    current = policy(tmp_path)
    assert current.validate("inspect", 100)[3] == "qemu"
    assert current.validate("start", 110)[0] == "start"
    assert current.validate("update", 101)[0] == "update"
    with pytest.raises(HostControlError, match="managed-executor"):
        current.validate("update", 110)
    with pytest.raises(HostControlError, match="lifecycle"):
        current.validate("start", 100)
    with pytest.raises(HostControlError, match="owned"):
        current.validate("snapshot-rollback", 106, "foreign-backup")
    with pytest.raises(HostControlError, match="Action not allowed"):
        current.validate("exec", 106, "id")


def test_controller_uses_fixed_argv_without_shell_and_enforces_runtime(tmp_path: Path) -> None:
    runner = FakeRunner()
    controller = HostController(policy(tmp_path), runner=runner)

    result = controller.execute("start", 101)

    assert result["lxc_status"] == "running"
    assert ["pct", "start", "101"] in [call[0] for call in runner.calls]
    assert all(call[1]["shell"] is False for call in runner.calls)
    with pytest.raises(HostControlError, match="requires stopped"):
        controller.execute("start", 101)


def test_controller_inspects_and_launches_only_the_approved_release_fingerprint(
    tmp_path: Path,
) -> None:
    runner = FakeRunner()
    controller = HostController(policy(tmp_path), runner=runner)
    release = controller.execute("self-update-release", 110)

    result = controller.execute(
        "self-update",
        110,
        release["fingerprint"],
        source_job_id="abcd1234",
    )

    assert release["version"] == "0.4.0"
    assert release["release_id"].startswith("hubinet-ops-0.4.0-")
    assert result["supervisor_started"] is True
    assert runner.calls[-1][0] == [
        str(tmp_path / "self-update"),
        "--job-id",
        "abcd1234",
        "--fingerprint",
        release["fingerprint"],
    ]
    _write(
        tmp_path / "approved-release" / "CHANGELOG.md",
        "changed staged content\n",
    )
    changed = controller.execute("self-update-release", 110)
    assert changed["fingerprint"] != release["fingerprint"]
    assert changed["release_id"] != release["release_id"]
    with pytest.raises(HostControlError, match="fingerprint"):
        controller.execute("self-update", 110, "bad", source_job_id="abcd1234")


def test_snapshot_list_marks_only_project_snapshots_as_eligible(tmp_path: Path) -> None:
    runner = FakeRunner()
    controller = HostController(policy(tmp_path), runner=runner)

    snapshots = controller.execute("list-snapshots", 106)["snapshots"]

    owned = next(item for item in snapshots if item["owned_by_hubinet_ops"])
    foreign = next(item for item in snapshots if not item["owned_by_hubinet_ops"])
    assert owned["kind"] == "manual"
    assert owned["rollback_eligible"] is True
    assert foreign["rollback_eligible"] is False
    assert foreign["delete_eligible"] is False
    assert owned_snapshot(owned["name"], 106)
    with pytest.raises(HostControlError, match="owned"):
        controller.execute("snapshot-delete", 106, "foreign-backup")


def test_pve_snapshot_restore_policy_rejects_owned_snapshot_for_disallowed_vmid(
    tmp_path: Path,
) -> None:
    runner = FakeRunner()
    controller = HostController(
        policy(tmp_path, snapshot_restore_vmids="110\n"),
        runner=runner,
    )
    name = "hubinet-ops-106-manual-20260720T170000Z"

    with pytest.raises(HostControlError, match="snapshot restore allowed by PVE policy"):
        controller.execute("snapshot-rollback", 106, name)

    assert not any(call[0][:2] == ["pct", "rollback"] for call in runner.calls)


def test_pve_snapshot_restore_always_rejects_foreign_snapshot(tmp_path: Path) -> None:
    controller = HostController(policy(tmp_path), runner=FakeRunner())

    with pytest.raises(HostControlError, match="not owned"):
        controller.execute("snapshot-rollback", 110, "foreign-backup")


def test_ct110_snapshot_restore_works_without_backend_through_explicit_pve_policy(
    tmp_path: Path,
) -> None:
    runner = FakeRunner()
    name = "hubinet-ops-110-manual-20260723T190000Z"
    runner.snapshots = [
        {
            "name": name,
            "description": "hubinet-ops;kind=manual;source_job_id=abc12345",
            "snaptime": 1_721_492_400,
        }
    ]
    controller = HostController(policy(tmp_path), runner=runner)

    result = controller.execute("snapshot-rollback", 110, name)

    assert result["action"] == "rollback"
    assert ["pct", "rollback", "110", name] in [call[0] for call in runner.calls]


def test_active_host_job_blocks_explicit_snapshot_restore(tmp_path: Path) -> None:
    store = HostJobStore(tmp_path / "jobs.db")
    controller = HostController(policy(tmp_path), runner=FakeRunner())
    application = HostdApplication(
        HostdConfig(
            bind="127.0.0.1",
            port=8741,
            database=tmp_path / "jobs.db",
            client_allowlist=frozenset(),
        ),
        store,
        HostJobRunner(store, controller),
        "g" * 64,
        "u" * 64,
    )
    store.create(
        vmid=101,
        operation_type="lifecycle_start",
        request_id="request-active-host-operation",
    )

    with pytest.raises(HostControlError, match="active"):
        application.submit(
            vmid=110,
            operation_type="snapshot_rollback",
            request_id="request-ct110-explicit-restore",
            argument="hubinet-ops-110-manual-20260723T190000Z",
        )


def test_snapshot_create_uses_validated_name_and_auditable_description(tmp_path: Path) -> None:
    runner = FakeRunner()
    controller = HostController(policy(tmp_path), runner=runner)
    name = "hubinet-ops-106-manual-20260720T191500Z"

    result = controller.execute("snapshot-create", 106, name, source_job_id="abcd1234")

    argv = next(call[0] for call in runner.calls if call[0][:2] == ["pct", "snapshot"])
    assert argv[:4] == ["pct", "snapshot", "106", name]
    assert "source_job_id=abcd1234" in argv[-1]
    assert result["owned_by_hubinet_ops"] is True


def test_forced_command_rejects_command_text_and_unknown_arguments(tmp_path: Path) -> None:
    controller = HostController(policy(tmp_path), runner=FakeRunner())
    with pytest.raises(HostControlError):
        run_forced_command("inspect 106 extra text", controller)
    with pytest.raises(HostControlError):
        run_forced_command("inspect;id 106", controller)


class DummyPolicy:
    def validate(self, action: str, vmid: int, argument: str | None = None) -> tuple[str, int, str | None, str]:
        return action, vmid, argument, "lxc"


class DummyController:
    policy = DummyPolicy()

    def __init__(self) -> None:
        self.calls: list[tuple[str, int, str | None, str | None]] = []
        self.status = "stopped"
        self.status_payload: dict[str, Any] | None = None
        self.status_error: Exception | None = None

    def execute(
        self,
        action: str,
        vmid: int,
        argument: str | None = None,
        *,
        source_job_id: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append((action, vmid, argument, source_job_id))
        if action == "status":
            if self.status_error is not None:
                raise self.status_error
            if self.status_payload is not None:
                return dict(self.status_payload)
            return {
                "resource_type": "lxc",
                "runtime_status": self.status,
                "lxc_status": self.status,
            }
        return {"action": action, "vmid": vmid}


class BlockingController(DummyController):
    def __init__(self) -> None:
        super().__init__()
        self.operation_started = threading.Event()
        self.allow_completion = threading.Event()

    def execute(
        self,
        action: str,
        vmid: int,
        argument: str | None = None,
        *,
        source_job_id: str | None = None,
    ) -> dict[str, Any]:
        if action == "status":
            return super().execute(
                action,
                vmid,
                argument,
                source_job_id=source_job_id,
            )
        self.calls.append((action, vmid, argument, source_job_id))
        self.operation_started.set()
        if not self.allow_completion.wait(timeout=5):
            raise RuntimeError("test did not release the blocked host operation")
        return {"action": action, "vmid": vmid}


class DelayedSelfUpdateController(DummyController):
    def __init__(self, results: Path) -> None:
        super().__init__()
        self.results = results
        self.prepare_started = threading.Event()
        self.allow_running_marker = threading.Event()
        self.running_marker_written = threading.Event()
        self.allow_terminal_marker = threading.Event()
        self.terminal_marker_written = threading.Event()

    def execute(
        self,
        action: str,
        vmid: int,
        argument: str | None = None,
        *,
        source_job_id: str | None = None,
    ) -> dict[str, Any]:
        if action != "self-update":
            return super().execute(
                action,
                vmid,
                argument,
                source_job_id=source_job_id,
            )
        assert source_job_id is not None
        assert argument is not None
        self.calls.append((action, vmid, argument, source_job_id))
        self.prepare_started.set()
        assert self.allow_running_marker.wait(timeout=5)
        write_marker(
            self.results,
            source_job_id,
            {
                "job_id": source_job_id,
                "status": "running",
                "fingerprint": argument,
                "deadline_at": "2999-01-01T00:00:00+00:00",
            },
        )
        self.running_marker_written.set()
        assert self.allow_terminal_marker.wait(timeout=5)
        write_marker(
            self.results,
            source_job_id,
            {
                "job_id": source_job_id,
                "status": "succeeded",
                "fingerprint": argument,
                "version": "0.4.0",
                "release_id": "hubinet-ops-0.4.0-aaaaaaaaaaaaaaaa",
                "exit_code": 0,
                "error": None,
            },
        )
        self.terminal_marker_written.set()
        return {"supervisor_started": True}


def test_host_jobs_are_durable_idempotent_and_single_writer(tmp_path: Path) -> None:
    store = HostJobStore(tmp_path / "jobs.db")
    job, created = store.create(
        vmid=110,
        operation_type="lifecycle_start",
        request_id="request-0001",
    )
    same, created_again = store.create(
        vmid=110,
        operation_type="lifecycle_start",
        request_id="request-0001",
    )
    assert created is True
    assert created_again is False
    assert same["id"] == job["id"]
    with pytest.raises(HostControlError, match="active"):
        store.create(
            vmid=106,
            operation_type="snapshot_create",
            request_id="request-0002",
            argument="hubinet-ops-106-manual-20260720T191500Z",
        )
    assert HostJobStore(tmp_path / "jobs.db").get(job["id"])["status"] == "queued"


def test_general_hostd_bearer_cannot_authorize_self_update(tmp_path: Path) -> None:
    store = HostJobStore(tmp_path / "jobs.db")
    runner = HostJobRunner(store, DummyController())  # type: ignore[arg-type]
    application = HostdApplication(
        HostdConfig(
            bind="127.0.0.1",
            port=8741,
            database=tmp_path / "jobs.db",
            client_allowlist=frozenset(),
        ),
        store,
        runner,
        "g" * 64,
        "u" * 64,
    )

    assert application.authorize(
        {"Authorization": f"Bearer {'g' * 64}"},
        "127.0.0.1",
    )
    assert not application.authorize(
        {"Authorization": f"Bearer {'g' * 64}"},
        "127.0.0.1",
        self_update=True,
    )
    assert application.authorize(
        {"Authorization": f"Bearer {'u' * 64}"},
        "127.0.0.1",
        self_update=True,
    )


def test_host_job_runner_persists_terminal_result(tmp_path: Path) -> None:
    store = HostJobStore(tmp_path / "jobs.db")
    controller = DummyController()
    runner = HostJobRunner(store, controller)  # type: ignore[arg-type]
    job, _ = store.create(
        vmid=110,
        operation_type="lifecycle_start",
        request_id="request-0003",
    )

    terminal = runner.run(job["id"])

    assert terminal["status"] == "succeeded"
    assert terminal["progress"] == 100
    assert terminal["result"] == {"action": "start", "vmid": 110}
    assert controller.calls == [("start", 110, None, job["id"])]


@pytest.mark.parametrize(
    ("operation_type", "argument"),
    [
        ("snapshot_create", "hubinet-ops-110-manual-20260723T220000Z"),
        ("snapshot_rollback", "hubinet-ops-110-manual-20260723T220000Z"),
        ("snapshot_delete", "hubinet-ops-110-manual-20260723T220000Z"),
        ("lifecycle_shutdown", None),
        ("lifecycle_start", None),
        ("lifecycle_force_stop", None),
        ("lifecycle_reboot", None),
    ],
)
def test_live_job_polling_is_read_only_until_worker_finishes(
    tmp_path: Path,
    operation_type: str,
    argument: str | None,
) -> None:
    store = HostJobStore(tmp_path / "jobs.db")
    controller = BlockingController()
    runner = HostJobRunner(store, controller)  # type: ignore[arg-type]
    application = HostdApplication(
        HostdConfig(
            bind="127.0.0.1",
            port=8741,
            database=tmp_path / "jobs.db",
            client_allowlist=frozenset(),
        ),
        store,
        runner,
        "g" * 64,
        "u" * 64,
    )
    job, _ = store.create(
        vmid=110,
        operation_type=operation_type,
        request_id=f"request-live-{operation_type}",
        argument=argument,
    )
    worker = threading.Thread(target=runner.run, args=(job["id"],))
    worker.start()
    assert controller.operation_started.wait(timeout=5)

    for _ in range(3):
        polled = application.get_job(job["id"])
        assert polled["status"] == "running"
        assert polled["stage"] == "executing"
    assert all(call[0] != "status" for call in controller.calls)

    controller.allow_completion.set()
    worker.join(timeout=5)
    assert not worker.is_alive()
    terminal = application.get_job(job["id"])
    assert terminal["status"] == "succeeded"
    assert terminal["result"]["action"] == {
        "snapshot_create": "snapshot-create",
        "snapshot_rollback": "snapshot-rollback",
        "snapshot_delete": "snapshot-delete",
        "lifecycle_shutdown": "shutdown",
        "lifecycle_start": "start",
        "lifecycle_force_stop": "force-stop",
        "lifecycle_reboot": "reboot",
    }[operation_type]
    assert all(call[0] != "status" for call in controller.calls)


def test_get_job_leaves_queued_job_unchanged_before_worker_starts(
    tmp_path: Path,
) -> None:
    store = HostJobStore(tmp_path / "jobs.db")
    controller = DummyController()
    application = HostdApplication(
        HostdConfig(
            bind="127.0.0.1",
            port=8741,
            database=tmp_path / "jobs.db",
            client_allowlist=frozenset(),
        ),
        store,
        HostJobRunner(store, controller),  # type: ignore[arg-type]
        "g" * 64,
        "u" * 64,
    )
    job, _ = store.create(
        vmid=110,
        operation_type="snapshot_create",
        request_id="request-queued-before-worker",
        argument="hubinet-ops-110-manual-20260723T220100Z",
    )

    assert application.get_job(job["id"])["status"] == "queued"
    assert store.get(job["id"])["status"] == "queued"
    assert controller.calls == []


def test_worker_does_not_overwrite_existing_terminal_result(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = HostJobStore(tmp_path / "jobs.db")
    controller = BlockingController()
    runner = HostJobRunner(store, controller)  # type: ignore[arg-type]
    job, _ = store.create(
        vmid=110,
        operation_type="snapshot_delete",
        request_id="request-terminal-cas-preserved",
        argument="hubinet-ops-110-manual-20260723T220200Z",
    )
    worker = threading.Thread(target=runner.run, args=(job["id"],))
    worker.start()
    assert controller.operation_started.wait(timeout=5)
    store.transition_from_active(
        job["id"],
        status="interrupted",
        stage="interrupted",
        progress=100,
        error="startup ownership was lost; outcome is unknown",
    )

    controller.allow_completion.set()
    worker.join(timeout=5)
    assert not worker.is_alive()
    terminal = store.get(job["id"])
    assert terminal["status"] == "interrupted"
    assert terminal["error"] == "startup ownership was lost; outcome is unknown"
    assert terminal["result"] is None
    assert "transition skipped because job is no longer active" in caplog.text


@pytest.mark.parametrize(
    ("operation_type", "runtime_status"),
    [
        ("lifecycle_start", "running"),
        ("lifecycle_shutdown", "stopped"),
        ("lifecycle_force_stop", "stopped"),
    ],
)
def test_startup_reconciliation_after_store_recreation_uses_runtime_without_replay(
    tmp_path: Path,
    operation_type: str,
    runtime_status: str,
) -> None:
    store = HostJobStore(tmp_path / "jobs.db")
    controller = DummyController()
    controller.status = runtime_status
    job, _ = store.create(
        vmid=110,
        operation_type=operation_type,
        request_id=f"request-{operation_type}-0004",
    )

    restarted = HostJobStore(tmp_path / "jobs.db")
    reconciled = restarted.reconcile_startup(controller)  # type: ignore[arg-type]
    application = HostdApplication(
        HostdConfig(
            bind="127.0.0.1",
            port=8741,
            database=tmp_path / "jobs.db",
            client_allowlist=frozenset(),
        ),
        restarted,
        HostJobRunner(restarted, controller),  # type: ignore[arg-type]
        "g" * 64,
        "u" * 64,
    )

    assert reconciled[0]["status"] == "succeeded"
    assert reconciled[0]["result"] == {
        "reconciled": True,
        "runtime_status": runtime_status,
    }
    assert controller.calls == [("status", 110, None, None)]
    assert application.get_job(job["id"])["status"] == "succeeded"
    assert controller.calls == [("status", 110, None, None)]


@pytest.mark.parametrize(
    "operation_type",
    ["snapshot_create", "snapshot_rollback", "snapshot_delete"],
)
def test_snapshot_job_at_real_startup_is_interrupted_without_replay(
    tmp_path: Path,
    operation_type: str,
) -> None:
    initial = HostJobStore(tmp_path / "jobs.db")
    job, _ = initial.create(
        vmid=110,
        operation_type=operation_type,
        request_id=f"request-startup-{operation_type}",
        argument="hubinet-ops-110-manual-20260723T220300Z",
    )
    initial.begin_execution(job["id"])
    controller = DummyController()
    restarted = HostJobStore(tmp_path / "jobs.db")

    reconciled = restarted.reconcile_startup(controller)  # type: ignore[arg-type]

    assert reconciled[0]["status"] == "interrupted"
    assert "outcome is unknown" in reconciled[0]["error"]
    assert controller.calls == []


def test_reboot_reconciliation_is_unknown_even_when_lxc_is_running(
    tmp_path: Path,
) -> None:
    store = HostJobStore(tmp_path / "jobs.db")
    controller = DummyController()
    controller.status = "running"
    job, _ = store.create(
        vmid=110,
        operation_type="lifecycle_reboot",
        request_id="request-reboot-0004",
    )

    reconciled = store.reconcile_startup(controller)  # type: ignore[arg-type]

    assert reconciled[0]["status"] == "interrupted"
    assert "cannot prove" in reconciled[0]["error"]
    assert controller.calls == []
    assert store.get(job["id"])["status"] == "interrupted"


def test_status_reconciliation_falls_back_to_runtime_status(
    tmp_path: Path,
) -> None:
    store = HostJobStore(tmp_path / "jobs.db")
    controller = DummyController()
    controller.status_payload = {
        "resource_type": "lxc",
        "runtime_status": "running",
        "lxc_status": "",
    }
    store.create(
        vmid=110,
        operation_type="lifecycle_start",
        request_id="request-runtime-fallback-0004",
    )

    reconciled = store.reconcile_startup(controller)  # type: ignore[arg-type]

    assert reconciled[0]["status"] == "succeeded"
    assert reconciled[0]["result"]["runtime_status"] == "running"


@pytest.mark.parametrize(
    "failure",
    [
        {"resource_type": "lxc", "runtime_status": "paused", "lxc_status": "paused"},
        RuntimeError("pct status unavailable"),
    ],
)
def test_status_reconciliation_reports_controlled_unknown_for_invalid_payload_or_error(
    tmp_path: Path,
    failure: dict[str, Any] | Exception,
) -> None:
    store = HostJobStore(tmp_path / "jobs.db")
    controller = DummyController()
    if isinstance(failure, Exception):
        controller.status_error = failure
    else:
        controller.status_payload = failure
    store.create(
        vmid=110,
        operation_type="lifecycle_start",
        request_id="request-invalid-status-0004",
    )

    reconciled = store.reconcile_startup(controller)  # type: ignore[arg-type]

    assert reconciled[0]["status"] == "interrupted"
    assert reconciled[0]["error"].startswith("status reconciliation failed:")


@pytest.mark.parametrize(
    ("marker_status", "exit_code", "expected_status"),
    [("succeeded", 0, "succeeded"), ("failed", 37, "failed")],
)
def test_self_update_result_survives_hostd_store_recreation(
    tmp_path: Path,
    marker_status: str,
    exit_code: int,
    expected_status: str,
) -> None:
    database = tmp_path / "jobs.db"
    results = tmp_path / "self-update-results"
    fingerprint = "a" * 64
    initial = HostJobStore(database, results)
    job, _ = initial.create(
        vmid=110,
        operation_type="self_update",
        request_id=f"request-self-update-{marker_status}",
        argument=fingerprint,
    )
    initial.begin_self_update_launch(job["id"])
    write_marker(
        results,
        job["id"],
        {
            "job_id": job["id"],
            "status": "running",
            "version": "0.4.0",
            "release_id": "hubinet-ops-0.4.0-aaaaaaaaaaaaaaaa",
            "fingerprint": fingerprint,
            "exit_code": None,
            "error": None,
            "deadline_at": "2999-01-01T00:00:00+00:00",
        },
    )

    restarted = HostJobStore(database, results)
    still_running = restarted.reconcile_startup(DummyController())  # type: ignore[arg-type]

    assert still_running[0]["status"] == "running"
    write_marker(
        results,
        job["id"],
        {
            "job_id": job["id"],
            "status": marker_status,
            "version": "0.4.0",
            "release_id": "hubinet-ops-0.4.0-aaaaaaaaaaaaaaaa",
            "fingerprint": fingerprint,
            "exit_code": exit_code,
            "error": None if exit_code == 0 else "installer validation failed",
        },
    )

    reconciled = restarted.reconcile_startup(DummyController())  # type: ignore[arg-type]

    terminal = restarted.get(job["id"])
    assert reconciled[0]["status"] == expected_status
    assert terminal["status"] == expected_status
    assert terminal["result"]["exit_code"] == exit_code
    if expected_status == "failed":
        assert "exit code 37" in terminal["error"]
        assert "installer validation failed" in terminal["error"]
    assert not (results / f"{job['id']}.json").exists()


def test_self_update_without_supervisor_marker_is_interrupted_unknown(
    tmp_path: Path,
) -> None:
    store = HostJobStore(tmp_path / "jobs.db", tmp_path / "results")
    job, _ = store.create(
        vmid=110,
        operation_type="self_update",
        request_id="request-self-update-missing",
        argument="a" * 64,
    )
    store.begin_self_update_launch(
        job["id"],
        now=datetime(2000, 1, 1, tzinfo=UTC),
    )
    remove_marker(tmp_path / "results", job["id"])

    terminal = HostJobStore(
        tmp_path / "jobs.db",
        tmp_path / "results",
    ).reconcile_startup(DummyController())[0]  # type: ignore[arg-type]

    assert terminal["status"] == "interrupted"
    assert "outcome is unknown" in terminal["error"]


def test_get_during_slow_self_update_prepare_waits_for_marker_and_then_succeeds(
    tmp_path: Path,
) -> None:
    database = tmp_path / "jobs.db"
    results = tmp_path / "results"
    store = HostJobStore(database, results)
    controller = DelayedSelfUpdateController(results)
    runner = HostJobRunner(store, controller)  # type: ignore[arg-type]
    application = HostdApplication(
        HostdConfig(
            bind="127.0.0.1",
            port=8741,
            database=database,
            client_allowlist=frozenset(),
        ),
        store,
        runner,
        "g" * 64,
        "u" * 64,
    )

    job, created = application.submit(
        vmid=110,
        operation_type="self_update",
        request_id="request-slow-self-update-prepare",
        argument="a" * 64,
    )
    assert created is True
    assert controller.prepare_started.wait(timeout=5)

    during_prepare = application.get_job(job["id"])

    assert during_prepare["status"] == "running"
    assert during_prepare["stage"] == "launching"
    assert during_prepare["launching_started_at"]
    assert during_prepare["launch_deadline_at"]
    same, created_again = application.submit(
        vmid=110,
        operation_type="self_update",
        request_id="request-slow-self-update-prepare",
        argument="a" * 64,
    )
    assert created_again is False
    assert same["id"] == job["id"]
    assert controller.calls == [("self-update", 110, "a" * 64, job["id"])]

    controller.allow_running_marker.set()
    assert controller.running_marker_written.wait(timeout=5)
    assert application.get_job(job["id"])["status"] == "running"

    controller.allow_terminal_marker.set()
    assert controller.terminal_marker_written.wait(timeout=5)
    terminal = application.get_job(job["id"])

    assert terminal["status"] == "succeeded"
    assert terminal["result"]["exit_code"] == 0
    assert terminal["result"]["supervisor_result_refreshed"] is True


def test_missing_launch_marker_before_deadline_survives_hostd_restart_without_replay(
    tmp_path: Path,
) -> None:
    database = tmp_path / "jobs.db"
    results = tmp_path / "results"
    initial = HostJobStore(database, results)
    job, _ = initial.create(
        vmid=110,
        operation_type="self_update",
        request_id="request-self-update-launch-restart",
        argument="a" * 64,
    )
    initial.begin_self_update_launch(job["id"])
    remove_marker(results, job["id"])

    controller = DummyController()
    restarted = HostJobStore(database, results)
    reconciled = restarted.reconcile_startup(controller)  # type: ignore[arg-type]

    assert reconciled[0]["status"] == "running"
    assert reconciled[0]["stage"] == "launching"
    assert controller.calls == []
    assert read_marker(results, job["id"]) is None


def test_terminal_self_update_job_cannot_invoke_systemd_run(
    tmp_path: Path,
) -> None:
    database = tmp_path / "jobs.db"
    results = tmp_path / "results"
    store = HostJobStore(database, results)
    job, _ = store.create(
        vmid=110,
        operation_type="self_update",
        request_id="request-terminal-before-systemd-run",
        argument="a" * 64,
    )
    store.begin_self_update_launch(job["id"])
    store.transition_from_active(
        job["id"],
        status="interrupted",
        stage="interrupted",
        progress=100,
        error="launch deadline expired; outcome is unknown",
    )
    write_marker(
        results,
        job["id"],
        {
            "job_id": job["id"],
            "status": "running",
            "fingerprint": "a" * 64,
            "deadline_at": "2999-01-01T00:00:00+00:00",
        },
    )

    with pytest.raises(ReleaseError, match="no longer active"):
        verify_supervisor_launch(
            result_dir=results,
            database=database,
            job_id=job["id"],
            expected_fingerprint="a" * 64,
        )

    wrapper = (PVE / "hubinet-ops-self-update").read_text(encoding="utf-8")
    assert wrapper.index("verify-active") < wrapper.index('set +e')
    assert wrapper.index("verify-active") < wrapper.index('"$SYSTEMD_RUN"')


def test_prepare_does_not_publish_running_marker_after_job_terminalization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "jobs.db"
    results = tmp_path / "results"
    release_root = tmp_path / "release"
    (release_root / "deploy").mkdir(parents=True)
    _write(
        release_root / "deploy" / "upgrade-0.4.0-from-pve.sh",
        "#!/usr/bin/env bash\nexit 0\n",
    )
    release = inspect_staged_release(release_root)
    store = HostJobStore(database, results)
    job, _ = store.create(
        vmid=110,
        operation_type="self_update",
        request_id="request-terminal-during-prepare",
        argument=release["fingerprint"],
    )
    store.begin_self_update_launch(job["id"])

    def terminalize_during_scan(root: Path) -> dict[str, Any]:
        assert root == release_root
        store.transition_from_active(
            job["id"],
            status="interrupted",
            stage="interrupted",
            progress=100,
            error="launch deadline expired; outcome is unknown",
        )
        return release

    monkeypatch.setattr(
        "hubinet_ops_release.inspect_staged_release",
        terminalize_during_scan,
    )

    with pytest.raises(ReleaseError, match="no longer active"):
        prepare_supervisor(
            release_root=release_root,
            result_dir=results,
            database=database,
            job_id=job["id"],
            expected_fingerprint=release["fingerprint"],
        )

    assert read_marker(results, job["id"])["status"] == "launching"


def test_supervisor_rechecks_terminal_job_after_release_scan_before_rollout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "jobs.db"
    results = tmp_path / "results"
    release_root = tmp_path / "release"
    (release_root / "deploy").mkdir(parents=True)
    _write(
        release_root / "deploy" / "upgrade-0.4.0-from-pve.sh",
        "#!/usr/bin/env bash\nexit 0\n",
    )
    release = inspect_staged_release(release_root)
    store = HostJobStore(database, results)
    job, _ = store.create(
        vmid=110,
        operation_type="self_update",
        request_id="request-terminal-before-rollout",
        argument=release["fingerprint"],
    )
    store.begin_self_update_launch(job["id"])
    write_marker(
        results,
        job["id"],
        {
            "job_id": job["id"],
            "status": "running",
            "fingerprint": release["fingerprint"],
            "deadline_at": "2999-01-01T00:00:00+00:00",
        },
    )

    def terminalize_during_scan(root: Path) -> dict[str, Any]:
        assert root == release_root
        store.transition_from_active(
            job["id"],
            status="interrupted",
            stage="interrupted",
            progress=100,
            error="rollout outcome is unknown",
        )
        return release

    def forbidden_rollout(*args: Any, **kwargs: Any) -> None:
        pytest.fail("rollout subprocess must not be invoked for a terminal job")

    monkeypatch.setattr(
        "hubinet_ops_release.inspect_staged_release",
        terminalize_during_scan,
    )
    monkeypatch.setattr("hubinet_ops_release.subprocess.run", forbidden_rollout)

    exit_code = run_supervisor(
        release_root=release_root,
        result_dir=results,
        database=database,
        job_id=job["id"],
        expected_fingerprint=release["fingerprint"],
    )

    assert exit_code == 125
    assert store.get(job["id"])["status"] == "interrupted"


def test_hostd_service_is_root_owned_and_hardened() -> None:
    service = (PVE / "hubinet-ops-hostd.service").read_text(encoding="utf-8")
    hostd = (PVE / "hubinet_ops_hostd.py").read_text(encoding="utf-8")
    wrapper = (PVE / "hubinet-ops-host").read_text(encoding="utf-8")
    assert "User=root" in service
    assert "ProtectSystem=strict" in service
    assert "EnvironmentFile=/etc/hubinet-ops/hostd.env" in service
    assert "MAX_REQUEST_BYTES = 16 * 1024" in hostd
    assert "shell=True" not in hostd
    assert "eval " not in wrapper
