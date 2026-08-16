# ADR 0004: confirmed removal — Class-C operator removal authority plus authoritative absence attestation

Status: **PROPOSED**

This ADR is not yet accepted architecture. It does not authorize any schema,
persistence, or runtime implementation by itself. It does not amend ADR 0001,
ADR 0002, or ADR 0003; where it depends on their invariants it cites them and
adds a new, narrower normative layer on top, exactly as ADR 0003 added the
source-attestation layer on top of ADR 0001/0002 without changing either.

## 1. Context / problem

ADR 0001 and ADR 0002 both leave the terminal `confirmed_removed` transition
partially open. They accept the *shape* of the decision — `missing` is never
by itself proof of removal, no polling interval however long substitutes for
authoritative absence proof, and any accepted terminal transition requires
both a positive removal authority class (A, B, or C) and a separately
accepted authoritative absence evidence side (ADR 0002 §"Kiedy dokładnie
wolno ustawić `confirmed_removed`") — but neither ADR designs what that
absence evidence actually *is*, for any of the three classes. ADR 0002 says
so explicitly:

```text
Możliwe przyszłe klasy authoritative absence evidence obejmują oficjalnie
udokumentowany monotonic ACL/config revision pokrywający cały interval,
transactional/linearizable source snapshot albo trusted host/source-side
absence attestation. Żadna nie jest obecnie wybrana ani potwierdzona dla
stockowego PVE polling. Dlatego polling-only automatic confirmed_removed
pozostaje niedostępne.
```

This ADR proposes the decision ADR 0002's Class C row anticipates as needing
to be, in its own words, "separately accepted":

```text
C: operator confirmation | explicit audited operator decision |
   wymagany consistency check |
   osobny accepted proof/attestation; event cursor nie jest wymagany
```

It closes exactly one path — the Class-C explicit-operator path — leaving
Class A (backend-mediated removal) and Class B (trusted event/cursor proof)
exactly as unresolved as ADR 0002 left them (§6, §7 below). It proposes
Blocker A's architecture closure, in the same sense ADR 0003 proposed (and
was later separately accepted as) Blocker C's — this ADR's own acceptance
is a distinct, not-yet-made decision (Status above).

## 2. Scope and non-goals

