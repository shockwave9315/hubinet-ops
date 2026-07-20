from __future__ import annotations

import json

import httpx
import pytest

from app.host_control import HostControlClient, HostControlError


def test_host_control_client_uses_typed_paths_bearer_and_idempotent_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_HOSTD_TOKEN", "t" * 64)
    requests: list[httpx.Request] = []
    polls = iter(
        [
            {"id": "job1", "status": "running"},
            {
                "id": "job1",
                "status": "succeeded",
                "result": {"lxc_status": "running"},
            },
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["Authorization"] == f"Bearer {'t' * 64}"
        if request.method == "POST":
            assert json.loads(request.content) == {"request_id": "request-12345678"}
            return httpx.Response(202, json={"id": "job1", "status": "queued"})
        return httpx.Response(200, json=next(polls))

    client = HostControlClient(
        {
            "base_url": "http://hostd.invalid:8741",
            "token_env": "TEST_HOSTD_TOKEN",
            "poll_interval_seconds": 0.001,
        },
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda _seconds: None,
    )

    result = client.execute("lifecycle_start", 110, "request-12345678")

    assert result == {"lxc_status": "running"}
    assert requests[0].url.path == "/api/v1/resources/110/start"
    assert all("command" not in request.url.params for request in requests)


def test_host_control_client_bounds_response_contract_and_never_accepts_command_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_HOSTD_TOKEN", "t" * 64)
    client = HostControlClient(
        {"base_url": "http://hostd.invalid", "token_env": "TEST_HOSTD_TOKEN"},
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(200, json={"snapshots": []})
            )
        ),
    )
    assert client.list_snapshots(106) == []
    with pytest.raises(HostControlError, match="Unsupported host operation"):
        client.execute("pct_exec", 106, "request-12345678")


def test_host_control_health_is_the_only_unauthenticated_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_HOSTD_TOKEN", "t" * 64)

    def handler(request: httpx.Request) -> httpx.Response:
        assert "Authorization" not in request.headers
        return httpx.Response(200, json={"status": "ok", "version": "0.4.0"})

    client = HostControlClient(
        {"base_url": "http://hostd.invalid", "token_env": "TEST_HOSTD_TOKEN"},
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert client.health()["version"] == "0.4.0"
