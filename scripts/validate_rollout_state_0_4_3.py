#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
import math
import os
import sys
from typing import Any


EXPECTED_VMIDS = {str(vmid) for vmid in range(100, 111)}


def _stamp(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp has no timezone")
    return parsed.astimezone(UTC)


def validate(payload: Any, not_before: datetime) -> list[str]:
    if not isinstance(payload, dict):
        return ["state_parse_error=top-level payload is not an object"]
    resources = payload.get("resources")
    if not isinstance(resources, dict):
        return ["state_parse_error=resources is not an object"]
    errors: list[str] = []
    if payload.get("version") != "0.4.3":
        errors.append(f"invalid_version={payload.get('version')!r}")
    actual = {str(key) for key in resources}
    if actual != EXPECTED_VMIDS:
        missing = sorted(EXPECTED_VMIDS - actual, key=int)
        extra = sorted(actual - EXPECTED_VMIDS)
        errors.append(f"resource_keys=missing:{missing},extra:{extra}")

    stale: list[str] = []
    for vmid in sorted(EXPECTED_VMIDS & actual, key=int):
        value = resources[vmid].get("last_refresh")
        try:
            if _stamp(value) < not_before:
                stale.append(f"{vmid}({value})")
        except (TypeError, ValueError):
            stale.append(f"{vmid}({value})")
    if stale:
        errors.append(f"stale_last_refresh={','.join(stale)}")

    bad_executors: list[str] = []
    for vmid in map(str, range(101, 110)):
        state = resources.get(vmid)
        if not isinstance(state, dict):
            continue
        compatible = state.get("executor_compatible")
        version = state.get("executor_version")
        protocol = state.get("executor_protocol_version")
        if compatible is not True or version != "0.4.3" or protocol != 1:
            bad_executors.append(
                f"{vmid}(compatible={compatible!r},version={version!r},"
                f"protocol={protocol!r})"
            )
    if bad_executors:
        errors.append(f"bad_executors={','.join(bad_executors)}")

    vm100 = resources.get("100")
    if isinstance(vm100, dict):
        cpu = vm100.get("cpu")
        cpu_is_object = cpu is None or isinstance(cpu, dict)
        usage = cpu.get("usage_percent") if isinstance(cpu, dict) else None
        valid_usage = (
            cpu_is_object
            and not isinstance(usage, bool)
            and isinstance(usage, (int, float))
            and math.isfinite(usage)
            and 0 <= usage <= 100
        )
        qemu_status = vm100.get("qemu_status")
        health_status = vm100.get("health_status")
        valid = False
        if qemu_status == "running":
            valid = health_status == "healthy" and valid_usage
        elif qemu_status == "stopped":
            valid = health_status == "offline" and (
                (cpu_is_object and usage is None) or valid_usage
            )
        if not valid:
            errors.append(
                "bad_vm100="
                f"qemu_status:{qemu_status!r},health:{health_status!r},"
                f"cpu_usage:{usage!r}"
            )
    if isinstance(resources.get("106"), dict):
        health = resources["106"].get("health_status")
        if health != "healthy":
            errors.append(f"bad_ct106_health={health!r}")
    if isinstance(resources.get("110"), dict):
        health = resources["110"].get("health_status")
        score = resources["110"].get("health_score")
        if health != "healthy" or score != 100:
            errors.append(f"bad_ct110_health=health:{health!r},score:{score!r}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate fresh Hubinet Ops 0.4.3 rollout telemetry"
    )
    parser.add_argument("not_before")
    parser.add_argument("--input-fd", type=int, default=0)
    args = parser.parse_args()
    try:
        not_before = _stamp(args.not_before)
        with os.fdopen(args.input_fd, encoding="utf-8", closefd=args.input_fd != 0) as stream:
            payload = json.load(stream)
    except Exception as exc:
        print(
            f"Fresh 0.4.3 telemetry validation failed:\n"
            f"state_parse_error={type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1
    errors = validate(payload, not_before)
    if errors:
        print(
            "Fresh 0.4.3 telemetry validation failed:\n" + "\n".join(errors),
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
