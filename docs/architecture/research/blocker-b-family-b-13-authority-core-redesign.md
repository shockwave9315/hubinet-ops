# NON-NORMATIVE RESEARCH / EVIDENCE

# Family B experiment #13 harness — authority-core redesign checkpoint (S0.0 + S0.1)

This document is a checkpoint/design record for the Family B experiment #13
pre-execution harness analyzer. It is **not** an ADR, **not** architecture
authority, **not** a production contract, **not** an experiment result, and
**not** proof of B-S1. It authorizes no schema, persistence, runtime,
`hostd`, scheduler, HTTP API, Home Assistant, enrollment, or mutation work,
and it grants `security_continuity=trusted` nowhere. Where anything below
conflicts with an ACCEPTED ADR, the ADR wins; this document does not amend
ADR 0001, ADR 0002, ADR 0003, ADR 0004, ADR 0005, or ADR 0006.

Authority order for this checkpoint, unchanged from prior Family B research:

```text
explicit operator decisions
> ACCEPTED ADR / accepted architecture
> AGENTS.md
> implementation status
> code/tests
> non-normative research
```

## 1. Provenance

| Item | Value |
| --- | --- |
| Repository | `shockwave9315/hubinet-ops` |
| Exact base `main` at planning time | `cdbe8e07d4b5d62b7877aedda8e4e220b2c5a743` (merge commit of Research #2B / PR #51) |
| Historical PR #52 ("Research: prepare Family B experiment #13 harness") base | same, `cdbe8e07d4b5d62b7877aedda8e4e220b2c5a743` |
| Historical PR #52 frozen/superseded head | `56723770b5edb3a574a16c9b73d2ad5f668d903c` |
| PR #52 merged into `main`? | **No.** Verified: `56723770...` is not an ancestor of `main`; it is 33 commits ahead of the base above |
| PR #52 exact file delta at that head | `docs/architecture/research/blocker-b-family-b-13-preexecution-harness.md` (1,582 lines added), `scripts/research/blocker_b_family_b_13_analyzer.py` (3,231 lines added), `tests/test_blocker_b_family_b_13_analyzer.py` (7,055 lines added); 3 files changed, 11,868 insertions, 0 deletions — no other tracked file touched |
| New branch for this checkpoint | `research/family-b-13-authority-core-redesign`, created from `main` at the exact base SHA above |
| Live PVE/HA access by this checkpoint | None |
| Experiment #13 executions by this checkpoint | None |

## 2. What actually happened to PR #52

PR #52 was **not merged**. Its 33 commits are a real, frozen, local
development history of a v6 harness analyzer, reachable only from that
branch/head — not from `main`. Because it never merged:

- `main`'s product implementation did not roll back anything;
- merged Research #2A (PR #49), #2A.1 (PR #50), and #2B (PR #51) remain
  intact on `main`, unaffected by PR #52's disposition;
- R0/A1/C1 durable authority and runtime remain intact and unaffected;
- the rollback described in this checkpoint is **logical/research-only**:
  the v6 analyzer lost authority status as a design *before* Experiment #13
  was ever executed against it, not a reversal of any merged, accepted
  artifact.

This checkpoint does not call PR #52's work discarded. Its research
document, primitive-parsing work, adversarial witnesses, and regression
corpus remain retained research assets for the redesign below — they are
superseded as an *implementation path*, not erased as evidence.

## 3. Why patching stopped

The v6 monolith accumulated repeated, independent, adversarially-discovered
false-result findings across its development history. The final
stop-triggering review, performed against the exact frozen head
`56723770b5edb3a574a16c9b73d2ad5f668d903c`, confirmed at least:

- **P1** — reader-owned pre-T0 observer evidence can predate the reader's
  own `process_start`;
- **P1** — `generator_sequence` can relabel physical generator history;
- **P2** — combined-subrun phenomenon/evidence IDs need not be unique;
- **P1** — a post-T1 `gap_signal` can retroactively rewrite the T1 result.

These followed many earlier authority/evidence-binding corrections captured
in that branch's own commit history (visible on
`research/family-b-13-preexecution-harness`, reachable only from PR #52's
head, not from `main`).

