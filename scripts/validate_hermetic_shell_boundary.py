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


class ShellWord(NamedTuple):
    start: int
    end: int
    line: int
    raw: str
    value: str
    assembled: bool
    dynamic: bool
    malformed: bool


PARAMETER_NAME = r"(?:[A-Za-z_][A-Za-z0-9_]*|[0-9]+|[-!#$?*@])"
VARIABLE_PREFIX = rf"(?:\${PARAMETER_NAME}|\$\{{{PARAMETER_NAME}\}})"
SIMPLE_PARAMETER_EXPANSION = re.compile(VARIABLE_PREFIX)
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
SHELL_WORD_SEPARATORS = frozenset(" \t\r\n;&|()<>")


def _canonical_path(path: str) -> str:
    return posixpath.normpath(re.sub(r"/+", "/", path))


def _ansi_c_character(codepoint: int) -> str:
    # Bash arguments cannot contain NUL; ANSI-C NUL escapes disappear.
    return "" if codepoint == 0 else chr(codepoint)


def _ansi_c_projection(text: str, index: int) -> tuple[str, int]:
    marker = text[index + 1]
    simple = {
        "\\": "\\",
        "'": "'",
        '"': '"',
        "?": "?",
        "a": "\a",
        "b": "\b",
        "e": "\x1b",
        "E": "\x1b",
        "f": "\f",
        "n": "\n",
        "r": "\r",
        "t": "\t",
        "v": "\v",
    }
    if marker in simple:
        return simple[marker], index + 2

    if marker == "x":
        end = index + 2
        while end < len(text) and end < index + 4 and text[end] in "0123456789abcdefABCDEF":
            end += 1
        if end > index + 2:
            return _ansi_c_character(int(text[index + 2:end], 16)), end

    if marker in "01234567":
        if marker == "0":
            end = index + 2
            while end < len(text) and end < index + 5 and text[end] in "01234567":
                end += 1
            digits = text[index + 2:end] or "0"
        else:
            end = index + 2
            while end < len(text) and end < index + 4 and text[end] in "01234567":
                end += 1
            digits = text[index + 1:end]
        return _ansi_c_character(int(digits, 8)), end

    widths = {"u": 4, "U": 8}
    if marker in widths:
        width = widths[marker]
        end = index + 2
        slash_end: int | None = None
        while (
            end < len(text)
            and end < index + 2 + width
            and text[end] in "0123456789abcdefABCDEF"
        ):
            end += 1
            if int(text[index + 2:end], 16) == ord("/"):
                slash_end = end
        digits = text[index + 2:end]
        if digits:
            if slash_end is not None:
                # Conservatively retain a structural slash if a shorter valid
                # Unicode escape can assemble a forbidden path.
                return "/", slash_end
            codepoint = int(digits, 16)
            if codepoint <= sys.maxunicode:
                return _ansi_c_character(codepoint), end

    return f"\\{marker}", index + 2


