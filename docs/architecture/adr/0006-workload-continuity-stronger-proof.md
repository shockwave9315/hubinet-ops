# ADR 0006: stronger workload-continuity proof — trusted host lifecycle witness research

Status: **ACCEPTED** — accepted strictly as the negative/unresolved
stronger-proof research record and as the normative requirements any future
Blocker-B mechanism must satisfy. Acceptance selects **no** sufficient
mechanism, grants `security_continuity=trusted` nowhere, leaves Blocker B
**OPEN**, does not authorize WAVE B1, does not unblock Phase 1C, and
authorizes no schema, runtime, `hostd`, enrollment, HA, or mutation work
(§13).

This is the **normative core** of ADR 0006: the decision record and the
contract any future Blocker-B mechanism must satisfy. The primary-source
evidence its conclusions rest on lives in a separate, **non-normative**
document — `docs/architecture/research/adr0006-workload-continuity-evidence.md`
— which is evidence, not authority. Where it and this ADR (or any ACCEPTED
ADR) disagree, the normative architecture wins.

## 1. Status and decision summary

This ADR is a research pass on top of ADR 0005 and does not reopen it. ADR
0005's conclusion — that no evidence composed of ordinary, copyable
PVE/guest/config state (its Families A/B/C, §6–§13 there) is sufficient for
`security_continuity: unverified -> trusted` — stands unchanged and is not
re-litigated here. ADR 0005 left open exactly one undesigned class: "a
hardware-rooted attestation chain unavailable from stock, or an out-of-band
Hubinet-managed identity provisioned and verified through a channel that is
not itself guest disk state" (§13), and fixed the minimum properties any such
mechanism must satisfy (§14) without designing it. This ADR is that follow-on
research, directed at the **trusted host lifecycle witness** hypothesis plus
a comparative audit of additional candidate families.

**Decision: no mechanism is selected.** This ADR reaches **no NO-GO
conclusion** for any candidate family audited against the two PVE task-list
surfaces, the acknowledged unaudited exact-UPID reads, or either
`pmxcfs`-filesystem-witness variant. It is likewise **not** a claim that
every conceivable host-rooted lifecycle witness is impossible.

```text
PVE task/event-history witness:                     UNRESOLVED / NOT FULLY AUDITED
single-node pmxcfs watcher (generic cluster-wide):  UNRESOLVED / NOT FULLY AUDITED
distributed per-node pmxcfs watcher:                UNRESOLVED / NOT AUDITED HERE
broader host-rooted/external mechanisms:            UNRESOLVED
Families D/E/F-narrow:                              No / not sufficient
Family C:                                           not sufficient / not applicable as a
                                                    Blocker-B resource-continuity proof

ADR 0006:                                           ACCEPTED
Blocker B:                                          OPEN
future positive Blocker-B mechanism ADR:            NOT STARTED / UNRESOLVED
WAVE B1:                                            DEFERRED / NOT AUTHORIZED
Phase 1C:                                           BLOCKED
R0:                                                 unchanged / read-only
```

The Blocker B / WAVE B1 / Phase 1C / R0 consequences of that decision are
stated once, canonically, in §13. Two further limits apply here: this ADR
does not amend ADR 0001, 0002, 0003, 0004, or 0005 — where it depends on
their invariants it cites them and adds a narrower research layer on top —
and it authorizes no schema, persistence, `hostd`, HTTP API, HA control,
mutation, or enrollment-automation implementation of any kind, and does not
change `security_continuity` in code.

## 2. Scope and non-goals

**In scope:** the trusted host lifecycle witness hypothesis; the comparative
candidate families (§6); primary-source research on PVE task/event
observability and `pmxcfs` filesystem-change observability, separating
locally-originated (same-node, "Path A") from remotely-replicated
(cross-node, Corosync-originated, "Path B") delivery; classifying — but not
designing — a distributed, per-node `pmxcfs`-watcher variant as its own
candidate; the adversarial same-slot test against each family (§5d); the
explicit trust-root separation between node/hostd compromise and resource
continuity (§11); and the extended minimum-property list any future mechanism
must satisfy (§7–§8).

Explicitly **not** in scope, and not authorized by this ADR:

- any schema, table, column, or enum-value implementation, and any bump of
  the authority schema version (currently `5`); self-acceptance of this ADR
  by the agent that wrote it;
- designing, implementing, or partially wiring `hostd`, a witness daemon
  (single-node or distributed), an enrollment ceremony, or any HA control
  surface — the distributed variant is *classified* here, never *designed*;
- writing anything into a guest's configuration, disk, or vTPM state (that
  would be a mutation; no mutation authority exists); changing
  `security_continuity` in code; or any change to production startup,
  scheduler, HTTP, or Home Assistant wiring;
- any change to ADR 0001, 0002, 0003, 0004, or 0005; or authorizing WAVE B1
  under any circumstance;
- any weakening of ADR 0001's invisible same-slot destroy/recreate
  limitation, ADR 0003's epoch authority-eligibility rule, or ADR 0005's
  Family A/B/C rejection.

## 3. Accepted inherited invariants

These remain exactly as their owning ADRs define them. This ADR restates
them only to fix what it may not weaken.

1. `resource_id` / `binding_id` / `locator_generation` /
   `resource_continuity_revision` remain exactly as ADR 0001 defines them;
   `resource_continuity_revision` is the sole resource-level
   security/concurrency token (ADR 0001, `0.5-inventory-model.md`).
2. `security_continuity` (`unverified`/`trusted`/`revoked`) has exactly one
   durable owner, `resource_incarnations.security_continuity` (ADR 0001, ADR
   0005 §15). No future mechanism may introduce a second authoritative copy
   or a fourth canonical value (ADR 0005 §15, §17).
3. ADR 0005's Family A (ordinary stock fields), Family B (operator assertion
   alone), and Family C (administrative correlation marker, any entropy) are
   rejected as sufficient for `trusted`, permanently, for the reasons given
   there. This ADR does not re-audit them.
4. ADR 0005 §14's minimum-property list applies to any future mechanism; §7
   adds generic requirements, and §8 adds only channel-specific requirements
   applicable to the mechanism actually proposed.
5. ADR 0003's `source_attestation_epoch` authority-eligibility rule applies
   unchanged and **conditionally**, and node/hostd trust
   (`node_bindings`/`node_attestations`, ADR 0001) remains a separate axis
   from resource continuity — ADR 0005 §18/§21 already forbid collapsing
   them. §11 carries both, extended to the witness hypothesis's own trust
   root.
