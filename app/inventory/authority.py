"""Typed mutation boundary for Hubinet Ops 0.5 persistent authority."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import json
import re
import sqlite3
from typing import TypeVar
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
    HostSubmissionState,
    InventorySourceState,
    ObservedSnapshot,
    PackageScanFailure,
    PackageScanLifecycle,
    PackageScanOutcome,
    PackageScanPackage,
    PackageScanRun,
    PackagePlanApproval,
    PackageUpdateCheckpoint,
    PackageUpdateEventLevel,
    PackageUpdateEventType,
    PackageUpdateExecutionOutcome,
    PackageUpdateJob,
    PackageUpdateJobStatus,
    PackageUpdateRollbackTarget,
    PackageUpdateSnapshotIdentity,
    SnapshotOwnership,
    SnapshotSubmissionRefusedBeforeCallback,
    checkpoint_rank as _checkpoint_rank,
)
from .snapshot_identity import (
    build_snapshot_ownership,
    derive_pre_update_snapshot_identity,
)
from .discovery import (
    BaselineCompleteness,
    DiscoveryRunCompletionEvidence,
    NormalizedDiscoverySnapshot,
)
from .provider import PROVIDER_CONTRACT_VERSION
from .reconciliation import InventoryReconciler, ReconciliationSummary
from .store import InventoryAuthorityStore


_T = TypeVar("_T")


class InventoryAuthority:
    """The typed mutation boundary for every durable authority state change."""

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
        """Read-only precondition guard; grants no mutation authority."""

        source_id = _require_uuid(inventory_source_id, "inventory_source_id")
        with self._store._transaction() as connection:
            decision_time = self._authority_decision_time()
            self._materialize_due_expiry_in_transaction(
                connection, source_id, now=decision_time
            )
            return self._source_has_current_authority_in_transaction(
                connection, source_id
            )

    def issue_package_scan(self, resource_id: str) -> PackageScanRun:
        """Capture and durably own the current LXC execution context."""

        canonical_resource_id = _require_uuid(resource_id, "resource_id")
        scan_run_id = _new_uuid()
        source_authority_rejected = False
        with self._store._transaction() as connection:
            row = self._require_package_scan_target(connection, canonical_resource_id)
            if str(row["resource_type"]) != "lxc":
                raise AuthorityConflict("package scanning supports LXC resources only")
            source_id = str(row["inventory_source_id"])
            decision_time = self._authority_decision_time()
            self._materialize_due_expiry_in_transaction(
                connection, source_id, now=decision_time
            )
            if not self._source_has_current_authority_in_transaction(
                connection, source_id
            ):
                source_authority_rejected = True
            else:
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
                previous = connection.execute(
                    "SELECT MAX(attempt_sequence) FROM package_scan_runs WHERE resource_id=?",
                    (canonical_resource_id,),
                ).fetchone()[0]
                attempt_sequence = (int(previous) if previous is not None else 0) + 1
                try:
                    connection.execute(
                        "INSERT INTO package_scan_runs("
                        "scan_run_id, resource_id, inventory_source_id, "
                        "committed_source_config_revision, committed_endpoint_id, "
                        "committed_canonical_transport_locator, "
                        "committed_canonicalization_contract_version, "
                        "committed_transport_trust_revision, provider_contract_version, "
                        "attempt_sequence, "
                        "expected_binding_id, expected_locator_generation, "
                        "expected_resource_continuity_revision, expected_vmid, "
                        "expected_node_id, expected_node_name, started_at, lifecycle) "
                        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'running')",
                        (
                            scan_run_id,
                            canonical_resource_id,
                            source_id,
                            int(health["committed_source_config_revision"]),
                            str(health["committed_endpoint_id"]),
                            str(health["committed_canonical_transport_locator"]),
                            int(health["committed_canonicalization_contract_version"]),
                            int(health["committed_transport_trust_revision"]),
                            int(source["provider_contract_version"]),
                            attempt_sequence,
                            str(row["binding_id"]),
                            int(row["locator_generation"]),
                            int(row["resource_continuity_revision"]),
                            int(row["vmid"]),
                            str(row["current_node_id"]),
                            str(row["external_node_name"]),
                            _timestamp(decision_time),
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    raise AuthorityConflict(
                        "resource already has an active package scan"
                    ) from exc
                self._bump_global_revisions(
                    connection, inventory_changed=False, published_changed=True
                )
        # Preserve any expiry materialized above while refusing the scan row.
        if source_authority_rejected:
            raise AuthorityConflict(
                "package scan requires fresh healthy inventory authority"
            )
        return self._store.package_scan_run(scan_run_id)

    def package_scan_context_is_current(self, scan_run_id: str) -> bool:
        canonical_run_id = _require_uuid(scan_run_id, "scan_run_id")
        with self._store._read_transaction() as connection:
            run = self._require_package_scan_run_row(connection, canonical_run_id)
            if str(run["lifecycle"]) != PackageScanLifecycle.RUNNING.value:
                return False
            return self._package_scan_context_is_current(connection, run)

    def finalize_successful_package_scan(
        self,
        scan_run_id: str,
        *,
        os_id: str,
        os_version: str,
        packages: tuple[PackageScanPackage, ...],
        reboot_required: bool | None,
    ) -> PackageScanRun:
        """Commit one exact plan, or fence it as stale in the same transaction."""

        canonical_run_id = _require_uuid(scan_run_id, "scan_run_id")
        canonical_os_id = _require_text(os_id, "os_id", max_length=100).lower()
        canonical_os_version = _require_text(os_version, "os_version", max_length=200)
        if canonical_os_id not in {"debian", "ubuntu"}:
            raise ValueError("successful package scan OS must be Debian or Ubuntu")
        canonical_packages = _validate_package_plan(packages)
        if reboot_required not in {True, None}:
            raise ValueError("reboot_required must be true or unknown")
        fingerprint = package_plan_fingerprint(canonical_packages)
        completed_at = _timestamp(self._now())

        with self._store._transaction() as connection:
            run = self._require_package_scan_run_row(connection, canonical_run_id)
            self._require_running_package_scan(run)
            if not self._package_scan_context_is_current(connection, run):
                self._complete_failed_package_scan(
                    connection,
                    canonical_run_id,
                    completed_at=completed_at,
                    outcome=PackageScanOutcome.FAILED,
                    failure_class=PackageScanFailure.STALE_TARGET,
                    error_message="resource binding or generation changed during package scan",
                    os_id=None,
                    os_version=None,
                )
            else:
                connection.executemany(
                    "INSERT INTO package_scan_packages("
                    "scan_run_id, package_index, package_name, architecture, "
                    "installed_version, candidate_version, origin, description, "
                    "security) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        (
                            canonical_run_id,
                            index,
                            package.package_name,
                            package.architecture,
                            package.installed_version,
                            package.candidate_version,
                            package.origin,
                            package.description,
                            1 if package.security is True else None,
                        )
                        for index, package in enumerate(canonical_packages)
                    ),
                )
                connection.execute(
                    "UPDATE package_scan_runs SET lifecycle='completed', completed_at=?, "
                    "outcome='success', os_id=?, os_version=?, pending_count=?, "
                    "plan_fingerprint=?, reboot_required=? WHERE scan_run_id=?",
                    (
                        completed_at,
                        canonical_os_id,
                        canonical_os_version,
                        len(canonical_packages),
                        fingerprint,
                        1 if reboot_required is True else None,
                        canonical_run_id,
                    ),
                )
            self._bump_global_revisions(
                connection, inventory_changed=False, published_changed=True
            )
        return self._store.package_scan_run(canonical_run_id)

    def finalize_failed_package_scan(
        self,
        scan_run_id: str,
        *,
        failure_class: PackageScanFailure,
        error_message: str,
        os_id: str | None = None,
        os_version: str | None = None,
    ) -> PackageScanRun:
        canonical_run_id = _require_uuid(scan_run_id, "scan_run_id")
        if not isinstance(failure_class, PackageScanFailure):
            raise ValueError("failure_class must be a PackageScanFailure")
        message = _require_text(error_message, "error_message", max_length=500)
        normalized_os_id = (
            _require_text(os_id, "os_id", max_length=100).lower()
            if os_id is not None
            else None
        )
        normalized_os_version = (
            _require_text(os_version, "os_version", max_length=200)
            if os_version is not None
            else None
        )
        completed_at = _timestamp(self._now())
        with self._store._transaction() as connection:
            run = self._require_package_scan_run_row(connection, canonical_run_id)
            self._require_running_package_scan(run)
            self._complete_failed_package_scan(
                connection,
                canonical_run_id,
                completed_at=completed_at,
                outcome=PackageScanOutcome.FAILED,
                failure_class=failure_class,
                error_message=message,
                os_id=normalized_os_id,
                os_version=normalized_os_version,
            )
            self._bump_global_revisions(
                connection, inventory_changed=False, published_changed=True
            )
        return self._store.package_scan_run(canonical_run_id)

    def recover_interrupted_package_scans(self) -> tuple[str, ...]:
        """Terminalize every pre-restart running attempt as unknown/interrupted."""

        completed_at = _timestamp(self._now())
        with self._store._transaction() as connection:
            rows = connection.execute(
                "SELECT scan_run_id FROM package_scan_runs "
                "WHERE lifecycle='running' ORDER BY resource_id, attempt_sequence"
            ).fetchall()
            recovered = tuple(str(row["scan_run_id"]) for row in rows)
            for scan_run_id in recovered:
                self._complete_failed_package_scan(
                    connection,
                    scan_run_id,
                    completed_at=completed_at,
                    outcome=PackageScanOutcome.INTERRUPTED,
                    failure_class=PackageScanFailure.EXECUTION_FAILED,
                    error_message="backend restarted before package scan completed",
                    os_id=None,
                    os_version=None,
                )
            if recovered:
                self._bump_global_revisions(
                    connection, inventory_changed=False, published_changed=True
                )
        return recovered

    def approve_package_plan(
        self,
        resource_id: str,
        reviewed_scan_run_id: str,
        reviewed_plan_fingerprint: str,
    ) -> PackagePlanApproval:
        """Approve only the exact current plan reference supplied by the operator."""

        canonical_resource_id = _require_uuid(resource_id, "resource_id")
        canonical_scan_run_id = _require_uuid(
            reviewed_scan_run_id, "reviewed_scan_run_id"
        )
        canonical_fingerprint = _require_package_plan_fingerprint(
            reviewed_plan_fingerprint
        )
        approval_id = _new_uuid()
        approved_at = _timestamp(self._now())
        plan_is_stale = False
        result: PackagePlanApproval | None = None

        with self._store._transaction() as connection:
            resource = self._require_package_scan_target(
                connection, canonical_resource_id
            )
            if str(resource["resource_type"]) != "lxc":
                raise AuthorityConflict("package plan approval supports LXC resources only")

            run = self._require_package_scan_run_row(
                connection, canonical_scan_run_id
            )
            if str(run["resource_id"]) != canonical_resource_id:
                raise AuthorityConflict("reviewed package scan belongs to another resource")

            latest_sequence = connection.execute(
                "SELECT MAX(attempt_sequence) FROM package_scan_runs WHERE resource_id=?",
                (canonical_resource_id,),
            ).fetchone()[0]
            if latest_sequence is None or int(run["attempt_sequence"]) != int(
                latest_sequence
            ):
                raise AuthorityConflict("reviewed package scan is not the latest attempt")
            if (
                str(run["lifecycle"]) != PackageScanLifecycle.COMPLETED.value
                or str(run["outcome"]) != PackageScanOutcome.SUCCESS.value
            ):
                raise AuthorityConflict("reviewed package scan is not a successful exact plan")

            stored_fingerprint = self._successful_package_scan_fingerprint(
                connection, run
            )
            if stored_fingerprint != canonical_fingerprint:
                raise AuthorityConflict("reviewed package plan fingerprint does not match")

            decision_time = self._authority_decision_time()
            self._materialize_due_expiry_in_transaction(
                connection,
                str(run["inventory_source_id"]),
                now=decision_time,
            )
            if not self._package_scan_is_current_and_approvable(connection, run):
                # Preserve any expiry materialized just above while refusing
                # the approval: let the transaction commit the durable
                # freshness transition and raise the conflict after it,
                # mirroring issue_package_scan's pattern. Do not touch
                # package_plan_approvals on this path.
                plan_is_stale = True
            else:
                existing = connection.execute(
                    "SELECT * FROM package_plan_approvals WHERE resource_id=?",
                    (canonical_resource_id,),
                ).fetchone()
                if (
                    existing is not None
                    and str(existing["reviewed_scan_run_id"]) == canonical_scan_run_id
                    and str(existing["approved_plan_fingerprint"])
                    == canonical_fingerprint
                ):
                    result = PackagePlanApproval(
                        approval_id=str(existing["approval_id"]),
                        resource_id=canonical_resource_id,
                        reviewed_scan_run_id=canonical_scan_run_id,
                        approved_plan_fingerprint=canonical_fingerprint,
                        approved_at=str(existing["approved_at"]),
                    )
                else:
                    connection.execute(
                        "INSERT INTO package_plan_approvals("
                        "resource_id, approval_id, reviewed_scan_run_id, "
                        "approved_plan_fingerprint, approved_at) VALUES(?, ?, ?, ?, ?) "
                        "ON CONFLICT(resource_id) DO UPDATE SET approval_id=excluded.approval_id, "
                        "reviewed_scan_run_id=excluded.reviewed_scan_run_id, "
                        "approved_plan_fingerprint=excluded.approved_plan_fingerprint, "
                        "approved_at=excluded.approved_at",
                        (
                            canonical_resource_id,
                            approval_id,
                            canonical_scan_run_id,
                            canonical_fingerprint,
                            approved_at,
                        ),
                    )
                    self._after_package_plan_approval_write(
                        connection, resource_id=canonical_resource_id
                    )
                    self._bump_global_revisions(
                        connection, inventory_changed=False, published_changed=True
                    )
                    # Capture the exact fact this transaction wrote. Do not
                    # perform a post-commit reread of the mutable
                    # per-resource row: another transaction may replace it
                    # before we would have read it back, which would return
                    # someone else's approval for this request.
                    result = PackagePlanApproval(
                        approval_id=approval_id,
                        resource_id=canonical_resource_id,
                        reviewed_scan_run_id=canonical_scan_run_id,
                        approved_plan_fingerprint=canonical_fingerprint,
                        approved_at=approved_at,
                    )

        if plan_is_stale:
            raise AuthorityConflict(
                "reviewed package scan is stale or its current context is invalid"
            )
        if result is None:
            raise AuthorityInvariantError("package plan approval was not captured")
        return result

    def issue_package_update_job(
        self, resource_id: str, approval_id: str, request_id: str
    ) -> PackageUpdateJob:
        """Atomically freeze one approved, current, non-empty package plan.

        This is an internal authority operation in this stage. Production has
        no route, scheduler, or worker that calls it, and issuance itself
        performs no PVE, snapshot, or workload mutation.
        """

        canonical_resource_id = _require_uuid(resource_id, "resource_id")
        canonical_approval_id = _require_uuid(approval_id, "approval_id")
        canonical_request_id = _require_uuid(request_id, "request_id")
        rejected_current_authority = False
        result_job_id: str | None = None

        with self._store._transaction() as connection:
            existing = connection.execute(
                "SELECT job_id, resource_id, approval_id "
                "FROM package_update_jobs WHERE request_id=?",
                (canonical_request_id,),
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["resource_id"]) != canonical_resource_id
                    or str(existing["approval_id"]) != canonical_approval_id
                ):
                    raise AuthorityConflict(
                        "request_id was already used for another package update request"
                    )
                result_job_id = str(existing["job_id"])
            else:
                resource = self._require_package_scan_target(
                    connection, canonical_resource_id
                )
                if str(resource["resource_type"]) != "lxc":
                    raise AuthorityConflict(
                        "package update jobs support LXC resources only"
                    )

                approval = connection.execute(
                    "SELECT * FROM package_plan_approvals WHERE resource_id=?",
                    (canonical_resource_id,),
                ).fetchone()
                if approval is None:
                    raise AuthorityConflict(
                        "package update job requires a current package plan approval"
                    )
                if str(approval["approval_id"]) != canonical_approval_id:
                    raise AuthorityConflict(
                        "approval_id does not identify the current package plan approval"
                    )

                reviewed = self._require_package_scan_run_row(
                    connection, str(approval["reviewed_scan_run_id"])
                )
                if str(reviewed["resource_id"]) != canonical_resource_id:
                    raise AuthorityInvariantError(
                        "package plan approval references another resource"
                    )
                if (
                    str(reviewed["lifecycle"])
                    != PackageScanLifecycle.COMPLETED.value
                    or str(reviewed["outcome"])
                    != PackageScanOutcome.SUCCESS.value
                ):
                    raise AuthorityInvariantError(
                        "package plan approval does not reference a successful exact plan"
                    )
                reviewed_fingerprint = self._successful_package_scan_fingerprint(
                    connection, reviewed
                )
                approved_fingerprint = str(
                    approval["approved_plan_fingerprint"]
                )
                if reviewed_fingerprint != approved_fingerprint:
                    raise AuthorityInvariantError(
                        "approved package plan fingerprint does not match reviewed exact rows"
                    )

                current = self._latest_package_scan_row(
                    connection, canonical_resource_id
                )
                if current is None or (
                    str(current["lifecycle"])
                    != PackageScanLifecycle.COMPLETED.value
                    or str(current["outcome"])
                    != PackageScanOutcome.SUCCESS.value
                ):
                    raise AuthorityConflict(
                        "latest package scan attempt is not a successful exact plan"
                    )
                current_fingerprint = self._successful_package_scan_fingerprint(
                    connection, current
                )
                if current_fingerprint != approved_fingerprint:
                    raise AuthorityConflict(
                        "current package plan does not match the approved plan"
                    )

                decision_time = self._authority_decision_time()
                self._materialize_due_expiry_in_transaction(
                    connection,
                    str(current["inventory_source_id"]),
                    now=decision_time,
                )
                if not (
                    self._package_scan_is_current_and_approvable(
                        connection, current
                    )
                    and self._package_scan_context_matches_reviewed(
                        current, reviewed
                    )
                ):
                    # Commit any freshness expiry materialized above while
                    # refusing issuance. No job rows have been written.
                    rejected_current_authority = True
                else:
                    packages = connection.execute(
                        "SELECT * FROM package_scan_packages WHERE scan_run_id=? "
                        "ORDER BY package_index",
                        (str(current["scan_run_id"]),),
                    ).fetchall()
                    if not packages:
                        raise AuthorityConflict(
                            "package update job requires a non-empty exact plan"
                        )
                    job_id = _new_uuid()
                    issued_at = _timestamp(decision_time)

                    # The child FK is deferred specifically so immutable job
                    # package rows can be inserted before their parent. The
                    # parent insert seals that exact set against later INSERT,
                    # UPDATE, or DELETE in the same atomic transaction.
                    connection.executemany(
                        "INSERT INTO package_update_job_packages("
                        "job_id, package_index, package_name, architecture, "
                        "installed_version, candidate_version, origin, "
                        "description, security) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            (
                                job_id,
                                int(package["package_index"]),
                                str(package["package_name"]),
                                str(package["architecture"]),
                                str(package["installed_version"]),
                                str(package["candidate_version"]),
                                package["origin"],
                                package["description"],
                                package["security"],
                            )
                            for package in packages
                        ),
                    )
                    try:
                        connection.execute(
                            "INSERT INTO package_update_jobs("
                            "job_id, request_id, issued_at, resource_id, approval_id, "
                            "approval_reviewed_scan_run_id, approved_plan_fingerprint, "
                            "approval_approved_at, current_plan_scan_run_id, "
                            "inventory_source_id, committed_source_config_revision, "
                            "committed_endpoint_id, committed_canonical_transport_locator, "
                            "committed_canonicalization_contract_version, "
                            "committed_transport_trust_revision, provider_contract_version, "
                            "expected_resource_type, expected_binding_id, "
                            "expected_locator_generation, "
                            "expected_resource_continuity_revision, expected_vmid, "
                            "expected_node_id, expected_node_name, package_count, "
                            "status, checkpoint) "
                            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                            "?, ?, ?, ?, ?, ?, ?, ?, 'active', 'issued')",
                            (
                                job_id,
                                canonical_request_id,
                                issued_at,
                                canonical_resource_id,
                                canonical_approval_id,
                                str(approval["reviewed_scan_run_id"]),
                                approved_fingerprint,
                                str(approval["approved_at"]),
                                str(current["scan_run_id"]),
                                str(current["inventory_source_id"]),
                                int(current["committed_source_config_revision"]),
                                str(current["committed_endpoint_id"]),
                                str(current["committed_canonical_transport_locator"]),
                                int(
                                    current[
                                        "committed_canonicalization_contract_version"
                                    ]
                                ),
                                int(current["committed_transport_trust_revision"]),
                                int(current["provider_contract_version"]),
                                str(resource["resource_type"]),
                                str(current["expected_binding_id"]),
                                int(current["expected_locator_generation"]),
                                int(current["expected_resource_continuity_revision"]),
                                int(current["expected_vmid"]),
                                str(current["expected_node_id"]),
                                str(current["expected_node_name"]),
                                len(packages),
                            ),
                        )
                    except sqlite3.IntegrityError as exc:
                        raise AuthorityConflict(
                            "another active package update job owns the global slot"
                        ) from exc
                    self._append_package_update_job_event(
                        connection,
                        job_id=job_id,
                        created_at=issued_at,
                        level=PackageUpdateEventLevel.INFO,
                        stage=PackageUpdateCheckpoint.ISSUED,
                        event_type=PackageUpdateEventType.JOB_ISSUED,
                        message="package update job authority issued",
                        details={
                            "approval_reviewed_scan_run_id": str(
                                approval["reviewed_scan_run_id"]
                            ),
                            "current_plan_scan_run_id": str(current["scan_run_id"]),
                        },
                    )
                    self._after_package_update_job_issuance(
                        connection, job_id=job_id
                    )
                    result_job_id = job_id

        if rejected_current_authority:
            raise AuthorityConflict(
                "approved package plan or its current authority context is stale"
            )
        if result_job_id is None:
            raise AuthorityInvariantError("package update job issuance was not captured")
        return self._store.package_update_job(result_job_id)

    def revalidate_package_update_job(self, job_id: str) -> PackageUpdateJob:
        """Revalidate the current authority half of future mutation safety.

        Passing this check is necessary but deliberately not sufficient
        permission for package mutation. A future activation stage must call
        it again close to mutation and must also prove exact APT execution
        simulation/equality before any package operation.
        """

        canonical_job_id = _require_uuid(job_id, "job_id")
        with self._store._transaction() as connection:
            job = self._require_package_update_job_row(connection, canonical_job_id)
            current_authority_holds = self._package_update_job_authority_is_current(
                connection, job
            )

        # Deliberately raised after the transaction commits: the predicate
        # may have materialized due source-freshness expiry, and that
        # bookkeeping is kept even when the job itself is refused.
        if not current_authority_holds:
            raise AuthorityConflict(
                "package update job resource or source authority context is stale"
            )
        return self._store.package_update_job(canonical_job_id)

    def _package_update_job_authority_is_current(
        self, connection: sqlite3.Connection, job: sqlite3.Row
    ) -> bool:
        """Re-prove every current-authority predicate inside ONE transaction.

        This is the whole "current authority still permits this job to
        advance" proof, factored so that a transition can prove it in the
        *same* transaction that commits the transition. Splitting the proof
        and the commit across two transactions is a check-then-commit race:
        discovery reconciliation can replace the guest occupying the same
        VMID on the same node in between, and neither a checkpoint CAS (which
        only sees job status/checkpoint) nor the host helper (which can only
        verify live PVE VMID/type/node facts, never a backend resource
        incarnation) would catch it.

        Returns ``False`` for a stale resource/source context so the caller
        can commit any freshness expiry materialized here before refusing.
        Hard incoherences still raise.
        """

        if str(job["status"]) != PackageUpdateJobStatus.ACTIVE.value:
            raise AuthorityConflict("package update job is terminal")
        if str(job["expected_resource_type"]) != "lxc":
            raise AuthorityInvariantError(
                "package update job has an unsupported frozen resource type"
            )

        job_id = str(job["job_id"])
        decision_time = self._authority_decision_time()
        self._materialize_due_expiry_in_transaction(
            connection,
            str(job["inventory_source_id"]),
            now=decision_time,
        )
        if not (
            self._package_scan_context_is_current(connection, job)
            and self._package_scan_source_context_is_current(connection, job)
        ):
            return False

        current = self._latest_package_scan_row(connection, str(job["resource_id"]))
        if current is None or (
            str(current["lifecycle"]) != PackageScanLifecycle.COMPLETED.value
            or str(current["outcome"]) != PackageScanOutcome.SUCCESS.value
        ):
            raise AuthorityConflict(
                "latest package scan attempt is not a successful exact plan"
            )
        if not (
            self._package_scan_is_current_and_approvable(connection, current)
            and self._package_scan_context_matches_job(current, job)
        ):
            raise AuthorityConflict(
                "latest package scan authority context does not match the job"
            )
        fingerprint = self._successful_package_scan_fingerprint(connection, current)
        if fingerprint != str(job["approved_plan_fingerprint"]):
            raise AuthorityConflict(
                "latest package plan fingerprint does not match the job"
            )
        current_material = self._package_material_rows(
            connection,
            table="package_scan_packages",
            owner_column="scan_run_id",
            owner_id=str(current["scan_run_id"]),
        )
        job_material = self._package_material_rows(
            connection,
            table="package_update_job_packages",
            owner_column="job_id",
            owner_id=job_id,
        )
        if not current_material or current_material != job_material:
            raise AuthorityConflict(
                "current exact package material does not match the job"
            )
        return True

    def _snapshot_identity_in_transaction(
        self, connection: sqlite3.Connection, job: sqlite3.Row
    ) -> PackageUpdateSnapshotIdentity:
        """Derive one job's deterministic snapshot identity, in-transaction."""

        job_id = str(job["job_id"])
        backend_instance_id = str(
            connection.execute(
                "SELECT backend_instance_id FROM backend_instance"
            ).fetchone()["backend_instance_id"]
        )
        identity = derive_pre_update_snapshot_identity(
            backend_instance_id=backend_instance_id,
            job_id=job_id,
            resource_id=str(job["resource_id"]),
            resource_continuity_revision=int(
                job["expected_resource_continuity_revision"]
            ),
        )
        persisted_name = job["snapshot_name"]
        persisted_operation = job["snapshot_operation_id"]
        if (
            persisted_name is not None
            and str(persisted_name) != identity.snapshot_name
        ) or (
            persisted_operation is not None
            and str(persisted_operation) != identity.snapshot_operation_id
        ):
            raise AuthorityInvariantError(
                "persisted snapshot identity does not match the job's "
                "deterministic derivation"
            )
        return identity

    def _snapshot_ownership_in_transaction(
        self, connection: sqlite3.Connection, job: sqlite3.Row
    ) -> SnapshotOwnership:
        """Build one job's strict snapshot ownership metadata, in-transaction."""

        backend_instance_id = str(
            connection.execute(
                "SELECT backend_instance_id FROM backend_instance"
            ).fetchone()["backend_instance_id"]
        )
        return build_snapshot_ownership(
            job_id=str(job["job_id"]),
            resource_id=str(job["resource_id"]),
            resource_continuity_revision=int(
                job["expected_resource_continuity_revision"]
            ),
            inventory_source_id=str(job["inventory_source_id"]),
            backend_instance_id=backend_instance_id,
        )

    # ------------------------------------------------------------------
    # Job-owned snapshot safety
    #
    # These are internal authority transitions. Production has no route,
    # scheduler, or worker that reaches them, and none of them performs a
    # PVE, snapshot, or workload mutation of its own: they record durable
    # authority facts about a snapshot operation another component may
    # perform.
    # ------------------------------------------------------------------

    def package_update_job(self, job_id: str) -> PackageUpdateJob:
        """Read one durable package update job through the authority."""

        return self._store.package_update_job(_require_uuid(job_id, "job_id"))

    def record_package_update_preflight_passed(
        self, job_id: str
    ) -> PackageUpdateJob:
        """Record that one active job's authority preflight currently holds.

        Revalidates the full current-authority context first, so a stale job
        can never advance. Idempotent: a job already at ``preflight_passed``
        revalidates and returns unchanged.
        """

        canonical_job_id = _require_uuid(job_id, "job_id")
        recorded_at = _timestamp(self._now())
        with self._store._transaction() as connection:
            job = self._require_package_update_job_row(connection, canonical_job_id)
            # The authority proof and the transition it authorizes commit
            # together, so nothing can invalidate the job in between.
            current_authority_holds = self._package_update_job_authority_is_current(
                connection, job
            )
            self._after_package_update_authority_proof(
                connection, job_id=canonical_job_id
            )
            if current_authority_holds:
                checkpoint = PackageUpdateCheckpoint(str(job["checkpoint"]))
                if checkpoint is PackageUpdateCheckpoint.ISSUED:
                    updated = connection.execute(
                        "UPDATE package_update_jobs SET checkpoint='preflight_passed' "
                        "WHERE job_id=? AND status='active' AND checkpoint='issued'",
                        (canonical_job_id,),
                    )
                    if updated.rowcount != 1:
                        raise AuthorityConflict(
                            "package update job preflight lost durable ownership"
                        )
                    self._append_package_update_job_event(
                        connection,
                        job_id=canonical_job_id,
                        created_at=recorded_at,
                        level=PackageUpdateEventLevel.INFO,
                        stage=PackageUpdateCheckpoint.PREFLIGHT_PASSED,
                        event_type=PackageUpdateEventType.PREFLIGHT_PASSED,
                        message="package update job preflight authority revalidated",
                        details={},
                    )
                elif checkpoint is not PackageUpdateCheckpoint.PREFLIGHT_PASSED:
                    raise AuthorityConflict(
                        "package update job has already advanced past preflight"
                    )

        if not current_authority_holds:
            raise AuthorityConflict(
                "package update job resource or source authority context is stale"
            )
        return self._store.package_update_job(canonical_job_id)

    def package_update_snapshot_identity(
        self, job_id: str
    ) -> PackageUpdateSnapshotIdentity:
        """Derive one job's deterministic pre-update snapshot identity.

        Pure and restart-stable: it reads only immutable job facts plus this
        backend instance's identity, so the same job always derives the same
        snapshot name and operation id.
        """

        canonical_job_id = _require_uuid(job_id, "job_id")
        with self._store._transaction() as connection:
            job = self._require_package_update_job_row(connection, canonical_job_id)
            return self._snapshot_identity_in_transaction(connection, job)

    def package_update_snapshot_ownership(self, job_id: str) -> SnapshotOwnership:
        """Build the strict ownership metadata one job's snapshot must carry."""

        canonical_job_id = _require_uuid(job_id, "job_id")
        with self._store._transaction() as connection:
            job = self._require_package_update_job_row(connection, canonical_job_id)
            return self._snapshot_ownership_in_transaction(connection, job)

    def record_package_update_snapshot_intent(
        self, job_id: str
    ) -> PackageUpdateJob:
        """Durably commit the write-ahead snapshot-operation intent.

        This is the uncertainty boundary. It MUST be committed before any
        snapshot mutation request can be sent, because once a job is at
        ``snapshot_may_have_started`` nothing may ever conclude that no PVE
        mutation happened. Idempotent: re-recording the same derived identity
        returns the existing durable intent instead of creating a second one.
        """

        canonical_job_id = _require_uuid(job_id, "job_id")
        recorded_at = _timestamp(self._now())
        with self._store._transaction() as connection:
            job = self._require_package_update_job_row(connection, canonical_job_id)
            # The write-ahead intent is the point of no return, so its
            # authority proof commits in the same transaction. A guest
            # replaced at the same VMID on the same node between proof and
            # commit would otherwise let a stale job submit a snapshot
            # against the replacement and tag it with the old incarnation's
            # ownership metadata.
            current_authority_holds = self._package_update_job_authority_is_current(
                connection, job
            )
            self._after_package_update_authority_proof(
                connection, job_id=canonical_job_id
            )
            if current_authority_holds:
                identity = self._snapshot_identity_in_transaction(connection, job)
                checkpoint = PackageUpdateCheckpoint(str(job["checkpoint"]))
                if checkpoint is PackageUpdateCheckpoint.PREFLIGHT_PASSED:
                    self._commit_snapshot_intent(
                        connection,
                        job_id=canonical_job_id,
                        identity=identity,
                        recorded_at=recorded_at,
                    )
                elif (
                    checkpoint
                    is not PackageUpdateCheckpoint.SNAPSHOT_MAY_HAVE_STARTED
                ):
                    # Already at the intent checkpoint is idempotent; anything
                    # else has advanced past this transition.
                    raise AuthorityConflict(
                        "package update job snapshot intent requires a passed "
                        "preflight"
                    )

        if not current_authority_holds:
            raise AuthorityConflict(
                "package update job resource or source authority context is stale"
            )
        return self._store.package_update_job(canonical_job_id)

    def _commit_snapshot_intent(
        self,
        connection: sqlite3.Connection,
        *,
        job_id: str,
        identity: PackageUpdateSnapshotIdentity,
        recorded_at: str,
    ) -> None:
        updated = connection.execute(
            "UPDATE package_update_jobs "
            "SET checkpoint='snapshot_may_have_started', "
            "snapshot_operation_id=?, snapshot_name=?, "
            "snapshot_intent_recorded_at=? "
            "WHERE job_id=? AND status='active' "
            "AND checkpoint='preflight_passed' "
            "AND snapshot_operation_id IS NULL",
            (
                identity.snapshot_operation_id,
                identity.snapshot_name,
                recorded_at,
                job_id,
            ),
        )
        if updated.rowcount != 1:
            raise AuthorityConflict(
                "package update job snapshot intent lost durable ownership"
            )
        self._append_package_update_job_event(
            connection,
            job_id=job_id,
            created_at=recorded_at,
            level=PackageUpdateEventLevel.WARNING,
            stage=PackageUpdateCheckpoint.SNAPSHOT_MAY_HAVE_STARTED,
            event_type=PackageUpdateEventType.SNAPSHOT_INTENT_RECORDED,
            message="pre-update snapshot operation may be submitted from here on",
            details={
                "snapshot_operation_id": identity.snapshot_operation_id,
                "snapshot_name": identity.snapshot_name,
            },
        )

    def execute_snapshot_submission_if_current(
        self, job_id: str, submit: Callable[[], _T]
    ) -> _T:
        """Run one bounded host submission callback while authority holds.

        This is the short submission critical section a NEW PVE snapshot
        submission must run inside. It closes the race the write-ahead
        checkpoint alone does not: discovery reconciliation or a package scan
        could otherwise invalidate this job's authority *after* it was proved
        and *before* the host actually submits, in the gap between two
        separate transactions. Re-proving authority and calling the host
        happen here in the SAME transaction instead, so nothing else may
        write to this authority store while ``submit`` runs -- the BEGIN
        IMMEDIATE below holds this store's one writer lock across it.

        ``submit`` MUST be the host's submission-only operation and nothing
        else: no PVE task polling, no package mutation, no rollback, and no
        recursive authority mutation. It is invoked at most once, only when
        current authority still holds, and only while this transaction still
        owns the writer lock. A stale authority context refuses BEFORE
        ``submit`` is ever called, so the host is never asked to submit
        anything for a job whose authority context has already moved on --
        that specific refusal raises :class:`SnapshotSubmissionRefusedBeforeCallback`
        (a distinct, narrow subclass of :class:`AuthorityConflict`), never
        the bare base type, because a caller must be able to tell "current
        authority itself proved false before any host call" apart from a
        terminal job or a wrong checkpoint -- both of which remain ordinary
        :class:`AuthorityConflict` and say nothing about whether ``submit``
        ran. Nothing this method itself does ever recasts an exception
        ``submit`` raises after it begins executing into that narrow type.

        This is not a claim that SQLite and PVE are proved atomic, and it is
        not a claim that a snapshot is proved to belong to the same LXC PVE
        showed several minutes ago. It is the narrower claim
        `app/package_update_snapshot.py` documents: Hubinet's own current
        authority is held stable through the submission boundary, and the
        host independently re-validates the live PVE target immediately
        before it ever submits.

        Recovering evidence about an operation that may already have been
        submitted -- reading the host's durable journal state, and polling a
        known task to completion -- never calls this method: that evidence
        must never be discarded merely because authority has gone stale.
        """

        canonical_job_id = _require_uuid(job_id, "job_id")
        host_result: _T | None = None
        with self._store._transaction() as connection:
            job = self._require_package_update_job_row(connection, canonical_job_id)
            if str(job["status"]) != PackageUpdateJobStatus.ACTIVE.value:
                raise AuthorityConflict("package update job is terminal")
            if (
                PackageUpdateCheckpoint(str(job["checkpoint"]))
                is not PackageUpdateCheckpoint.SNAPSHOT_MAY_HAVE_STARTED
            ):
                raise AuthorityConflict(
                    "package update job is not inside a snapshot operation"
                )
            # The authority proof and the bounded submission callback share
            # this one transaction, so nothing can invalidate the job between
            # the proof just taken and the submission it authorizes.
            current_authority_holds = self._package_update_job_authority_is_current(
                connection, job
            )
            self._after_package_update_authority_proof(
                connection, job_id=canonical_job_id
            )
            if current_authority_holds:
                host_result = submit()

        if not current_authority_holds:
            # This is the one specific, structurally guaranteed case: the
            # current-authority predicate itself proved false, so `submit`
            # was never called (see the `if current_authority_holds:` guard
            # above, inside the same transaction). A distinct typed
            # exception marks this exactly, so a caller may route it into the
            # durable seal/liveness path without conflating it with the
            # terminal-job or wrong-checkpoint conflicts raised earlier in
            # this same method, which say nothing about whether a host call
            # occurred.
            raise SnapshotSubmissionRefusedBeforeCallback(
                "package update job resource or source authority context is stale"
            )
        return host_result  # type: ignore[return-value]

    def record_package_update_snapshot_task(
        self, job_id: str, task_upid: str
    ) -> PackageUpdateJob:
        """Persist the exact PVE task identity observed for this operation.

        Write-once: the same UPID may be recorded again, a different one is a
        conflict. Recording a task never confirms the snapshot.
        """

        canonical_job_id = _require_uuid(job_id, "job_id")
        canonical_upid = _require_pve_upid(task_upid)
        with self._store._transaction() as connection:
            job = self._require_package_update_job_row(connection, canonical_job_id)
            if str(job["status"]) != PackageUpdateJobStatus.ACTIVE.value:
                raise AuthorityConflict("package update job is terminal")
            if (
                PackageUpdateCheckpoint(str(job["checkpoint"]))
                is not PackageUpdateCheckpoint.SNAPSHOT_MAY_HAVE_STARTED
            ):
                raise AuthorityConflict(
                    "package update job is not inside a snapshot operation"
                )
            existing = job["snapshot_task_upid"]
            if existing is not None:
                if str(existing) != canonical_upid:
                    raise AuthorityConflict(
                        "package update job already observed a different PVE "
                        "snapshot task"
                    )
                return self._store.package_update_job(canonical_job_id)
            observed_at = _timestamp(self._now())
            updated = connection.execute(
                "UPDATE package_update_jobs SET snapshot_task_upid=? "
                "WHERE job_id=? AND status='active' "
                "AND checkpoint='snapshot_may_have_started' "
                "AND snapshot_task_upid IS NULL",
                (canonical_upid, canonical_job_id),
            )
            if updated.rowcount != 1:
                raise AuthorityConflict(
                    "package update job snapshot task record lost durable ownership"
                )
            self._append_package_update_job_event(
                connection,
                job_id=canonical_job_id,
                created_at=observed_at,
                level=PackageUpdateEventLevel.INFO,
                stage=PackageUpdateCheckpoint.SNAPSHOT_MAY_HAVE_STARTED,
                event_type=PackageUpdateEventType.SNAPSHOT_TASK_OBSERVED,
                message="observed the PVE task for this snapshot operation",
                details={"snapshot_task_upid": canonical_upid},
            )
        return self._store.package_update_job(canonical_job_id)

    def confirm_package_update_snapshot(
        self, job_id: str, observed: Sequence[ObservedSnapshot]
    ) -> PackageUpdateJob:
        """Confirm the job-owned snapshot from a fresh canonical PVE listing.

        ``observed`` must be a complete, freshly re-read canonical snapshot
        listing for the job's target. A successful PVE task alone is never
        enough: exactly one real, complete snapshot carrying this exact job's
        exact structured ownership metadata under this exact name must be
        present, and the job's current authority context must still hold.
        Every ambiguity fails closed.
        """

        canonical_job_id = _require_uuid(job_id, "job_id")
        snapshots = _require_observed_snapshots(observed)
        confirmed_at = _timestamp(self._now())
        with self._store._transaction() as connection:
            job = self._require_package_update_job_row(connection, canonical_job_id)
            # Confirmation is what makes the snapshot usable as a rollback
            # source, so its authority proof, its ownership derivation, the
            # strict canonical match, and the commit are all one transaction.
            current_authority_holds = self._package_update_job_authority_is_current(
                connection, job
            )
            self._after_package_update_authority_proof(
                connection, job_id=canonical_job_id
            )
            if current_authority_holds:
                checkpoint = PackageUpdateCheckpoint(str(job["checkpoint"]))
                if checkpoint is PackageUpdateCheckpoint.SNAPSHOT_MAY_HAVE_STARTED:
                    snapshot_name = str(job["snapshot_name"])
                    self._require_exactly_one_job_owned_snapshot(
                        job_id=canonical_job_id,
                        expected_name=snapshot_name,
                        observed=snapshots,
                        expected=self._snapshot_ownership_in_transaction(
                            connection, job
                        ),
                    )
                    updated = connection.execute(
                        "UPDATE package_update_jobs "
                        "SET checkpoint='snapshot_confirmed', "
                        "snapshot_confirmed_at=? "
                        "WHERE job_id=? AND status='active' "
                        "AND checkpoint='snapshot_may_have_started' "
                        "AND snapshot_confirmed_at IS NULL AND snapshot_name=?",
                        (confirmed_at, canonical_job_id, snapshot_name),
                    )
                    if updated.rowcount != 1:
                        raise AuthorityConflict(
                            "package update job snapshot confirmation lost "
                            "durable ownership"
                        )
                    self._append_package_update_job_event(
                        connection,
                        job_id=canonical_job_id,
                        created_at=confirmed_at,
                        level=PackageUpdateEventLevel.INFO,
                        stage=PackageUpdateCheckpoint.SNAPSHOT_CONFIRMED,
                        event_type=PackageUpdateEventType.SNAPSHOT_CONFIRMED,
                        message="job-owned pre-update snapshot confirmed canonically",
                        details={"snapshot_name": snapshot_name},
                    )
                elif checkpoint is not PackageUpdateCheckpoint.SNAPSHOT_CONFIRMED:
                    raise AuthorityConflict(
                        "package update job is not inside a snapshot operation"
                    )

        if not current_authority_holds:
            raise AuthorityConflict(
                "package update job resource or source authority context is stale"
            )
        return self._store.package_update_job(canonical_job_id)

    def block_package_update_after_snapshot_success_with_stale_authority(
        self, job_id: str, observed: Sequence[ObservedSnapshot]
    ) -> tuple[bool, PackageUpdateJob]:
        """Retain a proven snapshot but release a job whose authority is stale.

        Confirmation grants rollback authority and therefore keeps its current
        package/resource/source-authority requirement. This resolver instead
        re-proves exact canonical same-job success and stale current authority
        inside one transaction, then terminalizes without confirming. If
        authority is current again, it leaves the job active for confirmation.
        """

        canonical_job_id = _require_uuid(job_id, "job_id")
        snapshots = _require_observed_snapshots(observed)
        blocked = False
        reason = (
            "snapshot exists but current package authority became stale; "
            "retained but not authorized for rollback"
        )
        with self._store._transaction() as connection:
            job = self._require_package_update_job_row(connection, canonical_job_id)
            if str(job["status"]) != PackageUpdateJobStatus.ACTIVE.value:
                raise AuthorityConflict("package update job is terminal")
            if (
                PackageUpdateCheckpoint(str(job["checkpoint"]))
                is not PackageUpdateCheckpoint.SNAPSHOT_MAY_HAVE_STARTED
            ):
                raise AuthorityConflict(
                    "package update job is not inside a snapshot operation"
                )
            if job["snapshot_confirmed_at"] is not None:
                raise AuthorityInvariantError(
                    "package update job snapshot confirmation is inconsistent"
                )

            # Never inherit success from a caught exception or host outcome:
            # prove the exact canonical snapshot again in this transaction.
            self._require_exactly_one_job_owned_snapshot(
                job_id=canonical_job_id,
                expected_name=str(job["snapshot_name"]),
                observed=snapshots,
                expected=self._snapshot_ownership_in_transaction(connection, job),
            )
            try:
                current_authority_holds = (
                    self._package_update_job_authority_is_current(connection, job)
                )
            except AuthorityConflict:
                # Job lifecycle and canonical success were independently
                # established above. Remaining conflicts are failures of the
                # current package/source/resource authority predicate.
                current_authority_holds = False
            self._after_package_update_authority_proof(
                connection, job_id=canonical_job_id
            )
            if not current_authority_holds:
                terminalized_at = _timestamp(self._now())
                updated = connection.execute(
                    "UPDATE package_update_jobs SET status='blocked', "
                    "terminalized_at=?, terminal_reason=? "
                    "WHERE job_id=? AND status='active' "
                    "AND checkpoint='snapshot_may_have_started' "
                    "AND snapshot_confirmed_at IS NULL",
                    (terminalized_at, reason, canonical_job_id),
                )
                if updated.rowcount != 1:
                    raise AuthorityConflict(
                        "stale-authority snapshot retention lost durable ownership"
                    )
                self._append_package_update_job_event(
                    connection,
                    job_id=canonical_job_id,
                    created_at=terminalized_at,
                    level=PackageUpdateEventLevel.WARNING,
                    stage=PackageUpdateCheckpoint.SNAPSHOT_MAY_HAVE_STARTED,
                    event_type=(
                        PackageUpdateEventType.SNAPSHOT_RETAINED_AUTHORITY_STALE
                    ),
                    message=reason,
                    details={"snapshot_name": str(job["snapshot_name"])},
                )
                blocked = True
        return blocked, self._store.package_update_job(canonical_job_id)

    def record_package_update_snapshot_uncertain(
        self, job_id: str, reason: str
    ) -> PackageUpdateJob:
        """Record that a snapshot operation's outcome could not be established.

        Deliberately non-terminal. The job stays active, keeps owning the
        global destructive slot, and keeps its durable evidence, because a
        snapshot operation that may have run is not the same as one that did
        not. Nothing here permits a retry of the mutation.
        """

        canonical_job_id = _require_uuid(job_id, "job_id")
        canonical_reason = _require_text(reason, "reason", max_length=500)
        recorded_at = _timestamp(self._now())
        with self._store._transaction() as connection:
            job = self._require_package_update_job_row(connection, canonical_job_id)
            if str(job["status"]) != PackageUpdateJobStatus.ACTIVE.value:
                raise AuthorityConflict("package update job is terminal")
            if (
                PackageUpdateCheckpoint(str(job["checkpoint"]))
                is not PackageUpdateCheckpoint.SNAPSHOT_MAY_HAVE_STARTED
            ):
                raise AuthorityConflict(
                    "package update job is not inside a snapshot operation"
                )
            self._append_package_update_job_event(
                connection,
                job_id=canonical_job_id,
                created_at=recorded_at,
                level=PackageUpdateEventLevel.ERROR,
                stage=PackageUpdateCheckpoint.SNAPSHOT_MAY_HAVE_STARTED,
                event_type=PackageUpdateEventType.SNAPSHOT_OUTCOME_UNCERTAIN,
                message=canonical_reason,
                details={},
            )
        return self._store.package_update_job(canonical_job_id)

    def fail_package_update_snapshot(
        self, job_id: str, reason: str, observed: Sequence[ObservedSnapshot]
    ) -> PackageUpdateJob:
        """Terminalize a job whose snapshot provably did not come into being.

        Only legal when the caller supplies a fresh canonical listing that
        contains no trace of this job's snapshot -- not even an incomplete or
        wrongly-owned entry under its name. Anything else is uncertainty, and
        uncertainty must not be terminalized.
        """

        canonical_job_id = _require_uuid(job_id, "job_id")
        canonical_reason = _require_text(reason, "reason", max_length=500)
        recorded_at = _timestamp(self._now())
        with self._store._transaction() as connection:
            job_row = self._require_package_update_job_row(
                connection, canonical_job_id
            )
            if str(job_row["status"]) != PackageUpdateJobStatus.ACTIVE.value:
                raise AuthorityConflict("package update job is terminal")
            checkpoint = PackageUpdateCheckpoint(str(job_row["checkpoint"]))
            if checkpoint is not PackageUpdateCheckpoint.SNAPSHOT_MAY_HAVE_STARTED:
                raise AuthorityConflict(
                    "package update job is not inside a snapshot operation"
                )
            expected_name = str(job_row["snapshot_name"])
            for snapshot in _require_observed_snapshots(observed):
                if snapshot.is_current_pseudo_entry:
                    continue
                if snapshot.name == expected_name:
                    raise AuthorityConflict(
                        "canonical PVE state still shows this job's snapshot name; "
                        "the snapshot operation outcome is uncertain, not failed"
                    )
                if (
                    snapshot.ownership is not None
                    and snapshot.ownership.job_id == canonical_job_id
                ) or snapshot.ownership_malformed:
                    raise AuthorityConflict(
                        "canonical PVE state is ambiguous about this job's "
                        "snapshot; the outcome is uncertain, not failed"
                    )
            updated = connection.execute(
                "UPDATE package_update_jobs SET status='blocked', "
                "terminalized_at=?, terminal_reason=? "
                "WHERE job_id=? AND status='active' "
                "AND checkpoint='snapshot_may_have_started'",
                (recorded_at, canonical_reason, canonical_job_id),
            )
            if updated.rowcount != 1:
                raise AuthorityConflict(
                    "package update job snapshot failure lost durable ownership"
                )
            self._append_package_update_job_event(
                connection,
                job_id=canonical_job_id,
                created_at=recorded_at,
                level=PackageUpdateEventLevel.ERROR,
                stage=PackageUpdateCheckpoint.SNAPSHOT_MAY_HAVE_STARTED,
                event_type=PackageUpdateEventType.SNAPSHOT_FAILED,
                message=canonical_reason,
                details={},
            )
        return self._store.package_update_job(canonical_job_id)

    def resolve_pre_submission_block(
        self,
        job_id: str,
        seal: Callable[[], tuple[HostSubmissionState | None, str, _T]],
    ) -> tuple[bool, _T]:
        """Atomically decide, and durably apply, a pre-submission block.

        This is the mirror image of
        :meth:`execute_snapshot_submission_if_current`. That method never
        lets a NEW submission proceed once authority has gone stale; this one
        never lets a "never submitted" proof terminalize a job once a
        concurrent, authorized submission may have crossed the door in the
        gap between the proof and the durable transition -- a check-then-
        commit race just like the one that method itself closes, mirrored
        onto the release path instead of the submission path.

        ``seal`` performs exactly ONE bounded typed host seal operation --
        never a PVE read, resubmission, or poll loop -- and returns
        ``(host_submission_state, reason, evidence)``. It is
        invoked at most once, while this transaction still owns the writer
        lock, so no concurrent :meth:`execute_snapshot_submission_if_current`
        critical section can interleave between the proof and the block:
        whichever critical section acquires the store's one writer lock first
        reaches the host boundary first. The host's own per-VMID lease then
        orders the durable seal against every delayed submitter.

        This is the ONLY way a job past ``snapshot_may_have_started`` may be
        terminalized without canonical PVE evidence, and it exists because the
        write-ahead checkpoint is deliberately committed before the host is
        ever called: an ordinary pre-flight refusal on the host (a guest that
        moved node, say) would otherwise fence the single global destructive
        slot forever, with no PVE mutation having been attempted at all.

        Current package-update authority is deliberately NOT required here:
        this path exists specifically for operations whose authority may
        already be stale, or may never have been proved for this exact call
        at all -- recovering evidence, and releasing a job the host proves it
        never touched, must never depend on a context that may never become
        current again. What IS still enforced is that the job's own durable
        record is consistent with never having been submitted -- active,
        still at the write-ahead checkpoint, no observed PVE task, and no
        confirmed snapshot. A job that ever recorded a task identity provably
        *was* submitted and can never be released down this path, whatever
        ``seal`` reports.

        Returns whether the job was blocked, plus whatever ``seal``
        returned as evidence either way, so the caller can recover through
        the ordinary pipeline when it was not.
        """

        canonical_job_id = _require_uuid(job_id, "job_id")
        evidence: _T | None = None
        blocked = False
        with self._store._transaction() as connection:
            job = self._require_package_update_job_row(connection, canonical_job_id)
            if str(job["status"]) != PackageUpdateJobStatus.ACTIVE.value:
                raise AuthorityConflict("package update job is terminal")
            if (
                PackageUpdateCheckpoint(str(job["checkpoint"]))
                is not PackageUpdateCheckpoint.SNAPSHOT_MAY_HAVE_STARTED
            ):
                raise AuthorityConflict(
                    "package update job is not inside a snapshot operation"
                )
            if job["snapshot_task_upid"] is not None:
                raise AuthorityConflict(
                    "package update job observed a PVE snapshot task, so its "
                    "operation was submitted and cannot be released as unsubmitted"
                )
            if job["snapshot_confirmed_at"] is not None:
                raise AuthorityInvariantError(
                    "package update job snapshot confirmation is inconsistent"
                )
            # The durable host seal happens HERE, while this transaction still
            # owns the writer lock -- never before BEGIN IMMEDIATE, and never
            # trusted if merely supplied by the caller as a precomputed value.
            submission_state, reason, evidence = seal()
            self._after_pre_submission_block_proof(
                connection, job_id=canonical_job_id
            )
            if submission_state is HostSubmissionState.SEALED_NOT_SUBMITTED:
                self._commit_pre_submission_block(
                    connection,
                    job_id=canonical_job_id,
                    reason=_require_text(reason, "reason", max_length=500),
                )
                blocked = True
        return blocked, evidence  # type: ignore[return-value]

    def _commit_pre_submission_block(
        self, connection: sqlite3.Connection, *, job_id: str, reason: str
    ) -> None:
        """Durably terminalize one job as blocked, inside the caller's own
        transaction. Only ever called while that transaction still owns the
        authority store's writer lock -- see
        :meth:`resolve_pre_submission_block`.
        """

        recorded_at = _timestamp(self._now())
        updated = connection.execute(
            "UPDATE package_update_jobs SET status='blocked', "
            "terminalized_at=?, terminal_reason=? "
            "WHERE job_id=? AND status='active' "
            "AND checkpoint='snapshot_may_have_started' "
            "AND snapshot_task_upid IS NULL AND snapshot_confirmed_at IS NULL",
            (recorded_at, reason, job_id),
        )
        if updated.rowcount != 1:
            raise AuthorityConflict(
                "package update job pre-submission block lost durable ownership"
            )
        self._append_package_update_job_event(
            connection,
            job_id=job_id,
            created_at=recorded_at,
            level=PackageUpdateEventLevel.WARNING,
            stage=PackageUpdateCheckpoint.SNAPSHOT_MAY_HAVE_STARTED,
            event_type=(
                PackageUpdateEventType.SNAPSHOT_BLOCKED_BEFORE_SUBMISSION
            ),
            message=reason,
            details={},
        )

    def select_package_update_rollback_target(
        self, job_id: str, observed: Sequence[ObservedSnapshot]
    ) -> PackageUpdateRollbackTarget:
        """Return the ONLY snapshot this exact job may ever roll back to.

        There is no caller-supplied snapshot name, no "latest Hubinet
        snapshot", no "newest pre-update snapshot", and no fallback to another
        job's snapshot. The target is the snapshot this job itself created and
        confirmed, re-proved against a fresh canonical listing every time.
        """

        canonical_job_id = _require_uuid(job_id, "job_id")
        snapshots = _require_observed_snapshots(observed)
        # One read transaction, so the job row and the ownership derived from
        # it are a consistent view of authority.
        with self._store._transaction() as connection:
            job_row = self._require_package_update_job_row(
                connection, canonical_job_id
            )
            expected_ownership = self._snapshot_ownership_in_transaction(
                connection, job_row
            )
        job = self._store.package_update_job(canonical_job_id)
        if job.status is not PackageUpdateJobStatus.ACTIVE:
            # A terminal job never rolls anything back. Its snapshot is
            # retained, but retention is not authorization.
            raise AuthorityConflict(
                "package update job is terminal and cannot roll back"
            )
        if job.snapshot_confirmed_at is None or job.snapshot_name is None:
            raise AuthorityConflict(
                "package update job has no confirmed job-owned snapshot"
            )
        if (
            _checkpoint_rank(job.checkpoint)
            < _checkpoint_rank(PackageUpdateCheckpoint.SNAPSHOT_CONFIRMED)
        ):
            raise AuthorityInvariantError(
                "package update job snapshot confirmation is inconsistent"
            )
        self._require_exactly_one_job_owned_snapshot(
            job_id=canonical_job_id,
            expected_name=job.snapshot_name,
            observed=snapshots,
            expected=expected_ownership,
        )
        return PackageUpdateRollbackTarget(
            job_id=canonical_job_id,
            resource_id=job.resource_id,
            expected_vmid=job.expected_vmid,
            expected_node_name=job.expected_node_name,
            snapshot_name=job.snapshot_name,
            snapshot_operation_id=str(job.snapshot_operation_id),
            snapshot_confirmed_at=job.snapshot_confirmed_at,
        )

    @staticmethod
    def _require_exactly_one_job_owned_snapshot(
        *,
        job_id: str,
        expected_name: str | None,
        observed: Sequence[ObservedSnapshot],
        expected: SnapshotOwnership,
    ) -> ObservedSnapshot:
        """Fail closed unless the canonical listing proves exactly one owner.

        Ambiguity is never resolved in the job's favour: a duplicate, an
        incomplete entry, a name collision with foreign metadata, or a
        Hubinet-looking snapshot whose metadata did not parse all refuse.
        """

        if expected_name is None:
            raise AuthorityInvariantError(
                "package update job has no persisted snapshot identity"
            )
        matches: list[ObservedSnapshot] = []
        for snapshot in _require_observed_snapshots(observed):
            if snapshot.is_current_pseudo_entry:
                if snapshot.name == expected_name:
                    raise AuthorityConflict(
                        "canonical PVE state reports the job snapshot name as the "
                        "current pseudo-entry"
                    )
                continue
            claims_this_job = (
                snapshot.ownership is not None
                and snapshot.ownership.job_id == job_id
            )
            if snapshot.name != expected_name and not claims_this_job:
                if snapshot.ownership_malformed:
                    # A malformed Hubinet-looking snapshot elsewhere on this
                    # guest cannot be attributed, so it cannot be ruled out as
                    # a second claim on this job either.
                    raise AuthorityConflict(
                        "canonical PVE state contains malformed Hubinet snapshot "
                        "metadata; job-owned snapshot ownership is ambiguous"
                    )
                continue
            if snapshot.ownership_malformed:
                raise AuthorityConflict(
                    "job-owned snapshot name carries malformed Hubinet metadata"
                )
            if snapshot.name != expected_name:
                raise AuthorityConflict(
                    "another snapshot claims this job under a different name"
                )
            if snapshot.incomplete:
                raise AuthorityConflict(
                    "canonical PVE state reports the job-owned snapshot as "
                    "incomplete"
                )
            if snapshot.ownership != expected:
                raise AuthorityConflict(
                    "job-owned snapshot name does not carry this job's exact "
                    "ownership metadata"
                )
            matches.append(snapshot)
        if not matches:
            raise AuthorityConflict(
                "canonical PVE state does not contain this job's snapshot"
            )
        if len(matches) != 1:
            raise AuthorityConflict(
                "canonical PVE state contains duplicate job-owned snapshots"
            )
        return matches[0]

    @staticmethod
    def _require_active_execution_gate_job(job: sqlite3.Row) -> None:
        """Shared checkpoint guard for the execution-time plan gate.

        The job must be exactly ACTIVE at ``snapshot_confirmed`` -- the only
        window this gate is ever meaningful in. A job that has since gone
        terminal for some other reason, or has not yet reached this
        checkpoint, is never overwritten or reopened: this raises an
        ordinary :class:`AuthorityConflict` and the caller's transaction
        writes nothing.
        """

        if str(job["status"]) != PackageUpdateJobStatus.ACTIVE.value:
            raise AuthorityConflict("package update job is terminal")
        if (
            PackageUpdateCheckpoint(str(job["checkpoint"]))
            is not PackageUpdateCheckpoint.SNAPSHOT_CONFIRMED
        ):
            raise AuthorityConflict(
                "package update job is not awaiting the execution-time plan gate"
            )

    def _terminalize_execution_gate_job_if_authority_stale(
        self,
        connection: sqlite3.Connection,
        job: sqlite3.Row,
        decided_at: str,
    ) -> bool:
        """Re-prove current authority; terminalize BLOCKED if it is stale.

        Must be called from inside a transaction that already re-read the
        job row and proved it is ACTIVE at ``snapshot_confirmed`` in THIS
        same transaction -- the proof here and any terminalization it
        authorizes are therefore atomic with each other: no other writer can
        interleave between "authority is proven stale" and "the job is
        released", and a caller that finds authority current here is
        guaranteed that fact was true at commit time, not from some earlier,
        possibly stale, read.

        The job is exactly ACTIVE at ``snapshot_confirmed`` here by
        construction (no package mutation has begun), so terminalizing it is
        always pre-mutation-safe: the confirmed snapshot is retained,
        ``mutation_may_have_started_at`` stays NULL, and the job releases
        the one global destructive slot without ever gaining rollback
        authority. This is a deliberately conservative policy: current
        authority for THIS job's exact frozen material may in principle
        become available again later (a fresh scan could reproduce it), but
        the operator can always issue and approve a fresh plan/job, and
        leaving a provably stale job ACTIVE would otherwise be able to
        starve the global slot forever with no path back except a backend
        restart -- which must never be the ordinary release mechanism (see
        ARCHITECTURE.md, "Execution-time plan equality").

        Returns ``True`` (and has terminalized the job) when authority was
        proven stale here; ``False`` (job unchanged) when it is current.
        """

        current_authority_holds = self._package_update_job_authority_is_current(
            connection, job
        )
        job_id = str(job["job_id"])
        self._after_package_update_authority_proof(connection, job_id=job_id)
        if current_authority_holds:
            return False
        reason = (
            "package update job's current resource/source/approval authority "
            "became stale before package mutation; retained but released for "
            "a fresh plan and job"
        )
        updated = connection.execute(
            "UPDATE package_update_jobs SET status='blocked', "
            "terminalized_at=?, terminal_reason=? "
            "WHERE job_id=? AND status='active' AND checkpoint='snapshot_confirmed'",
            (decided_at, reason, job_id),
        )
        if updated.rowcount != 1:
            raise AuthorityConflict(
                "package update job stale-authority release lost durable ownership"
            )
        self._append_package_update_job_event(
            connection,
            job_id=job_id,
            created_at=decided_at,
            level=PackageUpdateEventLevel.WARNING,
            stage=PackageUpdateCheckpoint.SNAPSHOT_CONFIRMED,
            event_type=PackageUpdateEventType.EXECUTION_AUTHORITY_STALE_RELEASED,
            message=reason,
            details={},
        )
        return True

    def revalidate_or_release_stale_package_update_execution(
        self, job_id: str
    ) -> tuple[bool, PackageUpdateJob]:
        """Cheap pre-host authority check for the execution-time plan gate.

        Intended as an optimization immediately before the (potentially
        multi-minute) host round trip: avoid spending it on a job authority
        has already moved past. Requires the job ACTIVE at exactly
        ``snapshot_confirmed`` (an ordinary :class:`AuthorityConflict`
        otherwise, job untouched), then atomically re-proves current
        authority and, if it is stale, terminalizes the job the exact same
        way :meth:`evaluate_package_update_execution_plan` would -- see
        :meth:`_terminalize_execution_gate_job_if_authority_stale` and
        ARCHITECTURE.md, "Execution-time plan equality". This is deliberately
        a distinct, narrower check from :meth:`revalidate_package_update_job`
        (which never terminalizes anything and remains a pure, generic
        "is authority still current" read usable at any checkpoint).

        Returns ``(True, job)`` when authority is current (job unchanged) or
        ``(False, job)`` when it was just proven stale and released.
        """

        canonical_job_id = _require_uuid(job_id, "job_id")
        decided_at = _timestamp(self._now())
        released = False
        with self._store._transaction() as connection:
            job = self._require_package_update_job_row(connection, canonical_job_id)
            self._require_active_execution_gate_job(job)
            released = self._terminalize_execution_gate_job_if_authority_stale(
                connection, job, decided_at
            )
        return (not released), self._store.package_update_job(canonical_job_id)

    def evaluate_package_update_execution_plan(
        self,
        job_id: str,
        fresh_packages: tuple[PackageScanPackage, ...],
    ) -> tuple[PackageUpdateExecutionOutcome, PackageUpdateJob]:
        """Compare a fresh execution-time APT plan against one job's frozen material.

        This is the equality half of the execution-time gate (see
        ``ARCHITECTURE.md``, "Execution-time plan equality"). ``fresh_packages``
        MUST already be the canonical parse of a metadata-refreshed, freshly
        re-simulated APT upgrade read from the live guest: this method
        performs no host I/O of its own, so it can run inside one short
        authority-store writer transaction instead of holding that lock
        across the (potentially multi-minute) host round trip a caller must
        perform first, outside any transaction -- exactly like every other
        snapshot-safety transition holds the writer lock only across a
        single bounded operation, never across PVE's own asynchronous work.

        The job must currently be ACTIVE at exactly ``snapshot_confirmed``:
        this gate exists specifically for the window after the job's own
        snapshot is confirmed and before any package mutation. A job that
        has since gone terminal for some other reason, or has not yet
        reached this checkpoint, is never overwritten or reopened -- that
        raises an ordinary :class:`AuthorityConflict` and leaves the job
        exactly as it was.

        Current job/source/resource/approval authority is re-proved inside
        the SAME transaction as the comparison and any terminal write, so
        nothing can invalidate the job between the proof and the decision it
        authorizes. Unlike most other package-update transitions, a stale
        authority context here does NOT merely refuse: it terminalizes the
        job as ``blocked`` in this same transaction (see
        :meth:`_terminalize_execution_gate_job_if_authority_stale`), because
        leaving a provably stale job ACTIVE at this pre-mutation checkpoint
        would otherwise starve the one global destructive slot forever, with
        a backend restart as the only way out -- which must never be the
        ordinary release mechanism. This returns
        :attr:`PackageUpdateExecutionOutcome.AUTHORITY_STALE` for that case.

        An exact material match -- the complete frozen job material set
        equals the complete fresh material set, never subset/superset/
        name-only matching -- returns
        :attr:`PackageUpdateExecutionOutcome.MATCHED` and changes nothing
        durable about the job: no checkpoint advances, no new persisted flag
        is written, and it remains exactly at ``snapshot_confirmed``. A
        successful match is evidence for this one invocation, never a
        timeless mutation permit (see ``PRODUCT.md`` rule 2); only a
        non-authorizing diagnostic event is appended. The next stage that
        actually mutates packages MUST re-run this exact gate immediately
        before it does, not trust this result from earlier.

        Any mismatch -- an added, removed, or changed package, an
        architecture change, or an empty fresh plan where the job is
        non-empty -- returns :attr:`PackageUpdateExecutionOutcome.MISMATCHED`
        and terminalizes the job as ``blocked`` in the same transaction: the
        job's already-confirmed snapshot is retained, but the job releases
        the global destructive slot and never gains rollback authority. The
        operator must obtain a current scan/plan and approve it again.
        """

        canonical_job_id = _require_uuid(job_id, "job_id")
        fresh_material = frozenset(
            (
                package.package_name,
                package.architecture,
                package.installed_version,
                package.candidate_version,
            )
            for package in _validate_package_plan(fresh_packages)
        )
        decided_at = _timestamp(self._now())
        outcome: PackageUpdateExecutionOutcome | None = None
        with self._store._transaction() as connection:
            job = self._require_package_update_job_row(connection, canonical_job_id)
            self._require_active_execution_gate_job(job)
            # The authority proof (and, on staleness or mismatch, the
            # terminal write) share this one transaction with the equality
            # decision, so nothing can invalidate the job between the proof
            # just taken and the decision it authorizes.
            if self._terminalize_execution_gate_job_if_authority_stale(
                connection, job, decided_at
            ):
                outcome = PackageUpdateExecutionOutcome.AUTHORITY_STALE
            else:
                job_material = self._package_material_rows(
                    connection,
                    table="package_update_job_packages",
                    owner_column="job_id",
                    owner_id=canonical_job_id,
                )
                if job_material and job_material == fresh_material:
                    outcome = PackageUpdateExecutionOutcome.MATCHED
                    self._append_package_update_job_event(
                        connection,
                        job_id=canonical_job_id,
                        created_at=decided_at,
                        level=PackageUpdateEventLevel.INFO,
                        stage=PackageUpdateCheckpoint.SNAPSHOT_CONFIRMED,
                        event_type=PackageUpdateEventType.EXECUTION_PLAN_VERIFIED,
                        message=(
                            "fresh execution-time APT simulation exactly "
                            "matched this job's frozen approved material"
                        ),
                        details={"package_count": len(job_material)},
                    )
                else:
                    outcome = PackageUpdateExecutionOutcome.MISMATCHED
                    reason = (
                        "fresh execution-time APT simulation no longer "
                        "exactly matches this job's frozen approved material"
                    )
                    updated = connection.execute(
                        "UPDATE package_update_jobs SET status='blocked', "
                        "terminalized_at=?, terminal_reason=? "
                        "WHERE job_id=? AND status='active' "
                        "AND checkpoint='snapshot_confirmed'",
                        (decided_at, reason, canonical_job_id),
                    )
                    if updated.rowcount != 1:
                        raise AuthorityConflict(
                            "package update job execution-plan mismatch "
                            "lost durable ownership"
                        )
                    self._append_package_update_job_event(
                        connection,
                        job_id=canonical_job_id,
                        created_at=decided_at,
                        level=PackageUpdateEventLevel.ERROR,
                        stage=PackageUpdateCheckpoint.SNAPSHOT_CONFIRMED,
                        event_type=PackageUpdateEventType.EXECUTION_PLAN_MISMATCH,
                        message=reason,
                        details={
                            "job_package_count": len(job_material),
                            "fresh_package_count": len(fresh_material),
                        },
                    )

        if outcome is None:
            raise AuthorityInvariantError(
                "execution-time plan comparison was not captured"
            )
        return outcome, self._store.package_update_job(canonical_job_id)

    #: Checkpoints from which ordinary startup may safely terminalize an
    #: active job. Every one of them is provably before any PVE snapshot
    #: submission *and* before any package mutation:
    #:
    #: - ``issued``/``preflight_passed`` -- nothing was ever submitted.
    #: - ``snapshot_confirmed`` -- a snapshot exists and is retained, but no
    #:   package mutation has begun, so interrupting is safe.
    #:
    #: ``snapshot_may_have_started`` is deliberately absent: a snapshot
    #: operation may already be in flight, so the job stays active and fenced.
    _STARTUP_INTERRUPTIBLE_CHECKPOINTS = (
        PackageUpdateCheckpoint.ISSUED,
        PackageUpdateCheckpoint.PREFLIGHT_PASSED,
        PackageUpdateCheckpoint.SNAPSHOT_CONFIRMED,
    )

    def recover_interrupted_package_update_jobs(self) -> tuple[str, ...]:
        """Interrupt provably-safe jobs; preserve every uncertain state intact.

        Ordinary production startup terminalizes only the checkpoints in
        ``_STARTUP_INTERRUPTIBLE_CHECKPOINTS``. A job at
        ``snapshot_may_have_started`` may already have submitted a PVE
        snapshot mutation, and a job at or beyond ``mutation_may_have_started``
        may already have mutated packages; both are left active, keep owning
        the global destructive slot, and keep their durable evidence. This
        pass never replays a snapshot operation, never guesses an outcome, and
        never silently frees destructive ownership. Repeating it is
        idempotent: an already-terminal job is not selected again.
        """

        recovered_at = _timestamp(self._now())
        pre_mutation = tuple(
            checkpoint.value
            for checkpoint in self._STARTUP_INTERRUPTIBLE_CHECKPOINTS
        )
        with self._store._transaction() as connection:
            rows = connection.execute(
                "SELECT job_id, checkpoint FROM package_update_jobs "
                "WHERE status='active' AND checkpoint IN "
                f"({', '.join('?' * len(pre_mutation))}) "
                "ORDER BY issued_at, job_id",
                pre_mutation,
            ).fetchall()
            recovered = tuple(str(row["job_id"]) for row in rows)
            for row in rows:
                job_id = str(row["job_id"])
                reason = "backend restarted before package mutation began"
                updated = connection.execute(
                    "UPDATE package_update_jobs SET status='interrupted', "
                    "terminalized_at=?, terminal_reason=? "
                    "WHERE job_id=? AND status='active'",
                    (recovered_at, reason, job_id),
                )
                if updated.rowcount != 1:
                    raise AuthorityConflict(
                        "package update job recovery lost durable ownership"
                    )
                self._append_package_update_job_event(
                    connection,
                    job_id=job_id,
                    created_at=recovered_at,
                    level=PackageUpdateEventLevel.WARNING,
                    stage=PackageUpdateCheckpoint(str(row["checkpoint"])),
                    event_type=PackageUpdateEventType.RESTART_INTERRUPTED,
                    message=reason,
                    details={},
                )
        return recovered

    @staticmethod
    def _latest_package_scan_row(
        connection: sqlite3.Connection, resource_id: str
    ) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM package_scan_runs WHERE resource_id=? "
            "ORDER BY attempt_sequence DESC LIMIT 1",
            (resource_id,),
        ).fetchone()

    @staticmethod
    def _require_package_update_job_row(
        connection: sqlite3.Connection, job_id: str
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM package_update_jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        if row is None:
            raise AuthorityNotFound("package update job does not exist")
        return row

    @staticmethod
    def _package_scan_context_matches_job(
        current: sqlite3.Row, job: sqlite3.Row
    ) -> bool:
        fields = (
            "resource_id",
            "inventory_source_id",
            "committed_source_config_revision",
            "committed_endpoint_id",
            "committed_canonical_transport_locator",
            "committed_canonicalization_contract_version",
            "committed_transport_trust_revision",
            "provider_contract_version",
            "expected_binding_id",
            "expected_locator_generation",
            "expected_resource_continuity_revision",
            "expected_vmid",
            "expected_node_id",
            "expected_node_name",
        )
        return all(current[field] == job[field] for field in fields)

    @staticmethod
    def _package_material_rows(
        connection: sqlite3.Connection,
        *,
        table: str,
        owner_column: str,
        owner_id: str,
    ) -> frozenset[tuple[str, str, str, str]]:
        """Read one owner's complete material set: (name, arch, installed, candidate).

        A ``frozenset``, not an ordered tuple: material equality is a set
        comparison over the complete rows, never an ordering-sensitive one
        (row order is presentation, not material -- see
        ``package_plan_fingerprint``), and never subset/superset matching.
        The (package_name, architecture) part of each row is unique by
        construction (the ``UNIQUE(scan_run_id/job_id, package_name,
        architecture)`` constraint and :func:`_validate_package_plan`'s own
        duplicate check), so the frozenset never silently collapses two
        distinct rows.
        """

        allowed = {
            ("package_scan_packages", "scan_run_id"),
            ("package_update_job_packages", "job_id"),
        }
        if (table, owner_column) not in allowed:
            raise AuthorityInvariantError("unsupported package material source")
        rows = connection.execute(
            f"SELECT package_name, architecture, installed_version, candidate_version "
            f"FROM {table} WHERE {owner_column}=? ORDER BY package_index",
            (owner_id,),
        ).fetchall()
        return frozenset(
            (
                str(row["package_name"]),
                str(row["architecture"]),
                str(row["installed_version"]),
                str(row["candidate_version"]),
            )
            for row in rows
        )

    @staticmethod
    def _append_package_update_job_event(
        connection: sqlite3.Connection,
        *,
        job_id: str,
        created_at: str,
        level: PackageUpdateEventLevel,
        stage: PackageUpdateCheckpoint,
        event_type: PackageUpdateEventType,
        message: str,
        details: Mapping[str, object],
    ) -> None:
        canonical_message = _require_text(message, "event message", max_length=500)
        if not isinstance(details, Mapping):
            raise ValueError("event details must be a mapping")
        details_json = json.dumps(
            dict(details), ensure_ascii=True, sort_keys=True, separators=(",", ":")
        )
        if len(details_json) > 4000:
            raise ValueError("event details exceed the durable bound")
        sequence = int(
            connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 "
                "FROM package_update_job_events WHERE job_id=?",
                (job_id,),
            ).fetchone()[0]
        )
        connection.execute(
            "INSERT INTO package_update_job_events("
            "job_id, sequence, created_at, level, stage, event_type, message, "
            "details_json) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            (
                job_id,
                sequence,
                created_at,
                level.value,
                stage.value,
                event_type.value,
                canonical_message,
                details_json,
            ),
        )

    def _after_package_update_authority_proof(
        self, connection: sqlite3.Connection, *, job_id: str
    ) -> None:
        """Test seam inside a transition transaction, after the authority proof.

        Fires between proving current authority and committing the transition
        it authorizes. Both are in one transaction, so this is exactly the
        point where a check-then-commit race would have been possible.
        """

    def _after_package_update_job_issuance(
        self, connection: sqlite3.Connection, *, job_id: str
    ) -> None:
        """Test seam after all issuance writes, inside the transaction."""

    def _after_pre_submission_block_proof(
        self, connection: sqlite3.Connection, *, job_id: str
    ) -> None:
        """Test seam inside :meth:`resolve_pre_submission_block`'s transaction,
        after the durable host seal and before the backend block it may
        authorize. Both are in one transaction, so this is exactly the point
        where an interleaving submission critical section would otherwise be
        able to race the block.
        """

    @staticmethod
    def _require_package_scan_target(
        connection: sqlite3.Connection, resource_id: str
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT r.*, b.binding_id, b.locator_generation, n.external_node_name "
            "FROM resource_incarnations r "
            "LEFT JOIN resource_locator_bindings b ON b.resource_id=r.resource_id "
            "AND b.valid_to_run_sequence IS NULL "
            "LEFT JOIN inventory_nodes n ON n.node_id=r.current_node_id "
            "WHERE r.resource_id=?",
            (resource_id,),
        ).fetchone()
        if row is None:
            raise AuthorityNotFound("package scan resource does not exist")
        if (
            row["presence"] != "present"
            or row["lifecycle"] != "active"
            or row["binding_id"] is None
            or row["current_node_id"] is None
            or row["external_node_name"] is None
        ):
            raise AuthorityConflict("resource has no current executable binding")
        return row

    @staticmethod
    def _require_package_scan_run_row(
        connection: sqlite3.Connection, scan_run_id: str
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM package_scan_runs WHERE scan_run_id=?", (scan_run_id,)
        ).fetchone()
        if row is None:
            raise AuthorityNotFound("package scan run does not exist")
        return row

    @staticmethod
    def _require_running_package_scan(run: sqlite3.Row) -> None:
        if str(run["lifecycle"]) != PackageScanLifecycle.RUNNING.value:
            raise AuthorityConflict("package scan run is already terminal")

    @staticmethod
    def _package_scan_context_is_current(
        connection: sqlite3.Connection, run: sqlite3.Row
    ) -> bool:
        current = connection.execute(
            "SELECT r.resource_id, r.inventory_source_id, r.vmid, "
            "r.resource_continuity_revision, r.resource_type, r.presence, r.lifecycle, "
            "r.status, r.current_node_id, b.binding_id, b.locator_generation, "
            "n.external_node_name, n.available AS node_available "
            "FROM resource_incarnations r "
            "LEFT JOIN resource_locator_bindings b ON b.resource_id=r.resource_id "
            "AND b.valid_to_run_sequence IS NULL "
            "LEFT JOIN inventory_nodes n ON n.node_id=r.current_node_id "
            "WHERE r.resource_id=?",
            (str(run["resource_id"]),),
        ).fetchone()
        if current is None:
            return False
        if current["binding_id"] is None:
            return False
        # A missing/quarantined resource (or one whose node reference has not
        # resolved) retains no current node, so these joined fields are NULL.
        # Reject before any int()/comparison coercion so nullable legal state
        # fails closed instead of raising.
        if current["current_node_id"] is None:
            return False
        if current["node_available"] is None:
            return False
        return all(
            (
                str(current["inventory_source_id"]) == str(run["inventory_source_id"]),
                str(current["resource_type"]) == "lxc",
                str(current["presence"]) == "present",
                str(current["lifecycle"]) == "active",
                str(current["status"]) == "running",
                int(current["vmid"]) == int(run["expected_vmid"]),
                current["binding_id"] == run["expected_binding_id"],
                int(current["locator_generation"]) == int(run["expected_locator_generation"]),
                int(current["resource_continuity_revision"])
                == int(run["expected_resource_continuity_revision"]),
                current["current_node_id"] == run["expected_node_id"],
                current["external_node_name"] == run["expected_node_name"],
                current["node_available"] == 1,
            )
        )

    def _package_scan_source_context_is_current(
        self, connection: sqlite3.Connection, run: sqlite3.Row
    ) -> bool:
        source_id = str(run["inventory_source_id"])
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
        return all(
            (
                self._source_has_current_authority_in_transaction(
                    connection, source_id
                ),
                int(run["committed_source_config_revision"])
                == int(health["committed_source_config_revision"]),
                str(run["committed_endpoint_id"])
                == str(health["committed_endpoint_id"]),
                str(run["committed_canonical_transport_locator"])
                == str(health["committed_canonical_transport_locator"]),
                int(run["committed_canonicalization_contract_version"])
                == int(health["committed_canonicalization_contract_version"]),
                int(run["committed_transport_trust_revision"])
                == int(health["committed_transport_trust_revision"]),
                int(run["provider_contract_version"])
                == int(source["provider_contract_version"]),
                str(run["committed_endpoint_id"]) == str(endpoint["endpoint_id"]),
            )
        )

    def _successful_package_scan_fingerprint(
        self, connection: sqlite3.Connection, run: sqlite3.Row
    ) -> str:
        rows = connection.execute(
            "SELECT * FROM package_scan_packages WHERE scan_run_id=? "
            "ORDER BY package_index",
            (str(run["scan_run_id"]),),
        ).fetchall()
        pending_count = run["pending_count"]
        if pending_count is None or int(pending_count) != len(rows):
            raise AuthorityInvariantError(
                "successful package scan pending count does not match exact rows"
            )
        packages = tuple(
            PackageScanPackage(
                package_name=str(row["package_name"]),
                architecture=str(row["architecture"]),
                installed_version=str(row["installed_version"]),
                candidate_version=str(row["candidate_version"]),
                origin=row["origin"],
                description=row["description"],
                security=True if row["security"] == 1 else None,
            )
            for row in rows
        )
        recomputed = package_plan_fingerprint(packages)
        if recomputed != str(run["plan_fingerprint"]):
            raise AuthorityInvariantError(
                "successful package scan fingerprint does not match exact rows"
            )
        return recomputed

    def _package_scan_is_current_and_approvable(
        self, connection: sqlite3.Connection, run: sqlite3.Row
    ) -> bool:
        if (
            str(run["lifecycle"]) != PackageScanLifecycle.COMPLETED.value
            or str(run["outcome"]) != PackageScanOutcome.SUCCESS.value
        ):
            return False
        self._successful_package_scan_fingerprint(connection, run)
        return self._package_scan_context_is_current(
            connection, run
        ) and self._package_scan_source_context_is_current(connection, run)

    @staticmethod
    def _package_scan_context_matches_reviewed(
        current: sqlite3.Row, reviewed: sqlite3.Row
    ) -> bool:
        fields = (
            "resource_id",
            "inventory_source_id",
            "committed_source_config_revision",
            "committed_endpoint_id",
            "committed_canonical_transport_locator",
            "committed_canonicalization_contract_version",
            "committed_transport_trust_revision",
            "provider_contract_version",
            "expected_binding_id",
            "expected_locator_generation",
            "expected_resource_continuity_revision",
            "expected_vmid",
            "expected_node_id",
            "expected_node_name",
        )
        return all(current[field] == reviewed[field] for field in fields)

    def _after_package_plan_approval_write(
        self, connection: sqlite3.Connection, *, resource_id: str
    ) -> None:
        """Test seam after the approval write, still inside its transaction."""

    @staticmethod
    def _complete_failed_package_scan(
        connection: sqlite3.Connection,
        scan_run_id: str,
        *,
        completed_at: str,
        outcome: PackageScanOutcome,
        failure_class: PackageScanFailure,
        error_message: str,
        os_id: str | None,
        os_version: str | None,
    ) -> None:
        connection.execute(
            "UPDATE package_scan_runs SET lifecycle='completed', completed_at=?, "
            "outcome=?, failure_class=?, error_message=?, os_id=?, os_version=? "
            "WHERE scan_run_id=?",
            (
                completed_at,
                outcome.value,
                failure_class.value,
                error_message,
                os_id,
                os_version,
                scan_run_id,
            ),
        )

    def _authority_decision_time(self) -> datetime:
        return _parse_timestamp(_timestamp(self._now()), "authority decision time")

    def _source_has_current_authority_in_transaction(
        self, connection: sqlite3.Connection, source_id: str
    ) -> bool:
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


