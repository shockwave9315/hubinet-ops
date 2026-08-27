"""Physical stream position (S2 foundation layer).

Represents ONLY physical record order inside one sealed JSONL stream of a
``family-b-13-capture-v6`` capture. Nothing else lives here: no timestamp,
no ``harness_sequence``, no ``generator_sequence``, no phase, no interval,
no authority state.

``PhysicalPos`` MUST be derived from actual iteration order of the sealed
stream (its physical line/record order as decoded), and MUST NOT be derived
from any declared field -- not ``harness_sequence``, not
``generator_sequence``, not ``heartbeat_sequence``, not ``request_start``/
``request_end``, not ``monotonic_ns``, not wall-clock time. This is a
load-bearing contract: it is what lets S2 (and later stages) tell a
physically-relabeled or physically-reordered sealed stream apart from a
merely re-timestamped or re-sequenced one, independent of the value of any
field a stream's own participants get to declare.

Two positions from different streams are never globally ordered -- there is
no meaningful single physical order across e.g. ``ground-truth.jsonl`` and
``harness-events.jsonl``. ``PhysicalPos`` therefore deliberately has no
``__lt__``; only :meth:`PhysicalPos.precedes`, which fails closed
(``CrossStreamComparisonError``) rather than silently returning an ordering
when the two positions belong to different streams.

Raw-record snapshot boundary (S2 final finite corrective, local
stop-patching rule)
---------------------------------------------------------------------------
An independent adversarial review found the original ``assign_physical_positions``
helper's public contract untruthful: its docstring claimed positions were
"derived from actual iteration order," but the implementation only read
``len(records)``. A caller (``records.decode_harness_stream``) that used
this helper for position *count* and then separately re-iterated its own
``records`` argument for position *content* was therefore making two
independent observations of one external caller-supplied object. For a
``Sequence``-conforming object whose ``__len__`` and ``__iter__`` (or
successive full iterations) described different histories, that let
position count and record content silently desynchronize -- exactly the
same derivation-boundary defect
:func:`~scripts.research.family_b_13_v7.participants.build_participant_table`
was already corrected for on typed records (see that function's own
docstring). ``assign_physical_positions`` was removed outright (local
stop-patching rule: no replacement under another name that keeps the same
untruthful contract) and replaced by :func:`snapshot_physical_stream` /
:class:`PhysicalStreamSnapshot` below, which make the *same* single-
observation snapshot guarantee the load-bearing rule for every external-
Sequence derivation boundary in this package: a caller-supplied ``Sequence``
is traversed EXACTLY ONCE, and every provenance-bearing fact derived from it
(here, physical position) is paired with the content from that SAME
traversal -- never a second, independent observation of the original
object.

Snapshot content ownership (R1/R2 corrective pass, local stop-patching rule)
---------------------------------------------------------------------------
A subsequent compliance audit found the single-traversal guarantee above
incomplete: ``tuple(records)`` freezes the snapshot's own *arity and order*,
but each element it held remained the SAME mutable dict/list object the
caller still held a reference to. A caller mutating its own original record
mapping (or a nested mapping/list inside it) after
:func:`snapshot_physical_stream` returned could therefore silently change
what the already-returned :class:`PhysicalStreamSnapshot` represents --
exactly the ownership violation "immutable snapshot" was meant to rule out,
even though no second *observation* of the caller's ``Sequence`` itself ever
occurred. :func:`snapshot_physical_stream` now additionally requires each
top-level record to be a plain ``dict`` and every nested value to belong to
the exact decoded-JSON family S1 produces: plain ``dict``/``list``
containers plus ``str``/``int``/``float``/``bool``/``None`` scalars. Each
accepted record is recursively converted into an immutable equivalent
(:func:`_freeze_record_value`) as part of that SAME single traversal -- a
``dict`` becomes a fresh, independent
:class:`types.MappingProxyType` (never a view over the caller's own dict),
a ``list`` becomes a fresh ``tuple``, accepted scalars pass through
unchanged, and every Mapping/Sequence substitute or opaque/custom object is
rejected. This changes container *identity* only, never scalar type or value
-- S1 remains the sole scalar-syntax authority. Once content crosses this
boundary, no caller-owned mutable object remains authoritative for it. This
guarantee is
:func:`snapshot_physical_stream`'s alone: :class:`PhysicalStreamSnapshot`'s
own ``__post_init__`` continues to prove only local structural
well-formedness (types, lengths, sequential ordinals) for *any* construction
path -- the same well-formed-vs-established distinction already documented
on that class below -- so a test exercising that type's own invariants by
direct construction may still pass a live, unfrozen dict, exactly as it
could before this pass.
"""

