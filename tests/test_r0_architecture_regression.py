"""Runtime/adversarial architecture regression coverage.

Adversarial regression checks proving the runtime cannot silently regress
into legacy, uncontrolled-mutation, or static-inventory behavior. See
`ARCHITECTURE.md`.

## What changed at production activation, and what did not

Until this release the update lifecycle was DARK, and this file proved it by
asserting that nothing on any production path so much as named the snapshot,
execution, mutation, rollback, or health stages. Production activation
deliberately invalidates that exact assertion -- those stages are now
reachable, and the tests that said otherwise would be false.

They are replaced by something stronger rather than deleted, because
"unreachable" was only ever a proxy for the property that actually matters:
**a real workload package mutation begins for exactly one reason, and a
rollback for exactly one other.** So the tests below pin the allowed edges
instead of the absence of edges --

```text
authenticated explicit API/HA operator action
  -> durable job authority        (issue_package_update_job / arm_..._rollback)
  -> the ONE production worker    (composition only, no state machine)
  -> the existing typed orchestrators
  -> their own dedicated typed host controls and forced-command helpers
```

-- and continue to prove the ABSENCE of every other route to one: no
scheduler-issued job, no scan-triggered job, no generic command dispatch, no
caller-supplied VMID/snapshot/package/probe, no automatic rollback, no shared
privileged helper, and no static VMID configuration.
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



def test_r0_production_modules_define_only_authority_metadata_mutations() -> None:
    """The R0 write surface is an exact allowlist of explicit operations.

    Two `@app.put` (the exact-plan approval and the health-contract
    replacement), one `@app.delete` (the health-contract clear), and exactly
    four `@app.post` -- start, resume, and roll back one update, plus the
    product updater's own exclusive maintenance fence. The first three are
    explicit operator controls over the update lifecycle; the fourth performs
    no workload action at all and exists only to make a product update and a
    workload update mutually exclusive. None is a generic dispatcher, and
    `@app.patch` stays absent entirely.

    Production activation is what made a destructive verb possible at all, so
    this list is now the thing that stops a fifth one appearing quietly.
    """

    text = (REPO_ROOT / "app/inventory_runtime.py").read_text(encoding="utf-8")
    assert "@app.patch(" not in text
    assert text.count("@app.put(") == 2
    assert text.count("@app.delete(") == 1
    assert text.count("@app.post(") == 4
    assert (
        'f"{API_PREFIX}/resources/{{resource_id}}/package-plan-approval"'
        in text
    )
    assert (
        '_HEALTH_CONTRACT_ROUTE = f"{API_PREFIX}/resources/{{resource_id}}/health-contract"'
        in text
    )
    assert (
        '_PACKAGE_UPDATE_ROUTE = f"{API_PREFIX}/resources/{{resource_id}}/package-update"'
        in text
    )
    assert f'{{API_PREFIX}}/package-update/maintenance-fence' in text



#: Every module that could plausibly want to start an update and must not.
#: A scheduler, a scan, an approval write, and the worker itself are all
#: things that run WITHOUT an operator asking, so none of them may issue a
#: job. `app/inventory_runtime.py` is deliberately absent from this list: it
#: is the one place an authenticated operator request lands.
_MODULES_THAT_MAY_NEVER_ISSUE_AN_UPDATE_JOB = (
    "app/inventory_scheduler.py",
    "app/package_scan_scheduler.py",
    "app/package_scan.py",
    "app/package_scan_host_control.py",
    "app/package_update_worker.py",
    "app/package_update_snapshot.py",
    "app/package_update_execution.py",
    "app/package_update_mutation.py",
    "app/package_update_rollback.py",
    "app/package_update_health.py",
    "custom_components/hubinet_ops/coordinator.py",
    "custom_components/hubinet_ops/sensor.py",
    "custom_components/hubinet_ops/transport_http.py",
    "deploy/bootstrap-proxmox-0.5.sh",
    "deploy/update-proxmox-0.5.sh",
    "deploy/install-0.5.0-fresh.sh",
)


def _called_names(tree: ast.AST) -> set[str]:
    """Every attribute/name actually CALLED anywhere in a parsed module.

    A substring scan cannot tell a call from a docstring sentence, and these
    modules document their own negative space at length ("this module never
    calls ``arm_package_update_rollback``"). Only real call syntax counts.
    """

    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute):
            names.add(func.attr)
        elif isinstance(func, ast.Name):
            names.add(func.id)
    return names


def _innermost_callers(source: str, symbol: str) -> list[str]:
    """Name every function whose OWN body calls ``symbol``.

    Nested-function-aware on purpose. `ast.walk` from a module would also
    report every enclosing function -- and the R0 route handlers are all
    closures defined inside ``create_read_only_app`` -- which would make
    "exactly one function calls this" unprovable rather than false.
    """

    callers: list[str] = []

    def own_calls(node: ast.AST) -> set[str]:
        names: set[str] = set()
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            names |= _called_names(child)
            for grandchild in ast.walk(child):
                if isinstance(
                    grandchild, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                ):
                    # A lambda body is still this function's own code; a
                    # nested def is not.
                    names -= _called_names(grandchild)
        return names

    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if symbol in own_calls(node):
                callers.append(node.name)
    return sorted(callers)


def test_only_an_explicit_operator_request_can_issue_an_update_job() -> None:
    """NO AUTO-UPDATE, proven structurally rather than asserted in prose.

    A durable package-update job is the only thing that can lead to a real
    workload package mutation, and `issue_package_update_job` is the only
    thing that creates one. So the complete set of production callers of that
    method is the complete set of ways an update can begin -- and it has
    exactly one member: the authenticated `POST
    /r0/v1/resources/{id}/package-update` route in the composition root.

    Neither scheduler calls it. No scan callback calls it. The approval write
    does not call it. The Home Assistant coordinator does not call it. And
    the worker does not call it either: continuing a job an operator started
    is not auto-update, but inventing one would be.
    """

    runtime = REPO_ROOT / "app/inventory_runtime.py"
    tree = ast.parse(runtime.read_text(encoding="utf-8"))
    assert "issue_package_update_job" in _called_names(tree)

    # And it is called from exactly one function, which is the POST route.
    assert _innermost_callers(
        runtime.read_text(encoding="utf-8"), "issue_package_update_job"
    ) == ["start_package_update"]

    for rel_path in _MODULES_THAT_MAY_NEVER_ISSUE_AN_UPDATE_JOB:
        text = _code(REPO_ROOT / rel_path)
        assert "issue_package_update_job" not in text, rel_path


def test_the_maintenance_fence_is_read_inside_the_issuance_transaction() -> None:
    """The synchronization is the writer lock, not the file's existence.

    A product update and a workload update are made mutually exclusive by
    both sides taking the authority store's single `BEGIN IMMEDIATE` writer
    lock. That only works if the fence read happens INSIDE issuance's
    transaction: a read hoisted above the `with` block would be exactly the
    check-then-act race the fence exists to close.
    """

    tree = ast.parse((REPO_ROOT / "app/inventory/authority.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        if node.name not in (
            "issue_package_update_job",
            "acquire_product_update_maintenance_fence",
        ):
            continue
        transactions = [
            item for item in ast.walk(node) if isinstance(item, ast.With)
        ]
        assert transactions, node.name
        inside = set()
        for block in transactions:
            for statement in block.body:
                inside |= _called_names(statement)
        assert "read_product_update_fence" in inside, node.name
        if node.name == "acquire_product_update_maintenance_fence":
            # And the fence is made durable BEFORE the commit that releases
            # the lock, or an issuing transaction could slip in behind it.
            assert "write_product_update_fence" in inside


def test_no_scheduler_worker_or_operator_path_can_take_the_maintenance_fence() -> None:
    """The fence is the product updater's control, and only its own.

    Nothing in the workload lifecycle may take it: a worker or an operator
    action that could fence the installation would be able to block workload
    updates as a side effect of doing ordinary work.
    """

    for rel_path in (
        "app/package_update_worker.py",
        "app/inventory_scheduler.py",
        "app/package_scan_scheduler.py",
        "app/package_update_snapshot.py",
        "app/package_update_execution.py",
        "app/package_update_mutation.py",
        "app/package_update_rollback.py",
        "app/package_update_health.py",
        "custom_components/hubinet_ops/services.py",
        "custom_components/hubinet_ops/coordinator.py",
        "custom_components/hubinet_ops/transport_http.py",
    ):
        text = _code(REPO_ROOT / rel_path)
        for symbol in (
            "acquire_product_update_maintenance_fence",
            "write_product_update_fence",
            "maintenance-fence",
        ):
            assert symbol not in text, (rel_path, symbol)

    # And exactly one production caller takes it.
    runtime = (REPO_ROOT / "app/inventory_runtime.py").read_text(encoding="utf-8")
    assert _innermost_callers(
        runtime, "acquire_product_update_maintenance_fence"
    ) == ["acquire_product_update_maintenance_fence"]


def test_the_product_updater_never_releases_a_fence_it_does_not_hold() -> None:
    """Release is filesystem-only, terminal-only, and keyed to this run.

    It needs no atomicity -- removing a fence only ever widens what is
    permitted -- but it must never remove one another run holds, and must
    never happen while this run can still be asked to roll back.
    """

    activate = (REPO_ROOT / "deploy/lib/update-activate.sh").read_text(encoding="utf-8")
    assert "_update_release_maintenance_fence" in activate
    # Keyed off the fence's OWN recorded holder, not this process's memory --
    # that is what makes a crash between "the fence is durable" and "this run
    # recorded that it holds it" recoverable rather than orphaning it.
    assert '"${holder}" != "${UPDATE_RUN_ID}"' in activate
    assert "is held by another product update" in activate
    # Acquired after every harmless refusal, immediately before the window.
    assert (
        "_update_preflight_ct_sync\n" in activate
        and activate.index("_update_acquire_maintenance_fence\n}")
        > activate.index("_update_preflight_ct_sync\n")
    )
    # The marker survives a journal reload, so an interrupted run's own
    # recovery can release exactly its own fence.
    recovery = (REPO_ROOT / "deploy/lib/update-recovery.sh").read_text(encoding="utf-8")
    assert "update-maintenance-fence-held) return 0 ;;" in recovery
    # No bypass anywhere.
    for text in (activate, recovery, (REPO_ROOT / "deploy/lib/update-plan.sh").read_text(encoding="utf-8")):
        for forbidden in ("--force-fence", "skip_fence", "SKIP_FENCE", "--no-fence"):
            assert forbidden not in text, forbidden


def test_no_scheduler_timer_or_scan_callback_can_reach_the_update_worker() -> None:
    """The worker is woken by explicit operator routes and startup only.

    A wake is a hint, but a hint from a timer that fires every six hours
    would be a scheduler deciding to advance a workload update. So the set of
    things holding a reference to the worker matters: it is the composition
    root (which starts it, wakes it from the three operator routes, and stops
    it at shutdown) and nothing else.
    """

    for rel_path in (
        "app/inventory_scheduler.py",
        "app/package_scan_scheduler.py",
        "app/package_scan.py",
        "custom_components/hubinet_ops/coordinator.py",
    ):
        text = _code(REPO_ROOT / rel_path)
        for symbol in ("PackageUpdateWorker", "package_update_worker"):
            assert symbol not in text, (rel_path, symbol)

    # The worker itself owns no timer. Its loop blocks on an Event with no
    # timeout: with nothing to do it sleeps forever rather than polling.
    assert "self._wake_event.wait()" in (
        REPO_ROOT / "app/package_update_worker.py"
    ).read_text(encoding="utf-8")
    worker = _code(REPO_ROOT / "app/package_update_worker.py")
    for forbidden in (
        "interval_seconds",
        "poll_interval",
        "initial_delay",
        "time.sleep",
        "datetime.now",
        "threading.Timer",
    ):
        assert forbidden not in worker, forbidden


def test_startup_recovery_never_issues_a_job_and_never_marks_success() -> None:
    """Recovering an already-started job is not starting one.

    The authority's own startup recovery runs FIRST and terminalizes every
    provably pre-mutation job; only then does the worker inspect whatever
    still owns the global slot. Neither step can create a job, and neither
    can conclude a job succeeded.
    """

    runtime = (REPO_ROOT / "app/inventory_runtime.py").read_text(encoding="utf-8")
    assert runtime.count("authority.recover_interrupted_package_update_jobs()") == 1
    assert runtime.index("recover_interrupted_package_update_jobs") < runtime.index(
        "_build_package_update_runtime(authority, store, config)"
    )

    worker = _code(REPO_ROOT / "app/package_update_worker.py")
    for forbidden in (
        "issue_package_update_job",
        "complete_package_update_health",
        "PackageUpdateJobStatus.SUCCEEDED",
        "succeeded",
    ):
        assert forbidden not in worker, forbidden



#: The complete set of stage entry points the production worker is allowed to
#: compose, and the exact host-control protocol methods those stages may use.
#: Anything the worker calls that is not in the first set, or any privileged
#: host operation that is not in the second, is a new production edge.
_ALLOWED_WORKER_STAGE_CALLS = frozenset(
    {
        "ensure_job_owned_snapshot",
        "run_package_update_execution_gate",
        "execute_job_owned_mutation",
        "recover_job_owned_mutation",
        "evaluate_job_health",
        "roll_back_to_job_snapshot",
    }
)

#: Authority transitions the worker may NEVER perform itself. Each of them is
#: either an authority-level decision that belongs to a stage, or -- in the
#: rollback case -- a decision that belongs to an authenticated operator.
_AUTHORITY_TRANSITIONS_FORBIDDEN_TO_THE_WORKER = (
    "issue_package_update_job",
    "arm_package_update_rollback",
    "arm_package_update_mutation",
    "record_package_update_snapshot_intent",
    "confirm_package_update_snapshot",
    "execute_snapshot_submission_if_current",
    "execute_package_mutation_submission_if_current",
    "execute_rollback_submission_if_current",
    "complete_package_update_mutation",
    "complete_package_update_rollback",
    "complete_package_update_health",
    "select_package_update_rollback_target",
)


def test_the_production_worker_composes_exactly_the_existing_stages() -> None:
    """Production reachability is a composition, not a new state machine.

    The one worker calls the six stage entry points PRs #67-#73 already
    built, and performs no authority transition of its own. Every durable
    decision -- arming a write-ahead boundary, submitting to a host,
    completing an operation, recording a verdict -- still belongs to the
    stage that owns it, inside the transaction that owns it.
    """

    tree = ast.parse(
        (REPO_ROOT / "app/package_update_worker.py").read_text(encoding="utf-8")
    )
    called = _called_names(tree)
    assert _ALLOWED_WORKER_STAGE_CALLS <= called
    for forbidden in _AUTHORITY_TRANSITIONS_FORBIDDEN_TO_THE_WORKER:
        assert forbidden not in called, forbidden

    # The only authority reads it performs, so that "re-read the durable job
    # before acting" cannot quietly become "trust what the last cycle saw".
    assert "package_update_job" in called
    assert "active_package_update_job" in called


def test_the_production_worker_constructs_no_host_control_of_its_own() -> None:
    """One implementation of each host protocol, built in one place.

    The worker receives already-constructed orchestrators. It never imports
    or instantiates an SSH host control, so it cannot acquire a transport
    with different bounds, a different key, or a different forced command
    from the ones the composition root deliberately wired.
    """

    text = (REPO_ROOT / "app/package_update_worker.py").read_text(encoding="utf-8")
    for forbidden in (
        "SshPackageUpdateSnapshotHostControl",
        "SshPackageUpdateExecutionHostControl",
        "SshPackageUpdateMutationHostControl",
        "SshPackageUpdateRollbackHostControl",
        "SshPackageUpdateHealthHostControl",
        "subprocess",
        "Popen",
        "ssh",
        "pct exec",
    ):
        assert forbidden not in text, forbidden


def test_every_privileged_host_control_is_built_exactly_once() -> None:
    """Five stages, five constructions, five dedicated keys.

    A single shared privileged transport would be a single shared privilege
    boundary. The composition root builds each stage's own existing SSH host
    control exactly once, and hands each a DIFFERENT private key -- the key
    is what selects which forced command the connection may run.
    """

    text = (REPO_ROOT / "app/inventory_runtime.py").read_text(encoding="utf-8")
    for symbol in (
        "SshPackageUpdateSnapshotHostControl(",
        "SshPackageUpdateExecutionHostControl(",
        "SshPackageUpdateMutationHostControl(",
        "SshPackageUpdateRollbackHostControl(",
        "SshPackageUpdateHealthHostControl(",
    ):
        assert text.count(symbol) == 1, symbol
    for key_field in (
        "boundary.snapshot_private_key_path",
        "boundary.execution_private_key_path",
        "boundary.mutation_private_key_path",
        "boundary.rollback_private_key_path",
        "boundary.health_private_key_path",
    ):
        assert text.count(key_field) == 1, key_field

    # And the config layer refuses a configuration that points two boundaries
    # at one key, rather than trusting the deployment to have got it right.
    config = (REPO_ROOT / "app/inventory_runtime_config.py").read_text(encoding="utf-8")
    assert "each package-update host-control boundary requires its own" in config



#: The five privileged boundaries production activation deploys, as
#: (kind, helper source file) -- one root-owned forced command each.
_DEPLOYED_UPDATE_BOUNDARIES = (
    ("snapshot", "hubinet-package-snapshot-helper.py"),
    ("execution", "hubinet-package-update-helper.py"),
    ("mutation", "hubinet-package-mutation-helper.py"),
    ("rollback", "hubinet-package-rollback-helper.py"),
    ("health", "hubinet-package-health-helper.py"),
)


def test_bootstrap_deploys_five_separate_dedicated_key_boundaries() -> None:
    """Five helpers, five keys, five forced commands -- never one of each.

    Merging these into one multifunction root helper would collapse "create a
    snapshot", "mutate packages", and "roll a guest back" into a single
    privilege. Sharing one key across two forced-command entries would do the
    same thing more quietly, because the key is what selects the command.
    """

    boundaries = (REPO_ROOT / "deploy/lib/bootstrap-update-boundaries.sh").read_text(
        encoding="utf-8"
    )
    for kind, source_name in _DEPLOYED_UPDATE_BOUNDARIES:
        assert source_name in boundaries, source_name
        assert f"id_ed25519_{kind}" in boundaries, kind

    # Every entry carries the same hardening the scan boundary already has,
    # and each names its OWN helper path as the forced command.
    assert (
        'command="%s",no-port-forwarding,no-agent-forwarding,'
        "no-X11-forwarding,no-pty %s %s %s" in boundaries.replace("\\\n", "")
        or 'no-port-forwarding,no-agent-forwarding,no-X11-forwarding,no-pty' in boundaries
    )
    assert "update_boundary_helper_path" in boundaries

    # Acceptance is structural refusal only. Bootstrap must never create a
    # snapshot, change a package, roll anything back, or probe a workload.
    assert "_accept_update_boundaries" in boundaries
    for forbidden in (
        "ensure_pre_update_snapshot_submitted",
        "execute_exact_package_mutation",
        "submit_same_job_rollback",
        "evaluate_health_contract",
        "apt-get",
    ):
        assert forbidden not in boundaries, forbidden


def test_the_deployed_pve_api_role_remains_the_exact_audit_pair() -> None:
    """Activating workload mutation broadens NO PVE API privilege.

    Every mutation runs host-local behind a root-owned forced command, so the
    inventory API identity never needs one. `VM.Snapshot` and
    `VM.Snapshot.Rollback` must appear in no deployment script at all -- the
    provisioned role stays exactly `Sys.Audit,VM.Audit`.
    """

    for path in sorted((REPO_ROOT / "deploy").rglob("*")):
        if path.is_file() and path.suffix in (".sh", ".py"):
            text = path.read_text(encoding="utf-8")
            assert "VM.Snapshot" not in text, path
            assert "VM.Snapshot.Rollback" not in text, path
            assert "VM.Allocate" not in text, path
            assert "VM.Config" not in text, path


def test_a_failed_activation_update_leaves_no_new_privileged_access_path() -> None:
    """Rollback removes exactly the boundaries this run created.

    A product update that creates a key, an `authorized_keys` entry, and a
    root-owned mutation helper and then fails must remove all three. The
    reverse is equally load-bearing: it must remove ONLY what it created, so
    an unrelated operator key and a Hubinet entry from the original bootstrap
    both survive untouched.
    """

    text = (REPO_ROOT / "deploy/lib/update-boundaries.sh").read_text(encoding="utf-8")
    assert "update_boundaries_rollback" in text
    assert "_update_boundary_deauthorize" in text
    # The journal marker is written BEFORE the artifact exists, so a crash in
    # between still leaves rollback a record of what to undo.
    assert "update_journal_record update-boundary-created" in text
    # Removal is filtered by this run's exact marker, never by a broad match.
    assert 'awk -v marker=" ${marker}" \'index($0, marker) == 0 { print }\'' in text
    # An existing journal directory is never destroyed to tidy up: it may
    # hold another operation's durable at-most-once evidence.
    assert "update-boundary-journal-created" in text

    activate = (REPO_ROOT / "deploy/lib/update-activate.sh").read_text(encoding="utf-8")
    assert "update_boundaries_rollback" in activate


def test_the_product_updater_refuses_while_a_workload_job_is_active() -> None:
    """The fence, and where it sits.

    It runs in Phase U2 -- classification -- which is strictly before
    staging, before the service is stopped, and before any helper, key,
    config file, or unit is touched. There is deliberately no bypass flag.
    """

    plan = (REPO_ROOT / "deploy/lib/update-plan.sh").read_text(encoding="utf-8")
    assert "package_update_active" in plan
    assert "refusing to update: package update job" in plan
    for forbidden in ("--force-active-job", "force_active_job", "FORCE_ACTIVE_JOB"):
        assert forbidden not in plan, forbidden
    updater = (REPO_ROOT / "deploy/update-proxmox-0.5.sh").read_text(encoding="utf-8")
    for forbidden in ("--force-active-job", "force_active_job"):
        assert forbidden not in updater, forbidden

    # The fence is part of classification, which the orchestration runs
    # before update_plan_confirm and therefore before update_stage_all.
    assert plan.index("_update_pre_probe\n") < plan.index("update_plan_print()")
    assert updater.index("update_plan_classify") < updater.index("update_stage_all")


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



def test_r0_ha_transport_defines_an_exact_operator_method_allowlist() -> None:
    """The HA transport's method set is an exact allowlist.

    It now includes four explicit operator update controls, and that is the
    point of pinning it: each one is a named verb an operator invokes, and
    none of them is a generic request builder. There is no method here
    through which Home Assistant could name a VMID, a package, a snapshot, a
    probe, or a helper operation.
    """

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
        "fetch_health_contract",
        "replace_health_contract",
        "clear_health_contract",
        "start_package_update",
        "fetch_package_update",
        "resume_package_update",
        "rollback_package_update",
    }
    exact_private_methods = {
        "_get",
        "_put",
        "_decode",
        "_health_contract_request",
        "_package_update_request",
        "_decode_package_update",
    }
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
# The execution-time APT plan equality gate, and what a caller may say.
# ---------------------------------------------------------------------------



def test_the_update_api_accepts_no_execution_material_from_a_caller() -> None:
    """The whole caller-controlled surface of the update lifecycle is one UUID.

    `PackageUpdateStartRequest` has exactly one field. The resume and
    rollback bodies have none at all -- an operator selects a RESOURCE and
    the backend resolves the job, the plan, the snapshot, and the target from
    durable authority. `extra="forbid"` means a caller who sends anything
    else gets a 422 rather than having it quietly ignored.
    """

    tree = ast.parse((REPO_ROOT / "app/inventory_runtime.py").read_text(encoding="utf-8"))
    models = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef)
        and node.name.startswith("PackageUpdate")
        and node.name.endswith(("Request", "RequestBody"))
    }
    assert set(models) == {
        "PackageUpdateStartRequest",
        "PackageUpdateResumeRequest",
        "PackageUpdateRollbackRequestBody",
    }

    def fields(node: ast.ClassDef) -> list[str]:
        return [
            item.target.id
            for item in node.body
            if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name)
        ]

    assert fields(models["PackageUpdateStartRequest"]) == ["request_id"]
    assert fields(models["PackageUpdateResumeRequest"]) == []
    assert fields(models["PackageUpdateRollbackRequestBody"]) == []
    for name, node in models.items():
        source = ast.get_source_segment(
            (REPO_ROOT / "app/inventory_runtime.py").read_text(encoding="utf-8"), node
        )
        assert 'extra="forbid"' in source, name

    # Belt and braces at the text level: no field of ANY of these names may
    # appear as a request model attribute anywhere in the route module.
    forbidden_fields = (
        "vmid",
        "node",
        "package_name",
        "package_version",
        "architecture",
        "snapshot_name",
        "snapshot_id",
        "operation_id",
        "rollback_target",
        "checkpoint",
        "stage",
        "command",
        "argv",
        "shell",
        "script",
        "host",
        "helper",
    )
    for name, node in models.items():
        for field_name in fields(node):
            assert field_name not in forbidden_fields, (name, field_name)


def test_the_execution_gate_still_runs_before_every_mutation() -> None:
    """Composition must not skip the gate because mutation re-proves things.

    The mutation stage does re-prove exact material in the same transaction
    that commits its write-ahead boundary -- and that is not a reason to drop
    the cheap, entirely non-mutating refusal that keeps a drifted plan from
    ever reaching the stage that owns the real package command.
    """

    tree = ast.parse(
        (REPO_ROOT / "app/package_update_worker.py").read_text(encoding="utf-8")
    )
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.FunctionDef)
            and node.name == "_run_execution_gate_then_mutation"
        ):
            called = _called_names(node)
            assert "run_package_update_execution_gate" in called
            assert "execute_job_owned_mutation" in called
            return
    raise AssertionError("_run_execution_gate_then_mutation not found")


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
# Crash-safe real package mutation, and restart safety.
# ---------------------------------------------------------------------------



def test_the_worker_never_resubmits_a_mutation_after_a_restart() -> None:
    """An armed mutation is RECOVERED, never re-driven.

    The asymmetry lives in the mutation stage: an invocation that finds the
    job at `snapshot_confirmed` may prepare, arm, and submit once; one that
    finds it already armed may only observe. The worker must route to the
    recovery entry point for the armed checkpoint, or a restart would become
    a second destructive submission.
    """

    tree = ast.parse(
        (REPO_ROOT / "app/package_update_worker.py").read_text(encoding="utf-8")
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_run_mutation_recovery":
            called = _called_names(node)
            assert "recover_job_owned_mutation" in called
            assert "execute_job_owned_mutation" not in called
            break
    else:  # pragma: no cover - the function must exist
        raise AssertionError("_run_mutation_recovery not found")

    source = (REPO_ROOT / "app/package_update_worker.py").read_text(encoding="utf-8")
    assert (
        "if checkpoint is PackageUpdateCheckpoint.MUTATION_MAY_HAVE_STARTED:\n"
        "            return self._run_mutation_recovery(job)" in source
    )


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
# Same-job rollback: explicit, durable before acknowledgement, never automatic.
# ---------------------------------------------------------------------------



def test_no_automatic_rollback_exists_on_any_production_path() -> None:
    """NO AUTO-ROLLBACK, proven the same way NO AUTO-UPDATE is.

    `arm_package_update_rollback` is the only transition that can put a job
    at `rollback_may_have_started`, and reaching that checkpoint is the only
    way the rollback stage will ever submit. So the complete set of
    production callers of that method is the complete set of ways a rollback
    can begin -- and it has exactly one member: the authenticated `POST
    .../package-update/rollback` route.

    A failed mutation, an unproven mutation, a FAILED health verdict, and an
    UNKNOWN health verdict each leave the job ACTIVE and rollback-CAPABLE.
    None of them calls anything here.
    """

    runtime_source = (REPO_ROOT / "app/inventory_runtime.py").read_text(encoding="utf-8")
    assert _innermost_callers(runtime_source, "arm_package_update_rollback") == [
        "rollback_package_update"
    ]

    for rel_path in (
        "app/package_update_worker.py",
        "app/package_update_health.py",
        "app/package_update_mutation.py",
        "app/package_update_snapshot.py",
        "app/package_update_execution.py",
        "app/inventory_scheduler.py",
        "app/package_scan_scheduler.py",
        "custom_components/hubinet_ops/coordinator.py",
        "custom_components/hubinet_ops/sensor.py",
    ):
        text = _code(REPO_ROOT / rel_path)
        assert "arm_package_update_rollback" not in text, rel_path


def test_the_operator_rollback_request_is_durable_before_it_is_acknowledged() -> None:
    """Accepting a rollback on an in-memory wakeup would be a lie.

    The route's order is exact and load-bearing: resolve the one applicable
    ACTIVE job, obtain a FRESH canonical listing through the existing
    read-only inspection, arm the write-ahead boundary, and only THEN
    respond. A crash after the response therefore leaves a durable state
    startup recovery understands, and a crash before arming leaves a job that
    was never told to roll back.
    """

    source = (REPO_ROOT / "app/inventory_runtime.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "rollback_package_update":
            body = ast.get_source_segment(source, node)
            break
    else:  # pragma: no cover - the route must exist
        raise AssertionError("rollback_package_update route not found")

    observe = body.index("inspect_job_snapshot_state")
    arm = body.index("arm_package_update_rollback")
    acknowledge = body.index("status_code=202")
    wake = body.index("runtime.worker.wake()")
    assert observe < arm < wake < acknowledge or observe < arm < acknowledge
    assert arm < wake, "the durable boundary must precede the worker wake"

    # The operator selects a resource. Nothing in this route reads a
    # caller-supplied snapshot, target, or operation.
    for forbidden in ("body.snapshot", "body.target", "body.operation", "body.vmid"):
        assert forbidden not in body, forbidden


def test_the_worker_only_ever_continues_an_already_armed_rollback() -> None:
    """It enters the rollback stage at one checkpoint, and cannot arm one."""

    source = (REPO_ROOT / "app/package_update_worker.py").read_text(encoding="utf-8")
    assert (
        "if job.checkpoint is not PackageUpdateCheckpoint.ROLLBACK_MAY_HAVE_STARTED:\n"
        '            return self._stopped(job, "no_automatic_continuation")' in source
    )
    assert "arm_package_update_rollback" not in _code(
        REPO_ROOT / "app/package_update_worker.py"
    )


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


def test_only_the_authority_may_write_a_health_verdict() -> None:
    """Health execution exists now, so the invariant changes shape.

    Nothing may claim a verdict except the ONE authority boundary that proves
    it: no other module -- including the dark health orchestrator, its host
    control, and every other dark stage -- may write `health_started_at`,
    `health_outcome`, `health_completed_at`, the health checkpoints, or
    `status='succeeded'`. Those are transitions the authority owns, and a
    caller supplies typed observations to it, never a verdict.
    """

    verdict_writes = (
        "health_started_at=",
        "health_completed_at=",
        "health_outcome=",
        "status='succeeded'",
        "checkpoint='health_started'",
        "checkpoint='health_completed'",
    )
    for rel_path in (
        "app/package_update_health.py",
        "app/package_update_health_host_control.py",
        "app/package_update_rollback.py",
        "app/package_update_mutation.py",
        "app/package_update_snapshot.py",
        "app/package_update_execution.py",
        "app/inventory_runtime.py",
        "custom_components/hubinet_ops/services.py",
        "custom_components/hubinet_ops/transport_http.py",
        "deploy/hubinet-package-health-helper.py",
    ):
        text = _executable_source(REPO_ROOT / rel_path)
        for forbidden in verdict_writes:
            assert forbidden not in text, (rel_path, forbidden)


# ---------------------------------------------------------------------------
# Job-bound healthcheck execution: one attempt per wake, no retry policy.
# ---------------------------------------------------------------------------



def test_the_worker_invents_no_health_retry_policy() -> None:
    """PR #73 invented no retry policy, and activation does not either.

    One wake performs at most one truthful, read-only health attempt. An
    UNKNOWN verdict leaves the job ACTIVE at `health_started` with its
    snapshot and rollback authority intact, and the worker idle for it. There
    is no interval, no backoff, no grace period, no attempt count, and no
    threshold anywhere in the production composition -- production liveness
    comes from an operator invoking `resume_update`, not from a timer.
    """

    for rel_path in (
        "app/package_update_worker.py",
        "app/package_update_health.py",
        "app/inventory_runtime.py",
    ):
        text = _code(REPO_ROOT / rel_path)
        for forbidden in (
            "retry_count",
            "max_retries",
            "max_attempts",
            "attempt_count",
            "backoff",
            "grace_period",
            "health_threshold",
            "retry_interval",
            "retry_after",
        ):
            assert forbidden not in text, (rel_path, forbidden)

    tree = ast.parse(
        (REPO_ROOT / "app/package_update_worker.py").read_text(encoding="utf-8")
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_run_health":
            # Exactly one evaluation call per invocation, and no loop around it.
            assert not any(
                isinstance(item, (ast.For, ast.While)) for item in ast.walk(node)
            )
            assert "evaluate_job_health" in _called_names(node)
            return
    raise AssertionError("_run_health not found")


def test_the_worker_stop_reason_vocabulary_is_closed_and_means_stop() -> None:
    """A stop is an idle worker, never a scheduled retry.

    Every reason the worker stops progressing a still-ACTIVE job is a member
    of one closed set, and no member of it names an interval, a deadline, or
    a compensating action. That is what makes "the worker stops and waits to
    be asked" a checkable property rather than a description.
    """

    spec = importlib.util.spec_from_file_location(
        "_worker_stop_reasons", REPO_ROOT / "app/package_update_worker.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["_worker_stop_reasons"] = module
    spec.loader.exec_module(module)
    reasons = module.PACKAGE_UPDATE_WORKER_STOP_REASONS
    assert isinstance(reasons, frozenset) and reasons
    for reason in reasons:
        for forbidden in ("retry", "backoff", "seconds", "interval", "rollback_now"):
            assert forbidden not in reason, reason
    # The two health outcomes that must never advance anything.
    assert {"health_failed", "health_unknown"} <= reasons


def test_health_execution_never_calls_the_rollback_stage() -> None:
    """No automatic health-triggered compensation, at the source level.

    This product has made no compensation policy, so a failing health verdict
    reports and stops. Nothing in the health stage may arm, submit, seal, or
    even inspect a rollback -- and there is no retry count, grace period,
    threshold, majority, or OR logic anywhere in it either.
    """

    forbidden = (
        "arm_package_update_rollback",
        "roll_back_to_job_snapshot",
        "submit_same_job_rollback",
        "seal_rollback_never_submitted",
        "inspect_rollback_state",
        "execute_rollback_submission_if_current",
        "PackageUpdateRollbackOrchestrator",
        "app.package_update_rollback",
        "retry_count",
        "grace_period",
        "health_threshold",
    )
    for rel_path in (
        "app/package_update_health.py",
        "app/package_update_health_host_control.py",
        "deploy/hubinet-package-health-helper.py",
    ):
        text = (REPO_ROOT / rel_path).read_text(encoding="utf-8")
        for symbol in forbidden:
            assert symbol not in text, (rel_path, symbol)


def test_the_health_helper_exposes_exactly_one_read_only_operation() -> None:
    """One typed operation, and nothing that can change anything.

    It is deliberately the narrowest helper in this repository: no journal,
    no lease, no seal, no submission -- because a read has no at-most-once
    property to protect.
    """

    text = (REPO_ROOT / "deploy/hubinet-package-health-helper.py").read_text(
        encoding="utf-8"
    )
    assert '"evaluate_health_contract"' in text
    executable = _executable_source(REPO_ROOT / "deploy/hubinet-package-health-helper.py")
    for forbidden in (
        "shell=True",
        "os.system",
        "sh -c",
        "apt-get",
        "dpkg",
        "pct destroy",
        "pct start",
        "pct stop",
        "pct rollback",
        "snapshot",
        "pvesh create",
        "pvesh delete",
        "pvesh set",
        "systemctl start",
        "systemctl stop",
        "systemctl restart",
        "is-active",
        "docker start",
        "docker stop",
        "docker restart",
        "docker rm",
        "docker exec",
        "docker run",
    ):
        assert forbidden not in executable, forbidden


def test_the_health_helper_builds_only_fixed_argv_around_a_data_target() -> None:
    """A probe target is DATA, and the commands around it are constants.

    Asserted structurally over the AST rather than by substring: every string
    inside the three probe evaluators must be a literal this file owns, so a
    target can never be concatenated, formatted, or templated into command
    text. The one interpolation allowed anywhere near Docker is the exact
    `/`-prefixed name comparison, which is a CHECK on the answer, not part of
    a command.
    """

    path = REPO_ROOT / "deploy/hubinet-package-health-helper.py"
    module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    evaluators = {
        node.name: node
        for node in module.body
        if isinstance(node, ast.FunctionDef)
        and node.name.startswith("evaluate_")
    }
    assert set(evaluators) == {
        "evaluate_systemd_unit_active",
        "evaluate_docker_container_running",
        "evaluate_docker_container_healthy",
    }
    for name, node in evaluators.items():
        for inner in ast.walk(node):
            # An f-string or a `%`/`.format()` call building a command would
            # be exactly the interpolation this stage forbids.
            assert not isinstance(inner, ast.JoinedStr) or name.startswith(
                "evaluate_docker"
            ), name
            if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute):
                assert inner.func.attr != "format", name

    # The Docker template is a module-level constant, not built anywhere.
    constants = {
        target.id: node
        for node in module.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    template = constants["DOCKER_INSPECT_FORMAT"]
    assert isinstance(template.value, (ast.Constant, ast.JoinedStr, ast.BinOp))
    assert "{{.Name}}" in _load_health_helper().DOCKER_INSPECT_FORMAT


def _load_health_helper():
    spec = importlib.util.spec_from_file_location(
        "hubinet_package_health_helper_r0",
        REPO_ROOT / "deploy" / "hubinet-package-health-helper.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_the_health_helper_is_the_only_file_that_can_probe_a_workload() -> None:
    """Every other helper keeps its own, unrelated promise."""

    health = REPO_ROOT / "deploy/hubinet-package-health-helper.py"
    assert health.exists()
    for other in (
        "deploy/hubinet-package-scan-helper.py",
        "deploy/hubinet-package-snapshot-helper.py",
        "deploy/hubinet-package-update-helper.py",
        "deploy/hubinet-package-mutation-helper.py",
        "deploy/hubinet-package-rollback-helper.py",
    ):
        text = (REPO_ROOT / other).read_text(encoding="utf-8")
        for forbidden in (
            "evaluate_health_contract",
            "systemctl",
            "docker",
        ):
            assert forbidden not in text, (other, forbidden)


def _shell_code(path) -> str:
    """Return one shell script with its comment lines removed.

    Same reason as :func:`_executable_source`: the deployment scripts document
    their own negative space ("this never rotates the scan boundary"), and a
    raw substring scan would punish writing that down.
    """

    return "\n".join(
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )


def _code(path) -> str:
    """Executable text of a Python module or a shell script, prose removed."""

    return (
        _executable_source(path)
        if path.suffix == ".py"
        else _shell_code(path)
    )


def _executable_source(path) -> str:
    """Return one module's source with its docstrings and comments removed.

    Negative documentation is the point of several modules in this repository
    -- they say, in prose, exactly what they must never do. A scan that could
    not tell that prose from code would punish writing it down.
    """

    import io
    import tokenize

    source = path.read_text(encoding="utf-8")
    lines = source.splitlines()
    tree = ast.parse(source, filename=str(path))
    blanked = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", ())
        if not body:
            continue
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
            if isinstance(first.value.value, str):
                blanked.update(range(first.lineno, (first.end_lineno or first.lineno) + 1))
    kept = [
        "" if index + 1 in blanked else line for index, line in enumerate(lines)
    ]
    without_docstrings = "\n".join(kept)
    tokens = tokenize.generate_tokens(io.StringIO(without_docstrings).readline)
    return "\n".join(
        token.string for token in tokens if token.type != tokenize.COMMENT
    )



def test_health_contracts_are_configuration_and_never_execution() -> None:
    """The contract layer declares what healthy means; it never checks it.

    Unchanged by production activation: the layer that stores a contract is
    still a different layer from the one that evaluates it, and the API
    surface that edits a contract still carries no command-shaped field.
    """

    probe_execution = (
        "is-active",
        "systemctl",
        "docker",
        "pct",
        "subprocess",
        "Popen",
        "healthcheck",
    )
    job_lifecycle = (
        "health_started",
        "issue_package_update_job",
        "package_update_jobs",
        "succeeded",
        "rollback",
    )
    for rel_path in (
        "app/inventory/health_contract.py",
        "custom_components/hubinet_ops/contract/health_contract_validation.py",
    ):
        text = _executable_source(REPO_ROOT / rel_path)
        for forbidden in probe_execution + job_lifecycle:
            assert forbidden not in text, (rel_path, forbidden)

    # No caller-supplied command material anywhere on the operator surface --
    # health contracts or update controls alike.
    for rel_path in (
        "app/inventory/health_contract.py",
        "app/inventory_runtime.py",
        "custom_components/hubinet_ops/services.py",
        "custom_components/hubinet_ops/transport_http.py",
        "custom_components/hubinet_ops/services.yaml",
    ):
        text = (
            (REPO_ROOT / rel_path).read_text(encoding="utf-8")
            if rel_path.endswith(".yaml")
            else _code(REPO_ROOT / rel_path)
        )
        for forbidden in (
            '"command"',
            '"argv"',
            '"shell"',
            '"script"',
            '"executable"',
            '"working_directory"',
            '"environment"',
            "command:",
            "argv:",
        ):
            assert forbidden not in text, (rel_path, forbidden)



def test_exactly_one_update_worker_exists_and_no_per_resource_pool() -> None:
    """One bounded worker, one thread, no pool.

    A per-resource worker pool would be concurrency this product has already
    made impossible: the durable authority permits exactly one active job
    globally. The in-process cycle lock only stops this one worker running
    two cycles at once, and is never the thing that stops two mutations.
    """

    existing = {path.name for path in (REPO_ROOT / "app").glob("*.py")}
    for forbidden in (
        "health_scheduler.py",
        "health_worker.py",
        "package_update_scheduler.py",
        "package_update_health_scheduler.py",
    ):
        assert forbidden not in existing, forbidden
    assert "package_update_worker.py" in existing

    worker = (REPO_ROOT / "app/package_update_worker.py").read_text(encoding="utf-8")
    assert worker.count("threading.Thread(") == 1
    for forbidden in (
        "ThreadPoolExecutor",
        "ProcessPoolExecutor",
        "multiprocessing",
        "workers=",
        "max_workers",
        "for resource in",
    ):
        assert forbidden not in worker, forbidden

    runtime = (REPO_ROOT / "app/inventory_runtime.py").read_text(encoding="utf-8")
    assert runtime.count("PackageUpdateWorker(") == 1


def test_home_assistant_coordinator_polling_can_never_start_an_update() -> None:
    """HA is presentation and controlled input; never an authority.

    The coordinator polls one published snapshot. It holds no reference to
    any operator action, no update transport method, and no worker -- so a
    refresh, however frequent, cannot begin, resume, or roll back anything.
    """

    coordinator = (REPO_ROOT / "custom_components/hubinet_ops/coordinator.py").read_text(
        encoding="utf-8"
    )
    for forbidden in (
        "start_package_update",
        "resume_package_update",
        "rollback_package_update",
        "async_start_package_update",
        "async_resume_package_update",
        "async_rollback_package_update",
        "issue_package_update_job",
        "PackageUpdateWorker",
    ):
        assert forbidden not in coordinator, forbidden

    # The three mutating operator actions live only in the actions module,
    # and each is registered as a service a person invokes.
    services = (REPO_ROOT / "custom_components/hubinet_ops/services.py").read_text(
        encoding="utf-8"
    )
    for symbol in (
        "async_start_package_update",
        "async_resume_package_update",
        "async_rollback_package_update",
    ):
        assert services.count(symbol) == 1, symbol


def test_home_assistant_entities_carry_no_plan_events_or_probe_material() -> None:
    """A concise state entity, not a replica of the job.

    The per-resource update-job entity publishes a state and a handful of
    bounded identity attributes. The event log, the frozen package rows, and
    the per-probe health results are response data from an explicitly invoked
    action and must never become entity attributes.
    """

    sensor = (REPO_ROOT / "custom_components/hubinet_ops/sensor.py").read_text(
        encoding="utf-8"
    )
    for forbidden in (
        "events",
        "packages",
        "probes",
        "probe_results",
        "health_probe_results",
        "stdout",
        "stderr",
    ):
        assert forbidden not in sensor, forbidden

    # The published snapshot itself carries only the concise summary.
    publication = (REPO_ROOT / "app/inventory/publication.py").read_text(encoding="utf-8")
    for forbidden in (
        "package_update_job_events",
        "package_update_job_packages",
        "package_update_job_health_probe_results",
    ):
        assert forbidden not in publication, forbidden
