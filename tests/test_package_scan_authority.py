from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.inventory import (
    AuthorityConflict,
    BaselineCompleteness,
    BaselineMode,
    DetailReadStatus,
    DiscoveredNode,
    DiscoveredResource,
    InventoryAuthority,
    InventoryAuthorityStore,
    NormalizedDiscoverySnapshot,
    PackageScanFailure,
    PackageScanOutcome,
    PackageScanPackage,
    SourceAvailability,
    package_plan_fingerprint,
)
from app.inventory.discovery import ProviderGuestLocatorSet, ProviderNodeScope


START = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.value = START

    def __call__(self) -> datetime:
        return self.value


def _system(tmp_path: Path, *, resource_type: str = "lxc"):
    clock = Clock()
    store = InventoryAuthorityStore(tmp_path / "authority.db", now=clock)
    authority = InventoryAuthority(store, now=clock)
    source = authority.create_inventory_source(
        provider_kind="proxmox_ve",
        display_name="Primary",
        credential_reference="secret://pve",
        transport_locator="https://pve.example:8006",
    )
    _reconcile(authority, source.source.inventory_source_id, resource_type=resource_type)
    return clock, store, authority, store.list_resources()[0]


def _reconcile(
    authority: InventoryAuthority,
    source_id: str,
    *,
    resource_type: str = "lxc",
    status: str = "running",
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
        observed_at=START.isoformat(),
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
            "ok_count": 1,
            "temporarily_unavailable_count": 0,
            "error_count": 0,
        },
        failed_detail_scopes=(),
        nodes=(DiscoveredNode("pve-a", "online", True, START.isoformat(), {}),),
        resources=(
            DiscoveredResource(
                source_id,
                101,
                resource_type,
                "guest",
                status,
                "pve-a",
                START.isoformat(),
                DetailReadStatus.OK,
                {},
            ),
        ),
        provider_node_scope=ProviderNodeScope._from_provider(
            BaselineMode.CLUSTER, ("pve-a",)
        ),
        provider_guest_locators=ProviderGuestLocatorSet._from_provider(
            ({"vmid": 101, "type": resource_type, "node": "pve-a"},)
        ),
    )
    authority.finalize_successful_discovery_run(source_id, run.run_id, snapshot)


def _packages() -> tuple[PackageScanPackage, ...]:
    return (
        PackageScanPackage(
            "zlib1g",
            "1:1.2.13.dfsg-1",
            "1:1.2.13.dfsg-2",
            "Debian:12/stable-security",
            "compression library",
            True,
        ),
        PackageScanPackage("apt", "2.6.1", "2.6.2"),
    )


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
