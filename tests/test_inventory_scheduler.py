"""WAVE R0-B Family 3 -- discovery runtime, scheduler, restart recovery.

Covers §28 tests #20, #21, #22, #23, #24, #25, #26, #41, #42 of
docs/architecture/0.5-r0-read-only-runtime-activation.md. No real network
access anywhere in this file: every PVE request is intercepted in-process
by ``httpx.MockTransport``.
"""

from __future__ import annotations

from pathlib import Path
import threading
import time

import httpx
import pytest

import app.inventory_scheduler as sched
from app.inventory import (
    AuthorityConflict,
    DiscoveryRunLifecycle,
    InventoryAuthority,
    InventoryAuthorityStore,
    InventoryPublication,
    PROVIDER_CONTRACT_VERSION,
)
from app.inventory_pve_transport import ProxmoxHttpTransport, _PVE_API_PREFIX
from app.inventory_runtime_config import bootstrap_or_reconcile_source, parse_r0_runtime_config

VALID_ENV = {
    "HUBINET_OPS_R0_PVE_TOKEN": "root@pam!hubinet-ops=00000000-0000-0000-0000-000000000000",
    "HUBINET_OPS_R0_API_TOKEN": "a" * 32,
}


def _raw(*, ca_bundle_path: str | None = None):
    return {
        "source": {
            "display_name": "Home Proxmox",
            "provider_kind": "proxmox_ve",
            "pve_endpoint": "https://pve.example.internal:8006",
            "freshness_duration_seconds": 300,
            "credential_reference": "secret://v1",
            "pve_token_env": "HUBINET_OPS_R0_PVE_TOKEN",
            "tls": {"verify": True, "ca_bundle_path": ca_bundle_path},
        },
        "runtime": {
            "authority_db_path": "/var/lib/hubinet-ops/authority.db",
            "api_token_env": "HUBINET_OPS_R0_API_TOKEN",
        },
    }


def _config(*, ca_bundle_path: str | None = None):
    return parse_r0_runtime_config(_raw(ca_bundle_path=ca_bundle_path), env=VALID_ENV)


def _store(tmp_path: Path) -> InventoryAuthorityStore:
    return InventoryAuthorityStore(tmp_path / "authority.db")


def _pve_handler(*, guests=(), block: threading.Event | None = None):
    node_names = ("pve-a",)
    permissions = {
        "/": {"Sys.Audit": 1},
        "/access": {"Sys.Audit": 1},
        "/nodes": {"Sys.Audit": 1},
        "/vms": {"VM.Audit": 1},
        "/nodes/pve-a": {"Sys.Audit": 1},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if block is not None:
            block.wait(timeout=5)
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
                        {
                            "type": "node",
                            "id": "node/pve-a",
                            "name": "pve-a",
                            "nodeid": 0,
                            "local": 1,
                            "online": 1,
                        }
                    ]
                },
            )
        if rel == "/access/permissions":
            path = dict(request.url.params).get("path")
            privileges = permissions.get(path, {})
            return httpx.Response(200, json={"data": {path: privileges} if privileges else {}})
        if rel == "/nodes":
            return httpx.Response(200, json={"data": [{"node": n, "status": "online"} for n in node_names]})
        if rel == "/nodes/pve-a/qemu":
            return httpx.Response(200, json={"data": [g for g in guests if g.get("type") == "qemu"]})
        if rel == "/nodes/pve-a/lxc":
            return httpx.Response(200, json={"data": [g for g in guests if g.get("type") == "lxc"]})
        raise AssertionError(f"unexpected PVE request path {rel!r}")

    return handler


