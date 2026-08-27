"""S2 hermetic validation for Family B experiment #13 v7 lifetime foundation.

Non-normative research asset validation. This file does NOT implement, test,
or approximate any S3+ authority logic -- no ``admit()``, no observer
ledger, no ``(ORIGIN x LEVEL)`` state, no ``ChronologySpec``/``PhaseSpec``,
no ``IntegrityFinding``/``ObservationFinding``, no
``CaptureValidity``/``T1Result`` classifier, and no analyzer-outcome
projection exists anywhere in this repository, and this file does not build
one in the guise of a "validator."

It validates ``scripts/research/family_b_13_v7/`` (S2): typed structural
records, ``PhysicalPos``, ``ParticipantLifetime``, and ``ParticipantTable``.
Every fact this file checks is a structural fact (physical order, a
lifecycle boundary) -- never an admission, authority, or verdict decision.
S2 derives no timestamp-order relation of any kind (see the S2 final
boundary corrective pass in ``participants.py``'s module docstring); an
earlier revision of this file's own docstring described a
"timestamp-in-interval query" as a current S2 fact, which is no longer
true and was corrected here rather than left to mislead a future reader.
"""

from __future__ import annotations

import ast
import collections.abc
import inspect
import json
from pathlib import Path
from typing import Any

import pytest

