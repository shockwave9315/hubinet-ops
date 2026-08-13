from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sys
from types import ModuleType
from typing import Any

from hypothesis import given, settings, strategies as st
import pytest


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

from custom_components.hubinet_ops.contract import (  # noqa: E402
    BackendInformation,
    DetailStatus,
    HubinetOpsSnapshot,
    InventorySourceSnapshot,
    LifecycleState,
    NodeAvailability,
    NodeSnapshot,
    ObservationalContinuity,
    PresenceState,
    ResourceSnapshot,
    ResourceStateLevel,
    ResourceType,
    SecurityContinuity,
    SourceContext,
    SourceFreshness,
    SourceHealth,
    SourceHealthOrigin,
)


POSITIVE_REVISION = st.integers(min_value=1, max_value=1_000_000)
INVENTORY_REVISION = st.integers(min_value=0, max_value=1_000_000)
POSITIVE_DELTA = st.integers(min_value=1, max_value=100_000)
GENERATION = st.integers(min_value=1, max_value=1_000_000)
VMID = st.integers(min_value=100, max_value=999_999)
CANONICAL_UUID = st.uuids(version=4).map(str)
ASCII_TEXT = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_",
    min_size=1,
    max_size=24,
)
DNS_LABEL = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789",
    min_size=1,
    max_size=24,
)
NON_SUCCESS_OUTCOME = st.sampled_from(("failed", "partial", "audit_only"))


def _context(
    *,
    endpoint_id: str,
    config_revision: int = 1,
    locator: str = "https://pve.example.test:8006",
    canonicalization_version: int = 1,
    trust_revision: int = 1,
) -> SourceContext:
    return SourceContext(
        source_config_revision=config_revision,
        endpoint_id=endpoint_id,
        canonical_transport_locator=locator,
        canonicalization_contract_version=canonicalization_version,
        transport_trust_revision=trust_revision,
    )


def _successful_source(
    *,
    source_id: str,
    endpoint_id: str,
    run_sequence: int = 1,
    name: str = "Property source",
    context: SourceContext | None = None,
) -> InventorySourceSnapshot:
    committed_context = context or _context(endpoint_id=endpoint_id)
    return InventorySourceSnapshot(
        inventory_source_id=source_id,
        name=name,
        provider_kind="proxmox",
        health=SourceHealth.HEALTHY,
        freshness=SourceFreshness.FRESH,
        health_origin=SourceHealthOrigin.DISCOVERY_RUN,
        health_reason="authoritative_inventory_commit",
        last_issued_run_sequence=run_sequence,
        latest_completed_run_sequence=run_sequence,
        latest_completed_outcome="success",
        last_health_run_sequence=run_sequence,
        last_run_health_outcome="success",
        last_committed_run_sequence=run_sequence,
        last_successful_observed_at="2026-08-13T11:59:30+00:00",
        freshness_reference_at="2026-08-13T11:59:00+00:00",
        freshness_valid_until="2026-08-13T12:04:00+00:00",
        current_context=committed_context,
        committed_context=committed_context,
        facts={},
    )


def _time_expiry_source(source: InventorySourceSnapshot) -> InventorySourceSnapshot:
    return replace(
        source,
        health=SourceHealth.HEALTHY,
        freshness=SourceFreshness.STALE,
        health_origin=SourceHealthOrigin.TIME_EXPIRY,
        health_reason="freshness_deadline_elapsed",
    )


def _controlled_source(
    source: InventorySourceSnapshot,
    *,
    current_context: SourceContext,
    committed_context: SourceContext,
) -> InventorySourceSnapshot:
    return replace(
        source,
        health=SourceHealth.DEGRADED,
        freshness=SourceFreshness.STALE,
        health_origin=SourceHealthOrigin.CONTROLLED_CONTEXT_TRANSITION,
        health_reason="source_context_changed",
        current_context=current_context,
        committed_context=committed_context,
    )


def _resource(
    *,
    resource_id: str,
    source_id: str,
    binding_id: str | None,
    vmid: int,
    generation: int,
    continuity_revision: int,
    security: SecurityContinuity = SecurityContinuity.UNVERIFIED,
    presence: PresenceState = PresenceState.PRESENT,
    lifecycle: LifecycleState = LifecycleState.ACTIVE,
    observational: ObservationalContinuity = ObservationalContinuity.CONSISTENT,
    current_node_id: str | None = None,
    node_availability: NodeAvailability = NodeAvailability.UNRESOLVED,
    termination_reason: str | None = None,
) -> ResourceSnapshot:
    return ResourceSnapshot(
        resource_id=resource_id,
        inventory_source_id=source_id,
        active_binding_id=binding_id,
        resource_type=ResourceType.LXC,
        vmid=vmid,
        locator_generation=generation,
        resource_continuity_revision=continuity_revision,
        name="Property resource",
        status="running" if presence is PresenceState.PRESENT else "unknown",
        current_node_id=current_node_id,
        last_known_node_id=None,
        presence=presence,
        lifecycle=lifecycle,
        observational_continuity=observational,
        security_continuity=security,
        detail_status=(
            DetailStatus.OK
            if presence is PresenceState.PRESENT
            else DetailStatus.NOT_APPLICABLE
        ),
        node_availability=node_availability,
        state_level=ResourceStateLevel.DISCOVERED,
        retained_policy={},
        effective_policy={},
        policy_applicable=False,
        suspended_reason=None,
        effective_capabilities=frozenset(),
        state={},
        termination_reason=termination_reason,
        successor_resource_id=None,
    )


