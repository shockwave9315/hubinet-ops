> **ARCHIVED / SUPERSEDED RESEARCH — do not read by default.**
> This document is part of the abandoned Family B / B-S1 task-history witness
> path. B-S1 is **NO-GO** as the mutation-authority path, Experiment #13 was
> never executed, and Blocker B remains **OPEN**. Nothing here is architecture
> authority or a current roadmap. Read
> `docs/archive/postmortems/blocker-b-family-b-13.md` first, and
> `docs/archive/blocker-b-family-b/README.md` for why this is archived.
> The original document text follows unchanged.

# NON-NORMATIVE RESEARCH / EVIDENCE

# Family B Research #2A.1 exact-target version reconciliation

## 1. Scope, authority, and fixed outcome boundary

This addendum reconciles Family B Research #2A against the exact package
versions reported by the operator for the real target PVE host. It does not
rewrite Research #2A's historical audit conclusions.

This document:

- does not amend any ACCEPTED ADR;
- does not authorize implementation, schema, runtime, `hostd`, Home Assistant,
  enrollment, or mutation work;
- does not grant `security_continuity=trusted`;
- does not close Family B or Blocker B;
- does not authorize WAVE B1 or Phase 1C;
- does not authorize or execute Research #2B; and
- does not change R0 from strictly read-only operation.

Accepted ADR0005 and ADR0006 remain controlling. Source applicability is not a
runtime security property, source silence is not completeness proof, and a
coverage gap is never positive replacement evidence.

The required final state remains:

```text
Family B:    UNRESOLVED / NOT FULLY AUDITED
Blocker B:   OPEN
WAVE B1:     DEFERRED / NOT AUTHORIZED
Phase 1C:    BLOCKED
R0:          GO / STRICTLY READ-ONLY
Research #2B: NOT EXECUTED / REQUIRES SEPARATE OPERATOR APPROVAL
```

## 2. Repository and operator-evidence provenance

