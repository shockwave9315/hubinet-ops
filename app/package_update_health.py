"""Job-bound healthcheck execution for package update jobs.

**Production reachable, through the one worker.** `app/inventory_runtime.py`
constructs this orchestrator and composes it into the one
`PackageUpdateWorker` when `package_update.enabled` is configured true (see
"Production activation" in `ARCHITECTURE.md`). No route, scheduler, or the
Home Assistant integration calls into it directly -- the worker is the only
caller, entering this stage at `mutation_completed` or `health_started`. This
module still contains no host I/O, no write-ahead uncertainty checkpoint, and
no policy of its own: it composes authority and one read-only dark host
boundary, exactly as documented below.

## What this stage answers, and what it refuses to answer

It answers exactly one question: **did this job's own frozen health contract
generation hold?** It does not decide what to do about the answer. A failing
verdict leaves the job ACTIVE and rollback-capable and calls nothing; there is
no retry count, grace period, threshold, majority, or automatic compensation
anywhere in this file, because this product has made no such policy and
inventing one here would be inventing a product decision.

```text
job ACTIVE at mutation_completed
  -> health_started                         (durable, authority-only)
  -> re-prove the backend still names this exact resource/locator context
  -> ONE bounded read-only host round trip over a separate dark SSH boundary
  -> strict validation of the answer against the FROZEN contract
  -> early re-prove of backend context after the host answered
  -> aggregate ALL-OF:
       every probe passed          -> atomic final context proof + passed -> SUCCEEDED
       any probe proven failed     -> atomic final context proof + failed -> ACTIVE
       anything else               -> no verdict at all, retryable
```

## The contract is FROZEN, and that is the whole point

A package-update job's success criterion is decided at issuance, when the
resource identity, source authority, approval provenance, and exact package
plan are already frozen and nothing has been mutated. From that moment the job
carries its own immutable copy of the contract generation -- `revision`,
`fingerprint`, and the complete canonical probe set.

Before the write-ahead package-mutation boundary, the live contract drifting
away from that copy makes the job STALE and forbids the mutation: an operator
who has withdrawn or changed the definition of healthy must not have a job
validated against the old one. After that boundary the frozen copy is the ONLY
authority, because packages may already have changed and re-deciding success
against a contract the operator edited afterwards is moving the goalposts.
Schema v15 never reuses a revision, so a clear-and-recreate of byte-identical
probes is correctly seen as a new generation.

## PASS, FAIL, and UNKNOWN are three different things

A contract is an ALL-OF over its declared probes, so:

- **PASS** requires every frozen probe to be POSITIVELY proven. Absence of an
  observed failure is not a pass, and neither is a probe that could not be
  evaluated.
- **FAIL** needs one probe positively proven false, inside a COMPLETE DECISIVE
  observation set. One false conjunct proves an ALL-OF false whatever the
  others did -- but UNKNOWN is impossible in material accepted for durable
  finalization: a decisive round admits no unevaluated probe at all (a host
  claiming both at once is rejected outright by `validate_host_health_result`
  below), and `InventoryAuthority.complete_package_update_health` refuses any
  observation set carrying an UNKNOWN outcome independently of that, as
  defense in depth.
- **UNKNOWN** is the remainder, and it is never success. It writes no verdict
  and no durable result rows: the job stays ACTIVE at `health_started`, keeps
  its snapshot and its rollback authority, and the evaluation may simply be
  run again.

Retrying is safe here in a way it is emphatically NOT for the snapshot,
mutation, and rollback stages, and for one structural reason: **health
execution is read-only.** It runs `systemctl show` (or, for the built-in
`guest_operational` baseline, one fixed `/bin/true` liveness check), and
neither changes anything. There is therefore deliberately no host operation
journal, no `may_have_started` uncertainty checkpoint, and no at-most-once
fence in this stage -- inventing one would be mimicking the shape of the
destructive stages without their reason for existing.

## Verified CLI semantics

Every fixed argv here was verified against the real tools rather than assumed;
`ARCHITECTURE.md`, "Job-bound healthcheck execution", records what was
observed and why the systemd command is the one that cannot false-PASS:
`systemctl is-active <pattern>` expands globs and exits 0 if ANY matching unit
is active, and `--` does NOT stop that expansion, so it is unusable for a
probe that must name one exact unit. The executor uses `systemctl show`
instead, requires exactly one property block, and validates the target
charset so it cannot be a glob.

The `guest_operational` baseline runs no discovery command at all: it is one
fixed, code-owned, argument-less liveness check (`/bin/true`) against the
exact current resource context, executed exactly once per evaluation attempt
-- never settled, never retried within one attempt. Docker workload health is
not part of v0.5 Hubinet Ops package-update health.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from app.inventory import (
    AuthorityConflict,
    HealthOutcome,
    HealthProbeKind,
    HealthProbeObservation,
    HealthProbeOutcome,
    InventoryAuthority,
    PackageUpdateCheckpoint,
    PackageUpdateHealthRequest,
    PackageUpdateJob,
    PackageUpdateJobHealthProbe,
    PackageUpdateJobStatus,
    PackageUpdateHealthContextChanged,
    aggregate_health_outcome,
)
from app.inventory.health_observation import (
    HealthProbeSemanticError,
    require_health_probe_semantics,
)


class PackageUpdateHealthError(RuntimeError):
    """A dark health evaluation could not be carried out safely.

    ``reason`` is the bounded token this failure should be recorded under
    when it becomes a durable "no verdict" event. It defaults to
    ``host_response_rejected`` -- the honest description of a malformed,
    contradictory, or mismatched answer -- and the host boundary sets
    something more specific when the host told us WHY it refused, so an
    operator reading the job's history sees "the guest was not running"
    rather than "something went wrong".
    """

    def __init__(self, message: str, *, reason: str = "host_response_rejected") -> None:
        super().__init__(message)
        self.reason = reason


#: How a whole-request refusal by the host maps onto the bounded reason
#: taxonomy. Anything the helper classifies differently, or does not classify
#: at all, stays the generic rejection: a token is only used when the host
#: actually said the thing it names.
HOST_REFUSAL_REASONS: dict[str, str] = {
    "guest_unavailable": "guest_unavailable",
    "stale_target": "resource_context_changed",
    "unsupported_resource_type": "resource_context_changed",
    "execution_failed": "command_failed",
}


#: Bounded reason tokens the HOST may report for a single probe. A strict
#: subset of the authority's closed taxonomy: the backend adds its own tokens
#: for things only it can know (a rejected response, a changed resource
#: context), and the host can never claim one of those.
HOST_PROBE_REASONS: frozenset[str] = frozenset(
    {
        "unit_active",
        "unit_not_active",
        "unit_activating",
        "unit_deactivating",
        "unit_reloading",
        "unit_job_pending",
        "guest_operational_confirmed",
        "probe_target_not_exact",
        "probe_target_ambiguous",
        "guest_unavailable",
        "command_failed",
        "command_timed_out",
        "malformed_output",
        # PR #80 review finding 1: the host's own absolute settling deadline
        # ran out before this probe's family could safely start (or finish)
        # another subprocess. Meaningful only for the advanced systemd
        # settling window -- the guest_operational baseline is a single
        # one-shot execution and never settles.
        "settling_budget_exhausted",
    }
)

class HealthStageStatus(StrEnum):
    """How one dark health evaluation attempt ended, from the caller's side."""

    #: Every frozen probe positively proven. The job is durably SUCCEEDED.
    PASSED = "passed"
    #: At least one frozen probe positively proven false. The job stays
    #: ACTIVE, keeps its snapshot, and may still arm a same-job rollback.
    FAILED = "failed"
    #: No verdict could be reached truthfully. Nothing durable was written
    #: beyond a bounded event; the evaluation may be repeated.
    UNKNOWN = "unknown"


