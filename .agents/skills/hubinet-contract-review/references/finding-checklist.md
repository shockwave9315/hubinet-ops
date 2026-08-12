# Hubinet Ops bounded finding checklist

Use this checklist only after a concrete finding or when the user explicitly requests a broad contract review. It is a reminder of sibling dimensions, not a source of new architecture.

## Canonical input boundary

For a field used in security, identity, routing, revisions, or permission checks:

- Is the runtime type actually enforced, not only annotated?
- Is normalization intentionally allowed or must malformed wire text fail closed?
- Are enum members canonical enum instances?
- Are UUID identities canonical lower-case hyphenated non-NIL UUID text where the accepted contract requires it?
- Are integer revisions/sequences truly integers rather than truthy bools or strings?
- Are caller-owned mutable containers defensively frozen before publication?

Do not broaden this into "validate every annotation" unless the reviewed field family requires it.

## One published snapshot

Check only accepted same-view invariants relevant to the changed family:

- source/node/resource identities are unique in their required namespaces;
- references point to retained objects in the same source/view;
- at most one current occupant owns a source-local VMID slot;
- retained locator generations obey the accepted retained-history rules;
- terminal/non-current resources cannot expose active authority;
- presence/detail/node-availability axes form a canonical combination;
- normal policy applicability/capabilities are fail-closed;
- fresh/current facts have exact accepted source provenance;
- arbitrary facts/policy/state are immutable and diagnostics-safe.

## Cross-snapshot successor

For an existing retained identity, ask whether the new view illegally changes:

- immutable source/resource/node ownership;
- provider/type/locator/generation identity facts;
- active binding ownership;
- terminal state;
- previously observed security lower bounds;
- monotonic revisions/sequences;
- same-token immutable outcome/provenance;
- exact committed/freshness context;
- resource continuity fencing for security-relevant transitions;
- previously observed finalized-run facts.

Never assume the next numeric revision represents the next backend transaction.

## Source run provenance

Keep these concepts distinct:

- issued run;
- completed run;
- applied health outcome;
- committed reconciliation run;
- current source context;
- committed source context;
- derived time expiry.

Accepted order is a partial order, not adjacency.

When one torn-success case appears, inspect the sibling successful completion/health/commit relations before reporting.

When one retroactive-commit case appears, inspect the entire range of runs already observed as finalized, not one run number.

## Resource authority

Keep separate:

- `presence`;
- `lifecycle`;
- `observational_continuity`;
- `security_continuity`;
- `state_level`;
- retained policy;
- effective/applicable policy;
- effective capabilities;
- `resource_continuity_revision`.

Autodiscovery never grants management authority.

A state-level/security/continuity decision that changes mutation eligibility must obey the accepted fencing contract.

Break-glass is not a shortcut through normal policy applicability.

## HA presentation

HA may present backend-owned state but must not derive authority.

Check that:

- registry identity uses backend/resource UUIDs, not VMID/name/node;
- migration changes topology without changing resource identity;
- absent/terminal resources remain retained but unavailable as required;
- source/detail/node failure overlays fail closed without synthesizing resource presence;
- coordinator rejects invalid snapshots before publishing callbacks/registry writes.

## Diagnostics

For arbitrary repository-controlled mappings:

- secrets must be redacted recursively;
- key normalization must cover accepted naming styles;
- credential/header containers should fail closed as containers when the repository rule requires it;
- negative controls must prove ordinary metadata is not over-redacted.

Do not implement arbitrary secret-value scanning without a concrete accepted requirement.

## Final classification

For each candidate finding choose exactly one:

- `P1/P2 IMPLEMENTATION BLOCKER` — concrete violation of accepted contract at this layer;
- `P3/NON-BLOCKER` — quality or maintainability issue without current correctness/security violation;
- `NEW ARCHITECTURAL DECISION` — sensible idea not yet accepted;
- `PHASE 1 / DEFERRED` — requires durable backend ownership/history;
- `NOT A BUG` — contradicts accepted polling/phase semantics.