from __future__ import annotations

import collections.abc
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Sequence

__all__ = [
    "StreamName",
    "PhysicalPos",
    "CrossStreamComparisonError",
    "PhysicalStreamSnapshot",
    "snapshot_physical_stream",
]


class StreamName(str, Enum):
    """The closed set of sealed JSONL physical record streams a
    ``family-b-13-capture-v6`` capture declares (``CAPTURE_FILES`` in the
    frozen v6 oracle). ``manifest.json`` and ``seal.json`` are not physical
    record streams under this model and have no member here."""

    GROUND_TRUTH = "ground_truth"
    PRE_T0_ESTABLISHMENT = "pre_t0_establishment"
    WATCH_EVENTS = "watch_events"
    WATCH_LIFECYCLE = "watch_lifecycle"
    SCAN_ROUNDS = "scan_rounds"
    SURFACE_OBSERVATIONS = "surface_observations"
    API_PAGES = "api_pages"
    EXACT_UPID = "exact_upid"
    HARNESS_EVENTS = "harness_events"


class CrossStreamComparisonError(ValueError):
    """Raised by :meth:`PhysicalPos.precedes` when asked to order two
    positions from different streams. There is no meaningful answer, so
    this fails explicitly rather than returning an arbitrary result."""


@dataclass(frozen=True)
class PhysicalPos:
    """One record's physical position within one sealed stream.

    ``ordinal`` is the record's 1-based position in that stream's actual
    physical iteration order -- record 1 is the first physical line/record
    decoded, record 2 the second, and so on. It carries no other meaning.
    """

    stream: StreamName
    ordinal: int

    def __post_init__(self) -> None:
        if not isinstance(self.stream, StreamName):
            raise TypeError("PhysicalPos.stream must be a StreamName")
        if not isinstance(self.ordinal, int) or isinstance(self.ordinal, bool):
            raise TypeError("PhysicalPos.ordinal must be an int")
        if self.ordinal < 1:
            raise ValueError("PhysicalPos.ordinal must be >= 1")

    def precedes(self, other: "PhysicalPos") -> bool:
        """Whether this position is physically before ``other`` in the
        same stream. Raises :class:`TypeError` if ``other`` is not itself a
        real :class:`PhysicalPos` -- checked before any field access, so a
        duck-typed object exposing ``stream``/``ordinal`` attributes can
        never manufacture a positive physical-order fact by impersonating
        one. Raises :class:`CrossStreamComparisonError` if the two positions
        belong to different streams -- there is deliberately no silent
        cross-stream ordering."""

        if not isinstance(other, PhysicalPos):
            raise TypeError("PhysicalPos.precedes requires another PhysicalPos")
        if self.stream is not other.stream:
            raise CrossStreamComparisonError(
                f"positions in {self.stream!r} and {other.stream!r} are not comparable"
            )
        return self.ordinal < other.ordinal