| Item | Result |
| --- | --- |
| Repository | `shockwave9315/hubinet-ops` |
| Exact base `main` | `506fdcde0d2a009f59f2f164b67e470796214015` (PR #49 merge) |
| PR #49 reviewed head | `42bc09dc6ba43e6bee364dc4fe08de88fdedc105` |
| Reconciliation date | 2026-08-25 UTC |
| Target-version provenance | `FACT-OPERATOR`: operator-supplied, manually collected read-only `pveversion -v` output |
| Operator-read timestamp | Not separately supplied; this document does not invent one |
| Upstream mapping method | Fresh read-only official Proxmox mirrors; exact immutable changelog-bump commits re-derived independently |
| Live PVE/HA access by this research | None |
| CT112 evidence used | None |
| Research #2B experiments | None run or authorized |

The operator evidence establishes only the package/version and running-version
values listed below. No version is inferred for an unlisted package.

## 3. Exact target package evidence

| Operator-observed item | Exact observed version/value | Evidence |
| --- | --- | --- |
| `proxmox-ve` | `9.2.0` | `FACT-OPERATOR` |
| running kernel | `7.0.14-12-pve` | `FACT-OPERATOR` |
| `pve-manager` | `9.2.11`; running `9.2.11/f6997e698c7933ea` | `FACT-OPERATOR` |
| `qemu-server` | `9.2.6` | `FACT-OPERATOR` |
| `pve-cluster` | `9.1.6` | `FACT-OPERATOR` |
| `libpve-cluster-api-perl` | `9.1.6` | `FACT-OPERATOR` |
| `libpve-cluster-perl` | `9.1.6` | `FACT-OPERATOR` |
| `libpve-common-perl` | `9.2.1` | `FACT-OPERATOR` |
| `pve-container` | `6.1.13` | `FACT-OPERATOR` |
| `libpve-storage-perl` | `9.1.8` | `FACT-OPERATOR` |
| `libpve-access-control` | `9.1.1` | `FACT-OPERATOR` |
| `pve-ha-manager` | `5.2.5` | `FACT-OPERATOR` |
| `pve-qemu-kvm` | `11.0.3-2` | `FACT-OPERATOR` |
| `lxc-pve` | `7.0.0-2` | `FACT-OPERATOR` |
| `proxmox-kernel-7.0.14-12-pve-signed` | `7.0.14-12` | `FACT-OPERATOR` |

## 4. Independently re-verified upstream revision mapping

No tag is claimed where upstream publishes the package boundary as a changelog
bump commit. Each mapping below was independently re-derived by locating the
commit that introduces the exact top-level `debian/changelog` version and then
checking the binary package names in `debian/control`.

| Target package(s) | Target version | Official source project | Exact upstream commit | Reconciliation result |
| --- | --- | --- | --- | --- |
| `proxmox-ve` | `9.2.0` | `proxmox-ve` | [`339b52bbec418a0978c187b1e95eea5b098b9322`](https://git.proxmox.com/?p=proxmox-ve.git;a=commit;h=339b52bbec418a0978c187b1e95eea5b098b9322) | Exact changelog-bump mapping confirmed |
| `qemu-server` | `9.2.6` | `qemu-server` | [`e6352be67f70042a7433a3a3c712b36d02f9f7cb`](https://github.com/proxmox/qemu-server/commit/e6352be67f70042a7433a3a3c712b36d02f9f7cb) | Exact target equals Research #2A audited revision |
| `pve-manager` | `9.2.11` | `pve-manager` | [`f6997e698c7933ea8e62319e2bf1bf7262daa56a`](https://github.com/proxmox/pve-manager/commit/f6997e698c7933ea8e62319e2bf1bf7262daa56a) | Exact target equals Research #2A; operator running-version hash matches the commit prefix |
| `pve-cluster`, `libpve-cluster-api-perl`, `libpve-cluster-perl` | `9.1.6` | `pve-cluster` | [`7091d92e594952dba65c1e57568b3d7cc244e960`](https://github.com/proxmox/pve-cluster/commit/7091d92e594952dba65c1e57568b3d7cc244e960) | All three observed binaries map to the same exact source release audited by #2A |
| `libpve-common-perl` | `9.2.1` | `pve-common` | [`f665029eac78022e81810ab2e44eace57ade13fb`](https://github.com/proxmox/pve-common/commit/f665029eac78022e81810ab2e44eace57ade13fb) | Exact target equals Research #2A audited endpoint |
| `pve-container` | `6.1.13` | `pve-container` | [`c8132559faedb76a56498d411bf3e024c1ff07e7`](https://github.com/proxmox/pve-container/commit/c8132559faedb76a56498d411bf3e024c1ff07e7) | Exact target equals Research #2A audited revision |
| `libpve-storage-perl` | `9.1.8` | `pve-storage` | [`cd5c90ccd9ffd14a9578f58bbf528e78120f8bf2`](https://github.com/proxmox/pve-storage/commit/cd5c90ccd9ffd14a9578f58bbf528e78120f8bf2) | Exact target mapped; differs from #2A's audited 9.1.9 revision and is reconciled in section 6 |
| `libpve-access-control` | `9.1.1` | `pve-access-control` | [`5ccd07d9302562b73374d331b63d25b04b86766c`](https://github.com/proxmox/pve-access-control/commit/5ccd07d9302562b73374d331b63d25b04b86766c) | Exact target equals Research #2A audited revision |
| `pve-ha-manager` | `5.2.5` | `pve-ha-manager` | [`c73364c19d5317e6df5bb1c1b727d080a5e897ef`](https://github.com/proxmox/pve-ha-manager/commit/c73364c19d5317e6df5bb1c1b727d080a5e897ef) | Exact target equals Research #2A audited revision |
| `pve-qemu-kvm` | `11.0.3-2` | `pve-qemu` | [`6d044073095e3f6fb8b44a66f6f8fb710e2a81af`](https://github.com/proxmox/pve-qemu/commit/6d044073095e3f6fb8b44a66f6f8fb710e2a81af) | Exact package mapping confirmed; not used to upgrade a #2A task-history claim |
| `lxc-pve` | `7.0.0-2` | `lxc` | [`680dfc7595269dff54d7d262f3cb7dc4b5297f3d`](https://git.proxmox.com/?p=lxc.git;a=commit;h=680dfc7595269dff54d7d262f3cb7dc4b5297f3d) | Exact package mapping confirmed; not used to upgrade a #2A task-history claim |
| running/signed kernel | `7.0.14-12-pve` / `7.0.14-12` | kernel source | **No commit mapped in this pass** | Running and package identity known; exact kernel-source mapping deliberately deferred until experiment #16/reboot semantics makes it load-bearing |

The kernel row is not an inference or a failed lookup. It is an explicit scope
boundary: no current Research #2A conclusion requires a kernel-source audit.

## 5. Reconciliation against merged Research #2A

This section upgrades only installed-target applicability. Every underlying
`FACT-SOURCE`, `FACT-DOC`, `INFERENCE`, and `UNKNOWN` classification remains as
Research #2A recorded it.

| Research #2A source family | Exact installed-target result | Applicability update |
| --- | --- | --- |
| `qemu-server` operation/task/no-UPID findings | Target is exact audited 9.2.6 commit `e6352be...` | **Exact-target applicable.** The create/destroy/clone/snapshot/restore/migration/move/config worker findings and supported synchronous no-UPID `PUT .../config`, `qm disk import`, and `qm importovf` findings now apply directly to the installed package. The bounded `INFERENCE` about non-universal UPID coverage stays an inference; no continuity-relevant no-UPID replacement witness is promoted. |
| `pve-manager` active/archive/all, exact-UPID routes, cleanup, and service findings | Target is exact audited 9.2.11 commit `f6997e...`; the operator's running hash independently agrees | **Exact-target applicable.** Runtime/crash/durability properties left `UNKNOWN` remain `UNKNOWN`. |
| `pve-common` worker start, active/archive handoff, rotation, and UPID mechanics | Target is exact audited 9.2.1 commit `f665029...` | **Exact-target applicable.** Normal writer ordering and rotation mechanics remain `FACT-SOURCE`; externally observable gaplessness, crash durability, and completeness remain `UNKNOWN`. |
| `pve-container` operation/task/no-UPID findings | Target is exact audited 6.1.13 commit `c813255...` | **Exact-target applicable.** LXC create/destroy/clone/snapshot/restore/migration/move and synchronous config-route findings no longer carry a target-version qualifier. Runtime variants remain subject to validation. |
| `pve-access-control` root/token authorization and ACL-ABA-relevant findings | Target is exact audited 9.1.1 commit `5ccd07d...` | **Exact-target applicable.** The source rules apply directly; interval-wide visibility remains `UNKNOWN`, and this research creates no privileged-reader design. |
| `pve-ha-manager` HA migration wrappers | Target is exact audited 5.2.5 commit `c73364c...` | **Exact-target applicable.** The LRM wrapper-to-QEMU/LXC call behavior applies directly; a complete durable UPID predecessor chain and failure ordering remain `UNKNOWN`. |
| `pve-cluster` cluster-task cache | Target is exact audited 9.1.6 commit `7091d92...` | **Exact-target applicable.** The 32 KiB per-node bound and membership-based aggregation apply directly; they do not become a complete interval ledger. |
| `pve-storage` 9.1.9 audit boundary | Target is 9.1.8 commit `cd5c90c...`, not #2A's 9.1.9 `c403d5f...` | Reconciled narrowly in section 6. No blanket equivalence is asserted. |

The installed-target mapping is now confirmed by operator evidence for every
above package. This does not convert source mechanics into runtime observations
or an accepted completeness contract.

## 6. `pve-storage` 9.1.8 versus Research #2A's 9.1.9

### 6.1 Exact version relationship and complete delta

The expected 9.1.8 candidate mapping is confirmed:

```text
9.1.8 = cd5c90ccd9ffd14a9578f58bbf528e78120f8bf2
9.1.9 = c403d5f6793cc3dd4bc2168d0205b211d6295903
```

The former is an ancestor of the latter. Exactly two commits occur in the
range:

1. `a8b6dc5b04cc39c24d54503319755f50b8f19ba3` — `ceph: accept keyring files whose key uses the aes256k cipher`;
2. `c403d5f6793cc3dd4bc2168d0205b211d6295903` — version bump to 9.1.9.

The complete tree delta is 11 insertions and 2 deletions in exactly two files:

- `debian/changelog`: adds the 9.1.9 release entry;
- `src/PVE/CephConfig.pm`: changes only the private
  `$ceph_check_keyfile` validation regex and its explanatory comment.

No other file changed.

### 6.2 Load-bearing classification

| Comparison | Classification | Proof and bounded consequence |
| --- | --- | --- |
| `debian/changelog` release entry | **DIFFERENT BUT NON-LOAD-BEARING** | Packaging metadata only; it changes no operation, worker, UPID, archive, clone, restore, import/export, move, or path function. |
| `PVE::CephConfig` private `$ceph_check_keyfile` | **DIFFERENT BUT NON-LOAD-BEARING** for Family B's #2A findings | 9.1.8 requires the older two-`=` base64 padding shape. 9.1.9 accepts both older AES and 32-byte AES-256 key shapes. The call site remains `ceph_connect_option`; the change can decide whether an RBD/CephFS storage using the newer key shape is accepted, so it is a real runtime/storage-availability difference, not cosmetic equivalence. It creates no worker/UPID, changes no task owner/type, exposes no task cursor, and changes no lifecycle evidence. |
| `src/PVE/Storage.pm` core clone/snapshot/migrate/path/import/export functions | **IDENTICAL** | The file has the same Git blob `64ea9dadc567a959664bc4a02ed3031b7018fb61` at both revisions. This covers, among others, `path`, `storage_migrate_snapshot`, `storage_migrate`, `vdisk_clone`, `vdisk_alloc`, `volume_snapshot*`, `volume_export*`, and `volume_import*`. |
| `src/PVE/Storage/Plugin.pm` generic plugin contract | **IDENTICAL** | Same Git blob `4f69f9b5db69674335eb3024d61d4a3430bca1ec` at both revisions, including clone/allocation/path/snapshot/import/export methods. |
| `src/PVE/Storage/RBDPlugin.pm` | **IDENTICAL** | Same Git blob `b5374251f0846bce23e864cf1c83f134b91156a2`; its clone/path/allocation/snapshot/import/export implementations did not change. It can still receive the separately described keyfile-validation outcome through unchanged `ceph_connect_option` calls. |
| `src/PVE/Storage/CephFSPlugin.pm` | **IDENTICAL** | Same Git blob `fbc9711372aa3c9c9dd51c8fa00c787f4aeb6b16`; only the called validator in `CephConfig.pm` differs. |
| Every other storage plugin and source file | **IDENTICAL** | The complete `git diff --name-status` contains only `debian/changelog` and `src/PVE/CephConfig.pm`. Therefore no LVM/LVM-thin/ZFS/BTRFS/directory/NFS/CIFS/PBS/iSCSI plugin implementation changed in this range. |

### 6.3 Effect on Research #2A

Research #2A's load-bearing task/no-task route findings are owned by exact
`qemu-server` and `pve-container` API/CLI handlers, not by the changed Ceph
keyfile validator. Its caller-level clone, restore, import, move, synchronous
configuration, and backing-path findings therefore require **no substantive
correction** for installed `pve-storage` 9.1.8.

The target qualification changes as follows:

- installed `pve-storage` is now exactly mapped to 9.1.8;
- the 9.1.8 storage core/plugin behavior relevant to clone, snapshot/restore,
  migration/move, allocation, path resolution, and import/export is identical
  to the later 9.1.9 tree audited by #2A;
- the one actual behavior difference is retained explicitly: 9.1.8 may reject
  an RBD/CephFS keyfile using the newer AES-256 key shape that 9.1.9 accepts;
- no target storage type, key cipher, or backing topology was supplied, so this
  addendum does not infer whether that difference is exercised on the target;
  and
- #2A's deliberately `UNKNOWN` plugin-specific operation coverage remains
  `UNKNOWN`; an identical source tree does not retroactively perform the audit
  #2A left undone.

The reconciliation result is therefore **target mapping qualification only,
not a substantive correction**, with the Ceph availability difference retained
as a non-load-bearing fixture/environment consideration.

## 7. Unknown group #1 updated disposition

Research #1's unknown group #1 was `Exact installed/current-release source
mapping`. The operator read and this independent source pass change its
disposition to:

```text
USER-SPACE / PACKAGE APPLICABILITY: RESOLVED — FACT-OPERATOR + FACT-SOURCE
RUNNING KERNEL IDENTITY:             RESOLVED — FACT-OPERATOR
EXACT KERNEL-SOURCE COMMIT:          DEFERRED / NOT CURRENTLY LOAD-BEARING
UNKNOWN GROUP #1 OVERALL:            PARTIALLY RESOLVED AT THE REQUESTED SCOPE
```

More precisely:

- every operator-listed non-kernel package has an exact upstream source commit;
- all packages that carry Research #2A's task, authorization, migration,
  storage, and UPID findings are mapped to the exact installed target;
- the installed signed-kernel package and running-kernel version are known, but
  no kernel source commit is invented;
- the kernel source mapping may remain deferred until experiment #16/reboot
  semantics makes it load-bearing;
- unlisted package versions remain outside this evidence set and are not
  inferred; and
- disposable-fixture, topology, storage-backend, ACL-window, restart, and
  controlled-experiment dependencies are not package-mapping unknowns and
  remain separately open.

There is no longer a `pveversion -v` operator-read blocker for Research #2A's
audited package set. That narrow closure does not resolve Family B.

## 8. Experiment scheduling impact

No experiment is authorized by this table. It removes only version-mapping
dependencies and names the next truthful research status. Source applicability
does not promote a runtime property.

| # | Experiment family | Previous #2A scheduling status | #2A.1 scheduling status | Version-only change and remaining condition |
| --- | --- | --- | --- | --- |
| 1 | QEMU same-slot create/destroy/create | `VALIDATION / ADVERSARIAL CONFIRMATION` | **VALIDATION / ADVERSARIAL CONFIRMATION** | Exact target 9.2.6 applicability is confirmed; disposable fixture, approval, and adversarial no-task-route boundary remain. |
| 2 | LXC same-slot create/destroy/create | `BLOCKED BY MISSING VERSION/ENVIRONMENT DATA` | **VALIDATION / ADVERSARIAL CONFIRMATION** | The version blocker is removed by exact 6.1.13 mapping; disposable LXC fixture/environment and approval remain. |
| 3 | QEMU clone variants | `VALIDATION / ADVERSARIAL CONFIRMATION` | **VALIDATION / ADVERSARIAL CONFIRMATION** | Exact target applicability is confirmed; runtime variant validation remains. |
| 4 | LXC clone | `BLOCKED BY MISSING VERSION/ENVIRONMENT DATA` | **VALIDATION / ADVERSARIAL CONFIRMATION** | Exact 6.1.13 `vzclone` source applies; fixture and approval remain. |
| 5 | QEMU snapshot create/delete/rollback | `VALIDATION / ADVERSARIAL CONFIRMATION` | **VALIDATION / ADVERSARIAL CONFIRMATION** | Exact target task types apply; class-P semantics and adversarial validation remain. |
| 6 | LXC snapshot create/delete/rollback | `BLOCKED BY MISSING VERSION/ENVIRONMENT DATA` | **VALIDATION / ADVERSARIAL CONFIRMATION** | Exact 6.1.13 task types apply; fixture and approval remain. |
| 7 | QEMU restore same/new VMID | `VALIDATION / ADVERSARIAL CONFIRMATION` | **VALIDATION / ADVERSARIAL CONFIRMATION** | Exact target route applies; accepted R/P/N evidence consequences and runtime variants remain. |
| 8 | LXC restore same/new VMID | `BLOCKED BY MISSING VERSION/ENVIRONMENT DATA` | **VALIDATION / ADVERSARIAL CONFIRMATION** | Exact container/storage applicability is established; storage-specific runtime variants, fixture, and approval remain. |
| 9 | QEMU migration | `NEEDS REFINEMENT BEFORE EXECUTION` | **NEEDS REFINEMENT BEFORE EXECUTION** | `qemu-server` and `pve-ha-manager` version dependencies are cleared; local/remote/HA/helper/failure, multi-node, and fixture design remain. |
| 10 | LXC migration | `NEEDS REFINEMENT BEFORE EXECUTION` | **NEEDS REFINEMENT BEFORE EXECUTION** | `pve-container` and `pve-ha-manager` version dependencies are cleared; the same variant/topology refinement remains. |
| 11 | QEMU disk/import/move/attach/storage | `NEEDS REFINEMENT BEFORE EXECUTION` | **NEEDS REFINEMENT BEFORE EXECUTION** | QEMU and storage versions are exact; the supported route/plugin matrix, backing topology, continuity relevance, and disposable fixture remain. |
| 12 | LXC rootfs/storage paths | `NEEDS REFINEMENT BEFORE EXECUTION` | **NEEDS REFINEMENT BEFORE EXECUTION** | Container and storage versions are exact; `move_volume` versus synchronous config and plugin/fixture scope remain. |
| 13 | High-volume pagination/archive rotation | `NEEDS REFINEMENT BEFORE EXECUTION` | **NEEDS REFINEMENT BEFORE EXECUTION** | Exact `pve-common` writer/rotation mechanics now apply; a candidate repeated-traversal protocol and adversarial interleavings still require definition. |
| 14 | Exact-UPID retention | `BLOCKED BY MISSING VERSION/ENVIRONMENT DATA` | **VALIDATION / ADVERSARIAL CONFIRMATION** | Exact `pve-common` UPID/log/cleanup source now applies; runtime retention, rotation, restart, rejoin, fixture, and approval remain unproven. |

Experiments #15, #16, and #17 remain **NEEDS REFINEMENT BEFORE EXECUTION**.
Package mapping does not close their service-restart, reboot/kernel-durability,
or interval-wide ACL-ABA questions. Experiment #16 is the point at which exact
kernel-source mapping may become load-bearing.

## 9. Remaining blockers after reconciliation

Package mapping is no longer the blocker for the audited target set. The
remaining Family B blockers include:

- no monotonic task cursor, immutable enumeration snapshot, archive generation,
  predecessor relation, or other accepted completeness anchor;
- no proof that all uncertainty reaches a stateful overlap sentinel before
  stale authority could be consumed;
- active-to-archive external gaplessness, crash durability, and concurrent
  pagination/rotation completeness remain unproven;
- exact-UPID runtime retention, restart, reboot, rejoin, and node-name reuse
  semantics remain unproven;
- interval-wide authorization visibility, including ACL/token ABA, remains
  unproven;
- multi-node ownership, membership, migration helper/failure, disappearance,
  and rejoin coverage remain incomplete;
- Research #2A still has no source-proven continuity-relevant physical/logical
  replacement path whose required transition occurs through a no-UPID route;
- plugin-specific storage operation coverage left `UNKNOWN` by #2A remains
  `UNKNOWN`;
- the exact kernel-source mapping is deferred until it becomes load-bearing;
  and
- every experiment still needs a refined protocol where noted, a separately
  approved controlled window, and an explicitly disposable non-production
  fixture. CT112 is not production evidence and is not designated as that
  fixture here.

## 10. Research execution statement

This reconciliation performed source and documentation review only. It made
zero live PVE calls, zero live Home Assistant calls, zero private-network
connections, zero PVE/guest/storage/kernel lifecycle actions, and zero Research
#2B experiment executions. It created no trusted enrollment and authorized no
experiment, implementation, deployment, WAVE B1, or Phase 1C work.
