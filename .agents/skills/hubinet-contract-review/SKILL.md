---
name: hubinet-contract-review
description: Review Hubinet Ops 0.5 contract code, PRs, snapshots, provenance, identity, HA presentation, policy/capability or focused review findings. Use for api.py/contract code, coordinator, sensor, diagnostics, source/resource transitions, and PR review; do not use to invent architecture or implement deferred Phase 1 ownership.
---

# Hubinet Ops contract review

This is a review procedure, not a source of architecture truth.

## Mandatory orientation

Before reviewing significant 0.5 code:

1. Read `AGENTS.md`.
2. Read `docs/architecture/0.5-implementation-status.md` to learn the current merged baseline, implemented/deferred boundaries, known debt, and next phase.
3. Read the accepted architecture relevant to the review:
   - `docs/architecture/0.5-foundation.md`;
   - `docs/architecture/0.5-inventory-model.md`;
   - `docs/architecture/adr/0001-resource-identity-incarnation.md` for identity/incarnation/binding work;
   - `docs/architecture/adr/0002-proxmox-discovery-reconciliation.md` for discovery/source/reconciliation work.
4. Read only the production code and tests needed for the requested review surface before expanding scope.

Authority order:

```text
explicit user-approved architecture change
> ACCEPTED ADRs / accepted architecture model
> AGENTS.md repository rules
> implementation-status current-state map
> implementation and tests
```

The implementation-status document describes what exists; it cannot override an ACCEPTED ADR.

## Choose review mode

Determine the mode before reviewing:

- **Broad review**: only when explicitly requested or genuinely new evidence invalidates the scope of the last broad audit.
- **Targeted review**: only the named finding, exact diff, subsystem, or bounded family plus the minimum surrounding code required to prove it.

A targeted follow-up must not restart a broad audit because nearby code is interesting.

## Finding standard

Report a P1/P2 implementation blocker only when all are true:

1. There is a concrete reachable witness or malformed accepted publication.
2. It violates an accepted invariant, security boundary, or explicit user requirement.
3. That invariant is enforceable at the reviewed layer.
4. The fix does not require inventing skipped history or a new architecture decision.

For each blocker state:

- severity and title;
- exact witness;
- violated accepted contract;
- why current behavior accepts it;
- smallest safe **family-level** invariant;
- negative matrix;
- positive controls preserving legal behavior;
- Phase 0/HA versus Phase 1/backend ownership.

If the concern needs a rule not already accepted, label it:

`NEW ARCHITECTURAL DECISION`

Do not quietly turn a plausible design idea into a blocker.

## Polling and history semantics

HA polling may skip arbitrary published revisions.

Never infer backend transaction adjacency from:

- `published_state_revision == previous + 1`;
- numeric run adjacency;
- wall-clock proximity;
- absence of an intermediate refresh.

Phase 0 may preserve permanent facts that an earlier accepted snapshot actually established. Examples include immutable ownership, terminal state, monotonic lower bounds, or a specific run token already observed finalized with fixed provenance.

Phase 0 must not reconstruct transient history it never observed. Examples include whether an intermediate trust/enrollment state existed, exact transaction adjacency, unseen run outcomes, or unseen handoff state.

If correctness requires durable history beyond facts visible in accepted snapshots, classify it as `PHASE 1 / DEFERRED`.

## One witness means one bounded family review

When one real witness is found:

1. Identify the semantic dimensions of that exact bug family.
2. Inspect sibling states that could bypass the same invariant.
3. Express one family-level rule.
4. Prefer table-driven/matrix coverage over a single regression.
5. Include positive controls for allowed polling gaps, audit/failure paths, and monotonic skips where relevant.
6. Stop after the family is closed.

Do not wait for later reviews to rediscover obvious siblings one by one.

Use `references/finding-checklist.md` as a reminder of common sibling dimensions. It is not permission to start a generalized sweep.

## Phase boundary while reviewing

Current status after PR #20 says Phase 0 contract/HA presentation are implemented, while persistent Phase 1 owners are not. Respect that boundary.

HA may validate canonical values, one-view consistency, observable successor contradictions, and permanent facts already observed.

Do not make HA a durable owner of:

- inventory reconciliation;
- run issuance/single-flight/history;
- binding/generation allocation;
- CAS/fencing transactions;
- enrollment/trust/policy authority;
- scheduler-independent freshness expiry;
- plans/jobs/locks/audit or mutation authorization.

If the finding needs one of these, report the Phase 1 requirement instead of adding hidden HA state.

## Review writes

For review-only work, do not write.

For an explicitly authorized fix:

- change only the accepted family invariant and necessary tests;
- preserve legal polling-gap semantics;
- do not combine a finding fix with the known structural `api.py` refactor;
- do not merge, deploy, resolve review threads, or change Draft/Ready unless explicitly authorized;
- after the fix, review the exact diff/family rather than restarting a full audit.

## Stop conditions

Stop and report when:

- the proposed fix conflicts with an ACCEPTED ADR;
- correctness needs a new architecture decision;
- correctness needs deferred Phase 1 durable authority;
- an explicitly expected remote/PR HEAD moved;
- a targeted fix requires unrelated broad refactoring.
