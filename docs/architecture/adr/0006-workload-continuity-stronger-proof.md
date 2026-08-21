# ADR 0006: stronger workload-continuity proof — trusted host lifecycle witness research

Status: **PROPOSED** (post-merge correction under review — see note below)

This ADR is a research pass on top of ADR 0005, not a reopening of it. ADR
0005's conclusion — that no evidence composed of ordinary, copyable PVE/
guest/config state (Families A/B/C, its §6–§13) is sufficient for
`security_continuity: unverified -> trusted` — stands unchanged and is not
re-litigated here. ADR 0005 explicitly left open exactly one undesigned
class: "a hardware-rooted attestation chain unavailable from stock, or an
out-of-band Hubinet-managed identity provisioned and verified through a
channel that is not itself guest disk state" (§13), and fixed the minimum
properties any such future mechanism must satisfy (§14) without designing
it. This ADR is that follow-on research, directed specifically at the
**trusted host lifecycle witness** hypothesis, plus a comparative audit of
six further candidate families.

**Revision history note.** This ADR was independently reviewed and briefly
recorded as ACCEPTED. An automated PR review (Codex) subsequently raised
two P2 findings against that acceptance, both accepted as valid, one of
which (§7c's local-vs-remote `pmxcfs` conflation) was **load-bearing**: it
changed this ADR's classification of the single-node `pmxcfs`-filesystem-
watcher design and introduced a new, previously unaudited distributed
variant. That did **not** mean the automated review disproved the whole
ADR — it specifically narrowed the `pmxcfs`-witness conclusion (§7c, §8,
§9 Family A/A2, §10, §12). Status was reverted `ACCEPTED -> PROPOSED`, both
findings were corrected, and a fresh independent review of the corrected
material found no further P1/P2 findings, so this ADR was **re-accepted**
and merged as PR #44 (merge commit
`d6144f72164162a1de6c3a73aa23a771b317b05d`).

**Post-merge correction (this revision).** A *second* automated review
arrived after PR #44's merge and raised two further P2 findings, both
accepted as valid:

1. The merged text's single-node `pmxcfs`-watcher verdict (`NO-GO/
   insufficient`) overstated what the underlying research actually
   established. The prior revision correctly identified that Path A
   (same-node delivery) is **UNKNOWN**, that Path B's (cross-node
   delivery) "no mechanism found" finding was bounded to five checked
   files with several `src/pmxcfs/` files unchecked, and that the
   repository-wide absence claim was itself only **INFERENCE** — but then
   promoted that bounded search into a definitive "no coverage at all"
   NO-GO anyway. An unchecked file could emit a remote invalidation, or
   remote delivery could occur through a mechanism the `notify` substring
   search would not have matched. **Corrected: single-node `pmxcfs`
   watcher reclassified `NO-GO/insufficient -> UNRESOLVED/NOT FULLY
   AUDITED`** — this ADR does not claim it succeeds, only that the
   evidence assembled here is insufficient to prove the required
   cross-node blind spot across the *complete* `pmxcfs` remote-application
   path (§7c, §8, §9, §10, §12, §24).
2. §10's critical same-slot witness test demonstrated Family B's failure
   using a direct-`pmxcfs`/storage write — a T3-tier bypass. §5 explicitly
   forbids using T3 as the *load-bearing* reason for an anchor-less
   candidate's NO-GO, so that proof was internally inconsistent. **§10 is
   corrected to demonstrate Family B's failure using an in-scope T1/T2
   scenario** — ordinary API/CLI destroy+recreate, which *does* produce
   ordinary task records — showing why task-history-only evidence still
   cannot prove gapless continuity, because the task log is a bounded
   rotating window with no monotonic/gap-detectable cursor: sufficiently
   many further ordinary tasks between two observations can age the
   destroy/create pair out of the retained window, and the next
   observation has no cursor proving whether anything was missed. The
   direct-`pmxcfs` T3 observation is retained only as supplementary,
   out-of-scope threat-model context (§7b), never as the load-bearing
   proof.

Because a load-bearing accepted conclusion (finding 1) changed *after*
merge, Status reverted `ACCEPTED -> PROPOSED (post-merge correction under
review)` pending a fresh review pass over this corrected material, and PR
#45 recorded that pass.

**Full-review correction #2 (this revision).** A fresh, full end-to-end
independent architecture review of PR #45 found two further P2 findings
(P1 = 0), both accepted as valid, again requiring correction rather than
re-acceptance:

1. **Family B (task history)'s NO-GO was itself not proven.** §10's prior
   witness demonstrated only that a *stateless* observer — one that
   queries a task surface at O1 and again at O2 with no memory of what it
   saw in between — cannot distinguish "nothing happened" from "something
   happened but aged out of the retained window." That is a real
   limitation of a stateless design, but it is not a proof that **every**
   task-history-based witness must fail: a *stateful, fail-closed*
   witness that durably remembers an overlap sentinel (e.g. specific
   previously-visible UPIDs) at O1, and requires that sentinel to still
   be present in the retained set at O2 — treating its *absence* as a
   detected coverage gap forcing immediate revocation, rather than
   silence — was never audited. This ADR does not design or evaluate that
   protocol, and does not claim it succeeds; ADR 0002 is consistent with
   treating this as genuinely **UNKNOWN** rather than impossible ("trusted
   destroy/create event-chain evidence requires an accepted contract
   guaranteeing contiguous event/cursor semantics... for stock PVE this
   is currently **UNKNOWN**" — not a claim of impossibility). **Corrected:
   PVE task/event-history witness reclassified `NO-GO/insufficient ->
   UNRESOLVED/NOT FULLY AUDITED`** (§7a, §8, §9, §10, §12, §24).
2. **Surface A's facts were mismodeled.** The prior revision cited
   `broadcast_tasklist()`'s size cap as 128 KiB and described Surface A as
   populated only on `pvestatd`'s 10-second cycle. Primary-source
   re-verification shows the *executable* truncation loop in
   `proxmox/pve-cluster`'s `src/PVE/Cluster.pm` actually enforces **32
   KiB** (`while ($size >= (32 * 1024))`) — the 128 KiB
   (`CFS_MAX_STATUS_SIZE`) figure is only a `# TODO: update to 128 KiB in
   PVE 8.x` comment on code that still truncates at 32 KiB today. Separately,
   `PVE::RESTEnvironment::fork_worker()` calls `active_workers()` and
   `broadcast_tasklist()` **immediately at every worker start**, not only
   on `pvestatd`'s periodic cycle — the 10-second cycle is an *additional*
   periodic refresh (which also notices finished/crashed workers), not
   the sole update path. **Corrected: §7a now states the 32 KiB
   executable threshold as FACT-SOURCE, the 128 KiB comment as a
   documented-but-unimplemented TODO, and both the immediate
   worker-start broadcast and the 10-second periodic refresh as
   FACT-SOURCE update paths.** This ADR also no longer states that an
   ordinary API/CLI operation "produces a record on both surfaces" as an
   unconditional visibility guarantee — task **creation** (a UPID exists)
   is distinct from guaranteed **observation** in any one Surface A cache
   snapshot, which is a bounded, truncating view, not a durable ledger
   (§7a, §7d, §10, §24 item 5).

Because finding 1 changes another load-bearing conclusion, **Status
remains `PROPOSED (full-review corrections pending)`** — this ADR is
**not** re-accepted in this pass either. This is **not** a rollback of
ADR 0001–0005, and does **not** reopen R0. Unchanged throughout every
revision, including this one: **Blocker B remains OPEN; the future
positive Blocker-B mechanism ADR remains NOT STARTED / UNRESOLVED; WAVE
B1 remains DEFERRED / NOT AUTHORIZED; Phase 1C remains BLOCKED; R0
remains unchanged and strictly read-only.**

This ADR reaches **no NO-GO conclusion for any candidate family audited
against the two PVE task surfaces or either `pmxcfs`-filesystem-witness
variant** — narrowly scoped to what this ADR actually primary-source
audited (§9). Exact classification, corrected this revision:

```text
PVE task/event-history witness:                     UNRESOLVED / NOT FULLY AUDITED
single-node pmxcfs watcher (generic cluster-wide):  UNRESOLVED / NOT FULLY AUDITED
distributed per-node pmxcfs watcher:                UNRESOLVED / NOT AUDITED HERE
broader host-rooted/external mechanisms:            UNRESOLVED

