---
name: hubinet-phase-boundary
description: Decide whether Hubinet Ops work belongs in Phase 0 Home Assistant validation/presentation or deferred Phase 1+ backend persistence. Use for polling gaps, reconciliation, run history, CAS/fencing, freshness, enrollment, trust, policy, bindings, SQLite ownership, or authority questions.
---

# Hubinet Ops phase boundary

Use this skill to stop Phase 0 Home Assistant code from quietly becoming a second backend.

This is a procedure. `AGENTS.md` and ACCEPTED architecture remain authoritative.

## Mandatory orientation

Read in this order:

1. `AGENTS.md`;
2. `docs/architecture/0.5-implementation-status.md`;
3. relevant accepted architecture:
   - `docs/architecture/0.5-foundation.md`;
   - `docs/architecture/0.5-inventory-model.md`;
   - ADR 0001 for resource identity/incarnation/binding;
   - ADR 0002 for source discovery/reconciliation.

Current status after PR #20 is explicit: Phase 0 contract and HA presentation exist; persistent Phase 1 inventory/discovery authority does not.

## Phase 0 / HA may own

HA may:

- consume one backend-owned canonical snapshot;
- reject malformed canonical identities, enums, booleans, revisions, and JSON-like values;
- detach/freeze caller-owned published data;
- validate consistency inside one complete published view;
- validate monotonic/immutable successor properties that remain valid across skipped refreshes;
- preserve permanent facts it actually observed;
- fail closed when source freshness, detail availability, node relation, policy applicability, or capabilities do not permit presentation;
- maintain HA registry identity/topology;
- expose backend-computed read-only state and effective capabilities.

## Phase 0 / HA must not own

Do not implement these as HA-local durable authority:

- persistent inventory reconciliation;
- backend/source/resource identity allocation;
- locator-generation or binding allocation;
- transactional handoff/tombstone ownership;
- discovery-run issuance or single-flight ownership;
- persistent per-run history;
- CAS/fencing against concurrent source changes;
- global durable revision allocation;
- scheduler-independent freshness materialization;
- source endpoint activation/source-binding proof;
- enrollment/continuity proof;
- trust authority;
- stored policy authority;
- effective-capability computation authority;
- maintenance approvals/plans/jobs/locks/audit;
- mutation authorization;
- automatic rollback authority;
- final 0.5 break-glass design.

These are Phase 1+ backend/SQLite responsibilities unless a later ACCEPTED architecture change says otherwise.

## Polling-gap test

For a proposed HA invariant ask:

> Could valid backend publications exist between the two snapshots HA observed?

If yes, HA may use only facts that survive arbitrary skipped publications.

Never infer:

```text
revision N -> revision N+1
```

means:

```text
backend transaction A -> immediately following transaction B
```

The same applies to run sequence numbers.

## Permanent observed facts versus transient reconstruction

Usually safe to preserve when accepted by the architecture:

- an ID was already owned by a particular retained object/source;
- a resource was already observed terminal;
- a monotonic revision/sequence lower bound was observed;
- a specific run token was observed finalized with a fixed outcome/provenance;
- immutable provenance for the same token was observed.

Not safe to reconstruct without backend history:

- the first transient state of a resource when publications may have been skipped;
- whether an intermediate trusted/managed state existed;
- exact transaction adjacency;
- unseen source run outcomes;
- unseen binding handoff steps;
- unobserved policy/approval transitions.

## Decision output

Classify ambiguous work as exactly one of:

- `PHASE 0 VALIDATION` — enforceable from current/canonical/permanently observed snapshot facts;
- `PHASE 1 BACKEND OWNER` — needs durable history, transactions, scheduling, or authority;
- `NEW ARCHITECTURAL DECISION` — ownership/semantics are not accepted yet.

State which accepted document/status section supports the classification.

Do not implement a `PHASE 1 BACKEND OWNER` requirement in HA merely because it is easy to code.
