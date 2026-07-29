from __future__ import annotations

import importlib.util
import http.client
import json
import sqlite3
import subprocess
import sys
import threading
from datetime import UTC, datetime
from http.server import ThreadingHTTPServer
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
    parse_snapshot,
    run_forced_command,
)
from hubinet_ops_hostd import (  # noqa: E402
    HostdApplication,
    HostdConfig,
    HostdHandler,
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
                "snapname": "current",
                "description": "active LXC state",
                "current": 1,
            },
            {
                "snapname": "foreign-backup",
                "description": "not managed by Hubinet Ops",
                "snaptime": 1_700_000_000,
            },
            {
                "snapname": "hubinet-ops-106-manual-20260720T170000Z",
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
        elif (
            argv[:2] == ["pvesh", "get"]
            and len(argv) >= 3
            and argv[2].endswith("/snapshot")
        ):
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

    assert [
        "pvesh",
        "get",
        "/nodes/pve-a/lxc/106/snapshot",
        "--output-format",
        "json",
    ] in [call[0] for call in runner.calls]
    assert not any(call[0][:2] == ["pct", "listsnapshot"] for call in runner.calls)
    owned = next(
        item
        for item in snapshots
        if item["name"] == "hubinet-ops-106-manual-20260720T170000Z"
    )
    foreign = next(item for item in snapshots if item["name"] == "foreign-backup")
    current = next(item for item in snapshots if item["name"] == "current")
    assert current["name"] == "current"
    assert current["owned_by_hubinet_ops"] is False
    assert owned["kind"] == "manual"
    assert owned["created_at"] == "2024-07-20T16:20:00+00:00"
    assert owned["owned_by_hubinet_ops"] is True
    assert owned["rollback_eligible"] is True
    assert owned["delete_eligible"] is True
    assert foreign["owned_by_hubinet_ops"] is False
    assert foreign["kind"] is None
    assert foreign["rollback_eligible"] is False
    assert foreign["delete_eligible"] is False
    assert current["rollback_eligible"] is False
    assert current["delete_eligible"] is False
    assert owned_snapshot(owned["name"], 106)
    with pytest.raises(HostControlError, match="owned"):
        controller.execute("snapshot-delete", 106, "foreign-backup")


def test_snapshot_list_falls_back_to_legacy_name(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.snapshots = [{"name": "foreign-legacy"}]
    controller = HostController(policy(tmp_path), runner=runner)

    snapshots = controller.execute("list-snapshots", 106)["snapshots"]

    assert snapshots[0]["name"] == "foreign-legacy"
    assert snapshots[0]["owned_by_hubinet_ops"] is False


def test_snapshot_list_prefers_snapname_over_legacy_name(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.snapshots = [
        {
            "snapname": "foreign-authoritative",
            "name": "hubinet-ops-106-manual-20260720T170000Z",
        }
    ]
    controller = HostController(policy(tmp_path), runner=runner)

    snapshots = controller.execute("list-snapshots", 106)["snapshots"]

    assert snapshots[0]["name"] == "foreign-authoritative"
    assert snapshots[0]["owned_by_hubinet_ops"] is False


def test_host_snapshot_parser_normalizes_pre_alias_and_preserves_legacy() -> None:
    current = parse_snapshot(
        "hubinet-ops-106-pre-20260724T153100Z",
        106,
    )
    legacy = parse_snapshot(
        "hubinet-ops-106-pre-update-20260724T153100Z",
        106,
    )
    compact_manual = parse_snapshot(
        "hubinet-ops-999999-man-20260724T153100Z",
        999999,
    )

    assert current and current["kind"] == "pre-update"
    assert legacy and legacy["kind"] == "pre-update"
    assert compact_manual and compact_manual["kind"] == "manual"


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
            "snapname": name,
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
        "b" * 64,
        "u" * 64,
        "r" * 64,
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


class RecordingHostJobRunner:
    def __init__(self, controller: DummyController) -> None:
        self.controller = controller
        self.started: list[str] = []

    def start(self, job_id: str) -> None:
        self.started.append(job_id)


class NotifyingHostJobRunner(HostJobRunner):
    def __init__(self, store: HostJobStore, controller: DummyController) -> None:
        super().__init__(store, controller)  # type: ignore[arg-type]
        self.completed = threading.Event()

    def start(self, job_id: str) -> None:
        def run_and_notify() -> None:
            try:
                self.run(job_id)
            finally:
                self.completed.set()

        threading.Thread(target=run_and_notify, daemon=True).start()


class RecoveryController(DummyController):
    def __init__(self) -> None:
        super().__init__()
        self.status = "stopped"
        self.rollback_started = threading.Event()
        self.allow_completion = threading.Event()
        self.snapshot_name = "hubinet-ops-110-manual-20260724T120000Z"

    def execute(
        self,
        action: str,
        vmid: int,
        argument: str | None = None,
        *,
        source_job_id: str | None = None,
    ) -> dict[str, Any]:
        if action == "list-snapshots":
            self.calls.append((action, vmid, argument, source_job_id))
            return {
                "snapshots": [
                    {
                        "name": self.snapshot_name,
                        "owned_by_hubinet_ops": True,
                        "rollback_eligible": True,
                    }
                ]
            }
        if action == "snapshot-rollback":
            self.calls.append((action, vmid, argument, source_job_id))
            self.rollback_started.set()
            if not self.allow_completion.wait(timeout=5):
                raise RuntimeError("test did not release offline restore")
            return {
                "action": "rollback",
                "snapshot": argument,
                "lxc_status": "stopped",
            }
        return super().execute(
            action,
            vmid,
            argument,
            source_job_id=source_job_id,
        )


@pytest.mark.parametrize("initial_status", ["queued", "running"])
def test_http_lookup_by_request_is_read_only_and_preserves_active_job(
    tmp_path: Path,
    initial_status: str,
) -> None:
    store = HostJobStore(tmp_path / "jobs.db")
    job, _ = store.create(
        vmid=110,
        operation_type="snapshot_rollback",
        request_id=f"lookup-{initial_status}-request-0001",
        argument="hubinet-ops-110-manual-20260723T220000Z",
    )
    if initial_status == "running":
        store.begin_execution(job["id"])
    controller = DummyController()
    runner = RecordingHostJobRunner(controller)
    application = HostdApplication(
        HostdConfig(
            bind="127.0.0.1",
            port=0,
            database=tmp_path / "jobs.db",
            client_allowlist=frozenset(),
        ),
        store,
        runner,  # type: ignore[arg-type]
        "g" * 64,
        "b" * 64,
        "u" * 64,
        "r" * 64,
    )
    handler = type(
        "LookupHostdHandler",
        (HostdHandler,),
        {"app": application},
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    connection = http.client.HTTPConnection(
        "127.0.0.1",
        server.server_address[1],
        timeout=5,
    )
    headers = {"Authorization": f"Bearer {'g' * 64}"}
    try:
        path = f"/api/v1/jobs/by-request/110/{job['request_id']}"
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        payload = json.loads(response.read())
        assert response.status == 200
        assert {
            key: payload[key]
            for key in (
                "id",
                "vmid",
                "request_id",
                "operation_type",
                "argument",
                "status",
                "stage",
                "result",
                "error",
            )
        } == {
            "id": job["id"],
            "vmid": 110,
            "request_id": job["request_id"],
            "operation_type": "snapshot_rollback",
            "argument": "hubinet-ops-110-manual-20260723T220000Z",
            "status": initial_status,
            "stage": "queued" if initial_status == "queued" else "executing",
            "result": None,
            "error": None,
        }

        connection.request(
            "GET",
            "/api/v1/jobs/by-request/110/missing-request-0001",
            headers=headers,
        )
        missing = connection.getresponse()
        assert missing.status == 404
        assert json.loads(missing.read())["error"] == "host job not found"

        connection.request(
            "GET",
            "/api/v1/jobs/by-request/110/bad",
            headers=headers,
        )
        invalid = connection.getresponse()
        assert invalid.status == 400
        invalid.read()
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)

    assert runner.started == []
    assert controller.calls == []
    assert store.get(job["id"])["status"] == initial_status
    assert len(store.queued()) == (1 if initial_status == "queued" else 0)


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
    with pytest.raises(
        ValueError,
        match="request_id was already used for another operation",
    ):
        store.create(
            vmid=110,
            operation_type="lifecycle_shutdown",
            request_id="request-0001",
        )
    with pytest.raises(
        ValueError,
        match="request_id was already used for another operation",
    ):
        store.create(
            vmid=110,
            operation_type="lifecycle_start",
            request_id="request-0001",
            argument="different-argument",
        )
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
        "b" * 64,
        "u" * 64,
        "r" * 64,
    )

    assert application.authentication_scope(
        {"Authorization": f"Bearer {'g' * 64}"},
        "127.0.0.1",
    ) == "general"
    assert application.authentication_scope(
        {"Authorization": f"Bearer {'g' * 64}"},
        "127.0.0.1",
    ) != "self_update"
    assert application.authentication_scope(
        {"Authorization": f"Bearer {'u' * 64}"},
        "127.0.0.1",
    ) == "self_update"


def test_general_ha_scope_cannot_submit_online_destructive_jobs(
    tmp_path: Path,
) -> None:
    store = HostJobStore(tmp_path / "jobs.db")
    controller = DummyController()
    runner = RecordingHostJobRunner(controller)
    application = HostdApplication(
        HostdConfig("127.0.0.1", 0, tmp_path / "jobs.db", frozenset()),
        store,
        runner,  # type: ignore[arg-type]
        "g" * 64,
        "b" * 64,
        "u" * 64,
        "r" * 64,
    )
    handler = type("ScopedHostdHandler", (HostdHandler,), {"app": application})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for index, (method, path) in enumerate(
            [
                ("POST", "/api/v1/resources/110/shutdown"),
                ("POST", "/api/v1/resources/110/reboot"),
                ("POST", "/api/v1/resources/110/force-stop"),
                ("POST", "/api/v1/resources/110/self-update"),
                ("POST", "/api/v1/resources/110/snapshots"),
                (
                    "POST",
                    "/api/v1/resources/110/snapshots/"
                    "hubinet-ops-110-manual-20260724T120000Z/restore",
                ),
                (
                    "DELETE",
                    "/api/v1/resources/110/snapshots/"
                    "hubinet-ops-110-manual-20260724T120000Z",
                ),
            ]
        ):
            connection = http.client.HTTPConnection(
                "127.0.0.1", server.server_address[1], timeout=5
            )
            connection.request(
                method,
                path,
                body=json.dumps(
                    {
                        "request_id": f"ha-scope-denied-request-{index:04d}",
                        "name": "hubinet-ops-110-manual-20260724T120000Z",
                    }
                ),
                headers={
                    "Authorization": f"Bearer {'g' * 64}",
                    "Content-Type": "application/json",
                },
            )
            response = connection.getresponse()
            assert response.status == 403
            response.read()
            connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert store.queued() == []
    assert runner.started == []
    assert controller.calls == []


def test_backend_scope_submits_restore_and_general_scope_still_starts_ct110(
    tmp_path: Path,
) -> None:
    store = HostJobStore(tmp_path / "jobs.db")
    controller = DummyController()
    runner = RecordingHostJobRunner(controller)
    application = HostdApplication(
        HostdConfig("127.0.0.1", 0, tmp_path / "jobs.db", frozenset()),
        store,
        runner,  # type: ignore[arg-type]
        "g" * 64,
        "b" * 64,
        "u" * 64,
        "r" * 64,
    )
    handler = type("BackendScopedHostdHandler", (HostdHandler,), {"app": application})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        restore = http.client.HTTPConnection(
            "127.0.0.1", server.server_address[1], timeout=5
        )
        restore.request(
            "POST",
            "/api/v1/resources/110/snapshots/"
            "hubinet-ops-110-manual-20260724T120000Z/restore",
            body=json.dumps({"request_id": "backend-restore-request-0001"}),
            headers={
                "Authorization": f"Bearer {'b' * 64}",
                "Content-Type": "application/json",
            },
        )
        response = restore.getresponse()
        assert response.status == 202
        restore_job = json.loads(response.read())
        restore.close()
        assert restore_job["operation_type"] == "snapshot_rollback"
        store.transition_from_active(
            restore_job["id"],
            status="interrupted",
            stage="interrupted",
            progress=100,
            error="test cleanup",
        )

        start = http.client.HTTPConnection(
            "127.0.0.1", server.server_address[1], timeout=5
        )
        start.request(
            "POST",
            "/api/v1/resources/110/start",
            body=json.dumps({"request_id": "general-start-request-0001"}),
            headers={
                "Authorization": f"Bearer {'g' * 64}",
                "Content-Type": "application/json",
            },
        )
        response = start.getresponse()
        assert response.status == 202
        start_job = json.loads(response.read())
        start.close()
        assert start_job["operation_type"] == "lifecycle_start"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert len(runner.started) == 2


def test_offline_restore_persists_recovery_marker_before_rollback_and_survives_restart(
    tmp_path: Path,
) -> None:
    database = tmp_path / "jobs.db"
    store = HostJobStore(database)
    controller = RecoveryController()
    runner = NotifyingHostJobRunner(store, controller)
    application = HostdApplication(
        HostdConfig("127.0.0.1", 0, database, frozenset()),
        store,
        runner,
        "g" * 64,
        "b" * 64,
        "u" * 64,
        "r" * 64,
    )

    job, created = application.submit_recovery(
        vmid=110,
        operation_type="offline_snapshot_restore",
        request_id="offline-restore-request-0001",
        argument=controller.snapshot_name,
    )
    assert created is True
    assert controller.rollback_started.wait(timeout=5)
    running = store.list_unacknowledged_recovery_events()
    assert len(running) == 1
    assert running[0]["host_job_id"] == job["id"]
    assert running[0]["status"] == "running"
    assert running[0]["mutation_started_at"] is not None
    assert running[0]["completed_at"] is None

    restarted = HostJobStore(database)
    restarted_event = restarted.list_unacknowledged_recovery_events()[0]
    assert restarted_event["status"] == "running"
    assert (
        restarted_event["mutation_started_at"]
        == running[0]["mutation_started_at"]
    )
    controller.allow_completion.set()
    assert runner.completed.wait(timeout=5)
    terminal = store.get(job["id"])
    assert terminal["status"] == "succeeded"
    event = HostJobStore(database).list_unacknowledged_recovery_events()[0]
    assert event["status"] == "succeeded"
    assert event["result"]["snapshot"] == controller.snapshot_name
    assert event["completed_at"]


def test_offline_restore_http_requires_recovery_scope_and_explicit_confirmation(
    tmp_path: Path,
) -> None:
    store = HostJobStore(tmp_path / "jobs.db")
    controller = RecoveryController()
    runner = RecordingHostJobRunner(controller)
    application = HostdApplication(
        HostdConfig("127.0.0.1", 0, tmp_path / "jobs.db", frozenset()),
        store,
        runner,  # type: ignore[arg-type]
        "g" * 64,
        "b" * 64,
        "u" * 64,
        "r" * 64,
    )
    handler = type("RecoveryScopedHostdHandler", (HostdHandler,), {"app": application})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    path = (
        "/api/v1/resources/110/snapshots/"
        f"{controller.snapshot_name}/offline-restore"
    )

    def request(token: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        connection = http.client.HTTPConnection(
            "127.0.0.1", server.server_address[1], timeout=5
        )
        connection.request(
            "POST",
            path,
            body=json.dumps(payload),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        response = connection.getresponse()
        value = json.loads(response.read())
        connection.close()
        return response.status, value

    try:
        status, _ = request(
            "g" * 64,
            {"request_id": "offline-wrong-scope-request-0001"},
        )
        assert status == 403
        status, _ = request(
            "r" * 64,
            {"request_id": "offline-no-confirm-request-0001"},
        )
        assert status == 400
        assert store.list_unacknowledged_recovery_events() == []

        status, payload = request(
            "r" * 64,
            {
                "request_id": "offline-confirmed-request-0001",
                "confirm": "RESTORE_CT110_OFFLINE",
            },
        )
        assert status == 202
        assert payload["operation_type"] == "offline_snapshot_restore"
        recovery_list = http.client.HTTPConnection(
            "127.0.0.1", server.server_address[1], timeout=5
        )
        recovery_list.request(
            "GET",
            "/api/v1/recovery-events",
            headers={"Authorization": f"Bearer {'b' * 64}"},
        )
        response = recovery_list.getresponse()
        assert response.status == 200
        events = json.loads(response.read())["events"]
        recovery_list.close()
        assert len(events) == 1
        assert "mutation_started_at" in events[0]
        assert events[0]["mutation_started_at"] is None
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert len(store.list_unacknowledged_recovery_events()) == 1
    assert runner.started == [payload["id"]]


def test_offline_restore_rejects_running_ct110_foreign_snapshot_and_active_host_job(
    tmp_path: Path,
) -> None:
    store = HostJobStore(tmp_path / "jobs.db")
    controller = RecoveryController()
    runner = RecordingHostJobRunner(controller)
    application = HostdApplication(
        HostdConfig("127.0.0.1", 0, tmp_path / "jobs.db", frozenset()),
        store,
        runner,  # type: ignore[arg-type]
        "g" * 64,
        "b" * 64,
        "u" * 64,
        "r" * 64,
    )
    controller.status = "running"
    with pytest.raises(HostControlError, match="stopped"):
        application.submit_recovery(
            vmid=110,
            operation_type="offline_snapshot_restore",
            request_id="offline-running-request-0001",
            argument=controller.snapshot_name,
        )
    controller.status = "stopped"
    with pytest.raises(HostControlError, match="owned"):
        application.submit_recovery(
            vmid=110,
            operation_type="offline_snapshot_restore",
            request_id="offline-foreign-request-0001",
            argument="foreign-snapshot",
        )
    store.create(
        vmid=106,
        operation_type="lifecycle_shutdown",
        request_id="active-host-job-request-0001",
    )
    with pytest.raises(HostControlError, match="active"):
        application.submit_recovery(
            vmid=110,
            operation_type="offline_snapshot_restore",
            request_id="offline-blocked-request-0001",
            argument=controller.snapshot_name,
        )
    assert store.list_unacknowledged_recovery_events() == []
    assert runner.started == []


def test_recovery_marker_tracks_controller_failure(tmp_path: Path) -> None:
    class FailingRecoveryController(RecoveryController):
        def execute(
            self,
            action: str,
            vmid: int,
            argument: str | None = None,
            *,
            source_job_id: str | None = None,
        ) -> dict[str, Any]:
            if action == "snapshot-rollback":
                raise RuntimeError("exact pct rollback failure")
            return super().execute(
                action,
                vmid,
                argument,
                source_job_id=source_job_id,
            )

    database = tmp_path / "jobs.db"
    store = HostJobStore(database)
    controller = FailingRecoveryController()
    job, _ = store.create_recovery(
        vmid=110,
        operation_type="offline_snapshot_restore",
        request_id="offline-failure-request-0001",
        argument=controller.snapshot_name,
    )
    terminal = HostJobRunner(store, controller).run(job["id"])  # type: ignore[arg-type]
    assert terminal["status"] == "failed"
    failed = store.list_unacknowledged_recovery_events()[0]
    assert failed["status"] == "failed"
    assert failed["error"] == "exact pct rollback failure"
    assert failed["mutation_started_at"] is not None


def test_recovery_restart_distinguishes_queued_from_mutation_started(
    tmp_path: Path,
) -> None:
    queued_database = tmp_path / "queued.db"
    queued_store = HostJobStore(queued_database)
    queued, _ = queued_store.create_recovery(
        vmid=110,
        operation_type="offline_snapshot_restore",
        request_id="offline-queued-restart-0001",
        argument="hubinet-ops-110-manual-20260724T100000Z",
    )
    queued_controller = DummyController()
    HostJobStore(queued_database).reconcile_startup(queued_controller)  # type: ignore[arg-type]
    queued_event = HostJobStore(
        queued_database
    ).list_unacknowledged_recovery_events()[0]
    assert queued_event["host_job_id"] == queued["id"]
    assert queued_event["status"] == "interrupted"
    assert queued_event["mutation_started_at"] is None
    assert queued_controller.calls == []

    started_database = tmp_path / "started.db"
    started_store = HostJobStore(started_database)
    started, _ = started_store.create_recovery(
        vmid=110,
        operation_type="offline_snapshot_restore",
        request_id="offline-started-restart-0001",
        argument="hubinet-ops-110-manual-20260724T100000Z",
    )
    started_store.begin_execution(started["id"])
    marked = started_store.mark_recovery_mutation_started(started["id"])
    assert marked["mutation_started_at"] is not None
    started_controller = DummyController()
    HostJobStore(started_database).reconcile_startup(started_controller)  # type: ignore[arg-type]
    interrupted = HostJobStore(
        started_database
    ).list_unacknowledged_recovery_events()[0]
    assert interrupted["status"] == "interrupted"
    assert interrupted["mutation_started_at"] == marked["mutation_started_at"]
    assert "outcome is unknown" in interrupted["error"]
    assert started_controller.calls == []


def test_hostd_migrates_recovery_events_mutation_marker(tmp_path: Path) -> None:
    database = tmp_path / "legacy-hostd.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE host_jobs (
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
                launching_started_at TEXT,
                launch_deadline_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(vmid, request_id)
            );
            CREATE TABLE recovery_events (
                recovery_id TEXT PRIMARY KEY,
                host_job_id TEXT NOT NULL UNIQUE,
                request_id TEXT NOT NULL,
                vmid INTEGER NOT NULL,
                snapshot_name TEXT,
                operation_type TEXT NOT NULL,
                started_at TEXT NOT NULL,
                status TEXT NOT NULL,
                result_json TEXT,
                error TEXT,
                completed_at TEXT,
                acknowledged_at TEXT
            );
            """
        )

    HostJobStore(database)

    with sqlite3.connect(database) as connection:
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(recovery_events)")
        }
    assert "mutation_started_at" in columns


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
        "b" * 64,
        "u" * 64,
        "r" * 64,
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
        "b" * 64,
        "u" * 64,
        "r" * 64,
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
        "b" * 64,
        "u" * 64,
        "r" * 64,
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
        "b" * 64,
        "u" * 64,
        "r" * 64,
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
