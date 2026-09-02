"""R0 composition root and read-only HTTP API.

Covers tests #1, #2, #3, #4, #27, #28, #29 (backend-side half), #30
(P3 cleanup: explicitly assigned here), #32, #35, #40 (runtime/composition
portion) of the R0 runtime contract in ARCHITECTURE.md.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path
import sqlite3
import subprocess
import sys

import httpx
import pytest
from fastapi.testclient import TestClient

from app.inventory import AuthorityDatabaseRejected, PackageScanPackage
from app.inventory_pve_transport import ProxmoxHttpTransport, _PVE_API_PREFIX
from app.inventory_runtime import create_app_from_env, create_read_only_app
from app.inventory_runtime_config import R0ConfigError, parse_r0_runtime_config
import app.inventory_scheduler as sched

REPO_ROOT = Path(__file__).resolve().parents[1]

VALID_ENV = {
    "HUBINET_OPS_R0_PVE_TOKEN": "root@pam!hubinet-ops=00000000-0000-0000-0000-000000000000",
    "HUBINET_OPS_R0_API_TOKEN": "a" * 32,
}

_DENYLIST = (
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

_R0_MODULE_RELATIVE_PATHS = (
    "app/inventory_runtime.py",
    "app/inventory_scheduler.py",
    "app/inventory_pve_transport.py",
    "app/inventory_runtime_config.py",
)


def _is_denylisted(module_name: str) -> bool:
    return any(module_name == d or module_name.startswith(d + ".") for d in _DENYLIST)


def _raw(*, db_path: str) -> dict:
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
            "authority_db_path": db_path,
            "api_token_env": "HUBINET_OPS_R0_API_TOKEN",
        },
    }


def _config(db_path: Path):
    return parse_r0_runtime_config(_raw(db_path=str(db_path)), env=VALID_ENV)


def _build_app(tmp_path: Path, *, start_scheduler: bool = False, now=None):
    config = _config(tmp_path / "authority.db")
    return create_read_only_app(config, start_scheduler=start_scheduler, now=now), config


def _pve_handler(*, guests=()):
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


def _run_discovery(monkeypatch, config, authority, source_id: str, *, guests=(), now=None):
    def fake_build_transport(run, cfg):
        return ProxmoxHttpTransport(
            canonical_transport_locator=run.expected_canonical_transport_locator,
            pve_api_token=cfg.pve_api_token,
            _transport=httpx.MockTransport(_pve_handler(guests=guests)),
        )

    monkeypatch.setattr(sched, "_build_transport", fake_build_transport)
    kwargs = {"now": now} if now is not None else {}
    outcome = sched.run_discovery_cycle(authority, source_id, config, **kwargs)
    assert outcome.status == "success", outcome
    return outcome


# ---------------------------------------------------------------------------
# test #1 -- composition-root import-graph test
# ---------------------------------------------------------------------------


def test_1_composition_root_import_graph_excludes_legacy_modules() -> None:
    script = (
        "import sys\n"
        "import app.inventory_runtime\n"
        "print('|'.join(sorted(m for m in sys.modules)))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    modules = result.stdout.strip().split("|") if result.stdout.strip() else []
    leaked = [m for m in modules if _is_denylisted(m)]
    assert leaked == [], f"R0 composition root imported denylisted legacy modules: {leaked}"


def test_1_static_ast_scan_finds_no_denylisted_import_in_r0_modules() -> None:
    for rel_path in _R0_MODULE_RELATIVE_PATHS:
        source = (REPO_ROOT / rel_path).read_text(encoding="utf-8")
        tree = ast.parse(source, filename=rel_path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not _is_denylisted(alias.name), (rel_path, alias.name)
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                assert not _is_denylisted(module), (rel_path, module)


# ---------------------------------------------------------------------------
# test #2 -- FastAPI route enumeration
# ---------------------------------------------------------------------------


def test_2_only_reads_and_exact_authority_metadata_writes_exist(tmp_path: Path) -> None:
    """The R0 route table is an exact allowlist, not a shape.

    Every write this API exposes changes authority metadata and nothing else:
    the exact-plan approval, and the three per-resource health-contract
    operations. None of them can start a job, mutate a package, take or roll
    back a snapshot, or run a healthcheck -- and this test is what stops a
    later route from quietly becoming the first one that can.
    """

    app, _config = _build_app(tmp_path)
    observed: dict[str, set[str]] = {}
    for route in app.routes:
        methods = getattr(route, "methods", None)
        path = str(getattr(route, "path", ""))
        if methods is None or not path.startswith("/r0/v1"):
            continue
        observed.setdefault(path, set()).update(
            method for method in methods if method not in {"HEAD", "OPTIONS"}
        )

    assert observed == {
        "/r0/v1/health": {"GET"},
        "/r0/v1/backend": {"GET"},
        "/r0/v1/snapshot": {"GET"},
        "/r0/v1/resources/{resource_id}/package-plan-approval": {"PUT"},
        "/r0/v1/resources/{resource_id}/health-contract": {"GET", "PUT", "DELETE"},
    }
    assert app.docs_url is None
    assert app.redoc_url is None
    assert app.openapi_url is None


# ---------------------------------------------------------------------------
# test #3 -- app.main import/behavior unchanged (narrow regression guard)
# ---------------------------------------------------------------------------
#
# The legacy 0.2.x-0.4.x runtime (app/main.py, app/service.py, app/database.py,
# etc.) has been retired from the current tree as part of the 0.5-only
# repository cleanup; its historical source remains available through Git
# history/tags. The invariant this test protected -- "the runtime never edits the
# legacy composition root" -- is now vacuously true (the file no longer
# exists) and is superseded by the AST-based forbidden-legacy-import guard
# below (`test_r0_production_modules_import_no_forbidden_legacy_symbol` in
# tests/test_r0_architecture_regression.py and this file's own denylist
# checks), which does not require the legacy modules to exist to be
# meaningful.


# ---------------------------------------------------------------------------
# test #4 -- R0 DB separateness + rejection of legacy/incompatible DB
# ---------------------------------------------------------------------------


def test_4_startup_fails_closed_against_a_real_legacy_ops_db_fixture(tmp_path: Path) -> None:
    legacy_path = tmp_path / "authority.db"
    with sqlite3.connect(legacy_path) as connection:
        connection.executescript(
            """
            CREATE TABLE plans (id TEXT PRIMARY KEY);
            CREATE TABLE jobs (id TEXT PRIMARY KEY);
            PRAGMA user_version=400;
            """
        )
    before = legacy_path.read_bytes()

    config = _config(legacy_path)
    with pytest.raises(AuthorityDatabaseRejected, match="legacy Hubinet Ops 0.4"):
        create_read_only_app(config, start_scheduler=False)

    assert legacy_path.read_bytes() == before


# ---------------------------------------------------------------------------
# test #27/#28 -- no path to trusted; effective capabilities always empty
# ---------------------------------------------------------------------------


def test_27_28_security_continuity_always_unverified_and_capabilities_always_empty(
    tmp_path: Path, monkeypatch
) -> None:
    app, config = _build_app(tmp_path)
    authority = app.state.authority
    source_id = app.state.store.list_source_states()[0].source.inventory_source_id
    _run_discovery(
        monkeypatch, config, authority, source_id,
        guests=({"vmid": 100, "type": "qemu", "name": "vm1", "status": "running"},),
    )

    client = TestClient(app)
    response = client.get(
        "/r0/v1/snapshot", headers={"Authorization": f"Bearer {config.api_bearer_token}"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["resources"], "expected at least one discovered resource"
    for resource in body["resources"]:
        assert resource["security_continuity"] == "unverified"
        assert resource["effective_capabilities"] == []
        assert resource["policy_applicable"] is False


# ---------------------------------------------------------------------------
# test #29 (backend-side half) -- publication -> HTTP field mapping
# ---------------------------------------------------------------------------


def test_29_backend_and_snapshot_http_shape_matches_publication_contract(
    tmp_path: Path, monkeypatch
) -> None:
    app, config = _build_app(tmp_path)
    authority = app.state.authority
    source_id = app.state.store.list_source_states()[0].source.inventory_source_id
    _run_discovery(
        monkeypatch, config, authority, source_id,
        guests=({"vmid": 100, "type": "qemu", "name": "vm1", "status": "running"},),
    )

    client = TestClient(app)
    headers = {"Authorization": f"Bearer {config.api_bearer_token}"}

    backend_body = client.get("/r0/v1/backend", headers=headers).json()
    assert set(backend_body) == {"backend_instance_id", "name", "version", "api_version"}
    import uuid

    uuid.UUID(backend_body["backend_instance_id"])  # must not raise

    snapshot_body = client.get("/r0/v1/snapshot", headers=headers).json()
    assert set(snapshot_body) == {
        "backend", "sources", "nodes", "resources",
        "inventory_revision", "published_state_revision", "published_at",
    }
    assert snapshot_body["backend"] == backend_body
    source = snapshot_body["sources"][0]
    for field in ("health", "freshness", "health_origin", "last_issued_run_sequence"):
        assert field in source
    resource = snapshot_body["resources"][0]
    for field in (
        "resource_id", "resource_type", "vmid", "presence", "lifecycle",
        "observational_continuity", "security_continuity", "detail_status",
        "node_availability", "state_level", "effective_capabilities",
    ):
        assert field in resource
    assert isinstance(resource["effective_capabilities"], list)
    assert isinstance(resource["locator_generation"], int)
    # No credential-shaped field anywhere in the published/HTTP contract.
    assert "credential_reference" not in source


# ---------------------------------------------------------------------------
# test #30 -- backend identity stable across restart (P3: assigned here)
# ---------------------------------------------------------------------------


def test_30_backend_identity_stable_across_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "authority.db"
    config = _config(db_path)

    first_app = create_read_only_app(config, start_scheduler=False)
    first_id = TestClient(first_app).get(
        "/r0/v1/backend", headers={"Authorization": f"Bearer {config.api_bearer_token}"}
    ).json()["backend_instance_id"]
    first_app.state.store.close()

    # Simulate a process restart: build an entirely new app/store/authority
    # bound to the same durable DB path.
    second_app = create_read_only_app(config, start_scheduler=False)
    second_id = TestClient(second_app).get(
        "/r0/v1/backend", headers={"Authorization": f"Bearer {config.api_bearer_token}"}
    ).json()["backend_instance_id"]

    assert first_id == second_id


# ---------------------------------------------------------------------------
# test #32 -- auth failures correctly mapped
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ("/r0/v1/backend", "/r0/v1/snapshot"))
def test_32_missing_wrong_and_correct_token(tmp_path: Path, path: str) -> None:
    app, config = _build_app(tmp_path)
    client = TestClient(app)

    assert client.get(path).status_code == 401
    assert client.get(path, headers={"Authorization": "Bearer wrong-token"}).status_code == 401
    assert client.get(path, headers={"Authorization": "not-even-bearer-shaped"}).status_code == 401
    ok = client.get(path, headers={"Authorization": f"Bearer {config.api_bearer_token}"})
    assert ok.status_code == 200


def test_32_health_is_unauthenticated_and_minimal(tmp_path: Path) -> None:
    app, _config = _build_app(tmp_path)
    client = TestClient(app)
    response = client.get("/r0/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def _discover_lxc_plan(app, config, monkeypatch):
    source_id = app.state.store.list_source_states()[0].source.inventory_source_id
    _run_discovery(
        monkeypatch,
        config,
        app.state.authority,
        source_id,
        guests=(
            {"vmid": 101, "type": "lxc", "name": "ct1", "status": "running"},
        ),
    )
    resource = app.state.store.list_resources(source_id)[0]
    run = app.state.authority.issue_package_scan(resource.resource_id)
    completed = app.state.authority.finalize_successful_package_scan(
        run.scan_run_id,
        os_id="debian",
        os_version="12",
        packages=(PackageScanPackage("apt", "amd64", "2.6.1", "2.6.2"),),
        reboot_required=None,
    )
    return resource, completed


def test_package_plan_approval_route_auth_validation_and_not_found(
    tmp_path: Path, monkeypatch
) -> None:
    app, config = _build_app(tmp_path)
    resource, completed = _discover_lxc_plan(app, config, monkeypatch)
    path = f"/r0/v1/resources/{resource.resource_id}/package-plan-approval"
    client = TestClient(app)
    exact = {
        "scan_run_id": completed.scan_run_id,
        "plan_fingerprint": completed.plan_fingerprint,
    }
    assert client.put(path, json=exact).status_code == 401
    headers = {"Authorization": f"Bearer {config.api_bearer_token}"}
    assert client.put(path, headers=headers, json={}).status_code == 422
    assert client.put(
        path, headers=headers, json={**exact, "unexpected": True}
    ).status_code == 422
    assert client.put(
        path, headers=headers, json={**exact, "scan_run_id": "not-a-uuid"}
    ).status_code == 422
    assert client.put(
        path, headers=headers, json={**exact, "plan_fingerprint": "not-a-hash"}
    ).status_code == 422
    assert client.put(
        "/r0/v1/resources/not-a-uuid/package-plan-approval",
        headers=headers,
        json=exact,
    ).status_code == 422

    unknown_resource = "11111111-1111-1111-1111-111111111111"
    assert client.put(
        f"/r0/v1/resources/{unknown_resource}/package-plan-approval",
        headers=headers,
        json=exact,
    ).status_code == 404
    unknown_scan = "22222222-2222-2222-2222-222222222222"
    assert client.put(
        path,
        headers=headers,
        json={**exact, "scan_run_id": unknown_scan},
    ).status_code == 404


def test_package_plan_approval_route_accepts_exact_reference_and_refuses_stale_race(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    app, config = _build_app(tmp_path)
    resource, plan_a = _discover_lxc_plan(app, config, monkeypatch)
    path = f"/r0/v1/resources/{resource.resource_id}/package-plan-approval"
    headers = {"Authorization": f"Bearer {config.api_bearer_token}"}
    client = TestClient(app)

    def forbidden_host_mutation(*args, **kwargs):
        raise AssertionError("approval route reached package host control")

    monkeypatch.setattr(
        app.state.package_scan_host_control,
        "scan_packages",
        forbidden_host_mutation,
    )

    response = client.put(
        path,
        headers=headers,
        json={
            "scan_run_id": plan_a.scan_run_id,
            "plan_fingerprint": plan_a.plan_fingerprint,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["resource_id"] == resource.resource_id
    assert body["reviewed_scan_run_id"] == plan_a.scan_run_id
    assert body["plan_fingerprint"] == plan_a.plan_fingerprint
    assert config.api_bearer_token not in response.text
    assert config.api_bearer_token not in caplog.text

    plan_b_run = app.state.authority.issue_package_scan(resource.resource_id)
    app.state.authority.finalize_successful_package_scan(
        plan_b_run.scan_run_id,
        os_id="debian",
        os_version="12",
        packages=(PackageScanPackage("apt", "amd64", "2.6.1", "2.6.9"),),
        reboot_required=None,
    )
    stale = client.put(
        path,
        headers=headers,
        json={
            "scan_run_id": plan_a.scan_run_id,
            "plan_fingerprint": plan_a.plan_fingerprint,
        },
    )
    assert stale.status_code == 409


# ---------------------------------------------------------------------------
# test #35 -- freshness expiry remains backend-owned
# ---------------------------------------------------------------------------


def test_35_freshness_expiry_is_materialized_by_the_backend_on_get(
    tmp_path: Path, monkeypatch
) -> None:
    current = {"value": datetime(2026, 1, 1, tzinfo=UTC)}

    def moving_now() -> datetime:
        return current["value"]

    app, config = _build_app(tmp_path, now=moving_now)
    authority = app.state.authority
    source_id = app.state.store.list_source_states()[0].source.inventory_source_id
    _run_discovery(monkeypatch, config, authority, source_id, now=moving_now)

    client = TestClient(app)
    headers = {"Authorization": f"Bearer {config.api_bearer_token}"}
    fresh_body = client.get("/r0/v1/snapshot", headers=headers).json()
    assert fresh_body["sources"][0]["freshness"] == "fresh"

    # Advance well past freshness_duration_seconds (300s) without a new run.
    current["value"] = current["value"] + timedelta(seconds=3600)
    stale_body = client.get("/r0/v1/snapshot", headers=headers).json()
    assert stale_body["sources"][0]["freshness"] == "stale"
    assert stale_body["sources"][0]["health_origin"] == "time_expiry"
    # Identity/inventory facts are untouched by a pure expiry transition.
    assert stale_body["backend"]["backend_instance_id"] == fresh_body["backend"]["backend_instance_id"]


# ---------------------------------------------------------------------------
# test #40 (runtime/composition portion) -- never touches an unrelated DB
# ---------------------------------------------------------------------------


def test_create_app_from_env_has_no_import_time_side_effect_and_reads_env_at_call(
    tmp_path: Path, monkeypatch
) -> None:
    # Importing app.inventory_runtime must never itself construct an app
    # (test #1's own premise); create_app_from_env only reads its
    # configured path when actually called.
    monkeypatch.setenv("HUBINET_OPS_R0_CONFIG", str(tmp_path / "does-not-exist.yaml"))
    with pytest.raises(R0ConfigError):
        create_app_from_env()

    config_path = tmp_path / "inventory.yaml"
    import yaml

    config_path.write_text(
        yaml.safe_dump(_raw(db_path=str(tmp_path / "authority.db"))), encoding="utf-8"
    )
    monkeypatch.setenv("HUBINET_OPS_R0_CONFIG", str(config_path))
    monkeypatch.setenv("HUBINET_OPS_R0_PVE_TOKEN", VALID_ENV["HUBINET_OPS_R0_PVE_TOKEN"])
    monkeypatch.setenv("HUBINET_OPS_R0_API_TOKEN", VALID_ENV["HUBINET_OPS_R0_API_TOKEN"])

    # Intercept the actual app construction so this test never starts a
    # real scheduler thread or attempts real network I/O against the
    # configured (unreachable, fictitious) PVE endpoint -- it only proves
    # create_app_from_env reads the env-configured path/secrets correctly
    # and hands off to create_read_only_app.
    import app.inventory_runtime as runtime_module

    captured: dict[str, object] = {}

    def fake_create_read_only_app(config, **kwargs):
        captured["config"] = config
        captured["kwargs"] = kwargs
        return "sentinel-app"

    monkeypatch.setattr(runtime_module, "create_read_only_app", fake_create_read_only_app)

    result = create_app_from_env()

    assert result == "sentinel-app"
    assert captured["config"].source.display_name == "Home Proxmox"


def test_40_composition_root_never_touches_an_unrelated_legacy_db_on_disk(
    tmp_path: Path,
) -> None:
    legacy_elsewhere = tmp_path / "ops.db"
    with sqlite3.connect(legacy_elsewhere) as connection:
        connection.executescript(
            """
            CREATE TABLE plans (id TEXT PRIMARY KEY);
            CREATE TABLE jobs (id TEXT PRIMARY KEY);
            PRAGMA user_version=400;
            """
        )
    before = legacy_elsewhere.read_bytes()

    fresh_db_path = tmp_path / "authority.db"
    app = create_read_only_app(_config(fresh_db_path), start_scheduler=False)
    app.state.store.close()

    assert legacy_elsewhere.read_bytes() == before
    assert fresh_db_path.exists()


# ---------------------------------------------------------------------------
# Per-resource health contracts over HTTP.
#
# Authority metadata only: these three routes read and write what "healthy"
# would mean for one exact resource incarnation. Nothing they do can start a
# job, mutate a package, or run a probe -- there is no healthcheck executor to
# reach.
# ---------------------------------------------------------------------------

_HEALTH_PROBES = [
    {"kind": "systemd_unit_active", "target": "nginx.service"},
    {"kind": "docker_container_healthy", "target": "immich_server"},
]


def _health_contract_path(resource_id: str) -> str:
    return f"/r0/v1/resources/{resource_id}/health-contract"


def _discover_lxc_resource(app, config, monkeypatch):
    source_id = app.state.store.list_source_states()[0].source.inventory_source_id
    _run_discovery(
        monkeypatch,
        config,
        app.state.authority,
        source_id,
        guests=({"vmid": 101, "type": "lxc", "name": "ct1", "status": "running"},),
    )
    return app.state.store.list_resources(source_id)[0]


def test_health_contract_routes_require_bearer_authentication(
    tmp_path: Path, monkeypatch
) -> None:
    app, config = _build_app(tmp_path)
    resource = _discover_lxc_resource(app, config, monkeypatch)
    path = _health_contract_path(resource.resource_id)
    client = TestClient(app)

    assert client.get(path).status_code == 401
    assert client.put(path, json={"probes": _HEALTH_PROBES}).status_code == 401
    assert client.delete(path).status_code == 401
    assert client.get(path, headers={"Authorization": "Bearer wrong"}).status_code == 401
    # Nothing was written by any unauthenticated attempt.
    assert app.state.store.resource_health_contract(resource.resource_id) is None


def test_health_contract_get_distinguishes_unconfigured_from_unknown_resource(
    tmp_path: Path, monkeypatch
) -> None:
    """An absent contract is never a 200 with an empty probe list."""

    app, config = _build_app(tmp_path)
    resource = _discover_lxc_resource(app, config, monkeypatch)
    headers = {"Authorization": f"Bearer {config.api_bearer_token}"}
    client = TestClient(app)

    unconfigured = client.get(_health_contract_path(resource.resource_id), headers=headers)
    assert unconfigured.status_code == 404
    assert unconfigured.json()["detail"]["error"] == "contract_unconfigured"

    unknown = client.get(
        _health_contract_path("11111111-1111-1111-1111-111111111111"),
        headers=headers,
    )
    assert unknown.status_code == 404
    assert unknown.json()["detail"]["error"] == "resource_not_found"

    assert client.get("/r0/v1/resources/not-a-uuid/health-contract", headers=headers).status_code == 422


def test_health_contract_put_then_get_returns_the_exact_canonical_contract(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    app, config = _build_app(tmp_path)
    resource = _discover_lxc_resource(app, config, monkeypatch)
    path = _health_contract_path(resource.resource_id)
    headers = {"Authorization": f"Bearer {config.api_bearer_token}"}
    client = TestClient(app)

    def forbidden_host_call(*args, **kwargs):
        raise AssertionError("health contract route reached package host control")

    monkeypatch.setattr(
        app.state.package_scan_host_control, "scan_packages", forbidden_host_call
    )

    created = client.put(
        path, headers=headers, json={"probes": list(reversed(_HEALTH_PROBES))}
    )
    assert created.status_code == 200
    body = created.json()
    assert body["resource_id"] == resource.resource_id
    assert body["status"] == "configured"
    assert body["revision"] == 1
    # Canonical order, independent of how the operator listed them.
    assert body["probes"] == [
        {"kind": "docker_container_healthy", "target": "immich_server"},
        {"kind": "systemd_unit_active", "target": "nginx.service"},
    ]
    assert client.get(path, headers=headers).json() == body

    # Same material again is not a change, so the revision does not move.
    assert client.put(path, headers=headers, json={"probes": _HEALTH_PROBES}).json() == body
    replaced = client.put(
        path,
        headers=headers,
        json={"probes": [{"kind": "docker_container_running", "target": "redis"}]},
    ).json()
    assert replaced["revision"] == 2
    assert replaced["fingerprint"] != body["fingerprint"]
    assert replaced["created_at"] == body["created_at"]

    # The published snapshot carries the summary, never the probe list.
    published = client.get("/r0/v1/snapshot", headers=headers).json()["resources"][0]
    assert published["health_contract"] == {
        "status": "configured",
        "revision": 2,
        "fingerprint": replaced["fingerprint"],
        "probe_count": 1,
        "updated_at": replaced["updated_at"],
    }
    assert config.api_bearer_token not in created.text
    assert config.api_bearer_token not in caplog.text


@pytest.mark.parametrize(
    "body",
    (
        {},
        {"probes": []},
        {"probes": _HEALTH_PROBES, "unexpected": True},
        {"probes": [{"kind": "http_get", "target": "https://example"}]},
        {"probes": [{"kind": "systemd_unit_active"}]},
        {"probes": [{"kind": "systemd_unit_active", "target": ""}]},
        {"probes": [{"kind": "systemd_unit_active", "target": "a b.service"}]},
        {"probes": [{"kind": "systemd_unit_active", "target": "x" * 201}]},
        # No command-shaped field may ever be accepted alongside a probe.
        {
            "probes": [
                {
                    "kind": "systemd_unit_active",
                    "target": "nginx.service",
                    "command": "rm -rf /",
                }
            ]
        },
        # Duplicate (kind, target): an all-of contract stating the same
        # requirement twice is a mistake, not a shape to silently repair.
        {"probes": _HEALTH_PROBES + [_HEALTH_PROBES[0]]},
        # Bounded payload: 33 probes exceeds the durable maximum of 32.
        {
            "probes": [
                {"kind": "systemd_unit_active", "target": f"unit-{index}.service"}
                for index in range(33)
            ]
        },
        {"probes": _HEALTH_PROBES, "expected_revision": -1},
    ),
)
def test_health_contract_put_refuses_malformed_or_unbounded_declarations(
    tmp_path: Path, monkeypatch, body
) -> None:
    app, config = _build_app(tmp_path)
    resource = _discover_lxc_resource(app, config, monkeypatch)
    path = _health_contract_path(resource.resource_id)
    headers = {"Authorization": f"Bearer {config.api_bearer_token}"}
    client = TestClient(app)

    response = client.put(path, headers=headers, json=body)
    assert response.status_code == 422, response.text
    assert app.state.store.resource_health_contract(resource.resource_id) is None


def test_health_contract_delete_is_idempotent_and_means_unconfigured(
    tmp_path: Path, monkeypatch
) -> None:
    app, config = _build_app(tmp_path)
    resource = _discover_lxc_resource(app, config, monkeypatch)
    path = _health_contract_path(resource.resource_id)
    headers = {"Authorization": f"Bearer {config.api_bearer_token}"}
    client = TestClient(app)

    client.put(path, headers=headers, json={"probes": _HEALTH_PROBES})
    cleared = client.delete(path, headers=headers)
    assert cleared.status_code == 200
    assert cleared.json() == {
        "resource_id": resource.resource_id,
        "status": "unconfigured",
        "cleared": True,
    }
    again = client.delete(path, headers=headers)
    assert again.status_code == 200
    assert again.json()["cleared"] is False
    # And the resource is now unconfigured, not healthy and not passing.
    assert client.get(path, headers=headers).json()["detail"]["error"] == (
        "contract_unconfigured"
    )


def test_health_contract_compare_and_set_refuses_a_stale_editor(
    tmp_path: Path, monkeypatch
) -> None:
    app, config = _build_app(tmp_path)
    resource = _discover_lxc_resource(app, config, monkeypatch)
    path = _health_contract_path(resource.resource_id)
    headers = {"Authorization": f"Bearer {config.api_bearer_token}"}
    client = TestClient(app)

    first = client.put(
        path, headers=headers, json={"probes": _HEALTH_PROBES, "expected_revision": 0}
    )
    assert first.status_code == 200
    stale = client.put(
        path,
        headers=headers,
        json={
            "probes": [{"kind": "docker_container_running", "target": "redis"}],
            "expected_revision": 0,
        },
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["error"] == "resource_not_current"
    assert client.delete(f"{path}?expected_revision=7", headers=headers).status_code == 409
    # The refused writes left the original contract exactly as it was.
    assert client.get(path, headers=headers).json() == first.json()
    assert client.delete(f"{path}?expected_revision=1", headers=headers).status_code == 200


def test_health_contract_routes_fail_closed_on_a_non_current_resource(
    tmp_path: Path, monkeypatch
) -> None:
    """A QEMU guest and a replaced incarnation are both refused, not defaulted."""

    app, config = _build_app(tmp_path)
    source_id = app.state.store.list_source_states()[0].source.inventory_source_id
    _run_discovery(
        monkeypatch,
        config,
        app.state.authority,
        source_id,
        guests=({"vmid": 101, "type": "lxc", "name": "ct1", "status": "running"},),
    )
    resource = app.state.store.list_resources(source_id)[0]
    path = _health_contract_path(resource.resource_id)
    headers = {"Authorization": f"Bearer {config.api_bearer_token}"}
    client = TestClient(app)
    assert client.put(path, headers=headers, json={"probes": _HEALTH_PROBES}).status_code == 200

    # The same VMID now hosts a QEMU guest: a different resource incarnation.
    _run_discovery(
        monkeypatch,
        config,
        app.state.authority,
        source_id,
        guests=({"vmid": 101, "type": "qemu", "name": "vm1", "status": "running"},),
    )
    successor = next(
        item
        for item in app.state.store.list_resources(source_id)
        if item.resource_id != resource.resource_id
    )

    for method in ("get", "put", "delete"):
        for target in (resource.resource_id, successor.resource_id):
            call = getattr(client, method)
            kwargs = {"headers": headers}
            if method == "put":
                kwargs["json"] = {"probes": _HEALTH_PROBES}
            response = call(_health_contract_path(target), **kwargs)
            assert response.status_code == 409, (method, target, response.text)
            assert response.json()["detail"]["error"] == "resource_not_current"

    # The successor never inherited anything, and the predecessor's own
    # historical row was not edited by any of those refusals.
    assert app.state.store.resource_health_contract(successor.resource_id) is None
    predecessor = app.state.store.resource_health_contract(resource.resource_id)
    assert predecessor is not None and predecessor.revision == 1
