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
- when, and only when, ``package_update.enabled`` is configured true: the
  five dedicated production host controls and the ONE
  ``app.package_update_worker.PackageUpdateWorker`` that composes the
  existing snapshot/execution-gate/mutation/health/rollback stages;
- the bounded HTTP route table below (read-only inventory, exact-plan
  approval, per-resource health-contract configuration, and the explicit
  operator update controls).

Production update activation, and what it deliberately is not
-------------------------------------------------------------

A real workload package mutation may begin for exactly one reason: an
authenticated operator invoked ``POST
/r0/v1/resources/{resource_id}/package-update``. That route resolves the
resource's own CURRENT durable approval itself and calls
``InventoryAuthority.issue_package_update_job``; the caller supplies a
``request_id`` and nothing else. There is no field here through which a
caller can name a VMID, a node, a package, a version, an architecture, a
plan fingerprint, a snapshot, a probe, a contract revision, a command, an
argv, a host, or a helper operation, and ``extra="forbid"`` means one cannot
be smuggled in.

Nothing else can start an update. Neither scheduler issues a job, no scan
callback does, no approval write does, no coordinator poll does, and the
worker itself cannot: it continues jobs an operator already started and
never invents one. Rollback is the same shape -- an explicit authenticated
operator request, durable before it is acknowledged, and never automatic
after a failed mutation or a failed or unknown health verdict.

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
from dataclasses import dataclass
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
    HealthContractRevisionConflict,
    HealthProbeKind,
    InventoryAuthority,
    InventoryAuthorityStore,
    InventoryPublication,
    MAX_HEALTH_PROBES,
    MAX_HEALTH_PROBE_TARGET_LENGTH,
    MIN_HEALTH_PROBES,
    PackageUpdateCheckpoint,
    PackageUpdateIssuanceRefused,
    PackageUpdateJob,
    PackageUpdateJobStatus,
    ProductUpdateFenceError,
    ResourceHealthContract,
    ResourceHealthProbe,
)
from app.inventory_runtime_config import (
    PACKAGE_UPDATE_EXECUTION_TIMEOUT_SECONDS,
    PACKAGE_UPDATE_HEALTH_TIMEOUT_SECONDS,
    PACKAGE_UPDATE_MAX_RESULT_BYTES,
    PACKAGE_UPDATE_MUTATION_SUBMISSION_TIMEOUT_SECONDS,
    PACKAGE_UPDATE_MUTATION_TIMEOUT_SECONDS,
    PACKAGE_UPDATE_ROLLBACK_INSPECTION_TIMEOUT_SECONDS,
    PACKAGE_UPDATE_ROLLBACK_SUBMISSION_TIMEOUT_SECONDS,
    PACKAGE_UPDATE_SNAPSHOT_TIMEOUT_SECONDS,
    R0RuntimeConfig,
    load_r0_runtime_config,
)
from app.inventory_scheduler import R0Scheduler, bootstrap_and_start_r0_runtime
from app.package_scan_host_control import SshPackageScanHostControl
from app.package_scan_scheduler import PackageScanScheduler
from app.package_update_execution_host_control import (
    SshPackageUpdateExecutionHostControl,
)
from app.package_update_health import PackageUpdateHealthOrchestrator
from app.package_update_health_host_control import SshPackageUpdateHealthHostControl
from app.package_update_mutation import PackageUpdateMutationOrchestrator
from app.package_update_mutation_host_control import (
    SshPackageUpdateMutationHostControl,
)
from app.package_update_rollback import PackageUpdateRollbackOrchestrator
from app.package_update_rollback_host_control import (
    SshPackageUpdateRollbackHostControl,
)
from app.package_update_snapshot import (
    PackageUpdateSnapshotHostControl,
    PackageUpdateSnapshotOrchestrator,
)
from app.package_update_snapshot_host_control import (
    SshPackageUpdateSnapshotHostControl,
)
from app.package_update_worker import PackageUpdateWorker

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


