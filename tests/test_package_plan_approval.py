from __future__ import annotations

from datetime import timedelta
from pathlib import Path
import uuid

import pytest

from app.inventory import (
    AuthorityConflict,
    AuthorityInvariantError,
    InventoryAuthority,
    InventoryAuthorityStore,
    InventoryPublication,
    PackageScanFailure,
    PackageScanPackage,
    package_plan_fingerprint,
)
from tests.test_package_scan_authority import (
    START,
    _finalize_discovery_failure,
    _packages,
    _reconcile,
    _system,
)
from app.inventory.discovery import BaselineCompleteness


def _successful_plan(authority: InventoryAuthority, resource_id: str, packages=None):
    run = authority.issue_package_scan(resource_id)
    completed = authority.finalize_successful_package_scan(
        run.scan_run_id,
        os_id="debian",
        os_version="12",
        packages=_packages() if packages is None else packages,
        reboot_required=None,
    )
    return completed


def _approval_view(store, authority, resource_id):
    view = InventoryPublication(store, authority).read()
    resource = next(
        item for item in view.resources if item["resource_id"] == resource_id
    )
    return resource["package_plan_approval"]


def test_current_exact_plan_approval_is_durable_idempotent_and_recomputed(
    tmp_path: Path,
) -> None:
    clock, store, authority, resource = _system(tmp_path)
    completed = _successful_plan(authority, resource.resource_id)
    expected = package_plan_fingerprint(completed.packages)

    first = authority.approve_package_plan(
        resource.resource_id, completed.scan_run_id, expected
    )
    revision = store.backend_instance().published_state_revision
    repeated = authority.approve_package_plan(
        resource.resource_id, completed.scan_run_id, expected
    )

    assert repeated == first
    assert store.backend_instance().published_state_revision == revision
    assert store.package_plan_approval(resource.resource_id) == first
    assert _approval_view(store, authority, resource.resource_id)["status"] == "approved"

    db_path = store.path
    store.close()
    reopened = InventoryAuthorityStore(db_path, now=clock)
    restarted = InventoryAuthority(reopened, now=clock)
    assert reopened.package_plan_approval(resource.resource_id) == first
    assert _approval_view(reopened, restarted, resource.resource_id)["status"] == "approved"


def test_wrong_fingerprint_cross_resource_scan_and_old_attempt_fail_closed(
    tmp_path: Path,
) -> None:
    _, store, authority, first_resource = _system(tmp_path)
    first = _successful_plan(authority, first_resource.resource_id)

    second_source = authority.create_inventory_source(
        provider_kind="proxmox_ve",
        display_name="Secondary",
        credential_reference="secret://pve-secondary",
        transport_locator="https://pve-secondary.example:8006",
    )
    _reconcile(authority, second_source.source.inventory_source_id)
    second_resource = next(
        item
        for item in store.list_resources()
        if item.inventory_source_id == second_source.source.inventory_source_id
    )
    second = _successful_plan(authority, second_resource.resource_id)

    with pytest.raises(AuthorityConflict, match="fingerprint"):
        authority.approve_package_plan(
            first_resource.resource_id, first.scan_run_id, "0" * 64
        )
    with pytest.raises(AuthorityConflict, match="another resource"):
        authority.approve_package_plan(
            first_resource.resource_id,
            second.scan_run_id,
            second.plan_fingerprint,
        )

    newer = _successful_plan(authority, first_resource.resource_id)
    with pytest.raises(AuthorityConflict, match="latest attempt"):
        authority.approve_package_plan(
            first_resource.resource_id,
            first.scan_run_id,
            first.plan_fingerprint,
        )
    assert newer.plan_fingerprint == first.plan_fingerprint


def test_changed_plan_is_stale_but_same_plan_with_same_context_stays_approved(
    tmp_path: Path,
) -> None:
    clock, store, authority, resource = _system(tmp_path)
    reviewed = _successful_plan(authority, resource.resource_id)
    approval = authority.approve_package_plan(
        resource.resource_id, reviewed.scan_run_id, reviewed.plan_fingerprint
    )

    same = _successful_plan(authority, resource.resource_id)
    same_view = _approval_view(store, authority, resource.resource_id)
    assert same.plan_fingerprint == reviewed.plan_fingerprint
    assert same_view["status"] == "approved"
    assert same_view["reviewed_scan_run_id"] == approval.reviewed_scan_run_id

    changed = _successful_plan(
        authority,
        resource.resource_id,
        (PackageScanPackage("apt", "2.6.1", "2.6.3"),),
    )
    changed_view = _approval_view(store, authority, resource.resource_id)
    assert changed.plan_fingerprint != reviewed.plan_fingerprint
    assert changed_view["status"] == "stale"
    assert changed_view["approvable"] is True
    assert store.package_plan_approval(resource.resource_id) == approval


