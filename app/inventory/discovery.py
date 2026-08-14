"""Immutable normalized discovery values for the dormant Phase 1B backend."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
import hashlib
import json
import re
from types import MappingProxyType
from typing import Any
import uuid


_EXTERNAL_NODE_NAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")


class BaselineCompleteness(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    CONFIGURATION_ERROR = "configuration_error"
    SOURCE_UNAVAILABLE = "source_unavailable"
    INVALID = "invalid"


class DetailReadStatus(StrEnum):
    OK = "ok"
    TEMPORARILY_UNAVAILABLE = "temporarily_unavailable"
    ERROR = "error"


class SourceAvailability(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class BaselineMode(StrEnum):
    CLUSTER = "cluster"
    STANDALONE = "standalone"


class ContinuityEvidenceKind(StrEnum):
    RESOURCE_TYPE_CHANGE = "resource_type_change"
    CONTINUITY_ANCHOR_MISMATCH = "continuity_anchor_mismatch"
    TRUSTED_EVENT_CHAIN = "trusted_event_chain"
    OPERATOR_REPLACEMENT = "operator_replacement"


def _freeze(value: Any) -> Any:
    if value is None or type(value) in {str, int, float, bool}:
        return value
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("discovery mapping keys must be text")
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    raise TypeError(f"discovery values must be JSON-like, got {type(value).__name__}")


def _mapping(value: Mapping[str, Any] | None) -> Mapping[str, Any]:
    result = _freeze(value or {})
    assert isinstance(result, Mapping)
    return result


def _text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty text")
    return value


def _uuid(value: str, name: str) -> str:
    _text(value, name)
    try:
        parsed = uuid.UUID(value)
    except (TypeError, ValueError, AttributeError):
        raise ValueError(f"{name} must be a canonical UUID") from None
    if parsed.int == 0 or str(parsed) != value:
        raise ValueError(f"{name} must be a canonical non-NIL UUID")
    return value


def _positive(value: int, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


@dataclass(frozen=True, slots=True)
class DiscoveredNode:
    external_node_name: str
    status: str
    available: bool
    observed_at: str
    facts: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        if not isinstance(self.external_node_name, str) or not _EXTERNAL_NODE_NAME.fullmatch(
            self.external_node_name
        ):
            raise ValueError("external_node_name must be a canonical node name")
        _text(self.status, "node status")
        if type(self.available) is not bool:
            raise ValueError("node available must be a boolean")
        _text(self.observed_at, "node observed_at")
        object.__setattr__(self, "facts", _mapping(self.facts))


@dataclass(frozen=True, slots=True)
class DiscoveredResource:
    inventory_source_id: str
    vmid: int
    resource_type: str
    name: str
    status: str
    current_node_name: str | None
    observed_at: str
    detail_status: DetailReadStatus
    facts: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    continuity_evidence: tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self) -> None:
        _uuid(self.inventory_source_id, "resource inventory_source_id")
        _positive(self.vmid, "vmid")
        if self.resource_type not in {"qemu", "lxc"}:
            raise ValueError("resource_type must be qemu or lxc")
        _text(self.name, "resource name")
        _text(self.status, "resource status")
        if self.current_node_name is not None:
            _text(self.current_node_name, "current_node_name")
        _text(self.observed_at, "resource observed_at")
        if not isinstance(self.detail_status, DetailReadStatus):
            raise ValueError("detail_status must be canonical")
        object.__setattr__(self, "facts", _mapping(self.facts))
        object.__setattr__(
            self,
            "continuity_evidence",
            tuple(_mapping(item) for item in tuple(self.continuity_evidence)),
        )


@dataclass(frozen=True, slots=True)
class NormalizedDiscoverySnapshot:
    run_id: str
    discovery_run_sequence: int
    inventory_source_id: str
    expected_source_config_revision: int
    endpoint_id: str
    canonical_transport_locator: str
    canonicalization_contract_version: int
    expected_transport_trust_revision: int
    provider_contract_version: int
    observed_at: str
    source_facts: Mapping[str, Any]
    source_availability: SourceAvailability
    baseline_completeness: BaselineCompleteness
    baseline_mode: BaselineMode
    acl_topology_hash_before: str | None
    acl_topology_hash_after: str | None
    permission_snapshot_hash_before: str | None
    permission_snapshot_hash_after: str | None
    permission_coverage_complete: bool
    boundary_consistent: bool
    covered_nodes: tuple[str, ...]
    failed_baseline_scopes: tuple[str, ...]
    detail_summary: Mapping[str, Any]
    failed_detail_scopes: tuple[str, ...]
    nodes: tuple[DiscoveredNode, ...]
    resources: tuple[DiscoveredResource, ...]

    def __post_init__(self) -> None:
        _uuid(self.run_id, "run_id")
        _positive(self.discovery_run_sequence, "discovery_run_sequence")
        _uuid(self.inventory_source_id, "inventory_source_id")
        _positive(self.expected_source_config_revision, "expected_source_config_revision")
        _uuid(self.endpoint_id, "endpoint_id")
        _text(self.canonical_transport_locator, "canonical_transport_locator")
        _positive(self.canonicalization_contract_version, "canonicalization_contract_version")
        _positive(self.expected_transport_trust_revision, "expected_transport_trust_revision")
        _positive(self.provider_contract_version, "provider_contract_version")
        _text(self.observed_at, "observed_at")
        if not isinstance(self.source_availability, SourceAvailability):
            raise ValueError("source_availability must be canonical")
        if not isinstance(self.baseline_completeness, BaselineCompleteness):
            raise ValueError("baseline_completeness must be canonical")
        if not isinstance(self.baseline_mode, BaselineMode):
            raise ValueError("baseline_mode must be canonical")
        if type(self.permission_coverage_complete) is not bool:
            raise ValueError("permission_coverage_complete must be boolean")
        if type(self.boundary_consistent) is not bool:
            raise ValueError("boundary_consistent must be boolean")
        object.__setattr__(self, "source_facts", _mapping(self.source_facts))
        object.__setattr__(self, "detail_summary", _mapping(self.detail_summary))
        object.__setattr__(self, "covered_nodes", tuple(self.covered_nodes))
        object.__setattr__(self, "failed_baseline_scopes", tuple(self.failed_baseline_scopes))
        object.__setattr__(self, "failed_detail_scopes", tuple(self.failed_detail_scopes))
        object.__setattr__(self, "nodes", tuple(self.nodes))
        object.__setattr__(self, "resources", tuple(self.resources))
        self._validate()

    def _validate(self) -> None:
        covered_node_names = list(self.covered_nodes)
        node_names = [node.external_node_name for node in self.nodes]
        if len(set(node_names)) != len(node_names):
            raise ValueError("normalized snapshot contains duplicate external node name")
        if any(
            not isinstance(name, str) or not _EXTERNAL_NODE_NAME.fullmatch(name)
            for name in covered_node_names
        ):
            raise ValueError("covered_nodes must contain canonical node names")
        if len(set(covered_node_names)) != len(covered_node_names):
            raise ValueError("normalized snapshot contains duplicate covered node name")
        slots: set[tuple[str, int]] = set()
        for resource in self.resources:
            if resource.inventory_source_id != self.inventory_source_id:
                raise ValueError("normalized resource references the wrong inventory source")
            slot = (resource.inventory_source_id, resource.vmid)
            if slot in slots:
                raise ValueError("normalized snapshot contains duplicate VMID locator")
            slots.add(slot)
            if resource.current_node_name is not None and resource.current_node_name not in node_names:
                raise ValueError("normalized resource references an unknown node name")
        if self.baseline_completeness is BaselineCompleteness.COMPLETE:
            if not node_names or set(covered_node_names) != set(node_names):
                raise ValueError(
                    "complete baseline requires exact non-empty covered node scope"
                )
            if self.baseline_mode is BaselineMode.STANDALONE and len(node_names) != 1:
                raise ValueError("complete standalone baseline requires exactly one covered node")
            hashes = (
                self.acl_topology_hash_before,
                self.acl_topology_hash_after,
                self.permission_snapshot_hash_before,
                self.permission_snapshot_hash_after,
            )
            if any(not value for value in hashes):
                raise ValueError("complete baseline requires boundary hashes")
            if not self.boundary_consistent or not self.permission_coverage_complete:
                raise ValueError("complete baseline requires consistent complete permission coverage")
            if hashes[0] != hashes[1] or hashes[2] != hashes[3]:
                raise ValueError("complete baseline requires matching boundary hashes")

    @property
    def snapshot_hash(self) -> str:
        return hashlib.sha256(
            json.dumps(self.to_json_value(), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def to_json_value(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "sequence": self.discovery_run_sequence,
            "source_id": self.inventory_source_id,
            "expected_source_config_revision": self.expected_source_config_revision,
            "endpoint_id": self.endpoint_id,
            "canonical_transport_locator": self.canonical_transport_locator,
            "canonicalization_contract_version": self.canonicalization_contract_version,
            "expected_transport_trust_revision": self.expected_transport_trust_revision,
            "provider_contract_version": self.provider_contract_version,
            "observed_at": self.observed_at,
            "completeness": self.baseline_completeness.value,
            "mode": self.baseline_mode.value,
            "source_availability": self.source_availability.value,
            "acl_topology_hash_before": self.acl_topology_hash_before,
            "acl_topology_hash_after": self.acl_topology_hash_after,
            "permission_snapshot_hash_before": self.permission_snapshot_hash_before,
            "permission_snapshot_hash_after": self.permission_snapshot_hash_after,
            "permission_coverage_complete": self.permission_coverage_complete,
            "boundary_consistent": self.boundary_consistent,
            "covered_nodes": list(self.covered_nodes),
            "failed_baseline_scopes": list(self.failed_baseline_scopes),
            "detail_summary": _thaw(self.detail_summary),
            "failed_detail_scopes": list(self.failed_detail_scopes),
            "source_facts": _thaw(self.source_facts),
            "nodes": [
                {"name": node.external_node_name, "status": node.status, "available": node.available, "observed_at": node.observed_at, "facts": _thaw(node.facts)}
                for node in self.nodes
            ],
            "resources": [
                {"vmid": item.vmid, "type": item.resource_type, "name": item.name, "status": item.status, "node": item.current_node_name, "observed_at": item.observed_at, "detail": item.detail_status.value, "facts": _thaw(item.facts), "evidence": _thaw(item.continuity_evidence)}
                for item in self.resources
            ],
        }


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_thaw(item) for item in value]
    return value
