"""R0 discovery runtime: scheduler, orchestrator, and restart recovery.

Implements exactly §§11-16 of
``docs/architecture/0.5-r0-read-only-runtime-activation.md`` (WAVE R0-B,
Family 3). This module is a thin orchestrator over the existing, already-
tested ``app.inventory`` authority/provider/discovery methods -- it never
touches ``discovery_runs``/``inventory_sources``/``resource_incarnations``
directly, and it introduces no new durable authority behavior.

It has a narrow, closed call surface by construction: the only
``InventoryAuthority`` methods this module ever calls are
``issue_discovery_run``, ``mark_discovery_run_running``,
``finalize_successful_discovery_run``, ``finalize_failed_discovery_run``,
and (startup recovery only) ``abandon_discovery_run``. It never calls any
attestation-shaped, candidate-activation-shaped, or confirmed-removal-
shaped authority method -- there is no code path here that could, by
construction, not merely by convention.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
import logging
import threading
from typing import Literal

from app.inventory import (
    AuthorityConflict,
    BaselineCompleteness,
    BaselineMode,
    DetailReadStatus,
    DiscoveredNode,
    DiscoveredResource,
    DiscoveryRun,
    DiscoveryRunCompletionEvidence,
    DiscoveryRunLifecycle,
    InventoryAuthority,
    InventoryAuthorityStore,
    NormalizedDiscoverySnapshot,
    PROVIDER_CONTRACT_VERSION,
    ProviderContractError,
    ProviderFailureKind,
    ProxmoxProviderV1,
    SourceAvailability,
    classify_provider_failure,
)
from app.inventory.provider import BoundaryBaselineResult
from app.inventory_pve_transport import ProxmoxHttpTransport, PveTransportError
from app.inventory_runtime_config import R0RuntimeConfig

_LOGGER = logging.getLogger(__name__)

# Baseline mode is only semantically meaningful for a COMPLETE snapshot
# (discovery.py's own _validate only inspects it in the COMPLETE branch);
# for a total transport/schema failure we never established a real mode at
# all, so this fixed placeholder is recorded purely as inert evidence.
_UNESTABLISHED_BASELINE_MODE = BaselineMode.CLUSTER

# The scheduler's own polling interval should be comfortably shorter than
# the source's configured freshness_duration_seconds so successful
# discovery ordinarily keeps the source fresh (an operational tuning
# relationship, not a new invariant -- the authority fails closed to stale
# on its own regardless of what the scheduler does). Bounded above and
# below so a pathological config value can't produce a busy-loop or an
# effectively-disabled scheduler.
_MIN_INTERVAL_SECONDS = 30
_MAX_INTERVAL_SECONDS = 3600


DiscoveryCycleStatus = Literal["success", "failed", "conflict", "skipped", "error"]


@dataclass(frozen=True, slots=True)
class DiscoveryCycleOutcome:
    """Result of one attempted discovery cycle for one source."""

    status: DiscoveryCycleStatus
    detail: str


def _default_now() -> datetime:
    return datetime.now(UTC)


def _classify_exception(
    exc: Exception,
) -> tuple[BaselineCompleteness, ProviderFailureKind, SourceAvailability]:
    if isinstance(exc, PveTransportError):
        kind = exc.kind
    else:
        # A bare ProviderContractError raised directly by provider.py logic
        # (malformed envelope, ambiguous cluster status, disagreeing
        # expected mode, unsupported PVE release, ...) represents data the
        # provider could not accept as schema-valid, not a network-layer
        # transport failure.
        kind = ProviderFailureKind.SCHEMA
    completeness = classify_provider_failure(kind)
    availability = (
        SourceAvailability.UNAVAILABLE
        if kind is ProviderFailureKind.TRANSPORT
        else SourceAvailability.AVAILABLE
    )
    return completeness, kind, availability


def _failure_snapshot(
    run: DiscoveryRun,
    *,
    completeness: BaselineCompleteness,
    availability: SourceAvailability,
    failed_scope: str,
    observed_at: str,
) -> NormalizedDiscoverySnapshot:
    """Build a minimal valid snapshot for a run that never produced provider
    evidence at all (constructed directly, not via ``from_provider_baseline``,
    since there is no ``BoundaryBaselineResult`` to normalize)."""

    return NormalizedDiscoverySnapshot(
        run_id=run.run_id,
        discovery_run_sequence=run.discovery_run_sequence,
        inventory_source_id=run.inventory_source_id,
        expected_source_config_revision=run.expected_source_config_revision,
        endpoint_id=run.expected_endpoint_id,
        canonical_transport_locator=run.expected_canonical_transport_locator,
        canonicalization_contract_version=run.expected_canonicalization_contract_version,
        expected_transport_trust_revision=run.expected_transport_trust_revision,
        provider_contract_version=run.provider_contract_version,
        observed_at=observed_at,
        source_facts={},
        source_availability=availability,
        baseline_completeness=completeness,
        baseline_mode=_UNESTABLISHED_BASELINE_MODE,
        acl_topology_hash_before=None,
        acl_topology_hash_after=None,
        permission_snapshot_hash_before=None,
        permission_snapshot_hash_after=None,
        permission_coverage_complete=False,
        boundary_consistent=False,
        covered_nodes=(),
        failed_baseline_scopes=(failed_scope,),
        detail_summary={
            "ok_count": 0,
            "temporarily_unavailable_count": 0,
            "error_count": 0,
        },
        failed_detail_scopes=(),
        nodes=(),
        resources=(),
        expected_source_attestation_epoch=run.expected_source_attestation_epoch,
        provider_node_scope=None,
        provider_guest_locators=None,
    )


def _discovered_nodes(
    result: BoundaryBaselineResult, *, observed_at: str
) -> tuple[DiscoveredNode, ...]:
    nodes = []
    for row in result.node_rows:
        status = str(row.get("status", "online"))
        nodes.append(
            DiscoveredNode(
                external_node_name=str(row["node"]),
                status=status,
                available=status == "online",
                observed_at=observed_at,
                facts={},
            )
        )
    return tuple(nodes)


def _discovered_resources(
    result: BoundaryBaselineResult,
    *,
    inventory_source_id: str,
    observed_at: str,
) -> tuple[DiscoveredResource, ...]:
    # Per-guest detail reads (the optional
    # "/nodes/{node}/{type}/{vmid}/config" endpoint) are not implemented by
    # this R0-B package -- every discovered guest is recorded as detail_
    # status=OK from the baseline read alone, matching the accepted MVP
    # scope (§9's ENDPOINT_ACL_MATRIX marks that endpoint
    # baseline_prerequisite=False).
    resources = []
    for row in result.guest_rows:
        vmid = int(row["vmid"])
        resource_type = str(row["type"])
        node = str(row["node"])
        resources.append(
            DiscoveredResource(
                inventory_source_id=inventory_source_id,
                vmid=vmid,
                resource_type=resource_type,
                name=str(row.get("name") or f"{resource_type}-{vmid}"),
                status=str(row.get("status", "unknown")),
                current_node_name=node,
                observed_at=observed_at,
                detail_status=DetailReadStatus.OK,
                facts={},
            )
        )
    return tuple(resources)


def _normalize_result(
    result: BoundaryBaselineResult,
    run: DiscoveryRun,
    *,
    observed_at: str,
) -> NormalizedDiscoverySnapshot:
    if result.completeness is BaselineCompleteness.COMPLETE:
        nodes = _discovered_nodes(result, observed_at=observed_at)
        resources = _discovered_resources(
            result, inventory_source_id=run.inventory_source_id, observed_at=observed_at
        )
    else:
        nodes = ()
        resources = ()
    detail_summary = {
        "ok_count": len(resources),
        "temporarily_unavailable_count": 0,
        "error_count": 0,
    }
    release_major, release_minor = result.release
    return NormalizedDiscoverySnapshot.from_provider_baseline(
        result,
        run_id=run.run_id,
        discovery_run_sequence=run.discovery_run_sequence,
        inventory_source_id=run.inventory_source_id,
        expected_source_config_revision=run.expected_source_config_revision,
        endpoint_id=run.expected_endpoint_id,
        canonical_transport_locator=run.expected_canonical_transport_locator,
        canonicalization_contract_version=run.expected_canonicalization_contract_version,
        expected_transport_trust_revision=run.expected_transport_trust_revision,
        provider_contract_version=run.provider_contract_version,
        observed_at=observed_at,
        source_facts={"release": f"{release_major}.{release_minor}"},
        source_availability=SourceAvailability.AVAILABLE,
        failed_baseline_scopes=(),
        detail_summary=detail_summary,
        failed_detail_scopes=(),
        nodes=nodes,
        resources=resources,
        expected_source_attestation_epoch=run.expected_source_attestation_epoch,
    )


def _build_transport(
    run: DiscoveryRun, config: R0RuntimeConfig
) -> ProxmoxHttpTransport:
    # Constructed fresh per discovery run from the run's own captured
    # expected_canonical_transport_locator (§9) -- never a cached client
    # pointed at whatever the current config happens to say.
    verify: bool | str = (
        config.source.tls.ca_bundle_path
        if config.source.tls.ca_bundle_path is not None
        else config.source.tls.verify
    )
    return ProxmoxHttpTransport(
        canonical_transport_locator=run.expected_canonical_transport_locator,
        pve_api_token=config.pve_api_token,
        verify=verify,
    )


def run_discovery_cycle(
    authority: InventoryAuthority,
    inventory_source_id: str,
    config: R0RuntimeConfig,
    *,
    now: Callable[[], datetime] = _default_now,
) -> DiscoveryCycleOutcome:
    """Perform exactly one issue -> transport I/O -> finalize cycle (§11).

    Never bypasses the durable authority CAS: issuance, running transition,
    and finalization are exactly the four ``InventoryAuthority`` calls
    listed in this module's docstring, in that order, with no direct table
    access at any point.

    Post-issuance safety invariant (§12): once ``issue_discovery_run`` has
    successfully claimed ownership, this function never lets an exception
    (classifiable or not) leave ``active_discovery_run_id`` durably
    stranded. Classifiable transport/provider failures finalize as a
    failed run with real evidence, exactly as before; anything else --
    an authority ownership/context conflict, or a genuinely unexpected
    error this function cannot honestly classify without fabricating
    provider evidence -- falls through to a single, uniform safety net
    that fences the run with the existing, already-tested
    ``abandon_discovery_run`` mechanism (§13) rather than leaving it
    active until the next process restart.
    """

    try:
        run = authority.issue_discovery_run(inventory_source_id, PROVIDER_CONTRACT_VERSION)
    except AuthorityConflict as exc:
        # No run was issued at all -- nothing to strand. Per-source
        # single-flight is already durably enforced by the authority's
        # own CAS (§1 item 11); a second concurrent issuance attempt (this
        # scheduler racing itself, or a future manual trigger) must
        # cleanly lose, exactly like two scheduler ticks would -- log and
        # skip, never crash the process.
        _LOGGER.info("R0 discovery run issuance skipped: %s", exc)
        return DiscoveryCycleOutcome("conflict", str(exc))

    observed_at = now().isoformat()

    try:
        return _execute_issued_run(authority, inventory_source_id, run, config, observed_at)
    except Exception as exc:  # noqa: BLE001 -- last-resort post-issuance safety net
        is_conflict = isinstance(exc, AuthorityConflict)
        if is_conflict:
            _LOGGER.warning(
                "R0 discovery run %s ownership/context conflict: %s", run.run_id, exc
            )
        else:
            _LOGGER.exception(
                "R0 discovery run %s hit an unexpected error; fencing the run", run.run_id
            )
        try:
            authority.abandon_discovery_run(
                inventory_source_id,
                run.run_id,
                reason=("post_issuance_failure: " + str(exc))[:500],
            )
        except AuthorityConflict:
            # Already released/terminalized -- e.g. finalize_successful_
            # discovery_run's own completion-context-changed path already
            # audits an "invalid" completion and releases ownership before
            # raising (§11); a mark_discovery_run_running conflict that
            # never touched ownership is the other case this call
            # actually fences. Either way, active_discovery_run_id is
            # guaranteed clear by the time we return.
            pass
        return DiscoveryCycleOutcome("conflict" if is_conflict else "error", str(exc))


def _execute_issued_run(
    authority: InventoryAuthority,
    inventory_source_id: str,
    run: DiscoveryRun,
    config: R0RuntimeConfig,
    observed_at: str,
) -> DiscoveryCycleOutcome:
    """Run the classifiable portion of one issued run's transport I/O and
    finalization. Any exception raised here propagates to the caller's
    uniform post-issuance safety net."""

    try:
        authority.mark_discovery_run_running(inventory_source_id, run.run_id)
        with _build_transport(run, config) as transport:
            result = ProxmoxProviderV1.collect_boundary_baseline(transport)
        snapshot = _normalize_result(result, run, observed_at=observed_at)
    except (PveTransportError, ProviderContractError) as exc:
        completeness, _kind, availability = _classify_exception(exc)
        failure_snapshot = _failure_snapshot(
            run,
            completeness=completeness,
            availability=availability,
            failed_scope="transport_or_provider_boundary",
            observed_at=observed_at,
        )
        authority.finalize_failed_discovery_run(
            inventory_source_id,
            run.run_id,
            completion_evidence=DiscoveryRunCompletionEvidence.from_snapshot(failure_snapshot),
            reason=str(exc)[:500],
        )
        _LOGGER.warning("R0 discovery run %s failed: %s", run.run_id, exc)
        return DiscoveryCycleOutcome("failed", str(exc))

    if snapshot.baseline_completeness is BaselineCompleteness.COMPLETE:
        authority.finalize_successful_discovery_run(inventory_source_id, run.run_id, snapshot)
        return DiscoveryCycleOutcome("success", f"run {run.run_id} reconciled")

    authority.finalize_failed_discovery_run(
        inventory_source_id,
        run.run_id,
        completion_evidence=DiscoveryRunCompletionEvidence.from_snapshot(snapshot),
        reason=f"baseline completeness {snapshot.baseline_completeness.value}",
    )
    return DiscoveryCycleOutcome(
        "failed", f"baseline completeness {snapshot.baseline_completeness.value}"
    )


def perform_startup_recovery(
    authority: InventoryAuthority, store: InventoryAuthorityStore
) -> tuple[str, ...]:
    """Clear any stale active-run ownership left by a prior crash (§13).

    Must run before any configuration-drift comparison (Family 1's
    ``bootstrap_or_reconcile_source``) and before the first scheduled
    discovery issuance (§8 step 4 / §42). Returns the run ids abandoned,
    for logging/test observability only.

    Uses only the existing, already-tested ``abandon_discovery_run``
    method -- no raw SQL, no invented recovery mechanism, no fabricated
    observation/completion/health, and no interaction with confirmed
    removal or attestation of any kind.
    """

    abandoned: list[str] = []
    for state in store.list_source_states():
        source_id = state.source.inventory_source_id
        active_run_id = state.source.active_discovery_run_id
        if active_run_id is None:
            continue
        run = store.discovery_run(active_run_id)
        if run.lifecycle in (DiscoveryRunLifecycle.ISSUED, DiscoveryRunLifecycle.RUNNING):
            authority.abandon_discovery_run(
                source_id, active_run_id, reason="r0_startup_recovery"
            )
            abandoned.append(active_run_id)
    return tuple(abandoned)


def bootstrap_and_start_r0_runtime(
    authority: InventoryAuthority,
    store: InventoryAuthorityStore,
    config: R0RuntimeConfig,
    *,
    start: bool = True,
    now: Callable[[], datetime] = _default_now,
) -> "R0Scheduler":
    """Exact §8 startup order, in one place (§42): recovery, then config-
    drift comparison, then scheduling. Family 4's composition root calls
    only this function to wire startup together -- it does not reimplement
    or reorder any of these three steps itself."""

    from app.inventory_runtime_config import bootstrap_or_reconcile_source

    perform_startup_recovery(authority, store)
    bootstrap_or_reconcile_source(authority, store, config)
    scheduler = R0Scheduler(authority, store, config, now=now)
    if start:
        scheduler.start()
    return scheduler


def _clamped_interval_seconds(config: R0RuntimeConfig) -> int:
    candidate = config.source.freshness_duration_seconds // 2
    return max(_MIN_INTERVAL_SECONDS, min(_MAX_INTERVAL_SECONDS, candidate or _MIN_INTERVAL_SECONDS))


class R0Scheduler:
    """Single background thread driving §11's exact call chain on a timer.

    The smallest safe scheduler (§12): no framework beyond the standard
    library. Single-source in this R0-B package (§5); a future multi-source
    package would run one independent instance of this per source.
    """

    def __init__(
        self,
        authority: InventoryAuthority,
        store: InventoryAuthorityStore,
        config: R0RuntimeConfig,
        *,
        interval_seconds: int | None = None,
        now: Callable[[], datetime] = _default_now,
    ) -> None:
        self._authority = authority
        self._store = store
        self._config = config
        self._interval_seconds = interval_seconds or _clamped_interval_seconds(config)
        self._now = now
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        # In-process single-flight guard: a performance/clarity nicety
        # only. The actual security boundary is the durable
        # active_discovery_run_id CAS inside issue_discovery_run (§12).
        self._cycle_lock = threading.Lock()

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.is_running:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop, name="hubinet-r0-scheduler", daemon=True
        )
        self._thread.start()

    def stop(self, *, grace_seconds: float = 10.0) -> None:
        """Stop issuing new runs; allow an in-flight run its own grace period.

        Never force-abandons a running discovery run from here (§12/§41) --
        that would race the run's own natural finalize path. If the grace
        period elapses first, the process is expected to exit and the next
        startup's :func:`perform_startup_recovery` picks the run up.
        """

        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=grace_seconds)

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.run_once()
            except Exception:  # noqa: BLE001 - never let the loop die silently
                _LOGGER.exception("R0 scheduler cycle raised an unhandled exception")
            self._stop_event.wait(self._interval_seconds)

    def run_once(self) -> DiscoveryCycleOutcome:
        if not self._cycle_lock.acquire(blocking=False):
            return DiscoveryCycleOutcome("skipped", "previous cycle still in flight")
        try:
            states = self._store.list_source_states()
            if not states:
                return DiscoveryCycleOutcome("skipped", "no configured source")
            source_id = states[0].source.inventory_source_id
            return run_discovery_cycle(
                self._authority, source_id, self._config, now=self._now
            )
        finally:
            self._cycle_lock.release()
