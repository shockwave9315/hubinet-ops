from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Iterable


SEMVER_RE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


class ReleaseDecisionError(ValueError):
    pass


def _version(value: str) -> tuple[int, int, int]:
    match = SEMVER_RE.fullmatch(str(value))
    if match is None:
        raise ReleaseDecisionError("Version must be a stable semantic version")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def decide_release(current_version: str, tags: Iterable[str]) -> dict[str, str]:
    current = _version(current_version)
    stable: dict[tuple[int, int, int], str] = {}
    for raw in tags:
        tag = str(raw).strip()
        if not tag.startswith("v") or SEMVER_RE.fullmatch(tag[1:]) is None:
            continue
        stable[_version(tag[1:])] = tag
    current_tag = f"v{current_version}"
    latest = max(stable, default=None)
    previous = ".".join(str(part) for part in latest) if latest else "none"
    if current in stable:
        status = "no_version_bump"
    elif latest is not None and current < latest:
        status = "downgrade_blocked"
    elif latest is not None and current == latest:
        status = "no_version_bump"
    else:
        status = "release"
    return {
        "status": status,
        "version": current_version,
        "tag": current_tag,
        "previous_version": previous,
    }


def read_application_version(source_root: Path) -> str:
    mqtt = source_root / "app" / "mqtt.py"
    try:
        text = mqtt.read_text(encoding="utf-8")
    except OSError as exc:
        raise ReleaseDecisionError("Cannot read app/mqtt.py") from exc
    matches = re.findall(r'^VERSION = "([^"]+)"$', text, flags=re.MULTILINE)
    if len(matches) != 1:
        raise ReleaseDecisionError("app/mqtt.py must define exactly one VERSION")
    _version(matches[0])
    return matches[0]


def _git_tags(source_root: Path) -> list[str]:
    completed = subprocess.run(
        ["git", "tag", "--list", "v*"],
        cwd=source_root,
        text=True,
        capture_output=True,
        check=False,
        shell=False,
    )
    if completed.returncode != 0:
        raise ReleaseDecisionError("Cannot list repository tags")
    return completed.stdout.splitlines()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, default=Path.cwd())
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    version = read_application_version(args.source_root)
    decision = decide_release(version, _git_tags(args.source_root))
    print(json.dumps(decision, sort_keys=True))
    output = args.github_output
    if output is None and os.environ.get("GITHUB_OUTPUT"):
        output = Path(os.environ["GITHUB_OUTPUT"])
    if output is not None:
        with output.open("a", encoding="utf-8", newline="\n") as handle:
            for key in ("status", "version", "tag", "previous_version"):
                handle.write(f"{key}={decision[key]}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