def _security_resource(
    security: SecurityContinuity,
    *,
    resource_id: str,
    source_id: str,
    binding_id: str,
    vmid: int,
    generation: int,
    continuity_revision: int,
) -> ResourceSnapshot:
    quarantined = security is SecurityContinuity.REVOKED
    return _resource(
        resource_id=resource_id,
        source_id=source_id,
        binding_id=binding_id,
        vmid=vmid,
        generation=generation,
        continuity_revision=continuity_revision,
        security=security,
        lifecycle=(
            LifecycleState.QUARANTINED if quarantined else LifecycleState.ACTIVE
        ),
        observational=(
            ObservationalContinuity.UNCERTAIN
            if quarantined
            else ObservationalContinuity.CONSISTENT
        ),
    )


def _removed_resource(
    *,
    resource_id: str,
    source_id: str,
    vmid: int,
    generation: int,
    continuity_revision: int = 2,
) -> ResourceSnapshot:
    return _resource(
        resource_id=resource_id,
        source_id=source_id,
        binding_id=None,
        vmid=vmid,
        generation=generation,
        continuity_revision=continuity_revision,
        presence=PresenceState.CONFIRMED_REMOVED,
        lifecycle=LifecycleState.RETIRED,
        observational=ObservationalContinuity.CONSISTENT,
        node_availability=NodeAvailability.NOT_APPLICABLE,
        termination_reason="confirmed_removed",
    )


def _snapshot(
    *,
    backend_id: str,
    sources: tuple[InventorySourceSnapshot, ...],
    inventory_revision: int,
    published_revision: int,
    resources: tuple[ResourceSnapshot, ...] = (),
    nodes: tuple[NodeSnapshot, ...] = (),
    backend_name: str = "Hubinet Ops Property",
    backend_version: str = "0.5.0.dev0",
) -> HubinetOpsSnapshot:
    return HubinetOpsSnapshot(
        backend=BackendInformation(
            backend_instance_id=backend_id,
            name=backend_name,
            version=backend_version,
            api_version="0.5-draft",
        ),
        sources=sources,
        nodes=nodes,
        resources=resources,
        inventory_revision=inventory_revision,
        published_state_revision=published_revision,
        published_at="2026-08-13T12:00:00+00:00",
    )


def _lattice_source(
    *,
    source_id: str,
    endpoint_id: str,
    committed: int,
    health: int,
    completed: int,
    issued: int,
    later_health_outcome: str = "failed",
    later_completed_outcome: str = "audit_only",
) -> InventorySourceSnapshot:
    source = _successful_source(
        source_id=source_id,
        endpoint_id=endpoint_id,
        run_sequence=committed,
    )
    health_is_commit = health == committed
    return replace(
        source,
        health=(
            SourceHealth.HEALTHY
            if health_is_commit
            else SourceHealth.SOURCE_UNAVAILABLE
        ),
        freshness=(
            SourceFreshness.FRESH if health_is_commit else SourceFreshness.STALE
        ),
        health_reason=(
            "authoritative_inventory_commit"
            if health_is_commit
            else "applicable_run_failed"
        ),
        last_issued_run_sequence=issued,
        latest_completed_run_sequence=completed,
        latest_completed_outcome=(
            "success"
            if completed == committed
            else later_completed_outcome
        ),
        last_health_run_sequence=health,
        last_run_health_outcome=(
            "success" if health_is_commit else later_health_outcome
        ),
    )


@settings(max_examples=100, deadline=None)
@given(
    inventory_revision=INVENTORY_REVISION,
    published_revision=POSITIVE_REVISION,
    inventory_delta=POSITIVE_DELTA,
    published_delta=POSITIVE_DELTA,
    ids=st.lists(CANONICAL_UUID, min_size=3, max_size=3, unique=True),
    old_name=ASCII_TEXT,
)
def test_dual_global_revision_tokens_are_independent(
    inventory_revision: int,
    published_revision: int,
    inventory_delta: int,
    published_delta: int,
    ids: list[str],
    old_name: str,
) -> None:
    backend_id, source_id, endpoint_id = ids
    previous_source = _successful_source(
        source_id=source_id,
        endpoint_id=endpoint_id,
        name=old_name,
    )
    current_source = replace(previous_source, name=f"{old_name}-changed")
    previous = _snapshot(
        backend_id=backend_id,
        sources=(previous_source,),
        inventory_revision=inventory_revision,
        published_revision=published_revision,
    )

    accepted = _snapshot(
        backend_id=backend_id,
        sources=(current_source,),
        inventory_revision=inventory_revision + inventory_delta,
        published_revision=published_revision + published_delta,
    )
    accepted.validate_revision_successor(previous)

    inventory_stale = replace(accepted, inventory_revision=inventory_revision)
    with pytest.raises(
        ValueError,
        match="inventory-owned changes require a newer inventory_revision",
    ):
        inventory_stale.validate_revision_successor(previous)

    publication_stale = replace(accepted, published_state_revision=published_revision)
    with pytest.raises(
        ValueError,
        match="one published_state_revision must identify one immutable view",
    ):
        publication_stale.validate_revision_successor(previous)


