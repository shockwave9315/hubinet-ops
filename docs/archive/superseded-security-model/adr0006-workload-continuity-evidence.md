# ADR 0006 supporting research: Proxmox VE lifecycle-observability evidence

**Status: NON-NORMATIVE RESEARCH / EVIDENCE. This document is not
architecture authority.**

Read this first:

- This document is the **evidence and source-research record** supporting
  `docs/architecture/adr/0006-workload-continuity-stronger-proof.md`. It is
  not an ADR, not an accepted architecture contract, and not a decision
  record.
- **If anything here conflicts with an ACCEPTED ADR, or with ADR 0006's
  accepted normative text, the normative architecture wins** and
  this document is the thing that must be corrected.
- Nothing here authorizes implementation, schema, runtime, `hostd`, HTTP,
  Home Assistant, mutation, or enrollment work of any kind, and nothing here
  authorizes `security_continuity: unverified -> trusted`.
- Nothing here selects a Blocker-B mechanism, closes Blocker B, authorizes
  WAVE B1, or unblocks Phase 1C. ADR 0006's normative core owns every
  classification; this document only records the evidence those
  classifications were reached from.
- Evidence labels are load-bearing and must not be silently upgraded:
  **FACT-DOC** (documented by Proxmox), **FACT-SOURCE** (behavior visible in
  official Proxmox/Linux source, read in the session noted), **INFERENCE**
  (architectural conclusion drawn from those facts), **UNKNOWN** (not
  confirmed by an official contract). An UNKNOWN or INFERENCE item here is
  not a fact, and a future ADR may not cite it as one.
- Every time-bound finding below is current only as of the session noted
  (research performed August 2026). A future ADR relying on any of it must
  re-verify against the exact PVE/kernel release it actually supports, never
  cite this document's date as still current.

Section numbers in the form "ADR 0006 §N" refer to the **restructured
normative core**, not to any earlier revision's numbering.

---

## R1. Two PVE task-list/enumeration surfaces audited

This section audits two structurally different task-list/enumeration
surfaces, each with its own publication/retention properties. It is **not**
an exhaustive model of every PVE task-evidence read.

Neither list view is a complete durable historical ledger, but their source
provenance partially overlaps: Surface A is derived from the node-local
active-task state before independent broadcast/truncation, Surface B's
`active`/`all` branches consume that same active state, and only Surface B's
archive branch uses the separate `index`/`index.1` lifecycle.

Both list surfaces are identified by `UPID`
(`node:pid:pstart:starttime:type:id:user:`), assembled from the node name,
worker OS PID, the PID's start time (`pstart`, used to disambiguate PID
reuse), a wall-clock start timestamp, worker type, target ID, and acting
user (`PVE::UPID::encode`, `proxmox/pve-common`, `PVE/RESTEnvironment.pm`).
**A UPID is not a monotonic counter and does not reference a prior UPID** —
there is no chain, hash-link, or sequence field tying one task to "the task
before it" for a given slot; wall-clock time is not concurrency authority
(mirrors ADR 0002's own rule for `source_config_revision`/timestamps
generally). This holds for both list surfaces below.

### R1.1 Exact-UPID child reads — acknowledged, NOT AUDITED

The same `PVE::API2::Tasks` registers
`GET /nodes/<node>/tasks/<upid>/status` and
`GET /nodes/<node>/tasks/<upid>/log`; `read_task_status` decodes the supplied
exact UPID and requires the corresponding task file to exist. Both child
reads require task ownership or `Sys.Audit` on `/nodes/<node>`.

This research has **not** established exact-UPID task-file/log retention,
readability after a task disappears from `index`/`index.1`, useful
prefix/gap semantics, coupling to the `node_tasks` list retention, or whether
direct reads help or hurt a stateful sentinel. Their completeness and
usefulness remain **UNRESOLVED / NOT AUDITED HERE**.

### R1.2 Surface A — `GET /cluster/tasks`: a recent, cluster-wide status-cache view

- **FACT-SOURCE.** `GET /cluster/tasks` (`PVE::API2::Cluster`, the `tasks`
  route) is documented in its own route registration as *"List recent tasks
  (cluster wide)"* — the word "recent" is the route's own description, not
  this research's characterization. It calls `PVE::Cluster::get_tasklist()`
  and filters results by caller privilege (own tasks unless `Sys.Audit`); the
  handler does not sort, page, or attach any sequence/cursor field of its own.
- **FACT-SOURCE.** `PVE::Cluster::get_tasklist()` (`proxmox/pve-cluster`,
  `src/PVE/Cluster.pm`) reads a **per-node, corosync-distributed
  in-memory/KV status cache** (`ipcc_get_status("tasklist", $node)`,
  version-checked against a per-node `kvstore` entry, with a local
  `$tasklistcache`) — this is **not** the same storage as the archive files
  in Surface B's archive branch. Its pre-broadcast source is
  `active_workers()`: that function reads and, when updated, writes the
  node-local `PVE::INotify` `active` state that Surface B's `active`/`all`
  branches also read. The list views therefore partially share source-state
  provenance even though Surface A adds an independent cluster broadcast and
  truncation lifecycle.
- **FACT-SOURCE.** The cache is updated on **confirmed update paths that
  include at least the following three** — this is not claimed to be an
  exhaustive list, since the full call graph was not audited:
  1. `PVE::RESTEnvironment::fork_worker()` (`proxmox/pve-common`) calls
     `$self->active_workers($upid, $sync)` and
     `$self->broadcast_tasklist($tlist)` **immediately at every worker
     start**;
  2. `PVE::RESTEnvironment::log_task_result()` (`proxmox/pve-common`,
     `PVE/RESTEnvironment.pm`, invoked by the worker reaper on completion)
     calls `$self->active_workers($upid)` and
     `$self->broadcast_tasklist($tlist)` **immediately at worker
     completion**;
  3. `pvestatd` (`proxmox/pve-manager`, `PVE/Service/pvestatd.pm`)
     separately re-runs `active_workers()` + `broadcast_tasklist()` on its
     own periodic `update_status()` loop every **10 seconds**
     (`my $updatetime = 10`), which among other things notices
     finished/crashed workers and refreshes cluster status.

  The correct phrasing is always "confirmed update paths include…", never
  "the two update paths" and never "the 10-second cycle alone."
- **FACT-SOURCE — stage 1, the *pre-broadcast* `active_workers()` list, not
  yet what gets published.** `active_workers()` (`proxmox/pve-common`,
  `PVE/RESTEnvironment.pm`) retains **all currently running tasks
  unconditionally** in this intermediate list — the running-task count never,
  by itself, removes or caps running entries there. It then adds
  recently-finished tasks *only while the current list length remains below*
  a fixed threshold of **`MAX_FINISHED = 25`**:

  ```perl
  my $max = $MAX_FINISHED - scalar(@$tlist);
  foreach my $task (@ta) { last if $max <= 0; push @$tlist, $task; $max--; }
  ```

  Running tasks are already in `$tlist` before this loop runs. Concretely: 0
  running admits up to 25 finished (list length up to 25); 5 running admits
  up to 20 finished (length up to 25); 20 running admits up to 5 finished
  (length up to 25); 25 or more running admits **no** finished tasks — and
  the list length is then however many tasks are actually running, which
  **can exceed 25** (e.g. 40 running tasks yields a 40-entry list with 0
  finished tasks added). `MAX_FINISHED` is **not** a hard cap on this list's
  total size — it only bounds how many *finished* tasks are appended.
- **FACT-SOURCE — stage 2, the actually-published Surface A.**
  `broadcast_tasklist()`'s *executable* truncation loop
  (`proxmox/pve-cluster`, `src/PVE/Cluster.pm`) is:

  ```perl
  while ($size >= (32 * 1024)) { pop @$data; ... }
  ```

  — an actual, currently-enforced cap of **32 KiB**, not 128 KiB (the 128 KiB
  `CFS_MAX_STATUS_SIZE` figure is only a
  `# TODO: update to 128 KiB in PVE 8.x` comment on code that still truncates
  at 32 KiB in the source checked). This is the step that produces the
  actually-published Surface A: it `pop`s entries — **including running
  tasks**, if that is what the list still contains — from whatever
  `active_workers()` handed it, in list order, until the serialized JSON is
  under 32 KiB, with no exemption for running-task entries. A pre-broadcast
  list containing many running tasks is truncated by this same 32 KiB bound
  exactly like a list padded with finished tasks.
- **Conclusion for Surface A.** Two stages exist, and only the second is what
  a caller of `GET /cluster/tasks` observes. **The published Surface A is a
  recent, doubly-bounded status-cache view with no durable retention and no
  cursor** — a sufficiently busy node (enough running and/or finished tasks
  to exceed either the pre-broadcast 25-entry finished-task budget or the
  published 32 KiB payload bound) can cause a completed task's record, or
  even a still-running task's record, to be absent from what is actually
  published, independent of the archive files in Surface B. **Task creation
  (a UPID exists) is distinct from guaranteed observation in the published
  Surface A** — neither stage is a durable ledger of every task ever created.

### R1.3 Surface B — `GET /nodes/<node>/tasks`: node-local list with archive and active branches

