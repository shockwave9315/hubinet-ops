#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import re
import sys


REQUIRED_SECRETS = (
    "hubinet_ops_scan_url",
    "hubinet_ops_refresh_url",
    "hubinet_ops_retry_healthcheck_url",
    "hubinet_ops_rollback_url",
    "hubinet_ops_approve_url",
    "hubinet_ops_reject_url",
    "hubinet_ops_self_update_plan_url",
    "hubinet_ops_start_url",
    "hubinet_ops_shutdown_url",
    "hubinet_ops_reboot_url",
    "hubinet_ops_force_stop_url",
    "hubinet_ops_snapshot_create_url",
    "hubinet_ops_snapshot_restore_url",
    "hubinet_ops_snapshot_delete_url",
    "hubinet_ops_authorization",
    "hubinet_ops_host_start_url",
    "hubinet_ops_host_authorization",
    "hubinet_ops_host_offline_snapshot_restore_url",
    "hubinet_ops_host_offline_force_stop_url",
    "hubinet_ops_host_recovery_authorization",
    "hubinet_ops_webhook_id",
    "hubinet_ops_notify_service",
)

LEGACY_ENDPOINTS = {
    "hubinet_ops_approve_url": "/api/v1/plans/approve",
    "hubinet_ops_reject_url": "/api/v1/plans/reject",
}


def _values(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        match = re.match(r"^([A-Za-z0-9_]+)\s*:\s*(.*?)\s*$", line)
        if match:
            values[match.group(1)] = match.group(2).strip("\"'")
    return values


def validate(text: str) -> list[str]:
    values = _values(text)
    errors: list[str] = []
    missing = [key for key in REQUIRED_SECRETS if not values.get(key)]
    if missing:
        errors.append(
            "Missing required Home Assistant secrets:\n"
            + "\n".join(f" - {key}" for key in missing)
        )
    for key, legacy in LEGACY_ENDPOINTS.items():
        if legacy in values.get(key, ""):
            suffix = "approve-active" if key.endswith("approve_url") else "reject-active"
            errors.append(
                f"Legacy endpoint rejected for {key}; use "
                f"/api/v1/resources/{{{{ vmid }}}}/plans/{suffix}"
            )
    return errors


def main() -> int:
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} SECRETS_FILE", file=sys.stderr)
        return 2
    path = Path(sys.argv[1])
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"Cannot read Home Assistant secrets file: {exc}", file=sys.stderr)
        return 1
    errors = validate(text)
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