@settings(max_examples=100, deadline=None)
@given(
    inventory_revision=INVENTORY_REVISION,
    published_revision=POSITIVE_REVISION,
    published_delta=POSITIVE_DELTA,
    ids=st.lists(CANONICAL_UUID, min_size=3, max_size=3, unique=True),
    metadata=ASCII_TEXT,
)
def test_published_view_token_is_immutable(
    inventory_revision: int,
    published_revision: int,
    published_delta: int,
    ids: list[str],
    metadata: str,
) -> None:
    backend_id, source_id, endpoint_id = ids
    source = _successful_source(source_id=source_id, endpoint_id=endpoint_id)
    previous = _snapshot(
        backend_id=backend_id,
        sources=(source,),
        inventory_revision=inventory_revision,
        published_revision=published_revision,
        backend_version=metadata,
    )
    changed = replace(
        previous,
        backend=replace(previous.backend, version=f"{metadata}-next"),
    )

    with pytest.raises(
        ValueError,
        match="one published_state_revision must identify one immutable view",
    ):
        changed.validate_revision_successor(previous)

    published = replace(
        changed,
        published_state_revision=published_revision + published_delta,
    )
    assert published.inventory_projection == previous.inventory_projection
    published.validate_revision_successor(previous)


@settings(max_examples=100, deadline=None)
@given(
    inventory_revision=INVENTORY_REVISION,
    published_revision=POSITIVE_REVISION,
    published_delta=POSITIVE_DELTA,
    ids=st.lists(CANONICAL_UUID, min_size=3, max_size=3, unique=True),
)
def test_non_inventory_publication_preserves_inventory_revision(
    inventory_revision: int,
    published_revision: int,
    published_delta: int,
    ids: list[str],
) -> None:
    backend_id, source_id, endpoint_id = ids
    source = _successful_source(source_id=source_id, endpoint_id=endpoint_id)
    previous = _snapshot(
        backend_id=backend_id,
        sources=(source,),
        inventory_revision=inventory_revision,
        published_revision=published_revision,
    )
    current = _snapshot(
        backend_id=backend_id,
        sources=(_time_expiry_source(source),),
        inventory_revision=inventory_revision,
        published_revision=published_revision + published_delta,
    )

    assert current.inventory_projection == previous.inventory_projection
    current.validate_revision_successor(previous)


LEGAL_SECURITY_PAIRS = st.sampled_from(
    (
        (SecurityContinuity.UNVERIFIED, SecurityContinuity.UNVERIFIED),
        (SecurityContinuity.UNVERIFIED, SecurityContinuity.TRUSTED),
        (SecurityContinuity.UNVERIFIED, SecurityContinuity.REVOKED),
        (SecurityContinuity.TRUSTED, SecurityContinuity.TRUSTED),
        (SecurityContinuity.TRUSTED, SecurityContinuity.REVOKED),
        (SecurityContinuity.REVOKED, SecurityContinuity.REVOKED),
        (SecurityContinuity.REVOKED, SecurityContinuity.TRUSTED),
    )
)


@settings(max_examples=100, deadline=None)
@given(
    revision=POSITIVE_REVISION,
    revision_delta=POSITIVE_DELTA,
    inventory_revision=INVENTORY_REVISION,
    published_revision=POSITIVE_REVISION,
    inventory_delta=POSITIVE_DELTA,
    published_delta=POSITIVE_DELTA,
    legal_pair=LEGAL_SECURITY_PAIRS,
    erased_security=st.sampled_from(
        (SecurityContinuity.TRUSTED, SecurityContinuity.REVOKED)
    ),
    vmid=VMID,
    generation=GENERATION,
    ids=st.lists(CANONICAL_UUID, min_size=5, max_size=5, unique=True),
)
def test_security_history_lower_bound_survives_revision_gaps(
    revision: int,
    revision_delta: int,
    inventory_revision: int,
    published_revision: int,
    inventory_delta: int,
    published_delta: int,
    legal_pair: tuple[SecurityContinuity, SecurityContinuity],
    erased_security: SecurityContinuity,
    vmid: int,
    generation: int,
    ids: list[str],
) -> None:
    backend_id, source_id, endpoint_id, resource_id, binding_id = ids
    source = _successful_source(source_id=source_id, endpoint_id=endpoint_id)

    def view(security: SecurityContinuity, continuity_revision: int) -> HubinetOpsSnapshot:
        return _snapshot(
            backend_id=backend_id,
            sources=(source,),
            resources=(
                _security_resource(
                    security,
                    resource_id=resource_id,
                    source_id=source_id,
                    binding_id=binding_id,
                    vmid=vmid,
                    generation=generation,
                    continuity_revision=continuity_revision,
                ),
            ),
            inventory_revision=(
                inventory_revision
                if continuity_revision == revision
                else inventory_revision + inventory_delta
            ),
            published_revision=(
                published_revision
                if continuity_revision == revision
                else published_revision + published_delta
            ),
        )

    old_security, new_security = legal_pair
    view(new_security, revision + revision_delta).validate_revision_successor(
        view(old_security, revision)
    )

    with pytest.raises(
        ValueError,
        match="resource cannot erase known security history",
    ):
        view(
            SecurityContinuity.UNVERIFIED,
            revision + revision_delta,
        ).validate_revision_successor(view(erased_security, revision))