class PackageUpdateStartRequest(BaseModel):
    """Everything an operator may say when starting an update: one UUID.

    ``request_id`` is the caller's idempotency key and the ONLY
    caller-controlled material in the whole production update lifecycle.
    ``extra="forbid"`` is load-bearing: there is deliberately no field here
    for a VMID, a node, a package name, a package version, an architecture,
    a plan fingerprint, a snapshot name or id, a health probe, a contract
    revision, a command, an argv, a host, or a helper operation, and a caller
    who sends one gets a 422 rather than having it ignored. The backend
    resolves the resource's own current durable approval; what gets installed
    is decided by authority, never by the request body.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    request_id: str = Field(pattern=_CANONICAL_UUID_PATTERN)


class ProductUpdateFenceRequest(BaseModel):
    """The one field a Hubinet PRODUCT update supplies to fence itself in.

    ``holder`` is the product updater's own run id -- an opaque bounded
    label this backend compares for equality and nothing else. It is not a
    workload parameter: there is no resource, VMID, package, snapshot, or
    job anywhere on this surface, and taking the fence performs no workload
    action of any kind. It only makes future workload starts refuse.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    holder: str = Field(pattern=r"^[0-9A-Za-z-]{1,64}$")


class PackageUpdateResumeRequest(BaseModel):
    """A request to re-enter an existing recoverable job. No stage, no target.

    Empty on purpose. "Resume" means *inspect the durable checkpoint and
    invoke only the existing safe continuation semantics for whatever state
    it turns out to be in* -- it is emphatically not "submit the destructive
    command again", and there is no caller-supplied stage, checkpoint,
    operation, or snapshot for it to be pointed at.
    """

    model_config = ConfigDict(extra="forbid", strict=True)


class PackageUpdateRollbackRequestBody(BaseModel):
    """A request to roll one resource back to its own job's snapshot.

    Also empty, and for the same reason: the operator selects a RESOURCE.
    The backend resolves the one applicable ACTIVE job and reuses the exact
    same-job rollback selection contract, which derives the target from
    durable authority. There is no snapshot name, snapshot id, VMID, node,
    operation id, or rollback target anywhere on this surface, and no
    "latest snapshot" fallback exists to fall back to.
    """

    model_config = ConfigDict(extra="forbid", strict=True)


def _package_update_error(
    status_code: int, error: str, message: str
) -> HTTPException:
    """Build one machine-distinguishable package-update failure.

    Same taxonomy discipline as the health-contract errors below: an operator
    must be able to tell "nobody approved a plan" from "the plan drifted"
    from "another update owns the slot". Every ``error`` an issuance refusal
    produces is the exact reason
    :meth:`InventoryAuthority.issue_package_update_job` itself committed --
    this layer renders a decision, it never makes one.
    """

    return HTTPException(
        status_code=status_code, detail={"error": error, "message": message}
    )


#: Issuance refusals that mean "not right now" rather than "not like this".
#: Rendered as 503 so a caller can tell a transient condition from a plan or
#: authority problem it has to do something about.
_RETRYABLE_ISSUANCE_REFUSALS = frozenset(
    {"source_authority_unavailable", "product_update_in_progress"}
)


def _package_update_job_body(job: PackageUpdateJob) -> dict[str, Any]:
    """Render one job as bounded typed facts.

    Deliberately absent: helper stdout/stderr, raw PVE task logs, command
    text, credentials, the frozen package rows, and the per-probe health
    result rows. What an operator needs here is what the job IS and what it
    is doing; exact material is read through the actions that exist for it.
    """

    return {
        "job_id": job.job_id,
        "request_id": job.request_id,
        "resource_id": job.resource_id,
        "status": job.status.value,
        "checkpoint": job.checkpoint.value,
        "issued_at": job.issued_at,
        "approved_plan_fingerprint": job.approved_plan_fingerprint,
        "package_count": job.package_count,
        "snapshot": {
            "operation_id": job.snapshot_operation_id,
            "name": job.snapshot_name,
            "intent_recorded_at": job.snapshot_intent_recorded_at,
            "task_upid": job.snapshot_task_upid,
            "confirmed_at": job.snapshot_confirmed_at,
        },
        "mutation": {
            "operation_id": job.mutation_operation_id,
            "may_have_started_at": job.mutation_may_have_started_at,
            "completed_at": job.mutation_completed_at,
        },
        "health": {
            "contract_revision": job.health_contract_revision,
            "contract_fingerprint": job.health_contract_fingerprint,
            "probe_count": job.health_contract_probe_count,
            "started_at": job.health_started_at,
            "completed_at": job.health_completed_at,
            "outcome": None if job.health_outcome is None else job.health_outcome.value,
        },
        "rollback": {
            "operation_id": job.rollback_operation_id,
            "may_have_started_at": job.rollback_may_have_started_at,
            "task_upid": job.rollback_task_upid,
            "completed_at": job.rollback_completed_at,
            "available": _rollback_available(job),
        },
        "terminalized_at": job.terminalized_at,
        "terminal_reason": job.terminal_reason,
    }