def _patch_transport(monkeypatch, handler) -> None:
    def fake_build_transport(run, config):
        return ProxmoxHttpTransport(
            canonical_transport_locator=run.expected_canonical_transport_locator,
            pve_api_token=config.pve_api_token,
            _transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr(sched, "_build_transport", fake_build_transport)


def _bootstrap(tmp_path: Path):
    store = _store(tmp_path)
    authority = InventoryAuthority(store)
    config = _config()
    state = bootstrap_or_reconcile_source(authority, store, config)
    return store, authority, config, state.source.inventory_source_id


# ---------------------------------------------------------------------------
# §28 test #20 -- per-source single-flight
# ---------------------------------------------------------------------------


def test_20_concurrent_issuance_for_the_same_source_loses_cleanly(
    tmp_path: Path, monkeypatch
) -> None:
    store, authority, config, source_id = _bootstrap(tmp_path)
    _patch_transport(monkeypatch, _pve_handler())
    # Simulate a run already in flight (e.g. a slow previous cycle).
    authority.issue_discovery_run(source_id, PROVIDER_CONTRACT_VERSION)

    outcome = sched.run_discovery_cycle(authority, source_id, config)
    assert outcome.status == "conflict"


def test_20_scheduler_run_once_never_overlaps_itself(tmp_path: Path, monkeypatch) -> None:
    store, authority, config, source_id = _bootstrap(tmp_path)
    block = threading.Event()
    _patch_transport(monkeypatch, _pve_handler(block=block))
    scheduler = sched.R0Scheduler(authority, store, config)

    results: list[sched.DiscoveryCycleOutcome] = []
    thread = threading.Thread(target=lambda: results.append(scheduler.run_once()))
    thread.start()
    time.sleep(0.05)  # let the first cycle acquire the in-process lock
    second = scheduler.run_once()
    block.set()
    thread.join(timeout=5)

    assert second.status == "skipped"
    assert results[0].status == "success"


# ---------------------------------------------------------------------------
# §28 test #21 -- process crash after issuance, restart recovery
# ---------------------------------------------------------------------------


def test_21_crash_after_issuance_is_recovered_on_restart(tmp_path: Path, monkeypatch) -> None:
    store, authority, config, source_id = _bootstrap(tmp_path)
    crashed_run = authority.issue_discovery_run(source_id, PROVIDER_CONTRACT_VERSION)
    authority.mark_discovery_run_running(source_id, crashed_run.run_id)
    # Simulate the process dying here -- no finalize call.

    abandoned = sched.perform_startup_recovery(authority, store)
    assert abandoned == (crashed_run.run_id,)

    recovered_run = store.discovery_run(crashed_run.run_id)
    assert recovered_run.lifecycle is DiscoveryRunLifecycle.ABANDONED

    state = store.source_state(source_id)
    assert state.source.active_discovery_run_id is None

    _patch_transport(monkeypatch, _pve_handler())
    outcome = sched.run_discovery_cycle(authority, source_id, config)
    assert outcome.status == "success"


# ---------------------------------------------------------------------------
# §28 test #22 -- abandoned-run restart recovery: sequence non-reuse, no
# fabricated observation
# ---------------------------------------------------------------------------


def test_22_abandoned_run_sequence_is_never_reused_and_nothing_is_fabricated(
    tmp_path: Path, monkeypatch
) -> None:
    store, authority, config, source_id = _bootstrap(tmp_path)
    crashed_run = authority.issue_discovery_run(source_id, PROVIDER_CONTRACT_VERSION)
    crashed_sequence = crashed_run.discovery_run_sequence

    sched.perform_startup_recovery(authority, store)

    publication = InventoryPublication(store, authority)
    view_after_recovery = publication.read()
    assert view_after_recovery.resources == ()
    source_after_recovery = view_after_recovery.sources[0]
    assert source_after_recovery["health"] == "not_yet_observed"
    assert source_after_recovery["freshness"] == "not_yet_observed"

    _patch_transport(monkeypatch, _pve_handler())
    outcome = sched.run_discovery_cycle(authority, source_id, config)
    assert outcome.status == "success"

    runs = store.list_discovery_runs(source_id)
    sequences = [run.discovery_run_sequence for run in runs]
    assert sequences == sorted(sequences)
    assert len(sequences) == len(set(sequences))
    next_run = next(r for r in runs if r.discovery_run_sequence == crashed_sequence + 1)
    assert next_run.lifecycle is DiscoveryRunLifecycle.COMPLETED

    # The abandoned run itself must never be re-finalizable (a late worker
    # from the "crashed" attempt trying to finalize after restart recovery
    # already reclaimed ownership must lose cleanly).
    from app.inventory import BaselineCompleteness, DiscoveryRunCompletionEvidence

    with pytest.raises(AuthorityConflict):
        authority.finalize_failed_discovery_run(
            source_id,
            crashed_run.run_id,
            completion_evidence=DiscoveryRunCompletionEvidence(
                baseline_completeness=BaselineCompleteness.SOURCE_UNAVAILABLE
            ),
            reason="late worker",
        )


# ---------------------------------------------------------------------------
# §28 test #23 -- source epoch 0 discovery works (not_yet_attested)
# ---------------------------------------------------------------------------


def test_23_discovery_succeeds_at_epoch_zero_not_yet_attested(
    tmp_path: Path, monkeypatch
) -> None:
    store, authority, config, source_id = _bootstrap(tmp_path)
    attestation = store.attestation_state(source_id)
    assert attestation.attestation_status.value == "not_yet_attested"
    assert attestation.source_attestation_epoch == 0

    _patch_transport(monkeypatch, _pve_handler(guests=({"vmid": 100, "type": "qemu", "name": "vm1", "status": "running"},)))
    outcome = sched.run_discovery_cycle(authority, source_id, config)
    assert outcome.status == "success"

    publication = InventoryPublication(store, authority)
    view = publication.read()
    assert len(view.resources) == 1
    assert view.resources[0]["security_continuity"] == "unverified"


# ---------------------------------------------------------------------------
# §28 test #24/#25/#26 -- adversarial: no forbidden authority calls
# ---------------------------------------------------------------------------

_FORBIDDEN_AUTHORITY_METHODS = (
    "enroll_source_attestation",
    "reattest_source",
    "accept_source_attestation_anchor_change",
    "revoke_source_attestation",
    "check_candidate_attestation",
    "confirm_class_c_resource_removal",
)

_ENDPOINT_ACTIVATION_SHAPED_NAMES = (
    "activate_endpoint",
    "replace_endpoint",
    "failover",
    "promote_candidate",
    "promote_endpoint",
)


def test_24_25_26_full_cycle_never_calls_any_forbidden_authority_method(
    tmp_path: Path, monkeypatch
) -> None:
    store, authority, config, source_id = _bootstrap(tmp_path)
    _patch_transport(monkeypatch, _pve_handler(guests=({"vmid": 100, "type": "qemu", "name": "vm1", "status": "running"},)))

    call_counts = {name: 0 for name in _FORBIDDEN_AUTHORITY_METHODS}
    for name in _FORBIDDEN_AUTHORITY_METHODS:
        original = getattr(InventoryAuthority, name)

        def _make_spy(method_name, bound_original):
            def _spy(self, *args, **kwargs):
                call_counts[method_name] += 1
                return bound_original(self, *args, **kwargs)

            return _spy

        monkeypatch.setattr(InventoryAuthority, name, _make_spy(name, original))

    # Success cycle, then a cycle over a now-missing guest (presence should
    # become "missing", never "confirmed_removed").
    first = sched.run_discovery_cycle(authority, source_id, config)
    assert first.status == "success"

    _patch_transport(monkeypatch, _pve_handler(guests=()))
    second = sched.run_discovery_cycle(authority, source_id, config)
    assert second.status == "success"

    view = InventoryPublication(store, authority).read()
    assert view.resources[0]["presence"] == "missing"
    assert view.resources[0]["presence"] != "confirmed_removed"

    assert all(count == 0 for count in call_counts.values()), call_counts


def test_25_no_endpoint_activation_shaped_method_exists_on_the_authority() -> None:
    # Defensive/structural: there is nothing to call in the first place.
    for name in _ENDPOINT_ACTIVATION_SHAPED_NAMES:
        assert not hasattr(InventoryAuthority, name), name


# ---------------------------------------------------------------------------
# §28 test #41 -- scheduler shutdown does not force-abandon an in-flight run
# ---------------------------------------------------------------------------


def test_41_shutdown_does_not_force_abandon_an_in_flight_run(
    tmp_path: Path, monkeypatch
) -> None:
    store, authority, config, source_id = _bootstrap(tmp_path)
    block = threading.Event()
    _patch_transport(monkeypatch, _pve_handler(block=block))
    scheduler = sched.R0Scheduler(authority, store, config, interval_seconds=3600)
    scheduler.start()
    # Give the background thread a moment to issue the run and block inside
    # the (fake) transport call.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        state = store.source_state(source_id)
        if state.source.active_discovery_run_id is not None:
            break
        time.sleep(0.02)
    else:
        pytest.fail("scheduler never issued a discovery run")

    active_run_id = store.source_state(source_id).source.active_discovery_run_id
    scheduler.stop(grace_seconds=0.2)

    # Must still be running/issued, never force-abandoned by stop().
    run = store.discovery_run(active_run_id)
    assert run.lifecycle in (DiscoveryRunLifecycle.ISSUED, DiscoveryRunLifecycle.RUNNING)
    assert store.source_state(source_id).source.active_discovery_run_id == active_run_id

    block.set()  # let the blocked request complete so the thread can exit cleanly


# ---------------------------------------------------------------------------
# §28 test #42 -- startup ordering: recovery -> config drift -> scheduling
# ---------------------------------------------------------------------------


def test_42_recovery_precedes_config_drift_precedes_scheduling(
    tmp_path: Path, monkeypatch
) -> None:
    store = _store(tmp_path)
    authority = InventoryAuthority(store)
    config = _config()
    order: list[str] = []

    original_recovery = sched.perform_startup_recovery

    def spy_recovery(a, s):
        order.append("recovery")
        return original_recovery(a, s)

    monkeypatch.setattr(sched, "perform_startup_recovery", spy_recovery)

    import app.inventory_runtime_config as cfgmod

    original_bootstrap = cfgmod.bootstrap_or_reconcile_source

    def spy_bootstrap(a, s, c):
        order.append("config")
        return original_bootstrap(a, s, c)

    monkeypatch.setattr(cfgmod, "bootstrap_or_reconcile_source", spy_bootstrap)

    class SpyScheduler(sched.R0Scheduler):
        def __init__(self, *args, **kwargs):
            order.append("scheduler_constructed")
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(sched, "R0Scheduler", SpyScheduler)

    scheduler = sched.bootstrap_and_start_r0_runtime(authority, store, config, start=False)

    assert order == ["recovery", "config", "scheduler_constructed"]
    assert isinstance(scheduler, sched.R0Scheduler)


# ---------------------------------------------------------------------------
# Corrective pass, P2 Finding 1 -- post-issuance exception must never
# strand the active run
# ---------------------------------------------------------------------------


def test_finding1_ca_bundle_removed_after_startup_does_not_strand_the_run(
    tmp_path: Path,
) -> None:
    ca_path = tmp_path / "ca-bundle.pem"
    ca_path.write_text(
        "-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----\n", encoding="utf-8"
    )
    store = _store(tmp_path)
    authority = InventoryAuthority(store)
    config = _config(ca_bundle_path=str(ca_path))
    state = bootstrap_or_reconcile_source(authority, store, config)
    source_id = state.source.inventory_source_id

    # Valid at config-parse time; removed before the discovery cycle runs
    # -- simulates the exact runtime race Finding 1 part B describes. No
    # transport is patched here: this exercises the real _build_transport
    # -> real ProxmoxHttpTransport construction path, which must fail
    # closed at construction (before any network I/O) rather than escape
    # unclassified.
    ca_path.unlink()

    outcome = sched.run_discovery_cycle(authority, source_id, config)

    assert outcome.status == "failed"
    state_after = store.source_state(source_id)
    assert state_after.source.active_discovery_run_id is None
    view = InventoryPublication(store, authority).read()
    assert view.sources[0]["health"] == "configuration_error"
    assert view.resources == ()  # nothing fabricated

    # A subsequent, correctly-configured cycle must be able to issue
    # normally -- the source must not be wedged.
    ca_path.write_text(
        "-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----\n", encoding="utf-8"
    )
    second_run = authority.issue_discovery_run(source_id, PROVIDER_CONTRACT_VERSION)
    assert second_run.discovery_run_sequence == 2
    authority.abandon_discovery_run(source_id, second_run.run_id, reason="test cleanup")


def test_finding1_unexpected_exception_after_issuance_fences_the_run(
    tmp_path: Path, monkeypatch
) -> None:
    store, authority, config, source_id = _bootstrap(tmp_path)
    _patch_transport(monkeypatch, _pve_handler())

    def boom(*args, **kwargs):
        raise RuntimeError("injected unexpected programming error")

    monkeypatch.setattr(sched, "_normalize_result", boom)

    outcome = sched.run_discovery_cycle(authority, source_id, config)

    assert outcome.status == "error"
    assert "injected unexpected programming error" in outcome.detail

    # Not stranded: the run was fenced via abandon_discovery_run, not left
    # active.
    state_after = store.source_state(source_id)
    assert state_after.source.active_discovery_run_id is None
    run = store.list_discovery_runs(source_id)[-1]
    assert run.lifecycle is sched.DiscoveryRunLifecycle.ABANDONED

    # No fabricated observation or absence: health/resources are exactly
    # the pre-existing not_yet_observed/empty state, untouched by the
    # failure path.
    view = InventoryPublication(store, authority).read()
    assert view.sources[0]["health"] == "not_yet_observed"
    assert view.resources == ()

    # A subsequent cycle (bug fixed / transient condition gone) issues and
    # succeeds normally.
    monkeypatch.undo()
    _patch_transport(monkeypatch, _pve_handler(guests=({"vmid": 100, "type": "qemu", "name": "vm1", "status": "running"},)))
    recovered = sched.run_discovery_cycle(authority, source_id, config)
    assert recovered.status == "success"


def test_finding1_authority_conflict_from_mark_running_is_also_fenced(
    tmp_path: Path, monkeypatch
) -> None:
    store, authority, config, source_id = _bootstrap(tmp_path)
    _patch_transport(monkeypatch, _pve_handler())

    original_mark_running = authority.mark_discovery_run_running

    def flaky_mark_running(*args, **kwargs):
        raise AuthorityConflict("simulated ownership race")

    monkeypatch.setattr(authority, "mark_discovery_run_running", flaky_mark_running)

    outcome = sched.run_discovery_cycle(authority, source_id, config)

    assert outcome.status == "conflict"
    state_after = store.source_state(source_id)
    assert state_after.source.active_discovery_run_id is None


# ---------------------------------------------------------------------------
# Corrective pass, P2 Finding 3 -- PVE auth/ACL failures classify as
# configuration_error, never source_unavailable
# ---------------------------------------------------------------------------


def _unauthorized_handler(status_code: int):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"data": None})

    return handler