SECURITY_CHANGE_PAIRS = st.sampled_from(
    (
        (SecurityContinuity.UNVERIFIED, SecurityContinuity.TRUSTED),
        (SecurityContinuity.UNVERIFIED, SecurityContinuity.REVOKED),
        (SecurityContinuity.TRUSTED, SecurityContinuity.REVOKED),
        (SecurityContinuity.REVOKED, SecurityContinuity.TRUSTED),
    )
)


@settings(max_examples=100, deadline=None)
@given(
    data=st.data(),
    revision=POSITIVE_REVISION,
    revision_delta=POSITIVE_DELTA,
    security_pair=SECURITY_CHANGE_PAIRS,
    vmid=VMID,
    generation=GENERATION,
    ids=st.lists(CANONICAL_UUID, min_size=5, max_size=5, unique=True),
)
def test_security_change_requires_strictly_newer_continuity_token(
    data: st.DataObject,
    revision: int,
    revision_delta: int,
    security_pair: tuple[SecurityContinuity, SecurityContinuity],
    vmid: int,
    generation: int,
    ids: list[str],
) -> None:
    backend_id, source_id, endpoint_id, resource_id, binding_id = ids
    source = _successful_source(source_id=source_id, endpoint_id=endpoint_id)
    old_security, new_security = security_pair
    previous_resource = _security_resource(
        old_security,
        resource_id=resource_id,
        source_id=source_id,
        binding_id=binding_id,
        vmid=vmid,
        generation=generation,
        continuity_revision=revision,
    )
    previous = _snapshot(
        backend_id=backend_id,
        sources=(source,),
        resources=(previous_resource,),
        inventory_revision=10,
        published_revision=20,
    )
    stale_revision = data.draw(st.integers(min_value=1, max_value=revision))
    stale_resource = _security_resource(
        new_security,
        resource_id=resource_id,
        source_id=source_id,
        binding_id=binding_id,
        vmid=vmid,
        generation=generation,
        continuity_revision=stale_revision,
    )
    stale = _snapshot(
        backend_id=backend_id,
        sources=(source,),
        resources=(stale_resource,),
        inventory_revision=11,
        published_revision=21,
    )
    expected = (
        "resource_continuity_revision must not regress"
        if stale_revision < revision
        else "security-relevant resource transition requires a newer"
    )
    with pytest.raises(ValueError, match=expected):
        stale.validate_revision_successor(previous)

    newer = replace(
        stale,
        resources=(
            replace(
                stale_resource,
                resource_continuity_revision=revision + revision_delta,
            ),
        ),
    )
    newer.validate_revision_successor(previous)


@settings(max_examples=100, deadline=None)
@given(
    committed=st.integers(min_value=2, max_value=1_000_000),
    health_delta=st.integers(min_value=0, max_value=100_000),
    completed_delta=st.integers(min_value=0, max_value=100_000),
    issued_delta=st.integers(min_value=0, max_value=100_000),
    health_outcome=NON_SUCCESS_OUTCOME,
    completed_outcome=NON_SUCCESS_OUTCOME,
    ids=st.lists(CANONICAL_UUID, min_size=2, max_size=2, unique=True),
)
def test_source_run_provenance_forms_partial_order_lattice(
    committed: int,
    health_delta: int,
    completed_delta: int,
    issued_delta: int,
    health_outcome: str,
    completed_outcome: str,
    ids: list[str],
) -> None:
    source_id, endpoint_id = ids
    health = committed + health_delta
    completed = health + completed_delta
    issued = completed + issued_delta
    legal = _lattice_source(
        source_id=source_id,
        endpoint_id=endpoint_id,
        committed=committed,
        health=health,
        completed=completed,
        issued=issued,
        later_health_outcome=health_outcome,
        later_completed_outcome=completed_outcome,
    )
    assert (
        legal.last_committed_run_sequence
        <= legal.last_health_run_sequence
        <= legal.latest_completed_run_sequence
        <= legal.last_issued_run_sequence
    )

    broken_tuples = (
        (committed, committed - 1, completed, issued),
        (committed, health, health - 1, issued),
        (committed, health, completed, completed - 1),
    )
    for broken_committed, broken_health, broken_completed, broken_issued in broken_tuples:
        with pytest.raises(
            ValueError,
            match="source run provenance must satisfy",
        ):
            _lattice_source(
                source_id=source_id,
                endpoint_id=endpoint_id,
                committed=broken_committed,
                health=broken_health,
                completed=broken_completed,
                issued=broken_issued,
                later_health_outcome=health_outcome,
                later_completed_outcome=completed_outcome,
            )


