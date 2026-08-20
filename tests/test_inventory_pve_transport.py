"""WAVE R0-B Family 2 -- production GET-only Proxmox VE HTTP transport.

Covers §28 tests #9-#19 of
docs/architecture/0.5-r0-read-only-runtime-activation.md. No real network
access anywhere in this file: every request is intercepted in-process by
``httpx.MockTransport``, exactly like every other ``app.inventory`` test
uses a fake/mock transport boundary instead of a live endpoint.
"""

from __future__ import annotations

import ssl

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


# ---------------------------------------------------------------------------
# Corrective pass, P2 Finding 2 -- genuinely streaming, bounded response read
# ---------------------------------------------------------------------------


def test_finding2_oversized_chunked_body_aborts_without_full_consumption() -> None:
    consumed_chunk_count = {"value": 0}
    total_chunks = 1000
    chunk_size = 1024  # 1000 * 1024 bytes >> the configured cap below

    def body_chunks():
        for _ in range(total_chunks):
            consumed_chunk_count["value"] += 1
            yield b"x" * chunk_size

    def handler(request: httpx.Request) -> httpx.Response:
        # No Content-Length -- httpx does not set one for iterable/streamed
        # content, exactly the "chunked response without a trustworthy
        # Content-Length" case the finding describes.
        return httpx.Response(200, content=body_chunks())

    transport = _transport(handler, max_response_bytes=4096)
    try:
        with pytest.raises(PveTransportError, match="maximum allowed response size") as exc:
            transport.get("/version")
        assert exc.value.kind is ProviderFailureKind.SCHEMA
    finally:
        transport.close()

    # The whole point: the adapter must abort as soon as the cap is
    # crossed, never drain the full 1000-chunk (~1 MiB) oversized stream.
    assert 0 < consumed_chunk_count["value"] < total_chunks
    assert consumed_chunk_count["value"] <= 10


def test_finding2_content_length_over_cap_rejected_before_any_body_read() -> None:
    body_read = {"value": False}

    def body_chunks():
        body_read["value"] = True
        yield b"x" * 100

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=body_chunks(),
            headers={"content-length": str(10 * 1024 * 1024)},
        )

    transport = _transport(handler, max_response_bytes=4096)
    try:
        with pytest.raises(PveTransportError, match="maximum allowed response size"):
            transport.get("/version")
    finally:
        transport.close()
    assert body_read["value"] is False


def test_finding2_bounded_body_within_cap_still_succeeds() -> None:
    payload = {"data": {"release": "9.0"}}
    transport = _transport(
        lambda request: httpx.Response(200, json=payload), max_response_bytes=4096
    )
    try:
        assert transport.get("/version") == payload
    finally:
        transport.close()


# ---------------------------------------------------------------------------
# Corrective pass, P2 Finding 3 -- PVE 401/403 classify as SECURITY_PROOF
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status_code", (401, 403))
def test_finding3_401_403_classify_as_security_proof(status_code: int) -> None:
    transport = _transport(lambda request: httpx.Response(status_code, json={"data": None}))
    try:
        with pytest.raises(PveTransportError) as exc:
            transport.get("/version")
        assert exc.value.kind is ProviderFailureKind.SECURITY_PROOF
    finally:
        transport.close()


def test_finding3_500_still_classifies_as_transport() -> None:
    transport = _transport(lambda request: httpx.Response(500, json={"data": None}))
    try:
        with pytest.raises(PveTransportError) as exc:
            transport.get("/version")
        assert exc.value.kind is ProviderFailureKind.TRANSPORT
    finally:
        transport.close()


# ---------------------------------------------------------------------------
# Real dogfood #2, Finding A -- TLS certificate verification failures must
# classify as SECURITY_PROOF (-> configuration_error), not TRANSPORT
# (-> source_unavailable). The real evidence: a legacy PVE root CA missing
# the X509v3 Key Usage extension made Python 3.13's strict certificate-chain
# validation reject the leaf with ssl.SSLCertVerificationError, which httpx/
# httpcore wrapped inside an outer httpx.ConnectError before it ever reached
# this module.
# ---------------------------------------------------------------------------


