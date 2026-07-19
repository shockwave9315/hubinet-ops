from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
SMOKE = ROOT / "tests" / "shell" / "runtime_smoke_0_2_4.sh"


def test_v024_installers_execute_with_fake_remote_commands() -> None:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is unavailable on this platform")
    result = subprocess.run(
        [bash, str(SMOKE)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "0.2.4 installer runtime smoke passed" in result.stdout
