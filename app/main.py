from __future__ import annotations

import hmac
import logging
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, status
from pydantic import BaseModel, Field

from .config import Settings, load_settings
from .database import Database
from .executor import Executor, ExecutorError
from .host_control import HostControlClient, HostControlError
from .mqtt import MqttTelemetry, VERSION
from .resource_adapters import ResourceExecutor
from .service import ConflictError, OpsService

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


class PlanRequest(BaseModel):
    plan_id: str = Field(min_length=16, max_length=64, pattern=r"^[a-f0-9]+$")


class OperationRequest(BaseModel):
    request_id: str | None = Field(
        default=None,
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]+$",
    )


class SnapshotPruneRequest(OperationRequest):
    confirm: str | None = Field(default=None, max_length=64)


class SnapshotCreateRequest(OperationRequest):
    include_ram: bool = Field(default=False, strict=True)


def create_app(
    app_settings: Settings,
    *,
    database: Database | None = None,
    executor: Executor | None = None,
    mqtt: MqttTelemetry | None = None,
    host_control: HostControlClient | None = None,
) -> FastAPI:
    db = database or Database(app_settings.db_path)
    if executor is None:
        executor_config = dict(app_settings.executor)
        executor_config["allowed_vmids"] = sorted(app_settings.resources)
        executor = ResourceExecutor(
            Executor(executor_config),
            app_settings.resources,
        )
    mqtt = mqtt or MqttTelemetry(app_settings.mqtt, app_settings.resources)
    if host_control is None and app_settings.host_control.get("enabled"):
        host_control = HostControlClient(app_settings.host_control)
    service = OpsService(app_settings, db, executor, mqtt, host_control=host_control)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        service.start()
        try:
            yield
        finally:
            service.stop()

    api = FastAPI(
        title="Hubinet Ops Agent",
        version=VERSION,
        docs_url="/docs",
        redoc_url=None,
        lifespan=lifespan,
    )
    api.state.settings = app_settings
    api.state.database = db
    api.state.service = service

    def require_token(authorization: Annotated[str | None, Header()] = None) -> None:
        expected = f"Bearer {app_settings.api_token}"
        if not authorization or not hmac.compare_digest(authorization, expected):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    auth = [Depends(require_token)]

    @api.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "hubinet-ops", "version": VERSION}

    @api.get("/api/v1/containers", dependencies=auth)
    def containers() -> list[dict[str, Any]]:
        return service.list_containers()

    @api.get("/api/v1/resources", dependencies=auth)
    def resources() -> list[dict[str, Any]]:
        return service.list_resources()

    @api.get("/api/v1/resources/{vmid}", dependencies=auth)
    def resource(vmid: int) -> dict[str, Any]:
        try:
            return service.get_resource(vmid)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Resource not found") from exc

    @api.get("/api/v1/state", dependencies=auth)
    def states() -> dict[str, Any]:
        return service.list_states()

    @api.get("/api/v1/containers/{vmid}/state", dependencies=auth)
    def container_state(vmid: int) -> dict[str, Any]:
        if vmid not in app_settings.containers:
            raise HTTPException(status_code=404, detail="Container not found")
        try:
            return service.get_state(vmid)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Container not found") from exc

    @api.get("/api/v1/resources/{vmid}/state", dependencies=auth)
    def resource_state(vmid: int) -> dict[str, Any]:
        try:
            return service.get_state(vmid)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Resource not found") from exc

    @api.post("/api/v1/containers/{vmid}/refresh", dependencies=auth)
    def refresh_one(vmid: int) -> dict[str, Any]:
        if vmid not in app_settings.containers:
            raise HTTPException(status_code=404, detail="Container not found")
        try:
            return service.refresh_container(vmid, operator=True)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Container not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @api.post("/api/v1/resources/{vmid}/refresh", dependencies=auth)
    def refresh_resource(vmid: int) -> dict[str, Any]:
        try:
            return service.refresh_container(vmid, operator=True)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Resource not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @api.post("/api/v1/refresh", dependencies=auth)
    def refresh_all() -> list[dict[str, Any]]:
        return service.refresh_all(operator=True)

    @api.post("/api/v1/scan", dependencies=auth)
    def scan_all() -> list[dict[str, Any]]:
        try:
            return service.scan_all()
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @api.post("/api/v1/containers/{vmid}/scan", dependencies=auth)
    def scan_one(vmid: int) -> dict[str, Any]:
        if vmid not in app_settings.containers:
            raise HTTPException(status_code=404, detail="Container not found")
        try:
            return service.scan_container(vmid)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Container not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @api.post("/api/v1/resources/{vmid}/scan", dependencies=auth)
    def scan_resource(vmid: int) -> dict[str, Any]:
        try:
            return service.scan_container(vmid)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Resource not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    def lifecycle(
        vmid: int,
        action: str,
        request: OperationRequest | None = None,
    ) -> dict[str, Any]:
        try:
            return service.queue_lifecycle(
                vmid,
                action,
                request.request_id if request else None,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Container not found") from exc
        except (ValueError, ExecutorError, HostControlError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    def lifecycle_container_alias(
        vmid: int,
        action: str,
        request: OperationRequest | None = None,
    ) -> dict[str, Any]:
        if vmid not in app_settings.containers:
            raise HTTPException(status_code=404, detail="Container not found")
        return lifecycle(vmid, action, request)

    @api.post("/api/v1/containers/{vmid}/start", dependencies=auth)
    def start_container(vmid: int, request: OperationRequest | None = None) -> dict[str, Any]:
        return lifecycle_container_alias(vmid, "start", request)

    @api.post("/api/v1/containers/{vmid}/shutdown", dependencies=auth)
    def shutdown_container(vmid: int, request: OperationRequest | None = None) -> dict[str, Any]:
        return lifecycle_container_alias(vmid, "shutdown", request)

    @api.post("/api/v1/containers/{vmid}/reboot", dependencies=auth)
    def reboot_container(vmid: int, request: OperationRequest | None = None) -> dict[str, Any]:
        return lifecycle_container_alias(vmid, "reboot", request)

    @api.post("/api/v1/resources/{vmid}/start", dependencies=auth)
    def start_resource(vmid: int, request: OperationRequest | None = None) -> dict[str, Any]:
        return lifecycle(vmid, "start", request)

    @api.post("/api/v1/resources/{vmid}/shutdown", dependencies=auth)
    def shutdown_resource(vmid: int, request: OperationRequest | None = None) -> dict[str, Any]:
        return lifecycle(vmid, "shutdown", request)

    @api.post("/api/v1/resources/{vmid}/reboot", dependencies=auth)
    def reboot_resource(vmid: int, request: OperationRequest | None = None) -> dict[str, Any]:
        return lifecycle(vmid, "reboot", request)

    @api.post("/api/v1/resources/{vmid}/force-stop", dependencies=auth)
    def force_stop_resource(vmid: int, request: OperationRequest | None = None) -> dict[str, Any]:
        return lifecycle(vmid, "force-stop", request)

    @api.get("/api/v1/resources/{vmid}/snapshots", dependencies=auth)
    def snapshots(vmid: int) -> dict[str, Any]:
        try:
            return service.list_snapshots(vmid)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Resource not found") from exc
        except (ValueError, ExecutorError, HostControlError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @api.post("/api/v1/resources/{vmid}/snapshots", dependencies=auth)
    def create_snapshot(
        vmid: int,
        request: SnapshotCreateRequest | None = None,
    ) -> dict[str, Any]:
        try:
            return service.queue_snapshot_create(
                vmid,
                request.request_id if request else None,
                include_ram=request.include_ram if request else False,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Resource not found") from exc
        except (ValueError, ExecutorError, HostControlError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @api.post("/api/v1/resources/{vmid}/snapshots/{name}/restore", dependencies=auth)
    @api.post("/api/v1/resources/{vmid}/snapshots/{name}/rollback", dependencies=auth)
    def restore_snapshot(
        vmid: int,
        name: str,
        request: OperationRequest | None = None,
    ) -> dict[str, Any]:
        try:
            return service.queue_snapshot_action(
                vmid, "rollback", name, request.request_id if request else None
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Resource not found") from exc
        except (ValueError, ExecutorError, HostControlError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @api.delete("/api/v1/resources/{vmid}/snapshots/{name}", dependencies=auth)
    def delete_snapshot(
        vmid: int,
        name: str,
        request: OperationRequest | None = None,
    ) -> dict[str, Any]:
        try:
            return service.queue_snapshot_action(
                vmid, "delete", name, request.request_id if request else None
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Resource not found") from exc
        except (ValueError, ExecutorError, HostControlError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @api.post("/api/v1/resources/{vmid}/snapshots/delete-oldest", dependencies=auth)
    def delete_oldest_snapshot(
        vmid: int,
        request: SnapshotPruneRequest | None = None,
    ) -> dict[str, Any]:
        try:
            return service.queue_snapshot_prune(
                vmid,
                "oldest",
                request.request_id if request else None,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Resource not found") from exc
        except (ValueError, ExecutorError, HostControlError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @api.post("/api/v1/resources/{vmid}/snapshots/delete-unprotected", dependencies=auth)
    def delete_unprotected_snapshots(
        vmid: int,
        request: SnapshotPruneRequest,
    ) -> dict[str, Any]:
        try:
            return service.queue_snapshot_prune(
                vmid,
                "all_unprotected",
                request.request_id,
                confirmation=request.confirm,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Resource not found") from exc
        except (ValueError, ExecutorError, HostControlError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @api.post("/api/v1/resources/{vmid}/self-update", dependencies=auth)
    def self_update_resource(
        vmid: int,
        request: OperationRequest | None = None,
    ) -> dict[str, Any]:
        try:
            return service.create_self_update_plan(vmid)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Resource not found") from exc
        except ConflictError as exc:
            raise HTTPException(status_code=409, detail=exc.detail()) from exc
        except HostControlError as exc:
            raise HTTPException(
                status_code=502,
                detail={
                    "code": "application_release_unavailable",
                    "message": str(exc),
                },
            ) from exc
        except (ValueError, ExecutorError) as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "self_update_conflict",
                    "message": str(exc),
                },
            ) from exc

    @api.post("/api/v1/resources/{vmid}/retry-healthcheck", dependencies=auth)
    @api.post("/api/v1/containers/{vmid}/retry-healthcheck", dependencies=auth)
    def retry_healthcheck(
        vmid: int,
        request: OperationRequest | None = None,
    ) -> dict[str, Any]:
        try:
            return service.queue_retry_healthcheck(
                vmid, request.request_id if request else None
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Container not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @api.post("/api/v1/containers/{vmid}/rollback", dependencies=auth)
    def rollback(vmid: int) -> dict[str, Any]:
        try:
            return service.manual_rollback(vmid)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Container not found") from exc
        except (ValueError, HostControlError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @api.get("/api/v1/plans", dependencies=auth)
    def plans(
        status_filter: str | None = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> list[dict[str, Any]]:
        return db.list_plans(status=status_filter, limit=limit)

    @api.post("/api/v1/plans/approve", dependencies=auth)
    def approve(request: PlanRequest) -> dict[str, Any]:
        try:
            return service.approve(request.plan_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Plan not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @api.post("/api/v1/plans/reject", dependencies=auth)
    def reject(request: PlanRequest) -> dict[str, Any]:
        try:
            return service.reject(request.plan_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Plan not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @api.post("/api/v1/resources/{vmid}/plans/approve-active", dependencies=auth)
    def approve_active(
        vmid: int,
        request: OperationRequest | None = None,
    ) -> dict[str, Any]:
        try:
            return service.approve_active(vmid, request.request_id if request else None)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Resource not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @api.post("/api/v1/resources/{vmid}/plans/reject-active", dependencies=auth)
    def reject_active(vmid: int) -> dict[str, Any]:
        try:
            return service.reject_active(vmid)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Resource not found") from exc
        except (ValueError, ExecutorError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @api.get("/api/v1/jobs", dependencies=auth)
    def jobs(limit: Annotated[int, Query(ge=1, le=500)] = 100) -> list[dict[str, Any]]:
        return db.list_jobs(limit=limit)

    @api.get("/api/v1/jobs/{job_id}", dependencies=auth)
    def job(job_id: str) -> dict[str, Any]:
        try:
            return db.get_job(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Job not found") from exc

    @api.get("/api/v1/jobs/{job_id}/events", dependencies=auth)
    def job_events(
        job_id: str,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> list[dict[str, Any]]:
        try:
            return db.list_job_events(job_id, limit)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Job not found") from exc

    @api.get("/api/v1/containers/{vmid}/events", dependencies=auth)
    def container_events(
        vmid: int,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> list[dict[str, Any]]:
        if vmid not in app_settings.containers:
            raise HTTPException(status_code=404, detail="Container not found")
        return db.list_container_events(vmid, limit)

    @api.get("/api/v1/resources/{vmid}/events", dependencies=auth)
    def resource_events(
        vmid: int,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> list[dict[str, Any]]:
        if vmid not in app_settings.resources:
            raise HTTPException(status_code=404, detail="Resource not found")
        return db.list_resource_events(vmid, limit)

    return api


settings = load_settings()
app = create_app(settings)