- **FACT-SOURCE.** `GET /nodes/<node>/tasks` (`PVE::API2::Tasks`,
  `node_tasks`) supports `source=archive|active|all` (default `archive`). The
  **archive branch** reads the node-local persisted
  `/var/log/pve/tasks/index` and `/var/log/pve/tasks/index.1` via
  `File::ReadBackwards`; `active` and `all` can additionally consume the same
  node-local `active` state used by `active_workers()`. The endpoint also
  supports `start`/`limit`/`since`/`until`, plus
  `userfilter`/`typefilter`/`vmid`/`errors`/`statusfilter` — but **no
  monotonic or gap-detectable cursor parameter of any kind**.
  `/var/log/pve/tasks/` is a regular node-local path, not a
  `pmxcfs`/`/etc/pve` path, so this archive is per-node, not
  cluster-replicated.
- **FACT-SOURCE.** Results are authorization-filtered: `Sys.Audit` on
  `/nodes/<node>` permits all node tasks; without it, the caller sees only
  tasks it owns/is allowed to see. A successful response therefore does not
  establish complete task coverage unless a future reader contract proves the
  required privilege and ACL scope.
- **FACT-SOURCE.** The same `RESTEnvironment.pm` that defines
  `active_workers()` also rotates this archive: the active `index` file is
  capped at a **fixed size threshold**
  (`my $maxsize = 50000; # about 1000 entries`); when exceeded, it is renamed
  to `index.1` and a fresh `index` begins. This is a **bounded rolling
  window**, not a permanent, officially size-unbounded audit trail.
- **UNKNOWN (forum-strength corroboration only).** Community-observed
  defaults report a further archive-file rotation (roughly 512 KiB per
  archive file, newest ~20 archive files retained, on the order of ~100,000
  total entries). This specific figure is **not** FACT-DOC/FACT-SOURCE — it is
  forum-sourced, not independently re-derived from source — and is cited only
  as corroborating context, never as a load-bearing claim. The load-bearing
  claim is the confirmed-in-source rotation-on-size behavior itself.
- **Conclusion for Surface B.** Its finished historical-task finding rests on
  the bounded, node-local **archive branch**, which has no
  monotonic/gap-detectable cursor. The endpoint is not archive-only:
  `active`/`all` may add current active tasks, but that does not establish
  complete historical coverage. A **stateless observer** relying only on the
  currently visible archive branch cannot distinguish "no destroy/create pair
  occurred" from "one occurred, but the record is no longer present in the
  API-visible `index`/`index.1` retained set." This research does not assert
  where a rotated-past record physically goes (the further archive-file
  retention layer is only forum-strength/UNKNOWN, above); it asserts only
  that the API-visible retained set stops showing it. Whether a **stateful**,
  sentinel-tracking observer could detect the disappearance itself as a
  coverage gap is a separate, genuinely unresolved question (R6).

### R1.4 Combined conclusion for the task surfaces

The list views partially overlap in active-state provenance, while Surface
A's cluster publication and Surface B's archive retention add different
limits. Neither carries an officially documented, monotonic, gap-detectable
cursor of its own — consistent with ADR 0002 §"Kiedy dokładnie wolno ustawić
`confirmed_removed`", Class B; this research reaches the identical conclusion
ADR 0002 already reached and marked **UNKNOWN**, which is expected
corroboration, not a new finding.

This shows that a **stateless** witness — one with no memory of its own
between observations, relying purely on whatever either surface happens to
still show at query time — **cannot** prove gapless coverage from either
surface alone. **It does not show that a *stateful*, fail-closed witness
necessarily fails.** A witness that durably remembers its own overlap
sentinel (e.g. specific previously-observed UPIDs) between observations, and
treats the sentinel's *absence* from the currently retained set as a detected
coverage gap forcing immediate revocation rather than silent continuation,
was never designed or audited here. Exact-UPID status/log reads do not change
that conclusion, because their retention and gap semantics were not audited
either. A durable, witness-owned coverage state/protocol remains necessary
either way — PVE's own task retention (of either kind) is never sufficient by
itself.

## R2. Task-generation coverage for destroy/create/clone/rollback/restore

- **FACT-SOURCE — narrowed to the same-slot create/destroy witness, confirmed
  directly against source; not a blanket claim across every
  continuity-relevant operation.** For **QEMU**: create dispatches through
  `fork_worker('qmcreate', ...)`, and destroy through
  `fork_worker('qmdestroy', ...)` (`proxmox/qemu-server`,
  `src/PVE/API2/Qemu.pm` — **not** `proxmox/pve-manager`, which an earlier
  research draft misattributed). For **LXC**: create dispatches through a
  `vzcreate`-named worker via `fork_worker(...)`, and destroy through
  `fork_worker('vzdestroy', ...)` (`proxmox/pve-container`,
  `src/PVE/API2/LXC.pm` — likewise not `proxmox/pve-manager`). Ordinary
  QEMU/LXC destroy and create, through these verified normal API routes, are
  confirmed to create worker/UPID task records exactly like any other task.
- **UNKNOWN — explicitly not extrapolated from the bullet above.** The exact
  route-registration source line for clone, snapshot rollback, backup
  restore, or migration — ADR 0006 §4's class N, P, and T operations — was
  **not** re-derived for either QEMU or LXC. The general pattern (Proxmox's
  long-standing, widely-documented convention of dispatching guest-lifecycle
  operations as UPID-tracked background workers) makes it *plausible* that
  these also generate task records, but complete QEMU/LXC parity for them is
  **not** established at FACT-SOURCE strength — each remains its own open
  question until individually audited. A future stateful Family-B ADR relying
  on task-generation coverage must establish that coverage itself, operation
  by operation, rather than inherit this narrower, verified create/destroy
  witness.
- **INFERENCE — the direct-write gap (T3-tier, out of scope for the audited
  anchor-less candidates).** Coverage-via-task-creation is a property of the
  **high-level API/CLI path**, not of the underlying storage. `pmxcfs`
  (`/etc/pve`) is a normal, directly writable POSIX-like mount to anyone with
  sufficient privilege on any cluster node — nothing in the filesystem layer
  itself requires that a guest's config file be created/deleted only via a
  task-wrapped worker. A party who can write to `/etc/pve` directly (root on
  any member node) can delete/recreate a guest's **config metadata object**
  without ever invoking `qm`/`pct`/the API at all, producing **no task, no
  UPID, no log entry**, for that metadata event. Rewriting the `.conf` object
  with the disk genuinely untouched is not, by itself, a workload replacement
  (ADR 0006 §4's metadata-versus-occupant distinction) — but nothing in stock
  PVE prevents the same node-root actor from **also** replacing the
  underlying disk/process in the same window (e.g. by pointing the recreated
  config at different storage, or overwriting the volume directly), producing
  a genuine occupant replacement with **no task/UPID trace for the combined
  operation**. This is not a stock-PVE defect; it is an inherent consequence
  of `pmxcfs` being a shared, directly-writable configuration store rather
  than an access-mediated service boundary.

  Per ADR 0006 §5's T3/T4 boundary and consistency rule, this is a
  **T3-tier** capability that, for a co-resident witness with no
  root-resistant/external anchor, collapses into T4 — out of scope. It is
  therefore **supplementary, non-load-bearing threat-model context only**,
  and is never the reason for any candidate's classification.
- **Distinction that must not be collapsed: task *creation* is not task
  *observation*, and the creation claim itself is scoped to what is actually
  source-verified.** The bullets above establish that an in-scope T1/T2
  destroy/create pair, through the verified normal QEMU/LXC API routes,
  genuinely creates a UPID task record. They do **not** establish the same
  for clone/migrate/restore/rollback (UNKNOWN, above), and, for
  destroy/create itself, they do **not** establish that any later query of
  either Surface A or Surface B is guaranteed to still show that record.

## R3. `pmxcfs` FUSE architecture and file-change notification

### R3.1 The five-file source inspection

- **FACT-SOURCE.** `pmxcfs`'s FUSE operation table (`proxmox/pve-cluster`,
  `src/pmxcfs/pmxcfs.c`) registers exactly: `getattr, readdir, mkdir, rmdir,
  rename, open, read, write, truncate, create, unlink, readlink, utimens,
  statfs, init, chown, chmod`. **No kernel-notification callback of any kind
  is registered in this table** — no call to
  `fuse_lowlevel_notify_inval_entry`, `fuse_notify_poll`, or any equivalent
  low-level invalidation/notification primitive appears in the
  `fuse_operations` struct itself.
