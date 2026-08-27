"""Participant identity, structural lifetime, and table (S2 foundation
layer -- the top of the S2 stack; STOP here, see the package docstring).

Builds a :class:`ParticipantTable` from typed
:class:`~scripts.research.family_b_13_v7.records.HarnessRecordHeader`
values alone. A participant identity is the exact sealed
``process_identity`` string carried by harness records -- never a role
inferred from naming convention (a value like ``"synthetic-reader:1"`` is
not interpreted as "this is the reader"; it is only ever the literal
identity string a lifecycle actually belongs to).

Frozen capture-v6 harness-stream contract: single identity (load-bearing)
---------------------------------------------------------------------------
The byte-frozen v6 oracle
(``tests/oracles/family_b_13/v6/blocker_b_family_b_13_analyzer.py``, its
harness-lifecycle validation block) requires every
``harness-events.jsonl`` record's ``process_identity`` to equal one single
``manifest.reader_context.process_identity`` value -- any disagreement is
an unconditional ``harness_reader_process_identity_mismatch`` rejection. A
full ``harness-events.jsonl`` stream therefore describes exactly **one**
participant identity, never an arbitrary set of independent identities.
Every one of the 29 checked-in S0 sealed captures independently agrees
(see ``test_all_captured_harness_streams_have_a_singleton_process_identity``
in the S2 test file).

S2 has no ``manifest`` and therefore never attempts to prove that the
singleton identity it observes structurally equals
``manifest.reader_context.process_identity`` itself -- that binding is
intentionally outside S2 (see this module's own kill-switch note below).
What S2 *can* and *must* prove, from the sealed harness records alone, is
the structural shape of the frozen contract: the stream is nonempty, every
record shares the exact same ``process_identity``, and
:func:`build_participant_table` therefore builds exactly one
:class:`ParticipantLifetime`. A physically coherent stream containing two
different ``process_identity`` values is not a valid capture-v6 harness
history; it is a structural rejection, never grouped into two independent
lifecycles the way an unbounded multi-participant model would.

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

- the full harness stream is nonempty and carries exactly one
  ``process_identity`` value (frozen v6's structural analogue of requiring
  every record's ``process_identity`` to equal the single
  ``reader_context.process_identity``, without S2 consulting that
  manifest value itself -- see above);
- exactly one ``process_start`` and exactly one ``process_stop`` for that
  identity (frozen v6: ``kinds.count("process_start") != 1 or
  kinds.count("process_stop") != 1`` fails closed -- a missing or
  duplicated boundary is always rejected, never left optional);
- any ``process_crash`` record is an unconditional structural rejection,
  never an alternate valid terminal (frozen v6:
  ``if any(record["event"] == "process_crash" ...): raise
  CaptureError("harness_process_crash")`` -- unconditional, regardless of
  position or count). This resolves a design question with direct sourced
  evidence rather than a guess: :class:`TerminationKind` therefore has
  exactly one legal value;
- ``process_start`` must be physically first in the stream (physical order,
  never timestamp or ``harness_sequence`` order);
- ``process_stop`` must be physically last.

S2 final boundary corrective pass -- no timestamp-order relation anywhere
in S2 (load-bearing correction)
---------------------------------------------------------------------------
An independent adversarial review of the clock-domain boundary (the third
S2 corrective pass, above) found its own remaining justification for
comparing timestamps still overreached. That pass deleted
``ParticipantLifetime.contains_ns`` (a *cross-stream* relation) but kept
comparing a single harness stream's own records' ``monotonic_ns`` values
against each other inside :func:`build_participant_table`, reasoning that
"every record inside one ``harness-events.jsonl`` stream is emitted by the
same single writer process ... so ordering those records' own
``monotonic_ns`` values against each other needs no external clock-domain
proof -- there is exactly one writer, one clock." That chain --
singleton ``process_identity`` implies one writer implies one clock implies
timestamp comparison is safe -- is exactly the warrant this final pass
found unproven and retracts. A self-declared ``process_identity`` string is
structural equality only; it is not the frozen v6 oracle's
``manifest.clock_contract`` (one bound ``CLOCK_MONOTONIC`` domain, proven
before any monotonic relation is trusted -- see
``_validate_clock_contract`` in the frozen oracle), and S2 has no manifest
to derive that proof from, singleton identity or not.

Consequently S2 now performs NO timestamp-order relation of any kind,
cross-stream or within one harness stream: not ``start_ns <= end_ns``, not
``start_ns <= event.monotonic_ns <= end_ns``, nothing equivalent. Every
``monotonic_ns``/``start_ns``/``end_ns`` value remains only an individually
S1-validated scalar fact -- never compared against another. Physical
lifecycle boundaries (process_start physically first, process_stop
physically last, and :class:`ParticipantLifetime`'s own
``start_pos``/``end_pos`` invariants) are unaffected: they are derived from
sealed physical stream order, never from a timestamp, so no clock-domain
proof is needed to establish them. See ``build_participant_table`` and
``ParticipantLifetime`` below for exactly what is (and is not) checked now,
and the redesign checkpoint doc (S2, final boundary corrective pass) for
the full record, including the historical witnesses this reclassifies as
UNRESOLVED AT S2 CLOCK-RELATION LEVEL rather than a structural rejection.

This same pass also removed ``records.TimedRecordRef`` and
``records.decode_timed_record_ref`` outright (local stop-patching rule: no
replacement under another name) -- their only remaining S2 use had become
demonstrating exactly the cross-stream timestamp relation this pass
forbids. A historical fixture that needs to preserve an observer record's
``monotonic_ns`` as a bare fact uses the accepted S1 scalar primitive
directly (``require_nonnegative_int``) and, separately, ``PhysicalPos`` if
its physical position is itself being tested.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Mapping, Sequence

from scripts.research.family_b_13_primitives import (
    require_nonempty_string,
    require_nonnegative_int,
)

from .physical import PhysicalPos, StreamName
from .records import HarnessEventKind, HarnessRecordHeader

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
        result = require_nonempty_string(self.value)
        if not result.ok:
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
    records: an immutable structural fact holder -- ``identity``,
    ``start_ns``, ``end_ns``, ``start_pos``, ``end_pos``,
    ``termination_kind`` -- exposing NO query method in S2. What its own
    boundaries mean, and what may be compared against them, is a later-
    stage (``admit()``, S3+) question this type never answers itself; see
    :func:`build_participant_table` for how the boundaries themselves are
    established.

    S2 corrective decision (local stop-patching rule) -- record-ownership
    containment: this type exposes no ``contains_record`` (or a
    replacement under another name: ``record_within_lifetime``,
    ``contains_timed_record``, ``owns_record``, ``participant_contains``;
    see ``test_participant_lifetime_exposes_no_record_containment_helper``).
    A full harness stream already carries exactly one participant identity
    (see the module docstring's frozen capture-v6 single-identity
    contract), so there is no second identity for an ownership-aware
    relation to disambiguate in the first place. Separately, the
    now-removed ``TimedRecordRef`` type (see the S2 final boundary
    corrective pass below and the redesign checkpoint doc) was generic
    across every sealed stream and carried no participant-identity/
    ownership binding of its own, so a bare (position, timestamp) match
    could never honestly answer a record-ownership question even if S2
    needed one. Both reasons hold independently.

    S2 corrective decision (local stop-patching rule) -- clock-domain
    boundary: this type ALSO exposes no cross-record timestamp-relation
    query -- no ``contains_ns``, and no replacement under another name
    such as ``contains_time``, ``before_start``, ``after_start``,
    ``in_lifetime``, ``timestamp_within``, or ``compare_ns`` (see
    ``test_participant_lifetime_exposes_no_timestamp_relation_helper``).
    The frozen v6 oracle validates an explicit ``manifest.clock_contract``
    -- one bound ``CLOCK_MONOTONIC`` domain, shared across every plane/
    participant -- BEFORE trusting any cross-process/cross-stream
    monotonic relation; a missing or mismatched contract is an
    unconditional environment-ineligibility failure there, never a
    silently-accepted default (see ``_validate_clock_contract`` in the
    frozen oracle). S2 has no manifest and therefore no ``clock_contract``
    context, so it cannot prove that an arbitrary externally-supplied
    ``monotonic_ns`` was even captured in the same clock domain as this
    lifetime's own boundaries. Exposing a ``contains_ns``-shaped query
    would let a caller derive a cross-stream/cross-participant temporal
    relation S2 has no basis to assert.

    S2 final boundary corrective pass -- the type itself derives NO
    timestamp relation either, not even between its own ``start_ns`` and
    ``end_ns``: a self-declared singleton ``process_identity`` is
    structural equality only, never proof of "one writer, one clock" (see
    the module docstring's final corrective-pass note; a prior pass's
    "same single writer process ... needs no external clock-domain proof"
    reasoning is exactly the retracted warrant). ``start_ns`` and
    ``end_ns`` therefore remain two independently validated scalars only;
    direct construction with ``start_ns > end_ns`` is NOT rejected by this
    type merely because of that numeric relation -- both scalars
    individually satisfying S1 nonnegative-int semantics is all this type
    proves about them.

    ``__post_init__`` enforces this type's own internal structural
    consistency -- a real ``ParticipantIdentity``; S1-valid nonnegative
    ``start_ns``/``end_ns`` as independent scalars (no relation derived
    between them); ``start_pos``/``end_pos`` both ``PhysicalPos`` in
    ``StreamName.HARNESS_EVENTS`` with ``start_pos.ordinal == 1`` (a
    process's lifetime must start at the physically-first record) and
    ``end_pos.ordinal > start_pos.ordinal`` (strictly after -- one physical
    record cannot serve as both ``process_start`` and ``process_stop``);
    and a real :class:`TerminationKind` -- for *any* construction path.
    This physical invariant is derived from sealed physical order, never
    from a timestamp, so it needs no clock-domain proof. It does not, and
    cannot, prove that lifecycle records actually existed to justify those
    boundaries; that proof remains :func:`build_participant_table`'s job.
    ``ParticipantTable`` can then trust that any actual
    ``ParticipantLifetime`` instance it holds is internally self-consistent.
    """

    identity: ParticipantIdentity
    start_ns: int
    end_ns: int
    start_pos: PhysicalPos
    end_pos: PhysicalPos
    termination_kind: TerminationKind

    def __post_init__(self) -> None:
        if not isinstance(self.identity, ParticipantIdentity):
            raise ParticipantLifetimeError("lifetime_identity_invalid")

        start_result = require_nonnegative_int(self.start_ns)
        if not start_result.ok:
            raise ParticipantLifetimeError("lifetime_start_ns_invalid")
        end_result = require_nonnegative_int(self.end_ns)
        if not end_result.ok:
            raise ParticipantLifetimeError("lifetime_end_ns_invalid")
        # No relation is derived between start_ns and end_ns here -- see the
        # class docstring's S2 final boundary corrective-pass note. A
        # numerically "inverted" (start_ns > end_ns) pair of otherwise-valid
        # scalars is NOT rejected by this type.

        if not isinstance(self.start_pos, PhysicalPos) or not isinstance(
            self.end_pos, PhysicalPos
        ):
            raise ParticipantLifetimeError("lifetime_position_invalid")
        if (
            self.start_pos.stream is not StreamName.HARNESS_EVENTS
            or self.end_pos.stream is not StreamName.HARNESS_EVENTS
        ):
            raise ParticipantLifetimeError("lifetime_position_stream_mismatch")
        if self.start_pos.ordinal != 1:
            raise ParticipantLifetimeError("lifetime_start_position_not_physically_first")
        if self.end_pos.ordinal <= self.start_pos.ordinal:
            raise ParticipantLifetimeError("lifetime_end_position_not_after_start")

        if not isinstance(self.termination_kind, TerminationKind):
            raise ParticipantLifetimeError("lifetime_termination_kind_invalid")


