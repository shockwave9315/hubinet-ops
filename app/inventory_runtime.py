"""R0 composition root: inventory HTTP API plus automatic package scanning.

See ``ARCHITECTURE.md``.

This module is the only production entrypoint for Hubinet Ops 0.5's R0
runtime. It constructs exactly:

- ``app.inventory.store.InventoryAuthorityStore`` (opened against the R0
  authority DB path from configuration);
- ``app.inventory.authority.InventoryAuthority``;
- ``app.inventory.publication.InventoryPublication``;
- the R0 discovery scheduler (``app.inventory_scheduler.R0Scheduler``, via
  ``bootstrap_and_start_r0_runtime`` -- recovery, then config-drift
  reconciliation, then scheduling, in that exact order);
- the bounded HTTP route table below (read-only inventory, exact-plan
  approval, and per-resource health-contract configuration).

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
from typing import Annotated, Any

from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Path as ApiPath,
    Query,
    Request,
)
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.inventory import (
    AuthorityConflict,
    AuthorityNotFound,
    HealthProbeKind,
    InventoryAuthority,
    InventoryAuthorityStore,
    InventoryPublication,
    MAX_HEALTH_PROBES,
    MAX_HEALTH_PROBE_TARGET_LENGTH,
    MIN_HEALTH_PROBES,
    ResourceHealthContract,
    ResourceHealthProbe,
)
from app.inventory_runtime_config import R0RuntimeConfig, load_r0_runtime_config
from app.inventory_scheduler import R0Scheduler, bootstrap_and_start_r0_runtime
from app.package_scan_host_control import SshPackageScanHostControl
from app.package_scan_scheduler import PackageScanScheduler

_LOGGER = logging.getLogger(__name__)

API_PREFIX = "/r0/v1"
DEFAULT_R0_CONFIG_PATH = "/etc/hubinet-ops/inventory.yaml"
_CANONICAL_UUID_PATTERN = (
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_PLAN_FINGERPRINT_PATTERN = r"^[0-9a-f]{64}$"


class PackagePlanApprovalRequest(BaseModel):
    """The exact package-plan reference the operator reviewed."""

    model_config = ConfigDict(extra="forbid", strict=True)

    scan_run_id: str = Field(pattern=_CANONICAL_UUID_PATTERN)
    plan_fingerprint: str = Field(pattern=_PLAN_FINGERPRINT_PATTERN)


class HealthProbeRequest(BaseModel):
    """One required typed probe in a declared health contract.

    Exactly two fields, forever. There is no ``command``, ``argv``, ``shell``,
    ``script``, ``executable``, ``working_directory``, or ``environment``
    here, and ``extra="forbid"`` means a caller cannot smuggle one in: the
    future executor builds fixed argv from ``kind``, and ``target`` is data
    that only ever becomes one bounded argument.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    # `strict=False` on this one field only: JSON carries the enum's wire
    # value as a string, and strict mode would otherwise demand an actual
    # enum member. The value set stays exactly `HealthProbeKind` -- anything
    # else is still a 422.
    kind: Annotated[HealthProbeKind, Field(strict=False)]
    target: str = Field(min_length=1, max_length=MAX_HEALTH_PROBE_TARGET_LENGTH)


