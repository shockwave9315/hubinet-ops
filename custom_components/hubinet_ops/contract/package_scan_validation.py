"""Package-scan portion of one resource snapshot."""

from __future__ import annotations

import hashlib
import json
import re
from typing import TYPE_CHECKING

from .enums import PackageScanStatus
from .primitives import _require_enum_instance, _require_text, _require_uuid_identity

if TYPE_CHECKING:
    from .models import PackageScanSnapshot


def validate_package_scan_snapshot(scan: "PackageScanSnapshot") -> None:
    _require_enum_instance(scan.status, PackageScanStatus, "package_scan.status")
    object.__setattr__(scan, "packages", tuple(scan.packages))
    if scan.reboot_required not in {True, None}:
        raise ValueError("package_scan.reboot_required must be true or unknown")
    if scan.scan_run_id is not None:
        _require_uuid_identity(scan.scan_run_id, "package_scan.scan_run_id")
    if scan.started_at is not None:
        _require_text(scan.started_at, "package_scan.started_at")
    if scan.completed_at is not None:
        _require_text(scan.completed_at, "package_scan.completed_at")
    if scan.pending_count is not None and (
        type(scan.pending_count) is not int or scan.pending_count < 0
    ):
        raise ValueError("package_scan.pending_count must be non-negative or unknown")

    empty_evidence = (
        scan.pending_count is None
        and scan.plan_fingerprint is None
        and scan.reboot_required is None
        and not scan.packages
    )
    if scan.status in {PackageScanStatus.UNSUPPORTED, PackageScanStatus.NOT_SCANNED}:
        if any(
            value is not None
            for value in (
                scan.scan_run_id,
                scan.started_at,
                scan.completed_at,
                scan.os,
                scan.error,
            )
        ) or not empty_evidence:
            raise ValueError("unattempted package scan cannot publish scan evidence")
        return
    if scan.scan_run_id is None or scan.started_at is None:
        raise ValueError("attempted package scan requires run identity and start time")
    if scan.status is PackageScanStatus.SCANNING:
        if scan.completed_at is not None or scan.os is not None or scan.error is not None or not empty_evidence:
            raise ValueError("running package scan cannot publish terminal evidence")
        return
    if scan.completed_at is None:
        raise ValueError("terminal package scan requires completion time")
    if scan.status is PackageScanStatus.SUCCESS:
        if scan.os is None or scan.error is not None:
            raise ValueError("successful package scan requires OS and forbids error")
        if scan.os.os_id not in {"debian", "ubuntu"}:
            raise ValueError("successful package scan OS is unsupported")
        if scan.pending_count != len(scan.packages):
            raise ValueError("package count does not match exact package rows")
        if not isinstance(scan.plan_fingerprint, str) or not re.fullmatch(
            r"[0-9a-f]{64}", scan.plan_fingerprint
        ):
            raise ValueError("successful package scan fingerprint is malformed")
        identities = [(package.name, package.architecture) for package in scan.packages]
        if identities != sorted(identities) or len(identities) != len(set(identities)):
            raise ValueError("package rows must be unique and deterministic")
        material = [
            {
                "architecture": package.architecture,
                "candidate_version": package.candidate_version,
                "installed_version": package.installed_version,
                "package_name": package.name,
            }
            for package in scan.packages
        ]
        encoded = json.dumps(
            material, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        if hashlib.sha256(encoded).hexdigest() != scan.plan_fingerprint:
            raise ValueError("package scan fingerprint does not match exact package rows")
        return
    if scan.status in {PackageScanStatus.FAILED, PackageScanStatus.INTERRUPTED}:
        if scan.error is None or not empty_evidence:
            raise ValueError("failed package scan requires only bounded error evidence")
        return
    if scan.status is PackageScanStatus.UNAVAILABLE:
        if scan.error is not None or not empty_evidence:
            raise ValueError("unavailable package scan cannot publish stale plan evidence")
        return
    raise ValueError("unknown package scan status")
