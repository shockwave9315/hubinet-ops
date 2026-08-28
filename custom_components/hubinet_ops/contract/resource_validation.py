"""Single-resource legality validation for the snapshot contract."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .enums import (
    DetailStatus,
    LifecycleState,
    NodeAvailability,
    ObservationalContinuity,
    PresenceState,
    ResourceStateLevel,
    ResourceType,
    SecurityContinuity,
)
from .primitives import (
    _immutable_mapping,
    _require_enum_instance,
    _require_positive,
    _require_uuid_identity,
)

if TYPE_CHECKING:
    from .models import ResourceSnapshot


def validate_resource_snapshot(self: "ResourceSnapshot") -> None:
    from .models import PackageScanSnapshot

    if not isinstance(self.package_scan, PackageScanSnapshot):
        raise ValueError("package_scan must be a PackageScanSnapshot")
    _require_uuid_identity(self.resource_id, "resource_id")
    _require_uuid_identity(self.inventory_source_id, "inventory_source_id")
    for value, enum_type, field_name in (
        (self.resource_type, ResourceType, "resource_type"),
        (self.presence, PresenceState, "presence"),
        (self.lifecycle, LifecycleState, "lifecycle"),
        (
            self.observational_continuity,
            ObservationalContinuity,
            "observational_continuity",
        ),
        (
            self.security_continuity,
            SecurityContinuity,
            "security_continuity",
        ),
        (self.detail_status, DetailStatus, "detail_status"),
        (self.node_availability, NodeAvailability, "node_availability"),
        (self.state_level, ResourceStateLevel, "state_level"),
    ):
        _require_enum_instance(value, enum_type, field_name)
    if not isinstance(self.policy_applicable, bool):
        raise ValueError("policy_applicable must be a boolean")
    _require_positive(self.vmid, "vmid")
    _require_positive(self.locator_generation, "locator_generation")
    _require_positive(
        self.resource_continuity_revision, "resource_continuity_revision"
    )

    nonterminal = self.presence in {PresenceState.PRESENT, PresenceState.MISSING}
    if nonterminal:
        if self.active_binding_id is None:
            raise ValueError("nonterminal resource requires active_binding_id")
        _require_uuid_identity(self.active_binding_id, "active_binding_id")
    elif self.active_binding_id is not None:
        raise ValueError("terminal resource must not have an active binding")

    _validate_state_matrix(self)
    _validate_node_relation(self)
    _validate_terminal_relation(self)

    object.__setattr__(
        self, "retained_policy", _immutable_mapping(self.retained_policy)
    )
    object.__setattr__(
        self, "effective_policy", _immutable_mapping(self.effective_policy)
    )
    object.__setattr__(self, "state", _immutable_mapping(self.state))
    object.__setattr__(
        self, "effective_capabilities", frozenset(self.effective_capabilities)
    )

    policy_eligible = (
        self.presence is PresenceState.PRESENT
        and self.lifecycle is LifecycleState.ACTIVE
        and self.observational_continuity is ObservationalContinuity.CONSISTENT
        and self.security_continuity is SecurityContinuity.TRUSTED
        and self.state_level
        in {ResourceStateLevel.MANAGED, ResourceStateLevel.MAINTENANCE}
    )
    if self.policy_applicable and not policy_eligible:
        raise ValueError(
            "policy cannot be applicable outside managed/maintenance "
            "trusted current state"
        )
    if not self.policy_applicable and self.effective_capabilities:
        raise ValueError(
            "effective capabilities require backend-published policy applicability"
        )


def _validate_state_matrix(self) -> None:
    case = (
        self.presence,
        self.lifecycle,
        self.observational_continuity,
    )
    valid_security: set[SecurityContinuity]
    if case == (
        PresenceState.PRESENT,
        LifecycleState.ACTIVE,
        ObservationalContinuity.CONSISTENT,
    ):
        valid_security = {
            SecurityContinuity.UNVERIFIED,
            SecurityContinuity.TRUSTED,
        }
    elif case in {
        (
            PresenceState.PRESENT,
            LifecycleState.QUARANTINED,
            ObservationalContinuity.UNCERTAIN,
        ),
        (
            PresenceState.MISSING,
            LifecycleState.QUARANTINED,
            ObservationalContinuity.UNCERTAIN,
        ),
    }:
        valid_security = {
            SecurityContinuity.UNVERIFIED,
            SecurityContinuity.REVOKED,
        }
    elif (
        self.presence is PresenceState.CONFIRMED_REMOVED
        and self.lifecycle is LifecycleState.RETIRED
        and self.observational_continuity
        in {
            ObservationalContinuity.CONSISTENT,
            ObservationalContinuity.UNCERTAIN,
        }
    ):
        valid_security = {
            SecurityContinuity.UNVERIFIED,
            SecurityContinuity.REVOKED,
        }
    elif case == (
        PresenceState.NOT_CURRENT,
        LifecycleState.RETIRED,
        ObservationalContinuity.REPLACED,
    ):
        valid_security = {
            SecurityContinuity.UNVERIFIED,
            SecurityContinuity.REVOKED,
        }
    else:
        raise ValueError("resource axes violate the canonical state matrix")
    if self.security_continuity not in valid_security:
        raise ValueError("security continuity violates the canonical state matrix")

    if self.presence is PresenceState.PRESENT:
        if self.detail_status is DetailStatus.NOT_APPLICABLE:
            raise ValueError("present resource requires a current detail status")
    elif self.detail_status is not DetailStatus.NOT_APPLICABLE:
        raise ValueError(
            "missing, confirmed_removed, and not_current require detail_status=not_applicable"
        )


def _validate_node_relation(self) -> None:
    if self.presence is not PresenceState.PRESENT:
        if self.current_node_id is not None:
            raise ValueError("non-present resource must not have current_node_id")
        if self.node_availability is not NodeAvailability.NOT_APPLICABLE:
            raise ValueError("non-present resource node availability is not_applicable")
        if self.last_known_node_id is not None:
            _require_uuid_identity(
                self.last_known_node_id, "last_known_node_id"
            )
        return

    if self.current_node_id is None:
        if self.node_availability is not NodeAvailability.UNRESOLVED:
            raise ValueError("unresolved current node relation must be explicit")
        if self.last_known_node_id is not None:
            _require_uuid_identity(
                self.last_known_node_id, "last_known_node_id"
            )
        return

    _require_uuid_identity(self.current_node_id, "current_node_id")
    if self.last_known_node_id is not None:
        raise ValueError("resolved current node forbids last_known_node_id")
    if self.node_availability not in {
        NodeAvailability.AVAILABLE,
        NodeAvailability.UNAVAILABLE,
    }:
        raise ValueError("resolved current node requires available or unavailable")


def _validate_terminal_relation(self) -> None:
    if self.presence is PresenceState.NOT_CURRENT:
        if self.termination_reason != "replaced":
            raise ValueError("not_current resource requires replacement provenance")
        if self.successor_resource_id is None:
            raise ValueError("not_current resource requires successor_resource_id")
        _require_uuid_identity(
            self.successor_resource_id, "successor_resource_id"
        )
    elif self.presence is PresenceState.CONFIRMED_REMOVED:
        if self.termination_reason != "confirmed_removed":
            raise ValueError(
                "confirmed_removed resource requires removal provenance"
            )
        if self.successor_resource_id is not None:
            raise ValueError("confirmed_removed resource cannot name a successor")
    elif self.termination_reason is not None or self.successor_resource_id is not None:
        raise ValueError("nonterminal resource cannot publish terminal provenance")