@dataclass(frozen=True)
class PhysicalStreamSnapshot:
    """One immutable, single-observation snapshot of an external record
    stream: the exact records observed, paired 1:1, in order, with the
    :class:`PhysicalPos` values derived from that SAME observation.

    This is the sole mechanism by which S2 derives physical positions for
    an external caller-supplied ``Sequence`` and still safely pairs those
    positions with the records' own content: both come from exactly one
    traversal of the caller's object, performed once by
    :func:`snapshot_physical_stream` and never repeated. A consumer that
    needs "record + its PhysicalPos" together must use
    ``snapshot.records``/``snapshot.positions`` from an already-built
    :class:`PhysicalStreamSnapshot` -- it must never separately re-observe
    (re-iterate, re-index, or take a fresh ``len()`` of) the original
    external ``Sequence`` its own caller supplied. A ``Sequence``-conforming
    object whose ``__len__`` and ``__iter__`` (or successive full
    iterations) disagree could otherwise let position *count* and position
    *content* be derived from two different histories -- silently dropping
    or shuffling sealed evidence a downstream validator never sees.

    When built by :func:`snapshot_physical_stream` (the normal path),
    ``records``' own elements are additionally content-immutable -- see the
    module docstring's "Snapshot content ownership" section -- so mutating
    the caller's original mapping/list after the call returns cannot change
    what this snapshot represents, closing the same ownership boundary for
    record *content* that this type already closed for record *arity and
    order*.

    Preferably constructed only by :func:`snapshot_physical_stream` (see the
    package-wide architecture gate enforcing this in the S2 test file), but
    ``__post_init__`` defends this type's own internal consistency for
    *any* construction path: ``records``/``positions`` must both be real
    tuples of exactly equal length; every position must belong to
    ``stream``; and position ``i`` (1-based) must have ``ordinal == i``,
    sequential and gapless, matching its paired record's index. This proves
    only that the value is internally self-consistent, never that
    ``records`` is the genuine result of one real external observation, and
    never that its elements are content-immutable -- both are
    :func:`snapshot_physical_stream`'s job alone (the same
    well-formed-constructor-vs-established-derivation distinction as
    :class:`~scripts.research.family_b_13_v7.participants.ParticipantLifetime`);
    a direct construction may still pass a live, caller-owned dict, exactly
    as before this pass.
    """

    stream: StreamName
    records: tuple[object, ...]
    positions: tuple[PhysicalPos, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.stream, StreamName):
            raise TypeError("PhysicalStreamSnapshot.stream must be a StreamName")
        if not isinstance(self.records, tuple):
            raise TypeError("PhysicalStreamSnapshot.records must be a tuple")
        if not isinstance(self.positions, tuple):
            raise TypeError("PhysicalStreamSnapshot.positions must be a tuple")
        if len(self.records) != len(self.positions):
            raise ValueError(
                "PhysicalStreamSnapshot.records and .positions must have equal length"
            )
        for index, pos in enumerate(self.positions, start=1):
            if not isinstance(pos, PhysicalPos):
                raise TypeError("PhysicalStreamSnapshot.positions must all be PhysicalPos")
            if pos.stream is not self.stream:
                raise ValueError("PhysicalStreamSnapshot.positions must all belong to .stream")
            if pos.ordinal != index:
                raise ValueError(
                    "PhysicalStreamSnapshot.positions must be sequential ordinals"
                    " starting at 1, matching their paired record's index"
                )


def _freeze_record_value(value: object) -> object:
    """Validate and freeze one exact S1-decoded JSON value.

    Recursively convert one accepted value into an immutable equivalent,
    so that later mutation of a caller-owned mutable container
    cannot change what a :class:`PhysicalStreamSnapshot` already captured
    (see the module docstring's "Snapshot content ownership" section).

    Only the exact container/scalar family produced by S1's accepted
    ``json.loads`` path is admitted: plain ``dict``/``list`` containers,
    exact ``str``/``int``/``float``/``bool`` scalars, and ``None``. A
    ``dict`` becomes a fresh :class:`types.MappingProxyType` wrapping a
    fresh ``dict`` of recursively-frozen values -- never a proxy VIEW over
    the caller's own dict object, which would still silently reflect the
    caller's later mutation of it. A ``list`` becomes a ``tuple`` of
    recursively-frozen elements. Mapping/Sequence substitutes, subclasses,
    tuples, and opaque/custom objects are rejected rather than retained by
    alias. Accepted scalar syntax and values are not reinterpreted here, so
    S1 remains their sole syntax authority."""

    if type(value) is dict:
        frozen: dict[str, object] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise TypeError("raw JSON object keys must be plain str values")
            frozen[key] = _freeze_record_value(item)
        return MappingProxyType(frozen)
    if type(value) is list:
        return tuple(_freeze_record_value(item) for item in value)
    if value is None or type(value) in (str, int, float, bool):
        return value
    raise TypeError("raw record value is outside the exact decoded-JSON domain")


