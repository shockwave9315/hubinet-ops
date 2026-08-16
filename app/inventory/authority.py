"""Typed mutation boundary for Hubinet Ops 0.5 persistent authority."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
import sqlite3
import uuid

from .attestation import (
    ANCHOR_KIND_PVE_ROOT_CA_SHA256_FINGERPRINT,
    SourceAttestationEvidenceReader,
    SourceAttestationEvidenceReading,
    SourceAttestationReadOutcome,
)
from .canonicalization import (
    CANONICALIZATION_CONTRACT_VERSION,
    canonicalize_transport_locator,
)
from .models import (
    AttestationEvidenceTier,
    AttestationOperation,
    AttestationOutcome,
    AuthorityConflict,
    AuthorityInvariantError,
    AuthorityNotFound,
    DiscoveryRun,
    DiscoveryRunLifecycle,
    InventorySourceState,
    SourceAttestationEvent,
    SourceAttestationRelationshipGate,
    TierTwoEvaluationStatus,
)
from .discovery import (
    BaselineCompleteness,
    DiscoveryRunCompletionEvidence,
    NormalizedDiscoverySnapshot,
)
from .provider import PROVIDER_CONTRACT_VERSION
from .reconciliation import InventoryReconciler, ReconciliationSummary
from .store import InventoryAuthorityStore


@dataclass(frozen=True, slots=True)
class _AttestationContext:
    """Exact expected/current context compared across the ADR 0003 §19a gap.

    Deliberately excludes ``attestation_status``/anchor value: every status
    or anchor change in this module also bumps ``source_attestation_epoch``
    in the same atomic transaction, so epoch equality is already a strict
    proxy for "nothing security-relevant changed concurrently" -- exactly
    like the existing discovery-run CAS context. ``relationship_gate`` (ADR
    0003 §17) IS included: a concurrent operation that commits a durable
    mismatch between this read and this write must fence out the older,
    now-stale attempt exactly like every other context field.

    ``endpoint_id``/``canonical_transport_locator``/
    ``canonicalization_contract_version``/``transport_trust_revision``/
    ``endpoint_lifecycle`` describe whichever single endpoint is under
    evaluation -- the source's active endpoint for enrollment/re-
    attestation/accept, or an explicit candidate endpoint for a candidate
    check (ADR 0003 §14/§19a). ``endpoint_lifecycle`` is always ``active``
    for the former (nothing to race against) and is the load-bearing field
    that fences a candidate retired/made-ineligible mid-read (§29 negative
    witness 17) for the latter.
    """

    source_config_revision: int
    endpoint_id: str
    canonical_transport_locator: str
    canonicalization_contract_version: int
    transport_trust_revision: int
    source_attestation_epoch: int
    relationship_gate: SourceAttestationRelationshipGate
    endpoint_lifecycle: str


class InventoryAuthority:
    """Expose explicit dormant Phase 1 source and reconciliation transitions."""

    def __init__(
        self,
        store: InventoryAuthorityStore,
        *,
        now: Callable[[], datetime] | None = None,
        _test_migration_contracts: Mapping[int, Callable[[str], str]] | None = None,
    ) -> None:
        self._store = store
        self._now = now or (lambda: datetime.now(UTC))
        self._migration_contracts = dict(_test_migration_contracts or {})

    def create_inventory_source(
        self,
        *,
        provider_kind: str,
        display_name: str,
        credential_reference: str,
        transport_locator: str,
        freshness_duration_seconds: int = 300,
        provider_contract_version: int = PROVIDER_CONTRACT_VERSION,
    ) -> InventorySourceState:
        """Atomically establish one source, active endpoint, and initial health."""

        provider_kind = _require_text(provider_kind, "provider_kind", max_length=100)
        display_name = _require_text(display_name, "display_name", max_length=300)
        credential_reference = _require_credential_reference(credential_reference)
        freshness_duration = _require_positive_integer(
            freshness_duration_seconds, "freshness_duration_seconds"
        )
        contract_version = _require_positive_integer(
            provider_contract_version, "provider_contract_version"
        )
        if contract_version != PROVIDER_CONTRACT_VERSION:
            raise ValueError("provider contract version is unsupported")
        canonical_locator = canonicalize_transport_locator(transport_locator)
        source_id = _new_uuid()
        endpoint_id = _new_uuid()
        created_at = _timestamp(self._now())

        with self._store._transaction() as connection:
            backend = connection.execute(
                "SELECT backend_instance_id FROM backend_instance"
            ).fetchall()
            if len(backend) != 1:
                raise AuthorityInvariantError(
                    "authority database must contain exactly one backend instance"
                )
            connection.execute(
                "INSERT INTO inventory_sources("
                "inventory_source_id, backend_instance_id, provider_kind, display_name, "
                "credential_reference, created_at, source_config_revision, "
                "last_issued_run_sequence, last_committed_run_sequence, "
                "active_discovery_run_id, provider_contract_version, "
                "freshness_duration_seconds, facts_json) "
                "VALUES(?, ?, ?, ?, ?, ?, 1, 0, NULL, NULL, ?, ?, '{}')",
                (
                    source_id,
                    str(backend[0]["backend_instance_id"]),
                    provider_kind,
                    display_name,
                    credential_reference,
                    created_at,
                    contract_version,
                    freshness_duration,
                ),
            )
            self._insert_initial_endpoint(
                connection,
                source_id=source_id,
                endpoint_id=endpoint_id,
                canonical_locator=canonical_locator,
                created_at=created_at,
            )
            self._insert_initial_health(connection, source_id=source_id)
            self._insert_initial_attestation_state(connection, source_id=source_id)
            self._bump_global_revisions(
                connection, inventory_changed=True, published_changed=True
            )

        return self._store.source_state(source_id)

    def rename_inventory_source(
        self, inventory_source_id: str, display_name: str
    ) -> InventorySourceState:
        """Change presentation only, preserving source configuration and run context."""

        source_id = _require_uuid(inventory_source_id, "inventory_source_id")
        name = _require_text(display_name, "display_name", max_length=300)
        with self._store._transaction() as connection:
            row = self._require_source_row(connection, source_id)
            if str(row["display_name"]) != name:
                connection.execute(
                    "UPDATE inventory_sources SET display_name=? "
                    "WHERE inventory_source_id=?",
                    (name, source_id),
                )
                self._bump_global_revisions(
                    connection, inventory_changed=True, published_changed=True
                )
        return self._store.source_state(source_id)

    def issue_discovery_run(
        self,
        inventory_source_id: str,
        provider_contract_version: int,
    ) -> DiscoveryRun:
        """Durably allocate and own one per-source run before provider I/O."""

        source_id = _require_uuid(inventory_source_id, "inventory_source_id")
        contract_version = _require_positive_integer(
            provider_contract_version, "provider_contract_version"
        )
        run_id = _new_uuid()
        issued_at = _timestamp(self._now())

        with self._store._transaction() as connection:
            source = self._require_source_row(connection, source_id)
            if contract_version != PROVIDER_CONTRACT_VERSION:
                raise ValueError("provider contract version is unsupported")
            if contract_version != int(source["provider_contract_version"]):
                raise AuthorityConflict(
                    "provider contract version does not match current source configuration"
                )
            if source["active_discovery_run_id"] is not None:
                raise AuthorityConflict(
                    "inventory source already has an active discovery run"
                )
            endpoint_rows = connection.execute(
                "SELECT * FROM source_endpoints "
                "WHERE inventory_source_id=? AND lifecycle='active'",
                (source_id,),
            ).fetchall()
            if len(endpoint_rows) != 1:
                raise AuthorityInvariantError(
                    "inventory source must have exactly one active endpoint"
                )
            endpoint = endpoint_rows[0]
            attestation = self._require_attestation_row(connection, source_id)
            old_sequence = int(source["last_issued_run_sequence"])
            new_sequence = old_sequence + 1
            updated = connection.execute(
                "UPDATE inventory_sources SET last_issued_run_sequence=? "
                "WHERE inventory_source_id=? AND last_issued_run_sequence=? "
                "AND active_discovery_run_id IS NULL",
                (new_sequence, source_id, old_sequence),
            )
            if updated.rowcount != 1:
                raise AuthorityConflict("discovery run issuance lost source ownership")
            connection.execute(
                "INSERT INTO discovery_runs("
                "run_id, inventory_source_id, discovery_run_sequence, issued_at, "
                "expected_source_config_revision, expected_endpoint_id, "
                "expected_canonical_transport_locator, "
                "expected_canonicalization_contract_version, "
                "expected_transport_trust_revision, expected_source_attestation_epoch, "
                "provider_contract_version, lifecycle, "
                "terminalized_at, terminal_reason, completed_at, provider_outcome, "
                "observed_at, normalized_snapshot_hash) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'issued', "
                "NULL, NULL, NULL, NULL, NULL, NULL)",
                (
                    run_id,
                    source_id,
                    new_sequence,
                    issued_at,
                    int(source["source_config_revision"]),
                    str(endpoint["endpoint_id"]),
                    str(endpoint["canonical_transport_locator"]),
                    int(endpoint["canonicalization_contract_version"]),
                    int(endpoint["transport_trust_revision"]),
                    int(attestation["source_attestation_epoch"]),
                    contract_version,
                ),
            )
            self._claim_active_run(connection, source_id=source_id, run_id=run_id)
            self._bump_global_revisions(
                connection, inventory_changed=False, published_changed=True
            )

        return self._store.discovery_run(run_id)

    def mark_discovery_run_running(
        self, inventory_source_id: str, run_id: str
    ) -> DiscoveryRun:
        """Move the exact issued owner to running without changing its context."""

        source_id = _require_uuid(inventory_source_id, "inventory_source_id")
        canonical_run_id = _require_uuid(run_id, "run_id")
        with self._store._transaction() as connection:
            source = self._require_source_row(connection, source_id)
            run = self._require_run_row(connection, canonical_run_id)
            self._require_exact_run_owner(source, run, source_id, canonical_run_id)
            if run["lifecycle"] != DiscoveryRunLifecycle.ISSUED.value:
                raise AuthorityConflict("only an issued discovery run can begin running")
            connection.execute(
                "UPDATE discovery_runs SET lifecycle='running' WHERE run_id=?",
                (canonical_run_id,),
            )
            self._bump_global_revisions(
                connection, inventory_changed=False, published_changed=True
            )
        return self._store.discovery_run(canonical_run_id)

    def abandon_discovery_run(
        self,
        inventory_source_id: str,
        run_id: str,
        *,
        reason: str,
    ) -> DiscoveryRun:
        """One-time fence and release of the exact active incomplete run."""

        source_id = _require_uuid(inventory_source_id, "inventory_source_id")
        canonical_run_id = _require_uuid(run_id, "run_id")
        terminal_reason = _require_text(reason, "reason", max_length=500)
        terminalized_at = _timestamp(self._now())

        with self._store._transaction() as connection:
            source = self._require_source_row(connection, source_id)
            run = self._require_run_row(connection, canonical_run_id)
            self._require_exact_run_owner(source, run, source_id, canonical_run_id)
            if run["lifecycle"] not in {
                DiscoveryRunLifecycle.ISSUED.value,
                DiscoveryRunLifecycle.RUNNING.value,
            }:
                raise AuthorityConflict("discovery run is already terminal")

            released = connection.execute(
                "UPDATE inventory_sources SET active_discovery_run_id=NULL "
                "WHERE inventory_source_id=? AND active_discovery_run_id=?",
                (source_id, canonical_run_id),
            )
            if released.rowcount != 1:
                raise AuthorityConflict("discovery run no longer owns the source")
            self._terminalize_abandoned_run(
                connection,
                run_id=canonical_run_id,
                terminalized_at=terminalized_at,
                reason=terminal_reason,
            )
            self._bump_global_revisions(
                connection, inventory_changed=False, published_changed=True
            )

        return self._store.discovery_run(canonical_run_id)

    def finalize_successful_discovery_run(
        self,
        inventory_source_id: str,
        run_id: str,
        snapshot: NormalizedDiscoverySnapshot,
    ) -> ReconciliationSummary:
        """Atomically finalize, reconcile, publish health, and release exact ownership."""

        source_id = _require_uuid(inventory_source_id, "inventory_source_id")
        canonical_run_id = _require_uuid(run_id, "run_id")
        committed_at = _timestamp(self._now())
        completion_evidence = DiscoveryRunCompletionEvidence.from_snapshot(snapshot)
        context_rejected = False
        summary = ReconciliationSummary()

        with self._store._transaction() as connection:
            source = self._require_source_row(connection, source_id)
            run = self._require_run_row(connection, canonical_run_id)
            self._require_exact_run_owner(source, run, source_id, canonical_run_id)
            self._require_nonterminal_run(run)
            self._validate_snapshot_issuance(snapshot, run)
            endpoint = self._require_active_endpoint_row(connection, source_id)
            attestation = self._require_attestation_row(connection, source_id)

            if not self._run_context_is_current(source, endpoint, attestation, run):
                self._release_run(connection, source_id, canonical_run_id)
                self._complete_run(
                    connection,
                    run,
                    completed_at=committed_at,
                    outcome="invalid",
                    terminal_reason="completion_context_changed",
                    evidence=completion_evidence,
                    completion_source=None,
                    completion_endpoint=None,
                )
                self._after_run_completion(
                    connection, run_id=canonical_run_id
                )
                self._update_completion_provenance(
                    connection,
                    source_id=source_id,
                    sequence=int(run["discovery_run_sequence"]),
                    outcome="invalid",
                )
                self._bump_global_revisions(
                    connection, inventory_changed=False, published_changed=True
                )
                context_rejected = True
            else:
                sequence = int(run["discovery_run_sequence"])
                committed_sequence = source["last_committed_run_sequence"]
                health = connection.execute(
                    "SELECT * FROM source_runtime_health WHERE inventory_source_id=?",
                    (source_id,),
                ).fetchone()
                if health is None:
                    raise AuthorityInvariantError("source runtime health is missing")
                if committed_sequence is not None and sequence <= int(committed_sequence):
                    raise AuthorityConflict("successful run does not advance committed sequence")
                if health["last_health_run_sequence"] is not None and sequence <= int(health["last_health_run_sequence"]):
                    raise AuthorityConflict("successful run does not advance health sequence")

                summary = InventoryReconciler(new_uuid=_new_uuid).reconcile(
                    connection, snapshot, committed_at=committed_at
                )
                self._after_reconciliation(connection, snapshot)
                freshness_reference = _freshness_reference_at(snapshot)
                deadline = freshness_reference + timedelta(
                    seconds=int(source["freshness_duration_seconds"])
                )
                now = _parse_timestamp(committed_at, "committed_at")
                if freshness_reference > now:
                    freshness = "stale"
                    origin = "discovery_run"
                    reason = "clock_anomaly_future_observation"
                elif now >= deadline:
                    freshness = "stale"
                    origin = "time_expiry"
                    reason = "freshness_deadline_elapsed_before_commit"
                else:
                    freshness = "fresh"
                    origin = "discovery_run"
                    reason = "successful_authoritative_reconciliation"
                connection.execute(
                    "UPDATE inventory_sources SET last_committed_run_sequence=? "
                    "WHERE inventory_source_id=?",
                    (sequence, source_id),
                )
                connection.execute(
                    "UPDATE source_runtime_health SET health='healthy', freshness=?, "
                    "health_origin=?, health_reason=?, latest_completed_run_sequence=?, "
                    "latest_completed_outcome='success', last_health_run_sequence=?, "
                    "last_run_health_outcome='success', last_successful_observed_at=?, "
                    "freshness_reference_at=?, freshness_valid_until=?, "
                    "committed_source_config_revision=?, committed_endpoint_id=?, "
                    "committed_canonical_transport_locator=?, "
                    "committed_canonicalization_contract_version=?, "
                    "committed_transport_trust_revision=?, "
                    "committed_source_attestation_epoch=? WHERE inventory_source_id=?",
                    (
                        freshness,
                        origin,
                        reason,
                        sequence,
                        sequence,
                        snapshot.observed_at,
                        freshness_reference.isoformat(),
                        deadline.isoformat(),
                        int(source["source_config_revision"]),
                        str(endpoint["endpoint_id"]),
                        str(endpoint["canonical_transport_locator"]),
                        int(endpoint["canonicalization_contract_version"]),
                        int(endpoint["transport_trust_revision"]),
                        int(run["expected_source_attestation_epoch"]),
                        source_id,
                    ),
                )
                self._after_health_update(connection, snapshot)
                self._release_run(connection, source_id, canonical_run_id)
                self._complete_run(
                    connection,
                    run,
                    completed_at=committed_at,
                    outcome="success",
                    terminal_reason=None,
                    evidence=completion_evidence,
                    completion_source=source,
                    completion_endpoint=endpoint,
                )
                self._after_run_completion(
                    connection, run_id=canonical_run_id
                )
                self._bump_global_revisions(
                    connection, inventory_changed=True, published_changed=True
                )

        if context_rejected:
            raise AuthorityConflict(
                "discovery run context changed; completion audited without reconciliation"
            )
        return summary

    def finalize_failed_discovery_run(
        self,
        inventory_source_id: str,
        run_id: str,
        *,
        completion_evidence: DiscoveryRunCompletionEvidence,
        reason: str,
    ) -> DiscoveryRun:
        """Finalize one non-success run and apply health only to its exact context."""

        if not isinstance(completion_evidence, DiscoveryRunCompletionEvidence):
            raise TypeError("failed discovery completion requires typed evidence")
        outcome = completion_evidence.baseline_completeness
        if outcome is BaselineCompleteness.COMPLETE:
            raise ValueError("complete outcome must use successful reconciliation")
        source_id = _require_uuid(inventory_source_id, "inventory_source_id")
        canonical_run_id = _require_uuid(run_id, "run_id")
        health_reason = _require_text(reason, "reason", max_length=500)
        completed_at = _timestamp(self._now())
        if completion_evidence.observed_at is not None:
            _parse_timestamp(completion_evidence.observed_at, "observed_at")

        with self._store._transaction() as connection:
            source = self._require_source_row(connection, source_id)
            run = self._require_run_row(connection, canonical_run_id)
            self._require_exact_run_owner(source, run, source_id, canonical_run_id)
            self._require_nonterminal_run(run)
            endpoint = self._require_active_endpoint_row(connection, source_id)
            attestation = self._require_attestation_row(connection, source_id)
            sequence = int(run["discovery_run_sequence"])
            applicable = self._run_context_is_current(source, endpoint, attestation, run)
            self._release_run(connection, source_id, canonical_run_id)
            self._complete_run(
                connection,
                run,
                completed_at=completed_at,
                outcome=outcome.value,
                terminal_reason=health_reason,
                evidence=completion_evidence,
                completion_source=None,
                completion_endpoint=None,
            )
            self._after_run_completion(connection, run_id=canonical_run_id)
            if applicable:
                health_row = connection.execute(
                    "SELECT last_health_run_sequence FROM source_runtime_health "
                    "WHERE inventory_source_id=?",
                    (source_id,),
                ).fetchone()
                if health_row is None:
                    raise AuthorityInvariantError("source runtime health is missing")
                old_health_sequence = health_row["last_health_run_sequence"]
                if old_health_sequence is None or sequence > int(old_health_sequence):
                    health = {
                        BaselineCompleteness.SOURCE_UNAVAILABLE: "source_unavailable",
                        BaselineCompleteness.CONFIGURATION_ERROR: "configuration_error",
                        BaselineCompleteness.PARTIAL: "degraded",
                        BaselineCompleteness.INVALID: "degraded",
                    }[outcome]
                    connection.execute(
                        "UPDATE source_runtime_health SET health=?, freshness='stale', "
                        "health_origin='discovery_run', health_reason=?, "
                        "last_health_run_sequence=?, last_run_health_outcome=? "
                        "WHERE inventory_source_id=?",
                        (health, health_reason, sequence, outcome.value, source_id),
                    )
            self._update_completion_provenance(
                connection, source_id=source_id, sequence=sequence, outcome=outcome.value
            )
            self._bump_global_revisions(
                connection, inventory_changed=False, published_changed=True
            )
        return self._store.discovery_run(canonical_run_id)

    def enroll_source_attestation(
        self,
        inventory_source_id: str,
        *,
        endpoint_id: str,
        actor: str,
        evidence_reader: SourceAttestationEvidenceReader,
    ) -> SourceAttestationEvent:
        """Explicit initial enrollment: not_yet_attested -> attested (ADR 0003 §12).

        Never invoked by discovery. Requires the source's exact current
        active endpoint as ``endpoint_id`` -- candidate endpoint attestation
        remains a separate operation (Commit 4), not this one.
        """

        return self._perform_attestation_read_and_transition(
            inventory_source_id,
            endpoint_id=endpoint_id,
            actor=actor,
            evidence_reader=evidence_reader,
            operation=AttestationOperation.ENROLLMENT,
            accept_new_anchor=True,
        )

    def reattest_source(
        self,
        inventory_source_id: str,
        *,
        endpoint_id: str,
        actor: str,
        evidence_reader: SourceAttestationEvidenceReader,
    ) -> SourceAttestationEvent:
        """Explicit re-attestation of an already-attested source (ADR 0003 §16).

        A same-anchor read is a reconfirmation (audit only, epoch
        unchanged). A different anchor is recorded as a mismatch and never
        silently accepted -- accepting a new anchor requires the operator to
        separately call :meth:`accept_source_attestation_anchor_change`.
        """

        return self._perform_attestation_read_and_transition(
            inventory_source_id,
            endpoint_id=endpoint_id,
            actor=actor,
            evidence_reader=evidence_reader,
            operation=AttestationOperation.REATTESTATION,
            accept_new_anchor=False,
        )

    def accept_source_attestation_anchor_change(
        self,
        inventory_source_id: str,
        *,
        endpoint_id: str,
        actor: str,
        evidence_reader: SourceAttestationEvidenceReader,
    ) -> SourceAttestationEvent:
        """Explicit operator decision accepting a freshly read, different
        anchor as a deliberate environment change (ADR 0003 §16, case G).

        This never happens automatically merely because :meth:`reattest_source`
        observed a mismatch -- the operator must call this method separately.
        If the fresh read happens to match the currently enrolled anchor
        after all, this degrades to an ordinary reconfirmation (ADR 0003
        §20's deterministic epoch rule is not a caller choice).
        """

        return self._perform_attestation_read_and_transition(
            inventory_source_id,
            endpoint_id=endpoint_id,
            actor=actor,
            evidence_reader=evidence_reader,
            operation=AttestationOperation.REATTESTATION,
            accept_new_anchor=True,
        )

    def revoke_source_attestation(
        self, inventory_source_id: str, *, actor: str, reason: str
    ) -> SourceAttestationEvent:
        """Explicit operator-driven revocation/reset (ADR 0003 §20).

        A pure local decision: no remote evidence read, since there is
        nothing to corroborate -- the operator is withdrawing trust, not
        asserting a new anchor. Transitions attested/N -> not_yet_attested/
        N+1 (never yet_attested/0 -- the epoch token never decreases or
        resets) so the source is fail-closed for every attestation-gated
        operation until an explicit new enrollment.
        """

        source_id = _require_uuid(inventory_source_id, "inventory_source_id")
        actor_text = _require_text(actor, "actor", max_length=200)
        revoke_reason = _require_text(reason, "reason", max_length=500)
        attempted_at = _timestamp(self._now())
        event_id = _new_uuid()

        with self._store._transaction() as connection:
            source = self._require_source_row(connection, source_id)
            endpoint = self._require_active_endpoint_row(connection, source_id)
            attestation = self._require_attestation_row(connection, source_id)
            if attestation["attestation_status"] != "attested":
                raise AuthorityConflict("source is not currently attested")
            previous_epoch = int(attestation["source_attestation_epoch"])
            new_epoch = previous_epoch + 1
            expected = _capture_attestation_context(source, endpoint, attestation)

            self._fence_active_run_for_attestation_transition(
                connection, source, reason="attestation_epoch_transition"
            )
            connection.execute(
                "UPDATE source_attestation_state SET attestation_status='not_yet_attested', "
                "source_attestation_epoch=?, anchor_kind=NULL, anchor_value=NULL, "
                "evidence_tier=NULL, tier2_evaluation=NULL, relationship_gate='clear', "
                "accepted_at=NULL, accepted_by=NULL, evaluated_endpoint_id=NULL "
                "WHERE inventory_source_id=?",
                (new_epoch, source_id),
            )
            self._insert_attestation_event(
                connection,
                event_id=event_id,
                source_id=source_id,
                target_endpoint_id=str(endpoint["endpoint_id"]),
                operation=AttestationOperation.REVOCATION,
                actor=actor_text,
                attempted_at=attempted_at,
                expected=expected,
                outcome=AttestationOutcome.ACCEPTED,
                evidence_tier=None,
                tier2_evaluation=None,
                asserted_anchor_kind=None,
                asserted_anchor_value=None,
                endpoint_lifecycle_at_check=None,
                previous_epoch=previous_epoch,
                resulting_epoch=new_epoch,
                resulting_relationship_gate=SourceAttestationRelationshipGate.CLEAR,
                reason=revoke_reason,
            )
            self._after_attestation_transition(connection, event_id=event_id)
            self._mark_controlled_context_transition(
                connection, source_id, "source_attestation_revoked"
            )
            self._bump_global_revisions(
                connection, inventory_changed=False, published_changed=True
            )
        return self._store.attestation_event(event_id)

    def check_candidate_attestation(
        self,
        inventory_source_id: str,
        *,
        endpoint_id: str,
        actor: str,
        evidence_reader: SourceAttestationEvidenceReader,
    ) -> SourceAttestationEvent:
        """Explicit operator-driven candidate endpoint attestation check
        (ADR 0003 §14).

        A successful check persists an epoch-scoped candidate attestation
        binding as retained prerequisite evidence only -- it is NECESSARY
        but never SUFFICIENT for any future activation/failover ADR (§15).
        It never activates, promotes, or replaces the active endpoint,
        never changes candidate lifecycle, never fences an ordinary
        discovery run, and never grants workload/resource trust or
        mutation authority (§28). Discovery never invokes this itself.

        Attestation-gated preconditions (checked before any remote I/O):
        the source must currently be ``attested`` with an enrolled anchor,
        and its ``relationship_gate`` must be ``clear`` -- a source that is
        ``not_yet_attested`` or has an unresolved
        ``mismatch_pending_reattestation`` cannot receive a candidate
        binding (§17, §29 negative witness 1).
        """

        source_id = _require_uuid(inventory_source_id, "inventory_source_id")
        candidate_endpoint_id = _require_uuid(endpoint_id, "endpoint_id")
        actor_text = _require_text(actor, "actor", max_length=200)

        # ---- PHASE 1: authoritative pre-read (no held write transaction) ----
        with self._store._read_transaction() as connection:
            source = self._require_source_row(connection, source_id)
            active_endpoint = self._require_active_endpoint_row(connection, source_id)
            candidate = self._require_endpoint_row(
                connection, source_id, candidate_endpoint_id
            )
            attestation = self._require_attestation_row(connection, source_id)

        if candidate_endpoint_id == str(active_endpoint["endpoint_id"]):
            raise ValueError(
                "endpoint_id must be a candidate endpoint, not the source's "
                "current active endpoint"
            )
        if str(candidate["lifecycle"]) != "candidate":
            raise AuthorityConflict(
                "candidate endpoint is not in an eligible admissibility state"
            )
        if str(attestation["attestation_status"]) != "attested":
            raise AuthorityConflict(
                "source has no enrolled attestation anchor to check candidates against"
            )
        if str(attestation["relationship_gate"]) != "clear":
            raise AuthorityConflict(
                "source has an unresolved attestation mismatch pending re-attestation"
            )

        expected = _capture_attestation_context(source, candidate, attestation)
        enrolled_anchor_kind = attestation["anchor_kind"]
        enrolled_anchor_value = attestation["anchor_value"]
        previous_epoch = expected.source_attestation_epoch

        # ---- PHASE 2: remote evidence read, OUTSIDE any write transaction ----
        # Reads the CANDIDATE's own endpoint/locator -- never the active one.
        attempted_at = _timestamp(self._now())
        try:
            reading = evidence_reader.read(
                inventory_source_id=source_id,
                endpoint_id=expected.endpoint_id,
                canonical_transport_locator=expected.canonical_transport_locator,
                enrolled_anchor_kind=enrolled_anchor_kind,
                enrolled_anchor_value=enrolled_anchor_value,
            )
        except Exception:
            reading = SourceAttestationEvidenceReading(
                outcome=SourceAttestationReadOutcome.UNAVAILABLE
            )
            reader_raised = True
        else:
            reader_raised = False
            if not isinstance(reading, SourceAttestationEvidenceReading):
                raise TypeError(
                    "evidence reader must return a typed SourceAttestationEvidenceReading"
                )

        event_id = _new_uuid()
        context_rejected = False

        # ---- PHASE 3: authoritative write transaction; CAS-revalidate ----
        with self._store._transaction() as connection:
            source = self._require_source_row(connection, source_id)
            candidate = self._require_endpoint_row(
                connection, source_id, candidate_endpoint_id
            )
            attestation = self._require_attestation_row(connection, source_id)
            current = _capture_attestation_context(source, candidate, attestation)

            if current != expected:
                # Covers every §29 negative-witness-17-class race: candidate
                # retired/made-ineligible, locator/canonicalization/transport-
                # trust changed, source_config_revision changed, epoch
                # changed, or relationship_gate changed underneath the read.
                self._insert_attestation_event(
                    connection,
                    event_id=event_id,
                    source_id=source_id,
                    target_endpoint_id=candidate_endpoint_id,
                    operation=AttestationOperation.CANDIDATE_CHECK,
                    actor=actor_text,
                    attempted_at=attempted_at,
                    expected=expected,
                    outcome=AttestationOutcome.STALE_CAS,
                    evidence_tier=None,
                    tier2_evaluation=None,
                    asserted_anchor_kind=None,
                    asserted_anchor_value=None,
                    endpoint_lifecycle_at_check=expected.endpoint_lifecycle,
                    previous_epoch=previous_epoch,
                    resulting_epoch=None,
                    resulting_relationship_gate=None,
                    reason="attestation_context_changed_between_read_and_write",
                )
                context_rejected = True
            else:
                (
                    outcome,
                    evidence_tier,
                    tier2_evaluation,
                    asserted_kind,
                    asserted_value,
                    resulting_relationship_gate,
                    audit_reason,
                ) = _classify_candidate_attestation_reading(
                    reading,
                    enrolled_anchor_kind=enrolled_anchor_kind,
                    enrolled_anchor_value=enrolled_anchor_value,
                )
                if reader_raised:
                    audit_reason = "attestation_evidence_reader_raised_exception"
                self._insert_attestation_event(
                    connection,
                    event_id=event_id,
                    source_id=source_id,
                    target_endpoint_id=candidate_endpoint_id,
                    operation=AttestationOperation.CANDIDATE_CHECK,
                    actor=actor_text,
                    attempted_at=attempted_at,
                    expected=expected,
                    outcome=outcome,
                    evidence_tier=evidence_tier,
                    tier2_evaluation=tier2_evaluation,
                    asserted_anchor_kind=asserted_kind,
                    asserted_anchor_value=asserted_value,
                    endpoint_lifecycle_at_check=expected.endpoint_lifecycle,
                    previous_epoch=previous_epoch,
                    resulting_epoch=None,
                    resulting_relationship_gate=resulting_relationship_gate,
                    reason=audit_reason,
                )
                if outcome is AttestationOutcome.ACCEPTED:
                    connection.execute(
                        "INSERT INTO candidate_attestation_bindings("
                        "binding_id, inventory_source_id, endpoint_id, "
                        "source_attestation_epoch, evidence_tier, tier2_evaluation, "
                        "endpoint_lifecycle_at_check, canonical_transport_locator, "
                        "canonicalization_contract_version, transport_trust_revision, "
                        "matched_at, created_by, event_id) "
                        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            _new_uuid(),
                            source_id,
                            candidate_endpoint_id,
                            previous_epoch,
                            evidence_tier.value,
                            tier2_evaluation.value,
                            expected.endpoint_lifecycle,
                            expected.canonical_transport_locator,
                            expected.canonicalization_contract_version,
                            expected.transport_trust_revision,
                            attempted_at,
                            actor_text,
                            event_id,
                        ),
                    )
                    self._after_attestation_transition(connection, event_id=event_id)
                elif outcome is AttestationOutcome.MISMATCH:
                    # ADR 0003 §17: durably gate future attestation-gated
                    # actions; never touches epoch/anchor/health/candidate
                    # lifecycle/active endpoint.
                    connection.execute(
                        "UPDATE source_attestation_state SET relationship_gate=? "
                        "WHERE inventory_source_id=?",
                        (resulting_relationship_gate.value, source_id),
                    )
                    self._after_attestation_transition(connection, event_id=event_id)

        if context_rejected:
            raise AuthorityConflict(
                "attestation evidence context changed; audited without a state transition"
            )
        return self._store.attestation_event(event_id)

    def rotate_credential_reference(
        self, inventory_source_id: str, credential_reference: str
    ) -> InventorySourceState:
        return self._change_source_configuration(
            inventory_source_id,
            column="credential_reference",
            value=_require_credential_reference(credential_reference),
            reason="credential_reference_rotated",
        )

    def configure_freshness_duration(
        self, inventory_source_id: str, freshness_duration_seconds: int
    ) -> InventorySourceState:
        return self._change_source_configuration(
            inventory_source_id,
            column="freshness_duration_seconds",
            value=_require_positive_integer(freshness_duration_seconds, "freshness_duration_seconds"),
            reason="freshness_duration_changed",
        )

    def rotate_transport_trust(self, inventory_source_id: str) -> InventorySourceState:
        source_id = _require_uuid(inventory_source_id, "inventory_source_id")
        with self._store._transaction() as connection:
            source = self._require_source_row(connection, source_id)
            self._require_no_active_run(source)
            endpoint = self._require_active_endpoint_row(connection, source_id)
            connection.execute(
                "UPDATE source_endpoints SET transport_trust_revision=transport_trust_revision+1 "
                "WHERE endpoint_id=?",
                (str(endpoint["endpoint_id"]),),
            )
            connection.execute(
                "UPDATE inventory_sources SET source_config_revision=source_config_revision+1 "
                "WHERE inventory_source_id=?",
                (source_id,),
            )
            self._mark_controlled_context_transition(
                connection, source_id, "transport_trust_rotated"
            )
            self._bump_global_revisions(
                connection, inventory_changed=False, published_changed=True
            )
        return self._store.source_state(source_id)

    def migrate_canonicalization_contract(
        self, inventory_source_id: str, target_version: int
    ) -> InventorySourceState:
        """Atomically migrate a retained source namespace through a registered contract."""

        source_id = _require_uuid(inventory_source_id, "inventory_source_id")
        version = _require_positive_integer(target_version, "target_version")
        canonicalizer = self._migration_contracts.get(version)
        if canonicalizer is None:
            raise ValueError("canonicalization migration contract is not registered")
        migration_id = _new_uuid()
        migrated_at = _timestamp(self._now())
        with self._store._transaction() as connection:
            source = self._require_source_row(connection, source_id)
            self._require_no_active_run(source)
            endpoints = connection.execute(
                "SELECT * FROM source_endpoints WHERE inventory_source_id=? ORDER BY endpoint_id",
                (source_id,),
            ).fetchall()
            if not endpoints:
                raise AuthorityInvariantError("source has no retained endpoint namespace")
            pairs: list[tuple[sqlite3.Row, str]] = []
            seen: set[str] = set()
            for endpoint in endpoints:
                new_locator = canonicalizer(str(endpoint["canonical_transport_locator"]))
                if not isinstance(new_locator, str) or not new_locator.strip():
                    raise ValueError("migration produced an invalid canonical locator")
                if new_locator in seen:
                    raise AuthorityConflict("canonicalization migration creates a retained locator collision")
                seen.add(new_locator)
                pairs.append((endpoint, new_locator))
            for endpoint, new_locator in pairs:
                connection.execute(
                    "INSERT INTO canonicalization_migrations VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        migration_id,
                        source_id,
                        str(endpoint["endpoint_id"]),
                        int(endpoint["canonicalization_contract_version"]),
                        str(endpoint["canonical_transport_locator"]),
                        version,
                        new_locator,
                        migrated_at,
                    ),
                )
                connection.execute(
                    "UPDATE source_endpoints SET canonical_transport_locator=?, "
                    "canonicalization_contract_version=? WHERE endpoint_id=?",
                    (new_locator, version, str(endpoint["endpoint_id"])),
                )
            connection.execute(
                "UPDATE inventory_sources SET source_config_revision=source_config_revision+1 "
                "WHERE inventory_source_id=?",
                (source_id,),
            )
            self._mark_controlled_context_transition(
                connection, source_id, "canonicalization_contract_migrated"
            )
            self._bump_global_revisions(
                connection, inventory_changed=False, published_changed=True
            )
        return self._store.source_state(source_id)

    def _perform_attestation_read_and_transition(
        self,
        inventory_source_id: str,
        *,
        endpoint_id: str,
        actor: str,
        evidence_reader: SourceAttestationEvidenceReader,
        operation: AttestationOperation,
        accept_new_anchor: bool,
    ) -> SourceAttestationEvent:
        """Implement the ADR 0003 §19a three-phase pattern for one explicit,
        operator-driven attestation read (enrollment or re-attestation).

        Phase 1 captures one consistent expected context from a short
        read-only transaction. Phase 2 calls the trusted evidence reader
        entirely outside any write transaction. Phase 3 opens one
        authoritative write transaction, revalidates the captured context by
        CAS, and only then may accept a security transition -- atomically
        with its audit event and, if an epoch bump is accepted, the
        controlled source security-context transition (ADR 0003 §20).
        """

        source_id = _require_uuid(inventory_source_id, "inventory_source_id")
        target_endpoint_id = _require_uuid(endpoint_id, "endpoint_id")
        actor_text = _require_text(actor, "actor", max_length=200)

        # ---- PHASE 1: authoritative pre-read (no held write transaction) ----
        with self._store._read_transaction() as connection:
            source = self._require_source_row(connection, source_id)
            endpoint = self._require_active_endpoint_row(connection, source_id)
            attestation = self._require_attestation_row(connection, source_id)

        if str(endpoint["endpoint_id"]) != target_endpoint_id:
            raise ValueError(
                "endpoint_id must be the source's current active endpoint; "
                "candidate endpoint attestation is a separate operation"
            )
        current_status = str(attestation["attestation_status"])
        if operation is AttestationOperation.ENROLLMENT:
            if current_status != "not_yet_attested":
                raise AuthorityConflict(
                    "source is already attested; use reattest_source or "
                    "accept_source_attestation_anchor_change"
                )
        elif current_status != "attested":
            raise AuthorityConflict(
                "source has not yet been enrolled; use enroll_source_attestation"
            )

        expected = _capture_attestation_context(source, endpoint, attestation)
        enrolled_anchor_kind = attestation["anchor_kind"]
        enrolled_anchor_value = attestation["anchor_value"]
        previous_epoch = expected.source_attestation_epoch

        # ---- PHASE 2: remote evidence read, OUTSIDE any write transaction ----
        attempted_at = _timestamp(self._now())
        try:
            reading = evidence_reader.read(
                inventory_source_id=source_id,
                endpoint_id=expected.endpoint_id,
                canonical_transport_locator=expected.canonical_transport_locator,
                enrolled_anchor_kind=enrolled_anchor_kind,
                enrolled_anchor_value=enrolled_anchor_value,
            )
        except Exception:
            # ADR 0003 §18: a failed remote read is its own audited outcome,
            # never a silent bypass of the attestation audit contract. Never
            # persist the raised exception's own text -- it may contain
            # transport errors carrying URLs, credentials, or other private
            # material; only a fixed, deterministic, sanitized reason is
            # ever written (Finding 2).
            reading = SourceAttestationEvidenceReading(
                outcome=SourceAttestationReadOutcome.UNAVAILABLE
            )
            reader_raised = True
        else:
            reader_raised = False
            if not isinstance(reading, SourceAttestationEvidenceReading):
                raise TypeError(
                    "evidence reader must return a typed SourceAttestationEvidenceReading"
                )

        event_id = _new_uuid()
        context_rejected = False

        # ---- PHASE 3: authoritative write transaction; CAS-revalidate ----
        with self._store._transaction() as connection:
            source = self._require_source_row(connection, source_id)
            endpoint = self._require_active_endpoint_row(connection, source_id)
            attestation = self._require_attestation_row(connection, source_id)
            current = _capture_attestation_context(source, endpoint, attestation)

            if current != expected:
                self._insert_attestation_event(
                    connection,
                    event_id=event_id,
                    source_id=source_id,
                    target_endpoint_id=target_endpoint_id,
                    operation=operation,
                    actor=actor_text,
                    attempted_at=attempted_at,
                    expected=expected,
                    outcome=AttestationOutcome.STALE_CAS,
                    evidence_tier=None,
                    tier2_evaluation=None,
                    asserted_anchor_kind=None,
                    asserted_anchor_value=None,
                    endpoint_lifecycle_at_check=None,
                    previous_epoch=previous_epoch,
                    resulting_epoch=None,
                    resulting_relationship_gate=None,
                    reason="attestation_context_changed_between_read_and_write",
                )
                context_rejected = True
            else:
                (
                    outcome,
                    resulting_epoch,
                    evidence_tier,
                    tier2_evaluation,
                    asserted_kind,
                    asserted_value,
                    resulting_relationship_gate,
                    audit_reason,
                ) = _classify_attestation_reading(
                    reading,
                    operation=operation,
                    accept_new_anchor=accept_new_anchor,
                    enrolled_anchor_kind=enrolled_anchor_kind,
                    enrolled_anchor_value=enrolled_anchor_value,
                    previous_epoch=previous_epoch,
                )
                if reader_raised:
                    audit_reason = "attestation_evidence_reader_raised_exception"
                self._insert_attestation_event(
                    connection,
                    event_id=event_id,
                    source_id=source_id,
                    target_endpoint_id=target_endpoint_id,
                    operation=operation,
                    actor=actor_text,
                    attempted_at=attempted_at,
                    expected=expected,
                    outcome=outcome,
                    evidence_tier=evidence_tier,
                    tier2_evaluation=tier2_evaluation,
                    asserted_anchor_kind=asserted_kind,
                    asserted_anchor_value=asserted_value,
                    endpoint_lifecycle_at_check=None,
                    previous_epoch=previous_epoch,
                    resulting_epoch=resulting_epoch,
                    resulting_relationship_gate=resulting_relationship_gate,
                    reason=audit_reason,
                )
                if resulting_epoch is not None:
                    self._fence_active_run_for_attestation_transition(
                        connection, source, reason="attestation_epoch_transition"
                    )
                    connection.execute(
                        "UPDATE source_attestation_state SET attestation_status='attested', "
                        "source_attestation_epoch=?, anchor_kind=?, anchor_value=?, "
                        "evidence_tier=?, tier2_evaluation=?, relationship_gate=?, "
                        "accepted_at=?, accepted_by=?, evaluated_endpoint_id=? "
                        "WHERE inventory_source_id=?",
                        (
                            resulting_epoch,
                            asserted_kind,
                            asserted_value,
                            evidence_tier.value,
                            tier2_evaluation.value,
                            resulting_relationship_gate.value,
                            attempted_at,
                            actor_text,
                            target_endpoint_id,
                            source_id,
                        ),
                    )
                    self._after_attestation_transition(connection, event_id=event_id)
                    transition_reason = (
                        "source_attestation_enrolled"
                        if operation is AttestationOperation.ENROLLMENT
                        else "source_attestation_anchor_changed"
                    )
                    self._mark_controlled_context_transition(
                        connection, source_id, transition_reason
                    )
                    self._bump_global_revisions(
                        connection, inventory_changed=False, published_changed=True
                    )
                elif outcome is AttestationOutcome.MISMATCH:
                    # ADR 0003 §17: durably gate every future attestation-
                    # gated action without touching epoch/anchor/health/
                    # revisions -- ordinary discovery remains unaffected.
                    connection.execute(
                        "UPDATE source_attestation_state SET relationship_gate=? "
                        "WHERE inventory_source_id=?",
                        (resulting_relationship_gate.value, source_id),
                    )
                    self._after_attestation_transition(connection, event_id=event_id)

        if context_rejected:
            raise AuthorityConflict(
                "attestation evidence context changed; audited without a state transition"
            )
        return self._store.attestation_event(event_id)

    def _fence_active_run_for_attestation_transition(
        self, connection: sqlite3.Connection, source: sqlite3.Row, *, reason: str
    ) -> None:
        """Reuse the existing abandon/release pattern (ADR 0002) so an active
        run never remains a valid owner across an accepted epoch transition."""

        run_id = source["active_discovery_run_id"]
        if run_id is None:
            return
        terminalized_at = _timestamp(self._now())
        released = connection.execute(
            "UPDATE inventory_sources SET active_discovery_run_id=NULL "
            "WHERE inventory_source_id=? AND active_discovery_run_id=?",
            (str(source["inventory_source_id"]), run_id),
        )
        if released.rowcount != 1:
            raise AuthorityConflict("discovery run no longer owns the source")
        self._terminalize_abandoned_run(
            connection, run_id=run_id, terminalized_at=terminalized_at, reason=reason
        )

    def _after_attestation_transition(
        self, connection: sqlite3.Connection, *, event_id: str
    ) -> None:
        """Test injection seam inside an accepted attestation transition."""

    @staticmethod
    def _insert_attestation_event(
        connection: sqlite3.Connection,
        *,
        event_id: str,
        source_id: str,
        target_endpoint_id: str,
        operation: AttestationOperation,
        actor: str,
        attempted_at: str,
        expected: _AttestationContext,
        outcome: AttestationOutcome,
        evidence_tier: AttestationEvidenceTier | None,
        tier2_evaluation: TierTwoEvaluationStatus | None,
        asserted_anchor_kind: str | None,
        asserted_anchor_value: str | None,
        endpoint_lifecycle_at_check: str | None,
        previous_epoch: int,
        resulting_epoch: int | None,
        resulting_relationship_gate: SourceAttestationRelationshipGate | None,
        reason: str,
    ) -> None:
        connection.execute(
            "INSERT INTO source_attestation_events("
            "event_id, inventory_source_id, target_endpoint_id, operation, actor, "
            "attempted_at, expected_source_config_revision, expected_endpoint_id, "
            "expected_canonical_transport_locator, "
            "expected_canonicalization_contract_version, "
            "expected_transport_trust_revision, expected_source_attestation_epoch, "
            "expected_relationship_gate, "
            "outcome, evidence_tier, tier2_evaluation, asserted_anchor_kind, "
            "asserted_anchor_value, endpoint_lifecycle_at_check, previous_epoch, "
            "resulting_epoch, resulting_relationship_gate, reason) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event_id,
                source_id,
                target_endpoint_id,
                operation.value,
                actor,
                attempted_at,
                expected.source_config_revision,
                expected.endpoint_id,
                expected.canonical_transport_locator,
                expected.canonicalization_contract_version,
                expected.transport_trust_revision,
                expected.source_attestation_epoch,
                expected.relationship_gate.value,
                outcome.value,
                evidence_tier.value if evidence_tier is not None else None,
                tier2_evaluation.value if tier2_evaluation is not None else None,
                asserted_anchor_kind,
                asserted_anchor_value,
                endpoint_lifecycle_at_check,
                previous_epoch,
                resulting_epoch,
                (
                    resulting_relationship_gate.value
                    if resulting_relationship_gate is not None
                    else None
                ),
                reason,
            ),
        )

    def materialize_due_expiry(
        self,
        inventory_source_id: str,
        *,
        expected_run_sequence: int | None = None,
        expected_deadline: str | None = None,
    ) -> bool:
        source_id = _require_uuid(inventory_source_id, "inventory_source_id")
        with self._store._transaction() as connection:
            decision_time = self._authority_decision_time()
            return self._materialize_due_expiry_in_transaction(
                connection,
                source_id,
                now=decision_time,
                expected_run_sequence=expected_run_sequence,
                expected_deadline=expected_deadline,
            )

    def source_is_fresh_for_future_mutation(self, inventory_source_id: str) -> bool:
        """Read-only Phase 1C precondition guard; grants no mutation authority."""

        source_id = _require_uuid(inventory_source_id, "inventory_source_id")
        with self._store._transaction() as connection:
            decision_time = self._authority_decision_time()
            self._materialize_due_expiry_in_transaction(
                connection, source_id, now=decision_time
            )
            source = self._require_source_row(connection, source_id)
            endpoint = self._require_active_endpoint_row(connection, source_id)
            attestation = self._require_attestation_row(connection, source_id)
            health = connection.execute(
                "SELECT * FROM source_runtime_health WHERE inventory_source_id=?",
                (source_id,),
            ).fetchone()
            if health is None:
                raise AuthorityInvariantError(
                    "inventory source must have exactly one runtime health record"
                )
            return (
                health["health"] == "healthy"
                and health["freshness"] == "fresh"
                and self._committed_context_is_current(source, endpoint, attestation, health)
            )

    def _authority_decision_time(self) -> datetime:
        return _parse_timestamp(_timestamp(self._now()), "authority decision time")

    def _materialize_due_expiry_in_transaction(
        self,
        connection: sqlite3.Connection,
        source_id: str,
        *,
        now: datetime,
        expected_run_sequence: int | None = None,
        expected_deadline: str | None = None,
    ) -> bool:
        source = self._require_source_row(connection, source_id)
        health = connection.execute(
            "SELECT * FROM source_runtime_health WHERE inventory_source_id=?",
            (source_id,),
        ).fetchone()
        endpoint = self._require_active_endpoint_row(connection, source_id)
        attestation = self._require_attestation_row(connection, source_id)
        if health is None or health["freshness"] != "fresh":
            return False
        if (
            expected_run_sequence is not None
            and source["last_committed_run_sequence"] != expected_run_sequence
        ):
            return False
        if (
            expected_deadline is not None
            and health["freshness_valid_until"] != expected_deadline
        ):
            return False
        if not self._committed_context_is_current(source, endpoint, attestation, health):
            return False
        reference_value = health["freshness_reference_at"]
        deadline_value = health["freshness_valid_until"]
        if reference_value is None or deadline_value is None:
            raise AuthorityInvariantError(
                "fresh source must have a freshness reference and deadline"
            )
        try:
            reference = _parse_timestamp(
                str(reference_value), "freshness_reference_at"
            )
            deadline = _parse_timestamp(str(deadline_value), "freshness_valid_until")
        except ValueError as exc:
            raise AuthorityInvariantError(
                "fresh source has an invalid persisted freshness interval"
            ) from exc
        if now < reference:
            reason = "freshness_clock_rollback_before_reference"
        elif now < deadline:
            return False
        else:
            reason = "freshness_deadline_elapsed"
        connection.execute(
            "UPDATE source_runtime_health SET freshness='stale', "
            "health_origin='time_expiry', health_reason=? "
            "WHERE inventory_source_id=?",
            (reason, source_id),
        )
        self._bump_global_revisions(
            connection,
            inventory_changed=False,
            published_changed=True,
            published_at=now,
        )
        return True

    def _change_source_configuration(
        self,
        inventory_source_id: str,
        *,
        column: str,
        value: object,
        reason: str,
    ) -> InventorySourceState:
        if column not in {"credential_reference", "freshness_duration_seconds"}:
            raise AuthorityInvariantError("unsupported controlled source configuration field")
        source_id = _require_uuid(inventory_source_id, "inventory_source_id")
        with self._store._transaction() as connection:
            source = self._require_source_row(connection, source_id)
            self._require_no_active_run(source)
            if source[column] != value:
                connection.execute(
                    f"UPDATE inventory_sources SET {column}=?, "
                    "source_config_revision=source_config_revision+1 "
                    "WHERE inventory_source_id=?",
                    (value, source_id),
                )
                self._mark_controlled_context_transition(connection, source_id, reason)
                self._bump_global_revisions(
                    connection, inventory_changed=False, published_changed=True
                )
        return self._store.source_state(source_id)

    @staticmethod
    def _require_no_active_run(source: sqlite3.Row) -> None:
        if source["active_discovery_run_id"] is not None:
            raise AuthorityConflict(
                "source configuration transition waits for active run terminalization"
            )

    @staticmethod
    def _mark_controlled_context_transition(
        connection: sqlite3.Connection, source_id: str, reason: str
    ) -> None:
        connection.execute(
            "UPDATE source_runtime_health SET freshness='stale', "
            "health=CASE WHEN health='not_yet_observed' THEN 'not_yet_observed' ELSE health END, "
            "health_origin='controlled_context_transition', health_reason=? "
            "WHERE inventory_source_id=?",
            (reason, source_id),
        )

    @staticmethod
    def _require_nonterminal_run(run: sqlite3.Row) -> None:
        if run["lifecycle"] not in {
            DiscoveryRunLifecycle.ISSUED.value,
            DiscoveryRunLifecycle.RUNNING.value,
        }:
            raise AuthorityConflict("discovery run is already terminal")

    @staticmethod
    def _require_active_endpoint_row(
        connection: sqlite3.Connection, source_id: str
    ) -> sqlite3.Row:
        rows = connection.execute(
            "SELECT * FROM source_endpoints WHERE inventory_source_id=? AND lifecycle='active'",
            (source_id,),
        ).fetchall()
        if len(rows) != 1:
            raise AuthorityInvariantError("inventory source must have exactly one active endpoint")
        return rows[0]

    @staticmethod
    def _require_endpoint_row(
        connection: sqlite3.Connection, source_id: str, endpoint_id: str
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM source_endpoints WHERE inventory_source_id=? AND endpoint_id=?",
            (source_id, endpoint_id),
        ).fetchone()
        if row is None:
            raise AuthorityNotFound(
                "endpoint does not exist for this inventory source"
            )
        return row

    @staticmethod
    def _run_context_is_current(
        source: sqlite3.Row,
        endpoint: sqlite3.Row,
        attestation: sqlite3.Row,
        run: sqlite3.Row,
    ) -> bool:
        """ADR 0003 §21: source_attestation_epoch is a peer of every other
        expected-context field here, checked against the exact durable
        current attestation row -- never against audit history, and never
        gated by attestation_status/relationship_gate (ordinary read-only
        discovery is not attestation-gated)."""

        return (
            int(source["source_config_revision"]) == int(run["expected_source_config_revision"])
            and str(endpoint["endpoint_id"]) == str(run["expected_endpoint_id"])
            and str(endpoint["canonical_transport_locator"]) == str(run["expected_canonical_transport_locator"])
            and int(endpoint["canonicalization_contract_version"]) == int(run["expected_canonicalization_contract_version"])
            and int(endpoint["transport_trust_revision"]) == int(run["expected_transport_trust_revision"])
            and int(source["provider_contract_version"]) == int(run["provider_contract_version"])
            and int(attestation["source_attestation_epoch"]) == int(run["expected_source_attestation_epoch"])
        )

    @staticmethod
    def _committed_context_is_current(
        source: sqlite3.Row,
        endpoint: sqlite3.Row,
        attestation: sqlite3.Row,
        health: sqlite3.Row,
    ) -> bool:
        """ADR 0003 §20 Freshness-context participation: committed
        provenance requires exact equality with current
        source_attestation_epoch, exactly like every other committed
        context field -- never gated by relationship_gate."""

        return (
            health["committed_source_config_revision"] == source["source_config_revision"]
            and health["committed_endpoint_id"] == endpoint["endpoint_id"]
            and health["committed_canonical_transport_locator"] == endpoint["canonical_transport_locator"]
            and health["committed_canonicalization_contract_version"] == endpoint["canonicalization_contract_version"]
            and health["committed_transport_trust_revision"] == endpoint["transport_trust_revision"]
            and health["committed_source_attestation_epoch"] == attestation["source_attestation_epoch"]
        )

    @staticmethod
    def _validate_snapshot_issuance(
        snapshot: NormalizedDiscoverySnapshot, run: sqlite3.Row
    ) -> None:
        expected = (
            snapshot.run_id,
            snapshot.inventory_source_id,
            snapshot.discovery_run_sequence,
            snapshot.expected_source_config_revision,
            snapshot.endpoint_id,
            snapshot.canonical_transport_locator,
            snapshot.canonicalization_contract_version,
            snapshot.expected_transport_trust_revision,
            snapshot.provider_contract_version,
            snapshot.expected_source_attestation_epoch,
        )
        actual = (
            str(run["run_id"]),
            str(run["inventory_source_id"]),
            int(run["discovery_run_sequence"]),
            int(run["expected_source_config_revision"]),
            str(run["expected_endpoint_id"]),
            str(run["expected_canonical_transport_locator"]),
            int(run["expected_canonicalization_contract_version"]),
            int(run["expected_transport_trust_revision"]),
            int(run["provider_contract_version"]),
            int(run["expected_source_attestation_epoch"]),
        )
        if expected != actual:
            raise AuthorityConflict("normalized snapshot does not match immutable run issuance context")
        if snapshot.baseline_completeness is not BaselineCompleteness.COMPLETE:
            raise ValueError("successful reconciliation requires a complete baseline")

    @staticmethod
    def _release_run(
        connection: sqlite3.Connection, source_id: str, run_id: str
    ) -> None:
        released = connection.execute(
            "UPDATE inventory_sources SET active_discovery_run_id=NULL "
            "WHERE inventory_source_id=? AND active_discovery_run_id=?",
            (source_id, run_id),
        )
        if released.rowcount != 1:
            raise AuthorityConflict("discovery run no longer owns the source")

    @staticmethod
    def _complete_run(
        connection: sqlite3.Connection,
        run: sqlite3.Row,
        *,
        completed_at: str,
        outcome: str,
        terminal_reason: str | None,
        evidence: DiscoveryRunCompletionEvidence,
        completion_source: sqlite3.Row | None,
        completion_endpoint: sqlite3.Row | None,
    ) -> None:
        if (completion_source is None) != (completion_endpoint is None):
            raise AuthorityInvariantError("completion context must be jointly known")
        updated = connection.execute(
            "UPDATE discovery_runs SET lifecycle='completed', terminalized_at=?, "
            "terminal_reason=?, completed_at=?, provider_outcome=?, observed_at=?, "
            "normalized_snapshot_hash=?, baseline_completeness=?, source_availability=?, "
            "baseline_mode=?, permission_coverage_complete=?, boundary_consistent=?, "
            "covered_nodes_json=?, failed_baseline_scopes_json=?, "
            "acl_topology_hash_before=?, acl_topology_hash_after=?, "
            "permission_snapshot_hash_before=?, permission_snapshot_hash_after=?, "
            "detail_ok_count=?, detail_temporarily_unavailable_count=?, detail_error_count=?, "
            "failed_detail_scopes_json=?, completion_source_config_revision=?, "
            "completion_endpoint_id=?, completion_canonical_transport_locator=?, "
            "completion_canonicalization_contract_version=?, "
            "completion_transport_trust_revision=?, completion_source_attestation_epoch=? "
            "WHERE run_id=? AND lifecycle IN ('issued', 'running')",
            (
                completed_at,
                terminal_reason,
                completed_at,
                outcome,
                evidence.observed_at,
                evidence.normalized_snapshot_hash,
                evidence.baseline_completeness.value,
                (
                    evidence.source_availability.value
                    if evidence.source_availability is not None
                    else None
                ),
                evidence.baseline_mode.value if evidence.baseline_mode is not None else None,
                evidence.permission_coverage_complete,
                evidence.boundary_consistent,
                _completion_collection_json(evidence.covered_nodes),
                _completion_collection_json(evidence.failed_baseline_scopes),
                evidence.acl_topology_hash_before,
                evidence.acl_topology_hash_after,
                evidence.permission_snapshot_hash_before,
                evidence.permission_snapshot_hash_after,
                evidence.detail_ok_count,
                evidence.detail_temporarily_unavailable_count,
                evidence.detail_error_count,
                _completion_collection_json(evidence.failed_detail_scopes),
                (
                    int(completion_source["source_config_revision"])
                    if completion_source is not None
                    else None
                ),
                (
                    str(completion_endpoint["endpoint_id"])
                    if completion_endpoint is not None
                    else None
                ),
                (
                    str(completion_endpoint["canonical_transport_locator"])
                    if completion_endpoint is not None
                    else None
                ),
                (
                    int(completion_endpoint["canonicalization_contract_version"])
                    if completion_endpoint is not None
                    else None
                ),
                (
                    int(completion_endpoint["transport_trust_revision"])
                    if completion_endpoint is not None
                    else None
                ),
                (
                    int(run["expected_source_attestation_epoch"])
                    if completion_source is not None
                    else None
                ),
                str(run["run_id"]),
            ),
        )
        if updated.rowcount != 1:
            raise AuthorityConflict("discovery run could not be finalized")

    @staticmethod
    def _update_completion_provenance(
        connection: sqlite3.Connection,
        *,
        source_id: str,
        sequence: int,
        outcome: str,
    ) -> None:
        connection.execute(
            "UPDATE source_runtime_health SET latest_completed_run_sequence=?, "
            "latest_completed_outcome=? WHERE inventory_source_id=? AND "
            "(latest_completed_run_sequence IS NULL OR latest_completed_run_sequence < ?)",
            (sequence, outcome, source_id, sequence),
        )

    def _after_reconciliation(
        self, connection: sqlite3.Connection, snapshot: NormalizedDiscoverySnapshot
    ) -> None:
        """Test injection seam inside the success transaction."""

    def _after_health_update(
        self, connection: sqlite3.Connection, snapshot: NormalizedDiscoverySnapshot
    ) -> None:
        """Test injection seam inside the success transaction."""

    def _after_run_completion(
        self, connection: sqlite3.Connection, *, run_id: str
    ) -> None:
        """Test injection seam after evidence write inside terminal transactions."""

    def _insert_initial_endpoint(
        self,
        connection: sqlite3.Connection,
        *,
        source_id: str,
        endpoint_id: str,
        canonical_locator: str,
        created_at: str,
    ) -> None:
        connection.execute(
            "INSERT INTO source_endpoints("
            "endpoint_id, inventory_source_id, canonical_transport_locator, "
            "canonicalization_contract_version, lifecycle, "
            "transport_trust_revision, created_at) "
            "VALUES(?, ?, ?, ?, 'active', 1, ?)",
            (
                endpoint_id,
                source_id,
                canonical_locator,
                CANONICALIZATION_CONTRACT_VERSION,
                created_at,
            ),
        )

    def _insert_initial_health(
        self, connection: sqlite3.Connection, *, source_id: str
    ) -> None:
        connection.execute(
            "INSERT INTO source_runtime_health("
            "inventory_source_id, health, freshness, health_origin, health_reason, "
            "latest_completed_run_sequence, latest_completed_outcome, "
            "last_health_run_sequence, last_run_health_outcome, "
            "last_successful_observed_at, freshness_reference_at, "
            "freshness_valid_until, committed_source_config_revision, "
            "committed_endpoint_id, committed_canonical_transport_locator, "
            "committed_canonicalization_contract_version, "
            "committed_transport_trust_revision) "
            "VALUES(?, 'not_yet_observed', 'not_yet_observed', 'initial', '', "
            "NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL)",
            (source_id,),
        )

    def _insert_initial_attestation_state(
        self, connection: sqlite3.Connection, *, source_id: str
    ) -> None:
        """Every source starts explicitly not-yet-attested at epoch 0 (ADR 0003 §12).

        This grants no trust; it only records the fixed initial sentinel so
        every later CAS/fencing check has a durable row to compare against.
        """

        connection.execute(
            "INSERT INTO source_attestation_state("
            "inventory_source_id, attestation_status, source_attestation_epoch, "
            "anchor_kind, anchor_value, evidence_tier, tier2_evaluation, accepted_at, "
            "accepted_by, evaluated_endpoint_id) "
            "VALUES(?, 'not_yet_attested', 0, NULL, NULL, NULL, NULL, NULL, NULL, NULL)",
            (source_id,),
        )

    def _claim_active_run(
        self, connection: sqlite3.Connection, *, source_id: str, run_id: str
    ) -> None:
        claimed = connection.execute(
            "UPDATE inventory_sources SET active_discovery_run_id=? "
            "WHERE inventory_source_id=? AND active_discovery_run_id IS NULL",
            (run_id, source_id),
        )
        if claimed.rowcount != 1:
            raise AuthorityConflict("discovery run could not claim source ownership")

    def _terminalize_abandoned_run(
        self,
        connection: sqlite3.Connection,
        *,
        run_id: str,
        terminalized_at: str,
        reason: str,
    ) -> None:
        updated = connection.execute(
            "UPDATE discovery_runs SET lifecycle='abandoned', terminalized_at=?, "
            "terminal_reason=? WHERE run_id=? AND lifecycle IN ('issued', 'running')",
            (terminalized_at, reason, run_id),
        )
        if updated.rowcount != 1:
            raise AuthorityConflict("discovery run could not be abandoned")

    def _bump_global_revisions(
        self,
        connection: sqlite3.Connection,
        *,
        inventory_changed: bool,
        published_changed: bool,
        published_at: datetime | None = None,
    ) -> None:
        if inventory_changed and not published_changed:
            raise AuthorityInvariantError(
                "an inventory change must also change the published revision"
            )
        updated = connection.execute(
            "UPDATE backend_instance SET "
            "inventory_revision=inventory_revision+?, "
            "published_state_revision=published_state_revision+?, "
            "published_at=CASE WHEN ? THEN ? ELSE published_at END WHERE singleton=1",
            (
                int(inventory_changed),
                int(published_changed),
                int(published_changed),
                _timestamp(published_at if published_at is not None else self._now()),
            ),
        )
        if updated.rowcount != 1:
            raise AuthorityInvariantError(
                "global revisions require exactly one backend instance"
            )

    @staticmethod
    def _require_source_row(
        connection: sqlite3.Connection, source_id: str
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM inventory_sources WHERE inventory_source_id=?",
            (source_id,),
        ).fetchone()
        if row is None:
            raise AuthorityNotFound("inventory source does not exist")
        return row

    @staticmethod
    def _require_attestation_row(
        connection: sqlite3.Connection, source_id: str
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM source_attestation_state WHERE inventory_source_id=?",
            (source_id,),
        ).fetchone()
        if row is None:
            raise AuthorityInvariantError(
                "inventory source must have a source attestation state record"
            )
        return row

    @staticmethod
    def _require_run_row(
        connection: sqlite3.Connection, run_id: str
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM discovery_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if row is None:
            raise AuthorityNotFound("discovery run does not exist")
        return row

    @staticmethod
    def _require_exact_run_owner(
        source: sqlite3.Row,
        run: sqlite3.Row,
        source_id: str,
        run_id: str,
    ) -> None:
        if run["inventory_source_id"] != source_id:
            raise AuthorityConflict("discovery run belongs to another source")
        if source["active_discovery_run_id"] != run_id:
            raise AuthorityConflict("discovery run is not the active source owner")


def _capture_attestation_context(
    source: sqlite3.Row, endpoint: sqlite3.Row, attestation: sqlite3.Row
) -> _AttestationContext:
    return _AttestationContext(
        source_config_revision=int(source["source_config_revision"]),
        endpoint_id=str(endpoint["endpoint_id"]),
        canonical_transport_locator=str(endpoint["canonical_transport_locator"]),
        canonicalization_contract_version=int(
            endpoint["canonicalization_contract_version"]
        ),
        transport_trust_revision=int(endpoint["transport_trust_revision"]),
        source_attestation_epoch=int(attestation["source_attestation_epoch"]),
        relationship_gate=SourceAttestationRelationshipGate(
            str(attestation["relationship_gate"])
        ),
        endpoint_lifecycle=str(endpoint["lifecycle"]),
    )


def _classify_attestation_reading(
    reading: SourceAttestationEvidenceReading,
    *,
    operation: AttestationOperation,
    accept_new_anchor: bool,
    enrolled_anchor_kind: str | None,
    enrolled_anchor_value: str | None,
    previous_epoch: int,
) -> tuple[
    AttestationOutcome,
    int | None,
    AttestationEvidenceTier | None,
    TierTwoEvaluationStatus | None,
    str | None,
    str | None,
    SourceAttestationRelationshipGate | None,
    str,
]:
    """Turn one evidence reading into an audited outcome plus, if accepted,
    the resulting epoch/gate and durable anchor/tier fields to persist.

    Returns ``(outcome, resulting_epoch, evidence_tier, tier2_evaluation,
    asserted_anchor_kind, asserted_anchor_value, resulting_relationship_gate,
    reason)``. ``resulting_epoch`` is non-``None`` only for an accepted
    security transition (ADR 0003 §20's fixed epoch rule -- never a caller
    choice). ``resulting_relationship_gate`` is non-``None`` only when this
    outcome has a defined effect on the ADR 0003 §17 gate (mismatch sets
    it, an accepted transition clears it); ``None`` means "leave whatever
    the gate currently is untouched" -- in particular a same-anchor
    reconfirmation never clears an already-pending mismatch.

    Never raises for evidence that is merely unsupported/malformed --
    ADR 0003 §18 requires every such read to reach an audited outcome, not
    an exception that would bypass the audit write entirely (Finding 2).
    """

    if reading.outcome is SourceAttestationReadOutcome.UNAVAILABLE:
        return (
            AttestationOutcome.UNAVAILABLE,
            None,
            None,
            None,
            None,
            None,
            None,
            "attestation_evidence_unavailable",
        )
    if reading.outcome is SourceAttestationReadOutcome.MALFORMED:
        return (
            AttestationOutcome.MALFORMED,
            None,
            None,
            None,
            None,
            None,
            None,
            "attestation_evidence_malformed",
        )

    # OBSERVED: a genuine self-reported (tier 1) anchor was read, optionally
    # corroborated by the trusted reader's own tier-2 chain verification.
    # An unsupported anchor kind or structurally invalid anchor value is
    # fail-closed evidence, not a programming error -- classify it exactly
    # like a reader-signaled MALFORMED outcome instead of raising.
    if reading.anchor_kind != ANCHOR_KIND_PVE_ROOT_CA_SHA256_FINGERPRINT or not (
        _is_bounded_text(reading.anchor_value, max_length=200)
    ):
        return (
            AttestationOutcome.MALFORMED,
            None,
            None,
            None,
            None,
            None,
            None,
            "observed_anchor_evidence_is_structurally_invalid",
        )
    asserted_kind = reading.anchor_kind
    asserted_value = reading.anchor_value
    evidence_tier, tier2_evaluation = _classify_tier(reading)

    if operation is AttestationOperation.ENROLLMENT:
        # Nothing enrolled yet: the first observation defines the anchor.
        # previous_epoch is normally 0 (the pristine sentinel), but a source
        # re-enrolling after an explicit revocation/reset carries its epoch
        # forward (ADR 0003 §20: the token never decreases or resets).
        return (
            AttestationOutcome.ACCEPTED,
            previous_epoch + 1,
            evidence_tier,
            tier2_evaluation,
            asserted_kind,
            asserted_value,
            SourceAttestationRelationshipGate.CLEAR,
            "initial_enrollment_accepted",
        )

    same_anchor = (
        asserted_kind == enrolled_anchor_kind and asserted_value == enrolled_anchor_value
    )
    if same_anchor:
        # ADR 0003 §16: audit-only. A same-anchor reconfirmation never
        # touches the epoch, the anchor, or an already-pending mismatch
        # gate -- resolving a pending mismatch requires an explicit
        # accepted transition (ACCEPTED/REVOCATION below), never a match.
        return (
            AttestationOutcome.MATCH,
            None,
            evidence_tier,
            tier2_evaluation,
            asserted_kind,
            asserted_value,
            None,
            "reattestation_same_anchor_reconfirmed",
        )
    if accept_new_anchor:
        return (
            AttestationOutcome.ACCEPTED,
            previous_epoch + 1,
            evidence_tier,
            tier2_evaluation,
            asserted_kind,
            asserted_value,
            SourceAttestationRelationshipGate.CLEAR,
            "attestation_anchor_change_accepted_by_operator",
        )
    return (
        AttestationOutcome.MISMATCH,
        None,
        evidence_tier,
        tier2_evaluation,
        asserted_kind,
        asserted_value,
        SourceAttestationRelationshipGate.MISMATCH_PENDING_REATTESTATION,
        "reattestation_anchor_mismatch_not_accepted",
    )


def _is_bounded_text(value: object, *, max_length: int) -> bool:
    """Non-raising sibling of ``_require_text`` for classification paths
    that must never raise on untrusted/malformed evidence content."""

    return (
        isinstance(value, str)
        and bool(value.strip())
        and len(value) <= max_length
        and all(ord(character) >= 0x20 and ord(character) != 0x7F for character in value)
    )


def _classify_tier(
    reading: SourceAttestationEvidenceReading,
) -> tuple[AttestationEvidenceTier, TierTwoEvaluationStatus]:
    if reading.tier2_verified is True:
        return AttestationEvidenceTier.TIER_2, TierTwoEvaluationStatus.VERIFIED
    if reading.tier2_verified is False:
        return AttestationEvidenceTier.TIER_1, TierTwoEvaluationStatus.FAILED
    return AttestationEvidenceTier.TIER_1, TierTwoEvaluationStatus.NOT_EVALUATED


def _classify_candidate_attestation_reading(
    reading: SourceAttestationEvidenceReading,
    *,
    enrolled_anchor_kind: str | None,
    enrolled_anchor_value: str | None,
) -> tuple[
    AttestationOutcome,
    AttestationEvidenceTier | None,
    TierTwoEvaluationStatus | None,
    str | None,
    str | None,
    SourceAttestationRelationshipGate | None,
    str,
]:
    """Classify one candidate-endpoint evidence reading (ADR 0003 §14/§17).

    Returns ``(outcome, evidence_tier, tier2_evaluation, asserted_anchor_kind,
    asserted_anchor_value, resulting_relationship_gate, reason)``. Unlike
    source-level re-attestation, a candidate check never bumps the source
    epoch or changes the enrolled anchor by itself: a match is retained
    binding evidence only (ACCEPTED), never an activation or a re-
    attestation decision. A mismatch is evidence only, exactly like a
    source-level mismatch (§17) -- it durably gates future attestation-
    gated actions via the same relationship_gate, never anything else.
    """

    if reading.outcome is SourceAttestationReadOutcome.UNAVAILABLE:
        return (
            AttestationOutcome.UNAVAILABLE,
            None,
            None,
            None,
            None,
            None,
            "candidate_attestation_evidence_unavailable",
        )
    if reading.outcome is SourceAttestationReadOutcome.MALFORMED:
        return (
            AttestationOutcome.MALFORMED,
            None,
            None,
            None,
            None,
            None,
            "candidate_attestation_evidence_malformed",
        )

    if reading.anchor_kind != ANCHOR_KIND_PVE_ROOT_CA_SHA256_FINGERPRINT or not (
        _is_bounded_text(reading.anchor_value, max_length=200)
    ):
        return (
            AttestationOutcome.MALFORMED,
            None,
            None,
            None,
            None,
            None,
            "observed_candidate_anchor_evidence_is_structurally_invalid",
        )

    asserted_kind = reading.anchor_kind
    asserted_value = reading.anchor_value
    evidence_tier, tier2_evaluation = _classify_tier(reading)

    if asserted_kind == enrolled_anchor_kind and asserted_value == enrolled_anchor_value:
        return (
            AttestationOutcome.ACCEPTED,
            evidence_tier,
            tier2_evaluation,
            asserted_kind,
            asserted_value,
            None,
            "candidate_attestation_accepted",
        )
    return (
        AttestationOutcome.MISMATCH,
        evidence_tier,
        tier2_evaluation,
        asserted_kind,
        asserted_value,
        SourceAttestationRelationshipGate.MISMATCH_PENDING_REATTESTATION,
        "candidate_attestation_anchor_mismatch",
    )


def _new_uuid() -> str:
    value = str(uuid.uuid4())
    if value == str(uuid.UUID(int=0)):
        raise AuthorityInvariantError("backend UUID generator returned NIL")
    return value


def _require_uuid(value: str, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a canonical UUID")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"{field_name} must be a canonical UUID") from exc
    if parsed.int == 0 or str(parsed) != value:
        raise ValueError(
            f"{field_name} must be a canonical lowercase hyphenated non-NIL UUID"
        )
    return value


def _require_positive_integer(value: int, field_name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


def _require_text(value: str, field_name: str, *, max_length: int) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > max_length
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise ValueError(f"{field_name} must be bounded non-empty text")
    return value


def _require_credential_reference(value: str) -> str:
    reference = _require_text(value, "credential_reference", max_length=500)
    if not reference.startswith("secret://"):
        raise ValueError("credential_reference must be an opaque secret reference")
    return reference


def _timestamp(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("authority clock must return a timezone-aware datetime")
    return value.astimezone(UTC).isoformat()


def _parse_timestamp(value: str, field_name: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return parsed.astimezone(UTC)


def _freshness_reference_at(snapshot: NormalizedDiscoverySnapshot) -> datetime:
    """Return provider-v1's oldest freshness-relevant observation in UTC."""

    observations = [_parse_timestamp(snapshot.observed_at, "observed_at")]
    observations.extend(
        _parse_timestamp(node.observed_at, "node observed_at") for node in snapshot.nodes
    )
    observations.extend(
        _parse_timestamp(resource.observed_at, "resource observed_at")
        for resource in snapshot.resources
    )
    return min(observations)


def _completion_collection_json(value: tuple[str, ...] | None) -> str | None:
    if value is None:
        return None
    return json.dumps(list(value), ensure_ascii=True, separators=(",", ":"))