class HealthEvaluationStatus(StrEnum):
    """Whether the HOST's own bounded settling window reached a round that
    may be aggregated into a durable verdict at all.

    This is the explicit, typed carrier of "decisiveness" the frozen
    architecture requires: a durable PASS or FAIL may come ONLY from one
    complete DECISIVE round (every probe resolved, the round itself
    completed within its bound, and it was not the first round). Inferring
    decisiveness a second time from the probe outcomes alone -- "every probe
    happens to be PASSED or FAILED, so it must have been decisive" -- would
    silently accept a round that only LOOKS complete because the settling
    deadline or round cap cut it off mid-transition. This orchestrator checks
    this field BEFORE ever calling `aggregate_health_outcome`; neither the
    backend elsewhere nor Home Assistant may re-derive it.
    """

    #: Every probe in this round resolved (none transient); PASSED/FAILED is
    #: this round's proven complete ALL-OF verdict, and may be persisted.
    DECISIVE = "decisive"
    #: The settling deadline, round cap, or guest-command ceiling was
    #: reached with at least one probe still transient, OR a structurally
    #: broken target makes a decisive round unreachable by construction.
    #: Never aggregated into a verdict, whatever the individual probes say.
    UNRESOLVED = "unresolved"


@dataclass(frozen=True, slots=True)
class HostProbeResult:
    """What the dark host reported about ONE frozen probe."""

    probe_index: int
    kind: HealthProbeKind
    #: ``None`` for, and only for, ``HealthProbeKind.GUEST_OPERATIONAL``.
    target: str | None
    outcome: HealthProbeOutcome
    reason: str