def snapshot_physical_stream(
    stream: StreamName, records: Sequence[dict[str, object]]
) -> PhysicalStreamSnapshot:
    """The sole point where an external caller-supplied ``Sequence`` of
    sealed records is observed to derive :class:`PhysicalPos` values.

    Validates ``stream`` is a real :class:`StreamName` and ``records`` is a
    real :class:`collections.abc.Sequence` (never a one-shot iterator/
    generator, and never an arbitrary ``Sized``+``Iterable`` container that
    only happens to define ``__len__``/``__iter__`` without actually being a
    ``Sequence``), then traverses ``records`` EXACTLY ONCE -- via one
    generator expression consumed by ``tuple(...)``, one call to
    ``iter(records)`` -- into an immutable snapshot. Each element observed
    during that same traversal is additionally passed through
    :func:`_freeze_record_value`, after requiring that each observed
    top-level value is a plain ``dict``, so the snapshot's own records are
    content-immutable, not merely arity/order-immutable: this is still
    exactly one observation of the caller's top-level ``Sequence``; only
    each element's OWN nested structure is (independently, recursively)
    copied into an immutable equivalent as part of materializing that one
    observation, not a second observation of ``records`` itself. Every
    :class:`PhysicalPos`
    this function returns is derived from ``len()`` of THAT SAME snapshot
    tuple, never from the original ``records`` argument again, so the
    returned :class:`PhysicalStreamSnapshot`'s ``records``/``positions`` are
    truthfully paired: position ``i`` really does describe
    ``snapshot.records[i - 1]``'s physical order and content in the one
    observation that was actually made.

    A caller that needs records paired with their physical positions (e.g.
    :func:`~scripts.research.family_b_13_v7.records.decode_harness_stream`)
    MUST consume ``.records``/``.positions`` off the returned snapshot --
    never re-traverse, re-index, or take a fresh ``len()`` of the original
    ``records`` argument after calling this function. Doing so would
    reintroduce exactly the double-observation defect this function exists
    to close (a Sequence whose ``__len__`` and ``__iter__`` -- or
    successive full iterations -- describe different histories could then
    silently desynchronize position count from record content).

    ``stream`` is checked before ``records`` is touched at all -- including
    when ``records`` is empty -- so an invalid stream can never silently
    pass through merely because there was nothing to snapshot.
    """

    if not isinstance(stream, StreamName):
        raise TypeError("snapshot_physical_stream requires a StreamName")
    if not isinstance(records, collections.abc.Sequence):
        raise TypeError("snapshot_physical_stream requires a Sequence")

    # Exactly one traversal of `records` (one `iter(records)` call, fully
    # consumed): freezing each element's own nested content happens inline,
    # not as a separate pass over `records` itself.
    def freeze_record(record: object) -> object:
        if type(record) is not dict:
            raise TypeError("snapshot_physical_stream requires plain dict records")
        return _freeze_record_value(record)

    snapshot_records: tuple[object, ...] = tuple(
        freeze_record(record) for record in records
    )
    positions = tuple(
        PhysicalPos(stream=stream, ordinal=index)
        for index in range(1, len(snapshot_records) + 1)
    )
    return PhysicalStreamSnapshot(stream=stream, records=snapshot_records, positions=positions)
