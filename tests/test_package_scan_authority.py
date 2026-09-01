from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
import threading

import pytest

from app.inventory import (
    AuthorityConflict,
    BaselineCompleteness,
    BaselineMode,
    DetailReadStatus,
    DiscoveredNode,
    DiscoveredResource,
    DiscoveryRunCompletionEvidence,
    InventoryAuthority,
    InventoryAuthorityStore,
    InventoryPublication,
    NormalizedDiscoverySnapshot,
    PackageScanFailure,
    PackageScanOutcome,
    PackageScanPackage,
    SourceAvailability,
    package_plan_fingerprint,
)
from app.inventory.discovery import ProviderGuestLocatorSet, ProviderNodeScope
from app.package_scan import HostScanFailure, HostScanResult, expected_host_context
from app.package_scan_scheduler import PackageScanScheduler


START = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.value = START

    def __call__(self) -> datetime:
        return self.value


def _system(
    tmp_path: Path, *, resource_type: str = "lxc", freshness_duration_seconds: int = 300
):
    clock = Clock()
    store = InventoryAuthorityStore(tmp_path / "authority.db", now=clock)
    authority = InventoryAuthority(store, now=clock)
    source = authority.create_inventory_source(
        provider_kind="proxmox_ve",
        display_name="Primary",
        credential_reference="secret://pve",
        transport_locator="https://pve.example:8006",
        freshness_duration_seconds=freshness_duration_seconds,
    )
    _reconcile(authority, source.source.inventory_source_id, resource_type=resource_type)
    return clock, store, authority, store.list_resources()[0]


def _reconcile(
    authority: InventoryAuthority,
    source_id: str,
    *,
    resource_type: str = "lxc",
    status: str = "running",
    observed_at: str = START.isoformat(),
    resource_present: bool = True,
) -> None:
    run = authority.issue_discovery_run(source_id, 1)
    snapshot = NormalizedDiscoverySnapshot(
        run_id=run.run_id,
        discovery_run_sequence=run.discovery_run_sequence,
        inventory_source_id=source_id,
        expected_source_config_revision=run.expected_source_config_revision,
        endpoint_id=run.expected_endpoint_id,
        canonical_transport_locator=run.expected_canonical_transport_locator,
        canonicalization_contract_version=run.expected_canonicalization_contract_version,
        expected_transport_trust_revision=run.expected_transport_trust_revision,
        provider_contract_version=1,
        observed_at=observed_at,
        source_facts={},
        source_availability=SourceAvailability.AVAILABLE,
        baseline_completeness=BaselineCompleteness.COMPLETE,
        baseline_mode=BaselineMode.CLUSTER,
        acl_topology_hash_before="acl",
        acl_topology_hash_after="acl",
        permission_snapshot_hash_before="permissions",
        permission_snapshot_hash_after="permissions",
        permission_coverage_complete=True,
        boundary_consistent=True,
        covered_nodes=("pve-a",),
        failed_baseline_scopes=(),
        detail_summary={
            "ok_count": 1 if resource_present else 0,
            "temporarily_unavailable_count": 0,
            "error_count": 0,
        },
        failed_detail_scopes=(),
        nodes=(DiscoveredNode("pve-a", "online", True, observed_at, {}),),
        resources=(
            DiscoveredResource(
                source_id,
                101,
                resource_type,
                "guest",
                status,
                "pve-a",
                observed_at,
                DetailReadStatus.OK,
                {},
            ),
        )
        if resource_present
        else (),
        provider_node_scope=ProviderNodeScope._from_provider(
            BaselineMode.CLUSTER, ("pve-a",)
        ),
        provider_guest_locators=ProviderGuestLocatorSet._from_provider(
            ({"vmid": 101, "type": resource_type, "node": "pve-a"},)
            if resource_present
            else ()
        ),
    )
    authority.finalize_successful_discovery_run(source_id, run.run_id, snapshot)


def _packages() -> tuple[PackageScanPackage, ...]:
    return (
        PackageScanPackage(
            "zlib1g",
            "amd64",
            "1:1.2.13.dfsg-1",
            "1:1.2.13.dfsg-2",
            "Debian:12/stable-security",
            "compression library",
            True,
        ),
        PackageScanPackage("apt", "amd64", "2.6.1", "2.6.2"),
    )


