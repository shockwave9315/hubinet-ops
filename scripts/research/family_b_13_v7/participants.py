"""Participant identity, structural lifetime, and table (S2 foundation
layer -- the top of the S2 stack; STOP here, see the package docstring).

Builds a :class:`ParticipantTable` from typed
:class:`~scripts.research.family_b_13_v7.records.HarnessRecordHeader`
values alone. A participant identity is the exact sealed
``process_identity`` string carried by harness records -- never a role
inferred from naming convention (a value like ``"synthetic-reader:1"`` is
not interpreted as "this is the reader"; it is only ever the literal
identity string a lifecycle actually belongs to).

Ground truth, observer-stream records, UPIDs, phase/interval classification,
and T0/T1 play no part anywhere in this module.
:func:`build_participant_table` takes only harness records; it has no
``ground_truth``, ``observer_records``, ``manifest_context``, ``t0``,
``t1``, ``phase``, ``interval``, ``authority``, or ``ledger`` parameter, and
never will in this module.

Structural lifetime invariants enforced here (derived from the frozen v6
oracle's own harness-lifecycle validation --
``tests/oracles/family_b_13/v6/blocker_b_family_b_13_analyzer.py``, the
``_analyze_loaded`` harness-boundary block):

- exactly one ``process_start`` and exactly one ``process_stop`` per
  participant (frozen v6: ``kinds.count("process_start") != 1 or
  kinds.count("process_stop") != 1`` fails closed -- a missing or
  duplicated boundary is always rejected, never left optional);
- any ``process_crash`` record is an unconditional structural rejection,
  never an alternate valid terminal (frozen v6:
  ``if any(record["event"] == "process_crash" ...): raise
  CaptureError("harness_process_crash")`` -- unconditional, regardless of
  position or count). This resolves a design question with direct sourced
  evidence rather than a guess: :class:`TerminationKind` therefore has
  exactly one legal value;
- ``process_start`` must be physically first among that participant's
  records (physical order, never timestamp or ``harness_sequence`` order);
- ``process_stop`` must be physically last;
- every record's ``monotonic_ns`` must fall within
  ``[start_ns, end_ns]`` inclusive, and every record's physical position
  must fall within ``[start_pos, end_pos]`` inclusive.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Mapping, Sequence

from .physical import CrossStreamComparisonError, PhysicalPos
from .records import HarnessEventKind, HarnessRecordHeader, TimedRecordRef

__all__ = [
    "ParticipantIdentity",
    "TerminationKind",
    "ParticipantLifetimeError",
    "ParticipantLifetime",
    "ParticipantTable",
    "build_participant_table",
]


@dataclass(frozen=True)
class ParticipantIdentity:
    """The exact sealed ``process_identity`` string. Not a role -- see the
    module docstring."""

    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str) or not self.value:
            raise ValueError("ParticipantIdentity.value must be a nonempty string")


class TerminationKind(str, Enum):
    """The set of structurally valid participant terminal-lifecycle
    boundaries. Frozen v6 unconditionally rejects any capture containing a
    ``process_crash`` record (see the module docstring), so this has
    exactly one member -- it is not a design placeholder for a second one
    to be added casually later; a future terminal kind requires the same
    kind of direct sourced verification this one had."""

    PROCESS_STOP = "process_stop"


class ParticipantLifetimeError(ValueError):
    """A harness record set cannot be structurally assembled into a
    coherent :class:`ParticipantLifetime`. This is an S2-local structural-
    model error, never an external analyzer-outcome string."""


@dataclass(frozen=True)
class ParticipantLifetime:
    """One participant's structural process lifetime, as established
    solely by its own explicit sealed ``harness-events.jsonl`` lifecycle
    records.

    Exposes only pure structural-fact queries
    (:meth:`contains_ns`, :meth:`contains_record`) -- never an admission,
    authority, or verdict decision. "A timestamp lies outside this
    lifetime" is a fact this type can state; what that fact *means* is a
    later-stage (``admit()``, S3+) question this type never answers.
    """

    identity: ParticipantIdentity
    start_ns: int
    end_ns: int
    start_pos: PhysicalPos
    end_pos: PhysicalPos
    termination_kind: TerminationKind

    def contains_ns(self, monotonic_ns: int) -> bool:
        """Whether ``monotonic_ns`` falls within this lifetime's declared
        ``[start_ns, end_ns]`` interval. A pure numeric fact -- carries no
        opinion about phase, interval, or evidence admissibility."""

        return self.start_ns <= monotonic_ns <= self.end_ns

    def contains_record(self, ref: TimedRecordRef) -> bool:
        """Whether ``ref`` structurally falls within this lifetime -- both
        its timestamp AND its physical position, since a
        :class:`~scripts.research.family_b_13_v7.records.TimedRecordRef`
        carries no participant-identity/ownership binding of its own: a
        bare timestamp match is not evidence that ``ref`` belongs to (or
        was produced by) this lifetime's participant at all.

        Raises :class:`~scripts.research.family_b_13_v7.physical.CrossStreamComparisonError`
        if ``ref`` belongs to a different sealed stream than the one this
        lifetime's own boundaries were established in
        (``harness-events.jsonl``) -- checked *before* the timestamp bound,
        so the exception is raised whether or not ``ref``'s timestamp
        happens to fall inside ``[start_ns, end_ns]``. A cross-stream
        record's physical position is simply not comparable to this
        lifetime's ``start_pos``/``end_pos`` (see
        ``scripts.research.family_b_13_v7.physical``), so this method
        cannot honestly answer "does it structurally fall within this
        lifetime" for it at all -- it must fail closed rather than
        approximate that answer from the timestamp alone. Callers that only
        have (and only need) a cross-stream timestamp fact must use
        :meth:`contains_ns` directly, exactly as the
        ``stop_reader_pre_t0_before_process_start`` historical witness
        does.
        """

        if self.start_pos.stream is not ref.pos.stream:
            raise CrossStreamComparisonError(
                f"{ref.pos.stream!r} is not comparable to this lifetime's "
                f"{self.start_pos.stream!r} boundaries"
            )
        if not self.contains_ns(ref.monotonic_ns):
            return False
        return not ref.pos.precedes(self.start_pos) and not self.end_pos.precedes(ref.pos)


@dataclass(frozen=True)
class ParticipantTable:
    """An immutable collection of every participant's
    :class:`ParticipantLifetime`, keyed by :class:`ParticipantIdentity`.

    Preferably constructed by :func:`build_participant_table`, but
    ``__post_init__`` defends the immutability/type invariants for *any*
    construction path: the incoming mapping is copied (never aliased back
    to caller-held state -- mutating the caller's original dict/mapping
    after construction cannot change this table), every key must be a
    :class:`ParticipantIdentity`, every value a :class:`ParticipantLifetime`,
    and every key must equal that lifetime's own ``identity``.
    """

    _by_identity: Mapping[ParticipantIdentity, ParticipantLifetime]

    def __post_init__(self) -> None:
        if not isinstance(self._by_identity, Mapping):
            raise TypeError("ParticipantTable requires a mapping")
        copied: dict[ParticipantIdentity, ParticipantLifetime] = {}
        for key, lifetime in self._by_identity.items():
            if not isinstance(key, ParticipantIdentity):
                raise TypeError("ParticipantTable keys must be ParticipantIdentity")
            if not isinstance(lifetime, ParticipantLifetime):
                raise TypeError("ParticipantTable values must be ParticipantLifetime")
            if lifetime.identity != key:
                raise ValueError("ParticipantTable key must equal lifetime.identity")
            copied[key] = lifetime
        object.__setattr__(self, "_by_identity", MappingProxyType(copied))

    def get(self, identity: ParticipantIdentity) -> ParticipantLifetime | None:
        return self._by_identity.get(identity)

    def __contains__(self, identity: ParticipantIdentity) -> bool:
        return identity in self._by_identity

    def __iter__(self):
        return iter(self._by_identity.values())

    def __len__(self) -> int:
        return len(self._by_identity)


def _group_by_identity(
    harness_records: Sequence[HarnessRecordHeader],
) -> dict[ParticipantIdentity, list[HarnessRecordHeader]]:
    groups: dict[ParticipantIdentity, list[HarnessRecordHeader]] = {}
    for record in harness_records:
        identity = ParticipantIdentity(record.process_identity)
        groups.setdefault(identity, []).append(record)
    return groups


def _build_one_lifetime(
    identity: ParticipantIdentity, records: list[HarnessRecordHeader]
) -> ParticipantLifetime:
    crash_count = sum(1 for r in records if r.kind is HarnessEventKind.PROCESS_CRASH)
    if crash_count > 0:
        raise ParticipantLifetimeError("participant_process_crash_present")

    start_records = [r for r in records if r.kind is HarnessEventKind.PROCESS_START]
    if len(start_records) != 1:
        raise ParticipantLifetimeError("participant_process_start_missing_or_duplicate")
    start_record = start_records[0]

    stop_records = [r for r in records if r.kind is HarnessEventKind.PROCESS_STOP]
    if len(stop_records) != 1:
        raise ParticipantLifetimeError("participant_process_stop_missing_or_duplicate")
    stop_record = stop_records[0]

    first_ordinal = min(r.pos.ordinal for r in records)
    last_ordinal = max(r.pos.ordinal for r in records)
    if start_record.pos.ordinal != first_ordinal:
        raise ParticipantLifetimeError("participant_process_start_not_physically_first")
    if stop_record.pos.ordinal != last_ordinal:
        raise ParticipantLifetimeError("participant_process_stop_not_physically_last")

    start_ns = start_record.monotonic_ns
    end_ns = stop_record.monotonic_ns
    if start_ns > end_ns:
        raise ParticipantLifetimeError("participant_lifetime_interval_inverted")

    for record in records:
        if not (start_ns <= record.monotonic_ns <= end_ns):
            raise ParticipantLifetimeError("participant_record_timestamp_outside_lifetime")
        if not (first_ordinal <= record.pos.ordinal <= last_ordinal):
            raise ParticipantLifetimeError(
                "participant_record_outside_lifetime_physical_position"
            )

    return ParticipantLifetime(
        identity=identity,
        start_ns=start_ns,
        end_ns=end_ns,
        start_pos=start_record.pos,
        end_pos=stop_record.pos,
        termination_kind=TerminationKind.PROCESS_STOP,
    )


def build_participant_table(
    harness_records: Sequence[HarnessRecordHeader],
) -> ParticipantTable:
    """Build a :class:`ParticipantTable` solely from typed harness
    lifecycle records -- one :class:`ParticipantLifetime` per distinct
    ``process_identity`` found in ``harness_records``.

    Deliberately takes no other parameter. If building a correct table ever
    seems to require ground truth, observer records, a manifest, T0/T1, or
    a phase/interval, that is a sign this layering is wrong -- it must not
    be added here (see the package/module docstrings' kill-switch note).
    """

    groups = _group_by_identity(harness_records)
    lifetimes = {
        identity: _build_one_lifetime(identity, records)
        for identity, records in groups.items()
    }
    return ParticipantTable(_by_identity=MappingProxyType(lifetimes))