6. R0 remains read-only. ADR 0005 §24's list of what R0 must never do (grant
   `trusted`, run enrollment automation, write a marker into guest config,
   expose writable HA controls, enable policy/jobs/mutation, treat any
   copyable evidence as security authority) is unaffected by this ADR
   regardless of its conclusion.
7. `discovery_runs` / `source_runtime_health` / CAS discipline (ADR 0002) and
   ADR 0003 §19a's read-then-write remote-evidence pattern remain the
   template any future mechanism's own remote reads must follow.

## 4. Canonical R/P/N/T lifecycle taxonomy

A **continuity-relevant lifecycle event** is any event whose occurrence bears
on whether an existing resource's continuity proof may still be relied on for
a slot. Such events are **not** one uniform "identity-breaking" class:
destroy+create, snapshot rollback, backup restore, clone, and migration carry
**materially different** accepted ADR 0001 consequences. Ordinary config
mutation, and config-metadata churn alone, are not continuity-relevant events
under any class. The class letters below are this ADR's shorthand for ADR
0001's existing consequences, not new architecture. **This table is the
single canonical definition of each class; every later section references it
rather than restating it.**

| Class | Operations | Required consequence (ADR 0001) |
| --- | --- | --- |
| **R — same-slot potential replacement** | destroy + create at the same locator | Accepted **positive replacement evidence** triggers ADR 0001's canonical **atomic direct replacement** (rows 9/10 and its positive-replacement-evidence rules): the old binding is closed/replaced, the old incarnation goes terminal/`not_current`/`replaced`, and the successor receives a **new** `resource_id`, a new active binding and `locator_generation`, `presence=present`, `security_continuity=unverified`, and **no inherited effective destructive authority**. Absent such evidence, the invisible same-slot delete/recreate limitation applies: the existing read-only `resource_id`/binding is retained and continuity/policy fail closed under the accepted ambiguity contract |
| **P — same-resource continuity-proof invalidation** | snapshot rollback of the same logical workload; a same-resource restore where accepted identity is retained | **The same logical resource identity is retained: the same `resource_id` is retained and the active binding is retained.** The rollback/restore itself **MUST NOT** mint a successor `resource_id` and **MUST NOT** close the binding. The continuity proof is invalidated/revoked/revalidated according to the accepted security-continuity rules; ambiguity or revalidation failure moves that **same** `resource_id`/binding to `uncertain`/`quarantined` with no effective destructive policy, removing authority (ADR 0001 row 5). This is **not** direct replacement. A new `resource_id` or direct replacement is admissible **only** when separately accepted positive replacement evidence establishes actual replacement — at which point the event belongs to the class-R path, not to ordinary class-P rollback semantics |
| **N — new-locator duplication** | clone; restore to a new locator | The **target** is a new locator, therefore a new `resource_id`, `unverified`, with no policy copied (ADR 0001 row 6). The **source's** identity and trust are **not** invalidated merely because a duplication occurred. A future mechanism must nevertheless ensure copied/cloned evidence cannot grant the *target* trust |
| **T — identity-preserving relation transfer** | migration | `resource_id` is **preserved**; node is a mutable relation updated in place (ADR 0001 row 2). A mechanism may require explicit coverage/handoff semantics across the node change, or fail-closed continuity handling on that same resource (§10) — migration is **never** canonical resource replacement and must never be modeled as one |

**Backup restore under the old VMID spans classes by context, exactly as ADR
0001 row 17 states:**

- with separately accepted positive replacement evidence (e.g. an accepted
  continuity-anchor mismatch): **class R** → atomic direct replacement →
  successor `resource_id`;
- without positive replacement evidence: the existing read-only
  identity/binding is **retained**, with class-P-style fail-closed continuity
  semantics (`uncertain`/`quarantined`/`unverified` or `revoked`);
- **a mere gap or ambiguity is never positive replacement evidence.** A
  coverage gap alone must never manufacture a new `resource_id` or close a
  binding; ADR 0001's read-only identity behavior is preserved and
  continuity/policy fail closed (ADR 0001 rows 10/11/14).

This ADR does not decide which branch applies in the abstract — the evidence
accepted at the time does.

### 4a. `workload_epoch_id`

A hypothesized opaque, backend-generated, per-slot value a future mechanism
could rotate or revoke on the events its own contract covers. It is
**mechanism-specific evidence/provenance only**:

- it is **not** canonical resource identity, and **not** a replacement for,
  or an alternative encoding of, `resource_id`, `binding_id`,
  `locator_generation`, `resource_continuity_revision`, or any ADR 0001
  lifecycle transition;
- it must be durably associated with the exact accepted resource/binding
  context its mechanism requires, and stored only in Hubinet's own authority
  state — never in guest config/disk/vTPM;
- **rotating it is never, by itself, an ADR 0001 identity decision.** A
  mechanism must map each detected event to the ADR 0001 consequence its §4
  class actually carries, never to a uniform response, and must never
  substitute an epoch rotation for an accepted direct replacement.

### 4b. Detection scope versus definition scope

Two different obligations, kept separate:

- **Definition — unnarrowed.** Every future mechanism must define exact
  clone, snapshot-rollback, backup-restore, and migration semantics for its
  claimed scope (§7); nothing below weakens that.
- **Event generation / detection — narrowed to what matters.** A mechanism's
  *event-generation and detection* obligations extend to operations whose
  **unnoticed occurrence could leave stale authority attached to the same
  current resource/binding** — class R (an unobserved same-slot replacement)
  and class P (an unobserved proof-invalidating rollback/restore on the
  retained `resource_id`) — plus any further mechanism-specific event needed
  to prevent **proof copying** onto a class-N target or an **unsafe class-T
  handoff**. A class-N clone does not, by itself, require a detected event to
  protect the *source*, because the source keeps its identity and the target
  starts `unverified` by construction.

### 4c. Config metadata versus actual occupant

Three distinct concepts, not to be conflated:

1. **Ordinary config mutation** — editing memory/CPU/description/tags on an
   existing guest. ADR 0001 already establishes this preserves resource
   identity; it is not a continuity-relevant event in any class.
2. **Deletion/recreation of PVE config metadata** — removing and rewriting
   the `.conf` object in `pmxcfs` for a `(inventory_source_id, vmid)` slot. A
   privileged actor could do this while leaving the disk image byte-for-byte
   untouched, in which case the occupant has not changed at all. That this
   metadata-lifecycle event can happen without generating a task/UPID is a
   finding about **observability of metadata lifecycle**, not a claim that
   the workload changed.
3. **Actual physical/logical workload replacement** — the disk content and/or
   running process backing a slot is genuinely destroyed and replaced (class
   R). This is what §5d's same-slot test is about.

