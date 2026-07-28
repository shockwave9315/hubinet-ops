from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys
from typing import NamedTuple


class Violation(NamedTuple):
    line: int
    kind: str
    fragment: str


COMMAND_END = r"(?=$|[\s;&|()<>'\"])"
VARIABLE_PREFIX = r"(?:\$[A-Za-z_][A-Za-z0-9_]*|\$\{[A-Za-z_][A-Za-z0-9_]*\})"
FORBIDDEN_ABSOLUTE_EXECUTABLE = re.compile(
    rf"(?:{VARIABLE_PREFIX}|(?<![A-Za-z0-9_./+-]))"
    rf"(?P<path>/(?:usr/)?s?bin/[^/\s;&|()<>'\"]+)"
    rf"{COMMAND_END}"
)
PATH_REFERENCE = re.compile(
    r"(?P<expansion>\$\{PATH\}|\$PATH\b)"
    r"|(?P<assignment>\bPATH\s*\+?=)"
    r"|(?P<keyword>\b(?:export|readonly|declare|typeset|unset)\s+PATH\b)"
    r"|(?P<name>\bPATH\b)"
)
ALLOWED_SHEBANG = "#!/usr/bin/env bash"


def find_violations(text: str) -> list[Violation]:
    violations = [
        Violation(
            line=text.count("\n", 0, match.start()) + 1,
            kind="PATH reference",
            fragment=match.group(0),
        )
        for match in PATH_REFERENCE.finditer(text)
    ]
    first_line = text.splitlines()[0] if text else ""
    for match in FORBIDDEN_ABSOLUTE_EXECUTABLE.finditer(text):
        path = match.group("path")
        if (
            first_line == ALLOWED_SHEBANG
            and match.start("path") == 2
            and path == "/usr/bin/env"
        ):
            continue
        violations.append(
            Violation(
                line=text.count("\n", 0, match.start("path")) + 1,
                kind="absolute executable path",
                fragment=path,
            )
        )
    return sorted(violations, key=lambda violation: (violation.line, violation.fragment))


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
            f"{args.script}:{violation.line}: forbidden {violation.kind}: "
            f"{violation.fragment}",
            file=sys.stderr,
        )
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
