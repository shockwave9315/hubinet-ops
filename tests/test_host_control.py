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
from hubinet_ops_hostd import HostJobRunner, HostJobStore  # noqa: E402


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
            return {"status": self.status}
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


def test_startup_reconciliation_never_replays_destructive_operation(tmp_path: Path) -> None:
    store = HostJobStore(tmp_path / "jobs.db")
    controller = DummyController()
    controller.status = "running"
    job, _ = store.create(
        vmid=110,
        operation_type="lifecycle_start",
        request_id="request-0004",
    )

    reconciled = store.reconcile(controller)  # type: ignore[arg-type]

    assert reconciled[0]["status"] == "succeeded"
    assert reconciled[0]["result"] == {"reconciled": True, "status": "running"}
    assert controller.calls == [("status", 110, None, None)]
    assert store.get(job["id"])["status"] == "succeeded"


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
