"""R0 composition root: the final, sole production 0.5 read-only runtime.

Implements exactly §2 and §§17-20 of
``docs/architecture/0.5-r0-read-only-runtime-activation.md`` (WAVE R0-B,
Family 4).

This module is the only production entrypoint for Hubinet Ops 0.5's R0
read-only runtime. It constructs exactly:

- ``app.inventory.store.InventoryAuthorityStore`` (opened against the R0
  authority DB path from configuration);
- ``app.inventory.authority.InventoryAuthority``;
- ``app.inventory.publication.InventoryPublication``;
- the R0 discovery scheduler (``app.inventory_scheduler.R0Scheduler``, via
  ``bootstrap_and_start_r0_runtime`` -- recovery, then config-drift
  reconciliation, then scheduling, in that exact order, §42);
- the read-only HTTP route table below.

Import denylist (never import, directly or transitively, from this
module, ``app.inventory_scheduler``, or ``app.inventory_pve_transport``):
``app.main`` (constructs a live legacy ``OpsService`` at import time),
``app.service.OpsService``, ``app.service.ConflictError``,
``app.executor.Executor``, ``app.executor.ExecutorError``,
``app.resource_adapters.ResourceExecutor``,
``app.host_control.HostControlClient``, ``app.host_control.HostControlError``,
``app.mqtt.MqttTelemetry``, ``app.mqtt_budget``, ``app.database.Database``,
``app.stabilization``, ``app.ha_entities``, ``app.contracts``. This module
also never mounts, includes, or otherwise reuses ``app.main``'s FastAPI
instance or router in any form.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from datetime import datetime
import hmac
import logging
import os
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from app.inventory import InventoryAuthority, InventoryAuthorityStore, InventoryPublication
from app.inventory_runtime_config import R0RuntimeConfig, load_r0_runtime_config
from app.inventory_scheduler import R0Scheduler, bootstrap_and_start_r0_runtime

_LOGGER = logging.getLogger(__name__)

API_PREFIX = "/r0/v1"
DEFAULT_R0_CONFIG_PATH = "/etc/hubinet-ops/inventory.yaml"


def _thaw(value: Any) -> Any:
    """Convert frozen publication containers to plain JSON-shaped values."""

    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(item) for item in value]
    return value


def create_read_only_app(
    config: R0RuntimeConfig,
    *,
    start_scheduler: bool = True,
    now: Callable[[], datetime] | None = None,
) -> FastAPI:
    """Build the R0 FastAPI application.

    Fully synchronous construction: by the time this function returns, the
    authority DB is open (fail-closed per its own schema contract), startup
    recovery has run, source bootstrap/config-drift has been reconciled,
    and (unless ``start_scheduler=False``, a testing-only escape hatch) the
    discovery scheduler is running.

    ``now`` is a testing-only seam (matching the existing convention
    throughout ``app.inventory``) letting tests deterministically observe
    backend-owned freshness expiry through the HTTP layer; production
    callers should never pass it.
    """

    if not config.api_bearer_token or not config.api_bearer_token.strip():
        # Family 1 already guarantees this via R0ConfigError; this is a
        # defensive, redundant fail-closed check at the actual point the
        # token becomes security-relevant (§20: "reject empty/missing
        # configured API token at startup").
        raise RuntimeError("R0 API bearer token must not be empty")

    store = InventoryAuthorityStore(config.authority_db_path)
    authority = InventoryAuthority(store, now=now)
    scheduler_kwargs: dict[str, Any] = {"start": start_scheduler}
    if now is not None:
        scheduler_kwargs["now"] = now
    scheduler: R0Scheduler = bootstrap_and_start_r0_runtime(
        authority, store, config, **scheduler_kwargs
    )
    publication = InventoryPublication(store, authority)

    @asynccontextmanager
    async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            scheduler.stop()
            store.close()

    app = FastAPI(
        title="Hubinet Ops R0",
        # Minimal-exposure posture (AGENTS.md): no interactive API
        # documentation surface in production.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=_lifespan,
    )
    app.state.store = store
    app.state.authority = authority
    app.state.scheduler = scheduler
    app.state.publication = publication
    app.state.config = config

    expected_authorization = f"Bearer {config.api_bearer_token}"

    def _require_bearer_token(request: Request) -> None:
        # Constant-time comparison (§20); the Authorization header value
        # itself is never logged anywhere in this module.
        provided = request.headers.get("authorization", "")
        if not hmac.compare_digest(provided, expected_authorization):
            raise HTTPException(status_code=401, detail="invalid or missing bearer token")

    @app.get(f"{API_PREFIX}/health")
    def health() -> JSONResponse:
        """Unauthenticated minimal liveness probe (§19).

        Proves the process is alive and the authority DB is reachable.
        Must never leak inventory contents, credentials, or any other
        published fact -- the body is exactly ``{"status": "ok"}`` on
        success.
        """

        try:
            store.backend_instance()
        except Exception:  # noqa: BLE001 - liveness probe, never leak detail
            _LOGGER.exception("R0 health check failed: authority DB unreachable")
            return JSONResponse(status_code=503, content={"status": "unavailable"})
        return JSONResponse(status_code=200, content={"status": "ok"})

    @app.get(f"{API_PREFIX}/backend", dependencies=[Depends(_require_bearer_token)])
    def backend() -> dict[str, Any]:
        """Backend identity (the shared shape for both HA transport methods
        ``validate_connection`` and ``fetch_backend_information``, §19)."""

        view = publication.read()
        return _thaw(view.backend)

    @app.get(f"{API_PREFIX}/snapshot", dependencies=[Depends(_require_bearer_token)])
    def snapshot() -> dict[str, Any]:
        """The full published read-only inventory snapshot (§17/§19).

        Pure type conversion of ``InventoryPublication.read()``'s already-
        assembled, already-consistent view -- no new reconciliation,
        aggregation, or business logic belongs here. Note (§18): this GET
        may itself cause a backend-owned local freshness transition
        (fresh -> stale on an elapsed deadline) as a side effect of
        ``InventoryPublication.read()`` -- entirely by design, not a bug.
        """

        view = publication.read()
        return {
            "backend": _thaw(view.backend),
            "sources": [_thaw(item) for item in view.sources],
            "nodes": [_thaw(item) for item in view.nodes],
            "resources": [_thaw(item) for item in view.resources],
            "inventory_revision": view.inventory_revision,
            "published_state_revision": view.published_state_revision,
            "published_at": view.published_at,
        }

    return app


def create_app_from_env() -> FastAPI:
    """Zero-argument production factory for ``uvicorn --factory`` (§25).

    ``ExecStart=... uvicorn app.inventory_runtime:create_app_from_env
    --factory --host 0.0.0.0 --port 8787`` -- deliberately a separate
    function from :func:`create_read_only_app`, and never called at module
    import time, so that importing ``app.inventory_runtime`` itself (test
    #1's import-graph check, any tool inspection) never has a side effect
    of its own: no file I/O, no environment read, no authority DB open, no
    scheduler start. Config/secret loading and app construction only
    happen when uvicorn's factory mode actually calls this function once
    at process startup.
    """

    config_path = Path(os.environ.get("HUBINET_OPS_R0_CONFIG", DEFAULT_R0_CONFIG_PATH))
    config = load_r0_runtime_config(config_path)
    return create_read_only_app(config)
