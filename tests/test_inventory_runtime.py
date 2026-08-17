"""WAVE R0-B Family 4 -- R0 composition root and read-only HTTP API.

Covers §28 tests #1, #2, #3, #4, #27, #28, #29 (backend-side half), #30
(P3 cleanup: explicitly assigned here), #32, #35, #40 (runtime/composition
portion) of docs/architecture/0.5-r0-read-only-runtime-activation.md.
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

from app.inventory import AuthorityDatabaseRejected
from app.inventory_pve_transport import ProxmoxHttpTransport, _PVE_API_PREFIX
from app.inventory_runtime import create_read_only_app
from app.inventory_runtime_config import parse_r0_runtime_config
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
# §28 test #1 -- composition-root import-graph test
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
# §28 test #2 -- FastAPI route enumeration
# ---------------------------------------------------------------------------


def test_2_only_get_head_options_routes_exist(tmp_path: Path) -> None:
    app, _config = _build_app(tmp_path)
    found_r0_route = False
    for route in app.routes:
        methods = getattr(route, "methods", None)
        if methods is None:
            continue
        assert methods <= {"GET", "HEAD", "OPTIONS"}, (getattr(route, "path", route), methods)
        if str(getattr(route, "path", "")).startswith("/r0/v1"):
            found_r0_route = True
    assert found_r0_route
    assert app.docs_url is None
    assert app.redoc_url is None
    assert app.openapi_url is None


# ---------------------------------------------------------------------------
# §28 test #3 -- app.main import/behavior unchanged (narrow regression guard)
# ---------------------------------------------------------------------------


def test_3_legacy_app_main_is_untouched_by_this_wave(tmp_path: Path, monkeypatch) -> None:
    import importlib

    from app.config import Settings
    from app.database import Database

    class FakeExecutor:
        def run(self, action, vmid, argument=None, timeout=None, on_event=None):
            return {"ok": True, "data": {}}

    config_path = tmp_path / "legacy-config.yaml"
    config_path.write_text(
        "scheduler:\n  enabled: false\ncontainers:\n  106:\n    enabled: true\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HUBINET_OPS_CONFIG", str(config_path))
    monkeypatch.setenv("HUBINET_OPS_DB", str(tmp_path / "legacy-import.db"))
    monkeypatch.setenv("HUBINET_OPS_API_TOKEN", "l" * 64)
    main = importlib.import_module("app.main")

    cfg = Settings(
        raw={"scheduler": {"enabled": False}, "mqtt": {"enabled": False}, "home_assistant": {}, "containers": {106: {"enabled": True}}},
        config_path=tmp_path / "legacy-config.yaml",
        db_path=tmp_path / "legacy.db",
        api_token="t" * 64,
    )
    db = Database(cfg.db_path)
    client = TestClient(main.create_app(cfg, database=db, executor=FakeExecutor()))

    # These legacy routes must remain exactly as before this wave -- R0-B
    # never edits app/main.py.
    assert client.get("/health").status_code == 200
    assert client.get("/api/v1/resources", headers={"Authorization": "Bearer " + cfg.api_token}).status_code == 200


# ---------------------------------------------------------------------------
# §28 test #4 -- R0 DB separateness + rejection of legacy/incompatible DB
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
# §28 test #27/#28 -- no path to trusted; effective capabilities always empty
# ---------------------------------------------------------------------------


def test_27_28_security_continuity_never_trusted_and_capabilities_always_empty(
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
        assert resource["security_continuity"] in ("unverified", "revoked")
        assert resource["security_continuity"] != "trusted"
        assert resource["effective_capabilities"] == []
        assert resource["policy_applicable"] is False


# ---------------------------------------------------------------------------
# §28 test #29 (backend-side half) -- publication -> HTTP field mapping
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
# §28 test #30 -- backend identity stable across restart (P3: assigned here)
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
# §28 test #32 -- auth failures correctly mapped
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


# ---------------------------------------------------------------------------
# §28 test #35 -- freshness expiry remains backend-owned
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
# §28 test #40 (runtime/composition portion) -- never touches an unrelated DB
# ---------------------------------------------------------------------------


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
