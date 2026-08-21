# ADR 0006: stronger workload-continuity proof — trusted host lifecycle witness research

Status: **ACCEPTED**

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

**This ADR has completed independent review and is ACCEPTED**, following
two targeted corrective passes that resolved every P1/P2 finding raised
against the original draft; the final independent review verdict was
**PASS — no P1/P2 findings**. Acceptance records, as the current
architecture record, the explicit **NO-GO, narrowly scoped to the families
this ADR actually primary-source audited** (§9: a lifecycle witness built on
Proxmox's own task/event history and/or `pmxcfs` filesystem-level
observation, plus five further families already resolvable on separate,
independently-established grounds). **It is not a claim that every
conceivable host-rooted lifecycle witness is impossible, and acceptance
does not change that.** In particular, a direct `pmxcfs` config write
proves that *this ADR's audited observation channels* (task history,
filesystem-change notification) can be bypassed; it does not, by itself,
prove that a genuinely different class of host-side enforcement — kernel
audit-subsystem/`auditd` rules, LSM hooks, `eBPF`-based syscall interception,
storage-layer block-change tracking, or a witness backed by an explicitly
root-resistant/external trust anchor — could never observe an actual
physical/logical workload substitution. Those broader classes were **not**
audited here and remain **unresolved**, not disproven (§24 item 7).
Acceptance confirms this ADR's own scoped conclusions are architecturally
sound; it does not broaden them. Consistent with the mission that produced
it, and unaffected by acceptance:

- this ADR does not, by itself, close Blocker B for mutation authority —
  Blocker B remains **OPEN**, exactly as ADR 0005 left it;
- this ADR does not, by itself, authorize WAVE B1 — WAVE B1 remains
  **DEFERRED / NOT AUTHORIZED**;
- independent review and acceptance of *this* ADR means the research
  conclusion below is accepted as the current record — because that
  conclusion is negative, acceptance does **not** authorize WAVE B1 or
  grant `trusted` to anything; only a **different**, later ADR that
  actually proposes a sufficient mechanism could do that;
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

This audit is bounded to two concrete, primary-source-verifiable observation
channels: Proxmox's own task/event history (`/cluster/tasks`, per-node task
logs), and `pmxcfs` filesystem-level change observation (§7). It does not
extend to, and does not reach a conclusion about, fundamentally different
host-rooted enforcement classes — the Linux kernel audit subsystem/`auditd`,
LSM hooks, `eBPF`-based syscall interception, storage-layer block-change
tracking, or a witness deliberately backed by an explicitly root-resistant
or fully external trust anchor. Those remain genuinely **unresolved** after
this ADR, not proven impossible (§24 item 7).

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
They are applied here only against the specific observation channels this
ADR primary-source audited (§7): Proxmox's own task/event history and
`pmxcfs` filesystem-level change observation. A witness built on a
genuinely different channel — kernel audit/LSM/`eBPF`-based enforcement, or
one backed by an explicit root-resistant/external trust anchor (§5) — was
not audited here and is not covered by this ADR's NO-GO (§24 item 7).

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
  equivalent in effect to T4, not a lesser, bounded gap (§11).

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
- **INFERENCE, consequence for cluster-replicated changes (bounded as
  above).** Given the above, a witness's `inotify`/`fanotify` watch on one
  node's `/etc/pve` mount has **no evidence-backed mechanism, found this
  session,** by which it would fire for a change whose *origin* was another
  node. A witness would therefore need to be co-located with wherever the
  actual identity-breaking change is applied for a given guest — potentially
  every node the guest could ever occupy — which is itself **UNKNOWN** at
  FACT-DOC strength this session (which node actually executes a given
  filesystem write when a command is issued against a different node's API
  endpoint; the general PVE proxy-to-owning-node pattern for API-mediated
  operations is well-established, but this ADR does not re-verify it as a
  citation-grade fact, and it says nothing about where a *direct* `pmxcfs`
  write, §7b, is actually applied).