@pytest.mark.parametrize("terminal", ("failed", "interrupted"))
def test_latest_failed_or_interrupted_attempt_makes_approval_ineffective(
    tmp_path: Path, terminal: str
) -> None:
    _, store, authority, resource = _system(tmp_path)
    reviewed = _successful_plan(authority, resource.resource_id)
    authority.approve_package_plan(
        resource.resource_id, reviewed.scan_run_id, reviewed.plan_fingerprint
    )
    latest = authority.issue_package_scan(resource.resource_id)
    if terminal == "failed":
        authority.finalize_failed_package_scan(
            latest.scan_run_id,
            failure_class=PackageScanFailure.GUEST_UNAVAILABLE,
            error_message="guest unavailable",
        )
    else:
        assert authority.recover_interrupted_package_scans() == (latest.scan_run_id,)

    view = _approval_view(store, authority, resource.resource_id)
    assert view["status"] == "stale"
    assert view["approvable"] is False


def test_unknown_unsupported_and_unavailable_plans_are_not_approvable(
    tmp_path: Path,
) -> None:
    _, store, authority, resource = _system(tmp_path)
    assert _approval_view(store, authority, resource.resource_id) == {
        "status": "none",
        "approvable": False,
        "approval_id": None,
        "reviewed_scan_run_id": None,
        "plan_fingerprint": None,
        "approved_at": None,
    }

    reviewed = _successful_plan(authority, resource.resource_id)
    authority.approve_package_plan(
        resource.resource_id, reviewed.scan_run_id, reviewed.plan_fingerprint
    )
    with store._transaction() as connection:
        connection.execute(
            "UPDATE resource_incarnations SET resource_continuity_revision="
            "resource_continuity_revision+1 WHERE resource_id=?",
            (resource.resource_id,),
        )
    unavailable = _approval_view(store, authority, resource.resource_id)
    assert unavailable["status"] == "stale"
    assert unavailable["approvable"] is False

    qemu_path = tmp_path / "qemu"
    qemu_path.mkdir()
    _, qemu_store, qemu_authority, qemu = _system(qemu_path, resource_type="qemu")
    qemu_view = _approval_view(qemu_store, qemu_authority, qemu.resource_id)
    assert qemu_view["status"] == "none"
    assert qemu_view["approvable"] is False
    with pytest.raises(AuthorityConflict, match="supports LXC"):
        qemu_authority.issue_package_scan(qemu.resource_id)


def test_resource_binding_generation_continuity_and_replacement_do_not_inherit(
    tmp_path: Path,
) -> None:
    _, store, authority, resource = _system(tmp_path)
    reviewed = _successful_plan(authority, resource.resource_id)
    authority.approve_package_plan(
        resource.resource_id, reviewed.scan_run_id, reviewed.plan_fingerprint
    )

    with store._transaction() as connection:
        old = connection.execute(
            "SELECT * FROM resource_locator_bindings WHERE resource_id=? "
            "AND valid_to_run_sequence IS NULL",
            (resource.resource_id,),
        ).fetchone()
        connection.execute(
            "UPDATE resource_locator_bindings SET valid_to_run_sequence=2, "
            "closure_reason='test_rebind' WHERE binding_id=?",
            (old["binding_id"],),
        )
        connection.execute(
            "INSERT INTO resource_locator_bindings("
            "binding_id, inventory_source_id, vmid, locator_generation, resource_id, "
            "valid_from_run_sequence) VALUES(?, ?, ?, ?, ?, 2)",
            (
                str(uuid.uuid4()),
                old["inventory_source_id"],
                old["vmid"],
                int(old["locator_generation"]) + 1,
                resource.resource_id,
            ),
        )
    rebound = _approval_view(store, authority, resource.resource_id)
    assert rebound["status"] == "stale"
    assert rebound["approvable"] is False

    _reconcile(authority, resource.inventory_source_id, resource_type="qemu")
    resources = store.list_resources(resource.inventory_source_id)
    old_resource = next(item for item in resources if item.resource_id == resource.resource_id)
    successor = next(item for item in resources if item.resource_id != resource.resource_id)
    assert old_resource.successor_resource_id == successor.resource_id
    assert store.package_plan_approval(successor.resource_id) is None
    assert _approval_view(store, authority, successor.resource_id)["status"] == "none"