**Project decision: STOP PATCHING THE V6 MONOLITH.** No v7 is authorized as
another locally-patched revision of that same architecture. This is an
**implementation-structure failure**, not proof that B-S1 itself is false.
B-S1 remains exactly what Research #2B (PR #51,
`docs/architecture/research/blocker-b-family-b-2b-stateful-sentinel-design.md`
§19) already recorded: **PLAUSIBLE CANDIDATE / NOT PROVEN.**

## 4. Frozen project status (unchanged by this checkpoint)

None of the following classifications changes here:

```text
R0:                                   GO / STRICTLY READ-ONLY
Blocker B:                            OPEN
Family B:                             UNRESOLVED / NOT FULLY AUDITED
B-S1:                                 PLAUSIBLE CANDIDATE / NOT PROVEN
Phase S:                              DESIGN ONLY / REQUIRES CONTROLLED FALSIFICATION
future positive Blocker-B ADR:        NOT STARTED / UNRESOLVED
WAVE B1:                              DEFERRED / NOT AUTHORIZED
Phase 1C:                             BLOCKED
security_continuity=trusted:          GRANTED NOWHERE
Experiment #13:                       NOT AUTHORIZED / NOT EXECUTED
```

Nothing in this checkpoint changes R0, A1, C1, production code, schema,
persistence, runtime, `hostd`, Home Assistant, enrollment, or policy. No
mutation authority exists anywhere in this repository.

## 5. Authority-core redesign decisions

The redesign has completed a broad design pass and a targeted reconciliation
pass over the v6 monolith's failure modes. The frozen direction:

External capture format **`family-b-13-capture-v6` is KEPT** — no new sealed
capture field has yet been shown necessary. The new authority analyzer is
named **v7** and centers on:

1. a `ParticipantTable` established before any evidence is admitted;
2. one centralized `admit()` gate — no evidence enters authority state
   through any other path;
3. an explicit `PhysicalPos` derived from sealed stream order, rather than
   inferred from generator-relabeled sequence numbers;
4. a centralized `ChronologySpec`;
5. a phase × interval admission model;
6. immutable, typed derived facts — no in-place mutation of an already-admitted
   fact;
7. a strict distinction between: sealed primitives; decoded records; derived
   facts; projections; authority-bearing facts; and classification — each a
   separate, non-conflatable layer;
8. an observer authority model along two independent axes:
   - **ORIGIN**: `BASELINE` / `CANDIDATE_OBSERVED`;
   - **LEVEL**: `ENUMERATED` / `CONFIRMED` / `FINAL`.
   There is **no** ceremonial `KNOWN`/`UNKNOWN` transition. A new candidate
   observer primitive may enter the observer ledger directly as
   `(CANDIDATE_OBSERVED, ENUMERATED)`. Ground truth must never manufacture
   observer knowledge;
9. exact-UPID lookup remains **confirmation-only**, never unknown-record
   enumeration authority;
10. **P-ANTITONE**: generator/ground-truth facts may only constrain/shrink
    observer interpretation; they may never *introduce* observer knowledge;
11. interval classes: `PRE_T0`, `AT_T0`, `CANDIDATE`, `POST_T1_DIAGNOSTIC`,
    `POST_FINALIZATION`;
12. phase and interval must **both** agree before authority is admitted;
13. observer T1 classification consumes evidence under the correct `t <= T1`
    semantics; post-T1 observer diagnostics cannot retroactively rewrite T1
    — this directly closes the fourth stop-triggering P1 in §3;
14. capture integrity is kept separate from the T1 result. Conceptually:
    - `CaptureValidity`: `INELIGIBLE` / `INCOHERENT` / `COHERENT`;
    - `T1Result`: `PASS` / `GAP` / `WITNESS`.
    A flat external result may be projected later, but the internal model
    must preserve this distinction;
15. `IntegrityFinding` has no interval field and cannot be filtered as
    ordinary observer evidence;
16. `ObservationFinding` is interval/phase typed;
17. contradictions/incoherent sealed evidence can invalidate the run; they
    cannot manufacture or preserve an omission witness;
18. centralized classification precedence — one place decides precedence,
    not scattered per-field logic;
19. architecture AST/static gates prevent downstream phases from re-reading
    raw manifest/records after the designated boundary;
20. property/model/metamorphic testing becomes a primary verification
    mechanism, not an adjunct to example-based tests.

## 6. Reconciled ambiguities