#: The exact four durable checkpoints from which
#: :meth:`InventoryAuthority.arm_package_update_rollback` accepts an ACTIVE
#: job. Mirrored here for a read-only *availability* hint in the readback
#: body, never as a second eligibility decision: the arming transaction
#: re-proves the whole rule itself, and a request that races past this hint
#: is refused there, not here.
_ROLLBACK_ELIGIBLE_CHECKPOINTS = (
    PackageUpdateCheckpoint.MUTATION_MAY_HAVE_STARTED,
    PackageUpdateCheckpoint.MUTATION_COMPLETED,
    PackageUpdateCheckpoint.HEALTH_STARTED,
    PackageUpdateCheckpoint.HEALTH_COMPLETED,
)


def _rollback_available(job: PackageUpdateJob) -> bool:
    return (
        job.status is PackageUpdateJobStatus.ACTIVE
        and job.checkpoint in _ROLLBACK_ELIGIBLE_CHECKPOINTS
    )


def _package_update_event_body(event: Any) -> dict[str, Any]:
    return {
        "sequence": event.sequence,
        "created_at": event.created_at,
        "level": event.level.value,
        "stage": event.stage.value,
        "event_type": event.event_type.value,
        "message": event.message,
        "details": dict(event.details),
    }


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


@dataclass(frozen=True, slots=True)
class PackageUpdateRuntime:
    """The production update composition: one worker, one read-only observer.

    ``snapshot_host_control`` is held here, beside the worker, for exactly one
    reason: the explicit operator rollback route must obtain a FRESH canonical
    PVE snapshot listing before it may arm anything, and
    ``inspect_job_snapshot_state`` is the existing read-only operation that
    produces one. It submits nothing, seals nothing, and creates nothing.
    """

    worker: PackageUpdateWorker
    snapshot_host_control: PackageUpdateSnapshotHostControl