def _finalize_discovery_failure(
    authority: InventoryAuthority,
    source_id: str,
    outcome: BaselineCompleteness,
) -> None:
    run = authority.issue_discovery_run(source_id, 1)
    authority.finalize_failed_discovery_run(
        source_id,
        run.run_id,
        completion_evidence=DiscoveryRunCompletionEvidence(outcome),
        reason=f"test {outcome.value}",
    )


def test_fresh_current_source_allows_scan_and_captures_resource_context(
    tmp_path: Path,
) -> None:
    _, store, authority, resource = _system(tmp_path)
    state = store.source_state(resource.inventory_source_id)
    assert state.runtime_health.health.value == "healthy"
    assert state.runtime_health.freshness.value == "fresh"
    assert (
        state.runtime_health.committed_source_config_revision
        == state.source.source_config_revision
    )
    assert (
        state.runtime_health.committed_endpoint_id
        == state.active_endpoint.endpoint_id
    )
    assert (
        state.runtime_health.committed_transport_trust_revision
        == state.active_endpoint.transport_trust_revision
    )

    run = authority.issue_package_scan(resource.resource_id)

    assert run.attempt_sequence == 1
    assert run.expected_binding_id == resource.active_binding_id
    assert run.expected_locator_generation == resource.locator_generation
    assert (
        run.expected_resource_continuity_revision
        == resource.resource_continuity_revision
    )
    assert run.expected_vmid == resource.vmid
    assert run.expected_node_id == resource.current_node_id
    assert (
        run.committed_source_config_revision
        == state.runtime_health.committed_source_config_revision
    )
    assert run.committed_endpoint_id == state.runtime_health.committed_endpoint_id
    assert (
        run.committed_canonical_transport_locator
        == state.runtime_health.committed_canonical_transport_locator
    )
    assert (
        run.committed_canonicalization_contract_version
        == state.runtime_health.committed_canonicalization_contract_version
    )
    assert (
        run.committed_transport_trust_revision
        == state.runtime_health.committed_transport_trust_revision
    )
    assert run.provider_contract_version == state.source.provider_contract_version


def test_scan_issuance_atomically_materializes_expiry_and_consumes_no_attempt(
    tmp_path: Path,
) -> None:
    clock, store, authority, resource = _system(
        tmp_path, freshness_duration_seconds=60
    )
    clock.value = START + timedelta(seconds=61)

    with pytest.raises(
        AuthorityConflict,
        match="package scan requires fresh healthy inventory authority",
    ):
        authority.issue_package_scan(resource.resource_id)

    assert store.list_package_scan_runs(resource.resource_id) == ()
    stale = store.source_state(resource.inventory_source_id).runtime_health
    assert stale.health.value == "healthy"
    assert stale.freshness.value == "stale"
    assert stale.health_origin.value == "time_expiry"
    assert stale.health_reason == "freshness_deadline_elapsed"

    _reconcile(
        authority,
        resource.inventory_source_id,
        observed_at=clock.value.isoformat(),
    )
    retry = authority.issue_package_scan(resource.resource_id)
    assert retry.attempt_sequence == 1


@pytest.mark.parametrize(
    ("outcome", "expected_health"),
    (
        (BaselineCompleteness.SOURCE_UNAVAILABLE, "source_unavailable"),
        (BaselineCompleteness.PARTIAL, "degraded"),
    ),
)
def test_unavailable_or_degraded_source_refuses_scan_without_attempt(
    tmp_path: Path,
    outcome: BaselineCompleteness,
    expected_health: str,
) -> None:
    _, store, authority, resource = _system(tmp_path)
    _finalize_discovery_failure(authority, resource.inventory_source_id, outcome)
    retained = store.list_resources(resource.inventory_source_id)[0]
    assert retained.presence == "present"
    assert retained.lifecycle == "active"
    assert retained.active_binding_id is not None
    assert (
        store.source_state(resource.inventory_source_id).runtime_health.health.value
        == expected_health
    )

    with pytest.raises(AuthorityConflict, match="fresh healthy inventory authority"):
        authority.issue_package_scan(resource.resource_id)

    assert store.list_package_scan_runs(resource.resource_id) == ()


