"""Cross-snapshot successor validation for observable Phase 0 views."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .enums import (
    LifecycleState,
    PresenceState,
    SecurityContinuity,
    SourceFreshness,
)

if TYPE_CHECKING:
    from .models import HubinetOpsSnapshot


def validate_transition(previous: "HubinetOpsSnapshot", current: "HubinetOpsSnapshot") -> None:
    """Reject regressing or mutable views for an existing backend entry."""

    self = current
    if self.backend.backend_instance_id != previous.backend.backend_instance_id:
        raise ValueError("snapshot belongs to a different backend instance")
    if self.inventory_revision < previous.inventory_revision:
        raise ValueError("inventory_revision must not regress")
    if self.published_state_revision < previous.published_state_revision:
        raise ValueError("published_state_revision must not regress")
    if (
        self.published_state_revision == previous.published_state_revision
        and self != previous
    ):
        raise ValueError("one published_state_revision must identify one immutable view")

    previous_sources_by_id = previous.sources_by_id
    previous_nodes_by_id = previous.nodes_by_id
    previous_resources_by_id = previous.resources_by_id
    previous_reconciliation_projection = (
        previous.source_reconciliation_projection
    )
    reconciliation_projection = self.source_reconciliation_projection
    reconciliation_changed_source_ids = {
        source_id
        for source_id in set(previous_sources_by_id) & set(self.sources_by_id)
        if reconciliation_projection[source_id]
        != previous_reconciliation_projection[source_id]
    }
    current_source_ids = set(self.sources_by_id)
    current_node_ids = set(self.nodes_by_id)
    current_resource_ids = set(self.resources_by_id)

    for resource_id, old_resource in previous_resources_by_id.items():
        resource = self.resources_by_id.get(resource_id)
        if (
            resource is not None
            and old_resource.presence is PresenceState.NOT_CURRENT
            and (
                resource.termination_reason != old_resource.termination_reason
                or resource.successor_resource_id
                != old_resource.successor_resource_id
            )
        ):
            raise ValueError("terminal replacement lineage is immutable")

    missing_source_ids = set(previous_sources_by_id) - current_source_ids
    if missing_source_ids:
        raise ValueError("published snapshot cannot omit a retained inventory source")
    missing_node_ids = set(previous_nodes_by_id) - current_node_ids
    if missing_node_ids:
        raise ValueError("published snapshot cannot omit a retained node")
    missing_resource_ids = set(previous_resources_by_id) - current_resource_ids
    if missing_resource_ids:
        raise ValueError("published snapshot cannot omit a retained resource")

    previous_binding_owners = {
        resource.active_binding_id: resource.resource_id
        for resource in previous.resources
        if resource.active_binding_id is not None
    }
    for resource in self.resources:
        if resource.active_binding_id is None:
            continue
        previous_owner = previous_binding_owners.get(resource.active_binding_id)
        if previous_owner is not None and previous_owner != resource.resource_id:
            raise ValueError(
                "active binding identity cannot move between resources"
            )

    previous_max_generation_by_locator: dict[tuple[str, int], int] = {}
    for old_resource in previous.resources:
        locator = (old_resource.inventory_source_id, old_resource.vmid)
        previous_max_generation_by_locator[locator] = max(
            previous_max_generation_by_locator.get(locator, 0),
            old_resource.locator_generation,
        )
    for resource in self.resources:
        if resource.resource_id in previous_resources_by_id:
            continue
        locator = (resource.inventory_source_id, resource.vmid)
        previous_max_generation = previous_max_generation_by_locator.get(locator)
        if (
            previous_max_generation is not None
            and resource.locator_generation <= previous_max_generation
        ):
            raise ValueError(
                "new locator history must follow all previously retained generations"
            )

    if (
        self.inventory_projection != previous.inventory_projection
        and self.inventory_revision <= previous.inventory_revision
    ):
        raise ValueError(
            "inventory-owned changes require a newer inventory_revision"
        )

    previous_endpoint_owners: dict[str, str] = {}
    for old_source in previous.sources:
        for context in (
            old_source.current_context,
            old_source.committed_context,
        ):
            if context is not None:
                previous_endpoint_owners.setdefault(
                    context.endpoint_id, old_source.inventory_source_id
                )
    for source in self.sources:
        for context in (source.current_context, source.committed_context):
            if context is None:
                continue
            old_owner = previous_endpoint_owners.get(context.endpoint_id)
            if old_owner is not None and old_owner != source.inventory_source_id:
                raise ValueError(
                    "endpoint identity cannot move between inventory sources"
                )

    for source in self.sources:
        old_source = previous_sources_by_id.get(source.inventory_source_id)
        if old_source is None:
            continue
        if (
            old_source.freshness is SourceFreshness.STALE
            and source.freshness is SourceFreshness.FRESH
        ):
            old_commit = old_source.last_committed_run_sequence
            new_commit = source.last_committed_run_sequence
            if new_commit is None or (
                old_commit is not None and new_commit <= old_commit
            ):
                raise ValueError(
                    "a stale source requires a newer successful inventory "
                    "commit before returning to fresh"
                )
        if source.provider_kind != old_source.provider_kind:
            raise ValueError("provider_kind is immutable for an inventory source")
        if (
            source.current_context.source_config_revision
            < old_source.current_context.source_config_revision
        ):
            raise ValueError("source_config_revision must not regress")
        if (
            source.current_context.transport_trust_revision
            < old_source.current_context.transport_trust_revision
        ):
            raise ValueError("transport_trust_revision must not regress")
        if (
            source.current_context.endpoint_id
            != old_source.current_context.endpoint_id
        ):
            raise ValueError(
                "endpoint_id is immutable for an existing inventory source"
            )
        if (
            source.current_context.canonicalization_contract_version
            == old_source.current_context.canonicalization_contract_version
        ):
            if (
                source.current_context.canonical_transport_locator
                != old_source.current_context.canonical_transport_locator
            ):
                raise ValueError(
                    "canonical transport locator is immutable within a "
                    "canonicalization contract version"
                )
        else:
            if (
                source.current_context.source_config_revision
                <= old_source.current_context.source_config_revision
            ):
                raise ValueError(
                    "canonicalization migration requires a newer "
                    "source_config_revision"
                )
            if (
                source.current_context.canonicalization_contract_version
                < old_source.current_context.canonicalization_contract_version
            ):
                raise ValueError(
                    "canonicalization_contract_version must increase during migration"
                )

        old_committed = old_source.committed_context
        committed = source.committed_context
        if old_committed is not None and committed is not None:
            if (
                committed.source_config_revision
                < old_committed.source_config_revision
            ):
                raise ValueError(
                    "committed source_config_revision must not regress"
                )
            if (
                committed.transport_trust_revision
                < old_committed.transport_trust_revision
            ):
                raise ValueError(
                    "committed transport_trust_revision must not regress"
                )
            if (
                committed.canonicalization_contract_version
                < old_committed.canonicalization_contract_version
            ):
                raise ValueError(
                    "committed canonicalization contract must not regress"
                )
            if (
                committed.canonicalization_contract_version
                == old_committed.canonicalization_contract_version
            ):
                if (
                    committed.canonical_transport_locator
                    != old_committed.canonical_transport_locator
                ):
                    raise ValueError(
                        "committed canonical locator is immutable within a "
                        "canonicalization contract version"
                    )
            elif (
                committed.source_config_revision
                <= old_committed.source_config_revision
            ):
                raise ValueError(
                    "committed canonicalization migration requires a newer "
                    "source_config_revision"
                )
        if (
            source.last_issued_run_sequence
            < old_source.last_issued_run_sequence
        ):
            raise ValueError("last_issued_run_sequence must not regress")
        for field_name in (
            "latest_completed_run_sequence",
            "last_health_run_sequence",
            "last_committed_run_sequence",
        ):
            old_sequence = getattr(old_source, field_name)
            sequence = getattr(source, field_name)
            if old_sequence is None:
                continue
            if sequence is None:
                raise ValueError(f"{field_name} cannot be cleared")
            if sequence < old_sequence:
                raise ValueError(f"{field_name} must not regress")

        if (
            source.last_committed_run_sequence is not None
            and (
                old_source.last_committed_run_sequence is None
                or source.last_committed_run_sequence
                > old_source.last_committed_run_sequence
            )
            and old_source.latest_completed_run_sequence is not None
            and source.last_committed_run_sequence
            <= old_source.latest_completed_run_sequence
        ):
            raise ValueError(
                "a new commit must postdate every previously finalized run"
            )

        if (
            source.last_committed_run_sequence is not None
            and (
                old_source.last_committed_run_sequence is None
                or source.last_committed_run_sequence
                > old_source.last_committed_run_sequence
            )
            and self.inventory_revision <= previous.inventory_revision
        ):
            raise ValueError(
                "successful inventory commit requires a newer inventory_revision"
            )

        if (
            source.latest_completed_run_sequence
            == old_source.latest_completed_run_sequence
            and source.latest_completed_run_sequence is not None
            and source.latest_completed_outcome
            != old_source.latest_completed_outcome
        ):
            raise ValueError(
                "latest completed outcome is immutable for its run sequence"
            )
        if (
            source.last_health_run_sequence
            == old_source.last_health_run_sequence
            and source.last_health_run_sequence is not None
            and source.last_run_health_outcome
            != old_source.last_run_health_outcome
        ):
            raise ValueError(
                "last run health outcome is immutable for its run sequence"
            )
        if (
            source.last_committed_run_sequence
            == old_source.last_committed_run_sequence
            and source.last_committed_run_sequence is not None
            and (
                source.last_successful_observed_at,
                source.freshness_reference_at,
                source.freshness_valid_until,
                source.committed_context,
            )
            != (
                old_source.last_successful_observed_at,
                old_source.freshness_reference_at,
                old_source.freshness_valid_until,
                old_source.committed_context,
            )
        ):
            raise ValueError(
                "successful commit provenance is immutable for its run sequence"
            )

    for node in self.nodes:
        old_node = previous_nodes_by_id.get(node.node_id)
        if (
            old_node is not None
            and node.inventory_source_id != old_node.inventory_source_id
        ):
            raise ValueError("node identity cannot move between sources")

    for resource in self.resources:
        old = previous_resources_by_id.get(resource.resource_id)
        if old is None:
            continue
        if resource.inventory_source_id != old.inventory_source_id:
            raise ValueError("resource identity cannot move between sources")
        if resource.resource_type is not old.resource_type:
            raise ValueError("resource_type is immutable for an incarnation")
        if resource.vmid != old.vmid:
            raise ValueError("resource locator is immutable for an incarnation")
        if resource.locator_generation != old.locator_generation:
            raise ValueError(
                "locator_generation is immutable for an incarnation"
            )
        if resource.resource_continuity_revision < old.resource_continuity_revision:
            raise ValueError("resource_continuity_revision must not regress")

        old_terminal = old.presence in {
            PresenceState.CONFIRMED_REMOVED,
            PresenceState.NOT_CURRENT,
        }
        terminal = resource.presence in {
            PresenceState.CONFIRMED_REMOVED,
            PresenceState.NOT_CURRENT,
        }
        if (
            old.security_continuity
            in {SecurityContinuity.TRUSTED, SecurityContinuity.REVOKED}
            and resource.security_continuity is SecurityContinuity.UNVERIFIED
        ):
            raise ValueError("resource cannot erase known security history")
        if old_terminal:
            if (
                not terminal
                or resource.presence is not old.presence
                or resource.termination_reason != old.termination_reason
                or resource.successor_resource_id != old.successor_resource_id
            ):
                raise ValueError("terminal resource cannot be reopened or reclassified")
        elif terminal:
            if resource.active_binding_id is not None:
                raise ValueError("terminal transition must close the active binding")
        elif resource.active_binding_id != old.active_binding_id:
            raise ValueError(
                "active binding is immutable before a terminal transition"
            )

        security_relevant_transition = (
            resource.observational_continuity
            is not old.observational_continuity
            or resource.security_continuity is not old.security_continuity
            or resource.state_level is not old.state_level
            or (
                (old.lifecycle is LifecycleState.QUARANTINED)
                != (resource.lifecycle is LifecycleState.QUARANTINED)
            )
            or (terminal and not old_terminal)
        )
        if (
            security_relevant_transition
            and resource.resource_continuity_revision
            <= old.resource_continuity_revision
        ):
            raise ValueError(
                "security-relevant resource transition requires a newer "
                "resource_continuity_revision"
            )

    for source_id in reconciliation_changed_source_ids:
        old_commit = previous_sources_by_id[
            source_id
        ].last_committed_run_sequence
        new_commit = self.sources_by_id[source_id].last_committed_run_sequence
        if new_commit is None or (
            old_commit is not None and new_commit <= old_commit
        ):
            raise ValueError(
                "discovery/reconciliation-owned source inventory changes "
                "require a newer last_committed_run_sequence"
            )
