"""WAVE R0-B Family 2 -- production GET-only Proxmox VE HTTP transport.

Covers §28 tests #9-#19 of
docs/architecture/0.5-r0-read-only-runtime-activation.md. No real network
access anywhere in this file: every request is intercepted in-process by
``httpx.MockTransport``, exactly like every other ``app.inventory`` test
uses a fake/mock transport boundary instead of a live endpoint.
"""

from __future__ import annotations

import httpx
import pytest

from app.inventory import (
    BaselineCompleteness,
    BaselineMode,
    ProviderContractError,
    ProxmoxProviderV1,
)
from app.inventory.provider import ProviderFailureKind
from app.inventory_pve_transport import (
    DEFAULT_CONNECT_TIMEOUT_SECONDS,
    DEFAULT_POOL_TIMEOUT_SECONDS,
    DEFAULT_READ_TIMEOUT_SECONDS,
    DEFAULT_WRITE_TIMEOUT_SECONDS,
    ProxmoxHttpTransport,
    PveTransportError,
    _PVE_API_PREFIX,
)

LOCATOR = "https://pve.example.internal:8006"
TOKEN = "root@pam!hubinet-ops=00000000-0000-0000-0000-000000000000"


def _transport(handler, **kwargs) -> ProxmoxHttpTransport:
    return ProxmoxHttpTransport(
        canonical_transport_locator=LOCATOR,
        pve_api_token=TOKEN,
        _transport=httpx.MockTransport(handler),
        **kwargs,
    )


def _json_response(payload, *, status_code: int = 200, headers=None) -> httpx.Response:
    return httpx.Response(status_code, json=payload, headers=headers)


# ---------------------------------------------------------------------------
# §28 test #9 -- HTTPS-only PVE transport
# ---------------------------------------------------------------------------


def test_9_rejects_non_https_locator() -> None:
    with pytest.raises(PveTransportError, match="HTTPS") as exc:
        ProxmoxHttpTransport(
            canonical_transport_locator="http://pve.example.internal:8006",
            pve_api_token=TOKEN,
        )
    assert exc.value.kind is ProviderFailureKind.SCHEMA


def test_9_accepts_https_locator() -> None:
    transport = _transport(lambda request: _json_response({"data": {"release": "9.0"}}))
    assert transport.get("/version") == {"data": {"release": "9.0"}}


# ---------------------------------------------------------------------------
# §28 test #10 -- no verify=False production transport
# ---------------------------------------------------------------------------


def test_10_rejects_verify_false_at_construction() -> None:
    with pytest.raises(PveTransportError, match="certificate verification") as exc:
        ProxmoxHttpTransport(
            canonical_transport_locator=LOCATOR, pve_api_token=TOKEN, verify=False
        )
    assert exc.value.kind is ProviderFailureKind.SCHEMA


def test_10_accepts_verify_true() -> None:
    ProxmoxHttpTransport(canonical_transport_locator=LOCATOR, pve_api_token=TOKEN, verify=True).close()


def test_10_ca_bundle_path_string_is_not_rejected_by_the_construction_guard() -> None:
    # Unit-level check of the fail-closed guard itself, without exercising
    # httpx's own SSL context/CA-file loading (which requires a real,
    # readable CA bundle on disk and is out of scope for this construction
    # assertion): only `verify=False` may ever be rejected here.
    from app.inventory_pve_transport import _require_verification_enabled

    assert _require_verification_enabled("/etc/ssl/ca.pem") == "/etc/ssl/ca.pem"
    assert _require_verification_enabled(True) is True


# ---------------------------------------------------------------------------
# §28 test #11 -- exact token Authorization header shape
# ---------------------------------------------------------------------------


def test_11_authorization_header_has_exact_pve_token_shape() -> None:
    captured: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers.get("authorization")
        return _json_response({"data": {"release": "9.0"}})

    _transport(handler).get("/version")
    assert captured["authorization"] == f"PVEAPIToken={TOKEN}"


def test_11_pve_secret_never_appears_outside_the_authorization_header() -> None:
    captured: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return _json_response({"data": {"release": "9.0"}})

    _transport(handler).get("/version")
    request = captured["request"]
    assert TOKEN not in str(request.url)
    for name, value in request.headers.items():
        if name.lower() != "authorization":
            assert TOKEN not in value


# ---------------------------------------------------------------------------
# §28 test #12 -- GET allowlist only
# ---------------------------------------------------------------------------