The realistic direct-write attack combines (2) and (3): an actor who can
write `pmxcfs` directly typically also controls the storage layer well enough
to replace the disk content in the same operation, producing an actual
occupant replacement with no task/UPID trace, because the config half
bypassed the task-generating path. Rewriting the `.conf` file alone, disk
genuinely untouched, is not a workload replacement under (2) alone.

## 5. Controlling security property and threat boundary

### 5a. Threat tiers

| Tier | Actor | Example action |
| --- | --- | --- |
| **T1 — ordinary PVE operator action** | an authenticated operator using the normal GUI/API/CLI, in good faith or by mistake | destroy VM100, recreate a different guest at the same VMID |
| **T2 — PVE admin action** | a user with full administrative PVE privilege (`Sys.Modify`/`VM.Allocate`/`Realm.AllocateUser`-class), still acting through PVE's own management surface | bulk-recreate guests via API/CLI scripting, disable auditing features, rotate certificates |
| **T3 — direct root tampering** | a party with root shell access to any one cluster node | directly edit/delete `/etc/pve/nodes/<node>/qemu-server/<vmid>.conf`, manipulate disk images on storage directly, stop/restart PVE daemons |
| **T4 — compromised node/hostd trust root** | the PVE host's control-plane software itself (`pvedaemon`, `pmxcfs`, or any future Hubinet-managed host-resident witness component) is compromised or malicious | fabricate task history, suppress task creation, fabricate lifecycle events, lie to a co-resident witness |

T2 is **not** the same capability as T3: ordinary PVE admin privilege through
the API/CLI management surface does not, by itself, establish host-root shell
authority. No candidate audited here is expected, or required, to defend
against T4 — that is the existing, separate node/hostd trust gate (§11; ADR
0001 node section). For every T1/T2 continuity-relevant workflow inside a
candidate's claimed supported scope — scoped per §4b — the mechanism must
either positively detect/prove the transition as its contract requires, or
make authority fail closed before stale `trusted` can survive. A candidate
may deliberately declare an operation or workload type unsupported, but
**unsupported scope cannot silently retain mutation authority.** The answer
(detected / fail-closed-though-undetected / silently defeated) must be stated
for T1 and T2, not glossed over.

### 5b. The T3 boundary rule

A witness process **co-resident** on the node it observes — running as an
ordinary process subject to that node's root — is **not** a defense against
T3 merely by existing: a root-shell actor on that node can kill it, patch it,
or feed it fabricated events, exactly as at T4.

**Unless a candidate specifies an explicit root-resistant or external trust
anchor** — a cryptographic or structural root of trust *not* extractable,
killable, or forgeable by a root-shell user on the node being observed (a
hardware-sealed key, a secure enclave, a fully out-of-band logging channel
the node cannot silently rewrite) — **T3 must be treated as equivalent to T4
for that candidate: out of scope, not a bounded, partially-defensible gap.**

None of the *lifecycle-witness candidates this ADR hypothesizes and audits*
(Families A, A2, and B, §6) specifies such an anchor. This is a claim about
those three witness designs specifically, not about every family in §6:
Family C (hardware-rooted node attestation) and Family F's broader
externally-rooted/out-of-band class discuss root/anchor properties on a
different, non-lifecycle-witness basis and are unaffected.

**Consistency rule.** Because T3 is declared out of scope for an anchor-less
witness, **T3 must never simultaneously be the load-bearing reason that same
witness's NO-GO rests on** — a tier excluded from scope cannot also be the
reason a candidate fails within that scope. Any NO-GO reached for an
anchor-less candidate must rest on the in-scope tiers T1/T2 plus any genuine,
privilege-tier-independent architecture gap, never on the T3 bypass alone.
Correspondingly, a mechanism claiming T3 resilience must define a
root-resistant/external anchor and prove that property on its own terms
(§11).

### 5c. The two controlling tests

Test 1, unchanged from ADR 0005:

```text
if a mechanism cannot distinguish its intended security claim after an
adversary or ordinary workflow has copied all the state it relies on
into another incarnation, it cannot be sufficient for persistent
canonical trusted
```

Test 2, added by this research:

```text
a mechanism may not treat an OBSERVED / UNCONTRACTED behavior as a
security boundary; before relying on it, a future positive ADR must
establish a separately ACCEPTED, security-sufficient, version-scoped
completeness contract for that behavior -- whether grounded in an
upstream normative Proxmox guarantee, a separately reviewed and
version-pinned primary-source contract, or another sufficiently strong
mechanism. This ADR does not decide which of those a future contract
must come from; a behavior that merely "seems to always happen," with
no such accepted contract behind it, is evidence, never proof, exactly
as ADR 0001/0002 already rule for task history and ADR 0002 rules for
interval-wide ACL consistency
```

Test 2 is deliberately architecture-neutral about *where* an accepted
completeness contract comes from. **This ADR does not introduce "only
upstream documented guarantees may ever be security boundaries" as a hidden
new invariant.** Both tests apply to every candidate in §6. **Neither test,
nor this ADR's conclusion, is a claim about every conceivable host-rooted
witness design.**

**Silence rule.** Mere absence of an adverse event in an incomplete,
uncontracted, stale, ambiguous, or gapped channel is never positive
continuity proof. Absence of a continuity-relevant event may contribute to a
positive conclusion **only** when the accepted mechanism independently proves
complete, gapless coverage, throughout that exact interval, of every
operation its §4b detection scope covers. This is the canonical statement of
the rule; §7, §9, and §14 reference it rather than restating it.

### 5d. The same-slot witness test

This is the single most important test in this ADR, and the one §7, §8, §13,
and §15 refer to as "§5d's same-slot test."

```text
T0: VMID 101 occupant A is trusted (hypothetically, once some future
    mechanism grants trust).

Between observations:
  A is destroyed.
  B is recreated at VMID 101.
  B has identical type/name/config/disk contents, if the operator or
  attacker wants it to.

Question: why can B NOT inherit A's security_continuity=trusted?
```

Per §4c, "destroyed"/"recreated" means the **actual physical/logical
occupant** genuinely changed, not merely that the `.conf` metadata object was
rewritten. This is §4's **class R**, so the two admissible outcomes are
exactly ADR 0001's: with accepted positive replacement evidence, atomic
direct replacement; without it, the retained read-only identity plus
fail-closed continuity/policy. A coverage gap is not positive replacement
evidence and may not manufacture either outcome's identity effects.

