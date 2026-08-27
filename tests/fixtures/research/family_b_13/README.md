# Family B experiment #13 — S0.1 declarative witness corpus

Non-normative research asset. Not architecture authority, not a production
contract, not an experiment result. See
`docs/architecture/research/blocker-b-family-b-13-authority-core-redesign.md`
for the redesign checkpoint this corpus supports, and
`tests/oracles/family_b_13/v6/oracle_manifest.json` for the frozen historical
oracle's provenance.

## What this is

A self-contained set of sealed `family-b-13-capture-v6` capture directories,
each directly consumable by `analyze_capture()` from the byte-frozen v6
historical oracle at `tests/oracles/family_b_13/v6/`. No fixture here depends
on the historical PR #52 pytest builders at runtime — every capture is
checked-in data, generated once from those builders in an untracked scratch
environment (per the S0.1 task's "one-time fixture generation" allowance) and
then copied in as plain files.

`corpus_manifest.json` records one row per fixture: its `kind`
(`positive_control` / `historical_witness` / `model_derived_witness`), the
frozen oracle's actual `expected_v6_result` (the complete
`AnalysisResult.as_dict()`, captured by replaying the vendored oracle against
the checked-in bytes), the reconciled design's `intended_v7_outcome`, whether
`migration_required`, and a `file_hashes` map binding every capture file to
its checked-in SHA-256.

**`expected_v6_result` vs. `intended_v7_outcome` are deliberately asymmetric.**
`expected_v6_result` is the full, exact deterministic `AnalysisResult.as_dict()`
— oracle replay is checked against it byte-for-byte, because the frozen v6
oracle's behavior is fully known and must never silently drift.
`intended_v7_outcome` is a single outcome string only (one of the five
`AnalyzerOutcome` values) — it is migration/design metadata, not a frozen v7
contract. G7 migration semantics are outcome-to-outcome: `migration_required`
is computed as `expected_v6_result["outcome"] != intended_v7_outcome`. v7 has
no designed reason-string/witness-body taxonomy yet, and S0.1 must not freeze
one by implication — a future v7 that reaches the same *outcome* through
different, better reasons is not a migration and needs no ledger row.

`migration_expectations.json` (G7) lists only the fixtures where
`intended_v7_outcome` intentionally differs from `expected_v6_result`'s
outcome, with an explicit `reason_class`, `source_ref`, and `explanation` per
row, validated against a cause/cell-specific outcome-pair ->
required-`reason_class` matrix (not merely checked for membership in the set
of allowed reason classes) — see
`docs/architecture/research/blocker-b-family-b-13-authority-core-redesign.md`
§6.E. No wildcard fixture IDs or result pairs; no runtime/operator override
field.

## Provenance

All witnesses are sourced from the historical PR #52 branch
(`research/family-b-13-preexecution-harness`), frozen head
`56723770b5edb3a574a16c9b73d2ad5f668d903c` — its regression test suite
(`tests/test_blocker_b_family_b_13_analyzer.py`, read-only at that commit) and
its PR review history (inline review comments on that exact commit, read
read-only via the GitHub API). No witness was invented merely to hit a count;
every fixture below traces to a concrete regression test or a concrete review
finding at that frozen head.

### A. Positive controls (13A–13G)

One coherent positive-control capture per subrun letter, each satisfying that
subrun's machine obligation
(`blocker-b-family-b-13-preexecution-harness.md` §9's obligation table). 13E's
correct/expected classification is `B_S1_GAP_DETECTED`, not
`ANALYZER_PASS_TESTED_INTERLEAVING` — 13E's phenomenon *is* the approved
injected gap signal.

### B. Historical false-result classes (already fixed at the frozen head)

Sixteen witnesses reproducing historical false-result classes that the frozen
v6 oracle now handles correctly (each traces to an existing passing regression
test at the frozen head). `intended_v7_outcome` is identical to
`expected_v6_result["outcome"]` for all sixteen — the redesign does not loosen
any of them.

### C. The latest stop-triggering four (frozen v6 is WRONG; unfixed)

The four P1/P2 findings named in
`blocker-b-family-b-13-authority-core-redesign.md` §3 that triggered the
project decision to stop patching the v6 monolith. For all four, the frozen
oracle's actual output was reproduced empirically (not assumed from review
text) and is genuinely wrong; `migration_expectations.json` carries the
correction:

- `stop_reader_pre_t0_before_process_start`
- `stop_generator_sequence_relabel`
- `stop_duplicate_subrun_evidence_ids`
- `stop_post_t1_gap_signal_rewrites_t1`

### D. Model-derived witnesses (E1, E3)

Two additional leaks surfaced by applying the reconciled v7 admission model to
the frozen v6 oracle (`blocker-b-family-b-13-authority-core-redesign.md` §8),
constructed directly against the analyzer's source and confirmed empirically:

- `model_e1_pre_t0_surface_influences_candidate`
- `model_e3_13c_handoff_after_t1`

## G7 correction found during S0.1 materialization

While materializing `stop_post_t1_gap_signal_rewrites_t1`, S0.1 work surfaced
a genuine internal contradiction between two pieces of the redesign material:

- the reconciled design (§5.13, §6.B) requires v7 to recover
  `ANALYZER_PASS_TESTED_INTERLEAVING` for this witness (a post-T1 diagnostic
  must not retroactively rewrite an already-reached T1 verdict) — the fourth
  stop-triggering P1 is defined as closed exactly by this behavior;
- the S0.0-era G7 rule ("**PREVIOUS G7 DECISION RETRACTED — over-broad
  differential rule**": *any* `v6 non-PASS -> v7 PASS` transition is
  absolutely forbidden, no exceptions) forbade exactly that transition.

This was reported and stopped on before committing, rather than silently
resolved either by mislabeling the intended v7 result or by weakening the G7
test. The retraction and its cause/cell-specific replacement are recorded in
`docs/architecture/research/blocker-b-family-b-13-authority-core-redesign.md`
§6.E. This is the corpus doing its intended job: a declarative migration
ledger, checked against a differential gate, found a real design defect
before any v7 code existed.

The corrected gate distinguishes cause-specific cells. Two remain absolute,
never overridable by any ledger reason including `CONTRACT_AMENDMENT`:

- `ENVIRONMENT_INELIGIBLE -> `(any non-`ENVIRONMENT_INELIGIBLE` outcome);
- `HARNESS_INCOMPLETE -> ANALYZER_PASS_TESTED_INTERLEAVING`.

`HARNESS_INCOMPLETE -> B_S1_GAP_DETECTED` and
`HARNESS_INCOMPLETE -> GENERATOR_WINDOW_ENUMERATION_OMISSION_WITNESS` remain
contract-amendment-only; the current redesign does not loosen capture-v6, so
this corpus carries zero `CONTRACT_AMENDMENT` rows.
`GENERATOR_WINDOW_ENUMERATION_OMISSION_WITNESS -> ANALYZER_PASS_TESTED_INTERLEAVING`
is not authorized by this correction and does not appear here.

## NOT MATERIALIZED

None. Every historical false-result class enumerated in the S0.1 task
(sixteen already-fixed classes, the latest stop-triggering four, and both
model-derived witnesses E1/E3) was reproduced as a stable, concrete sealed
capture and is present in `corpus_manifest.json`.
