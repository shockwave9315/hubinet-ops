"""Direct, hermetic unit coverage for the two host-side path classifiers
added by the Family A correction pass:

- deploy/lib/update-boundaries.sh::_update_boundary_helper_path_state
- deploy/lib/bootstrap-host-control.sh::_host_control_dir_state

Both are pure filesystem operations (a single local `python3 -c` os.lstat
call, no `pct`/`ssh`/`systemctl`/network call anywhere in them), so
exercising them directly -- sourcing the small library files into a
throwaway bash subprocess against a tmp_path-rooted fake root -- is both
faithful and fast, the same reasoning tests/test_authorized_keys_atomic.py
already establishes for the sibling authorized_keys classifier. No Docker
sandbox is required; the full end-to-end behavior (no rollback marker and
no destructive mutation for an UNKNOWN classification) is covered
separately in tests/test_update_proxmox_0_5_smoke.py, which exercises
these same functions through their real call sites.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
COMMON_SH = REPO_ROOT / "deploy" / "lib" / "bootstrap-common.sh"
HOST_CONTROL_SH = REPO_ROOT / "deploy" / "lib" / "bootstrap-host-control.sh"
UPDATE_BOUNDARIES_SH = REPO_ROOT / "deploy" / "lib" / "update-boundaries.sh"

pytestmark = pytest.mark.skipif(
    __import__("shutil").which("bash") is None, reason="bash is not available"
)


def _run(tmp_path: Path, script: str):
    wrapper = f"""
set -Eeuo pipefail
source '{COMMON_SH}'
source '{HOST_CONTROL_SH}'
source '{UPDATE_BOUNDARIES_SH}'
{script}
"""
    env = dict(os.environ, HUBINET_OPS_TEST_MODE="1")
    return subprocess.run(
        ["bash", "-c", wrapper],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _blocked_dir(tmp_path: Path, child_name: str):
    """A directory with mode 0o000, denying path traversal to lstat/stat
    (EACCES, never ENOENT) even for the owning, non-root process running
    this test. <child_name> is never actually created -- the block happens
    one level up, at the parent directory itself."""

    blocked = tmp_path / "blocked"
    blocked.mkdir()
    child = blocked / child_name
    blocked.chmod(0o000)
    return blocked, child


class TestUpdateBoundaryHelperPathState:
    """deploy/lib/update-boundaries.sh::_update_boundary_helper_path_state"""

    def test_genuinely_absent_is_absent(self, tmp_path: Path) -> None:
        missing = tmp_path / "does-not-exist"
        result = _run(tmp_path, f'_update_boundary_helper_path_state "{missing}"')
        assert result.returncode == 0, result.stderr
        assert result.stdout == "ABSENT"

    def test_existing_regular_file_is_regular(self, tmp_path: Path) -> None:
        helper = tmp_path / "hubinet-package-snapshot-boundary-x"
        helper.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        result = _run(tmp_path, f'_update_boundary_helper_path_state "{helper}"')
        assert result.returncode == 0, result.stderr
        assert result.stdout == "REGULAR"

    def test_a_directory_at_the_path_is_unknown_not_absent(self, tmp_path: Path) -> None:
        """A non-regular existing object must never be silently read as
        absent -- the caller (update_boundaries_classify) must fail
        closed instead of planning a fresh provision over it."""

        not_a_file = tmp_path / "hubinet-package-snapshot-boundary-x"
        not_a_file.mkdir()
        result = _run(tmp_path, f'_update_boundary_helper_path_state "{not_a_file}"')
        assert result.returncode == 0, result.stderr
        assert result.stdout == "UNKNOWN"

    def test_a_genuine_stat_error_is_unknown(self, tmp_path: Path) -> None:
        blocked, child = _blocked_dir(tmp_path, "hubinet-package-snapshot-boundary-x")
        try:
            result = _run(tmp_path, f'_update_boundary_helper_path_state "{child}"')
        finally:
            blocked.chmod(0o755)
        assert result.returncode == 0, result.stderr
        assert result.stdout == "UNKNOWN"


class TestHostControlDirState:
    """deploy/lib/bootstrap-host-control.sh::_host_control_dir_state"""

    def test_genuinely_absent_is_absent(self, tmp_path: Path) -> None:
        missing = tmp_path / "snapshot-operations"
        result = _run(tmp_path, f'_host_control_dir_state "{missing}"')
        assert result.returncode == 0, result.stderr
        assert result.stdout == "ABSENT"

    def test_existing_directory_is_directory(self, tmp_path: Path) -> None:
        journal = tmp_path / "snapshot-operations"
        journal.mkdir()
        result = _run(tmp_path, f'_host_control_dir_state "{journal}"')
        assert result.returncode == 0, result.stderr
        assert result.stdout == "DIRECTORY"

    def test_a_regular_file_at_the_path_is_unknown_not_absent(self, tmp_path: Path) -> None:
        not_a_dir = tmp_path / "snapshot-operations"
        not_a_dir.write_text("not a directory\n", encoding="utf-8")
        result = _run(tmp_path, f'_host_control_dir_state "{not_a_dir}"')
        assert result.returncode == 0, result.stderr
        assert result.stdout == "UNKNOWN"

    def test_a_genuine_stat_error_is_unknown(self, tmp_path: Path) -> None:
        blocked, child = _blocked_dir(tmp_path, "snapshot-operations")
        try:
            result = _run(tmp_path, f'_host_control_dir_state "{child}"')
        finally:
            blocked.chmod(0o755)
        assert result.returncode == 0, result.stderr
        assert result.stdout == "UNKNOWN"
