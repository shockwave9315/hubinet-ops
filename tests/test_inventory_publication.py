from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
import sys
from types import ModuleType

from app.inventory import (
    BaselineCompleteness,
    BaselineMode,
    DetailReadStatus,
    DiscoveredNode,
    DiscoveredResource,
    InventoryAuthority,
    InventoryAuthorityStore,
    InventoryPublication,
    NormalizedDiscoverySnapshot,
    SourceAvailability,
)
from app.inventory.discovery import ProviderGuestLocatorSet, ProviderNodeScope

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CUSTOM_COMPONENTS_PATH = REPOSITORY_ROOT / "custom_components"
HUBINET_OPS_PATH = CUSTOM_COMPONENTS_PATH / "hubinet_ops"
for package_name, package_path in (
    ("custom_components", CUSTOM_COMPONENTS_PATH),
    ("custom_components.hubinet_ops", HUBINET_OPS_PATH),
):
    if package_name not in sys.modules:
        package = ModuleType(package_name)
        package.__path__ = [str(package_path)]
        sys.modules[package_name] = package

from custom_components.hubinet_ops.contract.enums import (
    DetailStatus,
    LifecycleState,
    NodeAvailability,
    ObservationalContinuity,
    PresenceState,
    ResourceStateLevel,
    ResourceType,
    SecurityContinuity,
    SourceFreshness,
    SourceHealth,
    SourceHealthOrigin,
)
from custom_components.hubinet_ops.contract.models import (
    BackendInformation,
    HubinetOpsSnapshot,
    InventorySourceSnapshot,
    NodeSnapshot,
    PackageScanError,
    PackageScanOs,
    PackageScanPackage,
    PackageScanSnapshot,
    ResourceSnapshot,
    SourceContext,
)
from custom_components.hubinet_ops.contract.enums import PackageScanStatus


START = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.value = START

    def __call__(self) -> datetime:
        return self.value


def source_context(data):
    return SourceContext(**dict(data)) if data is not None else None


def contract_snapshot(view) -> HubinetOpsSnapshot:
    return HubinetOpsSnapshot(
        backend=BackendInformation(**dict(view.backend)),
        sources=tuple(
            InventorySourceSnapshot(
                **{
                    **dict(source),
                    "health": SourceHealth(source["health"]),
                    "freshness": SourceFreshness(source["freshness"]),
                    "health_origin": SourceHealthOrigin(source["health_origin"]),
                    "current_context": source_context(source["current_context"]),
                    "committed_context": source_context(source["committed_context"]),
                }
            )
            for source in view.sources
        ),
        nodes=tuple(NodeSnapshot(**dict(node)) for node in view.nodes),
        resources=tuple(
            ResourceSnapshot(
                **{
                    **dict(resource),
                    "resource_type": ResourceType(resource["resource_type"]),
                    "presence": PresenceState(resource["presence"]),
                    "lifecycle": LifecycleState(resource["lifecycle"]),
                    "observational_continuity": ObservationalContinuity(
                        resource["observational_continuity"]
                    ),
                    "security_continuity": SecurityContinuity(resource["security_continuity"]),
                    "detail_status": DetailStatus(resource["detail_status"]),
                    "node_availability": NodeAvailability(resource["node_availability"]),
                    "state_level": ResourceStateLevel(resource["state_level"]),
                    "effective_capabilities": frozenset(resource["effective_capabilities"]),
                    "package_scan": _contract_package_scan(resource["package_scan"]),
                }
            )
            for resource in view.resources
        ),
        inventory_revision=view.inventory_revision,
        published_state_revision=view.published_state_revision,
        published_at=view.published_at,
    )


def _contract_package_scan(value) -> PackageScanSnapshot:
    value = dict(value)
    os_value = value["os"]
    error_value = value["error"]
    return PackageScanSnapshot(
        status=PackageScanStatus(value["status"]),
        scan_run_id=value["scan_run_id"],
        started_at=value["started_at"],
        completed_at=value["completed_at"],
        os=PackageScanOs(str(os_value["id"]), str(os_value["version"])) if os_value else None,
        pending_count=value["pending_count"],
        plan_fingerprint=value["plan_fingerprint"],
        reboot_required=value["reboot_required"],
        packages=tuple(PackageScanPackage(**dict(package)) for package in value["packages"]),
        error=PackageScanError(**dict(error_value)) if error_value else None,
    )


