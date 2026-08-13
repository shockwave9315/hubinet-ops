"""Versioned canonical HTTPS endpoint normalization for authority persistence."""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit


CANONICALIZATION_CONTRACT_VERSION = 1

_DNS_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def canonicalize_transport_locator(raw_locator: str) -> str:
    """Return the canonical v1 HTTPS transport locator or fail closed."""

    if not isinstance(raw_locator, str) or not raw_locator:
        raise ValueError("transport locator must be non-empty text")
    if raw_locator != raw_locator.strip():
        raise ValueError("transport locator must not contain surrounding whitespace")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in raw_locator):
        raise ValueError("transport locator must not contain control characters")
    if "\\" in raw_locator:
        raise ValueError("transport locator must not contain backslashes")
    if "?" in raw_locator:
        raise ValueError("transport locator must not contain a query")
    if "#" in raw_locator:
        raise ValueError("transport locator must not contain a fragment")

    try:
        parsed = urlsplit(raw_locator)
    except ValueError as exc:
        raise ValueError("transport locator is invalid") from exc

    if parsed.scheme.lower() != "https":
        raise ValueError("transport locator must use HTTPS")
    if not parsed.netloc or parsed.hostname is None:
        raise ValueError("transport locator must include a host")
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        raise ValueError("transport locator must not contain userinfo")
    if parsed.path not in {"", "/"}:
        raise ValueError("transport locator subpaths are not supported")
    if "%" in parsed.netloc:
        raise ValueError("transport locator host must not use percent encoding")

    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("transport locator has an invalid port") from exc

    if port == 0:
        raise ValueError("transport locator has an invalid port")

    host = parsed.hostname
    assert host is not None
    bracketed_host = parsed.netloc.startswith("[")
    canonical_host = _canonical_host(host, bracketed=bracketed_host)
    _validate_authority_syntax(parsed.netloc, canonical_host.startswith("["))

    if port is None or port == 443:
        return f"https://{canonical_host}"
    return f"https://{canonical_host}:{port}"


def _canonical_host(host: str, *, bracketed: bool) -> str:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if bracketed:
            raise ValueError("bracketed transport locator host must be IPv6") from None
        return _canonical_dns_name(host)
    if isinstance(address, ipaddress.IPv6Address):
        if not bracketed:
            raise ValueError("IPv6 transport locator must use brackets")
        return f"[{address.compressed}]"
    if bracketed:
        raise ValueError("only IPv6 transport locator hosts use brackets")
    return address.compressed


def _canonical_dns_name(host: str) -> str:
    if not host.isascii():
        raise ValueError("transport locator DNS host must be ASCII")
    lowered = host.lower()
    if len(lowered) > 253 or lowered.endswith("."):
        raise ValueError("transport locator DNS host is ambiguous or invalid")
    labels = lowered.split(".")
    if not labels or any(not _DNS_LABEL.fullmatch(label) for label in labels):
        raise ValueError("transport locator DNS host is invalid")
    if len(labels) == 4 and all(label.isdigit() for label in labels):
        raise ValueError("transport locator numeric host is not canonical IPv4")
    return lowered


def _validate_authority_syntax(netloc: str, ipv6: bool) -> None:
    if ipv6:
        closing = netloc.find("]")
        if not netloc.startswith("[") or closing < 0:
            raise ValueError("IPv6 transport locator must use brackets")
        suffix = netloc[closing + 1 :]
        if suffix and (not suffix.startswith(":") or not suffix[1:].isdigit()):
            raise ValueError("transport locator authority is invalid")
        if suffix == ":":
            raise ValueError("transport locator has an invalid port")
        return

    if netloc.count(":") > 1:
        raise ValueError("IPv6 transport locator must use brackets")
    if ":" in netloc:
        _, port_text = netloc.rsplit(":", 1)
        if not port_text or not port_text.isdigit():
            raise ValueError("transport locator has an invalid port")
