from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys
from typing import NamedTuple


class Violation(NamedTuple):
    line: int
    command_path: str


FORBIDDEN_COMMAND = (
    r"apt(?:-get)?|ssh|scp|sftp|rsync|pct|pvesh|qm|lxc-[A-Za-z0-9_.+-]+|"
    r"systemctl|docker|podman|curl|wget|nc|socat|sudo"
)
COMMAND_END = r"(?=$|[\s;&|()<>'\"])"
FORBIDDEN_ABSOLUTE_COMMAND = re.compile(
    rf"(?P<path>/(?:usr/)?s?bin/(?:{FORBIDDEN_COMMAND}))"
    rf"{COMMAND_END}"
)


def find_violations(text: str) -> list[Violation]:
    return [
        Violation(
            line=text.count("\n", 0, match.start("path")) + 1,
            command_path=match.group("path"),
        )
        for match in FORBIDDEN_ABSOLUTE_COMMAND.finditer(text)
    ]


def validate_file(path: Path) -> list[Violation]:
    return find_violations(path.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Reject real network, deployment, container, and hypervisor commands "
            "referenced through absolute standard executable paths."
        )
    )
    parser.add_argument("script", type=Path)
    args = parser.parse_args(argv)

    try:
        violations = validate_file(args.script)
    except (OSError, UnicodeError) as exc:
        print(f"{args.script}: unable to validate shell boundary: {exc}", file=sys.stderr)
        return 2

    for violation in violations:
        print(
            f"{args.script}:{violation.line}: forbidden absolute command path: "
            f"{violation.command_path}",
            file=sys.stderr,
        )
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
