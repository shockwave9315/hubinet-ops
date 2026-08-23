# NON-NORMATIVE RESEARCH / EVIDENCE

# Blocker B Family-B stateful PVE task-witness feasibility

Status: research only

Research date: 2026-08-22
Research execution base (exact `main` when this research ran):
`e092eb14e73ff9e4ab76200bc682c67ca665d056`
Current integration base after branch sync:
`9282ace21c91f5b4ea04b62d7d82b631a0a5ddaa` (the PR #47 merge)

These two are **not** the same commit and must not be conflated: the second is
only where this branch is now rebased for integration, not where the research
was executed.

This document:

- does not amend any accepted ADR;
- does not authorize implementation;
- does not close Blocker B;
- does not authorize WAVE B1;
- does not grant `trusted`;
- does not unblock Phase 1C; and
- does not change R0 from read-only operation.

Evidence authority remains:

```text
accepted architecture
> AGENTS.md
> prior research/evidence
> observations made during this research
```

The accepted same-slot adversary and fail-closed rules are not weakened to make
this candidate work. Transport failures in the research environment are not
evidence for or against Family B.

Evidence labels used below:

- **FACT-DOC** — stated by an accepted architecture document or official
  upstream documentation.
- **FACT-SOURCE** — supported by an identified upstream source revision.
- **INFERENCE** — a bounded conclusion from the cited facts.
- **UNKNOWN** — not established at the strength needed by the contract.
- **PREVIOUSLY PINNED / VERIFIED IN ADR0006** — accepted ADR 0006's supporting
  research reviewed the exact revision; this pass did not silently re-pin it.
- **REVERIFIED THIS RESEARCH PASS** — the exact fact or mapping was independently
  checked for this pass.
- **CURRENT-RELEASE MAPPING UNKNOWN** — an older exact source fact exists, but
  its identity with the installed/current release was not established.

## 1. Question / scope

The question is not whether Hubinet-Ops can call `GET /cluster/tasks`. It is:

> Can a durable, stateful, fail-closed witness prove a complete task-history
> interval across polling, process restart, retention, archive rotation,
> pagination, node routing, and authorization changes, so that an invisible
> same-VMID replacement cannot preserve stale trusted authority?

The candidate protocol may sacrifice availability. If it cannot prove an
interval complete, it must produce a coverage gap before authority can be
consumed. Silence is evidence only inside an independently proven, complete,
gapless interval.

This pass covers ordinary supported PVE management paths. ADR 0006's T3 direct
root bypass is outside the anchor-less Family-B claim and is not used to decide
this result.

The controlling scenario is:

1. trusted logical occupant A exists at VMID X;
2. A disappears between Hubinet-Ops observations;
3. a different occupant B appears at VMID X; and
4. B can reproduce config, disks, names, tags, MACs, ordinary metadata, and
   guest-visible state.

The witness is acceptable only if positive evidence detects replacement, or a
coverage uncertainty makes authority ineligible before stale trust is used.
Snapshot equality and repeated identical polling results are never continuity
proof.

## 2. Current environment / source pins

### 2.1 Repository and operational prerequisite

- **FACT-SOURCE:** this research branch was created from, and the research was
  executed against, exact `origin/main` commit
  `e092eb14e73ff9e4ab76200bc682c67ca665d056`.
- **CORRECTED PROVENANCE.** An earlier revision of this section asserted that
  "R0 operational closure was merged by PR 43 before this branch was created."
  That statement is **false** and is withdrawn. PR #43 did **not** record final
  R0 operational closure. It recorded a roughly 7-hour R0 checkpoint whose
  decision at that time was **GO WITH OBSERVATION**; the recommended >=24-hour
  observation window was not yet complete, and the optional/synthetic
  operational observations remained open.
- **PROCESS / PROVENANCE DEFECT.** This research execution began at
  `e092eb14`, before PR #47 existed. The task-level prerequisite under which the
  research was originally started -- a completed R0 operational closure -- was
  therefore **not satisfied at research-execution time**. That sequencing defect
  is recorded here rather than hidden or rewritten.
- **SCOPE OF THAT DEFECT.** It is a defect of research provenance and
  sequencing, not of the source reading itself. It does not by itself invalidate
  the factual source observations established by this research. It does **not**
  strengthen the Family-B result, does **not** close Blocker B, does **not**
  authorize WAVE B1, and does **not** unblock Phase 1C.
- **LATER REPOSITORY STATUS -- NOT A RETROACTIVE FIX.** PR #47 subsequently
  merged into `main` as `9282ace21c91f5b4ea04b62d7d82b631a0a5ddaa`, onto which
  this branch has since been rebased. That commit records: R0 bootstrap
  **PASS**; Home Assistant enrollment/acceptance **PASS**; the recommended
  >=24-hour observation **COMPLETE**; node migration **N/A** for the real
  single-node topology; CT110 reboot / normal restart behavior **OBSERVED**;
  abnormal-stop stranded-run fencing still **UNEXERCISED / UNRESOLVED**; a
  current operational decision that remains **GO WITH OBSERVATION** and is not a
  plain §9 GO; exactly five operational observations still open; and a common
  re-check date of 2026-08-23. It is cited here **only** as a later
  repository-status fact. It did not exist when this research ran, and it does
  not retroactively satisfy the prerequisite described above.
- **FACT-DOC:** the repository implementation status records the prior dogfood
  host as one node running `pve-manager 9.2.11`, `pve-cluster 9.1.6`, and kernel
  `7.0.14-12-pve`. These are prior repository evidence, not a new live query.

### 2.2 Upstream revision ledger

| Component | Revision | Evidence status in this pass | Use in this document |
|---|---|---|---|
| `qemu-server` 9.2.6 | `e6352be67f70042a7433a3a3c712b36d02f9f7cb` | **REVERIFIED THIS RESEARCH PASS**: repository, version meaning, and `src/PVE/API2/Qemu.pm` path independently verified | Current QEMU create/destroy route evidence; other files are named only where supported |
| `pve-manager` | `14a22df35955d97dfc1af21e117dc894a29df0c9` | **PREVIOUSLY PINNED / VERIFIED IN ADR0006**; mapping to installed `9.2.11` is **CURRENT-RELEASE MAPPING UNKNOWN** | `/cluster/tasks`, per-node task lists, exact-UPID routes, `pvestatd` publication |
| `pve-cluster` | `7091d92e594952dba65c1e57568b3d7cc244e960` | **PREVIOUSLY PINNED / VERIFIED IN ADR0006**; mapping to installed `9.1.6` is **CURRENT-RELEASE MAPPING UNKNOWN** | cluster task-list cache behavior |
| `pve-common` | `f665029eac78022e81810ab2e44eace57ade13fb` | **PREVIOUSLY PINNED / VERIFIED IN ADR0006**; current package mapping **UNKNOWN** | worker publication, archive rotation, UPID encoding |
| `pve-container` | `c8132559faedb76a56498d411bf3e024c1ff07e7` | **PREVIOUSLY PINNED / VERIFIED IN ADR0006**; current package mapping **UNKNOWN** | LXC create/destroy task evidence |
| `pve-storage` | none established | **CURRENT-RELEASE MAPPING UNKNOWN** | Storage-operation coverage remains unknown |

The relevant prior exact files are:

- [`PVE/API2/Cluster.pm` at `14a22df...`](https://github.com/proxmox/pve-manager/blob/14a22df35955d97dfc1af21e117dc894a29df0c9/PVE/API2/Cluster.pm)
- [`PVE/API2/Tasks.pm` at `14a22df...`](https://github.com/proxmox/pve-manager/blob/14a22df35955d97dfc1af21e117dc894a29df0c9/PVE/API2/Tasks.pm)
- [`PVE/Service/pvestatd.pm` at `14a22df...`](https://github.com/proxmox/pve-manager/blob/14a22df35955d97dfc1af21e117dc894a29df0c9/PVE/Service/pvestatd.pm)
- [`src/PVE/Cluster.pm` at `7091d92...`](https://github.com/proxmox/pve-cluster/blob/7091d92e594952dba65c1e57568b3d7cc244e960/src/PVE/Cluster.pm)
- [`src/PVE/RESTEnvironment.pm` at `f665029...`](https://github.com/proxmox/pve-common/blob/f665029eac78022e81810ab2e44eace57ade13fb/src/PVE/RESTEnvironment.pm)
- [`src/PVE/API2/Qemu.pm` at qemu-server 9.2.6](https://github.com/proxmox/qemu-server/blob/e6352be67f70042a7433a3a3c712b36d02f9f7cb/src/PVE/API2/Qemu.pm)
- [`src/PVE/API2/LXC.pm` at `c813255...`](https://github.com/proxmox/pve-container/blob/c8132559faedb76a56498d411bf3e024c1ff07e7/src/PVE/API2/LXC.pm)

The Windows Schannel `SEC_E_NO_CREDENTIALS (0x8009030e)`, an HTTP 403 from one
`git.proxmox.com` fetch path, and an in-app browser sandbox-policy error were
research-tool transport failures. They caused no repository/configuration
change and have no bearing on the classification. Exact current mappings not
otherwise established are left **UNKNOWN**.

## 3. Task-surface model

### 3.1 Surface A — cluster recent-task cache

| Property | Finding |
|---|---|
| API | `GET /cluster/tasks` |
| Implementation | `PVE::API2::Cluster::tasks`; data populated through `PVE::Cluster` and `PVE::Service::pvestatd` |
| Scope | Cluster aggregation of per-node published task status |
| Active source | Per-node `active_workers()` state before cluster publication |
| Finished source | Recently finished workers admitted into the same published list |
| Ordering | **FACT-SOURCE at prior pin:** returned recent task data is time-oriented, but no stable snapshot/cursor or predecessor relationship is exposed |
| Limits | **FACT-SOURCE at prior pin:** `MAX_FINISHED = 25` limits finished entries admitted before broadcast; separate 32 KiB broadcast truncation can remove running or finished entries |
| Pagination | No durable cursor or immutable page snapshot |
| Retention | Bounded recent cache, not durable history |
| Start publication | Worker start enters node-local active state and is broadcast; cluster observation is refresh/publication dependent |
| Completion publication | Worker completion updates active state and is broadcast; no gapless handoff contract was found |
| Authorization | Caller sees own tasks unless it has `Sys.Audit` at `/` |
| Anchor value | **INSUFFICIENT:** useful discovery/cache surface, not a coverage ledger |

**INFERENCE:** successful enumeration cannot prove that no record was truncated,
aged out, filtered, not yet aggregated, or hidden during an earlier interval.

### 3.2 Surface B active — node-local active workers

| Property | Finding |
|---|---|
| API | `GET /nodes/<node>/tasks?source=active` |
| Implementation | `PVE::API2::Tasks::node_tasks` using node-local active worker state from `PVE::RESTEnvironment` |
| Scope | One routed node |
| Filters | `userfilter`, `typefilter`, `vmid`, errors/status and time-related filters as implemented by `node_tasks` |
| Ordering | Derived from current active entries; not a durable sequence |
| Pagination | `start`/`limit`; no immutable snapshot token |
| Retention | Running/recent active state only |
| Start visibility | Worker start publication is source-confirmed at the prior pin |
| Completion visibility | Completion changes/removes active status and broadcasts; atomic visibility with archive append is **UNKNOWN** |
| Authorization | Own tasks or `Sys.Audit` on `/nodes/<node>` |
| Anchor value | **INSUFFICIENT alone:** a short task can start and finish between observations |

### 3.3 Surface B archive — node-local rolling archive

| Property | Finding |
|---|---|
| API | `GET /nodes/<node>/tasks?source=archive` (default) |
| Implementation | `PVE::API2::Tasks::node_tasks`; reverse reads of `/var/log/pve/tasks/index` and `index.1` |
| Scope | One routed node; completed history is node-local |
| Filters | `since`, `until`, user, type, VMID, errors/status, plus `start`/`limit` |
| Ordering | Reverse archive traversal, newest records first at the prior pin |
| Pagination | Offset/limit over a list that can change between requests; no cursor/snapshot generation |
| Retention | `index` rotates at the source `maxsize = 50000` threshold to `index.1`; bounds beyond the exposed pair are not asserted here |
| Start visibility | Not an archive property |
| Completion visibility | Completed worker is appended to archive; exact ordering/atomicity with active removal is **UNKNOWN** |
| Authorization | Own tasks or `Sys.Audit` on `/nodes/<node>` |
| Anchor value | **INSUFFICIENT alone:** filenames/offsets are mutable and API exposes no archive generation or loss marker |

### 3.4 Surface B all — active plus archive

`GET /nodes/<node>/tasks?source=all` combines the active and archive branches.
It increases observational opportunity but does not add a cursor, a stable
snapshot, an archive-generation identifier, or an interval-wide authorization
proof. Duplicate/handoff behavior during concurrent completion has not been
proven to form an atomic union. Its coverage value is therefore **UNKNOWN** and
its value as a standalone anchor is **INSUFFICIENT**.

### 3.5 Exact-UPID reads

| Property | Finding |
|---|---|
| APIs | `GET /nodes/<node>/tasks/<upid>/status` and `/log` |
| Routing | Node encoded in/corresponding to the UPID route; task file must still exist |
| Authorization | Task owner or `Sys.Audit` on `/nodes/<node>` |
| Identity | UPID encodes node, PID, process start, task start time, type, ID, and user |
| Monotonicity | **No:** UPID is unique task identity, not a global/per-node counter and carries no predecessor/hash link |
| Retention | Readability after disappearance from `index`/`index.1` is **UNKNOWN** |
| Completeness | Re-reading a known UPID proves only that known task; it cannot prove no unknown task was omitted |
| Anchor value | **INSUFFICIENT alone**; potentially useful for reconciliation within some separately proven envelope |

### 3.6 Surface conclusions

- **FACT-SOURCE:** start and completion publication mechanisms exist.
- **FACT-SOURCE:** each audited list surface is bounded or mutable and lacks a
  native monotonic, gap-detectable cursor.
- **UNKNOWN:** active-to-archive handoff is atomically and completely observable.
- **UNKNOWN:** exact-UPID records remain readable long enough to close a missed
  list interval.
- **UNKNOWN:** current-release implementation is identical to every prior pin.
- **INFERENCE:** none of Surface A, Surface B, or exact-UPID reads is a proven
  sufficient coverage anchor by itself.

## 4. Operation -> task matrix

Classification is for the evidence actually established. `CONDITIONAL` means a
worker route is shown in official source history or a prior pin, but a
load-bearing condition such as exact current-release mapping, route coverage,
or operation variant remains unverified. No create/destroy fact is extrapolated
to another operation.

### 4.1 QEMU

| Operation | Classification | Task type / owner | Start and completion surfaces | Evidence and unresolved condition |
|---|---|---|---|---|
| Create | **CONFIRMED TASK GENERATED** | `qmcreate`; API execution node | Active at worker start; archive on completion; cluster cache subject to publication | Exact qemu-server 9.2.6 `Qemu.pm` pin |
| Destroy | **CONFIRMED TASK GENERATED** | `qmdestroy`; API execution node | Same | Exact qemu-server 9.2.6 pin and prior ADR0006 verification |
| Clone | **CONDITIONAL** | `qmclone`; initiating/source node | Expected normal worker surfaces | Official [source patch evidence](https://lists.proxmox.com/pipermail/pve-devel/2022-February/051570.html); exact 9.2.6 route re-verification incomplete |
| Snapshot create | **CONDITIONAL** | `qmsnapshot`; execution node | Expected normal worker surfaces | Official [source evidence](https://lists.proxmox.com/pipermail/pve-devel/2024-May/064010.html); exact current route/variants incomplete |
| Snapshot delete | **CONDITIONAL** | snapshot-delete worker shown in QEMU snapshot source history; execution node | Expected normal worker surfaces | Same source family; exact current task-type spelling and every route **UNKNOWN** |
| Snapshot rollback | **CONDITIONAL** | `qmrollback`; execution node | Expected normal worker surfaces | Official [source evidence](https://lists.proxmox.com/pipermail/pve-devel/2017-June/027087.html); current mapping incomplete |
| Backup restore | **UNKNOWN** | Expected restore worker, but exact current task type/owner not established | **UNKNOWN** | No exact current route was established in this pass |
| Migrate | **CONDITIONAL** | `qmigrate`; initiating/source node for the primary worker | Node-local active/archive on owner; possible helpers require audit | Official [source evidence](https://lists.proxmox.com/pipermail/pve-devel/2021-April/047598.html) and [task-return semantics](https://lists.proxmox.com/pipermail/pve-devel/2025-May/071076.html); exact current multi-node semantics incomplete |
| Disk import | **UNKNOWN** | `import-from` behavior exists, but standalone worker coverage is not established | **UNKNOWN** | Creation-time import cannot be generalized to every import path |
| Disk move/reassign | **CONDITIONAL** | `qmmove`; execution node | Expected normal worker surfaces | Official [move-disk source evidence](https://lists.proxmox.com/pipermail/pve-devel/2021-August/049717.html); current route and all storage variants incomplete |
| Disk attach/change | **UNKNOWN** | Some config changes may be synchronous or nested in another worker | **UNKNOWN** | No complete route-by-route audit |
| Config change affecting backing storage | **UNKNOWN** | Route/task behavior varies by property and storage action | **UNKNOWN** | No complete route-by-route audit |

### 4.2 LXC

| Operation | Classification | Task type / owner | Start and completion surfaces | Evidence and unresolved condition |
|---|---|---|---|---|
| Create | **CONDITIONAL** | `vzcreate`; API execution node | Normal active/archive worker surfaces at prior pin | Task generation **PREVIOUSLY PINNED / VERIFIED IN ADR0006**; current pve-container release mapping unknown |
| Destroy | **CONDITIONAL** | `vzdestroy`; API execution node | Same | Same current-release limitation |
| Clone | **UNKNOWN** | Commonly described as `vzclone`, but exact supported current route not source-established here | **UNKNOWN** | UI/task naming is not sufficient proof |
| Snapshot create | **CONDITIONAL** | `pctsnapshot`; execution node | Expected normal worker surfaces | Official [source evidence](https://lists.proxmox.com/pipermail/pve-devel/2016-March/019713.html); current mapping incomplete |
| Snapshot delete | **CONDITIONAL** | `lxcdelsnapshot`; execution node | Expected normal worker surfaces | Same source evidence/current limitation |
| Snapshot rollback | **CONDITIONAL** | `lxcrollback`; execution node | Expected normal worker surfaces | Same source evidence/current limitation |
| Backup restore | **CONDITIONAL** | `vzrestore` selected for restore versus `vzcreate` for create | Expected normal worker surfaces | Official [source evidence](https://lists.proxmox.com/pipermail/pve-devel/2019-January/035441.html); exact current route incomplete |
| Migrate | **CONDITIONAL** | `vzmigrate`; initiating/source node | Node-local worker surfaces; helpers/target semantics incomplete | Official [source evidence](https://lore.proxmox.com/pve-devel/20220928125059.1139296-4-f.gruenbichler%40proxmox.com/); current semantics incomplete |
| Rootfs/storage move | **CONDITIONAL** | `move_volume`; execution node | Expected normal worker surfaces | Official [source evidence](https://lists.proxmox.com/pipermail/pve-devel/2021-November/050609.html); current variants incomplete |
| Rootfs replacement/attach | **UNKNOWN** | Synchronous config update versus worker behavior not exhaustively audited | **UNKNOWN** | No route-by-route proof |
| Relevant config changes | **UNKNOWN** | Depends on the route/property | **UNKNOWN** | No route-by-route proof |

### 4.3 Matrix implications

No operation in this pass is classified **CONFIRMED NO TASK**. That does not
mean every operation generates a task; it means no such negative claim was
proven.

Current evidence establishes the controlling normal QEMU create/destroy pair,
but not the complete QEMU/LXC operation set at the installed release. Restore,
clone, rollback, migration, and backing-storage paths can preserve, replace,
move, or rewind workload state and are load-bearing. Their conditional/unknown
rows prevent a complete operation-coverage claim.

## 5. Stateful overlap model

### 5.1 Candidate durable state

A future design would at least need to atomically persist:

- immutable PVE source identity and its Hubinet source binding;
- stable per-node identity/binding, not only a reusable node name;
- last accepted per-node coverage boundary and local witness revision;
- a bounded recent set of exact UPIDs and their normalized facts;
- oldest/newest archive facts seen and the source/API version context;
- all known active tasks, including owner node and last state;
- an authorization-visibility state/epoch;
- unresolved task reads and explicit gap state; and
- the inventory/CAS context described in section 10.

On every cycle it would:

1. establish source/node/version and authorization preconditions;
2. read every node's active tasks;
3. traverse per-node archive history with overlap;
4. reconcile known exact UPIDs where useful;
5. prove overlap with the last atomically committed boundary;
6. prove pagination, retention, routing, handoff, and visibility completeness;
7. atomically commit the new boundary and witness revision; or
8. persist `GAP` before any authority dependent on the interval is consumable.

### 5.2 Candidate anchors

| Observable | Assessment | Reason |
|---|---|---|
| UPID identity | **INSUFFICIENT** | Deduplicates a known task but is not an ordered chain and says nothing about unknown omitted tasks |
| UPID task start timestamp | **INSUFFICIENT** | Wall-clock field, not a monotonic sequence/cursor; ties and clock changes do not prove adjacency |
| Surface B `start` offset | **INSUFFICIENT** | Offset is evaluated against a concurrently mutable newest-first result |
| Archive `index`/`index.1` position | **INSUFFICIENT** | API exposes neither immutable file generation nor rotation/loss marker |
| Exact-UPID re-read | **INSUFFICIENT** | Confirms one known task only; retention and completeness are unknown |
| Persisted recent-UPID set | **INSUFFICIENT alone** | A retained old sentinel can coexist with a newly hidden/omitted lifecycle task |
| Active-to-finished handoff | **UNKNOWN** | No source-backed atomic, gap-free list-observation contract established |
| Surface A publication revision | **INSUFFICIENT / absent** | No durable task-ledger revision or cursor is returned |
| All nodes plus sentinel overlap | **UNKNOWN** | Still depends on retention, snapshot pagination, authorization interval, and handoff proofs |

No concrete observable is **PROVEN SUFFICIENT**.

### 5.3 Research simulator

`scripts/research/blocker_b_task_witness_sim.py` is an offline deterministic
model, not production code. It never contacts PVE. It consumes `ACTIVE`,
`FINISHED`, `ARCHIVE_PAGE`, `ROTATE`, `RESTART`, `API_FAILURE`, `NODE_DOWN`,
`ACL_VISIBILITY_LOST`, `ACL_VISIBILITY_RESTORED`, and `TASK_UNKNOWN` events.

It returns `COVERAGE_COMPLETE` only if all independently supplied properties are
true:

- retained prefix/overlap is proven;
- pagination is a proven stable snapshot;
- authorization is proven throughout the interval;
- all relevant nodes are covered; and
- active/archive handoff is proven complete.

Any known uncertainty is sticky and produces `COVERAGE_GAP`. The simulator's
positive control demonstrates only that the logical protocol is internally
expressible if those properties exist. It does not prove stock PVE supplies
them. Focused tests ensure intentionally missing envelope properties cannot
produce `COVERAGE_COMPLETE`.

## 6. Gap conditions

| Scenario | Can success responses distinguish complete overlap from loss? | Required result |
|---|---|---|
| Sentinel absent from retained archive | Yes: overlap failed, although the missing cause is unknown | `GAP` |
| `index` -> `index.1` rotation while sentinel remains visible | **UNKNOWN:** retained sentinel does not prove no intervening record was omitted | `GAP` unless an independent generation/prefix proof exists |
| Sentinel and earlier history rotated out | Yes: no overlap | `GAP` |
| Task volume exceeds one offset page | No stable snapshot/cursor was found | `GAP` unless pages are independently frozen/proven |
| Task completes while pages are traversed | **UNKNOWN:** active/archive union atomicity not proven | `GAP` |
| New task appears between page reads | Offset drift can duplicate or skip records | `GAP` |
| Witness restarts between pages | In-memory traversal cannot be trusted; prior atomic boundary remains | Restart traversal; `GAP` if old overlap cannot be re-proven |
| API call fails or returns malformed/incomplete data | No | `GAP` |
| Node unreachable | No per-node completeness | `GAP` |
| Node set changes | Coverage membership changes without a proven node epoch | `GAP` |
| PVE node/service restarts | Task-state persistence/handoff semantics not established | `GAP` |
| Exact-UPID read returns not found | Cannot distinguish retention from unknown identity/route/visibility | `GAP` |
| Exact-UPID known task remains readable | Does not exclude an unknown missing task | Still needs independent completeness proof |
| Reader loses privilege and later regains it | Before/after equality cannot reveal hidden interval | `GAP` |
| Successful empty/identical response | Silence lacks a complete envelope | Never positive evidence by itself |

Thus the audited APIs can return HTTP success after a client missed evidence.
The currently identified fields do not always let the client distinguish that
case from complete overlap.

## 7. Authorization-visibility analysis

### 7.1 Exact privileges

- **FACT-SOURCE at prior pin:** Surface A exposes all users' tasks only with
  `Sys.Audit` at `/`; otherwise it filters to the caller's own tasks.
- **FACT-SOURCE at prior pin:** Surface B and exact-UPID status/log expose
  another owner's task only with `Sys.Audit` on `/nodes/<node>`.
- **INFERENCE:** `VM.Audit` does not substitute for these task-list checks.
- **INFERENCE:** lifecycle work initiated by root, another user, a token, HA,
  backup/migration services, or another node is incomplete under owner-only
  visibility.

The existing R0 bootstrap reader is configured with `{Sys.Audit, VM.Audit}` at
`/` and privilege separation enabled. That is useful point-in-time scope
evidence, not proof that the identity retained those effective privileges for
every instant in a past accepted interval.

### 7.2 Interval-wide visibility

Accepted ADR 0002 already rejects equal ACL snapshots as interval proof. An ACL
or token scope can undergo:

```text
allowed -> denied -> allowed
```

while a task is created and ages out of the reader's visible set. A later
successful response and equal before/after permissions do not expose the ABA.
Token/user privilege intersection, path scope, node membership, ownership, and
permission propagation all matter.

**Conclusion:** the audited PVE task APIs and mutable ACL snapshots do not prove
that the chosen reader had complete visibility throughout an interval. Under
the accepted architecture, every such unproven interval is a `GAP` and is
authority-ineligible.

This is a concrete blocker for a witness that relies only on ordinary mutable
PVE ACL point reads. It is not a **NO-GO WITH PROOF** for every conceivable
Family-B design: a structurally constrained local/root reader, an independently
audited authorization-change ledger, or an external trust boundary was not
designed or disproven here. That remaining alternative is **UNKNOWN**, not an
assumed solution.

## 8. Multi-node / migration

- Surface B active/archive and exact-UPID reads are node-routed.
- Completed archive history is node-local.
- A UPID includes its owning node, but the node component is routing context,
  not a proof of durable node identity.
- Surface A aggregates nodes but is bounded, asynchronously published, and
  non-durable; it cannot replace per-node coverage.
- Official QEMU/LXC source history places the primary migration worker on the
  initiating/source node. Target-side/helper task generation and failure
  semantics remain **UNKNOWN** at the exact current releases.

A generic witness therefore must query every member node and persist separate
coverage state per stable source-node binding. It must define fail-closed
semantics for node joins, disappearance, rejoin under a reused name, routing
failure, membership disagreement, migration helpers, and partial source/target
success.

If a node disappears, its interval cannot be declared complete merely because
cluster aggregation still succeeds. A newly joined or rejoined node begins
without accepted continuity until its identity and retained overlap are proven.

Migration is accepted class T behavior: it preserves `resource_id`; it is not a
replacement event. A witness must carry/invalidate coverage safely across the
source/target handoff without minting a successor identity. Current evidence
does not establish those semantics. A one-node prototype could test mechanics
but could not provide generic multi-node proof.

## 9. Restart behavior

| Event | Safe research-level behavior |
|---|---|
| Hubinet-Ops clean restart | Load only atomically committed state; re-establish source/node/auth context and overlap before accepting new coverage |
| Crash before coverage commit | Retain old committed boundary; repeat traversal; gap if overlap is unavailable |
| Crash after atomic coverage commit | Resume from committed boundary, still rechecking all context |
| Database restart/recovery ambiguity | Treat non-durable or ambiguous state as `GAP`; never reconstruct optimistically |
| Host reboot or long downtime | Attempt retained overlap; if the sentinel aged out or node context changed, `GAP` |
| Network partition/API failure | Immediately make dependent authority ineligible; successful recovery does not heal the hidden interval |
| PVE node reboot | `GAP` until node identity, active/archive persistence, and overlap are re-proven |
| PVE service restart | `GAP`; active-worker survival/archive handoff is **UNKNOWN** |
| Partial multi-node availability | Global/source coverage incomplete; `GAP` |

Persisted state can say “I cannot prove continuity” reliably. Current evidence
does not show that it can always say “nothing relevant occurred while I was
away.” Failing closed is acceptable availability behavior, but without a
provable recovery envelope the mechanism may remain permanently or frequently
gapped.

## 10. Same-slot adversarial evaluation

### 10.1 Positive evidence path

If both ordinary destroy and create records for VMID X are observed inside a
proven complete envelope, they are accepted positive replacement evidence for a
future mechanism to process under ADR 0001/0002 class R rules. Merely seeing a
new `qmcreate`/`vzcreate`, or merely seeing equality before/after, is not enough
without route, task, and binding reconciliation.

### 10.2 Uncertainty path

If either lifecycle record may have been filtered, skipped during pagination,
lost at rotation/handoff, owned by an uncovered node, produced during restart,
or not generated by the particular operation, the interval is a `GAP`.
Before any stale trust can be consumed, dependent security continuity must be
ineligible. A gap is never itself replacement evidence and does not invent a
successor resource.

### 10.3 Evidence binding and invalidation

Any future accepted coverage claim must bind to and be atomically rechecked at
consumption time with at least:

- exact `resource_id`;
- exact active `binding_id`;
- exact `locator_generation`;
- exact `resource_continuity_revision`;
- `source_attestation_epoch` when source attestation is a precondition;
- current node locator and stable source/node binding when node-scoped; and
- node binding/trust context whenever authority depends on it.

Invalidation triggers include any mismatch, inactive/closed binding, locator
reuse/change, continuity-revision advance, source epoch advance, source or node
rebind, node membership/routing ambiguity, reader-authorization ambiguity,
unresolved relevant task, or coverage gap. This is evidence-binding research,
not a B1 schema design.

### 10.4 Result against the controlling witness

The current evidence cannot prove that every supported same-slot replacement
path necessarily produces visible tasks inside a complete envelope. It also
cannot prove a recoverable gap will always be distinguishable from successful
but incomplete API output. Therefore stale trust cannot safely be preserved by
this candidate today. A future implementation must remain unauthorized; this
is **UNRESOLVED**, not proof that all stateful Family-B designs fail.

## 11. Live read-only observations

No live PVE API/CLI call was performed in this research session.

At `2026-08-22T21:20:22.0495192+02:00`, the research environment exposed no
Hubinet/PVE/Proxmox endpoint or credential variable names. No credentials were
requested, printed, or modified. The repository's previously recorded one-node
versions are included in section 2 only as prior evidence.

Consequently this pass provides no new live archive sample, exact-UPID retention
observation, API-versus-CLI comparison, per-node route observation, or effective
permission readback. This lack of access limits only those observations; it is
not evidence for or against Family B.

## 12. Required controlled experiments

None of these experiments was executed. Every lifecycle, ACL, restart, and
high-volume experiment requires a separately approved controlled test window
and disposable/non-production scope.

| # | Exact operation | Kind | Expected task type | Evidence surface | Property tested | Disposable workload / restart | Risk and cleanup | PASS criterion | FAIL criterion |
|---|---|---|---|---|---|---|---|---|---|
| 1 | Create A, destroy A, create B at same test VMID | QEMU | `qmcreate`, `qmdestroy`, `qmcreate` | active, archive, exact UPID on owner node | Same-slot positive records and ordering | Disposable QEMU; no restart | Destructive to test guest; delete B and test disks | Every transition produces durable, attributable records and sentinel overlap | Any transition has no record or can vanish without detectable gap |
| 2 | Create A, destroy A, create B at same test VMID | LXC | `vzcreate`, `vzdestroy`, `vzcreate` | Same | LXC parity | Disposable LXC; no restart | Destructive to test CT; delete CT/rootfs | Same as #1 | Same as #1 |
| 3 | Clone disposable VM to new and reused test VMID variants | QEMU | `qmclone` | owner-node active/archive/exact | Clone generation, ownership, target locator | Disposable QEMU | Storage allocation; delete clone | Every supported variant is task-covered with unambiguous target | Untasked variant or ambiguous target |
| 4 | Clone disposable CT | LXC | expected `vzclone` | Same | Exact clone task type/route | Disposable LXC | Storage allocation; delete clone | Worker and target semantics established | No task or unresolvable semantics |
| 5 | Snapshot create/delete/rollback on disposable VM | QEMU | `qmsnapshot`, delete worker, `qmrollback` | Same plus config observation | Task generation and class-P invalidation trigger | Disposable QEMU | Rewinds test guest; remove snapshots | Every operation recorded before coverage acceptance | Missing/ambiguous record |
| 6 | Snapshot create/delete/rollback on disposable CT | LXC | `pctsnapshot`, `lxcdelsnapshot`, `lxcrollback` | Same | LXC parity | Disposable LXC | Rewinds test CT; remove snapshots | Same as #5 | Same as #5 |
| 7 | Restore backup over same test VMID and to new VMID | QEMU | exact restore type to be measured | Same | Restore replacement versus new-locator semantics | Disposable QEMU and test backup | Destructive overwrite; delete restore and backup | Both variants generate attributable durable tasks | Any supported variant is silent/ambiguous |
| 8 | Restore backup over same test VMID and to new VMID | LXC | expected `vzrestore` | Same | LXC restore parity | Disposable LXC and test backup | Same class of risk/cleanup | Same as #7 | Same as #7 |
| 9 | Migrate disposable VM between two test nodes, including failure | QEMU | `qmigrate` plus observed helpers | Both nodes, cluster surface, exact UPIDs | Owner routing, helpers, source/target handoff | Disposable QEMU; two nodes | Movement/storage/network; migrate back/delete | Per-node protocol remains gap-detectable through all outcomes | Successful APIs can omit a relevant node/task without gap |
| 10 | Migrate disposable CT between two test nodes, including failure | LXC | `vzmigrate` plus helpers | Same | LXC migration parity | Disposable LXC; two nodes | Same; migrate back/delete | Same as #9 | Same as #9 |
| 11 | Import/move/attach/change VM disks and backing storage | QEMU | expected `qmmove` plus exact observed types | Owner active/archive/exact | Every backing-state-changing route | Disposable QEMU/storage | Data/storage changes; remove disks/guest | Complete route matrix and task attribution | Any in-scope supported route silently replaces state |
| 12 | Move/replace/attach CT rootfs/storage | LXC | expected `move_volume` plus observed types | Same | LXC backing-state route matrix | Disposable LXC/storage | Data/storage changes; remove CT | Same as #11 | Same as #11 |
| 13 | Generate enough harmless test tasks to cross page and rotate archive while polling | Both/task harness | Multiple known types | `index`, `index.1`, active/all, exact reads | Retention, offset drift, rotation, concurrent completion | Disposable workloads; no service restart | High load/log growth; stop load and remove guests | Client always proves prefix/overlap or deterministically gaps | A deliberately omitted task still yields complete |
| 14 | Track exact UPIDs beyond list disappearance and successive rotation | Both | Existing experiment UPIDs | exact status/log | Exact-UPID retention/usefulness | Uses disposable tasks from #13 | Log growth; natural/test cleanup | Retention bound and not-found semantics are gap-detectable | Known record disappears with no usable distinction |
| 15 | Restart PVE task-related service during a controlled active task | Both | Operation-specific | active/archive/exact | Start/completion durability and handoff | Disposable workload; **service restart required** | Service disruption; resume service, verify cluster | Record remains provably covered or witness gaps | Silent loss followed by successful complete result |
| 16 | Reboot one test node during/around a controlled task | Both | Operation-specific | all per-node surfaces | Node epoch, archive persistence, rejoin | Disposable workload; **node restart required** | Node outage; rejoin and clean guest | Rejoin proves old overlap or gaps | Rejoin silently resets evidence while complete persists |
| 17 | Remove then restore reader `Sys.Audit` during known task (ACL ABA) | Both | Known operation type | filtered lists and exact read | Interval-wide authorization detection | Disposable workload; ACL change required | Temporary audit visibility loss; restore exact ACL | Witness detects the hidden interval and gaps before use | Before/after equality permits complete coverage |

Experiment #17 is particularly load-bearing. A PASS would show only that the
specific tested ABA is detectable by an added mechanism; it would not prove all
permission-change paths without a source-backed completeness contract.

## 13. Findings

| Question | Finding |
|---|---|
| Can durable witness state be modeled? | **Yes, as a research abstraction.** Atomic state and sticky gap semantics are coherent |
| Is there a proven concrete coverage boundary? | **No.** No audited observable is a monotonic gap-detectable ledger cursor |
| Can prior/current observations overlap using UPIDs? | **Partially observable, insufficient as proof.** Known-UPID overlap does not exclude an unknown omitted task |
| Can retention/rotation loss always be detected? | **UNKNOWN.** Sentinel loss is detectable; loss while an older sentinel remains is not proven detectable |
| Can mutable offset pagination be proven complete? | **No proof found.** No immutable snapshot/cursor was established |
| Is active-to-archive handoff gapless? | **UNKNOWN** |
| Are exact-UPID reads a durable fallback? | **UNKNOWN for retention; insufficient for completeness by themselves** |
| Do all relevant operations generate tasks? | **UNKNOWN/CONDITIONAL beyond exact QEMU create/destroy and prior pinned create/destroy evidence** |
| Can the reader prove interval-wide visibility? | **No with ordinary point-in-time ACL reads.** An untested stronger boundary remains UNKNOWN |
| Is cluster aggregation a generic multi-node proof? | **No.** Per-node durable state/routing semantics remain required and unresolved |
| Can restart be fail-closed? | **Yes conceptually:** persist/latched gap; but recovery to complete coverage is not proven |
| Can current evidence defend same-slot replacement? | **No accepted positive mechanism today.** Unknowns must gap and prohibit stale authority |

The simulator supports one narrow finding: a fail-closed state machine can avoid
false completion when uncertainty is explicitly represented. It cannot supply
the missing upstream properties. Therefore it improves the precision of the
requirements but does not turn Family B into a feasible candidate.

## 14. Final classification

# UNRESOLVED

This is a successful research result under the classification rules. It is not
`FEASIBLE CANDIDATE` because load-bearing properties remain unknown. It is not
`NO-GO WITH PROOF` because this pass has not shown an unavoidable impossibility
for every stateful Family-B design.

Exact remaining unknowns are:

1. exact installed/current-release mapping for `pve-manager`, `pve-common`,
   `pve-cluster`, `pve-container`, and `pve-storage` task behavior;
2. task generation, type, owner node, and all route variants for QEMU/LXC clone,
   restore, rollback, migration, disk/rootfs replacement, import, attach, and
   backing-storage-changing config operations;
3. whether active-to-archive publication is atomically observable without a
   lost interval;
4. whether mutable offset pagination can be made complete during concurrent
   starts/completions/rotation, or exposes a stable snapshot mechanism not yet
   found;
5. a machine-observable archive generation/loss boundary and the exact
   `index`/`index.1` retention behavior at the current release;
6. exact-UPID status/log retention and semantics after list disappearance,
   rotation, node/service restart, and node rejoin;
7. a concrete overlap anchor sufficient to prove absence of unknown omitted
   tasks rather than only presence of a known sentinel;
8. a way to prove complete reader visibility throughout the interval, including
   ACL/token ABA and all task actors, or a stronger independently audited
   authorization boundary;
9. exact multi-node task ownership, migration helper/target records, cluster
   membership epochs, node-name reuse, disappearance/rejoin, and per-node
   recovery semantics;
10. exact PVE node/service restart effects on active state, archive append, and
    task-file durability; and
11. controlled experimental confirmation that an intentionally hidden or
    omitted lifecycle event can never produce `COVERAGE_COMPLETE`.

Until a future accepted ADR resolves every load-bearing item:

```text
Blocker B: OPEN
B1: DEFERRED / NOT AUTHORIZED
Phase 1C: BLOCKED
R0: read-only
```
