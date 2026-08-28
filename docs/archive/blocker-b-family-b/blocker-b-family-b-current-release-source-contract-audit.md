> **ARCHIVED / SUPERSEDED RESEARCH — do not read by default.**
> This document is part of the abandoned Family B / B-S1 task-history witness
> path. B-S1 is **NO-GO** as the mutation-authority path, Experiment #13 was
> never executed, and Blocker B remains **OPEN**. Nothing here is architecture
> authority or a current roadmap. Read
> `docs/archive/postmortems/blocker-b-family-b-13.md` first, and
> `docs/archive/blocker-b-family-b/README.md` for why this is archived.
> The original document text follows unchanged.

# NON-NORMATIVE RESEARCH / EVIDENCE

# Family B current-release/source-contract audit

## 1. Question, scope, and non-authority notice

This document records Family B Research #2A: a version-pinned audit of official
Proxmox source for PVE task-history behavior and the supported operations that
do, or do not, create task records.

This document:

- does not amend any ACCEPTED ADR;
- does not authorize implementation;
- does not close Blocker B;
- does not authorize WAVE B1;
- does not grant `security_continuity=trusted`;
- does not unblock Phase 1C; and
- does not change R0 from strictly read-only.

Research is non-normative. Accepted ADR0001, ADR0002, ADR0005, and ADR0006 remain
the architecture contract. In particular:

- `R` is same-slot potential replacement;
- `P` is rollback or same-resource restore and retains the same `resource_id`
  and binding;
- `N` is clone or restore to a new locator and starts unverified;
- `T` is migration, preserves `resource_id`, and changes the node relation; and
- a coverage gap is never positive replacement evidence.

The question is not whether Family B can be made to succeed. It is what the
current target source proves, what it disproves within a precise scope, and what
remains unknown before any separately approved experiment campaign.

## 2. Repository base and provenance

| Item | Result |
| --- | --- |
| Expected `origin/main` | `aee065b68ba52040370071361e544dd936127015` |
| Actual `origin/main` at research start | `aee065b68ba52040370071361e544dd936127015` |
| Main moved | No |
| Local branch | `research/family-b-2a-source-contract-audit` |
| Worktree at start | Clean |
| Source retrieval | Fresh clones of official `github.com/proxmox/*` read-only mirrors |
| Live PVE/HA access | None |
| Experiments #1--#17 | None executed |

The SHA supplied in the Research #2A handoff for `qemu-server` 9.2.6 omitted one
`a3` group and did not identify a Git object. The repository's Research #1
artifact already contained the valid 40-character SHA
`e6352be67f70042a7433a3a3c712b36d02f9f7cb`. This audit independently mapped
the 9.2.6 changelog entry to that valid SHA. This is a provenance correction,
not an architecture change.

The accepted repository state was re-read before research and remained:

```text
Family B:  UNRESOLVED / NOT FULLY AUDITED
Blocker B: OPEN
WAVE B1:   DEFERRED / NOT AUTHORIZED
Phase 1C:  BLOCKED
R0:        GO / STRICTLY READ-ONLY
```

## 3. Research methodology

The audit used four evidence classes, with exactly one primary class per
finding:

| Class | Meaning in this document |
| --- | --- |
| `FACT-SOURCE` | Established in an identified immutable upstream revision. |
| `FACT-DOC` | Explicitly established in official version-relevant documentation. |
| `INFERENCE` | Bounded conclusion derived from cited facts; not an upstream contract. |
| `UNKNOWN` | Not proven at ADR0005/ADR0006 security strength. |

Annotations such as `CURRENT-RELEASE MAPPING UNKNOWN`, `OPERATOR READ REQUIRED`,
and `REVERIFIED THIS RESEARCH PASS` do not replace the primary class.

The method was:

1. map known installed versions to changelog bump commits in official mirrors;
2. trace API and supported CLI entry points to their actual worker creation;
3. trace task start, active state, archive append, rotation, cluster publication,
   exact-UPID lookup, and cleanup;
4. construct only bounded concurrency inferences from that source; and
5. retain `UNKNOWN` wherever package mapping, runtime behavior, or a security
   completeness contract was absent.

No source observation was promoted into an accepted trust mechanism.

## 4. Exact target package/source mapping ledger

