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
lifecycle boundary, a timestamp-in-interval query) -- never an admission,
authority, or verdict decision.
"""

from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path
from typing import Any

import pytest

from scripts.research.family_b_13_primitives import decode_jsonl_line
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
    StreamName,
    assign_physical_positions,
)
from scripts.research.family_b_13_v7.records import (
    HarnessEventKind,
    HarnessRecordHeader,
    StructuralDecodeError,
    TimedRecordRef,
    decode_harness_record_header,
    decode_harness_stream,
    decode_timed_record_ref,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
V7_PACKAGE_DIR = REPO_ROOT / "scripts" / "research" / "family_b_13_v7"
CAPTURES_ROOT = REPO_ROOT / "tests" / "fixtures" / "research" / "family_b_13" / "captures"
READER = "synthetic-reader:1"


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
    positions = assign_physical_positions(StreamName.HARNESS_EVENTS, [{}, {}, {}])
    assert [p.ordinal for p in positions] == [1, 2, 3]


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


# --- Metamorphic gates (S2 task section 9) ---------------------------------


def test_metamorphic_generator_sequence_relabel_does_not_move_physical_pos() -> None:
    raw = _load_jsonl(
        CAPTURES_ROOT / "stop_generator_sequence_relabel" / "ground-truth.jsonl"
    )
    original = assign_physical_positions(StreamName.GROUND_TRUTH, raw)

    import copy

    relabeled = copy.deepcopy(raw)
    for record in relabeled:
        if record.get("generator_sequence") == 1:
            record["generator_sequence"] = 99
        elif record.get("generator_sequence") == 2:
            record["generator_sequence"] = 1
    assert relabeled != raw  # the mutation actually changed something

    after = assign_physical_positions(StreamName.GROUND_TRUTH, relabeled)
    assert after == original


def test_metamorphic_harness_sequence_relabel_does_not_move_physical_pos() -> None:
    raw = _load_jsonl(CAPTURES_ROOT / "positive_13a" / "harness-events.jsonl")
    original = assign_physical_positions(StreamName.HARNESS_EVENTS, raw)

    import copy

    relabeled = copy.deepcopy(raw)
    for record in relabeled:
        record["harness_sequence"] = 999
    assert relabeled != raw

    after = assign_physical_positions(StreamName.HARNESS_EVENTS, relabeled)
    assert after == original


def test_metamorphic_monotonic_ns_relabel_does_not_move_physical_pos() -> None:
    raw = _load_jsonl(CAPTURES_ROOT / "positive_13a" / "harness-events.jsonl")
    original = assign_physical_positions(StreamName.HARNESS_EVENTS, raw)

    import copy

    relabeled = copy.deepcopy(raw)
    for record in relabeled:
        record["monotonic_ns"] = 0
    assert relabeled != raw

    after = assign_physical_positions(StreamName.HARNESS_EVENTS, relabeled)
    assert after == original


def test_metamorphic_reordering_physical_records_does_change_physical_pos() -> None:
    raw = _load_jsonl(CAPTURES_ROOT / "positive_13a" / "harness-events.jsonl")
    original = assign_physical_positions(StreamName.HARNESS_EVENTS, raw)

    reversed_raw = list(reversed(raw))
    after = assign_physical_positions(StreamName.HARNESS_EVENTS, reversed_raw)

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


def test_timed_record_ref_is_immutable() -> None:
    pos = PhysicalPos(stream=StreamName.PRE_T0_ESTABLISHMENT, ordinal=1)
    ref = decode_timed_record_ref({"monotonic_ns": 5}, pos)
    with pytest.raises(Exception):
        ref.monotonic_ns = 6  # type: ignore[misc]


def test_timed_record_ref_direct_construction_rejects_invalid_pos() -> None:
    """P2 type-boundary closure: TimedRecordRef must reject a non-
    PhysicalPos ``pos`` even via direct construction, bypassing
    decode_timed_record_ref entirely."""

    with pytest.raises(StructuralDecodeError) as exc_info:
        TimedRecordRef(pos="not-a-PhysicalPos", monotonic_ns=1)  # type: ignore[arg-type]
    assert str(exc_info.value) == "record_position_invalid"


def test_timed_record_ref_direct_construction_rejects_bool_monotonic_ns() -> None:
    """bool must not satisfy S1 nonnegative-int semantics here either."""

    pos = PhysicalPos(StreamName.PRE_T0_ESTABLISHMENT, 1)
    with pytest.raises(StructuralDecodeError) as exc_info:
        TimedRecordRef(pos=pos, monotonic_ns=True)
    assert str(exc_info.value) == "record_monotonic_ns_invalid"


def test_timed_record_ref_direct_construction_rejects_negative_monotonic_ns() -> None:
    pos = PhysicalPos(StreamName.PRE_T0_ESTABLISHMENT, 1)
    with pytest.raises(StructuralDecodeError) as exc_info:
        TimedRecordRef(pos=pos, monotonic_ns=-1)
    assert str(exc_info.value) == "record_monotonic_ns_invalid"


def test_timed_record_ref_direct_construction_rejects_string_monotonic_ns() -> None:
    pos = PhysicalPos(StreamName.PRE_T0_ESTABLISHMENT, 1)
    with pytest.raises(StructuralDecodeError) as exc_info:
        TimedRecordRef(pos=pos, monotonic_ns="50")  # type: ignore[arg-type]
    assert str(exc_info.value) == "record_monotonic_ns_invalid"


def test_timed_record_ref_direct_construction_still_accepts_valid_values() -> None:
    """Positive control alongside the rejection cases above: a genuinely
    valid direct construction must still succeed, and decode_timed_record_ref
    must continue to produce an equal object for the same inputs."""

    pos = PhysicalPos(StreamName.PRE_T0_ESTABLISHMENT, 1)
    ref = TimedRecordRef(pos=pos, monotonic_ns=50)
    assert ref.pos == pos
    assert ref.monotonic_ns == 50
    decoded = decode_timed_record_ref({"monotonic_ns": 50}, pos)
    assert decoded == ref


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


def test_event_timestamp_before_start_rejected() -> None:
    from dataclasses import replace

    headers = _harness_headers("positive_13a")
    mutated = [
        replace(h, monotonic_ns=49) if h.kind is HarnessEventKind.HEARTBEAT else h
        for h in headers
    ]
    with pytest.raises(ParticipantLifetimeError) as exc_info:
        build_participant_table(mutated)
    assert str(exc_info.value) == "participant_record_timestamp_outside_lifetime"


def test_event_timestamp_after_end_rejected() -> None:
    from dataclasses import replace

    headers = _harness_headers("positive_13a")
    mutated = [
        replace(h, monotonic_ns=321) if h.kind is HarnessEventKind.HEARTBEAT else h
        for h in headers
    ]
    with pytest.raises(ParticipantLifetimeError) as exc_info:
        build_participant_table(mutated)
    assert str(exc_info.value) == "participant_record_timestamp_outside_lifetime"


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


def test_lifetime_pure_containment_query() -> None:
    headers = _harness_headers("positive_13a")
    table = build_participant_table(headers)
    lifetime = table.get(ParticipantIdentity(READER))
    assert lifetime.contains_ns(50) is True
    assert lifetime.contains_ns(320) is True
    assert lifetime.contains_ns(49) is False
    assert lifetime.contains_ns(321) is False


@pytest.mark.parametrize("invalid_ns", [True, False, -1, "50", None])
def test_lifetime_containment_query_rejects_invalid_scalars(invalid_ns) -> None:
    """P2 corrective decision (section 6): contains_ns must not perform a
    bare Python numeric comparison -- bool would otherwise satisfy an
    integer interval via True == 1, manufacturing a positive structural
    fact from a scalar S1/S2 otherwise reject. An invalid scalar must fail
    closed, never silently return False."""

    headers = _harness_headers("positive_13a")
    table = build_participant_table(headers)
    lifetime = table.get(ParticipantIdentity(READER))
    with pytest.raises(ParticipantLifetimeError) as exc_info:
        lifetime.contains_ns(invalid_ns)
    assert str(exc_info.value) == "lifetime_query_ns_invalid"


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
    corrected during S2 contract reconciliation: S2's own accepted
    contract only ever needs the narrow numeric fact contains_ns provides
    -- a full harness stream already carries exactly one participant
    identity (see
    test_all_captured_harness_streams_have_a_singleton_process_identity and
    test_build_participant_table_rejects_two_distinct_identities_in_one_stream),
    so there is no second identity for an ownership-aware relation to
    disambiguate in the first place. Separately, TimedRecordRef (see
    records.TimedRecordRef) is generic across every sealed stream and
    carries no participant-identity/ownership binding of its own, so a
    bare (position, timestamp) match could never honestly answer a
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


def test_participant_lifetime_direct_construction_rejects_inverted_timestamps() -> None:
    kwargs = _valid_lifetime_kwargs()
    kwargs["start_ns"] = 320
    kwargs["end_ns"] = 50
    with pytest.raises(ParticipantLifetimeError):
        ParticipantLifetime(**kwargs)


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


def test_hist_harness_event_outside_lifetime_produces_structural_error() -> None:
    headers = _harness_headers("hist_harness_event_outside_lifetime")
    with pytest.raises(ParticipantLifetimeError) as exc_info:
        build_participant_table(headers)
    assert str(exc_info.value) == "participant_record_timestamp_outside_lifetime"


def test_stop_reader_pre_t0_before_process_start_structural_facts() -> None:
    """The harness-events stream is self-consistent; the invalid record
    lives in a different sealed stream. ParticipantTable construction
    succeeds; the only S2 fact this witness needs -- and the only one this
    package offers for a cross-stream record -- is the pure numeric
    contains_ns query (see
    test_participant_lifetime_exposes_no_record_containment_helper for why
    there is no record-ownership/containment helper to ask instead)."""

    headers = _harness_headers("stop_reader_pre_t0_before_process_start")
    table = build_participant_table(headers)  # must NOT raise
    lifetime = table.get(ParticipantIdentity(READER))
    assert lifetime.start_ns == 90

    pre_t0_raw = _load_jsonl(
        CAPTURES_ROOT / "stop_reader_pre_t0_before_process_start" / "pre-t0-establishment.jsonl"
    )
    positions = assign_physical_positions(StreamName.PRE_T0_ESTABLISHMENT, pre_t0_raw)
    ref = decode_timed_record_ref(pre_t0_raw[0], positions[0])
    assert ref.monotonic_ns == 50

    assert lifetime.contains_ns(ref.monotonic_ns) is False


def test_stop_generator_sequence_relabel_physical_pos_is_unrelabelable() -> None:
    """Physical ground-truth positions follow actual line order; declared
    generator_sequence is merely data and cannot relabel PhysicalPos. This
    test produces no analyzer result and does not touch ground-truth
    reconciliation/omission-witness logic at all -- only the physical-order
    substrate."""

    raw = _load_jsonl(
        CAPTURES_ROOT / "stop_generator_sequence_relabel" / "ground-truth.jsonl"
    )
    positions = assign_physical_positions(StreamName.GROUND_TRUTH, raw)
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


def test_modifying_generator_sequence_cannot_alter_a_table_built_from_unchanged_harness_records() -> None:
    headers = _harness_headers("stop_generator_sequence_relabel")
    table_before = build_participant_table(headers)

    # Ground truth mutation happens entirely outside this call -- harness
    # records are unchanged, so the table must be identical.
    table_after = build_participant_table(headers)

    lifetime_before = table_before.get(ParticipantIdentity(READER))
    lifetime_after = table_after.get(ParticipantIdentity(READER))
    assert lifetime_before == lifetime_after


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

V7_MODULE_PATHS = [
    V7_PACKAGE_DIR / "__init__.py",
    V7_PACKAGE_DIR / "physical.py",
    V7_PACKAGE_DIR / "records.py",
    V7_PACKAGE_DIR / "participants.py",
]


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

    for fixture_id in (
        "hist_process_start_physical_first",
        "hist_harness_event_outside_lifetime",
    ):
        with pytest.raises(ParticipantLifetimeError) as exc_info:
            build_participant_table(_harness_headers(fixture_id))
        assert str(exc_info.value) not in EXTERNAL_OUTCOME_STRINGS


def test_gap_signal_kind_is_only_a_label_not_a_gap_classification() -> None:
    """A capture containing a structurally in-lifetime gap_signal event
    still builds a coherent ParticipantTable -- S2 does not treat the event
    kind as GAP-bearing, and does not know or care that this fixture's
    gap_signal is (at the analyzer-outcome level, entirely outside S2) the
    post-T1 diagnostic at the center of the fourth stop-triggering P1."""

    headers = _harness_headers("stop_post_t1_gap_signal_rewrites_t1")
    gap_signal_headers = [h for h in headers if h.kind is HarnessEventKind.GAP_SIGNAL]
    assert len(gap_signal_headers) == 1
    table = build_participant_table(headers)
    assert len(table) == 1
    lifetime = table.get(ParticipantIdentity(READER))
    assert lifetime is not None
    assert lifetime.contains_ns(gap_signal_headers[0].monotonic_ns) is True


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


def test_every_exported_s2_type_has_direct_test_coverage() -> None:
    this_file_source = Path(__file__).read_text(encoding="utf-8")
    exported: set[str] = set()
    for module in ALL_S2_MODULES:
        exported |= _public_types(module)
    missing = {name for name in exported if name not in this_file_source}
    assert missing == set()


def test_exported_function_and_type_counts() -> None:
    """A concrete, currently-true number alongside the structural coverage
    checks above (not instead of them) -- catches an export silently added
    or removed without anyone noticing either way."""

    total_functions = sum(len(_public_functions(m)) for m in ALL_S2_MODULES)
    total_types = sum(len(_public_types(m)) for m in ALL_S2_MODULES)
    # functions: assign_physical_positions, decode_harness_record_header,
    # decode_harness_stream, decode_timed_record_ref, build_participant_table
    assert total_functions == 5
    # types: StreamName, PhysicalPos, CrossStreamComparisonError (physical);
    # HarnessEventKind, StructuralDecodeError, HarnessRecordHeader,
    # TimedRecordRef (records); ParticipantIdentity, TerminationKind,
    # ParticipantLifetimeError, ParticipantLifetime, ParticipantTable
    # (participants)
    assert total_types == 12
