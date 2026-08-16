from __future__ import annotations

import pytest

from app.inventory import (
    ENDPOINT_ACL_MATRIX,
    BaselineCompleteness,
    BaselineMode,
    DiscoveredNode,
    NormalizedDiscoverySnapshot,
    ProviderContractError,
    ProviderFailureKind,
    ProxmoxProviderV1,
    SourceAvailability,
    classify_boundary,
    classify_provider_failure,
    evaluate_permission_coverage,
    validate_supported_pve_release,
)


@pytest.mark.parametrize("release", ("9.0", "9.1", "9.99"))
def test_provider_contract_v1_accepts_only_documented_pve9_major(release: str) -> None:
    assert validate_supported_pve_release(release)[0] == 9


@pytest.mark.parametrize("release", ("8.4", "10.0", "9", "9.x", "", None))
def test_provider_contract_rejects_unsupported_missing_or_malformed_version(release) -> None:
    with pytest.raises(ProviderContractError):
        validate_supported_pve_release(release)


def test_endpoint_acl_matrix_separates_baseline_from_detail_reads() -> None:
    by_path = {entry.path: entry for entry in ENDPOINT_ACL_MATRIX}
    assert by_path["/cluster/resources"].acl_path == "/vms"
    assert by_path["/cluster/resources"].privilege == "VM.Audit"
    assert by_path["/access/acl"].privilege == "Sys.Audit"
    assert by_path["/access/acl"].baseline_prerequisite is True
    assert by_path["/nodes/{node}/{type}/{vmid}/config"].baseline_prerequisite is False
    assert "/nodes/{node}/qemu" in by_path
    assert "/nodes/{node}/lxc" in by_path


def test_provider_transport_surface_rejects_mutation_or_generic_request_methods() -> None:
    class GetOnly:
        def get(self, path, *, params=None):
            return {}

    class Unsafe(GetOnly):
        def post(self, path, payload):
            return None

    assert ProxmoxProviderV1.require_get_transport(GetOnly()) is not None
    with pytest.raises(ProviderContractError, match="mutation escape"):
        ProxmoxProviderV1.require_get_transport(Unsafe())


def test_upstream_permission_results_are_compared_without_acl_reimplementation() -> None:
    permissions = {
        "/": {"Sys.Audit": 1},
        "/access": {"Sys.Audit": 1},
        "/nodes": {"Sys.Audit": 1},
        "/nodes/pve-a": {"Sys.Audit": 1},
        "/vms": {"VM.Audit": 1},
        "/vms/100": {"VM.Audit": 1},
    }
    assert evaluate_permission_coverage(
        permissions,
        node_names=("pve-a",),
        security_relevant_descendant_paths=("/vms/100",),
    )
    denied = {**permissions, "/vms/100": {}}
    assert not evaluate_permission_coverage(
        denied,
        node_names=("pve-a",),
        security_relevant_descendant_paths=("/vms/100",),
    )


def test_boundary_classification_fails_closed_for_missing_proof_and_mismatch() -> None:
    common = dict(
        topology_before=[{"path": "/vms"}],
        topology_after=[{"path": "/vms"}],
        permissions_before={"/vms": {"VM.Audit": 1}},
        permissions_after={"/vms": {"VM.Audit": 1}},
        topology_available=True,
        permissions_available=True,
        permission_coverage_complete=True,
        baseline_complete=True,
    )
    assert classify_boundary(**common) is BaselineCompleteness.COMPLETE
    assert classify_boundary(**{**common, "topology_available": False}) is BaselineCompleteness.CONFIGURATION_ERROR
    assert classify_boundary(**{**common, "permissions_available": False}) is BaselineCompleteness.CONFIGURATION_ERROR
    assert classify_boundary(**{**common, "permission_coverage_complete": False}) is BaselineCompleteness.CONFIGURATION_ERROR
    assert classify_boundary(**{**common, "baseline_complete": False}) is BaselineCompleteness.PARTIAL
    assert classify_boundary(**{**common, "topology_after": [{"path": "/vms/100"}]}) is BaselineCompleteness.INVALID
    assert classify_boundary(**{**common, "permissions_after": {"/vms": {}}}) is BaselineCompleteness.INVALID


