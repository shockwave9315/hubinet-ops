# `docs/architecture/research/` — non-normative research still referenced by an ACCEPTED ADR

This directory holds research that is **not** architecture authority but is
**not** archived either, because an ACCEPTED ADR explicitly points at it.

| File | Classification | Read it when |
| --- | --- | --- |
| `adr0006-workload-continuity-evidence.md` | **SPECIALIZED REFERENCE** — non-normative evidence record for ACCEPTED ADR 0006 (referenced by ADR 0006 §1 and its source ledger) | only when doing continuity-proof (Blocker B) research |

## Reading policy

**Ordinary Phase 1C, update-workflow, package-scanning, inventory, or Home
Assistant work must not read this directory.** It is continuity-proof research
detail. Reading it by default costs context and invites mistaking evidence
labels for decisions.

## Authority

- Non-normative. Where anything here conflicts with an ACCEPTED ADR — including
  ADR 0006's own normative text — the normative architecture wins and this
  directory is what must be corrected.
- Nothing here authorizes implementation, schema, runtime, `hostd`, HTTP, Home
  Assistant, mutation, or enrollment work.
- Nothing here selects a Blocker-B mechanism, closes Blocker B, authorizes
  WAVE B1, or unblocks Phase 1C.
- Evidence labels (`FACT-DOC`, `FACT-SOURCE`, `INFERENCE`, `UNKNOWN`) are
  load-bearing and must not be silently upgraded.

## What is not here

The abandoned Family B / B-S1 task-history research that used to live in this
directory has been archived to `docs/archive/blocker-b-family-b/`. It is
**superseded**, it is referenced by no ADR, and it is not a roadmap. See
`docs/archive/postmortems/blocker-b-family-b-13.md` for why.