These correct ambiguities left open by the first redesign pass.

**A. Post-T0 authority graph.** Entry into the observer ledger *is* the
enumeration edge. Ground truth is not required to introduce the UPID.

**B. Post-T1 semantics.** Coherent observer evidence originating only after
T1 cannot change the T1 observer verdict. However, global sealed-capture
incoherence may still turn an otherwise usable run into
`HARNESS_INCOMPLETE`. A post-T1 observer gap may **not** turn `PASS`/
`WITNESS` into `GAP`.

**C. T0.** Authority is not assigned from timestamp equality alone. Sealed
stream phase + derived interval class must agree. Pre-T0 establishment owns
its valid `<= T0` evidence. Candidate authority requires `> T0`. Observer
loss at/before T0 remains result-bearing `GAP` evidence rather than being
silently discarded.

**D. Frozen oracle.** The v6 analyzer at the historical PR #52 head
(`56723770b5edb3a574a16c9b73d2ad5f668d903c`) is a historical oracle only. It
must be byte-identical/frozen when vendored in S0. It must **not** import
future v7 primitive parsers. Its known-unsound behaviors remain useful as
historical differential witnesses. Parser reuse is proven through
parser-vector differential tests, not by making the oracle use new code.

**E. Differential gate G7.** Do not encode the old naive rule "v7 must
always be `>=`/stricter than v6" — v6 is known-unsound. Differences require
a checked-in migration expectation ledger. Known intentional old/new
differences must be explicit, typed, and checked in. No runtime/operator
override exists for any cell of this gate.

**PREVIOUS G7 DECISION RETRACTED — over-broad differential rule.** S0.0
stated an absolute, no-exception rule: *any* `v6 non-PASS -> v7 PASS`
transition is forbidden. S0.1 corpus materialization found this over-broad:
it collided with this checkpoint's own §5.13/§6.B, which requires v7 to
recover `ANALYZER_PASS_TESTED_INTERLEAVING` for the fourth stop-triggering
P1 (§3) — a post-T1 `gap_signal` that frozen v6 wrongly turns into
`B_S1_GAP_DETECTED`. That is a v6 interval-partition bug (evidence from the
wrong interval polluting the candidate result), not a contract loosening,
and the reconciled design already required v7 to correct it. The
contradiction was found and reported before any commit, before any v7 code
existed — this is the declarative migration corpus doing its intended job.
The over-broad rule is retracted and replaced below with a cause/cell-
specific matrix. This is a correction to the differential migration gate,
not a capture-v6 contract amendment.

Differential legality is cause/cell-specific. `PASS`, `GAP`, `WITNESS`,
`INCOMPLETE`, and `INELIGIBLE` below abbreviate `ANALYZER_PASS_TESTED_
INTERLEAVING`, `B_S1_GAP_DETECTED`, `GENERATOR_WINDOW_ENUMERATION_OMISSION_
WITNESS`, `HARNESS_INCOMPLETE`, and `ENVIRONMENT_INELIGIBLE`.

Allowed without a ledger row: `same outcome -> same outcome`.

Allowed with an exact checked-in ledger entry:

```text
PASS      -> INCOMPLETE   reason_class = V6_UNSOUND
PASS      -> GAP          reason_class = V6_UNSOUND
PASS      -> WITNESS      reason_class = V6_UNSOUND
GAP       -> PASS         reason_class = V6_INTERVAL_POLLUTION
GAP       -> WITNESS      reason_class = V6_INTERVAL_POLLUTION
GAP       -> INCOMPLETE   reason_class = V6_UNSOUND
WITNESS   -> INCOMPLETE   reason_class = V6_UNSOUND
WITNESS   -> GAP          reason_class = V6_UNSOUND
non-INELIGIBLE -> INELIGIBLE   only with an exact reviewed V6_UNSOUND
                                applicability witness
```

`V6_CRASH` remains a separately ledgered historical-oracle failure class,
used only when the frozen oracle actually raises rather than returning an
`AnalysisResult` for that fixture.

Absolute / structural restrictions, current for this redesign, not
overridable by any ledger reason including `CONTRACT_AMENDMENT`:

```text
1. INELIGIBLE -> any non-INELIGIBLE outcome        absolutely forbidden
2. INCOMPLETE -> PASS                              forbidden for this
                                                    redesign (G12: v7
                                                    integrity coverage must
                                                    not convert an
                                                    incoherent capture into
                                                    positive evidence)
```

