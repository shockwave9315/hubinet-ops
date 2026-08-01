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
            "backend_token_env": "TEST_HOSTD_TOKEN",
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
        {"base_url": "http://hostd.invalid", "backend_token_env": "TEST_HOSTD_TOKEN"},
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(200, json={"snapshots": []})
            )
        ),
    )
    assert client.list_snapshots(106) == []
    with pytest.raises(HostControlError, match="Unsupported host operation"):
        client.execute("pct_exec", 106, "request-12345678")


@pytest.mark.parametrize(
    ("operation_type", "include_ram"),
    [("snapshot_create", False), ("snapshot_create_ram", True)],
)
def test_host_control_client_sends_typed_qemu_include_ram(
    monkeypatch: pytest.MonkeyPatch,
    operation_type: str,
    include_ram: bool,
) -> None:
    monkeypatch.setenv("TEST_HOSTD_TOKEN", "t" * 64)
    requests: list[httpx.Request] = []
    host_job_id = "d" * 32

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST":
            assert json.loads(request.content) == {
                "request_id": "vm100-snapshot-request",
                "name": "hubinet-ops-100-manual-20260729T120000Z",
                "include_ram": include_ram,
            }
            return httpx.Response(202, json={"id": host_job_id, "status": "queued"})
        return httpx.Response(
            200,
            json={
                "id": host_job_id,
                "status": "succeeded",
                "result": {
                    "name": "hubinet-ops-100-manual-20260729T120000Z",
                    "kind": "manual",
                    "source_job_id": host_job_id,
                    "pve_snaptime": 1785329640,
                },
            },
        )

    client = HostControlClient(
        {
            "base_url": "http://hostd.invalid",
            "backend_token_env": "TEST_HOSTD_TOKEN",
            "poll_interval_seconds": 0.001,
        },
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda _seconds: None,
    )

    client.execute(
        operation_type,
        100,
        "vm100-snapshot-request",
        snapshot_name="hubinet-ops-100-manual-20260729T120000Z",
    )
    assert requests[0].url.path == "/api/v1/resources/100/snapshots"


def test_host_control_client_rejects_snapshot_result_from_different_host_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_HOSTD_TOKEN", "t" * 64)
    host_job_id = "d" * 32

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                202,
                json={"id": host_job_id, "status": "queued"},
            )
        return httpx.Response(
            200,
            json={
                "id": host_job_id,
                "status": "succeeded",
                "result": {
                    "name": "hubinet-ops-106-pre-20260729T120000Z",
                    "kind": "pre-update",
                    "source_job_id": "e" * 32,
                },
            },
        )

    client = HostControlClient(
        {
            "base_url": "http://hostd.invalid",
            "backend_token_env": "TEST_HOSTD_TOKEN",
            "poll_interval_seconds": 0.001,
        },
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda _seconds: None,
    )

    with pytest.raises(HostControlError, match="does not match its host job"):
        client.execute(
            "snapshot_create",
            106,
            "pre-update-snapshot-request-0001",
            snapshot_name="hubinet-ops-106-pre-20260729T120000Z",
        )


def test_host_control_client_rejects_create_result_without_pve_snaptime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_HOSTD_TOKEN", "t" * 64)
    host_job_id = "d" * 32

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, json={"id": host_job_id, "status": "queued"})
        return httpx.Response(
            200,
            json={
                "id": host_job_id,
                "status": "succeeded",
                "result": {
                    "name": "hubinet-ops-106-pre-20260729T120000Z",
                    "kind": "pre-update",
                    "source_job_id": host_job_id,
                },
            },
        )

    client = HostControlClient(
        {
            "base_url": "http://hostd.invalid",
            "backend_token_env": "TEST_HOSTD_TOKEN",
            "poll_interval_seconds": 0.001,
        },
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda _seconds: None,
    )
    with pytest.raises(HostControlError, match="PVE snaptime"):
        client.execute(
            "snapshot_create",
            106,
            "missing-snaptime-create-result-0001",
            snapshot_name="hubinet-ops-106-pre-20260729T120000Z",
        )