@settings(max_examples=100, deadline=None)
@given(
    committed=POSITIVE_REVISION,
    gap=POSITIVE_DELTA,
    ids=st.lists(CANONICAL_UUID, min_size=2, max_size=2, unique=True),
)
def test_success_outcome_names_exact_committed_run(
    committed: int,
    gap: int,
    ids: list[str],
) -> None:
    source_id, endpoint_id = ids
    exact = _lattice_source(
        source_id=source_id,
        endpoint_id=endpoint_id,
        committed=committed,
        health=committed,
        completed=committed,
        issued=committed,
    )
    assert exact.last_run_health_outcome == "success"
    assert exact.latest_completed_outcome == "success"

    later_health = _lattice_source(
        source_id=source_id,
        endpoint_id=endpoint_id,
        committed=committed,
        health=committed + gap,
        completed=committed + gap,
        issued=committed + gap,
    )
    with pytest.raises(
        ValueError,
        match="successful applied health requires the exact committed run sequence",
    ):
        replace(later_health, last_run_health_outcome="success")

    later_completion = _lattice_source(
        source_id=source_id,
        endpoint_id=endpoint_id,
        committed=committed,
        health=committed,
        completed=committed + gap,
        issued=committed + gap,
    )
    with pytest.raises(
        ValueError,
        match="successful completion requires the exact committed run sequence",
    ):
        replace(later_completion, latest_completed_outcome="success")


@settings(max_examples=100, deadline=None)
@given(
    committed=POSITIVE_REVISION,
    commit_delta=POSITIVE_DELTA,
    published_revision=POSITIVE_REVISION,
    published_delta=POSITIVE_DELTA,
    inventory_revision=INVENTORY_REVISION,
    inventory_delta=POSITIVE_DELTA,
    ids=st.lists(CANONICAL_UUID, min_size=3, max_size=3, unique=True),
)
def test_stale_to_fresh_requires_newer_successful_commit(
    committed: int,
    commit_delta: int,
    published_revision: int,
    published_delta: int,
    inventory_revision: int,
    inventory_delta: int,
    ids: list[str],
) -> None:
    backend_id, source_id, endpoint_id = ids
    committed_source = _successful_source(
        source_id=source_id,
        endpoint_id=endpoint_id,
        run_sequence=committed,
    )
    previous = _snapshot(
        backend_id=backend_id,
        sources=(_time_expiry_source(committed_source),),
        inventory_revision=inventory_revision,
        published_revision=published_revision,
    )
    same_commit = _snapshot(
        backend_id=backend_id,
        sources=(committed_source,),
        inventory_revision=inventory_revision,
        published_revision=published_revision + published_delta,
    )
    with pytest.raises(
        ValueError,
        match="a stale source requires a newer successful inventory commit before returning to fresh",
    ):
        same_commit.validate_revision_successor(previous)

    recovered_source = _successful_source(
        source_id=source_id,
        endpoint_id=endpoint_id,
        run_sequence=committed + commit_delta,
    )
    recovered = _snapshot(
        backend_id=backend_id,
        sources=(recovered_source,),
        inventory_revision=inventory_revision + inventory_delta,
        published_revision=published_revision + published_delta,
    )
    recovered.validate_revision_successor(previous)


@settings(max_examples=100, deadline=None)
@given(
    inventory_revision=INVENTORY_REVISION,
    published_revision=POSITIVE_REVISION,
    continuity_revision=POSITIVE_REVISION,
    run_sequence=POSITIVE_REVISION,
    inventory_delta=POSITIVE_DELTA,
    published_delta=POSITIVE_DELTA,
    continuity_delta=POSITIVE_DELTA,
    run_delta=POSITIVE_DELTA,
    previous_security=st.sampled_from(
        (SecurityContinuity.UNVERIFIED, SecurityContinuity.TRUSTED)
    ),
    vmid=VMID,
    generation=GENERATION,
    ids=st.lists(CANONICAL_UUID, min_size=5, max_size=5, unique=True),
)
def test_polling_gaps_do_not_imply_adjacency_or_identity_replacement(
    inventory_revision: int,
    published_revision: int,
    continuity_revision: int,
    run_sequence: int,
    inventory_delta: int,
    published_delta: int,
    continuity_delta: int,
    run_delta: int,
    previous_security: SecurityContinuity,
    vmid: int,
    generation: int,
    ids: list[str],
) -> None:
    backend_id, source_id, endpoint_id, resource_id, binding_id = ids
    previous_source = _successful_source(
        source_id=source_id,
        endpoint_id=endpoint_id,
        run_sequence=run_sequence,
    )
    old = _security_resource(
        previous_security,
        resource_id=resource_id,
        source_id=source_id,
        binding_id=binding_id,
        vmid=vmid,
        generation=generation,
        continuity_revision=continuity_revision,
    )
    previous = _snapshot(
        backend_id=backend_id,
        sources=(previous_source,),
        resources=(old,),
        inventory_revision=inventory_revision,
        published_revision=published_revision,
    )
    current_security = (
        SecurityContinuity.REVOKED
        if previous_security is SecurityContinuity.TRUSTED
        else SecurityContinuity.UNVERIFIED
    )
    ambiguous = _security_resource(
        current_security,
        resource_id=resource_id,
        source_id=source_id,
        binding_id=binding_id,
        vmid=vmid,
        generation=generation,
        continuity_revision=continuity_revision + continuity_delta,
    )
    current = _snapshot(
        backend_id=backend_id,
        sources=(
            _successful_source(
                source_id=source_id,
                endpoint_id=endpoint_id,
                run_sequence=run_sequence + run_delta,
            ),
        ),
        resources=(ambiguous,),
        inventory_revision=inventory_revision + inventory_delta,
        published_revision=published_revision + published_delta,
    )

    current.validate_revision_successor(previous)
    retained = current.resources[0]
    assert retained.resource_id == old.resource_id
    assert retained.active_binding_id == old.active_binding_id
    assert retained.locator_generation == old.locator_generation


