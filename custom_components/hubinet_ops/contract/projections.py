"""Stable inventory and reconciliation projections."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .models import HubinetOpsSnapshot


def inventory_projection(self) -> tuple[tuple[Any, ...], ...]:
    """Return the explicit inventory-owned portion of this published view."""

    sources = tuple(
        sorted(
            (
                source.inventory_source_id,
                source.name,
                source.provider_kind,
                source.facts,
            )
            for source in self.sources
        )
    )
    nodes = tuple(
        sorted(
            (
                node.node_id,
                node.inventory_source_id,
                node.name,
                node.status,
                node.available,
                node.facts,
            )
            for node in self.nodes
        )
    )
    resources = tuple(
        sorted(
            (
                resource.resource_id,
                resource.inventory_source_id,
                resource.active_binding_id,
                resource.resource_type,
                resource.vmid,
                resource.locator_generation,
                resource.resource_continuity_revision,
                resource.name,
                resource.status,
                resource.current_node_id,
                resource.last_known_node_id,
                resource.presence,
                resource.lifecycle,
                resource.observational_continuity,
                resource.security_continuity,
                resource.detail_status,
                resource.node_availability,
                resource.state_level,
                resource.retained_policy,
                resource.state,
                resource.termination_reason,
                resource.successor_resource_id,
            )
            for resource in self.resources
        )
    )
    return sources, nodes, resources


def source_reconciliation_projection(
    self,
) -> Mapping[str, tuple[Any, ...]]:
    """Return successful discovery/reconciliation-owned state per source.

    This deliberately excludes source configuration, display labels, policy,
    enrollment/security state, revision tokens, and derived capabilities.
    Those fields have independent authoritative owners and must not require a
    fabricated discovery commit.
    """

    projections: dict[str, tuple[Any, ...]] = {}
    for source in self.sources:
        source_id = source.inventory_source_id
        nodes = tuple(
            sorted(
                (
                    node.node_id,
                    node.inventory_source_id,
                    node.name,
                    node.status,
                    node.available,
                    node.facts,
                )
                for node in self.nodes
                if node.inventory_source_id == source_id
            )
        )
        resources = tuple(
            sorted(
                (
                    resource.resource_id,
                    resource.inventory_source_id,
                    resource.active_binding_id,
                    resource.resource_type,
                    resource.vmid,
                    resource.locator_generation,
                    resource.name,
                    resource.status,
                    resource.current_node_id,
                    resource.last_known_node_id,
                    resource.presence,
                    resource.detail_status,
                    resource.node_availability,
                    resource.state,
                    resource.termination_reason,
                    resource.successor_resource_id,
                )
                for resource in self.resources
                if resource.inventory_source_id == source_id
            )
        )
        projections[source_id] = (source.facts, nodes, resources)
    return MappingProxyType(projections)
