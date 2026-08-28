"""R0 source bootstrap / configuration loader for the 0.5 read-only runtime.

See ``ARCHITECTURE.md``. This is a small, R0-dedicated settings loader —
deliberately not a general application-settings type, so the composition
root never pulls in configuration shapes it does not need.

Configuration here describes a Proxmox **source**, never workloads: there
is no configured list of VMIDs, LXC containers, QEMU VMs, or per-resource
settings anywhere in this module. Every node/resource is discovered
dynamically through the already-implemented ``app.inventory`` provider/
discovery/reconciliation chain (see ``app/inventory_pve_transport.py`` and
``app/inventory_scheduler.py``).

Two functions matter to callers:

- :func:`load_r0_runtime_config` parses the YAML config file plus the
  process environment (secrets never live in the YAML file) into an
  immutable :class:`R0RuntimeConfig`.
- :func:`bootstrap_or_reconcile_source` performs steps 2-4 of's exact
  startup decision sequence (empty-DB bootstrap, or restart comparison
  against the durable source with explicit controlled-transition/fail-
  closed handling). It deliberately does **not** perform startup discovery-
  run recovery — that is ``app/inventory_scheduler.py``'s responsibility,
  and it must run *before* this function is called: startup discovery-run
  recovery always precedes any configuration comparison.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import os
from pathlib import Path
from typing import Any

import yaml

from app.inventory import (
    InventoryAuthority,
    InventoryAuthorityStore,
    InventorySourceState,
    canonicalize_transport_locator,
)

PROVIDER_KIND_PROXMOX_VE = "proxmox_ve"

# Recommended minimum entropy for the R0 API bearer token. Not
# mechanically enforced beyond a floor on length: the credential is opaque
# and operator-controlled either way, so this is guidance, not a security
# boundary.
_MIN_API_TOKEN_LENGTH = 16


class R0ConfigError(ValueError):
    """The R0 source-bootstrap configuration is malformed, incomplete, or a
    required secret is missing."""


class R0ConfigDriftError(RuntimeError):
    """A durable source field differs from the configured value in a way
    that is not an explicitly-permitted controlled transition.

    The exact mismatched field is always named in the message so an
    operator can either revert their config change or perform the correct
    explicit procedure.
    """


@dataclass(frozen=True, slots=True)
class R0TlsConfig:
    """PVE endpoint TLS trust configuration. Distinct from, and never
    conflated with, HA<->R0's own transport trust."""

    verify: bool = True
    ca_bundle_path: str | None = None


@dataclass(frozen=True, slots=True)
class R0SourceConfig:
    """Exactly the source-level fields ``create_inventory_source`` accepts.

    No VMID, LXC/QEMU list, node membership, or per-resource field exists
    here by design.
    """

    display_name: str
    provider_kind: str
    transport_locator: str
    freshness_duration_seconds: int
    credential_reference: str
    tls: R0TlsConfig


@dataclass(frozen=True, slots=True)
class R0RuntimeConfig:
    """Fully loaded R0 runtime configuration, secrets included in memory only.

    Never serialize this object, log it, or persist ``pve_api_token``/
    ``api_bearer_token`` anywhere — they are process-memory-only secrets
    read once from the environment at startup.
    """

    source: R0SourceConfig
    authority_db_path: Path
    pve_api_token: str = field(repr=False)
    api_bearer_token: str = field(repr=False)

    def __repr__(self) -> str:  # never leak secrets via logging/debugging
        return (
            "R0RuntimeConfig(source="
            f"{self.source!r}, authority_db_path={self.authority_db_path!r}, "
            "pve_api_token=<redacted>, api_bearer_token=<redacted>)"
        )


def _require_mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise R0ConfigError(f"{name} must be a mapping")
    return value


def _require_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise R0ConfigError(f"{name} must be non-empty text")
    return value


def _require_positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise R0ConfigError(f"{name} must be a positive integer")
    return value


