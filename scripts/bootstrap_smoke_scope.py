#!/usr/bin/env python3
"""Decide whether a pull-request head update touches bootstrap-smoke dependencies.

The GitHub Actions pull_request.paths filter is intentionally coarse: GitHub
re-evaluates it against the complete PR diff on every synchronize event. This
helper is the second-stage, synchronize-only filter used after the workflow has
already started. It receives only the exact before..after changed filenames for
that head update and prints exactly ``true`` or ``false``.

The dependency patterns below must stay byte-for-byte aligned with
.github/workflows/bootstrap-smoke.yml's pull_request.paths list. The dedicated
stdlib-only unit test enforces that contract.
"""

from __future__ import annotations

import argparse
import fnmatch
import sys
from collections.abc import Iterable


SMOKE_PATHS: tuple[str, ...] = (
    "deploy/bootstrap-proxmox-0.5.sh",
    "deploy/lib/bootstrap-*.sh",
    "deploy/lib/hubinet-ops-bootstrap-*.py",
    "deploy/install-0.5.0-fresh.sh",
    "deploy/hubinet-ops-0.5.service",
    "requirements.txt",
    "tests/_bootstrap_fake_pve.py",
    "tests/test_bootstrap_proxmox_0_5_smoke.py",
    "tests/shell/**",
    "scripts/validate_hermetic_shell_boundary.py",
    "scripts/bootstrap_smoke_scope.py",
    "AGENTS.md",
    ".github/workflows/bootstrap-smoke.yml",
)


def path_requires_smoke(path: str) -> bool:
    """Return True when *path* matches any bootstrap-smoke dependency pattern."""

    return any(fnmatch.fnmatchcase(path, pattern) for pattern in SMOKE_PATHS)


def update_requires_smoke(paths: Iterable[str]) -> bool:
    """Return True when any changed path requires the expensive sandbox smoke."""

    return any(path_requires_smoke(path) for path in paths)


def _read_paths(*, null_delimited: bool) -> list[str]:
    raw = sys.stdin.buffer.read()
    chunks = raw.split(b"\0") if null_delimited else raw.splitlines()
    return [chunk.decode("utf-8", errors="surrogateescape") for chunk in chunks if chunk]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--null",
        action="store_true",
        help="read NUL-delimited filenames (the format from git diff --name-only -z)",
    )
    args = parser.parse_args()

    paths = _read_paths(null_delimited=args.null)
    print("true" if update_requires_smoke(paths) else "false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
