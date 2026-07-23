#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
EXECUTOR = ROOT / "deploy" / "managed" / "hubinet-maint"
PROFILES = ROOT / "deploy" / "managed" / "profiles"

CAPABILITY_KEYS = (
    "refresh", "scan", "approve", "reject", "retry_healthcheck", "rollback",
    "start", "shutdown", "reboot", "force_stop", "snapshot_create",
    "snapshot_list", "snapshot_rollback", "snapshot_delete", "self_update",
)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def capabilities(enabled: set[str]) -> dict[str, bool]:
    return {name: name in enabled for name in CAPABILITY_KEYS}


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate an existing Hubinet Ops config to 0.4.0")
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--host-control-url", required=True)
    args = parser.parse_args()
    raw = yaml.safe_load(args.input.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise SystemExit("Agent configuration must be a YAML object")
    resources: dict[Any, Any] = raw.get("resources") or raw.get("containers")
    if not isinstance(resources, dict):
        raise SystemExit("Agent configuration has no resources inventory")
    normalized = {int(vmid): cfg for vmid, cfg in resources.items()}
    if set(normalized) != set(range(100, 111)):
        raise SystemExit("0.4.0 migration requires the unchanged inventory VMID 100-110")

    full = set(CAPABILITY_KEYS) - {"self_update"}
    for vmid, cfg in normalized.items():
        if not isinstance(cfg, dict):
            raise SystemExit(f"VMID {vmid} configuration must be an object")
        if vmid == 100:
            cfg["operator_capabilities"] = capabilities(set())
        elif vmid <= 109:
            cfg["operator_capabilities"] = capabilities(full)
            cfg["executor_contract"] = {
                "executor_sha256": digest(EXECUTOR),
                "profile_sha256": digest(PROFILES / f"ct{vmid}.json"),
            }
            cfg.setdefault("snapshot_retention", 5)
        else:
            cfg["operator_capabilities"] = capabilities(
                {"refresh", "approve", "reject", "start", "shutdown", "reboot", "force_stop",
                 "snapshot_create", "snapshot_list", "snapshot_rollback",
                 "snapshot_delete", "self_update"}
            )
            cfg.setdefault("snapshot_retention", 5)
    raw.pop("containers", None)
    raw["resources"] = normalized
    raw["host_control"] = {
        "enabled": True,
        "base_url": args.host_control_url.rstrip("/"),
        "token_env": "HUBINET_OPS_HOSTD_TOKEN",
        "update_token_env": "HUBINET_OPS_HOSTD_UPDATE_TOKEN",
        "timeout_seconds": 30,
        "operation_timeout_seconds": 1800,
        "poll_interval_seconds": 1,
    }
    args.output.write_text(
        yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