@pytest.mark.parametrize("status_code", (401, 403))
def test_finding3_auth_failure_classifies_as_configuration_error_end_to_end(
    tmp_path: Path, monkeypatch, status_code: int
) -> None:
    store, authority, config, source_id = _bootstrap(tmp_path)
    # First, a successful run so we have prior inventory to prove is
    # retained.
    _patch_transport(
        monkeypatch,
        _pve_handler(guests=({"vmid": 100, "type": "qemu", "name": "vm1", "status": "running"},)),
    )
    first = sched.run_discovery_cycle(authority, source_id, config)
    assert first.status == "success"
    before = InventoryPublication(store, authority).read()

    _patch_transport(monkeypatch, _unauthorized_handler(status_code))
    outcome = sched.run_discovery_cycle(authority, source_id, config)

    assert outcome.status == "failed"
    after = InventoryPublication(store, authority).read()
    assert after.sources[0]["health"] == "configuration_error"
    assert after.sources[0]["health"] != "source_unavailable"
    # Prior inventory is completely retained -- a configuration_error
    # discovery must never reconcile absence.
    assert after.resources == before.resources


def test_finding3_connect_failure_still_takes_source_unavailable_path(
    tmp_path: Path, monkeypatch
) -> None:
    store, authority, config, source_id = _bootstrap(tmp_path)

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    _patch_transport(monkeypatch, down)
    outcome = sched.run_discovery_cycle(authority, source_id, config)

    assert outcome.status == "failed"
    view = InventoryPublication(store, authority).read()
    assert view.sources[0]["health"] == "source_unavailable"
