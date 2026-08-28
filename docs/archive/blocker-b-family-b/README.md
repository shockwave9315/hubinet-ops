# Archived — Family B / B-S1 research (Blocker B task-history witness)

**Status: SUPERSEDED RESEARCH. Non-authoritative. Not a roadmap.**

## Do not read these by default

These documents describe an **abandoned** implementation path. They are detailed
and heavily reviewed, which makes them easy to mistake for a plan. They are not.

Read them only if you are explicitly doing continuity-proof (Blocker B)
research, and read the postmortem first:
`docs/archive/postmortems/blocker-b-family-b-13.md`.

## What this research was

An attempt to prove workload continuity — that the occupant of a VMID slot did
not change during an unobserved gap — by proving *complete coverage* of PVE
task/event history. The provisional candidate was named **B-S1**.

## What happened

- B-S1 never advanced past "plausible candidate / not proven".
- Two follow-on research branches (PR #52, PR #53, both **unmerged**) built
  ~25,700 lines of analyzer/harness apparatus.
- **Experiment #13 — the falsification experiment — was never executed.**
- The architecture recovery review returned **NO-GO** on B-S1 as the
  mutation-authority path.
- **Blocker B remains OPEN.** This research did not close it and did not solve
  it.

## Files

| File | What it is |
| --- | --- |
| `blocker-b-family-b-task-witness-feasibility.md` | Research #1 — feasibility of a stateful PVE task witness. Final classification: **UNRESOLVED**, with 11 named load-bearing unknowns. |
| `blocker-b-family-b-current-release-source-contract-audit.md` | Research #2A — version-pinned audit of official PVE source for task-history behavior. |
| `blocker-b-family-b-target-version-reconciliation.md` | Research #2A.1 — reconciliation of #2A against the real target host's exact package versions. |
| `blocker-b-family-b-2b-stateful-sentinel-design.md` | Research #2B — the B-S1 candidate design and its (never executed) falsification plan. |

The two later documents — PR #52's `blocker-b-family-b-13-preexecution-harness.md`
and PR #53's `blocker-b-family-b-13-authority-core-redesign.md` — were never
merged to `main` and remain only on their own branches:

- `research/family-b-13-preexecution-harness` @ `56723770b5edb3a574a16c9b73d2ad5f668d903c`
- `research/family-b-13-authority-core-redesign` @ `fa87ec55d4b458a2e3257acf7aa06cce05344fc4`

## What is NOT archived here

`docs/architecture/research/adr0006-workload-continuity-evidence.md` is **not**
part of this archive. It is the evidence record that ACCEPTED ADR 0006
explicitly references, it remains in the active tree, and it is a specialized
reference rather than abandoned material.

## Authority

Nothing in this directory amends any ADR, authorizes any implementation, grants
`security_continuity=trusted`, closes Blocker B, authorizes WAVE B1, unblocks
Phase 1C, or changes R0's read-only status. Where it conflicts with an ACCEPTED
ADR, the ADR wins.
