"""Immutable value objects for the Hubinet Ops 0.5 authority core."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from collections.abc import Iterable, Mapping


class EndpointLifecycle(StrEnum):
    ACTIVE = "active"
    CANDIDATE = "candidate"
    INACTIVE = "inactive"
    RETIRED = "retired"


class DiscoveryRunLifecycle(StrEnum):
    ISSUED = "issued"
    RUNNING = "running"
    COMPLETED = "completed"
    ABANDONED = "abandoned"


class PackageScanLifecycle(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"


class PackageScanOutcome(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class PackageUpdateJobStatus(StrEnum):
    ACTIVE = "active"
    SUCCEEDED = "succeeded"
    BLOCKED = "blocked"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"
    INTERRUPTED = "interrupted"
    MANUAL_INTERVENTION = "manual_intervention"


class HostSubmissionState(StrEnum):
    """Durable host journal phase for one snapshot operation."""

    ABSENT = "absent"
    INTENT = "intent"
    SEALED_NOT_SUBMITTED = "sealed_not_submitted"
    SUBMITTED = "submitted"
    TASK_KNOWN = "task_known"
    TERMINAL = "terminal"


class HostMutationState(StrEnum):
    """Durable host journal phase for one job-owned package mutation.

    Deliberately fewer states than :class:`HostSubmissionState`: a package
    mutation has no PVE task identity to capture, so there is nothing
    between "the real package command was launched" and "it reached one
    terminal result". ``SUBMITTED`` is therefore the whole genuinely
    uncertain window and is NEVER resubmitted, and the two terminal phases
    are distinct durable facts rather than one phase plus an outcome field,
    so a reader can never mistake a failure for a success by dropping a
    field it did not understand.
    """

    #: No journal record exists. The ordinary state before the backend has
    #: armed anything -- a read-only prepare deliberately writes nothing --
    #: and also what a backend that died between arming and executing leaves
    #: behind. It is transient pre-submission routing evidence only: never a
    #: release proof, because a helper launched by a backend that then died
    #: may not have taken its host lease yet. Releasing an armed job needs
    #: absence to be durably SEALED first, never merely observed.
    ABSENT = "absent"
    #: An already-armed operation reached the host's submit-capable boundary,
    #: binding the accepted evidence digest, but no package command has been
    #: launched. Also transient routing evidence.
    INTENT = "intent"
    #: The durable no-future-submit fence. The ONLY evidence that may release
    #: a job which is already past its write-ahead mutation checkpoint.
    SEALED_NOT_SUBMITTED = "sealed_not_submitted"
    #: The real package command was durably journaled BEFORE it was launched.
    #: Packages may be changing right now, or may have been left partly
    #: changed by a runner that died. Never resubmitted, never inferred to be
    #: a failure.
    SUBMITTED = "submitted"
    #: The package command ran to completion and exited zero. This is host
    #: evidence about the command, never proof that the approved mutation is
    #: complete -- see the backend's independent completion proof.
    TERMINAL_SUCCESS = "terminal_success"
    #: The package command reached a terminal non-zero, killed, or timed-out
    #: result. Packages may be partly changed.
    TERMINAL_FAILURE = "terminal_failure"


class HostRollbackState(StrEnum):
    """Durable host journal phase for one job-owned same-job rollback.

    Deliberately the same SHAPE as :class:`HostSubmissionState`, and
    deliberately a DIFFERENT type. The shape matches because the verified
    upstream semantics match: like snapshot create, an LXC rollback is
    submitted through an asynchronous PVE endpoint that returns a UPID
    immediately (`fork_worker('vzrollback', ...)`), so there is a real
    ``task_known`` phase between "submission crossed PVE's door" and "the
    operation reached a terminal result".

    The type is separate so a snapshot journal phase can never be fed into a
    rollback decision, or the reverse, merely because the two enums happen to
    spell their members the same way. Rollback is strictly more destructive
    than create -- it force-stops a running container and replaces its config
    -- so conflating the two evidence channels must be impossible by typing,
    not by convention.
    """

    #: No journal record exists. Ordinary pre-submission routing evidence
    #: only: a helper launched by a backend that then died may not have taken
    #: its per-VMID lease yet, so absence is NEVER a release proof.
    ABSENT = "absent"
    #: The request was journaled but no rollback has been submitted to PVE.
    #: Transient routing evidence, exactly like ``ABSENT``.
    INTENT = "intent"
    #: The durable no-future-submit fence, and the ONLY evidence that may
    #: release a job already past its write-ahead rollback checkpoint.
    SEALED_NOT_SUBMITTED = "sealed_not_submitted"
    #: The rollback was durably journaled BEFORE `pvesh create` was invoked.
    #: The container may be stopping, or its volumes and config may be being
    #: replaced right now. NEVER resubmitted, and never inferred to be a
    #: failure.
    SUBMITTED = "submitted"
    #: PVE returned a UPID for this exact rollback. The caller polls that
    #: task through the read-only inspection operation.
    TASK_KNOWN = "task_known"
    #: The operation reached a recorded terminal result.
    TERMINAL = "terminal"


class PackageUpdateCheckpoint(StrEnum):
    ISSUED = "issued"
    PREFLIGHT_PASSED = "preflight_passed"
    SNAPSHOT_MAY_HAVE_STARTED = "snapshot_may_have_started"
    SNAPSHOT_CONFIRMED = "snapshot_confirmed"
    MUTATION_MAY_HAVE_STARTED = "mutation_may_have_started"
    MUTATION_COMPLETED = "mutation_completed"
    HEALTH_STARTED = "health_started"
    HEALTH_COMPLETED = "health_completed"
    ROLLBACK_MAY_HAVE_STARTED = "rollback_may_have_started"
    ROLLBACK_COMPLETED = "rollback_completed"


#: Durable checkpoint order. ``snapshot_may_have_started`` is the write-ahead
#: uncertainty boundary: once a job reaches it, a PVE snapshot mutation may
#: already have been submitted, so nothing may ever conclude that no PVE
#: mutation happened. Used by the schema and by every typed transition; the
#: SQL CHECK/trigger set in ``store.py`` encodes exactly this order.
#:
#: **This order is a monotonic no-regression fence, NOT a chain of implied
#: successes.** The lifecycle branches: a package mutation that failed, was
#: partial, timed out, was killed, or simply could not be proven complete
#: stays at ``mutation_may_have_started`` with ``mutation_completed_at``
#: NULL -- and it is exactly that job which most needs compensation, so it
#: must be able to reach ``rollback_may_have_started`` without anything
#: fabricating a completion it never had. A later rank therefore means only
#: "this job has passed this boundary", never "every earlier milestone
#: succeeded". Schema v14 encodes that distinction: each durable fact is
#: tied to its OWN checkpoint in both directions, and no rank implies
#: another stage's success fact. See ``store.py`` and ``ARCHITECTURE.md``,
#: "Same-job rollback execution".
#:
#: Schema v16 adds ``health_completed`` and keeps exactly that discipline for
#: the health branch too. ``health_completed`` means "this job's frozen health
#: contract was evaluated to a DEFINITIVE verdict", not "the workload is
#: healthy": the verdict itself lives in ``health_outcome``, and a job that
#: reaches this checkpoint with ``health_outcome='failed'`` stays ACTIVE and
#: rollback-capable. Rollback is reachable from ``mutation_may_have_started``,
#: ``mutation_completed``, ``health_started`` and a FAILED
#: ``health_completed`` alike, so no constraint may let ranks 9/10 imply a
#: health fact any more than they may imply ``mutation_completed_at``.
CHECKPOINT_ORDER: tuple[PackageUpdateCheckpoint, ...] = (
    PackageUpdateCheckpoint.ISSUED,
    PackageUpdateCheckpoint.PREFLIGHT_PASSED,
    PackageUpdateCheckpoint.SNAPSHOT_MAY_HAVE_STARTED,
    PackageUpdateCheckpoint.SNAPSHOT_CONFIRMED,
    PackageUpdateCheckpoint.MUTATION_MAY_HAVE_STARTED,
    PackageUpdateCheckpoint.MUTATION_COMPLETED,
    PackageUpdateCheckpoint.HEALTH_STARTED,
    PackageUpdateCheckpoint.HEALTH_COMPLETED,
    PackageUpdateCheckpoint.ROLLBACK_MAY_HAVE_STARTED,
    PackageUpdateCheckpoint.ROLLBACK_COMPLETED,
)

_CHECKPOINT_RANKS = {
    checkpoint: rank for rank, checkpoint in enumerate(CHECKPOINT_ORDER, start=1)
}


def checkpoint_rank(checkpoint: PackageUpdateCheckpoint) -> int:
    """Return the durable ordinal of one package-update checkpoint."""

    try:
        return _CHECKPOINT_RANKS[PackageUpdateCheckpoint(checkpoint)]
    except (KeyError, ValueError) as exc:
        raise AuthorityInvariantError(
            "unknown package update checkpoint"
        ) from exc


class PackageUpdateEventLevel(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class PackageUpdateEventType(StrEnum):
    JOB_ISSUED = "job_issued"
    RESTART_INTERRUPTED = "restart_interrupted"
    PREFLIGHT_PASSED = "preflight_passed"
    SNAPSHOT_INTENT_RECORDED = "snapshot_intent_recorded"
    SNAPSHOT_TASK_OBSERVED = "snapshot_task_observed"
    SNAPSHOT_CONFIRMED = "snapshot_confirmed"
    SNAPSHOT_OUTCOME_UNCERTAIN = "snapshot_outcome_uncertain"
    SNAPSHOT_FAILED = "snapshot_failed"
    SNAPSHOT_BLOCKED_BEFORE_SUBMISSION = "snapshot_blocked_before_submission"
    SNAPSHOT_RETAINED_AUTHORITY_STALE = "snapshot_retained_authority_stale"
    EXECUTION_PLAN_VERIFIED = "execution_plan_verified"
    EXECUTION_PLAN_MISMATCH = "execution_plan_mismatch"
    EXECUTION_AUTHORITY_STALE_RELEASED = "execution_authority_stale_released"
    MUTATION_MAY_HAVE_STARTED = "mutation_may_have_started"
    MUTATION_SUBMITTED = "mutation_submitted"
    MUTATION_TERMINAL_FAILURE = "mutation_terminal_failure"
    MUTATION_OUTCOME_UNCERTAIN = "mutation_outcome_uncertain"
    MUTATION_COMPLETED = "mutation_completed"
    MUTATION_BLOCKED_BEFORE_SUBMISSION = "mutation_blocked_before_submission"
    HEALTH_STARTED = "health_started"
    HEALTH_PASSED = "health_passed"
    POST_UPDATE_SCAN_REQUESTED = "post_update_scan_requested"
    HEALTH_FAILED = "health_failed"
    HEALTH_OUTCOME_UNKNOWN = "health_outcome_unknown"
    ROLLBACK_MAY_HAVE_STARTED = "rollback_may_have_started"
    ROLLBACK_SUBMITTED = "rollback_submitted"
    ROLLBACK_TASK_OBSERVED = "rollback_task_observed"
    ROLLBACK_OUTCOME_UNCERTAIN = "rollback_outcome_uncertain"
    ROLLBACK_TERMINAL_FAILURE = "rollback_terminal_failure"
    ROLLBACK_COMPLETED = "rollback_completed"
    ROLLBACK_BLOCKED_BEFORE_SUBMISSION = "rollback_blocked_before_submission"


class PackageUpdateExecutionOutcome(StrEnum):
    """Result of comparing a fresh execution-time plan against a frozen job.

    This is the in-memory/typed result of one equality decision. It is never
    itself persisted as a durable "safe to mutate" flag: a ``MATCHED``
    outcome leaves the job's checkpoint at ``snapshot_confirmed`` untouched,
    while ``MISMATCHED`` and ``AUTHORITY_STALE`` both terminalize the job as
    ``blocked`` (retained snapshot, released global slot, no rollback
    authority) for their own distinct, truthful reasons. See
    ``ARCHITECTURE.md``, "Execution-time plan equality".
    """

    MATCHED = "matched"
    MISMATCHED = "mismatched"
    AUTHORITY_STALE = "authority_stale"


class PackageMutationArmOutcome(StrEnum):
    """Which invocation actually committed the write-ahead arming facts.

    ``MATCHED`` alone is not enough for a caller that wants to submit. Two
    invocations can both prepare fresh evidence, both find the job ACTIVE at
    ``snapshot_confirmed``, and both derive the same deterministic
    ``mutation_operation_id``; exactly one of them commits its evidence
    digest, and the other must not be able to submit merely because the
    identity it derived happens to match. So the arming transition reports
    which one it was.
    """

    #: THIS invocation atomically committed the checkpoint together with its
    #: own accepted evidence digest. Only it may enter the submit-capable
    #: path, and only with that same digest.
    ARMED_NOW = "armed_now"
    #: The job was already armed by some earlier or concurrent invocation.
    #: This caller is recovery-only and may never submit.
    ALREADY_ARMED = "already_armed"


class PackageScanFailure(StrEnum):
    GUEST_UNAVAILABLE = "guest_unavailable"
    UNSUPPORTED_RESOURCE_TYPE = "unsupported_resource_type"
    UNSUPPORTED_OS = "unsupported_os"
    PACKAGE_MANAGER_BUSY = "package_manager_busy"
    METADATA_REFRESH_FAILED = "metadata_refresh_failed"
    SIMULATION_FAILED = "simulation_failed"
    TIMEOUT = "timeout"
    MALFORMED_PLAN = "malformed_plan"
    STALE_TARGET = "stale_target"
    EXECUTION_FAILED = "execution_failed"


class HealthProbeKind(StrEnum):
    """The exact typed workload-health probes an operator may declare.

    Deliberately closed and deliberately small. Each member names one fixed
    argv operation a future executor can perform truthfully against a
    Debian/Ubuntu LXC guest; there is no member for "run this command", and
    there never will be. `PRODUCT.md` records why this list is what it is.
    """

    #: The explicitly named systemd unit must be active.
    SYSTEMD_UNIT_ACTIVE = "systemd_unit_active"
    #: The explicitly named Docker container must be running.
    DOCKER_CONTAINER_RUNNING = "docker_container_running"
    #: The explicitly named Docker container must be running AND report
    #: Docker HEALTHCHECK status healthy. A container with no HEALTHCHECK
    #: therefore cannot satisfy this probe -- that is the point of choosing
    #: it over ``docker_container_running``.
    DOCKER_CONTAINER_HEALTHY = "docker_container_healthy"


class HealthProbeOutcome(StrEnum):
    """How ONE frozen probe of a job's health contract was resolved.

    Three values, and the difference between the last two is the whole point
    of this stage:

    - ``PASSED`` -- the exact requested object was positively observed in the
      exact state the probe requires.
    - ``FAILED`` -- the exact requested object was positively observed in a
      state that does NOT satisfy the probe. This is a proof that the
      contract is false, not an absence of proof that it is true.
    - ``UNKNOWN`` -- the probe could not be evaluated truthfully: the host
      round trip failed, the command timed out, the output was malformed, the
      Docker daemon could not be reached, or the target could not be resolved
      to one exact object. It is NEVER a pass and never a failure.
    """

    PASSED = "passed"
    FAILED = "failed"
    UNKNOWN = "unknown"


class HealthOutcome(StrEnum):
    """The verdict over one job's COMPLETE frozen probe set.

    A health contract is an ALL-OF: every declared probe must hold. So the
    aggregation is not a vote and not a score:

    - ``PASSED`` requires EVERY frozen probe to be positively proven
      ``PASSED``. Absence of failure is not a pass.
    - ``FAILED`` needs exactly one definitively ``FAILED`` member -- one false
      conjunct proves an ALL-OF false, whatever the other members did, so a
      deterministic failure alongside an unknown is still a failure.
    - ``UNKNOWN`` is the remainder: nothing proved the contract false, and at
      least one required probe could not be evaluated truthfully. It is never
      success, and it is deliberately NOT durable -- see
      ``InventoryAuthority.complete_package_update_health``, which refuses it.
    """

    PASSED = "passed"
    FAILED = "failed"
    UNKNOWN = "unknown"


#: The health outcomes that may ever be written to
#: ``package_update_jobs.health_outcome``. ``UNKNOWN`` is absent on purpose:
#: a durable health completion is a definitive verdict about the frozen
#: contract, and "I could not tell" must stay a retryable non-answer rather
#: than becoming a durable one.
DEFINITIVE_HEALTH_OUTCOMES: tuple[HealthOutcome, ...] = (
    HealthOutcome.PASSED,
    HealthOutcome.FAILED,
)


def aggregate_health_outcome(
    outcomes: Iterable[HealthProbeOutcome],
) -> HealthOutcome:
    """Aggregate one complete frozen probe set into one ALL-OF verdict.

    Pure and total, with no ordering, threshold, majority, quorum, or
    percentage anywhere in it -- deliberately, because a contract that needs
    those is not a contract anyone can read at 3am (`PRODUCT.md`).

    The caller is responsible for having proved that ``outcomes`` is the
    result set for the job's COMPLETE frozen contract; an empty set raises,
    because "zero required things all held" is exactly the false pass the
    product refuses to make.
    """

    materialized = tuple(HealthProbeOutcome(outcome) for outcome in outcomes)
    if not materialized:
        raise AuthorityInvariantError(
            "a health contract verdict requires at least one probe result"
        )
    if any(outcome is HealthProbeOutcome.FAILED for outcome in materialized):
        # One false conjunct is enough. Checked BEFORE unknown on purpose:
        # a deterministic failure beside an unevaluable probe is still a
        # deterministic failure of the whole contract.
        return HealthOutcome.FAILED
    if all(outcome is HealthProbeOutcome.PASSED for outcome in materialized):
        return HealthOutcome.PASSED
    return HealthOutcome.UNKNOWN


class PersistentSourceHealth(StrEnum):
    HEALTHY = "healthy"
    SOURCE_UNAVAILABLE = "source_unavailable"
    DEGRADED = "degraded"
    CONFIGURATION_ERROR = "configuration_error"
    NOT_YET_OBSERVED = "not_yet_observed"


class PersistentSourceFreshness(StrEnum):
    FRESH = "fresh"
    STALE = "stale"
    NOT_YET_OBSERVED = "not_yet_observed"


class PersistentSourceHealthOrigin(StrEnum):
    DISCOVERY_RUN = "discovery_run"
    CONTROLLED_CONTEXT_TRANSITION = "controlled_context_transition"
    TIME_EXPIRY = "time_expiry"
    INITIAL = "initial"


class AuthorityError(RuntimeError):
    """Base class for durable authority failures."""


class AuthorityDatabaseRejected(AuthorityError):
    """The database is not a recognized Hubinet Ops 0.5 authority database."""


class AuthorityConflict(AuthorityError):
    """A durable ownership or lifecycle precondition did not hold."""


class HealthContractRevisionConflict(AuthorityConflict):
    """A health contract write lost its compare-and-set on the revision.

    Distinct from an ordinary conflict because it means something different to
    the operator: the resource is fine and the request was well formed, but
    someone else changed the contract first. Collapsing it into "this resource
    is not current" would send them looking for the wrong problem.
    """


class SnapshotSubmissionRefusedBeforeCallback(AuthorityConflict):
    """Current-authority proof refused a snapshot submission before the host
    submission callback was ever invoked.

    Raised ONLY by :meth:`InventoryAuthority.execute_snapshot_submission_if_current`,
    and ONLY for the one case where its current-authority predicate itself
    proved false -- structurally guaranteeing the submission callback ran zero
    times for this call. A terminal job, a checkpoint other than
    ``snapshot_may_have_started``, or any other lifecycle/invariant conflict
    raised by that same method remains ordinary :class:`AuthorityConflict`
    (this being a subclass, existing generic handlers still catch it): those
    mean the job's own durable state moved out from under the caller for some
    other reason, which a caller must not conflate with "current authority
    definitely refused before any host call". Any exception raised once the
    callback has begun executing must never be recast as this type.
    """


class MutationSubmissionRefusedBeforeCallback(AuthorityConflict):
    """Current-authority proof refused a package mutation submission before
    the host submission callback was ever invoked.

    The package-mutation mirror of
    :class:`SnapshotSubmissionRefusedBeforeCallback`, and raised ONLY by
    :meth:`InventoryAuthority.execute_package_mutation_submission_if_current`
    for the one case where its current-authority predicate itself proved
    false -- structurally guaranteeing the submission callback ran zero
    times, so no real package command can possibly have been launched by
    this call. A terminal job, a checkpoint other than
    ``mutation_may_have_started``, or any other lifecycle/invariant conflict
    raised by that same method stays ordinary :class:`AuthorityConflict`:
    those say nothing about whether the host was asked to mutate, and must
    never be routed into the durable seal path.
    """


class RollbackSubmissionRefusedBeforeCallback(AuthorityConflict):
    """Rollback authority proof refused a submission before the host callback.

    The same-job rollback mirror of
    :class:`SnapshotSubmissionRefusedBeforeCallback`, raised ONLY by
    :meth:`InventoryAuthority.execute_rollback_submission_if_current` for the
    one case where its rollback-authority predicate itself proved false --
    structurally guaranteeing the submission callback ran zero times, so no
    PVE rollback can possibly have been submitted by this call. A terminal
    job, a checkpoint other than ``rollback_may_have_started``, or any other
    lifecycle/invariant conflict raised by that same method stays an ordinary
    :class:`AuthorityConflict`: those say nothing about whether the host was
    asked to roll back, and must never be routed into the durable seal path.

    Note what this predicate is, and is not. It re-proves the job's ACTIVE
    ownership, its exact rollback operation identity, its exact confirmed
    same-job snapshot, and its exact resource/locator context. It deliberately
    does NOT re-prove current package-plan authority: rollback is compensation
    for a workload that may ALREADY have been mutated, so a newer scan or a
    stale approval must never be able to strand a half-upgraded guest by
    withdrawing its recovery path.
    """


class PackageMutationEvidenceNotAccepted(AuthorityConflict):
    """A submitting caller did not carry the accepted preparation evidence.

    Raised by
    :meth:`InventoryAuthority.execute_package_mutation_submission_if_current`
    when the digest the caller supplies is not the
    ``accepted_prepared_evidence_digest`` the arming transaction durably
    committed. It means a DIFFERENT invocation's evidence is the one
    authority accepted, so this caller may never submit -- and, critically,
    it is a distinct type from
    :class:`MutationSubmissionRefusedBeforeCallback` precisely so it can
    never be routed into the pre-submission seal: the invocation that did
    win the arming race may be submitting a real package command at this
    very moment, and sealing the operation "never submitted" on its behalf
    would be a lie. The callback ran zero times; the job stays ACTIVE,
    armed, and fenced, and this caller becomes recovery-only.
    """


class AuthorityNotFound(AuthorityError):
    """A requested durable authority record does not exist."""


class AuthorityInvariantError(AuthorityError):
    """Persisted state violates an invariant required by the authority layer."""


@dataclass(frozen=True, slots=True)
class BackendInstance:
    backend_instance_id: str
    created_at: str
    inventory_revision: int
    published_state_revision: int
    published_at: str


@dataclass(frozen=True, slots=True)
class InventorySource:
    inventory_source_id: str
    backend_instance_id: str
    provider_kind: str
    display_name: str
    credential_reference: str
    created_at: str
    source_config_revision: int
    last_issued_run_sequence: int
    last_committed_run_sequence: int | None
    active_discovery_run_id: str | None
    provider_contract_version: int
    freshness_duration_seconds: int
    facts: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class SourceEndpoint:
    endpoint_id: str
    inventory_source_id: str
    canonical_transport_locator: str
    canonicalization_contract_version: int
    lifecycle: EndpointLifecycle
    transport_trust_revision: int
    created_at: str


@dataclass(frozen=True, slots=True)
class SourceRuntimeHealth:
    inventory_source_id: str
    health: PersistentSourceHealth
    freshness: PersistentSourceFreshness
    health_origin: PersistentSourceHealthOrigin
    health_reason: str
    latest_completed_run_sequence: int | None
    latest_completed_outcome: str | None
    last_health_run_sequence: int | None
    last_run_health_outcome: str | None
    last_successful_observed_at: str | None
    freshness_reference_at: str | None
    freshness_valid_until: str | None
    committed_source_config_revision: int | None
    committed_endpoint_id: str | None
    committed_canonical_transport_locator: str | None
    committed_canonicalization_contract_version: int | None
    committed_transport_trust_revision: int | None


@dataclass(frozen=True, slots=True)
class DiscoveryRun:
    run_id: str
    inventory_source_id: str
    discovery_run_sequence: int
    issued_at: str
    expected_source_config_revision: int
    expected_endpoint_id: str
    expected_canonical_transport_locator: str
    expected_canonicalization_contract_version: int
    expected_transport_trust_revision: int
    provider_contract_version: int
    lifecycle: DiscoveryRunLifecycle
    terminalized_at: str | None
    terminal_reason: str | None
    completed_at: str | None
    provider_outcome: str | None
    observed_at: str | None
    normalized_snapshot_hash: str | None
    baseline_completeness: str | None
    source_availability: str | None
    baseline_mode: str | None
    permission_coverage_complete: bool | None
    boundary_consistent: bool | None
    covered_nodes: tuple[str, ...] | None
    failed_baseline_scopes: tuple[str, ...] | None
    acl_topology_hash_before: str | None
    acl_topology_hash_after: str | None
    permission_snapshot_hash_before: str | None
    permission_snapshot_hash_after: str | None
    detail_ok_count: int | None
    detail_temporarily_unavailable_count: int | None
    detail_error_count: int | None
    failed_detail_scopes: tuple[str, ...] | None
    completion_source_config_revision: int | None
    completion_endpoint_id: str | None
    completion_canonical_transport_locator: str | None
    completion_canonicalization_contract_version: int | None
    completion_transport_trust_revision: int | None


@dataclass(frozen=True, slots=True)
class InventorySourceState:
    source: InventorySource
    active_endpoint: SourceEndpoint
    runtime_health: SourceRuntimeHealth


@dataclass(frozen=True, slots=True)
class InventoryNode:
    node_id: str
    inventory_source_id: str
    external_node_name: str
    status: str
    available: bool
    facts: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ResourceIncarnation:
    resource_id: str
    inventory_source_id: str
    resource_type: str
    vmid: int
    resource_continuity_revision: int
    name: str
    status: str
    current_node_id: str | None
    last_known_node_id: str | None
    presence: str
    lifecycle: str
    observational_continuity: str
    security_continuity: str
    detail_status: str
    node_availability: str
    state_level: str
    active_binding_id: str | None
    locator_generation: int
    facts: Mapping[str, Any]
    termination_reason: str | None
    successor_resource_id: str | None


@dataclass(frozen=True, slots=True)
class ResourceLocatorBinding:
    binding_id: str
    inventory_source_id: str
    vmid: int
    locator_generation: int
    resource_id: str
    valid_from_run_sequence: int
    valid_to_run_sequence: int | None
    closure_reason: str | None


@dataclass(frozen=True, slots=True)
class ResourceTermination:
    """Retained terminal/tombstone record.

    The terminal/tombstone owner for direct replacement
    (``reason='replaced'``). ``run_sequence`` is always the exact discovery
    run that supplied the observation provenance for the closure.
    """

    resource_id: str
    inventory_source_id: str
    binding_id: str
    locator_generation: int
    reason: str
    successor_resource_id: str | None
    run_sequence: int
    created_at: str


@dataclass(frozen=True, slots=True)
class PackageScanPackage:
    package_name: str
    architecture: str
    installed_version: str
    candidate_version: str
    origin: str | None = None
    description: str | None = None
    security: bool | None = None


@dataclass(frozen=True, slots=True)
class PackageScanRun:
    scan_run_id: str
    resource_id: str
    inventory_source_id: str
    committed_source_config_revision: int
    committed_endpoint_id: str
    committed_canonical_transport_locator: str
    committed_canonicalization_contract_version: int
    committed_transport_trust_revision: int
    provider_contract_version: int
    attempt_sequence: int
    expected_binding_id: str
    expected_locator_generation: int
    expected_resource_continuity_revision: int
    expected_vmid: int
    expected_node_id: str
    expected_node_name: str
    started_at: str
    lifecycle: PackageScanLifecycle
    completed_at: str | None
    outcome: PackageScanOutcome | None
    failure_class: PackageScanFailure | None
    error_message: str | None
    os_id: str | None
    os_version: str | None
    pending_count: int | None
    plan_fingerprint: str | None
    reboot_required: bool | None
    packages: tuple[PackageScanPackage, ...] = ()


@dataclass(frozen=True, slots=True)
class PackagePlanApproval:
    approval_id: str
    resource_id: str
    reviewed_scan_run_id: str
    approved_plan_fingerprint: str
    approved_at: str


@dataclass(frozen=True, slots=True)
class ResourceHealthProbe:
    """One required typed probe inside an operator-declared health contract."""

    kind: HealthProbeKind
    target: str


@dataclass(frozen=True, slots=True)
class ResourceHealthContract:
    """One exact resource incarnation's complete current health contract.

    ``probes`` is the complete required set in canonical order: every one of
    them must hold. ``fingerprint`` covers only that material, so a future
    health-execution stage can record exactly which contract it evaluated.
    There is no "empty contract" value of this type -- a resource with no
    contract has no row at all, which is *unconfigured*, never healthy.
    """

    resource_id: str
    revision: int
    fingerprint: str
    created_at: str
    updated_at: str
    probes: tuple[ResourceHealthProbe, ...]


@dataclass(frozen=True, slots=True)
class PackageUpdateJobHealthProbe:
    """ONE probe of the health contract generation a job froze at issuance.

    A copy, not a reference. The live
    :class:`ResourceHealthContract` may be edited, replaced, or cleared while
    a job runs, and after that job may have mutated the workload its success
    criterion must not move with it. So the exact ``(kind, target)`` material
    is copied into immutable job-owned rows, in the same canonical order the
    contract's fingerprint covers.
    """

    probe_index: int
    kind: HealthProbeKind
    target: str


@dataclass(frozen=True, slots=True)
class PackageUpdateJobHealthProbeResult:
    """The durable, definitive result recorded for ONE frozen probe.

    Only ever written by the one definitive finalization boundary, and only
    as a complete set covering every frozen probe. ``outcome`` may be
    ``UNKNOWN`` for an individual probe inside a FAILED contract verdict --
    an unevaluable member alongside a proven failure is truthful history --
    but a PASSED contract requires every one of these to be ``PASSED``.

    ``reason`` is a bounded token from a closed taxonomy, never raw command
    output: nothing a guest printed reaches durable state.
    """

    probe_index: int
    outcome: HealthProbeOutcome
    checked_at: str
    reason: str


@dataclass(frozen=True, slots=True)
class PackageUpdateJobPackage:
    package_index: int
    package_name: str
    architecture: str
    installed_version: str
    candidate_version: str
    origin: str | None = None
    description: str | None = None
    security: bool | None = None


@dataclass(frozen=True, slots=True)
class PackageUpdateJob:
    job_id: str
    request_id: str
    issued_at: str
    #: Durable per-resource issuance order (schema v17), allocated
    #: atomically in the same transaction as this job's insert -- the
    #: package_scan_runs.attempt_sequence pattern applied here. `issued_at`
    #: is wall-clock text and is never sufficient to answer "which of this
    #: resource's jobs is most recently issued": an ordinary host clock
    #: step backward between two issuances can give a genuinely LATER job
    #: an EARLIER issued_at. issuance_sequence has no such dependency --
    #: it is written once, never reused, and strictly increasing in real
    #: issuance order -- and every "latest job for this resource" read
    #: must order by this field, not issued_at.
    issuance_sequence: int
    resource_id: str
    approval_id: str
    approval_reviewed_scan_run_id: str
    approved_plan_fingerprint: str
    approval_approved_at: str
    current_plan_scan_run_id: str
    inventory_source_id: str
    committed_source_config_revision: int
    committed_endpoint_id: str
    committed_canonical_transport_locator: str
    committed_canonicalization_contract_version: int
    committed_transport_trust_revision: int
    provider_contract_version: int
    expected_resource_type: str
    expected_binding_id: str
    expected_locator_generation: int
    expected_resource_continuity_revision: int
    expected_vmid: int
    expected_node_id: str
    expected_node_name: str
    package_count: int
    #: The exact health contract GENERATION this job froze at issuance, from
    #: the live contract that existed before any workload mutation could
    #: begin. Immutable, non-null: a job may not be issued for a resource
    #: with no declared health contract, because a job whose success
    #: criterion does not exist yet could never be called successful.
    health_contract_revision: int
    health_contract_fingerprint: str
    health_contract_probe_count: int
    status: PackageUpdateJobStatus
    checkpoint: PackageUpdateCheckpoint
    snapshot_operation_id: str | None
    snapshot_name: str | None
    snapshot_intent_recorded_at: str | None
    snapshot_task_upid: str | None
    snapshot_confirmed_at: str | None
    mutation_operation_id: str | None
    mutation_may_have_started_at: str | None
    #: The digest of the EXACT preparation evidence the arming transaction
    #: accepted. Committed together with the write-ahead checkpoint, and
    #: write-once from then on, so only the invocation carrying this exact
    #: digest may cross the submission boundary.
    accepted_prepared_evidence_digest: str | None
    mutation_completed_at: str | None
    health_started_at: str | None
    #: Set by the ONE definitive health finalization boundary, together with
    #: ``health_outcome`` and the complete durable probe result set, in one
    #: statement. An UNKNOWN contract verdict deliberately writes neither:
    #: health execution is read-only and safe to repeat, so a non-answer
    #: stays retryable instead of becoming a durable one.
    health_completed_at: str | None
    #: ``passed`` or ``failed`` only. ``passed`` is the single legal route to
    #: ``SUCCEEDED`` and the schema ties the two together in both directions.
    health_outcome: HealthOutcome | None
    #: The deterministic same-job rollback operation identity, committed
    #: together with ``rollback_may_have_started_at`` as ONE indivisible
    #: write-ahead authority fact, and write-once from then on.
    rollback_operation_id: str | None
    rollback_may_have_started_at: str | None
    #: The PVE task identity observed for this exact rollback, recorded the
    #: instant the host durably knows one. Write-once; it is what lets a
    #: restarted backend reattach to an asynchronous `vzrollback` task
    #: instead of guessing.
    rollback_task_upid: str | None
    rollback_completed_at: str | None
    terminalized_at: str | None
    terminal_reason: str | None
    packages: tuple[PackageUpdateJobPackage, ...] = ()
    #: The complete frozen contract material, in canonical order. Always
    #: exactly ``health_contract_probe_count`` long; the read path refuses to
    #: hand back a job whose frozen probes disagree with its own header.
    health_probes: tuple[PackageUpdateJobHealthProbe, ...] = ()
    #: Empty until a definitive verdict was durably recorded, and complete
    #: from then on.
    health_probe_results: tuple[PackageUpdateJobHealthProbeResult, ...] = ()


@dataclass(frozen=True, slots=True)
class PackageUpdateSnapshotIdentity:
    """One package update job's single deterministic pre-update snapshot.

    Both fields are derived from immutable job identity, so the exact same
    job derives the exact same identity after any restart. ``snapshot_name``
    is the physical PVE key; ``snapshot_operation_id`` keys the host-side
    durable operation journal.
    """

    snapshot_operation_id: str
    snapshot_name: str


@dataclass(frozen=True, slots=True)
class PackageUpdateMutationRequest:
    """Everything the dark host boundary is fenced against for one mutation.

    Assembled by :meth:`InventoryAuthority.package_update_mutation_request`
    in ONE read transaction, so every field is a consistent view of the same
    durable job. The host binds all of it into its journal's request
    fingerprint, so a request differing in ANY of these facts is a different
    request and is refused rather than allowed to reuse an existing
    operation:

    - ``mutation_operation_id`` -- the deterministic job-owned identity;
    - ``backend_instance_id``/``job_id``/``resource_id``/
      ``resource_continuity_revision`` -- who owns the operation, and which
      exact resource incarnation it belongs to, so a reused VMID or a
      replaced guest can never inherit it;
    - ``binding_id``/``locator_generation``/``vmid``/``expected_node`` -- the
      execution locator the host independently re-validates against live PVE
      immediately before it does anything;
    - ``plan_fingerprint``/``packages`` -- the exact approved material. The
      host recomputes the fingerprint from ``packages`` and refuses a request
      whose declared digest does not describe its own package list.

    ``packages`` is the material quadruple set, sorted by
    ``(package_name, architecture)``: it is used by the host ONLY to refuse a
    mutation whose starting state drifted, never to build the package
    command, which is fixed argv with no package name in it at all.
    """

    mutation_operation_id: str
    plan_fingerprint: str
    backend_instance_id: str
    job_id: str
    resource_id: str
    binding_id: str
    locator_generation: int
    resource_continuity_revision: int
    vmid: int
    expected_node: str
    packages: tuple[tuple[str, str, str, str], ...]


@dataclass(frozen=True, slots=True)
class PackageUpdateMutationIdentity:
    """One package update job's single deterministic package mutation.

    Derived purely from immutable job identity (this backend instance, the
    job, the resource incarnation, its continuity revision), so the exact
    same job derives the exact same operation id after any restart and no
    other job -- including another job for the same resource incarnation, or
    the same job on a different backend installation -- can ever derive it.
    A reused VMID or a replaced resource therefore never inherits a mutation
    operation. ``mutation_operation_id`` keys the host-side durable
    at-most-once journal.
    """

    mutation_operation_id: str


@dataclass(frozen=True, slots=True)
class SnapshotOwnership:
    """Strict structured Hubinet ownership metadata carried by a snapshot.

    This, never the snapshot name, is the ownership proof. Anything that does
    not parse into exactly this shape is treated as malformed and fails
    closed rather than being silently skipped.
    """

    protocol: int
    kind: str
    job_id: str
    resource_id: str
    resource_continuity_revision: int
    inventory_source_id: str
    backend_instance_id: str


@dataclass(frozen=True, slots=True)
class ObservedSnapshot:
    """One canonical observation of a PVE LXC snapshot listing entry.

    ``is_current_pseudo_entry`` marks PVE's synthetic ``current`` row, which
    is never a real snapshot. ``incomplete`` marks an entry PVE still reports
    a ``snapstate`` for, i.e. a snapshot operation that has not finished.
    ``ownership_malformed`` marks an entry that looks Hubinet-owned but whose
    metadata did not parse strictly.
    """

    name: str
    description: str
    is_current_pseudo_entry: bool = False
    incomplete: bool = False
    snaptime: int | None = None
    parent: str | None = None
    ownership: SnapshotOwnership | None = None
    ownership_malformed: bool = False


@dataclass(frozen=True, slots=True)
class PackageUpdateRollbackTarget:
    """The one snapshot a specific package update job may roll back to."""

    job_id: str
    resource_id: str
    expected_vmid: int
    expected_node_name: str
    snapshot_name: str
    snapshot_operation_id: str
    snapshot_confirmed_at: str


@dataclass(frozen=True, slots=True)
class PackageUpdateHealthRequest:
    """Everything the dark health boundary is told, for one evaluation.

    Assembled by :meth:`InventoryAuthority.package_update_health_request` in
    ONE read transaction, so every field is a consistent view of the same
    durable job, and assembled ENTIRELY from job authority: there is no
    parameter through which any caller may supply a VMID, a node, a contract
    revision, a fingerprint, a probe, a probe kind, or a probe target. Those
    are durable facts this job froze, not arguments.

    Unlike the snapshot, mutation, and rollback requests, this one carries no
    operation identity and binds no host journal. That is deliberate rather
    than an omission: the operation is READ-ONLY, so there is no at-most-once
    property to protect and inventing a destructive-operation journal to
    mimic the other stages would add a failure mode without adding a
    guarantee. Repeating a health read is safe.
    """

    job_id: str
    backend_instance_id: str
    resource_id: str
    binding_id: str
    locator_generation: int
    resource_continuity_revision: int
    vmid: int
    expected_node: str
    #: The exact frozen contract generation this evaluation is about. The
    #: host echoes both back, and the backend re-proves them against the job
    #: before believing a single probe result.
    health_contract_revision: int
    health_contract_fingerprint: str
    #: The complete frozen probe set, in canonical order.
    probes: tuple[PackageUpdateJobHealthProbe, ...]


@dataclass(frozen=True, slots=True)
class PackageUpdateRollbackIdentity:
    """One package update job's single deterministic same-job rollback.

    Derived purely from immutable authority -- this backend instance, the
    job, the resource incarnation, its continuity revision, and the job's own
    CONFIRMED snapshot identity -- so the exact same job derives the exact
    same operation id after any restart, and no other job can ever derive it.
    ``rollback_operation_id`` keys the host-side durable at-most-once journal.

    Binding the confirmed snapshot identity in is what makes the operation
    id mean "roll THIS job back to THIS exact snapshot" rather than merely
    "roll this job back": a rollback aimed at any other snapshot is a
    different operation and can never reuse this one's durable journal.
    """

    rollback_operation_id: str


@dataclass(frozen=True, slots=True)
class PackageUpdateRollbackRequest:
    """Everything the dark host boundary is fenced against for one rollback.

    Assembled by :meth:`InventoryAuthority.package_update_rollback_request` in
    ONE read transaction, so every field is a consistent view of the same
    durable job. The host binds all of it into its journal's request
    fingerprint, so a request differing in ANY of these facts is a different
    request and is refused rather than allowed to reuse an existing
    operation:

    - ``rollback_operation_id`` -- the deterministic job-owned identity;
    - ``backend_instance_id``/``job_id``/``resource_id``/
      ``resource_continuity_revision`` -- who owns the operation and which
      exact resource incarnation it belongs to, so a reused VMID or a
      replaced guest can never inherit it;
    - ``binding_id``/``locator_generation``/``vmid``/``expected_node`` -- the
      execution locator the host independently re-validates against live PVE
      immediately before it does anything;
    - ``snapshot_name``/``snapshot_operation_id`` -- the exact same-job
      snapshot selected by authority, never by a caller;
    - ``expected_snapshot_ownership`` -- the strict structured ownership
      metadata that snapshot must carry in its PVE description.

    ``expected_snapshot_ownership`` exists because **a snapshot name is a
    physical PVE key and is never ownership proof** (see
    ``snapshot_identity.py``). Authority proves ownership from a fresh
    canonical listing when it ARMS the rollback, but PVE state can change
    between that proof and the destructive call: the same name can come to
    exist carrying absent, malformed, foreign, or another job's metadata. So
    the host re-proves ownership from its OWN fresh listing immediately
    before it crosses the ``submitted`` boundary, and this field is what it
    compares against. It is derived entirely from authority -- never from a
    caller -- and is bound into the host's request fingerprint, so a request
    naming different expected ownership is a different operation.

    There is deliberately no ``start`` field: this stage always rolls back
    with PVE's ``start`` parameter at 0, as a code-owned constant on the host
    side, so a successful rollback leaves the container STOPPED and nothing
    on this boundary can ask for anything else.
    """

    rollback_operation_id: str
    backend_instance_id: str
    job_id: str
    resource_id: str
    binding_id: str
    locator_generation: int
    resource_continuity_revision: int
    vmid: int
    expected_node: str
    snapshot_name: str
    snapshot_operation_id: str
    expected_snapshot_ownership: SnapshotOwnership


@dataclass(frozen=True, slots=True)
class PackageUpdateJobEvent:
    job_id: str
    sequence: int
    created_at: str
    level: PackageUpdateEventLevel
    stage: PackageUpdateCheckpoint
    event_type: PackageUpdateEventType
    message: str
    details: Mapping[str, Any]