In scope: the concept, terminology, threat model, exact normative semantics,
concurrency/CAS discipline, and adversarial matrix for the Class-C minimal
confirmed-removal path — the combination of an explicit operator positive
removal decision, an explicit operator authoritative-absence attestation, and
a machine-produced sampled absence witness, and how all three must interact
with every existing accepted revision/epoch/fencing mechanism (ADR 0001's
`resource_continuity_revision`, ADR 0002's `discovery_run_sequence`/
freshness/CAS discipline, ADR 0003's `source_attestation_epoch`).

Explicitly **not** in scope, and not authorized by this ADR:

- any schema, table, column, trigger, or enum-value implementation;
- any bump of the authority schema version (currently `4`, merged on
  `main`; any future bump to `5` is WAVE A1's implementation concern, not
  this ADR's);
- self-acceptance of this ADR by the agent that wrote it;
- Class A (backend-mediated removal) or Class B (trusted event/cursor
  proof) implementation of any kind — both remain exactly as unresolved as
  ADR 0002 left them; see §6/§7/§26;
- any change to production startup, scheduler, HTTP, MQTT, or Home
  Assistant wiring;
- any change to ADR 0001, ADR 0002, or ADR 0003;
- any new destructive/mutation execution, plan, job, or policy authority;
- any weakening of the existing fail-closed posture that `missing for N
  polls` never becomes removal, that observable ambiguity never closes an
  incarnation, or that a `resource_continuity_revision`/CAS mismatch always
  fails closed.

## 3. Existing accepted invariants that remain unchanged

Restated here only as the fixed floor this ADR builds on — none of the
following is re-litigated, weakened, or reinterpreted:

1. VMID is a source-local locator slot `(inventory_source_id, vmid)`, never
   durable resource identity (ADR 0001).
2. `resource_id` is the immutable backend identity of one inventory
   incarnation (ADR 0001).
3. `binding_id` and `locator_generation` are separate retained locator
   provenance; at most one open binding per slot at any time
   (`one_active_binding_per_locator`, ADR 0001/0002, already schema-enforced
   on `main`).
4. Polling-only absence never proves removal (ADR 0002).
5. `missing` for `N` polls never becomes `confirmed_removed`, regardless of
   `N` (ADR 0002, verbatim: "`missing przez N polli` nie wystarcza do
   `confirmed_removed`, niezależnie od N").
6. Time alone never becomes removal proof (ADR 0002).
7. Partial, source-unavailable, `configuration_error`, or invalid discovery
   never produces `missing` or removal transitions (ADR 0002).
8. Boundary-complete polling is observational completeness at sampled
   boundaries, not interval-wide authoritative absence proof (ADR 0002
   §"Boundary consistency nie jest interval-wide proof"):
   `boundary-consistent complete snapshot != authoritative absence proof`.
9. Direct replacement and confirmed removal are different identity
   boundaries: `replacement proof != absence proof`,
   `confirmed_removed != replaced` (ADR 0002).
10. Direct replacement remains exactly as already accepted and implemented
    (ADR 0001/0002; `app/inventory/reconciliation.py::_replace_resource`,
    already merged). This ADR does not touch it.
11. `confirmed_removed` is terminal, per ADR 0001's canonical state matrix:
    old active binding closes; `lifecycle=retired`; `presence=
    confirmed_removed`; `detail_status=not_applicable`;
    `node_availability=not_applicable`; `current_node_id=null`;
    `last_known_node_id` presentation relation may remain;
    `resource_continuity_revision` advances exactly once for the terminal
    decision; prior `security_continuity=trusted` becomes `revoked`; a
    resource never `trusted` retains historical `unverified`; retained
    policy/history remain retained but ineffective (applicability=false,
    destructive capabilities=none, maintenance permission=none); no
    destructive capability remains; audit/tombstone provenance is retained
    (ADR 0001, ADR 0002 §"terminal invariant dla każdego accepted terminal
    transition").
12. Reappearance of the same slot after `confirmed_removed` always creates a
    new `resource_id`, the next `locator_generation`, a new active binding,
    and inherits no trust/policy/approval/job authority (ADR 0001 scenario
    15, ADR 0002 `confirmed_removed + późniejszy powrót locatora → new
    resource_id, nigdy stara incarnation`).
13. Confirmed removal does not itself create a successor (ADR 0001/0002;
    successor creation belongs exclusively to the separate direct-
    replacement transition).
14. Positive removal authority classes A, B, C remain distinct from the
    authoritative-absence side; every class requires both (ADR 0002
    §"Kiedy dokładnie wolno ustawić `confirmed_removed`").
15. Class B remains unavailable absent a separately proven trusted
    contiguous event/cursor contract; ordinary recent PVE task history is
    explicitly insufficient (ADR 0002 §"Reliable event/task proof").
16. Class A requires a future durable typed mutation/job path and is not
    implemented by this ADR or authorized for implementation by it (ADR
    0002 §"Backend-mediated removal"; this repository's mutation/policy/job
    authority remains entirely unimplemented — `docs/architecture/
    0.5-implementation-status.md` "NOT IMPLEMENTED").
17. Source attestation never itself proves workload absence, and grants no
    workload/mutation authority for any resource on that source (ADR 0003
    §28, §26).
18. Source-attestation epoch changes never themselves change resource
    presence, lifecycle, or identity (ADR 0003 §20 negative witness 11,
    §29 negative witness 11) — an epoch bump only ever affects the
    authority-eligibility of epoch-dependent evidence, never discovered-
    inventory facts.
19. `closure_reason=confirmed_removed` is reserved, in the conceptual model
    already published in `0.5-inventory-model.md` §"Terminal history,
    tombstones i retention", exactly for the authoritative-absence path,
    and is explicitly stated not to substitute for `replaced`
    (`0.5-inventory-model.md`: "`closure_reason=confirmed_removed` jest
    zarezerwowane dla ścieżki z authoritative absence proof i nie zastępuje
    `replaced`"). This ADR proposes the proof contract that value has been
    waiting for since that document was accepted; it does not, by its own
    drafting, supply that proof contract until this ADR is itself accepted.
20. `resource_terminations` (already implemented on `main`,
    `app/inventory/store.py`) already carries `reason`, `successor_
    resource_id`, `run_sequence`, and is written exactly once per
    `resource_id` by the existing direct-replacement path. This ADR reuses
    that table's conceptual role for the Class-C terminal decision; see
    §19, §31.

## 4. Terminology

Two authority artifacts must never be treated as interchangeable, and must
never be merged into one operator click producing one undifferentiated
record:

| Term | What it asserts | Who/what produces it | Alone sufficient for `confirmed_removed`? |
| --- | --- | --- | --- |
| **Class-C positive removal authority** | "I am confirming removal of this exact retained resource incarnation" — an operator decision bound to exact identity/binding/generation/revision | explicit, audited, human operator act (§9) | No |
| **Operator authoritative-absence attestation** | "the exact old incarnation is no longer the current occupant of this exact slot, and I am explicitly authorizing terminal closure on that administrative basis" | explicit, audited, human operator act, separate from the above (§10) | No |
| **Sampled absence witness** | "a specific, exact, boundary-complete, fresh, currently-committed discovery run observed this exact slot absent" | machine-produced, by ordinary reconciliation, never by the operator (§11, §12) | No — a consistency check, not proof (ADR 0002 §8) |
| **Terminal transition** | the atomic backend authority decision that actually sets `presence=confirmed_removed` | one atomic backend transaction consuming all three of the above together (§19) | — |

This ADR does not introduce a fourth term for the already-accepted `resource_
terminations`/tombstone record (§3 item 19/20); it reuses that existing
concept as the durable record of the terminal decision's outcome, linked to
the three artifacts above.

## 5. Threat model

This ADR must reason about an operator-facing, high-friction, security-
sensitive administrative action, not a remote attacker on the wire (ADR
0003's threat model already covers transport/attestation attacks
separately and is reused unchanged, §16). The threats specific to this ADR
are:

- an operator, under time pressure or with an ambiguous mental model,
  attempting to confirm removal of a resource that is not actually gone
  (fat-fingered VMID, wrong resource selected, stale UI, resource is only
  ambiguously missing due to an ACL/permission gap — ADR 0002's ACL-ABA
  case);
- an operator confirming removal against a stale or already-superseded
  view of the resource (the resource has since reappeared, been replaced,
  or already been confirmed-removed by a concurrent operator/session —
  "two operator confirmations racing", adversarial row AH);
- an operator confirming removal against a stale or already-superseded
  view of the *source* (a newer discovery run has committed, a controlled
  source/endpoint/canonicalization/transport-trust transition has
  occurred, or the source's attestation epoch has moved — adversarial rows
  M–U);
- an implementation shortcut that treats "N consecutive missing polls" as
  authoritative absence proof, defeating the entire ADR 0002 boundary — the
  explicit reason this ADR exists rather than simply raising a poll-count
  threshold (§8's negative framing, adversarial row AF);
- an implementation shortcut that lets the *positive removal authority*
  half alone (an operator clicking "confirm removal") stand in for the
  *absence attestation* half, or vice versa, silently treating one click as
  both artifacts — the reason §4 insists on two structurally separate
  records (adversarial rows H, I);
- an implementation shortcut that accepts a VMID-only target, letting slot
  reuse or a stale UI silently confirm removal of the *wrong* incarnation
  (adversarial row L);
- a two-step flow where "operator confirmation" is durably accepted first
  and a much later, separately-timed "terminal closure" step consumes it
  under materially different state — reopening exactly the TOCTOU class
  ADR 0002's `discovery_runs`/CAS discipline and ADR 0003's §19a pattern
  already close for every other controlled transition (§22);
- an implementation that lets a later source-attestation epoch bump
  silently resurrect or reopen an already-`confirmed_removed` incarnation,
  or conversely lets stale-epoch evidence silently authorize a *new*
  terminal decision after the epoch has moved on (adversarial rows T, X,
  and the §16/§29 items 6–7);
- a crash between the two separate operator artifacts (removal authority
  recorded, absence attestation not yet recorded, or vice versa) leaving a
  half-authorized, ambiguous state that a later process could misinterpret
  as sufficient (§22 closes this by construction, not by recovery logic).

None of these is a remote-attacker/cryptographic threat class; all of them
are "the backend must not let its own operator-facing authority path be
tricked, raced, or shortcut into an authoritative decision the accepted
evidence does not actually support."

## 6. Why polling is insufficient (recap, made concrete for this ADR)

ADR 0002 already proves this exhaustively (§3 items 4–9 above); this ADR
adds nothing new to that proof, only makes explicit why it forecloses the
naive "just count misses" implementation that a less careful Blocker A
closure might otherwise reach for:

- a boundary-complete baseline is a sampled statement about two points in
  time (before/after the discovery window), never about the interval
  between polls (ADR 0002 §"Boundary consistency nie jest interval-wide
  proof") — a slot could legitimately be destroyed and recreated, or
  temporarily hidden by an ACL flip (`A → B → A`), entirely inside one
  polling gap, indistinguishably from genuine, permanent removal;
- `configuration_error`/`partial`/`source_unavailable`/`invalid` runs never
  produce `missing` at all (ADR 0002 §"Failure modes"), so "N consecutive
  misses" is not even well-defined across a run that failed to observe
  anything;
- no monotonic ACL/config revision or interval-wide consistency proof is
  verified for stock PVE (ADR 0002 §"Nierozstrzygnięte kwestie" #5,
  **UNKNOWN**) — there is no cursor to prove the gap was actually empty,
  only that the two sampled boundaries matched;
- consequently, any implementation of "N missing polls ⇒ confirmed_removed"
  is provably a **fabricated absence proof**, not a discovered one, no
  matter how large `N` is chosen — this is exactly ADR 0002's already-
  accepted conclusion, restated because it is the one design temptation
  this ADR must foreclose explicitly (§28 row AF, §29 item 11).

## 7. Why Class B (trusted event/task-stream proof) remains unavailable

Unchanged from ADR 0002 §"Reliable event/task proof": Class B requires a
positively identified destroy/removal event for the exact slot/occupant
**plus** a continuous, trusted cursor/evidence chain spanning from that
event through to the current authoritative-absence decision, proving no
later re-occupation occurred. Stock PVE's task/event history retention and
completeness contract remains **UNKNOWN** (ADR 0002 §"Nierozstrzygnięte
kwestie" #4); an ordinary "recent tasks" list is explicitly insufficient
(ADR 0002: "Zwykła lista recent tasks nie spełnia tego wymagania"). This ADR
does not attempt to prove that contract, does not implement Class B, and
does not authorize any future package to treat a partial task-history read
as Class B evidence. Class B remains exactly as unavailable as ADR 0002 left
it; see §26.

## 8. Why Class A (backend-mediated removal) is future-only

Class A's positive side is a durable, successful, typed backend removal
operation, previously bound to expected `resource_id`/`binding_id`/
`locator_generation`/`resource_continuity_revision` (ADR 0002
§"Backend-mediated removal"). That operation does not exist: this
repository has no destructive mutation/job/policy authority implemented for
either the 0.4 or the 0.5 identity model beyond the legacy CT101–110
maintenance lifecycle, which is not scoped to `confirmed_removed` and does
not use the 0.5 identity model at all. Class A additionally still requires
its own authoritative-absence side (ADR 0002's table: "event cursor nie jest
wymagany przez positive side" — but the absence side is still separately
required). This ADR does not design that future typed removal operation,
does not authorize it, and does not decide what its absence side would look
like; it only confirms Class A remains exactly future-only, consistent with
`0.5-implementation-status.md`'s "NOT IMPLEMENTED" mutation-authority
section. See §26.

## 9. Class-C positive removal authority — exact semantics

The explicit, audited, human-operator decision:

> "I am confirming removal of this exact retained resource incarnation."

Bound, at the moment the operator issues it, to the exact identity/binding
context the operator was actually looking at — never to a bare VMID:

```text
inventory_source_id
resource_id
active binding_id
locator_generation
resource_continuity_revision            (the exact revision the operator reviewed)
VMID/slot                               (redundant locator provenance only — never
                                          alone sufficient to target the operation, §18)
operator actor
decision timestamp
explicit, bounded, audited reason text
```

This artifact, by itself, is **not** absence evidence. It is a targeting and
intent record: it proves an operator looked at this exact incarnation and
decided it should be considered removed. It says nothing, by itself, about
whether the slot is actually empty. A terminal transition must never be
authorized from this artifact alone (adversarial row H).

## 10. Operator authoritative-absence attestation — exact semantics

A **second**, structurally separate, explicit, audited operator assertion,
issued in the same authority decision as §9 (never a separately-timed
follow-up, §22) but recorded as a distinct artifact:

> - the exact old incarnation is no longer the current occupant of this
>   exact slot;
> - the exact slot is, to the operator's own administrative knowledge,
>   currently intentionally absent;
> - the operator is explicitly authorizing terminal closure of the
>   incarnation on that administrative basis;
> - the operator is not merely acknowledging that one polling result
>   happened to omit the VMID.

**What this attestation honestly is:** an explicit administrative authority
decision — a human accepting responsibility for a factual claim about the
current state of infrastructure they administer, exactly as an operator
enrollment decision in ADR 0003 is a human accepting responsibility for
trusting an asserted anchor value, not a cryptographic proof.

**What this attestation must never be described or implemented as** — this
list is exhaustive and binding on the implementation, mirroring ADR 0003's
own discipline about not overclaiming evidence strength (ADR 0003 §9/§10):

- **not** cryptographic absence proof of any kind;
- **not** PVE event-stream proof (that is Class B, §7, unavailable);
- **not** a linearizable or transactional source-side snapshot proof;
- **not** physical proof that the machine/slot is empty;
- **not** an automatic inference from polling count, polling duration, or
  any machine-computed heuristic — it is never machine-generated, only
  ever a human-issued assertion (adversarial row AF).

## 11. The sampled absence witness — machine consistency evidence

Operator authority (§9, §10) is not permitted to fight current observed
machine reality. The terminal action additionally, and unconditionally,
requires one exact, machine-produced absence witness, itself produced by
the ordinary discovery/reconciliation backend already accepted by ADR 0002
— never by the operator, and never fabricated for the occasion.

Required witness properties, all simultaneously true of the **same** run:

```text
exact inventory_source_id
exact known resource_id / slot (inventory_source_id, vmid)
an authoritative successful committed discovery run
baseline_completeness == complete
boundary consistency valid (ACL topology + effective-permission hashes
  identical BEFORE/AFTER, ADR 0002 §"ACL topology i boundary
  effective-permission proof")
exact required ACL/permission coverage valid for the full guest/node tree
slot absent from that baseline
resource current presence is `missing`
no direct-replacement successor currently occupies the slot
source current health/freshness is fresh
witness belongs to the exact currently committed inventory context for the
  source (§13)
```

**What this witness proves:** "the slot was absent in this one accepted,
boundary-complete, currently-committed observation." Nothing more.

**What this witness explicitly does not prove:** "the slot was absent
throughout the interval leading up to this observation," or "the slot will
remain absent," or anything resembling Class B's cursor/stream continuity
(§7). It is exactly ADR 0002's boundary-consistency guarantee (§3 item 8),
never upgraded by this ADR into interval-wide proof.

The operator absence attestation (§10) supplies the separately accepted
*administrative* authority for the interval this machine witness cannot
itself prove. These two statements — "the sampled point was empty" (§11,
machine) and "I am administratively closing this on that basis" (§10,
human) — must remain structurally separate throughout the implementation,
never merged into a single derived boolean.

## 12. Witness provenance requirement — two retention shapes, not one

Current resource state `presence=missing` alone is **not** sufficient
historical provenance for §11's witness, for a concrete, already-observable
reason: the existing accepted reconciliation behavior (ADR 0002, implemented
in `app/inventory/reconciliation.py`) only writes a new state for a
resource the *first* time it transitions into `missing`; a resource that
remains missing across many subsequent complete-baseline runs is not
re-touched by any of those later runs (no new row, no bumped `updated_at`
or `resource_continuity_revision`, no durable pointer to which of those
later runs also re-confirmed the same absence). This is correct and
unchanged accepted ADR 0002/ADR 0001 behavior — a resource legitimately
sitting `missing`/`quarantined` for an extended, ambiguous period does not
need a security-relevant transition on every poll — but it means the
resource's own row cannot, by itself, answer "which exact currently-
committed run re-observed this slot absent," because a later run might
have committed for entirely unrelated reasons (a different resource
changed) without ever re-confirming *this* resource's absence at all. §13's
exact-current-run rule needs an answer to that question at every Class-C
attempt; this section requires the minimum durable representation that
actually answers it, no more.

This ADR distinguishes two structurally different retention needs, and
requires WAVE A1 to satisfy only what each actually needs — not a single
undifferentiated append-only log:

**A. Current eligible sampled-absence provenance (bounded, mutable-current).**
For each currently-`missing` resource, the backend must durably know the
exact most recent successful, complete-baseline committed run that sampled
that exact slot absent. This provenance must survive restart and must be
updated **atomically with** the successful reconciliation commit that
establishes or reconfirms that current absence (an additive, narrow
extension to existing accepted reconciliation provenance capture — ADR
0002 already requires `discovery_run_sequence` and boundary/completeness
classification per run; this requires that provenance to also be durably
linked to the specific resource it observed absent). It may be represented
as a single **bounded current pointer/record per resource** — updated in
place on each reconfirming run, not appended to indefinitely — rather than
an immutable log growing once per poll for every resource that happens to
stay ambiguously missing for a long time. It is **not** authority by
itself (§11), and reconciliation making no removal decision of its own
remains unchanged (§3 item 4/5/6/7). Required update semantics:

```text
complete successful run observes a known slot absent:
  current sampled-absence pointer for that resource -> this exact run

another later complete successful run again observes the same slot absent:
  current pointer -> the later exact run (overwrites, does not append)

slot becomes present:
  current sampled-absence eligibility is cleared / marked not eligible

source/context/epoch transition (§15/§16):
  the retained pointer value may remain for history/diagnostic display, but
  §13/§15/§16's exact-context/freshness/epoch CAS makes it
  authority-ineligible until a new eligible witness exists under the new
  context — this does not weaken §13's rule; it is the same rule applied
  to a differently-shaped retention record
```

**B. Immutable consumed terminal evidence.** Only when a Class-C decision
actually commits (§19) does the exact witness consumed by that decision
become permanently frozen: §19 step 3 copies/links the exact witness run
identity and exact source/epoch context into the immutable Class-C
evidence records, and §19 step 11 links the same into the terminal
`resource_terminations` record. This is the only place an unbounded,
permanently-retained, one-row-per-terminal-decision artifact is required —
identical in retention cost to every other terminal/audit record this
repository already accepts (§27).

After restart, it must always be possible to answer, without
reconstructing anything from mutable current state or wall-clock: (1) for
a still-`missing`, not-yet-decided resource — "which exact currently
eligible run last sampled this slot absent?" (retention shape A); and (2)
for an already-`confirmed_removed` resource — "which exact discovery run
supplied the sampled absence, under which exact source context and
`source_attestation_epoch`, and which exact Class-C decision consumed it?"
(retention shape B, permanently). Neither answer may ever be re-derived
from `presence=missing` plus the source's current `last_committed_run_
sequence` alone. §13's exact-current-run CAS rule is unweakened by this
distinction — it is satisfied by shape A's bounded pointer exactly as it
would have been by an unbounded log, at a fraction of the retention cost.

## 13. Current-witness fencing — exact CAS rule

The simplest fail-closed concurrency rule, chosen deliberately over any
"probably still valid" heuristic:

```text
at terminal commit time, the absence witness the operator reviewed (§11,
§12) MUST still be the exact currently committed inventory run for that
source:

  reviewed witness discovery_run_sequence == current
  inventory_sources.last_committed_run_sequence

if any later successful inventory run N+1 has committed for the source
before the operator's terminal decision commits, the old confirmation
request is stale and MUST be rejected/repeated against the newest
committed state — never silently re-validated, never "probably still
correct" heuristically accepted
```

The backend must never attempt to reason about whether a newer commit
"probably changes nothing" for this particular resource — that is exactly
the kind of optimistic reconstruction ADR 0001/0002/0003 already forbid for
every other security-sensitive CAS boundary (ADR 0001: "Security-sensitive
identity, binding, revision, freshness, provenance, and trust state must
not be reconstructed optimistically after gaps, failures, races, or
restart," `AGENTS.md`). This gives the same simple anti-race invariant every
other controlled transition in this repository already uses: operator
reviewed exact committed state `N`; authority decision closes exact
committed state `N`; anything else is stale by construction, with no hidden
observation window crossing the decision boundary.

## 14. Active discovery ownership requirement

The terminal confirmation operation must not race an active discovery run
for the same source, mirroring every other controlled source-context
transition already accepted (ADR 0002 §"Transaction boundary i publikacja",
ADR 0003 §20 "Freshness-context participation" step 1). The minimal rule
this ADR adopts:

```text
terminal Class-C confirmation requires no active discovery owner
  (inventory_sources.active_discovery_run_id IS NULL, or equivalent) for
  that inventory_source_id at the authoritative write boundary

if a discovery run is currently active for the source, the operator action
  fails/must be retried after that run reaches a terminal outcome
```

Class-C confirmation must **not** automatically kill, cancel, or fence a
legitimate in-flight discovery run merely to force the terminal transition
to succeed — unlike a controlled source/endpoint/transport/attestation
transition (ADR 0002, ADR 0003 §20 step 1), which is itself a *configuration*
decision entitled to fence an active run, the terminal removal decision is
a one-shot, retryable, non-configuration operator action with no ongoing
state of its own to protect; the simpler and safer rule is simply "wait
and retry," never "invent a queue," per the brief's own explicit
instruction. No scheduler or job-queue mechanism is designed or authorized
by this section.

## 15. Source context prerequisites

At minimum, the current committed witness (§11–§13) and the terminal
decision must agree, exactly, on the entire existing expected-context tuple
ADR 0002/ADR 0003 already define for every other controlled transition:

```text
source_config_revision
active endpoint_id
canonical_transport_locator
canonicalization_contract_version
transport_trust_revision
source_attestation_epoch                (§16)
```

Any controlled source/endpoint/transport/attestation transition occurring
after the witness was produced makes the witness stale under §13's exact-
equality rule (the witness's own `discovery_run_sequence` would no longer
equal `last_committed_run_sequence`, or a subsequent context-transition
would independently have advanced `source_config_revision`/
`transport_trust_revision`/`source_attestation_epoch` without necessarily
producing a new commit yet — either divergence is rejected the same way).
Current source freshness (ADR 0002's existing mutation-freshness contract,
already implemented as `source_is_fresh_for_future_mutation`) must
independently be fresh under that exact context; this ADR adds no new
freshness concept, it only requires the existing one to hold.

## 16. Source-attestation interaction (Blocker C gating)

This ADR makes the conservative decision, for this initial Class-C
authoritative terminal path, that confirmed removal **is**
attestation-gated. The action is security-sensitive and permanently closes
an incarnation partly on the basis of observations attributed to one
logical source trust domain (ADR 0003 §1); it is not appropriate to allow
that closure to proceed against a source whose trust-domain continuity is
itself currently unproven or in dispute.

Required at decision time, using exactly the vocabulary and mechanisms ADR
0003 already defines and WAVE C1 already implements — no new attestation
concept is introduced:

```text
source_attestation_status == attested
relationship_gate == clear
exact current source_attestation_epoch recorded on both new evidence
  records (§9's removal authority, §10's absence attestation)
the committed discovery witness (§11) was finalized under that exact same
  epoch (source_runtime_health.committed_source_attestation_epoch ==
  current source_attestation_epoch, the identical equality ADR 0003 §20
  already requires for ordinary mutation freshness)
```

Consequences, all direct restatements of ADR 0003's already-accepted rules,
never new attestation policy:

1. a `not_yet_attested` source (epoch `0`): ordinary read-only discovery
   remains fully allowed (ADR 0003 §3, §13, §21 unchanged), but confirmed
   removal is blocked until the source is explicitly enrolled;
2. `mismatch_pending_reattestation`: ordinary discovery remains allowed,
   confirmed removal is blocked until the operator explicitly resolves the
   mismatch (ADR 0003 §17);
3. a `source_attestation_epoch` change occurring after the witness (§11)
   was produced makes that witness/any not-yet-committed Class-C evidence
   stale under §13's exact-equality rule, exactly like any other context
   change — no special-case handling beyond the existing rule;
4. a same-anchor reconfirmation (ADR 0003 §16) never bumps the epoch, so it
   never invalidates an otherwise-current, already-eligible witness or
   in-flight Class-C evidence;
5. an accepted anchor change or an explicit revocation (ADR 0003 §16, §20:
   epoch `N → N+1`) makes any not-yet-committed Class-C evidence recorded
   under epoch `N` stale, exactly as ADR 0003 §20's authority-eligibility
   rule already requires for every other epoch-dependent security-sensitive
   evidence artifact (ADR 0003 explicitly names future Blocker A evidence
   as one of the classes that rule already governs, §20, §26 — this ADR is
   simply the first concrete instance of that already-fixed rule being
   exercised).

**Critical, explicit, non-negotiable clarification (mirrors ADR 0003 §20's
own worked witness):** a source-attestation epoch bump occurring **after**
an already-committed terminal `confirmed_removed` decision must **never**
resurrect the retired incarnation, reopen its closed binding, or reset its
terminal state in any way. The epoch-bump authority-eligibility rule (ADR
0003 §20, §29 item 10) governs whether *not-yet-consumed* evidence remains
usable for a *future* decision — it has no retroactive effect on a decision
that has already been atomically committed. Old Class-C evidence remains
retained, immutable, historical audit exactly like any other superseded-
epoch evidence (ADR 0003 §20: "an epoch bump retains all historical
evidence recorded under prior epochs, unchanged, in full audit"); it simply
cannot be cited to authorize a *new* decision after the epoch has moved on.
This ADR does **not** design, and forbids, any "carry-forward
`confirmed_removed`" state — `confirmed_removed` is terminal, full stop
(§3 item 11), independent of anything the source-attestation axis does
afterward.

## 17. Resource/binding/revision preconditions

At minimum, all of the following must hold, atomically re-verified inside
the same authority write transaction as §19 (a pre-check outside that
transaction is not a security boundary, per this repository's established
CAS discipline, ADR 0002):

```text
exact resource_id exists
exact inventory_source_id matches
presence == missing
lifecycle == quarantined
  (already implied by ADR 0001's canonical state matrix — presence=missing
  has no active-lifecycle row — restated here for defense-in-depth, exactly
  like WAVE C1's own defense-in-depth pattern of re-checking derived
  invariants a schema constraint already guarantees)
exact active binding_id still exists and is open
  (valid_to_run_sequence IS NULL — already implied by presence=missing
  under the existing reconciliation contract, restated for defense-in-
  depth)
exact locator_generation matches the operator-reviewed value
exact current resource_continuity_revision matches the operator-reviewed
  value
exact VMID matches the binding/resource provenance the operator reviewed
resource is not already terminal (lifecycle != retired)
no current successor/other active occupant exists for the slot
  (already guaranteed by the existing one_active_binding_per_locator
  unique index — restated for defense-in-depth)
```

## 18. Prohibited transitions

This ADR explicitly forbids the following, none of which is a legitimate
Class-C entry point:

- `present → confirmed_removed` directly from the operator command — the
  resource must already be observationally `missing` (§17); an operator
  cannot declare a currently-observed-present resource removed by fiat,
  because that would let operator assertion override machine observation
  rather than supplement it (§11's witness would be structurally
  impossible to produce for a `present` resource in the first place, since
  it requires the slot to be absent from the boundary-complete baseline);
- `not_current`/`replaced → confirmed_removed` — a resource already
  terminally retired via direct replacement is not a Class-C target; it
  already has its own terminal history and its own successor (§3 items 9,
  13);
- `confirmed_removed → another new terminal decision` — terminal means
  terminal; no second Class-C (or any other) decision may ever target an
  already-`confirmed_removed` `resource_id` (§3 item 11);
- targeting the operation by VMID alone, without the exact `resource_id`/
  `binding_id`/`locator_generation`/`resource_continuity_revision` tuple —
  VMID is redundant locator provenance only (§9), never sufficient by
  itself to identify which exact incarnation is being closed, exactly
  mirroring ADR 0002's already-accepted rule that "Ogólne potwierdzenie
  operatora... nie może wskazywać jedynie VMID bez resource/binding
  context."

## 19. Atomic terminal transition

One atomic backend authority transaction, for WAVE A1 to implement exactly,
with no security-policy decision left open for that future package to
invent. It must, inside one transaction, first re-validate every expected
context captured in §13/§14/§15/§16/§17 by exact CAS, and only then
atomically:

```text
1.  persist an immutable Class-C positive-removal-authority record (§9)
2.  persist an immutable operator authoritative-absence-attestation record
    (§10)
3.  bind both records to:
      - exact resource_id
      - exact active binding_id
      - exact locator_generation
      - exact pre-transition resource_continuity_revision
      - exact witness discovery_run_sequence / witness artifact (§12) —
        this is the **sampled-absence observation** provenance only (§20);
        it is not, and must never be treated as, the timing of the closure
        itself
      - exact source context (§15)
      - exact source_attestation_epoch (§16)
      - exact actor / decision provenance: operator identity, **this
        transaction's own decision timestamp**, and audited reason text —
        this is the **formal authority-closure** provenance (§20); it is
        the only durable record of when and by whom the binding actually
        closed
4.  close the exact active locator binding: `closure_reason=
    'confirmed_removed'` (the value already reserved for this exact path
    in 0.5-inventory-model.md, §3 item 19 — never 'replaced'); the schema's
    existing `valid_to_run_sequence` column is populated with the witness
    run's `discovery_run_sequence` as **retained observation provenance
    only** (§20) — this column must never be read, described, or
    implemented as recording when the binding was formally closed, only as
    recording which exact machine observation the later closure decision
    was based on
5.  the witness run's `discovery_run_sequence` (step 4) is never treated as
    a stand-in for this transaction's own identity: the discovery-run
    namespace is never written to, incremented, or fabricated by this
    transaction (step 14), and any other place the existing binding/
    termination model already requires a run_sequence-shaped value is
    populated with the same witness value under the identical "observation
    provenance only" caveat as step 4 — never described as "this
    transaction's own sequence," because this transaction has none
6.  advance resource_continuity_revision exactly once for this one accepted
    terminal security/continuity decision (ADR 0001's existing rule: one
    atomic decision changing several fields advances the token once, never
    once per field)
7.  set:
      presence = confirmed_removed
      lifecycle = retired
      detail_status = not_applicable
      node_availability = not_applicable
      current_node_id = NULL
8.  preserve last_known_node_id where it was already set (no purge)
9.  preserve the last meaningful observational_continuity value
    (consistent or uncertain) exactly as it stood before the transition;
    never invent 'retired' as an observational_continuity value (ADR 0001
    already forbids this for every terminal transition; restated here as a
    defense-in-depth constraint on this specific path)
10. security_continuity:
      trusted  -> revoked
      previously unverified -> remains unverified
    (identical to the existing terminal invariant, ADR 0001, §3 item 11 —
    this ADR adds no new security_continuity value, exactly as ADR 0003
    §20's own "Representation boundary" already forbids doing for its own
    epoch-bump transition)
11. persist retained termination/tombstone provenance by writing the
    existing single-row-per-resource_id `resource_terminations` record
    (extended as needed for step 3's linkage fields — exact column shape
    is a WAVE A1 implementation detail, §30 item 1) with
    reason='confirmed_removed', successor_resource_id=NULL,
    run_sequence=the witness run's `discovery_run_sequence` (observation
    provenance only, identical caveat as step 4), and durable links to the
    two new evidence records from steps 1-2 — which themselves carry this
    transaction's own actor/timestamp per step 3, i.e. the actual
    authority-closure provenance. `resource_terminations` is the single,
    normative terminal/tombstone owner for this path, exactly as it
    already is for the existing 'replaced' path (§3 item 20); this ADR
    requires WAVE A1 to reuse it and forbids inventing a second, parallel
    tombstone concept
12. make every effective mutation/policy/maintenance capability ineligible
    according to the already-accepted resource terminal contract (ADR
    0001's existing "stored policy = retained, effective/applicable
    destructive policy = false, destructive capabilities = none,
    maintenance permission = none")
13. advance:
      inventory_revision           exactly once, for the inventory-owned
                                    transition (ADR 0002's existing rule:
                                    every reconciliation-class commit
                                    advances this token)
      published_state_revision     exactly once, for the committed
                                    visible change
14. leave source health/freshness itself completely unchanged by the
    terminal decision — the source witness was already independently
    validated as current/fresh by §11-§15's preconditions; this transition
    is not itself a discovery-run commit and must not fabricate one, must
    not advance last_committed_run_sequence, and must not write a new
    source_runtime_health row
15. create NO successor of any kind — Class-C confirmed removal never
    creates a new resource_id, never opens a new binding, and never
    transitions any other resource (§3 item 13, §18)

Everything above commits together, or nothing commits. A partial write
(e.g. binding closed but resource_terminations not yet written) must never
be observable outside the transaction; this repository's existing
`BEGIN IMMEDIATE`-transaction discipline (ADR 0002, already implemented
throughout `app/inventory/store.py`/`authority.py`) is the required
mechanism, not a new one.
```

## 20. Binding closure semantics

**Two distinct provenance moments must never be conflated, and this section
is the ADR's own normative closure decision for how they are represented:**

**A. Sampled-absence observation** — the exact discovery run `N` (§11) that
observed the slot absent. This is machine observation provenance only.

**B. Formal authority closure** — the later, separate, atomic Class-C
operator authority transaction (§19) that actually closes the binding and
actually sets `presence=confirmed_removed`. This is when the closure
*happens*, and who/what authorized it.

These are not the same moment, and — unlike ADR 0002's existing direct-
replacement path, where the binding closure is written by the *same*
reconciliation transaction that owns run `N`'s own `discovery_run_sequence`
(`app/inventory/reconciliation.py::_replace_resource`, a genuinely
contemporaneous write) — this ADR's Class-C path does **not** mirror that
pattern: the closing transaction here is a separate, later, non-discovery-
run transaction with no `discovery_run_sequence` of its own (§19 step 14
forbids fabricating one), reaching back to reuse witness run `N`'s
already-committed, historical sequence value purely as retained
observation provenance.

**Normative representation decision:** the active `resource_locator_
bindings` row is closed exactly once; its existing `valid_to_run_sequence`
column is populated with the witness run's `discovery_run_sequence` (§19
step 4), but that value means **only** "the sampled-absence observation
this closure was based on," never "the run whose own reconciliation
transaction performed this closure." Between run `N`'s commit and the
later Class-C authority commit, the binding remains genuinely open/live in
durable backend state — ADR 0002's already-accepted rule that `missing`/
`quarantined` never itself closes the binding is not violated or
reinterpreted by this ADR; the binding is closed only when, and exactly
when, the Class-C transaction (§19) commits.

Because `valid_to_run_sequence` alone cannot distinguish these two moments
after the fact, WAVE A1 must retain the actual authority-decision
provenance separately and durably: §19 step 3 already requires this
transaction's own actor identity, decision timestamp, and audited reason
to be recorded on the two new evidence records (§9, §10), and §19 step 11
requires `resource_terminations` to durably link to those same records.
No new global sequence and no new `resource_locator_bindings` column are
required for this — the existing evidence/termination records already
carry the actual closure identity/timestamp/actor once §19's requirements
are implemented — but WAVE A1 must not omit that linkage, because without
it, `valid_to_run_sequence=N` alone would let a future reader incorrectly
conclude "binding was formally closed by discovery run `N`" when the true
history is "run `N` observed absence; a later, separate operator decision
`D` closed the binding." After restart, audit must be able to answer both
"which run supplied the sampled absence" (via `valid_to_run_sequence`/the
witness artifact, §12) **and** "which exact decision, by whom, at what
time, actually closed this binding" (via the linked evidence records) as
two separate, non-conflated answers.

`closure_reason='confirmed_removed'` is structurally distinct from
`closure_reason='replaced'` (§3 item 19); a validator/contract test must
reject a `confirmed_removed` closure that coexists with a
`successor_resource_id` on the same terminal record (§18), exactly as the
existing model already separates the two paths.

## 21. Revision semantics

- `resource_continuity_revision`: advances exactly once for the one atomic
  terminal decision (§19 step 6), never once per constituent field change
  within that same decision — identical to every other multi-field
  security-continuity decision ADR 0001 already governs;
- `inventory_revision`: advances exactly once, because this is an
  inventory-owned identity/presence transition, not merely a health/
  context-provenance change (ADR 0002's existing rule distinguishing
  reconciliation-class commits from health-only CAS updates);
- `published_state_revision`: advances exactly once, in the same
  transaction, because the transition changes API-visible published state
  (ADR 0002's existing "every committed change of any API-visible field"
  rule);
- no other durable token this ADR touches (`source_config_revision`,
  `transport_trust_revision`, `source_attestation_epoch`,
  `last_committed_run_sequence`, `last_issued_run_sequence`) is advanced
  by this transition — it consumes their current values as CAS
  preconditions (§13, §15, §16) but is not itself a controlled-context
  transition over any of them (§19 step 14).

## 22. No partial / two-step authority window

This ADR explicitly rejects any design where operator confirmation is
durably accepted first and terminal closure happens later, under
potentially different state — the exact TOCTOU class ADR 0002's
`discovery_runs` issuance/commit pattern and ADR 0003's §19a read-then-write
pattern already exist specifically to close for every other controlled
transition in this repository.

The design this ADR proposes for WAVE A1, if accepted: the Class-C
positive-removal-authority record (§9), the operator absence-attestation
record (§10), and the terminal transition (§19) are **one atomic authority
decision** — a single backend authority-commit transaction that re-validates
every precondition (§13, §14, §15, §16, §17) immediately before writing
anything, and either commits the complete decision or writes nothing at
all. This ADR does not propose a "staged token" alternative for this
initial path; if a future
package genuinely needs a longer-running, multi-step variant, it must
follow ADR 0003 §19a's exact discipline (context captured before any
remote/slow step, re-validated by CAS immediately before the write, no
provisional acceptance recorded in between) — but this ADR does not design
or authorize that variant, because unlike ADR 0003's remote evidence read,
nothing in the Class-C path requires slow I/O: the witness (§11/§12) is
already durable and local, and the operator's two assertions (§9/§10) are
ordinary form input, not a network call. WAVE A1 should therefore prefer
the simpler single-transaction design; UI confirmation dialogs may occur
before the backend call exactly as with any other operator action, but the
backend authority commit itself remains one fail-closed CAS transaction.

## 23. Restart / crash semantics

Because the terminal authority write (§19) is short, local, and touches no
remote I/O of its own (§22):

```text
crash before the write transaction commits
  -> no accepted removal authority record exists
  -> no accepted absence attestation record exists
  -> no terminal transition occurred
  -> the resource remains exactly as it was (missing/quarantined)
  -> the operator must retry the entire decision, re-reviewing current
     state (§13's exact-equality rule makes stale retries fail closed
     anyway)

crash after the write transaction commits
  -> both evidence records, the closed binding, the terminal resource
     state, and the tombstone/termination record are all durably present
     after reopen, exactly together (all-or-nothing per §19)

no issued/running/abandoned job lifecycle is required for this decision —
  unlike discovery_runs, there is no long-running remote phase to fence or
  abandon on restart; the transaction either happened or it did not
```

This ADR explicitly forbids inventing any workflow-engine, job-queue, or
multi-phase issued/running/completed lifecycle for the Class-C decision
itself — that machinery belongs to `discovery_runs` for a genuinely
different reason (unavoidable slow remote I/O between issuance and commit),
which does not apply here.

## 24. Race / CAS semantics

The following races must all be classified fail-closed, using the
already-accepted CAS discipline (ADR 0002/ADR 0003) rather than any
timestamp-based heuristic (this repository already forbids wall-clock as
concurrency authority everywhere else, ADR 0002 §"discovery_run_sequence"):

| # | Race | Classification |
| --- | --- | --- |
| 1 | `resource_continuity_revision` changed after the operator's read | stale — reject, require re-review |
| 2 | active binding changed/closed | stale — reject |
| 3 | `locator_generation` changed | stale — reject |
| 4 | a newer successful discovery run committed for the source | stale — reject (§13 exact-equality) |
| 5 | an active discovery run started for the source | reject/retry after it terminalizes (§14) |
| 6 | `source_config_revision` changed | stale — reject (§15) |
| 7 | active `endpoint_id` changed | stale — reject (§15) |
| 8 | canonical locator/canonicalization version changed | stale — reject (§15) |
| 9 | `transport_trust_revision` changed | stale — reject (§15) |
| 10 | `source_attestation_epoch` changed | stale — reject (§16) |
| 11 | `relationship_gate` became `mismatch_pending_reattestation` | reject (§16 item 2) |
| 12 | source freshness expired before commit | reject (§15, existing freshness CAS) |
| 13 | a newer applicable failed discovery run made the source non-fresh/degraded | reject (§15) |
| 14 | the slot became `present` again before commit | reject — precondition `presence==missing` fails (§17) |
| 15 | a direct replacement happened for the slot before commit | reject — precondition "no successor/already terminal" fails (§17, §18) |
| 16 | the resource is already terminal (a concurrent decision won the race) | reject — precondition `lifecycle != retired` fails (§17, §18) |

No timestamp of any kind is CAS authority for any of the above; every check
is an exact-value comparison against durable, previously-captured expected
context, re-verified inside the single commit transaction (§19), identical
in spirit to every existing `discovery_runs`/attestation CAS boundary in
this repository.

## 25. Reappearance / post-terminal slot reuse — new-incarnation semantics

Explicit, and unchanged from ADR 0001/ADR 0002's already-accepted rule
(§3 item 12), restated here because Class-C is the first concrete path that
actually produces a `confirmed_removed` resource for this rule to apply to:
after confirmed removal, any later boundary-valid discovery of the same
VMID creates a **new** incarnation, unconditionally, even if:

- `resource_type` is identical to the retired incarnation;
- name is identical;
- observed config facts are identical;
- node is identical.

```text
old incarnation:
  resource_id            retained, unchanged
  binding                retained, closed (closure_reason='confirmed_removed')
  locator_generation     retained, unchanged
  presence/lifecycle     confirmed_removed / retired (permanent)
  termination/evidence   retained (§19 steps 1-3, 11)

new incarnation (created by ordinary reconciliation, ADR 0001/0002 —
  this ADR does not change how a new incarnation is created, only confirms
  it applies here identically to every other post-terminal slot reuse):
  resource_id            new, backend-generated
  locator_generation     old_generation + 1
  binding_id             new
  presence               present
  lifecycle              active
  security_continuity    unverified
  state_level            discovered
  inherited policy/trust/approvals/jobs   none
```

`confirmed_removed` is an accepted incarnation boundary exactly like direct
replacement is (§3 item 9) — the only two boundaries at which a new,
current `resource_id` may legitimately appear for an already-known slot.

## 26. Positive authority Class A / Class B — explicit non-implementation

To close the risk of this ADR being read as implementing more than it does:

**Class A** (backend-mediated removal, ADR 0002 §"Backend-mediated
removal") retains its accepted conceptual definition — a future successful
typed backend-mediated removal operation — and remains unavailable until
the mutation/job authority this repository does not yet have is designed
and accepted. Its positive side does not itself require PVE cursor
continuity (ADR 0002's table), but it still independently needs its own
accepted authoritative-absence side; this ADR does not decide what that
would be, and does not authorize treating Class-C's absence-attestation
design (§10) as automatically reusable for Class A without its own review.

**Class B** (trusted contiguous destroy/removal event/cursor chain, ADR
0002 §"Reliable event/task proof") retains its accepted conceptual
definition and remains unavailable for stock PVE until a contiguous,
retained, trusted event/cursor-stream contract is independently proven for
the supported PVE version (§7). Ordinary recent task history is not
sufficient (ADR 0002, restated).

This ADR's first implementation package (WAVE A1) implements **only** the
Class-C operator path (§9–§24). Neither Class A nor Class B becomes
implemented, available, or partially available as a side effect of this
ADR's acceptance.

## 27. Retention / audit semantics

All three artifacts introduced by this ADR — the sampled absence witness
(§11/§12), the Class-C positive-removal-authority record (§9), and the
operator absence-attestation record (§10) — are immutable once written and
retained indefinitely, exactly like every other accepted audit/evidence
class in this repository (ADR 0001's terminal/tombstone retention, ADR
0002's `discovery_runs` completion audit, ADR 0003's `source_attestation_
events`). No purge/retention policy is designed or authorized by this ADR;
`0.5-inventory-model.md`'s existing statement that "Purge source, tombstone
albo HA device nie jest automatycznym skutkiem discovery" applies
identically to these new artifacts. Credentials, secrets, and raw
authentication headers must never be copied into any of these evidence
records, mirroring the existing repository-wide rule (`AGENTS.md`).

## 28. Adversarial matrix

`allowed` means the terminal transition (§19) is permitted to proceed once
all other stated preconditions independently hold; `rejected` means the
scenario alone blocks the transition regardless of any other precondition.
"Identity effect" / "binding effect" / "revision effect" describe the
concrete consequence for the resource in question; "terminal?" states
whether `presence` becomes `confirmed_removed` in that scenario.

| # | Scenario | Allowed / rejected | Reason | Identity effect | Binding effect | Revision effect | Terminal? |
| --- | --- | --- | --- | --- | --- | --- | --- |
| A | Resource missing once | rejected (alone) | one sampled observation is not, by itself, the required combination of both operator artifacts + fresh exact-current witness (§9-§11 all required together) | none | unchanged (open) | unchanged | No |
| B | Resource missing 100 consecutive polls | rejected (alone) | poll count is never authority (§6, §29 item 11) | none | unchanged | unchanged | No |
| C | Resource missing for a month | rejected (alone) | duration is never authority (§6, §3 item 6) | none | unchanged | unchanged | No |
| D | Partial baseline says missing | rejected | `baseline_completeness != complete` invalidates the witness precondition (§11) | none | unchanged | unchanged | No |
| E | Source unavailable | rejected | witness requires current source health/freshness fresh (§11, §15) | none | unchanged | unchanged | No |
| F | ACL-filtered view (silent `NoAccess`) | rejected | boundary consistency/permission-coverage precondition fails or is unprovable (§11, ADR 0002 ACL-ABA) | none | unchanged | unchanged | No |
| G | Boundary-complete fresh sampled absence only, no operator artifacts | rejected | witness alone is a consistency check, never proof (§11); both operator artifacts still required | none | unchanged | unchanged | No |
| H | Valid Class-C removal confirmation (§9) without absence attestation (§10) | rejected | §4/§9: positive removal authority alone is never absence evidence | none | unchanged | unchanged | No |
| I | Operator absence assertion (§10) without positive removal authority (§9) | rejected | §4/§10: absence attestation alone never identifies which exact incarnation to close | none | unchanged | unchanged | No |
| J | Both operator assertions present, but no fresh complete witness | rejected | §11 witness is unconditionally required alongside both operator artifacts | none | unchanged | unchanged | No |
| K | Both operator assertions + valid exact fresh witness, all preconditions hold | allowed | full §9-§19 contract satisfied | new terminal state; `resource_id` retained, immutable | closed, `closure_reason='confirmed_removed'` | `resource_continuity_revision`+1 once; `inventory_revision`+1; `published_state_revision`+1 | Yes |
| L | VMID-only operator confirmation (no resource_id/binding/generation/revision) | rejected | §9, §18: VMID alone is never a sufficient target | none | unchanged | unchanged | No |
| M | Stale resource_continuity_revision (changed since operator review) | rejected | §17, §24 row 1 | none | unchanged | unchanged | No |
| N | Stale binding_id (closed/changed since review) | rejected | §17, §24 row 2 | none | unchanged | unchanged | No |
| O | Stale `locator_generation` | rejected | §17, §24 row 3 | none | unchanged | unchanged | No |
| P | Newer successful discovery committed before click/commit | rejected | §13, §24 row 4 — exact-equality CAS fails | none | unchanged | unchanged | No |
| Q | Active discovery in flight for the source at commit time | rejected/retry | §14, §24 row 5 | none | unchanged | unchanged | No |
| R | Source freshness expires before commit | rejected | §15, §24 row 12 | none | unchanged | unchanged | No |
| R2 | Sampled absence witness `N` exists, no newer *successful* inventory commit occurs, but a newer applicable failed/partial/source_unavailable/invalid discovery outcome makes current source health non-fresh/degraded before the Class-C decision | rejected | §15's current-freshness prerequisite fails even though `last_committed_run_sequence` still equals `N` (§24 row 13) — a non-committing outcome does not advance `last_committed_run_sequence` so §13's exact-equality check alone would not catch it; §15's independent freshness requirement does | none | unchanged | unchanged | No |
| S | Source config/transport-trust/canonicalization changes before commit | rejected | §15, §24 rows 6-9 | none | unchanged | unchanged | No |
| T | Source attestation epoch changes before commit | rejected | §16 item 3, §24 row 10 | none | unchanged | unchanged | No |
| U | Source attestation `mismatch_pending_reattestation` gate pending | rejected | §16 item 2, §24 row 11 | none | unchanged | unchanged | No |
| V | Same-anchor source re-attestation occurs before commit | allowed (no invalidation) | §16 item 4: epoch unchanged, witness/evidence remain eligible | unaffected by this event | unaffected | unaffected by this event alone | depends on other rows only |
| W | Slot reappears (`present`) before confirmation commits | rejected | §17 precondition `presence==missing` fails; §24 row 14 | resource remains its existing incarnation, unaffected | unchanged (open) | unchanged | No |
| X | Slot reappears after `confirmed_removed` already committed | allowed, but as a **new** incarnation | §25: reappearance after terminal always creates new `resource_id`/generation, never reopens the old one | new `resource_id`; old retained/terminal, unaffected | new binding; old binding remains closed, unaffected | new resource starts its own initial revision; old resource's revision is never touched again | Old: already Yes (unaffected); new: No (present/active) |
| Y | Identical type/name/config reappears after `confirmed_removed` | allowed, but as a **new** incarnation | §25 — identical facts never resurrect the old `resource_id` | same as row X | same as row X | same as row X | same as row X |
| Z | Direct-replacement current occupant already exists for the slot | rejected | §17 precondition "no successor/other active occupant"; the resource is not even in `missing/quarantined` in this scenario — it is `not_current/retired/replaced` (§18) | none for the old resource; successor already has its own independent identity from the replacement path | unchanged by this ADR | unchanged by this ADR | No (this ADR never applies here at all) |
| AA | Backend crash before atomic commit (§19) | rejected — no partial acceptance | §22, §23 "crash before commit" | none | unchanged | unchanged | No |
| AB | Backend crash after commit | already committed, unaffected by the crash | §23 "crash after commit" — all-or-nothing durability | new terminal state retained after reopen | closed, retained after reopen | advanced, retained after reopen | Yes (unaffected by the crash) |
| AC | Backend DB reopen/restart (no crash involved) | unaffected | §23; no job/workflow lifecycle exists to recover for this decision | unchanged from last committed state | unchanged | unchanged | unchanged from before restart |
| AD | Replay of old Class-C evidence (an already-committed removal/absence record resubmitted) | rejected | the resource is already terminal; §17 precondition `lifecycle != retired` fails; §18 forbids a second terminal decision | none (already terminal) | none (already closed) | none | No (already Yes from the original commit; replay itself does nothing) |
| AE | Attempt to use old-epoch evidence after an epoch bump | rejected | §16 item 3/5, §24 row 10 | none | unchanged | unchanged | No |
| AF | Attempt to auto-confirm removal purely from polling (no operator artifacts at all) | rejected — this path does not exist | §6, §10 "never an automatic inference from polling," §29 item 11 | none | unchanged | unchanged | No |
| AG | Attempt to derive successor trust/policy from the just-removed old resource | rejected — not applicable; no successor is created by this ADR at all | §19 step 15, §3 item 13 | n/a — no successor exists from this transition | n/a | n/a | Yes for old only; no successor exists to inherit anything |
| AH | Two operator confirmations racing for the same resource | exactly one wins, one rejected | §17 precondition `lifecycle != retired` fails for the second commit inside the same CAS transaction discipline as row AD | winner: new terminal state; loser: none | winner: closed; loser: unchanged (already closed by winner) | winner: advanced once; loser: none | Yes (once, by the winner only) |

## 29. Open questions closed (normative)

The following are fixed decisions of this ADR, not implementation choices
left to WAVE A1:

1. Is Class-C confirmed removal attestation-gated? **YES** (§16).
2. Must the source be currently attested? **YES** (§16 item 1 of the
   prerequisite list).
3. Must `relationship_gate` be clear? **YES** (§16).
4. Must the exact `source_attestation_epoch` be recorded on both new
   evidence records? **YES** (§16, §19 step 3).
5. Does a same-anchor reconfirmation invalidate otherwise-eligible
   evidence? **NO** — epoch is unchanged (§16 item 4).
6. Does an epoch bump invalidate not-yet-committed Class-C evidence?
   **YES** (§16 item 5).
7. Does an epoch bump reopen an already-`confirmed_removed` resource?
   **NO** (§16, explicit clarification paragraph).
8. Must current source inventory be fresh? **YES** (§15).
9. Must the sampled absence witness be the exact latest committed run?
   **YES** (§13).
10. May there be an active discovery run during the terminal commit?
    **NO** (§14).
11. Does `N` polling misses ever substitute for operator authority?
    **NO** (§6, §28 row AF).
12. Does the operator action create a successor? **NO** (§19 step 15, §3
    item 13).
13. Does reappearance after confirmed removal preserve `resource_id`?
    **NO** (§25).
14. Do Class A or Class B become implemented by this ADR? **NO** (§26).
15. Can the Class-C action be based on VMID only? **NO** (§18).
16. Can source attestation itself prove absence? **NO** (ADR 0003 §28,
    restated at §3 item 17 — source attestation is never cited as any part
    of the absence evidence in §11; it is only ever a gating precondition
    on the *source trust domain*, §16).

## 30. What remains unresolved after this ADR

Limited strictly to items genuinely outside the Class-C implementation path
this ADR closes:

1. Exact schema (table/column/enum names) for the three new durable record
   concepts (§4, §31) — an implementation-package choice, not decided here,
   mirroring ADR 0003 §30 item 3's identical posture toward its own
   attestation schema.
2. Exact operator UX/audit-trail presentation for the Class-C confirmation
   flow (the two-artifact form, the witness display, the failure/stale-CAS
   messaging) — implementation-package UX work, not an architecture
   decision.
3. Long-term retention/purge policy for superseded-epoch or very old
   terminal evidence — explicitly out of scope (§27), mirroring ADR 0003
   §30 item 6's identical open item for attestation evidence.
4. Whether a future Class A or Class B design would be able to reuse this
   ADR's absence-attestation concept (§10) for their own authoritative-
   absence side, or would need their own — left entirely to those future
   ADRs (§26); this ADR neither authorizes nor forecloses that reuse.
5. Whether ongoing/continuous re-validation of source attestation (ADR
   0003 §30 item 1, itself already unresolved) should ever become a
   prerequisite for Class-C specifically, beyond the point-in-time gating
   this ADR already requires (§16) — not decided here, inherits ADR 0003's
   own open status on that question unchanged.

No genuine contradiction with ADR 0001, ADR 0002, or ADR 0003 was found
while drafting this ADR. Every normative rule above is either a direct
restatement of an already-accepted invariant (§3) or a new, narrower rule
that applies only to the new Class-C evidence/decision this ADR itself
introduces, never a reinterpretation of an existing accepted term
(`resource_continuity_revision`, `locator_generation`, `binding_id`,
`source_attestation_epoch`, `discovery_run_sequence`, `presence`,
`lifecycle`, `security_continuity`, `observational_continuity` all keep
their exact ADR 0001/0002/0003 meanings throughout).

## 31. Implementation consequences for WAVE A1 (not implemented here)

A future, separately reviewed and separately accepted implementation
package would need to add, at minimum — this ADR fixes the security-policy
decisions below so that package has none left to invent:

- an authority schema version bump, most likely `v4 -> v5` (the merged `v4`
  marker/objects would be rejected without migration, exactly like `v1`/
  `v2`/`v3` before them, per the existing established schema-versioning
  policy — **no automatic v4 -> v5 migration is authorized by this ADR**;
  0.5 remains entirely dormant; old `v4` databases may be rejected fail-
  closed by WAVE A1 exactly as `v1`/`v2`/`v3` already are; production 0.4
  data must never be touched or auto-migrated by any part of this work);
- a bounded, current, per-resource sampled-absence provenance pointer,
  produced/updated by ordinary complete-baseline reconciliation itself
  (extending, not replacing, the existing accepted `missing` transition —
  §12 retention shape A), overwritten on each reconfirming run rather than
  appended to indefinitely, queryable after restart without reconstructing
  anything from mutable current state;
- an immutable Class-C positive-removal-authority record (§9), structurally
  separate from the below, carrying this transaction's own actor/decision
  timestamp/reason (§19 step 3, §20);
- an immutable operator absence-attestation record (§10), structurally
  separate from the above — this ADR requires two separate durable record
  concepts, never one merged "operator confirmed removal" row (§4, §28 rows
  H/I);
- at Class-C commit time only, the exact witness consumed (run identity,
  source/epoch context) becomes permanently frozen/linked into those two
  immutable evidence records (§12 retention shape B) — this permanent,
  one-row-per-terminal-decision linkage is the only unbounded retention
  this ADR requires;
- retained linkage from both new evidence records to the existing
  `resource_terminations`/tombstone record, which this ADR **normatively
  requires** WAVE A1 to reuse (not merely permits it to reuse) as the
  single terminal/tombstone owner for this path, extending its existing
  shape (`reason`, `successor_resource_id`, `run_sequence`) as needed for
  the new linkage fields rather than inventing a parallel tombstone concept
  (§19 step 11, §20, §3 items 19-20); `resource_terminations.run_sequence`
  is populated with the witness run's sequence under the identical
  "observation provenance only, never closure-timing" caveat as the
  binding's own `valid_to_run_sequence` (§20) — the actual closure identity/
  timestamp/actor lives on the linked evidence records, never implied by
  `run_sequence` alone;
- an explicit `InventoryAuthority` Class-C confirmation operation, taking
  the operator's targeting/reason input and performing the exact atomic
  transition of §19 in one `BEGIN IMMEDIATE` transaction, following the
  exact source/resource/binding/revision/run/epoch CAS discipline of
  §13-§17/§24, with no remote I/O of its own (§22-§23);
- exact §19-step semantics for binding closure (§20) and revision
  advancement (§21);
- explicit handling, exercised by the reconciler's existing, unmodified
  code path (no change to reconciliation's own missing/replacement logic
  required beyond §12's witness-capture extension), of later-slot-
  reappearance as a brand-new incarnation (§25) — this ADR requires no new
  reconciliation branch beyond what ADR 0001/0002 already define for
  "unknown VMID observed" (the existing `_create_resource` path already
  does the right thing once the old binding is closed and the old resource
  is terminal; WAVE A1 only needs contract tests proving this, not new
  reconciliation code);
- restart/retention/immutability tests for both new immutable evidence
  record concepts (§9, §10) and the `resource_terminations` linkage,
  mirroring the existing `source_attestation_events`/`candidate_
  attestation_bindings` test discipline WAVE C1 already established
  (immutable, delete-blocked, retained across reopen), plus separate
  restart-survival (not immutability) tests for the bounded current
  sampled-absence pointer, which is durable but intentionally mutable
  (§12 retention shape A);
- the full adversarial matrix of §28, translated into concrete negative-
  witness tests, plus the sixteen normative decisions of §29 as positive/
  negative contract tests;
- a descriptive-only update to `docs/architecture/0.5-implementation-
  status.md` once WAVE A1 actually lands, following this document's exact
  distinction between "architecture decision" (this ADR) and
  "implementation" (WAVE A1) — this ADR's own acceptance changes only this
  status document's description of Blocker A's *architecture* state, never
  its implementation state, exactly as ADR 0003's acceptance did for
  Blocker C (`0.5-implementation-status.md`'s existing WAVE C0/C1
  distinction is the precedent to follow).

WAVE A0 (this ADR) implements none of the above; it only records, and
awaits the operator's decision to accept, this architecture as normative.
Everything listed in this section remains WAVE A1's future implementation
work, exactly as ADR 0003 §31's closing line already established the same
posture for its own next package.
