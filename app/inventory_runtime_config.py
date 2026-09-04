"""R0 source and package-scan runtime configuration loader.

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
from urllib.parse import urlsplit

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
DEFAULT_PACKAGE_SCAN_INTERVAL_SECONDS = 6 * 60 * 60
DEFAULT_PACKAGE_SCAN_INITIAL_DELAY_SECONDS = 30


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
class R0HostControlConfig:
    host: str
    port: int
    user: str
    private_key_path: Path
    known_hosts_path: Path
    timeout_seconds: int
    max_result_bytes: int


@dataclass(frozen=True, slots=True)
class R0PackageScanConfig:
    interval_seconds: int
    initial_delay_seconds: int
    host_control: R0HostControlConfig


# ---------------------------------------------------------------------------
# Package-update production execution boundaries.
#
# Source-level execution-boundary information ONLY. There is no VMID, no
# resource id, no per-guest setting, and no managed-resource list here, for
# exactly the reason there is none in the source section above: adding or
# removing a PVE guest must never require a config change.
#
# There are also deliberately no timeout knobs. Each stage already owns its
# bound -- three of them are load-bearing ceilings derived in
# `app.inventory.contention_policy` from how long a bounded host round trip
# may hold the authority store's writer lock -- and a per-installation
# override would let an operator quietly widen a safety bound. The constants
# below are those exact existing bounds, named once.
# ---------------------------------------------------------------------------

#: Snapshot submission/seal/inspection. Bounded by the writer-lock ceiling.
PACKAGE_UPDATE_SNAPSHOT_TIMEOUT_SECONDS = 60
#: The execution-time equality gate: an APT metadata refresh plus a full
#: `-s upgrade` simulation, entirely outside every writer transaction. Same
#: budget the package scanner already uses for the same two operations.
PACKAGE_UPDATE_EXECUTION_TIMEOUT_SECONDS = 900
#: The mutation stage's read-only PREPARE/inspect round trips -- metadata
#: refresh, simulation, and two dpkg identity reads -- outside the lock.
PACKAGE_UPDATE_MUTATION_TIMEOUT_SECONDS = 600
#: The mutation stage's submit/seal round trips, which run while the backend
#: holds the authority writer lock. Never waits for `apt-get` itself.
PACKAGE_UPDATE_MUTATION_SUBMISSION_TIMEOUT_SECONDS = 60
#: The rollback stage's submit/seal round trips, under the writer lock.
PACKAGE_UPDATE_ROLLBACK_SUBMISSION_TIMEOUT_SECONDS = 60
#: The rollback stage's read-only inspection, outside the lock.
PACKAGE_UPDATE_ROLLBACK_INSPECTION_TIMEOUT_SECONDS = 120
#: One read-only health evaluation of a complete frozen probe set.
PACKAGE_UPDATE_HEALTH_TIMEOUT_SECONDS = 300
#: Shared bounded response ceiling for every production update host control.
PACKAGE_UPDATE_MAX_RESULT_BYTES = 8 * 1024 * 1024

#: One dedicated private key per privileged forced-command boundary. The
#: five host-side helpers are five different privilege boundaries -- create a
#: snapshot, simulate a plan, mutate packages, roll a guest back, read health
#: -- so one key must never be able to reach two of them. The package-scan
#: key is a sixth, separate, unchanged boundary and is never reused here.
PACKAGE_UPDATE_KEY_FIELDS: tuple[tuple[str, str], ...] = (
    ("snapshot_private_key_path", "id_ed25519_snapshot"),
    ("execution_private_key_path", "id_ed25519_execution"),
    ("mutation_private_key_path", "id_ed25519_mutation"),
    ("rollback_private_key_path", "id_ed25519_rollback"),
    ("health_private_key_path", "id_ed25519_health"),
)

_HOST_CONTROL_DIR = "/etc/hubinet-ops/host-control"


@dataclass(frozen=True, slots=True)
class R0PackageUpdateHostControlConfig:
    """How to reach each privileged forced-command boundary on the source.

    Host, port, user, and pinned `known_hosts` are legitimately shared: they
    describe the one configured PVE source's SSH endpoint, which is the same
    endpoint for every boundary. The PRIVATE KEYS are not shared, because the
    key is what selects which forced command the connection may run.
    """

    host: str
    port: int
    user: str
    known_hosts_path: Path
    snapshot_private_key_path: Path
    execution_private_key_path: Path
    mutation_private_key_path: Path
    rollback_private_key_path: Path
    health_private_key_path: Path

    def private_key_paths(self) -> tuple[Path, ...]:
        return tuple(
            getattr(self, name) for name, _default in PACKAGE_UPDATE_KEY_FIELDS
        )


@dataclass(frozen=True, slots=True)
class R0PackageUpdateConfig:
    """Production activation of the operator-triggered update lifecycle.

    ``enabled`` is the whole activation switch. When it is false the backend
    builds no update host control, starts no update worker, and serves no
    update route -- the lifecycle stays exactly as unreachable as it was
    before this release. When it is true, a missing or unreadable required
    helper credential fails startup closed rather than deferring the failure
    to the first real operator request.
    """

    enabled: bool
    host_control: R0PackageUpdateHostControlConfig | None


@dataclass(frozen=True, slots=True)
class R0RuntimeConfig:
    """Fully loaded R0 runtime configuration, secrets included in memory only.

    Never serialize this object, log it, or persist ``pve_api_token``/
    ``api_bearer_token`` anywhere — they are process-memory-only secrets
    read once from the environment at startup.
    """

    source: R0SourceConfig
    authority_db_path: Path
    package_scan: R0PackageScanConfig
    package_update: R0PackageUpdateConfig
    pve_api_token: str = field(repr=False)
    api_bearer_token: str = field(repr=False)

    def __repr__(self) -> str:  # never leak secrets via logging/debugging
        return (
            "R0RuntimeConfig(source="
            f"{self.source!r}, authority_db_path={self.authority_db_path!r}, "
            f"package_scan={self.package_scan!r}, "
            f"package_update={self.package_update!r}, "
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


def _require_bounded_int(
    value: Any, name: str, *, minimum: int, maximum: int
) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        raise R0ConfigError(
            f"{name} must be an integer from {minimum} through {maximum}"
        )
    return value


def validate_package_scan_interval_seconds(value: Any) -> int:
    """Shared validation for config now and a future controlled-input writer."""

    return _require_bounded_int(
        value,
        "package_scan.interval_seconds",
        minimum=60,
        maximum=7 * 24 * 60 * 60,
    )


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


def _parse_package_update_config(
    value: Any, *, default_host: str
) -> R0PackageUpdateConfig:
    """Parse the production update-execution boundary section.

    Absent or ``enabled: false`` means the lifecycle is not activated on this
    installation and NOTHING is constructed for it. ``enabled: true`` requires
    every one of the five dedicated helper credentials to be present, absolute,
    and readable *now*, at startup: discovering a missing privileged credential
    at the moment an operator asks for a real package mutation would be
    discovering it far too late.
    """

    raw = _require_mapping(value, "package_update")
    enabled = _require_bool(raw.get("enabled"), "package_update.enabled", default=False)
    host_control_value = raw.get("host_control") or {}
    host_control_raw = _require_mapping(host_control_value, "package_update.host_control")
    if not enabled:
        # Deliberately not parsed further. An installation that has not
        # activated the lifecycle must not fail startup over the shape of a
        # section nothing will read.
        return R0PackageUpdateConfig(enabled=False, host_control=None)

    key_paths: dict[str, Path] = {}
    for name, default_basename in PACKAGE_UPDATE_KEY_FIELDS:
        key_paths[name] = Path(
            _require_text(
                host_control_raw.get(name, f"{_HOST_CONTROL_DIR}/{default_basename}"),
                f"package_update.host_control.{name}",
            )
        )
    known_hosts_path = Path(
        _require_text(
            host_control_raw.get("known_hosts_path", f"{_HOST_CONTROL_DIR}/known_hosts"),
            "package_update.host_control.known_hosts_path",
        )
    )
    host_control = R0PackageUpdateHostControlConfig(
        host=_require_text(
            host_control_raw.get("host", default_host),
            "package_update.host_control.host",
        ),
        port=_require_bounded_int(
            host_control_raw.get("port", 22),
            "package_update.host_control.port",
            minimum=1,
            maximum=65535,
        ),
        user=_require_text(
            host_control_raw.get("user", "root"),
            "package_update.host_control.user",
        ),
        known_hosts_path=known_hosts_path,
        **key_paths,
    )

    required = (known_hosts_path, *host_control.private_key_paths())
    for path in required:
        if not path.is_absolute():
            raise R0ConfigError(
                "package-update host-control credential paths must be absolute"
            )
    distinct = set(host_control.private_key_paths())
    if len(distinct) != len(PACKAGE_UPDATE_KEY_FIELDS):
        # One key reaching two forced commands would silently merge two
        # different privilege boundaries -- the exact thing separate helpers
        # exist to prevent.
        raise R0ConfigError(
            "each package-update host-control boundary requires its own "
            "dedicated private key"
        )
    for path in required:
        # Fail startup CLOSED on a missing or unreadable required production
        # helper credential, once activation is enabled.
        try:
            with path.open("rb") as handle:
                handle.read(1)
        except OSError as exc:
            raise R0ConfigError(
                f"package-update host-control credential {str(path)!r} is "
                f"missing or not readable: {exc}"
            ) from exc
    return R0PackageUpdateConfig(enabled=True, host_control=host_control)


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

    package_scan_value = raw.get("package_scan") or {}
    package_scan_raw = _require_mapping(package_scan_value, "package_scan")
    host_control_value = package_scan_raw.get("host_control") or {}
    host_control_raw = _require_mapping(
        host_control_value, "package_scan.host_control"
    )
    default_host = urlsplit(transport_locator).hostname
    if default_host is None:
        raise R0ConfigError("source.pve_endpoint has no host-control hostname")
    interval_seconds = validate_package_scan_interval_seconds(
        package_scan_raw.get(
            "interval_seconds", DEFAULT_PACKAGE_SCAN_INTERVAL_SECONDS
        )
    )
    initial_delay_seconds = _require_bounded_int(
        package_scan_raw.get(
            "initial_delay_seconds", DEFAULT_PACKAGE_SCAN_INITIAL_DELAY_SECONDS
        ),
        "package_scan.initial_delay_seconds",
        minimum=0,
        maximum=600,
    )
    host_control = R0HostControlConfig(
        host=_require_text(
            host_control_raw.get("host", default_host),
            "package_scan.host_control.host",
        ),
        port=_require_bounded_int(
            host_control_raw.get("port", 22),
            "package_scan.host_control.port",
            minimum=1,
            maximum=65535,
        ),
        user=_require_text(
            host_control_raw.get("user", "root"),
            "package_scan.host_control.user",
        ),
        private_key_path=Path(
            _require_text(
                host_control_raw.get(
                    "private_key_path",
                    "/etc/hubinet-ops/host-control/id_ed25519",
                ),
                "package_scan.host_control.private_key_path",
            )
        ),
        known_hosts_path=Path(
            _require_text(
                host_control_raw.get(
                    "known_hosts_path",
                    "/etc/hubinet-ops/host-control/known_hosts",
                ),
                "package_scan.host_control.known_hosts_path",
            )
        ),
        timeout_seconds=_require_bounded_int(
            host_control_raw.get("timeout_seconds", 900),
            "package_scan.host_control.timeout_seconds",
            minimum=30,
            maximum=3600,
        ),
        max_result_bytes=_require_bounded_int(
            host_control_raw.get("max_result_bytes", 8 * 1024 * 1024),
            "package_scan.host_control.max_result_bytes",
            minimum=1024 * 1024,
            maximum=16 * 1024 * 1024,
        ),
    )
    if not host_control.private_key_path.is_absolute() or not host_control.known_hosts_path.is_absolute():
        raise R0ConfigError("package-scan host-control paths must be absolute")

    package_update = _parse_package_update_config(
        raw.get("package_update") or {}, default_host=default_host
    )
    if package_update.enabled and package_update.host_control is not None:
        # The package-scan boundary is a SIXTH, separate forced-command
        # credential (see PACKAGE_UPDATE_KEY_FIELDS's own docstring) -- the
        # five-way distinctness check inside _parse_package_update_config
        # only proves the five update keys differ from EACH OTHER, not that
        # none of them was configured to literally reuse the scan key. A
        # reused key would let the scan boundary's connection also run
        # whichever update forced command that key was meant to gate,
        # silently merging two independent privilege boundaries into one.
        if host_control.private_key_path in package_update.host_control.private_key_paths():
            raise R0ConfigError(
                "package_scan.host_control.private_key_path must be distinct "
                "from every package_update.host_control forced-command key "
                "(snapshot/execution/mutation/rollback/health) -- the "
                "package-scan SSH key must never be reused by a "
                "package-update boundary"
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
        package_scan=R0PackageScanConfig(
            interval_seconds=interval_seconds,
            initial_delay_seconds=initial_delay_seconds,
            host_control=host_control,
        ),
        package_update=package_update,
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