def test_provider_failure_classes_preserve_baseline_vs_detail_semantics() -> None:
    assert classify_provider_failure(ProviderFailureKind.TRANSPORT) is BaselineCompleteness.SOURCE_UNAVAILABLE
    assert classify_provider_failure(ProviderFailureKind.BASELINE_SCOPE) is BaselineCompleteness.PARTIAL
    assert classify_provider_failure(ProviderFailureKind.SECURITY_PROOF) is BaselineCompleteness.CONFIGURATION_ERROR
    assert classify_provider_failure(ProviderFailureKind.SCHEMA) is BaselineCompleteness.INVALID
    assert classify_provider_failure(ProviderFailureKind.DETAIL) is BaselineCompleteness.COMPLETE


class FakeTransport:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def get(self, path, *, params=None):
        self.calls.append((path, params))
        response = self.responses[path]
        return response(params) if callable(response) else response


def cluster_status(*, name="cluster-a", nodes=("pve-a",), local_node="pve-a"):
    return {
        "data": [
            {
                "type": "cluster",
                "id": "cluster",
                "name": name,
                "nodes": len(nodes),
                "version": 7,
                "quorate": 1,
            },
            *(
                {
                    "type": "node",
                    "id": f"node/{node}",
                    "name": node,
                    "nodeid": index,
                    "local": int(node == local_node),
                    "online": 1,
                }
                for index, node in enumerate(nodes, start=1)
            ),
        ]
    }


def standalone_status(*, node="pve-a"):
    return {
        "data": [
            {
                "type": "node",
                "id": f"node/{node}",
                "name": node,
                "nodeid": 0,
                "local": 1,
                "online": 1,
            }
        ]
    }


def required_grants(*, nodes=("pve-a",), descendants=()):
    paths = {"/", "/access", "/nodes", "/vms"}
    paths.update(f"/nodes/{node}" for node in nodes)
    paths.update(descendants)
    return {
        path: {
            "VM.Audit" if path == "/vms" or path.startswith("/vms/") else "Sys.Audit": 1
        }
        for path in paths
    }


class BoundaryTransport:
    def __init__(
        self,
        *,
        status_payload=None,
        node_names=("pve-a",),
        topology_before=(),
        topology_after=None,
        permissions_before=None,
        permissions_after=None,
        permission_overrides=None,
        cluster_guests=(),
        qemu_guests=(),
        lxc_guests=(),
    ):
        self.status_payload = status_payload or cluster_status(nodes=node_names)
        self.node_names = node_names
        self.topology_before = tuple(topology_before)
        self.topology_after = tuple(
            topology_before if topology_after is None else topology_after
        )
        self.permissions_before = permissions_before or required_grants(nodes=node_names)
        self.permissions_after = permissions_after or self.permissions_before
        self.permission_overrides = permission_overrides or {}
        self.cluster_guests = tuple(cluster_guests)
        self.qemu_guests = tuple(qemu_guests)
        self.lxc_guests = tuple(lxc_guests)
        self.calls = []
        self.acl_reads = 0
        self.permission_reads = {}

    def get(self, path, *, params=None):
        self.calls.append((path, params))
        if path == "/version":
            return {"data": {"release": "9.0"}}
        if path == "/access/acl":
            rows = self.topology_before if self.acl_reads == 0 else self.topology_after
            self.acl_reads += 1
            return {"data": list(rows)}
        if path == "/cluster/status":
            return self.status_payload
        if path == "/access/permissions":
            if not isinstance(params, dict) or set(params) != {"path"}:
                raise AssertionError("effective permission read must name one exact path")
            permission_path = params["path"]
            boundary = self.permission_reads.get(permission_path, 0)
            self.permission_reads[permission_path] = boundary + 1
            override = self.permission_overrides.get((boundary, permission_path))
            if override is not None:
                return override
            permission_map = (
                self.permissions_before if boundary == 0 else self.permissions_after
            )
            privileges = permission_map.get(permission_path, {})
            return {"data": {permission_path: privileges} if privileges else {}}
        if path == "/nodes":
            return {"data": [{"node": node} for node in self.node_names]}
        if path == "/cluster/resources":
            assert params == {"type": "vm"}
            return {"data": list(self.cluster_guests)}
        if path == f"/nodes/{self.node_names[0]}/qemu":
            return {"data": list(self.qemu_guests)}
        if path == f"/nodes/{self.node_names[0]}/lxc":
            return {"data": list(self.lxc_guests)}
        raise AssertionError(path)


