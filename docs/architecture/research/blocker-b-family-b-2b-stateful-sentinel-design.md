# NON-NORMATIVE RESEARCH / EVIDENCE

# Family B Research #2B — stateful sentinel candidate design and controlled falsification plan

## 1. Authority, scope, and execution boundary

This document is a design/source-research pass for a provisional candidate named
**Family B / B-S1**. `B-S1` and every state or field defined here are local
research terminology only. They are not architecture, schema, API, or runtime
contracts.

This document:

- does not amend any ACCEPTED ADR;
- does not select a sufficient Blocker-B mechanism;
- does not authorize schema, persistence, runtime, `hostd`, scheduler, API,
  Home Assistant, enrollment, or mutation work;
- does not grant `security_continuity=trusted` anywhere;
- does not authorize or report a live PVE experiment; and
- cannot turn a successful future experiment into trusted authority without a
  separately reviewed and ACCEPTED positive ADR.

Authority for this pass remains:

```text
explicit operator decisions
> ACCEPTED ADRs / accepted architecture
> AGENTS.md
> implementation status
> code/tests
> non-normative research/evidence
```

The fixed result boundary is unchanged:

```text
Family B:  UNRESOLVED / NOT FULLY AUDITED
Blocker B: OPEN
WAVE B1:   DEFERRED / NOT AUTHORIZED
Phase 1C:  BLOCKED
R0:        GO / STRICTLY READ-ONLY
```

The phase-boundary classification of any hypothetical durable B-S1 owner is
`DEFERRED / DORMANT BACKEND OWNER`. It must never be implemented in Home
Assistant or connected to R0. This pass creates no owner at all.

### 1.1 Evidence discipline

Primary labels have exactly these meanings:

| Label | Meaning |
| --- | --- |
| `FACT-SOURCE` | Established in an identified immutable upstream source revision. |
| `FACT-DOC` | Established in an ACCEPTED architecture document or official version-relevant documentation. |
| `INFERENCE` | Bounded conclusion from cited facts; not an upstream or architecture contract. |
| `UNKNOWN` | Not established at ADR0005/ADR0006 security strength. |

`FACT-OPERATOR` is provenance metadata for the operator-supplied, manually
collected `pveversion -v` output; Research #2A.1 recorded a selected subset of
that output. It is not a new normative evidence class and says nothing beyond
what the operator actually read. Qualifiers such as
`REQUIRES CONTROLLED EXPERIMENT` and `LOADED-CODE APPLICABILITY UNKNOWN` do
not replace a primary label.

### 1.2 Repository and source provenance