def _wrapped_cert_verification_handler(request: httpx.Request) -> httpx.Response:
    # Reproduces the real dogfood #2 wrapping shape: the typed
    # ssl.SSLCertVerificationError only reachable via __cause__, exactly as
    # httpx/httpcore actually wrap it -- never a bare httpx.ConnectError
    # with no ssl exception underneath.
    try:
        raise ssl.SSLCertVerificationError(
            "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
            "CA cert does not include key usage extension"
        )
    except ssl.SSLCertVerificationError as ssl_exc:
        raise httpx.ConnectError(
            "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
            "CA cert does not include key usage extension"
        ) from ssl_exc


def test_findingA_wrapped_cert_verification_error_classifies_as_security_proof() -> None:
    transport = _transport(_wrapped_cert_verification_handler)
    try:
        with pytest.raises(PveTransportError, match="certificate verification") as exc:
            transport.get("/version")
        assert exc.value.kind is ProviderFailureKind.SECURITY_PROOF
    finally:
        transport.close()


def test_findingA_cert_verification_error_reachable_only_via_context_not_cause() -> None:
    # __context__ (implicit chaining, e.g. a bare `raise NewExc(...)` inside
    # an `except ssl.SSLCertVerificationError:` block, no `from`) must be
    # walked too -- not only __cause__.
    def handler(request: httpx.Request) -> httpx.Response:
        try:
            raise ssl.SSLCertVerificationError("certificate verify failed")
        except ssl.SSLCertVerificationError:
            raise httpx.ConnectError("connection failed")  # noqa: B904 -- deliberate implicit chaining

    transport = _transport(handler)
    try:
        with pytest.raises(PveTransportError) as exc:
            transport.get("/version")
        assert exc.value.kind is ProviderFailureKind.SECURITY_PROOF
    finally:
        transport.close()


def test_findingA_generic_connection_failure_without_cert_error_remains_transport() -> None:
    # Same outer exception TYPE as the wrapped-cert case above (ConnectError)
    # but with no ssl.SSLCertVerificationError anywhere in its chain --
    # proves the detection is genuinely structural (typed chain walk), not
    # merely "any ConnectError is now SECURITY_PROOF".
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    transport = _transport(handler)
    try:
        with pytest.raises(PveTransportError) as exc:
            transport.get("/version")
        assert exc.value.kind is ProviderFailureKind.TRANSPORT
    finally:
        transport.close()


def test_findingA_timeout_remains_transport_even_if_it_wraps_no_cert_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("handshake timed out", request=request)

    transport = _transport(handler)
    try:
        with pytest.raises(PveTransportError) as exc:
            transport.get("/version")
        assert exc.value.kind is ProviderFailureKind.TRANSPORT
    finally:
        transport.close()


def test_findingA_invalid_ca_bundle_construction_remains_security_proof(tmp_path) -> None:
    missing_ca = tmp_path / "does-not-exist.pem"
    with pytest.raises(PveTransportError) as exc:
        ProxmoxHttpTransport(
            canonical_transport_locator=LOCATOR,
            pve_api_token=TOKEN,
            verify=str(missing_ca),
        )
    assert exc.value.kind is ProviderFailureKind.SECURITY_PROOF


def test_findingA_401_still_classifies_as_security_proof_not_transport() -> None:
    # Regression guard: Finding A's new cert-error branch is nested inside
    # the same httpx.HTTPError except clause as the generic transport
    # fallback -- must never accidentally shadow the pre-existing 401/403
    # status-code check, which runs entirely inside the `try` body, before
    # any exception is even raised.
    transport = _transport(lambda request: httpx.Response(401, json={"data": None}))
    try:
        with pytest.raises(PveTransportError) as exc:
            transport.get("/version")
        assert exc.value.kind is ProviderFailureKind.SECURITY_PROOF
    finally:
        transport.close()


def test_findingA_error_text_never_contains_the_pve_token() -> None:
    transport = _transport(_wrapped_cert_verification_handler)
    try:
        with pytest.raises(PveTransportError) as exc:
            transport.get("/version")
        assert TOKEN not in str(exc.value)
    finally:
        transport.close()