def exact_permission_calls(transport):
    return [
        params["path"]
        for path, params in transport.calls
        if path == "/access/permissions"
    ]


def normalized_from_provider_result(result, *, nodes, resources=()):
    source_id = "00000000-0000-4000-8000-000000000001"
    return NormalizedDiscoverySnapshot.from_provider_baseline(
        result,
        run_id="00000000-0000-4000-8000-000000000002",
        discovery_run_sequence=1,
        inventory_source_id=source_id,
        expected_source_config_revision=1,
        endpoint_id="00000000-0000-4000-8000-000000000003",
        canonical_transport_locator="https://pve.example:8006",
        canonicalization_contract_version=1,
        expected_transport_trust_revision=1,
        provider_contract_version=1,
        observed_at="2026-08-14T12:00:00+00:00",
        source_facts={"release": "9.0"},
        source_availability=SourceAvailability.AVAILABLE,
        failed_baseline_scopes=(),
        detail_summary={
            "ok_count": len(resources),
            "temporarily_unavailable_count": 0,
            "error_count": 0,
        },
        failed_detail_scopes=(),
        nodes=nodes,
        resources=resources,
    )


def test_cluster_baseline_uses_source_wide_cluster_resources_get() -> None:
    transport = FakeTransport(
        {"/cluster/resources": {"data": [{"vmid": 100, "type": "qemu", "node": "pve-a"}]}}
    )
    rows = ProxmoxProviderV1.collect_guest_baseline(
        transport, mode=BaselineMode.CLUSTER, node_names=("pve-a", "pve-b")
    )
    assert rows[0]["vmid"] == 100
    assert transport.calls == [("/cluster/resources", {"type": "vm"})]


def test_standalone_fallback_enumerates_exact_local_qemu_and_lxc_scopes() -> None:
    transport = FakeTransport(
        {
            "/nodes/pve-a/qemu": {"data": [{"vmid": 100}]},
            "/nodes/pve-a/lxc": {"data": [{"vmid": 101}]},
        }
    )
    rows = ProxmoxProviderV1.collect_guest_baseline(
        transport, mode=BaselineMode.STANDALONE, node_names=("pve-a",)
    )
    assert {(row["vmid"], row["type"]) for row in rows} == {(100, "qemu"), (101, "lxc")}
    assert transport.calls == [
        ("/nodes/pve-a/qemu", None),
        ("/nodes/pve-a/lxc", None),
    ]


def test_standalone_duplicate_slot_or_malformed_envelope_fails_closed() -> None:
    duplicate = FakeTransport(
        {
            "/nodes/pve-a/qemu": {"data": [{"vmid": 100}]},
            "/nodes/pve-a/lxc": {"data": [{"vmid": 100}]},
        }
    )
    with pytest.raises(ProviderContractError, match="duplicate"):
        ProxmoxProviderV1.collect_guest_baseline(
            duplicate, mode=BaselineMode.STANDALONE, node_names=("pve-a",)
        )
    malformed = FakeTransport({"/cluster/resources": {"unexpected": []}})
    with pytest.raises(ProviderContractError, match="envelope"):
        ProxmoxProviderV1.collect_guest_baseline(
            malformed, mode=BaselineMode.CLUSTER, node_names=()
        )


