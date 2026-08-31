"""Runtime/adversarial architecture regression coverage.

Adversarial regression checks proving the runtime cannot silently regress
into legacy, mutation, or static-inventory behavior. See ARCHITECTURE.md.
"""

from __future__ import annotations

import ast
from pathlib import Path

import httpx
import pytest

from app.inventory import InventoryAuthority, InventoryAuthorityStore, InventoryPublication
from app.inventory_pve_transport import ProxmoxHttpTransport, PveTransportError, _PVE_API_PREFIX
import app.inventory_scheduler as sched
from app.inventory_runtime_config import bootstrap_or_reconcile_source, parse_r0_runtime_config

REPO_ROOT = Path(__file__).resolve().parents[1]

VALID_ENV = {
    "HUBINET_OPS_R0_PVE_TOKEN": "root@pam!hubinet-ops=00000000-0000-0000-0000-000000000000",
    "HUBINET_OPS_R0_API_TOKEN": "a" * 32,
}

_R0_PRODUCTION_MODULES = (
    "app/inventory_runtime.py",
    "app/inventory_scheduler.py",
    "app/inventory_pve_transport.py",
    "app/inventory_runtime_config.py",
    "custom_components/hubinet_ops/transport_http.py",
)


def _raw():
    return {
        "source": {
            "display_name": "Home Proxmox",
            "provider_kind": "proxmox_ve",
            "pve_endpoint": "https://pve.example.internal:8006",
            "freshness_duration_seconds": 300,
            "credential_reference": "secret://v1",
            "pve_token_env": "HUBINET_OPS_R0_PVE_TOKEN",
            "tls": {"verify": True, "ca_bundle_path": None},
        },
        "runtime": {
            "authority_db_path": "/var/lib/hubinet-ops/authority.db",
            "api_token_env": "HUBINET_OPS_R0_API_TOKEN",
        },
    }


def _config():
    return parse_r0_runtime_config(_raw(), env=VALID_ENV)


def _bootstrap(tmp_path: Path):
    store = InventoryAuthorityStore(tmp_path / "authority.db")
    authority = InventoryAuthority(store)
    config = _config()
    state = bootstrap_or_reconcile_source(authority, store, config)
    return store, authority, config, state.source.inventory_source_id


def _healthy_handler(*, guests=()):
    def handler(request: httpx.Request) -> httpx.Response:
        rel = request.url.path[len(_PVE_API_PREFIX):]
        if rel == "/version":
            return httpx.Response(200, json={"data": {"release": "9.0"}})
        if rel == "/access/acl":
            return httpx.Response(200, json={"data": []})
        if rel == "/cluster/status":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"type": "node", "id": "node/pve-a", "name": "pve-a", "nodeid": 0, "local": 1, "online": 1}
                    ]
                },
            )
        if rel == "/access/permissions":
            path = dict(request.url.params).get("path")
            permissions = {
                "/": {"Sys.Audit": 1},
                "/access": {"Sys.Audit": 1},
                "/nodes": {"Sys.Audit": 1},
                "/vms": {"VM.Audit": 1},
                "/nodes/pve-a": {"Sys.Audit": 1},
            }
            privileges = permissions.get(path, {})
            return httpx.Response(200, json={"data": {path: privileges} if privileges else {}})
        if rel == "/nodes":
            return httpx.Response(200, json={"data": [{"node": "pve-a", "status": "online"}]})
        if rel == "/nodes/pve-a/qemu":
            return httpx.Response(200, json={"data": [g for g in guests if g.get("type") == "qemu"]})
        if rel == "/nodes/pve-a/lxc":
            return httpx.Response(200, json={"data": [g for g in guests if g.get("type") == "lxc"]})
        raise AssertionError(f"unexpected PVE request path {rel!r}")

    return handler


