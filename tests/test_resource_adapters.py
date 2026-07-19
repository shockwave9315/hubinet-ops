from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from app.executor import ExecutorError
from app.resource_adapters import ResourceExecutor, SelfInspector


class RecordingExecutor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int, str | None]] = []

    def run(
        self,
        action: str,
        vmid: int,
        argument: str | None = None,
        timeout: int | None = None,
        on_event: Any = None,
    ) -> dict[str, Any]:
        self.calls.append((action, vmid, argument))
        key = "qemu_status" if vmid == 100 else "lxc_status"
        return {"ok": True, "data": {key: "running"}}


class FakeSelfInspector:
    def inspect(self) -> dict[str, Any]:
        return {"service_status": "active", "api_health": "ok"}


@pytest.fixture
def resources() -> dict[int, dict[str, Any]]:
    return {
        100: {"resource_type": "qemu", "adapter": "haos"},
        101: {"resource_type": "lxc", "adapter": "apt"},
        110: {"resource_type": "lxc", "adapter": "agent_self"},
    }


def test_adapter_routing_is_explicit(resources: dict[int, dict[str, Any]]) -> None:
    remote = RecordingExecutor()
    router = ResourceExecutor(remote, resources, FakeSelfInspector())  # type: ignore[arg-type]

    assert router.run("inspect", 100)["data"]["qemu_status"] == "running"
    assert router.run("inspect", 101)["data"]["lxc_status"] == "running"
    self_data = router.run("inspect", 110)["data"]

    assert self_data["service_status"] == "active"
    assert remote.calls == [
        ("inspect", 100, None),
        ("inspect", 101, None),
        ("status", 110, None),
    ]


@pytest.mark.parametrize("action", ["scan", "update", "snapshot", "rollback", "start"])
def test_qemu_rejects_managed_actions(
    resources: dict[int, dict[str, Any]], action: str
) -> None:
    remote = RecordingExecutor()
    router = ResourceExecutor(remote, resources)

    with pytest.raises(ExecutorError, match="haos adapter"):
        router.run(action, 100)
    assert remote.calls == []


def test_lxc_never_routes_to_qemu_action(resources: dict[int, dict[str, Any]]) -> None:
    remote = RecordingExecutor()
    router = ResourceExecutor(remote, resources)

    router.run("scan", 101)

    assert remote.calls == [("scan", 101, None)]


def test_self_adapter_never_runs_recursive_remote_inspect(
    resources: dict[int, dict[str, Any]],
) -> None:
    remote = RecordingExecutor()
    router = ResourceExecutor(remote, resources, FakeSelfInspector())  # type: ignore[arg-type]

    router.run("inspect", 110)

    assert remote.calls == [("status", 110, None)]


def test_self_inspector_uses_only_fixed_commands_and_bounds_output(tmp_path: Path) -> None:
    (tmp_path / "uptime").write_text("123.9 10", encoding="utf-8")
    (tmp_path / "meminfo").write_text(
        "MemTotal: 1000 kB\nMemAvailable: 400 kB\n", encoding="utf-8"
    )
    (tmp_path / "loadavg").write_text("0.25 0.20 0.10 1/100 1\n", encoding="utf-8")
    calls: list[list[str]] = []

    def runner(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if "is-active" in argv:
            return subprocess.CompletedProcess(argv, 0, "active\n", "")
        return subprocess.CompletedProcess(
            argv,
            0,
            "\n".join(["Authorization: Bearer secret warning"] * 30),
            "",
        )

    data = SelfInspector(runner=runner, proc_root=tmp_path).inspect()

    assert data["uptime_seconds"] == 123
    assert data["memory"]["used_bytes"] == 600 * 1024
    assert data["cpu"]["load_1m"] == 0.25
    assert len(data["recent_warnings"]) == 20
    assert "secret" not in "".join(data["recent_warnings"])
    assert calls[0] == ["/usr/bin/systemctl", "is-active", "hubinet-ops.service"]
    assert calls[1][0] == "/usr/bin/journalctl"
