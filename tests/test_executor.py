from __future__ import annotations

import io
from typing import Any

import pytest

from app.executor import Executor, ExecutorError


class FakeProcess:
    def __init__(self, stdout: str, stderr: str = "", returncode: int = 0):
        self.stdout = io.StringIO(stdout)
        self.stderr = io.StringIO(stderr)
        self.returncode = returncode
        self.killed = False

    def wait(self, timeout: int | None = None) -> int:
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


def executor() -> Executor:
    return Executor(
        {
            "proxmox_host": "proxmox.test",
            "ssh_key": "/keys/id",
            "known_hosts": "/keys/known",
            "allowed_vmids": [101, 106],
        }
    )


def test_ndjson_events_and_final_result(monkeypatch: pytest.MonkeyPatch) -> None:
    process = FakeProcess(
        "\n".join(
            [
                '{"type":"event","stage":"updating","progress":42,"level":"info","message":"package"}',
                '{"type":"event","stage":"updating","progress":20,"level":"info","message":"older"}',
                '{"type":"result","ok":true,"data":{"reboot_required":false}}',
            ]
        )
    )
    captured: dict[str, Any] = {}

    def popen(cmd: list[str], **kwargs: Any) -> FakeProcess:
        captured.update(kwargs)
        return process

    monkeypatch.setattr("app.executor.subprocess.Popen", popen)
    events: list[dict] = []
    result = executor().run("update", 106, on_event=events.append)
    assert result["data"]["reboot_required"] is False
    assert [item["progress"] for item in events] == [42, 42]
    assert captured["shell"] is False


def test_legacy_json_and_malformed_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    process = FakeProcess('not-json\n{"ok":true,"data":{"pending_count":2}}\n')
    monkeypatch.setattr("app.executor.subprocess.Popen", lambda *args, **kwargs: process)
    assert executor().run("scan", 106)["data"]["pending_count"] == 2


def test_malformed_event_fields_are_bounded_and_do_not_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = FakeProcess(
        "\n".join(
            [
                '{"type":"event","progress":"not-a-number","details":[1,2],"message":"event"}',
                '{"type":"result","ok":true,"data":{}}',
            ]
        )
    )
    monkeypatch.setattr("app.executor.subprocess.Popen", lambda *args, **kwargs: process)
    events: list[dict[str, Any]] = []
    executor().run("update", 106, on_event=events.append)
    assert events[0]["progress"] == 0
    assert events[0]["details"] == {}


def test_event_callback_failure_does_not_abort_or_repeat_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = FakeProcess(
        "\n".join(
            [
                '{"type":"event","progress":30,"message":"one"}',
                '{"type":"event","progress":40,"message":"two"}',
                '{"type":"result","ok":true,"data":{"done":true}}',
            ]
        )
    )
    calls = 0

    def popen(*args: Any, **kwargs: Any) -> FakeProcess:
        nonlocal calls
        calls += 1
        return process

    def broken_callback(_: dict[str, Any]) -> None:
        raise RuntimeError("database temporarily unavailable")

    monkeypatch.setattr("app.executor.subprocess.Popen", popen)
    result = executor().run("update", 106, on_event=broken_callback)
    assert result["data"]["done"] is True
    assert calls == 1


def test_bounded_stderr_and_error_data(monkeypatch: pytest.MonkeyPatch) -> None:
    process = FakeProcess(
        '{"ok":false,"data":{"docker":{"available":false}},"error":"failed"}\n',
        stderr="x" * 20_000,
        returncode=1,
    )
    monkeypatch.setattr("app.executor.subprocess.Popen", lambda *args, **kwargs: process)
    with pytest.raises(ExecutorError) as caught:
        executor().run("healthcheck", 106)
    assert caught.value.data["docker"]["available"] is False
    assert len(str(caught.value)) <= 2000


@pytest.mark.parametrize(
    ("action", "vmid", "argument"),
    [("shell", 106, None), ("update", 999, None), ("snapshot", 106, "bad;rm")],
)
def test_action_vmid_and_argument_injection_are_rejected(
    action: str,
    vmid: int,
    argument: str | None,
) -> None:
    with pytest.raises(ExecutorError):
        executor().run(action, vmid, argument)