def test_committed_context_mismatch_refuses_scan_and_preserves_history(
    tmp_path: Path,
) -> None:
    _, store, authority, resource = _system(tmp_path)
    historical = authority.issue_package_scan(resource.resource_id)
    authority.finalize_successful_package_scan(
        historical.scan_run_id,
        os_id="debian",
        os_version="12",
        packages=_packages(),
        reboot_required=None,
    )
    before = store.list_package_scan_runs(resource.resource_id)
    authority.rotate_transport_trust(resource.inventory_source_id)
    state = store.source_state(resource.inventory_source_id)
    assert state.runtime_health.freshness.value == "stale"
    assert (
        state.runtime_health.committed_transport_trust_revision
        != state.active_endpoint.transport_trust_revision
    )

    with pytest.raises(AuthorityConflict, match="fresh healthy inventory authority"):
        authority.issue_package_scan(resource.resource_id)

    assert store.list_package_scan_runs(resource.resource_id) == before


def test_success_persists_sorted_exact_plan_and_material_fingerprint(tmp_path: Path) -> None:
    _, store, authority, resource = _system(tmp_path)
    run = authority.issue_package_scan(resource.resource_id)
    completed = authority.finalize_successful_package_scan(
        run.scan_run_id,
        os_id="debian",
        os_version="12",
        packages=_packages(),
        reboot_required=True,
    )

    assert completed.outcome is PackageScanOutcome.SUCCESS
    assert completed.pending_count == 2
    assert tuple(package.package_name for package in completed.packages) == ("apt", "zlib1g")
    assert completed.plan_fingerprint == package_plan_fingerprint(_packages())
    metadata_changed = tuple(
        PackageScanPackage(
            item.package_name,
            item.architecture,
            item.installed_version,
            item.candidate_version,
            "different origin",
            "different description",
            None,
        )
        for item in _packages()
    )
    assert package_plan_fingerprint(metadata_changed) == completed.plan_fingerprint
    assert store.record_counts()["package_scan_packages"] == 2


def test_zero_updates_is_an_exact_success(tmp_path: Path) -> None:
    _, store, authority, resource = _system(tmp_path)
    run = authority.issue_package_scan(resource.resource_id)
    completed = authority.finalize_successful_package_scan(
        run.scan_run_id,
        os_id="ubuntu",
        os_version="24.04",
        packages=(),
        reboot_required=None,
    )
    assert completed.pending_count == 0
    assert completed.packages == ()
    assert completed.plan_fingerprint == package_plan_fingerprint(())


def test_failed_latest_attempt_has_unknown_count_and_does_not_reuse_success(tmp_path: Path) -> None:
    clock, store, authority, resource = _system(tmp_path)
    first = authority.issue_package_scan(resource.resource_id)
    authority.finalize_successful_package_scan(
        first.scan_run_id,
        os_id="debian",
        os_version="12",
        packages=_packages(),
        reboot_required=None,
    )
    clock.value += timedelta(minutes=1)
    second = authority.issue_package_scan(resource.resource_id)
    failed = authority.finalize_failed_package_scan(
        second.scan_run_id,
        failure_class=PackageScanFailure.METADATA_REFRESH_FAILED,
        error_message="apt metadata refresh failed",
    )
    assert failed.outcome is PackageScanOutcome.FAILED
    assert failed.pending_count is None
    assert failed.plan_fingerprint is None
    assert store.list_package_scan_runs(resource.resource_id)[0].pending_count == 2
    published = dict(InventoryPublication(store, authority).read().resources[0])[
        "package_scan"
    ]
    assert published["status"] == "failed"
    assert published["pending_count"] is None
    assert published["plan_fingerprint"] is None
    assert published["packages"] == ()
    assert published["error"]["classification"] == "metadata_refresh_failed"


