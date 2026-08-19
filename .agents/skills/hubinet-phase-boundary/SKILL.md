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
2. `docs/architecture/0.5-implementation-status.md` — the current implementation-state map; read it fresh each time rather than trusting a remembered PR number, since it changes as waves merge;
3. relevant accepted architecture:
   - `docs/architecture/0.5-foundation.md`;
   - `docs/architecture/0.5-inventory-model.md`;
   - ADR 0001 for resource identity/incarnation/binding;
   - ADR 0002 for source discovery/reconciliation;
   - ADR 0003/0004/0005 when the question touches source attestation, confirmed removal, or workload continuity.

## Current R0 backend ownership

The R0 read-only runtime (`app/inventory_runtime.py` composition root over
`app.inventory`) is implemented and merged. It already gives the backend +
SQLite durable ownership of:

- source/resource inventory and identity (ADR 0001/0002);
- discovery-run issuance, scheduling, and crash/abandoned-run recovery;
- the authoritative current snapshot/publication that Home Assistant consumes
  over HTTP (`custom_components/hubinet_ops/transport_http.py`);
- freshness/health/expiry materialization where R0 implements it.

This is not a contract/validation-only layer — it is real durable backend
authority already exercised by production code and tests. Do not describe
persistent Phase 1 inventory/discovery authority as absent; it exists.

Two further durable mechanisms exist but are **functionally dormant**: source
attestation (ADR 0003) and confirmed-removal (ADR 0004). R0's own call
surface never invokes either, and ordinary discovery/polling never
automatically attests, removes, or grants trust to a workload. Treat them as
implemented-but-not-yet-authorized-for-this-use, not as nonexistent.

Genuinely **not implemented** (deferred, not merely dormant): stored
policy/effective-capability authority, maintenance approvals/plans/jobs/
locks/audit, mutation authorization, automatic rollback authority, and final
0.5 break-glass design. Workload/resource continuity trust ("Blocker B",
ADR 0005) has accepted architecture but no implementation path yet — no
stock-PVE evidence is sufficient to grant `security_continuity=trusted`.

Always confirm exact current status against
`docs/architecture/0.5-implementation-status.md` before classifying a
specific mechanism; the summary above can go stale as waves merge.

## Phase 0 / HA may own

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

These are backend/SQLite responsibilities, not HA's. Some are already
fulfilled by the current R0 backend (see "Current R0 backend ownership"
above: reconciliation, identity allocation, discovery-run issuance/CAS,
revision allocation, freshness materialization); others remain genuinely
deferred (policy, jobs/locks/audit, mutation authorization, break-glass).
Either way, HA must consume the result from the published snapshot, never
reimplement the responsibility locally — unless a later ACCEPTED
architecture change says otherwise.

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
- `BACKEND OWNED (R0)` — the responsibility already exists as durable backend
  authority (see "Current R0 backend ownership" above); HA must consume it
  from the published snapshot, not reimplement it locally;
- `DEFERRED / DORMANT BACKEND OWNER` — needs durable backend history,
  transactions, scheduling, or authority that either doesn't exist yet
  (e.g. policy/jobs/mutation) or exists but is dormant/not authorized for
  this use (e.g. source attestation, confirmed removal invoked outside R0's
  own call surface);
- `NEW ARCHITECTURAL DECISION` — ownership/semantics are not accepted yet.

State which accepted document/status section supports the classification.

Do not implement a `BACKEND OWNED (R0)` or `DEFERRED / DORMANT BACKEND OWNER`
requirement in HA merely because it is easy to code.
