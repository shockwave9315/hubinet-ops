"""Versioned, strictly read-only Proxmox provider contract."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Protocol
from enum import StrEnum

from .discovery import BaselineCompleteness, BaselineMode


PROVIDER_CONTRACT_VERSION = 1
SUPPORTED_PVE_MAJOR = 9


class ProviderContractError(ValueError):
    """Provider input cannot satisfy the versioned read-only contract."""


class ProviderFailureKind(StrEnum):
    TRANSPORT = "transport"
    BASELINE_SCOPE = "baseline_scope"
    SECURITY_PROOF = "security_proof"
    SCHEMA = "schema"
    DETAIL = "detail"


def classify_provider_failure(kind: ProviderFailureKind) -> BaselineCompleteness:
    if not isinstance(kind, ProviderFailureKind):
        raise ProviderContractError("provider failure kind is unsupported")
    return {
        ProviderFailureKind.TRANSPORT: BaselineCompleteness.SOURCE_UNAVAILABLE,
        ProviderFailureKind.BASELINE_SCOPE: BaselineCompleteness.PARTIAL,
        ProviderFailureKind.SECURITY_PROOF: BaselineCompleteness.CONFIGURATION_ERROR,
        ProviderFailureKind.SCHEMA: BaselineCompleteness.INVALID,
        ProviderFailureKind.DETAIL: BaselineCompleteness.COMPLETE,
    }[kind]


class ReadOnlyProviderTransport(Protocol):
    """Injected transport surface; mutation verbs deliberately do not exist."""

    def get(self, path: str, *, params: Mapping[str, str] | None = None) -> Any: ...


@dataclass(frozen=True, slots=True)
class EndpointRequirement:
    path: str
    purpose: str
    acl_path: str
    privilege: str | None
    baseline_prerequisite: bool


@dataclass(frozen=True, slots=True)
class BoundaryBaselineResult:
    mode: BaselineMode
    release: tuple[int, int]
    node_rows: tuple[Mapping[str, Any], ...]
    guest_rows: tuple[Mapping[str, Any], ...]
    topology_hash_before: str
    topology_hash_after: str
    permission_hash_before: str
    permission_hash_after: str
    permission_coverage_complete: bool
    completeness: BaselineCompleteness


ENDPOINT_ACL_MATRIX = (
    EndpointRequirement("/version", "provider version", "/", None, True),
    EndpointRequirement("/access/acl", "ACL topology boundary", "/access", "Sys.Audit", True),
    EndpointRequirement("/access/permissions", "upstream effective permission proof", "self/path", None, True),
    EndpointRequirement("/cluster/status", "cluster or standalone mode", "/", "Sys.Audit", True),
    EndpointRequirement("/nodes", "node baseline", "/nodes", "Sys.Audit", True),
    EndpointRequirement("/cluster/resources", "cluster guest locator baseline", "/vms", "VM.Audit", True),
    EndpointRequirement("/nodes/{node}/qemu", "standalone QEMU locator baseline", "/vms", "VM.Audit", True),
    EndpointRequirement("/nodes/{node}/lxc", "standalone LXC locator baseline", "/vms", "VM.Audit", True),
    EndpointRequirement("/nodes/{node}/{type}/{vmid}/config", "optional guest detail", "/vms/{vmid}", "VM.Audit", False),
)


_RELEASE = re.compile(r"^(?P<major>[0-9]+)\.(?P<minor>[0-9]+)$")


def validate_supported_pve_release(release: object) -> tuple[int, int]:
    if not isinstance(release, str):
        raise ProviderContractError("PVE release is missing or malformed")
    match = _RELEASE.fullmatch(release)
    if match is None:
        raise ProviderContractError("PVE release is missing or malformed")
    result = int(match["major"]), int(match["minor"])
    if result[0] != SUPPORTED_PVE_MAJOR:
        raise ProviderContractError("PVE release is outside provider contract v1 support")
    return result


def canonical_evidence_hash(value: Any) -> str:
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    except (TypeError, ValueError) as exc:
        raise ProviderContractError("provider evidence must be canonical JSON-like data") from exc
    return hashlib.sha256(encoded.encode("ascii")).hexdigest()


def evaluate_permission_coverage(
    permissions_by_path: Mapping[str, Mapping[str, object]],
    *,
    node_names: tuple[str, ...] = (),
    security_relevant_descendant_paths: tuple[str, ...] = (),
) -> bool:
    """Compare upstream-evaluated effective permissions; never evaluate ACL inheritance."""

    required = {"/": "Sys.Audit", "/access": "Sys.Audit", "/nodes": "Sys.Audit", "/vms": "VM.Audit"}
    required.update({f"/nodes/{name}": "Sys.Audit" for name in node_names})
    for path in security_relevant_descendant_paths:
        if not isinstance(path, str) or not path.startswith(("/vms/", "/nodes/")):
            return False
        required[path] = "VM.Audit" if path.startswith("/vms/") else "Sys.Audit"
    return all(permissions_by_path.get(path, {}).get(privilege) in {1, True} for path, privilege in required.items())


def classify_boundary(
    *,
    topology_before: Any,
    topology_after: Any,
    permissions_before: Any,
    permissions_after: Any,
    topology_available: bool,
    permissions_available: bool,
    permission_coverage_complete: bool,
    baseline_complete: bool,
) -> BaselineCompleteness:
    if not topology_available or not permissions_available:
        return BaselineCompleteness.CONFIGURATION_ERROR
    if canonical_evidence_hash(topology_before) != canonical_evidence_hash(topology_after):
        return BaselineCompleteness.INVALID
    if canonical_evidence_hash(permissions_before) != canonical_evidence_hash(permissions_after):
        return BaselineCompleteness.INVALID
    if not permission_coverage_complete:
        return BaselineCompleteness.CONFIGURATION_ERROR
    if not baseline_complete:
        return BaselineCompleteness.PARTIAL
    return BaselineCompleteness.COMPLETE


class ProxmoxProviderV1:
    """Dormant orchestrator facade around an injected read-only transport."""

    contract_version = PROVIDER_CONTRACT_VERSION

    @staticmethod
    def validate_version_payload(payload: Mapping[str, Any]) -> tuple[int, int]:
        if not isinstance(payload, Mapping):
            raise ProviderContractError("PVE version payload is malformed")
        return validate_supported_pve_release(payload.get("release"))

    @staticmethod
    def require_get_transport(transport: object) -> ReadOnlyProviderTransport:
        method = getattr(transport, "get", None)
        if not callable(method):
            raise ProviderContractError("provider transport must expose typed GET")
        for forbidden in ("post", "put", "patch", "delete", "request"):
            if callable(getattr(transport, forbidden, None)):
                raise ProviderContractError("provider transport exposes a mutation escape hatch")
        return transport  # type: ignore[return-value]

    @classmethod
    def collect_guest_baseline(
        cls,
        transport: object,
        *,
        mode: BaselineMode,
        node_names: tuple[str, ...],
    ) -> tuple[Mapping[str, Any], ...]:
        """Read the contract-v1 locator baseline through an exact GET allowlist."""

        reader = cls.require_get_transport(transport)
        if mode is BaselineMode.CLUSTER:
            rows = _response_rows(
                reader.get("/cluster/resources", params={"type": "vm"}),
                "/cluster/resources",
            )
            return _validate_guest_rows(rows)
        if mode is not BaselineMode.STANDALONE:
            raise ProviderContractError("baseline mode is unsupported")
        if len(node_names) != 1 or not _NODE_NAME.fullmatch(node_names[0]):
            raise ProviderContractError("standalone fallback requires one valid local node")
        node = node_names[0]
        combined: list[Mapping[str, Any]] = []
        for guest_type in ("qemu", "lxc"):
            path = f"/nodes/{node}/{guest_type}"
            for row in _response_rows(reader.get(path), path):
                combined.append({**row, "type": guest_type, "node": row.get("node", node)})
        return _validate_guest_rows(tuple(combined))

    @classmethod
    def collect_boundary_baseline(
        cls,
        transport: object,
        *,
        mode: BaselineMode,
        standalone_node: str | None = None,
    ) -> BoundaryBaselineResult:
        """Capture a boundary-consistent baseline using one injected credential."""

        reader = cls.require_get_transport(transport)
        release = cls.validate_version_payload(_response_mapping(reader.get("/version"), "/version"))
        topology_before = _response_rows(reader.get("/access/acl"), "/access/acl")
        permissions_before = _response_mapping(reader.get("/access/permissions"), "/access/permissions")
        _response_rows(reader.get("/cluster/status"), "/cluster/status")
        node_rows = _response_rows(reader.get("/nodes"), "/nodes")
        discovered_nodes = tuple(
            str(row["node"])
            for row in node_rows
            if isinstance(row.get("node"), str) and _NODE_NAME.fullmatch(str(row["node"]))
        )
        if len(discovered_nodes) != len(node_rows) or len(set(discovered_nodes)) != len(discovered_nodes):
            raise ProviderContractError("node baseline is malformed or ambiguous")
        if mode is BaselineMode.STANDALONE:
            if standalone_node is None or discovered_nodes != (standalone_node,):
                raise ProviderContractError("standalone node scope is incomplete")
            baseline_nodes = (standalone_node,)
        else:
            baseline_nodes = discovered_nodes
        guest_rows = cls.collect_guest_baseline(
            reader, mode=mode, node_names=baseline_nodes
        )
        topology_after = _response_rows(reader.get("/access/acl"), "/access/acl")
        permissions_after = _response_mapping(reader.get("/access/permissions"), "/access/permissions")
        descendant_paths = _security_descendant_paths(topology_before)
        coverage = evaluate_permission_coverage(
            permissions_before,
            security_relevant_descendant_paths=descendant_paths,
        ) and evaluate_permission_coverage(
            permissions_after,
            security_relevant_descendant_paths=descendant_paths,
        )
        completeness = classify_boundary(
            topology_before=topology_before,
            topology_after=topology_after,
            permissions_before=permissions_before,
            permissions_after=permissions_after,
            topology_available=True,
            permissions_available=True,
            permission_coverage_complete=coverage,
            baseline_complete=True,
        )
        return BoundaryBaselineResult(
            mode=mode,
            release=release,
            node_rows=node_rows,
            guest_rows=guest_rows,
            topology_hash_before=canonical_evidence_hash(topology_before),
            topology_hash_after=canonical_evidence_hash(topology_after),
            permission_hash_before=canonical_evidence_hash(permissions_before),
            permission_hash_after=canonical_evidence_hash(permissions_after),
            permission_coverage_complete=coverage,
            completeness=completeness,
        )


_NODE_NAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")


def _response_rows(payload: object, path: str) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(payload, Mapping) or set(payload) != {"data"}:
        raise ProviderContractError(f"provider response envelope is malformed for {path}")
    data = payload["data"]
    if not isinstance(data, (list, tuple)):
        raise ProviderContractError(f"provider response data is malformed for {path}")
    if not all(isinstance(row, Mapping) for row in data):
        raise ProviderContractError(f"provider response row is malformed for {path}")
    return tuple(data)


def _response_mapping(payload: object, path: str) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping) or set(payload) != {"data"} or not isinstance(payload["data"], Mapping):
        raise ProviderContractError(f"provider response envelope is malformed for {path}")
    return payload["data"]


def _security_descendant_paths(topology: tuple[Mapping[str, Any], ...]) -> tuple[str, ...]:
    paths = {
        str(row["path"])
        for row in topology
        if isinstance(row.get("path"), str)
        and str(row["path"]).startswith(("/vms/", "/nodes/"))
    }
    return tuple(sorted(paths))


def _validate_guest_rows(rows: tuple[Mapping[str, Any], ...]) -> tuple[Mapping[str, Any], ...]:
    result: list[Mapping[str, Any]] = []
    locators: set[int] = set()
    for row in rows:
        vmid = row.get("vmid")
        guest_type = row.get("type")
        node = row.get("node")
        if type(vmid) is not int or vmid <= 0:
            raise ProviderContractError("guest baseline contains an invalid VMID")
        if guest_type not in {"qemu", "lxc"}:
            raise ProviderContractError("guest baseline contains an unsupported type")
        if not isinstance(node, str) or not _NODE_NAME.fullmatch(node):
            raise ProviderContractError("guest baseline contains an invalid node")
        if vmid in locators:
            raise ProviderContractError("guest baseline contains a duplicate source-local VMID")
        locators.add(vmid)
        result.append(dict(row))
    return tuple(result)