@settings(max_examples=100, deadline=None)
@given(
    data=st.data(),
    start_generation=GENERATION,
    history_length=st.integers(min_value=1, max_value=5),
    vmid=VMID,
)
def test_locator_history_accepts_arbitrary_retained_minimum(
    data: st.DataObject,
    start_generation: int,
    history_length: int,
    vmid: int,
) -> None:
    ids = data.draw(
        st.lists(
            CANONICAL_UUID,
            min_size=history_length + 5,
            max_size=history_length + 5,
            unique=True,
        )
    )
    backend_id, source_id, endpoint_id, binding_id, *resource_ids = ids
    terminal = tuple(
        _removed_resource(
            resource_id=resource_ids[index],
            source_id=source_id,
            vmid=vmid,
            generation=start_generation + index,
        )
        for index in range(history_length)
    )
    current = _resource(
        resource_id=resource_ids[-1],
        source_id=source_id,
        binding_id=binding_id,
        vmid=vmid,
        generation=start_generation + history_length,
        continuity_revision=1,
    )
    view = _snapshot(
        backend_id=backend_id,
        sources=(_successful_source(source_id=source_id, endpoint_id=endpoint_id),),
        resources=(*terminal, current),
        inventory_revision=1,
        published_revision=1,
    )

    assert current.locator_generation == (
        max(item.locator_generation for item in terminal) + 1
    )
    assert view.current_resources_by_locator[(source_id, vmid)] is current


@settings(max_examples=100, deadline=None)
@given(
    data=st.data(),
    start_generation=GENERATION,
    history_length=st.integers(min_value=3, max_value=5),
    vmid=VMID,
)
def test_locator_history_rejects_internal_generation_gap(
    data: st.DataObject,
    start_generation: int,
    history_length: int,
    vmid: int,
) -> None:
    ids = data.draw(
        st.lists(
            CANONICAL_UUID,
            min_size=history_length + 5,
            max_size=history_length + 5,
            unique=True,
        )
    )
    removed_index = data.draw(
        st.integers(min_value=1, max_value=history_length - 2)
    )
    backend_id, source_id, endpoint_id, binding_id, *resource_ids = ids
    terminal = tuple(
        _removed_resource(
            resource_id=resource_ids[index],
            source_id=source_id,
            vmid=vmid,
            generation=start_generation + index,
        )
        for index in range(history_length)
    )
    current = _resource(
        resource_id=resource_ids[-1],
        source_id=source_id,
        binding_id=binding_id,
        vmid=vmid,
        generation=start_generation + history_length,
        continuity_revision=1,
    )
    common = {
        "backend_id": backend_id,
        "sources": (
            _successful_source(source_id=source_id, endpoint_id=endpoint_id),
        ),
        "inventory_revision": 1,
        "published_revision": 1,
    }
    _snapshot(resources=(*terminal, current), **common)

    gapped = terminal[:removed_index] + terminal[removed_index + 1 :]
    with pytest.raises(
        ValueError,
        match="retained locator generations must be consecutive",
    ):
        _snapshot(resources=(*gapped, current), **common)


