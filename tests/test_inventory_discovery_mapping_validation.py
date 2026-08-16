from __future__ import annotations

from typing import Any

import pytest

from app.inventory.discovery import (
    BaselineCompleteness,
    BaselineMode,
    DetailReadStatus,
    DiscoveredNode,
    DiscoveredResource,
    NormalizedDiscoverySnapshot,
    SourceAvailability,
)


SOURCE_ID = "11111111-1111-4111-8111-111111111111"
RUN_ID = "22222222-2222-4222-8222-222222222222"
ENDPOINT_ID = "33333333-3333-4333-8333-333333333333"
OBSERVED_AT = "2026-08-15T12:00:00+00:00"


@pytest.mark.parametrize("value", [[], "", 0, False])
def test_node_rejects_falsey_non_mapping_facts(value: Any) -> None:
    with pytest.raises(TypeError, match="must be a mapping"):
        DiscoveredNode("pve-a", "online", True, OBSERVED_AT, value)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [[], "", 0, False])
def test_resource_rejects_falsey_non_mapping_facts(value: Any) -> None:
    with pytest.raises(TypeError, match="must be a mapping"):
        DiscoveredResource(
            SOURCE_ID,
            100,
            "qemu",
            "guest",
            "running",
            "pve-a",
            OBSERVED_AT,
            DetailReadStatus.OK,
            value,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("value", [[], "", 0, False])
def test_resource_rejects_falsey_non_mapping_continuity_evidence(value: Any) -> None:
    with pytest.raises(TypeError, match="must be a mapping"):
        DiscoveredResource(
            SOURCE_ID,
            100,
            "qemu",
            "guest",
            "running",
            "pve-a",
            OBSERVED_AT,
            DetailReadStatus.OK,
            {},
            (value,),  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("value", [[], "", 0, False])
def test_snapshot_rejects_falsey_non_mapping_source_facts(value: Any) -> None:
    with pytest.raises(TypeError, match="must be a mapping"):
        NormalizedDiscoverySnapshot(
            run_id=RUN_ID,
            discovery_run_sequence=1,
            inventory_source_id=SOURCE_ID,
            expected_source_config_revision=1,
            endpoint_id=ENDPOINT_ID,
            canonical_transport_locator="https://pve.example:8006",
            canonicalization_contract_version=1,
            expected_transport_trust_revision=1,
            provider_contract_version=1,
            observed_at=OBSERVED_AT,
            source_facts=value,  # type: ignore[arg-type]
            source_availability=SourceAvailability.AVAILABLE,
            baseline_completeness=BaselineCompleteness.PARTIAL,
            baseline_mode=BaselineMode.CLUSTER,
            acl_topology_hash_before=None,
            acl_topology_hash_after=None,
            permission_snapshot_hash_before=None,
            permission_snapshot_hash_after=None,
            permission_coverage_complete=False,
            boundary_consistent=False,
            covered_nodes=(),
            failed_baseline_scopes=("/nodes",),
            detail_summary={
                "ok_count": 0,
                "temporarily_unavailable_count": 0,
                "error_count": 0,
            },
            failed_detail_scopes=(),
            nodes=(),
            resources=(),
        )


def test_none_mapping_value_remains_supported_as_empty() -> None:
    node = DiscoveredNode("pve-a", "online", True, OBSERVED_AT, None)  # type: ignore[arg-type]
    assert dict(node.facts) == {}
