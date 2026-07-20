#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import importlib.machinery
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EXECUTOR = ROOT / "deploy" / "managed" / "hubinet-maint"
DEFAULT_PROFILES = ROOT / "deploy" / "managed" / "profiles"


def load_executor(path: Path) -> ModuleType:
    try:
        import fcntl  # noqa: F401
    except ModuleNotFoundError:
        sys.modules["fcntl"] = ModuleType("fcntl")
    loader = importlib.machinery.SourceFileLoader("hubinet_maint_contract", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    if spec is None:
        raise RuntimeError("Cannot load executor contract")
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate managed LXC profiles")
    parser.add_argument("--executor", type=Path, default=DEFAULT_EXECUTOR)
    parser.add_argument("--profiles", type=Path, default=DEFAULT_PROFILES)
    args = parser.parse_args()
    executor = load_executor(args.executor)
    expected = {f"ct{vmid}.json" for vmid in range(101, 110)}
    actual = {path.name for path in args.profiles.glob("ct*.json")}
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise SystemExit(f"Profile inventory mismatch; missing={missing}, extra={extra}")
    for name in sorted(expected):
        path = args.profiles / name
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise SystemExit(f"{name}: profile must be an object")
        status, errors = executor.validate_profile(raw)
        if status == "invalid":
            raise SystemExit(f"{name}: {'; '.join(errors)}")
        print(f"{name}: {status} sha256={sha256(path)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