If the answer reduces to "because polling/config looks different," the
mechanism **fails** — the exact test ADR 0005 §9 already applied to Family C.
Mere silence in an incomplete/uncontracted channel also fails, and a no-event
conclusion may contribute to a passing answer only inside an independently
proven complete, gapless coverage interval (§5c). Families A, A2, and B have
not established that contract here; the per-family evidence walkthrough is in
the research document (R6).

## 6. Candidate-family result table

This is the canonical, normative result for each family. The detailed
per-family evidence, the 14-column comparison matrix, and the adversarial
matrix live in the research document (R5, R6, R7).

| Family | Result | Basis |
| --- | --- | --- |
| **A. Single-node `pmxcfs`/hostd lifecycle witness + external epoch** | **UNRESOLVED / NOT FULLY AUDITED** | Neither shown to satisfy nor shown to fail §5c/§5d. Cross-node delivery (Path B) is unresolved; same-node delivery (Path A) is UNKNOWN, not disproven; operation-to-authoritative-event coverage is unestablished for every workflow. As hypothesized it specifies no root-resistant anchor, so T3 collapses into T4 for it — **never the load-bearing reason for this result** (§5b) |
| **A2. Distributed, per-node `pmxcfs` lifecycle witness + external epoch** | **UNRESOLVED / NOT AUDITED HERE** | Not audited and not designed. Depends on Path A completeness at every node, on which node executes a given operation's syscall, on operation-to-event coverage, and on cross-node coverage/gap/restart semantics never designed here. Not claimed to succeed; not claimed to fail |
| **B. PVE task/event/audit history as witness** | **UNRESOLVED / NOT FULLY AUDITED** | The demonstrated insufficiency covers a **stateless** observer only: neither audited list surface carries a monotonic, gap-detectable cursor, and both bound how long a created record stays observable. That does not rule out a **stateful**, fail-closed overlap-sentinel protocol, which this ADR neither designs nor audits; exact-UPID status/log retention was not audited either |
| **C. Hardware-rooted TPM / physical node attestation** | **Not sufficient / not applicable as a Blocker-B resource-continuity proof** | Axis mismatch, on grounds independent of this ADR's research: a hardware TPM attests the **node**, not which guest incarnation occupies a VMID slot. It answers a different question than Blocker B asks — not a weaker "No" of the same kind as D/E/F |
| **D. vTPM** | **No / not sufficient** | Disk-resident state, copied identically by clone/backup/snapshot (ADR 0005 §6 candidate 20). Independent of this ADR's research |
| **E. Guest cryptographic agent + guest-resident key** | **No / not sufficient** | Disk-resident key material, copied identically by clone/backup (ADR 0005 §13). Independent of this ADR's research |
| **F. External/HSM-backed guest identity — narrow variant** | **No / not sufficient** | The narrow variant (guest-resident credential, external signer) reduces to Family E: the artifact actually presented and copied still lives in guest-readable state |
| **F-broad. Externally-rooted / out-of-band per-workload identity** | **UNRESOLVED / NOT AUDITED HERE** | A workload's identity tracked/attested by an external system through a channel that is neither guest-resident nor a node-bound hardware property. Not claimed to satisfy Blocker B; not claimed to fail |
| **G. Operator per-mutation re-attestation / ephemeral trust** | **Does not satisfy Blocker B by itself; sidesteps rather than answers the question** | No persistent `trusted` is granted, so §5d is moot by construction rather than passed. Operator confirmation alone is not continuity proof; ADR 0001's CAS prevents replay of a stale *backend decision*, not occupant substitution; and a mutation model that never requires persistent `security_continuity=trusted` would itself require a separate architecture change to ADR 0001/0005's accepted mutation-precondition formula, which this ADR cannot make by implication |
| **H. Combinations** | **No / not sufficient — only for combinations drawn solely from C/D/E/F-narrow** | Combining evidence classes that each introduce no new independent security property does not manufacture one; useful only as an audit/anomaly-detection signal. **This is not a general "weakest member" rule.** A combination that includes A, A2, B, or F-broad is judged entirely by that component's own eventual resolution and **must not be manufactured into a NO-GO** |
| **Broader host-rooted/external mechanisms** (kernel audit/`auditd`, LSM hooks, `eBPF` syscall interception, storage-layer block-change tracking, or a witness backed by an explicit root-resistant/external anchor) | **UNRESOLVED** | Not primary-source audited here at all. Genuinely unresolved, not disproven. A future ADR auditing one must perform its own research and its own pass against §5c/§5d; there is no NO-GO here for it to inherit |