def make_system(tmp_path: Path, *, duration: int = 300):
    clock = Clock()
    db_path = tmp_path / "authority.db"
    store = InventoryAuthorityStore(db_path, now=clock)
    authority = InventoryAuthority(store, now=clock)
    source = authority.create_inventory_source(
        provider_kind="proxmox",
        display_name="Primary",
        credential_reference="secret://inventory/primary",
        transport_locator="https://pve.example:8006",
        freshness_duration_seconds=duration,
    )
    return clock, db_path, store, authority, source.source.inventory_source_id


def reconcile(
    authority,
    source_id,
    *,
    resource_type="qemu",
    current_node_name="pve-a",
    node_names=("pve-a",),
    provider_node_name=None,
):
    provider_node_name = provider_node_name or current_node_name or node_names[0]
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
        source_facts={"release": "9.0"},
        source_availability=SourceAvailability.AVAILABLE,
        baseline_completeness=BaselineCompleteness.COMPLETE,
        baseline_mode=BaselineMode.CLUSTER,
        acl_topology_hash_before="acl",
        acl_topology_hash_after="acl",
        permission_snapshot_hash_before="perm",
        permission_snapshot_hash_after="perm",
        permission_coverage_complete=True,
        boundary_consistent=True,
        covered_nodes=node_names,
        failed_baseline_scopes=(),
        detail_summary={"ok_count": 1, "temporarily_unavailable_count": 0, "error_count": 0},
        failed_detail_scopes=(),
        nodes=tuple(
            DiscoveredNode(name, "online", True, START.isoformat(), {})
            for name in node_names
        ),
        resources=(
            DiscoveredResource(
                source_id,
                100,
                resource_type,
                "guest",
                "running",
                current_node_name,
                START.isoformat(),
                DetailReadStatus.OK,
                {},
            ),
        ),
        provider_node_scope=ProviderNodeScope._from_provider(
            BaselineMode.CLUSTER, tuple(sorted(node_names))
        ),
        provider_guest_locators=ProviderGuestLocatorSet._from_provider(
            ({"vmid": 100, "type": resource_type, "node": provider_node_name},)
        ),
    )
    authority.finalize_successful_discovery_run(source_id, run.run_id, snapshot)


def test_backend_publication_is_accepted_by_phase0_contract_oracle(tmp_path: Path) -> None:
    _, _, store, authority, source_id = make_system(tmp_path)
    reconcile(authority, source_id)
    view = InventoryPublication(store, authority).read()
    snapshot = contract_snapshot(view)
    assert len(snapshot.nodes) == len(snapshot.resources) == 1
    assert snapshot.resources[0].inventory_source_id == snapshot.sources[0].inventory_source_id
    assert snapshot.resources[0].current_node_id == snapshot.nodes[0].node_id


def test_publication_retains_known_node_for_unresolved_current_relation(tmp_path: Path) -> None:
    _, _, store, authority, source_id = make_system(tmp_path)
    reconcile(authority, source_id)
    previous = store.list_resources(source_id)[0]
    reconcile(
        authority,
        source_id,
        current_node_name=None,
        node_names=("pve-a",),
    )
    snapshot = contract_snapshot(InventoryPublication(store, authority).read())
    resource = snapshot.resources[0]
    assert resource.resource_id == previous.resource_id
    assert resource.active_binding_id == previous.active_binding_id
    assert resource.locator_generation == previous.locator_generation
    assert resource.current_node_id is None
    assert resource.last_known_node_id == previous.current_node_id
    assert resource.node_availability is NodeAvailability.UNRESOLVED
    assert resource.last_known_node_id in snapshot.nodes_by_id


def test_direct_replacement_publishes_retained_predecessor_and_current_successor(tmp_path: Path) -> None:
    _, _, store, authority, source_id = make_system(tmp_path)
    reconcile(authority, source_id, resource_type="qemu")
    reconcile(authority, source_id, resource_type="lxc")
    snapshot = contract_snapshot(InventoryPublication(store, authority).read())
    old, successor = snapshot.resources
    assert old.vmid == successor.vmid == 100
    assert old.active_binding_id is None
    assert old.presence is PresenceState.NOT_CURRENT
    assert old.successor_resource_id == successor.resource_id
    assert successor.active_binding_id is not None
    assert successor.locator_generation == old.locator_generation + 1