def _build_package_update_runtime(
    authority: InventoryAuthority,
    store: InventoryAuthorityStore,
    config: R0RuntimeConfig,
) -> PackageUpdateRuntime | None:
    """Instantiate the five production host controls and the one worker.

    Every host control here is the EXISTING production SSH implementation for
    its stage, constructed with that stage's own existing bounds -- there is
    no second implementation of any of their protocols, and no generic
    privileged transport shared between them. Each gets its OWN dedicated
    private key, because the key is what selects which forced command the
    connection may run: one key reaching two helpers would merge two
    different privilege boundaries.

    Returns ``None`` when activation is not configured, in which case the
    caller builds no worker at all.
    """

    settings = config.package_update
    if not settings.enabled or settings.host_control is None:
        return None
    boundary = settings.host_control
    shared = {
        "host": boundary.host,
        "port": boundary.port,
        "user": boundary.user,
        "known_hosts_path": boundary.known_hosts_path,
        "max_result_bytes": PACKAGE_UPDATE_MAX_RESULT_BYTES,
    }
    snapshot_host_control = SshPackageUpdateSnapshotHostControl(
        private_key_path=boundary.snapshot_private_key_path,
        timeout_seconds=PACKAGE_UPDATE_SNAPSHOT_TIMEOUT_SECONDS,
        **shared,
    )
    execution_host_control = SshPackageUpdateExecutionHostControl(
        private_key_path=boundary.execution_private_key_path,
        timeout_seconds=PACKAGE_UPDATE_EXECUTION_TIMEOUT_SECONDS,
        **shared,
    )
    mutation_host_control = SshPackageUpdateMutationHostControl(
        private_key_path=boundary.mutation_private_key_path,
        timeout_seconds=PACKAGE_UPDATE_MUTATION_TIMEOUT_SECONDS,
        submission_timeout_seconds=(
            PACKAGE_UPDATE_MUTATION_SUBMISSION_TIMEOUT_SECONDS
        ),
        **shared,
    )
    rollback_host_control = SshPackageUpdateRollbackHostControl(
        private_key_path=boundary.rollback_private_key_path,
        submission_timeout_seconds=(
            PACKAGE_UPDATE_ROLLBACK_SUBMISSION_TIMEOUT_SECONDS
        ),
        inspection_timeout_seconds=(
            PACKAGE_UPDATE_ROLLBACK_INSPECTION_TIMEOUT_SECONDS
        ),
        **shared,
    )
    health_host_control = SshPackageUpdateHealthHostControl(
        private_key_path=boundary.health_private_key_path,
        timeout_seconds=PACKAGE_UPDATE_HEALTH_TIMEOUT_SECONDS,
        **shared,
    )
    return PackageUpdateRuntime(
        worker=PackageUpdateWorker(
            authority,
            store,
            snapshot=PackageUpdateSnapshotOrchestrator(
                authority, snapshot_host_control
            ),
            execution_host_control=execution_host_control,
            mutation=PackageUpdateMutationOrchestrator(
                authority, mutation_host_control
            ),
            rollback=PackageUpdateRollbackOrchestrator(
                authority, rollback_host_control
            ),
            health=PackageUpdateHealthOrchestrator(authority, health_host_control),
        ),
        snapshot_host_control=snapshot_host_control,
    )


