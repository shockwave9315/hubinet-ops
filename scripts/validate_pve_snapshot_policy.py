#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import yaml

ACTION_POLICIES = {
    "snapshot_create": "snapshot-create-vmids",
    "snapshot_rollback": "snapshot-restore-vmids",
    "snapshot_delete": "snapshot-delete-vmids",
}
EXPECTED_VMIDS = set(range(100, 111))


def _vmids(path: Path) -> set[int]:
    values: list[int] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        value = raw.strip()
        if not value:
            continue
        if not value.isdigit() or int(value) <= 0:
            raise ValueError(f"{path.name} contains an invalid VMID")
        values.append(int(value))
    if len(values) != len(set(values)):
        raise ValueError(f"{path.name} contains duplicate VMIDs")
    return set(values)


def _resources(config_path: Path) -> dict[int, dict[str, Any]]:
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Agent configuration must be an object")
    configured = raw.get("resources") or raw.get("containers")
    if not isinstance(configured, dict):
        raise ValueError("Agent configuration has no resources inventory")
    resources = {int(vmid): cfg for vmid, cfg in configured.items()}
    if set(resources) != EXPECTED_VMIDS:
        raise ValueError("Snapshot policy requires the exact VMID 100-110 inventory")
    if not all(isinstance(cfg, dict) for cfg in resources.values()):
        raise ValueError("Every resource configuration must be an object")
    return resources


def validate(config_path: Path, policy_dir: Path) -> None:
    resources = _resources(config_path)
    observation = _vmids(policy_dir / "observation-vmids")
    host_control = _vmids(policy_dir / "host-control-vmids")
    resource_types = {}
    for raw in (policy_dir / "resource-types").read_text(encoding="utf-8").splitlines():
        parts = raw.split()
        if len(parts) != 2 or not parts[0].isdigit():
            raise ValueError("resource-types contains an invalid entry")
        resource_types[int(parts[0])] = parts[1]
    policies = {
        capability: _vmids(policy_dir / filename)
        for capability, filename in ACTION_POLICIES.items()
    }
    for capability, allowed in policies.items():
        if not allowed <= observation or not allowed <= host_control:
            raise ValueError(f"{capability} policy exceeds PVE host-control boundaries")
        if any(resource_types.get(vmid) != "lxc" for vmid in allowed):
            raise ValueError(f"{capability} policy contains a non-LXC VMID")

    configured: dict[str, set[int]] = {
        capability: set() for capability in ACTION_POLICIES
    }
    for vmid, cfg in resources.items():
        capabilities = cfg.get("operator_capabilities") or {}
        if not isinstance(capabilities, dict):
            raise ValueError(f"Resource {vmid} operator_capabilities must be an object")
        restore_allowed = cfg.get("manual_snapshot_restore_allowed", False)
        if not isinstance(restore_allowed, bool):
            raise ValueError(
                f"Resource {vmid} manual_snapshot_restore_allowed must be a boolean"
            )
        if restore_allowed and not bool(capabilities.get("snapshot_rollback", False)):
            raise ValueError(
                f"Resource {vmid} snapshot restore policy requires snapshot_rollback capability"
            )
        for capability in ACTION_POLICIES:
            enabled = bool(capabilities.get(capability, False))
            if capability == "snapshot_rollback":
                enabled = enabled and restore_allowed
            if enabled:
                configured[capability].add(vmid)

    for capability, allowed in policies.items():
        if configured[capability] != allowed:
            raise ValueError(
                f"{capability} backend and PVE policies differ: "
                f"backend={sorted(configured[capability])}, pve={sorted(allowed)}"
            )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("config", type=Path)
    parser.add_argument(
        "--policy-dir",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "deploy" / "pve",
    )
    args = parser.parse_args()
    validate(args.config, args.policy_dir)
    print("PVE snapshot policy validation: CT101-CT110 consistent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