@settings(max_examples=100, deadline=None)
@given(
    data=st.data(),
    start_a=GENERATION,
    start_b=GENERATION,
    length_a=st.integers(min_value=1, max_value=5),
    length_b=st.integers(min_value=1, max_value=5),
    vmid=VMID,
)
def test_locator_generations_are_independent_between_sources(
    data: st.DataObject,
    start_a: int,
    start_b: int,
    length_a: int,
    length_b: int,
    vmid: int,
) -> None:
    total = length_a + length_b + 9
    ids = data.draw(
        st.lists(CANONICAL_UUID, min_size=total, max_size=total, unique=True)
    )
    (
        backend_id,
        source_a,
        endpoint_a,
        binding_a,
        source_b,
        endpoint_b,
        binding_b,
        *resource_ids,
    ) = ids
    a_ids = resource_ids[: length_a + 1]
    b_ids = resource_ids[length_a + 1 : length_a + length_b + 2]

    def history(
        source_id: str,
        binding_id: str,
        generated_ids: list[str],
        start: int,
        length: int,
    ) -> tuple[ResourceSnapshot, ...]:
        terminal = tuple(
            _removed_resource(
                resource_id=generated_ids[index],
                source_id=source_id,
                vmid=vmid,
                generation=start + index,
            )
            for index in range(length)
        )
        current = _resource(
            resource_id=generated_ids[-1],
            source_id=source_id,
            binding_id=binding_id,
            vmid=vmid,
            generation=start + length,
            continuity_revision=1,
        )
        return (*terminal, current)

    resources = (
        *history(source_a, binding_a, a_ids, start_a, length_a),
        *history(source_b, binding_b, b_ids, start_b, length_b),
    )
    view = _snapshot(
        backend_id=backend_id,
        sources=(
            _successful_source(source_id=source_a, endpoint_id=endpoint_a),
            _successful_source(source_id=source_b, endpoint_id=endpoint_b),
        ),
        resources=resources,
        inventory_revision=1,
        published_revision=1,
    )

    assert set(view.current_resources_by_locator) == {
        (source_a, vmid),
        (source_b, vmid),
    }


@settings(max_examples=100, deadline=None)
@given(
    run_sequence=POSITIVE_REVISION,
    run_delta=POSITIVE_DELTA,
    inventory_revision=INVENTORY_REVISION,
    inventory_delta=POSITIVE_DELTA,
    published_revision=POSITIVE_REVISION,
    published_delta=POSITIVE_DELTA,
    vmid=VMID,
    generation=GENERATION,
    continuity_revision=POSITIVE_REVISION,
    ids=st.lists(CANONICAL_UUID, min_size=6, max_size=6, unique=True),
)
def test_active_binding_is_immutable_before_terminal_transition(
    run_sequence: int,
    run_delta: int,
    inventory_revision: int,
    inventory_delta: int,
    published_revision: int,
    published_delta: int,
    vmid: int,
    generation: int,
    continuity_revision: int,
    ids: list[str],
) -> None:
    backend_id, source_id, endpoint_id, resource_id, binding_x, binding_y = ids
    previous_resource = _resource(
        resource_id=resource_id,
        source_id=source_id,
        binding_id=binding_x,
        vmid=vmid,
        generation=generation,
        continuity_revision=continuity_revision,
    )
    current_resource = replace(previous_resource, active_binding_id=binding_y)
    previous = _snapshot(
        backend_id=backend_id,
        sources=(
            _successful_source(
                source_id=source_id,
                endpoint_id=endpoint_id,
                run_sequence=run_sequence,
            ),
        ),
        resources=(previous_resource,),
        inventory_revision=inventory_revision,
        published_revision=published_revision,
    )
    current = _snapshot(
        backend_id=backend_id,
        sources=(
            _successful_source(
                source_id=source_id,
                endpoint_id=endpoint_id,
                run_sequence=run_sequence + run_delta,
            ),
        ),
        resources=(current_resource,),
        inventory_revision=inventory_revision + inventory_delta,
        published_revision=published_revision + published_delta,
    )

    with pytest.raises(
        ValueError,
        match="active binding is immutable before a terminal transition",
    ):
        current.validate_revision_successor(previous)


@settings(max_examples=100, deadline=None)
@given(
    run_sequence=POSITIVE_REVISION,
    run_delta=POSITIVE_DELTA,
    inventory_revision=INVENTORY_REVISION,
    inventory_delta=POSITIVE_DELTA,
    published_revision=POSITIVE_REVISION,
    published_delta=POSITIVE_DELTA,
    vmid=VMID,
    generation=GENERATION,
    continuity_revision=POSITIVE_REVISION,
    continuity_delta=POSITIVE_DELTA,
    ids=st.lists(CANONICAL_UUID, min_size=6, max_size=6, unique=True),
)
def test_active_binding_owner_cannot_move_between_resources(
    run_sequence: int,
    run_delta: int,
    inventory_revision: int,
    inventory_delta: int,
    published_revision: int,
    published_delta: int,
    vmid: int,
    generation: int,
    continuity_revision: int,
    continuity_delta: int,
    ids: list[str],
) -> None:
    backend_id, source_id, endpoint_id, resource_a, resource_b, binding_x = ids
    previous_resource = _resource(
        resource_id=resource_a,
        source_id=source_id,
        binding_id=binding_x,
        vmid=vmid,
        generation=generation,
        continuity_revision=continuity_revision,
    )
    closed_a = _removed_resource(
        resource_id=resource_a,
        source_id=source_id,
        vmid=vmid,
        generation=generation,
        continuity_revision=continuity_revision + continuity_delta,
    )
    current_b = _resource(
        resource_id=resource_b,
        source_id=source_id,
        binding_id=binding_x,
        vmid=vmid,
        generation=generation + 1,
        continuity_revision=1,
    )
    previous = _snapshot(
        backend_id=backend_id,
        sources=(
            _successful_source(
                source_id=source_id,
                endpoint_id=endpoint_id,
                run_sequence=run_sequence,
            ),
        ),
        resources=(previous_resource,),
        inventory_revision=inventory_revision,
        published_revision=published_revision,
    )
    current = _snapshot(
        backend_id=backend_id,
        sources=(
            _successful_source(
                source_id=source_id,
                endpoint_id=endpoint_id,
                run_sequence=run_sequence + run_delta,
            ),
        ),
        resources=(closed_a, current_b),
        inventory_revision=inventory_revision + inventory_delta,
        published_revision=published_revision + published_delta,
    )

    with pytest.raises(
        ValueError,
        match="active binding identity cannot move between resources",
    ):
        current.validate_revision_successor(previous)