def create_read_only_app(
    config: R0RuntimeConfig,
    *,
    start_scheduler: bool = True,
    now: Callable[[], datetime] | None = None,
    package_update_runtime_factory: Callable[
        [InventoryAuthority, InventoryAuthorityStore], PackageUpdateRuntime | None
    ]
    | None = None,
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

    ``package_update_runtime_factory`` is the same kind of seam for the
    production update composition. It receives this app's OWN authority and
    store -- the same two objects :func:`_build_package_update_runtime`
    receives -- so a hermetic test can drive the REAL routes, the real
    authority, and the real worker against fake host controls instead of
    against a Proxmox host. Production callers never pass it, and passing it
    changes nothing about how the routes behave: only which typed host
    boundary the existing stages talk to.
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
    # Production update activation. Built only when the operator configured
    # it: with `package_update.enabled` false there is no update host
    # control, no worker, and no thread -- exactly the pre-activation shape.
    package_update = (
        package_update_runtime_factory(authority, store)
        if package_update_runtime_factory is not None
        else _build_package_update_runtime(authority, store, config)
    )
    package_update_worker = None if package_update is None else package_update.worker
    if package_update_worker is not None and start_scheduler:
        # The worker's first cycle is a RECOVERY cycle. `authority.
        # recover_interrupted_package_update_jobs()` above has already
        # terminalized every provably pre-mutation job, so whatever still
        # owns the global slot is one of the durable uncertain states the
        # stages were built to re-observe: it is re-observed, never replayed
        # and never assumed. Startup can mark nothing SUCCEEDED and can
        # resubmit nothing.
        package_update_worker.start()
    publication = InventoryPublication(store, authority)

    @asynccontextmanager
    async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            # Stop scheduling NEW work first, then let the current cycle
            # finish inside its own stage's existing bounded timeout. A
            # submitted package mutation or PVE rollback is never killed and
            # never "undone" in backend authority to make shutdown tidy: the
            # host operation journal and the durable checkpoints stay the
            # truth, and the next start re-observes them.
            if package_update_worker is not None:
                package_update_worker.stop()
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
    app.state.package_update_worker = package_update_worker
    app.state.package_update = package_update
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
    # Explicit operator update controls.
    #
    # This is the ONLY production path to a real workload package mutation,
    # and every route in it requires an authenticated operator to have
    # invoked it deliberately. Nothing here runs on a timer, from a scan
    # callback, from an approval write, or from a Home Assistant poll.
    # ------------------------------------------------------------------

    _PACKAGE_UPDATE_ROUTE = f"{API_PREFIX}/resources/{{resource_id}}/package-update"

    def _require_activated() -> PackageUpdateRuntime:
        if package_update is None:
            raise _package_update_error(
                503,
                "package_update_not_activated",
                "operator-triggered package updates are not activated on this "
                "installation",
            )
        return package_update

    def _resource_job_or_error(resource_id: str) -> PackageUpdateJob:
        job = store.latest_package_update_job_for_resource(resource_id)
        if job is None:
            raise _package_update_error(
                404,
                "no_package_update_job",
                "this resource has no package update job",
            )
        return job

    def _active_job_for_resource_or_error(resource_id: str) -> PackageUpdateJob:
        """Resolve the ONE applicable ACTIVE job for this resource.

        Read from the durable global single-flight slot, not from "the latest
        job that happens to look active": at most one job can be active at a
        time, so this either names that job -- and only when it belongs to the
        resource the operator selected -- or refuses.
        """

        job = store.active_package_update_job()
        if job is None or job.resource_id != resource_id:
            raise _package_update_error(
                409,
                "no_active_package_update_job",
                "this resource has no active package update job",
            )
        return job

    @app.post(
        _PACKAGE_UPDATE_ROUTE, dependencies=[Depends(_require_bearer_token)]
    )
    def start_package_update(
        body: PackageUpdateStartRequest,
        resource_id: Annotated[str, ApiPath(pattern=_CANONICAL_UUID_PATTERN)],
    ) -> JSONResponse:
        """Explicitly start the currently approved update for one resource.

        The durable acknowledgement point is exact and is the whole contract
        of this route: by the time it returns 202, a durable package-update
        job representing THIS operator request exists in the authority
        database, owning the one global destructive slot. The worker wake
        that follows is a hint and nothing more -- a crash immediately after
        the response can leave the job un-continued, and existing startup
        recovery will then truthfully terminalize a still-pre-mutation job as
        interrupted. It can never make Hubinet claim the update succeeded.

        ``request_id`` keeps its existing UUID idempotency semantics
        unchanged: replaying the same id for the same resource and approval
        returns the same job rather than issuing a second one, and reusing it
        for a different resource or approval is a conflict.
        """

        runtime = _require_activated()
        approval = store.package_plan_approval(resource_id)
        if approval is None:
            raise _package_update_error(
                409,
                "no_current_approval",
                "package update requires a current package plan approval",
            )
        try:
            job = authority.issue_package_update_job(
                resource_id, approval.approval_id, body.request_id
            )
        except AuthorityNotFound as exc:
            raise _package_update_error(
                404, "resource_not_found", str(exc)
            ) from exc
        except PackageUpdateIssuanceRefused as exc:
            # The authority already decided AND named the refusal; this
            # renders it. Two members of that set are genuinely retryable --
            # a package scan that is still running, and a Hubinet product
            # update holding the maintenance fence -- and both say "ask again
            # shortly" rather than "your plan is wrong", so they get 503.
            status = 503 if exc.reason in _RETRYABLE_ISSUANCE_REFUSALS else 409
            raise _package_update_error(status, exc.reason, str(exc)) from exc
        except AuthorityConflict as exc:
            raise _package_update_error(
                409, "package_update_refused", str(exc)
            ) from exc
        except ValueError as exc:
            raise _package_update_error(
                422, "invalid_request", str(exc)
            ) from exc
        runtime.worker.wake()
        return JSONResponse(status_code=202, content=_package_update_job_body(job))

    @app.get(
        _PACKAGE_UPDATE_ROUTE, dependencies=[Depends(_require_bearer_token)]
    )
    def read_package_update(
        resource_id: Annotated[str, ApiPath(pattern=_CANONICAL_UUID_PATTERN)],
        events: Annotated[int, Query(ge=0, le=200)] = 20,
    ) -> dict[str, Any]:
        """Read this resource's current/latest job and a bounded event tail.

        Deliberately available whether or not execution is activated: an
        installation that has switched activation off may still own a durable
        job, and reporting nothing about it would be reporting a false
        absence. This route reads authority state and calls no host control.
        """

        job = _resource_job_or_error(resource_id)
        body = _package_update_job_body(job)
        body["events"] = (
            []
            if events == 0
            else [
                _package_update_event_body(event)
                for event in store.list_package_update_job_events(
                    job.job_id, limit=events
                )
            ]
        )
        return body

    @app.get(
        f"{API_PREFIX}/package-update/active",
        dependencies=[Depends(_require_bearer_token)],
    )
    def read_active_package_update() -> dict[str, Any]:
        """Report whether ANY package-update job owns the global slot.

        The product updater's fail-closed witness: replacing the backend or
        its privileged helpers while a job owns a snapshot, mutation, or
        rollback journal can create protocol-version ambiguity, so the
        updater asks this authenticated read-only question and refuses before
        it touches a file. Also available before activation, for the same
        reason the per-resource readback is.
        """

        job = store.active_package_update_job()
        return {
            "active": job is not None,
            "job": None if job is None else _package_update_job_body(job),
        }

    @app.post(
        f"{API_PREFIX}/package-update/maintenance-fence",
        dependencies=[Depends(_require_bearer_token)],
    )
    def acquire_product_update_maintenance_fence(
        body: ProductUpdateFenceRequest,
    ) -> dict[str, Any]:
        """Make a Hubinet PRODUCT update and a WORKLOAD update exclusive.

        This is the product updater's own control, not an operator one, and
        it is the load-bearing half of that exclusion. Asking "is a job
        active?" and then proceeding is a check-then-act race: between the
        answer and the updater's first mutation an operator can legitimately
        start an update, and a second, later check only moves the window
        rather than closing it.

        So acquisition and workload issuance take the SAME
        ``BEGIN IMMEDIATE`` writer lock, and the fence is made durable inside
        that critical section. Exactly one of them wins: either a job is
        already ACTIVE and this refuses before the updater has mutated
        anything, or the fence exists before any issuing transaction can
        begin and every subsequent ``start_update`` refuses.

        Deliberately available whether or not execution is activated: an
        installation with the lifecycle switched off can still be
        product-updated, and fencing it costs nothing. There is no release
        route -- the updater removes the fence file directly at a terminal
        point, which needs no atomicity and works even when a rolled-back
        pre-activation backend has no such route at all.
        """

        try:
            fence = authority.acquire_product_update_maintenance_fence(
                body.holder
            )
        except AuthorityConflict as exc:
            raise _package_update_error(
                409, "product_update_fence_unavailable", str(exc)
            ) from exc
        except ProductUpdateFenceError as exc:
            raise _package_update_error(
                503, "product_update_fence_unwritable", str(exc)
            ) from exc
        return {"holder": fence.holder, "acquired_at": fence.acquired_at}

    @app.get(
        f"{API_PREFIX}/package-update/maintenance-fence",
        dependencies=[Depends(_require_bearer_token)],
    )
    def read_product_update_maintenance_fence() -> dict[str, Any]:
        """Report whether a product update currently holds the fence."""

        fence = authority.product_update_maintenance_fence()
        return {
            "held": fence is not None,
            "holder": None if fence is None else fence.holder,
            "acquired_at": None if fence is None else fence.acquired_at,
        }

    @app.post(
        f"{_PACKAGE_UPDATE_ROUTE}/resume",
        dependencies=[Depends(_require_bearer_token)],
    )
    def resume_package_update(
        resource_id: Annotated[str, ApiPath(pattern=_CANONICAL_UUID_PATTERN)],
        body: PackageUpdateResumeRequest | None = None,
    ) -> JSONResponse:
        """Ask the worker to re-enter an existing recoverable ACTIVE job.

        This is production liveness for the states PR #73 deliberately gave
        no retry policy: a health evaluation that could not reach a verdict
        leaves the job ACTIVE at ``health_started`` and the worker idle, and
        an operator asks again here rather than a timer doing it.

        It does NOT mean "submit the destructive command again". It wakes the
        one worker, which re-reads the durable checkpoint and invokes only
        the existing safe continuation semantics for whatever state that
        turns out to be: read-only health may simply be evaluated again, an
        armed mutation is re-observed and never resubmitted, an uncertain
        snapshot follows its own journal/recovery rules, and an armed
        rollback reattaches to the operation it already started. No stage,
        checkpoint, operation, or target comes from the caller.
        """

        del body  # deliberately empty; see PackageUpdateResumeRequest
        runtime = _require_activated()
        job = _active_job_for_resource_or_error(resource_id)
        runtime.worker.wake()
        return JSONResponse(
            status_code=202, content=_package_update_job_body(job)
        )

    @app.post(
        f"{_PACKAGE_UPDATE_ROUTE}/rollback",
        dependencies=[Depends(_require_bearer_token)],
    )
    def rollback_package_update(
        resource_id: Annotated[str, ApiPath(pattern=_CANONICAL_UUID_PATTERN)],
        body: PackageUpdateRollbackRequestBody | None = None,
    ) -> JSONResponse:
        """Explicitly roll one resource back to its own job's snapshot.

        The crash rule this route exists to satisfy: an accepted rollback is
        DURABLE before it is acknowledged. The order is exact --

        1. resolve the one applicable ACTIVE job (never a caller-named one);
        2. obtain a FRESH canonical PVE snapshot listing through the existing
           read-only ``inspect_job_snapshot_state`` operation;
        3. ``arm_package_update_rollback`` -- which re-proves the four legal
           entry points, the exact resource/locator context, and exactly one
           complete snapshot carrying THIS job's ownership metadata, then
           commits ``rollback_may_have_started``;
        4. only now acknowledge the operator;
        5. wake the worker to execute it.

        So a crash after step 3 leaves a durable state startup recovery knows
        means "a rollback was requested and may already have started", and a
        crash before it leaves a job that was never told to roll back. There
        is no in-memory queue, no second command journal, and no
        acknowledgement based on a worker wakeup.

        A pre-mutation job never gains rollback authority merely because a
        snapshot exists, a successful job is refused as terminal, and a
        second rollback request re-derives and re-proves the same operation
        identity rather than submitting a second destructive rollback --
        every one of those decisions belongs to the arming transaction and is
        made there.
        """

        del body  # deliberately empty; see PackageUpdateRollbackRequestBody
        runtime = _require_activated()
        job = _active_job_for_resource_or_error(resource_id)
        try:
            identity = authority.package_update_snapshot_identity(job.job_id)
            ownership = authority.package_update_snapshot_ownership(job.job_id)
        except AuthorityConflict as exc:
            raise _package_update_error(
                409, "rollback_not_available", str(exc)
            ) from exc
        try:
            observation = runtime.snapshot_host_control.inspect_job_snapshot_state(
                snapshot_operation_id=identity.snapshot_operation_id,
                snapshot_name=identity.snapshot_name,
                vmid=job.expected_vmid,
                expected_node=job.expected_node_name,
                ownership=ownership,
            )
        except Exception as exc:  # noqa: BLE001 - classify, never leak detail
            _LOGGER.error(
                "package update rollback observation failed for job %s: %s",
                job.job_id,
                type(exc).__name__,
            )
            raise _package_update_error(
                503,
                "snapshot_observation_unavailable",
                "could not obtain a fresh canonical PVE snapshot listing",
            ) from exc
        if observation.snapshots is None:
            raise _package_update_error(
                503,
                "snapshot_observation_unavailable",
                "the host could not produce a canonical PVE snapshot listing",
            )
        try:
            armed = authority.arm_package_update_rollback(
                job.job_id, observation.snapshots
            )
        except AuthorityNotFound as exc:
            raise _package_update_error(
                404, "no_package_update_job", str(exc)
            ) from exc
        except AuthorityConflict as exc:
            raise _package_update_error(
                409, "rollback_refused", str(exc)
            ) from exc
        runtime.worker.wake()
        return JSONResponse(
            status_code=202, content=_package_update_job_body(armed)
        )

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
        except HealthContractRevisionConflict as exc:
            raise _health_contract_error(409, "revision_conflict", str(exc)) from exc
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
        except HealthContractRevisionConflict as exc:
            raise _health_contract_error(409, "revision_conflict", str(exc)) from exc
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