Contract-amendment-only cells — legal only with `reason_class =
CONTRACT_AMENDMENT` and a non-empty reference to a separately accepted
contract change; the current redesign does not loosen capture-v6, so no such
row exists yet:

```text
3. INCOMPLETE -> GAP        contract-amendment-only, no such amendment exists
   INCOMPLETE -> WITNESS    contract-amendment-only, no such amendment exists
```

Not authorized by this correction, and not to be inferred from `GAP ->
PASS`:

```text
4. WITNESS -> PASS   not authorized; a required witness apparently needing
                     this transition must STOP and be reported separately
```

The migration ledger remains closed and explicit either way: no wildcard
fixture IDs, no wildcard result pairs, no runtime/operator override field.
Each row binds exactly one fixture and one expected transition.

## 7. Kill switch / gates

**G8 — kill switch.** If the v7 redesign again requires recurring
cross-cutting authority/lifecycle/interval exceptions comparable to the v6
monolith, **stop**. Do not create v8. At that point, abandon this B-S1
experiment-harness *approach* and return to mechanism research; this does
not itself change B-S1's plausible/not-proven status.

**G10 — frozen v6 oracle integrity / byte identity**, as fixed in §6.D.

**G12 — v7 integrity coverage** must not drop old-oracle rejection/raise
conditions in a way that would let an incoherent capture become `GAP` or
positive evidence.

No additional normative meaning is invented beyond what the prior design
material already establishes for these gates.

## 7a. Anti-loop / continuity decision (S2 final boundary corrective pass)

**This is a durably recorded operator decision, load-bearing for every
subsequent implementation and review pass on this checkpoint.**

Draft PR #53 is the **only** active implementation PR for this checkpoint.
A difficult implementation, or a G8 trigger, does **not** by itself imply:

- a v8;
- a new PR #54 or any other new implementation PR;
- another clean rewrite;
- moving the same mechanism under new names to route around a stop.

What **G8 means, exactly**, restated for this pass:

```text
STOP IMPLEMENTATION
IDENTIFY THE BROKEN MECHANISM ASSUMPTION
RETURN TO MECHANISM RESEARCH
```

A new implementation PR requires a **separate, explicit operator decision**,
made only after the failed mechanism assumption has actually been
identified — never as a default reaction to difficulty.

Also recorded here, durably, as standing rules for this checkpoint:

- accepted stage boundary SHAs (§9) are immutable inputs — never rewritten
  or reinterpreted to make a later stage easier;
- a current stage may not import semantics from a later, not-yet-built
  stage to make itself work (S2 may not reach for S3+ authority concepts —
  see §5/§9's kill-switch note, repeated at the S2 stage level in the
  package docstring);
- witness-specific exceptions are forbidden — a fix must close the bounded
  contract family a witness belongs to, never carve out a special case for
  that one witness alone;
- missing provenance may never be replaced by a positive fact — an absence
  of evidence stays an absence, never silently upgraded to a "yes";
- if the accepted design, the current stage's own contract, and a witness
  cannot all be satisfied together: **STOP BEFORE COMMIT** and report the
  conflict, rather than bending any of the three to make the other two fit;
- green tests do not override a semantic-layer violation — passing tests
  are evidence, not proof of architectural correctness;
- do not silently amend the frozen design (§9's accepted stage boundaries,
  §4's frozen classifications) — any actual amendment is reported as an
  explicit, separately-flagged decision, never folded silently into a
  routine corrective pass.

This continuity guard applies to future implementation **and** review on
this checkpoint, not only to this pass.

## 8. Two model-derived leaks found before implementation

Applying the new model to the frozen v6 oracle surfaced two additional
leaks, neither of which is a reason for a third monolith patch — both are
acceptance witnesses for centralized `PhaseSpec`/interval-admission behavior
in v7:

- **E1**: a pre-T0 index/surface observation can influence the candidate
  result, because the old analyzer has an upper time bound without the
  required lower/phase bound;
- **E3**: a 13C active/archive handoff marker occurring after T1 can still
  discharge a candidate-interval obligation.

## 9. Delivery plan

The project has rejected literal in-place reduction of PR #52. Clean review
boundaries apply instead — as **one long-lived Draft PR**, not the separate
PR A/B/C/D sequence an earlier planning pass in this document used. That
wording is superseded; boundaries between stages are now **pinned commit
SHAs inside that one draft PR**, reviewed incrementally, not separate PRs:

- **Historical PR #52**: frozen / superseded research record. Not mutated by
  this checkpoint or any later stage.
- **Draft PR #53, "Research: rebuild Family B #13 authority core"** (base
  `main`, head `research/family-b-13-authority-core-redesign`) is the single
  development PR for every stage below. It was opened at S0.0 and **stays
  draft** while S0 through S7 are developed and reviewed on this branch; a
  separate, later operator decision changes that.
