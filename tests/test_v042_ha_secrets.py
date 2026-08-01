from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.validate_ha_secrets_0_4_2 import REQUIRED_SECRETS, validate


ROOT = Path(__file__).parents[1]
EXAMPLE = ROOT / "home-assistant" / "secrets.example.yaml"
PACKAGE = ROOT / "home-assistant" / "packages/hubinet_ops.yaml"
VALIDATOR = ROOT / "scripts/validate_ha_secrets_0_4_2.py"
SNAPSHOT_PRUNE_SECRETS = (
    "hubinet_ops_snapshot_delete_oldest_url",
    "hubinet_ops_snapshot_delete_unprotected_url",
)


def _example_text() -> str:
    return EXAMPLE.read_text(encoding="utf-8")


def _without_secret(text: str, secret: str) -> str:
    return "\n".join(
        line for line in text.splitlines() if not line.startswith(f"{secret}:")
    )


def test_complete_042_secrets_example_passes_validation() -> None:
    assert validate(_example_text()) == []


def test_package_secret_references_are_required_by_042_validator() -> None:
    package_secret_references = set(
        re.findall(r"!secret\s+([A-Za-z0-9_]+)", PACKAGE.read_text(encoding="utf-8"))
    )

    assert set(SNAPSHOT_PRUNE_SECRETS) <= package_secret_references
    assert set(SNAPSHOT_PRUNE_SECRETS) <= set(REQUIRED_SECRETS)
    assert package_secret_references <= set(REQUIRED_SECRETS)


@pytest.mark.parametrize("secret", SNAPSHOT_PRUNE_SECRETS)
def test_missing_snapshot_prune_secret_reports_exact_name(secret: str) -> None:
    errors = validate(_without_secret(_example_text(), secret))

    assert errors
    assert f" - {secret}" in "\n".join(errors)


@pytest.mark.parametrize("secret", SNAPSHOT_PRUNE_SECRETS)
def test_empty_snapshot_prune_secret_is_missing(secret: str) -> None:
    text = re.sub(
        rf"^{re.escape(secret)}:.*$",
        f'{secret}: ""',
        _example_text(),
        flags=re.MULTILINE,
    )

    assert f" - {secret}" in "\n".join(validate(text))


@pytest.mark.parametrize("secret", REQUIRED_SECRETS)
def test_every_042_required_secret_remains_enforced(secret: str) -> None:
    assert f" - {secret}" in "\n".join(
        validate(_without_secret(_example_text(), secret))
    )


def test_legacy_approve_and_reject_endpoints_remain_rejected() -> None:
    text = _example_text().replace(
        "/api/v1/resources/{{ vmid }}/plans/approve-active",
        "/api/v1/plans/approve",
    ).replace(
        "/api/v1/resources/{{ vmid }}/plans/reject-active",
        "/api/v1/plans/reject",
    )

    errors = "\n".join(validate(text))
    assert "Legacy endpoint rejected for hubinet_ops_approve_url" in errors
    assert "Legacy endpoint rejected for hubinet_ops_reject_url" in errors


def test_validator_reads_secrets_from_stdin_not_argv_or_environment() -> None:
    secret_marker = "stdin-only-secret-marker-042"
    input_text = _example_text() + f"\nunused_test_secret: {secret_marker}\n"
    command = [sys.executable, str(VALIDATOR), "-"]
    environment = {
        "PATH": os.environ["PATH"],
        "PYTHONIOENCODING": "utf-8",
    }

    assert secret_marker not in "\0".join(command)
    assert secret_marker not in "\0".join(environment.values())
    completed = subprocess.run(
        command,
        cwd=ROOT,
        env=environment,
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0
    assert completed.stdout == ""
    assert completed.stderr == ""
    assert secret_marker not in completed.stdout + completed.stderr