def test_publication_exposes_full_exact_plan_but_suppresses_stale_success(
    tmp_path: Path,
) -> None:
    _, store, authority, resource = _system(tmp_path)
    run = authority.issue_package_scan(resource.resource_id)
    authority.finalize_successful_package_scan(
        run.scan_run_id,
        os_id="debian",
        os_version="12",
        packages=_packages(),
        reboot_required=True,
    )
    published = dict(InventoryPublication(store, authority).read().resources[0])[
        "package_scan"
    ]
    assert published["status"] == "success"
    assert published["pending_count"] == 2
    assert tuple(package["name"] for package in published["packages"]) == (
        "apt",
        "zlib1g",
    )

    _reconcile(authority, resource.inventory_source_id, status="stopped")
    unavailable = dict(InventoryPublication(store, authority).read().resources[0])[
        "package_scan"
    ]
    assert unavailable["status"] == "unavailable"
    assert unavailable["pending_count"] is None
    assert unavailable["packages"] == ()


def test_per_resource_single_flight_and_qemu_rejection(tmp_path: Path) -> None:
    _, _, authority, resource = _system(tmp_path)
    authority.issue_package_scan(resource.resource_id)
    with pytest.raises(AuthorityConflict, match="active package scan"):
        authority.issue_package_scan(resource.resource_id)

    _, _, qemu_authority, qemu = _system(tmp_path / "qemu", resource_type="qemu")
    with pytest.raises(AuthorityConflict, match="LXC"):
        qemu_authority.issue_package_scan(qemu.resource_id)


def test_changed_resource_context_fences_success_as_stale(tmp_path: Path) -> None:
    _, _, authority, resource = _system(tmp_path)
    run = authority.issue_package_scan(resource.resource_id)
    _reconcile(authority, resource.inventory_source_id, resource_type="qemu")
    completed = authority.finalize_successful_package_scan(
        run.scan_run_id,
        os_id="debian",
        os_version="12",
        packages=_packages(),
        reboot_required=None,
    )
    assert completed.outcome is PackageScanOutcome.FAILED
    assert completed.failure_class is PackageScanFailure.STALE_TARGET
    assert completed.pending_count is None
    assert completed.packages == ()


def test_guest_disappearance_during_scan_fences_success_as_stale_not_execution_failed(
    tmp_path: Path,
) -> None:
    """Regression: nullable current-node state must fence cleanly, not raise.

    Before the fix, `_package_scan_context_is_current` evaluated
    `int(current["node_available"]) == 1` inside an eagerly built `all((...))`
    tuple. Once the guest disappears from a complete baseline, its
    `current_node_id` (and therefore the LEFT-JOINed `node_available`) is
    NULL while its locator binding is still retained, so `int(None)` raised
    TypeError instead of finalizing the scan as a clean stale-target failure.
    """

    _, store, authority, resource = _system(tmp_path)
    run = authority.issue_package_scan(resource.resource_id)

    _reconcile(authority, resource.inventory_source_id, resource_present=False)
    retained = store.list_resources(resource.inventory_source_id)[0]
    assert retained.presence == "missing"
    assert retained.lifecycle == "quarantined"
    assert retained.current_node_id is None
    assert retained.active_binding_id is not None

    completed = authority.finalize_successful_package_scan(
        run.scan_run_id,
        os_id="debian",
        os_version="12",
        packages=_packages(),
        reboot_required=None,
    )
    assert completed.outcome is PackageScanOutcome.FAILED
    assert completed.failure_class is PackageScanFailure.STALE_TARGET
    assert completed.pending_count is None
    assert completed.packages == ()


def test_restart_recovery_marks_running_attempt_interrupted_and_allows_retry(
    tmp_path: Path,
) -> None:
    _, store, authority, resource = _system(tmp_path)
    first = authority.issue_package_scan(resource.resource_id)
    assert authority.recover_interrupted_package_scans() == (first.scan_run_id,)
    recovered = store.package_scan_run(first.scan_run_id)
    assert recovered.outcome is PackageScanOutcome.INTERRUPTED
    assert recovered.pending_count is None
    retry = authority.issue_package_scan(resource.resource_id)
    assert retry.attempt_sequence == 2


class SuccessfulHostControl:
    def scan_packages(self, run):
        return HostScanResult(
            context=expected_host_context(run),
            os_release='ID=debian\nVERSION_ID="12"\n',
            simulation_stdout=(
                "Reading package lists...\n"
                "Building dependency tree...\n"
                "Reading state information...\n"
                "Calculating upgrade...\n"
                "0 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n"
            ),
            reboot_required=None,
        )


