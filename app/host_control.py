from __future__ import annotations

import os
import time
from typing import Any, Callable
from urllib.parse import quote

import httpx

from .security import sanitize_text


class HostControlError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status: str | None = None,
        result: dict[str, Any] | None = None,
        http_status: int | None = None,
        code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.result = dict(result or {})
        self.http_status = http_status
        self.code = str(code or "") or None


class HostControlClient:
    """Typed client for the independent PVE host control service."""

    def __init__(
        self,
        config: dict[str, Any],
        *,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = dict(config)
        self.base_url = str(self.config.get("base_url") or "").rstrip("/")
        token_env = str(
            self.config.get("backend_token_env")
            or "HUBINET_OPS_HOSTD_BACKEND_TOKEN"
        )
        self.token = os.environ.get(token_env, "")
        update_token_env = str(
            self.config.get("update_token_env")
            or "HUBINET_OPS_HOSTD_UPDATE_TOKEN"
        )
        self.update_token = os.environ.get(update_token_env, "")
        self.update_token_env = update_token_env
        if not self.base_url:
            raise HostControlError("host_control.base_url is not configured")
        if len(self.token) < 32:
            raise HostControlError(f"Host control bearer token is missing from {token_env}")
        self.timeout = max(1, int(self.config.get("timeout_seconds", 30)))
        self.operation_timeout = max(1, int(self.config.get("operation_timeout_seconds", 1800)))
        self.poll_interval = max(0.1, float(self.config.get("poll_interval_seconds", 1)))
        self.poll_error_retries = max(
            1,
            int(self.config.get("poll_error_retries", 3)),
        )
        self.client = client or httpx.Client(timeout=self.timeout)
        self.sleep = sleep
        self.monotonic = monotonic

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/health", authenticated=False)

    def status(self, vmid: int) -> dict[str, Any]:
        return self._request("GET", f"/api/v1/resources/{int(vmid)}/status")

    def list_snapshots(self, vmid: int) -> list[dict[str, Any]]:
        result = self._request("GET", f"/api/v1/resources/{int(vmid)}/snapshots")
        values = result.get("snapshots")
        if not isinstance(values, list):
            raise HostControlError("Host control returned an invalid snapshot list")
        return [dict(item) for item in values if isinstance(item, dict)]

    def inspect_self_update_release(self, vmid: int) -> dict[str, Any]:
        result = self._request(
            "GET",
            f"/api/v1/resources/{int(vmid)}/self-update/release",
        )
        required = ("version", "release_id", "fingerprint")
        if not all(isinstance(result.get(key), str) and result[key] for key in required):
            raise HostControlError("Host control returned an invalid staged release")
        fingerprint = str(result["fingerprint"])
        if len(fingerprint) != 64 or any(char not in "0123456789abcdef" for char in fingerprint):
            raise HostControlError("Host control returned an invalid release fingerprint")
        return result

    def list_recovery_events(self) -> list[dict[str, Any]]:
        result = self._retry_read(
            lambda: self._request("GET", "/api/v1/recovery-events"),
            deadline=self.monotonic() + self.timeout,
        )
        if result is None:
            raise HostControlError("Host control returned no recovery event list")
        events = result.get("events")
        if not isinstance(events, list):
            raise HostControlError("Host control returned an invalid recovery event list")
        return [dict(item) for item in events if isinstance(item, dict)]

    def acknowledge_recovery_event(self, recovery_id: str) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/v1/recovery-events/{quote(str(recovery_id), safe='')}/ack",
            json={},
        )

    def execute(
        self,
        operation_type: str,
        vmid: int,
        request_id: str,
        *,
        snapshot_name: str | None = None,
        release_fingerprint: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"request_id": request_id}
        if operation_type.startswith("lifecycle_"):
            action = operation_type.removeprefix("lifecycle_").replace("force_stop", "force-stop")
            method = "POST"
            path = f"/api/v1/resources/{int(vmid)}/{action}"
        elif operation_type == "self_update":
            method = "POST"
            path = f"/api/v1/resources/{int(vmid)}/self-update"
            if not release_fingerprint:
                raise HostControlError("Self-update requires an approved release fingerprint")
            if len(self.update_token) < 32:
                raise HostControlError(
                    f"Self-update bearer token is missing from {self.update_token_env}"
                )
            body["fingerprint"] = release_fingerprint
        elif operation_type in {"snapshot_create", "snapshot_create_ram"}:
            method = "POST"
            path = f"/api/v1/resources/{int(vmid)}/snapshots"
            body["name"] = snapshot_name
            body["include_ram"] = operation_type == "snapshot_create_ram"
        elif operation_type == "snapshot_rollback":
            method = "POST"
            path = (
                f"/api/v1/resources/{int(vmid)}/snapshots/"
                f"{quote(str(snapshot_name), safe='')}/restore"
            )
        elif operation_type == "snapshot_delete":
            method = "DELETE"
            path = f"/api/v1/resources/{int(vmid)}/snapshots/{quote(str(snapshot_name), safe='')}"
        else:
            raise HostControlError("Unsupported host operation")
        submitted = self._request(
            method,
            path,
            json=body,
            bearer_token=(
                self.update_token if operation_type == "self_update" else None
            ),
        )
        job_id = str(submitted.get("id") or "")
        if not job_id:
            raise HostControlError("Host control did not return a job ID")
        result = self._wait_for_terminal(job_id, submitted)
        if operation_type in {"snapshot_create", "snapshot_create_ram"}:
            self._validate_snapshot_source_job_id(result, job_id)
        return result

    def find_job_by_request_id(
        self,
        vmid: int,
        request_id: str,
    ) -> dict[str, Any] | None:
        try:
            return self._request(
                "GET",
                f"/api/v1/jobs/by-request/{int(vmid)}/"
                f"{quote(str(request_id), safe='._:-')}",
            )
        except HostControlError as exc:
            if exc.http_status == 404:
                return None
            raise

    def get_job(self, job_id: str) -> dict[str, Any]:
        try:
            return self._request(
                "GET",
                f"/api/v1/jobs/{quote(str(job_id), safe='')}",
            )
        except HostControlError as exc:
            if exc.http_status == 404:
                raise HostControlError(
                    "Existing host control job disappeared; outcome is unknown",
                    status="not_found",
                    http_status=404,
                ) from exc
            raise

    def wait_existing_job(
        self,
        operation_type: str,
        vmid: int,
        request_id: str,
        *,
        snapshot_name: str | None = None,
        release_fingerprint: str | None = None,
    ) -> dict[str, Any]:
        deadline = self.monotonic() + self.operation_timeout
        current = self._retry_read(
            lambda: self.find_job_by_request_id(vmid, request_id),
            deadline=deadline,
        )
        if current is None:
            raise HostControlError(
                "Host control job was not found; operation outcome is unknown",
                status="not_found",
            )

        def validate(remote: dict[str, Any]) -> None:
            self._validate_existing_contract(
                remote,
                operation_type=operation_type,
                vmid=vmid,
                request_id=request_id,
                snapshot_name=snapshot_name,
                release_fingerprint=release_fingerprint,
            )

        validate(current)
        job_id = str(current.get("id") or "")
        if not job_id:
            raise HostControlError(
                "Host control lookup returned no job ID",
                status="contract_mismatch",
            )
        result = self._wait_for_terminal(
            job_id,
            current,
            deadline=deadline,
            validate=validate,
        )
        if operation_type in {"snapshot_create", "snapshot_create_ram"}:
            self._validate_snapshot_source_job_id(result, job_id)
        return result

    @staticmethod
    def _validate_snapshot_source_job_id(
        result: dict[str, Any],
        host_job_id: str,
    ) -> None:
        source_job_id = str(result.get("source_job_id") or "")
        if (
            len(host_job_id) != 32
            or any(char not in "0123456789abcdef" for char in host_job_id)
            or source_job_id != host_job_id
        ):
            raise HostControlError(
                "Host snapshot result source job ID does not match its host job",
                status="contract_mismatch",
            )

    def _wait_for_terminal(
        self,
        job_id: str,
        current: dict[str, Any],
        *,
        deadline: float | None = None,
        validate: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        deadline = (
            deadline
            if deadline is not None
            else self.monotonic() + self.operation_timeout
        )
        if validate is not None:
            validate(current)
        while str(current.get("status")) not in {"succeeded", "failed", "interrupted"}:
            if self.monotonic() >= deadline:
                raise HostControlError(
                    "Timed out waiting for existing host control job; outcome is unknown",
                    status="unavailable",
                )
            self.sleep(self.poll_interval)
            current = self._retry_read(
                lambda: self.get_job(job_id),
                deadline=deadline,
            )
            if validate is not None and current is not None:
                validate(current)
        if current.get("status") != "succeeded":
            result = current.get("result")
            raise HostControlError(
                str(current.get("error") or "Host control job failed"),
                status=str(current.get("status") or "failed"),
                result=dict(result) if isinstance(result, dict) else {},
            )
        result = current.get("result")
        return dict(result) if isinstance(result, dict) else {}

    def _retry_read(
        self,
        operation: Callable[[], dict[str, Any] | None],
        *,
        deadline: float,
    ) -> dict[str, Any] | None:
        last_error: HostControlError | None = None
        for attempt in range(self.poll_error_retries):
            try:
                return operation()
            except HostControlError as exc:
                if not self._transient_read_error(exc):
                    raise
                last_error = exc
                if attempt + 1 >= self.poll_error_retries or self.monotonic() >= deadline:
                    break
                self.sleep(self.poll_interval)
        raise HostControlError(
            f"Host control is temporarily unavailable during read-only polling: {last_error}",
            status="unavailable",
            http_status=last_error.http_status if last_error else None,
        ) from last_error

    @staticmethod
    def _transient_read_error(error: HostControlError) -> bool:
        return (
            error.http_status is None
            or error.http_status in {408, 429}
            or error.http_status >= 500
        )

    @staticmethod
    def _validate_existing_contract(
        current: dict[str, Any],
        *,
        operation_type: str,
        vmid: int,
        request_id: str,
        snapshot_name: str | None,
        release_fingerprint: str | None,
    ) -> None:
        try:
            remote_vmid = int(current.get("vmid"))
        except (TypeError, ValueError) as exc:
            raise HostControlError(
                "Host control job VMID is invalid",
                status="contract_mismatch",
            ) from exc
        expected_argument: str | None = None
        if operation_type == "self_update":
            expected_argument = release_fingerprint
        elif operation_type.startswith("snapshot_"):
            expected_argument = snapshot_name
        remote_argument = current.get("argument")
        if remote_argument is None and operation_type == "self_update":
            remote_argument = current.get("fingerprint")
        if remote_argument is None and operation_type.startswith("snapshot_"):
            remote_argument = current.get("snapshot_name")
        mismatches: list[str] = []
        if remote_vmid != int(vmid):
            mismatches.append("vmid")
        if str(current.get("request_id") or "") != str(request_id):
            mismatches.append("request_id")
        if str(current.get("operation_type") or "") != operation_type:
            mismatches.append("operation_type")
        if remote_argument != expected_argument:
            if operation_type == "self_update":
                mismatches.append("fingerprint")
            elif operation_type.startswith("snapshot_"):
                mismatches.append("snapshot_name")
            else:
                mismatches.append("argument")
        if mismatches:
            raise HostControlError(
                "Host control job contract mismatch: " + ", ".join(mismatches),
                status="contract_mismatch",
            )

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        authenticated: bool = True,
        bearer_token: str | None = None,
    ) -> dict[str, Any]:
        token = bearer_token or self.token
        headers = {"Authorization": f"Bearer {token}"} if authenticated else {}
        try:
            response = self.client.request(
                method,
                f"{self.base_url}{path}",
                headers=headers,
                json=json,
                timeout=self.timeout,
            )
        except httpx.HTTPError as exc:
            raise HostControlError(
                f"Host control request failed: {exc}",
                status="unavailable",
            ) from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise HostControlError("Host control returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise HostControlError("Host control returned a non-object response")
        if response.status_code >= 400:
            raise HostControlError(
                sanitize_text(payload.get("error") or f"HTTP {response.status_code}", limit=1000),
                http_status=response.status_code,
                code=sanitize_text(payload.get("code"), limit=100) or None,
            )
        return payload
