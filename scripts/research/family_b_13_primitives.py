"""Pure parsing/decoding primitives for Family B experiment #13 (S1).

Non-normative research module. Not architecture authority, not a production
contract, not an experiment result. See
``docs/architecture/research/blocker-b-family-b-13-authority-core-redesign.md``
for the redesign checkpoint this module is a stage of.

Scope boundary (S1 core design rule)
-------------------------------------
This module answers exactly one kind of question, about exactly one input at
a time::

    "what syntactic/serialized primitive does this input represent?"

It never answers::

    "should this primitive count as evidence?"

Concretely, this module knows nothing about: cross-record chronology,
participant lifetime, phase/interval assignment, authority admission,
observer knowledge, ground-truth reconciliation, enumeration authority, gap
classification, capture validity, T1 classification, or subrun verdicts.
Those are strictly later-stage (S2+) concerns and do not exist here even in
embryonic form -- there is no ``ParticipantTable``, ``admit()``,
``ChronologySpec``, ``PhaseSpec``, observer ledger, or
``CaptureValidity``/``T1Result`` classifier anywhere in this file.

This module is standard-library only. It performs no network I/O, no
subprocess execution, knows nothing about a live PVE runtime, and has no
production imports. It does not import the frozen v6 oracle at
``tests/oracles/family_b_13/v6/``, does not read PR #52 source dynamically,
and does not import test modules.

Independent-implementation note (Model A)
------------------------------------------
This module is an independent implementation of the same frozen
``family-b-13-capture-v6`` serialization contract the historical v6 oracle
implements -- it does not import that oracle, is not imported by it, and was
not written by copying its source. Compatibility is *proven*, not assumed,
by ``tests/test_blocker_b_family_b_13_s1_primitives.py``'s differential
vectors, which run the same declarative input through both implementations
and require exact semantic agreement.

Frozen-v6 parser-surface inventory
-----------------------------------
Every helper in the byte-frozen v6 oracle
(``tests/oracles/family_b_13/v6/blocker_b_family_b_13_analyzer.py``) was
classified into one of four categories; only the first three belong in S1:

======================================  =======================  =========================================================
frozen v6 helper                        category                 in S1?
======================================  =======================  =========================================================
``_load_json``                          PURE_PARSER              yes -- ``parse_json_document``
``_load_jsonl`` (per-line decode step)  PURE_PARSER              yes -- ``decode_jsonl_line``
``_require_mapping``                    LEXICAL_VALIDATOR        yes -- ``require_mapping``
``_require_sequence``                   LEXICAL_VALIDATOR        yes -- ``require_sequence``
``_require_text``                       LEXICAL_VALIDATOR        yes -- ``require_nonempty_string``
``_require_string``                     LEXICAL_VALIDATOR        yes -- ``require_string``
``_require_int``                        LEXICAL_VALIDATOR        yes -- ``require_nonnegative_int``
``_require_signed_int``                 LEXICAL_VALIDATOR        yes -- ``require_signed_int``
``_require_bool``                       LEXICAL_VALIDATOR        yes -- ``require_bool``
``_require_upid`` / ``_decode_upid``    PURE_DECODER             yes -- ``parse_upid``
``_task_bucket_path`` (bucket half)     LEXICAL_VALIDATOR        yes -- ``validate_task_bucket_identifier``
``_decode_inotify_raw_mask``            PURE_DECODER             yes -- ``decode_inotify_raw_mask``
``_validated_inotify_masks``            PURE_DECODER             yes -- ``inotify_masks_agree`` (raw/declared agreement only;
                                                                        no chronology/record beyond the one mask pair)
``_watch_gap_reasons``                  PURE_DECODER             yes -- ``watch_gap_reasons`` (name-set -> reason-set only)
``_validated_canonical_absolute_path``  LEXICAL_VALIDATOR        yes -- ``validate_canonical_absolute_path``
``_without_trailing_line_terminator``   PURE_PARSER              yes -- ``strip_trailing_line_terminator``
``_split_lf_crlf_lines``                PURE_PARSER              yes -- ``split_lines_lf_crlf``
``_classify_exact_log_terminal_status`` PURE_DECODER             yes -- ``classify_terminal_status``
``_require_provenance_text``            LEXICAL_VALIDATOR        no  -- single-value lexical check, but its "unknown"/
                                                                        "placeholder" rejection encodes a provenance-
                                                                        eligibility policy judgment; left to a later stage
``_path_is_within``                     LEXICAL_VALIDATOR        no  -- pure string-relationship check, but every current
                                                                        caller uses it to establish mount/task-tree
                                                                        *authority* binding; deferred, not needed by any
                                                                        S1 vector family
``_validate_raw_result``                LEXICAL_VALIDATOR        no  -- combines a hash check with an ``available ==
                                                                        bool(raw_evidence)`` rule that already encodes an
                                                                        evidence-availability judgment; deferred to a
                                                                        later stage rather than split awkwardly
``_stat_identity``                      (trivial composition)    no  -- pure composition of ``_require_int`` twice; no
                                                                        independent primitive needed
``_capture_root`` / ``_safe_file``      CROSS_RECORD_VALIDATOR   no  -- consults the filesystem and a specific capture
                                                                        directory's file set
``_validated_task_tree_contract``       CROSS_RECORD_VALIDATOR   no  -- reads multiple manifest sub-fields together and
                                                                        fixes a specific ``bucket_layout`` contract value
``_validate_disposable_pve_provenance`` AUTHORITY_OR_CLASSIFICATION no -- decides live-fixture applicability
                                                                        (``ENVIRONMENT_INELIGIBLE``-adjacent)
``_validate_generator_contract``        AUTHORITY_OR_CLASSIFICATION no -- establishes generator evidence authority
``_validate_subrun_contract``           AUTHORITY_OR_CLASSIFICATION no -- declares/binds subrun evidence-id authority
                                                                        (this is exactly the family that stop-4 #3,
                                                                        "duplicate subrun evidence IDs", lives in)
``_validate_clock_contract``            AUTHORITY_OR_CLASSIFICATION no -- establishes cross-participant clock-domain trust
``_validate_manifest``                  AUTHORITY_OR_CLASSIFICATION no -- the manifest-wide admission entry point
``_derived_scan_observations``          CROSS_RECORD_VALIDATOR   no  -- reconciles a scan record against declared bucket
                                                                        observations
``_register_installed_watch`` /
``_bind_watch_event_to_installation``   CROSS_RECORD_VALIDATOR   no  -- binds one record to another via watch descriptors
``_parse_surface_raw_evidence``         CROSS_RECORD_VALIDATOR   no  -- source-typed, but its return value is fed straight
                                                                        into completeness-set membership; deferred rather
                                                                        than split ambiguously in S1
``_is_candidate_interval_watch``        AUTHORITY_OR_CLASSIFICATION no -- literally an interval-membership classifier
``_reject_generated_upids_not_...``     CROSS_RECORD_VALIDATOR   no -- compares evidence against ground-truth operation
                                                                        timing
======================================  =======================  =========================================================

A helper's name beginning with ``_require_`` or ``_validate_`` was
deliberately *not* used as the sole basis for inclusion; each was inspected
individually against the criterion above.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

__all__ = [
    "MAX_JSONL_LINE_BYTES",
    "UPID_PATTERN",
    "INOTIFY_EVENT_MASKS",
    "INOTIFY_KNOWN_RAW_MASK",
    "ParseResult",
    "DecodedUpid",
    "parse_json_document",
    "decode_jsonl_line",
    "require_mapping",
    "require_sequence",
    "require_nonempty_string",
    "require_string",
    "require_nonnegative_int",
    "require_signed_int",
    "require_bool",
    "parse_upid",
    "validate_task_bucket_identifier",
    "task_bucket_path",
    "decode_inotify_raw_mask",
    "inotify_masks_agree",
    "watch_gap_reasons",
    "validate_canonical_absolute_path",
    "strip_trailing_line_terminator",
    "split_lines_lf_crlf",
    "classify_terminal_status",
]


# Independently declared -- this is the same frozen ``family-b-13-capture-v6``
# serialization contract's line-size bound, not an import of the oracle's
# constant. See the module docstring's Model A note.
MAX_JSONL_LINE_BYTES = 1_048_576

# Independent regex reproduction of the frozen normalized-UPID lexical
# contract: ``UPID:node:pid:pstart:starttime:type:id:owner:``.
UPID_PATTERN = re.compile(
    r"^UPID:([A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?):"
    r"([0-9A-Fa-f]{8}):([0-9A-Fa-f]{8,9}):([0-9A-Fa-f]{8}):"
    r"([^:\s/]+):([^:\s/]*):([^:\s/]+):$"
)

# Independent reproduction of the Linux inotify UAPI event-output bits this
# bounded research protocol accepts (include/uapi/linux/inotify.h).
INOTIFY_EVENT_MASKS: dict[str, int] = {
    "IN_ACCESS": 0x00000001,
    "IN_MODIFY": 0x00000002,
    "IN_ATTRIB": 0x00000004,
    "IN_CLOSE_WRITE": 0x00000008,
    "IN_CLOSE_NOWRITE": 0x00000010,
    "IN_OPEN": 0x00000020,
    "IN_MOVED_FROM": 0x00000040,
    "IN_MOVED_TO": 0x00000080,
    "IN_CREATE": 0x00000100,
    "IN_DELETE": 0x00000200,
    "IN_DELETE_SELF": 0x00000400,
    "IN_MOVE_SELF": 0x00000800,
    "IN_UNMOUNT": 0x00002000,
    "IN_Q_OVERFLOW": 0x00004000,
    "IN_IGNORED": 0x00008000,
    "IN_ISDIR": 0x40000000,
}
INOTIFY_KNOWN_RAW_MASK = sum(INOTIFY_EVENT_MASKS.values())


@dataclass(frozen=True)
class ParseResult:
    """The uniform envelope every primitive in this module returns.

    ``value`` is always a bounded, typed value on success (a primitive
    scalar, a ``frozenset[str]``, or a small dataclass such as
    ``DecodedUpid``) -- never a free-form ``Mapping[str, Any]`` standing in
    for a real result shape. ``error`` is a short, stable, parser-local
    identifier (never a Python exception message or traceback) so that a
    differential comparison against another implementation's error can be
    exact rather than string-fuzzy.
    """

    ok: bool
    value: Any = None
    error: str | None = None

    @staticmethod
    def success(value: Any) -> "ParseResult":
        return ParseResult(ok=True, value=value, error=None)

    @staticmethod
    def failure(error: str) -> "ParseResult":
        return ParseResult(ok=False, value=None, error=error)


@dataclass(frozen=True)
class DecodedUpid:
    """The lexical decomposition of one normalized UPID string.

    Mirrors exactly the fields the frozen v6 oracle's ``_decode_upid``
    projects, as a typed value rather than a ``dict[str, str]``.
    """

    node: str
    pid: str
    pstart: str
    starttime: str
    task_type: str
    task_id: str
    owner: str
    task_bucket: str


# ---------------------------------------------------------------------------
# JSON / JSONL
# ---------------------------------------------------------------------------


def parse_json_document(text: str) -> ParseResult:
    """Decode one complete JSON document from text.

    Pure syntax only: does not care what the decoded value represents.
    """

    try:
        return ParseResult.success(json.loads(text))
    except json.JSONDecodeError:
        return ParseResult.failure("invalid_json")


def decode_jsonl_line(
    raw_line: bytes, *, max_line_bytes: int = MAX_JSONL_LINE_BYTES
) -> ParseResult:
    """Decode one physical JSONL line from its raw bytes.

    Mirrors the frozen v6 per-line contract: a line over the byte bound is
    rejected before decoding, a blank (whitespace-only) line is rejected,
    invalid UTF-8 or invalid JSON is rejected, and a syntactically valid but
    non-object JSON value is rejected -- this protocol's JSONL records are
    always objects.
    """

    if len(raw_line) > max_line_bytes:
        return ParseResult.failure("jsonl_line_too_large")
    if not raw_line.strip():
        return ParseResult.failure("blank_jsonl_line")
    # Matches the frozen v6 contract's granularity exactly: invalid UTF-8 and
    # invalid JSON syntax share one stable error identifier, since both are
    # "this physical line does not decode," not two distinguishable causes
    # the frozen protocol ever surfaces separately.
    try:
        value = json.loads(raw_line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ParseResult.failure("invalid_jsonl")
    if not isinstance(value, dict):
        return ParseResult.failure("jsonl_record_not_object")
    return ParseResult.success(value)


# ---------------------------------------------------------------------------
# Scalar typing primitives (operate on one bare value, not a record+field)
# ---------------------------------------------------------------------------


def require_mapping(value: Any) -> ParseResult:
    if not isinstance(value, dict):
        return ParseResult.failure("field_not_object")
    return ParseResult.success(value)


def require_sequence(value: Any) -> ParseResult:
    if not isinstance(value, list):
        return ParseResult.failure("field_not_array")
    return ParseResult.success(value)


def require_nonempty_string(value: Any) -> ParseResult:
    if not isinstance(value, str) or not value:
        return ParseResult.failure("field_not_nonempty_string")
    return ParseResult.success(value)


def require_string(value: Any) -> ParseResult:
    if not isinstance(value, str):
        return ParseResult.failure("field_not_string")
    return ParseResult.success(value)


def require_nonnegative_int(value: Any) -> ParseResult:
    # bool is an int subclass in Python; the frozen contract deliberately
    # never lets a boolean satisfy an integer field.
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return ParseResult.failure("field_not_nonnegative_integer")
    return ParseResult.success(value)


def require_signed_int(value: Any) -> ParseResult:
    if not isinstance(value, int) or isinstance(value, bool):
        return ParseResult.failure("field_not_integer")
    return ParseResult.success(value)


def require_bool(value: Any) -> ParseResult:
    if not isinstance(value, bool):
        return ParseResult.failure("field_not_boolean")
    return ParseResult.success(value)


# ---------------------------------------------------------------------------
# UPID
# ---------------------------------------------------------------------------


def parse_upid(text: Any) -> ParseResult:
    """Lexically decode one normalized UPID string.

    ``UPID:node:pid:pstart:starttime:type:id:owner:``. The task bucket is
    the final textual character of the (already 8-hex-digit) starttime
    field -- preserved verbatim, uppercase A-F is never lowercased, matching
    ``PVE::UPID::decode()``'s ``substr($starttime, 7, 8)`` selection in
    pinned pve-common 9.2.1.
    """

    if not isinstance(text, str):
        return ParseResult.failure("field_not_normalized_upid")
    match = UPID_PATTERN.fullmatch(text)
    if match is None:
        return ParseResult.failure("field_not_normalized_upid")
    node, pid, pstart, starttime, task_type, task_id, owner = match.groups()
    return ParseResult.success(
        DecodedUpid(
            node=node,
            pid=pid,
            pstart=pstart,
            starttime=starttime,
            task_type=task_type,
            task_id=task_id,
            owner=owner,
            task_bucket=starttime[-1],
        )
    )


def validate_task_bucket_identifier(value: Any) -> ParseResult:
    """A task bucket identifier is exactly one hex nibble character."""

    if not isinstance(value, str) or re.fullmatch(r"[0-9A-Fa-f]", value) is None:
        return ParseResult.failure("task_tree_bucket_identifier_invalid")
    return ParseResult.success(value)


def task_bucket_path(task_root: str, bucket: str) -> str:
    """Join an already-validated task root and bucket identifier.

    Pure string composition -- this does not itself validate either input;
    callers validate ``task_root`` with ``validate_canonical_absolute_path``
    and ``bucket`` with ``validate_task_bucket_identifier`` first.
    """

    return f"{task_root}/{bucket}"


# ---------------------------------------------------------------------------
# inotify
# ---------------------------------------------------------------------------


def decode_inotify_raw_mask(raw_mask: int) -> ParseResult:
    """Decode a raw inotify event mask into its known event-name set.

    An unknown bit fails closed rather than being silently dropped.
    """

    unknown_bits = raw_mask & ~INOTIFY_KNOWN_RAW_MASK
    if unknown_bits:
        return ParseResult.failure("inotify_raw_mask_unknown_bits")
    return ParseResult.success(
        frozenset(name for name, bit in INOTIFY_EVENT_MASKS.items() if raw_mask & bit)
    )


def inotify_masks_agree(raw_mask: int, declared_mask: list[Any]) -> ParseResult:
    """Decode ``raw_mask`` and require it to exactly agree with the declared
    textual mask list (no duplicates, same event-name set either direction).

    The raw mask remains authoritative; a declared textual mask can never
    introduce an event the raw mask does not carry, and vice versa.
    """

    decoded = decode_inotify_raw_mask(raw_mask)
    if not decoded.ok:
        return decoded
    if not isinstance(declared_mask, list):
        return ParseResult.failure("field_not_array")
    if not all(isinstance(item, str) and item for item in declared_mask):
        return ParseResult.failure("field_not_nonempty_string")
    declared_set = set(declared_mask)
    if len(declared_set) != len(declared_mask):
        return ParseResult.failure("inotify_declared_mask_duplicate")
    if declared_set != decoded.value:
        return ParseResult.failure("inotify_mask_mismatch_raw_mask")
    return ParseResult.success(decoded.value)


def watch_gap_reasons(masks: frozenset[str]) -> frozenset[str]:
    """Map a decoded inotify event-name set to structural observer-gap
    reason keywords. Purely a name-set -> name-set mapping; carries no
    opinion about whether the owning watch record is itself in scope for
    any particular interval or run."""

    reasons: set[str] = set()
    if "IN_Q_OVERFLOW" in masks:
        reasons.add("watch_queue_overflow")
    if masks.intersection({"IN_IGNORED", "IN_UNMOUNT", "IN_DELETE_SELF", "IN_MOVE_SELF"}):
        reasons.add("watch_invalidation_or_loss")
    return frozenset(reasons)


# ---------------------------------------------------------------------------
# POSIX lexical path
# ---------------------------------------------------------------------------


def validate_canonical_absolute_path(value: Any, *, allow_root: bool = False) -> ParseResult:
    """Validate one sealed POSIX path lexically, without host dereferencing.

    Canonical means: absolute, no trailing slash (unless it is exactly
    ``/`` and ``allow_root`` is set), no doubled slash, no NUL byte, and no
    ``.``/``..``/empty path component.
    """

    if not isinstance(value, str) or not value:
        return ParseResult.failure("task_tree_path_not_nonempty_string")
    if not value.startswith("/"):
        return ParseResult.failure("task_tree_path_not_absolute")
    if value == "/":
        if allow_root:
            return ParseResult.success(value)
        return ParseResult.failure("task_tree_path_not_canonical")
    if value.endswith("/") or "//" in value or "\x00" in value:
        return ParseResult.failure("task_tree_path_not_canonical")
    components = value.split("/")[1:]
    if any(component in {"", ".", ".."} for component in components):
        return ParseResult.failure("task_tree_path_not_canonical")
    return ParseResult.success(value)


# ---------------------------------------------------------------------------
# Exact task-log line / terminal-status serialization
# ---------------------------------------------------------------------------


def strip_trailing_line_terminator(raw: str) -> str:
    """Strip at most one trailing line terminator, never line content.

    A file's own final line terminator (a single trailing ``\\n`` or
    ``\\r\\n``) is a serialization artifact of capture, not terminal-line
    content. Any other whitespace is content and is never touched.
    """

    if raw.endswith("\r\n"):
        return raw[:-2]
    if raw.endswith("\n"):
        return raw[:-1]
    return raw


def split_lines_lf_crlf(raw: str) -> list[str]:
    """Split raw evidence only at literal LF and CRLF boundaries."""

    lines = raw.split("\n")
    return [
        line[:-1] if index < len(lines) - 1 and line.endswith("\r") else line
        for index, line in enumerate(lines)
    ]


def classify_terminal_status(terminal_status: str) -> str | None:
    """Classify one raw exact-log terminal line using pinned PVE syntax.

    Returns ``"ok"``, ``"warning"``, ``"error"``, or ``None`` if the line
    does not match a recognized terminal-status serialization. This is
    string-syntax classification only -- it says nothing about whether the
    line is the authoritative final line of any particular capture.
    """

    if terminal_status == "TASK OK":
        return "ok"
    if re.fullmatch(r"TASK WARNINGS: \d+", terminal_status):
        return "warning"
    if re.fullmatch(r"TASK ERROR: .+", terminal_status):
        return "error"
    return None