def test_source_context_change_refuses_old_scan_and_new_context_does_not_inherit(
    tmp_path: Path,
) -> None:
    _, store, authority, resource = _system(tmp_path)
    reviewed = _successful_plan(authority, resource.resource_id)
    approval = authority.approve_package_plan(
        resource.resource_id, reviewed.scan_run_id, reviewed.plan_fingerprint
    )
    old_context = (
        reviewed.committed_source_config_revision,
        reviewed.committed_endpoint_id,
        reviewed.committed_transport_trust_revision,
        reviewed.provider_contract_version,
    )

    authority.rotate_transport_trust(resource.inventory_source_id)
    _reconcile(authority, resource.inventory_source_id)
    with pytest.raises(AuthorityConflict, match="current context"):
        authority.approve_package_plan(
            resource.resource_id, reviewed.scan_run_id, reviewed.plan_fingerprint
        )
    fresh_context_scan = _successful_plan(authority, resource.resource_id)
    assert fresh_context_scan.plan_fingerprint == reviewed.plan_fingerprint
    assert (
        fresh_context_scan.committed_source_config_revision,
        fresh_context_scan.committed_endpoint_id,
        fresh_context_scan.committed_transport_trust_revision,
        fresh_context_scan.provider_contract_version,
    ) != old_context
    view = _approval_view(store, authority, resource.resource_id)
    assert view["status"] == "stale"
    assert view["approvable"] is True
    assert store.package_plan_approval(resource.resource_id) == approval


def test_stale_and_unhealthy_source_refuse_approval_and_restart_does_not_revive(
    tmp_path: Path,
) -> None:
    clock, store, authority, resource = _system(
        tmp_path, freshness_duration_seconds=60
    )
    reviewed = _successful_plan(authority, resource.resource_id)
    clock.value = START + timedelta(seconds=61)
    with pytest.raises(AuthorityConflict, match="current context"):
        authority.approve_package_plan(
            resource.resource_id, reviewed.scan_run_id, reviewed.plan_fingerprint
        )

    _reconcile(authority, resource.inventory_source_id, observed_at=clock.value.isoformat())
    current = _successful_plan(authority, resource.resource_id)
    authority.approve_package_plan(
        resource.resource_id, current.scan_run_id, current.plan_fingerprint
    )
    _finalize_discovery_failure(
        authority,
        resource.inventory_source_id,
        BaselineCompleteness.SOURCE_UNAVAILABLE,
    )
    with pytest.raises(AuthorityConflict, match="current context"):
        authority.approve_package_plan(
            resource.resource_id, current.scan_run_id, current.plan_fingerprint
        )
    assert _approval_view(store, authority, resource.resource_id)["status"] == "stale"

    db_path = store.path
    store.close()
    reopened = InventoryAuthorityStore(db_path, now=clock)
    restarted = InventoryAuthority(reopened, now=clock)
    view = _approval_view(reopened, restarted, resource.resource_id)
    assert view["status"] == "stale"
    assert view["approvable"] is False


def test_newer_scan_race_and_approval_transaction_failure_are_fail_closed(
    tmp_path: Path,
) -> None:
    clock, store, authority, resource = _system(tmp_path)
    viewed_a = _successful_plan(authority, resource.resource_id)
    plan_b = _successful_plan(
        authority,
        resource.resource_id,
        (PackageScanPackage("apt", "2.6.1", "2.6.9"),),
    )
    assert viewed_a.plan_fingerprint != plan_b.plan_fingerprint
    with pytest.raises(AuthorityConflict, match="latest attempt"):
        authority.approve_package_plan(
            resource.resource_id, viewed_a.scan_run_id, viewed_a.plan_fingerprint
        )
    assert store.package_plan_approval(resource.resource_id) is None

    before_revision = store.backend_instance().published_state_revision

    class FailingApprovalAuthority(InventoryAuthority):
        def _after_package_plan_approval_write(self, connection, *, resource_id):
            raise RuntimeError("injected approval transaction failure")

    failing = FailingApprovalAuthority(store, now=clock)
    with pytest.raises(RuntimeError, match="injected approval"):
        failing.approve_package_plan(
            resource.resource_id, plan_b.scan_run_id, plan_b.plan_fingerprint
        )
    assert store.package_plan_approval(resource.resource_id) is None
    assert store.backend_instance().published_state_revision == before_revision


def test_internal_exact_row_mismatch_refuses_approval(tmp_path: Path) -> None:
    _, store, authority, resource = _system(tmp_path)
    reviewed = _successful_plan(authority, resource.resource_id)
    with store._transaction() as connection:
        connection.execute(
            "UPDATE package_scan_packages SET candidate_version='tampered' "
            "WHERE scan_run_id=? AND package_index=0",
            (reviewed.scan_run_id,),
        )
    with pytest.raises(AuthorityInvariantError, match="exact rows"):
        authority.approve_package_plan(
            resource.resource_id, reviewed.scan_run_id, reviewed.plan_fingerprint
        )
    assert store.package_plan_approval(resource.resource_id) is None
