from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
SANDBOX_RUNNER = ROOT / "tests" / "shell" / "run_runtime_smoke_sandbox.sh"


def test_current_release_installer_runtime_smoke() -> None:
    if sys.platform == "win32":
        pytest.skip("system deployment-smoke sandbox is unavailable on Windows")
    if not sys.platform.startswith("linux"):
        pytest.skip("system deployment-smoke sandbox is supported only on Linux CI")
    if os.environ.get("GITHUB_ACTIONS") != "true":
        pytest.skip(
            "system deployment-smoke sandbox is restricted to controlled ephemeral CI"
        )
    assert os.environ.get("HUBINET_OPS_EPHEMERAL_CI") == "1", (
        "Linux CI must explicitly enable the deployment-smoke sandbox"
    )
    assert os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted", (
        "deployment smoke requires an ephemeral GitHub-hosted runner"
    )
    assert os.environ.get("GITHUB_RUN_ID", "").isdigit(), (
        "deployment smoke requires a GitHub Actions run ID"
    )
    bash = shutil.which("bash")
    assert bash is not None, "Linux runtime smoke requires bash"
    assert shutil.which("docker") is not None, (
        "Linux runtime smoke requires the fail-closed Docker sandbox"
    )
    result = subprocess.run(
        [bash, str(SANDBOX_RUNNER)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=900,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "sandbox self-test: passed" in result.stdout.splitlines()
    assert (
        "0.4.1 runtime smoke: success, retry and cross-layer rollback passed"
        in result.stdout.splitlines()
    )
    assert (
        "0.4.1 HA installer runtime smoke: passed"
        in result.stdout.splitlines()
    )