Blocker B:                                          OPEN
future positive Blocker-B mechanism ADR:            NOT STARTED / UNRESOLVED
WAVE B1:                                             DEFERRED / NOT AUTHORIZED
Phase 1C:                                             BLOCKED
R0:                                                   unchanged / read-only
```

All three audited candidate families are now unresolved, for different,
non-overlapping reasons (§7a, §7c, §8, §9, §24):

- **task history:** the stateless O1/O2 analysis this ADR performed
  cannot rule out a stateful, fail-closed overlap-sentinel protocol that
  was never designed or audited here, and Surface A's own facts required
  correction (above);
- **single-node `pmxcfs` watcher:** Path A (same-node delivery) is not
  fully verified, and Path B's (cross-node delivery) absence is not
  exhaustively proven across every `pmxcfs` file and every
  remote-application mechanism;
- **distributed (A2):** Path A completeness is unverified at every node,
  which node dispatches/originates a given operation is unknown, and
  cross-node coverage/gap/restart/storage-layer semantics for a multi-node
  witness fleet were never designed.

**It is not a claim that every conceivable host-rooted lifecycle witness is
impossible**, and it is not a claim that any of the three audited families
in particular fails — it is a claim that this ADR's own research was not
exhaustive enough to prove any of them does or does not achieve the
required coverage. A genuinely different class of host-side enforcement —
kernel audit-subsystem/`auditd` rules, LSM hooks, `eBPF`-based syscall
interception, storage-layer block-change tracking, or a witness backed by
an explicitly root-resistant/external trust anchor — remains equally
**unresolved**, not disproven (§24 item 7). Consistent with the mission
that produced it:

- this ADR does not, by itself, close Blocker B for mutation authority —
  Blocker B remains **OPEN**, exactly as ADR 0005 left it;
- this ADR does not, by itself, authorize WAVE B1 — WAVE B1 remains
  **DEFERRED / NOT AUTHORIZED**;
- even eventual re-acceptance of *this* ADR would only mean the (now
  further corrected) research conclusion above is accepted as the current
  record — because that conclusion is negative/unresolved, acceptance
  would still **not** authorize WAVE B1 or grant `trusted` to anything;
  only a **different**, later ADR that actually proposes a sufficient
  mechanism could do that;
- Phase 1C remains **BLOCKED**; R0 remains unchanged and strictly read-only;
- this ADR does not amend ADR 0001, ADR 0002, ADR 0003, ADR 0004, or ADR
  0005; where it depends on their invariants it cites them and adds a new,
  narrower research layer on top, exactly as ADR 0003/0004/0005 each added
  their own layer without changing the others;
- this ADR authorizes no schema, persistence, hostd, HTTP API, HA control,
  mutation, or enrollment-automation implementation of any kind, and does
  not change `security_continuity` in code.

## 1. Context / problem

ADR 0005 closed the research question "can stock PVE + ordinary read-only
config evidence safely provide generic persistent workload security
continuity?" with an honest **no**, while leaving open the *one* class that
could ever close Blocker B: a mechanism whose proof does not live entirely
inside state an ordinary destroy/recreate (with disk/config copy) can
reproduce (ADR 0005 §13). The operator has asked whether that gap can be
closed by a specific, concretely proposed candidate — a **trusted host
lifecycle witness**: an opaque, backend-generated `workload_epoch_id`,
stored outside guest/config/disk state, maintained by a trusted host-side
observer that watches identity-breaking lifecycle *events* directly (not
guest-readable state after the fact), and revokes/regenerates the epoch on
any observed or unobservable break in coverage.

This is a materially different claim from anything ADR 0005 evaluated:
Families A/B/C all inspected guest-readable state *after* a possible
replacement and asked whether it looked different. The witness hypothesis
instead asks whether the *transition itself* (destroy, create, clone,
rollback, restore) can be observed, cryptographically or at least durably
attributed to a specific `(inventory_source_id, vmid)` slot, with **provable
gapless coverage**, independent of what the resulting guest configuration
contains. This ADR audits whether real, current Proxmox VE actually
provides a channel capable of supporting that claim.

This audit is bounded to two concrete, primary-source-verifiable
observation classes: Proxmox's own task history — modeled as two
materially different surfaces, the `/cluster/tasks` recent status cache
and the `/nodes/<node>/tasks` bounded node-local archive (§7a) — and
`pmxcfs` filesystem-level change observation, the latter further split
into a *single-node* variant (one watcher, one node) and a *distributed*
variant (one watcher per relevant PVE node) (§7). **Corrected this
revision (full-review correction #2): none of the three audited
candidates is shown to fail to a NO-GO conclusion here.** Task history
was previously audited to a NO-GO using a *stateless* observer analysis;
that analysis does not rule out a *stateful*, fail-closed overlap-sentinel
witness protocol, which this ADR does not design or audit (§7a, §10, §24).
The single-node `pmxcfs` variant's search was not exhaustive enough to
prove the required cross-node blind spot across the complete `pmxcfs`
remote-application path (an unchecked file, or a remote-delivery mechanism
not matched by a `notify`-substring search, could still exist), and the
distributed variant was never audited at all. All three are classified
**UNRESOLVED**, for distinct reasons (§7a, §7c, §8, §24 item 7/8/10). This
ADR does not extend to, and does not reach a conclusion about,
fundamentally different host-rooted enforcement classes — the Linux
kernel audit subsystem/`auditd`, LSM hooks, `eBPF`-based syscall
interception, storage-layer block-change tracking, or a witness
deliberately backed by an explicitly root-resistant or fully external
trust anchor. Those remain genuinely **unresolved** after this ADR as
well, not proven impossible (§24 item 7).

## 2. Scope and non-goals

In scope: the trusted host lifecycle witness hypothesis; six further
comparison families (§9); primary-source research on Proxmox VE task/event
observability and `pmxcfs` filesystem-change observability, precisely
separating locally-originated (same-node) VFS-level file-change delivery
from remotely-replicated (cross-node, Corosync-originated) delivery (§7c);
classifying — but not designing — a distributed, per-node
`pmxcfs`-filesystem-watcher variant as its own candidate, distinct from the
single-node variant; the adversarial same-slot destroy/recreate test
against each family; the explicit trust-root separation between node/hostd
compromise and resource continuity; and the extended minimum-property list
any future mechanism must satisfy.

Explicitly **not** in scope, and not authorized by this ADR:

- any schema, table, column, or enum-value implementation;
- any bump of the authority schema version (currently `5`, merged on
  `main`); this ADR does not authorize a schema v6 package;
- self-acceptance of this ADR by the agent that wrote it;
- designing, implementing, or partially wiring `hostd`, a witness daemon
  (single-node or distributed), an enrollment ceremony, or any HA control
  surface — the distributed per-node `pmxcfs`-watcher variant is
  *classified* here (§7c, §9), never *designed*;
- writing anything into a guest's configuration, disk, or vTPM state (that
  would be a mutation; no mutation authority exists);
- changing `security_continuity` in code, or any change to production
  startup, scheduler, HTTP, or Home Assistant wiring;
- any change to ADR 0001, ADR 0002, ADR 0003, ADR 0004, or ADR 0005;
- marking this ADR ACCEPTED (that is a separate, later decision after
  independent review) or WAVE B1 authorized under any circumstance;
- any weakening of ADR 0001's invisible same-slot destroy/recreate
  limitation, ADR 0003's epoch authority-eligibility rule, or ADR 0005's
  Family A/B/C rejection.

## 3. Existing accepted invariants that remain unchanged

1. `resource_id`/`binding_id`/`locator_generation`/
   `resource_continuity_revision` remain exactly as ADR 0001 defines them;
   `resource_continuity_revision` is the sole resource-level security/
   concurrency token (ADR 0001, `0.5-inventory-model.md`).
2. `security_continuity` (`unverified`/`trusted`/`revoked`) has exactly one
   durable owner, `resource_incarnations.security_continuity` (ADR 0001,
   ADR 0005 §15); no future mechanism may introduce a second authoritative
   copy or a fourth canonical value (ADR 0005 §17, §26).
3. ADR 0005's Family A (ordinary stock fields), Family B (operator assertion
   alone), and Family C (administrative correlation marker, any entropy) are
   rejected as sufficient for `trusted`, permanently, for the reasons given
   there (§7–§11). This ADR does not re-audit them.
4. ADR 0005 §14's minimum-property list applies to any future mechanism,
   including the one evaluated here; §13 of this ADR extends it with
   findings specific to the witness hypothesis, it does not replace it.
5. ADR 0003's `source_attestation_epoch` authority-eligibility rule applies
   unchanged: evidence recorded under an old epoch is not authority-eligible
   under a newer one, and (§27, §29 witness 18) a resource whose `trusted`
   depended on epoch-stale proof must transition to `revoked` absent an
   accepted carry-forward procedure. Any future Blocker-B mechanism must
   satisfy this, not re-derive it.
6. Node/hostd trust (`node_bindings`/`node_attestations`, ADR 0001) is a
   separate axis from resource continuity; ADR 0005 §18/§21 already forbid
   collapsing them. §11 below extends this separation specifically to the
   witness hypothesis's own trust root.
7. R0 remains read-only; ADR 0005 §24's list of what R0 must never do
   (grant `trusted`, run enrollment automation, write a marker into guest
   config, expose writable HA controls, enable policy/jobs/mutation, treat
   any copyable evidence as security authority) is unaffected by this ADR
   regardless of its conclusion.
8. `discovery_runs`/`source_runtime_health`/CAS discipline (ADR 0002) and
   ADR 0003 §19a's read-then-write remote-evidence pattern remain the
   template any future mechanism's own remote reads must follow.

## 4. Terminology

| Term | What it is |
| --- | --- |
| **Identity-breaking lifecycle event** | an event after which the actual physical/logical occupant of a slot — its disk content and/or running process, not merely its PVE config metadata — may no longer be the same workload: destroy+create, clone, snapshot rollback, or backup restore. See the three-way distinction immediately below the table; this ADR does not treat ordinary config mutation, or config-metadata churn alone, as identity-breaking. |
| **Trusted host lifecycle witness** | a hypothesized trusted observer (process/component) that watches identity-breaking lifecycle events for a source, independent of guest-readable config/disk content |
| **`workload_epoch_id`** | a hypothesized opaque, backend-generated value per slot, incremented (or revoked) whenever the witness observes, or cannot prove the absence of, an identity-breaking event; stored only in Hubinet's own authority state, never in guest config/disk/vTPM |
| **Witness coverage epoch** | the interval during which the witness can prove continuous, gapless observation for a given slot |
| **Node/hostd trust root** | ADR 0001's existing, separate claim that a specific physical/virtual PVE node and its control-plane software are not compromised — a prerequisite this ADR's candidates may or may not additionally require, never something they can grant to *resource* continuity by assumption |
| **Root-resistant / external trust anchor** | a cryptographic or structural root of trust *not* extractable, killable, or forgeable by a root-shell user on the node being observed — e.g. a hardware-sealed key, a secure enclave, or a fully out-of-band logging channel the node cannot silently rewrite. See §5/§11: a witness lacking one is not a defense against T3. |
| **Task / UPID** | Proxmox's per-operation identifier and log record for an asynchronous worker (`PVE::UPID`); see §7 |
| **`pmxcfs`** | the cluster-replicated configuration filesystem backing `/etc/pve`, implemented as a FUSE mount over an internal SQLite database (`/var/lib/pve-cluster/config.db`), synchronized cluster-wide via Corosync |

**Three distinct concepts, not to be conflated** (this corrects an earlier
looseness in this ADR that risked treating config-metadata churn as if it
were, by itself, a workload replacement):

1. **Ordinary config mutation** — editing memory/CPU/description/tags/etc.
   on an existing guest. ADR 0001 already establishes this preserves
   resource identity; it is never, by itself, identity-breaking, and
   nothing in this ADR treats it as such.
2. **Deletion/recreation of PVE config metadata** — removing and rewriting
   the `.conf` object in `pmxcfs` for a given `(inventory_source_id, vmid)`
   slot. This is an operation on *metadata*, not necessarily on the
   underlying disk/process — a sufficiently privileged actor could delete
   and recreate a `.conf` file while leaving the actual disk image
   byte-for-byte untouched, in which case the physical/logical occupant has
   not changed at all, only its config record was rewritten. §7b's finding
   is precisely that *this specific metadata-lifecycle event* can happen
   without generating a task/UPID — a finding about **observability of
   metadata lifecycle**, not, by itself, a claim that the underlying
   workload changed.
3. **Actual physical/logical workload replacement** — the disk content
   and/or running process backing a slot is genuinely destroyed and
   replaced. This is what §10's same-slot witness test is actually about:
   does B (a genuinely different occupant) inherit A's trust?

The realistic attack §7b/§10 describe combines (2) and (3): an actor who can
write `pmxcfs` directly typically also controls the storage layer well
enough to replace the disk content in the same operation (e.g. by pointing
the recreated config at different storage, or by directly overwriting the
volume) — producing an actual occupant replacement (3) with no task/UPID
trace, because the config half of that combined operation (2) bypassed the
task-generating path. §7b's finding stands for this **combined, realistic**
case; it does **not** claim that rewriting the `.conf` file alone, with the
disk genuinely untouched, constitutes a workload replacement by itself — it
does not, under (2) alone.

## 5. Threat model

ADR 0005's threat model (§5 there) applies unchanged and is not restated in
full. This ADR requires, per the mission, an explicit split into four tiers
of actor, because different candidate mechanisms are defensible against
different subsets:

| Tier | Actor | Example action |
| --- | --- | --- |
| **T1 — ordinary PVE operator action** | an authenticated operator using the normal GUI/API/CLI, in good faith or by mistake | destroy VM100, recreate a different guest at the same VMID |
| **T2 — PVE admin action** | a user with full administrative PVE privilege (`Sys.Modify`/`VM.Allocate`/`Realm.AllocateUser`-class), still acting through PVE's own management surface | bulk-recreate guests via API/CLI scripting, disable auditing features, rotate certificates |
| **T3 — direct root tampering** | a party with root shell access to any one cluster node | directly edit/delete `/etc/pve/nodes/<node>/qemu-server/<vmid>.conf`, manipulate disk images on storage directly, stop/restart PVE daemons |
| **T4 — compromised node/hostd trust root** | the PVE host's control-plane software itself (`pvedaemon`, `pmxcfs`, or any future Hubinet-managed host-resident witness component) is compromised or malicious | fabricate task history, suppress task creation, fabricate lifecycle events, lie to a co-resident witness |

No candidate audited below is expected, or required, to defend against T4 —
that is the existing, separate node/hostd trust gate (§11; ADR 0001 node
section). T1 and T2 are the tiers every candidate should ideally defend
against, and the answer (detected / fail-closed-though-undetected / silently
defeated) must be stated for each, not glossed over.

**T3 requires a precise boundary, not an informal "partially defended"
status (§11/§11a resolve this precisely).** A witness process **co-resident** on the
node it observes — running as an ordinary process subject to that node's
root — is **not** a defense against T3 merely by existing: a root-shell
actor on that node can kill it, patch it, or feed it fabricated events,
exactly as at T4. **Unless a candidate specifies an explicit root-resistant
or external trust anchor** (table above) that a root-shell user on that node
cannot extract, disable, or forge, **T3 must be treated as equivalent to
T4 for that candidate — out of scope, not a bounded, partially-defensible
gap.** None of the families audited in §9 specifies such an anchor. This is
the same discipline ADR 0005 §5/§28 already applies, extended with the
T1–T4 tiering the mission requires, applied specifically to
lifecycle-observation-based candidates, which ADR 0005 did not evaluate.

**Consistency rule (corrects an internal contradiction identified this
reopening).** Because T3 is declared out of scope for an anchor-less
witness, it must never simultaneously be used as the *load-bearing* reason
that same witness's NO-GO rests on — a tier this ADR excludes from scope
cannot also be the reason a candidate fails within that scope. Any NO-GO
this ADR reaches for an anchor-less candidate must rest on what happens at
the in-scope tiers, T1/T2, plus any genuine, privilege-tier-independent
architecture gap (e.g. a cross-node coverage gap that would defeat even an
entirely ordinary T1 action, §7c) — never on the T3 bypass alone. §7c, §8,
and §10 are corrected accordingly.

## 6. Required security property (restated and extended from ADR 0005 §14)

The controlling test, unchanged from ADR 0005:

```text
if a mechanism cannot distinguish its intended security claim after an
adversary or ordinary workflow has copied all the state it relies on
into another incarnation, it cannot be sufficient for persistent
canonical trusted
```

This research adds one further, necessary condition, discovered by auditing
what Proxmox actually documents about its own lifecycle-observability
surface (§7):

```text
if a mechanism's claimed coverage completeness depends on a Proxmox VE
contract that Proxmox does not document or guarantee — retention,
monotonic ordering, gapless delivery, or notification completeness — that
mechanism cannot treat its own completeness as a security boundary; an
undocumented behavior that merely "seems to always happen" is evidence,
never proof, exactly as ADR 0001/0002 already rule for task history and
ADR 0002 rules for interval-wide ACL consistency
```

Both tests apply to every candidate in §9. **Neither test, nor this ADR's
conclusion, is a claim about every conceivable host-rooted witness design.**
They are applied here against the specific observation channels this ADR
primary-source audited (§7): Proxmox's own task history (both surfaces,
§7a), and the *single-node* variant of `pmxcfs` filesystem-level change
observation. **Corrected this revision (full-review correction #2): none
of them is shown to actually fail these tests to a NO-GO conclusion.**
Task history was previously found to fail via a *stateless* observer
analysis (query, then query again, with no memory in between) — but that
analysis does not rule out a *stateful*, fail-closed witness that durably
tracks an overlap sentinel between observations (§7a, §10); this ADR does
not design or audit that protocol, so task history is classified
UNRESOLVED, not NO-GO. The single-node `pmxcfs` variant was likewise
attempted against these tests, but the underlying research is not
exhaustive enough to conclude it fails them either — also classified
UNRESOLVED (§7c, §8, §24 item 7/8). A witness built on a genuinely
different channel — kernel audit/LSM/`eBPF`-based enforcement, one backed
by an explicit root-resistant/external trust anchor (§5), or the
*distributed*, per-node `pmxcfs`-filesystem-watcher variant (§7c) — was
not audited here at all and remains equally unresolved (§24 item 7).

## 7. Primary-source findings: Proxmox VE lifecycle/task/`pmxcfs` observability

Legend, identical to ADR 0001/0002/0003/0005's own discipline: **FACT-DOC**
(documented by Proxmox), **FACT-SOURCE** (behavior visible in official
Proxmox source, read this session), **INFERENCE** (architectural conclusion
from the facts), **UNKNOWN** (not confirmed by an official contract).

### 7a. Two materially different PVE task surfaces — corrected this revision (P2 #1)

**A prior revision treated PVE task history as one generic "rolling log."
That conflated two structurally different surfaces, each with its own
retention/cursor properties. Neither surface is backed by the other's
storage, and both are bounded — but for different reasons.** Both are
identified by `UPID` (`node:pid:pstart:starttime:type:id:user:`), assembled
from the node name, worker OS PID, the PID's start time (`pstart`, used to
disambiguate PID reuse), a wall-clock start timestamp, worker type, target
ID, and acting user (`PVE::UPID::encode`, `proxmox/pve-common`,
`PVE/RESTEnvironment.pm`). **A UPID is not a monotonic counter and does not
reference a prior UPID** — there is no chain, hash-link, or sequence field
tying one task to "the task before it" for a given slot; wall-clock time is
not concurrency authority (mirrors ADR 0002's own rule for
`source_config_revision`/timestamps generally). This holds for both
surfaces below.

**Surface A: `GET /cluster/tasks` — a recent, cluster-wide status-cache
view, not the durable archive.**

- **FACT-SOURCE.** `GET /cluster/tasks` (`PVE::API2::Cluster`, the `tasks`
  route) is documented in its own route registration as *"List recent
  tasks (cluster wide)"* — the word "recent" is the route's own
  description, not this ADR's characterization. It calls
  `PVE::Cluster::get_tasklist()` and filters results by caller privilege
  (own tasks unless `Sys.Audit`); the handler does not sort, page, or
  attach any sequence/cursor field of its own.
- **FACT-SOURCE.** `PVE::Cluster::get_tasklist()` (`proxmox/pve-cluster`,
  `src/PVE/Cluster.pm`) reads a **per-node, corosync-distributed
  in-memory/KV status cache** (`ipcc_get_status("tasklist", $node)`,
  version-checked against a per-node `kvstore` entry, with a local
  `$tasklistcache`) — this is **not** the same storage as the archive
  files in Surface B below.
- **FACT-SOURCE, corrected this revision (full-review correction #2,
  P2 #2).** This cache is updated on **two** paths, not just a periodic
  one: `PVE::RESTEnvironment::fork_worker()` (`proxmox/pve-common`) calls
  `$self->active_workers($upid, $sync)` and
  `$self->broadcast_tasklist($tlist)` **immediately at every worker
  start**; separately, `pvestatd` (`proxmox/pve-manager`,
  `PVE/Service/pvestatd.pm`) additionally re-runs `active_workers()` +
  `broadcast_tasklist()` on its own periodic `update_status()` loop every
  **10 seconds** (`my $updatetime = 10`), which among other things notices
  finished/crashed workers and refreshes cluster status. The prior
  revision incorrectly described the 10-second cycle as the *only* update
  path; it is an *additional* one.
- **FACT-SOURCE.** `active_workers()` (`proxmox/pve-common`,
  `PVE/RESTEnvironment.pm`) retains **all currently running tasks**, plus
  at most **`MAX_FINISHED = 25`** additional recently-finished tasks — an
  explicit, small, fixed cap on how many *completed* tasks this surface
  ever carries, independent of the archive's own size limit (Surface B),
  **before** the payload-size truncation below is applied.
- **FACT-SOURCE, corrected this revision (full-review correction #2,
  P2 #2).** `broadcast_tasklist()`'s *executable* truncation loop
  (`proxmox/pve-cluster`, `src/PVE/Cluster.pm`) is:
  `while ($size >= (32 * 1024)) { pop @$data; ... }` — an actual,
  currently-enforced cap of **32 KiB**, not 128 KiB. The 128 KiB figure
  (`CFS_MAX_STATUS_SIZE`, `pmxcfs`'s own status-object limit) appears only
  in a code comment — `# TODO: update to 128 KiB in PVE 8.x` — describing
  a change that, in the source checked this session, has **not** been
  made: the code still truncates at 32 KiB. This ADR does not label 128
  KiB as the effective FACT-SOURCE cap; the executable 32 KiB threshold is
  the load-bearing figure, with the unimplemented 128 KiB comment recorded
  only as documented intent, not current behavior.
- **Conclusion for Surface A:** this is a **recent status-cache surface
  only** — bounded to (at most) 25 finished tasks cluster-wide per node,
  updated immediately at every worker start and additionally refreshed
  every 10 seconds, with its serialized payload actually truncated at 32
  KiB (not 128 KiB), no durable retention, and no cursor. A sufficiently
  busy ordinary T1/T2 interval (more than ~25 further finished tasks, or
  enough further tasks to exceed 32 KiB serialized, on the same node
  before the next check) can cause a completed task's record to disappear
  from this surface entirely, independent of the archive files in Surface
  B. **Task creation (a UPID exists) is distinct from guaranteed
  observation in any one Surface A snapshot** — Surface A is a bounded,
  truncating status-cache view, not a durable ledger of every task ever
  created.

**Surface B: `GET /nodes/<node>/tasks` — the bounded node-local archive.**

- **FACT-SOURCE.** `GET /nodes/<node>/tasks` (`PVE::API2::Tasks`,
  `node_tasks`) reads the **node-local, persisted** archive: it opens
  `/var/log/pve/tasks/index` and `/var/log/pve/tasks/index.1` (via
  `File::ReadBackwards`) and supports `start`/`limit`/`since`/`until`/
  `source` (`archive`/`active`/`all`, default `archive`), plus
  `userfilter`/`typefilter`/`vmid`/`errors`/`statusfilter` — but **no
  monotonic or gap-detectable cursor parameter of any kind**.
  `/var/log/pve/tasks/` is a regular node-local path, not a
  `pmxcfs`/`/etc/pve` path, so this archive is per-node, not
  cluster-replicated.
- **FACT-SOURCE.** The same `RESTEnvironment.pm` that defines
  `active_workers()` also rotates this archive: the active `index` file is
  capped at a **fixed size threshold** (`my $maxsize = 50000; # about 1000
  entries`); when exceeded, it is renamed to `index.1` and a fresh `index`
  begins. This is a **bounded rolling window**, not a permanent,
  officially size-unbounded audit trail. Community-observed defaults
  report a further archive-file rotation (roughly 512 KiB per archive
  file, newest ~20 archive files retained, on the order of ~100,000 total
  entries) — this specific figure is **UNKNOWN** at FACT-DOC/FACT-SOURCE
  strength this session (forum-sourced, not independently re-derived from
  source here) and is cited only as corroborating context, not the
  load-bearing claim. The load-bearing claim is the confirmed-in-source
  rotation-on-size behavior itself.
- **Conclusion for Surface B:** a bounded, node-local archive with no
  monotonic/gap-detectable cursor. A witness relying on it cannot
  distinguish "no destroy/create pair occurred" from "one occurred, but
  its records have since rotated past `index`/`index.1` into an untracked
  older archive."

**Combined conclusion, corrected this revision (full-review correction
#2, P2 #1).** Neither surface is backed by the other's storage, and
neither carries an officially documented, monotonic, gap-detectable
cursor of its own (consistent with ADR 0002 §"Kiedy dokładnie wolno
ustawić `confirmed_removed`", Class B — this ADR's own research reaches
the identical conclusion ADR 0002 already reached and marked **UNKNOWN**,
expected corroboration, not a new finding). This shows that a
**stateless** witness — one with no memory of its own between
observations, relying purely on whatever either surface happens to still
show at query time — **cannot** prove gapless coverage from either
surface alone (§8, §10). **It does not show that a *stateful*,
fail-closed witness necessarily fails.** A witness that durably remembers
its own overlap sentinel (e.g. specific previously-observed UPIDs) between
observations, and treats the sentinel's *absence* from the currently
retained set as a detected coverage gap forcing immediate revocation
rather than silent continuation, was never designed or audited by this
ADR — whether such a protocol can actually be built on Surface A and/or
Surface B's specific retention/structure is left genuinely open (§10,
§24). A witness's own continuously-running, independently durable
observation remains necessary either way — PVE's own task retention (of
either kind) is never sufficient by itself — but this ADR does not
conclude that no such witness design can succeed (§8).

### 7b. Task creation coverage for destroy/create/clone/rollback/restore

