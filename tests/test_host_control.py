from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
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
from hubinet_ops_release import write_marker  # noqa: E402


def _write(path: Path, value: str) -> Path:
    path.write_text(value, encoding="utf-8")
    return path


def policy(tmp_path: Path) -> HostPolicy:
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
    ("operation_type", "runtime_status"),
    [
        ("lifecycle_start", "running"),
        ("lifecycle_shutdown", "stopped"),
        ("lifecycle_force_stop", "stopped"),
    ],
)
def test_startup_reconciliation_uses_production_runtime_payload_without_replay(
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

    reconciled = store.reconcile(controller)  # type: ignore[arg-type]

    assert reconciled[0]["status"] == "succeeded"
    assert reconciled[0]["result"] == {
        "reconciled": True,
        "runtime_status": runtime_status,
    }
    assert controller.calls == [("status", 110, None, None)]
    assert store.get(job["id"])["status"] == "succeeded"


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

    reconciled = store.reconcile(controller)  # type: ignore[arg-type]

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

    reconciled = store.reconcile(controller)  # type: ignore[arg-type]

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

    reconciled = store.reconcile(controller)  # type: ignore[arg-type]

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
    initial.transition(
        job["id"],
        status="running",
        stage="executing",
        progress=10,
    )
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
    still_running = restarted.reconcile(DummyController())  # type: ignore[arg-type]

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

    reconciled = restarted.reconcile(DummyController())  # type: ignore[arg-type]

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
    store.transition(
        job["id"],
        status="running",
        stage="executing",
        progress=10,
    )

    terminal = HostJobStore(
        tmp_path / "jobs.db",
        tmp_path / "results",
    ).reconcile(DummyController())[0]  # type: ignore[arg-type]

    assert terminal["status"] == "interrupted"
    assert "outcome is unknown" in terminal["error"]


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