def test_12_adapter_exposes_no_mutation_or_generic_request_method() -> None:
    transport = _transport(lambda request: _json_response({"data": {}}))
    try:
        for forbidden in ("post", "put", "patch", "delete", "request"):
            assert not callable(getattr(transport, forbidden, None))
        assert ProxmoxProviderV1.require_get_transport(transport) is transport
    finally:
        transport.close()


# ---------------------------------------------------------------------------
# §28 test #13 -- redirects fail closed
# ---------------------------------------------------------------------------


def test_13_redirect_response_fails_closed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://attacker.example/"})

    transport = _transport(handler)
    with pytest.raises(PveTransportError, match="redirect") as exc:
        transport.get("/version")
    assert exc.value.kind is ProviderFailureKind.TRANSPORT


def test_13_client_is_constructed_with_redirects_and_proxy_env_disabled() -> None:
    transport = _transport(lambda request: _json_response({"data": {}}))
    try:
        assert transport._client.follow_redirects is False
        assert transport._client.trust_env is False
    finally:
        transport.close()


# ---------------------------------------------------------------------------
# §28 test #14 -- timeout classification
# ---------------------------------------------------------------------------


def test_14_connect_timeout_is_classified_as_transport_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("boom", request=request)

    with pytest.raises(PveTransportError) as exc:
        _transport(handler).get("/version")
    assert exc.value.kind is ProviderFailureKind.TRANSPORT


def test_14_generic_transport_error_is_classified_as_transport_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(PveTransportError) as exc:
        _transport(handler).get("/version")
    assert exc.value.kind is ProviderFailureKind.TRANSPORT


def test_14_timeouts_are_explicit_finite_and_distinct_per_phase() -> None:
    transport = _transport(
        lambda request: _json_response({"data": {}}),
        connect_timeout_seconds=1.0,
        read_timeout_seconds=2.0,
        write_timeout_seconds=3.0,
        pool_timeout_seconds=4.0,
    )
    try:
        timeout = transport._client.timeout
        assert (timeout.connect, timeout.read, timeout.write, timeout.pool) == (
            1.0,
            2.0,
            3.0,
            4.0,
        )
    finally:
        transport.close()


def test_14_default_timeouts_are_all_finite() -> None:
    transport = _transport(lambda request: _json_response({"data": {}}))
    try:
        timeout = transport._client.timeout
        for value in (timeout.connect, timeout.read, timeout.write, timeout.pool):
            assert value is not None and value == pytest.approx(value) and value > 0
        assert timeout.connect == DEFAULT_CONNECT_TIMEOUT_SECONDS
        assert timeout.read == DEFAULT_READ_TIMEOUT_SECONDS
        assert timeout.write == DEFAULT_WRITE_TIMEOUT_SECONDS
        assert timeout.pool == DEFAULT_POOL_TIMEOUT_SECONDS
    finally:
        transport.close()


# ---------------------------------------------------------------------------
# §28 test #15 -- invalid JSON/schema classification (adapter-level)
# ---------------------------------------------------------------------------


def test_15_invalid_json_body_is_classified_as_schema_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json")

    with pytest.raises(PveTransportError) as exc:
        _transport(handler).get("/version")
    assert exc.value.kind is ProviderFailureKind.SCHEMA


def test_15_non_200_status_is_classified_as_transport_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"errors": "boom"})

    with pytest.raises(PveTransportError) as exc:
        _transport(handler).get("/version")
    assert exc.value.kind is ProviderFailureKind.TRANSPORT


def test_15_response_exceeding_max_size_fails_closed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response({"data": {"padding": "x" * 1000}})

    with pytest.raises(PveTransportError, match="maximum allowed response size"):
        _transport(handler, max_response_bytes=10).get("/version")


# ---------------------------------------------------------------------------
# End-to-end fixture shared by #16-#19
# ---------------------------------------------------------------------------


def _required_permissions(node_names: tuple[str, ...]) -> dict[str, dict[str, int]]:
    permissions = {
        "/": {"Sys.Audit": 1},
        "/access": {"Sys.Audit": 1},
        "/nodes": {"Sys.Audit": 1},
        "/vms": {"VM.Audit": 1},
    }
    permissions.update({f"/nodes/{node}": {"Sys.Audit": 1} for node in node_names})
    return permissions


