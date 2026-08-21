# ADR 0006: stronger workload-continuity proof — trusted host lifecycle witness research

Status: **PROPOSED**

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

**This ADR's own status is PROPOSED, not ACCEPTED.** Its conclusion (§12) is
an explicit **NO-GO** for a practical mechanism achievable today. Consistent
with the mission that produced it:

- this ADR does not, by itself, close Blocker B for mutation authority —
  Blocker B remains **OPEN**, exactly as ADR 0005 left it;
- this ADR does not, by itself, authorize WAVE B1 — WAVE B1 remains
  **DEFERRED / NOT AUTHORIZED**;
- even independent review and separate acceptance of *this* ADR would only
  mean the research conclusion below is accepted as the current record —
  because that conclusion is negative, acceptance still would not authorize
  WAVE B1 or grant `trusted` to anything; only a **different**, later ADR
  that actually proposes a sufficient mechanism could do that;
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

## 2. Scope and non-goals

In scope: the trusted host lifecycle witness hypothesis; six further
comparison families (§9); primary-source research on Proxmox VE task/event
observability and `pmxcfs` filesystem-change observability; the adversarial
same-slot destroy/recreate test against each family; the explicit trust-root
separation between node/hostd compromise and resource continuity; and the
extended minimum-property list any future mechanism must satisfy.

Explicitly **not** in scope, and not authorized by this ADR:

- any schema, table, column, or enum-value implementation;
- any bump of the authority schema version (currently `5`, merged on
  `main`); this ADR does not authorize a schema v6 package;
- self-acceptance of this ADR by the agent that wrote it;
- designing, implementing, or partially wiring `hostd`, a witness daemon, an
  enrollment ceremony, or any HA control surface;
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
| **Identity-breaking lifecycle event** | destroy, create, clone, snapshot rollback, backup restore, or root-disk/config replacement for a `(inventory_source_id, vmid)` slot — any event after which the *occupant* may no longer be the same physical/logical workload |
| **Trusted host lifecycle witness** | a hypothesized trusted observer (process/component) that watches identity-breaking lifecycle events for a source, independent of guest-readable config/disk content |
| **`workload_epoch_id`** | a hypothesized opaque, backend-generated value per slot, incremented (or revoked) whenever the witness observes, or cannot prove the absence of, an identity-breaking event; stored only in Hubinet's own authority state, never in guest config/disk/vTPM |
| **Witness coverage epoch** | the interval during which the witness can prove continuous, gapless observation for a given slot |
| **Node/hostd trust root** | ADR 0001's existing, separate claim that a specific physical/virtual PVE node and its control-plane software are not compromised — a prerequisite this ADR's candidates may or may not additionally require, never something they can grant to *resource* continuity by assumption |
| **Task / UPID** | Proxmox's per-operation identifier and log record for an asynchronous worker (`PVE::UPID`); see §7 |
| **`pmxcfs`** | the cluster-replicated configuration filesystem backing `/etc/pve`, implemented as a FUSE mount over an internal SQLite database (`/var/lib/pve-cluster/config.db`), synchronized cluster-wide via Corosync |

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
section). What matters is whether a candidate is (a) detected, (b)
fail-closed even though undetected, or (c) silently defeated, at **each** of
T1–T3, and the answer must be stated for each, not glossed over. This is the
same discipline ADR 0005 §5/§28 already applies; this ADR extends it with
the T1–T4 tiering the mission requires and applies it specifically to
lifecycle-observation-based candidates, which ADR 0005 did not evaluate.

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

Both tests apply to every candidate in §9.

## 7. Primary-source findings: Proxmox VE lifecycle/task/`pmxcfs` observability

Legend, identical to ADR 0001/0002/0003/0005's own discipline: **FACT-DOC**
(documented by Proxmox), **FACT-SOURCE** (behavior visible in official
Proxmox source, read this session), **INFERENCE** (architectural conclusion
from the facts), **UNKNOWN** (not confirmed by an official contract).

### 7a. `/cluster/tasks` and per-node task lists

- **FACT-SOURCE.** `GET /cluster/tasks` (`PVE::API2::Cluster`) returns
  whatever `PVE::Cluster::get_tasklist()` provides, filtered by caller
  privilege (own tasks unless `Sys.Audit`); the handler does not sort,
  page, or attach any sequence/cursor field of its own
  (`proxmox/pve-manager`, `PVE/API2/Cluster.pm`, the `tasks` route).