@dataclass(frozen=True, slots=True)
class HostHealthResult:
    """The bounded, typed answer one dark host health operation returns.

    ``contract_revision`` and ``contract_fingerprint`` are echoed back by the
    host from the request, and are re-proved against the job's frozen facts
    before a single probe result is believed: an answer about a different
    contract generation has told us nothing about this one.
    """

    contract_revision: int
    contract_fingerprint: str
    probes: tuple[HostProbeResult, ...]
    #: Explicit, typed decisiveness -- see `HealthEvaluationStatus`. Required
    #: (no default): a caller that has not decided whether a round was
    #: decisive must not be able to silently default into either answer.
    evaluation_status: HealthEvaluationStatus
    #: Bounded classification text for a whole-request failure. Never raw
    #: stdout, stderr, or command text.
    reason: str | None = None
    #: Bounded settling metadata from the host's own bounded settling window
    #: (ARCHITECTURE.md, "Job-bound healthcheck execution"): how many full
    #: rounds it ran, the total wall time it spent settling, and the span of
    #: the LAST round. Three small integers, never guest output. ``None``
    #: only for an answer that predates this metadata (defence in depth; the
    #: deployed helper always sends it).
    settling_rounds: int | None = None
    settling_seconds: float | None = None
    last_round_span_ms: int | None = None


@dataclass(frozen=True, slots=True)
class HealthStageResult:
    """The durable result of one dark health stage attempt."""

    status: HealthStageStatus
    job: PackageUpdateJob
    reason: str | None = None


class PackageUpdateHealthHostControl(Protocol):
    """The dark typed host boundary this orchestrator is allowed to use.

    Deliberately the narrowest boundary in this repository: ONE read-only
    operation, no submission, no journal, no seal, no lifecycle control, no
    generic action dispatcher, and no place to pass a command string, a
    template, an option, or a probe the job did not freeze.
    """

    def evaluate_health_contract(
        self, request: PackageUpdateHealthRequest
    ) -> HostHealthResult:
        """Evaluate one job's complete frozen probe set inside its guest."""


