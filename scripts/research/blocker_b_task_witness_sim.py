"""Research-only model of a fail-closed PVE task-coverage sentinel.

This module is not production code and must not contact Proxmox or any other
service.  It models the *logical* conditions a future Family-B protocol would
need.  It deliberately does not claim that stock PVE supplies those conditions.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable


class Coverage(str, Enum):
    COMPLETE = "COVERAGE_COMPLETE"
    GAP = "COVERAGE_GAP"


class EventKind(str, Enum):
    SEED = "SEED"
    ACTIVE = "ACTIVE"
    FINISHED = "FINISHED"
    ARCHIVE_TRAVERSAL_START = "ARCHIVE_TRAVERSAL_START"
    ARCHIVE_PAGE = "ARCHIVE_PAGE"
    ROTATE = "ROTATE"
    RESTART = "RESTART"
    API_FAILURE = "API_FAILURE"
    NODE_DOWN = "NODE_DOWN"
    ACL_VISIBILITY_LOST = "ACL_VISIBILITY_LOST"
    ACL_VISIBILITY_RESTORED = "ACL_VISIBILITY_RESTORED"
    TASK_UNKNOWN = "TASK_UNKNOWN"


@dataclass(frozen=True)
class CoverageEnvelope:
    """Properties that must be independently proven, never inferred here."""

    retention_prefix_proven: bool = False
    pagination_snapshot_proven: bool = False
    authorization_interval_proven: bool = False
    all_nodes_covered: bool = False
    active_archive_handoff_proven: bool = False

    @property
    def complete(self) -> bool:
        return all(
            (
                self.retention_prefix_proven,
                self.pagination_snapshot_proven,
                self.authorization_interval_proven,
                self.all_nodes_covered,
                self.active_archive_handoff_proven,
            )
        )


@dataclass(frozen=True)
class Event:
    kind: EventKind
    upid: str | None = None
    upids: tuple[str, ...] = ()
    final_page: bool = False


def evaluate(
    events: Iterable[Event],
    *,
    envelope: CoverageEnvelope = CoverageEnvelope(),
) -> Coverage:
    """Evaluate one chronological fixture sequence.

    SEED is initialization-only and must be the first event.  A known gap is
    sticky.  Every explicitly started archive traversal requires a fresh overlap
    with the persisted sentinel.  A traversal can close that requirement only
    when it reaches its final page after observing the sentinel in that same
    traversal and the caller supplies an independently proven envelope.
    """

    sentinel: str | None = None
    overlap_required = True
    traversal_active = False
    traversal_overlap_seen = False
    gap_latched = False
    stream_started = False
    visible = True

    for event in events:
        if not stream_started:
            stream_started = True
            if event.kind is EventKind.SEED and event.upid is not None:
                sentinel = event.upid
                overlap_required = False
                continue
            gap_latched = True

        if event.kind is EventKind.SEED:
            gap_latched = True
            continue

        if event.kind in {
            EventKind.API_FAILURE,
            EventKind.NODE_DOWN,
            EventKind.ACL_VISIBILITY_LOST,
            EventKind.TASK_UNKNOWN,
        }:
            gap_latched = True
            if event.kind is EventKind.ACL_VISIBILITY_LOST:
                visible = False
            continue

        if event.kind is EventKind.ACL_VISIBILITY_RESTORED:
            visible = True
            # Restoration cannot prove what was hidden during the interval.
            continue

        if event.kind in {EventKind.RESTART, EventKind.ROTATE}:
            overlap_required = True
            traversal_active = False
            traversal_overlap_seen = False
            continue

        if event.kind is EventKind.ARCHIVE_TRAVERSAL_START:
            overlap_required = True
            traversal_active = True
            traversal_overlap_seen = False
            continue

        if event.kind is EventKind.ARCHIVE_PAGE:
            if not traversal_active:
                gap_latched = True
                continue
            if not visible or sentinel is None:
                gap_latched = True
                if event.final_page:
                    traversal_active = False
                    traversal_overlap_seen = False
                continue
            traversal_overlap_seen = (
                traversal_overlap_seen or sentinel in event.upids
            )
            if event.final_page:
                if overlap_required:
                    if traversal_overlap_seen and envelope.complete:
                        overlap_required = False
                    elif not traversal_overlap_seen:
                        gap_latched = True
                traversal_active = False
                traversal_overlap_seen = False
            continue

        if event.kind in {EventKind.ACTIVE, EventKind.FINISHED} and event.upid is None:
            gap_latched = True

    if (
        gap_latched
        or sentinel is None
        or overlap_required
        or not envelope.complete
        or not visible
    ):
        return Coverage.GAP
    return Coverage.COMPLETE


__all__ = [
    "Coverage",
    "CoverageEnvelope",
    "Event",
    "EventKind",
    "evaluate",
]
