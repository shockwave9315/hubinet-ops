# Postmortem — Family B / B-S1 and Experiment #13 (PR #52, PR #53)

**Status: ARCHIVED POSTMORTEM. Not architecture authority. Not a roadmap.**
Written 2026-08-28. Non-normative: it authorizes nothing, selects no mechanism,
changes no ADR classification, and grants `security_continuity=trusted` nowhere.

This exists so that no future agent has to read ~25,700 lines of research to
learn what happened and why it stopped.

## 1. What Family B / B-S1 was

**Blocker B** is the open question of *persistent workload-incarnation proof*:

```text
scan sees occupant A at VMID 112
A is destroyed between scans
occupant B is recreated as VMID 112
next scan sees 112 again
Hubinet never observed ABSENT
```

ADR 0005 (ACCEPTED) concluded that **no evidence candidate composed of ordinary,
copyable PVE/guest/config state** is sufficient to grant
`security_continuity=trusted` under the stock-PVE baseline. ADR 0006 (ACCEPTED)
recorded the stronger-proof research as UNRESOLVED and selected no mechanism.

**Family B** — in the sense used by these research branches — was the attempt to
find that stronger proof in a different place: the PVE **task/event history**.
The provisional candidate was named **B-S1**: a stateful sentinel that would
watch PVE task history continuously and prove, with fail-closed gap semantics,
that no lifecycle event had been missed between two observations. If complete
coverage could be proven, an absence of destroy/create events between scans
would be positive evidence that the occupant never changed.