- **S0** — redesign checkpoint documentation, frozen v6 oracle, declarative
  witness corpus, migration expectation ledger, and hermetic asset gates.
  **Accepted stage boundary: `f40aa5c123abec8f8bfb00f6bb5d2701f20bcad5`.**
  Staged in two sub-steps:
  - **S0.0** (`702d4286dcbc2d2042a4d665a911841c8b282bb3`): the
    redesign/project-tree checkpoint — this document (§1–§8) and the Family-B
    S0 status entry in `docs/architecture/0.5-implementation-status.md`. The
    oracle, corpus, and ledger did not yet exist at S0.0.
  - **S0.1** (`f40aa5c123abec8f8bfb00f6bb5d2701f20bcad5`): the byte-frozen v6
    oracle (`tests/oracles/family_b_13/v6/`), the declarative sealed-capture
    witness corpus and migration expectation ledger
    (`tests/fixtures/research/family_b_13/`), and the hermetic S0
    asset-validation tests (`tests/test_blocker_b_family_b_13_s0_assets.py`).
    Also corrected the over-broad S0.0 G7 rule (§6.E) after S0.1 corpus
    materialization surfaced the contradiction it caused.
- **S1** (`1d26b2216c01f2100f3dab703746ce503d10eaf0`) — pure primitive
  parsers (`scripts/research/family_b_13_primitives.py`), a declarative
  parser-vector corpus
  (`tests/fixtures/research/family_b_13/parser_vectors.json`), and their
  differential proof against the frozen v6 oracle
  (`tests/test_blocker_b_family_b_13_s1_primitives.py`). S1 builds only
  sealed-bytes-to-typed-primitive parsing; it does not implement, and must
  not be read as implementing, any part of the v7 authority core (§5).
  Includes a corrective review that tightened inotify raw-mask sealed-value
  typing to compose the frozen contract exactly (`bool`/negative/non-int
  fails closed identically to frozen v6), removed the semantic
  inotify-mask-to-observer-gap-reason mapping (`watch_gap_reasons`) as
  out-of-scope classification (reclassified `AUTHORITY_OR_CLASSIFICATION`,
  deferred to S2+), and removed an unproven, vector-less path-composition
  export (`task_bucket_path`), keeping only the bucket-identifier lexical
  check the S1 vectors actually prove.