- **FACT-SOURCE, general kernel limitation, independent of `pmxcfs`, pinned
  to when it was checked.** Linux `inotify`/`fanotify` are documented as
  supported for local kernel filesystems; FUSE-backed filesystems have
  historically lacked reliable kernel-level change-notification delivery.
  As of the most recent upstream activity located this session — a Linux
  kernel mailing list thread on disallowing `inotify` watches on
  unsupported filesystems, with discussion as recent as May 2025 confirming
  FUSE/`virtiofs` `inotify` support was still **not merged into the
  mainline kernel** at that date — `inotify_add_watch()` can silently
  succeed without error on a filesystem that does not actually deliver
  events, and kernel-level `inotify` support for FUSE itself remains, at
  best, an RFC-stage patch series (originally posted ~October 2021,
  targeting `virtiofs`). **This is a time-bound finding, current as of the
  mid-2025 activity located this session (this research was performed
  August 2026) — not a permanent architectural fact.** A later mainline
  kernel release could merge this support; any future ADR relying on this
  finding must re-verify the current kernel/FUSE state at implementation
  time, not cite this ADR's date as still current.
- **Conclusion of §7c, precisely scoped.** A `pmxcfs`-filesystem-watch
  approach has **no documented completeness guarantee**, even for changes
  originated on the same node the watcher runs on, from the general
  FUSE/`inotify` limitation above (time-bound as stated); and, **within the
  five core files checked this session**, no mechanism was found for
  changes replicated in from other nodes. This is a **bounded, sourced
  negative finding for the specific files and kernel status checked**, not
  an exhaustive proof that no code path anywhere in `pmxcfs`, or any future
  kernel, ever could deliver such a notification (§24 item 8).

### 7d. Summary table

| Property required for a witness | Stock PVE support |
| --- | --- |
| Monotonic, gapless task/event cursor | **No** — UPID is not a sequence; retention is a rolling window (§7a) |
| Officially guaranteed task-history retention | **No** — fixed-size rotation, bounded window, no documented permanence (§7a) |
| Task creation for *every* identity-breaking event | **No** — only for the high-level API/CLI path; direct `pmxcfs` write bypasses it entirely (§7b) — a T3-tier gap |
| Reliable local-node `pmxcfs` change notification (`inotify`/`fanotify`) | **No documented guarantee, as of mid-2025 kernel status checked** — the five core `pmxcfs` files checked implement no kernel notify callback; FUSE-level `inotify` support was still RFC-stage at that date, and the kernel can silently no-op the watch (§7c) |
| Reliable cross-node `pmxcfs` change notification for Corosync-replicated writes | **No mechanism found in the five core files checked** — not verified as an exhaustive whole-repository absence (§7c, §24 item 8) |

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
   events.** This is where §7 bites: no *audited* channel (task history,
   `pmxcfs` filesystem-change observation) gives that witness **complete**
   coverage. Task history misses a combined config+disk occupant
   replacement done via direct `pmxcfs`/storage writes (T3, §7b) and is a
   bounded rolling window with no monotonic cursor (§7a); filesystem-level
   watching of `pmxcfs` has no confirmed completeness contract even locally
   and, in the five core files checked, no mechanism was found for
   cluster-replicated writes from other nodes (§7c — a bounded finding, not
   an exhaustive one). A witness built *only* from these two audited,
   ordinarily-privileged observation channels cannot honestly claim (4).
   Per §5/§11: because this hypothesis, as stated, specifies no explicit
   root-resistant/external trust anchor, its failure here is not a bounded
   T3-tier gap distinct from T4 — for this specific, anchor-less witness,
   T3 collapses into T4 (out of scope), not a partially-defensible tier.
5. **`trusted` persists only as long as the witness can durably/
   cryptographically prove uninterrupted coverage.** This is the right
   requirement in principle — but it presupposes (4) is achievable, and (4)
   is not, from the two audited channels alone. A witness that cannot observe
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
events with provable completeness — are not satisfiable from the two
audited channels (task history, `pmxcfs` filesystem-change observation),
per §7. A witness restricted to *those specific channels*, and lacking an
explicit root-resistant/external trust anchor (§5), has an inherent, silent
blind spot precisely where T3 collapses into T4 for it (§5, §11): an actor
able to replace an occupant's disk/process content can, by that same
node-root privilege class, bypass both audited channels at once. This
finding is scoped to the audited channels; it says nothing about whether a
fundamentally different observation channel (§1, §24 item 7) could close
this gap.

## 9. Candidate family comparison

