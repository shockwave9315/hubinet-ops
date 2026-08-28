---
name: hubinet-architecture-change
description: Handle Hubinet Ops requests that may change accepted architecture, security boundaries, phase ownership, identity, discovery, mutation paths, break-glass, or persistent data semantics. Use before implementing behavior that is not clearly covered by existing ADRs/status.
---

# Hubinet Ops architecture-change handling

Use this skill when a requested change may alter accepted architecture rather than merely implement it.

## Mandatory orientation

Read:

1. `AGENTS.md`;
2. `docs/architecture/0.5-implementation-status.md`;
3. `docs/architecture/0.5-foundation.md`;
4. `docs/architecture/0.5-inventory-model.md`;
5. the relevant accepted ADR(s).

The implementation-status document tells you what is currently merged, contract-only, deferred, or next. It does not override accepted architecture.

## First question: implementation or architecture?

Classify the requested behavior.

### Existing accepted behavior

If the architecture already defines the behavior and the implementation-status document shows it is not yet implemented:

- identify the owning phase/layer;
- implement only when the task is authorized and prerequisites are met;
- preserve all accepted fail-closed invariants;
- do not reopen settled architecture merely because another implementation would be easier.

### New architectural decision

Treat the request as `NEW ARCHITECTURAL DECISION` when it changes or newly defines topics such as:

- durable identity meaning;
- source/endpoint binding;
- resource incarnation/replacement semantics;
- generation/binding allocation;
- trust/enrollment model;
- policy/capability authority;
- mutation authorization path;
- maintenance approval model;
- rollback/restore authority;
- break-glass/self-host recovery;
- persistence/transaction ownership;
- polling/history guarantees beyond accepted ADRs;
- compatibility/migration policy for the clean-break 0.5 system.

Do not implement first and document later.

## Architecture decision workflow

For a new decision:

1. State the problem and why current accepted architecture does not decide it.
2. State the safety/security properties that must remain true.
3. Identify affected accepted ADR sections and implementation phases.
4. Present concrete options and tradeoffs.
5. Recommend one option without pretending it is already accepted.
6. Record the decision through the repository's architecture process before implementation.
7. Update `0.5-implementation-status.md` only after the architecture decision and implementation state actually change.

Do not edit an ACCEPTED ADR merely to make current code/tests legal unless the user explicitly approves an architecture change.

## Current 0.5 boundaries to preserve

Unless a later accepted decision changes them:

- backend + SQLite own durable inventory/policy/operation state;
- HA is presentation and controlled input, not authority;
- autodiscovery grants no management authority;
- VMID is a locator, not durable identity;
- normal mutation path remains:

```text
HA -> Hubinet Ops API -> backend policy/plans/jobs/locks/audit
   -> typed host-control -> hostd/forced-command -> Proxmox
```

- arbitrary command text is never an accepted mutation interface;
- Phase 0 must not emulate deferred Phase 1 persistence/transactions/history;
- break-glass is separate and must not become a fallback through normal policy/capability paths;
- polling gaps do not prove transaction adjacency.

## Structural refactors

A structural refactor is not automatically an architecture change.

Before starting one, check `docs/architecture/0.5-implementation-status.md`
for whether the specific debt is still current — the previously tracked
`custom_components/hubinet_ops/api.py` split (thin API/client/transport
compatibility facade over `contract/`) has since been completed and is
listed under "COMPLETE"; do not re-plan it or anchor new work to it.

For a genuine current structural refactor:

- preserve public behavior/imports where required;
- move existing rules without strengthening/weakening them;
- keep `validate_snapshot(current)` conceptually separate from `validate_transition(previous, current)`;
- avoid introducing a generic rules DSL/engine merely for elegance;
- keep new semantics/property-test discoveries in a separate follow-up unless a concrete existing bug requires a bounded fix.

## Stop conditions

Stop implementation and request/record an architecture decision when:

- two accepted documents materially conflict;
- ownership of durable state is ambiguous;
- the change would shorten the mutation trust path;
- the change would grant authority from discovery/presentation state;
- the change needs new persistent security history not already designed;
- the proposed implementation would redefine the clean-break 0.5 compatibility model.
