from __future__ import annotations

import hmac
import logging
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field

from .config import load_settings
from .database import Database
from .executor import Executor
from .service import OpsService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

VERSION = "0.2.0"
settings = load_settings()
db = Database(settings.db_path)
executor = Executor(settings.executor)
service = OpsService(settings, db, executor)


@asynccontextmanager
async def lifespan(_: FastAPI):
    service.start()
    try:
        yield
    finally:
        service.stop()


app = FastAPI(
    title="Hubinet Ops Agent",
    version=VERSION,
    docs_url="/docs",
    redoc_url=None,
    lifespan=lifespan,
)


class PlanRequest(BaseModel):
    plan_id: str = Field(min_length=16, max_length=64, pattern=r"^[a-f0-9]+$")


def require_token(authorization: Annotated[str | None, Header()] = None) -> None:
    expected = f"Bearer {settings.api_token}"
    if not authorization or not hmac.compare_digest(authorization, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "hubinet-ops", "version": VERSION}


@app.get("/api/v1/containers", dependencies=[Depends(require_token)])
def containers() -> list[dict]:
    return service.list_containers()


@app.get("/api/v1/state", dependencies=[Depends(require_token)])
def states() -> dict:
    return service.list_states()


@app.get("/api/v1/containers/{vmid}/state", dependencies=[Depends(require_token)])
def container_state(vmid: int) -> dict:
    try:
        return service.get_state(vmid)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/v1/containers/{vmid}/refresh", dependencies=[Depends(require_token)])
def refresh_one(vmid: int) -> dict:
    try:
        return service.refresh_container(vmid)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/v1/refresh", dependencies=[Depends(require_token)])
def refresh_all() -> list[dict]:
    return service.refresh_all()


@app.post("/api/v1/scan", dependencies=[Depends(require_token)])
def scan_all() -> list[dict]:
    return service.scan_all()


@app.post("/api/v1/containers/{vmid}/scan", dependencies=[Depends(require_token)])
def scan_one(vmid: int) -> dict:
    try:
        return service.scan_container(vmid)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/v1/plans", dependencies=[Depends(require_token)])
def plans(status_filter: str | None = None, limit: int = 100) -> list[dict]:
    return db.list_plans(status=status_filter, limit=min(max(limit, 1), 500))


@app.post("/api/v1/plans/approve", dependencies=[Depends(require_token)])
def approve(request: PlanRequest) -> dict:
    try:
        return service.approve(request.plan_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Plan not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/v1/plans/reject", dependencies=[Depends(require_token)])
def reject(request: PlanRequest) -> dict:
    try:
        return service.reject(request.plan_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Plan not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/v1/jobs", dependencies=[Depends(require_token)])
def jobs(limit: int = 100) -> list[dict]:
    return db.list_jobs(limit=min(max(limit, 1), 500))


@app.get("/api/v1/jobs/{job_id}", dependencies=[Depends(require_token)])
def job(job_id: str) -> dict:
    try:
        return db.get_job(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Job not found") from exc