def test_automatic_scheduler_scans_current_lxc_and_uses_runtime_interval(
    tmp_path: Path,
) -> None:
    _, store, authority, resource = _system(tmp_path)
    scheduler = PackageScanScheduler(
        authority,
        store,
        SuccessfulHostControl(),
        interval_seconds=21_600,
        initial_delay_seconds=30,
    )
    outcome = scheduler.run_once()
    assert len(outcome) == 1
    assert outcome[0].resource_id == resource.resource_id
    assert outcome[0].status == "success"
    assert store.list_package_scan_runs(resource.resource_id)[-1].pending_count == 0
    scheduler.configure_interval_seconds(3600)
    assert scheduler.interval_seconds == 3600
    with pytest.raises(ValueError):
        scheduler.configure_interval_seconds(59)


def test_scheduler_stale_source_conflict_never_reaches_host_control(
    tmp_path: Path,
) -> None:
    _, store, authority, resource = _system(tmp_path)
    _finalize_discovery_failure(
        authority,
        resource.inventory_source_id,
        BaselineCompleteness.SOURCE_UNAVAILABLE,
    )
    retained = store.list_resources(resource.inventory_source_id)[0]
    assert retained.resource_type == "lxc"
    assert retained.presence == "present"
    assert retained.lifecycle == "active"
    assert retained.active_binding_id is not None

    class CountingHostControl(SuccessfulHostControl):
        def __init__(self) -> None:
            self.calls = 0

        def scan_packages(self, run):
            self.calls += 1
            return super().scan_packages(run)

    host_control = CountingHostControl()
    scheduler = PackageScanScheduler(
        authority,
        store,
        host_control,
        interval_seconds=21_600,
        initial_delay_seconds=0,
    )

    outcomes = scheduler.run_once()

    assert len(outcomes) == 1
    assert outcomes[0].resource_id == resource.resource_id
    assert outcomes[0].status == "conflict"
    assert outcomes[0].scan_run_id is None
    assert host_control.calls == 0
    assert store.list_package_scan_runs(resource.resource_id) == ()


def test_scheduler_skips_qemu_without_inventory_error(tmp_path: Path) -> None:
    _, store, authority, _ = _system(tmp_path, resource_type="qemu")
    scheduler = PackageScanScheduler(
        authority,
        store,
        SuccessfulHostControl(),
        interval_seconds=21_600,
        initial_delay_seconds=0,
    )
    assert scheduler.run_once() == ()
    assert store.list_package_scan_runs() == ()


def test_scheduler_global_worker_prevents_overlapping_cycles(tmp_path: Path) -> None:
    _, store, authority, _ = _system(tmp_path)
    entered = threading.Event()
    release = threading.Event()

    class BlockingHostControl(SuccessfulHostControl):
        def scan_packages(self, run):
            entered.set()
            assert release.wait(timeout=5)
            return super().scan_packages(run)

    scheduler = PackageScanScheduler(
        authority,
        store,
        BlockingHostControl(),
        interval_seconds=21_600,
        initial_delay_seconds=0,
    )
    thread = threading.Thread(target=scheduler.run_once)
    thread.start()
    assert entered.wait(timeout=5)
    assert scheduler.run_once() == ()
    release.set()
    thread.join(timeout=5)
    assert not thread.is_alive()


def test_scheduler_persists_host_failure_as_unknown_not_zero(tmp_path: Path) -> None:
    _, store, authority, resource = _system(tmp_path)

    class StoppedHostControl:
        def scan_packages(self, _run):
            raise HostScanFailure(
                PackageScanFailure.GUEST_UNAVAILABLE,
                "guest is not running",
            )

    scheduler = PackageScanScheduler(
        authority,
        store,
        StoppedHostControl(),
        interval_seconds=21_600,
        initial_delay_seconds=0,
    )
    scheduler.run_once()
    failed = store.list_package_scan_runs(resource.resource_id)[-1]
    assert failed.failure_class is PackageScanFailure.GUEST_UNAVAILABLE
    assert failed.pending_count is None