- **S2** (current stage) — the first v7 authority-core foundation stage:
  typed structural records, `PhysicalPos`, `ParticipantLifetime`, and
  `ParticipantTable`
  (`scripts/research/family_b_13_v7/physical.py`,`records.py`,
  `participants.py`; `tests/test_blocker_b_family_b_13_s2_lifetimes.py`).
  S2 answers only "what structurally exists in the sealed record history" —
  physical stream order, a harness process's own lifecycle boundaries, and
  pure fact queries against that lifetime. `ParticipantTable` construction
  depends on typed harness lifecycle records alone: no ground truth, no
  observer records, no manifest T0/T1/phase/interval context. S2 does not
  implement, and must not be read as implementing, any part of `admit()`,
  the observer ledger, `(ORIGIN x LEVEL)` state, `ChronologySpec`/
  `PhaseSpec`, `IntegrityFinding`/`ObservationFinding`,
  `CaptureValidity`/`T1Result`, or any analyzer-outcome projection —
  **IMPLEMENTED / AWAITING INDEPENDENT REVIEW**, not accepted, until that
  review actually occurs. A subsequent S2 corrective review found and
  closed a real positive-fact-manufacturing gap: `ParticipantLifetime.
  contains_record` previously returned `True` for a cross-stream record
  whenever its timestamp alone fell in-bounds, even though a
  `TimedRecordRef` carries no participant-ownership binding to justify
  that; it now fails closed (`CrossStreamComparisonError`) for any
  cross-stream request, checked before the timestamp bound so the behavior
  never depends on where that timestamp happens to fall. The same review
  closed the type boundary so `HarnessRecordHeader` cannot exist with a
  non-`HARNESS_EVENTS` position through any construction path (not only
  the decoder), and made `ParticipantTable` copy and validate any input
  mapping rather than trust or alias it. A **final S2 boundary corrective
  pass** (§7a anti-loop decision, same Draft PR #53, no v8) went further
  than patching that gap again: independent review found the
  `contains_record` abstraction itself invalid for S2, since
  `TimedRecordRef` carries no participant-identity/ownership binding at
  all, so a bare in-bounds (position, timestamp) match can never honestly
  prove a record belongs to a given participant — even within one
  physically-coherent `harness-events.jsonl` stream, one participant's
  lifetime can legitimately contain another's record (two interleaved
  process lifecycles). `ParticipantLifetime.contains_record` was therefore
  **deleted outright** (local stop-patching rule: no replacement helper
  under any name such as `record_within_lifetime`, `contains_timed_record`,
  `owns_record`, or `participant_contains`); `contains_ns` remains as the
  intentionally narrow numeric-only fact. That same pass closed the
  `TimedRecordRef` and `ParticipantLifetime` type boundaries with
  `__post_init__` invariants so neither can be directly constructed
  internally inconsistent (bypassing their decoder/builder), and added a
  full-stream `PhysicalPos` coherence gate to `build_participant_table` —
  checked before grouping by participant identity — that fails closed on a
  duplicate, missing, or stale/reordered physical ordinal anywhere in the
  supplied harness stream. Still **IMPLEMENTED / AWAITING INDEPENDENT
  REVIEW**, not accepted.
- **S3–S6** (future, not started): the remaining dormant v7 authority-core
  stages, each its own independently SHA-gated/reviewed commit boundary on
  this same Draft PR #53. No real experiment. No production authority.
- **S7** (future, not started): explicit authority cutover, on this same
  Draft PR #53.

Only after S7, required independent review, and every gate above passes does
a **separate operator decision** determine whether to authorize real
Experiment #13. Experiment #13 remains **NOT AUTHORIZED** even after S7
until that separate decision exists.

## 10. Explicit safety / authority boundary

This checkpoint:

- does not modify PR #52;
- does not close, comment on, or otherwise mutate PR #52 or any GitHub
  review/process state;
- does not create a PR;
- does not merge anything;
- does not touch PVE, Home Assistant, `/var/log/pve`, `pct`, `qm`, or
  `pvesh`;
- does not run or authorize Experiment #13;
- does not modify production/runtime code, schema, persistence, `hostd`,
  scheduler, HTTP API, or Home Assistant wiring;
- does not modify the old `#52` analyzer or its tests;
- does not modify `docs/architecture/research/blocker-b-family-b-current-release-source-contract-audit.md`,
  `blocker-b-family-b-target-version-reconciliation.md`,
  `blocker-b-family-b-2b-stateful-sentinel-design.md`, ADR 0005, or ADR 0006
  — those are read-only inputs to this checkpoint;
- does not grant `security_continuity=trusted` anywhere;
- does not authorize WAVE B1;
- does not authorize or unblock Phase 1C;
- does not change R0's read-only status; and
- does not constitute or imply a positive Blocker-B ADR of any kind.

## 11. What remains unresolved after this checkpoint

Unchanged from the frozen status in §4, plus the v7 authority-core work
itself: Draft PR #53 remains draft and unmerged (§9). S1's pure primitive
parsers are accepted (`1d26b2216c01f2100f3dab703746ce503d10eaf0`); S2's
typed structural records/`PhysicalPos`/`ParticipantLifetime`/
`ParticipantTable` foundation is implemented and awaiting independent
review, not accepted; the remaining dormant v7 authority core (S3–S6) and
the explicit cutover review (S7) remain not started. B-S1 remains a
plausible, precisely falsifiable Phase-S candidate, not a proven mechanism
(Research #2B, §19). No experiment result exists to promote or demote that
status.