def test_findingA_exception_chain_walk_is_bounded_and_cycle_safe() -> None:
    from app.inventory_pve_transport import _find_certificate_verification_error

    # A self-referential __context__ cycle must terminate, not loop forever.
    a = ValueError("a")
    b = ValueError("b")
    a.__context__ = b
    b.__context__ = a
    assert _find_certificate_verification_error(a) is None

    # A very deep chain (deeper than the bound) with the cert error placed
    # beyond the bound must not be found -- proves the bound is real and
    # enforced, not merely decorative.
    deepest = ssl.SSLCertVerificationError("too deep")
    current: BaseException = deepest
    for _ in range(50):
        wrapper = ValueError("wrapper")
        wrapper.__cause__ = current
        current = wrapper
    assert _find_certificate_verification_error(current) is None

    # The same cert error placed well within the bound must be found.
    shallow_wrapper = ValueError("shallow")
    shallow_wrapper.__cause__ = ssl.SSLCertVerificationError("shallow cert failure")
    assert _find_certificate_verification_error(shallow_wrapper) is not None


def test_findingA_exception_chain_bounds_total_work_not_only_distinct_visits() -> None:
    # P3 hardening (independent review): every node's __cause__ AND
    # __context__ point to the SAME next node, so each real hop is
    # enqueued twice -- this must exhaust the traversal's iteration
    # budget on the resulting duplicate pops, reaching noticeably FEWER
    # distinct real nodes than a duplicate-free chain of the same length
    # would. This proves the bound caps TOTAL queue pops (including
    # wasted duplicate ones), not merely a count of genuinely-new nodes
    # visited -- a bound that only capped new visits would still walk
    # the full chain despite the duplication, doing unbounded extra work
    # for a sufficiently duplicate-heavy graph.
    from app.inventory_pve_transport import _exception_chain

    deepest = ValueError("deepest")
    current: BaseException = deepest
    for _ in range(30):
        node = ValueError("node")
        node.__cause__ = current
        node.__context__ = current  # identical to __cause__ -- doubles queue entries per hop
        current = node

    visited = list(_exception_chain(current))
    # Empirically 11 distinct nodes reached (well under the 20-node cap
    # a duplicate-free chain would reach) -- asserted as a bound, not the
    # exact count, so this stays robust to a future retuning of
    # _EXCEPTION_GRAPH_MAX_NODES while still proving genuine work-capping.
    assert len(visited) < 15
    assert deepest not in visited


def test_findingA_cert_error_found_via_context_even_when_a_different_cause_is_also_set() -> None:
    # P3 finding (independent review): an earlier version walked
    # `current.__cause__ or current.__context__` -- only ONE edge per
    # node -- so a node with BOTH a __cause__ (some unrelated, non-cert
    # exception) and a __context__ (the real cert error) would silently
    # never have its __context__ edge explored at all, since __cause__
    # being truthy short-circuits the `or`. Both attributes are
    # independent in real Python exception handling (a `raise X from Y`
    # sets __cause__ without clearing whatever __context__ the runtime
    # had already recorded for X) -- this reproduces that shape directly.
    from app.inventory_pve_transport import _find_certificate_verification_error

    non_cert_cause = ValueError("unrelated cause, not a cert error")
    cert_error = ssl.SSLCertVerificationError("only reachable via __context__")
    outer = httpx.ConnectError("connection failed")
    outer.__cause__ = non_cert_cause
    outer.__context__ = cert_error

    assert _find_certificate_verification_error(outer) is cert_error


def test_findingA_wrapped_cert_error_via_context_classifies_as_security_proof_end_to_end() -> None:
    # Same shape as above, exercised through the real transport.get()
    # path -- proves the graph walk (not just the standalone helper) is
    # wired correctly when a real request raises exactly this shape.
    def handler(request: httpx.Request) -> httpx.Response:
        non_cert_cause = ValueError("unrelated cause, not a cert error")
        cert_error = ssl.SSLCertVerificationError("only reachable via __context__")
        outer = httpx.ConnectError("connection failed")
        outer.__cause__ = non_cert_cause
        outer.__context__ = cert_error
        raise outer

    transport = _transport(handler)
    try:
        with pytest.raises(PveTransportError) as exc:
            transport.get("/version")
        assert exc.value.kind is ProviderFailureKind.SECURITY_PROOF
    finally:
        transport.close()