(Note the name collision: ADR 0005's own candidate taxonomy also has a
"Family B", meaning *operator assertion only*, rejected in ADR 0005 §8. That is
a different thing from the research branches described here, which target
ADR 0006 §8a's task-history channel.)

## 2. Why it was explored

It was the only remaining channel that looked like it might carry a
*host-rooted, non-copyable* witness. Every ordinary guest/config-resident field
had already been ruled insufficient by ADR 0005. Task history is written by the
host, not by the guest, and is not copyable by a replacement occupant — so if it
were provably complete over an interval, it would satisfy ADR 0006's same-slot
witness test.

## 3. Why PR #52 stopped

**PR #52 — "Research: prepare Family B experiment #13 harness"**
branch `research/family-b-13-preexecution-harness`,
frozen head `56723770b5edb3a574a16c9b73d2ad5f668d903c`,
33 commits, ~11,868 inserted lines, **never merged**.

It built a "v6" offline analyzer and harness for a planned **Experiment #13**
(pagination / archive rotation / watch-scan omission) — an experiment designed
to *falsify* B-S1 by trying to hide a lifecycle event from the observer.

The v6 analyzer was a monolith. Across its development it accumulated repeated,
independently discovered, adversarial false-result findings. The final
stop-triggering review against the frozen head confirmed at least:

- **P1** — reader-owned pre-T0 observer evidence could predate the reader's own
  `process_start`;
- **P1** — `generator_sequence` could relabel physical generator history;
- **P1** — a post-T1 `gap_signal` could retroactively rewrite the T1 result;
- **P2** — combined-subrun phenomenon/evidence IDs need not be unique.

The project decision was **stop patching the v6 monolith**. Importantly, this was
read as an *implementation-structure* failure of the analyzer, not as proof that
B-S1 itself was false.

## 4. Why PR #53 stopped

**PR #53 — "Research: rebuild Family B #13 authority core"**
branch `research/family-b-13-authority-core-redesign`,
stopped head `fa87ec55d4b458a2e3257acf7aa06cce05344fc4`,
~13,707 inserted lines, **never merged**.

It was a clean-boundary rebuild ("v7") of the same analyzer, planned as stages
S0 through S7 inside one long-lived draft PR. It reached only S0/S1/S2:

- S0 — checkpoint docs, byte-frozen v6 oracle, witness corpus, migration ledger;
- S1 — pure primitive parsers (accepted);
- S2 — typed structural records / lifetimes / participant table (implemented,
  awaiting review, never accepted).

S3–S6 (the dormant v7 authority core) and S7 (the cutover review) were never
started. Implementation stopped before S3.

The architecture recovery review then returned:

- **NO-GO on B-S1 as the mutation-authority path**;
- **NO-GO on continuing PR #53's architecture**;
- Blocker B remains open, but should not automatically sit on the critical path
  for the practical operator-driven product.

## 5. The durable facts

| Fact | Value |
| --- | --- |
| PR #52 merged? | **No.** Frozen historical branch. |
| PR #53 merged? | **No.** Stopped historical branch. |
| Experiment #13 executed? | **Never.** Zero live PVE contact in either branch. |
| Combined research apparatus | ~25,575 inserted lines across the two branches |
| Did B-S1 become a trusted mechanism? | **No.** It never advanced past "plausible candidate / not proven". |
| Did any of this close Blocker B? | **No.** Blocker B remains **OPEN**. |
| Did any of this grant `trusted`? | **No.** Granted nowhere. |
| Did any of this change an ACCEPTED ADR? | **No.** ADR 0005 and ADR 0006 are unchanged. |
| Did any of this change production code? | **No.** Both branches touched only research docs, research scripts, and their tests. |

The two branches remain in Git and are the complete archive of the work. Nothing
from them was merged to `main`; nothing needs to be rolled back.

## 6. Why the mechanism is off the ordinary product path

The practical product target (`docs/product-intent.md`) is: autodiscovery →
dynamic inventory → dynamic Home Assistant view → **read-only** package/update
scanning → explicit operator approval → job-owned snapshot → update → health
check → same-job rollback.

None of that requires proving that occupant A and occupant B at the same VMID
are the same incarnation across an unobserved gap. It requires:

- current presence, which discovery already establishes;
- an approval that is bound to a plan the operator just looked at;
- a snapshot created **by the same job**, moments before the change, on the
  **actual current guest**.

The thing Blocker B protects against — silently transferring *long-lived
destructive authority* from a destroyed occupant to its replacement — stays a
real constraint. But a same-job, minutes-long, freshly-approved workflow does not
depend on long-lived continuity in the way a persistent trusted-enrollment model
does. Whether that distinction is architecturally sufficient is a **NEW
ARCHITECTURAL DECISION** requiring its own ADR; this postmortem does not make it.

## 7. Lessons

1. **A negative-proof mechanism needs an upstream completeness contract before it
   needs a harness.** B-S1's load-bearing claim was "we can prove no event was
   missed". The upstream properties that claim depends on — immutable pagination
   cursors, an observable active→archive handoff, a machine-observable rotation
   boundary, interval-wide reader visibility — were listed as UNKNOWN from the
   very first feasibility pass and were still UNKNOWN when ~25,700 lines had been
   written. **Resolve the load-bearing unknowns before building apparatus to
   measure them.**
2. **Absence of evidence in a mutable, rotating log is not evidence of absence.**
   Every design iteration had to re-learn that a coverage gap is not positive
   evidence. Fail-closed state machines improve honesty; they cannot manufacture
   the missing upstream guarantee.
3. **Review convergence is a signal.** When independent adversarial reviews keep
   finding *new* P1 false-result classes in the same artifact, the artifact's
   structure — not the reviewers — is the problem. Rebuilding was the right call;
   what was missed is that the rebuild inherited the same unproven premise.
4. **Bound research by an execution gate.** The experiment that would have
   falsified the premise was never run, while the apparatus to analyze its output
   grew past 25,000 lines. Cheap falsification should come first, not last.
5. **Keep research physically separate from the product path.** This is the one
   thing that went right: neither branch touched production code, so stopping
   cost nothing but the research effort itself. Preserve that boundary.
6. **Do not let an open blocker expand its own scope.** Blocker B legitimately
   blocks *persistent trusted enrollment*. It was allowed to become a blanket
   blocker on ordinary product work that does not depend on it.

## 8. Where the material is

- Superseded research documents (merged to `main` before this cleanup):
  `docs/archive/blocker-b-family-b/`.
- PR #52's own research document and v6 analyzer: only on branch
  `research/family-b-13-preexecution-harness` at
  `56723770b5edb3a574a16c9b73d2ad5f668d903c`.
- PR #53's own checkpoint document, v7 S0–S2 code, oracle, and corpus: only on
  branch `research/family-b-13-authority-core-redesign` at
  `fa87ec55d4b458a2e3257acf7aa06cce05344fc4`.
- Still-active, still-referenced evidence for ACCEPTED ADR 0006:
  `docs/architecture/research/adr0006-workload-continuity-evidence.md` — that
  one was **not** archived and remains where ADR 0006 points at it.

## 9. Current status this postmortem does not change

```text
Blocker B:  OPEN
B-S1:       ABANDONED AS AN IMPLEMENTATION PATH (NO-GO)
WAVE B1:    DEFERRED / NOT AUTHORIZED
Phase 1C:   BLOCKED
R0:         GO / STRICTLY READ-ONLY
trusted:    GRANTED NOWHERE
```