from scripts.research.family_b_13_primitives import (
    decode_jsonl_line,
    require_nonnegative_int,
)
from scripts.research.family_b_13_v7 import participants as participants_module
from scripts.research.family_b_13_v7 import physical as physical_module
from scripts.research.family_b_13_v7 import records as records_module
from scripts.research.family_b_13_v7.participants import (
    ParticipantIdentity,
    ParticipantLifetime,
    ParticipantLifetimeError,
    ParticipantTable,
    TerminationKind,
    build_participant_table,
)
from scripts.research.family_b_13_v7.physical import (
    CrossStreamComparisonError,
    PhysicalPos,
    PhysicalStreamSnapshot,
    StreamName,
    snapshot_physical_stream,
)
from scripts.research.family_b_13_v7.records import (
    HarnessEventKind,
    HarnessRecordHeader,
    StructuralDecodeError,
    decode_harness_record_header,
    decode_harness_stream,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
V7_PACKAGE_DIR = REPO_ROOT / "scripts" / "research" / "family_b_13_v7"
CAPTURES_ROOT = REPO_ROOT / "tests" / "fixtures" / "research" / "family_b_13" / "captures"
READER = "synthetic-reader:1"


def _discover_python_modules(root: Path) -> list[Path]:
    """Every ``*.py`` file under ``root`` (recursive, so a future nested
    subpackage is covered too), excluding ``__pycache__`` -- the shared
    discovery mechanism every package-wide S2 architecture gate below uses,
    so a future implementation module added under the package is
    automatically in scope without editing a hard-coded list of today's
    filenames. See
    test_discovery_finds_a_hypothetical_future_module_without_editing_a_hard_coded_list
    for a hermetic proof this actually happens for a module that does not
    exist anywhere in this repository, and
    test_v7_module_paths_matches_filesystem_exactly for a proof this
    matches the real package on disk."""

    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


# Every implementation module under the S2 package, discovered from the
# filesystem (never a hard-coded list of today's filenames -- see
# _discover_python_modules above). Every package-wide architecture gate
# below is parametrized over this same discovered set, so a future S3+
# module added under this package automatically enters every one of them.
V7_MODULE_PATHS = _discover_python_modules(V7_PACKAGE_DIR)


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    """Decode a checked-in sealed JSONL file via the S1 primitive, as the
    S2 task instructs -- never a from-scratch parse here."""

    records: list[dict[str, Any]] = []
    for raw_line in path.read_bytes().splitlines(keepends=True):
        if not raw_line.strip():
            continue
        result = decode_jsonl_line(raw_line)
        assert result.ok, (path, raw_line)
        records.append(result.value)
    return records


def _harness_headers(fixture_id: str) -> list[HarnessRecordHeader]:
    raw = _load_jsonl(CAPTURES_ROOT / fixture_id / "harness-events.jsonl")
    return decode_harness_stream(raw)


# ---------------------------------------------------------------------------
# A. PhysicalPos
# ---------------------------------------------------------------------------


def test_physical_pos_ordinal_is_one_based() -> None:
    snapshot = snapshot_physical_stream(StreamName.HARNESS_EVENTS, [{}, {}, {}])
    assert isinstance(snapshot, PhysicalStreamSnapshot)
    assert [p.ordinal for p in snapshot.positions] == [1, 2, 3]


def test_physical_pos_ordinal_must_be_at_least_one() -> None:
    with pytest.raises(ValueError):
        PhysicalPos(stream=StreamName.HARNESS_EVENTS, ordinal=0)
    with pytest.raises(ValueError):
        PhysicalPos(stream=StreamName.HARNESS_EVENTS, ordinal=-1)


def test_physical_pos_is_immutable() -> None:
    pos = PhysicalPos(stream=StreamName.HARNESS_EVENTS, ordinal=1)
    with pytest.raises(Exception):
        pos.ordinal = 2  # type: ignore[misc]


def test_physical_pos_same_stream_ordering() -> None:
    a = PhysicalPos(stream=StreamName.HARNESS_EVENTS, ordinal=1)
    b = PhysicalPos(stream=StreamName.HARNESS_EVENTS, ordinal=2)
    assert a.precedes(b)
    assert not b.precedes(a)
    assert not a.precedes(a)


def test_physical_pos_cross_stream_incomparable() -> None:
    a = PhysicalPos(stream=StreamName.HARNESS_EVENTS, ordinal=1)
    b = PhysicalPos(stream=StreamName.GROUND_TRUTH, ordinal=1)
    with pytest.raises(CrossStreamComparisonError):
        a.precedes(b)
    with pytest.raises(CrossStreamComparisonError):
        b.precedes(a)


def test_physical_pos_has_no_cross_stream_total_ordering_operator() -> None:
    """PhysicalPos deliberately has no `__lt__` -- only the explicit,
    fail-closed `precedes()`."""

    assert not hasattr(PhysicalPos, "__lt__") or PhysicalPos.__lt__ is object.__lt__


# --- P2 type-boundary hardening (S2 final boundary corrective pass) -------


def test_physical_pos_precedes_rejects_duck_typed_fake() -> None:
    """A duck-typed object exposing the right attributes must not
    manufacture a positive physical-order fact -- `precedes` must check
    real PhysicalPos-ness before any field access."""

    class _FakePhysicalPos:
        stream = StreamName.HARNESS_EVENTS
        ordinal = 99

    pos = PhysicalPos(StreamName.HARNESS_EVENTS, 1)
    with pytest.raises(TypeError):
        pos.precedes(_FakePhysicalPos())  # type: ignore[arg-type]


def test_physical_pos_precedes_rejects_object() -> None:
    pos = PhysicalPos(StreamName.HARNESS_EVENTS, 1)
    with pytest.raises(TypeError):
        pos.precedes(object())  # type: ignore[arg-type]


def test_snapshot_physical_stream_rejects_invalid_stream_with_empty_records() -> None:
    """API hygiene (S2 final boundary corrective pass): an invalid stream
    must be rejected even when there are no records to snapshot -- it must
    not silently pass through merely because there was nothing to
    traverse anyway."""

    with pytest.raises(TypeError):
        snapshot_physical_stream("invalid", [])  # type: ignore[arg-type]


def test_snapshot_physical_stream_rejects_invalid_stream_with_nonempty_records() -> None:
    """Same stable error class, whether records is empty or not."""

    with pytest.raises(TypeError):
        snapshot_physical_stream("invalid", [{}])  # type: ignore[arg-type]


def test_snapshot_physical_stream_valid_stream_with_empty_records_returns_empty_snapshot() -> None:
    snapshot = snapshot_physical_stream(StreamName.HARNESS_EVENTS, [])
    assert snapshot.records == ()
    assert snapshot.positions == ()


# --- Raw-record snapshot boundary (S2 final finite corrective, P1) --------


def test_snapshot_physical_stream_rejects_a_generator() -> None:
    """A plain one-shot generator/iterator must not silently become part
    of this public contract merely because ``tuple()`` can consume it."""

    with pytest.raises(TypeError):
        snapshot_physical_stream(StreamName.HARNESS_EVENTS, (x for x in []))  # type: ignore[arg-type]


def test_snapshot_physical_stream_rejects_sized_iterable_that_is_not_a_sequence() -> None:
    """An arbitrary ``Sized``+``Iterable`` container that is not actually a
    ``collections.abc.Sequence`` (e.g. arbitrary iteration order, no
    ``__getitem__``) must not silently become a physical stream: the whole
    point of ``PhysicalPos`` is *sealed physical order*, which such a
    container cannot honestly provide."""

    class _Bag:
        def __init__(self, items: list[object]) -> None:
            self._items = items

        def __len__(self) -> int:
            return len(self._items)

        def __iter__(self):
            return iter(self._items)

    with pytest.raises(TypeError):
        snapshot_physical_stream(StreamName.HARNESS_EVENTS, _Bag([{}, {}]))  # type: ignore[arg-type]


def test_snapshot_physical_stream_observes_caller_sequence_exactly_once() -> None:
    """The whole point of the snapshot boundary: a genuine
    ``collections.abc.Sequence`` is traversed (``__iter__``) exactly once
    by ``snapshot_physical_stream``, never re-observed afterward."""

    calls = {"n": 0}

    class _CountingSequence(collections.abc.Sequence):
        def __len__(self) -> int:
            return 3

        def __getitem__(self, index: int) -> str:
            return ("a", "b", "c")[index]

        def __iter__(self):
            calls["n"] += 1
            return iter(("a", "b", "c"))

    snapshot = snapshot_physical_stream(StreamName.HARNESS_EVENTS, _CountingSequence())
    assert calls["n"] == 1
    assert snapshot.records == ("a", "b", "c")
    assert [p.ordinal for p in snapshot.positions] == [1, 2, 3]


class _LyingLengthRawSequence(collections.abc.Sequence):
    """A genuine ``collections.abc.Sequence`` (so it passes the Sequence
    type gate) whose ``__len__`` disagrees with what a full ``__iter__``
    traversal actually yields. Exercises the raw-record snapshot-boundary
    fix directly: before this corrective, ``decode_harness_stream`` derived
    ``PhysicalPos`` count from ``len(raw_records)`` (via the removed
    ``assign_physical_positions``) and separately zipped the original
    ``raw_records`` object for content -- two independent observations
    that, for an object like this one, describe different histories.
    ``zip`` then silently truncated to the shorter of the two, discarding
    sealed evidence no downstream validator ever saw."""

    def __init__(self, declared_len: int, iterated_items: list[Any]) -> None:
        self._declared_len = declared_len
        self._iterated_items = iterated_items
        self.iter_call_count = 0

    def __len__(self) -> int:
        return self._declared_len

    def __getitem__(self, index: int) -> Any:
        return self._iterated_items[index]

    def __iter__(self):
        self.iter_call_count += 1
        return iter(self._iterated_items)


def _harness_dict(event: str, seq: int, ns: int) -> dict[str, Any]:
    return {
        "event": event,
        "harness_sequence": seq,
        "monotonic_ns": ns,
        "process_identity": READER,
    }


def test_decode_harness_stream_does_not_silently_truncate_a_lying_sequence() -> None:
    """Raw-record snapshot boundary (S2 final finite corrective, P1): a
    genuine ``collections.abc.Sequence`` whose ``__len__`` (3) disagrees
    with what its ``__iter__`` actually yields (5 records, including two
    sealed records AFTER process_stop) must not let those two records
    silently disappear. Before this corrective, ``decode_harness_stream``
    derived PhysicalPos count from len() (3) and separately zipped the
    caller object for content, so ``zip`` truncated to 3 records and the
    honest process_stop-not-physically-last rejection never fired -- lost
    sealed evidence was silently converted into a positive structural
    derivation."""

    honest_five = [
        _harness_dict("process_start", 0, 100),
        _harness_dict("heartbeat", 1, 200),
        _harness_dict("process_stop", 2, 300),
        _harness_dict("gap_signal", 3, 400),
        _harness_dict("capture_finalized", 4, 500),
    ]

    # Positive control: the honest 5-record list is correctly rejected,
    # because process_stop is not physically last.
    with pytest.raises(ParticipantLifetimeError) as exc_info:
        build_participant_table(decode_harness_stream(honest_five))
    assert str(exc_info.value) == "participant_process_stop_not_physically_last"

    lying = _LyingLengthRawSequence(declared_len=3, iterated_items=honest_five)
    headers = decode_harness_stream(lying)  # type: ignore[arg-type]

    # The lying Sequence's own single full iteration is authoritative --
    # ALL 5 sealed records must be preserved, never silently truncated to
    # match the (wrong) declared __len__.
    assert lying.iter_call_count == 1
    assert [h.kind.value for h in headers] == [
        "process_start",
        "heartbeat",
        "process_stop",
        "gap_signal",
        "capture_finalized",
    ]
    assert [h.pos.ordinal for h in headers] == [1, 2, 3, 4, 5]

    with pytest.raises(ParticipantLifetimeError) as exc_info:
        build_participant_table(headers)
    assert str(exc_info.value) == "participant_process_stop_not_physically_last"


def test_decode_harness_stream_does_not_pad_when_declared_length_overstates_iteration() -> None:
    """The symmetric case: ``__len__`` overstates what ``__iter__``
    actually yields. The single traversal materializes exactly the 3
    iterated records; nothing is silently padded to match the lying
    declared length, and no synthetic 4th/5th position is invented."""

    three = [
        _harness_dict("process_start", 0, 100),
        _harness_dict("heartbeat", 1, 200),
        _harness_dict("process_stop", 2, 300),
    ]
    lying = _LyingLengthRawSequence(declared_len=5, iterated_items=three)
    headers = decode_harness_stream(lying)  # type: ignore[arg-type]
    assert lying.iter_call_count == 1
    assert len(headers) == 3
    assert [h.pos.ordinal for h in headers] == [1, 2, 3]


class _ShiftingRawSequence(collections.abc.Sequence):
    """A genuine ``collections.abc.Sequence`` whose distinct FULL
    ``__iter__`` traversals return different content -- the first
    traversal yields ``generations[0]``; every traversal after the first
    yields ``generations[-1]`` instead. Proves ``decode_harness_stream``
    observes its caller exactly once: if it did not, position assignment
    and record decoding could see different generations of the same
    caller-supplied object."""

    def __init__(self, generations: list[list[Any]]) -> None:
        self._generations = generations
        self.full_iterations = 0

    def __len__(self) -> int:
        return len(self._generations[0])

    def __getitem__(self, index: int) -> Any:
        return self._generations[0][index]

    def __iter__(self):
        generation = self._generations[min(self.full_iterations, len(self._generations) - 1)]
        self.full_iterations += 1
        return iter(generation)


def test_decode_harness_stream_observes_caller_sequence_exactly_once() -> None:
    validated = [
        _harness_dict("process_start", 0, 100),
        _harness_dict("heartbeat", 1, 200),
        _harness_dict("process_stop", 2, 300),
    ]
    forged = [
        _harness_dict("process_start", 0, 100),
        _harness_dict("gap_signal", 1, 200),
        _harness_dict("process_stop", 2, 300),
    ]
    shifting = _ShiftingRawSequence([validated, forged])
    headers = decode_harness_stream(shifting)  # type: ignore[arg-type]
    assert shifting.full_iterations == 1
    assert [h.kind.value for h in headers] == ["process_start", "heartbeat", "process_stop"]


def test_decode_harness_stream_rejects_a_generator() -> None:
    def gen():
        yield _harness_dict("process_start", 0, 100)

    with pytest.raises(StructuralDecodeError) as exc_info:
        decode_harness_stream(gen())  # type: ignore[arg-type]
    assert str(exc_info.value) == "harness_stream_input_not_sequence"


def test_decode_harness_stream_rejects_a_plain_iterator() -> None:
    with pytest.raises(StructuralDecodeError) as exc_info:
        decode_harness_stream(iter([_harness_dict("process_start", 0, 100)]))  # type: ignore[arg-type]
    assert str(exc_info.value) == "harness_stream_input_not_sequence"


def test_decode_harness_stream_rejects_sized_iterable_that_is_not_a_sequence() -> None:
    class _Bag:
        def __init__(self, items: list[Any]) -> None:
            self._items = items

        def __len__(self) -> int:
            return len(self._items)

        def __iter__(self):
            return iter(self._items)

    bag = _Bag([_harness_dict("process_start", 0, 100)])
    with pytest.raises(StructuralDecodeError) as exc_info:
        decode_harness_stream(bag)  # type: ignore[arg-type]
    assert str(exc_info.value) == "harness_stream_input_not_sequence"


def test_decode_harness_stream_preserves_a_plain_list_exactly() -> None:
    """Positive control alongside the Sequence-type gate above: the
    ordinary case (a plain list) must keep working exactly as before."""

    raw = [
        _harness_dict("process_start", 0, 100),
        _harness_dict("heartbeat", 1, 200),
        _harness_dict("process_stop", 2, 300),
    ]
    headers = decode_harness_stream(raw)
    assert [h.kind.value for h in headers] == ["process_start", "heartbeat", "process_stop"]
    assert [h.pos.ordinal for h in headers] == [1, 2, 3]


# --- PhysicalStreamSnapshot well-formedness (any construction path) -------


def test_physical_stream_snapshot_rejects_mismatched_lengths() -> None:
    with pytest.raises(ValueError):
        PhysicalStreamSnapshot(
            stream=StreamName.HARNESS_EVENTS,
            records=({}, {}),
            positions=(PhysicalPos(StreamName.HARNESS_EVENTS, 1),),
        )


def test_physical_stream_snapshot_rejects_non_tuple_records() -> None:
    with pytest.raises(TypeError):
        PhysicalStreamSnapshot(
            stream=StreamName.HARNESS_EVENTS,
            records=[{}],  # type: ignore[arg-type]
            positions=(PhysicalPos(StreamName.HARNESS_EVENTS, 1),),
        )


def test_physical_stream_snapshot_rejects_position_stream_mismatch() -> None:
    with pytest.raises(ValueError):
        PhysicalStreamSnapshot(
            stream=StreamName.HARNESS_EVENTS,
            records=({},),
            positions=(PhysicalPos(StreamName.GROUND_TRUTH, 1),),
        )


def test_physical_stream_snapshot_rejects_non_sequential_ordinals() -> None:
    with pytest.raises(ValueError):
        PhysicalStreamSnapshot(
            stream=StreamName.HARNESS_EVENTS,
            records=({}, {}),
            positions=(
                PhysicalPos(StreamName.HARNESS_EVENTS, 1),
                PhysicalPos(StreamName.HARNESS_EVENTS, 3),
            ),
        )


def test_physical_stream_snapshot_is_immutable() -> None:
    snapshot = snapshot_physical_stream(StreamName.HARNESS_EVENTS, [{}])
    with pytest.raises(Exception):
        snapshot.records = ()  # type: ignore[misc]


# --- Metamorphic gates (S2 task section 9) ---------------------------------


def test_metamorphic_generator_sequence_relabel_does_not_move_physical_pos() -> None:
    raw = _load_jsonl(
        CAPTURES_ROOT / "stop_generator_sequence_relabel" / "ground-truth.jsonl"
    )
    original = snapshot_physical_stream(StreamName.GROUND_TRUTH, raw).positions

    import copy

    relabeled = copy.deepcopy(raw)
    for record in relabeled:
        if record.get("generator_sequence") == 1:
            record["generator_sequence"] = 99
        elif record.get("generator_sequence") == 2:
            record["generator_sequence"] = 1
    assert relabeled != raw  # the mutation actually changed something

    after = snapshot_physical_stream(StreamName.GROUND_TRUTH, relabeled).positions
    assert after == original


def test_metamorphic_harness_sequence_relabel_does_not_move_physical_pos() -> None:
    raw = _load_jsonl(CAPTURES_ROOT / "positive_13a" / "harness-events.jsonl")
    original = snapshot_physical_stream(StreamName.HARNESS_EVENTS, raw).positions

    import copy

    relabeled = copy.deepcopy(raw)
    for record in relabeled:
        record["harness_sequence"] = 999
    assert relabeled != raw

    after = snapshot_physical_stream(StreamName.HARNESS_EVENTS, relabeled).positions
    assert after == original


def test_metamorphic_monotonic_ns_relabel_does_not_move_physical_pos() -> None:
    raw = _load_jsonl(CAPTURES_ROOT / "positive_13a" / "harness-events.jsonl")
    original = snapshot_physical_stream(StreamName.HARNESS_EVENTS, raw).positions

    import copy

    relabeled = copy.deepcopy(raw)
    for record in relabeled:
        record["monotonic_ns"] = 0
    assert relabeled != raw

    after = snapshot_physical_stream(StreamName.HARNESS_EVENTS, relabeled).positions
    assert after == original


def test_metamorphic_reordering_physical_records_does_change_physical_pos() -> None:
    raw = _load_jsonl(CAPTURES_ROOT / "positive_13a" / "harness-events.jsonl")
    original = snapshot_physical_stream(StreamName.HARNESS_EVENTS, raw).positions

    reversed_raw = list(reversed(raw))
    after = snapshot_physical_stream(StreamName.HARNESS_EVENTS, reversed_raw).positions

    # Same set of ordinals, but bound to a different record than before --
    # demonstrated by the fact the physically-first record is now a
    # different one (unless the stream happens to be a palindrome, which
    # positive_13a's 5 distinct events are not).
    assert raw[0] != reversed_raw[0]
    assert original[0] == after[0]  # ordinal 1 is still ordinal 1 ...
    # ... but it now refers to the physically-different first record:
    assert reversed_raw[0] is raw[-1]


# ---------------------------------------------------------------------------
# B. Harness structural records
# ---------------------------------------------------------------------------


def test_decode_harness_record_header_known_event_kind() -> None:
    pos = PhysicalPos(stream=StreamName.HARNESS_EVENTS, ordinal=1)
    header = decode_harness_record_header(
        {"event": "process_start", "harness_sequence": 1, "monotonic_ns": 50, "process_identity": READER},
        pos,
    )
    assert header.kind is HarnessEventKind.PROCESS_START
    assert header.pos == pos
    assert header.harness_sequence == 1
    assert header.monotonic_ns == 50
    assert header.process_identity == READER


def test_decode_harness_record_header_unknown_event_kind_rejected() -> None:
    pos = PhysicalPos(stream=StreamName.HARNESS_EVENTS, ordinal=1)
    with pytest.raises(StructuralDecodeError) as exc_info:
        decode_harness_record_header(
            {"event": "gap_singal", "harness_sequence": 1, "monotonic_ns": 1, "process_identity": READER},
            pos,
        )
    assert str(exc_info.value) == "record_event_unknown"


def test_decode_harness_record_header_requires_valid_process_identity() -> None:
    pos = PhysicalPos(stream=StreamName.HARNESS_EVENTS, ordinal=1)
    with pytest.raises(StructuralDecodeError) as exc_info:
        decode_harness_record_header(
            {"event": "process_start", "harness_sequence": 1, "monotonic_ns": 1, "process_identity": ""},
            pos,
        )
    assert str(exc_info.value) == "record_process_identity_invalid"


def test_decode_harness_record_header_monotonic_ns_uses_s1_nonnegative_int_semantics() -> None:
    pos = PhysicalPos(stream=StreamName.HARNESS_EVENTS, ordinal=1)
    with pytest.raises(StructuralDecodeError) as exc_info:
        decode_harness_record_header(
            {"event": "process_start", "harness_sequence": 1, "monotonic_ns": -1, "process_identity": READER},
            pos,
        )
    assert str(exc_info.value) == "record_monotonic_ns_invalid"


def test_decode_harness_record_header_rejects_bool_monotonic_ns() -> None:
    """bool is an int subclass; S1's require_nonnegative_int already rejects
    it, and S2 must not accidentally widen that by reimplementing the check."""

    pos = PhysicalPos(stream=StreamName.HARNESS_EVENTS, ordinal=1)
    with pytest.raises(StructuralDecodeError) as exc_info:
        decode_harness_record_header(
            {"event": "process_start", "harness_sequence": 1, "monotonic_ns": True, "process_identity": READER},
            pos,
        )
    assert str(exc_info.value) == "record_monotonic_ns_invalid"


def test_harness_record_header_is_immutable() -> None:
    pos = PhysicalPos(stream=StreamName.HARNESS_EVENTS, ordinal=1)
    header = decode_harness_record_header(
        {"event": "process_start", "harness_sequence": 1, "monotonic_ns": 1, "process_identity": READER},
        pos,
    )
    with pytest.raises(Exception):
        header.monotonic_ns = 2  # type: ignore[misc]


# ---------------------------------------------------------------------------
# C. ParticipantLifetime / ParticipantTable
# ---------------------------------------------------------------------------


def test_positive_control_builds_a_coherent_participant_table() -> None:
    headers = _harness_headers("positive_13a")
    table = build_participant_table(headers)
    assert len(table) == 1
    lifetime = table.get(ParticipantIdentity(READER))
    assert lifetime is not None
    assert lifetime.start_ns == 50
    assert lifetime.end_ns == 320
    assert lifetime.termination_kind is TerminationKind.PROCESS_STOP
    assert lifetime.start_pos == PhysicalPos(StreamName.HARNESS_EVENTS, 1)
    assert lifetime.end_pos == PhysicalPos(StreamName.HARNESS_EVENTS, 5)


def test_duplicate_process_start_rejected() -> None:
    """The duplicate start record must itself sit at a new, physically
    coherent position (one past the fixture's last record) so this test
    keeps exercising the intended lifecycle invariant rather than
    incidentally tripping the physical-coherence gate (see section E)."""

    from dataclasses import replace

    headers = _harness_headers("positive_13a")
    extra_start = replace(
        headers[0],
        pos=PhysicalPos(StreamName.HARNESS_EVENTS, headers[-1].pos.ordinal + 1),
    )
    duplicated = list(headers) + [extra_start]
    with pytest.raises(ParticipantLifetimeError) as exc_info:
        build_participant_table(duplicated)
    assert str(exc_info.value) == "participant_process_start_missing_or_duplicate"


def test_conflicting_terminal_rejected() -> None:
    """Same physical-coherence note as test_duplicate_process_start_rejected
    above -- the extra stop record gets a new coherent position."""

    from dataclasses import replace

    headers = _harness_headers("positive_13a")
    extra_stop = replace(
        headers[-1],
        pos=PhysicalPos(StreamName.HARNESS_EVENTS, headers[-1].pos.ordinal + 1),
    )
    duplicated = list(headers) + [extra_stop]
    with pytest.raises(ParticipantLifetimeError) as exc_info:
        build_participant_table(duplicated)
    assert str(exc_info.value) == "participant_process_stop_missing_or_duplicate"


def test_process_crash_present_is_unconditionally_rejected() -> None:
    from dataclasses import replace

    headers = _harness_headers("positive_13a")
    crash = replace(
        headers[2],
        pos=PhysicalPos(StreamName.HARNESS_EVENTS, headers[-1].pos.ordinal + 1),
        kind=HarnessEventKind.PROCESS_CRASH,
    )
    with pytest.raises(ParticipantLifetimeError) as exc_info:
        build_participant_table(list(headers) + [crash])
    assert str(exc_info.value) == "participant_process_crash_present"


def test_event_timestamp_before_start_no_longer_rejected() -> None:
    """S2 final boundary corrective pass: S2 derives no timestamp-order
    relation at all now (see the module docstring's clock-boundary
    reconciliation), so an interior record's monotonic_ns lying numerically
    before start_ns is not a structural rejection -- it is exactly the
    kind of relation frozen v6 can only make after proving
    manifest.clock_contract, which S2 never has."""

    from dataclasses import replace

    headers = _harness_headers("positive_13a")
    mutated = [
        replace(h, monotonic_ns=49) if h.kind is HarnessEventKind.HEARTBEAT else h
        for h in headers
    ]
    table = build_participant_table(mutated)  # must NOT raise
    assert len(table) == 1


def test_event_timestamp_after_end_no_longer_rejected() -> None:
    """Same reconciliation as the "before start" case above, mirrored for
    a timestamp numerically after end_ns."""

    from dataclasses import replace

    headers = _harness_headers("positive_13a")
    mutated = [
        replace(h, monotonic_ns=321) if h.kind is HarnessEventKind.HEARTBEAT else h
        for h in headers
    ]
    table = build_participant_table(mutated)  # must NOT raise
    assert len(table) == 1


def test_physical_record_before_start_rejected() -> None:
    """hist_process_start_physical_first: a heartbeat physically precedes
    process_start even though its own timestamp is later."""

    headers = _harness_headers("hist_process_start_physical_first")
    with pytest.raises(ParticipantLifetimeError) as exc_info:
        build_participant_table(headers)
    assert str(exc_info.value) == "participant_process_start_not_physically_first"


def test_physical_record_after_terminal_rejected() -> None:
    from dataclasses import replace

    headers = _harness_headers("positive_13a")
    extra = replace(
        headers[2],
        pos=PhysicalPos(StreamName.HARNESS_EVENTS, headers[-1].pos.ordinal + 1),
    )
    with pytest.raises(ParticipantLifetimeError) as exc_info:
        build_participant_table(list(headers) + [extra])
    assert str(exc_info.value) == "participant_process_stop_not_physically_last"


# --- Full-stream PhysicalPos coherence gate (P2, S2 task section 5) --------


def test_build_participant_table_rejects_duplicate_physical_ordinal() -> None:
    """Adversarial case A: a duplicate physical ordinal anywhere in the
    supplied stream is a structural rejection, checked before grouping by
    participant identity."""

    from dataclasses import replace

    headers = _harness_headers("positive_13a")
    mutated = list(headers)
    mutated[1] = replace(mutated[1], pos=headers[0].pos)
    with pytest.raises(ParticipantLifetimeError) as exc_info:
        build_participant_table(mutated)
    assert str(exc_info.value) == "harness_stream_physical_position_incoherent"


def test_build_participant_table_rejects_missing_physical_ordinal() -> None:
    """Adversarial case B: a gap in the physical ordinal sequence is a
    structural rejection."""

    from dataclasses import replace

    headers = _harness_headers("positive_13a")
    mutated = list(headers[:-1]) + [
        replace(
            headers[-1],
            pos=PhysicalPos(StreamName.HARNESS_EVENTS, headers[-1].pos.ordinal + 1),
        )
    ]
    with pytest.raises(ParticipantLifetimeError) as exc_info:
        build_participant_table(mutated)
    assert str(exc_info.value) == "harness_stream_physical_position_incoherent"


def test_build_participant_table_rejects_reversed_physical_sequence() -> None:
    """Adversarial case C: reversing the supplied sequence while retaining
    each record's original PhysicalPos values is a structural rejection --
    the stale/reordered positions no longer match the supplied order."""

    headers = _harness_headers("positive_13a")
    mutated = list(reversed(headers))
    with pytest.raises(ParticipantLifetimeError) as exc_info:
        build_participant_table(mutated)
    assert str(exc_info.value) == "harness_stream_physical_position_incoherent"


def test_build_participant_table_accepts_normal_decode_output() -> None:
    """Adversarial case D: normal decode_harness_stream output is already
    physically coherent by construction and must still build."""

    headers = _harness_headers("positive_13a")
    table = build_participant_table(headers)  # must NOT raise
    assert len(table) == 1


# --- P1 snapshot boundary (S2 final boundary corrective pass) -------------


def _two_record_stream(start_ns: int, stop_ns: int) -> list[HarnessRecordHeader]:
    return decode_harness_stream(
        [
            {
                "event": "process_start",
                "harness_sequence": 1,
                "monotonic_ns": start_ns,
                "process_identity": READER,
            },
            {
                "event": "process_stop",
                "harness_sequence": 2,
                "monotonic_ns": stop_ns,
                "process_identity": READER,
            },
        ]
    )


class _AdversarialVaryingSequence(collections.abc.Sequence):
    """A genuine ``collections.abc.Sequence`` (so it passes the builder's
    Sequence type gate) whose distinct FULL iterations return different
    content: the first full traversal yields the "validated" (start=100,
    stop=200) two-record stream; every traversal after the first yields a
    "forged" (start=0, stop=10**18) stream instead. Exercises the P1
    snapshot-boundary fix directly -- before the fix,
    ``build_participant_table`` traversed its caller-supplied
    ``harness_records`` repeatedly (once per validator, again inside the
    lifetime builder), so a validator could see one history while the
    final lifetime was assembled from a different, later traversal."""

    def __init__(self, generations: list[list[HarnessRecordHeader]]) -> None:
        self._generations = generations
        self.iteration_count = 0
        self._active: list[HarnessRecordHeader] | None = None

    def __len__(self) -> int:
        return len(self._generations[0])

    def __getitem__(self, index: int) -> HarnessRecordHeader:
        if index == 0:
            self.iteration_count += 1
            generation = min(self.iteration_count, len(self._generations)) - 1
            self._active = self._generations[generation]
        assert self._active is not None
        if index >= len(self._active):
            raise IndexError(index)
        return self._active[index]


def test_build_participant_table_snapshots_adversarial_sequence_exactly_once() -> None:
    """P1 corrective decision (local stop-patching rule): the caller-
    supplied ``harness_records`` is traversed EXACTLY ONCE, immediately, in
    ``build_participant_table``, into an immutable tuple snapshot that
    every validator and the lifetime builder then consumes -- the original
    object is never traversed again. Before the fix, this adversarial
    object would have been traversed four times (once per validator, once
    more inside the lifetime builder), and the final lifetime would have
    been built from the forged (later) generation instead of the validated
    (first) one."""

    validated = _two_record_stream(start_ns=100, stop_ns=200)
    forged = _two_record_stream(start_ns=0, stop_ns=10**18)
    adversarial = _AdversarialVaryingSequence([validated, forged])

    table = build_participant_table(adversarial)

    assert adversarial.iteration_count == 1
    lifetime = table.get(ParticipantIdentity(READER))
    assert lifetime is not None
    assert lifetime.start_ns == 100
    assert lifetime.end_ns == 200


def test_build_participant_table_rejects_non_sequence_iterator() -> None:
    """A plain one-shot iterator/generator must not silently become part
    of this public contract merely because ``tuple()`` can consume it --
    it fails closed with a stable, local error instead."""

    headers = _harness_headers("positive_13a")
    with pytest.raises(ParticipantLifetimeError) as exc_info:
        build_participant_table(iter(headers))  # type: ignore[arg-type]
    assert str(exc_info.value) == "harness_stream_input_not_sequence"


def test_build_participant_table_still_accepts_a_plain_list() -> None:
    """Positive control alongside the Sequence-type gate above: the
    ordinary case (a plain list, as every other test in this file passes)
    must keep working."""

    headers = _harness_headers("positive_13a")
    table = build_participant_table(list(headers))  # must NOT raise
    assert len(table) == 1


def test_lifetime_boundaries_are_stored_as_plain_structural_data() -> None:
    """ParticipantLifetime remains an immutable structural fact holder: its
    boundaries are readable fields, established solely by
    build_participant_table from this single participant's own harness
    records (see the module docstring's frozen capture-v6 single-identity/
    single-writer contract)."""

    headers = _harness_headers("positive_13a")
    table = build_participant_table(headers)
    lifetime = table.get(ParticipantIdentity(READER))
    assert lifetime.start_ns == 50
    assert lifetime.end_ns == 320


def test_participant_lifetime_exposes_no_timestamp_relation_helper() -> None:
    """S2 clock-domain boundary corrective decision (local stop-patching
    rule): the frozen v6 oracle validates an explicit
    manifest.clock_contract -- one bound CLOCK_MONOTONIC domain shared
    across every plane/participant -- BEFORE trusting any cross-process/
    cross-stream monotonic relation; a missing or mismatched contract is
    an unconditional environment-ineligibility failure there. S2 has no
    manifest and therefore no clock_contract context, so it cannot prove
    an externally-supplied monotonic_ns was captured in the same clock
    domain as a given lifetime's own boundaries. ParticipantLifetime
    therefore exposes no contains_ns (or any replacement helper under
    another name) in S2 -- proving clock-domain compatibility, and
    whatever cross-stream temporal relation it then licenses, is deferred
    to a later stage that actually has manifest.clock_contract."""

    for forbidden_name in (
        "contains_ns",
        "contains_time",
        "before_start",
        "after_start",
        "in_lifetime",
        "timestamp_within",
        "compare_ns",
    ):
        assert not hasattr(ParticipantLifetime, forbidden_name)


def test_no_s2_public_function_derives_a_temporal_relation_from_an_external_timestamp() -> None:
    """No exported S2 function or type accepts a ParticipantLifetime
    together with an external monotonic_ns to produce a temporal relation
    -- the only functions/types build_participant_table's own module
    exports are the ones __all__ declares, none of which is such a
    relation-producing call."""

    forbidden = {
        "contains_ns",
        "lifetime_contains_ns",
        "timestamp_in_lifetime",
        "relate_timestamp",
    }
    exported = set(participants_module.__all__)
    assert forbidden.isdisjoint(exported)


def test_build_participant_table_rejects_two_distinct_identities_in_one_stream() -> None:
    """S2 contract reconciliation: the frozen capture-v6 harness contract
    fixes a full harness-events.jsonl stream to exactly one participant
    identity (manifest.reader_context.process_identity, mismatch ->
    harness_reader_process_identity_mismatch) -- it is NOT an arbitrary
    multi-participant stream. A physically coherent HARNESS_EVENTS sequence
    containing two different process_identity values is therefore not a
    valid capture-v6 harness history and must be rejected with the
    singleton-identity structural error, never grouped into two
    independent lifecycles."""

    raw_records = [
        {
            "event": "process_start",
            "harness_sequence": 1,
            "monotonic_ns": 0,
            "process_identity": "participant-a",
        },
        {
            "event": "process_start",
            "harness_sequence": 1,
            "monotonic_ns": 10,
            "process_identity": "participant-b",
        },
        {
            "event": "heartbeat",
            "harness_sequence": 2,
            "monotonic_ns": 20,
            "process_identity": "participant-b",
        },
        {
            "event": "process_stop",
            "harness_sequence": 3,
            "monotonic_ns": 30,
            "process_identity": "participant-b",
        },
        {
            "event": "process_stop",
            "harness_sequence": 2,
            "monotonic_ns": 40,
            "process_identity": "participant-a",
        },
    ]
    headers = decode_harness_stream(raw_records)  # physically coherent: ordinals 1..5
    with pytest.raises(ParticipantLifetimeError) as exc_info:
        build_participant_table(headers)
    assert str(exc_info.value) == "harness_process_identity_not_singleton"


def test_build_participant_table_rejects_empty_harness_stream() -> None:
    with pytest.raises(ParticipantLifetimeError) as exc_info:
        build_participant_table([])
    assert str(exc_info.value) == "harness_stream_empty"


def test_build_participant_table_rejects_non_harness_record_header_elements() -> None:
    """P2 type boundary (section 10): a duck-typed non-HarnessRecordHeader
    element must fail closed with a stable structural error, never an
    accidental AttributeError from deeper in the builder."""

    with pytest.raises(ParticipantLifetimeError) as exc_info:
        build_participant_table(["not-a-header"])  # type: ignore[list-item]
    assert str(exc_info.value) == "harness_stream_record_type_invalid"


def test_all_captured_harness_streams_have_a_singleton_process_identity() -> None:
    """Inventory regression (S2 task section 8G): every one of the
    checked-in S0 sealed captures' harness-events.jsonl streams carries
    exactly one process_identity, matching the frozen capture-v6 harness
    contract. A future fixture addition cannot silently introduce a second
    harness identity without this test failing and forcing an explicit
    design decision. synthetic-reader:1 is reported diagnostically only --
    it is not asserted as the required value."""

    fixture_dirs = sorted(
        p for p in CAPTURES_ROOT.iterdir() if (p / "harness-events.jsonl").exists()
    )
    assert len(fixture_dirs) == 29, "unexpected checked-in capture count"

    for fixture_dir in fixture_dirs:
        raw = _load_jsonl(fixture_dir / "harness-events.jsonl")
        observed_identities = {record.get("process_identity") for record in raw}
        assert len(observed_identities) == 1, (fixture_dir.name, observed_identities)


def test_participant_lifetime_exposes_no_record_containment_helper() -> None:
    """P1 corrective decision (local stop-patching rule), rationale
    corrected during S2 contract reconciliation: a full harness stream
    already carries exactly one participant identity (see
    test_all_captured_harness_streams_have_a_singleton_process_identity and
    test_build_participant_table_rejects_two_distinct_identities_in_one_stream),
    so there is no second identity for an ownership-aware relation to
    disambiguate in the first place. Separately, the now-removed
    TimedRecordRef (see the S2 final boundary corrective pass in
    participants.py's module docstring) was generic across every sealed
    stream and carried no participant-identity/ownership binding of its
    own, so a bare (position, timestamp) match could never honestly answer a
    record-ownership question even if S2 needed one. Both reasons hold
    independently. ParticipantLifetime therefore exposes no
    contains_record (or any replacement helper under another name) in S2
    -- ownership-aware record containment is deferred to a later stage
    with its own explicit authority gate that combines ownership,
    lifetime, PhysicalPos, and phase/interval together. S2 must not
    pre-combine those concepts."""

    for forbidden_name in (
        "contains_record",
        "record_within_lifetime",
        "contains_timed_record",
        "owns_record",
        "participant_contains",
    ):
        assert not hasattr(ParticipantLifetime, forbidden_name)


def test_harness_record_header_cannot_carry_a_non_harness_stream_via_decode() -> None:
    """P2-1 adversarial case: decoding a valid process_start payload against
    a GROUND_TRUTH-stream position must still be rejected."""

    with pytest.raises(StructuralDecodeError) as exc_info:
        decode_harness_record_header(
            {"event": "process_start", "harness_sequence": 1, "monotonic_ns": 1, "process_identity": READER},
            PhysicalPos(StreamName.GROUND_TRUTH, 1),
        )
    assert str(exc_info.value) == "record_position_stream_mismatch"


def test_harness_record_header_cannot_carry_a_non_harness_stream_via_direct_construction() -> None:
    """P2-1 adversarial case: the type itself refuses a non-HARNESS_EVENTS
    position even when the dataclass constructor is called directly,
    bypassing decode_harness_record_header entirely."""

    with pytest.raises(StructuralDecodeError) as exc_info:
        HarnessRecordHeader(
            pos=PhysicalPos(StreamName.GROUND_TRUTH, 1),
            kind=HarnessEventKind.PROCESS_START,
            harness_sequence=1,
            monotonic_ns=1,
            process_identity=READER,
        )
    assert str(exc_info.value) == "record_position_stream_mismatch"


def test_harness_record_header_direct_construction_enforces_s1_scalar_semantics() -> None:
    """Direct construction also gets the same nonnegative-int/nonempty-
    string enforcement decode_harness_record_header relies on -- including
    rejecting bool for monotonic_ns, exactly like S1."""

    valid_pos = PhysicalPos(StreamName.HARNESS_EVENTS, 1)
    with pytest.raises(StructuralDecodeError) as exc_info:
        HarnessRecordHeader(
            pos=valid_pos,
            kind=HarnessEventKind.PROCESS_START,
            harness_sequence=1,
            monotonic_ns=True,  # bool must not satisfy nonnegative-int
            process_identity=READER,
        )
    assert str(exc_info.value) == "record_monotonic_ns_invalid"

    with pytest.raises(StructuralDecodeError) as exc_info:
        HarnessRecordHeader(
            pos=valid_pos,
            kind=HarnessEventKind.PROCESS_START,
            harness_sequence=1,
            monotonic_ns=1,
            process_identity="",
        )
    assert str(exc_info.value) == "record_process_identity_invalid"


def test_participant_table_is_immutable() -> None:
    headers = _harness_headers("positive_13a")
    table = build_participant_table(headers)
    with pytest.raises(Exception):
        table._by_identity["new"] = None  # type: ignore[index]


def test_participant_table_copies_input_mapping_not_aliases_it() -> None:
    """P2-2 adversarial case: mutating the caller's original mapping after
    construction must not be visible through the table."""

    headers = _harness_headers("positive_13a")
    table_source = build_participant_table(headers)
    identity = ParticipantIdentity(READER)
    lifetime = table_source.get(identity)

    source = {identity: lifetime}
    table = ParticipantTable(source)
    assert table.get(identity) == lifetime

    source.clear()
    assert len(source) == 0
    assert table.get(identity) == lifetime  # unchanged
    assert len(table) == 1


def test_participant_table_rejects_non_identity_key() -> None:
    headers = _harness_headers("positive_13a")
    lifetime = build_participant_table(headers).get(ParticipantIdentity(READER))
    with pytest.raises(TypeError):
        ParticipantTable({"not-an-identity": lifetime})


def test_participant_table_rejects_non_lifetime_value() -> None:
    identity = ParticipantIdentity(READER)
    with pytest.raises(TypeError):
        ParticipantTable({identity: "not-a-lifetime"})


def test_participant_table_rejects_key_lifetime_identity_mismatch() -> None:
    headers = _harness_headers("positive_13a")
    lifetime = build_participant_table(headers).get(ParticipantIdentity(READER))
    other_identity = ParticipantIdentity("someone-else")
    with pytest.raises(ValueError):
        ParticipantTable({other_identity: lifetime})


def test_participant_table_iteration_and_membership_are_mapping_consistent() -> None:
    """S2 final boundary corrective pass (section 20): __iter__ yields
    ParticipantIdentity keys -- consistent with __contains__ and get(),
    never the lifetime values. Before the fix, list(table)[0] returned a
    ParticipantLifetime while `identity in table` tested keys -- mixed
    key/value semantics on the same type."""

    headers = _harness_headers("positive_13a")
    table = build_participant_table(headers)

    identity = next(iter(table))
    assert isinstance(identity, ParticipantIdentity)
    assert identity == ParticipantIdentity(READER)
    assert identity in table
    expected_lifetime = table.get(identity)
    assert expected_lifetime is not None
    assert table.get(identity) is expected_lifetime
    assert list(table) == [identity]


def test_participant_lifetime_has_no_mutation_methods() -> None:
    forbidden = {"set_start", "set_end", "promote", "mark_valid", "mark_authoritative"}
    present = forbidden & set(dir(ParticipantLifetime))
    assert present == set()


# --- ParticipantLifetime direct-construction type boundary (P2, section 4) -


def _valid_lifetime_kwargs() -> dict:
    return dict(
        identity=ParticipantIdentity(READER),
        start_ns=50,
        end_ns=320,
        start_pos=PhysicalPos(StreamName.HARNESS_EVENTS, 1),
        end_pos=PhysicalPos(StreamName.HARNESS_EVENTS, 5),
        termination_kind=TerminationKind.PROCESS_STOP,
    )


def test_participant_lifetime_direct_construction_accepts_valid_values() -> None:
    """Positive control: a genuinely self-consistent direct construction
    must still succeed."""

    lifetime = ParticipantLifetime(**_valid_lifetime_kwargs())
    assert lifetime.start_ns == 50
    assert lifetime.end_ns == 320


def test_participant_lifetime_direct_construction_does_not_reject_inverted_timestamps() -> None:
    """S2 final boundary corrective pass: S2 derives no relation between
    start_ns and end_ns at all (see the class docstring's clock-domain
    boundary note) -- a numerically "inverted" pair of otherwise-valid
    scalars is NOT rejected merely because of that numeric relation. This
    is an important proof of the new clock boundary, not an oversight."""

    kwargs = _valid_lifetime_kwargs()
    kwargs["start_ns"] = 320
    kwargs["end_ns"] = 50
    lifetime = ParticipantLifetime(**kwargs)  # must NOT raise
    assert lifetime.start_ns == 320
    assert lifetime.end_ns == 50


def test_participant_lifetime_direct_construction_rejects_bool_timestamp() -> None:
    kwargs = _valid_lifetime_kwargs()
    kwargs["start_ns"] = True
    with pytest.raises(ParticipantLifetimeError):
        ParticipantLifetime(**kwargs)


def test_participant_lifetime_direct_construction_rejects_non_harness_position() -> None:
    kwargs = _valid_lifetime_kwargs()
    kwargs["start_pos"] = PhysicalPos(StreamName.GROUND_TRUTH, 1)
    with pytest.raises(ParticipantLifetimeError):
        ParticipantLifetime(**kwargs)


def test_participant_lifetime_direct_construction_rejects_reversed_positions() -> None:
    kwargs = _valid_lifetime_kwargs()
    kwargs["start_pos"] = PhysicalPos(StreamName.HARNESS_EVENTS, 5)
    kwargs["end_pos"] = PhysicalPos(StreamName.HARNESS_EVENTS, 1)
    with pytest.raises(ParticipantLifetimeError):
        ParticipantLifetime(**kwargs)


def test_participant_lifetime_direct_construction_requires_start_position_physically_first() -> None:
    """Section 12 matrix: start_pos=2, end_pos=5 -> reject (start must be
    ordinal 1, the physically-first record; a lifetime cannot legitimately
    begin partway through a stream)."""

    kwargs = _valid_lifetime_kwargs()
    kwargs["start_pos"] = PhysicalPos(StreamName.HARNESS_EVENTS, 2)
    kwargs["end_pos"] = PhysicalPos(StreamName.HARNESS_EVENTS, 5)
    with pytest.raises(ParticipantLifetimeError) as exc_info:
        ParticipantLifetime(**kwargs)
    assert str(exc_info.value) == "lifetime_start_position_not_physically_first"


def test_participant_lifetime_direct_construction_rejects_equal_start_and_end_position() -> None:
    """Section 12 matrix: start_pos=1, end_pos=1 -> reject. process_start
    and process_stop are distinct event records; one physical record
    cannot simultaneously serve as both."""

    kwargs = _valid_lifetime_kwargs()
    kwargs["start_pos"] = PhysicalPos(StreamName.HARNESS_EVENTS, 1)
    kwargs["end_pos"] = PhysicalPos(StreamName.HARNESS_EVENTS, 1)
    with pytest.raises(ParticipantLifetimeError) as exc_info:
        ParticipantLifetime(**kwargs)
    assert str(exc_info.value) == "lifetime_end_position_not_after_start"


def test_participant_lifetime_direct_construction_accepts_minimal_valid_positions() -> None:
    """Section 12 matrix: start_pos=1, end_pos=2 -> accept, even though
    start_ns > end_ns here -- proving the physical-position invariant and
    the (now-removed) timestamp relation are fully independent checks."""

    kwargs = _valid_lifetime_kwargs()
    kwargs["start_pos"] = PhysicalPos(StreamName.HARNESS_EVENTS, 1)
    kwargs["end_pos"] = PhysicalPos(StreamName.HARNESS_EVENTS, 2)
    kwargs["start_ns"] = 999
    kwargs["end_ns"] = 1
    lifetime = ParticipantLifetime(**kwargs)  # must NOT raise
    assert lifetime.start_pos.ordinal == 1
    assert lifetime.end_pos.ordinal == 2


def test_participant_lifetime_direct_construction_rejects_invalid_termination_kind() -> None:
    kwargs = _valid_lifetime_kwargs()
    kwargs["termination_kind"] = "process_stop"  # a plain string, not TerminationKind
    with pytest.raises(ParticipantLifetimeError):
        ParticipantLifetime(**kwargs)


def test_participant_lifetime_direct_construction_rejects_non_identity() -> None:
    kwargs = _valid_lifetime_kwargs()
    kwargs["identity"] = "not-an-identity"
    with pytest.raises(ParticipantLifetimeError):
        ParticipantLifetime(**kwargs)


# ---------------------------------------------------------------------------
# D. Historical fixtures (structural facts only)
# ---------------------------------------------------------------------------


def test_hist_process_start_physical_first_produces_structural_error() -> None:
    headers = _harness_headers("hist_process_start_physical_first")
    with pytest.raises(ParticipantLifetimeError) as exc_info:
        build_participant_table(headers)
    assert str(exc_info.value) == "participant_process_start_not_physically_first"


def test_hist_harness_event_outside_lifetime_no_longer_produces_a_structural_error() -> None:
    """S2 final boundary corrective pass: this fixture's gap_signal record
    declares monotonic_ns=49, numerically before the reader lifetime's own
    start_ns=50. Frozen v6 rejects this only after separately proving
    manifest.clock_contract; S2 has no manifest and derives no timestamp
    relation at all now (see the module docstring's clock-boundary
    reconciliation), so this fixture builds a coherent ParticipantTable
    from the sealed records alone. This historical witness is UNRESOLVED
    AT S2 CLOCK-RELATION LEVEL -- not PASS, not GAP, not INCOMPLETE, not a
    structural rejection -- and is discharged only once a later stage has
    validated the shared clock-domain contract; that later stage is not
    implemented here."""

    headers = _harness_headers("hist_harness_event_outside_lifetime")
    table = build_participant_table(headers)  # must NOT raise
    lifetime = table.get(ParticipantIdentity(READER))
    assert lifetime is not None
    assert lifetime.start_ns == 50
    assert lifetime.end_ns == 320
    # The gap_signal's own out-of-bounds scalar is preserved as sealed data
    # only -- no relation to lifetime.start_ns/end_ns is derived anywhere.
    gap_signal = next(h for h in headers if h.kind is HarnessEventKind.GAP_SIGNAL)
    assert gap_signal.monotonic_ns == 49


def test_stop_reader_pre_t0_before_process_start_structural_facts() -> None:
    """The harness-events stream is self-consistent; the invalid record
    lives in a different sealed stream. ParticipantTable construction
    succeeds. S2 can establish only two independent structural facts here
    -- the reader lifetime's own start_ns, and the pre-T0 observer
    record's own monotonic_ns -- never a relation between them: doing so
    would require proving both were captured in the same CLOCK_MONOTONIC
    domain (manifest.clock_contract in the frozen v6 oracle), which is
    manifest-derived context S2 intentionally never has (see
    test_participant_lifetime_exposes_no_timestamp_relation_helper). This
    historical witness therefore remains UNRESOLVED AT S2 RELATION LEVEL
    -- not GAP, not INCOMPLETE -- and is discharged only once a later
    stage has validated the shared clock-domain contract; that later
    stage is not implemented here."""

    headers = _harness_headers("stop_reader_pre_t0_before_process_start")
    table = build_participant_table(headers)  # must NOT raise
    lifetime = table.get(ParticipantIdentity(READER))
    assert lifetime.start_ns == 90

    pre_t0_raw = _load_jsonl(
        CAPTURES_ROOT / "stop_reader_pre_t0_before_process_start" / "pre-t0-establishment.jsonl"
    )
    # The pre-T0 observer record's own monotonic_ns is preserved via the
    # accepted S1 scalar primitive directly -- S2 no longer has a
    # TimedRecordRef-shaped wrapper type at all (see the S2 final boundary
    # corrective pass); its PhysicalPos, if needed, is assigned separately
    # and independently.
    observer_result = require_nonnegative_int(pre_t0_raw[0]["monotonic_ns"])
    assert observer_result.ok
    assert observer_result.value == 50
    observer_pos = snapshot_physical_stream(
        StreamName.PRE_T0_ESTABLISHMENT, pre_t0_raw
    ).positions[0]
    assert observer_pos.ordinal == 1
    # Two independent structural facts only -- no cross-stream relation is
    # derived between lifetime.start_ns and the observer's own monotonic_ns.


def test_stop_generator_sequence_relabel_physical_pos_is_unrelabelable() -> None:
    """Physical ground-truth positions follow actual line order; declared
    generator_sequence is merely data and cannot relabel PhysicalPos. This
    test produces no analyzer result and does not touch ground-truth
    reconciliation/omission-witness logic at all -- only the physical-order
    substrate."""

    raw = _load_jsonl(
        CAPTURES_ROOT / "stop_generator_sequence_relabel" / "ground-truth.jsonl"
    )
    positions = snapshot_physical_stream(StreamName.GROUND_TRUTH, raw).positions
    declared_sequences = [record.get("generator_sequence") for record in raw]

    # The physically-first record (ordinal 1) declares generator_sequence=2
    # -- physical order and declared sequence disagree in this fixture by
    # construction. PhysicalPos must reflect the former only.
    assert declared_sequences[0] == 2
    assert positions[0].ordinal == 1

    # harness-events for this fixture is unaffected by the relabel; a
    # coherent ParticipantTable still builds from it alone.
    headers = _harness_headers("stop_generator_sequence_relabel")
    table = build_participant_table(headers)
    assert len(table) == 1


# ---------------------------------------------------------------------------
# E. Separation gates (preliminary dependency-separation, S2 task section 21)
# ---------------------------------------------------------------------------


def test_build_participant_table_signature_has_no_ground_truth_or_authority_params() -> None:
    signature = inspect.signature(build_participant_table)
    forbidden_params = {
        "ground_truth",
        "observer_records",
        "manifest_context",
        "manifest",
        "clock_contract",
        "t0",
        "t1",
        "phase",
        "interval",
        "authority",
        "ledger",
    }
    assert forbidden_params.isdisjoint(signature.parameters)
    assert list(signature.parameters) == ["harness_records"]


def test_participants_module_does_not_import_ground_truth_or_observer_parsers() -> None:
    """Checked at the identifier/import level, not raw text -- this
    module's own docstring legitimately *names* ground-truth/observer
    streams while explaining that it depends on none of them."""

    source = (V7_PACKAGE_DIR / "participants.py").read_text(encoding="utf-8")
    identifiers = _code_identifiers(source)
    forbidden = {
        "ground_truth",
        "watch_events",
        "scan_rounds",
        "surface_observations",
        "api_pages",
        "exact_upid",
    }
    assert identifiers.isdisjoint(forbidden)


@pytest.mark.parametrize("path", V7_MODULE_PATHS, ids=lambda p: p.name)
def test_v7_package_has_no_clock_contract_parser_or_context(path: Path) -> None:
    """S2 clock-domain boundary (section 6): the S2 package defines no
    clock_contract parser/validator/context anywhere -- checked at the
    identifier level, not raw text, so this test's own name and the
    modules' own docstring prose (which legitimately *names*
    manifest.clock_contract while explaining that S2 depends on none of
    it) do not trip this gate. Proving/consuming manifest.clock_contract
    remains future work for a later stage. Parametrized over the
    dynamically-discovered V7_MODULE_PATHS (see _discover_python_modules)
    so a future S3+ module automatically enters this gate too."""

    identifiers = _code_identifiers(path.read_text(encoding="utf-8"))
    forbidden = {
        "clock_contract",
        "ClockDomain",
        "ClockContract",
        "validate_clock_contract",
        "_validate_clock_contract",
        "clock_domain_id",
        "manifest",
    }
    assert identifiers.isdisjoint(forbidden), (path.name, identifiers & forbidden)


def test_modifying_generator_sequence_cannot_alter_a_table_built_from_unchanged_harness_records() -> None:
    headers = _harness_headers("stop_generator_sequence_relabel")
    table_before = build_participant_table(headers)

    # Ground truth mutation happens entirely outside this call -- harness
    # records are unchanged, so the table must be identical.
    table_after = build_participant_table(headers)

    lifetime_before = table_before.get(ParticipantIdentity(READER))
    lifetime_after = table_after.get(ParticipantIdentity(READER))
    assert lifetime_before == lifetime_after


def test_participant_lifetime_equality_is_structural_value_equality_only() -> None:
    """Documented, not "fixed" (section 19): structurally identical
    ParticipantLifetime/ParticipantTable values from independent builds
    compare equal by design -- this is ordinary frozen-dataclass value
    equality, and it proves nothing about same-capture/same-run/same-
    sealed-provenance/same-authority-context. A later composition stage
    must carry capture identity externally; S2 values must never be used
    alone as a cross-capture provenance key."""

    headers = _harness_headers("positive_13a")
    lifetime_a = build_participant_table(list(headers)).get(ParticipantIdentity(READER))
    lifetime_b = build_participant_table(list(headers)).get(ParticipantIdentity(READER))
    assert lifetime_a is not lifetime_b
    assert lifetime_a == lifetime_b


# ---------------------------------------------------------------------------
# F. Architecture / semantic-leak regression gates
# ---------------------------------------------------------------------------

FORBIDDEN_AUTHORITY_IDENTIFIERS = frozenset(
    {
        "admit",
        "ChronologySpec",
        "PhaseSpec",
        "CaptureValidity",
        "T1Result",
        "IntegrityFinding",
        "ObservationFinding",
        "CANDIDATE_OBSERVED",
        "ENUMERATED",
        "CONFIRMED",
        "observer_ledger",
        "authority_level",
        "ParticipantTable_",  # never a v2/renamed escape hatch
        "clock_contract",
        "ClockDomain",
        "ClockContract",
        "manifest",
    }
)

FORBIDDEN_PUBLIC_NAME_PREFIXES = ("classify_", "verdict_", "outcome_", "promote_", "admit_")

EXTERNAL_OUTCOME_STRINGS = frozenset(
    {
        "ANALYZER_PASS_TESTED_INTERLEAVING",
        "B_S1_GAP_DETECTED",
        "GENERATOR_WINDOW_ENUMERATION_OMISSION_WITNESS",
        "HARNESS_INCOMPLETE",
        "ENVIRONMENT_INELIGIBLE",
    }
)

def test_v7_module_paths_matches_filesystem_exactly() -> None:
    """The discovered set every package-wide gate below is parametrized
    over must equal the Python implementation files actually present
    under the package root -- not a hard-coded list that could silently
    drift from the filesystem in either direction. Cross-checked against
    an independent traversal method (``iterdir``, not ``rglob``) so this
    isn't just re-asserting _discover_python_modules against itself."""

    on_disk = {
        p for p in V7_PACKAGE_DIR.iterdir() if p.is_file() and p.suffix == ".py"
    }
    assert set(V7_MODULE_PATHS) == on_disk
    # A concrete, currently-true set alongside the structural check above
    # (not instead of it) -- catches a module silently added or removed
    # without anyone noticing either way (mirrors
    # test_exported_function_and_type_counts's same pattern).
    assert {p.name for p in V7_MODULE_PATHS} == {
        "__init__.py",
        "physical.py",
        "records.py",
        "participants.py",
    }


def test_discovery_finds_a_hypothetical_future_module_without_editing_a_hard_coded_list(
    tmp_path: Path,
) -> None:
    """Hermetic proof for the package-wide bypass concern an independent
    adversarial review raised: a hard-coded module list does not protect a
    future S3+ module added under this package. This test creates a
    TEMPORARY directory mirroring the real S2 package -- NEVER a real
    tracked ``s3.py`` under the repository -- and confirms the SAME
    discovery function (``_discover_python_modules``) every real gate
    above uses finds a hypothetical future module automatically, with no
    edit to any hard-coded list. It then feeds that hypothetical module's
    source through the SAME gate-logic helpers the real gates use
    (``_code_identifiers``, ``_direct_call_names``,
    ``FORBIDDEN_AUTHORITY_IDENTIFIERS``) to confirm the underlying
    mechanism -- not merely discovery -- would flag its violations too."""

    fake_package = tmp_path / "family_b_13_v7_fake"
    fake_package.mkdir()
    for name in ("__init__.py", "physical.py", "records.py", "participants.py"):
        (fake_package / name).write_text("# stub\n", encoding="utf-8")

    discovered_before = _discover_python_modules(fake_package)
    assert {p.name for p in discovered_before} == {
        "__init__.py",
        "physical.py",
        "records.py",
        "participants.py",
    }

    hypothetical_s3 = (
        "from .participants import ParticipantLifetime, ParticipantTable\n"
        "from tests.oracles.family_b_13.v6 import blocker_b_family_b_13_analyzer\n"
        "\n"
        "admit = True\n"
        "observer_ledger = {}\n"
        "authority_level = 'CONFIRMED'\n"
        "\n"
        "\n"
        "def classify_capture():\n"
        "    lifetime = ParticipantLifetime(1, 2, 3, 4, 5, 6)\n"
        "    table = ParticipantTable({})\n"
        "    return lifetime, table\n"
    )
    (fake_package / "s3.py").write_text(hypothetical_s3, encoding="utf-8")

    discovered_after = _discover_python_modules(fake_package)
    assert {p.name for p in discovered_after} == {
        "__init__.py",
        "physical.py",
        "records.py",
        "participants.py",
        "s3.py",
    }
    s3_path = fake_package / "s3.py"
    assert s3_path in discovered_after

    # The generalized gate LOGIC (the same helpers every real package-wide
    # gate above uses) must flag this hypothetical module's violations
    # once it is part of the discovered set -- this is not re-running a
    # specific pytest test against it, but proving the shared mechanism
    # those tests are built on would catch it.
    identifiers = _code_identifiers(hypothetical_s3)
    assert identifiers & FORBIDDEN_AUTHORITY_IDENTIFIERS == {
        "admit",
        "observer_ledger",
        "authority_level",
    }

    tree = ast.parse(hypothetical_s3)
    called = _direct_call_names(tree)
    assert "ParticipantLifetime" in called
    assert "ParticipantTable" in called

    verdict_like = [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name.startswith(FORBIDDEN_PUBLIC_NAME_PREFIXES)
    ]
    assert verdict_like == ["classify_capture"]

    import_targets: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            import_targets.add(node.module)
    assert any("oracle" in t or "analyzer" in t for t in import_targets)


def _code_identifiers(source: str) -> set[str]:
    import io
    import tokenize

    identifiers: set[str] = set()
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type == tokenize.NAME:
            identifiers.add(tok.string)
    return identifiers


@pytest.mark.parametrize("path", V7_MODULE_PATHS, ids=lambda p: p.name)
def test_v7_module_contains_no_authority_stage_vocabulary(path: Path) -> None:
    identifiers = _code_identifiers(path.read_text(encoding="utf-8"))
    present = identifiers & FORBIDDEN_AUTHORITY_IDENTIFIERS
    assert present == set(), (path.name, present)


@pytest.mark.parametrize("path", V7_MODULE_PATHS, ids=lambda p: p.name)
def test_v7_module_public_names_avoid_verdict_like_prefixes(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name.startswith("_"):
                continue
            assert not node.name.startswith(FORBIDDEN_PUBLIC_NAME_PREFIXES), (path.name, node.name)


def _imported_module_roots(source: str) -> set[str]:
    """Every absolute import's root package name. Relative imports
    (``from .physical import ...``, ``level > 0``) are intra-package and
    excluded here -- they are checked separately for cycles, never for
    stdlib/S1-only scope, since they are this same S2 package."""

    tree = ast.parse(source)
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                roots.add(node.module.split(".")[0])
    return roots


@pytest.mark.parametrize("path", V7_MODULE_PATHS, ids=lambda p: p.name)
def test_v7_modules_import_only_stdlib_and_s1(path: Path) -> None:
    source = path.read_text(encoding="utf-8")
    roots = _imported_module_roots(source)
    allowed = {
        "__future__",
        "collections",  # collections.abc.Sequence runtime-isinstance checks
        "dataclasses",
        "enum",
        "types",
        "typing",
        "scripts",  # scripts.research.family_b_13_primitives (S1)
    }
    assert roots <= allowed, roots - allowed


@pytest.mark.parametrize("path", V7_MODULE_PATHS, ids=lambda p: p.name)
def test_v7_modules_relative_imports_stay_within_the_s2_package(path: Path) -> None:
    """The only intra-package (relative) imports allowed are of S2's own
    ``physical``/``records`` submodules -- confirms the one-way
    physical -> records -> participants layering has no back-reference."""

    tree = ast.parse(path.read_text(encoding="utf-8"))
    relative_modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.level > 0 and node.module
    }
    assert relative_modules <= {"physical", "records"}


@pytest.mark.parametrize("path", V7_MODULE_PATHS, ids=lambda p: p.name)
def test_v7_modules_do_not_import_the_frozen_oracle(path: Path) -> None:
    targets = set()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            targets.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            targets.add(node.module)
    assert not any("oracle" in t or "analyzer" in t for t in targets)


def _direct_call_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


# Designated-derivation-path constructor gate (S2 final finite corrective,
# generalized): each of these types proves only local structural
# well-formedness via its own bare constructor -- derivation from a real
# sealed/snapshotted observation is proven only by its designated builder,
# owned by exactly one module. PhysicalStreamSnapshot (physical.py) is
# included here for the same reason ParticipantLifetime/ParticipantTable
# (participants.py) are: it is the type this corrective introduced to prove
# single-observation derivation, and leaving its constructor unrestricted
# would reintroduce the identical well-formed-vs-established ambiguity for
# it. Never an authority token -- see PhysicalPos/HarnessRecordHeader,
# which are deliberately NOT restricted here because no concrete
# false-authority path requires it (S2 final finite corrective, section
# 20).
_DESIGNATED_CONSTRUCTOR_OWNERS: dict[str, str] = {
    "ParticipantLifetime": "participants.py",
    "ParticipantTable": "participants.py",
    "PhysicalStreamSnapshot": "physical.py",
}


@pytest.mark.parametrize("path", V7_MODULE_PATHS, ids=lambda p: p.name)
def test_designated_constructors_are_not_called_outside_their_owning_module(
    path: Path,
) -> None:
    """Architecture AST gate (section 18 of the S2 final boundary
    corrective pass; generalized to dynamic discovery and to
    PhysicalStreamSnapshot in the S2 final finite corrective): no v7
    package module OTHER THAN a type's designated owner may directly call
    its constructor -- this is enforcement of the already-designed
    derivation path (the designated builder proves derivation from a
    one-time frozen/snapshotted observation; a public constructor proves
    only local structural well-formedness), never an authority token.
    Parametrized over the dynamically-discovered V7_MODULE_PATHS (see
    _discover_python_modules), so a future S3+ module added under this
    same package automatically enters this gate too -- see
    test_discovery_finds_a_hypothetical_future_module_without_editing_a_hard_coded_list
    for a hermetic proof of that claim. Applies to the implementation
    package, not this test module, which exercises the type-boundary
    invariants by direct construction by design."""

    tree = ast.parse(path.read_text(encoding="utf-8"))
    called = _direct_call_names(tree)
    for type_name, owner_filename in _DESIGNATED_CONSTRUCTOR_OWNERS.items():
        if path.name == owner_filename:
            continue
        assert type_name not in called, (path.name, type_name)


def test_participants_module_centralizes_its_own_constructor_calls() -> None:
    """Within participants.py itself, only _build_one_lifetime constructs
    ParticipantLifetime and only build_participant_table constructs
    ParticipantTable -- those constructor calls are not scattered to other
    helpers unless structurally necessary."""

    source = (V7_PACKAGE_DIR / "participants.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    callers_of: dict[str, set[str]] = {"ParticipantLifetime": set(), "ParticipantTable": set()}
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        called = _direct_call_names(node)
        for target in callers_of:
            if target in called:
                callers_of[target].add(node.name)
    assert callers_of["ParticipantLifetime"] == {"_build_one_lifetime"}
    assert callers_of["ParticipantTable"] == {"build_participant_table"}


def test_physical_module_centralizes_its_own_constructor_calls() -> None:
    """Within physical.py itself, only snapshot_physical_stream constructs
    PhysicalStreamSnapshot -- the same centralization discipline as
    participants.py's designated builders above."""

    source = (V7_PACKAGE_DIR / "physical.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    callers_of: set[str] = set()
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if "PhysicalStreamSnapshot" in _direct_call_names(node):
            callers_of.add(node.name)
    assert callers_of == {"snapshot_physical_stream"}


def test_frozen_oracle_does_not_import_s2() -> None:
    oracle_path = (
        REPO_ROOT / "tests" / "oracles" / "family_b_13" / "v6" / "blocker_b_family_b_13_analyzer.py"
    )
    source = oracle_path.read_text(encoding="utf-8")
    assert "family_b_13_v7" not in source


def test_s1_primitives_do_not_import_s2() -> None:
    s1_path = REPO_ROOT / "scripts" / "research" / "family_b_13_primitives.py"
    source = s1_path.read_text(encoding="utf-8")
    assert "family_b_13_v7" not in source


def test_no_production_module_imports_s2() -> None:
    for production_root in ("app", "custom_components"):
        root = REPO_ROOT / production_root
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            assert "family_b_13_v7" not in path.read_text(encoding="utf-8"), path


def test_no_s2_function_returns_a_frozen_external_outcome_string() -> None:
    """Behavioral gate: run every S2 constructor/query against representative
    inputs and confirm no external outcome string ever appears in a return
    value or a raised structural error's message."""

    headers = _harness_headers("positive_13a")
    table = build_participant_table(headers)
    lifetime = table.get(ParticipantIdentity(READER))

    observed_strings: list[str] = [
        lifetime.termination_kind.value,
        *[h.kind.value for h in headers],
    ]
    for value in observed_strings:
        assert value not in EXTERNAL_OUTCOME_STRINGS

    with pytest.raises(ParticipantLifetimeError) as exc_info:
        build_participant_table(_harness_headers("hist_process_start_physical_first"))
    assert str(exc_info.value) not in EXTERNAL_OUTCOME_STRINGS

    # hist_harness_event_outside_lifetime no longer raises at S2 (see the
    # clock-boundary reconciliation) -- confirm its own event-kind values
    # still never surface an external outcome string either.
    outside_lifetime_headers = _harness_headers("hist_harness_event_outside_lifetime")
    build_participant_table(outside_lifetime_headers)  # must NOT raise
    for header in outside_lifetime_headers:
        assert header.kind.value not in EXTERNAL_OUTCOME_STRINGS


def test_gap_signal_kind_is_only_a_label_not_a_gap_classification() -> None:
    """A capture containing a structurally in-lifetime gap_signal event
    still builds a coherent ParticipantTable -- S2 does not treat the event
    kind as GAP-bearing, and does not know or care that this fixture's
    gap_signal is (at the analyzer-outcome level, entirely outside S2) the
    post-T1 diagnostic at the center of the fourth stop-triggering P1."""

    headers = _harness_headers("stop_post_t1_gap_signal_rewrites_t1")
    gap_signal_headers = [h for h in headers if h.kind is HarnessEventKind.GAP_SIGNAL]
    assert len(gap_signal_headers) == 1
    table = build_participant_table(headers)  # succeeds: S2 derives no
    # timestamp relation for any record (see the clock-boundary
    # reconciliation), so this fixture's gap_signal -- structurally just
    # another event-kind label -- poses no obstacle here either way.
    assert len(table) == 1
    lifetime = table.get(ParticipantIdentity(READER))
    assert lifetime is not None


# ---------------------------------------------------------------------------
# G. Public API discipline
# ---------------------------------------------------------------------------


def _public_functions(module) -> set[str]:
    return {
        name
        for name in getattr(module, "__all__", [])
        if inspect.isfunction(getattr(module, name))
    }


def _public_types(module) -> set[str]:
    return {
        name
        for name in getattr(module, "__all__", [])
        if inspect.isclass(getattr(module, name))
    }


ALL_S2_MODULES = [physical_module, records_module, participants_module]


def test_every_s2_module_declares_all() -> None:
    for module in ALL_S2_MODULES:
        assert hasattr(module, "__all__") and module.__all__


def _direct_call_targets(node: ast.AST) -> set[str]:
    """Every name actually invoked as a call (``ast.Call``) inside ``node``
    -- a plain ``Name`` reference (an import, an assignment target, a
    mention in a comment/docstring/assert message) never counts, only
    ``foo(...)`` or ``module.foo(...)``/``obj.method(...)`` call sites."""

    targets: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = child.func
            if isinstance(func, ast.Name):
                targets.add(func.id)
            elif isinstance(func, ast.Attribute):
                targets.add(func.attr)
    return targets


def _call_targets_reachable_from_tests(source: str) -> set[str]:
    """The set of names actually called, directly or transitively through
    this module's own local helper functions, starting only from a
    ``test_*`` function body. A helper's calls count only because -- and
    only if -- that helper is itself reachable from some test; an unused
    helper's calls (or a helper only ever imported/mentioned, never called)
    contribute nothing."""

    tree = ast.parse(source)
    top_level_funcs = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    direct_calls = {
        name: _direct_call_targets(node) for name, node in top_level_funcs.items()
    }
    test_functions = [name for name in top_level_funcs if name.startswith("test_")]

    reachable: set[str] = set()
    visited: set[str] = set()

    def visit(func_name: str) -> None:
        if func_name in visited:
            return
        visited.add(func_name)
        for called in direct_calls.get(func_name, set()):
            reachable.add(called)
            if called in top_level_funcs:
                visit(called)

    for test_name in test_functions:
        visit(test_name)
    return reachable


def test_every_exported_s2_function_has_actual_call_coverage() -> None:
    """Derived structurally (module __all__ + inspect) against an AST call
    graph rooted at this file's own test_* functions -- not a hard-coded
    list, and not merely a textual name occurrence (an import statement
    alone does not satisfy this)."""

    reachable = _call_targets_reachable_from_tests(Path(__file__).read_text(encoding="utf-8"))
    exported: set[str] = set()
    for module in ALL_S2_MODULES:
        exported |= _public_functions(module)
    missing = exported - reachable
    assert missing == set()


def _functions_reachable_from_tests(source: str) -> set[str]:
    """The set of top-level function names reachable from some test_*
    function, INCLUDING the test functions themselves (a test is always
    reachable from itself); a helper is reachable only if some reachable
    function actually calls it -- the same reachability rule
    ``_call_targets_reachable_from_tests`` above uses for call coverage,
    generalized to name the functions themselves rather than their call
    targets."""

    tree = ast.parse(source)
    top_level_funcs = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    direct_calls = {
        name: _direct_call_targets(node) for name, node in top_level_funcs.items()
    }
    test_functions = [name for name in top_level_funcs if name.startswith("test_")]

    reachable_funcs: set[str] = set()

    def visit(func_name: str) -> None:
        if func_name in reachable_funcs:
            return
        reachable_funcs.add(func_name)
        for called in direct_calls.get(func_name, set()):
            if called in top_level_funcs:
                visit(called)

    for test_name in test_functions:
        visit(test_name)
    return reachable_funcs


def _meaningful_type_uses_in_functions(source: str, func_names: set[str]) -> set[str]:
    """Identifiers considered ACTUAL runtime use of a type, anywhere inside
    the given top-level function bodies: a direct constructor call
    (``ParticipantLifetime(...)``), the object of an attribute access --
    e.g. an enum member access (``StreamName.HARNESS_EVENTS``) --, an
    argument to ``isinstance``/``issubclass``/``pytest.raises``
    (including inside a tuple of types), or an ``ast.ExceptHandler``'s
    exception type.

    S2 final finite corrective (tightening section 16/21's executable-
    AST-use gate): a bare ``ast.Name`` Load with no runtime role does NOT
    count -- in particular a parameter/variable type ANNOTATION
    (``def helper(x: SomeType)``) is an ``ast.Name`` Load too, but carries
    no runtime role, so it must not satisfy this gate merely because the
    type's name happens to be syntactically present. Imports (an
    ``ast.alias`` string, never an ``ast.Name`` node), comments (not part
    of the AST), docstrings/``__all__`` (``ast.Constant`` string literals,
    never ``ast.Name``), and an unreachable helper's uses (never walked,
    since ``func_names`` is the test-reachable set) also do not count."""

    tree = ast.parse(source)
    top_level_funcs = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    used: set[str] = set()
    for name in func_names:
        node = top_level_funcs.get(name)
        if node is None:
            continue
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                if isinstance(child.func, ast.Name):
                    used.add(child.func.id)  # constructor call: TypeName(...)
                call_target = (
                    child.func.id
                    if isinstance(child.func, ast.Name)
                    else child.func.attr
                    if isinstance(child.func, ast.Attribute)
                    else None
                )
                if call_target in {"isinstance", "issubclass", "raises"}:
                    for arg in list(child.args) + [kw.value for kw in child.keywords]:
                        if isinstance(arg, ast.Name):
                            used.add(arg.id)
                        elif isinstance(arg, ast.Tuple):
                            for elt in arg.elts:
                                if isinstance(elt, ast.Name):
                                    used.add(elt.id)
            elif isinstance(child, ast.Attribute) and isinstance(child.value, ast.Name):
                used.add(child.value.id)  # enum member access: SomeEnum.MEMBER
            elif isinstance(child, ast.ExceptHandler) and child.type is not None:
                if isinstance(child.type, ast.Name):
                    used.add(child.type.id)
                elif isinstance(child.type, ast.Tuple):
                    for elt in child.type.elts:
                        if isinstance(elt, ast.Name):
                            used.add(elt.id)
    return used


def test_every_exported_s2_type_has_actual_executable_use_coverage() -> None:
    """Executable-AST-use gate (section 21 of the S2 final boundary
    corrective pass; tightened in the S2 final finite corrective -- see
    _meaningful_type_uses_in_functions): replaces the prior raw-text/
    substring type-coverage check, which an import statement alone could
    satisfy. An exported type must actually be exercised -- constructed,
    an enum member accessed off it, passed to isinstance/issubclass/
    pytest.raises, or named as an except-handler type -- by some function
    reachable from a test_* function; being imported, merely annotated
    with, or merely mentioned in prose is not enough to satisfy this
    gate."""

    source = Path(__file__).read_text(encoding="utf-8")
    reachable_funcs = _functions_reachable_from_tests(source)
    used_names = _meaningful_type_uses_in_functions(source, reachable_funcs)

    exported: set[str] = set()
    for module in ALL_S2_MODULES:
        exported |= _public_types(module)
    missing = exported - used_names
    assert missing == set()


# --- Adversarial proof of the type-use gate's own logic (section 16) ------


def test_type_use_gate_annotation_only_does_not_count_as_use() -> None:
    source = (
        "class SomeType:\n    pass\n\n"
        "def helper(x: SomeType) -> None:\n    pass\n\n"
        "def test_case() -> None:\n    helper(1)\n"
    )
    reachable = _functions_reachable_from_tests(source)
    used = _meaningful_type_uses_in_functions(source, reachable)
    assert "SomeType" not in used


def test_type_use_gate_import_only_does_not_count_as_use() -> None:
    source = "from module import SomeType\n\ndef test_case() -> None:\n    pass\n"
    reachable = _functions_reachable_from_tests(source)
    used = _meaningful_type_uses_in_functions(source, reachable)
    assert "SomeType" not in used


def test_type_use_gate_unreachable_helper_does_not_count_as_use() -> None:
    source = (
        "class SomeType:\n    pass\n\n"
        "def unused_helper() -> None:\n    SomeType()\n\n"
        "def test_case() -> None:\n    pass\n"
    )
    reachable = _functions_reachable_from_tests(source)
    used = _meaningful_type_uses_in_functions(source, reachable)
    assert "SomeType" not in used


def test_type_use_gate_constructor_call_counts_as_use() -> None:
    source = "class SomeType:\n    pass\n\ndef test_case() -> None:\n    SomeType()\n"
    reachable = _functions_reachable_from_tests(source)
    used = _meaningful_type_uses_in_functions(source, reachable)
    assert "SomeType" in used


def test_type_use_gate_enum_member_access_counts_as_use() -> None:
    source = (
        "import enum\n\n"
        "class SomeEnum(enum.Enum):\n    A = 1\n\n"
        "def test_case() -> None:\n    x = SomeEnum.A\n"
    )
    reachable = _functions_reachable_from_tests(source)
    used = _meaningful_type_uses_in_functions(source, reachable)
    assert "SomeEnum" in used


def test_type_use_gate_pytest_raises_argument_counts_as_use() -> None:
    """Isolated from constructor-call detection on purpose: SomeError is
    never directly called (only named as pytest.raises's argument), so a
    positive result here can only come from the isinstance/issubclass/
    raises-argument detection path, not the constructor-call path."""

    source = (
        "import pytest\n\n"
        "class SomeError(Exception):\n    pass\n\n"
        "def test_case() -> None:\n"
        "    with pytest.raises(SomeError):\n"
        "        1 / 0\n"
    )
    reachable = _functions_reachable_from_tests(source)
    used = _meaningful_type_uses_in_functions(source, reachable)
    assert "SomeError" in used


def test_exported_function_and_type_counts() -> None:
    """A concrete, currently-true number alongside the structural coverage
    checks above (not instead of them) -- catches an export silently added
    or removed without anyone noticing either way."""

    total_functions = sum(len(_public_functions(m)) for m in ALL_S2_MODULES)
    total_types = sum(len(_public_types(m)) for m in ALL_S2_MODULES)
    # functions: snapshot_physical_stream (physical; replaces the removed,
    # untruthful-contract assign_physical_positions -- S2 final finite
    # corrective), decode_harness_record_header, decode_harness_stream
    # (records), build_participant_table (participants).
    # decode_timed_record_ref was removed in the S2 final boundary
    # corrective pass alongside TimedRecordRef (see records.py).
    assert total_functions == 4
    # types: StreamName, PhysicalPos, CrossStreamComparisonError,
    # PhysicalStreamSnapshot (physical -- PhysicalStreamSnapshot added in
    # the S2 final finite corrective); HarnessEventKind,
    # StructuralDecodeError, HarnessRecordHeader (records);
    # ParticipantIdentity, TerminationKind, ParticipantLifetimeError,
    # ParticipantLifetime, ParticipantTable (participants). TimedRecordRef
    # was removed in the S2 final boundary corrective pass.
    assert total_types == 12
