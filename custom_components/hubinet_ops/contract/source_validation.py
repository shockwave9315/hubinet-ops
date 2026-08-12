"""Single-view validation for source context, health, and run provenance."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .enums import SourceFreshness, SourceHealth, SourceHealthOrigin
from .primitives import (
    _immutable_mapping,
    _require_enum_instance,
    _require_optional_positive,
    _require_positive,
    _require_text,
    _require_uuid_identity,
)

if TYPE_CHECKING:
    from .models import InventorySourceSnapshot, SourceContext


_SUCCESSFUL_RUN_OUTCOME = "success"


def validate_source_context(self: "SourceContext") -> None:
    _require_positive(self.source_config_revision, "source_config_revision")
    _require_uuid_identity(self.endpoint_id, "endpoint_id")
    _require_text(
        self.canonical_transport_locator, "canonical_transport_locator"
    )
    _require_positive(
        self.canonicalization_contract_version,
        "canonicalization_contract_version",
    )
    _require_positive(
        self.transport_trust_revision, "transport_trust_revision"
    )


def validate_inventory_source_snapshot(self: "InventorySourceSnapshot") -> None:
    _require_enum_instance(self.health, SourceHealth, "health")
    _require_enum_instance(self.freshness, SourceFreshness, "freshness")
    _require_enum_instance(
        self.health_origin, SourceHealthOrigin, "health_origin"
    )
    _require_uuid_identity(self.inventory_source_id, "inventory_source_id")
    _require_text(self.name, "source name")
    _require_text(self.provider_kind, "provider_kind")
    if (
        type(self.last_issued_run_sequence) is not int
        or self.last_issued_run_sequence < 0
    ):
        raise ValueError("last_issued_run_sequence must be a non-negative integer")
    for field_name in (
        "latest_completed_run_sequence",
        "last_health_run_sequence",
        "last_committed_run_sequence",
    ):
        _require_optional_positive(getattr(self, field_name), field_name)
    _validate_sequence_pair(
        self.latest_completed_run_sequence,
        self.latest_completed_outcome,
        "latest completed run",
    )
    _validate_sequence_pair(
        self.last_health_run_sequence,
        self.last_run_health_outcome,
        "last health run",
    )
    _validate_run_sequence_lattice(self)
    _validate_committed_run_outcomes(self)

    successful_fields = (
        self.last_successful_observed_at,
        self.freshness_reference_at,
        self.freshness_valid_until,
        self.committed_context,
    )
    has_successful_commit = self.last_committed_run_sequence is not None
    if (
        has_successful_commit
        and not all(item is not None for item in successful_fields)
    ) or (
        not has_successful_commit
        and any(item is not None for item in successful_fields)
    ):
        raise ValueError(
            "committed run, fixed freshness facts, and committed context must be published together"
        )
    for field_name in (
        "last_successful_observed_at",
        "freshness_reference_at",
        "freshness_valid_until",
    ):
        value = getattr(self, field_name)
        if value is not None:
            _require_text(value, field_name)

    _validate_context_provenance(self)

    if self.health_origin is SourceHealthOrigin.INITIAL:
        if self.health is not SourceHealth.NOT_YET_OBSERVED:
            raise ValueError("initial source health must be not_yet_observed")
        if self.freshness is not SourceFreshness.NOT_YET_OBSERVED:
            raise ValueError("initial source freshness must be not_yet_observed")
        if self.last_health_run_sequence is not None or has_successful_commit:
            raise ValueError(
                "initial source health cannot have applied run or committed provenance"
            )
    else:
        _require_text(self.health_reason, "health_reason")
        if self.freshness is SourceFreshness.NOT_YET_OBSERVED:
            raise ValueError("non-initial source cannot be not_yet_observed")

    if self.health_origin is SourceHealthOrigin.DISCOVERY_RUN:
        if self.last_health_run_sequence is None:
            raise ValueError("discovery_run health origin requires run provenance")
    if self.health_origin is SourceHealthOrigin.TIME_EXPIRY:
        if (
            not has_successful_commit
            or self.freshness is not SourceFreshness.STALE
            or self.current_context != self.committed_context
            or self.last_health_run_sequence
            != self.last_committed_run_sequence
        ):
            raise ValueError(
                "time_expiry requires stale exact committed run and context provenance"
            )
    if self.health_origin is SourceHealthOrigin.CONTROLLED_CONTEXT_TRANSITION:
        if self.freshness is not SourceFreshness.STALE:
            raise ValueError("controlled context transition must be stale")

    if self.freshness is SourceFreshness.FRESH:
        if (
            self.health is not SourceHealth.HEALTHY
            or self.health_origin is not SourceHealthOrigin.DISCOVERY_RUN
            or not has_successful_commit
            or self.current_context != self.committed_context
            or self.last_health_run_sequence
            != self.last_committed_run_sequence
        ):
            raise ValueError(
                "fresh source requires a healthy authoritative discovery commit"
            )
    elif self.health not in {SourceHealth.HEALTHY, SourceHealth.NOT_YET_OBSERVED}:
        if self.freshness is not SourceFreshness.STALE:
            raise ValueError("unhealthy source must be stale")

    object.__setattr__(self, "facts", _immutable_mapping(self.facts))


def _validate_sequence_pair(
    sequence: int | None, outcome: str | None, label: str
) -> None:
    if (sequence is None) != (outcome is None):
        raise ValueError(f"{label} sequence and outcome must be published together")
    if outcome is not None:
        _require_text(outcome, f"{label} outcome")


def _validate_run_sequence_lattice(self) -> None:
    sequence_lattice = (
        ("last_committed_run_sequence", self.last_committed_run_sequence),
        ("last_health_run_sequence", self.last_health_run_sequence),
        ("latest_completed_run_sequence", self.latest_completed_run_sequence),
        ("last_issued_run_sequence", self.last_issued_run_sequence),
    )
    for (lower_name, lower), (upper_name, upper) in zip(
        sequence_lattice, sequence_lattice[1:]
    ):
        if lower is not None and (upper is None or lower > upper):
            raise ValueError(
                "source run provenance must satisfy "
                "last_committed_run_sequence <= last_health_run_sequence "
                "<= latest_completed_run_sequence <= last_issued_run_sequence "
                f"({lower_name} exceeds {upper_name})"
            )


def _validate_committed_run_outcomes(self) -> None:
    committed_sequence = self.last_committed_run_sequence
    if (
        self.last_run_health_outcome == _SUCCESSFUL_RUN_OUTCOME
        and self.last_health_run_sequence != committed_sequence
    ):
        raise ValueError(
            "successful applied health requires the exact committed run sequence"
        )
    if (
        self.latest_completed_outcome == _SUCCESSFUL_RUN_OUTCOME
        and self.latest_completed_run_sequence != committed_sequence
    ):
        raise ValueError(
            "successful completion requires the exact committed run sequence"
        )
    if committed_sequence is None:
        return
    if (
        self.last_health_run_sequence == committed_sequence
        and self.last_run_health_outcome != _SUCCESSFUL_RUN_OUTCOME
    ):
        raise ValueError(
            "the committed run must retain a successful health outcome"
        )
    if (
        self.latest_completed_run_sequence == committed_sequence
        and self.latest_completed_outcome != _SUCCESSFUL_RUN_OUTCOME
    ):
        raise ValueError(
            "the committed run must retain a successful completion outcome"
        )


def _validate_context_provenance(self) -> None:
    committed = self.committed_context
    if committed is None:
        return

    current = self.current_context
    if current.source_config_revision < committed.source_config_revision:
        raise ValueError(
            "current source_config_revision cannot predate committed context"
        )
    if current.transport_trust_revision < committed.transport_trust_revision:
        raise ValueError(
            "current transport_trust_revision cannot predate committed context"
        )
    if current.endpoint_id != committed.endpoint_id:
        raise ValueError(
            "current and committed context must reference the same endpoint"
        )
    if (
        current.canonicalization_contract_version
        < committed.canonicalization_contract_version
    ):
        raise ValueError(
            "current canonicalization contract cannot predate committed context"
        )
    if (
        current.canonicalization_contract_version
        == committed.canonicalization_contract_version
    ):
        if (
            current.canonical_transport_locator
            != committed.canonical_transport_locator
        ):
            raise ValueError(
                "one canonicalization contract version cannot reinterpret "
                "the committed transport locator"
            )
    elif current.source_config_revision <= committed.source_config_revision:
        raise ValueError(
            "canonicalization migration requires newer current source configuration"
        )