class HealthContractRequest(BaseModel):
    """The complete health contract an operator declares for one resource.

    Complete replacement only -- there is no probe-level patch verb -- and at
    least one probe, because an empty contract is malformed rather than
    trivially satisfied. ``expected_revision`` is an optional compare-and-set
    (``0`` meaning "currently unconfigured").
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    probes: list[HealthProbeRequest] = Field(
        min_length=MIN_HEALTH_PROBES, max_length=MAX_HEALTH_PROBES
    )
    expected_revision: int | None = Field(default=None, ge=0)


def _health_contract_error(status_code: int, error: str, message: str) -> HTTPException:
    """Build one machine-distinguishable health-contract failure.

    The taxonomy matters more here than anywhere else in this API: a caller
    MUST be able to tell "this resource is not the current authority target"
    from "this resource has no contract" from "your request was malformed".
    Collapsing the middle one into a successful empty contract would report
    absence as health, which is the exact thing this product refuses to do.
    """

    return HTTPException(
        status_code=status_code, detail={"error": error, "message": message}
    )


def _health_contract_body(
    resource_id: str, contract: ResourceHealthContract
) -> dict[str, Any]:
    return {
        "resource_id": resource_id,
        "status": "configured",
        "revision": contract.revision,
        "fingerprint": contract.fingerprint,
        "created_at": contract.created_at,
        "updated_at": contract.updated_at,
        "probes": [
            {"kind": probe.kind.value, "target": probe.target}
            for probe in contract.probes
        ],
    }


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
        # The config loader already guarantees this via R0ConfigError;
        # this is a defensive, redundant fail-closed check at the actual
        # point the token becomes security-relevant.
        raise RuntimeError("R0 API bearer token must not be empty")

    store = InventoryAuthorityStore(config.authority_db_path)
    authority = InventoryAuthority(store, now=now)
    # NEXT-A has no update executor. Startup only terminalizes active jobs
    # that are provably still pre-mutation; reserved mutation-intent states
    # stay durably fenced for a later recovery implementation.
    authority.recover_interrupted_package_update_jobs()
    authority.recover_interrupted_package_scans()
    scheduler_kwargs: dict[str, Any] = {"start": start_scheduler}
    if now is not None:
        scheduler_kwargs["now"] = now
    scheduler: R0Scheduler = bootstrap_and_start_r0_runtime(
        authority, store, config, **scheduler_kwargs
    )
    host_config = config.package_scan.host_control
    host_control = SshPackageScanHostControl(
        host=host_config.host,
        port=host_config.port,
        user=host_config.user,
        private_key_path=host_config.private_key_path,
        known_hosts_path=host_config.known_hosts_path,
        timeout_seconds=host_config.timeout_seconds,
        max_result_bytes=host_config.max_result_bytes,
    )
    package_scan_scheduler = PackageScanScheduler(
        authority,
        store,
        host_control,
        interval_seconds=config.package_scan.interval_seconds,
        initial_delay_seconds=config.package_scan.initial_delay_seconds,
    )
    if start_scheduler:
        package_scan_scheduler.start()
    publication = InventoryPublication(store, authority)

    @asynccontextmanager
    async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            package_scan_scheduler.stop()
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
    app.state.package_scan_scheduler = package_scan_scheduler
    app.state.package_scan_host_control = host_control
    app.state.publication = publication
    app.state.config = config

    expected_authorization = f"Bearer {config.api_bearer_token}"

    def _require_bearer_token(request: Request) -> None:
        # Constant-time comparison; the Authorization header value
        # itself is never logged anywhere in this module.
        provided = request.headers.get("authorization", "")
        if not hmac.compare_digest(provided, expected_authorization):
            raise HTTPException(status_code=401, detail="invalid or missing bearer token")

    @app.get(f"{API_PREFIX}/health")
    def health() -> JSONResponse:
        """Unauthenticated minimal liveness probe.

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
        ``validate_connection`` and ``fetch_backend_information``)."""

        view = publication.read()
        return _thaw(view.backend)

    @app.get(f"{API_PREFIX}/snapshot", dependencies=[Depends(_require_bearer_token)])
    def snapshot() -> dict[str, Any]:
        """The full published read-only inventory snapshot.

        Pure type conversion of ``InventoryPublication.read()``'s already-
        assembled, already-consistent view -- no new reconciliation,
        aggregation, or business logic belongs here. Note: this GET
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

    @app.put(
        f"{API_PREFIX}/resources/{{resource_id}}/package-plan-approval",
        dependencies=[Depends(_require_bearer_token)],
    )
    def approve_package_plan(
        body: PackagePlanApprovalRequest,
        resource_id: Annotated[str, ApiPath(pattern=_CANONICAL_UUID_PATTERN)],
    ) -> dict[str, Any]:
        """Persist approval of only the exact reviewed package-plan reference."""

        try:
            approval = authority.approve_package_plan(
                resource_id,
                body.scan_run_id,
                body.plan_fingerprint,
            )
        except AuthorityNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except AuthorityConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {
            "approval_id": approval.approval_id,
            "resource_id": approval.resource_id,
            "reviewed_scan_run_id": approval.reviewed_scan_run_id,
            "plan_fingerprint": approval.approved_plan_fingerprint,
            "approved_at": approval.approved_at,
        }

    # ------------------------------------------------------------------
    # Per-resource health contracts.
    #
    # Authority metadata only. These three routes read and write what
    # "healthy" would mean for one exact resource incarnation; none of them
    # runs a probe, issues a job, or advances any update-lifecycle state, and
    # there is no healthcheck executor for them to reach.
    # ------------------------------------------------------------------

    _HEALTH_CONTRACT_ROUTE = f"{API_PREFIX}/resources/{{resource_id}}/health-contract"

    def _health_contract_or_error(resource_id: str) -> ResourceHealthContract:
        try:
            contract = authority.resource_health_contract(resource_id)
        except AuthorityNotFound as exc:
            raise _health_contract_error(404, "resource_not_found", str(exc)) from exc
        except AuthorityConflict as exc:
            raise _health_contract_error(409, "resource_not_current", str(exc)) from exc
        if contract is None:
            raise _health_contract_error(
                404,
                "contract_unconfigured",
                "resource has no configured health contract",
            )
        return contract

    @app.get(
        _HEALTH_CONTRACT_ROUTE, dependencies=[Depends(_require_bearer_token)]
    )
    def read_health_contract(
        resource_id: Annotated[str, ApiPath(pattern=_CANONICAL_UUID_PATTERN)],
    ) -> dict[str, Any]:
        """Return one resource's complete current health contract.

        An unconfigured resource is a 404 carrying ``contract_unconfigured``,
        never a 200 with an empty probe list: absence of a contract is not a
        contract that nothing has to satisfy.
        """

        return _health_contract_body(
            resource_id, _health_contract_or_error(resource_id)
        )

    @app.put(
        _HEALTH_CONTRACT_ROUTE, dependencies=[Depends(_require_bearer_token)]
    )
    def replace_health_contract(
        body: HealthContractRequest,
        resource_id: Annotated[str, ApiPath(pattern=_CANONICAL_UUID_PATTERN)],
    ) -> dict[str, Any]:
        """Install one complete health contract. Authority state only."""

        try:
            contract = authority.replace_resource_health_contract(
                resource_id,
                tuple(
                    ResourceHealthProbe(kind=probe.kind, target=probe.target)
                    for probe in body.probes
                ),
                expected_revision=body.expected_revision,
            )
        except AuthorityNotFound as exc:
            raise _health_contract_error(404, "resource_not_found", str(exc)) from exc
        except AuthorityConflict as exc:
            raise _health_contract_error(409, "resource_not_current", str(exc)) from exc
        except ValueError as exc:
            raise _health_contract_error(422, "invalid_contract", str(exc)) from exc
        return _health_contract_body(resource_id, contract)

    @app.delete(
        _HEALTH_CONTRACT_ROUTE, dependencies=[Depends(_require_bearer_token)]
    )
    def clear_health_contract(
        resource_id: Annotated[str, ApiPath(pattern=_CANONICAL_UUID_PATTERN)],
        expected_revision: Annotated[int | None, Query(ge=0)] = None,
    ) -> dict[str, Any]:
        """Clear one resource's health contract.

        Afterwards the resource is unconfigured -- it has no declared meaning
        of healthy. That is emphatically not "healthy", and no caller may read
        it as one.
        """

        try:
            cleared = authority.clear_resource_health_contract(
                resource_id, expected_revision=expected_revision
            )
        except AuthorityNotFound as exc:
            raise _health_contract_error(404, "resource_not_found", str(exc)) from exc
        except AuthorityConflict as exc:
            raise _health_contract_error(409, "resource_not_current", str(exc)) from exc
        except ValueError as exc:
            raise _health_contract_error(422, "invalid_contract", str(exc)) from exc
        return {
            "resource_id": resource_id,
            "status": "unconfigured",
            "cleared": cleared,
        }

    return app


def create_app_from_env() -> FastAPI:
    """Zero-argument production factory for ``uvicorn --factory``.

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