**The only settled negative conclusions in this ADR** are Families D, E, and
F-narrow ("No", on disk-resident-copyability grounds), Family C ("not
sufficient / not applicable", on the node-vs-resource axis-mismatch ground),
and Family H when drawn solely from that set — all resting on grounds ADR
0005 already established, not on this ADR's own task/`pmxcfs` research.

## 7. Normative requirements for any future positive mechanism

ADR 0005 §14's minimum-property list applies unchanged. Every future positive
Blocker-B mechanism must additionally:

1. satisfy ADR 0005 §14 and pass §5d's same-slot test;
2. for every T1/T2 continuity-relevant workflow in its declared QEMU/LXC
   supported scope, positively detect/prove the transition as its contract
   requires, or fail-close authority before stale `trusted` can survive.
   Unsupported operations/types cannot retain mutation authority silently.
   The *detection* obligation is scoped by §4b;
3. map each detected event to the accepted ADR 0001 consequence its §4 class
   actually carries — never to a uniform "identity-breaking" response.
   Specifically: accepted positive replacement evidence invokes atomic direct
   replacement (class R); a class-P event revokes/revalidates the continuity
   proof on the **retained** `resource_id` and active binding, without
   minting a successor `resource_id` and without closing the binding; a
   class-N target starts as a new `unverified` resource without invalidating
   the source; a class-T migration preserves `resource_id` and requires
   coverage/handoff semantics (§10). **A coverage gap or ambiguous evidence
   is never positive replacement evidence and must never manufacture a new
   `resource_id` or close a binding** (§4);
4. treat any `workload_epoch_id`-style value it introduces as
   mechanism-specific evidence/provenance durably bound to the accepted
   resource/binding context — never as canonical identity, and never as a
   substitute for an ADR 0001 lifecycle transition (§4a);
5. fail closed on missing, ambiguous, stale, or gapped evidence, and obey
   §5c's silence rule without exception;
6. define exact clone, rollback, restore, migration, restart, offline-gap,
   enrollment/revalidation, replay, and upgrade/version semantics for its
   claimed scope;
7. define durable coverage/evidence state and the exact CAS, revision,
   source-context, node-context, and remote-read dependencies its accepted
   security contract actually uses (§9–§12). Source-attestation and
   node-trust coupling are **conditional** on evidence authority actually
   depending on those contexts (§11); every separate mutation gate remains
   independently required;
8. state its T3 contract explicitly (§5b). An anchor-less/co-resident design
   may place T3 with T4 out of scope but may not claim root resilience; a
   T3-resilient claim must identify a root-resistant/external anchor and
   close, detect, or be structurally immune to the direct-root bypass.

## 8. Channel-specific obligations for the unresolved candidates

These apply **only** to a mechanism actually built on the named channel. A
genuinely different mechanism (§6's broader class) does **not** inherit
irrelevant task-history or `pmxcfs` requirements merely because they appear
here; it must satisfy §7 plus its own channel-specific contract.

### 8a. Family B — task-history-specific

- must establish **task-generation coverage operation by operation** for its
  exact QEMU/LXC scope. Only ordinary create/destroy through the verified
  normal API routes is source-confirmed to generate a UPID worker record;
  clone, rollback, restore, and migration are **not** established at that
  strength (research R2);
- must state **which evidence surfaces it uses** — the audited
  list/enumeration surfaces, exact-UPID status/log reads, or both — and
  establish those exact reads' retention, completeness,
  pagination/concurrent-rotation, and gap semantics. PVE's own bounded task
  retention is never the completeness authority;
- must define a **durable, CAS-protected stateful sentinel/coverage-gap
  contract**, including its false-negative and false-positive behavior.
  Merely maintaining state is not proof;
- must define the **exact reader privilege/visibility contract** for every
  chosen read and prove visibility of every relevant actor's tasks across
  every relevant node. `VM.Audit` alone is **not** assumed sufficient. A
  successful but permission-filtered response is **never** complete history.
  Missing, partial, or ambiguous audit privilege/ACL coverage is a coverage
  gap and authority-ineligible; security-sensitive permission/ACL changes
  must invalidate or explicitly revalidate coverage;
- **must inherit ADR 0002's interval-wide ACL limitation** — an explicit
  carry-forward, not new architecture here. ADR 0002 already establishes that
  ACL/effective-permission state being identical BEFORE and AFTER an interval
  does **not** prove the ACL was unchanged *during* it: a transient
  `full visibility -> hidden/NoAccess -> full visibility` produces equal
  boundary states. So if a Family-B completeness claim depends on mutable PVE
  ACL/permission state, point-in-time or before/after revalidation alone is
  **not** interval-wide visibility proof. A future positive ADR must either
  establish an accepted interval-wide/monotonic ACL visibility contract, use
  a reader/trust boundary whose complete visibility is proven by a stronger
  accepted mechanism, or **treat the inability to prove interval-wide
  authorization visibility as a coverage gap / authority-ineligible**. This
  ADR invents no new ACL solution;
- must define per-node retention/sentinel ownership, node join/removal,
  migration, restart, initial-overlap, and version-upgrade semantics.

### 8b. Family A — single-node `pmxcfs`-specific

- **Operation-to-event generation and event delivery are separate questions,
  and the first is prior to the second.** The mechanism must establish
  complete operation-to-authoritative-event coverage for every claimed
  workflow. This ADR has not established that contract for **any**
  continuity-relevant workflow at the required strength: ordinary
  create/destroy is *plausible* because QEMU/LXC configs live under `pmxcfs`,
  but config location is applicability, not coverage — and rollback, restore,
  and storage-level changes remain separately unresolved. Perfect delivery
  does not rescue a transition that never emitted an event to deliver;
- must independently establish **Path A** (same-node, locally-originated) and
  **Path B** (cross-node, Corosync-replicated) delivery completeness and
  node-dispatch semantics for the exact supported PVE/kernel/`pmxcfs` scope.
  This ADR leaves Path A **UNKNOWN** (plausible per general Linux VFS
  behavior, neither proven nor disproven for `pmxcfs`) and Path B
  **UNRESOLVED**; per §5c it does not decide where an accepted completeness
  contract must come from;
- **for Path B specifically, FUSE cache/dentry invalidation must not be
  substituted for `fsnotify`/`inotify` delivery** — they are different
  mechanisms, and the bounded five-file `pmxcfs` search is evidence about the
  former, never a Path-B proof. A positive Path-B contract must, in order:
  (i) fix the exact supported PVE kernel/FUSE release; (ii) establish the
  exact kernel/userspace mechanism that can turn a behind-the-mount change
  into a local `fsnotify`/`inotify` event on that release; (iii) audit
  whether `pmxcfs`/`libfuse` actually uses **that** mechanism; and only then
  (iv) prove its completeness, ordering, and gap semantics.

### 8c. Family A2 — distributed per-node `pmxcfs`-specific

- must establish the same operation-to-event and Path A contracts as §8b;
- must define node dispatch plus complete fleet coverage, membership/gap,
  restart, migration/handoff, and storage-layer semantics. It does not
  inherit a Path B requirement if its accepted design proves Path A coverage
  at every relevant execution node — but this ADR audits none of that design.

## 9. Gap/restart semantics

Not implemented here; recorded as the fail-closed default any future
mechanism must adopt.

```text
the mechanism / authoritative evidence channel loses or cannot prove
  authoritative coverage (crash, restart, partition from the source,
  expiry or unavailability of an external anchor, or any other
  interruption it cannot prove did not overlap a continuity-relevant
  event)
  => every resource whose trust depended on that coverage becomes
     immediately authority-ineligible

durable materialization: trusted -> revoked (resource_continuity_revision
  +1 exactly once), expressed strictly within ADR 0001's existing
  three-value vocabulary -- no fourth canonical state (ADR 0005 §17)

restoring trusted requires a fresh ACCEPTED enrollment/revalidation of
  the current occupant, under the future mechanism's current exact
  security context (CAS/epoch/node-dependence rules included) -- never
  an automatic replay or reconstruction of the unobserved interval, never
  optimistic carry-forward of pre-gap evidence, and never "nothing looked
  different" as a substitute for positive proof (§5c)
```

This rule is deliberately **classification-neutral**: a future mechanism need
not be a witness *process* at all — a genuinely external or cryptographic
mechanism may have no process to crash — and the same fail-closed semantics
apply to whatever its authoritative evidence channel is. It is grounded in
the accepted invariant itself, not in any candidate having been shown to
fail: an unobserved interval cannot establish the *absence* of a
continuity-relevant event, and missing or ambiguous evidence removes
authority rather than defaulting to it. A gap's consequence operates on the
**same** `resource_id` and binding (§4): it never mints a successor
`resource_id` and never closes the active binding.

Explicit operator enrollment is one acceptable conservative default. This ADR
does **not** preemptively forbid a future mechanism from defining an
automatic fresh re-enrollment/revalidation path — but only if that future,
separately accepted positive ADR proves the *current* occupant's identity
directly (not by inference from the gap-preceding state), satisfies ADR 0005
§14 and §5d, and satisfies every applicable CAS/epoch/node-dependence rule
(§11, §12) for that revalidation itself. An automatic path is **not**
authorized here.

## 10. Migration semantics

Not designed here. Per this ADR's scope, a future mechanism is **not**
required to support secure migration in its first version. The acceptable
default, if a safe source-node → target-node handoff cannot be proven by that
mechanism's own evidence:

```text
migration => trusted -> revoked (resource_continuity_revision +1 exactly
  once), unless the future mechanism's own ADR explicitly proves a safe
  handoff and defines its exact semantics
```

This must be a stated, deliberate decision in that future ADR, not a silent
gap. Migration is §4's **class T**: `resource_id` is preserved and the node
is a mutable relation updated in place (ADR 0001 row 2). What a future mechanism
owes here is **coverage/handoff** semantics, never canonical
resource-replacement semantics. Revoking continuity on migration is a
fail-closed continuity decision on the **same** resource — it must never be
implemented as a direct replacement, a new `resource_id`, or a closed
binding. This mirrors ADR 0001's existing separation of preserved identity
from the continuity/trust axis, and does not change ADR 0001's migration
identity rules.

## 11. Source / node / resource trust conditionality

Three axes, deliberately not collapsed. Each dependency below is
**conditional on what the mechanism's accepted security contract actually
depends on** — neither may be universalized.

### 11a. Source attestation (`source_attestation_epoch`)

- Any future Blocker-B evidence **whose authority depends on source
  trust-domain continuity** is authority-eligible only under the exact
  `source_attestation_epoch` at which it was accepted (ADR 0003 §19, §20,
  §26, §27).
- For such source-dependent evidence: the durable evidence/provenance must
  bind the exact epoch; every accepting transition must capture and re-CAS
  it; and every later authority consumption must require
  `current source_attestation_epoch == evidence.source_attestation_epoch`.
  Mismatch is immediately authority-ineligible. Absent an accepted
  carry-forward procedure, `trusted -> revoked` and
  `resource_continuity_revision +1` materialize exactly once; historical
  evidence is retained for audit (ADR 0003 §27; ADR 0005 §19, §26).
- **A genuinely source-independent continuity proof does not inherit a
  `source_attestation_epoch` dependency merely because PVE inventories the
  resource.** Normal mutation still obeys every separate accepted
  source/node/resource gate that actually applies.

### 11b. Node/hostd trust

- **A trusted node/hostd does not, by itself, grant resource continuity.**
  Even a perfectly honest, uncompromised PVE node still exposes `pmxcfs` as a
  directly-writable configuration store to a **T3-tier** actor (§5a). Node
  honesty says nothing about whether a *specific slot's* occupant changed via
  a channel the mechanism observes.
- **Resource continuity evidence does not, by itself, grant node/hostd
  trust.** Future destructive mutation independently requires the accepted
  node/hostd trust route (ADR 0001; ADR 0005 §21). **`trusted` resource
  continuity and `trusted` node/hostd remain two independent, both-required
  preconditions for mutation — neither implies the other.**
- **Resolved current node locator is not node trust.** It is
  routing/presentation context (which node an evidence read was served from).
  Ordinary read-only evidence acquisition does not automatically require a
  trusted node.
- **Families A, A2, and B assume T4 is out of scope** — that the node's own
  control-plane software is not itself compromised or lying. No mechanism
  evaluated here defends against a compromised node/hostd trust root; that
  defense belongs to the separate node/hostd attestation protocol ADR 0001
  §"Nierozstrzygnięte kwestie" #6 already flags as future work.
- **The concrete node/hostd attestation/trust-root contract remains
  unresolved in accepted architecture.** ADR 0001 defines `node_trust_state`
  and requires re-attestation on reinstall/rejoin/hostd-key-change, but
  leaves the concrete protocol, key rotation, and operator re-grant procedure
  to a separate future review. Nothing in accepted architecture specifies
  that a currently `trusted` binding proves the absence of an in-session
  root-shell compromise: attestation as designed verifies host/endpoint
  *identity* across reinstall/rejoin-class events, not continuous absence of
  root tampering. **`node_trust_state=trusted` must never be presented as if
  it already meant "no root-shell actor could have tampered with
  `pmxcfs`/storage on this node."**

### 11c. When a T3-resilience claim is admissible

The requirement depends on *where* the claim comes from:

- **A mechanism deriving its T3-resilience claim from node/hostd trust** must
  satisfy **both**, neither of which exists today: (a) its
  witness-authority-eligibility is explicitly coupled to `node_trust_state`,
  **and** (b) a separately accepted node/hostd attestation/trust-root
  contract actually defines detection or prevention semantics against a
  root-shell actor on that node. Absent (b), coupling alone gives a
  fencing/freshness property, not a resilience property, and no
  node-trust-derived candidate is entitled to claim T3 resilience on (a)
  alone.
- **A mechanism relying on a genuinely independent, root-resistant/external
  anchor** — one whose T3 resistance does not derive from, and is not
  contingent on, `node_trust_state` or any node/hostd attestation contract —
  is **not** required to satisfy (a)/(b); those apply specifically to
  node-trust-*derived* claims. Its own future positive ADR must instead prove
  that independent anchor's T3-resistance property explicitly and directly.
  This ADR does not pre-approve an anchor merely for being independent of
  node trust.
- **In every case**, future destructive mutation still independently requires
  the separately accepted node/hostd trust gate (§11b); a
  resource-continuity mechanism's own T3-resistance never substitutes for it.

Any future mechanism requiring a host-resident witness component must define
its own node-migration/re-attestation semantics (mirroring ADR 0005 §21's
requirement for node-mediated evidence collection).

### 11d. Remote reads

Any live remote-evidence read a future mechanism performs (e.g. to correlate
a witness event with a resource) must follow ADR 0003 §19a's three-phase
read-then-write discipline, including capturing the resolved current node
locator before the read and re-validating it by CAS in the write transaction,
exactly as ADR 0005 §18/§20 already require for marker-correlation reads.

## 12. CAS / transaction / consumption-time requirements

Not implemented here; recorded as a binding requirement on any future
mechanism, mirroring ADR 0001/0002/0003's existing discipline.

1. **Atomic committing transition.** Any future enrollment, revocation, or
   coverage-epoch transition must be a single atomic transaction that
   revalidates every expected-context field its accepted mechanism actually
   depends on: **always** exact `resource_id`, active `binding_id`,
   `locator_generation`, and current `resource_continuity_revision`;
   **additionally** current `source_attestation_epoch` only for
   source-trust-dependent evidence (§11a); and the **resolved current node
   locator** when the evidence read is node-scoped (routing context, not node
   trust — §11b).
2. **Node-trust context is a conditional, not universal, addition.** If, and
   only if, a future accepted mechanism makes node/hostd trust part of the
   *authority-eligibility of its continuity evidence itself* (a
   node-trust-derived design, §11c), its committing transition **must also**
   capture and revalidate the exact accepted node-trust context — at minimum
   `node_binding_id`, node `binding_revision`, `attestation_id`, and the
   required `node_trust_state == trusted` (or an exact future accepted
   equivalent) — immediately before accepting the transition. Any change or
   mismatch classifies the attempt as stale/authority-ineligible, granting no
   trust and accepting no stale evidence. **A mechanism relying on a
   genuinely independent, root-resistant/external anchor whose accepted
   contract does not depend on node trust for evidence authority is not
   required to carry these fields in its own CAS.** This CAS governs only
   whether resource-continuity *evidence* is fresh; destructive *mutation*
   still independently requires the accepted node/hostd trust gate (§11b).
3. **Consumption-time re-checking.** Commit-time revalidation alone does not
   prevent already-accepted, node-trust-derived evidence from outliving the
   context it was accepted under. For a node-trust-derived design, the
   durable evidence/provenance record must **bind** the exact accepted
   node-trust context at acceptance time (at minimum `node_binding_id`,
   `binding_revision`, `attestation_id`), and **every subsequent authority
   consumption** — not only the original commit — must re-require an exact
   match against the *current* context. A later
   node-binding/`binding_revision`/attestation change (reinstall, rejoin,
   re-attestation) makes that evidence immediately authority-ineligible
   regardless of what `security_continuity` physically stores. Absent an
   explicitly ACCEPTED carry-forward/revalidation contract, the fail-closed
   default is `trusted -> revoked` (`resource_continuity_revision` +1 exactly
   once, §9); restoring authority requires a fresh accepted
   revalidation/enrollment under the new context, never an inference from the
   stale evidence. Superseded evidence is retained for audit but is no longer
   authority-bearing. This applies only when continuity-evidence authority
   actually depends on node trust; the symmetric rule for source-dependent
   evidence is §11a's.
4. **Stale CAS.** A stale expected-context CAS must classify the attempt as
   stale and accept no transition, never partially apply one (ADR 0002/0003
   pattern).
5. **Revision advancement and class discipline.** A coverage-gap transition
   (§9) and a migration-triggered transition (§10) are both security-relevant
   continuity decisions under ADR 0001's own rule and therefore each advance
   `resource_continuity_revision` exactly once per decision, never per
   affected field. Both operate on the **same** `resource_id`/binding:
   neither is a direct replacement, neither mints a new `resource_id`, and
   neither closes the active binding. An accepted **direct replacement**
   (class R, on accepted positive replacement evidence) is a different
   transition governed by ADR 0001's own atomic direct-replacement rules —
   closing the old binding, retiring the old incarnation, and creating a new
   `resource_id`. A future mechanism must not substitute an epoch rotation
   for it, and must not reach it from a mere coverage gap (§4).

### 12a. Revision/publication semantics

Unchanged from ADR 0005 §22: because this ADR selects no mechanism and grants
`trusted` nowhere, there is no new `security_continuity` transition to wire
into `resource_continuity_revision`, `inventory_revision`, or
`published_state_revision`. A future stronger-proof ADR owns its own
revision/publication effect, following the pattern ADR 0001/0003/0004 already
established. Home Assistant remains presentation-only.

## 13. Blocker B / WAVE B1 / Phase 1C / R0 consequences

**Blocker B: OPEN.** This ADR selects no mechanism for
`security_continuity: unverified -> trusted` and does not conclude that any
candidate built on the two audited PVE task-list surfaces, the unaudited
exact-UPID reads, or either `pmxcfs`-filesystem-witness variant fails. Its
only settled negative conclusions are §6's D/E/F-narrow, C, and H-restricted
rows, on grounds ADR 0005 already established. Blocker B's resolution depends
on future research this ADR does not foreclose — research that may find any
of the three unresolved candidates sufficient, insufficient, or itself
further inconclusive; this ADR does not prejudge which.

**Future positive Blocker-B mechanism ADR: NOT STARTED / UNRESOLVED** — a
distinct, later ADR from this one, which is a negative/unresolved research
record only.

```text
WAVE B1 remains DEFERRED / NOT AUTHORIZED.

Acceptance of THIS ADR means only that its negative/unresolved research
conclusion is accepted as the current record. It does NOT authorize WAVE B1,
because this ADR proposes no sufficient mechanism for WAVE B1 to implement.

WAVE B1 may only begin after a DIFFERENT, later, separately reviewed and
separately ACCEPTED ADR proposes an actual mechanism -- whether within the
task/pmxcfs-observation class audited here, or a genuinely different
host-rooted class left unresolved (§6) -- satisfying ADR 0005 §14, this
ADR's §5d same-slot test, and every §7 generic and §8 channel-specific
requirement applicable to the mechanism actually proposed. A different
channel does not inherit irrelevant task/pmxcfs requirements.
```

**Phase 1C: BLOCKED.** Policy/jobs/mutation authority remains blocked exactly
as ADR 0005 §27 already sequenced. This ADR does not move Blocker B closer to
closed, and therefore does not move Phase 1C closer to unblocked.

**R0: unchanged / strictly read-only.** ADR 0005 §24's list of what R0 must
never do is unaffected, stated classification-neutrally: regardless of the
unresolved/negative research outcome, R0's already-accepted read-only posture
is unchanged, since R0 never depended on Blocker B closing (ADR 0005 §24,
§27; `0.5-inventory-model.md`'s Phase 1 runtime activation gate references
neither workload continuity nor trusted enrollment).

## 14. Open questions

Compact form. The full detail, including the evidence each rests on, is in
the research document (R8).

1. The design of a clone-resistant / externally-rooted continuity mechanism
   remains undesigned; none is proposed here (R8.1).
2. Whether a genuinely hardware-rooted, non-clonable, **per-guest** (not
   per-node) attestation primitive could ever exist for QEMU/LXC on commodity
   hardware is unresolved; stock PVE provides none today (R8.2).
3. Whether Family G should be pursued in a future mutation-authority ADR is
   not decided here. If pursued, that ADR must address all four points in
   R8.3 — including that a mutation model without persistent
   `security_continuity=trusted` would itself require a separate architecture
   change to ADR 0001/0005's mutation-precondition formula.
4. Exact PVE node-to-syscall dispatch behavior is **UNKNOWN** and must be
   independently verified before any future ADR relies on it (R8.4, R8.9).
5. Surface A/B parameter figures are FACT-SOURCE where noted but must be
   re-pinned against whichever PVE release a future ADR targets; the further
   archive-file rotation figures beyond `index.1` are forum-strength only
   (R8.5).
6. Whether Proxmox will ever ship a documented, monotonic, gapless,
   officially-retained cluster-wide event stream (closing ADR 0002's Class B
   gap) is unknown and outside Hubinet Ops's control (R8.6).
7. Broader host-rooted witness classes — a stateful overlap-sentinel task
   witness, Family A2, kernel audit/`auditd`, LSM hooks, `eBPF`, storage-layer
   block-change tracking, an explicitly root-resistant/external anchor, and
   Family F-broad — are genuinely **unresolved**, not disproven. No NO-GO
   exists here for a future ADR to inherit (R8.7).
8. The `pmxcfs` notification-absence finding is bounded (five of ~a dozen
   files), bears only on Path B, and is **not** the Path-B proof target,
   because the primitives searched are cache-coherency calls rather than an
   fsnotify-propagation mechanism. Path A's completeness for `pmxcfs` is its
   own separate **UNKNOWN** (R8.8).
9. Whether Family A is ultimately sufficient, insufficient, or further
   inconclusive requires closing at least Path A same-node completeness, Path
   B's actual proof target, node-dispatch predictability, and
   operation-to-event coverage (R8.10). The parallel question for Family B via
   a stateful overlap-sentinel design requires closing the full set in R8.12,
   including ADR 0002's interval-wide ACL limit (§8a).
10. **T3-consistency is a standing review obligation:** whenever new material
    referencing T3 / direct-`pmxcfs` access is added, re-scan that §5b's rule
    holds — T3 is never the load-bearing NO-GO reason for an anchor-less
    witness, and T2 is never conflated with T3 (R8.11).

## 15. Acceptance checklist

ADR 0006 is **ACCEPTED**. Every answer below was verified before acceptance
and remains the accepted record; acceptance changes none of them.

1. Does this ADR select or authorize any mechanism sufficient for
   `security_continuity=trusted`, WAVE B1, Phase 1C mutation, or any
   runtime/schema/API/hostd/HA change? **No.**
2. Are A (single-node `pmxcfs`) and B (task evidence) still **UNRESOLVED /
   NOT FULLY AUDITED**, A2 (distributed `pmxcfs`) still **UNRESOLVED / NOT
   AUDITED HERE**, and broader host-rooted/external mechanisms still
   **UNRESOLVED** (§6)?
3. Are D/E/F-narrow still **No / not sufficient**, Family C still **not
   sufficient / not applicable** as a Blocker-B resource-continuity proof,
   Blocker B OPEN, the future positive ADR NOT STARTED/UNRESOLVED, WAVE B1
   DEFERRED/NOT AUTHORIZED, Phase 1C BLOCKED, and R0 unchanged/read-only
   (§6, §13)?
4. Does every supported T1/T2 continuity-relevant workflow either produce the
   accepted positive proof/detection or fail-close authority before stale
   `trusted` survives, with unsupported scope unable to retain mutation
   authority silently (§5a, §7)? Does the ADR reject mere silence in an
   incomplete, uncontracted, stale, ambiguous, or gapped channel, while
   allowing no-event evidence to contribute only inside an independently
   proven complete, gapless coverage interval (§5c)?
5. Is the T3 boundary rule intact — an anchor-less co-resident witness may
   place T3 with T4 out of scope, T3 is never the load-bearing NO-GO reason
   for such a candidate, and a T3-resilience claim requires the evidentiary
   basis §11c specifies (§5b, §11c)?
6. Does §4 keep the four classes distinct rather than collapsing them into
   one "identity-breaking" class, and preserve each class's accepted ADR 0001
   consequence — **class P retaining the same `resource_id` and active
   binding, minting no successor and closing no binding, with direct
   replacement reserved for class R on accepted positive replacement
   evidence**; class-N producing a new unverified target without invalidating
   the source; class-T preserving `resource_id` — and keep
   `workload_epoch_id` as mechanism-specific evidence rather than canonical
   identity (§4, §4a)?
7. Does Family B's contract require exact reader privileges proving task
   visibility for every relevant actor/node, treat missing/partial/ambiguous
   ACL coverage, security-sensitive permission changes, and successful but
   permission-filtered responses as coverage gaps, and carry forward ADR
   0002's interval-wide ACL limitation (§8a)?
8. Do §7/§8 apply only generic plus the actual mechanism's channel-specific
   requirements, preserving **conditional** source-attestation and node-trust
   dependencies rather than imposing them on independent proofs (§7, §8, §11,
   §12)?
9. Does the `pmxcfs` contract keep operation-to-event coverage distinct from
   Path A / Path B delivery, keep FUSE cache/dentry invalidation distinct
   from `fsnotify`/`inotify` delivery, and avoid turning the bounded
   five-file search into an exhaustive absence claim or a Path-B NO-GO (§8b)?
10. Are task generation, list/direct-read retention, stateful overlap/gap,
    authorization visibility, per-node ownership/routing/migration/restart,
    and permission-change semantics all left unresolved until a future
    Family-B ADR proves them (§8a, §14)?
11. Is the evidence separated from the contract — the research document
    explicitly non-normative, subordinate to accepted architecture, and
    authorizing nothing (§16) — and is ADR 0006 **ACCEPTED** strictly as that
    negative/unresolved record, selecting no mechanism and authorizing no
    implementation?

## 16. Evidence

All primary-source research, evidence tables, source pins, the detailed
candidate matrix, the same-slot per-family walkthrough, the adversarial
matrix, and the full open-question detail supporting this ADR live in
**`docs/architecture/research/adr0006-workload-continuity-evidence.md`** —
NON-NORMATIVE research/evidence.

That document is subordinate to this ADR and to every ACCEPTED ADR. It
authorizes nothing, selects no mechanism, and changes no classification. Its
FACT-DOC / FACT-SOURCE / INFERENCE / UNKNOWN labels are load-bearing and must
not be silently upgraded, and its time-bound findings must be re-verified
against the exact PVE/kernel release a future ADR actually supports.
