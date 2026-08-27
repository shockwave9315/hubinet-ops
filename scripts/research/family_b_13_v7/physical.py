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
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Sequence

__all__ = [
    "StreamName",
    "PhysicalPos",
    "CrossStreamComparisonError",
    "assign_physical_positions",
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


def assign_physical_positions(
    stream: StreamName, records: Sequence[object]
) -> list[PhysicalPos]:
    """Assign one :class:`PhysicalPos` per record, in exactly the physical
    order ``records`` is already in (its list/iteration order -- the order
    the sealed JSONL stream was decoded in). This function looks at each
    record's *position in the sequence* only; it never inspects the record's
    own content, so nothing a record declares about itself (a sequence
    number, a timestamp) can influence the assignment.

    ``stream`` must be a real :class:`StreamName`, checked before anything
    else -- including when ``records`` is empty, so an invalid stream can
    never silently pass through merely because there was nothing to assign
    positions to.
    """

    if not isinstance(stream, StreamName):
        raise TypeError("assign_physical_positions requires a StreamName")
    return [PhysicalPos(stream=stream, ordinal=index) for index in range(1, len(records) + 1)]
