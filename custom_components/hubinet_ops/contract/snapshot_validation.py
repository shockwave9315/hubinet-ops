"""One-view legality validation for a complete published snapshot."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .enums import NodeAvailability
from .primitives import _require_positive, _require_text

if TYPE_CHECKING:
    from .models import HubinetOpsSnapshot, ResourceSnapshot


def validate_snapshot(self: "HubinetOpsSnapshot") -> None:
    object.__setattr__(self, "sources", tuple(self.sources))
    object.__setattr__(self, "nodes", tuple(self.nodes))
    object.__setattr__(self, "resources", tuple(self.resources))

    if type(self.inventory_revision) is not int or self.inventory_revision < 0:
        raise ValueError("inventory_revision must be a non-negative integer")
    _require_positive(self.published_state_revision, "published_state_revision")
    _require_text(self.published_at, "published_at")

    source_ids = {source.inventory_source_id for source in self.sources}
    if len(source_ids) != len(self.sources):
        raise ValueError("snapshot contains duplicate source identities")
    endpoint_owners: dict[str, str] = {}
    for source in self.sources:
        contexts = (source.current_context, source.committed_context)
        for context in contexts:
            if context is None:
                continue
            owner = endpoint_owners.setdefault(
                context.endpoint_id, source.inventory_source_id
            )
            if owner != source.inventory_source_id:
                raise ValueError(
                    "endpoint identity cannot be shared across inventory sources"
                )
    node_ids = {node.node_id for node in self.nodes}
    if len(node_ids) != len(self.nodes):
        raise ValueError("snapshot contains duplicate node identities")
    resource_ids = {resource.resource_id for resource in self.resources}
    if len(resource_ids) != len(self.resources):
        raise ValueError("snapshot contains duplicate resource identities")

    if any(node.inventory_source_id not in source_ids for node in self.nodes):
        raise ValueError("node references an unknown inventory source")
    if any(
        resource.inventory_source_id not in source_ids
        for resource in self.resources
    ):
        raise ValueError("resource references an unknown inventory source")

    uncommitted_source_ids = {
        source.inventory_source_id
        for source in self.sources
        if source.last_committed_run_sequence is None
    }
    if any(
        node.inventory_source_id in uncommitted_source_ids
        for node in self.nodes
    ) or any(
        resource.inventory_source_id in uncommitted_source_ids
        for resource in self.resources
    ):
        raise ValueError(
            "a source without a successful inventory commit cannot publish "
            "node or resource inventory"
        )

    nodes_by_id = {node.node_id: node for node in self.nodes}
    active_locators: set[tuple[str, int]] = set()
    locator_generations: set[tuple[str, int, int]] = set()
    resources_by_locator: dict[tuple[str, int], list[ResourceSnapshot]] = {}
    active_bindings: set[str] = set()
    for resource in self.resources:
        locator = (resource.inventory_source_id, resource.vmid)
        resources_by_locator.setdefault(locator, []).append(resource)
        locator_generation = (
            resource.inventory_source_id,
            resource.vmid,
            resource.locator_generation,
        )
        if locator_generation in locator_generations:
            raise ValueError(
                "snapshot contains duplicate retained locator generation"
            )
        locator_generations.add(locator_generation)
        if resource.active_binding_id is not None:
            if locator in active_locators:
                raise ValueError("snapshot contains multiple current occupants for a locator")
            active_locators.add(locator)
            if resource.active_binding_id in active_bindings:
                raise ValueError("snapshot contains duplicate active binding identities")
            active_bindings.add(resource.active_binding_id)
        for node_id in (resource.current_node_id, resource.last_known_node_id):
            if node_id is None:
                continue
            node = nodes_by_id.get(node_id)
            if node is None:
                raise ValueError("resource references a node absent from the same snapshot")
            if node.inventory_source_id != resource.inventory_source_id:
                raise ValueError("resource node relation crosses inventory sources")
        if resource.current_node_id is not None:
            node = nodes_by_id[resource.current_node_id]
            expected = (
                NodeAvailability.AVAILABLE
                if node.available
                else NodeAvailability.UNAVAILABLE
            )
            if resource.node_availability is not expected:
                raise ValueError("resource node availability disagrees with node record")

    for locator_resources in resources_by_locator.values():
        current = next(
            (
                resource
                for resource in locator_resources
                if resource.active_binding_id is not None
            ),
            None,
        )
        terminal_generations = [
            resource.locator_generation
            for resource in locator_resources
            if resource.active_binding_id is None
        ]
        if (
            current is not None
            and terminal_generations
            and current.locator_generation != max(terminal_generations) + 1
        ):
            raise ValueError(
                "current locator generation must follow retained terminal history"
            )
        generations = sorted(
            resource.locator_generation for resource in locator_resources
        )
        for previous_generation, generation in zip(
            generations, generations[1:]
        ):
            if generation != previous_generation + 1:
                raise ValueError(
                    "retained locator generations must be consecutive"
                )

    resources_by_id = {
        resource.resource_id: resource for resource in self.resources
    }
    for old in self.resources:
        if old.successor_resource_id is None:
            continue
        if old.successor_resource_id == old.resource_id:
            raise ValueError("replacement lineage cannot reference itself")
        successor = resources_by_id.get(old.successor_resource_id)
        if successor is None:
            raise ValueError("replacement successor is absent from the same snapshot")
        if (
            successor.inventory_source_id != old.inventory_source_id
            or successor.vmid != old.vmid
            or successor.locator_generation != old.locator_generation + 1
        ):
            raise ValueError(
                "replacement lineage violates locator history invariants"
            )

    sources_by_id = {
        source.inventory_source_id: source for source in self.sources
    }
    for resource in self.resources:
        source = sources_by_id[resource.inventory_source_id]
        if (
            (resource.policy_applicable or resource.effective_capabilities)
            and not source.current_facts_available
        ):
            raise ValueError(
                "policy/capabilities require a fresh healthy source snapshot"
            )
