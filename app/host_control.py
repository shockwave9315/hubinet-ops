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
    ) -> None:
        super().__init__(message)
        self.status = status
        self.result = dict(result or {})


class HostControlClient:
    """Typed client for the independent PVE host control service."""

    def __init__(
        self,
        config: dict[str, Any],
        *,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = dict(config)
        self.base_url = str(self.config.get("base_url") or "").rstrip("/")
        token_env = str(self.config.get("token_env") or "HUBINET_OPS_HOSTD_TOKEN")
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
        self.client = client or httpx.Client(timeout=self.timeout)
        self.sleep = sleep

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
        elif operation_type == "snapshot_create":
            method = "POST"
            path = f"/api/v1/resources/{int(vmid)}/snapshots"
            body["name"] = snapshot_name
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
        deadline = time.monotonic() + self.operation_timeout
        current = submitted
        last_poll_error: HostControlError | None = None
        while str(current.get("status")) not in {"succeeded", "failed", "interrupted"}:
            if time.monotonic() >= deadline:
                detail = f": {last_poll_error}" if last_poll_error else ""
                raise HostControlError(f"Timed out waiting for host control job{detail}")
            self.sleep(self.poll_interval)
            try:
                current = self._request("GET", f"/api/v1/jobs/{job_id}")
                last_poll_error = None
            except HostControlError as exc:
                # hostd is expected to restart during CT110 self-update. The durable
                # host job remains authoritative and is polled again without replay.
                if operation_type != "self_update":
                    raise
                last_poll_error = exc
        if current.get("status") != "succeeded":
            result = current.get("result")
            raise HostControlError(
                str(current.get("error") or "Host control job failed"),
                status=str(current.get("status") or "failed"),
                result=dict(result) if isinstance(result, dict) else {},
            )
        result = current.get("result")
        return dict(result) if isinstance(result, dict) else {}

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
            raise HostControlError(f"Host control request failed: {exc}") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise HostControlError("Host control returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise HostControlError("Host control returned a non-object response")
        if response.status_code >= 400:
            raise HostControlError(
                sanitize_text(payload.get("error") or f"HTTP {response.status_code}", limit=1000)
            )
        return payload