def test_boundary_window_orders_security_evidence_around_cluster_baseline() -> None:
    transport = BoundaryTransport(
        cluster_guests=({"vmid": 100, "type": "qemu", "node": "pve-a"},)
    )
    result = ProxmoxProviderV1.collect_boundary_baseline(transport)
    assert result.mode is BaselineMode.CLUSTER
    assert result.completeness is BaselineCompleteness.COMPLETE
    assert transport.calls == [
        ("/version", None),
        ("/access/acl", None),
        ("/cluster/status", None),
        ("/access/permissions", {"path": "/"}),
        ("/access/permissions", {"path": "/access"}),
        ("/access/permissions", {"path": "/nodes"}),
        ("/access/permissions", {"path": "/nodes/pve-a"}),
        ("/access/permissions", {"path": "/vms"}),
        ("/nodes", None),
        ("/cluster/resources", {"type": "vm"}),
        ("/access/acl", None),
        ("/access/permissions", {"path": "/"}),
        ("/access/permissions", {"path": "/access"}),
        ("/access/permissions", {"path": "/nodes"}),
        ("/access/permissions", {"path": "/nodes/pve-a"}),
        ("/access/permissions", {"path": "/vms"}),
    ]
    assert all(
        params is not None
        for path, params in transport.calls
        if path == "/access/permissions"
    )


def test_normalization_rejects_provider_observed_guest_omitted_from_resources() -> None:
    # provider_baseline.guest_rows observed VM 100; the caller supplies no
    # normalized resources for it. Normalization must fail closed instead of
    # silently producing a COMPLETE snapshot that could later be reconciled
    # as VM 100 being missing.
    transport = BoundaryTransport(
        cluster_guests=({"vmid": 100, "type": "qemu", "node": "pve-a"},)
    )
    result = ProxmoxProviderV1.collect_boundary_baseline(transport)
    assert result.guest_rows == ({"vmid": 100, "type": "qemu", "node": "pve-a"},)
    with pytest.raises(ValueError, match="exactly match"):
        normalized_from_provider_result(
            result,
            nodes=(
                DiscoveredNode(
                    "pve-a", "online", True, "2026-08-14T12:00:00+00:00", {}
                ),
            ),
            resources=(),
        )


def test_boundary_checks_each_cluster_node_at_both_boundaries() -> None:
    node_names = ("pve-b", "pve-a")
    transport = BoundaryTransport(
        status_payload=cluster_status(nodes=node_names, local_node="pve-a"),
        node_names=node_names,
        permissions_before=required_grants(nodes=node_names),
        cluster_guests=({"vmid": 100, "type": "qemu", "node": "pve-a"},),
    )
    result = ProxmoxProviderV1.collect_boundary_baseline(transport)
    required = ["/", "/access", "/nodes", "/nodes/pve-a", "/nodes/pve-b", "/vms"]
    assert result.completeness is BaselineCompleteness.COMPLETE
    assert result.node_scope.node_names == ("pve-a", "pve-b")
    assert exact_permission_calls(transport) == required + required


def test_provider_scope_rejects_truncated_complete_normalization() -> None:
    result = ProxmoxProviderV1.collect_boundary_baseline(
        BoundaryTransport(
            status_payload=cluster_status(
                nodes=("pve-a", "pve-b"), local_node="pve-a"
            ),
            node_names=("pve-a", "pve-b"),
            permissions_before=required_grants(nodes=("pve-a", "pve-b")),
        )
    )
    with pytest.raises(ValueError, match="provider-established covered node scope"):
        normalized_from_provider_result(
            result,
            nodes=(
                DiscoveredNode(
                    "pve-a", "online", True, "2026-08-14T12:00:00+00:00", {}
                ),
            ),
        )


