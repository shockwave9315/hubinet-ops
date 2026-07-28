from __future__ import annotations

import argparse
from pathlib import Path
import posixpath
import re
import sys
from typing import NamedTuple


class Violation(NamedTuple):
    line: int
    kind: str
    fragment: str


VARIABLE_PREFIX = r"(?:\$[A-Za-z_][A-Za-z0-9_]*|\$\{[A-Za-z_][A-Za-z0-9_]*\})"
ABSOLUTE_PATH = re.compile(
    rf"(?<![A-Za-z0-9_./:+-])(?:{VARIABLE_PREFIX})?"
    r"(?P<path>/[^\s;&|()<>'\"]+)"
)
PATH_REFERENCE = re.compile(
    r"(?P<expansion>\$\{PATH\}|\$PATH\b)"
    r"|(?P<assignment>\bPATH\s*\+?=)"
    r"|(?P<keyword>\b(?:export|readonly|declare|typeset|unset)\s+PATH\b)"
    r"|(?P<name>\bPATH\b)"
)
COMMAND_DEFAULT_PATH = re.compile(r"\bcommand\s+-p(?:\s|$)")
ALLOWED_SHEBANG = "#!/usr/bin/env bash"
FORBIDDEN_EXECUTABLE_PREFIXES = (
    "/bin/",
    "/sbin/",
    "/usr/bin/",
    "/usr/sbin/",
)
FORBIDDEN_BASH_NETWORK_PREFIXES = ("/dev/tcp/", "/dev/udp/")


def _canonical_path(path: str) -> str:
    return posixpath.normpath(re.sub(r"/+", "/", path))


def find_violations(text: str) -> list[Violation]:
    violations = [
        Violation(
            line=text.count("\n", 0, match.start()) + 1,
            kind="PATH reference",
            fragment=match.group(0),
        )
        for match in PATH_REFERENCE.finditer(text)
    ]
    violations.extend(
        Violation(
            line=text.count("\n", 0, match.start()) + 1,
            kind="command default PATH escape",
            fragment=match.group(0).rstrip(),
        )
        for match in COMMAND_DEFAULT_PATH.finditer(text)
    )
    first_line = text.splitlines()[0] if text else ""
    for match in ABSOLUTE_PATH.finditer(text):
        path = match.group("path")
        canonical = _canonical_path(path)
        if (
            first_line == ALLOWED_SHEBANG
            and match.start("path") == 2
            and path == "/usr/bin/env"
        ):
            continue
        if canonical.startswith(FORBIDDEN_BASH_NETWORK_PREFIXES):
            violations.append(
                Violation(
                    line=text.count("\n", 0, match.start("path")) + 1,
                    kind="Bash network device",
                    fragment=path,
                )
            )
            continue
        if not canonical.startswith(FORBIDDEN_EXECUTABLE_PREFIXES):
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