_PVE_UPID_RE = re.compile(
    r"UPID:(?P<node>[a-zA-Z0-9](?:[a-zA-Z0-9\-]*[a-zA-Z0-9])?)"
    r":[0-9A-Fa-f]{8}:[0-9A-Fa-f]{8,9}:[0-9A-Fa-f]{8}"
    r":[^:\s/]+:[^:\s/]*:[^:\s/]+:"
)


def _require_pve_upid(value: str) -> str:
    """Validate a PVE task identity against PVE's own UPID grammar.

    Mirrors ``PVE::UPID::decode`` (``pve-common``): the trailing colon is part
    of the format, and a value that does not decode is never a task identity
    we may later poll.
    """

    if (
        not isinstance(value, str)
        or len(value) > 300
        or not _PVE_UPID_RE.fullmatch(value)
    ):
        raise ValueError("task_upid must be a canonical PVE UPID")
    return value


def _require_observed_snapshots(
    observed: Sequence[ObservedSnapshot],
) -> tuple[ObservedSnapshot, ...]:
    if isinstance(observed, (str, bytes)) or not isinstance(observed, Sequence):
        raise ValueError("observed snapshots must be a sequence")
    snapshots = tuple(observed)
    if not all(isinstance(item, ObservedSnapshot) for item in snapshots):
        raise ValueError("observed snapshots must be ObservedSnapshot values")
    return snapshots


