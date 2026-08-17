"""Production GET-only Proxmox VE HTTP transport for the R0 read-only runtime.

Implements exactly §9/§10 of
``docs/architecture/0.5-r0-read-only-runtime-activation.md`` (WAVE R0-B,
Family 2). This module is deliberately narrow: it is the concrete
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

from collections.abc import Mapping
import json
from types import TracebackType
from typing import Any

import httpx

from app.inventory.provider import ProviderContractError, ProviderFailureKind

# Conservative, finite tuning defaults (§31 item 3 -- implementation
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
    # §9: "no verify=False production fallback" -- construction-time
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


class ProxmoxHttpTransport:
    """Narrow, GET-only, synchronous production PVE transport.

    Constructed fresh per discovery run from that run's own captured
    ``expected_canonical_transport_locator`` (§9) -- never a long-lived
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
            # uncaught construction-time exception (§9, §12).
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
        buffered in memory before the cap is enforced (§9). Status/
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
                    # transport outage (ADR 0002: token/effective-ACL/
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
            raise PveTransportError(
                f"PVE request timed out: {exc}", kind=ProviderFailureKind.TRANSPORT
            ) from exc
        except httpx.HTTPError as exc:
            raise PveTransportError(
                f"PVE request failed: {exc}", kind=ProviderFailureKind.TRANSPORT
            ) from exc

        try:
            return json.loads(bytes(buffer))
        except ValueError as exc:
            raise PveTransportError(
                "PVE response body is not valid JSON", kind=ProviderFailureKind.SCHEMA
            ) from exc