| Item | Result |
| --- | --- |
| Repository | `shockwave9315/hubinet-ops` |
| Exact base `main` | `c1a3f60ffb1347c385803580b96d7d6c0564a210` (PR #50 merge) |
| PR #50 reviewed head | `d423c9d91b64146fa85b2b45af16dbe5120bea34` |
| Main moved after expected merge | No |
| Research date | 2026-08-25 UTC |
| Upstream method | Fresh, read-only official Proxmox mirrors at the exact #2A/#2A.1 pins |
| Live PVE/HA access | None |
| Experiments #1--#17 | None executed |

## 2. Exact inherited findings

The following findings are inputs, not decisions made by B-S1.

1. **FACT-DOC:** ADR0001's identity and lifecycle contract remains unchanged.
   VMID is a reusable locator; a coverage gap is never replacement evidence;
   ordinary config mutation is not continuity-relevant; and config metadata
   replacement alone is not physical/logical workload replacement.
2. **FACT-DOC:** ADR0006's R/P/N/T meanings are used verbatim:
   class R is same-slot potential replacement; class P retains the same
   `resource_id` and active binding while invalidating/revalidating proof;
   class N creates a new-locator unverified target without invalidating the
   source; class T preserves `resource_id` and changes the node relation.
3. **FACT-OPERATOR:** Research #2A.1 records the operator-observed installed
   package versions. **FACT-SOURCE:** that pass independently maps those exact
   versions to immutable upstream revisions, including
   `qemu-server` 9.2.6 `e6352be...`, `pve-manager` 9.2.11 `f6997e...`,
   `pve-common` 9.2.1 `f665029...`, `pve-container` 6.1.13 `c813255...`,
   `pve-cluster` 9.1.6 `7091d92...`, `pve-access-control` 9.1.1
   `5ccd07d...`, `pve-ha-manager` 5.2.5 `c73364c...`, and `pve-storage`
   9.1.8 `cd5c90c...`. The fuller original operator `pveversion -v` output
   also contains `libpve-guest-common-perl` 6.0.5; #2A.1's selected evidence
   subset omitted that line. **FACT-SOURCE:** 6.0.5 maps to upstream revision
   `191c23e385e5dbed1938b2d1d322196831ef9331`.
4. **INFERENCE:** the combined operator/version mapping establishes installed-
   target source applicability, not which Perl code a long-lived process has
   loaded.
5. **FACT-SOURCE:** per-node native task enumeration uses mutable
   `start`/`limit` offsets; no immutable snapshot, cursor, archive generation,
   predecessor link, or monotonic task sequence is exposed.
6. **FACT-SOURCE:** completed task list history is reverse-read from
   `/var/log/pve/tasks/index` and `index.1`. Stopped tasks found by one
   `active_workers()` call are sorted by task `starttime` descending within
   that processed batch; the batch is appended to `index`, and `endtime` is
   the time that call observes/finalizes the stopped task. Normal writer
   rotation occurs after `index` exceeds 50,000 bytes by renaming it to
   `index.1`. No global chronological or adjacency contract follows.
7. **FACT-SOURCE:** `/cluster/tasks` is a bounded, asynchronously published
   per-node cache: all detected running tasks are retained first, and only the
   remaining slots up to `MAX_FINISHED = 25` total are used for finished tasks,
   before the separate 32 KiB per-node broadcast bound. It is not the node
   archive; this is not “all running plus 25 finished.”
8. **FACT-SOURCE:** in the exact pinned `pve-common` 9.2.1 `fork_worker`, an
   async child creates its exact UPID log before reporting readiness. In CLI
   foreground sync mode, the child reports readiness before the exact log
   exists; the parent validates readiness, creates the exact log, sends `OK`,
   and only then may the child invoke the worker function. The parent
   registers/publishes active state after the readiness exchange. Both pinned
   paths give a promising log-before-worker-body point, but not a generalized
   caller or complete external observer contract.
9. **FACT-SOURCE:** exact-UPID logs are separate files beneath
   `/var/log/pve/tasks/<bucket>/<UPID>`. `cleanup_tasks()` obtains its cleanup
   boundary from the first retained `index.1` entry, recursively examines exact
   UPID files, and unlinks any whose **file mtime** is older than that boundary,
   with no task-liveness or active/index/index.1-membership guard. File mtime is
   a file-write property, not task start ordering or a durable predecessor
   relation. Known-record retention is distinct from enumeration completeness.
10. **FACT-SOURCE:** ordinary API task reads are owner-filtered unless the
    caller has the route's `Sys.Audit`; privilege-separated token authority is
    the intersection of token and base-user authority; `root@pam` takes a
    privileged branch at the exact target access-control source.
11. **FACT-DOC:** ADR0002 proves that equal permission snapshots before and
    after an interval do not rule out ACL ABA during the interval.
12. **UNKNOWN:** no prior pass established a complete active-to-archive
    external handoff, crash durability, interval-wide authorization visibility,
    or finite repeated-traversal proof excluding unknown omitted tasks.

## 3. B-S1 candidate definition

### 3.1 Claimed scope: Phase S only

B-S1 starts with the smallest useful target:

```text
one exact-version PVE node
+ one structurally privileged local read-only sentinel
+ stock per-node task files/logs
+ corroborating per-node task APIs
+ no migration and no cluster-wide completeness claim
```

Its provisional positive statement is deliberately narrower than workload
trust:

> For a logical sentinel interval `[T0, T1]`, enumerate every stock UPID worker
> whose exact UPID log was created and whose worker body began on the covered
> node during that interval, for the explicitly supported route/task matrix;
> otherwise return `COVERAGE UNKNOWN / GAP` before any dependent authority
> could be consumed.

Even if later validated, this statement would be task-channel coverage only.
It would not, by itself, establish that all continuity-relevant physical events
must use a supported worker route, would not identify a workload incarnation,
and would not grant `trusted`.

### 3.2 Initial QEMU/LXC operation matrix

| Class | QEMU Phase-S routes/task types | LXC Phase-S routes/task types | B-S1 consequence |
| --- | --- | --- | --- |
| R | normal create `qmcreate`; normal destroy `qmdestroy` | normal create `vzcreate`; normal destroy `vzdestroy` | Preserve both records and final status. Only an accepted future contract plus inventory/context reconciliation could treat a successful destroy/create chain as positive replacement evidence. B-S1 alone does not perform the ADR0001 transition. |
| P | snapshot rollback `qmrollback`; same-locator `qmrestore` where identity is retained | snapshot rollback `vzrollback`; same-locator `vzrestore` where identity is retained | The same `resource_id`/binding is retained; proof must fail closed/revalidate. The task label never mints a successor. |
| N | `qmclone`; new-locator `qmrestore` | `vzclone`; new-locator `vzrestore` | Target begins as a new unverified resource under ADR0001. Source trust is not revoked merely because duplication occurred. |
| T | **Unsupported in Phase S** | **Unsupported in Phase S** | Any migration task, node-relation change, or migration ambiguity makes B-S1 authority-ineligible. It never creates a successor. |

Snapshot create/delete (`qmsnapshot`, `qmdelsnapshot`, `vzsnapshot`,
`vzdelsnapshot`) are observed for experiment and handoff validation but are not
classified as continuity-relevant merely because they mutate snapshot metadata.
Ordinary `PUT .../config`, tags, description, CPU/memory edits, or digest churn
are not continuity events. Deleting/recreating only a `.conf` object, with the
physical workload unchanged, is also not class R.

Supported synchronous QEMU/LXC config/import/backing routes that create no UPID
remain a load-bearing boundary. B-S1 does **not** assume they are harmless and
does **not** assume they are continuity-relevant. Until a route-by-route source
and controlled-fixture audit proves that no in-scope R/P transition can occur
silently through them, B-S1 is not security-complete even for Phase S.

## 4. Selected evidence surfaces

B-S1 intentionally separates an **enumeration plane** from a **confirmation
plane**. An API success response never substitutes for the local enumeration
plane, and exact-UPID confirmation never proves an unknown UPID did not exist.

| Surface | B-S1 role | Exact source/scope | Retention/rotation | Authorization and limits |
| --- | --- | --- | --- | --- |
| Local task-directory watch | Primary prospective enumeration | Read-only watch of `/var/log/pve/tasks` and its bucket directories on the one Phase-S node; observe create/move/delete/watch-loss/queue-overflow events | Kernel queue is bounded; process/watch loss or overflow is a gap. Exact target delivery completeness is `UNKNOWN / REQUIRES SOURCE REVIEW AND CONTROLLED EXPERIMENT` | Structurally privileged local process must be able to traverse directories/read files. PVE ACLs do not filter this file view, but Linux identity/permissions, mount, namespace, process health, and T3/T4 assumptions become explicit context |
| Recursive exact-UPID log scan | Primary recovery/overlap enumeration | `/var/log/pve/tasks/<hex-bucket>/<UPID>` files; source derives filename from UPID | Separate from `index/index.1`; `cleanup_tasks()` may unlink any exact log whose file mtime predates the first retained `index.1` boundary, without liveness or membership guards. No accepted minimum retention interval exists | Direct file read; exact files default to owner `www-data`, caller group, mode `0640` in `PVE::File::create_owned_file_fh`; exact deployment identity still requires design |
| Local active file | Handoff/status cross-check | `/var/log/pve/tasks/active`; all running plus bounded recent saved entries as written by `active_workers()` | Mutable/re-written; all running are retained first and only remaining slots up to 25 total are filled by finished tasks; no exposed generation | Direct local read avoids API owner filter. File atomicity/generation and safe read protocol are not accepted contracts |
| Local archive files | Completion/order/overlap cross-check | `/var/log/pve/tasks/index`, then `index.1`; append occurs under `.active.lock` | Each processed stopped-task batch is sorted by starttime descending and appended; endtime is finalization/observation time. No global chronology/adjacency contract exists. Rotation at `> 50,000` bytes renames to `index.1`, replacing its prior generation | `index` is created `0644` in exact source. inode/stat/hash observations are research anchors, not stock PVE cursor semantics |
| Per-node API active | Corroboration and API-profile testing | `GET /nodes/{node}/tasks?source=active` | Mutable active state; offset/limit; no snapshot | All tasks require `Sys.Audit` at `/nodes/{node}`; otherwise owner filtered |
| Per-node API archive | Repeated-traversal corroboration | `GET /nodes/{node}/tasks?source=archive&start=...&limit=...` | Reverse reads mutable `index` then `index.1`; numeric offsets move | Same route privilege/filtering. Pagination is never primary completeness proof |
| Per-node API all | Active/archive handoff adversary | `GET /nodes/{node}/tasks?source=all` | Reads active first, then archive, without one shared read snapshot | Same privilege/filtering; duplicates/omissions remain possible |
| Exact-UPID API status/log | Known-record confirmation | `GET /nodes/{node}/tasks/{upid}/status` and `/log` | Depends on exact log survival; status missing gives `no such task`; normal log listing may return an `unable to open file` line | Owner, exact token owner, or node `Sys.Audit`; failure is never proof of nonexistence |
| `/cluster/tasks` | Diagnostic only; **not used for Phase-S completeness** | `GET /cluster/tasks`, pmxcfs per-node status cache | bounded recent cache; asynchronous publication; node contributions can be skipped | All entries require `Sys.Audit` at `/`; successful partial aggregation has no omission marker |
| CLI | No completeness role | `qm`/`pct`/`pvesh` may be operation initiators in a later fixture | Invocation-specific | A CLI command is not an independent history surface. Short-lived CLI loading behavior differs from long-lived API daemons and must be recorded |

Primary source pins for these claims are:

- [`pve-common` 9.2.1 `PVE::RESTEnvironment`](https://github.com/proxmox/pve-common/blob/f665029eac78022e81810ab2e44eace57ade13fb/src/PVE/RESTEnvironment.pm),
  [`PVE::UPID`](https://github.com/proxmox/pve-common/blob/f665029eac78022e81810ab2e44eace57ade13fb/src/PVE/UPID.pm),
  [`PVE::File`](https://github.com/proxmox/pve-common/blob/f665029eac78022e81810ab2e44eace57ade13fb/src/PVE/File.pm), and
  [`PVE::INotify`](https://github.com/proxmox/pve-common/blob/f665029eac78022e81810ab2e44eace57ade13fb/src/PVE/INotify.pm);
- [`pve-manager` 9.2.11 task API](https://github.com/proxmox/pve-manager/blob/f6997e698c7933ea8e62319e2bf1bf7262daa56a/PVE/API2/Tasks.pm),
  [`pvestatd`](https://github.com/proxmox/pve-manager/blob/f6997e698c7933ea8e62319e2bf1bf7262daa56a/PVE/Service/pvestatd.pm), and
  [`pveupdate`](https://github.com/proxmox/pve-manager/blob/f6997e698c7933ea8e62319e2bf1bf7262daa56a/bin/pveupdate);
- [`pve-cluster` 9.1.6 task cache](https://github.com/proxmox/pve-cluster/blob/7091d92e594952dba65c1e57568b3d7cc244e960/src/PVE/Cluster.pm); and
- [`pve-access-control` 9.1.1 reader rules](https://github.com/proxmox/pve-access-control/blob/5ccd07d9302562b73374d331b63d25b04b86766c/src/PVE/RPCEnvironment.pm).

### 4.1 Phase-S worker attribution

| Operation | QEMU task / owner / UPID id | LXC task / owner / UPID id |
| --- | --- | --- |
| Normal create | `qmcreate`; executing/route node; target VMID | `vzcreate`; executing/route node; target VMID |
| Normal destroy | `qmdestroy`; executing/route node; destroyed VMID | `vzdestroy`; executing/route node; destroyed VMID |
| Clone | `qmclone`; source/route node; source VMID | `vzclone`; source/route node; source VMID |
| Snapshot create | `qmsnapshot`; route node; VMID | `vzsnapshot`; route node; VMID |
| Snapshot delete | `qmdelsnapshot`; route node; VMID | `vzdelsnapshot`; route node; VMID |
| Snapshot rollback | `qmrollback`; route node; VMID | `vzrollback`; route node; VMID |
| Backup restore | `qmrestore`; target/route node; target VMID | `vzrestore`; target/route node; target VMID |

These are exact-target source facts for the installed package mapping, subject
to section 11's independent loaded-code/runtime applicability problem. The UPID
`id` is operation attribution, never durable workload identity.

## 5. Conceptual durable sentinel

This is a research data model only. It is not a schema proposal and it is not a
second authority for any canonical ADR0001 field.

| Conceptual field | Purpose |
| --- | --- |
| `sentinel_protocol = B-S1` and `protocol_revision` | Prevent silent reinterpretation after research/protocol changes |
| `sentinel_epoch_id` | Unique local coverage attempt; never resource identity or a fourth continuity state |
| `coverage_state` | One research-local state from section 6 |
| `covered_from_barrier`, `covered_through_barrier` | Durable logical `[T0,T1]` boundary identities, plus audit wall/monotonic timestamps |
| `last_durable_observation_sequence` | Sentinel-owned commit order, not a PVE task cursor |
| `last_successful_observation_at` and `heartbeat_deadline` | Give an independent supervisor and/or consumption-time freshness check the data needed to reject stale open coverage; a dead observer cannot latch its own gap |
| `known_upids` / bounded set digest | Exact normalized UPID set in current retention envelope; digest never substitutes for retained collision/audit details |
| `baseline_upids` | Files present before T0; prevents treating pre-existing logs as interval starts |
| `pending_upids` | Known records whose final status/log or supported-task classification is unresolved |
| `overlap_anchors` | Retained known UPIDs plus source/provenance used only to diagnose known-record continuity or loss. Survival of one or multiple anchors does not prove that every unknown record between them survived cleanup |
| `task_log_directory_digest` | Bounded scan result including bucket, filename, stat tuple, and content hash where readable; not a stock generation |
| `active_observation` | Raw digest, parsed UPIDs, file stat/context, read start/end |
| `archive_observation` | Separate `index`/`index.1` stat/inode/hash/first-last UPIDs and traversal boundaries |
| `archive_generation_observation` | Sentinel-observed identities only; a rename/inode change is evidence, not an upstream archive epoch |
| `watch_context` | watch descriptors, masks, queue/overflow/invalidation observations, mount/filesystem context |
| `node_context` | backend `node_id`/binding when applicable, external node name, boot ID observation, route name, node presence |
| `reader_context` | mode (`local-file`, token, full token, `root@pam`), credential identity/version, effective privilege proof, local uid/gid/groups/namespace |
| `source_context` | `inventory_source_id`; conditional exact `source_attestation_epoch`; endpoint/source revisions if the evidence authority depends on them |
| `version_context` | installed package versions/commits, relevant file hashes, daemon PIDs/start times, loaded-code applicability status |
| `operation_scope` | Exact supported QEMU/LXC route/task types; anything outside it is fail-closed |
| `gap_reason[]` | Append-only reasons, first detection time, affected interval, surfaces/context involved |
| `coverage_eligible` | Derived research bit; false unless every required invariant is proven for the exact interval |

The sentinel may retain historical coverage facts, but it must never contain an
independently authoritative `security_continuity` value. Any future accepted
mechanism would still use ADR0001's sole canonical owner and revision rules.

## 6. Candidate state machine

| State | Entry criteria | Exit criteria | Restart/durability semantics |
| --- | --- | --- | --- |
| `INITIALIZING` | No prior valid B-S1 epoch; new node/version/reader context; explicit rebaseline | Install the watch before scanning, durably capture context, complete baseline scans, and either enter `ESTABLISHING_OVERLAP` or latch a gap | Durable epoch/context required. Restart during initialization discards the attempt; no coverage exists |
| `ESTABLISHING_OVERLAP` | Watch active; baseline known; no detected gap yet | Required fixed-point traversals, exact-log reconciliation, and all independently proven prospective watch/scan preconditions succeed → `COVERAGE_ELIGIBLE`; ambiguity → `GAP_LATCHED`; context invalid → `AUTHORITY_INELIGIBLE` | Only atomically committed scan rounds survive. An unclean observer restart latches a gap for the attempted interval |
| `COVERAGE_ELIGIBLE` | Every section 8 invariant is independently satisfied for the exact current epoch and a committed T0 exists | Successful close protocol commits T1 and a complete interval; any failure/context change immediately enters `GAP_LATCHED` or `AUTHORITY_INELIGIBLE` before use | Heartbeat/lease and current context are durable. Loss of the observer or its supervisor is not automatically recoverable |
| `GAP_LATCHED` | Watch overflow/invalidation; missed heartbeat; unreadable/malformed surface; anchor/pagination/rotation/handoff ambiguity; unknown task/operation; crash/reboot/restart uncertainty | Never retroactively returns to eligible for the affected interval. A distinct, explicit rebaseline may start a **new** epoch only after the future accepted revalidation/enrollment contract permits it | Gap reason and affected interval are durable, append-only, and survive every restart |
| `AUTHORITY_INELIGIBLE` | Reader/source/node/version/loaded-code context is absent, changed, unsupported, or cannot be proven | Explicit context revalidation may start a new `INITIALIZING` epoch; it cannot heal an old interval or automatically restore trust | Durable. Package/daemon/credential equality after an ABA does not restore eligibility |
| `CLOSED_COMPLETE` | A T1 close transaction proves every required condition for `[T0,T1]` | Historical terminal state only. A later interval uses a new epoch/boundary | Immutable audit fact, still not `security_continuity=trusted` |

Transitions forbidden under every interpretation:

```text
missing evidence -> assumed clean
GAP_LATCHED -> old interval complete
same package version -> loaded code proven
known UPID readable -> no unknown UPID existed
equal ACL before/after -> interval visibility proven
coverage gap -> replacement identity transition
Phase-S complete -> Family-B complete
experiment PASS -> trusted
```

A stored `COVERAGE_ELIGIBLE` value is never self-certifying after observer
death. The crashed sentinel cannot record its own failure; an independent
supervisor and/or every consumption-time freshness/heartbeat check must reject
stale open coverage before dependent authority could be consumed. That future
mechanism is required for safety but is neither designed nor implemented here.

## 7. Exact coverage-interval semantics

### 7.1 Logical boundaries

`T0` and `T1` are durable B-S1 barrier identities, not inferences from PVE
wall-clock timestamps or UPID ordering.

- `T0` is committed only after watch installation, a complete recursive
  baseline scan, queue drain, repeated scan reconciliation, active/archive
  capture, reader/node/version validation, and durable commit all agree. The
  first candidate admits only a **quiescent T0**: every baseline in-scope UPID
  must be stopped/finalized and classified before the boundary. This prevents a
  log created before T0 but a delayed worker body beginning after T0 from being
  mistaken for harmless prehistory.
- `T1` is committed only after the watch remained live through the close
  attempt, every queued event and newly discovered exact log was reconciled,
  repeated full scans reached the required fixed point, active/pending tasks
  were accounted for, and the entire close result was durably committed. The
  first candidate likewise admits only a **quiescent T1**: an in-scope running
  or unresolved task keeps the interval open and authority-ineligible; it does
  not become clean by timeout.
- Local monotonic and wall timestamps are audit metadata. They do not order
  PVE tasks or prove adjacency.

Conditional on all required invariants, B-S1 would define:

```text
coverage of [T0,T1] is complete
<=>
every in-scope UPID worker whose operation body began after the committed T0
barrier and before the committed T1 barrier is present in the durable known-UPID
set with exact node/owner/type/id and final-status evidence;
AND no watch, scan, retention, rotation, handoff, reader, version, service,
process, node, or authorization gap affected that interval.
```

This is not yet established for stock PVE. Until the unknowns below are closed,
the only valid B-S1 output is `COVERAGE UNKNOWN / GAP`.

The research-local external outputs are exactly:

```text
COVERAGE PROVEN [T0,T1]
  only when one CLOSED_COMPLETE record exists for that exact logical interval
  under one unchanged eligible context and every required invariant is proven

COVERAGE UNKNOWN / GAP [T0,?]
  for every other case, with the earliest known gap boundary and durable reasons
```

`COVERAGE PROVEN` is an interval/channel statement, not a canonical continuity
state and not authority to set `security_continuity=trusted`. Under the present
research findings B-S1 cannot emit it; the definition exists so later source
work and controlled experiments can falsify the candidate precisely.

### 7.2 Required race handling

| Race/event | Candidate handling | Current strength |
| --- | --- | --- |
| Task starts while pages/files are read | Exact log creation should precede worker-body execution; watch-first plus scan reconciliation must discover it regardless of API page | `FACT-SOURCE` for exact pinned async and CLI foreground sync `fork_worker` ordering; no caller generalization; observer completeness `UNKNOWN` |
| Task completes while pages/files are read | Exact log remains primary known identity; final status may move active→archive. Repeat scans and exact reads until reconciled or gap | Normal writer order `FACT-SOURCE`; external gaplessness `UNKNOWN` |
| Active→archive handoff | Never rely on one `source=all` read. Compare exact UPID across log, active, index, index.1, and API; any unexplained omission/duplication is a gap | `UNKNOWN / REQUIRES CONTROLLED EXPERIMENT` |
| Archive rotation | Track file stat/inode/hash observations, reopen after rename, and rescan exact logs. Retained anchors are loss diagnostics only. Unchecked rename/error or ambiguous generation is a gap | Normal rename `FACT-SOURCE`; generation proof `UNKNOWN` |
| Offset pagination movement | Every API traversal starts again at offset zero after any prefix change. Require repeated identical normalized prefixes through overlap; API result remains corroborative only | Native offset weakness `FACT-SOURCE/INFERENCE`; no API-only proof |
| Exact-UPID failure | Keep the known identity and latch a gap; never reinterpret not-found as absence/nonexistence | `FACT-SOURCE` route behavior |
| Sentinel process restart | Clean close starts a new interval. After an unclean exit, an independent supervisor and/or consumption-time freshness check must reject stale open coverage and durably latch the gap; the dead sentinel cannot do so itself | Required fail-closed design; supervisor mechanism `UNKNOWN` |
| PVE service reload/restart | Close current interval as ineligible unless loaded-code and handoff semantics are separately proven; begin a new version/service epoch | Source behavior partly known; runtime result `UNKNOWN` |
| Node reboot | Always gap the old interval; no automatic overlap recovery in Phase S | Required conservative default |
| Reader/permission change | Any API-reader change gaps the interval; local-reader uid/gid/group/namespace/file-access change also gaps | ADR0002 ACL ABA applies; structural context contract `UNKNOWN` |
| Version change | Close eligibility immediately; preserve old audit; require exact source re-pin and new loaded-code-valid epoch | Required by ADR0006 version-scoped contract |

## 8. Watch-first overlap and repeated traversal protocol

### 8.1 Candidate algorithm

1. Capture exact source/node/reader/version context and commit a new ineligible
   sentinel epoch.
2. Install watches on the task root, existing bucket directories, `active`,
   `index`, and `index.1` **before** baseline enumeration. Watch creation must
   report failure, invalidation, unmount, queue overflow, and missing buckets.
3. Recursively enumerate exact log filenames and normalize every UPID. Record
   pre-scan/post-scan stat context for every directory and selected file.
4. Drain watch events that accumulated during the scan, add newly created
   bucket watches, and rescan every affected directory.
5. Repeat steps 3--4 until two consecutive complete normalized scans and the
   intervening drained event set form the same exact UPID set. Any overflow,
   malformed name, unreadable entry, disappearance (including cleanup loss), or
   unstable context latches a gap.
6. Traverse local `active`, `index`, and `index.1` completely. Independently
   traverse API `source=active`, `archive`, and `all` from offset zero, restarting
   a traversal whenever its normalized prefix changes.
7. Retain multiple prior anchors where available to diagnose known-record loss,
   but do not use their survival to prove that an unknown intervening record
   survived. Positive completeness depends on the independently proven
   prospective watch/scan contract, plus exact-log presence/status for every
   UPID that contract durably enumerates.
8. Compare the local exact-log set, local active/archive sets, API sets, and
   known pending set. Expected surface asymmetry must be explicitly explained by
   source behavior; unexplained asymmetry is a gap.
9. Commit a quiescent T0 only after all required invariants are satisfied and
   every baseline in-scope UPID is finalized/classified. During the open
   interval, process watch events durably in ordered batches and repeat scans
   before the queue/retention budget can plausibly be exhausted.
10. Close T1 using the same watch-drain/full-scan/repeated-traversal fixed point;
    require every pre-close in-scope task to be finalized/classified; then
    atomically commit either `CLOSED_COMPLETE` or `GAP_LATCHED`, never an
    intermediate positive state. A task that arrives after the logical close
    cut belongs to the next interval; whether watch/scan delivery exposes a
    sufficiently exact cut is itself a required invariant, not assumed here.

### 8.2 Required invariants

| Invariant | Status |
| --- | --- |
| Every claimed operation route reaches `fork_worker` with the expected type/owner/id | `FACT-SOURCE` for named exact-target routes; runtime/loaded-code validation required |
| Exact UPID log creation completes before the worker body begins | `FACT-SOURCE` for the exact pinned async path and CLI foreground sync handshake; no generalization beyond pinned implementation/callers |
| Exact-target kernel/filesystem watch delivery reports every relevant create/rename/delete or an unambiguous overflow/loss event | `UNKNOWN / REQUIRES KERNEL-SOURCE REVIEW AND CONTROLLED EXPERIMENT` |
| Watch installation plus recursive scan has no creation race | Plausible watch-first algorithm; `UNKNOWN / REQUIRES CONTROLLED EXPERIMENT` |
| Directory scans enumerate every retained exact log without silent filtering | `UNKNOWN / REQUIRES CONTROLLED EXPERIMENT` |
| Normal cleanup may unlink any exact UPID log whose file mtime is older than the boundary derived from the first retained `index.1` record, regardless of task liveness or active/archive membership | `FACT-SOURCE` at `pve-manager` 9.2.11 `f6997e...`, `bin/pveupdate`, `cleanup_tasks()` |
| Unknown-record completeness from surviving overlap anchors | **Cannot be provided by anchor survival itself**; exact-log mtime is not task start ordering or a durable predecessor relation. `UNKNOWN / REQUIRES CONTROLLED EXPERIMENT` whether the prospective watch/scan contract detects every relevant cleanup loss |
| Multiple repeated API traversals plus local file evidence distinguish new tasks from offset omissions | Candidate hypothesis; `UNKNOWN / REQUIRES CONTROLLED EXPERIMENT` |
| Local reader permission/process continuity is independently gap-detectable | `UNKNOWN`; requires a future accepted reader/supervisor boundary |
| No supported in-scope R/P event can occur through a no-UPID route | `UNKNOWN`; experiments #11/#12 are designed to kill this assumption |

The maximum tolerable movement is not a numeric page count. B-S1 tolerates
arbitrary duplicates or prefix movement only while every change is explained by
the primary exact-log/watch set and the full traversal can restart and reach its
fixed point before the watch/retention budget is exhausted. Retained known
anchors remain useful loss diagnostics, but their survival contributes no proof
about unknown records. B-S1 tolerates **zero unexplained omission**, **zero
watch loss**, and **zero known-record loss without a gap**. Otherwise it gaps.

### 8.3 What repeated traversal cannot prove today

No finite number of API-only offset traversals can prove the absence of an
unknown omitted record from the exposed fields alone. Two identical results can
occur after an unknown task was filtered, created and removed outside retention,
or hidden during reader ABA. Repeated API traversal is therefore never B-S1's
completeness authority.

Nor can survival of one or multiple exact-log anchors prove that no unknown
exact log was deleted. Stock cleanup compares file mtime—a file-write property,
not task start/end order—to the first retained `index.1` boundary and has no
liveness or active/archive-membership guard. Anchor survival therefore diagnoses
known-record retention only; it creates no durable predecessor relation and no
unknown-record completeness theorem.

The local watch-plus-exact-log design is stronger because it proposes a
pre-operation creation point and a detectable overflow channel. It still fails
unless every watch, retention, reader, loaded-code, and route-generation
invariant is proven. Experiment #13 falsifies B-S1 if a ground-truth generated
UPID is deliberately omitted from the candidate's durable set while the
candidate nevertheless closes the interval complete.

## 9. Exact-UPID role

B-S1 learns a UPID from one of three enumeration paths:

1. exact task-log file creation/watch event;
2. recursive task-log directory scan; or
3. active/archive enumeration.

Once known, exact-UPID status/log reads provide:

- confirmation of decoded node, PID/process-start, start time, task type, id,
  and owner;
- final status/log evidence;
- retention extension beyond list disappearance; and
- reconciliation of active/archive handoff.

They are not a cursor, predecessor chain, archive generation, or unknown-record
enumerator. A known UPID can never prove that no unknown UPID was omitted.

```text
known-record retention != enumeration completeness
```

An exact-UPID lookup failure is a gap because it can mean cleanup, wrong route,
permission loss, malformed identity, service state, or missing evidence. It is
never proof that the task did not exist.

## 10. Authorization and ACL ABA

| Reader | Exact point-in-time requirement/behavior | Interval-wide result |
| --- | --- | --- |
| Ordinary API user | `Sys.Audit` at `/nodes/{node}` for all per-node tasks; `Sys.Audit` at `/` for all cluster-cache tasks | **Ineligible.** ACL can change; owner filtering returns successful incomplete results with no omission marker |
| Privilege-separated API token | Token and base user must both retain the required privilege because effective authority is their intersection | **Ineligible.** Either side can undergo ABA; token identity also filters owner fallback differently |
| Full/non-separated API token | Inherits base-user privileges at the exact source | **Ineligible.** Base-user ACL ABA and credential/context changes remain; no interval proof |
| `root@pam` remote/API reader | Exact target access-control source gives the privileged branch; task routes can therefore avoid ordinary owner filtering | **Still not a completeness oracle.** It does not solve pagination, retention, node/service availability, credential/channel continuity, loaded code, or task generation. Selecting it as authority requires a reviewed credential/trust design |
| Structurally privileged local file reader | Must continuously retain Linux traversal/read access to task directories, `active`, `index*`, and `0640` exact logs; no PVE route filter is involved | **Strongest B-S1 profile, still UNKNOWN.** Requires explicit uid/gid/group/namespace, watchdog, file/mount integrity, version, and T3/T4 contract. A local root-capable process is not root-resistant |

For API profiles, point-in-time privilege proof before/after an interval cannot
close:

```text
visible -> hidden/NoAccess -> visible
```

For the local profile, PVE ACL ABA is not the reader filter, but an analogous
local context ABA (readable → unreadable/other namespace → readable) must be
detectable and must latch a gap. T3 direct root is outside the anchor-less
candidate exactly as ADR0006 §5b allows; it is not used as a NO-GO reason and
B-S1 claims no T3 resilience.

## 11. Daemon and loaded-code applicability

### 11.1 Surfaces and processes

| Dependency | Long-lived process impact |
| --- | --- |
| Per-node/cluster/exact-UPID HTTP reads | `pveproxy` and privileged `pvedaemon` load `PVE::API2`, including task route and access-control code, into long-lived Perl processes/workers |
| API-originated QEMU/LXC worker creation | The serving API worker has QEMU/LXC/guest-common/storage modules loaded; `fork_worker` children inherit that process image and execute the captured worker closure |
| Active/archive writer behavior | Worker parent/reaper paths use loaded `PVE::RESTEnvironment`/`PVE::UPID` code; `pvestatd` separately loads those modules for recovery/publication |
| `/cluster/tasks` publication | `pvestatd` and `pve-cluster` loaded code determine periodic publication/cache behavior |
| Short-lived CLI | A new `qm`/`pct`/`pvesh` process normally loads installed files at invocation; this does not prove a concurrent API daemon loaded the same revision |
| Local B-S1 reader | Its own reader code can start fresh, but the PVE process that created/wrote the evidence may still execute older loaded modules |

**FACT-SOURCE:** exact target `pvedaemon` and `pveproxy` set
`leave_children_open_on_reload => 1`; their units map `ExecReload` to the
daemon's restart command. `PVE::Daemon` handles HUP by execing the master while
allowing configured old children to remain open. The exact package trigger path
uses `reload-or-try-restart` for `pvedaemon`, `pvestatd`, and `pveproxy`.
Therefore a package transaction/reload can leave an old serving child alive
while the new master has loaded new files.

Primary sources:

- [`pvedaemon`](https://github.com/proxmox/pve-manager/blob/f6997e698c7933ea8e62319e2bf1bf7262daa56a/PVE/Service/pvedaemon.pm),
  [`pveproxy`](https://github.com/proxmox/pve-manager/blob/f6997e698c7933ea8e62319e2bf1bf7262daa56a/PVE/Service/pveproxy.pm), and
  [their units/postinst](https://github.com/proxmox/pve-manager/tree/f6997e698c7933ea8e62319e2bf1bf7262daa56a/services);
- [`PVE::Daemon` restart implementation](https://github.com/proxmox/pve-common/blob/f665029eac78022e81810ab2e44eace57ade13fb/src/PVE/Daemon.pm); and
- package `pve-api-updates` triggers in the exact QEMU/LXC/storage/guest-common
  source packages.

### 11.2 Experiment preconditions

Installed package equality is insufficient. Experiments #13/#14/#15 must
record:

- exact installed package versions and relevant file hashes;
- all relevant master/worker/task PIDs and process start times;
- whether the task was initiated through HTTP/API or a fresh CLI process;
- the last package transaction and daemon reload/restart boundary;
- evidence that no pre-upgrade serving child handled the request; and
- the candidate reader's own start/version context.

The preferred disposable-fixture precondition for #13/#14 is a clean boot after
the exact packages were installed, with no package upgrade since boot and with
all relevant process start times recorded. That is an environment precondition,
not permission to reboot any real node. Experiment #15 must separately compare
HUP/reload, full stop/start restart, and relevant daemon roles. No service was
restarted during this pass.

If loaded-code applicability cannot be proven, the experiment can still report
a runtime observation but cannot validate the exact pinned source hypothesis.
B-S1 must return `AUTHORITY_INELIGIBLE` for that context.

## 12. Storage and `pve-guest-common` dependencies

### 12.1 LXC restore experiment #8

**FACT-SOURCE:** exact `pve-container` 6.1.13 `PVE::LXC::Create` branches among
filesystem/tar, PBS, and generic `backup-provider` restore mechanisms and calls
`PVE::Storage`/the selected plugin. Research #2A.1 mapped `pve-storage` 9.1.8,
but did not select or audit the target fixture's actual storage/backup plugin.

Result: `pve-storage` is already in the source ledger, but experiment #8 must
add the **exact selected storage and backup-provider plugin files** and archive
format to its per-run ledger. It must not claim all-backend LXC restore parity
from one `dir`, PBS, RBD, ZFS, or external-provider run. No additional package
family is required until a chosen plugin imports one that affects the tested
restore path.

### 12.2 Snapshot/lock experiments #5/#6

**FACT-SOURCE:** exact qemu-server and pve-container API modules call
`PVE::GuestHelpers::guest_migration_lock` around rollback/delete paths, and
their QEMU/LXC config classes inherit snapshot create/delete/rollback core from
`PVE::AbstractConfig`. Both modules are shipped by `libpve-guest-common-perl`.
Exact qemu-server 9.2.6 requires `libpve-guest-common-perl >= 5.2.2`; exact
pve-container 6.1.13 requires `>= 5.1.3`.

**FACT-OPERATOR:** the fuller original operator `pveversion -v` evidence states
`libpve-guest-common-perl: 6.0.5`; Research #2A.1's selected evidence subset
omitted that line. **FACT-SOURCE:** exact upstream revision
[`191c23e385e5dbed1938b2d1d322196831ef9331`](https://github.com/proxmox/pve-guest-common/commit/191c23e385e5dbed1938b2d1d322196831ef9331)
is the 6.0.5 version bump. This establishes installed-target mapping, not an
audit of the loaded code or the two modules' relevant behavior.

Result: **add `libpve-guest-common-perl` to the later source ledger.** Before
#5/#6, audit exact 6.0.5 `PVE::AbstractConfig` plus `PVE::GuestHelpers` for
snapshot/lock behavior.
This dependency is load-bearing for whether the class-P operation actually
executes/succeeds and what its task log/status means, even though the outer API
route creates the UPID before entering the lock-protected worker body.

## 13. Phase S versus Phase M

### 13.1 Phase S — candidate addressed here

Phase S is one exact-version node, one local sentinel owner, one task-log
namespace, no migration, and no cluster completeness claim. It can at most
answer whether its exact scoped task channel covered a logical interval.

Phase S must never be called Family-B complete. It cannot support migration or
claim that every source node in a cluster was covered.

### 13.2 Phase M — future extension, not solved here

Any future Phase M must separately define and prove:

- per-node durable sentinel ownership and a fleet-wide aggregation transaction;
- durable node identity/binding, not node name alone;
- node join and initial coverage eligibility;
- node removal and the retained interval owned by the removed node;
- node disappearance versus planned removal;
- reinstall/rejoin and node-name reuse;
- migration handoff without changing `resource_id`;
- source/target worker ownership and target CLI helpers without UPIDs;
- HA `hamigrate` wrapper, LRM-issued worker, and helper linkage;
- cluster task cache's bounded/skipped-node semantics;
- partial source/target success and failure ordering;
- clock, UPID time, and cross-node ordering assumptions;
- per-node archive/log retention and cleanup skew;
- version skew and staggered daemon reload/restart; and
- a global rule that one missing/ineligible node prevents a cluster-wide
  positive coverage claim for affected resources.

No Phase-M assertion is made by this document.

## 14. Explicit fail-closed rules

| Condition | Mandatory B-S1 result |
| --- | --- |
| Unreadable required surface | `GAP_LATCHED`; retain prior audit, no positive coverage |
| Malformed/partial response or file | `GAP_LATCHED`; never skip the record and continue |
| Pagination ambiguity or changing prefix | Restart traversal; if a stable proven close cannot be reached before budget/retention limit, `GAP_LATCHED` |
| Watch overflow, invalidation, or loss | `GAP_LATCHED` for the affected interval; no scan or surviving anchor heals it |
| Archive rotation ambiguity, rename failure, unexplained inode/hash change | `GAP_LATCHED` |
| Known overlap-anchor loss | `GAP_LATCHED`; surviving anchors prove nothing about unknown records |
| Exact-UPID status/log lookup failure | Keep known identity; `GAP_LATCHED` |
| Reader permission/credential/local identity change | `AUTHORITY_INELIGIBLE` and gap the affected interval |
| ACL uncertainty or API-reader ABA | `GAP_LATCHED`; equal later ACL does not heal it |
| Daemon/service reload/restart with unproven loaded-code/handoff semantics | `AUTHORITY_INELIGIBLE`; new epoch only after explicit revalidation |
| Sentinel process crash or missed heartbeat | Independent supervisor and/or consumption-time freshness validation must reject stale open coverage and cause `GAP_LATCHED`; the crashed sentinel cannot latch its own gap, and no optimistic replay is allowed |
| Node reboot/boot-ID change | `GAP_LATCHED`; Phase S never auto-recovers the old interval |
| Source/package/version/file-hash change | `AUTHORITY_INELIGIBLE`; source re-pin and new epoch required |
| Unknown task type, owner, node, or malformed UPID | `GAP_LATCHED` and add to route/source audit |
| Unsupported operation observed | `AUTHORITY_INELIGIBLE`; apply canonical R/P/N/T semantics only after classification |
| Potentially continuity-relevant no-UPID route | B-S1 cannot cover it; scope is ineligible until independently excluded or another mechanism covers it |
| Unclassified lifecycle event | `GAP_LATCHED`; never call it config churn merely to preserve coverage |
| Source-attestation epoch mismatch when evidence is source-dependent | Authority-ineligible under ADR0003; old evidence retained only for audit |
| Node trust/binding mismatch when the future evidence contract depends on it | Authority-ineligible; no carry-forward by name equality |

None of these conditions silently preserves positive coverage. None is positive
replacement evidence by itself.

## 15. Falsification-first experiment plan — not executed

Every experiment below requires separate operator approval later. Every fixture
must be disposable, non-production, isolated from real Home Assistant and
private production workloads, and have explicit rollback/cleanup ownership.
No experiment is authorized by its inclusion here.

### 15.1 Recommended order

```text
preflight: exact versions + loaded-code-capable clean disposable fixture
A. #13 pagination/rotation/watch omission
B. #14 exact-UPID retention
C. #17 authorization ABA and local-reader context loss
D. #15 service reload/restart and loaded-code behavior
E. #11/#12 no-UPID backing-route falsification
F. #1/#2 R, #5/#6 P, #7/#8 restore, #3/#4 N lifecycle validation
G. #9/#10 migration/multi-node
H. #16 node reboot late
```

This order attacks enumeration, retention, reader, and loaded-code assumptions
before spending time on the full lifecycle matrix. #11/#12 move before the
ordinary lifecycle suite because a continuity-relevant no-UPID witness would
kill the claimed task-only operation boundary cheaply.

### 15.2 Preflight (not an experiment number)

- **Question:** is the fixture exact-version, disposable, isolated, and capable
  of proving which process cohort handles each request?
- **Required fixture/preconditions:** one Phase-S node booted after installing
  the exact package set; no later upgrades; exact file hashes, process tree,
  boot ID, storage backend, reader identity, time/retention settings recorded.
- **Collected evidence:** package DB, immutable upstream mapping, relevant file
  hashes, PIDs/start times, unit definitions, reader startup/heartbeat evidence.
- **Failure:** any unknown mapping, old child, production dependency, or cleanup
  uncertainty blocks the applicable experiment.
- **Approval/destructive scope:** fixture provisioning/reboot needs separate
  approval; this document performs neither.

### 15.3 #13 — pagination, rotation, and watch/scan omission

- **Question:** can B-S1 always enumerate or deterministically gap while tasks
  start/finish across API pages, watch drains, and `index -> index.1` rotation?
- **Hypothesis:** watch-first exact-log discovery plus repeated scans prevents a
  false `CLOSED_COMPLETE`; native offset traversal alone will show duplicates or
  omissions under adversarial interleavings.
- **Source-contract boundary:** exact watcher/kernel/filesystem semantics are
  load-bearing for any positive `SOURCE CONTRACT`. #13 is still useful as an
  early falsification attempt before universal source proof, but no PASS can
  close that source contract by itself.
- **Fixture/preconditions:** cold exact-version Phase-S node; approved bounded
  task generator; local sentinel instrumentation; volume sufficient to cross
  API pages and at least one archive rotation; ground-truth client records every
  returned UPID.
- **Collect:** all request/response bodies and timing, raw watch events including
  overflow, recursive scan sets, local `active/index/index.1` copies/stat hashes,
  exact logs, reader heartbeats, PIDs/version context, ground-truth UPIDs.
- **Falsification:** any ground-truth UPID is absent from durable B-S1 evidence
  while B-S1 reports complete; or an intentional offset/rotation omission is not
  converted to a gap.
- **PASS:** every tested interleaving is fully enumerated or explicitly gapped.
- **PASS does not prove:** the watcher/kernel/filesystem source contract, all
  kernel/filesystem interleavings, cleanup behavior, authorization, operation
  route coverage, or security sufficiency.
- **Cleanup/destructive scope:** stop load, remove only fixture-created tasks and
  disposable guests/snapshots according to the approved generator; preserve
  evidence copy. High task/log load is operationally disruptive.
- **Approval:** always separately required.

### 15.4 #14 — exact-UPID retention

- **Question:** what does exact stock cleanup do to completed and, if safely
  reproducible, long-running/low-output exact logs across archive rotation; how
  do file mtime and task start/end relate; and can B-S1 detect each loss?
- **Hypothesis:** `cleanup_tasks()` applies its first-retained-`index.1`
  boundary to exact-log file mtime without a liveness or membership guard;
  retained known anchors diagnose losses but do not establish enumeration
  completeness.
- **Fixture/preconditions:** #13 UPIDs; bounded completed tasks; a safely
  reproducible long-running/low-output task if available without broadening
  destructive scope; exact cleanup schedule/source; recorded task start/end,
  clock discipline, and no unrecorded manual log changes.
- **Collect:** every fixture ground-truth UPID; task start/end and running state;
  list membership; exact-log creation/write times, file mtime/presence/stat/hash;
  exact status/log API result; cleanup invocation provenance and computed
  `index.1` boundary; archive boundaries; watch/scan loss signals; sentinel and
  service/PID context at every observation.
- **Falsification:** a known record disappears or becomes unreadable while B-S1
  retains complete coverage without a prior gap; or an unknown in-scope exact
  log can be deleted before durable enumeration with no independent watcher/gap
  signal while B-S1 can still close complete.
- **PASS:** the tested stock mtime-bound cleanup behavior is reproduced (or any
  source/runtime mismatch is explicitly reported), and every tested known loss
  or ground-truth pre-enumeration loss is converted to a gap.
- **PASS does not prove:** that surviving anchors establish enumeration
  completeness, that no unknown exact log was deleted, a universal minimum
  retention guarantee, or watcher/kernel completeness.
- **Cleanup/destructive scope:** allow only approved fixture cleanup; do not
  delete task evidence manually merely to finish the run.
- **Approval:** separately required.

### 15.5 #17 — authorization ABA and reader-context loss

- **Question:** can a reader lose and regain visibility while a known task occurs
  without B-S1 invalidating the interval?
- **Hypothesis:** ordinary/privilege-separated/full-token API profiles fail the
  interval proof; the local profile is unaffected by PVE ACL ABA but gaps on its
  own Linux visibility/process-context loss.
- **Fixture/preconditions:** disposable reader identities; exact initial ACL;
  reversible ACL plan; separate local-reader uid/gid/namespace test; a known
  task created only during the hidden interval.
- **Collect:** exact ACL/user/token state, API responses, local file view,
  sentinel state, task ground truth, audit logs, before/during/after timestamps.
- **Falsification:** any profile reports complete after it failed to see the
  known task or after unobserved local visibility loss.
- **PASS:** token/API profiles gap on ABA; local profile either continuously
  sees the task or detects and latches its own context loss.
- **PASS does not prove:** all ACL mutation routes, credential theft/rotation,
  root resistance, or accepted authorization architecture.
- **Cleanup/destructive scope:** restore the exact fixture ACL/local identity and
  verify no production credential changed.
- **Approval:** separately required for each ACL/local-context mutation.

### 15.6 #15 — service reload/restart and loaded code

- **Question:** what happens to task creation, exact logs, active/archive
  handoff, API reads, and loaded code across `pvedaemon`/`pveproxy` HUP reload,
  full restart, and `pvestatd` restart?
- **Hypothesis:** HUP can retain old serving children; B-S1 must detect process
  cohort changes and gap rather than infer new loaded code from package version.
- **Fixture/preconditions:** exact services/units named; long-enough disposable
  tasks; old/new PID census; clean package files; recovery access; split subruns
  for HUP, full stop/start, and `pvestatd`.
- **Collect:** process trees/start times before/during/after, request-serving
  cohort, raw task files/logs, API results, journal, watch events, package/file
  hashes, B-S1 transitions.
- **Falsification:** an old-code worker serves after B-S1 records new-code
  eligibility, or task evidence is silently lost while coverage remains
  complete.
- **PASS:** every tested cohort/reload/restart either remains fully attributable
  or forces a gap/new epoch.
- **PASS does not prove:** every upgrade ordering, crash, node reboot, or code
  equivalence beyond the tested files/behavior.
- **Cleanup/destructive scope:** restore services to active healthy fixture
  state, terminate only fixture tasks, verify no old children remain. Service
  interruption is deliberate and disruptive.
- **Approval:** separately required; never run on a production node under this
  research authorization.

### 15.7 #11 — QEMU synchronous/backing routes

- **Question:** can exact-target supported `PUT .../config`, `qm disk import`,
  `qm importovf`, attach/unlink, or storage moves produce an R/P-relevant actual
  occupant/backing transition without an UPID and without a detectable gap?
- **Hypothesis:** ordinary config edits are irrelevant, but at least one backing
  route may expose a narrower unsupported boundary that B-S1 must reject.
- **Fixture/preconditions:** disposable stopped QEMU guest/disks; exact selected
  storage plugin; route-by-route expected physical state and ADR class agreed in
  advance; no direct-root manipulation.
- **Collect:** command/API result, every task surface, config and storage facts,
  disk/content markers used only as fixture ground truth, B-S1 outcome.
- **Falsification:** a genuine in-scope R/P event completes without a task while
  B-S1 closes complete.
- **PASS:** each route is either non-continuity-relevant, task-covered, or causes
  an explicit unsupported/gap result.
- **PASS does not prove:** untested storage plugins/routes or generic Family B.
- **Cleanup/destructive scope:** destructive to disposable disks/guest; remove
  imports and restore fixture baseline.
- **Approval:** separately required.

### 15.8 #12 — LXC synchronous rootfs/storage routes

- **Question/hypothesis:** same as #11 for `move_volume` versus synchronous
  rootfs/mount-point attach/replace, without assuming config metadata alone is
  occupant replacement.
- **Fixture/preconditions:** disposable stopped LXC, exact storage plugin and
  mount layout, route-by-route physical ground truth.
- **Collect/falsification/PASS:** same discipline as #11; a real R/P transition
  without task or gap falsifies the claimed scope.
- **PASS does not prove:** other plugins, bind mounts, external providers, or
  all LXC backing paths.
- **Cleanup/destructive scope:** destructive to disposable rootfs/mounts; restore
  or delete only fixture assets.
- **Approval:** separately required.

### 15.9 #1 and #2 — normal same-slot R sequences

- **Question:** do normal QEMU (#1) and LXC (#2) create→destroy→create sequences
  produce attributable `create/destroy/create` evidence before operation bodies
  and remain covered through completion/rotation?
- **Hypothesis:** exact source task types appear on the route node; successful
  chain can be retained as candidate positive R evidence only inside complete
  coverage plus independent inventory/context reconciliation.
- **Fixture/preconditions:** separate disposable QEMU/LXC VMIDs, no reused
  production storage, exact source/guest-common/storage contexts, sentinel
  already eligible.
- **Collect:** returned UPIDs, raw exact logs, active/archive/API views, final
  status, config/storage fixture ground truth, sentinel transitions.
- **Falsification:** any normal transition begins/completes without the expected
  evidence and without a gap, or owner/id cannot be bound unambiguously.
- **PASS:** tested normal routes produce and retain exact records; B-S1 never
  infers R merely from name/type equality.
- **PASS does not prove:** alternate create/destroy paths, proof sufficiency, or
  accepted direct-replacement architecture.
- **Cleanup/destructive scope:** destroys/recreates only the disposable guests;
  delete final fixture and its disks/rootfs.
- **Approval:** separately required for #1 and #2.

### 15.10 #5 and #6 — snapshot class-P operations

- **Question:** do QEMU (#5) and LXC (#6) snapshot create/delete/rollback tasks,
  especially rollback, remain fully observable through guest-common lock and
  snapshot core behavior?
- **Hypothesis:** exact outer task types appear; rollback is class P on the same
  `resource_id`/binding and must invalidate/revalidate proof, never create a
  successor.
- **Fixture/preconditions:** disposable guest with test data; exact installed
  `libpve-guest-common-perl` mapped/audited; selected storage supports snapshots;
  no replication/migration lock ambiguity unless that is the planned subcase.
- **Collect:** UPIDs/logs/status, lock contention, snapshot/storage state, task
  surfaces, sentinel result, fixture data before/after.
- **Falsification:** rollback executes without detectable task/gap, or B-S1
  maps it to replacement identity semantics.
- **PASS:** every tested operation is attributable or gaps and rollback retains
  canonical P identity semantics.
- **PASS does not prove:** every storage backend, crash point, or future trust
  revalidation mechanism.
- **Cleanup/destructive scope:** rollback rewinds disposable state; remove
  snapshots and fixture.
- **Approval:** separately required for each guest type.

### 15.11 #7 and #8 — backup restore

- **Question:** do same-locator and new-locator QEMU (#7)/LXC (#8) restore
  variants generate exact attributable tasks across selected storage/archive
  mechanisms, without confusing task name with R/P/N identity evidence?
- **Hypothesis:** `qmrestore`/`vzrestore` appear; new locator is N; same locator
  retains P-style identity unless separately accepted positive R evidence exists.
- **Fixture/preconditions:** disposable backup and guests; exact archive format,
  target collision/force policy, storage/backup-provider plugin ledger; LXC #8
  split into selected filesystem/tar, PBS, or provider subruns rather than a
  false all-backend claim.
- **Collect:** source/target UPIDs, logs/status, archive/storage/plugin context,
  fixture content ground truth, inventory relation evidence, sentinel result.
- **Falsification:** a tested restore executes without task/gap, a variant is
  silently generalized, or B-S1 mints/reuses identity from task label alone.
- **PASS:** selected variants are attributable and canonical R/P/N consequences
  remain evidence-dependent.
- **PASS does not prove:** untested backend/plugin variants or positive
  replacement proof.
- **Cleanup/destructive scope:** destructive overwrite/restore on disposable
  VMIDs; remove restored guests and backups after evidence retention.
- **Approval:** separately required for every subrun.

### 15.12 #3 and #4 — clone class-N operations

- **Question:** do QEMU (#3) and LXC (#4) clone variants produce source-owned
  attributable tasks while the target starts unverified?
- **Hypothesis:** `qmclone`/`vzclone` identify source VMID and owner node; target
  correlation needs parameters/inventory and never copies authority.
- **Fixture/preconditions:** disposable source/target VMIDs and selected linked/
  full/storage variants.
- **Collect:** UPID/log/status, request parameters, source/target storage facts,
  task surfaces, sentinel result.
- **Falsification:** a supported tested clone is silent without gap or target
  cannot be unambiguously correlated while B-S1 claims it can.
- **PASS:** tested variants are attributable and target remains N/unverified.
- **PASS does not prove:** source continuity, copy resistance, or untested
  plugins.
- **Cleanup/destructive scope:** storage allocation; delete clones/fixture.
- **Approval:** separately required.

### 15.13 #9 and #10 — migration Phase-M entry

- **Question:** can QEMU (#9) and LXC (#10) local, remote, HA, online/offline,
  restart, helper, success, and failure paths be joined into a gap-detectable
  source/target handoff?
- **Hypothesis:** primary source-node tasks exist, but helper/wrapper linkage and
  partial failure will expose unresolved Phase-M gaps.
- **Fixture/preconditions:** at least two disposable exact-version nodes,
  independently identified sentinels, compatible disposable shared/local
  storage, exact HA package/context, failure injection and recovery plan.
- **Collect:** every node's files/APIs/watch state, wrapper/source/target UPIDs,
  membership and node-binding context, guest location, clocks, failure timing.
- **Falsification:** migration changes relation while a relevant node/task is
  omitted and the aggregate remains complete; or B-S1 creates a successor.
- **PASS:** tested handoffs either remain complete or gap without changing
  `resource_id`.
- **PASS does not prove:** generic membership/rejoin/version-skew Phase M.
- **Cleanup/destructive scope:** guest movement/storage/network disruption;
  return/delete disposable guests and restore node health.
- **Approval:** separately required; these remain multi-node and need further
  fixture/variant refinement.

### 15.14 #16 — node reboot, intentionally late

- **Question:** can a node reboot during each relevant task phase ever be
  followed by a false clean interval?
- **Hypothesis:** Phase S always gaps on boot-ID change; archive/log survival can
  inform recovery research but cannot auto-heal the old interval.
- **Fixture/preconditions:** disposable node only; exact running kernel source
  now mapped because durability/reboot semantics are load-bearing; console and
  recovery access; task-phase trigger matrix.
- **Collect:** pre/post boot ID, filesystem/task evidence, exact UPID results,
  process/service cohorts, journal, sentinel durable state and heartbeat loss.
- **Falsification:** reboot or rejoin loses evidence while B-S1 reports the old
  interval complete, or node-name equality restores eligibility.
- **PASS:** every tested reboot latches a gap and preserves only honest audit;
  any new epoch requires explicit rebaseline.
- **PASS does not prove:** crash durability on other filesystems/kernels or
  multi-node rejoin.
- **Cleanup/destructive scope:** full node outage and possible task interruption;
  verify fixture/storage integrity and remove test guests.
- **Approval:** separately required; never infer approval from project
  continuation.

## 16. Stop / NO-GO conditions

B-S1 is **NO-GO for its exact claimed Phase-S scope** if one controlled witness
shows any of the following under the stated exact-version, in-scope T1/T2
conditions:

1. a continuity-relevant supported operation completes while no record exists
   on every selected B-S1 enumeration surface and no detectable gap occurs;
2. a created UPID/log can be omitted by watch/scan/traversal/rotation while
   B-S1 still closes the affected interval complete;
3. stock cleanup can silently remove an unknown in-scope exact log before the
   observer durably enumerates it, while no independent watcher/gap signal
   detects the loss and B-S1 can still close the affected interval complete;
4. a task begins before the supposed primary exact-log evidence point;
5. an active→archive or cleanup transition loses a known record without an
   independently detectable gap;
6. reader visibility disappears/reappears without invalidation;
7. daemon/package ABA or old loaded code is accepted as exact current code;
8. the sentinel crashes, loses durable state/coverage, and later reconstructs a
   false clean interval;
9. node reboot/name reuse restores coverage automatically; or
10. an unsupported/unknown task or lifecycle event is silently ignored.

A NO-GO is bounded to B-S1's exact protocol, reader profile, operation scope,
and version/fixture context. It is not “all Family B is impossible,” not a
T3-root-tampering conclusion, and not a claim about broader external mechanisms.

Source research alone may also stop the candidate before experiment if it proves
one of those properties for the exact scope. Conversely, repeated PASS results
cannot prove universal absence without the missing accepted completeness
contract.

## 17. Positive exit requirements

Family B cannot move beyond `UNRESOLVED / NOT FULLY AUDITED` until all of these
are separately closed:

| Gate | Required result |
| --- | --- |
| `SOURCE CONTRACT` | Exact version-scoped route generation, log-before-operation, watcher/filesystem, retention/cleanup, handoff, reader, and no-UPID boundaries established at security strength |
| `RUNTIME VALIDATION` | Controlled adversarial results match the source hypotheses across required interleavings and fixtures |
| `DURABLE SENTINEL CONTRACT` | Crash-safe state/heartbeat/gap/overlap/close semantics defined; false clean reconstruction excluded |
| `AUTHORIZATION CONTRACT` | Interval-wide visibility proven for the selected reader; API ACL ABA or local-reader context loss cannot be silent |
| `SINGLE-NODE COVERAGE` | Phase S complete for its declared versions, QEMU/LXC operations, storage variants, restart states, and unsupported-path behavior |
| `MULTI-NODE COVERAGE` | Phase M separately proves every node/membership/migration/handoff/version-skew case |
| `R/P/N/T OPERATION COVERAGE` | Every supported operation is mapped to the unchanged canonical consequence; ordinary config remains excluded; gaps never create replacement |
| `RESTART/REBOOT/GAP SEMANTICS` | Service reload/full restart, observer crash, cleanup, node reboot/rejoin, and version change always invalidate or preserve coverage exactly as contracted |
| `TRUST CONTEXT` | Conditional source-attestation and node-trust dependencies, CAS fields, consumption-time checks, revocation, and revalidation are closed without collapsing axes |
| `REVIEWED POSITIVE ADR` | A different future ADR selects a sufficient mechanism, survives architecture/security review, and is ACCEPTED before any B1 implementation |

Experiments alone satisfy none of the final architecture gates. Until every
applicable gate closes, `security_continuity=trusted` remains unavailable.

## 18. Explicit unresolved questions

1. Does exact kernel/filesystem watch delivery on the target stack guarantee
   every required task-log create/move/delete event or a reliable overflow/loss
   indication?
2. Can watch-first recursive scan establish a race-free T0/T1 logical barrier
   across bucket creation, file creation, cleanup, and mount changes?
3. Can any finite file/API repeated traversal exclude an unknown omitted record
   without relying on watcher completeness?
4. Can the prospective watch/scan contract durably enumerate every in-scope
   exact log before stock mtime-bound cleanup can silently remove it, or emit an
   independent gap signal for every such loss? Anchor survival cannot answer
   this question.
5. Can the structurally privileged local reader receive an accepted,
   independently gap-detectable identity/permission/watchdog contract without
   becoming an unjustified root oracle?
6. Can loaded Perl code be validated strongly enough without a disruptive full
   restart, and how are old HUP-surviving workers excluded?
7. Does the required audit of exact `libpve-guest-common-perl` 6.0.5
   `PVE::AbstractConfig` and `PVE::GuestHelpers` close snapshot/lock behavior
   for #5/#6?
8. Which LXC restore storage/archive/provider variants are in #8's first fixture,
   and what additional package/plugin sources do they import?
9. Can an actual class-R or class-P physical transition occur through an exact
   supported no-UPID QEMU/LXC route at T1/T2?
10. Which alternate `fork_worker` caller paths, if any, fall within a claimed
    lifecycle route and do they preserve the exact pinned log-before-body order?
11. What exact task generator safely produces #13's required volume without
    broadening lifecycle or storage scope?
12. How would a future backend atomically make dependent authority ineligible
    before a local sentinel heartbeat gap can be consumed? No implementation is
    authorized here.
13. Can Phase M ever establish stable per-node ownership across join/remove/
    rejoin/name reuse and migration helper chains?
14. Which exact kernel commit/filesystem durability contract becomes
    load-bearing for #16?

Experiments still needing material pre-execution refinement are #8 (selected
LXC storage/provider variants), #9/#10 (multi-node topology and failure matrix),
#11/#12 (route-by-route physical continuity relevance and plugin matrix), #13
(safe bounded task generator and load budget), #15 (exact phase-duration and
request-serving-worker attribution probes), #16 (kernel source/durability and
reboot phase matrix), and #17's local-reader context-loss subrun. The remaining
experiments are specified well enough for fixture-specific runbooks only after
separate approval and preflight.

## 19. Research conclusion

B-S1 is a plausible, precisely falsifiable **Phase-S candidate**, not a proven
mechanism. Its strongest move is to enumerate exact UPID-log creation locally,
before the worker body begins, and treat mutable API pagination as corroboration
rather than completeness authority. That move removes neither the need for a
version-scoped watcher/retention contract nor the unsupported no-UPID route,
loaded-code, local-reader, crash, authorization, and multi-node gaps.

The honest current output is therefore:

```text
B-S1:       PLAUSIBLE CANDIDATE / NOT PROVEN
Phase S:    DESIGN ONLY / REQUIRES CONTROLLED FALSIFICATION
Phase M:    NOT DESIGNED / NOT SOLVED
Family B:   UNRESOLVED / NOT FULLY AUDITED
Blocker B:  OPEN
WAVE B1:    DEFERRED / NOT AUTHORIZED
Phase 1C:   BLOCKED
R0:         GO / STRICTLY READ-ONLY
trusted:    GRANTED NOWHERE
```

## 20. Execution statement

This pass performed repository, architecture, and official upstream source
review only. It made zero live PVE calls, zero live Home Assistant calls, zero
private-network connections, zero PVE/guest/storage/kernel lifecycle actions,
zero service reloads/restarts, zero Research #2B experiment executions, zero
fixture mutations, and zero trusted enrollments. No real fixture was selected or
modified. Every proposed experiment remains subject to separate operator
approval.
