"""Production GET-only Proxmox VE HTTP transport for the R0 read-only runtime.

See ``ARCHITECTURE.md``. This module is deliberately narrow: it is the concrete
production implementation of ``app.inventory.provider.ReadOnlyProviderTransport``
and nothing else -- no discovery orchestration, no authority calls, no
normalization. It is fed into the existing, already-tested
``ProxmoxProviderV1``/``NormalizedDiscoverySnapshot`` machinery exactly like
any fake transport already is in ``tests/test_inventory_provider_contract.py``.

Critical transport invariant: this class must genuinely not define
``post``/``put``/``patch``/``delete``/``request`` methods (not merely avoid
calling them) -- ``ProxmoxProviderV1.require_get_transport`` actively
inspects the transport object for exactly those callables and rejects any
transport that exposes one.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
import json
import ssl
from types import TracebackType
from typing import Any

import httpx

from app.inventory.provider import ProviderContractError, ProviderFailureKind

# Conservative, finite tuning defaults (item 3 -- implementation
# tuning, not architecture). PVE baseline responses are bounded in
# practice by node/guest counts, so 8 MiB is generous headroom while still
# being a real, enforced ceiling rather than "unbounded".
DEFAULT_CONNECT_TIMEOUT_SECONDS = 5.0
DEFAULT_READ_TIMEOUT_SECONDS = 15.0
DEFAULT_WRITE_TIMEOUT_SECONDS = 5.0
DEFAULT_POOL_TIMEOUT_SECONDS = 5.0
DEFAULT_MAX_RESPONSE_BYTES = 8 * 1024 * 1024

_PVE_API_PREFIX = "/api2/json"


class PveTransportError(ProviderContractError):
    """The production PVE transport failed at the HTTP boundary.

    Carries a :class:`ProviderFailureKind` so callers (the R0 scheduler,
    Family 3) can classify the failure using the exact existing
    ``classify_provider_failure`` mapping without re-deriving it from a
    plain exception message.
    """

    def __init__(self, message: str, *, kind: ProviderFailureKind) -> None:
        super().__init__(message)
        self.kind = kind


def _require_https_locator(canonical_transport_locator: str) -> str:
    if (
        not isinstance(canonical_transport_locator, str)
        or not canonical_transport_locator.startswith("https://")
    ):
        raise PveTransportError(
            "PVE transport requires an HTTPS canonical transport locator",
            kind=ProviderFailureKind.SCHEMA,
        )
    return canonical_transport_locator


def _require_verification_enabled(verify: bool | str) -> bool | str:
    #: "no verify=False production fallback" -- construction-time
    # assertion, not merely a documented convention. A caller-supplied test
    # transport that needs to disable verification against a local fixture
    # must use its own class, never this one.
    if verify is False:
        raise PveTransportError(
            "PVE transport must not disable certificate verification",
            kind=ProviderFailureKind.SCHEMA,
        )
    return verify


def _require_pve_api_token(pve_api_token: str) -> str:
    if not isinstance(pve_api_token, str) or not pve_api_token.strip():
        raise PveTransportError(
            "PVE transport requires a non-empty API token",
            kind=ProviderFailureKind.SCHEMA,
        )
    return pve_api_token


# Observed against a real PVE host:
# a real legacy PVE root CA missing the X509v3 Key Usage extension made
# Python 3.13's strict certificate-chain validation reject the leaf with
# ``ssl.SSLCertVerificationError``. httpx/httpcore wrap that underlying ssl
# exception inside their own ``httpx.ConnectError`` (or similar) before it
# reaches this module -- the real exception is only reachable by walking
# ``__cause__``/``__context__``, not by matching on the outer exception's
# type or its rendered message text.
_EXCEPTION_GRAPH_MAX_NODES = 20


def _exception_chain(exc: BaseException):
    """Yield ``exc`` then every exception reachable via ``__cause__``
    and/or ``__context__`` -- a breadth-first walk of the exception
    *graph*, not merely a linear chain.

    Eighth-pass corrective note (P3 finding, independent review): an
    earlier version used ``current.__cause__ or current.__context__``,
    which only ever follows ONE edge per node -- if a node has both an
    explicit cause (``raise X from Y``) and an implicit context (a
    DIFFERENT exception that happened to be active when ``X`` was
    raised), the ``or`` silently discards ``__context__`` whenever
    ``__cause__`` is set, even though Python preserves both
    independently. A real ``ssl.SSLCertVerificationError`` reachable only
    through the discarded edge would never be found. This walks both
    edges from every node.

    Ninth-pass corrective note (P2/P3 finding, independent review): the
    bound below applies to UNIQUE visited nodes, not raw queue pops. An
    intermediate version bounded total pops instead, reasoning that a
    duplicate-heavy graph could otherwise cause unbounded work -- but
    that reasoning was wrong: every push onto the queue happens only as
    a direct result of visiting a genuinely NEW (not-yet-seen) node, and
    each such node pushes at most two references (its cause and its
    context). Capping unique visits at ``_EXCEPTION_GRAPH_MAX_NODES``
    therefore already caps total pushes at ``2 * _EXCEPTION_GRAPH_MAX_
    NODES``, and total pops can never exceed pushes-plus-one -- so this
    stays strictly bounded (and cycle-safe, via the visited-identity
    ``seen`` set) for ANY graph shape, including one built specifically
    to maximize duplicate references. Bounding pops instead of unique
    nodes, as the intermediate version did, is strictly worse with no
    safety benefit: a genuinely distinct, security-relevant exception
    (e.g. the real ``ssl.SSLCertVerificationError``) sitting within the
    first ``_EXCEPTION_GRAPH_MAX_NODES`` unique ancestors could be missed
    simply because earlier duplicate references exhausted a pop-based
    budget before ever reaching it.
    """

    seen: set[int] = set()
    queue: deque[BaseException] = deque([exc])
    unique_visited = 0
    while queue and unique_visited < _EXCEPTION_GRAPH_MAX_NODES:
        current = queue.popleft()
        if id(current) in seen:
            continue
        seen.add(id(current))
        unique_visited += 1
        yield current
        if current.__cause__ is not None:
            queue.append(current.__cause__)
        if current.__context__ is not None:
            queue.append(current.__context__)


def _find_certificate_verification_error(
    exc: BaseException,
) -> ssl.SSLCertVerificationError | None:
    """Find a real ``ssl.SSLCertVerificationError`` anywhere in ``exc``'s
    cause/context chain, or ``None`` if there isn't one.

    Structural/typed exception inspection only -- this is deliberately not
    a substring/regex match against any exception's rendered message text,
    which would be a brittle proxy for the real, typed failure the ssl
    module already distinguishes for us.
    """

    for candidate in _exception_chain(exc):
        if isinstance(candidate, ssl.SSLCertVerificationError):
            return candidate
    return None


class ProxmoxHttpTransport:
    """Narrow, GET-only, synchronous production PVE transport.

    Constructed fresh per discovery run from that run's own captured
    ``expected_canonical_transport_locator`` -- never a long-lived
    client pointed at a value that could go stale mid-run without the run
    knowing. Callers (Family 3's scheduler) are expected to use this as a
    context manager or call :meth:`close` once per run.
    """

    __slots__ = ("_client", "_max_response_bytes")

    def __init__(
        self,
        *,
        canonical_transport_locator: str,
        pve_api_token: str,
        verify: bool | str = True,
        connect_timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
        read_timeout_seconds: float = DEFAULT_READ_TIMEOUT_SECONDS,
        write_timeout_seconds: float = DEFAULT_WRITE_TIMEOUT_SECONDS,
        pool_timeout_seconds: float = DEFAULT_POOL_TIMEOUT_SECONDS,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        _transport: httpx.BaseTransport | None = None,
    ) -> None:
        """Build one GET-only httpx client bound to one exact PVE endpoint.

        ``_transport`` is a private testing seam only (e.g.
        ``httpx.MockTransport``) -- production callers must never pass it;
        it exists so this class's own HTTP-shaping behavior (headers,
        timeouts, redirect/proxy posture, response-size enforcement) can be
        exercised deterministically without any real network access,
        exactly like every other ``app.inventory`` test uses a fake/mock
        boundary instead of a live endpoint.
        """

        locator = _require_https_locator(canonical_transport_locator)
        token = _require_pve_api_token(pve_api_token)
        trust = _require_verification_enabled(verify)
        if max_response_bytes <= 0:
            raise PveTransportError(
                "PVE transport max_response_bytes must be positive",
                kind=ProviderFailureKind.SCHEMA,
            )

        timeout = httpx.Timeout(
            connect=connect_timeout_seconds,
            read=read_timeout_seconds,
            write=write_timeout_seconds,
            pool=pool_timeout_seconds,
        )
        self._max_response_bytes = max_response_bytes
        try:
            self._client = httpx.Client(
                base_url=locator.rstrip("/") + _PVE_API_PREFIX,
                headers={"Authorization": f"PVEAPIToken={token}"},
                timeout=timeout,
                verify=trust,
                follow_redirects=False,
                trust_env=False,
                transport=_transport,
            )
        except (OSError, ValueError) as exc:
            # A configured CA bundle path that is missing/unreadable/
            # malformed raises here (e.g. FileNotFoundError, ssl.SSLError,
            # which is an OSError subclass) -- this is a security/TLS
            # configuration problem, never a plain network outage, and it
            # must be classifiable through the same PveTransportError
            # boundary as every other transport failure rather than an
            # uncaught construction-time exception.
            raise PveTransportError(
                f"PVE transport TLS/client configuration is invalid: {exc}",
                kind=ProviderFailureKind.SECURITY_PROOF,
            ) from exc

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> ProxmoxHttpTransport:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def get(self, path: str, *, params: Mapping[str, str] | None = None) -> Any:
        """Issue exactly one GET request and return the parsed JSON body.

        Streams the response body incrementally and aborts as soon as the
        accumulated size exceeds ``max_response_bytes`` -- a chunked
        response with no trustworthy ``Content-Length`` is never fully
        buffered in memory before the cap is enforced. Status/
        redirect/size checks are evaluated from the response headers
        before any body bytes are read, so an oversized or rejected
        response never pays the cost of a full body read at all.
        """

        try:
            with self._client.stream("GET", path, params=params) as response:
                if response.is_redirect:
                    raise PveTransportError(
                        "PVE endpoint returned a redirect; automatic redirect "
                        "following is disabled by construction",
                        kind=ProviderFailureKind.TRANSPORT,
                    )

                content_length = response.headers.get("content-length")
                if content_length is not None:
                    try:
                        declared_length = int(content_length)
                    except ValueError:
                        declared_length = None
                    if (
                        declared_length is not None
                        and declared_length > self._max_response_bytes
                    ):
                        raise PveTransportError(
                            "PVE response declares a size exceeding the "
                            "maximum allowed response size",
                            kind=ProviderFailureKind.SCHEMA,
                        )

                if response.status_code in (401, 403):
                    # A stock-PVE authentication/authorization rejection is
                    # a security/configuration proof failure, never a plain
                    # transport outage (token/effective-ACL/
                    # provider-configuration problems classify as
                    # configuration_error, not source_unavailable).
                    raise PveTransportError(
                        "PVE endpoint rejected the configured credentials "
                        f"(HTTP {response.status_code})",
                        kind=ProviderFailureKind.SECURITY_PROOF,
                    )
                if response.status_code != 200:
                    raise PveTransportError(
                        f"PVE endpoint returned HTTP {response.status_code}",
                        kind=ProviderFailureKind.TRANSPORT,
                    )

                buffer = bytearray()
                for chunk in response.iter_bytes():
                    buffer.extend(chunk)
                    if len(buffer) > self._max_response_bytes:
                        # Raising here, still inside the stream context
                        # manager, closes/aborts the connection instead of
                        # continuing to consume the remainder of an
                        # oversized body.
                        raise PveTransportError(
                            "PVE response exceeds the maximum allowed "
                            "response size",
                            kind=ProviderFailureKind.SCHEMA,
                        )
        except httpx.TimeoutException as exc:
            # Caught ahead of the broader httpx.HTTPError branch below so a
            # timeout is never reclassified as a certificate failure, even
            # if a timeout happened to occur mid-TLS-handshake (Finding A
            # item 6: timeout always remains TRANSPORT).
            raise PveTransportError(
                f"PVE request timed out: {exc}", kind=ProviderFailureKind.TRANSPORT
            ) from exc
        except httpx.HTTPError as exc:
            # Finding A: a TLS certificate verification failure is a
            # security/trust/configuration proof failure, never a plain
            # network-layer outage -- detected structurally (never by
            # parsing the rendered message) by walking the real, typed
            # ssl.SSLCertVerificationError httpx/httpcore wrap inside this
            # exception's cause/context chain.
            cert_error = _find_certificate_verification_error(exc)
            if cert_error is not None:
                raise PveTransportError(
                    f"PVE TLS certificate verification failed: {cert_error}",
                    kind=ProviderFailureKind.SECURITY_PROOF,
                ) from exc
            raise PveTransportError(
                f"PVE request failed: {exc}", kind=ProviderFailureKind.TRANSPORT
            ) from exc

        try:
            return json.loads(bytes(buffer))
        except ValueError as exc:
            raise PveTransportError(
                "PVE response body is not valid JSON", kind=ProviderFailureKind.SCHEMA
            ) from exc
