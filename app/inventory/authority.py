"""Typed mutation boundary for Hubinet Ops 0.5 persistent authority."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
import sqlite3
import uuid

from .canonicalization import (
    CANONICALIZATION_CONTRACT_VERSION,
    canonicalize_transport_locator,
)
from .models import (
    AuthorityConflict,
    AuthorityInvariantError,
    AuthorityNotFound,
    DiscoveryRun,
    DiscoveryRunLifecycle,
    InventorySourceState,
)
from .discovery import (
    BaselineCompleteness,
    DiscoveryRunCompletionEvidence,
    NormalizedDiscoverySnapshot,
)
from .provider import PROVIDER_CONTRACT_VERSION
from .reconciliation import InventoryReconciler, ReconciliationSummary
from .store import InventoryAuthorityStore


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
                "expected_transport_trust_revision, "
                "provider_contract_version, lifecycle, "
                "terminalized_at, terminal_reason, completed_at, provider_outcome, "
                "observed_at, normalized_snapshot_hash) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'issued', "
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

            if not self._run_context_is_current(source, endpoint, run):
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
                    "committed_transport_trust_revision=? WHERE inventory_source_id=?",
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
            sequence = int(run["discovery_run_sequence"])
            applicable = self._run_context_is_current(source, endpoint, run)
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
                and self._committed_context_is_current(source, endpoint, health)
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
        if not self._committed_context_is_current(source, endpoint, health):
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
    def _run_context_is_current(
        source: sqlite3.Row,
        endpoint: sqlite3.Row,
        run: sqlite3.Row,
    ) -> bool:
        """Every expected-context field captured at issuance must still be
        current, so a run finalized after a concurrent source/endpoint
        configuration change is fenced out instead of committing stale
        observations."""

        return (
            int(source["source_config_revision"]) == int(run["expected_source_config_revision"])
            and str(endpoint["endpoint_id"]) == str(run["expected_endpoint_id"])
            and str(endpoint["canonical_transport_locator"]) == str(run["expected_canonical_transport_locator"])
            and int(endpoint["canonicalization_contract_version"]) == int(run["expected_canonicalization_contract_version"])
            and int(endpoint["transport_trust_revision"]) == int(run["expected_transport_trust_revision"])
            and int(source["provider_contract_version"]) == int(run["provider_contract_version"])
        )

    @staticmethod
    def _committed_context_is_current(
        source: sqlite3.Row,
        endpoint: sqlite3.Row,
        health: sqlite3.Row,
    ) -> bool:
        """Committed provenance must still match the source's current
        configuration context, so a stale commit never counts as fresh."""

        return (
            health["committed_source_config_revision"] == source["source_config_revision"]
            and health["committed_endpoint_id"] == endpoint["endpoint_id"]
            and health["committed_canonical_transport_locator"] == endpoint["canonical_transport_locator"]
            and health["committed_canonicalization_contract_version"] == endpoint["canonicalization_contract_version"]
            and health["committed_transport_trust_revision"] == endpoint["transport_trust_revision"]
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
            "completion_transport_trust_revision=? "
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