def _shell_words(text: str) -> list[ShellWord]:
    words: list[ShellWord] = []
    index = 0
    length = len(text)

    while index < length:
        while index < length and text[index] in SHELL_WORD_SEPARATORS:
            index += 1
        if index >= length:
            break

        start = index
        line = text.count("\n", 0, start) + 1
        value: list[str] = []
        quote: str | None = None
        assembled = False
        dynamic = False
        malformed = False

        while index < length:
            character = text[index]
            if quote is None:
                if character in SHELL_WORD_SEPARATORS:
                    break
                if (
                    character == "$"
                    and index + 1 < length
                    and text[index + 1] in {"'", '"'}
                ):
                    assembled = True
                    dynamic = True
                    quote = (
                        "ansi-single"
                        if text[index + 1] == "'"
                        else "locale-double"
                    )
                    index += 2
                    continue
                if character == "$":
                    expansion = SIMPLE_PARAMETER_EXPANSION.match(text, index)
                    if expansion is not None:
                        assembled = True
                        dynamic = True
                        index = expansion.end()
                        continue
                if character in {"'", '"'}:
                    assembled = True
                    quote = character
                    index += 1
                    continue
                if character == "\\":
                    assembled = True
                    if index + 1 >= length:
                        malformed = True
                        index += 1
                        break
                    if text[index + 1] == "\n":
                        index += 2
                        continue
                    if (
                        text[index + 1] == "\r"
                        and index + 2 < length
                        and text[index + 2] == "\n"
                    ):
                        index += 3
                        continue
                    value.append(text[index + 1])
                    index += 2
                    continue
                value.append(character)
                index += 1
                continue

            closing_quote = (
                "'"
                if quote in {"'", "ansi-single"}
                else '"'
            )
            if character == closing_quote:
                quote = None
                index += 1
                continue
            if quote == "ansi-single" and character == "\\":
                assembled = True
                if index + 1 >= length:
                    malformed = True
                    index += 1
                    break
                projected, index = _ansi_c_projection(text, index)
                value.append(projected)
                continue
            if quote in {'"', "locale-double"} and character == "\\":
                assembled = True
                if index + 1 >= length:
                    malformed = True
                    index += 1
                    break
                escaped = text[index + 1]
                if escaped == "\n":
                    index += 2
                    continue
                if (
                    escaped == "\r"
                    and index + 2 < length
                    and text[index + 2] == "\n"
                ):
                    index += 3
                    continue
                if escaped in {'$', "`", '"', "\\"}:
                    value.append(escaped)
                else:
                    value.extend(("\\", escaped))
                index += 2
                continue
            if quote in {'"', "locale-double"} and character == "$":
                expansion = SIMPLE_PARAMETER_EXPANSION.match(text, index)
                if expansion is not None:
                    assembled = True
                    dynamic = True
                    index = expansion.end()
                    continue
            value.append(character)
            index += 1

        if quote is not None:
            malformed = True
        words.append(
            ShellWord(
                start=start,
                end=index,
                line=line,
                raw=text[start:index],
                value="".join(value),
                assembled=assembled,
                dynamic=dynamic,
                malformed=malformed,
            )
        )

    return words


def _display_shell_fragment(fragment: str) -> str:
    return (
        fragment.replace("\r", r"\r")
        .replace("\n", r"\n")
        .replace("\t", r"\t")
    )


def _shell_word_violations(
    words: list[ShellWord],
) -> list[tuple[Violation, int, int]]:
    results: list[tuple[Violation, int, int]] = []
    for word in words:
        if not (word.assembled or word.dynamic or word.malformed):
            continue
        for match in ABSOLUTE_PATH.finditer(word.value):
            path = match.group("path")
            canonical = _canonical_path(path)
            if canonical.startswith(FORBIDDEN_BASH_NETWORK_PREFIXES):
                path_kind = "Bash network device"
            elif canonical.startswith(FORBIDDEN_EXECUTABLE_PREFIXES):
                path_kind = "absolute executable path"
            else:
                continue

            if word.dynamic:
                kind = "dynamic shell path construction"
            elif word.malformed:
                kind = "ambiguous shell path construction"
            else:
                kind = f"shell-assembled {path_kind}"
            fragment = (
                f"{_display_shell_fragment(word.raw)} -> {canonical}"
            )
            results.append(
                (
                    Violation(line=word.line, kind=kind, fragment=fragment),
                    word.start,
                    word.end,
                )
            )
            break
    return results


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
    words = _shell_words(text)
    shell_word_violations = _shell_word_violations(words)
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
        if any(
            start <= match.start("path") < end
            for _, start, end in shell_word_violations
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
    violations.extend(
        violation for violation, _, _ in shell_word_violations
    )
    unique = {
        (violation.line, violation.kind, violation.fragment): violation
        for violation in violations
    }
    return sorted(
        unique.values(),
        key=lambda violation: (
            violation.line,
            violation.fragment,
            violation.kind,
        ),
    )


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
