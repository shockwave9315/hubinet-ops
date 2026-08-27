"""Typed structural record/header decoding (S2 foundation layer).

Decodes a sealed decoded JSON Mapping (already produced by an S1 primitive
such as ``decode_jsonl_line``) into a small, immutable, typed structural
value. This module answers only "what structurally exists in this sealed
record" -- never "does this count as evidence," "is this authoritative," or
"what phase/interval is this."

Scalar syntax/type validation (nonnegative-integer semantics, bool-vs-int
handling, nonempty-string checks) is delegated to
``scripts.research.family_b_13_primitives`` (S1) -- this module never
reimplements it.

Only the cross-cutting structural fields required to build
:class:`~scripts.research.family_b_13_v7.participants.ParticipantLifetime`
later are modeled here: event kind, the declared ``harness_sequence`` (kept
purely as declared data, never treated as authoritative physical order),
``monotonic_ns``, and ``process_identity``. Event-specific payloads
(``healthy``, ``heartbeat_sequence``, ``complete``, ``analyzer_revision``,
``phenomenon_id``, subrun marker details, ...) are deliberately dropped
rather than smuggled through as an unrestricted raw mapping.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Sequence

from scripts.research.family_b_13_primitives import (
    require_nonempty_string,
    require_nonnegative_int,
)

from .physical import PhysicalPos, StreamName, assign_physical_positions

__all__ = [
    "HarnessEventKind",
    "StructuralDecodeError",
    "HarnessRecordHeader",
    "TimedRecordRef",
    "decode_harness_record_header",
    "decode_harness_stream",
    "decode_timed_record_ref",
]


class HarnessEventKind(str, Enum):
    """The closed harness-event vocabulary the frozen v6 oracle accepts
    (``HARNESS_EVENTS`` in the frozen oracle). An event kind outside this
    set is a structural decode error, never a silently-ignored record.

    S2 note: ``GAP_SIGNAL`` is only an event-kind label here. It does not
    mean "this run has a GAP" -- that is a later-stage (S3+) authority
    classification this package never makes. Likewise
    ``ACTIVE_ARCHIVE_HANDOFF`` is only an event kind; it discharges no
    obligation here, and ``CAPTURE_FINALIZED`` is only a typed event kind,
    carrying no post-finalization semantics in this package.
    """

    PROCESS_START = "process_start"
    PROCESS_STOP = "process_stop"
    PROCESS_CRASH = "process_crash"
    HEARTBEAT = "heartbeat"
    CAPTURE_FINALIZED = "capture_finalized"
    ANALYZER_VERSION = "analyzer_version"
    GAP_SIGNAL = "gap_signal"
    SCHEDULED_INTERLEAVING = "scheduled_interleaving"
    ACTIVE_ARCHIVE_HANDOFF = "active_archive_handoff"
    INDEX_ROTATION = "index_rotation"


class StructuralDecodeError(ValueError):
    """One sealed record fails a structural (syntax/typing/vocabulary)
    check. This is an S2-local structural-model error, never an external
    analyzer-outcome string (never ``HARNESS_INCOMPLETE``,
    ``B_S1_GAP_DETECTED``, or any other frozen/v7 result taxonomy)."""


@dataclass(frozen=True)
class HarnessRecordHeader:
    """The structural header of one ``harness-events.jsonl`` record.

    ``harness_sequence`` is kept as declared data only -- it is never used
    to derive, check, or substitute for ``pos`` (see
    ``scripts.research.family_b_13_v7.physical`` for why).

    ``__post_init__`` enforces every structural invariant this type
    requires -- stream-bound position, a real event kind, and S1 scalar
    typing on the remaining fields -- so these invariants hold for *any*
    construction path, not just :func:`decode_harness_record_header`. A
    ``harness-events.jsonl`` record type existing with, say, a
    ``StreamName.GROUND_TRUTH`` position is a structural impossibility this
    type refuses to represent at all, regardless of how it was built.
    """

    pos: PhysicalPos
    kind: HarnessEventKind
    harness_sequence: int
    monotonic_ns: int
    process_identity: str

    def __post_init__(self) -> None:
        if not isinstance(self.pos, PhysicalPos):
            raise StructuralDecodeError("record_position_invalid")
        if self.pos.stream is not StreamName.HARNESS_EVENTS:
            raise StructuralDecodeError("record_position_stream_mismatch")
        if not isinstance(self.kind, HarnessEventKind):
            raise StructuralDecodeError("record_event_unknown")
        sequence_result = require_nonnegative_int(self.harness_sequence)
        if not sequence_result.ok:
            raise StructuralDecodeError("record_harness_sequence_invalid")
        monotonic_result = require_nonnegative_int(self.monotonic_ns)
        if not monotonic_result.ok:
            raise StructuralDecodeError("record_monotonic_ns_invalid")
        identity_result = require_nonempty_string(self.process_identity)
        if not identity_result.ok:
            raise StructuralDecodeError("record_process_identity_invalid")


@dataclass(frozen=True)
class TimedRecordRef:
    """A minimal structural reference into ANY sealed stream: just its
    physical position and its declared ``monotonic_ns``. Used to ask a pure
    structural fact question about a record without modeling that record's
    full semantics -- e.g. "does this pre-T0-establishment record's
    timestamp fall inside the reader's structural lifetime?" (a
    :class:`~scripts.research.family_b_13_v7.participants.ParticipantLifetime`
    query), never an admission or authority decision.
    """

    pos: PhysicalPos
    monotonic_ns: int


def _require_field(raw: Mapping[str, Any], field: str) -> Any:
    if not isinstance(raw, Mapping) or field not in raw:
        raise StructuralDecodeError(f"record_field_missing:{field}")
    return raw[field]


def decode_harness_record_header(
    raw: Mapping[str, Any], pos: PhysicalPos
) -> HarnessRecordHeader:
    """Decode one sealed ``harness-events.jsonl`` record's structural
    header. ``raw`` is a plain decoded JSON object (e.g. from an S1
    ``decode_jsonl_line`` success value); ``pos`` is that record's already
    independently-assigned :class:`PhysicalPos` (never derived here).

    Only the ``event`` string -> :class:`HarnessEventKind` translation
    happens here (it needs the raw string to distinguish "not a string at
    all" from "a string outside the closed vocabulary"); every other
    structural invariant -- including on ``pos`` itself -- is enforced once,
    by :meth:`HarnessRecordHeader.__post_init__`, for every construction
    path.
    """

    event_value = _require_field(raw, "event")
    if not isinstance(event_value, str) or not event_value:
        raise StructuralDecodeError("record_event_invalid")
    try:
        kind = HarnessEventKind(event_value)
    except ValueError as exc:
        raise StructuralDecodeError("record_event_unknown") from exc

    return HarnessRecordHeader(
        pos=pos,
        kind=kind,
        harness_sequence=_require_field(raw, "harness_sequence"),
        monotonic_ns=_require_field(raw, "monotonic_ns"),
        process_identity=_require_field(raw, "process_identity"),
    )


def decode_harness_stream(raw_records: Sequence[Mapping[str, Any]]) -> list[HarnessRecordHeader]:
    """Decode a full ``harness-events.jsonl`` stream (already JSON-decoded,
    e.g. one dict per line from S1) into typed headers, one per physical
    record, in the stream's own physical order."""

    positions = assign_physical_positions(StreamName.HARNESS_EVENTS, raw_records)
    return [
        decode_harness_record_header(raw, pos)
        for raw, pos in zip(raw_records, positions)
    ]


def decode_timed_record_ref(raw: Mapping[str, Any], pos: PhysicalPos) -> TimedRecordRef:
    """Decode the minimal structural (position, timestamp) reference for
    one sealed record from any stream, ignoring every other field."""

    monotonic_result = require_nonnegative_int(_require_field(raw, "monotonic_ns"))
    if not monotonic_result.ok:
        raise StructuralDecodeError("record_monotonic_ns_invalid")
    return TimedRecordRef(pos=pos, monotonic_ns=monotonic_result.value)
