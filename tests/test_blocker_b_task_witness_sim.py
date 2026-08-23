"""Focused tests for the research-only Blocker-B sentinel model."""

from dataclasses import replace

import pytest

from scripts.research.blocker_b_task_witness_sim import (
    Coverage,
    CoverageEnvelope,
    Event,
    EventKind,
    evaluate,
)


PROVEN = CoverageEnvelope(
    retention_prefix_proven=True,
    pagination_snapshot_proven=True,
    authorization_interval_proven=True,
    all_nodes_covered=True,
    active_archive_handoff_proven=True,
)


def test_fully_proven_envelope_and_overlap_can_model_complete_coverage() -> None:
    events = [
        Event(EventKind.SEED, upid="S"),
        Event(EventKind.RESTART),
        Event(EventKind.ACTIVE, upid="T"),
        Event(EventKind.FINISHED, upid="T"),
        Event(EventKind.ARCHIVE_TRAVERSAL_START),
        Event(EventKind.ARCHIVE_PAGE, upids=("T", "S"), final_page=True),
    ]

    assert evaluate(events, envelope=PROVEN) is Coverage.COMPLETE


@pytest.mark.parametrize(
    "archive_events",
    [
        (
            Event(EventKind.ARCHIVE_TRAVERSAL_START),
            Event(
                EventKind.ARCHIVE_PAGE,
                upids=("new", "S"),
                final_page=False,
            ),
            Event(EventKind.ARCHIVE_PAGE, upids=("older",), final_page=True),
        ),
        (
            Event(EventKind.ARCHIVE_TRAVERSAL_START),
            Event(EventKind.ARCHIVE_PAGE, upids=("new", "S"), final_page=True),
        ),
        (
            Event(EventKind.ARCHIVE_TRAVERSAL_START),
            Event(EventKind.ARCHIVE_PAGE, upids=("S",), final_page=False),
            Event(EventKind.ARCHIVE_TRAVERSAL_START),
            Event(EventKind.ARCHIVE_PAGE, upids=("S",), final_page=True),
        ),
    ],
    ids=(
        "sentinel-before-final-page",
        "sentinel-on-final-page",
        "abandoned-then-new-successful-traversal",
    ),
)
def test_valid_archive_traversals_can_complete(
    archive_events: tuple[Event, ...],
) -> None:
    events = [
        Event(EventKind.SEED, upid="S"),
        Event(EventKind.RESTART),
        *archive_events,
    ]

    assert evaluate(events, envelope=PROVEN) is Coverage.COMPLETE


def test_final_page_without_overlap_latches_gap_across_later_traversal() -> None:
    events = [
        Event(EventKind.SEED, upid="S"),
        Event(EventKind.RESTART),
        Event(EventKind.ARCHIVE_TRAVERSAL_START),
        Event(EventKind.ARCHIVE_PAGE, upids=("new",), final_page=True),
        Event(EventKind.ARCHIVE_TRAVERSAL_START),
        Event(EventKind.ARCHIVE_PAGE, upids=("S",), final_page=False),
    ]

    assert evaluate(events, envelope=PROVEN) is Coverage.GAP


def test_abandoned_traversal_overlap_cannot_satisfy_new_traversal() -> None:
    events = [
        Event(EventKind.SEED, upid="S"),
        Event(EventKind.RESTART),
        Event(EventKind.ARCHIVE_TRAVERSAL_START),
        Event(EventKind.ARCHIVE_PAGE, upids=("S",), final_page=False),
        Event(EventKind.ARCHIVE_TRAVERSAL_START),
        Event(EventKind.ARCHIVE_PAGE, upids=("new",), final_page=True),
    ]

    assert evaluate(events, envelope=PROVEN) is Coverage.GAP


def test_archive_page_without_explicit_traversal_start_is_a_gap() -> None:
    events = [
        Event(EventKind.SEED, upid="S"),
        Event(EventKind.RESTART),
        Event(EventKind.ARCHIVE_PAGE, upids=("S",), final_page=True),
    ]

    assert evaluate(events, envelope=PROVEN) is Coverage.GAP


@pytest.mark.parametrize(
    "field",
    [
        "retention_prefix_proven",
        "pagination_snapshot_proven",
        "authorization_interval_proven",
        "all_nodes_covered",
        "active_archive_handoff_proven",
    ],
)
def test_every_envelope_property_is_load_bearing(field: str) -> None:
    events = [
        Event(EventKind.SEED, upid="S"),
        Event(EventKind.RESTART),
        Event(EventKind.ARCHIVE_TRAVERSAL_START),
        Event(EventKind.ARCHIVE_PAGE, upids=("S",), final_page=True),
    ]

    assert evaluate(events, envelope=replace(PROVEN, **{field: False})) is Coverage.GAP


@pytest.mark.parametrize(
    "hazard",
    [
        EventKind.API_FAILURE,
        EventKind.NODE_DOWN,
        EventKind.ACL_VISIBILITY_LOST,
        EventKind.TASK_UNKNOWN,
    ],
)
def test_known_uncertainty_is_sticky_even_after_later_overlap(hazard: EventKind) -> None:
    events = [
        Event(EventKind.SEED, upid="S"),
        Event(hazard),
        Event(EventKind.ACL_VISIBILITY_RESTORED),
        Event(EventKind.ARCHIVE_TRAVERSAL_START),
        Event(EventKind.ARCHIVE_PAGE, upids=("S",), final_page=True),
    ]

    assert evaluate(events, envelope=PROVEN) is Coverage.GAP


def test_rotation_without_sentinel_overlap_is_a_gap() -> None:
    events = [
        Event(EventKind.SEED, upid="S"),
        Event(EventKind.ROTATE),
        Event(EventKind.ARCHIVE_TRAVERSAL_START),
        Event(EventKind.ARCHIVE_PAGE, upids=("new",), final_page=True),
    ]

    assert evaluate(events, envelope=PROVEN) is Coverage.GAP


def test_default_envelope_never_claims_pve_properties_are_proven() -> None:
    events = [Event(EventKind.SEED, upid="S")]

    assert evaluate(events) is Coverage.GAP
