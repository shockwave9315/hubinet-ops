"""Versioned, strictly read-only Proxmox provider contract."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import hashlib
import json
import re
from typing import Any, Protocol
from enum import StrEnum

from .discovery import BaselineCompleteness, BaselineMode, ProviderNodeScope


PROVIDER_CONTRACT_VERSION = 1
SUPPORTED_PVE_MAJOR = 9
_PROVIDER_RESULT_TOKEN = object()


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
class PermissionRequirement:
    privilege: str
    require_propagation: bool = False


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
    _token: object = field(repr=False, compare=False)
    node_scope: ProviderNodeScope = field(init=False)

    def __post_init__(self) -> None:
        if self._token is not _PROVIDER_RESULT_TOKEN:
            raise TypeError(
                "boundary baseline result must originate from provider collection"
            )
        node_names = tuple(
            str(row["node"])
            for row in self.node_rows
            if isinstance(row, Mapping)
            and isinstance(row.get("node"), str)
            and _NODE_NAME.fullmatch(str(row["node"]))
        )
        if len(node_names) != len(self.node_rows) or len(set(node_names)) != len(
            node_names
        ):
            raise ProviderContractError("provider result node scope is malformed")
        try:
            scope = ProviderNodeScope._from_provider(self.mode, node_names)
        except (TypeError, ValueError) as exc:
            raise ProviderContractError("provider result node scope is malformed") from exc
        object.__setattr__(self, "node_scope", scope)


@dataclass(frozen=True, slots=True)
class ClusterStatusResult:
    mode: BaselineMode
    local_node: str
    node_names: tuple[str, ...]


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

    required = {
        "/": PermissionRequirement("Sys.Audit"),
        "/access": PermissionRequirement("Sys.Audit"),
        "/nodes": PermissionRequirement("Sys.Audit", require_propagation=True),
        "/vms": PermissionRequirement("VM.Audit", require_propagation=True),
    }
    required.update(
        {
            f"/nodes/{name}": PermissionRequirement("Sys.Audit")
            for name in node_names
        }
    )
    for path in security_relevant_descendant_paths:
        if not isinstance(path, str) or not path.startswith(("/vms/", "/nodes/")):
            return False
        required[path] = PermissionRequirement(
            "VM.Audit" if path.startswith("/vms/") else "Sys.Audit"
        )
    for path, requirement in required.items():
        permissions = permissions_by_path.get(path, {})
        if requirement.privilege not in permissions:
            return False
        if requirement.require_propagation:
            propagation = permissions[requirement.privilege]
            if not (
                (type(propagation) is bool and propagation)
                or (type(propagation) is int and propagation == 1)
            ):
                return False
    return True


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

    @staticmethod
    def validate_cluster_status_payload(payload: object) -> ClusterStatusResult:
        """Derive provider-v1 mode from the exact PVE 9 cluster-status shape."""

        rows = _response_rows(payload, "/cluster/status")
        if not rows:
            raise ProviderContractError("cluster status cannot classify an empty source")
        cluster_rows: list[Mapping[str, Any]] = []
        node_rows: list[Mapping[str, Any]] = []
        for row in rows:
            row_type = row.get("type")
            if row_type == "cluster":
                _validate_cluster_status_cluster_row(row)
                cluster_rows.append(row)
            elif row_type == "node":
                _validate_cluster_status_node_row(row)
                node_rows.append(row)
            else:
                raise ProviderContractError("cluster status contains an unsupported row type")

        local_nodes = tuple(
            str(row["name"])
            for row in node_rows
            if _pve_boolean(row["local"], "cluster status local")
        )
        if len(local_nodes) != 1:
            raise ProviderContractError("cluster status has ambiguous local node identity")
        node_names = tuple(str(row["name"]) for row in node_rows)
        if len(set(node_names)) != len(node_names):
            raise ProviderContractError("cluster status contains duplicate node identity")

        if cluster_rows:
            if len(cluster_rows) != 1:
                raise ProviderContractError("cluster status has multiple cluster identities")
            declared_nodes = cluster_rows[0]["nodes"]
            if declared_nodes != len(node_rows) or not node_rows:
                raise ProviderContractError("cluster status member scope is inconsistent")
            if any(row["nodeid"] <= 0 for row in node_rows):
                raise ProviderContractError("cluster status contains a standalone node identity")
            return ClusterStatusResult(BaselineMode.CLUSTER, local_nodes[0], node_names)

        if len(node_rows) != 1:
            raise ProviderContractError("cluster status cannot unambiguously classify standalone mode")
        node = node_rows[0]
        if node["nodeid"] != 0 or not _pve_boolean(node["online"], "cluster status online"):
            raise ProviderContractError("standalone cluster status row is not canonical")
        return ClusterStatusResult(BaselineMode.STANDALONE, local_nodes[0], node_names)

    @classmethod
    def collect_effective_permissions(
        cls,
        transport: object,
        required_paths: tuple[str, ...],
    ) -> Mapping[str, Mapping[str, bool]]:
        """Ask Proxmox to evaluate effective permissions for every exact path."""

        reader = cls.require_get_transport(transport)
        if required_paths != tuple(sorted(set(required_paths))):
            raise ProviderContractError("required permission paths are not canonical")
        evidence: dict[str, Mapping[str, bool]] = {}
        for path in required_paths:
            if not _is_canonical_acl_path(path):
                raise ProviderContractError("required permission path is malformed")
            payload = _response_mapping(
                reader.get("/access/permissions", params={"path": path}),
                "/access/permissions",
            )
            evidence[path] = _validate_exact_permission_response(payload, path)
        return evidence

    @classmethod
    def collect_boundary_baseline(
        cls,
        transport: object,
        *,
        expected_mode: BaselineMode | None = None,
    ) -> BoundaryBaselineResult:
        """Capture a boundary-consistent baseline using one injected credential."""

        reader = cls.require_get_transport(transport)
        release = cls.validate_version_payload(_response_mapping(reader.get("/version"), "/version"))
        topology_before = _response_rows(reader.get("/access/acl"), "/access/acl")
        status = cls.validate_cluster_status_payload(reader.get("/cluster/status"))
        if expected_mode is not None:
            if not isinstance(expected_mode, BaselineMode):
                raise ProviderContractError("expected baseline mode is unsupported")
            if expected_mode is not status.mode:
                raise ProviderContractError("expected baseline mode disagrees with cluster status")
        descendant_paths = _security_descendant_paths(topology_before)
        required_permission_paths = _required_permission_paths(
            node_names=status.node_names,
            security_relevant_descendant_paths=descendant_paths,
        )
        permissions_before = cls.collect_effective_permissions(
            reader,
            required_permission_paths,
        )
        node_rows = _response_rows(reader.get("/nodes"), "/nodes")
        discovered_nodes = tuple(
            str(row["node"])
            for row in node_rows
            if isinstance(row.get("node"), str) and _NODE_NAME.fullmatch(str(row["node"]))
        )
        if len(discovered_nodes) != len(node_rows) or len(set(discovered_nodes)) != len(discovered_nodes):
            raise ProviderContractError("node baseline is malformed or ambiguous")
        if set(discovered_nodes) != set(status.node_names):
            raise ProviderContractError("node baseline disagrees with cluster status scope")
        if status.mode is BaselineMode.STANDALONE:
            if discovered_nodes != (status.local_node,):
                raise ProviderContractError("standalone node scope is incomplete")
            baseline_nodes = (status.local_node,)
        else:
            baseline_nodes = discovered_nodes
        guest_rows = cls.collect_guest_baseline(
            reader, mode=status.mode, node_names=baseline_nodes
        )
        topology_after = _response_rows(reader.get("/access/acl"), "/access/acl")
        permissions_after = cls.collect_effective_permissions(
            reader,
            required_permission_paths,
        )
        coverage = evaluate_permission_coverage(
            permissions_before,
            node_names=status.node_names,
            security_relevant_descendant_paths=descendant_paths,
        ) and evaluate_permission_coverage(
            permissions_after,
            node_names=status.node_names,
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
            mode=status.mode,
            release=release,
            node_rows=node_rows,
            guest_rows=guest_rows,
            topology_hash_before=canonical_evidence_hash(topology_before),
            topology_hash_after=canonical_evidence_hash(topology_after),
            permission_hash_before=canonical_evidence_hash(permissions_before),
            permission_hash_after=canonical_evidence_hash(permissions_after),
            permission_coverage_complete=coverage,
            completeness=completeness,
            _token=_PROVIDER_RESULT_TOKEN,
        )


_NODE_NAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")


def _validate_cluster_status_cluster_row(row: Mapping[str, Any]) -> None:
    required = {"type", "id", "name", "nodes", "version", "quorate"}
    allowed = required
    if set(row) != allowed:
        raise ProviderContractError("cluster status cluster row is malformed")
    if row["id"] != "cluster" or not isinstance(row["name"], str) or not row["name"].strip():
        raise ProviderContractError("cluster status cluster identity is malformed")
    if type(row["nodes"]) is not int or row["nodes"] <= 0:
        raise ProviderContractError("cluster status node count is malformed")
    if type(row["version"]) is not int or row["version"] < 0:
        raise ProviderContractError("cluster status version is malformed")
    _pve_boolean(row["quorate"], "cluster status quorate")


def _validate_cluster_status_node_row(row: Mapping[str, Any]) -> None:
    required = {"type", "id", "name", "nodeid", "local", "online"}
    allowed = required | {"ip", "level"}
    if not required.issubset(row) or not set(row).issubset(allowed):
        raise ProviderContractError("cluster status node row is malformed")
    name = row["name"]
    if not isinstance(name, str) or not _NODE_NAME.fullmatch(name):
        raise ProviderContractError("cluster status node name is malformed")
    if row["id"] != f"node/{name}":
        raise ProviderContractError("cluster status node identity is malformed")
    if type(row["nodeid"]) is not int or row["nodeid"] < 0:
        raise ProviderContractError("cluster status node id is malformed")
    _pve_boolean(row["local"], "cluster status local")
    _pve_boolean(row["online"], "cluster status online")
    if "ip" in row and (not isinstance(row["ip"], str) or not row["ip"].strip()):
        raise ProviderContractError("cluster status node IP is malformed")
    if "level" in row and not isinstance(row["level"], str):
        raise ProviderContractError("cluster status node level is malformed")


def _pve_boolean(value: object, field: str) -> bool:
    if type(value) is bool:
        return value
    if type(value) is int and value in {0, 1}:
        return bool(value)
    raise ProviderContractError(f"{field} is malformed")


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
    paths: set[str] = set()
    for row in topology:
        path = row.get("path")
        if not isinstance(path, str) or not _is_canonical_acl_path(path):
            raise ProviderContractError("ACL topology contains a malformed path")
        if path.startswith(("/vms/", "/nodes/")):
            paths.add(path)
    return tuple(sorted(paths))


def _required_permission_paths(
    *,
    node_names: tuple[str, ...],
    security_relevant_descendant_paths: tuple[str, ...],
) -> tuple[str, ...]:
    paths = {"/", "/access", "/nodes", "/vms"}
    for node_name in node_names:
        if not isinstance(node_name, str) or not _NODE_NAME.fullmatch(node_name):
            raise ProviderContractError("permission node scope is malformed")
        paths.add(f"/nodes/{node_name}")
    for path in security_relevant_descendant_paths:
        if not _is_canonical_acl_path(path) or not path.startswith(("/vms/", "/nodes/")):
            raise ProviderContractError("security-relevant ACL path is malformed")
        paths.add(path)
    return tuple(sorted(paths))


def _validate_exact_permission_response(
    payload: Mapping[str, Any],
    requested_path: str,
) -> Mapping[str, bool]:
    if not payload:
        return {}
    if set(payload) != {requested_path} or not isinstance(payload[requested_path], Mapping):
        raise ProviderContractError("effective permission response does not match requested path")
    privileges: dict[str, bool] = {}
    for privilege, granted in payload[requested_path].items():
        if not isinstance(privilege, str) or not privilege:
            raise ProviderContractError("effective permission privilege is malformed")
        privileges[privilege] = _pve_boolean(granted, "effective permission value")
    return privileges


def _is_canonical_acl_path(path: object) -> bool:
    if not isinstance(path, str) or not path.startswith("/"):
        return False
    if path == "/":
        return True
    parts = path[1:].split("/")
    return all(part not in {"", ".", ".."} for part in parts)


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
