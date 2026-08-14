from __future__ import annotations

import pytest

from app.inventory import (
    ENDPOINT_ACL_MATRIX,
    BaselineCompleteness,
    BaselineMode,
    ProviderContractError,
    ProviderFailureKind,
    ProxmoxProviderV1,
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
        return self.responses[path]


def cluster_status(*, name="cluster-a", node="pve-a"):
    return {
        "data": [
            {
                "type": "cluster",
                "id": "cluster",
                "name": name,
                "nodes": 1,
                "version": 7,
                "quorate": 1,
            },
            {
                "type": "node",
                "id": f"node/{node}",
                "name": node,
                "nodeid": 1,
                "local": 1,
                "online": 1,
            },
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
    permissions = {
        "/": {"Sys.Audit": 1},
        "/access": {"Sys.Audit": 1},
        "/nodes": {"Sys.Audit": 1},
        "/vms": {"VM.Audit": 1},
    }

    class SequenceTransport:
        def __init__(self):
            self.calls = []

        def get(self, path, *, params=None):
            self.calls.append((path, params))
            responses = {
                "/version": {"data": {"release": "9.0"}},
                "/access/acl": {"data": []},
                "/access/permissions": {"data": permissions},
                "/cluster/status": cluster_status(),
                "/nodes": {"data": [{"node": "pve-a"}]},
                "/cluster/resources": {
                    "data": [{"vmid": 100, "type": "qemu", "node": "pve-a"}]
                },
            }
            return responses[path]

    transport = SequenceTransport()
    result = ProxmoxProviderV1.collect_boundary_baseline(transport)
    assert result.mode is BaselineMode.CLUSTER
    assert result.completeness is BaselineCompleteness.COMPLETE
    assert [path for path, _ in transport.calls] == [
        "/version",
        "/access/acl",
        "/access/permissions",
        "/cluster/status",
        "/nodes",
        "/cluster/resources",
        "/access/acl",
        "/access/permissions",
    ]


def test_boundary_window_derives_standalone_and_uses_only_local_qemu_lxc() -> None:
    permissions = {
        "/": {"Sys.Audit": 1},
        "/access": {"Sys.Audit": 1},
        "/nodes": {"Sys.Audit": 1},
        "/vms": {"VM.Audit": 1},
    }
    transport = FakeTransport(
        {
            "/version": {"data": {"release": "9.0"}},
            "/access/acl": {"data": []},
            "/access/permissions": {"data": permissions},
            "/cluster/status": standalone_status(),
            "/nodes": {"data": [{"node": "pve-a"}]},
            "/nodes/pve-a/qemu": {"data": [{"vmid": 100}]},
            "/nodes/pve-a/lxc": {"data": [{"vmid": 101}]},
        }
    )
    result = ProxmoxProviderV1.collect_boundary_baseline(transport)
    paths = [path for path, _ in transport.calls]
    assert result.mode is BaselineMode.STANDALONE
    assert result.completeness is BaselineCompleteness.COMPLETE
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
    permissions = {
        "/": {"Sys.Audit": 1},
        "/access": {"Sys.Audit": 1},
        "/nodes": {"Sys.Audit": 1},
        "/vms": {"VM.Audit": 1},
    }
    transport = FakeTransport(
        {
            "/version": {"data": {"release": "9.0"}},
            "/access/acl": {"data": []},
            "/access/permissions": {"data": permissions},
            "/cluster/status": status_payload,
        }
    )
    with pytest.raises(ProviderContractError, match="disagrees"):
        ProxmoxProviderV1.collect_boundary_baseline(
            transport, expected_mode=expected_mode
        )
    assert forbidden_path not in [path for path, _ in transport.calls]


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


def test_boundary_window_detects_descendant_denial_and_boundary_mismatch() -> None:
    granted = {
        "/": {"Sys.Audit": 1},
        "/access": {"Sys.Audit": 1},
        "/nodes": {"Sys.Audit": 1},
        "/vms": {"VM.Audit": 1},
    }

    class BoundaryTransport:
        def __init__(self, *, change_topology=False):
            self.acl_reads = 0
            self.change_topology = change_topology

        def get(self, path, *, params=None):
            if path == "/version":
                return {"data": {"release": "9.0"}}
            if path == "/access/acl":
                self.acl_reads += 1
                rows = [{"path": "/vms/100", "roleid": "NoAccess"}]
                if self.change_topology and self.acl_reads == 2:
                    rows.append({"path": "/vms/101", "roleid": "NoAccess"})
                return {"data": rows}
            if path == "/access/permissions":
                return {"data": granted}
            if path == "/cluster/status":
                return cluster_status()
            if path == "/nodes":
                return {"data": [{"node": "pve-a"}]}
            if path == "/cluster/resources":
                return {"data": []}
            raise AssertionError(path)

    denied = ProxmoxProviderV1.collect_boundary_baseline(
        BoundaryTransport(), expected_mode=BaselineMode.CLUSTER
    )
    assert denied.completeness is BaselineCompleteness.CONFIGURATION_ERROR
    mismatched = ProxmoxProviderV1.collect_boundary_baseline(
        BoundaryTransport(change_topology=True), expected_mode=BaselineMode.CLUSTER
    )
    assert mismatched.completeness is BaselineCompleteness.INVALID