Families A and B, and their combination (row H), are evaluated only against
the two channels this ADR primary-source audited (§7); the "No" verdicts
for them are scoped to those channels, not to every conceivable host-rooted
witness (§1, §6, §24 item 7). Families C–F are evaluated on separate,
already-established grounds (clone-copyability of disk-resident state, or a
node-vs-resource axis mismatch) independent of the §7 audit, so their
verdicts are not narrowed by that scoping. Family G is corrected in this
revision (P2) — see the row below and §24 item 3 for the required
disclaimers.

| Family | What it proves | Trust root | Copyable by clone? | Same-slot recreate? | Snapshot rollback? | Restore? | Migration? | Watcher/backend/node restart? | Offline interval? | Replay? | Privilege assumption | QEMU/LXC parity | Satisfies ADR 0005 §14 test? |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| **A. Trusted hostd lifecycle witness + external epoch** | Intended: gapless observation of identity-breaking events | A host-resident (or continuously-connected) witness process; defends T1–T2 at most. As hypothesized, it specifies no explicit root-resistant/external trust anchor (§5), so T3 collapses into T4 for it — it is not a partially-defensible tier, and this ADR does not claim otherwise (§11) | Epoch value: no (external); but witness coverage itself has a blind spot (§7b/§7c) that is functionally equivalent to copyability | **Not distinguished from stock — witness cannot reliably observe it (§7b/§7c)** | Sound *if* the witness could observe it; same coverage gap applies | Same as rollback | Requires explicit handling (§15); not solved by the witness alone | Requires explicit fencing (§14); does not by itself close the coverage gap | Must fail closed (§14); correct in principle but does not fix (4)/(5) | Not inherently defended; depends on epoch uniqueness discipline (ADR 0005 §16-style), which is sound but does not fix coverage | Full-root/ordinarily-privileged observer assumed | Symmetric in principle (both QEMU/LXC configs live under `pmxcfs`) | **No** — fails §6/§8's coverage requirement |
| **B. PVE task/event/audit history as witness** | A record exists for operations that went through the high-level API/CLI path | Trust in Proxmox's own task subsystem; no additional host presence required beyond ordinary read access | No (event record itself isn't guest state) | **Not detected — direct `pmxcfs` writes create no task at all (§7b)** | Not detected unless rollback itself is API-invoked (usually is, but log is a rolling window, §7a) | Same as rollback | Not addressed | Bounded window means an old event can be silently rotated away (§7a) | A gap in polling this record is invisible; the record itself has no cursor to detect a gap (§7a) | Not addressed | Ordinary `VM.Audit`/`Sys.Audit`-class read access, not root | Symmetric | **No** — this is exactly ADR 0002's already-flagged Class B, still **UNKNOWN**/insufficient |
| **C. Hardware-rooted TPM / physical attestation** | Identity/integrity of the **physical host**, not of any specific guest incarnation | Physical TPM chip on one specific machine | N/A — a physical host property, not something guests carry | **Does not address this axis at all** — a hardware TPM attests the node, not which guest occupies a VMID slot | N/A | N/A | Breaks by construction: a hardware TPM cannot follow a guest across a live/offline migration to different physical hardware | N/A to resource continuity | N/A | N/A | Not applicable to resource continuity; **this is a node-attestation primitive, a different axis entirely (ADR 0001 node section)** | Would be identical for QEMU/LXC since it says nothing about either | **Not applicable** — solves a different problem (node trust), not Blocker B |
| **D. vTPM** | Guest-visible TPM state at read time | Software-emulated; backed by a `vtpm0` disk volume | **Yes — copied by clone/backup/snapshot identically to any other disk (already ADR 0005 §6 candidate 20)** | Fails identically to any disk-resident evidence | Fails (state travels with the snapshot) | Fails (state travels with the restore) | Travels with the guest, proves nothing about continuity | N/A | N/A | Fully replayable by anyone who can copy the disk | Root/API-level access to guest storage | QEMU only (no stock LXC vTPM) | **No** — already rejected in ADR 0005 |
| **E. Guest cryptographic agent + guest-resident key** | Key possession at read time | Private key material stored in guest disk/config state | **Yes — disk-resident, copied by clone/backup identically (ADR 0005 §13)** | Fails — new occupant can carry the copied key forward | Fails | Fails | N/A | N/A | N/A | Replayable by whoever can read the disk | Requires cooperative in-guest agent (QGA) or `pct exec`-class access; not default-on | Asymmetric (QGA is QEMU-only; LXC needs `pct exec`) | **No** — already evaluated and rejected in ADR 0005 §13 |
| **F. External/HSM-backed guest identity** — **narrowed this revision (P2 correction: the earlier row incorrectly collapsed the entire family into (E) or (C), excluding the genuinely externally-rooted/out-of-band class ADR 0005/0006 leave open; corrected below)** | **Narrow variant audited here: a guest-resident credential whose signing authority is an external HSM, but the guest itself still presents that credential at use time.** Proves key possession at read time, same as Family E, because the artifact actually presented/copyable still lives in guest-readable state | Narrow variant: reduces to (E) — an external signer does not change that the guest-side artifact is what a clone/restore copies | Narrow variant: **yes, same as (E)** — copied identically to Family E's own limitation | Narrow variant fails identically to (E) | Same as (E) | Same as (E) | N/A | N/A | N/A | Replayable identically to (E) | Requires cooperative in-guest presentation, same as (E) | Same asymmetry as (E) | **Narrow variant: No** — reduces to Family E, already rejected on those grounds. **The broader externally-rooted/out-of-band per-workload identity class — where a specific workload's identity is tracked/attested by an external system through a channel that is neither guest-resident nor a node-bound hardware property — is UNRESOLVED / NOT AUDITED HERE (§24 item 7). This ADR does not claim that broader class satisfies Blocker B, and does not claim it fails; it was not researched to either conclusion this pass.** |
| **G. Operator per-mutation re-attestation / ephemeral trust** | Nothing persists as `trusted`; every mutation instead requires its own fresh, explicit, human-confirmed identity check — this **sidesteps rather than answers** the persistent-`trusted` question this ADR audits (§24 item 3) | The human operator, at the instant of the check, **plus** a safe point-in-time target-identity proof binding that confirmation to the resource actually mutated — not yet defined by this family (§24 item 3) | N/A — no persistent trust artifact exists to copy | **Not immune, and not answered by this family** — there is no *persisted* `trusted` state for a recreated occupant to inherit, but a confirmation made against occupant A is exactly as vulnerable to a same-slot substitution as any other mechanism if the confirmation is not safely fenced against a race between the human check and backend execution (§24 item 3) | No persisted state to invalidate, but the underlying rollback-substitution risk is unaddressed by this family, not solved by it | Same as rollback | Same as rollback | No persisted coverage to lose across a restart — narrower claim than "immune" | No window during which *stale persisted* trust could be consumed — does not mean the underlying occupant-substitution question is solved | ADR 0001's exact-match CAS on `resource_id`/`binding_id`/`locator_generation`/`resource_continuity_revision` prevents replay of a **stale backend decision** — it does **not**, by itself, prove the physical/logical occupant was not substituted between confirmation and execution, since ADR 0001 explicitly permits those same tokens to remain unchanged across an observationally invisible same-slot delete/recreate (ADR 0001 row 10) | Symmetric | **Does not satisfy Blocker B by itself** — operator confirmation alone is not continuity proof (§24 item 3); adopting a mutation model that never requires persistent `security_continuity=trusted` would itself require a separate architecture change to ADR 0001/0005's accepted mutation-precondition formula, not something this ADR or a Family-G choice can authorize |
| **H. Combinations of the above** — **corrected this revision (P2): does not claim a combination automatically "inherits the weakest member"; the actual rule is narrower (below)** | Higher empirical confidence, no new independent security property, **for combinations drawn only from Families A/B/C/D/E and F's narrow variant** | Whichever combination of those insufficient families is used | Combining (A)+(B)+(E), for example, still fails at the shared T3 blind spot (§7b) that all three rely on ordinary API/task-visible operations to detect | **Still not distinguished, for combinations of only-insufficient families** — the shared blind spot is structural (direct `pmxcfs`/disk access), not statistical; adding more of the same class of evidence, none of which independently introduces a new security property, does not close a hole that is a *category* of access none of them observes | Still fails unless one member of the combination independently solves it (none of A–E/F-narrow does) | Same | Same | Same | Same | Same | Same | Depends on which families are combined — not a fixed "weakest member" rule; see below | Same | **No, for combinations drawn only from Families A/B/C/D/E/F-narrow** — combining only insufficient evidence classes that introduce no new independent security property does not manufacture sufficiency; useful only as an audit/anomaly-detection signal (mirrors ADR 0005 §9-10's demotion of the administrative marker to audit-only). **A combination that includes a future, independently sufficient externally-rooted proof (e.g. Family F's broader unresolved class, §24 item 7) would instead be judged entirely by that proof's own contract, not by this row** — this table does not evaluate, and does not pre-judge, any such future component. |

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

- **Family A (host lifecycle witness, limited to the audited channels):**
  the *intended* answer is "because the witness observed the destroy event
  and the create event as two distinct lifecycle transitions, and
  revoked/regenerated the epoch between them — independent of whether B's
  config matches A's." This is the **right kind of answer** — it does not
  rely on config inspection. But §7b/§7c show a witness built *only* from
  task history and/or `pmxcfs` filesystem-change observation **cannot
  reliably have observed that transition at all** if the occupant
  replacement was done via a combined direct `pmxcfs`+storage write (§4
  concepts 2+3; available to any node-root actor, a T3-tier capability, not
  an exotic one) — and, absent an explicit root-resistant/external trust
  anchor, that T3-tier bypass is equivalent to T4 for this witness, not a
  bounded gap (§5, §11). The honest answer becomes "because the witness
  observed it, **assuming it was watching everything, which it cannot
  prove**" — which collapses back into an unproven completeness
  assumption, i.e. still fails the test for these two audited channels,
  just one layer deeper than Family C did. A witness built on a
  fundamentally different, unaudited channel (§1, §24 item 7) is not shown
  to fail this test — it is simply not evaluated here.
- **Family B (task history):** the answer is "because a destroy task and a
  create task exist in the log for that slot" — but if the destroy/create
  went through direct `pmxcfs` writes, **no task exists at all**, so there
  is nothing to distinguish A from B; the mechanism silently reports
  "nothing observed," which — per §14's fail-closed requirement — should
  mean "authority-ineligible," but a naive implementation that treats
  "no adverse event logged" as "still trusted" would fail exactly as
  Family C failed. Fails the test.
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
- **Family H (combinations of only-insufficient families):** combining
  evidence classes that each introduce no new independent security property
  does not manufacture one — the shared T3 blind spot is a *category* of
  access (direct `pmxcfs`+storage write) that adding more *task/config*-based
  evidence does not observe, regardless of how many such families are
  combined. This is not a claim that a combination always inherits "the
  weakest member's" answer as a general rule: a combination that includes a
  future, independently sufficient externally-rooted proof would instead be
  judged by that proof's own contract against this same test, not by this
  bullet (§9, §24 item 7).

**Controlling conclusion, narrowly scoped:** no family built on the two
audited channels (task history, `pmxcfs` filesystem-change observation) —
Families A, B, and their combination in H — passes this test. Family G
sidesteps it rather than passing it (above). This is the same shape of
failure ADR 0005 already found for Family C, now shown to extend to
lifecycle-*event* observation on these two specific channels as well as
guest-*state* observation. It is **not** a claim that every conceivable
host-rooted witness fails this test — a fundamentally different observation
channel (§1, §6, §24 item 7) was not audited here and remains unresolved.

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

## 12. Selected mechanism: **NO-GO, narrowly scoped to the audited families**

This ADR's NO-GO has two independently-grounded parts, and neither should
be read as broader than its own evidence:

- **Families A and B (task-history and/or `pmxcfs`-filesystem-observation
  witnesses), and their combination in row H,** fail §6's required security
  property and §10's same-slot witness test **as applied against the two
  channels this ADR primary-source audited** (§7). This is **not** a claim
  that every conceivable host-rooted lifecycle witness is impossible — a
  witness built on a fundamentally different channel (kernel audit/LSM/
  `eBPF`-based enforcement, or one backed by an explicit root-resistant/
  external trust anchor, §5) was not audited here and remains **unresolved**
  (§24 item 7), not disproven.
- **Family C, D, E, and Family F's narrow guest-held-key variant** fail for
  reasons independent of the §7 audit — disk-resident state that
  clone/backup/restore copy identically (D, E, F-narrow), or a
  node-vs-resource axis mismatch that no amount of host observation
  changes (C) — and this part of the conclusion is not narrowed by the
  scoping above; it rests on the same grounds ADR 0005 already established
  for equivalent candidates. **Family F's broader externally-rooted/
  out-of-band per-workload identity class is UNRESOLVED / NOT AUDITED
  HERE** — this ADR does not claim it satisfies Blocker B, and does not
  claim it fails (§9, §24 item 7).
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

**Conclusion:** this ADR does **not** select a mechanism for
`security_continuity: unverified -> trusted`. **Blocker B remains OPEN.**
This is a **NO-GO for the stock-PVE task/event/`pmxcfs`-observation families
this ADR actually audited** (Families A, B, H), consistent with ADR 0005's
own honest negative conclusion for the weaker Family A/B/C question, now
shown to extend to lifecycle-*event* observation on these two specific
channels as well. It is **not** a broader claim that no practical host-side
lifecycle witness of any kind could ever exist — genuinely different
host-rooted mechanisms remain unresolved, not disproven (§24 item 7), and
Blocker B's resolution therefore still depends on future research this ADR
does not foreclose.

## 13. What remains required of any future stronger mechanism (extends ADR 0005 §14)

ADR 0005 §14's minimum-property list applies unchanged. This section applies
specifically to any future mechanism that relies on Proxmox's own task/event
history and/or `pmxcfs` filesystem-level observation as its lifecycle-
observation channel — the specific class this ADR audited. It does not, by
itself, apply to a fundamentally different host-rooted mechanism (e.g.
kernel audit/LSM/`eBPF`-based enforcement, or a genuinely root-resistant
external trust anchor, §5/§24 item 7) — such a mechanism would need its own
primary-source audit against §6/§10's tests, not merely inherit this
section's task/`pmxcfs`-specific findings. This research adds the
following, specific to lifecycle-observation-based mechanisms of the
audited class, as mandatory additional properties any future ADR proposing
one must satisfy:

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
| Combined direct `pmxcfs` config + storage manipulation (T3), no explicit anchor | The exact blind spot that defeats every audited witness/task-history family in §9 for the two audited channels (§7b, §10); per §11a, equivalent in effect to T4 for an anchor-less witness, not a bounded gap |
| Task-log rotation losing old entries | Removes the only evidence Family B could ever have offered for an old event (§7a) |
| `inotify`/`fanotify` silently not firing on `pmxcfs` | A filesystem-watch-based witness would falsely believe it has coverage it does not (§7c; time-bound to the kernel status checked, §24 item 8) |
| Compromised node/hostd (T4) | Out of scope for every family here; belongs to the separate node/hostd attestation gate (§11, §11a) |
| Attempting to reconstruct an unobserved interval optimistically | Explicitly forbidden as a future-mechanism default (§14) — would recreate this ADR's own negative finding |

## 20. Adversarial matrix

Extends ADR 0005 §28's format to the families audited in this ADR. No row
produces `security_continuity=trusted`, because this ADR selects no
mechanism; the matrix instead records what each family's evidence would
have shown, had one been implemented, to make the negative finding
falsifiable rather than asserted.

| # | Scenario | Family A (witness) | Family B (task history) | Family G (ephemeral) |
| --- | --- | --- | --- | --- |
| 1 | Ordinary destroy+recreate via API/CLI, identical config | Witness *would* see both tasks — but nothing here proves it was watching continuously; **unverified**, not trusted | Task log shows destroy+create pair, if not yet rotated away; still no cursor proving nothing else happened; **unverified** | No *persisted* state exists to be stale; each future mutation still needs its own safe point-in-time target proof (§24 item 3) — not "unaffected" in any stronger sense |
| 2 | Occupant replacement via combined direct `pmxcfs`+storage write (T3), no explicit anchor | **Silent blind spot** — no event observed at all (§7b); per §11a this is equivalent to row 8, not a lesser gap | **Silent blind spot** — no task exists (§7b) | No *persisted* trust to silently inherit — does not mean the underlying substitution is detected or prevented (§24 item 3) |
| 3 | Clone to a new VMID | New locator, new `resource_id` regardless of family (ADR 0001) | Same | Same |
| 4 | Snapshot rollback | Must revoke per §15/ADR 0005 §17 if a mechanism ever exists; this ADR grants nothing | Same | No persisted state to revoke — the rollback-substitution risk itself is unaddressed by this family (§24 item 3) |
| 5 | Node migration | Requires explicit handling (§15); not solved by witness presence alone | N/A — task history is not node-bound | Unaffected — no persisted trust to carry across a migration |
| 6 | Witness/backend/node restart | Must fail closed (§14); this ADR implements no witness | N/A | Unaffected — no persisted coverage claim exists to lose |
| 7 | Source-attestation epoch bump | Any prior-epoch evidence becomes authority-ineligible (§16, ADR 0003) | Same | Same, if evidence were ever collected at check time |
| 8 | Compromised node/hostd (T4) | Out of scope; assumed away (§11, §11a) — row 2, for an anchor-less witness, is this same category of failure, not a distinct lesser one | Same | Same |

## 21. B1 authorization boundary

Explicit, per the mission:

```text
WAVE B1 remains DEFERRED / NOT AUTHORIZED.

This ADR's own conclusion is NO-GO. Independent review and separate
acceptance of THIS ADR would mean the negative research conclusion is
accepted as the current record -- it would NOT authorize WAVE B1, because
this ADR proposes no sufficient mechanism for WAVE B1 to implement.

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
   mechanism remains undesigned — this ADR narrows *why* the
   lifecycle-witness variant of that idea fails under stock capabilities,
   but does not propose a replacement design.
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
7. **Broader host-rooted witness classes — genuinely unresolved, not
   disproven (P1 correction).** This ADR audited exactly two observation
   channels: Proxmox's own task/event history, and `pmxcfs` filesystem-level
   change observation. It did **not** primary-source audit, and reaches no
   conclusion about: the Linux kernel audit subsystem/`auditd` rules
   watching specific syscalls against `pmxcfs`/guest storage paths; LSM
   (SELinux/AppArmor-class) hooks; `eBPF`-based syscall/tracepoint
   interception; storage-layer block-change tracking (e.g. ZFS/LVM
   snapshot-diffing) as an independent occupant-substitution witness; a
   witness deliberately backed by an explicit root-resistant/external trust
   anchor (§5/§11a) rather than ordinary co-resident-process trust; or
   Family F's broader externally-rooted/out-of-band per-workload identity
   class (§9) — a specific workload's identity tracked/attested by an
   external system through a channel that is neither guest-resident nor
   node-bound hardware. Any of these could, in principle, observe an actual
   physical/logical occupant
   replacement (§4 concept 3) through a channel a node-root actor cannot as
   easily bypass as the two audited here — or could fail for reasons this
   ADR has not examined. A future ADR auditing one of these must perform
   its own primary-source research and its own pass against §6's required
   property and §10's same-slot witness test; it may not simply inherit
   this ADR's NO-GO, which does not extend to them.
8. **`pmxcfs` notification absence — bounded, not exhaustive (P2
   correction).** §7c's finding that no notification/invalidation call
   exists is confirmed across five core `src/pmxcfs/` files (`pmxcfs.c`,
   `server.c`, `dfsm.c`, `memdb.c`, `cfs-plug-memdb.c`), not the entire
   repository — `cfs-plug.c`, `cfs-plug-link.c`, `cfs-plug-func.c`,
   `cfs-utils.c`, `database.c`, `status.c`, `loop.c`, and `dcdb.c` remain
   unchecked at this citation granularity. A future ADR relying on §7c
   should independently re-check these remaining files (and the exact
   deployed PVE/kernel release's FUSE `inotify` support status, which is
   time-bound to the mid-2025 upstream activity located this session — see
   §7c) before treating the absence as exhaustively confirmed.

## 25. Acceptance checklist

This is the checklist the completed independent review verified before this
ADR was accepted (final verdict: **PASS — no P1/P2 findings**, following two
targeted corrective passes). It is retained as the standing record of what
acceptance confirmed — not of a trust-granting mechanism, since none is
proposed:

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
7. Does this ADR's NO-GO stay explicitly scoped to the audited task/event/
   `pmxcfs`-observation families (§9 rows A/B/H), without claiming that
   every conceivable host-rooted witness (kernel audit/LSM/`eBPF`-based,
   or externally-anchored) is impossible (§1, §6, §12, §24 item 7)? Verify
   before accepting.
8. Does Family G's treatment (§9, §10, §12, §24 item 3) avoid claiming
   "immunity" and instead state plainly that operator confirmation alone
   does not satisfy Blocker B, that CAS prevents stale backend decisions
   rather than occupant substitution, and that a persistent-trust-free
   mutation model would need its own ADR 0001/0005 architecture change plus
   a safe point-in-time target proof? Verify before accepting.
9. Does §7c's `pmxcfs`-notification-absence claim stay bounded to the five
   files actually checked (§7c, §24 item 8), rather than asserting an
   exhaustive whole-repository absence, and is the FUSE-`inotify`-support
   finding pinned to the mid-2025 kernel status located this session rather
   than presented as a permanent fact? Verify before accepting.
10. Does this ADR authorize any schema, runtime, hostd, HA control, or
    mutation implementation? **No** — confirm before accepting.
11. Is Blocker B left explicitly OPEN, R0 explicitly unaffected, and Phase
    1C explicitly BLOCKED? Verify before accepting.

Acceptance of this ADR records the research conclusion (NO-GO for the
families audited here) as the current architecture record. It does not,
by itself, authorize any further implementation.

## Sources / Evidence

Read this session (August 2026), in addition to the ADR 0001/0002/0003/0005
sources they build on. Findings pinned to upstream mailing-list activity are
current only as of the date noted; a future ADR relying on them must
re-verify against the then-current kernel/PVE release rather than citing
this ADR's date as still current (§7c, §24 item 8):

- [`proxmox/pve-manager`, `PVE/API2/Cluster.pm`](https://github.com/proxmox/pve-manager/blob/master/PVE/API2/Cluster.pm) — `/cluster/tasks` route, `get_tasklist()` usage, no cursor/sequence field (§7a)
- [`proxmox/pve-common`, `PVE/RESTEnvironment.pm`](https://github.com/proxmox/pve-common) — UPID encoding, task log path (`/var/log/pve/tasks/`), fixed-size archive-index rotation (§7a)
- [`proxmox/pve-cluster`, `src/pmxcfs/pmxcfs.c`](https://github.com/proxmox/pve-cluster/blob/master/src/pmxcfs/pmxcfs.c) — FUSE `fuse_operations` table; absence of any kernel-notification callback (§7c)
- `proxmox/pve-cluster`, `src/pmxcfs/server.c`, `dfsm.c`, `memdb.c`, `cfs-plug-memdb.c` — four further core files checked this session for any `notify`-related call; none found (§7c). The remaining files under `src/pmxcfs/` (`cfs-plug.c`, `cfs-plug-link.c`, `cfs-plug-func.c`, `cfs-utils.c`, `database.c`, `status.c`, `loop.c`, `dcdb.c`) were not individually re-checked (§24 item 8).
- [Proxmox Cluster File System (pmxcfs) documentation](https://pve.proxmox.com/pve-docs/chapter-pmxcfs.html) — `pmxcfs` architecture, `/etc/pve` mount, Corosync-backed replication (§7c)
- Linux kernel mailing list, [RFC PATCH 0/7] Inotify support in FUSE and virtiofs (originally posted ~October 2021, `https://lkml.kernel.org/linux-fsdevel/YYMNPqVnOWD3gNsw@redhat.com/t/`) — RFC-stage status of FUSE `inotify` support (§7c)
- Linux kernel mailing list / `virtiofsd` issue tracker, discussion on disallowing `inotify` watches on unsupported filesystems and on FUSE/`virtiofs` `inotify` support, with activity as recent as **May 2025** confirming the feature remained unmerged in the mainline kernel at that date — `inotify_add_watch()` silently succeeding without delivering events on unsupported filesystems (§7c; time-bound finding, see note above)
- Community-reported (forum-strength, not independently re-derived from source this session) task-log archive rotation figures, cited only as corroborating context, not as the load-bearing claim (§7a, §24 item 5)

Repositories on GitHub are official read-only mirrors; the authoritative
upstream remains [git.proxmox.com](https://git.proxmox.com/). Conclusions
about what a given behavior does *not* guarantee are architectural
inferences from the cited contract and source, not a claim of an
additional Proxmox guarantee.
