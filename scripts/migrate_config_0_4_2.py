#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

import yaml

from migrate_config_0_4_0 import main


if __name__ == "__main__":
    source = yaml.safe_load(Path(sys.argv[1]).read_text(encoding="utf-8"))
    source_resources = source.get("resources") or source.get("containers") or {}
    retention_by_vmid = {}
    preserved_policy_by_vmid = {}
    for raw_vmid, resource in source_resources.items():
        if isinstance(resource, dict):
            preserved_policy_by_vmid[int(raw_vmid)] = {
                key: resource[key]
                for key in (
                    "operator_capabilities",
                    "manual_rollback_allowed",
                    "manual_snapshot_restore_allowed",
                    "pre_update_snapshot",
                )
                if key in resource
            }
            configured = resource.get(
                "snapshot_retention_count",
                resource.get("snapshot_retention"),
            )
            if configured is not None:
                retention_by_vmid[int(raw_vmid)] = configured
    result = main("0.4.2")
    output = Path(sys.argv[2])
    raw = yaml.safe_load(output.read_text(encoding="utf-8"))
    resources = raw["resources"]
    for vmid, resource in resources.items():
        preserved = preserved_policy_by_vmid.get(int(vmid), {})
        configured_capabilities = preserved.get("operator_capabilities")
        if isinstance(configured_capabilities, dict):
            generated_capabilities = resource["operator_capabilities"]
            resource["operator_capabilities"] = {
                name: bool(configured_capabilities.get(name, False))
                for name in generated_capabilities
            }
        for key in (
            "manual_rollback_allowed",
            "manual_snapshot_restore_allowed",
            "pre_update_snapshot",
        ):
            if key in preserved:
                resource[key] = preserved[key]
    vm100 = resources[100]
    vm100["operator_capabilities"]["snapshot_create"] = True
    vm100["operator_capabilities"]["snapshot_list"] = True
    vm100["operator_capabilities"]["snapshot_delete"] = True
    for vmid, resource in resources.items():
        resource.pop("snapshot_retention", None)
        snapshots_enabled = any(
            resource["operator_capabilities"].get(name, False)
            for name in ("snapshot_create", "snapshot_list", "snapshot_delete")
        )
        resource["snapshot_retention_count"] = retention_by_vmid.get(
            int(vmid),
            3 if snapshots_enabled else 0,
        )
    mqtt = raw.setdefault("mqtt", {})
    mqtt.setdefault("cpu_publish_deadband_percent", 0.5)
    mqtt.setdefault("telemetry_heartbeat_seconds", 300)
    output.write_text(
        yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    raise SystemExit(result)