def test_provider_scope_order_is_canonical_and_snapshot_hash_is_deterministic() -> None:
    first_result = ProxmoxProviderV1.collect_boundary_baseline(
        BoundaryTransport(
            status_payload=cluster_status(
                nodes=("pve-b", "pve-a"), local_node="pve-a"
            ),
            node_names=("pve-a", "pve-b"),
            permissions_before=required_grants(nodes=("pve-a", "pve-b")),
        )
    )
    second_result = ProxmoxProviderV1.collect_boundary_baseline(
        BoundaryTransport(
            status_payload=cluster_status(
                nodes=("pve-a", "pve-b"), local_node="pve-a"
            ),
            node_names=("pve-b", "pve-a"),
            permissions_before=required_grants(nodes=("pve-a", "pve-b")),
        )
    )
    node_a = DiscoveredNode(
        "pve-a", "online", True, "2026-08-14T12:00:00+00:00", {}
    )
    node_b = DiscoveredNode(
        "pve-b", "online", True, "2026-08-14T12:00:00+00:00", {}
    )
    first = normalized_from_provider_result(
        first_result, nodes=(node_b, node_a)
    )
    second = normalized_from_provider_result(
        second_result, nodes=(node_a, node_b)
    )
    assert first_result.node_scope.node_names == ("pve-a", "pve-b")
    assert second_result.node_scope.node_names == ("pve-a", "pve-b")
    assert first.covered_nodes == second.covered_nodes == ("pve-a", "pve-b")
    assert first.snapshot_hash == second.snapshot_hash


def test_boundary_window_derives_standalone_and_uses_only_local_qemu_lxc() -> None:
    transport = BoundaryTransport(
        status_payload=standalone_status(),
        qemu_guests=({"vmid": 100},),
        lxc_guests=({"vmid": 101},),
    )
    result = ProxmoxProviderV1.collect_boundary_baseline(transport)
    paths = [path for path, _ in transport.calls]
    assert result.mode is BaselineMode.STANDALONE
    assert result.completeness is BaselineCompleteness.COMPLETE
    assert result.node_scope.node_names == ("pve-a",)
    assert paths.count("/access/permissions") == 10
    assert exact_permission_calls(transport).count("/nodes/pve-a") == 2
    assert "/nodes/pve-a/qemu" in paths
    assert "/nodes/pve-a/lxc" in paths
    assert "/cluster/resources" not in paths


@pytest.mark.parametrize(
    "status_payload,expected_mode,forbidden_path",
    (
        (standalone_status(), BaselineMode.CLUSTER, "/cluster/resources"),
        (cluster_status(), BaselineMode.STANDALONE, "/nodes/pve-a/qemu"),
    ),
)
def test_caller_expectation_cannot_override_observed_mode(
    status_payload, expected_mode, forbidden_path
) -> None:
    transport = BoundaryTransport(status_payload=status_payload)
    with pytest.raises(ProviderContractError, match="disagrees"):
        ProxmoxProviderV1.collect_boundary_baseline(
            transport, expected_mode=expected_mode
        )
    paths = [path for path, _ in transport.calls]
    assert forbidden_path not in paths
    assert "/access/permissions" not in paths


@pytest.mark.parametrize(
    "payload",
    (
        {"unexpected": []},
        {"data": []},
        {"data": [{"type": "node", "name": "pve-a"}]},
        {"data": [{"type": "unknown", "id": "unknown", "name": "pve-a"}]},
    ),
)
def test_cluster_status_malformed_or_empty_payload_fails_closed(payload) -> None:
    with pytest.raises(ProviderContractError):
        ProxmoxProviderV1.validate_cluster_status_payload(payload)