- **FACT-SOURCE.** The same absence of any FUSE/kernel
  invalidation-notification call was independently confirmed across four
  further core `pmxcfs` implementation files: `server.c` (the
  daemon/dispatch loop), `dfsm.c` (the distributed
  finite-state-machine/Corosync message-application layer — the code most
  architecturally likely to apply a remote node's change locally), `memdb.c`
  (the in-memory/SQLite-backed database layer), and `cfs-plug-memdb.c` (the
  FUSE plugin backing ordinary file content from that database). `memdb.c`
  contains GLib's unrelated `GDestroyNotify` type name, so this is **not** a
  zero-textual-substring claim.
- **Scope limit — UNKNOWN beyond those five files.** This is **five of
  roughly a dozen files** under `src/pmxcfs/`; the remainder (`cfs-plug.c`,
  `cfs-plug-link.c`, `cfs-plug-func.c`, `cfs-utils.c`, `database.c`,
  `status.c`, `loop.c`, `dcdb.c`) were **not** individually re-checked and
  are **UNKNOWN** at this citation granularity. This is not a claim of an
  exhaustive, whole-repository search.
- **INFERENCE, bounded to what was actually checked.** Because the FUSE
  operation table itself and the four files most architecturally likely to
  apply a remote (Corosync-originated) change contain no such call between
  them, the specific, narrow claim is: **no evidence was found, in the files
  checked, that `pmxcfs` uses those FUSE cache-invalidation primitives to
  translate a Corosync-originated change on another node into a local kernel
  dentry/inode invalidation.** Two further limits apply. First, the broader
  claim that *no file anywhere in the repository* does this is **not**
  independently verified and is INFERENCE at that broader scope. Second — and
  load-bearing — the primitives actually searched for
  (`fuse_lowlevel_notify_inval_entry`/`_inval_inode`/`fuse_notify_poll`-class
  calls) are **cache-coherency primitives**, not an fsnotify-delivery
  mechanism, so their absence is evidence about `pmxcfs`'s use of *those*
  primitives in *those* files — **not, by itself, the exact proof target**
  for whether a remote mutation reaches a local `fsnotify`/`inotify` watcher.

### R3.2 Path A — same-node, locally-originated VFS operations

When a process running *on the same node* issues an ordinary syscall
(`unlink()`, `create()`, `write()`, `rename()`) against the `pmxcfs` FUSE
mount, that syscall is dispatched through the normal Linux VFS layer before
FUSE ever sees it. Linux's `fsnotify`/`inotify` hooks fire as part of the
VFS's own syscall-handling path (`vfs_unlink`, `vfs_create`, etc.),
independent of the underlying filesystem type — this is general,
well-established VFS behavior, not something the FUSE filesystem driver
itself has to implement via `fuse_lowlevel_notify_*` calls. **The absence of
those callbacks in `pmxcfs` (R3.1) says nothing about this path** — those
low-level notify primitives exist for a FUSE server to proactively push an
invalidation for a change the *kernel does not already know about* (Path B);
they are not required for the kernel to notice a syscall it itself just
dispatched.

This research has **not** independently, primary-source verified that
`pmxcfs` specifically delivers correct `fsnotify` events for every relevant
local operation (e.g. whether any FUSE mount option `pmxcfs` sets suppresses
this, or whether every kernel version behind supported PVE releases behaves
identically). Same-node, locally-originated delivery is therefore classified
**plausible / expected per general Linux VFS behavior, but UNKNOWN at
FACT-DOC/FACT-SOURCE strength for `pmxcfs` specifically** — not proven, and,
critically, **not proven incomplete either**. Any claim that local-node
delivery has "no documented completeness guarantee" overstates what the FUSE
operation-table finding shows.

### R3.3 Path B — remotely-replicated / behind-the-mount changes

**FUSE cache invalidation must not be equated with `fsnotify`/`inotify` event
delivery.**

When a change originates on a *different* node and reaches this node only via
Corosync (applied inside `pmxcfs`'s own in-memory/SQLite state, not via a
syscall on this node's mount), the local kernel has no syscall on *this* node
to hook for that change. The security question is narrow: does a local
`fsnotify`/`inotify` watcher receive an **event** for a change that did not
originate through a local VFS syscall? It is not a broader question about
every way the kernel could later learn of, or revalidate, changed state.

The five-file absence finding (R3.1) is **not** the direct evidence for that
question. Those FUSE notification codes are **cache-coherency primitives** —
the reverse-invalidation path
(`fuse_notify_inval_inode -> fuse_reverse_inval_inode`,
`fuse_notify_inval_entry -> fuse_reverse_inval_entry`) performs dentry/inode
cache invalidation (`d_invalidate`, `fuse_invalidate_entry_cache`-class
operations). **Absent a separate, accepted contract, cache/dentry
invalidation must not be treated as equivalent to "remote filesystem mutation
-> `fsnotify`/`inotify` event delivered to a local watcher."** Historical
FUSE fsnotify work proposed a *distinct* fsnotify-propagation protocol
precisely for this class of remote/behind-the-mount event, which is why the
two cannot be collapsed.

Consequently: the five-file source fact is **preserved**, but what it proves
is **narrowed** — it is evidence that `pmxcfs` does not use those checked
cache-invalidation primitives in those files; it is **not**, by itself, the
exact proof target for remote fsnotify event delivery. Path B remains
**UNRESOLVED**, not a NO-GO.

The four-step positive Path-B audit a future ADR would owe (carried
normatively in ADR 0006 §8):

1. fix the exact supported PVE kernel/FUSE release;
2. establish the exact kernel/userspace protocol or mechanism that can turn a
   behind-the-mount change into a local `fsnotify`/`inotify` event on that
   release;
3. audit whether `pmxcfs`/`libfuse` actually uses **that** exact mechanism;
4. only then prove completeness, ordering, and gap semantics for it.

None of those four steps was performed here.

### R3.4 Upstream Linux source — time-bounded supporting evidence

**FACT-SOURCE, time-bound.** At Linux revision
`26260251022fbc2f248a3d747a9b2b961b18d2d8`, checked during this research:

- `include/uapi/linux/fuse.h` declares FUSE protocol version **7.45** and
  includes the ordinary notification codes `FUSE_NOTIFY_INVAL_INODE` /
  `FUSE_NOTIFY_INVAL_ENTRY` among others, while **no `FUSE_FSNOTIFY` protocol
  opcode was found**;
- `fs/fuse/notify.c` routes
  `fuse_notify_inval_inode -> fuse_reverse_inval_inode` and
  `fuse_notify_inval_entry -> fuse_reverse_inval_entry`;
- `fs/fuse/dir.c` implements reverse entry invalidation as cache/dentry
  invalidation (`d_invalidate` / `fuse_invalidate_entry_cache`-class
  operations).

This corroborates R3.3's distinction — the notification codes that exist are
invalidation primitives, and no dedicated fsnotify-propagation opcode was
found at that revision. **This is explicitly not a permanent NO-GO:** the
target/deployed PVE kernel may differ, or carry patches/backports, and the
exact supported PVE kernel was **not** audited here. Any future ADR relying
on this must re-verify against the kernel it actually supports.

### R3.5 Upstream FUSE `inotify` status — INFERENCE from discussion, not source

**INFERENCE / upstream-status evidence — deliberately *not* labelled
FACT-SOURCE.** The cited evidence for this item is Linux kernel-mailing-list
and `virtiofsd` issue-tracker *discussion*, not source code read directly,
and it does not meet the FACT-SOURCE bar.

General kernel limitation, independent of `pmxcfs`, pinned to when it was
checked, and scoped to Path B: Linux `inotify`/`fanotify` are documented as
supported for local kernel filesystems for ordinary, locally-dispatched
syscalls (Path A); the historical reliability gap is specifically about
surfacing changes that do not arrive via a local syscall (Path B class). As
of the most recent upstream discussion located — a Linux kernel mailing list
thread on disallowing `inotify` watches on unsupported filesystems, with
activity as recent as **May 2025** indicating FUSE/`virtiofs` support for
propagating exactly this class of behind-the-mount change was still **not
merged into the mainline kernel** as of that discussion —
`inotify_add_watch()` can silently succeed without error on a filesystem that
does not actually deliver such events, and kernel-level support for this
specific case appears, from that discussion, to remain at best an RFC-stage
patch series (originally posted ~October 2021, targeting `virtiofs`).

This is INFERENCE from upstream discussion, **not** independently confirmed
against current mainline kernel source, and is a **time-bound** finding —
current as of the mid-2025 discussion located (this research was performed
August 2026). It is not a permanent architectural fact and not a
FACT-SOURCE-level guarantee. It bears on Path B, not on ordinary local
`fsnotify` delivery (Path A). It is consistent with, and does not replace,
R3.3: the historical FUSE fsnotify RFC work exists precisely because the
ordinary invalidation notifications are not an fsnotify-propagation protocol.
A later mainline kernel release could merge this support, or current mainline
source could already differ; any future ADR must independently re-verify the
current kernel/FUSE state — ideally against source, not discussion — at
implementation time.

### R3.6 Operation-to-event generation — a distinct, prior, unaudited question

R3.2–R3.5 address whether an event, once generated, is *delivered* to a
watcher. They do **not** address whether every in-scope continuity-relevant
operation necessarily *generates* an authoritative `pmxcfs` file-level event
in the first place.

Ordinary create/destroy plausibly involve `write()`/`unlink()`/`rename()`
against the guest's config object, but config location alone does not
establish the security-contract-strength claim that those workflows
necessarily produce an authoritative event. This research has **not**
established complete `pmxcfs` operation-to-event coverage for **any**
continuity-relevant workflow. Rollback, restore, and changes partly or wholly
below the `pmxcfs` config layer (including disk/storage replacement) remain
separately unresolved.

**Operation-to-event coverage is UNRESOLVED**, independent of, and prior to,
the Path A / Path B delivery questions. A mechanism can have perfect delivery
and still miss a continuity-relevant transition that never emitted a
`pmxcfs`-level event to deliver.

### R3.7 The distributed, per-node `pmxcfs` watcher — identified, not audited

A distributed variant — one watcher process per relevant PVE node, each
relying only on Path A (its own node's local `fsnotify` delivery), such that
whichever node actually executes a given continuity-relevant operation's
local syscall has its own watcher observe it directly, without needing Path B
at all — is identified here but was **not** audited or designed. It depends
on:

- Path A's completeness (itself UNKNOWN, R3.2);
- which node actually executes a given operation's syscall (itself UNKNOWN,
  R2 / R8 item 9);
- distributed coverage/gap/restart semantics across every node in a source
  (never designed);
- the same operation-to-event coverage question (R3.6) — not resolved merely
  by solving Path A's delivery question at every node;
- whether it would also need independent coverage of the storage layer for
  the disk-replacement half of the combined direct-write scenario (R2).

Classified **UNRESOLVED / NOT AUDITED HERE**: not claimed to succeed, not
claimed to fail.

## R4. Evidence summary table

| Property required for a witness | Stock PVE support |
| --- | --- |
| Monotonic, gapless task/event cursor, native to either audited PVE task-list surface | **No** — UPID is not a sequence; neither list view is a complete gapless durable ledger, their additional publication/retention limits differ, and their active-state provenance partially overlaps (R1). Exact-UPID status/log retention and gap semantics are separately **UNRESOLVED / NOT AUDITED HERE** (R1.1) |
| Officially guaranteed task-history retention on the audited list surfaces | **No** — the pre-broadcast `active_workers()` list retains all running tasks unconditionally and admits finished tasks only while list length is below `MAX_FINISHED=25`, but the **published** Surface A is what survives `broadcast_tasklist()`'s separate 32 KiB truncation, which can drop running or finished entries; Surface B's archive branch rotates at a fixed size threshold (`index`/`index.1`), while `active`/`all` can additionally consume current active tasks but provide no complete historical ledger (R1.2, R1.3) |
| Whether a *stateful*, fail-closed overlap-sentinel witness could compensate using list enumeration and/or exact-UPID child reads | **UNRESOLVED / NOT DESIGNED OR AUDITED HERE** — only a stateless list observer is shown insufficient; exact-UPID retention and a witness-maintained sentinel were not audited (R1.1, R1.4, R8 item 12) |
| Complete task-reader authorization visibility | **UNRESOLVED / REQUIRED FOR FAMILY B** — the APIs filter by owner unless the reader has the applicable `Sys.Audit` scope; a successful filtered response is not complete history, and missing/ambiguous ACL coverage is an authority-ineligible gap (R1.3; ADR 0006 §8) |
| Task creation for *every* continuity-relevant event a mechanism's detection scope covers | **UNKNOWN beyond the verified create/destroy witness.** Ordinary QEMU/LXC create and destroy (class R), through the verified normal API routes, are confirmed to create UPID worker tasks. Complete task-generation coverage for every *other* in-scope route (class P rollback/restore, class N clone/restore-to-new-locator, class T migration) remains **UNKNOWN** (R2). Separately, a direct `pmxcfs`/storage write (T3) creates no task at all, but that remains supplementary, out-of-scope, non-load-bearing |
| Every claimed in-scope operation necessarily generates an authoritative `pmxcfs` file-level event at all (operation-to-event coverage — distinct from, and prior to, delivery) | **UNRESOLVED for every workflow at the required security-contract strength.** Ordinary create/destroy is plausible because QEMU/LXC configs live under `pmxcfs`, but config location does not establish complete operation-to-authoritative-event coverage. Rollback/restore/storage-level changes remain separately unresolved (R3.6) |
| Reliable same-node, locally-originated `pmxcfs` change delivery (Path A) | **UNKNOWN — plausible/expected per general Linux VFS behavior, NOT proven, and NOT disproven.** The absent FUSE notify-callback finding does not bear on this path (R3.2) |
| Reliable cross-node `pmxcfs` change delivery for Corosync-replicated writes (Path B) | **UNRESOLVED.** The five core files checked show no use of the FUSE *cache-invalidation* primitives searched for — a bounded finding about those primitives in those files, **not** the proof target for remote `fsnotify`/`inotify` event delivery (R3.1, R3.3). FUSE-level support for propagating this class of behind-the-mount change remained RFC-stage per upstream discussion as of the mid-2025 status checked, and upstream Linux `26260251…` exposes ordinary invalidation notification codes with no `FUSE_FSNOTIFY` opcode found — both time-bound, neither an exhaustive absence proof, and the exact supported PVE kernel was not audited (R3.4, R3.5) |
| Distributed, per-node `pmxcfs` watcher relying only on Path A per node | **UNRESOLVED / NOT AUDITED HERE** — depends on operation-to-event coverage, Path A's completeness, which node executes a given operation, and undesigned distributed coverage/gap semantics (R3.7) |

## R5. Candidate-family evidence detail

ADR 0006 §6 carries the canonical, normative **result** for each family. This
section records the underlying per-family evidence only, and changes no
classification.

Family A is split into two rows: the **single-node** `pmxcfs`-witness variant
audited alongside task history, and a **distributed**, per-node variant
identified but never audited. The per-operation columns are **not** one
collapsed "identity-breaking" verdict: each is read against ADR 0006 §4's
class for that operation, and each class carries its own accepted ADR 0001
consequence.

**No row in this table carries a "No" verdict grounded in the R1–R3
task/`pmxcfs` research** — A, A2, and B are all **UNRESOLVED / NOT (FULLY)
AUDITED**, neither a "No" nor a "Yes". Families C–F are evaluated on
separate, already-established grounds (clone-copyability of disk-resident
state, or a node-vs-resource axis mismatch) independent of that research, so
their verdicts are not narrowed by its scoping — but those independent
grounds do **not** verdict C–F uniformly. Families D, E, and F's narrow
variant remain **"No"** on disk-resident-copyability grounds; **Family C
remains "not sufficient / not applicable as a Blocker-B resource-continuity
proof"** on node-vs-resource-axis grounds — a hardware node attestation
answers a different axis than Blocker B asks about, not a weaker "No" of the
same kind as D/E/F.

| Family | What it proves | Trust root | Copyable by clone? | Same-slot recreate? (class R) | Snapshot rollback? (class P) | Restore? (class P or R by context) | Migration? (class T) | Coverage loss on restart? | Offline interval? | Replay? | Privilege assumption | QEMU/LXC parity | Satisfies ADR 0005 §14 test? |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| **A. Single-node `pmxcfs`/hostd lifecycle witness + external epoch** | Intended: gapless observation, on the one node this witness runs on, of the events in its detection scope whose local syscall executes there | A single host-resident witness process on one node; **intended** to defend T1/T2 for operations whose syscall executes on that same node (Path A — plausible/expected, but UNKNOWN/unproven for `pmxcfs` specifically, not disproven; the design *intent*, not established protection); whether it has coverage for operations executed on a different node is **UNRESOLVED** — the claimed cross-node gap is an unproven inference from a bounded search (R3.1, R3.3), not an established architectural fact. As hypothesized it specifies no explicit root-resistant/external trust anchor, so T3 additionally collapses into T4 for it — **not the load-bearing reason for this row's classification either way** | Epoch value: no (external); whether the cross-node question affects clone-copyability is not evaluated | **UNRESOLVED** — not shown to be distinguished, and not shown to fail, when the occupant's destroy/create syscall executes on a node other than the watched one (R3.3: the absence of a confirmed remote-delivery mechanism is bounded to five checked files). Even when the syscall executes on the watched node, Path A delivery completeness for `pmxcfs` is UNKNOWN | **UNRESOLVED for both delivery *and* generation reasons.** The same unresolved delivery question applies, **plus** a distinct, prior question: whether rollback necessarily produces a `pmxcfs`-level event to observe at all is unverified (R3.6) — perfect delivery would not rescue this row if no such event exists | Same (both delivery and generation questions) | Requires explicit handling; whether migration to a different node defeats coverage depends on the unresolved cross-node question | Must fail closed; does not resolve the cross-node question either way | Not inherently defended | Not inherently defended; depends on epoch uniqueness discipline (ADR 0005 §16-style), which is sound but does not resolve the coverage question | A single ordinarily-privileged observer confined to one node | Config-location applicability: symmetric in principle (both QEMU/LXC configs live under `pmxcfs`). Operation-to-event parity: **UNRESOLVED**. Delivery completeness: **UNRESOLVED** | **UNRESOLVED / NOT FULLY AUDITED** — not claimed to satisfy the coverage requirement, and not claimed to fail it |
| **A2. Distributed, per-node `pmxcfs` lifecycle witness + external epoch** — not audited, not designed | Hypothetical: one witness per relevant PVE node, each relying only on that node's own local (Path A) delivery, intended so that whichever node executes a given operation's syscall has its own watcher observe it directly | One witness process per node in the source; whether this closes, partially closes, or fails to close the single-node gap is **not evaluated** | **Not evaluated** | **UNRESOLVED / NOT AUDITED HERE** — depends on operation-to-event coverage and Path A completeness at every node (UNKNOWN), on which node executes a given operation (UNKNOWN), and on cross-node coverage/gap semantics never designed. Not claimed to observe a same-slot recreate, and not claimed to fail to | Not evaluated | Not evaluated | Not evaluated — distributed node-migration semantics never designed | Not evaluated — multi-node restart/coordination semantics never designed | Not evaluated | Not evaluated | Would require witness presence on every node in the source; whether this incidentally observes the T3 combined config+disk bypass — since even a root actor's local `rm` is still a local syscall — is **not verified or claimed either way** | Config-location applicability: symmetric in principle. Operation-to-event parity and delivery completeness: **UNRESOLVED / NOT AUDITED HERE** | **UNRESOLVED / NOT AUDITED HERE** — not claimed to satisfy ADR 0005 §14, not claimed to fail |
| **B. PVE task/event/audit history as witness** | Verified QEMU/LXC create/destroy routes create UPIDs; other operation coverage, list/direct-read retention, gap semantics, and stateful coverage proof remain unresolved (R1, R2) | PVE task subsystem plus durable witness-owned coverage state | No | **UNRESOLVED:** a stateless observer fails on bounded/no-cursor history; a stateful overlap protocol is unaudited | Task generation and stateful coverage unresolved | Same | Requires explicit per-node routing/migration semantics | Must fail closed; restart protocol unresolved | Polling gaps require proven retained overlap; otherwise authority-ineligible | Not addressed | The chosen reads must provide proven `Sys.Audit`-equivalent complete visibility for every relevant actor and node; `VM.Audit` alone is insufficient. Missing/ambiguous ACL coverage and security-sensitive permission changes are coverage gaps | Source-verified symmetric only for normal create/destroy; broader task-generation parity UNKNOWN | **UNRESOLVED / NOT FULLY AUDITED** |
| **C. Hardware-rooted TPM / physical attestation** | Identity/integrity of the **physical host**, not of any specific guest incarnation | Physical TPM chip on one specific machine | N/A — a physical host property, not something guests carry | **Does not address this axis at all** — a hardware TPM attests the node, not which guest occupies a VMID slot | N/A | N/A | Breaks by construction: a hardware TPM cannot follow a guest across a live/offline migration to different physical hardware | N/A to resource continuity | N/A | N/A | Not applicable to resource continuity; **this is a node-attestation primitive, a different axis entirely (ADR 0001 node section)** | Would be identical for QEMU/LXC since it says nothing about either | **Not applicable** — solves a different problem (node trust), not Blocker B |
| **D. vTPM** | Guest-visible TPM state at read time | Software-emulated; backed by a `vtpm0` disk volume | **Yes — copied by clone/backup/snapshot identically to any other disk (already ADR 0005 §6 candidate 20)** | Fails identically to any disk-resident evidence | Fails (state travels with the snapshot) | Fails (state travels with the restore) | Travels with the guest, proves nothing about continuity | N/A | N/A | Fully replayable by anyone who can copy the disk | Root/API-level access to guest storage | QEMU only (no stock LXC vTPM) | **No** — already rejected in ADR 0005 |
| **E. Guest cryptographic agent + guest-resident key** | Key possession at read time | Private key material stored in guest disk/config state | **Yes — disk-resident, copied by clone/backup identically (ADR 0005 §13)** | Fails — new occupant can carry the copied key forward | Fails | Fails | N/A | N/A | N/A | Replayable by whoever can read the disk | Requires cooperative in-guest agent (QGA) or `pct exec`-class access; not default-on | Asymmetric (QGA is QEMU-only; LXC needs `pct exec`) | **No** — already evaluated and rejected in ADR 0005 §13 |
| **F. External/HSM-backed guest identity** — narrow variant only | **Narrow variant: a guest-resident credential whose signing authority is an external HSM, but the guest itself still presents that credential at use time.** Proves key possession at read time, same as Family E, because the artifact actually presented/copyable still lives in guest-readable state | Narrow variant reduces to (E) — an external signer does not change that the guest-side artifact is what a clone/restore copies | Narrow variant: **yes, same as (E)** | Narrow variant fails identically to (E) | Same as (E) | Same as (E) | N/A | N/A | N/A | Replayable identically to (E) | Requires cooperative in-guest presentation, same as (E) | Same asymmetry as (E) | **Narrow variant: No** — reduces to Family E. **The broader externally-rooted/out-of-band per-workload identity class — a specific workload's identity tracked/attested by an external system through a channel that is neither guest-resident nor a node-bound hardware property — is UNRESOLVED / NOT AUDITED HERE:** not claimed to satisfy Blocker B, not claimed to fail |
| **G. Operator per-mutation re-attestation / ephemeral trust** | Nothing persists as `trusted`; every mutation instead requires its own fresh, explicit, human-confirmed identity check — this **sidesteps rather than answers** the persistent-`trusted` question | The human operator, at the instant of the check, **plus** a safe point-in-time target-identity proof binding that confirmation to the resource actually mutated — not defined by this family | N/A — no persistent trust artifact exists to copy | **Not immune, and not answered by this family** — no *persisted* `trusted` state exists for a recreated occupant to inherit, but a confirmation made against occupant A is exactly as vulnerable to a same-slot substitution as any other mechanism if the confirmation is not safely fenced against a race between the human check and backend execution | No persisted state to invalidate, but the underlying rollback-substitution risk is unaddressed, not solved | Same as rollback | Same as rollback | No persisted coverage to lose across a restart — a narrower claim than "immune" | No window during which *stale persisted* trust could be consumed — does not mean the occupant-substitution question is solved | ADR 0001's exact-match CAS on `resource_id`/`binding_id`/`locator_generation`/`resource_continuity_revision` prevents replay of a **stale backend decision** — it does **not**, by itself, prove the physical/logical occupant was not substituted between confirmation and execution, since ADR 0001 explicitly permits those tokens to remain unchanged across an observationally invisible same-slot delete/recreate (ADR 0001 row 10) | Symmetric | **Does not satisfy Blocker B by itself** — operator confirmation alone is not continuity proof; adopting a mutation model that never requires persistent `security_continuity=trusted` would itself require a separate architecture change to ADR 0001/0005's accepted mutation-precondition formula |
| **H. Combinations of the above** | Higher empirical confidence, no new independent security property, **for combinations drawn only from Families C/D/E and F's narrow variant — i.e. excluding A, A2, and B** | Whichever combination of those independently-insufficient families is used, **not including B** | Combining C with D/E/F-narrow does not help: a successor occupant can carry forward copied disk/config/key evidence for D/E/F-narrow (ADR 0005 §11/§13) regardless of node-identity evidence, and C proves node identity, not resource incarnation. **A combination that additionally includes A, A2, or B inherits that component's UNRESOLVED status — it must not be manufactured into a NO-GO** | Still not distinguished, for combinations drawn only from C/D/E/F-narrow | Still fails unless one member independently solves it (none of C–E/F-narrow does) | Same | Same | Same | Same | Same | Depends on which families are combined — not a fixed "weakest member" rule | Same | **No, for combinations drawn only from Families C/D/E/F-narrow** — combining only insufficient evidence classes that introduce no new independent security property does not manufacture sufficiency; useful only as an audit/anomaly-detection signal (mirrors ADR 0005 §9–§10's demotion of the administrative marker to audit-only). A combination including A, A2, B, or a future independently sufficient externally-rooted proof is judged entirely by that component's own eventual resolution |

## R6. The same-slot witness test — per-family evidence walkthrough

ADR 0006 §5 states the test itself normatively. This section records only how
each family's evidence reads against it, and changes no classification.

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

"Destroyed"/"recreated" means the **actual physical/logical occupant** — disk
content and/or running process — genuinely changed, not merely that the
`.conf` metadata object was rewritten (which alone, with the disk untouched,
is not a continuity-relevant event at all).

- **Family A (single-node `pmxcfs` witness) — inconclusive, not failing.**
  The *intended* answer is "because the witness observed the destroy event
  and the create event as two distinct lifecycle transitions — independent of
  whether B's config matches A's." That is the **right kind of answer**: it
  does not rely on config inspection. Note what the observation would then
  have to *produce*: if the observed chain is accepted positive replacement
  evidence, it invokes ADR 0001's atomic direct replacement, not merely an
  epoch rotation on a retained `resource_id`. Whether this can be ruled out
  is **unresolved**: the claim that a destroy/create executed on a *different*
  node is necessarily unobserved rested on a bounded, five-file search for
  FUSE cache-invalidation calls — not an exhaustive `pmxcfs` search, and not
  the correct proof target for remote `fsnotify` delivery (R3.1, R3.3).
  Neither pass nor fail can be concluded. Separately, and still not
  load-bearing either way, a combined direct `pmxcfs`+storage write would
  bypass it even on the watched node, but that is a T3-tier capability not
  used to settle this test for an anchor-less witness.
- **Family A2 (distributed, per-node witness) — not evaluated.** Whether a
  witness present on *every* relevant node would have observed the
  transition, regardless of which node's local syscall it went through,
  depends on Path A's completeness at every node (UNKNOWN) and on undesigned
  cross-node coverage/gap semantics. Neither pass nor fail is claimed.
- **Family B (task history) — inconclusive, not failing.** The demonstration
  below does not rely on a T3 bypass, and uses the two audited list surfaces
  (R1) — but it only shows a *stateless* witness fails; it does not rule out
  a *stateful* one.

  Suppose the operator (or an attacker with only ordinary privilege) destroys
  A and recreates B at the same VMID entirely through the normal API/CLI.
  This is **not** the direct-`pmxcfs`-write case — it **does** produce an
  ordinary destroy worker/UPID task record and an ordinary create worker/UPID
  task record via the verified normal QEMU/LXC routes (R2) — but creation is
  not the same claim as observation *on* a surface. Each record's presence in
  the *published* Surface A, and its presence in Surface B's archive branch,
  are independently governed by those list views' bounded lifecycles (R1.2,
  R1.3). Exact-UPID status/log reads exist, but their retention and sentinel
  usefulness were not audited.

  The explicit consecutive-observation witness, for a **stateless** observer:

  ```text
  observation O1: query the relevant task surface(s) for slot 101,
    with no memory retained of what O1 itself saw

  ordinary API/CLI destroy of occupant A
  ordinary API/CLI create of occupant B, same VMID
  enough further ordinary management-surface tasks occur, on this slot
    or on any other guest sharing the same bounded surface, to exceed:
      Surface A's finished-task admission budget under MAX_FINISHED=25
        (which shrinks as more tasks are concurrently running, and
        admits none once 25+ tasks are running) and/or its
        independent 32 KiB serialized-payload truncation, and/or
      Surface B archive branch's fixed-size rotation threshold

  observation O2: query the same task surface(s) again for slot 101,
    independently of O1, with no sentinel carried forward from O1
  ```

  For a stateless observer, the intended answer at O2 is "because a destroy
  task and a create task exist in the record for that slot." The honest
  failure is not that no task was created — the destroy and create operations
  each generate a worker/UPID record (R2) — but that **neither audited list
  surface guarantees that record remains observable, and neither carries a
  monotonic or gap-detectable cursor of its own** (R1). By O2, the
  destroy/create pair may have already aged out of Surface A's small,
  actively-truncating cache, out of Surface B's rotated archive, or both. A
  stateless O2 sees **no evidence at all** in that case, and cannot
  distinguish "nothing happened between O1 and O2" from "something happened,
  but I can no longer see it."

  **This is exactly where the proof stops, and no further** — it is not
  license to conclude every conceivable Family-B design fails. A **stateful**
  witness is a plausible, unaudited counter-design: one that, at O1, durably
  records one or more exact previously-visible UPIDs (or an equivalent
  retained-set sentinel) per relevant node; at O2, requires that sentinel to
  still overlap the currently visible retained set on the relevant surface;
  treats the sentinel's *disappearance* as a detected **coverage gap**,
  forcing immediate authority-ineligibility/revocation rather than silent
  continuation; and only advances its stored sentinel once overlap is proven.
  Under such a protocol, "the destroy/create pair aged out of view" would not
  automatically look identical to "nothing happened."

  **No claim is made that such a protocol succeeds** — it is not designed or
  evaluated here, and R8 item 12 lists what would have to be closed first.
  ADR 0002 is consistent with treating this as genuinely open rather than
  closed: it requires an accepted contract guaranteeing contiguous
  event/cursor semantics for trusted destroy/create event-chain evidence, and
  marks that contract **UNKNOWN** for stock PVE — not impossible.
- **Families D, E, and F's narrow guest-held-key variant:** already shown to
  be disk-resident, not slot-transition-observing at all; B trivially
  inherits whatever A had unless the mechanism separately fails closed for
  other reasons (ADR 0005 §9–§13). Fails the test for the identical reason
  ADR 0005 already gives.
- **Family C — a different verdict, not grouped with D/E/F-narrow's
  "Fails."** A hardware node attestation answers a different axis (node
  identity) than this test asks about (resource incarnation) — it is **not
  sufficient / not applicable** as a Blocker-B resource-continuity proof,
  rather than a "Fails the test" verdict of the same kind. **Family F's
  broader externally-rooted/out-of-band class is not evaluated against this
  test at all** — unresolved, not audited.
- **Family G (ephemeral re-attestation):** there is no *persisted*
  `security_continuity=trusted` for B to inherit, because none is ever
  durably granted — the question as literally posed ("why can B not inherit
  A's stored `trusted` row") is moot by construction, not affirmatively
  answered. This must **not** be read as "immune": if an operator's fresh
  confirmation at time T is not safely fenced against a race where the actual
  occupant is substituted between T and the backend's subsequent execution,
  the substitution risk this test probes is simply unaddressed.
- **Family H (combinations drawn only from C/D/E/F-narrow):** combining
  evidence classes that each introduce no new independent security property
  does not manufacture one. This is **not** a general "weakest member" rule:
  a combination including A, A2, B, or a future independently sufficient
  externally-rooted proof is judged by that component's own eventual
  resolution.

## R7. Adversarial evidence matrix

Extends ADR 0005 §28's format to the families audited here. No row produces
`security_continuity=trusted`, because no mechanism is selected; the matrix
records what each family's evidence *would* have shown, had one been
implemented, to make the unresolved/negative finding falsifiable rather than
asserted. Every Family A2 cell reads **UNRESOLVED / NOT AUDITED HERE**. Row
2's T3 scenario is supplementary, non-load-bearing context for every family.

| # | Scenario | Family A (single-node witness) | Family A2 (distributed witness) | Family B (task history) | Family G (ephemeral) |
| --- | --- | --- | --- | --- | --- |
| 1 | Ordinary destroy+recreate via API/CLI, same node as the watcher | Config-location applicability is symmetric in principle, but even ordinary create/destroy operation-to-authoritative-event coverage and Path A delivery completeness remain **UNRESOLVED** — **unverified**, not trusted | **UNRESOLVED / NOT AUDITED** — depends on unproven operation-to-event coverage and Path A completeness at whichever node executed the operation | **Stateless demonstration (R6):** task record may appear on either audited list surface if still retained; exact-UPID status/log retention after list disappearance is unaudited; sufficiently many later ordinary tasks can age it out with no native cursor proving anything was missed. Whether a *stateful* sentinel-tracking design would detect this as a gap is **UNRESOLVED**; **unverified**, not trusted | No *persisted* state exists to be stale; each future mutation still needs its own safe point-in-time target proof — not "unaffected" in any stronger sense |
| 1a | Ordinary destroy+recreate via API/CLI, on a node *other than* the watcher | **UNRESOLVED, not a confirmed blind spot.** Whether this goes unobserved depends on the unproven cross-node delivery question (R3.3) — the bounded five-file search covered FUSE cache-invalidation calls only, which is neither exhaustive nor the proof target for remote `fsnotify` delivery | **UNRESOLVED / NOT AUDITED** — exactly the case a distributed design is meant to address, but coverage/gap semantics for it were never designed | Surface A is cluster-wide; Surface B and exact-UPID reads are node-scoped. A stateful design must prove exact per-node ownership/routing semantics | Same as row 1 |
| 2 | Occupant replacement via combined direct `pmxcfs`+storage write (T3), no explicit anchor — **supplementary context only, never load-bearing** | A direct local `unlink()`/`write()` is itself an ordinary Path A (local VFS) operation (R3.2); whether a co-resident watcher's `fsnotify` subscription actually observes it is not evaluated. **T3 / out of scope for this anchor-less candidate** — local syscall delivery is not evaluated as a security guarantee against a root actor, because root can suppress, patch, or feed fabricated events to a co-resident witness regardless. **No in-scope verdict is derived from this row.** | **UNRESOLVED / NOT AUDITED** — whether a distributed watcher incidentally observes this (a root actor's local `rm` is still a local syscall on *some* node) is not verified or claimed | No task is generated for this direct-root path (R2) — real, but **supplementary/out-of-scope context only**, never part of Family B's classification; row 1 is Family B's actual demonstration | No *persisted* trust to silently inherit — does not mean the underlying substitution is detected or prevented |
| 3 | Clone to a new VMID (class N) | New locator, new `resource_id`, `unverified`, regardless of family (ADR 0001 row 6); the **source** is not invalidated merely because a clone was made. A future mechanism must still ensure copied evidence cannot grant the *target* trust | Same | Same | Same |
| 4 | Snapshot rollback (class P) | **The same `resource_id` and active binding are retained** (ADR 0001 row 5); the continuity proof must be revoked/revalidated on that same resource, and the rollback itself must **not** mint a successor `resource_id` or close the binding. Must revoke per ADR 0005 §17 if a mechanism ever exists; nothing is granted here. Also depends on unresolved operation-to-event coverage — whether rollback necessarily produces a `pmxcfs`-level event to act on at all is UNRESOLVED (R3.6), independent of the revoke-on-detection requirement itself | Same, plus the same operation-to-event question at every node | Same (ADR 0005 §17) — task history's own rollback-detection question is a task-*generation* question instead (R2) | No persisted state to revoke — the rollback-substitution risk itself is unaddressed by this family |
| 5 | Node migration (class T) | `resource_id` preserved; requires explicit coverage/handoff handling, never replacement semantics; not solved by witness presence alone; whether migration off the watched node defeats coverage is exactly the unresolved cross-node question, not a settled fact | **UNRESOLVED / NOT AUDITED** — distributed node-migration semantics never designed | **UNRESOLVED:** Surface A is cluster-wide, while Surface B and exact-UPID reads are node-scoped; a stateful design needs exact per-node ownership, routing, handoff, and migration coverage semantics | Unaffected — no persisted trust to carry across a migration |
| 6 | Witness/backend/node restart | Must fail closed; no witness is implemented | **UNRESOLVED / NOT AUDITED** — multi-node restart/coordination semantics never designed | **UNRESOLVED** — a *stateful* sentinel-tracking design's own restart/gap semantics were never designed here; restart is not claimed to be automatically safe for that design | Unaffected — no persisted coverage claim exists to lose |
| 7 | Source-attestation epoch bump | Prior-epoch evidence becomes authority-ineligible **if its authority depends on source trust-domain continuity** (ADR 0003); a source-independent proof does not inherit that dependency | Same conditional rule | Same conditional rule | Same conditional rule, if evidence were collected at check time |
| 8 | Compromised node/hostd (T4) | Out of scope; assumed away — row 2, for an anchor-less witness, is this same category of failure, not a distinct lesser one | Out of scope; assumed away — applies per-node, at every node in the distributed fleet | Same | Same |
| 9 | A stateful task-history witness's overlap sentinel: enough further tasks occur that the sentinel itself ages out of the retained set before the gap is detected | N/A — not a `pmxcfs`-witness scenario | N/A | **UNRESOLVED / NOT DESIGNED OR AUDITED HERE** — whether the sentinel protocol's own gap-detection can itself be starved by the same bounded retention it relies on is one of the open questions left unresolved | N/A |

## R8. Detailed unanswered research questions

ADR 0006 §14 carries a compact version of these. This section holds the full
detail. None of these is closed here.

1. **The design of a clone-resistant/externally-rooted continuity mechanism**
   remains undesigned. Task-history lifecycle evidence and both
   `pmxcfs`-filesystem-witness variants all remain **UNRESOLVED** — not
   "NO-GO", and not "fails under stock capabilities"; neither phrase
   accurately describes any of the three audited designs. No replacement
   design is proposed for any of them.
2. **Whether a genuinely hardware-rooted, non-clonable, per-guest (not
   per-node) attestation primitive could ever exist** for QEMU/LXC on
   commodity virtualization hardware is unresolved; stock Proxmox VE provides
   no such primitive today (R5 rows C/D).
3. **Family G (operator per-mutation re-attestation / ephemeral trust).**
   Whether this should be pursued as a deliberate design choice for a future
   mutation-authority ADR is not decided. If it is ever pursued, that future
   ADR must state all of the following — none of which Family G gets for free
   merely by avoiding persistent state:
   - **operator confirmation by itself does not satisfy Blocker B** — a human
     saying "this is trusted" at time T is not continuity proof for time
     T+ε, for the identical reason ADR 0005 §8 already rejects bare operator
     assertion;
   - **CAS (`resource_id`/`binding_id`/`locator_generation`/
     `resource_continuity_revision`) prevents replay of a stale *backend
     decision*, not physical/logical occupant substitution** — ADR 0001
     explicitly permits those tokens to remain unchanged across an
     observationally invisible same-slot delete/recreate (ADR 0001 row 10),
     so a CAS match alone does not prove the operator's confirmation still
     refers to the occupant actually being mutated;
   - **a mutation model that never requires persistent
     `security_continuity=trusted` would itself require a separate
     architecture change** to ADR 0001/0005's accepted mutation-precondition
     formula (ADR 0001's policy-applicability intersection includes "trusted
     security continuity" as one of its required terms) — not something ADR
     0006 or a Family-G choice can make by implication;
   - **any such future model would still need its own safe point-in-time
     target-identity proof and/or fencing/serialization contract** closing
     the gap between "operator confirms occupant A" and "backend executes
     against whatever currently occupies the slot".
4. **Exact node-to-syscall dispatch behavior** when a PVE API command targets
   a guest whose owning node differs from the node the request was issued
   against is **UNKNOWN** at FACT-DOC/FACT-SOURCE strength and would need
   independent verification before any future ADR relies on it.
5. **Surface A/B parameter pinning.** Surface A's exact parameters are
   **FACT-SOURCE**, confirmed directly against `PVE::RESTEnvironment`
   (`fork_worker`, `active_workers`, `broadcast_tasklist`),
   `PVE::Service::pvestatd`, and `PVE::Cluster`: `MAX_FINISHED = 25` bounds
   only how many *finished* tasks `active_workers()` appends to its own
   pre-broadcast list and is **not** a cap on that list's total size; running
   tasks are retained unconditionally there; `broadcast_tasklist()`'s
   independent 32 KiB serialized-payload truncation is applied next,
   `pop`ping entries — running or finished — until the payload fits, so the
   published Surface A can differ from, and be smaller than, the
   pre-broadcast output; the cache is updated through confirmed paths that
   include worker start (`fork_worker`), worker completion
   (`log_task_result`), *and* `pvestatd`'s separate 10-second periodic cycle
   (`my $updatetime = 10`), with no claim that these three are exhaustive;
   and the executable truncation threshold is **32 KiB**
   (`while ($size >= (32 * 1024))`), not 128 KiB — the 128 KiB
   `CFS_MAX_STATUS_SIZE` figure is only a
   `# TODO: update to 128 KiB in PVE 8.x` comment. Surface B's
   `index`/`index.1` rotation-on-fixed-size behavior (`maxsize = 50000`) is
   likewise **FACT-SOURCE**. What remains corroborated only at forum strength
   is the *further* archive-file rotation beyond `index.1` (~512 KiB per
   archive file, ~20 archive files retained, on the order of ~100,000 total
   entries) — re-verify against the exact deployed PVE release before citing
   as a bound, and re-verify the 32 KiB / `MAX_FINISHED=25` figures too,
   since the TODO comment shows Proxmox itself intends to change at least one
   of them.
6. **Whether Proxmox will ever ship a documented, monotonic, gapless,
   officially-retained cluster-wide event stream** (closing ADR 0002's own
   Class B gap) remains unknown and outside Hubinet Ops's control; no
   assumption is made that it will.
7. **Broader host-rooted witness classes — genuinely unresolved, not
   disproven.** This research audited two task-list surfaces (R1),
   acknowledged exact-UPID reads without auditing their
   retention/usefulness (R1.1), and audited a **single-node** variant of
   `pmxcfs` filesystem-level change observation (R3). It did **not**
   primary-source audit, and reaches no conclusion about:
   - a **stateful, fail-closed overlap-sentinel task-history witness** (R6,
     item 12 below);
   - a **distributed, per-node `pmxcfs` watcher (Family A2)** — depending on
     Path A's unproven completeness at every node and on undesigned
     multi-node coverage/gap semantics;
   - the Linux kernel audit subsystem / `auditd` rules watching specific
     syscalls against `pmxcfs`/guest storage paths;
   - LSM (SELinux/AppArmor-class) hooks;
   - `eBPF`-based syscall/tracepoint interception;
   - storage-layer block-change tracking (e.g. ZFS/LVM snapshot-diffing) as
     an independent occupant-substitution witness;
   - a witness deliberately backed by an explicit root-resistant/external
     trust anchor rather than ordinary co-resident-process trust;
   - Family F's broader externally-rooted/out-of-band per-workload identity
     class.

   Any of these could, in principle, observe an actual physical/logical
   occupant replacement through a channel a node-root actor cannot as easily
   bypass as the audited designs — or could fail for reasons not examined
   here. A future ADR auditing one of these must perform its own
   primary-source research and its own pass against ADR 0006 §5's required
   property and same-slot witness test. No NO-GO on the audited classes
   exists for such an ADR to inherit.
8. **`pmxcfs` notification absence — bounded, not exhaustive, and not the
   Path-B proof target.** The finding that no `fuse_lowlevel_notify_*`-class
   call exists is confirmed across five core `src/pmxcfs/` files (`pmxcfs.c`,
   `server.c`, `dfsm.c`, `memdb.c`, `cfs-plug-memdb.c`), not the entire
   repository — `cfs-plug.c`, `cfs-plug-link.c`, `cfs-plug-func.c`,
   `cfs-utils.c`, `database.c`, `status.c`, `loop.c`, and `dcdb.c` remain
   unchecked at this citation granularity. Two limits: first, this absence
   finding bears only on Path B — it says nothing about Path A; second, the
   primitives searched for are FUSE **cache-coherency** calls (reverse
   inode/entry invalidation), which are **not** an fsnotify-propagation
   mechanism, so even for Path B the search was never the exact proof target.
   Whether a remote mutation can deliver an `fsnotify`/`inotify` event to a
   local watcher at all requires R3.3's four-step positive audit and is
   **UNRESOLVED**; upstream Linux
   `26260251022fbc2f248a3d747a9b2b961b18d2d8` exposes ordinary invalidation
   notification codes (FUSE protocol 7.45) with no `FUSE_FSNOTIFY` opcode
   found, but that is time-bound supporting evidence, not a NO-GO. Path A's
   actual completeness for `pmxcfs` specifically remains its own separate
   open question — **UNKNOWN**, not answered by the notify-callback absence
   finding at all. Where an accepted Path A completeness contract would have
   to come from — an upstream normative Proxmox guarantee, a separately
   reviewed and version-pinned source-level contract, or some other mechanism
   — is deliberately left open; this research does not introduce "official
   upstream documentation is the only acceptable security proof" as a rule.
9. **Which node executes a given operation's local syscall** — directly
   relevant to both Family A (single-node) and Family A2 (distributed).
   Whether PVE's API-proxy-to-owning-node pattern guarantees a predictable,
   single node executes a given guest's config-lifecycle syscall — and
   whether that node can ever differ from wherever an operator issued the
   request — is **UNKNOWN** at FACT-DOC/FACT-SOURCE strength. This is
   precisely what determines whether a single-node witness has any chance of
   coincidentally covering a given operation, and what a distributed
   witness's node-count/placement would need to guarantee.
10. **Whether the single-node `pmxcfs` watcher (Family A) is ultimately
    sufficient, insufficient, or itself further inconclusive.** A genuinely
    conclusive audit would need to close **at least**: Path A's own same-node
    completeness (item 8 classifies this as UNKNOWN, a distinct gap from Path
    B's finding); Path B's actual proof target — the exact kernel/userspace
    mechanism by which a behind-the-mount change could deliver an
    `fsnotify`/`inotify` event on the exact supported PVE kernel/FUSE
    release, which is *not* answered by the checked cache-invalidation
    absence; node-dispatch predictability (item 9); and **operation-to-event
    coverage** (R3.6) — not established for **any** continuity-relevant
    workflow at the required security-contract strength, a question distinct
    from and prior to Path A/B delivery — plus any further gap a resulting
    design's own research exposes.
11. **T3-consistency.** The rule that T3 must never be the load-bearing
    NO-GO reason, nor a mandatory in-scope property, for an anchor-less
    witness must be propagated consistently through every statement that
    touches direct `pmxcfs` manipulation. A future review should re-scan for
    this failure mode whenever new material referencing T3/direct-`pmxcfs`
    access is added. Note in particular that ordinary PVE admin API privilege
    (T2) is **not** the same as host-root shell access (T3), and the two must
    not be conflated.
12. **Whether task history (Family B) — via a stateful, fail-closed
    overlap-sentinel design — is ultimately sufficient, insufficient, or
    itself further inconclusive.** The specific open questions a genuinely
    conclusive audit would need to close (R6) include at least:
    - whether Surface B's archive-branch retained-set behavior is
      sufficiently prefix/append-structured for an overlap sentinel to prove
      continuous coverage;
    - whether Surface A can participate safely or should be ignored, given
      its truncating, non-append-only nature;
    - whether exact-UPID status/log remains readable after list
      disappearance, how its task-file/log retention relates to `node_tasks`
      retention, and whether direct reads provide any useful completeness or
      prefix/gap semantics;
    - complete-fetch/pagination/concurrent-rotation semantics;
    - per-node sentinel ownership;
    - node join/removal/restart behavior;
    - how initial enrollment establishes the first trustworthy overlap point;
    - whether exact UPID uniqueness is sufficient for the sentinel role;
    - whether every in-scope T1/T2 continuity-relevant operation reliably
      generates the required task worker record for both QEMU and LXC;
    - whether the chosen credential has complete task visibility for every
      relevant actor and node, and how permission/ACL changes invalidate or
      revalidate that coverage — noting ADR 0002's existing limit that
      identical before/after ACL state does **not** prove the ACL was
      unchanged during the interval, so point-in-time revalidation alone is
      not interval-wide visibility proof;
    - version-pinning/upgrade behavior;
    - whether a coverage gap could occur while an old sentinel nonetheless
      remains visible (a false negative for the gap-detection itself).

    ADR 0002's own existing **UNKNOWN** classification for trusted
    destroy/create event-chain evidence (requiring an accepted contract
    guaranteeing contiguous event/cursor semantics) is the correct posture to
    carry forward, not a claim of impossibility.

## R9. Sources / evidence pins

Read in the sessions noted (August 2026), in addition to the ADR
0001/0002/0003/0005 sources they build on. Findings pinned to upstream
mailing-list activity are current only as of the date noted; a future ADR
relying on them must re-verify against the then-current kernel/PVE release
rather than citing this document's date as still current.

The R3 sources (`pmxcfs`/FUSE/`inotify`) bear on **Path B** (cross-node,
Corosync-replicated delivery) only — none of them establishes, or is cited to
establish, anything about Path A (same-node, locally-originated VFS
`fsnotify` delivery), which remains open. **They are also not, on their own,
a Path-B proof target:** the `pmxcfs` sources show the absence of FUSE
*cache-invalidation* calls in the checked files, which is a different
mechanism from `fsnotify` event propagation. The R1 sources are a separate
research thread (the two audited PVE task-list surfaces plus acknowledged,
unaudited exact-UPID child reads) and are not part of that Path A/B
distinction.

**R1 — task-list surfaces, authorization filtering, and acknowledged
exact-UPID child reads:**

- [`proxmox/pve-manager`, `PVE/API2/Cluster.pm` at `14a22df…`](https://github.com/proxmox/pve-manager/blob/14a22df35955d97dfc1af21e117dc894a29df0c9/PVE/API2/Cluster.pm) — `GET /cluster/tasks`, its no-cursor result, and `Sys.Audit` on `/` versus own-task filtering
- [`proxmox/pve-manager`, `PVE/API2/Tasks.pm` at `14a22df…`](https://github.com/proxmox/pve-manager/blob/14a22df35955d97dfc1af21e117dc894a29df0c9/PVE/API2/Tasks.pm) — `source=archive|active|all`; archive `index`/`index.1`; the active-state read; node-scoped `Sys.Audit`/owner filtering; and exact-UPID status/log owner/`Sys.Audit` checks. Exact-UPID retention and sentinel usefulness remain **UNRESOLVED / NOT AUDITED HERE**
- [`proxmox/pve-cluster`, `src/PVE/Cluster.pm` at `7091d92…`](https://github.com/proxmox/pve-cluster/blob/7091d92e594952dba65c1e57568b3d7cc244e960/src/PVE/Cluster.pm) — Surface A's corosync-distributed status cache and executable 32 KiB broadcast truncation
- [`proxmox/pve-manager`, `PVE/Service/pvestatd.pm` at `14a22df…`](https://github.com/proxmox/pve-manager/blob/14a22df35955d97dfc1af21e117dc894a29df0c9/PVE/Service/pvestatd.pm) — the additional 10-second `active_workers()`/broadcast refresh path
- [`proxmox/pve-common`, `src/PVE/RESTEnvironment.pm` at `f665029e…`](https://github.com/proxmox/pve-common/blob/f665029eac78022e81810ab2e44eace57ade13fb/src/PVE/RESTEnvironment.pm) — UPID encoding; `active_workers()` reading/writing the same node-local `active` state consumed by Surface B active/all; `MAX_FINISHED=25`; worker-start/completion broadcasts; and `index`/`index.1` rotation

**R2 — QEMU/LXC same-slot create/destroy task-generation witness:**

- [`proxmox/qemu-server`, `src/PVE/API2/Qemu.pm` at `e6352be…`](https://github.com/proxmox/qemu-server/blob/e6352be67f70042a7433a3a3c712b36d02f9f7cb/src/PVE/API2/Qemu.pm) — QEMU create's `qmcreate` worker and destroy's `qmdestroy` worker
- [`proxmox/pve-container`, `src/PVE/API2/LXC.pm` at `c813255…`](https://github.com/proxmox/pve-container/blob/c8132559faedb76a56498d411bf3e024c1ff07e7/src/PVE/API2/LXC.pm) — LXC `vzcreate`/`vzdestroy` worker dispatch
- This is the specific, source-verified same-slot (class R) witness relied on
  in R2/R6; clone and restore-to-new-locator (class N), snapshot rollback and
  same-resource restore (class P), and migration (class T) were **not**
  re-derived at this citation granularity and remain UNKNOWN at complete
  QEMU/LXC parity

**R3 — `pmxcfs`/FUSE/`inotify` (Path A/B only, per the note above):**

- [`proxmox/pve-cluster`, `src/pmxcfs/pmxcfs.c` at `7091d92…`](https://github.com/proxmox/pve-cluster/blob/7091d92e594952dba65c1e57568b3d7cc244e960/src/pmxcfs/pmxcfs.c) — FUSE operations table; no kernel-notification callback
- Upstream Linux at revision `26260251022fbc2f248a3d747a9b2b961b18d2d8`:
  `include/uapi/linux/fuse.h` (FUSE protocol 7.45;
  `FUSE_NOTIFY_INVAL_INODE`/`FUSE_NOTIFY_INVAL_ENTRY` and other notification
  codes present; **no `FUSE_FSNOTIFY` opcode found**), `fs/fuse/notify.c`
  (`fuse_notify_inval_inode -> fuse_reverse_inval_inode`;
  `fuse_notify_inval_entry -> fuse_reverse_inval_entry`), and `fs/fuse/dir.c`
  (reverse entry invalidation performing
  `d_invalidate`/`fuse_invalidate_entry_cache`-class cache/dentry
  invalidation). Cited as **time-bounded supporting evidence** that the
  existing FUSE notification codes are cache-coherency primitives rather than
  an fsnotify-propagation protocol — **not** as a permanent NO-GO
- At the same verified `7091d92…` revision, [`server.c`](https://github.com/proxmox/pve-cluster/blob/7091d92e594952dba65c1e57568b3d7cc244e960/src/pmxcfs/server.c), [`dfsm.c`](https://github.com/proxmox/pve-cluster/blob/7091d92e594952dba65c1e57568b3d7cc244e960/src/pmxcfs/dfsm.c), [`memdb.c`](https://github.com/proxmox/pve-cluster/blob/7091d92e594952dba65c1e57568b3d7cc244e960/src/pmxcfs/memdb.c), and [`cfs-plug-memdb.c`](https://github.com/proxmox/pve-cluster/blob/7091d92e594952dba65c1e57568b3d7cc244e960/src/pmxcfs/cfs-plug-memdb.c) contain no FUSE/kernel invalidation-notification call. `memdb.c` does contain GLib's unrelated `GDestroyNotify` type name, so this is not a claim of zero textual `notify` substrings. Unchecked files remain UNKNOWN, and the finding is about those cache-invalidation primitives, not about fsnotify delivery
- [Proxmox Cluster File System (pmxcfs) documentation](https://pve.proxmox.com/pve-docs/chapter-pmxcfs.html) — `pmxcfs` architecture, `/etc/pve` mount, Corosync-backed replication
- Linux kernel mailing list, [RFC PATCH 0/7] Inotify support in FUSE and virtiofs (originally posted ~October 2021, `https://lkml.kernel.org/linux-fsdevel/YYMNPqVnOWD3gNsw@redhat.com/t/`) — RFC-stage status of FUSE `inotify` support
- Linux kernel mailing list / `virtiofsd` issue tracker, discussion on
  disallowing `inotify` watches on unsupported filesystems and on
  FUSE/`virtiofs` `inotify` support, with activity as recent as **May 2025**
  indicating the feature remained unmerged in the mainline kernel as of that
  discussion — `inotify_add_watch()` silently succeeding without delivering
  events on unsupported filesystems. **INFERENCE / upstream-status evidence,
  not FACT-SOURCE:** this is mailing-list/issue-tracker discussion, not source
  code independently confirmed here; time-bound
- Community-reported (forum-strength, not independently re-derived from
  source) further archive-file rotation figures beyond `index.1`, cited only
  as corroborating context, not as a load-bearing claim

Repositories on GitHub are official read-only mirrors; the authoritative
upstream remains [git.proxmox.com](https://git.proxmox.com/). Conclusions
about what a given behavior does *not* guarantee are architectural inferences
from the cited contract and source, not a claim of an additional Proxmox
guarantee. Any future positive mechanism ADR must re-check and pin the exact
revisions it supports.
