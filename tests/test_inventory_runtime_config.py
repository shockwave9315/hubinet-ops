"""WAVE R0-B Family 1 -- R0 source bootstrap / configuration model.

Covers §28 tests #5, #6, #7, #8 of
docs/architecture/0.5-r0-read-only-runtime-activation.md, plus fail-closed
coverage for malformed/missing configuration and secrets.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.inventory import InventoryAuthority, InventoryAuthorityStore
from app.inventory_runtime_config import (
    PROVIDER_KIND_PROXMOX_VE,
    R0ConfigDriftError,
    R0ConfigError,
    R0RuntimeConfig,
    R0SourceConfig,
    R0TlsConfig,
    bootstrap_or_reconcile_source,
    load_r0_runtime_config,
    parse_r0_runtime_config,
)

VALID_ENV = {
    "HUBINET_OPS_R0_PVE_TOKEN": "root@pam!hubinet-ops=00000000-0000-0000-0000-000000000000",
    "HUBINET_OPS_R0_API_TOKEN": "a" * 32,
}


def _raw(**overrides):
    base = {
        "source": {
            "display_name": "Home Proxmox",
            "provider_kind": "proxmox_ve",
            "pve_endpoint": "https://pve.example.internal:8006",
            "freshness_duration_seconds": 300,
            "credential_reference": "secret://pve-token-v1",
            "pve_token_env": "HUBINET_OPS_R0_PVE_TOKEN",
            "tls": {"verify": True, "ca_bundle_path": None},
        },
        "runtime": {
            "authority_db_path": "/var/lib/hubinet-ops/authority.db",
            "api_token_env": "HUBINET_OPS_R0_API_TOKEN",
        },
    }
    for key, value in overrides.items():
        section, field = key.split("__", 1)
        base[section][field] = value
    return base


def _store(tmp_path: Path) -> InventoryAuthorityStore:
    return InventoryAuthorityStore(tmp_path / "authority.db")


# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------


def test_parse_valid_config_produces_frozen_config_with_secrets_from_env() -> None:
    config = parse_r0_runtime_config(_raw(), env=VALID_ENV)
    assert config.source.display_name == "Home Proxmox"
    assert config.source.provider_kind == PROVIDER_KIND_PROXMOX_VE
    assert config.source.transport_locator == "https://pve.example.internal:8006"
    assert config.source.freshness_duration_seconds == 300
    assert config.source.credential_reference == "secret://pve-token-v1"
    assert config.source.tls.verify is True
    assert config.authority_db_path == Path("/var/lib/hubinet-ops/authority.db")
    assert config.pve_api_token == VALID_ENV["HUBINET_OPS_R0_PVE_TOKEN"]
    assert config.api_bearer_token == VALID_ENV["HUBINET_OPS_R0_API_TOKEN"]


def test_config_repr_never_leaks_secrets() -> None:
    config = parse_r0_runtime_config(_raw(), env=VALID_ENV)
    rendered = repr(config)
    assert VALID_ENV["HUBINET_OPS_R0_PVE_TOKEN"] not in rendered
    assert VALID_ENV["HUBINET_OPS_R0_API_TOKEN"] not in rendered
    assert "redacted" in rendered


@pytest.mark.parametrize(
    "override",
    (
        {"source__provider_kind": "other"},
        {"source__provider_kind": ""},
        {"source__display_name": ""},
        {"source__pve_endpoint": "http://insecure.example"},
        {"source__pve_endpoint": ""},
        {"source__freshness_duration_seconds": 0},
        {"source__freshness_duration_seconds": -1},
        {"source__freshness_duration_seconds": "300"},
        {"source__credential_reference": ""},
        {"source__pve_token_env": ""},
    ),
)
def test_malformed_source_field_fails_closed(override) -> None:
    with pytest.raises(R0ConfigError):
        parse_r0_runtime_config(_raw(**override), env=VALID_ENV)


@pytest.mark.parametrize(
    "override",
    (
        {"runtime__authority_db_path": ""},
        {"runtime__api_token_env": ""},
    ),
)
def test_malformed_runtime_field_fails_closed(override) -> None:
    with pytest.raises(R0ConfigError):
        parse_r0_runtime_config(_raw(**override), env=VALID_ENV)


def test_missing_pve_token_secret_fails_closed() -> None:
    env = {"HUBINET_OPS_R0_API_TOKEN": VALID_ENV["HUBINET_OPS_R0_API_TOKEN"]}
    with pytest.raises(R0ConfigError, match="HUBINET_OPS_R0_PVE_TOKEN"):
        parse_r0_runtime_config(_raw(), env=env)


def test_empty_pve_token_secret_fails_closed() -> None:
    env = {**VALID_ENV, "HUBINET_OPS_R0_PVE_TOKEN": "   "}
    with pytest.raises(R0ConfigError, match="HUBINET_OPS_R0_PVE_TOKEN"):
        parse_r0_runtime_config(_raw(), env=env)


def test_missing_api_bearer_token_secret_fails_closed() -> None:
    env = {"HUBINET_OPS_R0_PVE_TOKEN": VALID_ENV["HUBINET_OPS_R0_PVE_TOKEN"]}
    with pytest.raises(R0ConfigError, match="HUBINET_OPS_R0_API_TOKEN"):
        parse_r0_runtime_config(_raw(), env=env)


def test_short_api_bearer_token_fails_closed() -> None:
    env = {**VALID_ENV, "HUBINET_OPS_R0_API_TOKEN": "short"}
    with pytest.raises(R0ConfigError, match="minimum recommended length"):
        parse_r0_runtime_config(_raw(), env=env)


def test_non_mapping_top_level_fails_closed() -> None:
    with pytest.raises(R0ConfigError):
        parse_r0_runtime_config({"source": "not-a-mapping", "runtime": {}}, env=VALID_ENV)


def test_load_from_disk_missing_file_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(R0ConfigError):
        load_r0_runtime_config(tmp_path / "does-not-exist.yaml")


def test_load_from_disk_malformed_yaml_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "inventory.yaml"
    path.write_text("source: [unterminated", encoding="utf-8")
    with pytest.raises(R0ConfigError):
        load_r0_runtime_config(path)


def test_load_from_disk_roundtrip(tmp_path: Path) -> None:
    import yaml

    path = tmp_path / "inventory.yaml"
    path.write_text(yaml.safe_dump(_raw()), encoding="utf-8")
    config = load_r0_runtime_config(path, env=VALID_ENV)
    assert config.source.display_name == "Home Proxmox"


# ---------------------------------------------------------------------------
# §28 test #5 -- empty-DB initial source bootstrap
# ---------------------------------------------------------------------------


def test_5_empty_db_bootstraps_exactly_one_source(tmp_path: Path) -> None:
    store = _store(tmp_path)
    authority = InventoryAuthority(store)
    config = parse_r0_runtime_config(_raw(), env=VALID_ENV)

    assert store.list_source_states() == ()
    state = bootstrap_or_reconcile_source(authority, store, config)

    assert state.source.display_name == "Home Proxmox"
    assert state.source.provider_kind == PROVIDER_KIND_PROXMOX_VE
    assert state.source.credential_reference == "secret://pve-token-v1"
    assert state.source.freshness_duration_seconds == 300
    assert state.active_endpoint.canonical_transport_locator == (
        "https://pve.example.internal:8006"
    )
    assert len(store.list_source_states()) == 1

    # Idempotent guard: bootstrapping again from an already-populated DB
    # must never create a second source.
    second = bootstrap_or_reconcile_source(authority, store, config)
    assert second.source.inventory_source_id == state.source.inventory_source_id
    assert len(store.list_source_states()) == 1


# ---------------------------------------------------------------------------
# §28 test #6 -- restart with matching exact source context (no-op path)
# ---------------------------------------------------------------------------


def test_6_restart_with_matching_context_is_a_no_op(tmp_path: Path) -> None:
    store = _store(tmp_path)
    authority = InventoryAuthority(store)
    config = parse_r0_runtime_config(_raw(), env=VALID_ENV)
    first = bootstrap_or_reconcile_source(authority, store, config)

    second = bootstrap_or_reconcile_source(authority, store, config)

    assert second.source.inventory_source_id == first.source.inventory_source_id
    assert second.source.source_config_revision == first.source.source_config_revision
    assert (
        second.active_endpoint.canonical_transport_locator
        == first.active_endpoint.canonical_transport_locator
    )
    assert second.active_endpoint.transport_trust_revision == (
        first.active_endpoint.transport_trust_revision
    )


# ---------------------------------------------------------------------------
# §28 test #7 -- restart with endpoint config drift fails closed
# ---------------------------------------------------------------------------


def test_7_restart_with_endpoint_drift_fails_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    authority = InventoryAuthority(store)
    config = parse_r0_runtime_config(_raw(), env=VALID_ENV)
    bootstrap_or_reconcile_source(authority, store, config)

    drifted = parse_r0_runtime_config(
        _raw(**{"source__pve_endpoint": "https://different-pve.example.internal:8006"}),
        env=VALID_ENV,
    )
    with pytest.raises(R0ConfigDriftError, match="pve_endpoint"):
        bootstrap_or_reconcile_source(authority, store, drifted)

    # Fail-closed must not have created a second source or endpoint.
    assert len(store.list_source_states()) == 1


def test_7_restart_with_provider_kind_or_display_name_drift_fails_closed(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    authority = InventoryAuthority(store)
    config = parse_r0_runtime_config(_raw(), env=VALID_ENV)
    bootstrap_or_reconcile_source(authority, store, config)

    renamed = parse_r0_runtime_config(
        _raw(**{"source__display_name": "Renamed Proxmox"}), env=VALID_ENV
    )
    with pytest.raises(R0ConfigDriftError, match="display_name"):
        bootstrap_or_reconcile_source(authority, store, renamed)


def test_7_second_durable_source_is_out_of_scope_and_fails_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    authority = InventoryAuthority(store)
    config = parse_r0_runtime_config(_raw(), env=VALID_ENV)
    bootstrap_or_reconcile_source(authority, store, config)
    # Directly create a second source through the authority to simulate an
    # out-of-band multi-source database; R0-B's single-source bootstrap must
    # refuse to guess which source the configuration describes.
    authority.create_inventory_source(
        provider_kind=PROVIDER_KIND_PROXMOX_VE,
        display_name="Second Source",
        credential_reference="secret://ref",
        transport_locator="https://pve2.example.internal:8006",
        freshness_duration_seconds=300,
    )
    with pytest.raises(R0ConfigDriftError, match="multi-source"):
        bootstrap_or_reconcile_source(authority, store, config)


# ---------------------------------------------------------------------------
# §28 test #8 -- credential-version drift behavior
# ---------------------------------------------------------------------------


def test_8_credential_reference_drift_rotates_automatically(tmp_path: Path) -> None:
    store = _store(tmp_path)
    authority = InventoryAuthority(store)
    config = parse_r0_runtime_config(_raw(), env=VALID_ENV)
    first = bootstrap_or_reconcile_source(authority, store, config)

    rotated_env = {**VALID_ENV, "HUBINET_OPS_R0_PVE_TOKEN": "root@pam!rotated=new-secret"}
    rotated_config = parse_r0_runtime_config(
        _raw(**{"source__credential_reference": "secret://pve-token-v2"}), env=rotated_env
    )
    second = bootstrap_or_reconcile_source(authority, store, rotated_config)

    assert second.source.credential_reference == "secret://pve-token-v2"
    assert second.source.source_config_revision > first.source.source_config_revision
    # Endpoint/locator identity must be completely untouched by rotation.
    assert (
        second.active_endpoint.canonical_transport_locator
        == first.active_endpoint.canonical_transport_locator
    )


def test_8_freshness_duration_drift_applies_controlled_transition(tmp_path: Path) -> None:
    store = _store(tmp_path)
    authority = InventoryAuthority(store)
    config = parse_r0_runtime_config(_raw(), env=VALID_ENV)
    first = bootstrap_or_reconcile_source(authority, store, config)

    changed_config = parse_r0_runtime_config(
        _raw(**{"source__freshness_duration_seconds": 600}), env=VALID_ENV
    )
    second = bootstrap_or_reconcile_source(authority, store, changed_config)

    assert second.source.freshness_duration_seconds == 600
    assert second.source.source_config_revision > first.source.source_config_revision


def test_8_credential_drift_is_rejected_while_a_run_is_active(tmp_path: Path) -> None:
    store = _store(tmp_path)
    authority = InventoryAuthority(store)
    config = parse_r0_runtime_config(_raw(), env=VALID_ENV)
    state = bootstrap_or_reconcile_source(authority, store, config)
    authority.issue_discovery_run(state.source.inventory_source_id, 1)

    rotated_config = parse_r0_runtime_config(
        _raw(**{"source__credential_reference": "secret://pve-token-v2"}), env=VALID_ENV
    )
    from app.inventory import AuthorityConflict

    with pytest.raises(AuthorityConflict):
        bootstrap_or_reconcile_source(authority, store, rotated_config)