def test_cluster_status_ambiguous_cluster_or_local_identity_fails_closed() -> None:
    duplicate_cluster = cluster_status()["data"]
    duplicate_cluster.insert(1, dict(duplicate_cluster[0], name="cluster-b"))
    with pytest.raises(ProviderContractError, match="multiple cluster"):
        ProxmoxProviderV1.validate_cluster_status_payload({"data": duplicate_cluster})

    ambiguous_local = standalone_status()["data"] + [
        {
            "type": "node",
            "id": "node/pve-b",
            "name": "pve-b",
            "nodeid": 0,
            "local": 1,
            "online": 1,
        }
    ]
    with pytest.raises(ProviderContractError, match="ambiguous local"):
        ProxmoxProviderV1.validate_cluster_status_payload({"data": ambiguous_local})

    contradictory = cluster_status()["data"]
    contradictory[1] = dict(contradictory[1], nodeid=0)
    with pytest.raises(ProviderContractError, match="standalone node identity"):
        ProxmoxProviderV1.validate_cluster_status_payload({"data": contradictory})


@pytest.mark.parametrize("exact_node_permissions", ({}, {"VM.Audit": 1}))
def test_parent_node_grant_never_substitutes_exact_node_permission(
    exact_node_permissions,
) -> None:
    permissions = required_grants()
    permissions["/nodes/pve-a"] = exact_node_permissions
    transport = BoundaryTransport(permissions_before=permissions)
    result = ProxmoxProviderV1.collect_boundary_baseline(transport)
    assert result.permission_coverage_complete is False
    assert result.completeness is BaselineCompleteness.CONFIGURATION_ERROR
    assert exact_permission_calls(transport).count("/nodes/pve-a") == 2


@pytest.mark.parametrize(
    "path,privilege",
    (("/", "Sys.Audit"), ("/access", "Sys.Audit"), ("/nodes/pve-a", "Sys.Audit")),
)
def test_non_propagating_exact_permission_is_an_effective_grant(
    path: str, privilege: str
) -> None:
    permissions = required_grants()
    permissions[path] = {privilege: 0}
    result = ProxmoxProviderV1.collect_boundary_baseline(
        BoundaryTransport(permissions_before=permissions)
    )
    assert result.permission_coverage_complete is True
    assert result.completeness is BaselineCompleteness.COMPLETE
    assert result.permission_hash_before == result.permission_hash_after


@pytest.mark.parametrize(
    "tree_root,privilege",
    (("/vms", "VM.Audit"), ("/nodes", "Sys.Audit")),
)
def test_source_wide_tree_root_requires_propagation(
    tree_root: str, privilege: str
) -> None:
    permissions = required_grants()
    permissions[tree_root] = {privilege: 0}
    result = ProxmoxProviderV1.collect_boundary_baseline(
        BoundaryTransport(permissions_before=permissions)
    )
    assert result.permission_coverage_complete is False
    assert result.completeness is BaselineCompleteness.CONFIGURATION_ERROR


@pytest.mark.parametrize(
    "path,privilege",
    (("/vms/100", "VM.Audit"), ("/nodes/pve-a/qemu", "Sys.Audit")),
)
def test_non_propagating_descendant_permission_remains_an_exact_grant(
    path: str, privilege: str
) -> None:
    topology = ({"path": path, "roleid": "PVEAuditor"},)
    permissions = required_grants(descendants=(path,))
    permissions[path] = {privilege: 0}
    result = ProxmoxProviderV1.collect_boundary_baseline(
        BoundaryTransport(
            topology_before=topology,
            permissions_before=permissions,
        )
    )
    assert result.permission_coverage_complete is True
    assert result.completeness is BaselineCompleteness.COMPLETE


def test_lost_source_wide_propagation_across_boundary_cannot_be_complete() -> None:
    before = required_grants()
    after = required_grants()
    after["/vms"] = {"VM.Audit": 0}
    result = ProxmoxProviderV1.collect_boundary_baseline(
        BoundaryTransport(permissions_before=before, permissions_after=after)
    )
    assert result.permission_hash_before != result.permission_hash_after
    assert result.permission_coverage_complete is False
    assert result.completeness is BaselineCompleteness.INVALID