@settings(max_examples=100, deadline=None)
@given(
    config_revision=POSITIVE_REVISION,
    version=POSITIVE_REVISION,
    config_delta=POSITIVE_DELTA,
    version_delta=POSITIVE_DELTA,
    published_revision=POSITIVE_REVISION,
    published_delta=POSITIVE_DELTA,
    inventory_revision=INVENTORY_REVISION,
    locator_label=DNS_LABEL,
    ids=st.lists(CANONICAL_UUID, min_size=3, max_size=3, unique=True),
)
def test_source_context_canonicalization_migration_is_controlled(
    config_revision: int,
    version: int,
    config_delta: int,
    version_delta: int,
    published_revision: int,
    published_delta: int,
    inventory_revision: int,
    locator_label: str,
    ids: list[str],
) -> None:
    backend_id, source_id, endpoint_id = ids
    old_locator = f"https://{locator_label}.example.test"
    new_locator = f"https://{locator_label.upper()}.example.test:443/"
    # Model a versioned representation change of one retained endpoint namespace,
    # not replacement with a different transport target.
    committed_context = _context(
        endpoint_id=endpoint_id,
        config_revision=config_revision,
        locator=old_locator,
        canonicalization_version=version,
    )
    previous_source = _successful_source(
        source_id=source_id,
        endpoint_id=endpoint_id,
        context=committed_context,
    )
    previous = _snapshot(
        backend_id=backend_id,
        sources=(previous_source,),
        inventory_revision=inventory_revision,
        published_revision=published_revision,
    )
    migrated_context = _context(
        endpoint_id=endpoint_id,
        config_revision=config_revision + config_delta,
        locator=new_locator,
        canonicalization_version=version + version_delta,
    )
    migrated_source = _controlled_source(
        previous_source,
        current_context=migrated_context,
        committed_context=committed_context,
    )
    migrated = _snapshot(
        backend_id=backend_id,
        sources=(migrated_source,),
        inventory_revision=inventory_revision,
        published_revision=published_revision + published_delta,
    )
    migrated.validate_revision_successor(previous)

    with pytest.raises(
        ValueError,
        match="one canonicalization contract version cannot reinterpret the committed transport locator",
    ):
        _controlled_source(
            previous_source,
            current_context=_context(
                endpoint_id=endpoint_id,
                config_revision=config_revision + config_delta,
                locator=new_locator,
                canonicalization_version=version,
            ),
            committed_context=committed_context,
        )

    with pytest.raises(
        ValueError,
        match="canonicalization migration requires newer current source configuration",
    ):
        _controlled_source(
            previous_source,
            current_context=_context(
                endpoint_id=endpoint_id,
                config_revision=config_revision,
                locator=new_locator,
                canonicalization_version=version + version_delta,
            ),
            committed_context=committed_context,
        )


@settings(max_examples=100, deadline=None)
@given(
    available=st.booleans(),
    vmid=VMID,
    generation=GENERATION,
    continuity_revision=POSITIVE_REVISION,
    ids=st.lists(CANONICAL_UUID, min_size=6, max_size=6, unique=True),
)
def test_node_availability_agreement_is_same_view_metamorphic_property(
    available: bool,
    vmid: int,
    generation: int,
    continuity_revision: int,
    ids: list[str],
) -> None:
    backend_id, source_id, endpoint_id, node_id, resource_id, binding_id = ids
    node = NodeSnapshot(
        node_id=node_id,
        inventory_source_id=source_id,
        name="Property node",
        status="online" if available else "offline",
        available=available,
        facts={},
    )
    matching_availability = (
        NodeAvailability.AVAILABLE if available else NodeAvailability.UNAVAILABLE
    )
    resource = _resource(
        resource_id=resource_id,
        source_id=source_id,
        binding_id=binding_id,
        vmid=vmid,
        generation=generation,
        continuity_revision=continuity_revision,
        current_node_id=node_id,
        node_availability=matching_availability,
    )
    common = {
        "backend_id": backend_id,
        "sources": (
            _successful_source(source_id=source_id, endpoint_id=endpoint_id),
        ),
        "nodes": (node,),
        "inventory_revision": 1,
        "published_revision": 1,
    }
    _snapshot(resources=(resource,), **common)

    opposite = (
        NodeAvailability.UNAVAILABLE
        if available
        else NodeAvailability.AVAILABLE
    )
    with pytest.raises(
        ValueError,
        match="resource node availability disagrees with node record",
    ):
        _snapshot(resources=(replace(resource, node_availability=opposite),), **common)
