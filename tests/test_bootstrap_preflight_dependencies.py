"""Direct, hermetic regression for deploy/lib/bootstrap-preflight.sh's
host-side dependency contract (dependency-contract correction pass).

python3 is a hard preflight requirement, not one of two interchangeable
JSON parsers ("jq" OR "python3"). A host with jq present but python3
absent used to pass phase1_preflight and would only fail later, deep
inside package-scan/package-update boundary provisioning -- bootstrap-
host-control.sh's own authorized_keys path classifier
(_host_control_authorized_keys_path_state) unconditionally shells out to
`python3 -c` -- after mutation had already begun.

This sources only bootstrap-common.sh and bootstrap-preflight.sh (never
the full deploy/bootstrap-proxmox-0.5.sh entrypoint) against a minimal,
fully-controlled PATH, so it needs no Docker sandbox: phase1_preflight's
own require_command calls are read-only presence checks, and the
python3 check is reached (or not) well before anything AGENTS.md
reserves for the hardened smoke sandbox.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
COMMON_SH = REPO_ROOT / "deploy" / "lib" / "bootstrap-common.sh"
PREFLIGHT_SH = REPO_ROOT / "deploy" / "lib" / "bootstrap-preflight.sh"

pytestmark = pytest.mark.skipif(
    __import__("shutil").which("bash") is None, reason="bash is not available"
)

# Every command phase1_preflight's own require_command calls check for
# BEFORE reaching the python3 requirement -- each is a read-only
# `command -v` presence probe, never actually invoked, so a trivial
# always-present stub is a faithful stand-in.
_PRESENCE_ONLY_COMMANDS = ("pct", "pveum", "pveam", "pvesh", "pvesm", "dpkg", "git")


def _fakebin(tmp_path: Path, *, include_python3: bool, include_jq: bool) -> Path:
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir(parents=True)
    for name in _PRESENCE_ONLY_COMMANDS:
        shim = fakebin / name
        shim.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        shim.chmod(0o755)
    if include_jq:
        shim = fakebin / "jq"
        shim.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        shim.chmod(0o755)
    if include_python3:
        shim = fakebin / "python3"
        shim.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        shim.chmod(0o755)
    return fakebin


def _run_preflight(tmp_path: Path, *, include_python3: bool, include_jq: bool):
    fakebin = _fakebin(tmp_path, include_python3=include_python3, include_jq=include_jq)
    script = f"""
set -Eeuo pipefail
source '{COMMON_SH}'
source '{PREFLIGHT_SH}'
TLS_TRUST_MODE=""
PVE_CA_PATH=""
VMID=""
STORAGE=""
phase1_preflight
echo UNEXPECTED_SUCCESS
"""
    env = {"PATH": str(fakebin), "HUBINET_OPS_TEST_MODE": "1"}
    bash_path = __import__("shutil").which("bash") or "/bin/bash"
    return subprocess.run(
        [bash_path, "-c", script],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_jq_present_python3_absent_fails_preflight_before_mutation(tmp_path):
    """The exact regression: jq present, python3 absent. Must fail
    phase1_preflight closed -- never pass and defer the failure to a
    mutation-adjacent phase.
    """

    result = _run_preflight(tmp_path, include_python3=False, include_jq=True)

    assert result.returncode != 0
    assert "UNEXPECTED_SUCCESS" not in result.stdout
    assert "python3" in result.stderr
    assert "required command 'python3' not found" in result.stderr


def test_python3_absent_and_jq_absent_also_fails_preflight(tmp_path):
    result = _run_preflight(tmp_path, include_python3=False, include_jq=False)

    assert result.returncode != 0
    assert "UNEXPECTED_SUCCESS" not in result.stdout
    assert "python3" in result.stderr


def test_python3_present_passes_the_dependency_gate_regardless_of_jq(tmp_path):
    """Positive control: python3 alone (jq absent) is sufficient -- jq
    remains an optional, preferred-when-present JSON parser, never a
    required one.
    """

    for index, include_jq in enumerate((False, True)):
        result = _run_preflight(
            tmp_path / f"case-{index}", include_python3=True, include_jq=include_jq
        )
        # phase1_preflight continues past the dependency gate into VMID
        # auto-detection, which this minimal fixture does not support
        # (no real `pvesh`) -- so it still fails, but never on python3,
        # and it must have gotten past the require_command line to do so.
        assert "required command 'python3' not found" not in result.stderr
