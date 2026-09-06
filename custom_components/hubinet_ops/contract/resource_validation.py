"""Single-resource legality validation for the snapshot contract."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .enums import (
    DetailStatus,
    HealthContractStatus,
    LifecycleState,
    NodeAvailability,
    ObservationalContinuity,
    PackageUpdateJobState,
    PackagePlanApprovalStatus,
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
    from .models import HubinetOpsSnapshot, OperatorAvailabilityView, ResourceSnapshot


def validate_resource_snapshot(self: "ResourceSnapshot") -> None:
    from .models import (
        HealthContractSummary,
        PackageUpdateJobSummary,
        PackagePlanApprovalSnapshot,
        PackageScanSnapshot,
    )

    if not isinstance(self.package_scan, PackageScanSnapshot):
        raise ValueError("package_scan must be a PackageScanSnapshot")
    if not isinstance(self.package_plan_approval, PackagePlanApprovalSnapshot):
        raise ValueError(
            "package_plan_approval must be a PackagePlanApprovalSnapshot"
        )
    if not isinstance(self.health_contract, HealthContractSummary):
        raise ValueError("health_contract must be a HealthContractSummary")
    if not isinstance(self.package_update_job, PackageUpdateJobSummary):
        raise ValueError("package_update_job must be a PackageUpdateJobSummary")
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



def validate_operator_availability(
    view: "OperatorAvailabilityView", snapshot: "HubinetOpsSnapshot"
) -> None:
    """Bind volatile backend facts to one coherent authority projection.

    This verifies consistency; it never derives permission. The backend still
    supplies every boolean and every mutation endpoint re-proves its own rule.
    """

    if view.backend_instance_id != snapshot.backend.backend_instance_id:
        raise ValueError("operator availability names a different backend")
    if (
        view.authority_published_state_revision
        != snapshot.published_state_revision
    ):
        raise ValueError("operator availability is not aligned to the snapshot")
    capabilities_by_id = view.resources_by_id
    if set(capabilities_by_id) != set(snapshot.resources_by_id):
        raise ValueError("operator availability resources do not match the snapshot")

    any_active_job = any(
        resource.package_update_job.state is PackageUpdateJobState.ACTIVE
        for resource in snapshot.resources
    )
    for resource_id, capabilities in capabilities_by_id.items():
        resource = snapshot.resources_by_id[resource_id]
        if resource.resource_type is not ResourceType.LXC and any(
            getattr(capabilities, name)
            for name in capabilities.__dataclass_fields__
        ):
            raise ValueError(
                "unsupported resources cannot publish operator availability"
            )
        if (
            capabilities.can_review_update_plan
            or capabilities.can_approve_update_plan
        ) and not resource.package_plan_approval.approvable:
            raise ValueError("plan availability requires backend approvability")
        has_job = resource.package_update_job.state not in {
            PackageUpdateJobState.UNSUPPORTED,
            PackageUpdateJobState.NOT_STARTED,
        }
        if capabilities.can_view_update_job and not has_job:
            raise ValueError("job view availability requires a published job")
        if capabilities.can_resume_update and (
            resource.package_update_job.state is not PackageUpdateJobState.ACTIVE
        ):
            raise ValueError("resume availability requires an active job")
        if (
            capabilities.can_rollback_update
            and not resource.package_update_job.rollback_available
        ):
            raise ValueError(
                "rollback availability requires backend rollback authority"
            )
        if capabilities.can_start_update and (
            resource.package_plan_approval.status
            is not PackagePlanApprovalStatus.APPROVED
            or resource.health_contract.status
            is not HealthContractStatus.CONFIGURED
            or any_active_job
        ):
            raise ValueError(
                "start availability contradicts the revisioned authority view"
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