@dataclass(frozen=True)
class ParticipantTable:
    """An immutable collection of every participant's
    :class:`ParticipantLifetime`, keyed by :class:`ParticipantIdentity`.

    For the accepted S2 model, this table represents the harness-lifecycle
    table established from one capture-v6 ``harness-events.jsonl`` stream,
    which the frozen oracle's contract fixes to exactly one participant
    identity (see the module docstring). ``ParticipantTable`` therefore
    represents exactly one entry -- never zero, never more than one -- and
    ``__post_init__`` enforces that for *any* construction path, not only
    :func:`build_participant_table`. This is not a general-purpose registry
    a future stage can silently broaden: a stage that genuinely needs
    lifetimes for other explicitly-proven participants must design its own
    composition mechanism rather than relaxing this type's own invariant.

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
        if len(copied) != 1:
            raise ValueError(
                "ParticipantTable must contain exactly one participant lifetime"
            )
        object.__setattr__(self, "_by_identity", MappingProxyType(copied))

    def get(self, identity: ParticipantIdentity) -> ParticipantLifetime | None:
        return self._by_identity.get(identity)

    def __contains__(self, identity: ParticipantIdentity) -> bool:
        return identity in self._by_identity

    def __iter__(self):
        """Yields :class:`ParticipantIdentity` keys -- mapping-consistent
        with :meth:`__contains__` and :meth:`get`, never the lifetime
        values. Use :meth:`get` to look up the corresponding
        :class:`ParticipantLifetime`."""

        return iter(self._by_identity)

    def __len__(self) -> int:
        return len(self._by_identity)


def _require_typed_harness_records(
    harness_records: Sequence[HarnessRecordHeader],
) -> None:
    """Fail closed with a stable S2-local structural error if any element
    of ``harness_records`` is not actually a :class:`HarnessRecordHeader`
    -- local input typing, never authority logic. Without this, an
    arbitrary duck-typed object reaching ``record.pos.ordinal`` or
    ``record.process_identity`` downstream would fail with an accidental
    ``AttributeError`` instead of a stable, catchable structural error."""

    for record in harness_records:
        if not isinstance(record, HarnessRecordHeader):
            raise ParticipantLifetimeError("harness_stream_record_type_invalid")


def _require_physically_coherent_harness_stream(
    harness_records: Sequence[HarnessRecordHeader],
) -> None:
    """Fail closed unless ``harness_records`` is, in its own supplied
    (physical-stream) order, a coherent representation of one sealed
    ``harness-events.jsonl`` stream: ordinal 1, then 2, then 3, ... with no
    duplicate, no gap, and no stale/reordered ``PhysicalPos`` left over from
    a different arrangement. This is deliberately not
    ``ChronologySpec`` -- it never inspects ``harness_sequence``, timestamp
    ordering, T0/T1, or phase/interval; it only confirms the typed sequence
    is a coherent representation of one physical sealed stream before
    anything downstream trusts it. ``HarnessRecordHeader.__post_init__``
    already rejects a non-``HARNESS_EVENTS`` position, so that case is not
    re-checked here."""

    for index, record in enumerate(harness_records, start=1):
        if record.pos.ordinal != index:
            raise ParticipantLifetimeError("harness_stream_physical_position_incoherent")


def _require_singleton_process_identity(
    harness_records: Sequence[HarnessRecordHeader],
) -> ParticipantIdentity:
    """Fail closed unless ``harness_records`` is nonempty and every record
    shares the exact same ``process_identity`` -- the structural shape S2
    can prove of the frozen capture-v6 single-identity harness contract
    (see the module docstring). Returns the one
    :class:`ParticipantIdentity` found. This never infers a role and never
    consults a manifest: the identity value comes only from the sealed
    records themselves."""

    if not harness_records:
        raise ParticipantLifetimeError("harness_stream_empty")

    identity_values = {record.process_identity for record in harness_records}
    if len(identity_values) != 1:
        raise ParticipantLifetimeError("harness_process_identity_not_singleton")

    return ParticipantIdentity(next(iter(identity_values)))


def _build_one_lifetime(
    identity: ParticipantIdentity, records: Sequence[HarnessRecordHeader]
) -> ParticipantLifetime:
    """Build the one :class:`ParticipantLifetime` this identity's records
    describe. ``records`` must already be the full, physically-coherent
    snapshot (ordinals ``1..len(records)`` in that order) that
    :func:`build_participant_table` validated -- this function does not
    re-derive that coherence, only relies on it for the physical
    first/last checks below, so ``records[0]``/``records[-1]`` (equivalently,
    ordinal ``1``/``len(records)``) are already known to be the physically
    first/last records without recomputing a min/max over ordinals.

    No timestamp relation is derived anywhere in this function -- not
    ``start_ns <= end_ns``, not any record's ``monotonic_ns`` against the
    lifetime boundary. See the module docstring's S2 final boundary
    corrective-pass note for why that reasoning ("singleton
    process_identity implies one writer implies one clock") is retracted.
    """

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

    if start_record.pos.ordinal != 1:
        raise ParticipantLifetimeError("participant_process_start_not_physically_first")
    if stop_record.pos.ordinal != len(records):
        raise ParticipantLifetimeError("participant_process_stop_not_physically_last")

    return ParticipantLifetime(
        identity=identity,
        start_ns=start_record.monotonic_ns,
        end_ns=stop_record.monotonic_ns,
        start_pos=start_record.pos,
        end_pos=stop_record.pos,
        termination_kind=TerminationKind.PROCESS_STOP,
    )


def build_participant_table(
    harness_records: Sequence[HarnessRecordHeader],
) -> ParticipantTable:
    """Build a :class:`ParticipantTable` solely from typed harness
    lifecycle records -- exactly one :class:`ParticipantLifetime`, for the
    single ``process_identity`` the frozen capture-v6 harness contract
    fixes every ``harness-events.jsonl`` stream to (see the module
    docstring). A stream carrying more than one distinct
    ``process_identity`` is not a valid capture-v6 harness history and is
    rejected as a structural error, never grouped into independent
    per-identity lifecycles.

    Deliberately takes no other parameter. If building a correct table ever
    seems to require ground truth, observer records, a manifest, T0/T1, or
    a phase/interval, that is a sign this layering is wrong -- it must not
    be added here (see the package/module docstrings' kill-switch note).

    Snapshot boundary (P1 corrective decision, local stop-patching rule):
    ``harness_records`` must be a real :class:`~typing.Sequence` (rejected
    with a stable ``harness_stream_input_not_sequence`` error otherwise --
    a plain one-shot iterator/generator does not silently become part of
    this public contract merely because ``tuple()`` can consume it), and is
    traversed EXACTLY ONCE, immediately, into an immutable ``tuple``
    snapshot. Every validator below, and the lifetime builder, consumes
    only that same snapshot -- the caller-supplied object itself is never
    traversed again. Without this, a Sequence-conforming object whose
    successive iterations return different content could let one validator
    see one history while the lifetime is built from a different one; see
    ``test_build_participant_table_snapshots_adversarial_sequence_exactly_once``.
    No future validator may re-traverse the caller object -- add any new
    check against ``snapshot`` instead.

    Checks, each failing closed in order: every element is actually a
    :class:`HarnessRecordHeader` (:func:`_require_typed_harness_records`);
    the full supplied stream is itself physically coherent
    (:func:`_require_physically_coherent_harness_stream`); the stream is
    nonempty and carries exactly one ``process_identity``
    (:func:`_require_singleton_process_identity`).
    """

    if not isinstance(harness_records, Sequence):
        raise ParticipantLifetimeError("harness_stream_input_not_sequence")
    snapshot: tuple[HarnessRecordHeader, ...] = tuple(harness_records)

    _require_typed_harness_records(snapshot)
    _require_physically_coherent_harness_stream(snapshot)
    identity = _require_singleton_process_identity(snapshot)

    lifetime = _build_one_lifetime(identity, snapshot)
    return ParticipantTable(_by_identity=MappingProxyType({identity: lifetime}))