- **FACT-SOURCE.** Each task is identified by a `UPID`
  (`node:pid:pstart:starttime:type:id:user:`), assembled from the node
  name, worker OS PID, the PID's start time (`pstart`, used to disambiguate
  PID reuse), a wall-clock start timestamp, worker type, target ID, and
  acting user (`PVE::UPID::encode`, `proxmox/pve-common`,
  `PVE/RESTEnvironment.pm`). **A UPID is not a monotonic counter and does
  not reference a prior UPID** — there is no chain, hash-link, or
  sequence field tying one task to "the task before it" for a given slot.
  Two tasks for the same VMID carry no structural proof of adjacency beyond
  their own wall-clock `starttime`, and wall-clock time is not concurrency
  authority (mirrors ADR 0002's own rule for `source_config_revision`/
  timestamps generally).
- **FACT-SOURCE.** Task logs are written under `/var/log/pve/tasks/`, with
  an archive `index` file. The archive index is rotated on a **fixed size
  threshold** (`RESTEnvironment.pm`: `my $maxsize = 50000; # about 1000
  entries`; when exceeded, the file is renamed `index.1` and a fresh index
  begins) — i.e. a **rolling window**, not a permanent, complete, or
  officially size-unbounded audit trail. Community-observed defaults report
  a further archive-file rotation (roughly 512 KiB per archive file, newest
  ~20 archive files retained, on the order of ~100,000 total entries) —
  this specific figure is **UNKNOWN** at FACT-DOC/FACT-SOURCE strength this
  session (forum-sourced, not independently re-derived from source here)
  and is cited only as corroborating context, not as the load-bearing claim.
  The load-bearing claim is the confirmed-in-source rotation-on-size
  behavior itself: **task history is a bounded rolling window, not a
  durable, officially-retained-forever ledger.**
- **FACT-SOURCE.** Task logs and the active/archive index are **per-node**
  local files (`/var/log/pve/tasks/` is a regular node-local path, not a
  `pmxcfs`/`/etc/pve` path). `/cluster/tasks`'s cluster-wide view depends on
  cross-node aggregation whose exact real-time consistency contract is
  **UNKNOWN** at FACT-DOC strength this session.
- **INFERENCE**, consistent with ADR 0002 §"Kiedy dokładnie wolno ustawić
  `confirmed_removed`", Class B: no monotonic, gapless, officially
  documented event/task cursor exists for stock PVE. This ADR's own
  research reaches the identical conclusion ADR 0002 already reached and
  marked **UNKNOWN** — this is expected corroboration, not a new finding,
  and is restated here because it directly determines whether a lifecycle
  witness can prove gapless coverage from PVE's own record alone (§8, §10):
  **it cannot** — a witness's own continuously-running observation, not
  PVE's task retention, would have to be the actual coverage guarantee
  (§8).

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
  guest's config object without ever invoking `qm`/`pct`/the API at all,
  producing **no task, no UPID, no log entry**. This is not a stock-PVE
  defect — it is an inherent consequence of `pmxcfs` being a shared,
  directly-writable configuration store rather than an access-mediated
  service boundary. It is also exactly the T3 tier (§5): a task-history-only
  witness has **zero coverage** against T3, by construction, regardless of
  retention or cursor properties.

### 7c. `pmxcfs` FUSE architecture and file-change notification

- **FACT-SOURCE.** `pmxcfs`'s FUSE operation table
  (`proxmox/pve-cluster`, `src/pmxcfs/pmxcfs.c`) registers exactly:
  `getattr, readdir, mkdir, rmdir, rename, open, read, write, truncate,
  create, unlink, readlink, utimens, statfs, init, chown, chmod`. **No
  kernel-notification callback of any kind is registered** — no call to
  `fuse_lowlevel_notify_inval_entry`, `fuse_notify_poll`, or any equivalent
  low-level invalidation/notification primitive appears anywhere in this
  operation table.
- **INFERENCE, consequence for cluster-replicated changes.** A change
  applied to a guest's config on node A is propagated to node B via
  Corosync and applied inside `pmxcfs`'s own in-memory/SQLite state on node
  B. Because `pmxcfs` never calls a kernel notify primitive, the Linux VFS
  dentry/inode cache on node B is never told anything changed as a *result*
  of that Corosync-originated update — an `inotify`/`fanotify` watch on
  node B's `/etc/pve` mount has no confirmed mechanism by which it would
  ever fire for a change whose *origin* was another node. A witness would
  therefore need to be co-located with wherever the actual destroy/create
  syscall is issued for a given guest — i.e., potentially every node the
  guest could ever occupy, or every node any operator could ever issue a
  command from, depending on how PVE's API proxies node-local operations
  (**UNKNOWN** at FACT-DOC strength this session which node actually
  executes the syscall when a command is issued against a different node's
  API endpoint; the general PVE proxy-to-owning-node pattern is
  well-established, but this ADR does not re-verify it as a citation-grade
  fact here).