def test_host_control_health_is_the_only_unauthenticated_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_HOSTD_TOKEN", "t" * 64)

    def handler(request: httpx.Request) -> httpx.Response:
        assert "Authorization" not in request.headers
        return httpx.Response(200, json={"status": "ok", "version": "0.4.1"})

    client = HostControlClient(
        {"base_url": "http://hostd.invalid", "backend_token_env": "TEST_HOSTD_TOKEN"},
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert client.health()["version"] == "0.4.1"


def test_backend_client_reads_and_acknowledges_recovery_events_with_backend_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_HOSTD_BACKEND_TOKEN", "b" * 64)
    requests: list[httpx.Request] = []
    event = {
        "recovery_id": "a" * 32,
        "request_id": "offline-recovery-request-0001",
        "vmid": 110,
        "snapshot_name": "hubinet-ops-110-manual-20260724T120000Z",
        "operation_type": "offline_snapshot_restore",
        "status": "succeeded",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["Authorization"] == f"Bearer {'b' * 64}"
        if request.method == "GET":
            return httpx.Response(200, json={"events": [event]})
        return httpx.Response(200, json={**event, "acknowledged_at": "now"})

    client = HostControlClient(
        {
            "base_url": "http://hostd.invalid",
            "backend_token_env": "TEST_HOSTD_BACKEND_TOKEN",
        },
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert client.list_recovery_events() == [event]
    assert client.acknowledge_recovery_event(event["recovery_id"])["acknowledged_at"] == "now"
    assert [request.url.path for request in requests] == [
        "/api/v1/recovery-events",
        f"/api/v1/recovery-events/{event['recovery_id']}/ack",
    ]


def test_self_update_poll_survives_transient_hostd_restart_without_resubmission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_HOSTD_TOKEN", "t" * 64)
    monkeypatch.setenv("TEST_HOSTD_UPDATE_TOKEN", "u" * 64)
    fingerprint = "a" * 64
    requests: list[httpx.Request] = []
    poll_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal poll_count
        requests.append(request)
        if request.method == "POST":
            assert request.headers["Authorization"] == f"Bearer {'u' * 64}"
            assert json.loads(request.content) == {
                "request_id": "self-update-request-0001",
                "fingerprint": fingerprint,
            }
            return httpx.Response(202, json={"id": "job-self", "status": "running"})
        poll_count += 1
        if poll_count == 1:
            raise httpx.ConnectError("hostd restarting", request=request)
        return httpx.Response(
            200,
            json={
                "id": "job-self",
                "status": "succeeded",
                "result": {"fingerprint": fingerprint, "exit_code": 0},
            },
        )

    client = HostControlClient(
        {
            "base_url": "http://hostd.invalid:8741",
            "backend_token_env": "TEST_HOSTD_TOKEN",
            "update_token_env": "TEST_HOSTD_UPDATE_TOKEN",
            "poll_interval_seconds": 0.001,
        },
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda _seconds: None,
    )

    result = client.execute(
        "self_update",
        110,
        "self-update-request-0001",
        release_fingerprint=fingerprint,
    )

    assert result == {"fingerprint": fingerprint, "exit_code": 0}
    assert len([request for request in requests if request.method == "POST"]) == 1


def test_normal_host_job_poll_retries_transient_get_without_resubmission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_HOSTD_TOKEN", "t" * 64)
    requests: list[httpx.Request] = []
    get_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal get_count
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(
                202,
                json={"id": "job-start", "status": "running"},
            )
        get_count += 1
        if get_count == 1:
            return httpx.Response(503, json={"error": "hostd temporarily unavailable"})
        return httpx.Response(
            200,
            json={
                "id": "job-start",
                "status": "succeeded",
                "result": {"lxc_status": "running"},
            },
        )

    client = HostControlClient(
        {
            "base_url": "http://hostd.invalid:8741",
            "backend_token_env": "TEST_HOSTD_TOKEN",
            "poll_interval_seconds": 0.001,
        },
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda _seconds: None,
    )

    assert client.execute(
        "lifecycle_start",
        110,
        "normal-transient-get-0001",
    ) == {"lxc_status": "running"}
    assert len([request for request in requests if request.method == "POST"]) == 1
    assert len([request for request in requests if request.method == "GET"]) == 2


@pytest.mark.parametrize(
    ("remote_operation", "remote_argument", "expected_error"),
    [
        ("snapshot_delete", "hubinet-ops-110-manual-20260723T220000Z", "operation_type"),
        ("snapshot_rollback", "hubinet-ops-110-manual-20260723T220001Z", "snapshot_name"),
    ],
)
def test_wait_existing_job_rejects_contract_mismatch_without_post(
    monkeypatch: pytest.MonkeyPatch,
    remote_operation: str,
    remote_argument: str,
    expected_error: str,
) -> None:
    monkeypatch.setenv("TEST_HOSTD_TOKEN", "t" * 64)
    requests: list[httpx.Request] = []
    request_id = "reattach-contract-0001"
    expected_snapshot = "hubinet-ops-110-manual-20260723T220000Z"

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        durable_argument = HostControlClient._snapshot_identity_argument(
            vmid=110,
            snapshot_name=remote_argument,
            snapshot_kind="manual",
            expected_source_job_id="a" * 32,
            expected_pve_snaptime=1785329640,
        )
        return httpx.Response(
            200,
            json={
                "id": "existing-job",
                "vmid": 110,
                "request_id": request_id,
                "operation_type": remote_operation,
                "argument": durable_argument,
                "status": "running",
                "stage": "executing",
                "result": None,
                "error": None,
            },
        )

    client = HostControlClient(
        {"base_url": "http://hostd.invalid", "backend_token_env": "TEST_HOSTD_TOKEN"},
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda _seconds: None,
    )

    with pytest.raises(HostControlError, match=expected_error) as captured:
        client.wait_existing_job(
            "snapshot_rollback",
            110,
            request_id,
            snapshot_name=expected_snapshot,
            snapshot_kind="manual",
            expected_source_job_id="a" * 32,
            expected_pve_snaptime=1785329640,
        )

    assert captured.value.status == "contract_mismatch"
    assert [request.method for request in requests] == ["GET"]


def test_wait_existing_job_retries_transient_lookup_without_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_HOSTD_TOKEN", "t" * 64)
    requests: list[httpx.Request] = []
    request_id = "reattach-transient-lookup-0001"

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            raise httpx.ConnectError("hostd restarting", request=request)
        return httpx.Response(
            200,
            json={
                "id": "existing-start",
                "vmid": 110,
                "request_id": request_id,
                "operation_type": "lifecycle_start",
                "argument": None,
                "status": "succeeded",
                "stage": "complete",
                "result": {"lxc_status": "running"},
                "error": None,
            },
        )

    client = HostControlClient(
        {
            "base_url": "http://hostd.invalid",
            "backend_token_env": "TEST_HOSTD_TOKEN",
            "poll_interval_seconds": 0.001,
        },
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda _seconds: None,
    )

    assert client.wait_existing_job(
        "lifecycle_start",
        110,
        request_id,
    ) == {"lxc_status": "running"}
    assert [request.method for request in requests] == ["GET", "GET"]
