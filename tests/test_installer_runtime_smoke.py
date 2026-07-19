from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
SMOKES = (
    ROOT / "tests" / "shell" / "runtime_smoke_0_3_0.sh",
    ROOT / "tests" / "shell" / "runtime_smoke_installers_0_3_0.sh",
    ROOT / "tests" / "shell" / "runtime_smoke_managed_safety_0_3_0.sh",
    ROOT / "tests" / "shell" / "runtime_smoke_agent_backup_0_3_0.sh",
    ROOT / "tests" / "shell" / "runtime_smoke_0_3_1.sh",
)


@pytest.mark.parametrize(
    "smoke",
    SMOKES,
    ids=("wrapper", "installers", "managed-safety", "agent-backup", "patch-031"),
)
def test_v030_scripts_execute_with_fake_remote_commands(smoke: Path) -> None:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is unavailable on this platform")
    result = subprocess.run(
        [bash, str(smoke)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=240,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    expected_version = "0.3.1" if "0_3_1" in smoke.name else "0.3.0"
    assert expected_version in result.stdout
    assert "smoke passed" in result.stdout