def _down_handler(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused", request=request)


def _patch_transport(monkeypatch, handler) -> None:
    def fake_build_transport(run, cfg):
        return ProxmoxHttpTransport(
            canonical_transport_locator=run.expected_canonical_transport_locator,
            pve_api_token=cfg.pve_api_token,
            _transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr(sched, "_build_transport", fake_build_transport)


# ---------------------------------------------------------------------------
# test #34 -- source unavailable retains inventory correctly, end-to-end
# through the real transport against a simulated-down PVE fixture
# ---------------------------------------------------------------------------


def test_34_source_unavailable_retains_prior_inventory_end_to_end(
    tmp_path: Path, monkeypatch
) -> None:
    store, authority, config, source_id = _bootstrap(tmp_path)

    _patch_transport(
        monkeypatch,
        _healthy_handler(guests=({"vmid": 100, "type": "qemu", "name": "vm1", "status": "running"},)),
    )
    first = sched.run_discovery_cycle(authority, source_id, config)
    assert first.status == "success"

    before = InventoryPublication(store, authority).read()
    assert len(before.resources) == 1
    assert before.sources[0]["health"] == "healthy"
    assert before.sources[0]["freshness"] == "fresh"

    _patch_transport(monkeypatch, _down_handler)
    second = sched.run_discovery_cycle(authority, source_id, config)
    assert second.status == "failed"

    after = InventoryPublication(store, authority).read()
    assert after.sources[0]["health"] == "source_unavailable"
    assert after.sources[0]["freshness"] == "stale"
    # Prior inventory (identity, presence, everything) must be completely
    # untouched by a source-unavailable failure -- no reconciliation runs
    # at all for a failed discovery run.
    assert after.resources == before.resources
    assert after.inventory_revision == before.inventory_revision


# ---------------------------------------------------------------------------
# test #39 -- no real Proxmox/private network in CI
# ---------------------------------------------------------------------------


_TRANSPORT_CONSUMING_TEST_FILES = (
    ("tests/test_inventory_pve_transport.py", "ProxmoxHttpTransport", "MockTransport"),
    ("tests/test_inventory_scheduler.py", "ProxmoxHttpTransport", "MockTransport"),
    ("tests/test_inventory_runtime.py", "ProxmoxHttpTransport", "MockTransport"),
    ("tests/test_deploy_0_5_fresh_install.py", None, None),
    ("tests/test_hubinet_ops_transport_http.py", "HttpHubinetOpsTransport", "aioclient_mock"),
    ("tests/test_r0_architecture_regression.py", "ProxmoxHttpTransport", "MockTransport"),
)


def test_39_every_r0b_test_file_using_a_real_transport_class_also_mocks_it() -> None:
    for rel_path, transport_symbol, mock_symbol in _TRANSPORT_CONSUMING_TEST_FILES:
        text = (REPO_ROOT / rel_path).read_text(encoding="utf-8")
        if transport_symbol is None:
            continue
        if transport_symbol in text:
            assert mock_symbol in text, (
                f"{rel_path} uses {transport_symbol} but never references "
                f"{mock_symbol} -- possible real network access"
            )


def test_39_production_pve_transport_never_hardcodes_a_real_reachable_host() -> None:
    text = (REPO_ROOT / "app/inventory_pve_transport.py").read_text(encoding="utf-8")
    # The production adapter must take its endpoint entirely from the
    # caller-supplied canonical_transport_locator -- the only "https://"
    # literal in the whole module is the scheme-prefix validation check,
    # never a hardcoded hostname/IP of its own.
    occurrences = text.count("https://")
    assert occurrences == 1, f"expected exactly one https:// literal (the scheme check), found {occurrences}"


# ---------------------------------------------------------------------------
# Adversarial regression checks
# ---------------------------------------------------------------------------

_FORBIDDEN_SYMBOLS_AS_IMPORTS = (
    "app.main",
    "app.service",
    "app.executor",
    "app.resource_adapters",
    "app.host_control",
    "app.mqtt",
    "app.mqtt_budget",
    "app.database",
    "app.stabilization",
    "app.ha_entities",
    "app.contracts",
)


def test_r0_production_modules_import_no_forbidden_legacy_symbol() -> None:
    for rel_path in _R0_PRODUCTION_MODULES:
        source = (REPO_ROOT / rel_path).read_text(encoding="utf-8")
        tree = ast.parse(source, filename=rel_path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                assert not any(
                    name == forbidden or name.startswith(forbidden + ".")
                    for forbidden in _FORBIDDEN_SYMBOLS_AS_IMPORTS
                ), (rel_path, name)


def test_r0_production_modules_define_only_exact_plan_approval_mutation() -> None:
    text = (REPO_ROOT / "app/inventory_runtime.py").read_text(encoding="utf-8")
    for verb in ("@app.post(", "@app.patch(", "@app.delete("):
        assert verb not in text
    assert text.count("@app.put(") == 1
    assert (
        'f"{API_PREFIX}/resources/{{resource_id}}/package-plan-approval"'
        in text
    )


def test_next_a_job_authority_has_recovery_but_no_production_issuance_surface() -> None:
    runtime = (REPO_ROOT / "app/inventory_runtime.py").read_text(encoding="utf-8")
    assert "authority.issue_package_update_job(" not in runtime
    assert runtime.count("authority.recover_interrupted_package_update_jobs()") == 1
    assert runtime.index("recover_interrupted_package_update_jobs") < runtime.index(
        "scheduler: R0Scheduler = bootstrap_and_start_r0_runtime("
    )

    for rel_path in (
        "app/inventory_scheduler.py",
        "app/package_scan_scheduler.py",
        "custom_components/hubinet_ops/services.py",
        "custom_components/hubinet_ops/transport_http.py",
    ):
        text = (REPO_ROOT / rel_path).read_text(encoding="utf-8")
        assert "issue_package_update_job" not in text, rel_path
        assert "start_package_update" not in text, rel_path
        assert "execute_package_update" not in text, rel_path


def test_next_a_keeps_forced_helper_scan_only_and_adds_no_mutation_operation() -> None:
    helper = (REPO_ROOT / "deploy/hubinet-package-scan-helper.py").read_text(
        encoding="utf-8"
    )
    assert 'payload["operation"] != "scan_packages"' in helper
    for forbidden in (
        "execute_packages",
        "install_packages",
        "pct snapshot",
        "pct rollback",
        '"apt-get", "install"',
        '"apt-get", "upgrade"',
        '"apt-get", "dist-upgrade"',
    ):
        assert forbidden not in helper


def test_r0_config_loader_has_no_static_workload_inventory_concept() -> None:
    # AST-exact check (not a substring scan, which would false-positive on
    # this module's own negative-documentation prose, e.g. "no configured
    # list of VMIDs"): no string-literal dict key anywhere in the code
    # equals one of the forbidden static-inventory-shaped names.
    source = (REPO_ROOT / "app/inventory_runtime_config.py").read_text(encoding="utf-8")
    tree = ast.parse(source, filename="app/inventory_runtime_config.py")
    forbidden = {"resources", "containers", "configured_vmids", "vmid", "vmids"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert node.value not in forbidden, node.value


def test_r0_config_loader_silently_ignores_an_extraneous_static_resource_section() -> None:
    # Defense in depth: even if an operator pastes a legacy-shaped
    # `resources:`/`containers:` block into the R0 YAML file, the R0
    # source-bootstrap contract has no field for it and must not use it
    # for anything.
    from app.inventory_runtime_config import parse_r0_runtime_config

    raw = _raw()
    raw["resources"] = {100: {"resource_type": "qemu"}}
    raw["containers"] = {101: {"resource_type": "lxc"}}
    config = parse_r0_runtime_config(raw, env=VALID_ENV)
    assert not hasattr(config.source, "resources")
    assert not hasattr(config.source, "containers")
    assert not hasattr(config, "resources")


def test_r0_ha_transport_never_references_pve_or_imports_app_inventory() -> None:
    text = (REPO_ROOT / "custom_components/hubinet_ops/transport_http.py").read_text(
        encoding="utf-8"
    )
    # Module docstring/negative-documentation prose may legitimately say
    # "never connects directly to Proxmox" -- what must never appear is an
    # actual PVE-shaped dependency: the PVE auth header format, PVE's
    # default port, or any import from the backend-only app.inventory*
    # subsystem (a different process/host boundary entirely).
    assert "PVEAPIToken" not in text
    assert "8006" not in text
    assert "app.inventory" not in text


def test_r0_ha_transport_defines_only_exact_plan_approval_write() -> None:
    tree = ast.parse(
        (REPO_ROOT / "custom_components/hubinet_ops/transport_http.py").read_text(
            encoding="utf-8"
        )
    )
    protocol_methods = {
        "validate_connection",
        "fetch_backend_information",
        "fetch_resource_snapshot",
        "approve_package_plan",
    }
    exact_private_methods = {"_get", "_put"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "HttpHubinetOpsTransport":
            method_names = {
                item.name for item in node.body if isinstance(item, ast.AsyncFunctionDef)
            }
            assert method_names == protocol_methods | exact_private_methods
            return
    raise AssertionError("HttpHubinetOpsTransport class not found")


def test_r0_bearer_token_never_appears_in_server_logs(tmp_path: Path, caplog) -> None:
    import logging

    from fastapi.testclient import TestClient

    from app.inventory_runtime import create_read_only_app

    config = parse_r0_runtime_config(
        {**_raw(), "runtime": {**_raw()["runtime"], "authority_db_path": str(tmp_path / "authority.db")}},
        env=VALID_ENV,
    )
    app = create_read_only_app(config, start_scheduler=False)
    client = TestClient(app)

    with caplog.at_level(logging.DEBUG):
        client.get("/r0/v1/backend", headers={"Authorization": "Bearer wrong-token-value"})
        client.get(
            "/r0/v1/backend",
            headers={"Authorization": f"Bearer {config.api_bearer_token}"},
        )

    rendered = "\n".join(record.getMessage() for record in caplog.records)
    assert "wrong-token-value" not in rendered
    assert config.api_bearer_token not in rendered
    assert config.pve_api_token not in rendered
