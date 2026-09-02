"""Runtime/adversarial architecture regression coverage.

Adversarial regression checks proving the runtime cannot silently regress
into legacy, mutation, or static-inventory behavior. See ARCHITECTURE.md.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
import sys

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


def test_job_owned_snapshot_safety_is_not_production_reachable() -> None:
    """The snapshot primitives exist internally and stay dark.

    Nothing on the production HTTP, Home Assistant, scheduler, bootstrap, or
    updater paths may construct or call the snapshot orchestrator, its host
    control, or the authority's snapshot transitions.
    """

    snapshot_symbols = (
        "package_update_snapshot",
        "PackageUpdateSnapshotOrchestrator",
        "SshPackageUpdateSnapshotHostControl",
        "ensure_job_owned_snapshot",
        "record_package_update_snapshot_intent",
        "record_package_update_snapshot_task",
        "confirm_package_update_snapshot",
        "select_package_update_rollback_target",
        "record_package_update_preflight_passed",
        "ensure_pre_update_snapshot_submitted",
        "execute_snapshot_submission_if_current",
        "resolve_pre_submission_block",
        "block_package_update_after_snapshot_success_with_stale_authority",
        "seal_operation_never_submitted",
        "hubinet-package-snapshot-helper",
    )
    for rel_path in (
        "app/inventory_runtime.py",
        "app/inventory_scheduler.py",
        "app/package_scan_scheduler.py",
        "app/package_scan.py",
        "app/package_scan_host_control.py",
        "app/inventory_runtime_config.py",
        "custom_components/hubinet_ops/services.py",
        "custom_components/hubinet_ops/transport_http.py",
        "custom_components/hubinet_ops/coordinator.py",
        "deploy/bootstrap-proxmox-0.5.sh",
        "deploy/update-proxmox-0.5.sh",
        "deploy/install-0.5.0-fresh.sh",
    ):
        text = (REPO_ROOT / rel_path).read_text(encoding="utf-8")
        for symbol in snapshot_symbols:
            assert symbol not in text, (rel_path, symbol)


def test_bootstrap_and_updater_deploy_no_snapshot_helper_or_key() -> None:
    """No mutating helper, forced-command line, key, or PVE privilege ships."""

    for rel_path in ("deploy", "deploy/lib"):
        directory = REPO_ROOT / rel_path
        for path in sorted(directory.glob("*.sh")):
            text = path.read_text(encoding="utf-8")
            assert "snapshot-helper" not in text, path
            assert "hubinet-package-snapshot" not in text, path

    # The deployed PVE privilege set stays exactly the audit-only pair: no
    # VM.Snapshot or VM.Snapshot.Rollback is provisioned anywhere.
    for path in sorted((REPO_ROOT / "deploy").rglob("*")):
        if path.is_file() and path.suffix in (".sh", ".py"):
            text = path.read_text(encoding="utf-8")
            assert "VM.Snapshot" not in text, path
            assert "VM.Snapshot.Rollback" not in text, path


def test_the_snapshot_helper_is_a_separate_file_from_the_scan_helper() -> None:
    scan = REPO_ROOT / "deploy/hubinet-package-scan-helper.py"
    snapshot = REPO_ROOT / "deploy/hubinet-package-snapshot-helper.py"
    assert scan.exists() and snapshot.exists()
    scan_text = scan.read_text(encoding="utf-8")
    # The scan helper gained no snapshot capability of any kind.
    for forbidden in (
        "snapshot",
        "pvesh create",
        "vzsnapshot",
        "VM.Snapshot",
    ):
        assert forbidden not in scan_text, forbidden


def test_the_snapshot_helper_exposes_no_delete_or_rollback_operation() -> None:
    text = (REPO_ROOT / "deploy/hubinet-package-snapshot-helper.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(text)
    operations = None
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "OPERATIONS"
                for target in node.targets
            )
        ):
            operations = ast.literal_eval(node.value)
    assert operations == (
        "inspect_job_snapshot_state",
        "ensure_pre_update_snapshot_submitted",
        "seal_operation_never_submitted",
    )
    # `pvesh` is only ever invoked with a read or a create verb.
    verbs = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Tuple) and node.elts:
            first = node.elts[0]
            if isinstance(first, ast.Constant) and first.value == "pvesh":
                second = node.elts[1]
                assert isinstance(second, ast.Constant), ast.dump(node)
                verbs.add(second.value)
    assert verbs == {"get", "create"}


def test_no_package_or_apt_mutation_exists_anywhere_in_the_backend() -> None:
    for path in sorted((REPO_ROOT / "app").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for forbidden in (
            '"apt-get", "install"',
            '"apt-get", "upgrade"',
            '"apt-get", "dist-upgrade"',
            '"dpkg"',
            "apt-get install",
            "apt-get upgrade",
        ):
            assert forbidden not in text, (path, forbidden)


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


# ---------------------------------------------------------------------------
# NEXT-C: the execution-time APT plan equality gate stays dark.
# ---------------------------------------------------------------------------


def test_execution_plan_gate_is_not_production_reachable() -> None:
    """The execution-time plan equality gate exists internally and stays dark.

    Nothing on the production HTTP, Home Assistant, scheduler, bootstrap, or
    updater paths may construct or call the execution-gate orchestrator, its
    host control, or the authority's execution-plan comparison transition.
    """

    execution_symbols = (
        "package_update_execution",
        "PackageUpdateExecutionHostControl",
        "SshPackageUpdateExecutionHostControl",
        "run_package_update_execution_gate",
        "evaluate_package_update_execution_plan",
        "simulate_exact_update_plan",
        "hubinet-package-update-helper",
    )
    for rel_path in (
        "app/inventory_runtime.py",
        "app/inventory_scheduler.py",
        "app/package_scan_scheduler.py",
        "app/package_scan.py",
        "app/package_scan_host_control.py",
        "app/package_update_snapshot.py",
        "app/package_update_snapshot_host_control.py",
        # The mutation stage re-proves exact plan equality through the
        # authority, never by reaching into the gate's own dark boundary.
        "app/package_update_mutation.py",
        "app/package_update_mutation_host_control.py",
        "app/inventory_runtime_config.py",
        "custom_components/hubinet_ops/services.py",
        "custom_components/hubinet_ops/transport_http.py",
        "custom_components/hubinet_ops/coordinator.py",
        "deploy/bootstrap-proxmox-0.5.sh",
        "deploy/update-proxmox-0.5.sh",
        "deploy/install-0.5.0-fresh.sh",
    ):
        text = (REPO_ROOT / rel_path).read_text(encoding="utf-8")
        for symbol in execution_symbols:
            assert symbol not in text, (rel_path, symbol)


def test_bootstrap_and_updater_deploy_no_execution_helper_or_key() -> None:
    """No update-execution helper, forced-command line, or key ships.

    ``hubinet-package-update`` (not the bare, ambiguous ``update-helper`` --
    that substring legitimately appears in the *existing* scan-helper
    in-place update machinery, e.g. ``UPDATE_HELPER_STAGED_HOST_PATH``) is
    this new dark helper's exact, unambiguous name prefix.
    """

    for rel_path in ("deploy", "deploy/lib"):
        directory = REPO_ROOT / rel_path
        for path in sorted(directory.glob("*.sh")):
            text = path.read_text(encoding="utf-8")
            assert "hubinet-package-update" not in text, path


def test_the_execution_helper_is_a_separate_file_from_the_scan_and_snapshot_helpers() -> None:
    scan = REPO_ROOT / "deploy/hubinet-package-scan-helper.py"
    snapshot = REPO_ROOT / "deploy/hubinet-package-snapshot-helper.py"
    execution = REPO_ROOT / "deploy/hubinet-package-update-helper.py"
    assert scan.exists() and snapshot.exists() and execution.exists()
    for other, text in (
        (scan, scan.read_text(encoding="utf-8")),
        (snapshot, snapshot.read_text(encoding="utf-8")),
    ):
        assert "simulate_exact_update_plan" not in text, other


def test_the_execution_helper_exposes_exactly_one_non_mutating_operation() -> None:
    text = (REPO_ROOT / "deploy/hubinet-package-update-helper.py").read_text(
        encoding="utf-8"
    )
    assert 'payload["operation"] != "simulate_exact_update_plan"' in text
    for forbidden in (
        "execute_packages",
        "install_packages",
        "pct snapshot",
        "pct rollback",
        "snapshot",
        '"apt-get", "install"',
        '"apt-get", "upgrade"',
        '"apt-get", "dist-upgrade"',
        "apt-get install",
        "apt-get upgrade",
        "apt-get dist-upgrade",
        "apt-get remove",
        "apt-get autoremove",
        "apt full-upgrade",
        "dpkg -i",
        "dpkg --configure",
        "VM.Snapshot",
    ):
        assert forbidden not in text, forbidden
    # The only two allowed APT invocations: a metadata refresh and a
    # simulated (never real) upgrade.
    assert '"apt-get", "update", "-qq"' in text
    assert '"apt-get", "-s", "upgrade"' in text


# ---------------------------------------------------------------------------
# Crash-safe real package mutation stays dark.
# ---------------------------------------------------------------------------


def test_package_mutation_is_not_production_reachable() -> None:
    """The only real package command in the product stays unreachable.

    Nothing on the production HTTP, Home Assistant, scheduler, bootstrap, or
    updater paths -- nor any other dark stage -- may construct or call the
    mutation orchestrator, its host control, or the authority's mutation
    transitions.
    """

    mutation_symbols = (
        "package_update_mutation",
        "PackageUpdateMutationHostControl",
        "SshPackageUpdateMutationHostControl",
        "PackageUpdateMutationOrchestrator",
        "execute_job_owned_mutation",
        "arm_package_update_mutation",
        "execute_package_mutation_submission_if_current",
        "resolve_pre_mutation_block",
        "complete_package_update_mutation",
        "execute_exact_package_mutation",
        "hubinet-package-mutation-helper",
    )
    for rel_path in (
        "app/inventory_runtime.py",
        "app/inventory_scheduler.py",
        "app/package_scan_scheduler.py",
        "app/package_scan.py",
        "app/package_scan_host_control.py",
        "app/package_update_snapshot.py",
        "app/package_update_snapshot_host_control.py",
        "app/package_update_execution.py",
        "app/package_update_execution_host_control.py",
        "app/inventory_runtime_config.py",
        "custom_components/hubinet_ops/services.py",
        "custom_components/hubinet_ops/transport_http.py",
        "custom_components/hubinet_ops/coordinator.py",
        "deploy/bootstrap-proxmox-0.5.sh",
        "deploy/update-proxmox-0.5.sh",
        "deploy/install-0.5.0-fresh.sh",
    ):
        text = (REPO_ROOT / rel_path).read_text(encoding="utf-8")
        for symbol in mutation_symbols:
            assert symbol not in text, (rel_path, symbol)


def test_bootstrap_and_updater_deploy_no_mutation_helper_or_key() -> None:
    """No package-mutation helper, forced-command line, or key ships."""

    for rel_path in ("deploy", "deploy/lib"):
        directory = REPO_ROOT / rel_path
        for path in sorted(directory.glob("*.sh")):
            text = path.read_text(encoding="utf-8")
            assert "hubinet-package-mutation" not in text, path
            assert "package-mutation-operations" not in text, path


def test_the_mutation_helper_is_the_only_file_that_can_change_a_package() -> None:
    """Every other helper keeps its own, weaker, non-mutating promise."""

    mutation = REPO_ROOT / "deploy/hubinet-package-mutation-helper.py"
    assert mutation.exists()
    for other in (
        "deploy/hubinet-package-scan-helper.py",
        "deploy/hubinet-package-snapshot-helper.py",
        "deploy/hubinet-package-update-helper.py",
    ):
        text = (REPO_ROOT / other).read_text(encoding="utf-8")
        for forbidden in (
            "execute_exact_package_mutation",
            "prepare_exact_package_mutation",
            '"apt-get", "install"',
            '"apt-get", "dist-upgrade"',
            "apt-get install",
            "apt-get dist-upgrade",
            "apt-get remove",
            "apt-get autoremove",
            "dpkg -i",
            "dpkg --configure",
        ):
            assert forbidden not in text, (other, forbidden)


def test_the_mutation_helper_runs_exactly_one_fixed_bounded_package_command() -> None:
    text = (REPO_ROOT / "deploy/hubinet-package-mutation-helper.py").read_text(
        encoding="utf-8"
    )
    # Exactly four typed operations, no generic dispatcher, no shell.
    for operation in (
        "prepare_exact_package_mutation",
        "execute_exact_package_mutation",
        "seal_mutation_never_submitted",
        "inspect_package_mutation_state",
    ):
        assert f'"{operation}"' in text, operation
    for forbidden in (
        "shell=True",
        "os.system",
        "sh -c",
        "pct snapshot",
        "pct rollback",
        "pct destroy",
        "VM.Snapshot",
        "apt-get install",
        "apt-get dist-upgrade",
        "apt-get remove",
        "apt-get purge",
        "apt-get autoremove",
        "full-upgrade",
        "dpkg -i",
        "--force-yes",
    ):
        assert forbidden not in text, forbidden

    # The one real package command's options are a fixed module-level tuple
    # built from literals only, never from request data.
    module = ast.parse(text, filename="hubinet-package-mutation-helper.py")
    argv = None
    for node in ast.walk(module):
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "_MUTATION_BASE_OPTIONS"
        ):
            argv = ast.literal_eval(node.value)
    assert isinstance(argv, tuple) and argv, (
        "_MUTATION_BASE_OPTIONS must be a literal tuple"
    )
    assert all(isinstance(item, str) for item in argv)
    assert "Dpkg::Options::=--force-confold" in argv

    # The two options that complete it install the pre-dpkg action gate. Both
    # are f-strings over ONE name -- the verifier path, which is itself
    # derived only from a canonical UUID -- so no request text, package
    # value, or shell fragment can enter the command line.
    spec = importlib.util.spec_from_file_location(
        "hubinet_package_mutation_helper_r0",
        REPO_ROOT / "deploy" / "hubinet-package-mutation-helper.py",
    )
    assert spec is not None and spec.loader is not None
    helper = importlib.util.module_from_spec(spec)
    # `slots=True` dataclasses re-create their class and look it up through
    # sys.modules, so the module must be registered before it is executed.
    sys.modules[spec.name] = helper
    spec.loader.exec_module(helper)
    operation_id = "55555555-5555-4555-8555-555555555555"
    full = helper.mutation_argv(operation_id)
    assert full[: len(argv)] == argv
    assert full[-1] == "upgrade"
    verifier = helper.guest_verifier_path(operation_id)
    assert list(full[len(argv):-1]) == [
        "-o",
        f"DPkg::Pre-Install-Pkgs::={verifier}",
        "-o",
        f"DPkg::Tools::Options::{verifier}::Version=3",
    ]
    # Staged strictly under Hubinet's own ephemeral guest root, and nowhere
    # a persistent guest artifact could outlive the operation.
    assert verifier.startswith("/run/hubinet-ops/package-mutation/")

    # The verifier itself is a guest-side artifact this product never
    # deploys: neither bootstrap nor the updater writes it, and it exists
    # only for the duration of one operation inside one guest's tmpfs.
    for rel_path in (
        "deploy/bootstrap-proxmox-0.5.sh",
        "deploy/update-proxmox-0.5.sh",
        "deploy/install-0.5.0-fresh.sh",
    ):
        installed = (REPO_ROOT / rel_path).read_text(encoding="utf-8")
        assert "hubinet-package-mutation-helper" not in installed
        assert "Pre-Install-Pkgs" not in installed
        assert "/run/hubinet-ops/package-mutation" not in installed


# ---------------------------------------------------------------------------
# Same-job rollback execution stays dark.
# ---------------------------------------------------------------------------


def test_rollback_execution_is_not_production_reachable() -> None:
    """The product's most destructive PVE operation stays unreachable.

    Nothing on the production HTTP, Home Assistant, scheduler, bootstrap, or
    updater paths -- nor any other dark stage -- may construct or call the
    rollback orchestrator, its host control, or the authority's rollback
    transitions.
    """

    rollback_symbols = (
        # Deliberately NOT the bare "package_update_rollback" prefix: the
        # pre-existing selection contract
        # (`select_package_update_rollback_target`, PR #67) legitimately
        # appears in the snapshot module, and authorizing a target is not
        # executing a rollback.
        "app.package_update_rollback",
        "app.package_update_rollback_host_control",
        "PackageUpdateRollbackHostControl",
        "SshPackageUpdateRollbackHostControl",
        "PackageUpdateRollbackOrchestrator",
        "roll_back_to_job_snapshot",
        "arm_package_update_rollback",
        "execute_rollback_submission_if_current",
        "resolve_pre_rollback_block",
        "complete_package_update_rollback",
        "submit_same_job_rollback",
        "hubinet-package-rollback-helper",
    )
    for rel_path in (
        "app/inventory_runtime.py",
        "app/inventory_scheduler.py",
        "app/package_scan_scheduler.py",
        "app/package_scan.py",
        "app/package_scan_host_control.py",
        "app/package_update_snapshot.py",
        "app/package_update_snapshot_host_control.py",
        "app/package_update_execution.py",
        "app/package_update_execution_host_control.py",
        "app/package_update_mutation.py",
        "app/package_update_mutation_host_control.py",
        "app/inventory_runtime_config.py",
        "custom_components/hubinet_ops/services.py",
        "custom_components/hubinet_ops/transport_http.py",
        "custom_components/hubinet_ops/coordinator.py",
        "deploy/bootstrap-proxmox-0.5.sh",
        "deploy/update-proxmox-0.5.sh",
        "deploy/install-0.5.0-fresh.sh",
    ):
        text = (REPO_ROOT / rel_path).read_text(encoding="utf-8")
        for symbol in rollback_symbols:
            assert symbol not in text, (rel_path, symbol)


def test_bootstrap_and_updater_deploy_no_rollback_helper_key_or_privilege() -> None:
    """No rollback helper, forced-command line, key, or PVE privilege ships.

    `VM.Snapshot.Rollback` (and `VM.Snapshot`, which upstream also accepts for
    the rollback endpoint) must appear nowhere in any deployment script: the
    provisioned role stays exactly `Sys.Audit,VM.Audit`.
    """

    for rel_path in ("deploy", "deploy/lib"):
        directory = REPO_ROOT / rel_path
        for path in sorted(directory.glob("*.sh")):
            text = path.read_text(encoding="utf-8")
            assert "hubinet-package-rollback" not in text, path
            assert "rollback-operations" not in text, path
            assert "VM.Snapshot.Rollback" not in text, path
            assert "VM.Snapshot" not in text, path


def test_the_rollback_helper_is_the_only_file_that_can_roll_back() -> None:
    """Every other helper keeps its own, weaker, non-rollback promise.

    The snapshot helper in particular must never gain rollback: keeping
    create and rollback in separate forced-command boundaries is what stops
    one deployed key carrying both "add a recovery point" and "destroy the
    guest's current state".
    """

    rollback = REPO_ROOT / "deploy/hubinet-package-rollback-helper.py"
    assert rollback.exists()
    for other in (
        "deploy/hubinet-package-scan-helper.py",
        "deploy/hubinet-package-snapshot-helper.py",
        "deploy/hubinet-package-update-helper.py",
        "deploy/hubinet-package-mutation-helper.py",
    ):
        text = (REPO_ROOT / other).read_text(encoding="utf-8")
        for forbidden in (
            "submit_same_job_rollback",
            "seal_rollback_never_submitted",
            "inspect_rollback_state",
            # The pvesh rollback endpoint path fragment, as it appears in
            # real argv rather than in prose.
            '/rollback"',
            "pct rollback",
        ):
            assert forbidden not in text, (other, forbidden)


def test_the_rollback_helper_exposes_exactly_three_typed_operations() -> None:
    text = (REPO_ROOT / "deploy/hubinet-package-rollback-helper.py").read_text(
        encoding="utf-8"
    )
    for operation in (
        "inspect_rollback_state",
        "submit_same_job_rollback",
        "seal_rollback_never_submitted",
    ):
        assert f'"{operation}"' in text, operation
    for forbidden in (
        "shell=True",
        "os.system",
        "sh -c",
        "pct exec",
        "pct destroy",
        "pct start",
        "pct stop",
        "snapshot/delete",
        "apt-get",
        "dpkg",
    ):
        assert forbidden not in text, forbidden

    # `start` is pinned to 0 as a module-level literal, so a successful
    # rollback always leaves the guest stopped and nothing on the request
    # boundary can ask for anything else.
    module = ast.parse(text, filename="hubinet-package-rollback-helper.py")
    start = None
    for node in ast.walk(module):
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name)
                and target.id == "ROLLBACK_START_AFTER"
                for target in node.targets
            )
        ):
            start = ast.literal_eval(node.value)
    assert start == 0, "this stage must always roll back with start=0"


def test_no_snapshot_deletion_exists_anywhere_in_the_product() -> None:
    """Retention and deletion remain entirely unimplemented."""

    for rel_path in (
        "app/package_update_rollback.py",
        "app/package_update_rollback_host_control.py",
        "app/package_update_snapshot.py",
        "app/package_update_snapshot_host_control.py",
        "deploy/hubinet-package-rollback-helper.py",
        "deploy/hubinet-package-snapshot-helper.py",
    ):
        text = (REPO_ROOT / rel_path).read_text(encoding="utf-8")
        for forbidden in ("pvesh\", \"delete", "snapshot/delete", "delete_snapshot"):
            assert forbidden not in text, (rel_path, forbidden)


def test_no_health_execution_exists_anywhere() -> None:
    """Healthcheck execution is deliberately NOT part of this stage.

    The product has no truthful generic workload-health definition yet (see
    STATUS.md), so no code may claim one: nothing writes `health_started_at`,
    advances to the `health_started` checkpoint, or terminalizes a job
    `succeeded`.
    """

    for rel_path in (
        "app/inventory/authority.py",
        "app/package_update_rollback.py",
        "app/package_update_mutation.py",
        "app/package_update_snapshot.py",
        "app/package_update_execution.py",
    ):
        text = (REPO_ROOT / rel_path).read_text(encoding="utf-8")
        assert "health_started_at=" not in text, rel_path
        assert "status='succeeded'" not in text, rel_path
        assert "checkpoint='health_started'" not in text, rel_path
