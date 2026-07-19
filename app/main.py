from __future__ import annotations

import hmac
import logging
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, status
from pydantic import BaseModel, Field

from .config import Settings, load_settings
from .database import Database
from .executor import Executor
from .mqtt import MqttTelemetry, VERSION
from .service import OpsService

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


class PlanRequest(BaseModel):
    plan_id: str = Field(min_length=16, max_length=64, pattern=r"^[a-f0-9]+$")


def create_app(
    app_settings: Settings,
    *,
    database: Database | None = None,
    executor: Executor | None = None,
    mqtt: MqttTelemetry | None = None,
) -> FastAPI:
    db = database or Database(app_settings.db_path)
    if executor is None:
        executor_config = dict(app_settings.executor)
        executor_config["allowed_vmids"] = sorted(app_settings.containers)
        executor = Executor(executor_config)
    mqtt = mqtt or MqttTelemetry(app_settings.mqtt, app_settings.containers)
    service = OpsService(app_settings, db, executor, mqtt)

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

    @api.get("/api/v1/state", dependencies=auth)
    def states() -> dict[str, Any]:
        return service.list_states()

    @api.get("/api/v1/containers/{vmid}/state", dependencies=auth)
    def container_state(vmid: int) -> dict[str, Any]:
        try:
            return service.get_state(vmid)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Container not found") from exc

    @api.post("/api/v1/containers/{vmid}/refresh", dependencies=auth)
    def refresh_one(vmid: int) -> dict[str, Any]:
        try:
            return service.refresh_container(vmid, operator=True)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Container not found") from exc
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
        try:
            return service.scan_container(vmid)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Container not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    def lifecycle(vmid: int, action: str) -> dict[str, Any]:
        try:
            return service.lifecycle_container(vmid, action)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Container not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @api.post("/api/v1/containers/{vmid}/start", dependencies=auth)
    def start_container(vmid: int) -> dict[str, Any]:
        return lifecycle(vmid, "start")

    @api.post("/api/v1/containers/{vmid}/shutdown", dependencies=auth)
    def shutdown_container(vmid: int) -> dict[str, Any]:
        return lifecycle(vmid, "shutdown")

    @api.post("/api/v1/containers/{vmid}/reboot", dependencies=auth)
    def reboot_container(vmid: int) -> dict[str, Any]:
        return lifecycle(vmid, "reboot")

    @api.post("/api/v1/containers/{vmid}/retry-healthcheck", dependencies=auth)
    def retry_healthcheck(vmid: int) -> dict[str, Any]:
        try:
            return service.retry_healthcheck(vmid)
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
        except ValueError as exc:
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

    return api


settings = load_settings()
app = create_app(settings)