def _make_pve_handler(
    *,
    release: str = "9.0",
    mode: str = "cluster",
    node_names: tuple[str, ...] = ("pve-a",),
    local_node: str = "pve-a",
    guests: tuple[dict, ...] = (),
    permission_overrides: dict[str, dict] | None = None,
):
    permissions = _required_permissions(node_names)
    overrides = permission_overrides or {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        assert path.startswith(_PVE_API_PREFIX)
        rel = path[len(_PVE_API_PREFIX):]
        if rel == "/version":
            return _json_response({"data": {"release": release}})
        if rel == "/access/acl":
            return _json_response({"data": []})
        if rel == "/cluster/status":
            if mode == "cluster":
                data = [
                    {
                        "type": "cluster",
                        "id": "cluster",
                        "name": "cluster-a",
                        "nodes": len(node_names),
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
                        for index, node in enumerate(node_names, start=1)
                    ),
                ]
            else:
                node = node_names[0]
                data = [
                    {
                        "type": "node",
                        "id": f"node/{node}",
                        "name": node,
                        "nodeid": 0,
                        "local": 1,
                        "online": 1,
                    }
                ]
            return _json_response({"data": data})
        if rel == "/access/permissions":
            permission_path = dict(request.url.params).get("path")
            override = overrides.get(permission_path)
            privileges = override if override is not None else permissions.get(permission_path, {})
            return _json_response({"data": {permission_path: privileges} if privileges else {}})
        if rel == "/nodes":
            return _json_response({"data": [{"node": node} for node in node_names]})
        if rel == "/cluster/resources":
            return _json_response({"data": list(guests)})
        if rel.startswith("/nodes/") and rel.endswith("/qemu"):
            return _json_response({"data": [g for g in guests if g.get("type") == "qemu"]})
        if rel.startswith("/nodes/") and rel.endswith("/lxc"):
            return _json_response({"data": [g for g in guests if g.get("type") == "lxc"]})
        raise AssertionError(f"unexpected PVE request path {rel!r}")

    return handler


# ---------------------------------------------------------------------------
# §28 test #16 -- unsupported PVE release, end-to-end through the real adapter
# ---------------------------------------------------------------------------


def test_16_unsupported_pve_release_fails_closed_end_to_end() -> None:
    transport = _transport(_make_pve_handler(release="8.4"))
    try:
        with pytest.raises(ProviderContractError, match="outside provider contract"):
            ProxmoxProviderV1.collect_boundary_baseline(transport)
    finally:
        transport.close()


# ---------------------------------------------------------------------------
# §28 test #17 -- ACL/permission failure, end-to-end
# ---------------------------------------------------------------------------


def test_17_missing_permission_produces_configuration_error_end_to_end() -> None:
    transport = _transport(
        _make_pve_handler(permission_overrides={"/vms": {}}),
    )
    try:
        result = ProxmoxProviderV1.collect_boundary_baseline(transport)
        assert result.completeness is BaselineCompleteness.CONFIGURATION_ERROR
    finally:
        transport.close()


# ---------------------------------------------------------------------------
# §28 test #18 -- cluster baseline, end-to-end
# ---------------------------------------------------------------------------


def test_18_cluster_baseline_completes_end_to_end() -> None:
    guests = ({"vmid": 100, "type": "qemu", "node": "pve-a"},)
    transport = _transport(_make_pve_handler(mode="cluster", guests=guests))
    try:
        result = ProxmoxProviderV1.collect_boundary_baseline(
            transport, expected_mode=BaselineMode.CLUSTER
        )
        assert result.mode is BaselineMode.CLUSTER
        assert result.completeness is BaselineCompleteness.COMPLETE
        assert result.guest_rows == guests
    finally:
        transport.close()


# ---------------------------------------------------------------------------
# §28 test #19 -- standalone baseline, end-to-end
# ---------------------------------------------------------------------------


def test_19_standalone_baseline_completes_end_to_end() -> None:
    guests = ({"vmid": 100, "type": "qemu", "node": "pve-a"}, {"vmid": 101, "type": "lxc", "node": "pve-a"})
    transport = _transport(_make_pve_handler(mode="standalone", guests=guests))
    try:
        result = ProxmoxProviderV1.collect_boundary_baseline(
            transport, expected_mode=BaselineMode.STANDALONE
        )
        assert result.mode is BaselineMode.STANDALONE
        assert result.completeness is BaselineCompleteness.COMPLETE
        assert sorted(result.guest_rows, key=lambda g: g["vmid"]) == sorted(
            guests, key=lambda g: g["vmid"]
        )
    finally:
        transport.close()