class PackageUpdateHealthOrchestrator:
    """Coordinate authority and one dark host boundary for one evaluation.

    PRODUCTION-REACHABLE, and built by the composition root: see the module
    docstring above -- `app/inventory_runtime.py`'s
    `_build_package_update_runtime` constructs it and composes it into the
    one `PackageUpdateWorker`, which is its only caller. It performs no
    package mutation, no snapshot operation, and no rollback -- and,
    critically, it never CALLS the rollback stage either. A failing health
    verdict is reported and nothing else happens; see `PRODUCT.md` on why
    automatic compensation is a separate, unmade decision.

    Every host round trip happens strictly OUTSIDE this store's writer
    transactions. The authority transitions here are short and local: start
    the evaluation, or atomically re-prove live context and accept its exact
    result. Nothing holds the writer lock
    across SSH, `pct`, `systemctl`, or a probe loop -- which is affordable
    precisely because a read-only evaluation needs no critical section to
    stop a second destructive submission.
    """

    def __init__(
        self,
        authority: InventoryAuthority,
        host_control: PackageUpdateHealthHostControl,
    ) -> None:
        self._authority = authority
        self._host_control = host_control

    def evaluate_job_health(self, job_id: str) -> HealthStageResult:
        """Drive one job's frozen health contract to a durable answer.

        Re-entrant by design, and safe to re-enter because it is read-only. A
        job already at ``health_started`` after a crash or a backend restart
        simply evaluates again; a job that already reached a verdict is
        refused rather than re-decided.
        """

        job = self._authority.package_update_job(job_id)
        if job.status is not PackageUpdateJobStatus.ACTIVE:
            raise PackageUpdateHealthError("package update job is terminal")
        if job.checkpoint not in (
            PackageUpdateCheckpoint.MUTATION_COMPLETED,
            PackageUpdateCheckpoint.HEALTH_STARTED,
        ):
            raise PackageUpdateHealthError(
                "package update job is not ready for health evaluation"
            )

        # A. Durably record that this job's frozen contract is being
        # evaluated. Idempotent, and never a claim about the outcome.
        job = self._authority.start_package_update_health(job.job_id)

        # B. Re-prove that the backend still names the intended current
        # resource incarnation BEFORE any host I/O. A read-only probe against
        # a REPLACEMENT guest at a reused VMID would be a false statement
        # about this job's workload even though it changes nothing.
        try:
            request = self._authority.package_update_health_request(job.job_id)
        except AuthorityConflict as exc:
            return self._unknown(job.job_id, "resource_context_changed", str(exc))

        # C. ONE bounded, read-only host round trip, entirely outside every
        # authority transaction.
        try:
            host_result = self._host_control.evaluate_health_contract(request)
        except PackageUpdateHealthError as exc:
            # The boundary already classified this as truthfully as it can:
            # the host's own refusal reason where it gave one, and the
            # generic rejection where it did not.
            return self._unknown(job.job_id, exc.reason, str(exc))
        except Exception as exc:  # noqa: BLE001 - any failure here is unknown
            return self._unknown(
                job.job_id,
                "host_unreachable",
                "health host evaluation did not return an answer: "
                f"{type(exc).__name__}",
            )

        # D. Validate the answer against the FROZEN contract before believing
        # any part of it.
        try:
            observations = validate_host_health_result(job, host_result)
        except PackageUpdateHealthError as exc:
            return self._unknown(job.job_id, exc.reason, str(exc))

        # E. Re-prove the backend resource context immediately after the host
        # answered, and BEFORE these validated observations are used for
        # ANYTHING -- decisive or not. A guest replaced while the round trip
        # was in flight means every observation above describes a
        # replacement/moved resource rather than the exact incarnation frozen
        # into this job, so it must carry no probe evidence at all: this is
        # exactly `_unknown`'s own documented contract for an in-flight
        # context change, and it applies whether the round below turns out
        # decisive or not. Reuses the SAME authority proof the pre-host
        # re-proof (step B) used, rather than inventing an approximate
        # context predicate here.
        try:
            self._authority.package_update_health_request(job.job_id)
        except AuthorityConflict as exc:
            return self._unknown(job.job_id, "resource_context_changed", str(exc))

        # F. The explicit, typed decisiveness carrier is checked BEFORE any
        # aggregation -- never re-derived from the probe outcomes alone. A
        # non-decisive round (the settling deadline/round-cap was reached
        # with a probe still transient, or a structurally broken target
        # makes decisiveness unreachable by construction) may look
        # all-PASSED or contain exactly one FAILED probe, but that is NOT
        # proof of anything: only a decisive round's complete observation
        # set may ever become a durable verdict.
        if host_result.evaluation_status is not HealthEvaluationStatus.DECISIVE:
            unresolved_probe = next(
                (
                    observation
                    for observation in observations
                    if observation.outcome is HealthProbeOutcome.UNKNOWN
                ),
                None,
            )
            # A non-decisive round need not contain any UNKNOWN probe at all
            # (e.g. every probe read PASSED, but the round itself ran too
            # long to trust, or this was the first round) -- there is no
            # single "blocking probe" reason to report in that case, so the
            # generic, honest classification for an answer that cannot be
            # believed as a verdict is used instead.
            blocking_reason = (
                unresolved_probe.reason
                if unresolved_probe is not None
                else "host_response_rejected"
            )
            return self._unknown(
                job.job_id,
                blocking_reason,
                "the host's bounded settling window did not reach a "
                f"decisive round ({blocking_reason})",
                probe_evidence=observations,
                settling_rounds=host_result.settling_rounds,
                settling_seconds=host_result.settling_seconds,
                last_round_span_ms=host_result.last_round_span_ms,
            )

        outcome = aggregate_health_outcome(
            observation.outcome for observation in observations
        )
        if outcome is HealthOutcome.UNKNOWN:  # pragma: no cover - defense in
            # depth only: `validate_host_health_result` already refuses a
            # DECISIVE payload carrying any UNKNOWN probe, so this is
            # structurally unreachable, and it stays here in case that
            # invariant is ever loosened by mistake.
            blocking = next(
                observation
                for observation in observations
                if observation.outcome is HealthProbeOutcome.UNKNOWN
            )
            return self._unknown(
                job.job_id,
                blocking.reason,
                "at least one frozen probe could not be evaluated truthfully "
                f"({blocking.reason})",
                probe_evidence=observations,
                settling_rounds=host_result.settling_rounds,
                settling_seconds=host_result.settling_seconds,
                last_round_span_ms=host_result.last_round_span_ms,
            )

        # The post-host context re-proof already happened once, at step E
        # above, for this exact validated observation set -- covering both
        # the decisive and non-decisive rounds alike. `complete_package_
        # update_health` still re-proves it a SECOND time, atomically, inside
        # the very transaction that would commit the verdict (see its
        # docstring: "the load-bearing post-host proof"), which is what
        # closes the true check/commit race for a decisive PASS/FAIL. That
        # atomic proof is the one that must never be skipped; the proof above
        # only decides, earlier and more cheaply, whether probe evidence may
        # be reported at all for a non-decisive round.
        try:
            decided = self._authority.complete_package_update_health(
                job.job_id, observations
            )
        except PackageUpdateHealthContextChanged as exc:
            return self._unknown(
                job.job_id, "resource_context_changed", str(exc)
            )
        return HealthStageResult(
            status=(
                HealthStageStatus.PASSED
                if outcome is HealthOutcome.PASSED
                else HealthStageStatus.FAILED
            ),
            job=decided,
        )

    def _unknown(
        self,
        job_id: str,
        reason_token: str,
        detail: str,
        *,
        probe_evidence: Sequence[HealthProbeObservation] = (),
        settling_rounds: int | None = None,
        settling_seconds: float | None = None,
        last_round_span_ms: int | None = None,
    ) -> HealthStageResult:
        """Record a truthful non-answer and leave the job exactly as it was.

        If the job moved on underneath this attempt -- an operator armed a
        rollback, say -- the event can no longer be appended, and that is not
        an error worth raising: the outcome of THIS attempt is still "no
        verdict", and the job's real state is read back and returned.

        ``probe_evidence`` is populated only when the host actually answered
        and its per-probe observations were validated against the frozen
        contract (an in-flight resource-context change or a whole-request
        host refusal carries no such evidence to report). Bounded, typed,
        never guest output -- see `InventoryAuthority.
        record_package_update_health_outcome_unknown`.
        """

        try:
            job = self._authority.record_package_update_health_outcome_unknown(
                job_id,
                reason_token,
                probe_evidence=probe_evidence,
                settling_rounds=settling_rounds,
                settling_seconds=settling_seconds,
                last_round_span_ms=last_round_span_ms,
            )
        except AuthorityConflict:
            job = self._authority.package_update_job(job_id)
        return HealthStageResult(
            status=HealthStageStatus.UNKNOWN, job=job, reason=detail
        )