def _require_package_plan_fingerprint(value: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(
            "reviewed_plan_fingerprint must be a lowercase SHA-256 fingerprint"
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


def _optional_bounded_text(
    value: str | None, field_name: str, *, max_length: int
) -> str | None:
    if value is None:
        return None
    return _require_text(value, field_name, max_length=max_length)


#: A dpkg/APT architecture string: 'all' (Architecture: all) or a real
#: architecture triplet such as 'amd64', 'i386', 'arm64'. Lowercase
#: alphanumeric segments joined by single hyphens, matching what
#: ``pkgCache::VerIterator::Arch()`` ever actually produces -- never
#: guessed, and never inferred from caller-supplied text. See
#: ARCHITECTURE.md, "Binary package identity".
_ARCHITECTURE_RE = re.compile(r"[a-z][a-z0-9]*(-[a-z0-9]+)*")


def _require_architecture(value: str) -> str:
    if (
        not isinstance(value, str)
        or not (2 <= len(value) <= 32)
        or not _ARCHITECTURE_RE.fullmatch(value)
    ):
        raise ValueError("architecture must be a bounded lowercase dpkg architecture")
    return value


def _validate_package_plan(
    packages: tuple[PackageScanPackage, ...],
) -> tuple[PackageScanPackage, ...]:
    if not isinstance(packages, tuple):
        raise ValueError("packages must be a tuple")
    normalized: list[PackageScanPackage] = []
    identities: set[tuple[str, str]] = set()
    for package in packages:
        if not isinstance(package, PackageScanPackage):
            raise ValueError("packages must contain PackageScanPackage values")
        name = _require_text(package.package_name, "package_name", max_length=300)
        architecture = _require_architecture(package.architecture)
        installed = _require_text(
            package.installed_version, "installed_version", max_length=500
        )
        candidate = _require_text(
            package.candidate_version, "candidate_version", max_length=500
        )
        identity = (name, architecture)
        if identity in identities:
            raise ValueError(
                "package plan contains a duplicate (package_name, architecture)"
            )
        identities.add(identity)
        if package.security not in {True, None}:
            raise ValueError("package security must be true or unknown")
        normalized.append(
            PackageScanPackage(
                package_name=name,
                architecture=architecture,
                installed_version=installed,
                candidate_version=candidate,
                origin=_optional_bounded_text(
                    package.origin, "origin", max_length=500
                ),
                description=_optional_bounded_text(
                    package.description, "description", max_length=500
                ),
                security=package.security,
            )
        )
    return tuple(
        sorted(normalized, key=lambda package: (package.package_name, package.architecture))
    )


def package_plan_fingerprint(packages: tuple[PackageScanPackage, ...]) -> str:
    """SHA-256 of canonical JSON for only the exact material package plan.

    The material tuple is ``(package_name, architecture, installed_version,
    candidate_version)``. Architecture is material identity, not
    presentation metadata: two rows with the same name and versions but
    different architectures are two different binary packages (see
    ARCHITECTURE.md, "Binary package identity") and therefore two different
    plans. Row ordering, origin, description, and security never affect the
    fingerprint.
    """

    canonical = _validate_package_plan(packages)
    payload = [
        {
            "architecture": package.architecture,
            "candidate_version": package.candidate_version,
            "installed_version": package.installed_version,
            "package_name": package.package_name,
        }
        for package in canonical
    ]
    encoded = json.dumps(
        payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


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
