from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from scripts.validate_yaml import yaml_paths


ROOT = Path(__file__).parents[1]


def test_validator_runs_in_archive_without_git_metadata(tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    (archive / "scripts").mkdir(parents=True)
    (archive / "home-assistant").mkdir()
    shutil.copy2(ROOT / "scripts" / "validate_yaml.py", archive / "scripts" / "validate_yaml.py")
    (archive / "config.yaml").write_text("root: true\n", encoding="utf-8")
    (archive / "home-assistant" / "package.yaml").write_text(
        "value: !secret private_value\n",
        encoding="utf-8",
    )
    (archive / ".git").mkdir()
    (archive / ".git" / "ignored.yaml").write_text("invalid: [\n", encoding="utf-8")
    shutil.rmtree(archive / ".git")

    result = subprocess.run(
        [sys.executable, str(archive / "scripts" / "validate_yaml.py")],
        cwd=archive,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "2 files parsed" in result.stdout


def test_yaml_discovery_is_deterministic_and_ignores_generated_directories(
    tmp_path: Path,
) -> None:
    (tmp_path / "z.yaml").write_text("z: 1\n", encoding="utf-8")
    (tmp_path / "a.yml").write_text("a: 1\n", encoding="utf-8")
    for directory in (
        ".git",
        ".venv",
        "venv",
        ".pytest_tmp",
        ".tmp",
        "build",
        "runtime",
        "backups",
        "__pycache__",
    ):
        target = tmp_path / directory
        target.mkdir()
        (target / "ignored.yaml").write_text("invalid: [\n", encoding="utf-8")
    assert [path.name for path in yaml_paths(tmp_path)] == ["a.yml", "z.yaml"]


def test_validator_returns_error_for_invalid_repository_yaml(tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    (archive / "scripts").mkdir(parents=True)
    shutil.copy2(ROOT / "scripts" / "validate_yaml.py", archive / "scripts" / "validate_yaml.py")
    (archive / "broken.yaml").write_text("broken: [\n", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(archive / "scripts" / "validate_yaml.py")],
        cwd=archive,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