def validate_host_health_result(
    job: PackageUpdateJob, result: HostHealthResult
) -> tuple[HealthProbeObservation, ...]:
    """Prove one host answer is about EXACTLY this job's frozen contract.

    A malformed or mismatched host response is never a pass and never a
    failure -- it is rejected, which the caller turns into UNKNOWN. Every
    check below is a distinct way an answer could be about something other
    than this job's frozen generation:

    - the echoed contract revision and fingerprint are this job's;
    - there is exactly one result per frozen probe, no more and no fewer;
    - the index set is exactly the frozen index set, with no duplicate;
    - each result's kind and target are the frozen kind and target at that
      index -- so an answer about the right number of the wrong things is
      refused;
    - each outcome is one of the three known values;
    - each reason is a bounded token the HOST is allowed to report, is
      consistent with the outcome it accompanies, and is possible for the
      kind of probe it claims to describe;
    - ``evaluation_status`` is a known value, and is never DECISIVE while any
      probe is UNKNOWN -- a decisive round is, by the frozen definition,
      exactly one where every probe resolved with no transient state, so a
      host claiming both at once is contradicting itself and is rejected
      outright rather than believed either way.
    """

    if not isinstance(result, HostHealthResult):
        raise PackageUpdateHealthError("a typed host health result is required")
    if not isinstance(result.evaluation_status, HealthEvaluationStatus):
        raise PackageUpdateHealthError(
            "host returned an unknown evaluation status"
        )
    if result.contract_revision != job.health_contract_revision:
        raise PackageUpdateHealthError(
            "host answered about a different health contract revision"
        )
    if result.contract_fingerprint != job.health_contract_fingerprint:
        raise PackageUpdateHealthError(
            "host answered about a different health contract fingerprint"
        )
    frozen: tuple[PackageUpdateJobHealthProbe, ...] = job.health_probes
    if len(frozen) != job.health_contract_probe_count:
        raise PackageUpdateHealthError(
            "package update job frozen health contract is incoherent"
        )
    if len(result.probes) != len(frozen):
        raise PackageUpdateHealthError(
            "host returned a different number of probe results than the "
            "frozen contract declares"
        )
    by_index: dict[int, HostProbeResult] = {}
    for probe_result in result.probes:
        if not isinstance(probe_result, HostProbeResult):
            raise PackageUpdateHealthError("a typed host probe result is required")
        if probe_result.probe_index in by_index:
            raise PackageUpdateHealthError(
                "host returned a duplicate probe result"
            )
        by_index[probe_result.probe_index] = probe_result

    observations: list[HealthProbeObservation] = []
    for probe in frozen:
        probe_result = by_index.get(probe.probe_index)
        if probe_result is None:
            raise PackageUpdateHealthError(
                "host result is missing a frozen probe"
            )
        if probe_result.kind is not probe.kind:
            raise PackageUpdateHealthError(
                "host result describes a different probe kind than the frozen one"
            )
        if probe_result.target != probe.target:
            raise PackageUpdateHealthError(
                "host result describes a different probe target than the frozen one"
            )
        outcome = probe_result.outcome
        if not isinstance(outcome, HealthProbeOutcome):
            raise PackageUpdateHealthError("host returned an unknown probe outcome")
        reason = probe_result.reason
        if reason not in HOST_PROBE_REASONS:
            raise PackageUpdateHealthError(
                "host returned a probe reason outside its bounded taxonomy"
            )
        try:
            require_health_probe_semantics(probe.kind, outcome, reason)
        except HealthProbeSemanticError as exc:
            raise PackageUpdateHealthError(f"host returned a probe {exc}") from exc
        observations.append(
            HealthProbeObservation(
                probe_index=probe.probe_index,
                kind=probe.kind,
                target=probe.target,
                outcome=outcome,
                reason=reason,
            )
        )
    if result.evaluation_status is HealthEvaluationStatus.DECISIVE and any(
        observation.outcome is HealthProbeOutcome.UNKNOWN
        for observation in observations
    ):
        raise PackageUpdateHealthError(
            "host claimed a decisive round while a probe remained unknown"
        )
    return tuple(observations)


def expected_health_host_probes(
    probes: Sequence[PackageUpdateJobHealthProbe],
) -> tuple[dict[str, Any], ...]:
    """Render one job's frozen probe set as the host request's probe list."""

    return tuple(
        {
            "index": probe.probe_index,
            "kind": probe.kind.value,
            "target": probe.target,
        }
        for probe in probes
    )