- **FACT-SOURCE, general kernel limitation, independent of `pmxcfs`.**
  Linux `inotify`/`fanotify` are documented as supported for local kernel
  filesystems; FUSE-backed filesystems have historically lacked reliable
  kernel-level change-notification delivery, and as of the most recent
  upstream discussion found this session (early-2025 Linux kernel mailing
  list activity, "inotify: disallow watches on unsupported filesystems"),
  `inotify_add_watch()` can **silently succeed without error on filesystems
  that do not actually deliver events** — a dedicated patch was still being
  discussed specifically because callers cannot otherwise tell the
  difference between "watching and will fire" and "watching and will never
  fire." Kernel-level `inotify` support for FUSE itself remains, at best, an
  RFC-stage patch series (originally posted ~October 2021, targeting
  `virtiofs`) rather than a stock, shipped, documented guarantee.
- **Conclusion of §7c:** a `pmxcfs`-filesystem-watch approach has **no
  documented completeness guarantee** even for changes originated on the
  same node the watcher runs on, has **no mechanism at all** for changes
  replicated in from other nodes (absent an explicit `pmxcfs` code change
  Hubinet Ops does not control), and the underlying kernel primitive it
  would depend on is documented to silently pretend to work on filesystems
  where it does not. This is not merely "unverified" (**UNKNOWN**) — it is
  an identified, sourced, negative finding: the specific mechanism the
  mission asked to investigate ("filesystem watch (inotify/fanotify/audit)
  na pmxcfs") does not, as a matter of confirmed upstream architecture,
  provide a contract a security decision could be built on.

### 7d. Summary table

| Property required for a witness | Stock PVE support |
| --- | --- |
| Monotonic, gapless task/event cursor | **No** — UPID is not a sequence; retention is a rolling window (§7a) |
| Officially guaranteed task-history retention | **No** — fixed-size rotation, bounded window, no documented permanence (§7a) |
| Task creation for *every* identity-breaking event | **No** — only for the high-level API/CLI path; direct `pmxcfs` write bypasses it entirely (§7b) — a T3-tier gap |
| Reliable local-node `pmxcfs` change notification (`inotify`/`fanotify`) | **No documented guarantee** — `pmxcfs` implements no kernel notify callback; FUSE-level `inotify` support is RFC-stage, and the kernel can silently no-op the watch (§7c) |
| Reliable cross-node `pmxcfs` change notification for Corosync-replicated writes | **No mechanism at all** — no notify callback exists to propagate a remote-origin change into a local kernel notification (§7c) |

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
   events.** This is where §7 bites: no stock channel gives that witness
   **complete** coverage. Task history misses anything done via direct
   `pmxcfs` writes (T3, §7b) and is a bounded rolling window with no
   monotonic cursor (§7a); filesystem-level watching of `pmxcfs` has no
   confirmed completeness contract even locally and **no mechanism at all**
   for cluster-replicated writes from other nodes (§7c). A witness built
   from stock, unprivileged, or ordinarily-privileged observation cannot
   honestly claim (4).
5. **`trusted` persists only as long as the witness can durably/
   cryptographically prove uninterrupted coverage.** This is the right
   requirement in principle — but it presupposes (4) is achievable, and (4)
   is not, from stock capabilities alone. A witness that cannot observe
   everything cannot prove it observed everything, no matter how strict its
   own internal bookkeeping is.
6. **Any observation gap is fail-closed.** Sound requirement, and the
   correct default *if* a witness existed (§14) — but a fail-closed rule
   only bounds the damage of a *detected* gap. §7b's direct-`pmxcfs`-write
   gap and §7c's cross-node notification gap are not "gaps that get
   detected and fail closed" — they are **channels the witness never
   observes in the first place**, which is a different and worse failure
   mode: a silent, permanent blind spot, not a bounded, recoverable outage.
7. **Same-slot destroy/recreate must always change the epoch or remove
   authority, even with identical observable facts.** This is exactly
   right as a *requirement* — see §10's dedicated adversarial walkthrough.
   The hypothesis's answer to *why* B cannot inherit A's trust is
   structurally sound (it does not rely on "config looks different"); the
   problem is that the witness cannot be relied upon to have actually
   *seen* the destroy/create pair at all, given §7b/§7c.
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
    never a security boundary — but this requirement, applied honestly,
    also rules out treating `/cluster/tasks` polling as sufficient (§7a),
    which is the only stock channel a witness could otherwise fall back
    on.

**Conclusion of §8:** properties (1)–(3) and (6)–(11) describe a
*well-designed* witness, if one could exist. Properties (4)/(5) — the
actual load-bearing claim that a witness can observe identity-breaking
events with provable completeness — are not satisfiable from stock,
read-only, or ordinarily-privileged Proxmox VE capabilities, per §7. A
witness restricted to those capabilities has an inherent, silent blind spot
at exactly the T3 tier (§5) that ADR 0005's own witness (§9) is built
around: an actor able to destroy/recreate a guest can, by the same
privilege class, bypass the very channel meant to observe that
destroy/recreate.

## 9. Candidate family comparison

| Family | What it proves | Trust root | Copyable by clone? | Same-slot recreate? | Snapshot rollback? | Restore? | Migration? | Watcher/backend/node restart? | Offline interval? | Replay? | Privilege assumption | QEMU/LXC parity | Satisfies ADR 0005 §14 test? |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| **A. Trusted hostd lifecycle witness + external epoch** | Intended: gapless observation of identity-breaking events | A host-resident (or continuously-connected) witness process; requires T1–T3 defense from the witness itself, and still assumes T4 (node/hostd) is not compromised | Epoch value: no (external); but witness coverage itself has a blind spot (§7b/§7c) that is functionally equivalent to copyability | **Not distinguished from stock — witness cannot reliably observe it (§7b/§7c)** | Sound *if* the witness could observe it; same coverage gap applies | Same as rollback | Requires explicit handling (§15); not solved by the witness alone | Requires explicit fencing (§14); does not by itself close the coverage gap | Must fail closed (§14); correct in principle but does not fix (4)/(5) | Not inherently defended; depends on epoch uniqueness discipline (ADR 0005 §16-style), which is sound but does not fix coverage | Full-root/ordinarily-privileged observer assumed | Symmetric in principle (both QEMU/LXC configs live under `pmxcfs`) | **No** — fails §6/§8's coverage requirement |
| **B. PVE task/event/audit history as witness** | A record exists for operations that went through the high-level API/CLI path | Trust in Proxmox's own task subsystem; no additional host presence required beyond ordinary read access | No (event record itself isn't guest state) | **Not detected — direct `pmxcfs` writes create no task at all (§7b)** | Not detected unless rollback itself is API-invoked (usually is, but log is a rolling window, §7a) | Same as rollback | Not addressed | Bounded window means an old event can be silently rotated away (§7a) | A gap in polling this record is invisible; the record itself has no cursor to detect a gap (§7a) | Not addressed | Ordinary `VM.Audit`/`Sys.Audit`-class read access, not root | Symmetric | **No** — this is exactly ADR 0002's already-flagged Class B, still **UNKNOWN**/insufficient |
| **C. Hardware-rooted TPM / physical attestation** | Identity/integrity of the **physical host**, not of any specific guest incarnation | Physical TPM chip on one specific machine | N/A — a physical host property, not something guests carry | **Does not address this axis at all** — a hardware TPM attests the node, not which guest occupies a VMID slot | N/A | N/A | Breaks by construction: a hardware TPM cannot follow a guest across a live/offline migration to different physical hardware | N/A to resource continuity | N/A | N/A | Not applicable to resource continuity; **this is a node-attestation primitive, a different axis entirely (ADR 0001 node section)** | Would be identical for QEMU/LXC since it says nothing about either | **Not applicable** — solves a different problem (node trust), not Blocker B |
| **D. vTPM** | Guest-visible TPM state at read time | Software-emulated; backed by a `vtpm0` disk volume | **Yes — copied by clone/backup/snapshot identically to any other disk (already ADR 0005 §6 candidate 20)** | Fails identically to any disk-resident evidence | Fails (state travels with the snapshot) | Fails (state travels with the restore) | Travels with the guest, proves nothing about continuity | N/A | N/A | Fully replayable by anyone who can copy the disk | Root/API-level access to guest storage | QEMU only (no stock LXC vTPM) | **No** — already rejected in ADR 0005 |
| **E. Guest cryptographic agent + guest-resident key** | Key possession at read time | Private key material stored in guest disk/config state | **Yes — disk-resident, copied by clone/backup identically (ADR 0005 §13)** | Fails — new occupant can carry the copied key forward | Fails | Fails | N/A | N/A | N/A | Replayable by whoever can read the disk | Requires cooperative in-guest agent (QGA) or `pct exec`-class access; not default-on | Asymmetric (QGA is QEMU-only; LXC needs `pct exec`) | **No** — already evaluated and rejected in ADR 0005 §13 |
| **F. External/HSM-backed guest identity** | Key possession bound to an external HSM | Reduces to (E) unless the credential itself is bound to specific node hardware, in which case it reduces to (C) | Same as (E) or (C) depending on binding | Same failure as whichever it reduces to | Same | Same | Same | Same | Same | Same | Requires either a guest-side credential (→E) or node-bound hardware (→C) | Same asymmetry concerns as whichever it reduces to | **No** — does not introduce a new, independent property beyond (C)/(E) |
| **G. Operator per-mutation re-attestation / ephemeral trust** | Nothing persists; every mutation requires a **fresh**, explicit, human-confirmed identity check at the moment of use | The human operator, at the instant of the check; no persistent state to steal | N/A — there is no persistent trust artifact to copy | **Immune by construction** — there is no persistent `trusted` state for a recreated occupant to inherit, because nothing is ever granted ahead of the mutation decision | Immune, for the same reason | Immune, for the same reason | Immune, for the same reason | Immune — no state to lose across a restart | Immune — no window during which stale trust could be consumed | Each check is against exact current `resource_id`/`binding_id`/`locator_generation`/`resource_continuity_revision`, so replay of an old check fails CAS | No stronger than ADR 0001's existing exact-match CAS discipline, already accepted | Symmetric | **Does not answer the question asked** — it does not grant persistent `security_continuity=trusted`; it is a way to avoid ever needing to. Left as a candidate direction for a future mutation-design ADR, not a Blocker-B mechanism (§12, §24) |
| **H. Combinations of the above** | Higher empirical confidence, no new completeness guarantee | Whichever combination of the above is used | Combining (A)+(B)+(E), for example, still fails at the shared T3 blind spot (§7b) that all three rely on ordinary API/task-visible operations to detect | **Still not distinguished** — the shared blind spot is structural (direct `pmxcfs`/disk access), not statistical; adding more of the same class of evidence does not close a hole that is a *category* of access none of them observes | Still fails unless one member of the combination independently solves it (none does) | Same | Same | Same | Same | Same | Same | Same union of assumptions as the weakest member | Same | **No** — combining insufficient evidence classes does not manufacture sufficiency; useful only as an audit/anomaly-detection signal (mirrors ADR 0005 §9-10's demotion of the administrative marker to audit-only) |

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

If the answer reduces to "because polling/config looks different," the
mechanism **fails** — this is the exact test ADR 0005 §9 already applied to
Family C, and it applies identically here.

**For every family in §9, the honest answer:**

- **Family A (host lifecycle witness):** the *intended* answer is "because
  the witness observed the destroy event and the create event as two
  distinct lifecycle transitions, and revoked/regenerated the epoch between
  them — independent of whether B's config matches A's." This is the
  **right kind of answer** — it does not rely on config inspection. But
  §7b/§7c show the witness, built from stock/ordinarily-privileged
  observation, **cannot reliably have observed that transition at all** if
  the destroy/recreate was done via direct `pmxcfs` manipulation (available
  to any node-root actor, a T3-tier capability, not an exotic one). The
  honest answer becomes "because the witness observed it, **assuming it
  was watching everything, which it cannot prove**" — which collapses back
  into an unproven completeness assumption, i.e. still fails the test, just
  one layer deeper than Family C did.
- **Family B (task history):** the answer is "because a destroy task and a
  create task exist in the log for that slot" — but if the destroy/create
  went through direct `pmxcfs` writes, **no task exists at all**, so there
  is nothing to distinguish A from B; the mechanism silently reports
  "nothing observed," which — per §14's fail-closed requirement — should
  mean "authority-ineligible," but a naive implementation that treats
  "no adverse event logged" as "still trusted" would fail exactly as
  Family C failed. Fails the test.
- **Family C/D/E/F:** already shown to be disk/config-resident or
  node-bound, not slot-transition-observing at all; B trivially inherits
  whatever A had unless the mechanism separately fails closed for other
  reasons (ADR 0005 §9–§13). Fails the test for the identical reason ADR
  0005 already gives.
- **Family G (ephemeral re-attestation):** there is no persistent trust for
  B to inherit in the first place — the question is moot by construction,
  not answered. This is the one family that is not defeated by this test,
  precisely because it refuses to attempt the thing the test is checking.
- **Family H:** inherits the worst-case answer of its weakest member for
  the T3 blind spot, because that blind spot is a category of access
  (direct `pmxcfs`/disk write) that adding more *task/config*-based
  evidence does not observe.

**Controlling conclusion:** no family audited in this ADR — other than the
non-answer of Family G — passes this test using only stock, read-only, or
ordinarily-privileged Proxmox VE observation. This is the same shape of
failure ADR 0005 already found for Family C, now shown to extend to
lifecycle-*event* observation as well as guest-*state* observation.

## 11. Node/hostd trust root vs. resource continuity — explicit separation

The mission requires this separation be stated explicitly, not left
implicit. Any future mutation path already has its own, entirely separate
node/hostd trust gate (ADR 0001's `node_bindings`/`node_attestations`; ADR
0005 §18/§21). This ADR's findings do not, and must not, blur that boundary
in either direction:

- **A trusted node/hostd does not, by itself, grant resource continuity.**
  Even a perfectly honest, uncompromised PVE node running genuine,
  unmodified software still exposes `pmxcfs` as a directly-writable
  configuration store to anyone with sufficient PVE-side privilege (T2/T3,
  §5) — node honesty says nothing about whether a *specific slot's*
  occupant changed via a channel the witness observes.
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
  mechanism evaluated here, or conceivable from the same stock-capability
  class, defends against a compromised node/hostd trust root.** That
  defense, if it is ever required, belongs to the separate node/hostd
  attestation protocol ADR 0001 §"Nierozstrzygnięte kwestie" #6 already
  flags as future work — not to Blocker B.
- Any future mechanism that turns out to require a host-resident witness
  component must, at minimum, define its own node-migration/re-attestation
  semantics (mirroring ADR 0005 §21's requirement for any node-mediated
  evidence collection), and must never present "the node/hostd is trusted"
  as if it also meant "this specific resource's continuity is proven."

## 12. Selected mechanism: **NO-GO**

No family evaluated in §9, alone or combined (§9 row H), satisfies §6's
required security property or survives §10's same-slot witness test using
capabilities actually available from stock, read-only, or ordinarily
privileged Proxmox VE. Family G (operator per-mutation re-attestation)
avoids the failure by declining to grant persistent trust at all, which
answers a different, narrower question than the one Blocker B poses and is
noted as a possible complementary direction for a future mutation-design
ADR (§24), not as a Blocker-B mechanism.

**Conclusion:** this ADR does **not** select a mechanism for
`security_continuity: unverified -> trusted`. **Blocker B remains OPEN.**
This is a **NO-GO** for a practical mechanism achievable today, consistent
with ADR 0005's own honest negative conclusion for the weaker Family A/B/C
question, now shown to extend to the trusted-host-lifecycle-witness
hypothesis and five further candidate families as well.

## 13. What remains required of any future stronger mechanism (extends ADR 0005 §14)

ADR 0005 §14's minimum-property list applies unchanged. This research adds
the following, specific to lifecycle-observation-based mechanisms, as
mandatory additional properties any future ADR proposing such a mechanism
must satisfy:

- it must not treat Proxmox's own task history, `/cluster/tasks`, or any
  `pmxcfs` file-level observation as, by itself, a complete or gapless
  event channel — §7 shows none of them carries an official completeness,
  retention, or monotonic-ordering guarantee;
- it must explicitly close the direct-`pmxcfs`-write bypass (§7b) — i.e.,
  define how the mechanism detects, or is structurally immune to, an
  identity-breaking change made without going through any task-generating
  API/CLI path; "no task was observed" must default to
  authority-ineligible, never to "presumed unchanged";
- if it depends on any filesystem-level change notification, it must not
  assume `inotify`/`fanotify` reliably fires for `pmxcfs`, on any node,
  for any origin (local or cluster-replicated) — §7c shows no such
  guarantee exists, and the kernel primitive itself can silently no-op;
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

| Failure | Consequence under this ADR's NO-GO conclusion |
| --- | --- |
| Witness process crash/restart | N/A — no witness exists; would be §14's fail-closed default if one did |
| Direct `pmxcfs` config manipulation (T3) | The exact blind spot that defeats every witness/task-history family in §9 (§7b, §10) |
| Task-log rotation losing old entries | Removes the only evidence Family B could ever have offered for an old event (§7a) |
| `inotify`/`fanotify` silently not firing on `pmxcfs` | A filesystem-watch-based witness would falsely believe it has coverage it does not (§7c) |
| Compromised node/hostd (T4) | Out of scope for every family here; belongs to the separate node/hostd attestation gate (§11) |
| Attempting to reconstruct an unobserved interval optimistically | Explicitly forbidden as a future-mechanism default (§14) — would recreate this ADR's own negative finding |

## 20. Adversarial matrix

Extends ADR 0005 §28's format to the families audited in this ADR. No row
produces `security_continuity=trusted`, because this ADR selects no
mechanism; the matrix instead records what each family's evidence would
have shown, had one been implemented, to make the negative finding
falsifiable rather than asserted.

| # | Scenario | Family A (witness) | Family B (task history) | Family G (ephemeral) |
| --- | --- | --- | --- | --- |
| 1 | Ordinary destroy+recreate via API/CLI, identical config | Witness *would* see both tasks — but nothing here proves it was watching continuously; **unverified**, not trusted | Task log shows destroy+create pair, if not yet rotated away; still no cursor proving nothing else happened; **unverified** | No persistent state exists to be wrong about; each future mutation re-checks exact current identity |
| 2 | Destroy+recreate via direct `pmxcfs` write (T3) | **Silent blind spot** — no event observed at all (§7b) | **Silent blind spot** — no task exists (§7b) | Unaffected — there is nothing to silently inherit |
| 3 | Clone to a new VMID | New locator, new `resource_id` regardless of family (ADR 0001) | Same | Same |
| 4 | Snapshot rollback | Must revoke per §15/ADR 0005 §17 if a mechanism ever exists; this ADR grants nothing | Same | Same — re-checked at next mutation attempt regardless |
| 5 | Node migration | Requires explicit handling (§15); not solved by witness presence alone | N/A — task history is not node-bound | Unaffected |
| 6 | Witness/backend/node restart | Must fail closed (§14); this ADR implements no witness | N/A | Unaffected — no persistent coverage claim exists to lose |
| 7 | Source-attestation epoch bump | Any prior-epoch evidence becomes authority-ineligible (§16, ADR 0003) | Same | Same, if evidence were ever collected at check time |
| 8 | Compromised node/hostd (T4) | Out of scope; assumed away (§11) | Same | Same |

## 21. B1 authorization boundary

Explicit, per the mission:

```text
WAVE B1 remains DEFERRED / NOT AUTHORIZED.

This ADR's own conclusion is NO-GO. Independent review and separate
acceptance of THIS ADR would mean the negative research conclusion is
accepted as the current record -- it would NOT authorize WAVE B1, because
this ADR proposes no sufficient mechanism for WAVE B1 to implement.

WAVE B1 may only begin after a DIFFERENT, later, separately reviewed and
separately ACCEPTED ADR proposes an actual mechanism satisfying ADR 0005
§14 and this ADR's §13 extensions, and passes ADR 0005 §14's controlling
test and this ADR's §10 same-slot witness test.
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
   mechanism remains undesigned — this ADR narrows *why* the
   lifecycle-witness variant of that idea fails under stock capabilities,
   but does not propose a replacement design.
2. Whether a genuinely hardware-rooted, non-clonable, per-guest (not
   per-node) attestation primitive could ever exist for QEMU/LXC on
   commodity virtualization hardware is unresolved; stock Proxmox VE
   provides no such primitive today (§9 rows C/D).
3. Whether Family G (operator per-mutation re-attestation / ephemeral
   trust) should be pursued as a deliberate design choice for a future
   mutation-authority ADR — i.e., choosing to never grant persistent
   `trusted` at all, and instead requiring a fresh, explicit,
   CAS-validated operator confirmation immediately before every
   destructive mutation — is not decided here and is left to that future
   ADR, which is free to accept or reject it on its own merits.
4. The exact node-to-syscall dispatch behavior when a PVE API command
   targets a guest whose owning node differs from the node the request was
   issued against (relevant to whether a hypothetical local-`pmxcfs`-watch
   witness could ever be co-located correctly) is **UNKNOWN** at
   FACT-DOC/FACT-SOURCE strength this session and would need independent
   verification before any future ADR relies on it.
5. The precise current PVE 9.x task-log archive rotation parameters (file
   count, per-file size) are corroborated only at forum strength this
   session, not re-derived from source; the rotation-on-fixed-size
   *behavior* itself is FACT-SOURCE (§7a), but the exact numbers should be
   re-verified against the exact deployed PVE release before any future
   ADR cites them as a specific bound.
6. Whether Proxmox will ever ship a documented, monotonic, gapless,
   officially-retained cluster-wide event stream (closing ADR 0002's own
   Class B gap) remains unknown and outside Hubinet Ops's control; this ADR
   does not assume it will.

## 25. Acceptance checklist

For a future independent review of *this* ADR (not of a trust-granting
mechanism, since none is proposed):

1. Does this ADR select or authorize any mechanism sufficient for
   `security_continuity=trusted`? **No** — confirm this remains true
   before accepting.
2. Does this ADR authorize WAVE B1? **No** (§21) — confirm this remains
   true before accepting.
3. Does this ADR weaken ADR 0001's invisible same-slot destroy/recreate
   limitation, ADR 0003's epoch authority-eligibility rule, or ADR 0005's
   Family A/B/C rejection? **No** — confirm no drift was introduced.
4. Does this ADR's §7 research (task history, `pmxcfs`/FUSE notification)
   accurately reflect the cited sources, and are FACT-DOC/FACT-SOURCE/
   INFERENCE/UNKNOWN tags used honestly, without overclaiming a guarantee
   Proxmox does not document? Verify before accepting.
5. Does §10's same-slot witness test correctly show why each family in §9
   fails, without any answer reducing to "because config looked
   different" or "because no adverse event was logged" being accepted as
   sufficient? Verify before accepting.
6. Does §11 correctly keep node/hostd trust and resource continuity as
   two separate, unmerged axes? Verify before accepting.
7. Does this ADR authorize any schema, runtime, hostd, HA control, or
   mutation implementation? **No** — confirm before accepting.
8. Is Blocker B left explicitly OPEN, R0 explicitly unaffected, and Phase
   1C explicitly BLOCKED? Verify before accepting.

Acceptance of this ADR, if it occurs, records the research conclusion
(NO-GO for the families audited here) as the current architecture record.
It does not, by itself, authorize any further implementation.

## Sources / Evidence

Read this session, in addition to the ADR 0001/0002/0003/0005 sources they
build on:

- [`proxmox/pve-manager`, `PVE/API2/Cluster.pm`](https://github.com/proxmox/pve-manager/blob/master/PVE/API2/Cluster.pm) — `/cluster/tasks` route, `get_tasklist()` usage, no cursor/sequence field (§7a)
- [`proxmox/pve-common`, `PVE/RESTEnvironment.pm`](https://github.com/proxmox/pve-common) — UPID encoding, task log path (`/var/log/pve/tasks/`), fixed-size archive-index rotation (§7a)
- [`proxmox/pve-cluster`, `src/pmxcfs/pmxcfs.c`](https://github.com/proxmox/pve-cluster/blob/master/src/pmxcfs/pmxcfs.c) — FUSE `fuse_operations` table; absence of any kernel-notification callback (§7c)
- [Proxmox Cluster File System (pmxcfs) documentation](https://pve.proxmox.com/pve-docs/chapter-pmxcfs.html) — `pmxcfs` architecture, `/etc/pve` mount, Corosync-backed replication (§7c)
- Linux kernel mailing list, "[RFC PATCH 0/7] Inotify support in FUSE and virtiofs"](https://lkml.kernel.org/linux-fsdevel/YYMNPqVnOWD3gNsw@redhat.com/t/) — RFC-stage status of FUSE `inotify` support (§7c)
- Linux kernel mailing list, "inotify: disallow watches on unsupported filesystems" (early-2025 discussion) — `inotify_add_watch()` silently succeeding without delivering events on unsupported filesystems (§7c)
- Community-reported (forum-strength, not independently re-derived from source this session) task-log archive rotation figures, cited only as corroborating context, not as the load-bearing claim (§7a, §24 item 5)

Repositories on GitHub are official read-only mirrors; the authoritative
upstream remains [git.proxmox.com](https://git.proxmox.com/). Conclusions
about what a given behavior does *not* guarantee are architectural
inferences from the cited contract and source, not a claim of an
additional Proxmox guarantee.