def _require_bool(value: Any, name: str, *, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise R0ConfigError(f"{name} must be a boolean")
    return value


def _require_env_secret(env: Mapping[str, str], var_name: str, *, purpose: str) -> str:
    if not isinstance(var_name, str) or not var_name.strip():
        raise R0ConfigError(f"{purpose} environment variable name must be non-empty text")
    value = env.get(var_name)
    if value is None or not value.strip():
        raise R0ConfigError(
            f"required secret environment variable {var_name!r} ({purpose}) "
            "is missing or empty"
        )
    return value


def parse_r0_runtime_config(
    raw: Mapping[str, Any], *, env: Mapping[str, str]
) -> R0RuntimeConfig:
    """Parse an already-loaded YAML mapping plus environment into config.

    Fails closed (raises :class:`R0ConfigError`) on any malformed, missing,
    or empty required field or secret. Never falls back to a default for a
    security-relevant field (requires an explicit operator freshness
    value rather than silently relying on the authority's own default).
    """

    source_raw = _require_mapping(raw.get("source"), "source")
    runtime_raw = _require_mapping(raw.get("runtime"), "runtime")

    display_name = _require_text(source_raw.get("display_name"), "source.display_name")
    provider_kind = _require_text(source_raw.get("provider_kind"), "source.provider_kind")
    if provider_kind != PROVIDER_KIND_PROXMOX_VE:
        raise R0ConfigError(
            "source.provider_kind must be exactly "
            f"{PROVIDER_KIND_PROXMOX_VE!r} -- the only supported provider"
        )
    transport_locator = _require_text(source_raw.get("pve_endpoint"), "source.pve_endpoint")
    # Fail closed immediately on a syntactically invalid/non-HTTPS locator
    # rather than deferring the error to first discovery-run issuance.
    try:
        canonicalize_transport_locator(transport_locator)
    except ValueError as exc:
        raise R0ConfigError(f"source.pve_endpoint is invalid: {exc}") from exc
    freshness_duration_seconds = _require_positive_int(
        source_raw.get("freshness_duration_seconds"),
        "source.freshness_duration_seconds",
    )
    credential_reference = _require_text(
        source_raw.get("credential_reference"), "source.credential_reference"
    )
    if not credential_reference.startswith("secret://"):
        # Mirrors app.inventory.authority's own opaque-reference contract
        # (never the PVE token itself) -- validated here too so a malformed
        # value fails closed with a clear R0-specific message before ever
        # reaching the authority layer.
        raise R0ConfigError(
            "source.credential_reference must be an opaque secret reference "
            "starting with 'secret://' (never the PVE token itself)"
        )
    pve_token_env = _require_text(source_raw.get("pve_token_env"), "source.pve_token_env")

    tls_raw = source_raw.get("tls") or {}
    if not isinstance(tls_raw, Mapping):
        raise R0ConfigError("source.tls must be a mapping")
    tls_verify = _require_bool(tls_raw.get("verify"), "source.tls.verify", default=True)
    if not tls_verify:
        #: mandatory strict TLS verification, no operator-facing
        # "skip verification" flag. The field is accepted (and must default
        # True) only so a future explicit CA-bundle-only shape has a stable
        # place to live; it must never be used to disable verification.
        raise R0ConfigError(
            "source.tls.verify must not be false; R0 does not support "
            "disabling PVE endpoint certificate verification"
        )
    ca_bundle_path = tls_raw.get("ca_bundle_path")
    if ca_bundle_path is not None:
        _require_text(ca_bundle_path, "source.tls.ca_bundle_path")
        # Fail closed at startup rather than at first discovery-run
        # transport construction: a missing/unreadable CA bundle
        # must never be discovered only after a durable run has already
        # been issued. This does not eliminate the runtime race (the file
        # can still disappear between startup and a later cycle) -- that
        # remaining window is closed separately by the production
        # transport's own construction-time exception classification.
        ca_path = Path(ca_bundle_path)
        if not ca_path.is_file():
            raise R0ConfigError(
                f"source.tls.ca_bundle_path {ca_bundle_path!r} does not "
                "exist or is not a regular file"
            )
        try:
            with ca_path.open("rb") as handle:
                handle.read(1)
        except OSError as exc:
            raise R0ConfigError(
                f"source.tls.ca_bundle_path {ca_bundle_path!r} is not "
                f"readable: {exc}"
            ) from exc

    authority_db_path = Path(
        _require_text(runtime_raw.get("authority_db_path"), "runtime.authority_db_path")
    )
    api_token_env = _require_text(
        runtime_raw.get("api_token_env"), "runtime.api_token_env"
    )

    pve_api_token = _require_env_secret(env, pve_token_env, purpose="PVE API token")
    api_bearer_token = _require_env_secret(env, api_token_env, purpose="R0 API bearer token")
    if len(api_bearer_token) < _MIN_API_TOKEN_LENGTH:
        raise R0ConfigError(
            "R0 API bearer token does not meet the minimum recommended length"
        )

    return R0RuntimeConfig(
        source=R0SourceConfig(
            display_name=display_name,
            provider_kind=provider_kind,
            transport_locator=transport_locator,
            freshness_duration_seconds=freshness_duration_seconds,
            credential_reference=credential_reference,
            tls=R0TlsConfig(verify=tls_verify, ca_bundle_path=ca_bundle_path),
        ),
        authority_db_path=authority_db_path,
        pve_api_token=pve_api_token,
        api_bearer_token=api_bearer_token,
    )


def load_r0_runtime_config(
    config_path: Path, *, env: Mapping[str, str] | None = None
) -> R0RuntimeConfig:
    """Load and validate the R0 YAML config file plus environment secrets."""

    resolved_env = os.environ if env is None else env
    try:
        text = Path(config_path).read_text(encoding="utf-8")
    except OSError as exc:
        raise R0ConfigError(f"cannot read R0 config file {config_path}: {exc}") from exc
    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise R0ConfigError(f"R0 config file {config_path} is not valid YAML: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise R0ConfigError("R0 config file must contain a top-level mapping")
    return parse_r0_runtime_config(raw, env=resolved_env)


def bootstrap_or_reconcile_source(
    authority: InventoryAuthority,
    store: InventoryAuthorityStore,
    config: R0RuntimeConfig,
) -> InventorySourceState:
    """Bootstrap or reconcile the single configured source.

    Startup discovery-run recovery is excluded; ``app/inventory_scheduler.py``
    owns it and must have run first.

    - zero durable sources: create exactly one, per the configured values;
    - exactly one durable source: compare field-by-field and either proceed
      unchanged, apply an explicitly-permitted controlled transition
      (credential rotation, freshness-duration change), or fail closed with
      :class:`R0ConfigDriftError` naming the exact mismatched field;
    - more than one durable source: out of scope for this single-source
      runtime — fails closed rather than guessing which source the
      configuration describes.

    Callers must ensure any active discovery-run ownership for the source
    has already been cleared by the scheduler's startup recovery before
    calling this function — the underlying ``rotate_credential_reference``/
    ``configure_freshness_duration`` authority methods themselves refuse to
    proceed while a run is active (``AuthorityConflict``), so this ordering
    requirement is enforced defensively even if a caller gets it wrong.
    """

    states = store.list_source_states()
    if len(states) == 0:
        return authority.create_inventory_source(
            provider_kind=config.source.provider_kind,
            display_name=config.source.display_name,
            credential_reference=config.source.credential_reference,
            transport_locator=config.source.transport_locator,
            freshness_duration_seconds=config.source.freshness_duration_seconds,
        )
    if len(states) > 1:
        raise R0ConfigDriftError(
            "multi-source authority database is out of scope for this "
            "single-source runtime; refusing to guess which source the "
            "configuration describes"
        )

    state = states[0]
    source = state.source
    endpoint = state.active_endpoint

    canonical_locator = canonicalize_transport_locator(config.source.transport_locator)
    if canonical_locator != endpoint.canonical_transport_locator:
        raise R0ConfigDriftError(
            "configured source.pve_endpoint does not match the durable active "
            "endpoint's canonical_transport_locator; bootstrap is not "
            "endpoint failover -- revert the config change or perform "
            "the correct explicit operator procedure"
        )
    if config.source.provider_kind != source.provider_kind:
        raise R0ConfigDriftError(
            "configured source.provider_kind does not match the durable source"
        )
    if config.source.display_name != source.display_name:
        raise R0ConfigDriftError(
            "configured source.display_name does not match the durable source; "
            "rename is not an automatic config-drift transition"
        )

    changed = False
    if config.source.credential_reference != source.credential_reference:
        authority.rotate_credential_reference(
            source.inventory_source_id, config.source.credential_reference
        )
        changed = True
    if config.source.freshness_duration_seconds != source.freshness_duration_seconds:
        authority.configure_freshness_duration(
            source.inventory_source_id, config.source.freshness_duration_seconds
        )
        changed = True

    if changed:
        return store.source_state(source.inventory_source_id)
    return state