def test_topology_descendant_denial_is_checked_upstream_without_inheritance() -> None:
    topology = ({"path": "/vms/100", "roleid": "NoAccess"},)
    permissions = required_grants(descendants=("/vms/100",))
    permissions["/vms/100"] = {}
    transport = BoundaryTransport(
        topology_before=topology,
        permissions_before=permissions,
    )
    result = ProxmoxProviderV1.collect_boundary_baseline(transport)
    assert result.permission_coverage_complete is False
    assert result.completeness is BaselineCompleteness.CONFIGURATION_ERROR
    assert exact_permission_calls(transport).count("/vms/100") == 2


def test_exact_permission_change_across_boundary_is_invalid() -> None:
    before = required_grants()
    before["/nodes/pve-a"] = {"Sys.Audit": 0}
    after = required_grants()
    assert evaluate_permission_coverage(before, node_names=("pve-a",))
    assert evaluate_permission_coverage(after, node_names=("pve-a",))
    result = ProxmoxProviderV1.collect_boundary_baseline(
        BoundaryTransport(permissions_before=before, permissions_after=after)
    )
    assert result.permission_hash_before != result.permission_hash_after
    assert result.completeness is BaselineCompleteness.INVALID


@pytest.mark.parametrize(
    "malformed",
    (
        {"unexpected": {}},
        {"data": {"/wrong": {"Sys.Audit": 1}}},
        {"data": {"/nodes/pve-a": ["Sys.Audit"]}},
        {"data": {"/nodes/pve-a": {"Sys.Audit": 2}}},
    ),
)
def test_malformed_exact_permission_response_fails_closed(malformed) -> None:
    transport = BoundaryTransport(
        permission_overrides={(0, "/nodes/pve-a"): malformed}
    )
    with pytest.raises(ProviderContractError):
        ProxmoxProviderV1.collect_boundary_baseline(transport)


def test_required_path_order_and_permission_hash_are_deterministic() -> None:
    topology_a = (
        {"path": "/vms/100", "roleid": "PVEAuditor"},
        {"path": "/nodes/pve-b", "roleid": "PVEAuditor"},
    )
    topology_b = tuple(reversed(topology_a))
    nodes_a = ("pve-a", "pve-b")
    nodes_b = tuple(reversed(nodes_a))
    permissions = required_grants(
        nodes=nodes_a,
        descendants=("/vms/100", "/nodes/pve-b"),
    )
    first = BoundaryTransport(
        status_payload=cluster_status(nodes=nodes_a, local_node="pve-a"),
        node_names=nodes_a,
        topology_before=topology_a,
        permissions_before=permissions,
    )
    second = BoundaryTransport(
        status_payload=cluster_status(nodes=nodes_b, local_node="pve-a"),
        node_names=nodes_b,
        topology_before=topology_b,
        permissions_before=permissions,
    )
    first_result = ProxmoxProviderV1.collect_boundary_baseline(first)
    second_result = ProxmoxProviderV1.collect_boundary_baseline(second)
    assert first_result.completeness is BaselineCompleteness.COMPLETE
    assert second_result.completeness is BaselineCompleteness.COMPLETE
    assert exact_permission_calls(first) == exact_permission_calls(second)
    assert first_result.permission_hash_before == second_result.permission_hash_before


def test_acl_topology_mismatch_remains_invalid_with_same_exact_path_set() -> None:
    before_topology = ({"path": "/vms/100", "roleid": "PVEAuditor"},)
    after_topology = before_topology + (
        {"path": "/vms/101", "roleid": "NoAccess"},
    )
    permissions = required_grants(descendants=("/vms/100",))
    transport = BoundaryTransport(
        topology_before=before_topology,
        topology_after=after_topology,
        permissions_before=permissions,
    )
    result = ProxmoxProviderV1.collect_boundary_baseline(
        transport,
        expected_mode=BaselineMode.CLUSTER,
    )
    assert result.completeness is BaselineCompleteness.INVALID
    assert "/vms/101" not in exact_permission_calls(transport)
