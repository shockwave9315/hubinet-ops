"""Exact package-plan approval portion of one resource snapshot."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from .enums import PackagePlanApprovalStatus
from .primitives import _require_enum_instance, _require_text, _require_uuid_identity

if TYPE_CHECKING:
    from .models import PackagePlanApprovalSnapshot


def validate_package_plan_approval_snapshot(
    approval: "PackagePlanApprovalSnapshot",
) -> None:
    _require_enum_instance(
        approval.status, PackagePlanApprovalStatus, "package_plan_approval.status"
    )
    if not isinstance(approval.approvable, bool):
        raise ValueError("package_plan_approval.approvable must be a boolean")

    historical = (
        approval.approval_id,
        approval.reviewed_scan_run_id,
        approval.plan_fingerprint,
        approval.approved_at,
    )
    if approval.status is PackagePlanApprovalStatus.NONE:
        if any(value is not None for value in historical):
            raise ValueError("an absent package plan approval has no historical fields")
        return

    if any(value is None for value in historical):
        raise ValueError("a historical package plan approval requires all fields")
    _require_uuid_identity(approval.approval_id, "package_plan_approval.approval_id")
    _require_uuid_identity(
        approval.reviewed_scan_run_id,
        "package_plan_approval.reviewed_scan_run_id",
    )
    if not isinstance(approval.plan_fingerprint, str) or not re.fullmatch(
        r"[0-9a-f]{64}", approval.plan_fingerprint
    ):
        raise ValueError("package_plan_approval.plan_fingerprint is malformed")
    _require_text(approval.approved_at, "package_plan_approval.approved_at")
    if (
        approval.status is PackagePlanApprovalStatus.APPROVED
        and not approval.approvable
    ):
        raise ValueError("an effective package plan approval must be approvable")