- **FACT-DOC/INFERENCE.** Ordinary `qm`/`pct` CLI and their API
  equivalents execute destroy, create, clone, migrate, and restore
  operations as asynchronous background workers, each assigned a UPID and
  logged exactly like any other task — this is long-standing, widely
  documented Proxmox behavior (visible directly in ordinary GUI/CLI use:
  every such action returns/records a UPID) and consistent with the task
  infrastructure confirmed in §7a. This session did not re-derive the exact
  route-registration source line for every one of destroy/create/clone/
  rollback/restore (flagged **UNKNOWN** at that specific citation
  granularity); the general pattern itself is not in serious doubt.
- **INFERENCE, the actual gap.** Coverage-via-task-creation is a property of
  the **high-level API/CLI path**, not of the underlying storage. `pmxcfs`
  (`/etc/pve`) is a normal, directly writable POSIX-like mount to anyone
  with sufficient privilege on any cluster node (T3, §5) — nothing in the
  filesystem layer itself requires that a guest's config file be
  created/deleted only via a task-wrapped worker. A party who can write to
  `/etc/pve` directly (root on any member node) can delete/recreate a
  guest's **config metadata object** without ever invoking `qm`/`pct`/the
  API at all, producing **no task, no UPID, no log entry**, for that
  metadata event. By itself, per §4's three-way distinction, rewriting the
  `.conf` object with the disk genuinely untouched is not a workload
  replacement — but nothing in stock PVE prevents the same node-root actor
  from **also** replacing the underlying disk/process in the same window
  (e.g. by pointing the recreated config at different storage, or
  overwriting the volume directly), producing a genuine occupant
  replacement (§4 concept 3) with **no task/UPID trace for the combined
  operation**. This is not a stock-PVE defect — it is an inherent
  consequence of `pmxcfs` being a shared, directly-writable configuration
  store rather than an access-mediated service boundary. Per §5's boundary:
  this specific gap is a **T3-tier** capability (a node-root actor, not an
  exotic privilege), and — for a witness that is co-resident on that same
  node without an explicit root-resistant/external trust anchor — it is
  equivalent in effect to T4, not a lesser, bounded gap (§11). **This
  finding is retained only as supplementary, out-of-scope threat-model
  context (§5's consistency rule) — it is never the load-bearing reason
  for Family B's or Family A's UNRESOLVED classification; §10's critical
  same-slot test uses an in-scope T1/T2 scenario instead (corrected
  post-merge, P2 #2).**
- **Distinction restated, corrected this revision (full-review correction
  #2, P2 #2): task *creation* is not the same claim as guaranteed task
  *observation*.** The bullets above establish that an in-scope T1/T2
  destroy/create/clone/migrate/restore operation genuinely creates a UPID
  task record. They do **not** establish that any later query of either
  Surface A or Surface B (§7a) is guaranteed to still show that record —
  Surface A is a small, actively-truncating status-cache view and Surface
  B is a size-rotated archive, and both bound *how long* a created record
  remains observable, independent of whether it was created at all (§7a,
  §10).

### 7c. `pmxcfs` FUSE architecture and file-change notification

- **FACT-SOURCE.** `pmxcfs`'s FUSE operation table
  (`proxmox/pve-cluster`, `src/pmxcfs/pmxcfs.c`) registers exactly:
  `getattr, readdir, mkdir, rmdir, rename, open, read, write, truncate,
  create, unlink, readlink, utimens, statfs, init, chown, chmod`. **No
  kernel-notification callback of any kind is registered in this table** —
  no call to `fuse_lowlevel_notify_inval_entry`, `fuse_notify_poll`, or any
  equivalent low-level invalidation/notification primitive appears in the
  `fuse_operations` struct itself.
- **FACT-SOURCE, broadened this pass.** The same absence — zero occurrences
  of the substring `notify` in any form — was independently confirmed this
  session across four further core `pmxcfs` implementation files:
  `server.c` (the daemon/dispatch loop), `dfsm.c` (the distributed
  finite-state-machine/Corosync message-application layer — the code most
  architecturally likely to apply a remote node's change locally),
  `memdb.c` (the in-memory/SQLite-backed database layer), and
  `cfs-plug-memdb.c` (the FUSE plugin backing ordinary file content from
  that database). This is **five of roughly a dozen files** under
  `src/pmxcfs/`; the remainder (`cfs-plug.c`, `cfs-plug-link.c`,
  `cfs-plug-func.c`, `cfs-utils.c`, `database.c`, `status.c`, `loop.c`,
  `dcdb.c`) were **not** individually re-checked this session and are
  **UNKNOWN** at this specific citation granularity — this is not a claim
  of an exhaustive, whole-repository search.
- **INFERENCE, bounded to what was actually checked.** Because the FUSE
  operation table itself and the four files most architecturally likely to
  apply a remote (Corosync-originated) change — the dispatch loop, the
  distributed-state-machine layer, the database layer, and the FUSE content
  plugin — contain no notification call between them, the specific, narrow
  claim this ADR makes is: **no evidence was found, in the files checked,
  that a Corosync-originated change on another node is ever translated
  into a local kernel dentry/inode invalidation.** The broader claim that
  *no file anywhere in the repository* does this is **not** independently
  verified to that standard this session and is marked **INFERENCE**, not
  **FACT-SOURCE**, at that broader scope.
**Corrected this reopening (P2 #1): two structurally different delivery
paths must not be conflated, and were conflated in the prior revision.**

- **Path A — same-node, locally-originated VFS operations.** When a process
  running *on the same node* issues an ordinary syscall (`unlink()`,
  `create()`, `write()`, `rename()`) against the `pmxcfs` FUSE mount, that
  syscall is dispatched through the normal Linux VFS layer before FUSE ever
  sees it. Linux's `fsnotify`/`inotify` hooks fire as part of the VFS's own
  syscall-handling path (`vfs_unlink`, `vfs_create`, etc.), independent of
  the underlying filesystem type — this is general, well-established VFS
  behavior, not something the FUSE filesystem driver itself has to
  implement via `fuse_lowlevel_notify_*` calls. **The absence of a
  `fuse_lowlevel_notify_inval_entry`/`fuse_notify_poll`-class callback in
  `pmxcfs` (confirmed above) says nothing about this path** — those
  low-level notify primitives exist for a FUSE server to proactively push
  an invalidation for a change the *kernel does not already know about*
  (see Path B); they are not required for the kernel to notice a syscall it
  itself just dispatched. **This ADR has not independently, primary-source
  verified that `pmxcfs` specifically delivers correct `fsnotify` events for
  every relevant local operation** (e.g. whether any FUSE mount option
  `pmxcfs` sets suppresses this, or whether every kernel version behind
  supported PVE releases behaves identically) — so same-node,
  locally-originated delivery is classified **plausible / expected per
  general Linux VFS behavior, but UNKNOWN at FACT-DOC/FACT-SOURCE strength
  for `pmxcfs` specifically**, not proven, and — critically — **not proven
  incomplete either**. The prior revision's claim that local-node delivery
  has "no documented completeness guarantee" overstated what the FUSE
  operation-table finding actually shows and is withdrawn.
- **Path B — remotely-replicated / behind-the-mount changes.** When a
  change originates on a *different* node and reaches this node only via
  Corosync (applied inside `pmxcfs`'s own in-memory/SQLite state, not via a
  syscall on this node's mount), the local kernel has no syscall to hook —
  the *only* way the kernel could learn of it is if `pmxcfs` explicitly
  pushes a low-level invalidation/notification call. This is exactly what
  §7c's FUSE-operation-table finding, and the four further files checked,
  found **absent**. This is the structurally correct scope for that
  finding, and for the FUSE/`virtiofs` `inotify`-support RFC status below
  (that RFC thread is itself about surfacing exactly this class of
  behind-the-mount, non-local-syscall change — the `virtiofs` guest-side
  case is architecturally analogous to this cross-node case, not to Path A).
- **FACT-SOURCE, general kernel limitation, independent of `pmxcfs`, pinned
  to when it was checked, and scoped to Path B.** Linux `inotify`/`fanotify`
  are documented as supported for local kernel filesystems for ordinary,
  locally-dispatched syscalls (Path A); the historical reliability gap is
  specifically about surfacing changes that do not arrive via a local
  syscall (Path B class). As of the most recent upstream activity located
  this session — a Linux kernel mailing list thread on disallowing
  `inotify` watches on unsupported filesystems, with discussion as recent
  as May 2025 confirming FUSE/`virtiofs` support for propagating exactly
  this class of behind-the-mount change was still **not merged into the
  mainline kernel** at that date — `inotify_add_watch()` can silently
  succeed without error on a filesystem that does not actually deliver
  such events, and kernel-level support for this specific case remains, at
  best, an RFC-stage patch series (originally posted ~October 2021,
  targeting `virtiofs`). **This is a time-bound finding, current as of the
  mid-2025 activity located this session (this research was performed
  August 2026) — not a permanent architectural fact**, and it bears on
  Path B, not on ordinary local `fsnotify` delivery (Path A). A later
  mainline kernel release could merge this support; any future ADR relying
  on this finding must re-verify the current kernel/FUSE state at
  implementation time, not cite this ADR's date as still current.
- **Conclusion of §7c, precisely scoped and corrected.** For **Path B**
  (cross-node, Corosync-replicated changes), within the five core files
  checked this session, **no mechanism was found** by which such a change
  would reach a local kernel dentry/inode invalidation on a *different*
  node's watcher — a bounded, sourced negative finding (§24 item 8), not an
  exhaustive proof that no code path anywhere in `pmxcfs` ever could. For
  **Path A** (same-node, locally-originated operations), this ADR makes
  **no negative finding at all** — local delivery is plausible/expected per
  general Linux VFS behavior and is classified **UNKNOWN**, pending
  primary-source verification specific to `pmxcfs`, not disproven.
- **New candidate this reopening identifies but does not audit: a
  distributed, per-node `pmxcfs`-filesystem-watcher** — one watcher process
  per relevant PVE node, each relying only on Path A (its own node's local
  `fsnotify` delivery), such that whichever node actually executes a given
  identity-breaking operation's local syscall has its own watcher observe
  it directly, without needing Path B at all. This design was **not**
  audited by this ADR: it depends on Path A's completeness (itself
  UNKNOWN above), on which node actually executes a given operation's
  syscall (itself UNKNOWN, §7b), on distributed coverage/gap/restart
  semantics across every node in a source (never designed here), and on
  whether it would also need independent coverage of the storage layer for
  the disk-replacement half of §4 concept 3's combined attack (never
  audited here). It is classified **UNRESOLVED / NOT AUDITED HERE** (§8,
  §9, §24 item 7) — this ADR does not claim it succeeds, and does not
  claim it fails.

### 7d. Summary table

| Property required for a witness | Stock PVE support |
| --- | --- |
| Monotonic, gapless task/event cursor, native to either PVE task surface | **No** — UPID is not a sequence; both Surface A and Surface B are independently bounded (§7a) |
| Officially guaranteed task-history retention (either surface) | **No** — Surface A truncates at `MAX_FINISHED=25` tasks and 32 KiB serialized payload, refreshed at worker-start and every 10s; Surface B rotates at a fixed size threshold (`index`/`index.1`); neither is a documented, permanent ledger (§7a) |
| Whether a *stateful*, fail-closed overlap-sentinel witness could compensate for either surface's boundedness | **UNRESOLVED / NOT DESIGNED OR AUDITED HERE** — this ADR only shows a *stateless* observer cannot; a witness that durably tracks its own sentinel between observations was never evaluated (§7a, §10, §24 item 10) |
| Task creation for *every* identity-breaking event | **No** — only for the high-level API/CLI path; direct `pmxcfs` write bypasses it entirely (§7b) — a T3-tier gap, supplementary/non-load-bearing (§5) |
| Reliable same-node, locally-originated `pmxcfs` change delivery (Path A: ordinary VFS `fsnotify` for a syscall issued on the watched node itself) | **UNKNOWN — plausible/expected per general Linux VFS behavior, NOT proven, and NOT disproven by this ADR** (corrected this reopening, P2 #1). The absent FUSE notify-callback finding does not bear on this path (§7c) |
| Reliable cross-node `pmxcfs` change delivery for Corosync-replicated writes (Path B: a change applied on this node only via Corosync, no local syscall) | **No mechanism found in the five core files checked**, and FUSE-level support for exactly this class of behind-the-mount notification remained RFC-stage as of the mid-2025 kernel status checked; not verified as an exhaustive whole-repository absence (§7c, §24 item 8) |
| Distributed, per-node `pmxcfs` watcher relying only on Path A per node | **UNRESOLVED / NOT AUDITED HERE** — depends on Path A's completeness (above, unproven), on which node executes a given operation (§7b, UNKNOWN), and on undesigned distributed coverage/gap semantics (§7c, §8, §9) |

## 8. The trusted host lifecycle witness hypothesis — evaluated against §7

Working through the eleven properties the hypothesis requires, using §7's
findings:

1. **Explicit operator enrollment of the current occupant.** Achievable in
   principle (an audited, explicit act) — this property does not depend on
   §7's findings and is not where the hypothesis fails.
2. **Opaque `workload_epoch_id` stored outside guest/config/disk/vTPM
   state.** Achievable in principle — storing it only in Hubinet's own
   authority database is a sound design choice on its own, and is in fact
   already implied by ADR 0005 §13's "channel that is not itself guest disk
   state." Not where the hypothesis fails.
3. **Value never written to PVE description/tags/guest filesystem/disk/LXC
   rootfs/vTPM image.** Same as (2) — achievable, not where it fails.
4. **A trusted host-side witness observes identity-breaking lifecycle
   events.** This is where §7's evidence matters; the reasoning rests only
   on in-scope (T1/T2, §5), never on the T3 bypass, for all three
   candidates:
   - **Task history — corrected this revision (full-review correction #2,
     P2 #1): no longer a confirmed failure claim.** It is true that
     neither PVE task surface carries a monotonic cursor (§7a), so an
     entirely ordinary T1 destroy+recreate *can* silently age out of
     retention with no provable-gapless record, **for a *stateless*
     observer**. But this ADR's own analysis only demonstrates that: it
     does not evaluate a *stateful*, fail-closed witness that durably
     tracks an overlap sentinel between observations and treats the
     sentinel's disappearance as a detected coverage gap forcing
     revocation, rather than silence (§7a, §10). Whether such a protocol
     can actually be built on either surface's specific
     retention/structure is genuinely unresolved. Classified
     **UNRESOLVED / NOT FULLY AUDITED**, not NO-GO.
   - **Single-node `pmxcfs` filesystem watcher:** **corrected post-merge
     (P2 #1) — no longer a load-bearing failure claim.** The prior
     revision asserted this design has "no coverage" for an operation
     executed on a different node, treating Path B's absence as an
     established fact. But that absence finding is itself only a bounded,
     **INFERENCE**-level search across five of roughly a dozen `pmxcfs`
     files, using a `notify`-substring match that could miss a
     differently-named or differently-implemented remote-invalidation
     mechanism, and is not a proof that no such mechanism exists anywhere
     in the complete `pmxcfs` remote-application path (§7c, §24 item 8).
     Because neither the presence nor the absence of cross-node delivery
     is established with sufficient confidence, this ADR can conclude
     **neither** that the single-node design fails (4) **nor** that it
     succeeds. Classified **UNRESOLVED / NOT FULLY AUDITED**.
   - **Distributed, per-node `pmxcfs` watcher:** this ADR does **not**
     evaluate whether this design achieves (4) — it depends on Path A's
     (currently UNKNOWN) completeness at every node, and on undesigned
     cross-node coverage/gap semantics (§7c). Classified **UNRESOLVED / NOT
     AUDITED HERE**, not failing and not succeeding at (4).
   - The combined config+disk occupant-replacement bypass via direct
     `pmxcfs`/storage writes (§7b) is real, but per §5's consistency rule
     it is a **T3-tier** capability, and T3 collapses into T4 (out of
     scope) for any of these candidates as stated, since none specifies an
     explicit root-resistant/external trust anchor (§5, §11a). It is
     therefore **not**, and never has been, used here as the load-bearing
     reason for any of the three candidates' classification — task
     history's UNRESOLVED status rests entirely on the stateless-vs-
     stateful gap above, and the T3 bypass remains only supplementary,
     out-of-scope context for all three (§7b).
5. **`trusted` persists only as long as the witness can durably/
   cryptographically prove uninterrupted coverage.** This is the right
   requirement in principle. **Corrected this revision:** this ADR does
   not establish whether task history (via a stateful overlap-sentinel
   design) or the single-node `pmxcfs` watcher satisfies (4) either way —
   property (5) is therefore correspondingly **UNRESOLVED** for both, not
   failing. A witness that cannot be shown to observe everything cannot be
   shown to prove it observed everything, but the converse — that it
   demonstrably fails to — is equally unestablished here for any of the
   three audited candidates. This does not extend to the distributed
   variant (A2) either, which this ADR does not audit at all (§8 property
   4, §24 item 7).
6. **Any observation gap is fail-closed.** Sound requirement, and the
   correct default *if* a witness existed (§14) — but a fail-closed rule
   only bounds the damage of a *detected* gap. **Corrected this revision
   (P2 #2, item A): the prior wording overstated what §7b actually shows
   for a `pmxcfs`-filesystem witness.** For **task history (Family B)**,
   §7b confirms a direct `pmxcfs`+storage write produces no task/UPID at
   all — a confirmed, real channel that surface never observes, a silent
   blind spot rather than a bounded, recoverable outage — though (per §5)
   this remains T3-tier and out-of-scope, never load-bearing for Family
   B's classification (its NO-GO rests on §7a/§10 instead). For a
   **`pmxcfs`-filesystem witness (Family A/A2)**, this ADR makes **no such
   confirmed claim**: a direct local `unlink()`/`write()` against `pmxcfs`
   is itself an ordinary Path A (local VFS) operation (§7c) — whether a
   co-resident watcher receives complete `fsnotify` for it is **UNKNOWN**
   here, exactly as for any other locally-issued syscall, not a
   established blind spot. Separately, §7c's cross-node notification
   question is not established as a confirmed blind spot for the
   single-node design either — it remains an open question whether such a
   gap exists at all (§7c, §24 item 8); *if* it exists, it would be
   exactly this kind of silent, undetectable blind spot rather than a
   fail-closed-recoverable one, but this ADR does not assert that it does.
7. **Same-slot destroy/recreate must always change the epoch or remove
   authority, even with identical observable facts.** This is exactly
   right as a *requirement* — see §10's dedicated adversarial walkthrough.
   The hypothesis's answer to *why* B cannot inherit A's trust is
   structurally sound (it does not rely on "config looks different"); for
   task history, whether a *stateless* observer reliably retains proof of
   the destroy/create pair before it ages out of either surface's bounded
   retention is a real, demonstrated concern (§7a) — but whether a
   *stateful* design closes it is, per the correction above, **unresolved**
   rather than a confirmed failure. For the single-node `pmxcfs` design,
   whether the witness reliably sees the transition is likewise
   **unresolved** rather than a confirmed failure (§7c).
8. **Snapshot rollback / backup restore must not inherit old trust.**
   Sound requirement, already fixed as mandatory by ADR 0001 row 5 and ADR
   0005 §17 for any future mechanism; this ADR does not weaken it (§15).
9. **Clone must not inherit trust.** Already covered by ADR 0001 (new
   locator → new `resource_id`) and ADR 0005 §11; a witness-based mechanism
   inherits this for free since clone always produces a new slot/locator.
10. **Watcher/backend/node restart must have explicit semantics.** Sound
    requirement (§14 extends it); does not rescue (4)/(5).
11. **No polling "probably nothing changed" is sufficient.** Correct, and
    consistent with ADR 0001/0002's existing rule that stock polling is
    never a security boundary — this rules out treating a *stateless*
    query of either PVE task surface (§7a: the recent `/cluster/tasks`
    status cache, or the bounded `/nodes/<node>/tasks` archive) as
    sufficient by itself. It does **not**, by itself, rule out a
    *stateful* design that retains its own record of what it previously
    observed and treats an unexplained disappearance as a detected gap
    (§7a, §10) — this ADR does not evaluate whether such a design would
    still reduce to "probably nothing changed" or would genuinely differ
    from ordinary polling.

**Conclusion of §8, corrected this revision (full-review correction #2):**
properties (1)–(3) and (6)–(11) describe a *well-designed* witness, if one
could exist. Property (4)/(5) — the actual load-bearing claim that a
witness can observe identity-breaking events with provable completeness —
is **no longer shown to fail for any of the three audited candidates.**
**Task history** is demonstrated insufficient only for a *stateless*
observer (a bounded, non-monotonic pair of surfaces, §7a); whether a
*stateful*, fail-closed overlap-sentinel design closes that gap is
genuinely unaudited here. **The single-node `pmxcfs` watcher** is likewise
not shown to fail: the claimed cross-node coverage gap rested on a
bounded, five-file, `notify`-substring search (§7c) that is not proof no
remote-invalidation mechanism exists anywhere in `pmxcfs`'s complete
remote-application path. Both are classified **UNRESOLVED / NOT FULLY
AUDITED**. A **distributed** per-node `pmxcfs` watcher is, as before,
**not** shown to fail (4)/(5) either — it is simply not audited (§7c, §9,
§24 item 7). The combined config+disk T3-tier bypass (§7b) remains real
for any of these designs that lack an explicit root-resistant/external
trust anchor (§5, §11a), but, per §5's consistency rule, it was never the
load-bearing reason for any of these three candidates' classification, and
it does not resolve any of their open status. This finding is scoped to
the audited channels and designs; it says nothing about whether a
fundamentally different observation channel, a stateful task-history
protocol, or either unaudited/unresolved `pmxcfs` design, could close this
gap (§1, §24 item 7/10).

## 9. Candidate family comparison

Family A is split into two rows: the **single-node** `pmxcfs`-witness
variant this ADR previously audited together with task history, and a
**distributed**, per-node variant identified but never audited. **Corrected
this revision (full-review correction #2, P2 #1): Family B (task history)
is now also downgraded from "No" to UNRESOLVED, alongside Family A.** The
prior revision's task-history NO-GO rested on a *stateless*-observer
analysis (§7a, §10) that does not rule out a stateful, fail-closed
overlap-sentinel design; the single-node `pmxcfs` variant's NO-GO
similarly rested on a bounded, five-file search that is not proof no
remote-invalidation mechanism exists anywhere in `pmxcfs`. This ADR can no
longer conclude either design fails, only that neither was fully audited.
**No row in this table now carries a "No" verdict grounded in the §7
task/`pmxcfs` research** — all three (A, A2, B) are **UNRESOLVED / NOT
(FULLY) AUDITED**, neither a "No" nor a "Yes" (§1, §6, §24 item 7/10).
Families C–F are evaluated on separate, already-established grounds
(clone-copyability of disk-resident state, or a node-vs-resource axis
mismatch) independent of the §7 audit, so their verdicts are not narrowed
by that scoping and remain "No" on those independent grounds. Family G is
corrected in a prior revision (P2) — see the row below and §24 item 3 for
the required disclaimers.

| Family | What it proves | Trust root | Copyable by clone? | Same-slot recreate? | Snapshot rollback? | Restore? | Migration? | Watcher/backend/node restart? | Offline interval? | Replay? | Privilege assumption | QEMU/LXC parity | Satisfies ADR 0005 §14 test? |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| **A. Single-node `pmxcfs`/hostd lifecycle witness + external epoch** — **corrected post-merge (P2 #1): downgraded from NO-GO to UNRESOLVED — the prior row's cross-node coverage-gap claim overstated a bounded five-file search as a proven blind spot** | Intended: gapless observation of identity-breaking events whose local syscall executes on the one node this witness runs on | A single host-resident witness process on one node; defends T1/T2 *for operations whose syscall executes on that same node* (Path A, §7c — plausible/expected, but UNKNOWN/unproven for `pmxcfs` specifically, not disproven); whether it has coverage for operations executed on a different node is **UNRESOLVED** — the claimed cross-node gap is only an unproven inference from a bounded search (§7c, §24 item 8), not an established architectural fact. As hypothesized, it also specifies no explicit root-resistant/external trust anchor (§5), so T3 additionally collapses into T4 for it (§11a) — **not the load-bearing reason for this row's classification either way** (§5's consistency rule, §8) | Epoch value: no (external); whether the cross-node question affects clone-copyability is not evaluated | **UNRESOLVED — not shown to be distinguished, and not shown to fail, when the occupant's destroy/create syscall executes on a node other than the one this witness watches** (§7c: the absence of a confirmed remote-delivery mechanism is bounded to five checked files, not exhaustive). Even when the syscall executes on the watched node, this ADR does not prove Path A delivery is complete for `pmxcfs` (UNKNOWN) | Same unresolved cross-node/single-node question applies | Same | Requires explicit handling (§15); whether migration to a different node defeats coverage depends on the unresolved cross-node question | Must fail closed (§14); does not resolve the cross-node question either way | Not inherently defended | Not inherently defended; depends on epoch uniqueness discipline (ADR 0005 §16-style), which is sound but does not resolve the coverage question | A single ordinarily-privileged observer confined to one node | Symmetric in principle (both QEMU/LXC configs live under `pmxcfs`) | **UNRESOLVED / NOT FULLY AUDITED** — this ADR does not claim this design satisfies §6/§8's coverage requirement, and does not claim it fails it (§8 property 4, §24 item 7/8) |
| **A2. Distributed, per-node `pmxcfs` lifecycle witness + external epoch** — **new this reopening (P2 #1): not audited, not designed** | Hypothetical: one witness per relevant PVE node, each relying only on that node's own local (Path A) delivery, intended so that whichever node actually executes a given operation's syscall has its own watcher observe it directly | One witness process per node in the source; whether this closes, partially closes, or fails to close the single-node gap above is **not evaluated** | **Not evaluated** | **UNRESOLVED / NOT AUDITED HERE** — depends on Path A's completeness at every node (UNKNOWN, §7c), on which node actually executes a given operation (UNKNOWN, §7b), and on cross-node coverage/gap semantics never designed here. This ADR does **not** claim this design observes a same-slot recreate, and does **not** claim it fails to | Not evaluated | Not evaluated | Not evaluated — a distributed witness's node-migration semantics were never designed | Not evaluated — multi-node restart/coordination semantics were never designed | Not evaluated | Not evaluated | Would require witness presence on every node in the source; whether this incidentally observes the T3 combined config+disk bypass (§7b) — since even a root actor's local `rm` is still a local syscall — is **not verified or claimed here either way** | Not evaluated | **UNRESOLVED / NOT AUDITED HERE** — this ADR does not claim this design satisfies ADR 0005 §14, and does not claim it fails (§24 item 7) |
| **B. PVE task/event/audit history as witness** — **corrected this revision (full-review correction #2, P2 #1): downgraded from NO-GO to UNRESOLVED — this ADR's own analysis only shows a *stateless* observer fails; a *stateful*, fail-closed overlap-sentinel design was never audited** | A record exists on at least one of two surfaces (§7a) for operations that went through the high-level API/CLI path; whether a *stateful* witness could durably prove continuous coverage from these records is unresolved | Trust in Proxmox's own task subsystem, **plus, for any stateful design, durable state of the witness's own (sentinel tracking) — not evaluated here** | No (event record itself isn't guest state) | **UNRESOLVED, not a confirmed failure (§10): a genuine destroy+create pair via the normal API/CLI DOES produce records on both surfaces (§7b) — Surface A retains only ~25 finished tasks (updated at worker-start and every 10s, truncated at 32 KiB), and Surface B rotates at a fixed size threshold; a *stateless* observer querying at two points in time cannot prove the pair wasn't aged out of both between checks. Whether a *stateful* witness that durably tracks its own overlap sentinel and fails closed on its disappearance can close this gap is genuinely unaudited (§7a, §24 item 10). The T3 direct-`pmxcfs`-write bypass (§7b) is a separate, real, but supplementary/non-load-bearing observation (§5)** | Not detected by a stateless observer unless rollback itself is API-invoked and checked before rotation (§7a); unresolved for a stateful design | Same as same-slot recreate | Not addressed | Both surfaces are bounded, for different reasons (count/payload-size/time for Surface A, size for Surface B) — an old event can be silently aged out of either for a stateless observer (§7a); whether a stateful sentinel design detects this as a gap is unresolved | A gap in polling either surface is invisible to a stateless observer; neither surface has a *native* cursor; whether a witness-maintained sentinel can substitute for one is unresolved (§7a, §24 item 10) | Not addressed | Ordinary `VM.Audit`/`Sys.Audit`-class read access, not root | Symmetric | **UNRESOLVED / NOT FULLY AUDITED** — this ADR does not claim a task-history-based witness (stateless or stateful) satisfies ADR 0005 §14, and does not claim every possible design fails it; this is a narrower conclusion than ADR 0002's own prior Class B **UNKNOWN**, not a claim beyond it |
| **C. Hardware-rooted TPM / physical attestation** | Identity/integrity of the **physical host**, not of any specific guest incarnation | Physical TPM chip on one specific machine | N/A — a physical host property, not something guests carry | **Does not address this axis at all** — a hardware TPM attests the node, not which guest occupies a VMID slot | N/A | N/A | Breaks by construction: a hardware TPM cannot follow a guest across a live/offline migration to different physical hardware | N/A to resource continuity | N/A | N/A | Not applicable to resource continuity; **this is a node-attestation primitive, a different axis entirely (ADR 0001 node section)** | Would be identical for QEMU/LXC since it says nothing about either | **Not applicable** — solves a different problem (node trust), not Blocker B |
| **D. vTPM** | Guest-visible TPM state at read time | Software-emulated; backed by a `vtpm0` disk volume | **Yes — copied by clone/backup/snapshot identically to any other disk (already ADR 0005 §6 candidate 20)** | Fails identically to any disk-resident evidence | Fails (state travels with the snapshot) | Fails (state travels with the restore) | Travels with the guest, proves nothing about continuity | N/A | N/A | Fully replayable by anyone who can copy the disk | Root/API-level access to guest storage | QEMU only (no stock LXC vTPM) | **No** — already rejected in ADR 0005 |
| **E. Guest cryptographic agent + guest-resident key** | Key possession at read time | Private key material stored in guest disk/config state | **Yes — disk-resident, copied by clone/backup identically (ADR 0005 §13)** | Fails — new occupant can carry the copied key forward | Fails | Fails | N/A | N/A | N/A | Replayable by whoever can read the disk | Requires cooperative in-guest agent (QGA) or `pct exec`-class access; not default-on | Asymmetric (QGA is QEMU-only; LXC needs `pct exec`) | **No** — already evaluated and rejected in ADR 0005 §13 |
| **F. External/HSM-backed guest identity** — **narrowed this revision (P2 correction: the earlier row incorrectly collapsed the entire family into (E) or (C), excluding the genuinely externally-rooted/out-of-band class ADR 0005/0006 leave open; corrected below)** | **Narrow variant audited here: a guest-resident credential whose signing authority is an external HSM, but the guest itself still presents that credential at use time.** Proves key possession at read time, same as Family E, because the artifact actually presented/copyable still lives in guest-readable state | Narrow variant: reduces to (E) — an external signer does not change that the guest-side artifact is what a clone/restore copies | Narrow variant: **yes, same as (E)** — copied identically to Family E's own limitation | Narrow variant fails identically to (E) | Same as (E) | Same as (E) | N/A | N/A | N/A | Replayable identically to (E) | Requires cooperative in-guest presentation, same as (E) | Same asymmetry as (E) | **Narrow variant: No** — reduces to Family E, already rejected on those grounds. **The broader externally-rooted/out-of-band per-workload identity class — where a specific workload's identity is tracked/attested by an external system through a channel that is neither guest-resident nor a node-bound hardware property — is UNRESOLVED / NOT AUDITED HERE (§24 item 7). This ADR does not claim that broader class satisfies Blocker B, and does not claim it fails; it was not researched to either conclusion this pass.** |
| **G. Operator per-mutation re-attestation / ephemeral trust** | Nothing persists as `trusted`; every mutation instead requires its own fresh, explicit, human-confirmed identity check — this **sidesteps rather than answers** the persistent-`trusted` question this ADR audits (§24 item 3) | The human operator, at the instant of the check, **plus** a safe point-in-time target-identity proof binding that confirmation to the resource actually mutated — not yet defined by this family (§24 item 3) | N/A — no persistent trust artifact exists to copy | **Not immune, and not answered by this family** — there is no *persisted* `trusted` state for a recreated occupant to inherit, but a confirmation made against occupant A is exactly as vulnerable to a same-slot substitution as any other mechanism if the confirmation is not safely fenced against a race between the human check and backend execution (§24 item 3) | No persisted state to invalidate, but the underlying rollback-substitution risk is unaddressed by this family, not solved by it | Same as rollback | Same as rollback | No persisted coverage to lose across a restart — narrower claim than "immune" | No window during which *stale persisted* trust could be consumed — does not mean the underlying occupant-substitution question is solved | ADR 0001's exact-match CAS on `resource_id`/`binding_id`/`locator_generation`/`resource_continuity_revision` prevents replay of a **stale backend decision** — it does **not**, by itself, prove the physical/logical occupant was not substituted between confirmation and execution, since ADR 0001 explicitly permits those same tokens to remain unchanged across an observationally invisible same-slot delete/recreate (ADR 0001 row 10) | Symmetric | **Does not satisfy Blocker B by itself** — operator confirmation alone is not continuity proof (§24 item 3); adopting a mutation model that never requires persistent `security_continuity=trusted` would itself require a separate architecture change to ADR 0001/0005's accepted mutation-precondition formula, not something this ADR or a Family-G choice can authorize |
| **H. Combinations of the above** — **corrected this revision (full-review correction #2): B is downgraded to UNRESOLVED (above) and therefore no longer part of this row's independently-"insufficient" set either — a combination that includes B now inherits B's UNRESOLVED status rather than being manufactured into a NO-GO. This row's own "No" verdict is retained only for combinations drawn from C/D/E/F-narrow that do NOT include B** | Higher empirical confidence, no new independent security property, **for combinations drawn only from Families C/D/E and F's narrow variant — i.e. excluding A, A2, and now B as well** | Whichever combination of those independently-insufficient families is used, **not including B** | **Re-grounded reasoning, now excluding B:** combining C with D/E/F-narrow does not help, because a successor occupant can carry forward copied disk/config/key evidence for D/E/F-narrow (ADR 0005 §11/§13) regardless of node-identity evidence, and C proves node identity, not resource incarnation (§9 row C) — this holds independently of anything B does or does not establish. **A combination that additionally includes B inherits B's UNRESOLVED status (§9 row B) — it is not, and must not be manufactured into, a NO-GO on this row's account** | Still not distinguished, for combinations drawn only from Families C/D/E/F-narrow (excluding B) — none of these introduces an independently sufficient continuity property, for the disk-resident/node-identity reasons above, independent of B | Still fails unless one member of the combination independently solves it (none of C–E/F-narrow does; B is excluded from this set) | Same | Same | Same | Same | Same | Same | Depends on which families are combined — not a fixed "weakest member" rule; see below | Same | **No, for combinations drawn only from Families C/D/E/F-narrow (excluding B)** — combining only insufficient evidence classes that introduce no new independent security property does not manufacture sufficiency; useful only as an audit/anomaly-detection signal (mirrors ADR 0005 §9-10's demotion of the administrative marker to audit-only). **A combination that includes B (task history, now unresolved), A (single-node, unresolved), A2 (distributed, unresolved), or a future, independently sufficient externally-rooted proof (e.g. Family F's broader unresolved class, §24 item 7) would instead be judged entirely by that unresolved component's own eventual resolution, not by this row — do not manufacture a NO-GO for a B-containing combination** — this table does not evaluate, and does not pre-judge, any such component. |

## 10. The critical same-slot witness test

Per the mission, this is the single most important test in this ADR.

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

Per §4's three-way distinction: "destroyed"/"recreated" here means the
**actual physical/logical occupant** — disk content and/or running
process — genuinely changed, not merely that the `.conf` metadata object
was rewritten (which, alone, with the disk untouched, would not be
identity-breaking at all). This test is about occupant substitution, never
about config-file churn by itself.

If the answer reduces to "because polling/config looks different," the
mechanism **fails** — this is the exact test ADR 0005 §9 already applied to
Family C, and it applies identically here.

**For every family in §9, the honest answer:**

- **Family A (single-node `pmxcfs`/hostd lifecycle witness) — corrected
  post-merge (P2 #1): inconclusive, not failing.** The *intended* answer is
  "because the witness observed the destroy event and the create event as
  two distinct lifecycle transitions, and revoked/regenerated the epoch
  between them — independent of whether B's config matches A's." This is
  the **right kind of answer** — it does not rely on config inspection.
  Whether this ADR can rule it out is, on reflection, **unresolved**: the
  prior revision claimed that if the occupant's destroy/create syscalls
  execute on a *different* node than the one this single witness watches,
  the witness necessarily never observes the transition — but that claim
  rested on a bounded, five-file search for a cross-node delivery
  mechanism, not a proof that none exists anywhere in `pmxcfs` (§7c, §24
  item 8). This ADR can therefore conclude **neither** that Family A
  answers this test correctly **nor** that it fails it — the test is
  **inconclusive** for this family, pending further primary-source
  research. Separately, and still not load-bearing either way, a combined
  direct `pmxcfs`+storage write (§4 concepts 2+3) would bypass it even on
  the watched node, but that is a T3-tier capability this ADR does not use
  to settle this test for an anchor-less witness (§5, §11a).
- **Family A2 (distributed, per-node `pmxcfs` lifecycle witness):** **not
  evaluated against this test.** Whether a witness present on *every*
  relevant node would have observed the transition, regardless of which
  node's local syscall it went through, depends on Path A's completeness at
  every node (UNKNOWN, §7c) and on undesigned cross-node coverage/gap
  semantics — neither established here. This ADR does not claim this
  design passes or fails this test (§9, §24 item 7).
- **Family B (task history) — corrected this revision (full-review
  correction #2, P2 #1): inconclusive, not failing.** The demonstration
  below no longer relies on a T3 bypass, per §5's consistency rule, and
  uses the explicit two-surface model from §7a — but this ADR's own
  analysis only shows a *stateless* witness fails; it does not rule out
  a *stateful* one. Suppose the operator (or an attacker with only
  ordinary privilege) destroys A and recreates B at the same VMID
  entirely through the normal API/CLI. This is **not** the
  direct-`pmxcfs`-write case — it **does** produce an ordinary destroy
  task and an ordinary create task on both PVE task surfaces, exactly
  like any other operation (§7b). The explicit consecutive-observation
  witness, evaluated for a **stateless** observer:

  ```text
  observation O1: query the relevant task surface(s) (§7a) for slot 101,
    with no memory retained of what O1 itself saw

  ordinary API/CLI destroy of occupant A
  ordinary API/CLI create of occupant B, same VMID
  enough further ordinary management-surface tasks occur, on this slot
    or on any other guest sharing the same bounded surface, to exceed:
      Surface A's ~25-finished-task cache / 32 KiB serialized payload, and/or
      Surface B's fixed-size archive rotation threshold (§7a)

  observation O2: query the same task surface(s) again for slot 101,
    independently of O1, with no sentinel carried forward from O1
  ```

  For a *stateless* observer, the intended answer at O2 is "because a
  destroy task and a create task exist in the record for that slot." The
  honest failure is not that no task was created — it was, on both
  surfaces — but that **neither surface carries a monotonic or
  gap-detectable cursor of its own (§7a)**: by O2, the destroy/create pair
  may have already aged out of Surface A's small, actively-truncating
  cache, out of Surface B's rotated archive, or both. A stateless O2 sees
  **no evidence at all** in that case, and cannot distinguish "nothing
  happened between O1 and O2" from "something happened, but I can no
  longer see it." **This is exactly where this ADR's proof stops, and no
  further — it is not license to conclude every conceivable Family B
  design fails.** A **stateful** witness is a plausible, unaudited
  counter-design: one that, at O1, durably records one or more exact
  previously-visible UPIDs (or an equivalent retained-set sentinel) per
  relevant node; at O2, requires that sentinel to still overlap the
  currently visible retained set on the relevant surface; treats the
  sentinel's *disappearance* as a detected **coverage gap**, forcing
  immediate authority-ineligibility/revocation rather than silent
  continuation; and only advances its stored sentinel once overlap is
  proven. Under such a protocol, "the destroy/create pair aged out of
  view" would not automatically look identical to "nothing happened" —
  if enough history was lost to remove the pair, the loss of the
  witness's *own* previously-retained sentinel could itself expose the
  coverage overrun and permit fail-closed revocation, rather than a
  silent false continuation. **This ADR does not claim such a protocol
  succeeds** — it is not designed or evaluated here, and at least the
  following are genuinely unresolved before it could be: whether either
  surface's retained set is sufficiently prefix/append-structured for an
  overlap sentinel to prove continuous coverage; whether Surface A can
  safely participate at all, or should be ignored given its truncating,
  non-append-only nature; complete-fetch/pagination/concurrent-rotation
  semantics; per-node sentinel ownership; node join/removal/restart
  behavior; how initial enrollment would establish the first trustworthy
  overlap point; whether UPID uniqueness alone is sufficient for the
  sentinel role; whether every in-scope T1/T2 identity-breaking operation
  reliably generates the required worker record for both QEMU *and* LXC;
  version-pinning/upgrade behavior; and whether a coverage gap could occur
  while a stale sentinel nonetheless remains visible (a false negative for
  the gap-detection itself). ADR 0002 is consistent with treating this as
  genuinely open rather than closed: it requires an accepted contract
  guaranteeing contiguous event/cursor semantics for trusted destroy/create
  event-chain evidence, and marks that contract **UNKNOWN** for stock PVE —
  not impossible. **Neither passes nor fails this test; it is
  inconclusive, pending the unresolved questions above (§7a, §24 item
  10).** The T3 direct-`pmxcfs`-write bypass (§7b) remains supplementary,
  non-load-bearing context regardless.
- **Family C/D/E, and Family F's narrow guest-held-key variant:** already
  shown to be disk/config-resident or node-bound, not slot-transition-
  observing at all; B trivially inherits whatever A had unless the
  mechanism separately fails closed for other reasons (ADR 0005 §9–§13).
  Fails the test for the identical reason ADR 0005 already gives. **Family
  F's broader externally-rooted/out-of-band class is not evaluated against
  this test at all** — it is unresolved, not audited (§9, §24 item 7).
- **Family G (ephemeral re-attestation):** there is no *persisted*
  `security_continuity=trusted` for B to inherit, because none is ever
  durably granted — the question as literally posed ("why can B not
  inherit A's stored `trusted` row") is moot by construction, not
  affirmatively answered. This must **not** be read as "immune" to the
  underlying substitution problem: if an operator's fresh confirmation at
  time T is not safely fenced against a race where the actual occupant is
  substituted between T and the backend's subsequent execution, the
  substitution risk this test probes is simply unaddressed, not solved
  (§24 item 3). Family G avoids this test by declining to attempt persistent
  `trusted` at all, not by passing it.
- **Family H (combinations of independently-insufficient families, i.e.
  C/D/E/F-narrow only — corrected this revision, full-review correction
  #2: B is downgraded to inconclusive/unresolved above and is therefore
  excluded from this bullet's "insufficient" set; a combination that
  includes B inherits B's unresolved status instead):** combining
  evidence classes that each introduce no new independent security
  property does not manufacture one, **for combinations that do not
  include B**: D/E/F-narrow's disk-resident evidence is copied forward
  onto a successor occupant regardless of what any node-identity or
  task-history evidence shows (ADR 0005 §11/§13); C proves node identity,
  not resource incarnation (§9 row C), so it answers a different axis
  entirely; neither gap is closed by adding more of the same evidence.
  This is not a claim that a combination always inherits "the weakest
  member's" answer as a general rule: a combination that includes Family
  B (task history, now unresolved), Family A (single-node, unresolved),
  A2 (distributed, unresolved), or a future, independently sufficient
  externally-rooted proof would instead be judged by that unresolved
  component's own eventual resolution against this same test, not by this
  bullet — this ADR does not manufacture a NO-GO for any such combination
  (§9, §24 item 7/10).

**Controlling conclusion, narrowly scoped and corrected this revision
(full-review correction #2):** **no family or combination audited in this
ADR is shown to fail this test.** Family C/D/E/F-narrow, and combinations
drawn only from them (row H, excluding B), fail for the identical,
already-established reasons ADR 0005 gives — disk-resident copyability or
a node-identity/resource-incarnation axis mismatch. **Task history (Family
B) is inconclusive, not failing**: this ADR's own stateless-observer
analysis cannot rule out a stateful, fail-closed overlap-sentinel design
(above). **Family A (single-node `pmxcfs` watcher) is likewise
inconclusive, not failing** — this ADR cannot confirm it passes or fails
either. Any combination that includes B, A, or A2 inherits that
component's unresolved status rather than being judged NO-GO by this
section. Family G sidesteps the test rather than passing it (above). This
is the same shape of finding ADR 0005 already reached for Family C, now
shown to extend cleanly to the disk-resident/node-identity families, but
**not** to task-history-based lifecycle-*event* observation, which remains
open. It is **not** a claim that every conceivable host-rooted witness
fails this test, nor that task history or the single-node `pmxcfs`
watcher do — a fundamentally different observation channel, a stateful
task-history protocol, the single-node `pmxcfs` watcher's still-open
cross-node question, or the **distributed**, per-node `pmxcfs`-watcher
variant (A2), were not conclusively audited here and remain unresolved
(§1, §6, §24 item 7/10).

## 11. Node/hostd trust root vs. resource continuity — explicit separation

The mission requires this separation be stated explicitly, not left
implicit. Any future mutation path already has its own, entirely separate
node/hostd trust gate (ADR 0001's `node_bindings`/`node_attestations`; ADR
0005 §18/§21). This ADR's findings do not, and must not, blur that boundary
in either direction:

- **A trusted node/hostd does not, by itself, grant resource continuity.**
  Even a perfectly honest, uncompromised PVE node running genuine,
  unmodified software still exposes `pmxcfs` as a directly-writable
  configuration store to a **T3-tier** actor (root-shell access to any
  cluster node, §5) — direct POSIX file manipulation of `/etc/pve`, not
  action through PVE's own management surface. **Corrected this revision
  (P2 #2, item D): this is a T3-only capability, not "T2/T3."** Ordinary
  T2 (PVE admin acting through the API/CLI management surface) does not,
  by itself, establish host-root shell authority — this ADR does not
  claim that PVE admin privilege equals direct filesystem root access
  unless a specific configuration (e.g. an admin who is separately also a
  root-shell user on the host) makes it so. Node honesty says nothing
  about whether a *specific slot's* occupant changed via a channel the
  witness observes, regardless of which tier performs the change.
- **Resource continuity evidence does not, by itself, grant node/hostd
  trust.** Nothing in this ADR's audit reads or writes anything about node
  identity; a future mutation still independently requires the accepted
  node/hostd trust route (ADR 0001, ADR 0005 §21), unaffected by whatever
  Blocker B mechanism, if any, is eventually accepted.
- **Every witness-based family in §9 additionally assumes T4 is out of
  scope** — i.e., that the PVE node's own control-plane software
  (`pvedaemon`, `pmxcfs`, or a future Hubinet-managed host-resident
  component) is not itself compromised or lying. This ADR states that
  assumption explicitly, as required, rather than leaving it implicit: **no
  mechanism evaluated here defends against a compromised node/hostd trust
  root.** That defense, if it is ever required, belongs to the separate
  node/hostd attestation protocol ADR 0001 §"Nierozstrzygnięte kwestie" #6
  already flags as future work — not to Blocker B.

### 11a. The precise T3/T4 boundary (resolves the earlier internal ambiguity)

§5 established the rule; this section states its resolution precisely,
keeping node trust and resource continuity two separate axes while making
their intersection coherent, rather than leaving T3 informally "in scope"
for a co-resident witness that cannot actually survive it:

- **A co-resident witness lacking an explicit root-resistant/external trust
  anchor (§5) is not a defense against T3 by construction.** For such a
  witness, T3 must be treated identically to T4 — out of scope, not a
  bounded, partially-defensible gap. §7b/§8/§9's "blind spot at T3"
  language for Family A describes exactly this: as hypothesized, Family A
  specifies no such anchor, so its failure at T3 is the *same category* of
  failure as T4, not a narrower one. This ADR does not claim Family A is
  "defensible against T3 in a meaningfully weaker sense than T4"; it is not.
- **Explicitly coupling witness-authority-eligibility to the current
  node/hostd trust state** (`node_trust_state`/`binding_revision`/
  `attestation_id`) — mirroring how ADR 0003 couples Blocker A/B evidence
  to `source_attestation_epoch` (§16) — **may be a necessary
  authority-eligibility gate for a future mechanism, but it is not, by
  itself, sufficient to claim T3 resilience, and this ADR does not claim
  that it is.** Coupling only means: the witness's coverage claim can never
  outlive the node's own trust state (an epoch-style fencing property). It
  says nothing about whether that node trust state, as currently defined,
  actually **detects or prevents** the specific act this ADR's threat model
  calls T3 — a root-shell actor tampering directly with `pmxcfs`/storage.
- **The concrete node/hostd attestation/trust-root contract remains
  unresolved in accepted architecture.** ADR 0001's node section defines
  `node_trust_state` (`unverified`/`trusted`/`revoked`) and requires
  re-attestation on reinstall/rejoin/hostd-key-change, but explicitly
  leaves "konkretny node/hostd attestation protocol, key rotation i
  operatorowa procedura ponownego nadania `trusted`" to a separate,
  not-yet-written future review (ADR 0001 §"Nierozstrzygnięte kwestie" #6).
  Today, nothing in accepted architecture specifies that a currently
  `trusted` node/hostd binding actually proves the absence of an
  in-session root-shell compromise on that node — attestation, as
  currently designed, verifies host/endpoint *identity* across
  reinstall/rejoin-class events, not continuous absence of root tampering
  during normal operation. Treating current `node_trust_state=trusted` as
  if it already meant "no root-shell actor could have tampered with
  `pmxcfs`/storage on this node" would be exactly the "trusted PVE
  node/hostd itself is not compromised" assumption this ADR requires be
  stated explicitly (§5) — it must not be presented as something the
  existing `node_trust_state` mechanism already detects or prevents.
- **T3 resilience may therefore only be claimed once both of the
  following hold, neither of which exists today:** (a) a future
  mechanism's witness-authority-eligibility is explicitly coupled to
  `node_trust_state` as described above, **and** (b) a separately accepted
  node/hostd attestation/trust-root contract — the still-unresolved item
  above — actually defines and provides detection or prevention semantics
  against a root-shell actor on that node. Absent (b), coupling to
  `node_trust_state` (a) alone gives a fencing/freshness property, not a
  resilience property, and no candidate audited in this ADR is entitled to
  claim any resilience against T3 on that basis — this corrects the
  earlier framing, which risked implying that coupling alone (a) already
  answered the T3 question.
- Any future mechanism that turns out to require a host-resident witness
  component must, at minimum, define its own node-migration/re-attestation
  semantics (mirroring ADR 0005 §21's requirement for any node-mediated
  evidence collection), and must never present "the node/hostd is trusted"
  as if it also meant "this specific resource's continuity is proven" or
  "root-shell tampering on this node is detected/prevented" — the latter
  requires the separate, not-yet-designed node/hostd attestation contract
  above, not an inference from the existing `node_trust_state` value.

## 12. Selected mechanism: **no mechanism selected — negative/unresolved conclusion, narrowly scoped to the audited families**

This ADR selects no mechanism, and its findings have four
independently-grounded parts; none should be read as broader than its own
evidence. **Exact classification, corrected this revision (full-review
correction #2):**

```text
PVE task/event-history witness (Family B):              UNRESOLVED /
                                                          NOT FULLY AUDITED
single-node pmxcfs filesystem watcher (Family A):        UNRESOLVED /
                                                          NOT FULLY AUDITED
distributed per-node pmxcfs filesystem watcher (Family A2): UNRESOLVED /
                                                             NOT AUDITED HERE
```

- **Family B (task history) — corrected this revision from NO-GO to
  UNRESOLVED/NOT FULLY AUDITED.** §10's demonstration only shows that a
  *stateless* observer of either PVE task surface (§7a) — one with no
  memory between two queries — cannot distinguish "nothing happened" from
  "a genuine destroy/create pair aged out of the retained window before
  being checked." It does not show that a *stateful*, fail-closed witness
  that durably tracks its own overlap sentinel between observations, and
  treats the sentinel's disappearance as a detected coverage gap forcing
  revocation, must also fail — that protocol was never designed or
  audited here, and a substantial list of open questions (§10, §24 item
  10) would need closing before it could be judged either way. ADR 0002's
  own prior **UNKNOWN** for trusted destroy/create event-chain evidence
  (requiring an accepted contiguous event/cursor contract this ADR does
  not establish) is the correct posture here too — not impossibility.
- **Family A (single-node `pmxcfs`-filesystem-observation witness) —
  already corrected to UNRESOLVED/NOT FULLY AUDITED in a prior revision,
  unchanged by this one**: the prior claimed cross-node coverage gap
  rested on a bounded, five-file search that is not proof no
  remote-invalidation mechanism exists anywhere in `pmxcfs`'s complete
  remote-application path (§7c, §24 item 8) — this ADR can conclude
  neither that it succeeds nor that it fails. This is **not** a claim that
  every conceivable host-rooted lifecycle witness is impossible — **Family
  A2 (a distributed, per-node `pmxcfs` watcher), a witness built on a
  fundamentally different channel (kernel audit/LSM/`eBPF`-based
  enforcement), or one backed by an explicit root-resistant/external trust
  anchor (§5)** was not conclusively audited here and remains
  **unresolved** (§24 item 7), not disproven.
- **Family C, D, E, and Family F's narrow guest-held-key variant** fail for
  reasons independent of the §7 audit — disk-resident state that
  clone/backup/restore copy identically (D, E, F-narrow), or a
  node-vs-resource axis mismatch that no amount of host observation
  changes (C) — and this part of the conclusion is not narrowed by the
  corrections above; it rests on the same grounds ADR 0005 already
  established for equivalent candidates. **This is the only part of this
  ADR's findings that remains a genuine "No" verdict.** **Family F's
  broader externally-rooted/out-of-band per-workload identity class is
  UNRESOLVED / NOT AUDITED HERE** — this ADR does not claim it satisfies
  Blocker B, and does not claim it fails (§9, §24 item 7).
- **Family G (operator per-mutation re-attestation)** does not fail this
  ADR's tests, but it also does not pass them — it sidesteps the question
  by declining to grant persistent `trusted` at all. Per §9/§10/§24 item 3:
  operator confirmation by itself does not satisfy Blocker B; ADR 0001's
  CAS discipline prevents replay of a stale *backend decision*, not
  physical/logical occupant substitution; adopting a mutation model that
  never requires persistent `security_continuity=trusted` would itself
  require a separate architecture change to ADR 0001/0005's accepted
  mutation-precondition formula; and any such future model would still need
  its own safe point-in-time target-identity proof and/or fencing/
  serialization contract, not yet defined here. Family G is left as a
  possible complementary direction for a future mutation-design ADR (§24
  item 3), not adopted as a Blocker-B mechanism.

**Conclusion, corrected this revision (full-review correction #2):** this
ADR does **not** select a mechanism for `security_continuity: unverified ->
trusted`, and it does **not** conclude that any candidate built on the two
PVE task surfaces or either `pmxcfs`-filesystem-witness variant fails.
**Blocker B remains OPEN.** The only genuine "No" verdict this ADR reaches
is for Families C/D/E/F-narrow (and combinations drawn only from them, row
H) — the same disk-resident-copyability/node-identity-axis grounds ADR
0005 already established. **Task history (Family B), the single-node
`pmxcfs` watcher (Family A), and the distributed `pmxcfs` watcher (Family
A2) are all classified UNRESOLVED**, each for its own distinct, unresolved
reason (§7a/§10 for B; §7c/§8 for A; never-designed for A2). It is **not**
a claim that no practical host-side lifecycle witness of any kind could
ever exist, and it is not a claim that task history in particular fails —
other genuinely different host-rooted mechanisms remain **unresolved**,
not disproven (§24 item 7). Blocker B's resolution therefore still depends
on future research this ADR does not foreclose — and that future research
may find any of the three unresolved candidates sufficient, insufficient,
or itself further inconclusive; this ADR does not prejudge which.

## 13. What remains required of any future stronger mechanism (extends ADR 0005 §14)

ADR 0005 §14's minimum-property list applies unchanged. This section
applies specifically to any future mechanism that relies on Proxmox's own
task history (§7a's two surfaces — **corrected this revision from a firm
NO-GO to UNRESOLVED/NOT FULLY AUDITED, full-review correction #2**) and/or
a **single-node** `pmxcfs` filesystem-level observation (also
**UNRESOLVED/NOT FULLY AUDITED**) as its lifecycle-observation channel. It
does not, by itself, apply to a fundamentally different host-rooted
mechanism (e.g. kernel audit/LSM/`eBPF`-based enforcement, a genuinely
root-resistant external trust anchor, §5/§24 item 7, or a **distributed**,
per-node `pmxcfs` watcher, §7c/§9/§24 item 7) — any of those would need its
own primary-source audit against §6/§10's tests, not merely inherit this
section's task/single-node-`pmxcfs`-specific findings. A future ADR
proposing either the task-history-based (stateless *or* stateful) or the
single-node design should treat this section as what a *complete* audit
would still need to establish before either a NO-GO or a sufficiency claim
could be honestly reached — neither is a settled NO-GO to work around, nor
a settled sufficiency to assume. This research adds the following,
specific to lifecycle-observation-based mechanisms of the audited classes,
as mandatory additional properties any future ADR proposing one must
satisfy:

- it must not treat Proxmox's own task history (either surface, §7a), or
  any `pmxcfs` file-level observation, as, by itself, a complete or
  gapless event channel — §7 shows none of them carries an official
  completeness, retention, or monotonic-ordering guarantee; a **stateful**
  task-history mechanism must instead define its own durable, CAS-protected
  overlap-sentinel/coverage-gap contract (§10's counter-design sketch, §24
  item 10) and prove it against every open question listed there, not
  merely assert that maintaining state is sufficient by itself;
- **corrected this revision (P2 #2, item E): it must explicitly state its
  own T3 contract, not be unconditionally required to close the T3
  direct-`pmxcfs`+storage-write bypass (§7b)** — closing it unconditionally
  would contradict §5 for a candidate that legitimately scopes T3 out.
  Specifically:
  - if the mechanism is an **anchor-less/co-resident** design (no explicit
    root-resistant/external trust anchor, §5/§11a): T3 may remain
    explicitly out of scope, treated identically to T4; it must not claim
    T3/root resilience anywhere in its own documentation or
    implementation; and its sufficiency must instead be proven against
    T1/T2 plus whatever separately-accepted node/hostd trust assumptions
    it relies on (§11/§11a);
  - if the mechanism **claims T3 resilience**: it must define an explicit
    root-resistant/external trust anchor (§5), and must then actually
    close, detect, or be structurally immune to the direct
    `pmxcfs`+storage-write bypass — only in that case does "no task/event
    was observed" need to default to authority-ineligible against a T3
    actor specifically; absent that anchor, "no task was observed"
    defaulting to authority-ineligible remains required only against
    in-scope T1/T2 actions, not as a T3 guarantee it never claimed;
- if it depends on any filesystem-level change notification, it must
  precisely distinguish, and independently primary-source verify, two
  separate claims (§7c): (a) whether same-node, locally-originated
  `fsnotify`/`inotify` delivery is actually complete and reliable for
  `pmxcfs` specifically (this ADR leaves this **UNKNOWN**, not proven and
  not disproven), and (b) whether any cross-node, Corosync-replicated
  change can ever reach a different node's local kernel notification (this
  ADR found **no mechanism** for this in the files checked, §7c) — it must
  not treat (a) as if it inherited (b)'s negative finding, and must not
  assume either holds without its own verification;
- a mechanism proposing the **distributed**, per-node variant (§9 Family
  A2) must additionally define: which node is guaranteed to execute a
  given identity-breaking operation's local syscall (§7b/§7c, UNKNOWN
  here), cross-node coverage/gap/restart semantics for a multi-node
  witness fleet (never designed here), and whether/how it also covers the
  storage-layer half of a combined config+disk occupant replacement (§4
  concept 3, §7b) — none of which this ADR resolves;
- it must state explicitly, as this ADR does in §11, whether it assumes a
  node/hostd trust root, and if so, must not conflate that assumption with
  proof of resource-level continuity;
- it must define its own coverage-epoch/gap semantics using durable,
  CAS-protected Hubinet Ops state as the actual completeness authority —
  never Proxmox's own task retention window — consistent with §14 below;
- it must pass §10's same-slot witness test with an answer that does not
  reduce to "because nothing looked different" or "because no adverse
  event was logged" — the answer must be a positive, structurally-grounded
  proof of continuous coverage, not the absence of contrary evidence.

## 14. Gap/restart semantics (required default for any future mechanism)

Not implemented here; recorded as the fail-closed default any future
mechanism must adopt, per the mission's explicit preference for fail-closed
behavior over ergonomics:

```text
witness process loses authoritative coverage (crash, restart, network
  partition from the source, or any other interruption it cannot prove
  did not overlap an identity-breaking event)
  => every resource whose trust depended on that witness's coverage
     becomes immediately authority-ineligible

durable materialization: trusted -> revoked (resource_continuity_revision
  +1 exactly once), expressed strictly within ADR 0001's existing
  three-value vocabulary — no fourth canonical state (ADR 0005 §17, §26)

restoring trusted requires a fresh, explicit operator enrollment against
  the current occupant -- never an automatic replay or reconstruction of
  the unobserved interval
```

This ADR does not attempt to design how a future mechanism would
reconstruct an unobserved interval, because it should not: an unobserved
interval is, by definition, a gap in the only evidence that could
distinguish the legitimate occupant from a substituted one, and
reconstructing it optimistically would recreate exactly the failure this
ADR's research found in every audited family.

## 15. Migration semantics (required default for any future mechanism)

Not designed here. Per the mission, a future mechanism is **not** required
to support secure migration in its first version; the acceptable default,
if a safe source-node → target-node handoff cannot be proven by that
mechanism's own evidence:

```text
migration => trusted -> revoked (resource_continuity_revision +1 exactly
  once), unless the future mechanism's own ADR explicitly proves a safe
  handoff proof and defines its exact semantics
```

This must be a stated, deliberate decision in that future ADR, not a
silent gap. It mirrors ADR 0001's existing "resource identity is preserved
across migration, but *this ADR's* continuity/trust guarantee is a
separate axis" distinction, and does not change ADR 0001's own migration
identity rules (`resource_id`/locator continuity across migration remain
exactly as ADR 0001 defines).

## 16. Source/node attestation coupling (extends ADR 0003 §19–§20, §26–§27; ADR 0005 §18–§19, §21)

Unchanged from ADR 0003/0005 and restated as a binding constraint on any
future mechanism:

- any future Blocker-B evidence that depends on source trust-domain
  continuity is authority-eligible only under the exact
  `source_attestation_epoch` at which it was accepted (ADR 0003 §19,
  §20, §26, §27);
- an epoch bump immediately makes prior-epoch evidence
  authority-ineligible; the mandatory default, absent an accepted
  carry-forward procedure, is `trusted -> revoked` with a
  `resource_continuity_revision` bump, expressed strictly within ADR
  0001's existing vocabulary (ADR 0003 §27, §29 witness 16/18; ADR 0005
  §19, §26);
- any live remote-evidence read a future mechanism performs (e.g., to
  correlate a witness event with a resource) must follow ADR 0003 §19a's
  three-phase read-then-write discipline, including capturing the
  resolved current node locator before the read and re-validating it by
  CAS in the write transaction, exactly as ADR 0005 §18/§20 already
  require for any marker-correlation-style read;
- node/hostd trust (`node_binding_id`/`binding_revision`/`attestation_id`)
  remains a wholly separate, both-required gate for any future mutation,
  never a substitute for resource-level continuity proof (§11 above; ADR
  0005 §21).

## 17. CAS/transaction model (required of any future mechanism)

Not implemented here; recorded as a binding requirement, mirroring ADR
0001/0002/0003's existing discipline:

- any future enrollment, revocation, or coverage-epoch transition must be
  a single atomic transaction that revalidates every expected-context
  field (exact `resource_id`, active `binding_id`, `locator_generation`,
  current `resource_continuity_revision`, current
  `source_attestation_epoch`, and, if node-mediated, the resolved current
  node locator) immediately before committing;
- a stale expected-context CAS must classify the attempt as stale and
  accept no transition, never partially apply one (ADR 0002/0003 pattern);
- a witness-coverage gap transition (§14) and a migration-triggered
  transition (§15) are both security-relevant continuity decisions under
  ADR 0001's own rule and therefore each advance
  `resource_continuity_revision` exactly once per decision, never per
  affected field.

## 18. Revision/publication semantics

Unchanged from ADR 0005 §22: because this ADR selects no mechanism and
grants `trusted` nowhere, there is no new `security_continuity` transition
to wire into `resource_continuity_revision`, `inventory_revision`, or
`published_state_revision`. If a future stronger-proof ADR ever introduces
an actual trust-granting transition, that ADR is responsible for its own
revision/publication effect, following the pattern ADR 0001/0003/0004
already established. Home Assistant remains presentation-only.

## 19. Failure modes

| Failure | Consequence under this ADR's corrected conclusion |
| --- | --- |
| Witness process crash/restart | N/A — no witness exists; would be §14's fail-closed default if one did |
| Task-log rotation aging a genuine destroy/create pair out of the retained window before it is checked | **Corrected this revision (full-review correction #2): a confirmed failure only for a *stateless* observer, not for task history in general.** A bounded, non-cursor-tracked window defeats stateless polling (§7a, §10); whether a *stateful*, fail-closed overlap-sentinel design closes this gap is genuinely **UNRESOLVED**, not designed or audited here (§10, §24 item 10) |
| Occupant executes its destroy/create syscall on a node other than the one a **single-node** watcher observes | No longer a confirmed, load-bearing failure. Whether this is actually a blind spot is UNRESOLVED — it depends on the unproven cross-node delivery question below (§7c, §8, §9, §10) |
| Combined direct `pmxcfs` config + storage manipulation (T3), no explicit anchor | A real, T3-tier capability, but out of scope and non-load-bearing for every family per §5's consistency rule (§7b, §11a, §20 row 2). For task history (Family B) this confirmed produces no task record; for a `pmxcfs`-filesystem witness (A/A2), whether it is actually observed is not evaluated — root can suppress/tamper with a co-resident witness regardless, so no in-scope verdict is derived either way |
| Cross-node (Corosync-replicated) `pmxcfs` change never reaching a different node's local kernel notification | Absence confirmed only in the five core files checked, not proven repository-wide (§7c, §24 item 8). This is why the single-node design is UNRESOLVED, not a confirmed failure — an unchecked file or a differently-named mechanism could still deliver this |
| Same-node, locally-originated `fsnotify`/`inotify` delivery being incomplete or unreliable for `pmxcfs` specifically | **UNKNOWN — not shown by this ADR.** Previously overclaimed as a proven gap; now classified as an open primary-source question (§7c, §24 item 8) |
| A stateful task-history witness's overlap sentinel failing to detect a genuine coverage gap, or falsely reporting one, on either PVE task surface | **UNRESOLVED / NOT DESIGNED OR AUDITED HERE** (full-review correction #2) — neither surface's retained-set structure has been verified sufficient (or insufficient) for this purpose (§7a, §10, §24 item 10) |
| Compromised node/hostd (T4) | Out of scope for every family here; belongs to the separate node/hostd attestation gate (§11, §11a) |
| Attempting to reconstruct an unobserved interval optimistically | Explicitly forbidden as a future-mechanism default (§14) — would still apply to any future stateful task-history mechanism as much as to a stateless one |

## 20. Adversarial matrix

Extends ADR 0005 §28's format to the families audited in this ADR. No row
produces `security_continuity=trusted`, because this ADR selects no
mechanism; the matrix instead records what each family's evidence would
have shown, had one been implemented, to make the negative/unresolved
finding falsifiable rather than asserted. A **Family A2** column covers the
distributed, per-node `pmxcfs` watcher — every A2 cell reads **UNRESOLVED
/ NOT AUDITED HERE**. Every Family A (single-node) cell that previously
asserted a confirmed blind spot is downgraded to UNRESOLVED — none of them
is a settled verdict either way. **Corrected this revision (full-review
correction #2): Family B's cells are likewise downgraded.** Row 1 shows
only that a *stateless* observer fails; whether a *stateful*,
sentinel-tracking design closes the gap is UNRESOLVED (§10, §24 item 10),
not a settled failure. Row 2's T3 scenario is retained only as
supplementary, non-load-bearing context for every family.

| # | Scenario | Family A (single-node witness) | Family A2 (distributed witness) | Family B (task history) | Family G (ephemeral) |
| --- | --- | --- | --- | --- | --- |
| 1 | Ordinary destroy+recreate via API/CLI, same node as the watcher | Witness *may* observe both local syscalls via Path A; completeness for `pmxcfs` specifically remains **UNKNOWN** here — **unverified**, not trusted | **UNRESOLVED / NOT AUDITED** — depends on the same unproven Path A completeness, at whichever node executed the operation | **Stateless demonstration (§10):** task record *may* exist on either surface *if not yet aged out of the bounded window*; sufficiently many later ordinary tasks can age it out with no native cursor proving anything was missed. Whether a *stateful* sentinel-tracking design would detect this as a gap is **UNRESOLVED**, not designed or audited here (§24 item 10); **unverified**, not trusted | No *persisted* state exists to be stale; each future mutation still needs its own safe point-in-time target proof (§24 item 3) — not "unaffected" in any stronger sense |
| 1a | Ordinary destroy+recreate via API/CLI, on a node *other than* the watcher | **UNRESOLVED, not a confirmed blind spot.** Whether this goes unobserved depends on the unproven cross-node delivery question (§7c, §8, §10) — a bounded five-file search found no mechanism, but that is not exhaustive | **UNRESOLVED / NOT AUDITED** — this is exactly the case a distributed design is meant to address, but coverage/gap semantics for it were never designed here | Same as row 1 (task history is not node-bound) | Same as row 1 |
| 2 | Occupant replacement via combined direct `pmxcfs`+storage write (T3), no explicit anchor — **supplementary context only, never load-bearing (§5, §7b)** | Removed the unsupported "silent blind spot — no event observed at all" claim, which contradicted Path A. A direct local `unlink()`/`write()` is itself an ordinary Path A (local VFS) operation (§7c); whether a co-resident watcher's `fsnotify` subscription actually observes it is not evaluated here. **T3 / out of scope for this anchor-less candidate** — local syscall delivery is not evaluated as a security guarantee against a root actor, because root can suppress, patch, or feed fabricated events to a co-resident witness regardless of what any single syscall would otherwise deliver (§5, §11a). **No in-scope verdict is derived from this row.** | **UNRESOLVED / NOT AUDITED** — whether a distributed watcher incidentally observes this (a root actor's local `rm` is still a local syscall on *some* node) is not verified or claimed here | No task is generated for this direct-root path (§7b) — real, but **supplementary/out-of-scope context only**, never part of Family B's classification; row 1 is Family B's actual demonstration | No *persisted* trust to silently inherit — does not mean the underlying substitution is detected or prevented (§24 item 3) |
| 3 | Clone to a new VMID | New locator, new `resource_id` regardless of family (ADR 0001) | Same | Same | Same |
| 4 | Snapshot rollback | Must revoke per §15/ADR 0005 §17 if a mechanism ever exists; this ADR grants nothing | Same | Same | No persisted state to revoke — the rollback-substitution risk itself is unaddressed by this family (§24 item 3) |
| 5 | Node migration | Requires explicit handling (§15); not solved by witness presence alone; whether migration off the watched node defeats coverage is exactly the unresolved cross-node question above, not a settled fact | **UNRESOLVED / NOT AUDITED** — distributed node-migration semantics never designed | N/A — task history is not node-bound | Unaffected — no persisted trust to carry across a migration |
| 6 | Witness/backend/node restart | Must fail closed (§14); this ADR implements no witness | **UNRESOLVED / NOT AUDITED** — multi-node restart/coordination semantics never designed | N/A | Unaffected — no persisted coverage claim exists to lose |
| 7 | Source-attestation epoch bump | Any prior-epoch evidence becomes authority-ineligible (§16, ADR 0003) | Same, if such evidence existed | Same | Same, if evidence were ever collected at check time |
| 8 | Compromised node/hostd (T4) | Out of scope; assumed away (§11, §11a) — row 2, for an anchor-less witness, is this same category of failure, not a distinct lesser one | Out of scope; assumed away (§11, §11a) — applies per-node, at every node in the distributed fleet | Same | Same |
| 9 | A stateful task-history witness's overlap sentinel: enough further tasks occur that the sentinel itself ages out of the retained set before the gap is detected | N/A — not a `pmxcfs`-witness scenario | N/A | **UNRESOLVED / NOT DESIGNED OR AUDITED HERE (full-review correction #2)** — whether the sentinel protocol's own gap-detection can itself be starved by the same bounded retention it relies on is one of the open questions this ADR leaves unresolved (§10, §24 item 10) | N/A |

## 21. B1 authorization boundary

Explicit, per the mission:

```text
WAVE B1 remains DEFERRED / NOT AUTHORIZED.

This ADR's own conclusion is UNRESOLVED for task history and for both
pmxcfs-witness variants, and NO-GO only for Families C/D/E/F-narrow. Any
eventual re-acceptance of THIS ADR would mean that corrected research
conclusion is accepted as the current record -- it would NOT authorize
WAVE B1, because this ADR proposes no sufficient mechanism for WAVE B1 to
implement.

WAVE B1 may only begin after a DIFFERENT, later, separately reviewed and
separately ACCEPTED ADR proposes an actual mechanism -- whether within the
task/pmxcfs-observation class this ADR audited, or a genuinely different
host-rooted class this ADR left unresolved (§24 item 7) -- satisfying ADR
0005 §14 and this ADR's §13 extensions, and passes ADR 0005 §14's
controlling test and this ADR's §10 same-slot witness test.
```

## 22. Phase 1C consequences

Unchanged: Phase 1C (policy/jobs/mutation authority) remains **BLOCKED**,
exactly as ADR 0005 §27 already sequenced. This ADR does not move Blocker B
any closer to closed, and therefore does not move Phase 1C any closer to
unblocked.

## 23. R0 boundary

Unchanged. R0 remains strictly read-only. ADR 0005 §24's list of what R0
must never do is unaffected by this ADR's conclusion — a NO-GO here changes
nothing about R0's already-accepted posture, since R0 never depended on
Blocker B closing (ADR 0005 §24, §27; `0.5-inventory-model.md`'s Phase 1
runtime activation gate references neither workload continuity nor trusted
enrollment).

## 24. Open questions

1. The actual design of a clone-resistant/externally-rooted continuity
   mechanism remains undesigned. **Corrected this revision (full-review
   correction #2, stale wording):** this ADR identifies that task-history
   lifecycle evidence, and both `pmxcfs`-filesystem-witness variants, all
   remain **UNRESOLVED** (not "NO-GO," and not "fails under stock
   capabilities" — neither phrase accurately describes any of the three
   audited designs any longer) — it does not propose a replacement design
   for any of them.
2. Whether a genuinely hardware-rooted, non-clonable, per-guest (not
   per-node) attestation primitive could ever exist for QEMU/LXC on
   commodity virtualization hardware is unresolved; stock Proxmox VE
   provides no such primitive today (§9 rows C/D).
3. **Family G (operator per-mutation re-attestation / ephemeral trust) —
   corrected framing (P2).** Whether this should be pursued as a
   deliberate design choice for a future mutation-authority ADR is not
   decided here. If it is ever pursued, that future ADR must state, and
   this ADR requires it to state, all of the following — none of which
   Family G gets for free merely by avoiding persistent state:
   - **operator confirmation by itself does not satisfy Blocker B** — a
     human saying "this is trusted" at time T is not continuity proof for
     time T+ε, for the identical reason ADR 0005 §8 already rejects bare
     operator assertion;
   - **CAS (`resource_id`/`binding_id`/`locator_generation`/
     `resource_continuity_revision`) prevents replay of a stale *backend
     decision*, not physical/logical occupant substitution** — ADR 0001
     explicitly permits those same tokens to remain unchanged across an
     observationally invisible same-slot delete/recreate (ADR 0001 row 10),
     so a CAS match alone does not prove the operator's confirmation still
     refers to the occupant actually being mutated;
   - **using a mutation model that never requires persistent
     `security_continuity=trusted` would itself require a separate
     architecture change** to ADR 0001/0005's accepted mutation-precondition
     formula (ADR 0001's policy-applicability intersection includes
     "trusted security continuity" as one of its required terms) — this
     ADR does not make, and cannot make, that change by implication;
   - **any such future model would still need its own safe point-in-time
     target-identity proof and/or fencing/serialization contract** closing
     the gap between "operator confirms occupant A" and "backend executes
     against whatever currently occupies the slot" — not yet defined by
     this ADR or by Family G as stated.
   A future mutation-design ADR remains free to accept or reject Family G
   on its own merits, but only after addressing all four points above.
4. The exact node-to-syscall dispatch behavior when a PVE API command
   targets a guest whose owning node differs from the node the request was
   issued against (relevant to whether a hypothetical local-`pmxcfs`-watch
   witness could ever be co-located correctly) is **UNKNOWN** at
   FACT-DOC/FACT-SOURCE strength this session and would need independent
   verification before any future ADR relies on it.
5. **Corrected this revision (full-review correction #2):** Surface A's
   exact parameters are now **FACT-SOURCE**, confirmed directly against
   `PVE::RESTEnvironment` (`fork_worker`, `active_workers`,
   `broadcast_tasklist`), `PVE::Service::pvestatd`, and `PVE::Cluster` this
   session (§7a) — specifically: `MAX_FINISHED = 25`; the cache is updated
   **immediately at every worker start** (`fork_worker`) *and* on
   `pvestatd`'s separate 10-second periodic cycle (`my $updatetime = 10`),
   not the 10-second cycle alone as a prior revision stated; and
   `broadcast_tasklist()`'s **executable** truncation threshold is **32
   KiB** (`while ($size >= (32 * 1024))`), not 128 KiB — the 128 KiB
   `CFS_MAX_STATUS_SIZE` figure is only a `# TODO: update to 128 KiB in
   PVE 8.x` comment on code that still enforces 32 KiB in the source
   checked this session. Surface B's `index`/`index.1`
   rotation-on-fixed-size *behavior* (`maxsize = 50000`) is likewise
   **FACT-SOURCE**. What remains corroborated only at forum strength, not
   re-derived from source, is the *further* archive-file rotation beyond
   `index.1` (community-reported figures of ~512 KiB per archive file, ~20
   archive files retained, on the order of ~100,000 total entries) — these
   specific numbers should be re-verified against the exact deployed PVE
   release before any future ADR cites them as a specific bound, and so
   should the 32 KiB/`MAX_FINISHED=25` figures themselves, against
   whichever PVE release a future ADR targets, since the TODO comment
   shows Proxmox itself intends to change at least one of them.
6. Whether Proxmox will ever ship a documented, monotonic, gapless,
   officially-retained cluster-wide event stream (closing ADR 0002's own
   Class B gap) remains unknown and outside Hubinet Ops's control; this ADR
   does not assume it will.
7. **Broader host-rooted witness classes — genuinely unresolved, not
   disproven.** This ADR audited task history (both surfaces, §7a) and a
   **single-node** variant of `pmxcfs` filesystem-level change observation.
   It did **not** primary-source audit, and reaches no conclusion about: a
   **stateful, fail-closed overlap-sentinel task-history witness** (§10,
   item 12 below) — newly identified this revision (full-review correction
   #2); a **distributed, per-node `pmxcfs` watcher (Family A2, §9)** —
   depending on Path A's (unproven) completeness at every node and on
   undesigned multi-node coverage/gap semantics; the Linux kernel audit
   subsystem/`auditd` rules watching specific syscalls against
   `pmxcfs`/guest storage paths; LSM (SELinux/AppArmor-class) hooks;
   `eBPF`-based syscall/tracepoint interception; storage-layer
   block-change tracking (e.g. ZFS/LVM snapshot-diffing) as an independent
   occupant-substitution witness; a witness deliberately backed by an
   explicit root-resistant/external trust anchor (§5/§11a) rather than
   ordinary co-resident-process trust; or Family F's broader
   externally-rooted/out-of-band per-workload identity class (§9) — a
   specific workload's identity tracked/attested by an external system
   through a channel that is neither guest-resident nor node-bound
   hardware. Any of these could, in principle, observe an actual
   physical/logical occupant replacement (§4 concept 3) through a channel
   a node-root actor cannot as easily bypass as the audited designs here —
   or could fail for reasons this ADR has not examined. A future ADR
   auditing one of these must perform its own primary-source research and
   its own pass against §6's required property and §10's same-slot witness
   test; this ADR reaches no NO-GO on the audited classes for such an ADR
   to inherit — the only genuine "No" verdict here is for Families
   C/D/E/F-narrow (§12), which is unrelated to any of these broader
   classes.
8. **`pmxcfs` notification absence — corrected and bounded, not exhaustive
   (P2 #1/#2 correction, reopening).** §7c's finding that no
   `fuse_lowlevel_notify_*`-class call exists is confirmed across five core
   `src/pmxcfs/` files (`pmxcfs.c`, `server.c`, `dfsm.c`, `memdb.c`,
   `cfs-plug-memdb.c`), not the entire repository — `cfs-plug.c`,
   `cfs-plug-link.c`, `cfs-plug-func.c`, `cfs-utils.c`, `database.c`,
   `status.c`, `loop.c`, and `dcdb.c` remain unchecked at this citation
   granularity. **Critically, and corrected this reopening: this absence
   finding bears only on Path B (cross-node, Corosync-replicated changes,
   §7c) — it does not, by itself, say anything about Path A (same-node,
   locally-originated `fsnotify`/`inotify` delivery, which is ordinary VFS
   behavior independent of whether the FUSE driver implements low-level
   notify callbacks).** Path A's actual completeness for `pmxcfs`
   specifically remains its own, separate open question — **UNKNOWN**, not
   answered by the notify-callback absence finding at all. A future ADR
   relying on §7c should independently re-check the remaining files (for
   the Path B finding) and independently primary-source verify Path A's
   completeness (a distinct question this ADR does not resolve), plus the
   exact deployed PVE/kernel release's FUSE `inotify` support status
   (time-bound to the mid-2025 upstream activity located this session),
   before treating either as settled.
9. **Which node executes a given operation's local syscall — directly
   relevant to both Family A (single-node) and Family A2 (distributed)
   (restated from item 4, now load-bearing for the A/A2 split).** Whether
   PVE's API-proxy-to-owning-node pattern guarantees a predictable,
   single node executes a given guest's config-lifecycle syscall — and
   whether that node can ever differ from wherever an operator issued the
   request — is **UNKNOWN** at FACT-DOC/FACT-SOURCE strength this session.
   This is precisely what determines whether a single-node witness has any
   chance of coincidentally covering a given operation, and what a
   distributed witness's node-count/placement would need to guarantee;
   neither is resolved here.
10. **Whether the single-node `pmxcfs` watcher (Family A) is ultimately
    sufficient, insufficient, or itself further inconclusive — corrected
    post-merge to be an explicitly open question, not a settled NO-GO.**
    A second automated review found that the prior revision's NO-GO
    verdict for this design outran its own evidence: items 8 and 9 above
    are exactly the two primary-source gaps (Path B's repository-wide
    absence, and node-dispatch predictability) that a genuinely conclusive
    audit of this design would need to close before reaching *either* a
    NO-GO or a sufficiency finding. This ADR deliberately does not attempt
    to close them in this corrective pass, to avoid broadening this
    revision's scope beyond the accepted findings — a future ADR (or a
    further revision of this one) auditing the remaining `src/pmxcfs/`
    files and the node-dispatch question could move this design to either
    NO-GO or a genuine sufficiency finding, or could leave it open for a
    different reason not yet identified.
11. **T3-consistency sweep — a whole-document review, corrected in a
    prior revision (P2 #2).** A full-review pass found that §5's rule (T3
    must never be the load-bearing NO-GO reason, nor a mandatory in-scope
    property, for an anchor-less witness) had not been propagated
    consistently through every section that touches direct `pmxcfs`
    manipulation. That revision re-grounded §8 property 6, the Family B
    table row and its §10 demonstration, Family H (§9 and §10), §11's
    taxonomy (fixing a "T2/T3" conflation that wrongly implied ordinary
    PVE admin API privilege equals host-root shell access), §13's future-
    mechanism requirements (now requiring an explicit T3 contract rather
    than an unconditional bypass-closure mandate), and §20's adversarial
    matrix (row 2) — all in the direction of removing confirmed-fact
    framing for anything this ADR did not actually establish as fact. A
    future review should re-scan for the same failure mode whenever new
    material referencing T3/direct-`pmxcfs` access is added.
12. **Whether task history (Family B) — via a stateful, fail-closed
    overlap-sentinel design — is ultimately sufficient, insufficient, or
    itself further inconclusive — new this revision (full-review
    correction #2), mirroring item 10's treatment of the single-node
    `pmxcfs` watcher.** A fresh full-review pass found that the prior
    NO-GO verdict for task history outran its own evidence: it proved only
    that a *stateless* observer fails, not that every conceivable
    task-history design does. The specific open questions a genuinely
    conclusive audit of a stateful design would need to close (§10)
    include at least: whether Surface B's retained-set behavior is
    sufficiently prefix/append-structured for an overlap sentinel to prove
    continuous coverage; whether Surface A can participate safely or
    should be ignored given its truncating, non-append-only nature;
    complete-fetch/pagination/concurrent-rotation semantics; per-node
    sentinel ownership; node join/removal/restart behavior; how initial
    enrollment establishes the first trustworthy overlap point; whether
    exact UPID uniqueness is sufficient for the sentinel role; whether
    every in-scope T1/T2 identity-breaking operation reliably generates
    the required task worker record for both QEMU and LXC; version-
    pinning/upgrade behavior; and whether a coverage gap could occur while
    an old sentinel nonetheless remains visible. This ADR deliberately
    does not attempt to design or close these in this corrective pass, to
    avoid broadening this revision's scope beyond the accepted findings —
    a future ADR (or a further revision of this one) closing them could
    move task history to either NO-GO or a genuine sufficiency finding, or
    could leave it open for a different reason not yet identified. ADR
    0002's own existing **UNKNOWN** classification for trusted
    destroy/create event-chain evidence (requiring an accepted contract
    guaranteeing contiguous event/cursor semantics) is the correct
    posture to carry forward, not a claim of impossibility.

## 25. Acceptance checklist

This ADR was independently reviewed and accepted, reopened after an
automated review raised two P2 findings (both accepted as valid, one
load-bearing) and corrected, then **re-accepted** after a fresh independent
review found no further P1/P2 findings and merged as PR #44 (merge commit
`d6144f72164162a1de6c3a73aa23a771b317b05d`). A second automated review,
arriving after that merge, raised two further P2 findings, both accepted
as valid (single-node `pmxcfs`-watcher NO-GO verdict outran its own
evidence; §10's critical test relied on an out-of-scope T3 scenario for
Family B), which were corrected in a post-merge follow-up (PR #45). **A
full end-to-end independent architecture review of that follow-up found
two further systemic P2 families, both accepted as valid** (P2 #1: §7a's
task history was modeled as one generic surface when Proxmox actually
exposes two materially different ones; P2 #2: the T3-consistency rule
had not been propagated through every section touching direct `pmxcfs`
manipulation), both corrected. **A second, fresh full end-to-end
independent review of that correction found two further P2 findings (P1 =
0), both accepted as valid, corrected in this revision:** (P2 #1) Family
B (task history)'s NO-GO was itself unproven — this ADR's own analysis
only rules out a *stateless* observer, not a *stateful*, fail-closed
overlap-sentinel design, which was never designed or audited; Family B is
downgraded to **UNRESOLVED/NOT FULLY AUDITED**, and Family H must inherit
B's unresolved status rather than being manufactured into a NO-GO for any
combination that includes B. (P2 #2) Surface A's facts required a further
correction: `broadcast_tasklist()`'s *executable* truncation threshold is
**32 KiB**, not 128 KiB (the 128 KiB `CFS_MAX_STATUS_SIZE` figure is only
an unimplemented `# TODO` comment), and the cache is updated **immediately
at every worker start** (`fork_worker`), not only on `pvestatd`'s 10-second
cycle. **ADR 0006 is NOT re-accepted in this pass** — Status remains
`PROPOSED (full-review corrections pending)`. This checklist, extended
with items 9–22, is retained as the standing record a fresh review must
verify before any future re-acceptance — not of a trust-granting
mechanism, since none is proposed:

1. Does this ADR select or authorize any mechanism sufficient for
   `security_continuity=trusted`? **No** — confirm this remains true
   before accepting.
2. Does this ADR authorize WAVE B1? **No** (§21) — confirm this remains
   true before accepting.
3. Does this ADR weaken ADR 0001's invisible same-slot destroy/recreate
   limitation, ADR 0003's epoch authority-eligibility rule, or ADR 0005's
   Family A/B/C rejection? **No** — confirm no drift was introduced.
4. Does this ADR's §7 research (task history, `pmxcfs` change delivery)
   accurately reflect the cited sources, and are FACT-DOC/FACT-SOURCE/
   INFERENCE/UNKNOWN tags used honestly, without overclaiming a guarantee
   Proxmox does not document — **and, critically, without conflating
   same-node (Path A) `fsnotify` delivery with cross-node (Path B)
   Corosync-replicated delivery** (§7c)? Verify before accepting.
5. Does §10's same-slot witness test correctly show that a **stateless**
   observer of **Family B** — using an **in-scope, ordinary T1/T2 API/CLI
   destroy+recreate** that genuinely produces task records (not a T3
   direct-`pmxcfs`-write) — cannot prove the bounded-retention/no-cursor
   gap didn't occur, while explicitly declining to claim that a
   *stateful*, fail-closed overlap-sentinel design must also fail — and
   does it correctly present both **Family B** and **Family A
   (single-node)** as **inconclusive/UNRESOLVED**, neither passing nor
   failing, rather than either as a confirmed failure? Verify before
   accepting.
6. Does §11/§11a correctly keep node/hostd trust and resource continuity
   as two separate, unmerged axes, while stating precisely that a
   co-resident witness without an explicit root-resistant/external anchor
   is not a defense against T3 (equivalent to T4), that coupling
   witness-authority-eligibility to `node_trust_state` is at most a
   *necessary* fencing gate — not sufficient by itself — and that T3
   resilience may only be claimed once a separately accepted node/hostd
   attestation contract (still unresolved, ADR 0001 §"Nierozstrzygnięte
   kwestie" #6) actually provides root-compromise detection/prevention
   semantics? Verify before accepting.
7. Does this ADR's negative conclusion stay explicitly scoped to
   **Families C/D/E/F-narrow alone (§9 rows C/D/E/F)**, without claiming
   that task history (§9 row B, now UNRESOLVED), the single-node `pmxcfs`
   design (§9 row A, UNRESOLVED), the distributed variant (row A2), or
   every conceivable host-rooted witness — kernel audit/LSM/`eBPF`-based,
   or externally-anchored designs — is impossible (§1, §6, §12, §24 item
   7)? Verify before accepting.
8. Does Family G's treatment (§9, §10, §12, §24 item 3) avoid claiming
   "immunity" and instead state plainly that operator confirmation alone
   does not satisfy Blocker B, that CAS prevents stale backend decisions
   rather than occupant substitution, and that a persistent-trust-free
   mutation model would need its own ADR 0001/0005 architecture change plus
   a safe point-in-time target proof? Verify before accepting.
9. Does §7c precisely separate Path A (same-node, locally-originated
   VFS/`fsnotify` delivery — classified **UNKNOWN**, plausible/expected,
   neither proven nor disproven here) from Path B (cross-node,
   Corosync-replicated delivery — a negative finding bounded to five
   checked files, not an exhaustive proof)? Verify before accepting.
10. **Updated this pass (full-review correction #2).** Is the exact
    classification correctly stated: PVE task-history witness =
    **UNRESOLVED/NOT FULLY AUDITED** (not NO-GO); single-node `pmxcfs`
    filesystem watcher = UNRESOLVED/NOT FULLY AUDITED; distributed
    per-node `pmxcfs` filesystem watcher = UNRESOLVED/NOT AUDITED HERE;
    only Families C/D/E/F-narrow remain a genuine "No" — and is this
    propagated consistently through the opening classification block, §6,
    §7a/§7d, §8, §9's Family A/B/H rows, §10, §12, §13, §19/§20, and §24
    (§9, §12, §24 item 7/10)? Verify before accepting.
11. **New this post-merge correction (P2 #1).** Does the ADR avoid
    asserting, anywhere, that the single-node design's cross-node coverage
    gap is a *proven* architectural fact, rather than an unproven inference
    from a bounded, five-file, `notify`-substring search that could miss an
    unchecked file or a differently-implemented remote-delivery mechanism
    (§7c, §24 item 8)? Verify before accepting.
12. **New this post-merge correction (P2 #2).** Does the ADR avoid using
    the T3 direct-`pmxcfs`+storage-write bypass as the *load-bearing*
    reason for Family B's failure, reserving it only as supplementary,
    out-of-scope threat-model context (§5's consistency rule, §7b, §10,
    §19, §20)? Verify before accepting.
13. Does §7c's `pmxcfs`-notification-absence claim (Path B only) stay
    bounded to the five files actually checked (§7c, §24 item 8), rather
    than asserting an exhaustive whole-repository absence, and is the
    FUSE-`inotify`-support finding pinned to the mid-2025 kernel status
    located this session rather than presented as a permanent fact? Verify
    before accepting.
14. Does this ADR authorize any schema, runtime, hostd, HA control, or
    mutation implementation? **No** — confirm before accepting.
15. Is Blocker B left explicitly OPEN, the future positive Blocker-B
    mechanism ADR left explicitly NOT STARTED/UNRESOLVED, R0 explicitly
    unaffected, and Phase 1C explicitly BLOCKED? Verify before accepting.
16. Does this ADR avoid describing this post-merge correction as a
    rollback of ADR 0001–0005, and avoid reopening R0 (§1, §22, §23)?
    Verify before accepting.
17. Does §7a correctly model two distinct PVE task surfaces —
    `/cluster/tasks`'s recent, corosync-distributed status cache (bounded
    to `MAX_FINISHED=25` finished tasks, updated immediately at every
    worker start *and* additionally refreshed every 10s by `pvestatd`,
    with its serialized payload truncated at an **executable 32 KiB**
    threshold — not the unimplemented 128 KiB `CFS_MAX_STATUS_SIZE`
    comment) and `/nodes/<node>/tasks`'s bounded, node-local
    `index`/`index.1` archive — without claiming `/cluster/tasks` is
    backed by the archive files, and is this corrected two-surface model
    propagated consistently through §7d, §8, the Family B row (§9), §10's
    explicit consecutive-observation witness, §19, §20, and §24 (§7a, §24
    item 5)? Verify before accepting.
18. **New this full-review pass (P2 #2).** Does a whole-document search
    for "T3", "direct pmxcfs", "silent blind spot", "no event observed",
    "never observes", "must explicitly close", "shared T3", and "T2/T3"
    confirm none of the remaining occurrences contradicts §5's consistency
    rule — specifically: §8 property 6 (no confirmed claim that a
    `pmxcfs` witness fails to observe a local root syscall), the Family B
    table row (§9, re-grounded in bounded-retention/no-cursor, not "no
    task"), Family H (§9/§10, re-grounded in the in-scope T1/T2 witness,
    not "shared T3"), §11 (T3-only, not "T2/T3"), §13 (an explicit T3
    contract requirement, not an unconditional bypass-closure mandate),
    and §20 row 2 (no "silent blind spot" claim for an anchor-less
    `pmxcfs` witness)? Verify before accepting.
19. **New this pass (full-review correction #2, P2 #1).** Does this ADR
    avoid claiming task history (Family B) fails, anywhere, on the basis
    of a *stateless*-observer analysis alone — and does §10 correctly
    describe a plausible, unaudited *stateful* overlap-sentinel
    counter-design (durable per-node sentinel; overlap check at O2;
    fail-closed revocation on sentinel disappearance; sentinel advances
    only on proven overlap) without claiming that design succeeds? Verify
    before accepting.
20. **New this pass (full-review correction #2, P2 #1).** Does §24 item
    12 enumerate the open questions a genuinely conclusive audit of that
    stateful design would need to close (Surface B's prefix/append
    structure; Surface A's participation or exclusion; pagination/
    concurrent-rotation; per-node sentinel ownership; node join/removal/
    restart; initial-enrollment overlap point; UPID-uniqueness
    sufficiency; QEMU/LXC parity of task generation; version pinning; and
    stale-sentinel-visible false negatives), and does §9's Family H row
    correctly exclude B from its independently-"insufficient" combination
    set — inheriting B's unresolved status for any B-containing
    combination, rather than manufacturing a NO-GO? Verify before
    accepting.
21. **New this pass (full-review correction #2, P2 #2).** Are Surface A's
    facts correctly stated: the executable `broadcast_tasklist()`
    truncation threshold is **32 KiB**, with the 128 KiB
    `CFS_MAX_STATUS_SIZE` figure recorded only as an unimplemented `# TODO`
    comment; and the cache is updated **both** immediately at every
    `fork_worker()` call **and** on `pvestatd`'s separate 10-second cycle,
    not the 10-second cycle alone? Does the ADR distinguish task
    *creation* (a UPID exists) from guaranteed *observation* in any one
    Surface A snapshot, rather than treating an ordinary operation's task
    creation as an unconditional visibility guarantee (§7a, §7b, §10, §24
    item 5)? Verify before accepting.
22. Does the adversarial matrix (§20) and failure-mode table (§19) avoid
    "Witness would see..." or similarly definite phrasing wherever the
    same cell's completeness is actually UNKNOWN or UNRESOLVED, preferring
    "may observe; completeness remains UNKNOWN/UNRESOLVED" instead? Verify
    before accepting.

Re-acceptance of this ADR, if it occurs, would record the corrected
research conclusion (UNRESOLVED for task history and for both the
single-node and distributed `pmxcfs`-witness variants; NO-GO only for
Families C/D/E/F-narrow; and UNRESOLVED for every other family this ADR
does not audit) as the current architecture record. It would not, by
itself, authorize any further implementation.

## Sources / Evidence

Read this session (August 2026), in addition to the ADR 0001/0002/0003/0005
sources they build on. Findings pinned to upstream mailing-list activity are
current only as of the date noted; a future ADR relying on them must
re-verify against the then-current kernel/PVE release rather than citing
this ADR's date as still current (§7c, §24 item 8). The §7c sources below
(`pmxcfs`/FUSE/`inotify`) establish Path B (cross-node, Corosync-replicated
delivery) only — none of them establishes, or is cited to establish,
anything about Path A (same-node, locally-originated VFS `fsnotify`
delivery), which remains open (§7c, §24 item 8/9). The §7a sources are a
separate research thread (the two PVE task surfaces) and are not part of
that Path A/B distinction.

**§7a — the two PVE task surfaces:**

- [`proxmox/pve-manager`, `PVE/API2/Cluster.pm`](https://github.com/proxmox/pve-manager/blob/master/PVE/API2/Cluster.pm) — the `tasks` route (`GET /cluster/tasks`), documented in its own registration as *"List recent tasks (cluster wide)"*, calling `PVE::Cluster::get_tasklist()` with no cursor/sequence field of its own (§7a)
- [`proxmox/pve-manager`, `PVE/API2/Tasks.pm`](https://github.com/proxmox/pve-manager/blob/master/PVE/API2/Tasks.pm) — `node_tasks` (`GET /nodes/<node>/tasks`), reading `/var/log/pve/tasks/index` and `index.1` via `File::ReadBackwards`; `start`/`limit`/`since`/`until`/`source=archive|active|all` parameters, no monotonic cursor (§7a)
- [`proxmox/pve-cluster`, `src/PVE/Cluster.pm`](https://github.com/proxmox/pve-cluster/blob/master/src/PVE/Cluster.pm) — `get_tasklist()` reading a per-node, corosync-distributed in-memory/KV status cache (`ipcc_get_status("tasklist", $node)`), and `broadcast_tasklist()`'s **executable** truncation loop, `while ($size >= (32 * 1024)) { pop @$data; ... }` — an actual 32 KiB cap, with the 128 KiB `CFS_MAX_STATUS_SIZE` figure appearing only in a `# TODO: update to 128 KiB in PVE 8.x` comment on that same code (§7a, corrected this pass, full-review correction #2/P2 #2)
- [`proxmox/pve-manager`, `PVE/Service/pvestatd.pm`](https://github.com/proxmox/pve-manager/blob/master/PVE/Service/pvestatd.pm) — the periodic `update_status()` loop calling `$rpcenv->active_workers()` then `PVE::Cluster::broadcast_tasklist($tlist)` every 10 seconds (`my $updatetime = 10`) — an *additional* refresh path, not the sole one (§7a)
- [`proxmox/pve-common`, `PVE/RESTEnvironment.pm`](https://github.com/proxmox/pve-common) — `active_workers()`'s `MAX_FINISHED = 25` cap on retained finished tasks (Surface A); the separate `index`/`index.1` archive rotation at `maxsize = 50000` (Surface B); UPID encoding; and `fork_worker()` calling `$self->active_workers($upid, $sync)` then `$self->broadcast_tasklist($tlist)` **immediately at every worker start**, confirming Surface A is updated on this path in addition to `pvestatd`'s periodic cycle (§7a, corrected this pass, full-review correction #2/P2 #2)

**§7c — `pmxcfs`/FUSE/`inotify` (Path A/B only, per the note above):**

- [`proxmox/pve-cluster`, `src/pmxcfs/pmxcfs.c`](https://github.com/proxmox/pve-cluster/blob/master/src/pmxcfs/pmxcfs.c) — FUSE `fuse_operations` table; absence of any kernel-notification callback (§7c)
- `proxmox/pve-cluster`, `src/pmxcfs/server.c`, `dfsm.c`, `memdb.c`, `cfs-plug-memdb.c` — four further core files checked this session for any `notify`-related call; none found (§7c). The remaining files under `src/pmxcfs/` (`cfs-plug.c`, `cfs-plug-link.c`, `cfs-plug-func.c`, `cfs-utils.c`, `database.c`, `status.c`, `loop.c`, `dcdb.c`) were not individually re-checked (§24 item 8).
- [Proxmox Cluster File System (pmxcfs) documentation](https://pve.proxmox.com/pve-docs/chapter-pmxcfs.html) — `pmxcfs` architecture, `/etc/pve` mount, Corosync-backed replication (§7c)
- Linux kernel mailing list, [RFC PATCH 0/7] Inotify support in FUSE and virtiofs (originally posted ~October 2021, `https://lkml.kernel.org/linux-fsdevel/YYMNPqVnOWD3gNsw@redhat.com/t/`) — RFC-stage status of FUSE `inotify` support (§7c)
- Linux kernel mailing list / `virtiofsd` issue tracker, discussion on disallowing `inotify` watches on unsupported filesystems and on FUSE/`virtiofs` `inotify` support, with activity as recent as **May 2025** confirming the feature remained unmerged in the mainline kernel at that date — `inotify_add_watch()` silently succeeding without delivering events on unsupported filesystems (§7c; time-bound finding, see note above)
- Community-reported (forum-strength, not independently re-derived from source this session) further archive-file rotation figures beyond `index.1`, cited only as corroborating context, not as the load-bearing claim (§7a, §24 item 5)

Repositories on GitHub are official read-only mirrors; the authoritative
upstream remains [git.proxmox.com](https://git.proxmox.com/). Conclusions
about what a given behavior does *not* guarantee are architectural
inferences from the cited contract and source, not a claim of an
additional Proxmox guarantee.