| Component | Installed/target version | Exact upstream tag/commit | Mapping evidence | Classification/status |
| --- | --- | --- | --- | --- |
| `qemu-server` | `INSTALLED VERSION UNKNOWN` | Audited 9.2.6 [`e6352be67f70042a7433a3a3c712b36d02f9f7cb`](https://github.com/proxmox/qemu-server/commit/e6352be67f70042a7433a3a3c712b36d02f9f7cb) | Exact `debian/changelog` bump proves the 9.2.6-to-commit mapping; repository evidence does not establish the target host's installed `qemu-server` version | `FACT-SOURCE` for the audited revision; `REVERIFIED THIS RESEARCH PASS`; `UNKNOWN`; `CURRENT-RELEASE MAPPING UNKNOWN`; `OPERATOR READ REQUIRED` |
| `pve-manager` | 9.2.11 | [`f6997e698c7933ea8e62319e2bf1bf7262daa56a`](https://github.com/proxmox/pve-manager/commit/f6997e698c7933ea8e62319e2bf1bf7262daa56a) | Exact `debian/changelog` bump | `FACT-SOURCE`; `REVERIFIED THIS RESEARCH PASS` |
| `pve-cluster` | 9.1.6 | [`7091d92e594952dba65c1e57568b3d7cc244e960`](https://github.com/proxmox/pve-cluster/commit/7091d92e594952dba65c1e57568b3d7cc244e960) | Exact `debian/changelog` bump | `FACT-SOURCE`; `REVERIFIED THIS RESEARCH PASS` |
| `pve-common` | `INSTALLED VERSION UNKNOWN` | Audited 9.1.21 [`5054082fe492429fc37574985c2ca812af9a3125`](https://github.com/proxmox/pve-common/commit/5054082fe492429fc37574985c2ca812af9a3125) and 9.2.1 [`f665029eac78022e81810ab2e44eace57ade13fb`](https://github.com/proxmox/pve-common/commit/f665029eac78022e81810ab2e44eace57ade13fb) | `pve-manager` 9.2.11 requires `libpve-common-perl (>= 9.1.21)`; relevant task files are identical at these audited endpoints | `UNKNOWN`; `CURRENT-RELEASE MAPPING UNKNOWN`; `OPERATOR READ REQUIRED` |
| `pve-container` | `INSTALLED VERSION UNKNOWN` | Audited 6.1.6 [`0a04a1213adc2cffd5445a86da8bd10a1e2d879d`](https://github.com/proxmox/pve-container/commit/0a04a1213adc2cffd5445a86da8bd10a1e2d879d) through 6.1.13 [`c8132559faedb76a56498d411bf3e024c1ff07e7`](https://github.com/proxmox/pve-container/commit/c8132559faedb76a56498d411bf3e024c1ff07e7) | `pve-manager` 9.2.11 requires `pve-container (>= 6.1.6)`; task worker names/routes were checked across release bumps | `UNKNOWN`; `CURRENT-RELEASE MAPPING UNKNOWN`; `OPERATOR READ REQUIRED` |
| `pve-storage` | `INSTALLED VERSION UNKNOWN` | Audited 9.1.9 [`c403d5f6793cc3dd4bc2168d0205b211d6295903`](https://github.com/proxmox/pve-storage/commit/c403d5f6793cc3dd4bc2168d0205b211d6295903) | Current audited source, not an installed-version mapping | `UNKNOWN`; `CURRENT-RELEASE MAPPING UNKNOWN`; `OPERATOR READ REQUIRED` |
| `pve-kernel` / running kernel | `7.0.14-12-pve` was previously recorded | No exact source commit established | Version string is repository provenance, not a verified source mapping | `UNKNOWN`; `OPERATOR READ REQUIRED` if running-kernel confirmation becomes material |
| `pve-access-control` | `INSTALLED VERSION UNKNOWN` | Audited 9.1.1 [`5ccd07d9302562b73374d331b63d25b04b86766c`](https://github.com/proxmox/pve-access-control/commit/5ccd07d9302562b73374d331b63d25b04b86766c) | Load-bearing authorization dependency; `pve-manager` 9.2.11 requires `>= 9.1.1~` | `UNKNOWN`; `CURRENT-RELEASE MAPPING UNKNOWN`; `OPERATOR READ REQUIRED` |
| `pve-ha-manager` | `INSTALLED VERSION UNKNOWN` | Audited 5.2.5 [`c73364c19d5317e6df5bb1c1b727d080a5e897ef`](https://github.com/proxmox/pve-ha-manager/commit/c73364c19d5317e6df5bb1c1b727d080a5e897ef) | Load-bearing only for HA-managed migration variants; not mapped to target | `UNKNOWN`; `CURRENT-RELEASE MAPPING UNKNOWN`; `OPERATOR READ REQUIRED` |

The CT112 package database and kernel were deliberately not used as production
PVE evidence. Exact installed-to-source mapping is complete for `pve-manager`
and `pve-cluster`. The `qemu-server` 9.2.6-to-commit mapping and source behavior
are exact for that audited upstream release, but its identity with the package
installed on the target host is unconfirmed. Other load-bearing package
mappings also remain incomplete.

## 5. QEMU operation-to-task matrix

All rows in this section are pinned to the audited upstream `qemu-server` 9.2.6
revision unless a row says otherwise. They are `FACT-SOURCE` for that revision;
their direct applicability to the installed target is `CURRENT-RELEASE MAPPING
UNKNOWN` until an operator read confirms the installed `qemu-server` version.
The main source is
[`src/PVE/API2/Qemu.pm`](https://github.com/proxmox/qemu-server/blob/e6352be67f70042a7433a3a3c712b36d02f9f7cb/src/PVE/API2/Qemu.pm).

| Operation | API/CLI route | Task generated? | Worker/task type | Owner node / UPID id | Alternate supported bypass? | Evidence | Classification |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Create | `POST /nodes/{node}/qemu` without archive | YES | `qmcreate` | Executing/route node; target VMID | `qm importovf` creates/imports synchronously without a worker | `create_vm`; [`PVE/CLI/qm.pm`](https://github.com/proxmox/qemu-server/blob/e6352be67f70042a7433a3a3c712b36d02f9f7cb/src/PVE/CLI/qm.pm) `importovf` | `FACT-SOURCE` |
| Destroy | `DELETE /nodes/{node}/qemu/{vmid}` | YES | `qmdestroy` | Executing node; source VMID | No supported API/CLI destroy bypass found in audited component | `destroy_vm` | `FACT-SOURCE` |
| Clone, linked/full/snapshot/storage/target variants | `POST /nodes/{node}/qemu/{vmid}/clone` | YES | `qmclone` | Source/route node; source VMID | No supported clone bypass found | `clone_vm` | `FACT-SOURCE` |
| Snapshot create | `POST /nodes/{node}/qemu/{vmid}/snapshot` | YES | `qmsnapshot` | Route node; VMID | None found | [`src/PVE/API2/Qemu.pm`](https://github.com/proxmox/qemu-server/blob/e6352be67f70042a7433a3a3c712b36d02f9f7cb/src/PVE/API2/Qemu.pm) `snapshot` | `FACT-SOURCE` |
| Snapshot delete | `DELETE /nodes/{node}/qemu/{vmid}/snapshot/{snapname}` | YES | `qmdelsnapshot` | Route node; VMID | None found | [`src/PVE/API2/Qemu.pm`](https://github.com/proxmox/qemu-server/blob/e6352be67f70042a7433a3a3c712b36d02f9f7cb/src/PVE/API2/Qemu.pm) `delsnapshot` | `FACT-SOURCE` |
| Snapshot rollback | `POST /nodes/{node}/qemu/{vmid}/snapshot/{snapname}/rollback` | YES | `qmrollback` | Route node; VMID | None found | [`src/PVE/API2/Qemu.pm`](https://github.com/proxmox/qemu-server/blob/e6352be67f70042a7433a3a3c712b36d02f9f7cb/src/PVE/API2/Qemu.pm) `rollback` | `FACT-SOURCE` |
| Backup restore, same or new VMID, including `force` overwrite and storage variants | `POST /nodes/{node}/qemu` with `archive` | YES | `qmrestore` | Target/route node; target VMID | No supported restore bypass found; `qmrestore` CLI calls this API method | `create_vm`; CLI `qmrestore` | `FACT-SOURCE` |
| Local migration, online/offline/storage variants | `POST /nodes/{node}/qemu/{vmid}/migrate` | CONDITIONAL | `qmigrate`; HA request wrapper is `hamigrate` | Source/route node; VMID | HA path queues separate HA work; target helper is not always a target-node task | `migrate_vm` | `FACT-SOURCE` |
| Remote migration source | `POST /nodes/{node}/qemu/{vmid}/remote_migrate` | YES | `qmigrate` | Source node; source VMID | None found for source operation | `remote_migrate_vm` | `FACT-SOURCE` |
| Remote migration target tunnel | target `POST /nodes/{node}/qemu/{vmid}/mtunnel` | YES | `qmtunnel` | Target node; target VMID | Local-cluster target uses CLI `qm mtunnel`, not a target UPID | `mtunnel`; CLI `mtunnel` | `FACT-SOURCE` |
| Move disk to storage | `POST /nodes/{node}/qemu/{vmid}/move_disk` | YES | `qmmove` | Route node; source VMID | Synchronous config/import routes can alter effective backing without `qmmove` | `move_vm_disk` | `FACT-SOURCE` |
| Reassign disk to another VM | same `move_disk` route with target VM | YES | `qmmove` | Route node; composite source/target disk id | Synchronous config route can attach/unlink disks | `move_vm_disk` | `FACT-SOURCE` |
| Asynchronous config update | `POST /nodes/{node}/qemu/{vmid}/config` | YES | `qmconfig` | Route node; VMID | Yes: the equivalent `PUT` route is synchronous | `update_vm_async` | `FACT-SOURCE` |
| Synchronous config update, including supported disk attach/change/unlink | `PUT /nodes/{node}/qemu/{vmid}/config`; `qm unlink` | NO | None | No UPID | This is itself the supported bypass | `update_vm`; CLI `unlink` | `FACT-SOURCE` |
| Disk import | `qm disk import` / `qm importdisk` | NO | None | No UPID | This is a supported synchronous CLI path | CLI `importdisk` | `FACT-SOURCE` |
| OVF import/create | `qm importovf` | NO | None | No UPID | This is a supported synchronous CLI path | CLI `importovf` | `FACT-SOURCE` |

For every `YES` row, the named API/CLI handler is the UPID creator through
`fork_worker`; the executing REST environment supplies the owner node and the
authenticated user is encoded as task owner. The worker `id` shown above is a
locator/operation attribute, not durable inventory identity. HA and remote
migration helpers are the noted conditional cases.

`INFERENCE`: an UPID-only event stream does not contain an event for every
supported QEMU creation/backing/configuration route in 9.2.6. A task record
cannot later be recovered for an operation that never called `fork_worker`.
This is negative route-coverage evidence; it does not establish that a
particular no-UPID route is continuity-relevant under ADR0006 §4b/§4c or that
every possible stateful repeated-traversal/overlap protocol is impossible.

This is a precise limitation, not a claim that every row is continuity-relevant
in every context. Ordinary config mutation is not continuity-relevant under
ADR0006, and this pass did not source-prove an actual physical/logical workload
replacement sequence whose continuity-relevant transition occurs through a
no-UPID route. The result does prove that a future task-only design cannot
silently assume all supported backing/configuration routes create tasks.

For restore classification, `FACT-SOURCE` establishes only that `qmrestore` is
owned by the target/route node and attributes the UPID to the target VMID,
including `force` and storage variants. `INFERENCE` from the accepted lifecycle
model, not from PVE task naming, classifies a new-locator restore as `N`. For a
same-locator restore, accepted positive replacement evidence invokes the class-R
backend identity transition; without it, the backend retains the read-only
identity/binding with class-P-style fail-closed continuity. Evidence controls
the backend recognition/identity consequence, not whether physical replacement
actually occurred.

## 6. LXC operation-to-task matrix

The exact installed `pve-container` version is unknown. Task-generation names
and the listed worker calls were checked at every 6.1.6--6.1.13 release bump;
route/variant details below are quoted from 6.1.13 and remain mapping-bounded.
The main sources are
[`src/PVE/API2/LXC.pm`](https://github.com/proxmox/pve-container/blob/c8132559faedb76a56498d411bf3e024c1ff07e7/src/PVE/API2/LXC.pm),
[`Snapshot.pm`](https://github.com/proxmox/pve-container/blob/c8132559faedb76a56498d411bf3e024c1ff07e7/src/PVE/API2/LXC/Snapshot.pm),
and
[`Config.pm`](https://github.com/proxmox/pve-container/blob/c8132559faedb76a56498d411bf3e024c1ff07e7/src/PVE/API2/LXC/Config.pm).

| Operation | API/CLI route | Task generated? | Worker/task type | Owner node / UPID id | Alternate supported bypass? | Evidence | Classification |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Create | `POST /nodes/{node}/lxc` without archive | YES | `vzcreate` | Executing/route node; target VMID | No separate create bypass found | `create_vm` | `FACT-SOURCE`; `CURRENT-RELEASE MAPPING UNKNOWN` |
| Destroy | `DELETE /nodes/{node}/lxc/{vmid}` | YES | `vzdestroy` | Route node; source VMID | None found | `destroy_vm` | `FACT-SOURCE`; `CURRENT-RELEASE MAPPING UNKNOWN` |
| Clone, linked/full/storage/target variants | `POST /nodes/{node}/lxc/{vmid}/clone` | YES | `vzclone` | Source/route node; source VMID | None found | `clone_vm` | `FACT-SOURCE`; `CURRENT-RELEASE MAPPING UNKNOWN` |
| Snapshot create | `POST /nodes/{node}/lxc/{vmid}/snapshot` | YES | `vzsnapshot` | Route node; VMID | None found | `snapshot` | `FACT-SOURCE`; `CURRENT-RELEASE MAPPING UNKNOWN` |
| Snapshot delete | `DELETE /nodes/{node}/lxc/{vmid}/snapshot/{snapname}` | YES | `vzdelsnapshot` | Route node; VMID | None found | `delsnapshot` | `FACT-SOURCE`; `CURRENT-RELEASE MAPPING UNKNOWN` |
| Snapshot rollback | `POST /nodes/{node}/lxc/{vmid}/snapshot/{snapname}/rollback` | YES | `vzrollback` | Route node; VMID | None found | `rollback` | `FACT-SOURCE`; `CURRENT-RELEASE MAPPING UNKNOWN` |
| Backup restore, same/new VMID and storage variants | `POST /nodes/{node}/lxc` with `restore=1` and `ostemplate` archive | YES | `vzrestore` | Target/route node; target VMID | None found | `create_vm` | `FACT-SOURCE`; `CURRENT-RELEASE MAPPING UNKNOWN` |
| Local migration, online/restart/offline/storage variants | `POST /nodes/{node}/lxc/{vmid}/migrate` | CONDITIONAL | `vzmigrate`; HA request wrapper `hamigrate` | Source/route node; VMID | HA path delegates to HA manager | `migrate_vm` | `FACT-SOURCE`; `CURRENT-RELEASE MAPPING UNKNOWN` |
| Remote migration source | `POST /nodes/{node}/lxc/{vmid}/remote_migrate` | YES | `vzmigrate` | Source node; source VMID | None found | `remote_migrate_vm` | `FACT-SOURCE`; `CURRENT-RELEASE MAPPING UNKNOWN` |
| Remote migration target tunnel | target `POST /nodes/{node}/lxc/{vmid}/mtunnel` | YES | `vzmtunnel` | Target node; target VMID | Local/remote variant details depend on calling path | `mtunnel` | `FACT-SOURCE`; `CURRENT-RELEASE MAPPING UNKNOWN` |
| Rootfs/mount-point move or volume reassign | `POST /nodes/{node}/lxc/{vmid}/move_volume` | YES | `move_volume` | Route node; source VMID | Synchronous config can attach/replace rootfs/mount-point references | `move_volume` | `FACT-SOURCE`; `CURRENT-RELEASE MAPPING UNKNOWN` |
| Resize | `PUT /nodes/{node}/lxc/{vmid}/resize` | YES | `resize` | Route node; VMID | Not a replacement claim by itself | `resize_vm` | `FACT-SOURCE`; `CURRENT-RELEASE MAPPING UNKNOWN` |
| Rootfs/mount-point config attach/change | `PUT /nodes/{node}/lxc/{vmid}/config` | NO | None | No UPID | This is the supported synchronous path | `update_vm` | `FACT-SOURCE`; `CURRENT-RELEASE MAPPING UNKNOWN` |

As with QEMU, the named handler creates each `YES` task through `fork_worker` on
the executing node and encodes the authenticated user in the UPID. VMID remains
a reusable locator; none of these task ids is a `resource_id`.

Research #1's expected LXC snapshot labels were not exact. The audited source
uses `vzsnapshot`, `vzdelsnapshot`, and `vzrollback`. This correction changes no
canonical `P` semantics: snapshot rollback retains the same `resource_id` and
binding, while continuity proof fails closed pending any future revalidation.

The LXC route conclusions cannot be called exact-current-target facts until the
installed `pve-container` version is read and mapped.

For LXC restore, the audited source range supports archive restore through
`vzrestore`, including forced overwrite of a stopped container and storage
selection. `INFERENCE` applies the same accepted new-locator `N` and
same-locator evidence-dependent backend consequence; the `vzrestore` label is
not replacement evidence, and evidence does not determine whether physical
replacement occurred.

The installed `pve-storage` revision is not pinned. Plugin-specific allocation,
copy, import, and migration internals therefore remain `UNKNOWN` where their
behavior would be load-bearing. This audit does establish the caller-level fact
that supported QEMU and LXC synchronous configuration paths can make newly
allocated/imported backing effective without creating a task. It does not use
direct root shell or out-of-band storage manipulation as negative route-coverage
evidence.

## 7. `/cluster/tasks`

The exact target sources are
[`PVE/API2/Cluster.pm`](https://github.com/proxmox/pve-manager/blob/f6997e698c7933ea8e62319e2bf1bf7262daa56a/PVE/API2/Cluster.pm)
and
[`src/PVE/Cluster.pm`](https://github.com/proxmox/pve-cluster/blob/7091d92e594952dba65c1e57568b3d7cc244e960/src/PVE/Cluster.pm).

| Property | Finding | Classification |
| --- | --- | --- |
| Data source | `GET /cluster/tasks` returns `PVE::Cluster::get_tasklist()`, a pmxcfs per-node status KV cache, not `/var/log/pve/tasks/index`. | `FACT-SOURCE` |
| Per-node publisher | `pvestatd` calls `active_workers()` and broadcasts the resulting list on its recurring status cycle; worker start/completion also attempts publication through the REST environment. | `FACT-SOURCE` |
| Publication cadence | `pvestatd`'s normal update loop publishes about every ten seconds, in addition to event-driven attempts. | `FACT-SOURCE` |
| Per-node bound | `broadcast_tasklist` removes oldest entries until serialized JSON is below 32 KiB. | `FACT-SOURCE` |
| Aggregation | `get_tasklist` iterates current node membership and concatenates each available per-node list. It does not globally sort the result. | `FACT-SOURCE` |
| Active/finished content | The published `active_workers` result contains all detected-running tasks and at most 25 recent finished tasks. | `FACT-SOURCE`; pve-common target mapping unknown |
| API pagination/order contract | The route has no `start`, `limit`, cursor, generation, or snapshot parameter. Per-node descending order is expected, but the aggregate is node-by-node concatenation. | `FACT-SOURCE` |
| Authorization | `Sys.Audit` at `/` returns all cached entries. Otherwise token-owned task records are reduced to their base user and compared to the exact authenticated id: a base user sees its own and its tokens' tasks, while a token caller does not match even its own token-owned record through this fallback. | `FACT-SOURCE` |
| Publication/node failure | A failed per-node status read is logged and that node is skipped; a successful response for the remaining nodes is still possible. Worker-side broadcast errors are logged. | `FACT-SOURCE` |
| Detectable completeness/loss | The API returns no node-set snapshot, omitted-node marker, truncation marker, stable cursor, or loss generation. | `FACT-SOURCE` |

`INFERENCE`: a successful `/cluster/tasks` response does not prove complete
interval history. It is a bounded, asynchronously published recent cache whose
contents are authorization-filtered and whose node contribution may be absent.
This reverifies Research #1 at the exact target `pve-manager` and `pve-cluster`
revisions.

## 8. Per-node `active`, `archive`, and `all`

The exact target route implementation is
[`PVE/API2/Tasks.pm`](https://github.com/proxmox/pve-manager/blob/f6997e698c7933ea8e62319e2bf1bf7262daa56a/PVE/API2/Tasks.pm).
Its state writer is in audited `pve-common`
[`RESTEnvironment.pm`](https://github.com/proxmox/pve-common/blob/f665029eac78022e81810ab2e44eace57ade13fb/src/PVE/RESTEnvironment.pm),
for which exact installed mapping remains unknown.

### `source=active`

| Property | Finding | Classification |
| --- | --- | --- |
| State source | Reads `/var/log/pve/tasks/active` through `PVE::INotify::read_file('active')`. | `FACT-SOURCE`; `CURRENT-RELEASE MAPPING UNKNOWN` for writer/parser |
| Active selection | The API includes entries not marked `saved`; saved completed entries are skipped because archive enumeration follows. | `FACT-SOURCE` |
| Ordering | Iterates the active file's array order; `active_workers` constructs running entries followed by up to 25 finished entries, with no immutable ordering token. | `FACT-SOURCE`; writer mapping unknown |
| Pagination | Shared `start` offset and `limit` (default 50) are applied to the mutable list. | `FACT-SOURCE` |
| Filters | Supports errors, VMID, user, type, and time filters after decoding entries. | `FACT-SOURCE` |
| Authorization | `Sys.Audit` at `/nodes/{node}` sees all. Otherwise a base user sees its own and its tokens' tasks; a token sees only tasks owned by that exact token. | `FACT-SOURCE` |
| Snapshot/completeness anchor | No immutable snapshot token, generation, sequence, high-water mark, or continuation token is exposed. | `FACT-SOURCE` |

### `source=archive`

| Property | Finding | Classification |
| --- | --- | --- |
| Files read | Reverse traversal of `/var/log/pve/tasks/index`, followed by `/var/log/pve/tasks/index.1`. No additional generation is read. | `FACT-SOURCE` |
| Append | Completed unsaved task records are appended to `index` under `.active.lock`, then marked saved in the active state. | `FACT-SOURCE`; `CURRENT-RELEASE MAPPING UNKNOWN` |
| Ordering | Physical append is chronological; API reverse traversal yields newest first, subject to concurrent change. | `FACT-SOURCE` |
| Offset/limit | The same numeric `start` and `limit` counters are applied while reverse-reading mutable files. | `FACT-SOURCE` |
| Rotation trigger | After append/close, when `index` exceeds 50,000 bytes, the writer calls `rename(index, index.1)` without checking its return. A successful same-filesystem rename replaces the previous retained generation. | `FACT-SOURCE`; `CURRENT-RELEASE MAPPING UNKNOWN` |
| Retention | The API reads only current `index` and prior `index.1`; under successful normal rotation, older list history is overwritten. | `FACT-SOURCE`; `CURRENT-RELEASE MAPPING UNKNOWN` |
| API generation | Neither filename identity, inode, rotation count, archive epoch, nor generation is returned by the API. | `FACT-SOURCE` |
| Stable offsets | Offsets name positions in the current reverse traversal, not durable records or a committed archive generation. | `INFERENCE` |
| Crash/partial-write durability | The audited code closes the append before marking saved, but no API-level durable transaction or externally consumable commit token is established. Filesystem/crash outcomes for the installed target are not proven. | `UNKNOWN` |

The rotation source is stronger than merely observing two filenames: it proves
normal whole-generation replacement. It does not turn `index.1` into a cursor
because the API exposes no generation identity and silently traverses whichever
files exist at each request.

### `source=all`

| Property | Finding | Classification |
| --- | --- | --- |
| Combination | Reads active first, skipping entries marked saved, then reverse-reads archive with the same counters. | `FACT-SOURCE` |
| De-duplication | The `saved` flag is the handoff convention; the route does not take a cross-surface immutable snapshot or maintain an API-level UPID de-duplication set. | `FACT-SOURCE` |
| Atomicity | Active and archive are separate reads. No shared read lock spans the API union. | `FACT-SOURCE` |
| Concurrent completion | A worker may move between the surfaces while the request is enumerating them; exact externally observable duplication/omission for every interleaving is not specified as a contract. | `UNKNOWN` |
| Stable boundary | No union snapshot, generation, cursor, high-water mark, or predecessor relation exists in the response. | `FACT-SOURCE` |

`INFERENCE`: combining two mutable, incomplete surfaces does not establish a
complete surface. The normal writer ordering reduces some handoff races, but it
does not supply the consumer with an immutable enumeration boundary.

## 9. Exact-UPID status and log

The route is in exact target
[`PVE/API2/Tasks.pm`](https://github.com/proxmox/pve-manager/blob/f6997e698c7933ea8e62319e2bf1bf7262daa56a/PVE/API2/Tasks.pm).
UPID encoding and log storage are in audited
[`PVE/UPID.pm`](https://github.com/proxmox/pve-common/blob/f665029eac78022e81810ab2e44eace57ade13fb/src/PVE/UPID.pm).

| Property | Finding | Classification |
| --- | --- | --- |
| UPID content | Encodes owner node, PID, process start tick, task start time, worker type, id, and user. | `FACT-SOURCE`; pve-common target mapping unknown |
| Routing/owner node | The node encoded in the UPID must match the route node; the route is proxied to that node. | `FACT-SOURCE` |
| Authorization | A base user may read its own and its tokens' tasks; a token may read only that exact token's tasks; otherwise `Sys.Audit` at `/nodes/{node}` is required. | `FACT-SOURCE` |
| File backing | Log filename is derived from the UPID beneath `/var/log/pve/tasks/<hex-bucket>/<UPID>`. Status reads the process state and final task-log status. | `FACT-SOURCE`; pve-common target mapping unknown |
| Status missing-file result | Status rejects the request with `no such task`. | `FACT-SOURCE` |
| Log missing-file result | Normal log listing can return a successful one-line `unable to open file` result; download/open failure has different error handling. | `FACT-SOURCE` |
| List disappearance | Task log files are separate from `index`/`index.1`, so an already-known UPID may remain readable after it disappears from lists. | `FACT-SOURCE`; pve-common mapping unknown |
| Cleanup | Exact target `pveupdate` daily cleanup uses the first retained `index.1` end time as an age boundary and unlinks older task log files. | `FACT-SOURCE` |
| Restart/reboot | Ordinary service restart or reboot source does not itself delete archive/log files; actual crash durability and the installed service/unit behavior are not fully established. | `INFERENCE` |
| Rejoin/name reuse | No durable node incarnation or rejoin generation is encoded in a UPID. Exact behavior after reinstall, removal/rejoin, or node-name reuse is not proven. | `UNKNOWN` |

`INFERENCE`: exact-UPID persistence gives only a lookup property for a
previously known identifier. It does not enumerate unknown tasks, prove that no
UPID was omitted, or provide a monotonic ledger cursor.

## 10. Active-to-archive handoff

The audited `pve-common` source orders normal completion handling as follows:

1. worker creation allocates the UPID and child process;
2. the parent sends `OK`, allowing the child worker function to begin;
3. the parent then calls `active_workers(new_upid)` and attempts cluster
   publication;
4. completion reaping calls `active_workers(upid)` again;
5. under `.active.lock`, a stopped task receives end time/status;
6. unsaved completion lines are appended and the archive file is closed;
7. rotation may rename `index` to `index.1`;
8. the task is retained as `saved` among the active file's recent finished
   entries; and
9. cluster publication is attempted after the state update.

| Finding | Classification |
| --- | --- |
| The child can begin the lifecycle function before the parent publishes the UPID into active state. | `FACT-SOURCE`; `CURRENT-RELEASE MAPPING UNKNOWN` |
| Archive append and close precede the active-file saved update in the normal completion path under one `.active.lock`; append error resets `saved` so a later scan can retry. | `FACT-SOURCE`; `CURRENT-RELEASE MAPPING UNKNOWN` |
| Cluster broadcast is outside that file-state critical section and errors are logged rather than making the worker start/completion fail. | `FACT-SOURCE`; `CURRENT-RELEASE MAPPING UNKNOWN` |
| The source does not expose the lock, a commit sequence, or a traversal snapshot to API consumers. | `FACT-SOURCE` |
| A gapless, durable, externally observable active-to-archive transition at ADR0006 security strength is established. | `UNKNOWN` |

The normal write ordering is useful implementation evidence. It is not a
durable external observation contract, particularly across process death,
filesystem failure, archive rotation, and non-atomic API reads.

## 11. Pagination and concurrent changes

No audited task enumeration route exposes an immutable snapshot, opaque cursor,
continuation token, generation, high-water mark, predecessor relation, or
monotonic task sequence.

For the per-node archive, consider this source-consistent interleaving:

```text
page 1 reads newest records at offsets 0..49
a task completes and is prepended to the reverse traversal
page 2 reads current offsets 50..99
```

The old offset-49 record can move to offset 50 and be duplicated, while the new
record at offset 0 was not in page 1 and is now behind page 2's numeric start.
Both API calls can succeed. Rotation can change the backing generations between
the calls as well.

| Finding | Classification |
| --- | --- |
| Numeric offset is evaluated against each request's then-current mutable files/list. | `FACT-SOURCE` |
| Concurrent prepend can cause a duplicate and omit the new record from that two-page traversal. | `INFERENCE` |
| Rotation can replace `index.1` between page requests without an API-visible generation change. | `INFERENCE` |
| A later fresh traversal may observe the new record, but no source property proves a stable end boundary for the interval just consumed. | `INFERENCE` |
| Successful offset pagination proves complete interval traversal. | `UNKNOWN` |

This partially resolves Research #1 unknown #4: native `start`/`limit`
pagination is not a snapshot/cursor contract, and the normal two-page traversal
above can omit/duplicate records. Whether a possible stateful
repeated-traversal/overlap protocol over these surfaces can establish
completeness remains `UNKNOWN`.

## 12. Archive rotation, generation, and loss

| Candidate signal | Source result | Classification |
| --- | --- | --- |
| Archive epoch/generation counter | Not present in task-list response or route schema. | `FACT-SOURCE` |
| Monotonic durable task counter | UPID contains timestamps/process identity, not a sequence with predecessor/completeness semantics. | `FACT-SOURCE` |
| File identity/inode contract | Not returned by API. | `FACT-SOURCE` |
| Explicit rotation/loss marker | Not returned by API. | `FACT-SOURCE` |
| Ledger/hash chaining | Not present in audited task archive format. | `FACT-SOURCE` |
| Known-sentinel disappearance | Demonstrates that the witness lost its known overlap. | `INFERENCE` |
| Older sentinel remains while a newer unknown record is selectively lost under normal whole-file rotation | Normal append plus whole-generation rename does not produce that selective pattern. | `INFERENCE`; bounded to normal audited writer behavior |
| Same pattern under crash, selective corruption, authorization filtering, non-generation, node absence, or unsupported source revision | Not excluded. | `UNKNOWN` |

Normal rotation replaces a whole retained generation. That bounded property is
stronger than arbitrary selective deletion, but it is not an API completeness
anchor. A consumer cannot bind pages to a particular generation or determine
that the complete intended interval was traversed.

## 13. Overlap, sentinel, and completeness anchor

The source search found no task API property establishing:

> No unknown relevant task can have been omitted between the previously
> committed coverage boundary and this completed traversal.

| Candidate | Result | Classification |
| --- | --- | --- |
| Monotonic cursor / durable high-water mark | None exposed. | `FACT-SOURCE` |
| Immutable enumeration snapshot | None exposed. | `FACT-SOURCE` |
| Archive generation/epoch | None exposed. | `FACT-SOURCE` |
| Predecessor linkage / durable sequence / chained ledger | None in task records. | `FACT-SOURCE` |
| Repeated known sentinel | Proves only that the known sentinel was observed. | `INFERENCE` |
| Sentinel plus source contract proving no unknown omissions | No such complete envelope was established. | `UNKNOWN` |

The Research #1 simulator correctly fails closed when uncertainty is represented.
Research #2A found no concrete PVE boundary that guarantees all uncertainty is
representable before `COVERAGE_COMPLETE`. The simulator and its tests were not
changed.

## 14. Authorization, visibility, and ACL ABA

### Route behavior

| Surface | Full-list privilege | Non-auditor result | Classification |
| --- | --- | --- | --- |
| `/cluster/tasks` | `Sys.Audit` at `/` | Base user sees own plus token-owned tasks; token caller's ownership fallback matches none; response succeeds without an omission marker | `FACT-SOURCE` |
| Node `active/archive/all` | `Sys.Audit` at `/nodes/{node}` | Base user sees own plus token tasks; token sees that exact token's tasks; response succeeds without an omission marker | `FACT-SOURCE` |
| Exact-UPID status | base owner, exact token owner, or node `Sys.Audit` | Permission error for a known non-owned UPID | `FACT-SOURCE` |
| Exact-UPID log | base owner, exact token owner, or node `Sys.Audit` | Permission error for a known non-owned UPID | `FACT-SOURCE` |

Exact target `pve-manager` implements those route checks. Audited
`pve-access-control` 9.1.1
[`PVE/RPCEnvironment.pm`](https://github.com/proxmox/pve-access-control/blob/5ccd07d9302562b73374d331b63d25b04b86766c/src/PVE/RPCEnvironment.pm)
establishes these underlying rules:

| Finding | Classification/status |
| --- | --- |
| `root@pam` permission checks return all Administrator privileges and API permission checks short-circuit successfully. | `FACT-SOURCE`; `CURRENT-RELEASE MAPPING UNKNOWN` |
| A non-privilege-separated API token receives its owning user's privileges. | `FACT-SOURCE`; `CURRENT-RELEASE MAPPING UNKNOWN` |
| A privilege-separated token's privileges are intersected with those of its owning user. | `FACT-SOURCE`; `CURRENT-RELEASE MAPPING UNKNOWN` |
| ACL changes can make an otherwise successful task-list response hide entries with no omission marker. | `INFERENCE` |

ADR0002's ACL ABA limitation remains binding:

```text
allowed -> denied/hidden -> allowed
```

can leave identical before/after permission snapshots while hiding a task in
the interval. Therefore ordinary mutable ACL point reads are insufficient for
interval proof. Rechecking `Sys.Audit` before and after a traversal does not
close the interval.

The audited unconditional `root@pam` branch is evidence that a structurally
privileged reader class exists in that source revision. It prevents this audit
from generalizing ordinary ACL ABA into impossibility for every imaginable
reader. It does not by itself prove credential continuity, node availability,
task generation, archive retention, or enumeration completeness. Exact target
mapping for `pve-access-control` is also missing.

```text
INTERVAL-WIDE VISIBILITY REMAINS UNKNOWN
```

for an ordinary mutable ACL/token reader. A future structurally privileged
reader proposal would require an explicit security design and accepted
architecture; Research #2A does not create one.

## 15. Node ownership, multi-node behavior, and migration

| Property | Finding | Classification |
| --- | --- | --- |
| Worker owner | `fork_worker` encodes the executing REST environment's node in the UPID. | `FACT-SOURCE`; pve-common mapping unknown |
| Exact routing | Exact-UPID routes decode that owner node and require the route node to match; API proxying directs the call to that node. | `FACT-SOURCE` |
| Archive ownership | `active`, `index`, `index.1`, and task log files are node-local. | `FACT-SOURCE` |
| Cluster aggregation | `/cluster/tasks` concatenates available bounded status contributions for current node membership. | `FACT-SOURCE` |
| Missing member | Per-node retrieval failure is logged/skipped; the aggregate can still succeed. | `FACT-SOURCE` |
| Node incarnation | Task APIs expose node name but no durable node incarnation, membership epoch, or name-reuse generation. | `FACT-SOURCE` |
| Join/removal/rejoin/name reuse | Complete task-history behavior across these transitions is not specified by an exposed task generation or durable node identity. | `UNKNOWN` |

For ordinary QEMU and LXC migration, the source-node API creates the primary
`qmigrate` or `vzmigrate` worker. Remote migration additionally creates a
target-side `qmtunnel` or `vzmtunnel` worker. Local-cluster QEMU migration uses
a target `qm mtunnel` CLI helper without a target UPID; source `qmigrate`
remains the visible primary task.

The HA API wrapper creates/returns `hamigrate` for the request that queues HA
work. Audited `pve-ha-manager` 5.2.5 later calls the QEMU/LXC migration API from
the source LRM and waits on the returned task, but the target's installed
HA-manager version is unknown and no task-API predecessor link joins the wrapper,
source migration, and target helper records.

| Migration question | Disposition | Classification |
| --- | --- | --- |
| Normal local source task type/owner | Established at audited upstream `qemu-server` 9.2.6, with target mapping unknown; established only in unmapped LXC range | `FACT-SOURCE` |
| Remote source and target task types | Established at audited upstream `qemu-server` 9.2.6, with target mapping unknown; established only in unmapped LXC range | `FACT-SOURCE` |
| HA wrapper-to-LRM task linkage as a complete observable chain | No durable UPID linkage/completeness contract established | `UNKNOWN` |
| Failure ordering and partial source/target visibility for every supported variant | Not completely established | `UNKNOWN` |
| Membership disagreement, node disappearance, and rejoin coverage | No complete contract established | `UNKNOWN` |

Migration remains class `T`: it preserves `resource_id`; node is a mutable
relation. Nothing in this audit turns migration into replacement or a coverage
gap into a successor.

## 16. Service restart and node reboot source findings

Exact `pve-manager` 9.2.11 includes
[`PVE/Service/pvedaemon.pm`](https://github.com/proxmox/pve-manager/blob/f6997e698c7933ea8e62319e2bf1bf7262daa56a/PVE/Service/pvedaemon.pm),
[`pvedaemon.service`](https://github.com/proxmox/pve-manager/blob/f6997e698c7933ea8e62319e2bf1bf7262daa56a/services/pvedaemon.service),
[`pvestatd.service`](https://github.com/proxmox/pve-manager/blob/f6997e698c7933ea8e62319e2bf1bf7262daa56a/services/pvestatd.service),
and
[`PVE/Service/pvestatd.pm`](https://github.com/proxmox/pve-manager/blob/f6997e698c7933ea8e62319e2bf1bf7262daa56a/PVE/Service/pvestatd.pm).

| Event/property | Source finding | Classification |
| --- | --- | --- |
| `pvedaemon` reload configuration | `PVE/Service/pvedaemon.pm` sets `leave_children_open_on_reload => 1`. | `FACT-SOURCE` |
| `pvedaemon` worker lifetime | Exact task-worker lifetime for reload, stop/restart, and every failure interleaving is not established by that daemon option. | `UNKNOWN` |
| `pvestatd` restart | On its update loop, it calls `active_workers()` specifically to recover a correct list after unexpected crash, then republishes cluster task status. | `FACT-SOURCE` |
| Dead active process | `active_workers` detects PID/process-start mismatch, reads final status, and attempts archive append; a killed task can become an error/unknown final status. | `FACT-SOURCE`; pve-common mapping unknown |
| Archive/log persistence | Files are under `/var/log/pve/tasks`; service restart source does not deliberately remove them. | `INFERENCE` |
| Cluster-cache repopulation | Restarted `pvestatd` republishes local task status on a later loop; the bounded cluster cache has no completeness epoch. | `FACT-SOURCE` |
| Node reboot | Processes do not survive a reboot; whether every active task is durably present and subsequently archived depends on pre-crash writes/filesystem state not proven here. | `UNKNOWN` |
| Exact-UPID after reboot | Readability follows survival of its local log file and cleanup boundary, but crash durability and rejoin routing are not fully proven. | `UNKNOWN` |
| Node rejoin | No task archive generation or durable node-incarnation handoff is exposed. | `UNKNOWN` |

Source establishes useful normal recovery intent, not complete restart/reboot
semantics at the required security strength. Experiments #15 and #16 therefore
remain materially useful once precisely scoped and separately approved.

## 17. Disposition of Research #1's eleven unknown groups

Annotations overlap; each numbered group remains present.

| # | Original unknown group | Research #2A disposition | Basis |
| --- | --- | --- | --- |
| 1 | Exact installed/current-release source mapping | **PARTIALLY RESOLVED** + **REQUIRES OPERATOR VERSION READ** | Exact installed-to-source mappings for `pve-manager` and `pve-cluster`; exact upstream mapping and behavior audit for `qemu-server` 9.2.6, whose installed-target identity remains unknown; other load-bearing installed versions also remain unknown. |
| 2 | Operation-to-task generation/type/owner/routes | **PARTIALLY RESOLVED** + **REQUIRES CONTROLLED EXPERIMENT** | Exact tracing at audited upstream `qemu-server` 9.2.6 found both worker routes and supported no-worker routes, disproving universal task-generation coverage for that release. Installed-target applicability, LXC mapping, runtime variants, and a continuity-relevant no-UPID replacement witness remain unestablished. |
| 3 | Active-to-archive publication/handoff completeness | **PARTIALLY RESOLVED** + **REQUIRES CONTROLLED EXPERIMENT** | Normal lock/write ordering and the start-publication race are sourced; durable external gaplessness is unproven. |
| 4 | Pagination completeness under concurrent task changes/rotation | **PARTIALLY RESOLVED** + **STILL UNKNOWN** + **REQUIRES CONTROLLED EXPERIMENT** | Source proves native mutable offsets are not a snapshot/cursor and a normal two-page traversal can omit/duplicate records. It does not prove every possible stateful repeated-traversal/overlap protocol incapable of establishing completeness. |
| 5 | Machine-observable archive generation/loss and exact `index/index.1` behavior | **PARTIALLY RESOLVED** + **REQUIRES CONTROLLED EXPERIMENT** | Normal append/rotation/retention are sourced; no API epoch/loss marker exists; crash behavior remains unknown. |
| 6 | Exact-UPID retention after list disappearance/rotation/restart/rejoin | **PARTIALLY RESOLVED** + **REQUIRES CONTROLLED EXPERIMENT** | Separate log files and daily cleanup boundary are sourced; restart/rejoin/crash outcomes are incomplete. |
| 7 | Concrete overlap anchor proving absence of unknown omitted tasks | **STILL UNKNOWN** | No cursor, immutable snapshot, generation, predecessor, sequence, or equivalent complete envelope found. |
| 8 | Interval-wide reader visibility including ACL/token ABA | **PARTIALLY RESOLVED** + **STILL UNKNOWN** + **REQUIRES CONTROLLED EXPERIMENT** | Exact route filters and audited root/token rules are sourced; mutable-reader ABA remains; a future stronger reader boundary is not designed. |
| 9 | Multi-node ownership/routing/membership/rejoin/migration | **PARTIALLY RESOLVED** + **REQUIRES CONTROLLED EXPERIMENT** | Primary and helper worker ownership is partly sourced; membership/rejoin/failure completeness is not. |
| 10 | PVE service/node restart effects on task state/archive durability | **PARTIALLY RESOLVED** + **REQUIRES CONTROLLED EXPERIMENT** | Recovery intent and file behavior are partly sourced; crash/reboot durable behavior is not. |
| 11 | Proof that a hidden/omitted lifecycle event can never still yield `COVERAGE_COMPLETE` | **STILL UNKNOWN** + **REQUIRES CONTROLLED EXPERIMENT** | The simulator fails closed only when uncertainty reaches it; no PVE completeness envelope proves every hidden/omitted event becomes observable uncertainty. |

Disposition annotation counts are:

| Annotation | Count | Groups |
| --- | ---: | --- |
| `RESOLVED — SOURCE/DOC PROOF` | 0 | None |
| `PARTIALLY RESOLVED` | 9 | 1, 2, 3, 4, 5, 6, 8, 9, 10 |
| `STILL UNKNOWN` | 4 | 4, 7, 8, 11 |
| `IMPOSSIBLE — WITH PROOF` | 0 | None |
| `REQUIRES CONTROLLED EXPERIMENT` | 9 | 2, 3, 4, 5, 6, 8, 9, 10, 11 |
| `REQUIRES OPERATOR VERSION READ` | 1 | 1 |

The controlled-experiment annotation on group 11 means an adversarial experiment
can falsify a proposed mapping or expose a false `COVERAGE_COMPLETE`. Repeated
successful experiments cannot prove the universal “never hidden” property
without a source-backed completeness contract.

## 18. Implications for controlled experiments #1--#17

No experiment was run. Each status below is the primary #2A scheduling status;
source-backed expectations remain subject to adversarial validation and cannot
be promoted into an undocumented security contract.

| # | Canonical experiment | #2A status | Refinement/source implication |
| --- | --- | --- | --- |
| 1 | QEMU create A, destroy A, create B at same VMID | **VALIDATION / ADVERSARIAL CONFIRMATION** | Normal `qmcreate/qmdestroy` routes are source-proven at audited upstream `qemu-server` 9.2.6; confirm the installed mapping before applying them directly, then test the boundary against supported no-task create/import routes. |
| 2 | LXC equivalent | **BLOCKED BY MISSING VERSION/ENVIRONMENT DATA** | Pin installed `pve-container`; use only a disposable approved LXC fixture. |
| 3 | QEMU clone variants | **VALIDATION / ADVERSARIAL CONFIRMATION** | The `qmclone` route and source-VMID UPID attribution are source-proven at audited upstream `qemu-server` 9.2.6; direct target applicability remains version-mapping dependent. |
| 4 | LXC clone | **BLOCKED BY MISSING VERSION/ENVIRONMENT DATA** | Pin installed container package first. |
| 5 | QEMU snapshot create/delete/rollback | **VALIDATION / ADVERSARIAL CONFIRMATION** | The `qmsnapshot/qmdelsnapshot/qmrollback` types are source-proven at audited upstream `qemu-server` 9.2.6; direct target applicability remains version-mapping dependent; preserve class `P`. |
| 6 | LXC snapshot create/delete/rollback | **BLOCKED BY MISSING VERSION/ENVIRONMENT DATA** | Pin installed package; expected types are `vzsnapshot/vzdelsnapshot/vzrollback`. |
| 7 | QEMU restore same/new VMID | **VALIDATION / ADVERSARIAL CONFIRMATION** | The `qmrestore` route is source-proven at audited upstream `qemu-server` 9.2.6; direct target applicability remains version-mapping dependent. New locator is `N`; at the same locator, accepted positive replacement evidence invokes the class-R backend identity transition, while its absence retains identity/binding with class-P-style fail-closed continuity. This evidence consequence does not determine whether physical replacement occurred. |
| 8 | LXC restore | **BLOCKED BY MISSING VERSION/ENVIRONMENT DATA** | Pin package and storage variants first. |
| 9 | QEMU migration including failure | **NEEDS REFINEMENT BEFORE EXECUTION** | Confirm the installed `qemu-server` mapping, then separate local, remote, HA, source worker, target CLI/API tunnel, online/offline, and partial-failure cases. |
| 10 | LXC migration including failure | **NEEDS REFINEMENT BEFORE EXECUTION** | Pin container/HA packages and separate local/remote/HA/restart variants. |
| 11 | QEMU disk/import/move/attach/storage paths | **NEEDS REFINEMENT BEFORE EXECUTION** | Confirm the installed `qemu-server` mapping; then include audited-9.2.6 `qmmove` plus synchronous `PUT config`, `qm disk import`, `qm importovf`, attach, unlink, and boot/backing changes; do not assume a task. |
| 12 | LXC rootfs/storage paths | **NEEDS REFINEMENT BEFORE EXECUTION** | Pin package; distinguish `move_volume` from synchronous config attach/replace. |
| 13 | High-volume tasks crossing pages/archive rotation | **NEEDS REFINEMENT BEFORE EXECUTION** | First pin the installed `pve-common` revision. Then define the candidate repeated-traversal/overlap protocol and adversarial interleavings; native offset traversal is already negatively bounded, but the broader stateful completeness question remains unknown. |
| 14 | Exact-UPID retention after list disappearance/rotation | **BLOCKED BY MISSING VERSION/ENVIRONMENT DATA** | Exact retention/rotation behavior depends on the still-unmapped installed `pve-common`; pin it before validating separate-log persistence and cleanup on a disposable fixture. |
| 15 | Task-related PVE service restart around active task | **NEEDS REFINEMENT BEFORE EXECUTION** | Name exact service and distinguish reload, restart, stop/start, worker phase, archive write, and publisher recovery. |
| 16 | Node reboot around active task | **NEEDS REFINEMENT BEFORE EXECUTION** | Define crash point, filesystem durability observation, membership state, and post-reboot/rejoin routing. |
| 17 | `Sys.Audit` remove/restore ACL ABA around known task | **NEEDS REFINEMENT BEFORE EXECUTION** | Keep ordinary token ABA test; separately decide whether any future structurally privileged reader candidate is in scope. |

Status counts:

| Status | Count | Experiments |
| --- | ---: | --- |
| `STILL REQUIRED` | 0 | None as the primary status; unresolved experiments are more precisely classified below. |
| `VALIDATION / ADVERSARIAL CONFIRMATION` | 4 | 1, 3, 5, 7 |
| `NEEDS REFINEMENT BEFORE EXECUTION` | 8 | 9, 10, 11, 12, 13, 15, 16, 17 |
| `BLOCKED BY MISSING VERSION/ENVIRONMENT DATA` | 5 | 2, 4, 6, 8, 14 |
| `PROPERTY ALREADY SOURCE-PROVEN` | 0 | None; even sourced mechanics retain adversarial validation value. |

Every future execution still requires explicit operator approval, a controlled
window, and a separately identified disposable non-production fixture. CT112 is
not that fixture.

## 19. Family B Research #2A exit classification

### CASE 2 — MATERIAL UNKNOWNS REMAIN

`CASE 2` is a local Research #2A exit classification, not an accepted
architecture taxonomy.

Research #2A establishes useful negative route-coverage evidence: the audited
upstream `qemu-server` 9.2.6 source includes supported synchronous `PUT .../config`,
`qm disk import`, and `qm importovf` paths that do not call `fork_worker` and
therefore create no UPID. Universal task generation across all supported QEMU
backing/configuration routes is disproven for that audited release. These exact
source findings apply directly to the installed target only if an operator read
confirms that its `qemu-server` maps to 9.2.6.

That fact is not a continuity-relevant NO-GO proof. ADR0006 §4c distinguishes
ordinary configuration mutation from actual physical/logical workload
replacement. This pass did not establish from exact source a supported T1/T2
no-UPID operation sequence whose unnoticed occurrence actually produces a
class-R or class-P transition within ADR0006 §4b's detection scope. Missing
that bridge is missing evidence, not impossibility.

Whether an actual replacement physically occurred is independent of whether
the backend has accepted positive replacement evidence. If actual replacement
occurred without such evidence, it does not become ordinary config mutation:
the backend retains the existing read-only `resource_id`/binding and continuity
and policy fail closed under the accepted ambiguity path. Accepted positive
replacement evidence instead authorizes the class-R atomic direct-replacement
identity transition. Evidence controls recognition and backend consequence; it
does not cause or define the physical replacement.

The missing completeness anchor, possible stateful repeated-traversal/overlap
protocol, interval visibility, multi-node, restart, and exact-version questions
remain material. Research #2A therefore establishes no sufficient mechanism
and no proven impossibility for the remaining Family-B candidate space.

Therefore the architecture classification remains:

```text
Family B:  UNRESOLVED / NOT FULLY AUDITED
Blocker B: OPEN
WAVE B1:   DEFERRED / NOT AUTHORIZED
Phase 1C:  BLOCKED
R0:        GO / STRICTLY READ-ONLY
```

Family B remains `UNRESOLVED / NOT FULLY AUDITED`. A separately approved
Research #2B campaign is justified for the exact experimentally resolvable
questions in section 18. Experiments can validate or falsify source-derived
runtime expectations. They cannot establish a security boundary solely from
undocumented observed behavior or substitute for the still-missing source and
architecture contract.

## 20. Exact primary-source ledger

| Component | Version | Commit | File | Symbol/function | Property established | Evidence class |
| --- | --- | --- | --- | --- | --- | --- |
| qemu-server | audited upstream 9.2.6; target unknown | [`e6352be...`](https://github.com/proxmox/qemu-server/commit/e6352be67f70042a7433a3a3c712b36d02f9f7cb) | [`src/PVE/API2/Qemu.pm`](https://github.com/proxmox/qemu-server/blob/e6352be67f70042a7433a3a3c712b36d02f9f7cb/src/PVE/API2/Qemu.pm) | `create_vm`, `destroy_vm`, `clone_vm` | Create/restore, destroy, clone task types/routes/owners | `FACT-SOURCE`; target mapping unknown |
| qemu-server | same | same | same | `migrate_vm`, `remote_migrate_vm`, `mtunnel` | Source migration and remote target tunnel workers | `FACT-SOURCE`; target mapping unknown |
| qemu-server | same | same | same | `move_vm_disk`, config update methods | `qmmove`, async `qmconfig`, synchronous `PUT` bypass | `FACT-SOURCE`; target mapping unknown |
| qemu-server | same | same | [`src/PVE/API2/Qemu.pm`](https://github.com/proxmox/qemu-server/blob/e6352be67f70042a7433a3a3c712b36d02f9f7cb/src/PVE/API2/Qemu.pm) | `snapshot`, `delsnapshot`, `rollback` | `qmsnapshot`, `qmdelsnapshot`, `qmrollback` | `FACT-SOURCE`; target mapping unknown |
| qemu-server | same | same | [`src/PVE/CLI/qm.pm`](https://github.com/proxmox/qemu-server/blob/e6352be67f70042a7433a3a3c712b36d02f9f7cb/src/PVE/CLI/qm.pm) | `importdisk`, `importovf`, `unlink`, `mtunnel` | Supported synchronous no-UPID routes and local helper | `FACT-SOURCE`; target mapping unknown |
| pve-manager | 9.2.11 | [`f6997e...`](https://github.com/proxmox/pve-manager/commit/f6997e698c7933ea8e62319e2bf1bf7262daa56a) | [`PVE/API2/Tasks.pm`](https://github.com/proxmox/pve-manager/blob/f6997e698c7933ea8e62319e2bf1bf7262daa56a/PVE/API2/Tasks.pm) | task index/status/log route methods | Active/archive/all enumeration, offsets, filters, authorization, exact-UPID behavior | `FACT-SOURCE` |
| pve-manager | 9.2.11 | same | [`PVE/API2/Cluster.pm`](https://github.com/proxmox/pve-manager/blob/f6997e698c7933ea8e62319e2bf1bf7262daa56a/PVE/API2/Cluster.pm) | `get_tasklist` route | Cluster task route and authorization filter | `FACT-SOURCE` |
| pve-manager | 9.2.11 | same | [`PVE/Service/pvestatd.pm`](https://github.com/proxmox/pve-manager/blob/f6997e698c7933ea8e62319e2bf1bf7262daa56a/PVE/Service/pvestatd.pm) | `update_status` | Active scan and periodic cluster publication | `FACT-SOURCE` |
| pve-manager | 9.2.11 | same | [`bin/pveupdate`](https://github.com/proxmox/pve-manager/blob/f6997e698c7933ea8e62319e2bf1bf7262daa56a/bin/pveupdate) | `cleanup_tasks` | Exact-UPID log cleanup boundary | `FACT-SOURCE` |
| pve-manager | 9.2.11 | same | [`PVE/Service/pvedaemon.pm`](https://github.com/proxmox/pve-manager/blob/f6997e698c7933ea8e62319e2bf1bf7262daa56a/PVE/Service/pvedaemon.pm) | `leave_children_open_on_reload` daemon option | Children remain open on daemon reload | `FACT-SOURCE` |
| pve-manager | 9.2.11 | same | [`services/pvedaemon.service`](https://github.com/proxmox/pve-manager/blob/f6997e698c7933ea8e62319e2bf1bf7262daa56a/services/pvedaemon.service), [`pvestatd.service`](https://github.com/proxmox/pve-manager/blob/f6997e698c7933ea8e62319e2bf1bf7262daa56a/services/pvestatd.service) | unit definitions | Service start/stop/reload entry points | `FACT-SOURCE` |
| pve-cluster | 9.1.6 | [`7091d92...`](https://github.com/proxmox/pve-cluster/commit/7091d92e594952dba65c1e57568b3d7cc244e960) | [`src/PVE/Cluster.pm`](https://github.com/proxmox/pve-cluster/blob/7091d92e594952dba65c1e57568b3d7cc244e960/src/PVE/Cluster.pm) | `broadcast_tasklist`, `get_tasklist` | 32 KiB per-node publication and membership-based cache aggregation | `FACT-SOURCE` |
| pve-common | audited 9.1.21/9.2.1 endpoints; target unknown | [`f665029...`](https://github.com/proxmox/pve-common/commit/f665029eac78022e81810ab2e44eace57ade13fb) | [`src/PVE/RESTEnvironment.pm`](https://github.com/proxmox/pve-common/blob/f665029eac78022e81810ab2e44eace57ade13fb/src/PVE/RESTEnvironment.pm) | `fork_worker`, `active_workers` | UPID start ordering, archive append, rotation, active retention | `FACT-SOURCE`; mapping unknown |
| pve-common | same | same | [`src/PVE/UPID.pm`](https://github.com/proxmox/pve-common/blob/f665029eac78022e81810ab2e44eace57ade13fb/src/PVE/UPID.pm) | `encode`, `decode`, `open_log`, `read_status` | UPID fields and exact log-file backing | `FACT-SOURCE`; mapping unknown |
| pve-container | audited 6.1.6--6.1.13; target unknown | [`c813255...`](https://github.com/proxmox/pve-container/commit/c8132559faedb76a56498d411bf3e024c1ff07e7) | [`src/PVE/API2/LXC.pm`](https://github.com/proxmox/pve-container/blob/c8132559faedb76a56498d411bf3e024c1ff07e7/src/PVE/API2/LXC.pm) | create/destroy/clone/migrate/move/mtunnel methods | LXC task types, source/target ownership, migration variants | `FACT-SOURCE`; mapping unknown |
| pve-container | same | same | [`src/PVE/API2/LXC/Snapshot.pm`](https://github.com/proxmox/pve-container/blob/c8132559faedb76a56498d411bf3e024c1ff07e7/src/PVE/API2/LXC/Snapshot.pm) | snapshot methods | `vzsnapshot`, `vzdelsnapshot`, `vzrollback` | `FACT-SOURCE`; mapping unknown |
| pve-container | same | same | [`src/PVE/API2/LXC/Config.pm`](https://github.com/proxmox/pve-container/blob/c8132559faedb76a56498d411bf3e024c1ff07e7/src/PVE/API2/LXC/Config.pm) | `update_vm` | Synchronous rootfs/mount-point config route | `FACT-SOURCE`; mapping unknown |
| pve-access-control | audited 9.1.1; target unknown | [`5ccd07d...`](https://github.com/proxmox/pve-access-control/commit/5ccd07d9302562b73374d331b63d25b04b86766c) | [`src/PVE/RPCEnvironment.pm`](https://github.com/proxmox/pve-access-control/blob/5ccd07d9302562b73374d331b63d25b04b86766c/src/PVE/RPCEnvironment.pm) | `permissions`, `check_api2_permissions` | Root privilege branch and token/user intersection | `FACT-SOURCE`; mapping unknown |
| pve-ha-manager | audited 5.2.5; target unknown | [`c73364c...`](https://github.com/proxmox/pve-ha-manager/commit/c73364c19d5317e6df5bb1c1b727d080a5e897ef) | [`src/PVE/HA/Resources/PVEVM.pm`](https://github.com/proxmox/pve-ha-manager/blob/c73364c19d5317e6df5bb1c1b727d080a5e897ef/src/PVE/HA/Resources/PVEVM.pm), [`PVECT.pm`](https://github.com/proxmox/pve-ha-manager/blob/c73364c19d5317e6df5bb1c1b727d080a5e897ef/src/PVE/HA/Resources/PVECT.pm) | `migrate` | HA LRM invokes guest migration API and waits on its UPID | `FACT-SOURCE`; mapping unknown |

No moving branch is used as load-bearing evidence. Source entries with an
unknown installed mapping remain explicitly bounded even when their behavior is
clear at the audited revision.

## 21. Operator-read dependencies still required

The minimum real-host package read needed before Research #2B planning is:

```bash
pveversion -v
```

The operator, not this research agent, must run it on the target PVE node and
supply the unredacted package names/versions (no credentials are involved). It
is needed to pin at least:

- `qemu-server`;
- `pve-common` / `libpve-common-perl`;
- `pve-container`;
- `pve-storage` / `libpve-storage-perl`;
- `pve-access-control` / `libpve-access-control`;
- `pve-ha-manager`; and
- the packaged/running PVE kernel relationship if reboot behavior becomes
  load-bearing.

If `pveversion -v` does not show which installed kernel is running and that fact
becomes necessary, the additional minimum read is:

```bash
uname -r
```

These are operator-read dependencies only. Research #2A did not execute them,
connect to PVE, inspect private infrastructure, or perform a lifecycle action.

The remaining non-version dependencies are a separately approved disposable
QEMU/LXC test fixture, controlled multi-node environment for migration/rejoin
questions, an explicit test window, and operator approval for each applicable
experiment. None authorizes implementation or trusted enrollment.
