from __future__ import annotations

import os
import time
from typing import Any, Callable
from urllib.parse import quote

import httpx

from .security import sanitize_text


class HostControlError(RuntimeError):
    pass


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

    def execute(
        self,
        operation_type: str,
        vmid: int,
        request_id: str,
        *,
        snapshot_name: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"request_id": request_id}
        if operation_type.startswith("lifecycle_"):
            action = operation_type.removeprefix("lifecycle_").replace("force_stop", "force-stop")
            method = "POST"
            path = f"/api/v1/resources/{int(vmid)}/{action}"
        elif operation_type == "self_update":
            method = "POST"
            path = f"/api/v1/resources/{int(vmid)}/self-update"
        elif operation_type == "snapshot_create":
            method = "POST"
            path = f"/api/v1/resources/{int(vmid)}/snapshots"
            body["name"] = snapshot_name
        elif operation_type == "snapshot_rollback":
            method = "POST"
            path = (
                f"/api/v1/resources/{int(vmid)}/snapshots/"
                f"{quote(str(snapshot_name), safe='')}/rollback"
            )
        elif operation_type == "snapshot_delete":
            method = "DELETE"
            path = f"/api/v1/resources/{int(vmid)}/snapshots/{quote(str(snapshot_name), safe='')}"
        else:
            raise HostControlError("Unsupported host operation")
        submitted = self._request(method, path, json=body)
        job_id = str(submitted.get("id") or "")
        if not job_id:
            raise HostControlError("Host control did not return a job ID")
        deadline = time.monotonic() + self.operation_timeout
        current = submitted
        while str(current.get("status")) not in {"succeeded", "failed", "interrupted"}:
            if time.monotonic() >= deadline:
                raise HostControlError("Timed out waiting for host control job")
            self.sleep(self.poll_interval)
            current = self._request("GET", f"/api/v1/jobs/{job_id}")
        if current.get("status") != "succeeded":
            raise HostControlError(str(current.get("error") or "Host control job failed"))
        result = current.get("result")
        return dict(result) if isinstance(result, dict) else {}

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        authenticated: bool = True,
    ) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self.token}"} if authenticated else {}
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
