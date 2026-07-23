from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]


def test_current_release_installer_runtime_smoke() -> None:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is unavailable on this platform")
    smoke = ROOT / "tests" / "shell" / "runtime_smoke_0_4_0.sh"
    result = subprocess.run(
        [bash, str(smoke)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=600,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (
        "0.4.0 runtime smoke: success and cross-layer rollback passed"
        in result.stdout.splitlines()
    )