def test_same_published_revision_returns_value_identical_immutable_view(tmp_path: Path) -> None:
    _, _, store, authority, source_id = make_system(tmp_path)
    reconcile(authority, source_id)
    publication = InventoryPublication(store, authority)
    first = publication.read()
    second = publication.read()
    assert first == second
    assert first.published_state_revision == second.published_state_revision
    try:
        first.backend["name"] = "changed"
    except TypeError:
        pass
    else:
        raise AssertionError("published view is mutable")


def test_expired_freshness_is_materialized_in_snapshot_transaction(
    tmp_path: Path, monkeypatch
) -> None:
    clock, _, store, authority, source_id = make_system(tmp_path, duration=10)
    reconcile(authority, source_id)
    before = store.backend_instance()
    clock.value = START + timedelta(seconds=11)
    split_wrapper_calls = 0
    original_materialize = authority.materialize_due_expiry

    def split_transaction_trap(*args, **kwargs):
        nonlocal split_wrapper_calls
        split_wrapper_calls += 1
        clock.value = START + timedelta(seconds=9)
        result = original_materialize(*args, **kwargs)
        clock.value = START + timedelta(seconds=11)
        return result

    monkeypatch.setattr(authority, "materialize_due_expiry", split_transaction_trap)
    snapshot = contract_snapshot(InventoryPublication(store, authority).read())
    source = snapshot.sources[0]
    assert split_wrapper_calls == 0
    assert source.freshness is SourceFreshness.STALE
    assert source.health_origin is SourceHealthOrigin.TIME_EXPIRY
    assert snapshot.inventory_revision == before.inventory_revision
    assert snapshot.published_state_revision == before.published_state_revision + 1


def test_clock_rollback_is_materialized_before_publication_without_revival(
    tmp_path: Path,
) -> None:
    clock, _, store, authority, source_id = make_system(tmp_path, duration=300)
    reconcile(authority, source_id)
    committed_resources = store.list_resources(source_id)
    before = store.backend_instance()

    clock.value = START - timedelta(minutes=10)
    snapshot = contract_snapshot(InventoryPublication(store, authority).read())
    source = snapshot.sources[0]
    assert source.freshness is SourceFreshness.STALE
    assert source.health_origin is SourceHealthOrigin.TIME_EXPIRY
    assert source.health_reason == "freshness_clock_rollback_before_reference"
    assert snapshot.inventory_revision == before.inventory_revision
    assert snapshot.published_state_revision == before.published_state_revision + 1
    assert store.list_resources(source_id) == committed_resources

    clock.value = START + timedelta(minutes=1)
    after_rollback = store.backend_instance()
    caught_up = contract_snapshot(InventoryPublication(store, authority).read())
    assert not authority.source_is_fresh_for_future_mutation(source_id)
    assert caught_up.sources[0].freshness is SourceFreshness.STALE
    assert caught_up.sources[0].health_reason == source.health_reason
    assert store.backend_instance() == after_rollback


def test_concurrent_commits_allocate_two_publications_and_reads_never_tear(tmp_path: Path) -> None:
    _, db_path, store, authority, first_id = make_system(tmp_path)
    second_id = authority.create_inventory_source(
        provider_kind="proxmox",
        display_name="Second",
        credential_reference="secret://inventory/second",
        transport_locator="https://pve-2.example:8006",
    ).source.inventory_source_id
    start_revision = store.backend_instance().published_state_revision

    def rename(source_id: str, name: str) -> None:
        local_store = InventoryAuthorityStore(db_path)
        try:
            InventoryAuthority(local_store).rename_inventory_source(source_id, name)
        finally:
            local_store.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = (
            pool.submit(rename, first_id, "First renamed"),
            pool.submit(rename, second_id, "Second renamed"),
        )
        views = [InventoryPublication(store, authority).read() for _ in range(8)]
        for future in futures:
            future.result()
    final = InventoryPublication(store, authority).read()
    assert final.published_state_revision == start_revision + 2
    assert final.inventory_revision == store.backend_instance().inventory_revision
    for view in (*views, final):
        contract_snapshot(view)
        assert view.inventory_revision <= view.published_state_revision
