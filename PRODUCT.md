# Hubinet Ops — product definition

Hubinet Ops is a practical Proxmox operations application for a trusted,
self-administered PVE environment, driven from Home Assistant.

## The product

```text
PVE autodiscovery
  -> dynamic backend inventory
  -> dynamic Home Assistant representation
  -> package/update scanning
  -> exact update plan shown to the operator
  -> explicit operator approval
  -> fresh Hubinet-owned snapshot
  -> update, with live job output
  -> healthcheck
  -> same-job rollback when appropriate
```

Plus ordinary lifecycle control (start/stop/reboot) and manual snapshot
operations on discovered guests.

## What each piece means

**Dynamic PVE discovery.** Every LXC and QEMU guest, and every node, is
discovered from the PVE API. Adding or removing a guest in Proxmox never
requires a repository change, a config change, or a static VMID list.

**Dynamic backend inventory.** The backend owns a durable database that is the
source of truth for inventory identity and state. Home Assistant presents it;
Home Assistant is never an authority.

**Dynamic Home Assistant representation.** Devices and entities follow
discovery. A guest that appears in PVE appears in HA; a guest that disappears
stops being shown as current, without hand-maintained cards.

**Package/update scanning.** Automatic, scheduled scanning of installed and
available packages inside managed guests. LXC on Debian/Ubuntu is the first
target. The operator sees the exact package detail: name, architecture,
installed version, candidate version, origin, description, and further
metadata — including security classification and reboot-required only where
those can be established reliably. A field that cannot be established
reliably is reported as unknown, never guessed. Architecture is part of a
binary package's durable identity, not presentation metadata: `libfoo` for
`amd64` and `libfoo` for `i386` are two different packages, and the operator
can tell them apart in the exact plan (see `ARCHITECTURE.md`, "Binary package
identity").

**Update plan and approval.** Scanning produces a concrete plan: exactly which
packages would change, from which version to which version. The operator reads
that plan and approves it explicitly. Nothing installs without that approval.

**Job, snapshot, live output.** An approved update runs as a job. The job takes
its own fresh pre-update snapshot first, then runs the update with live output
the operator can watch.

**Healthcheck and rollback.** After the update, the job health-checks the guest.
On failure the operator may roll back, or a configured same-job compensation
policy may roll back — always and only to the snapshot this job created.

## Hard rules

These are requirements, not preferences.

1. **NO AUTO-UPDATE.** The application never installs package updates on its
   own. "99 updates available" is never permission to install them.
2. **A changed package plan invalidates approval.** If the plan materially
   differs at execution time from the plan that was approved, fail closed and
   ask for approval of the new plan. Never execute a plan the operator did not
   approve.
3. **Every update job creates its own fresh Hubinet-owned snapshot** on the
   actual current guest, before touching any package.
4. **Rollback goes only to that same job's snapshot.** Never to an arbitrary
   older snapshot, and never days later.
5. **Hubinet cleanup never deletes ordinary or manual PVE snapshots.**
   Automatic retention applies only to snapshots Hubinet created and owns.
6. **A failed or unavailable scan means unknown, never zero updates.** Absence
   of evidence is reported as unknown.
7. **Failed or partial PVE discovery never means resource deletion.** A guest
   missing from an incomplete scan is retained and marked uncertain, not
   removed.

## What package scanning may do

Automatic package scanning is **non-installing and non-destructive to workload
packages**. It may refresh package-manager metadata and run package-manager
simulations, but it must not install, upgrade, remove, autoremove, or configure
workload packages.

Concretely, for Debian/Ubuntu LXC guests: refreshing the APT repository index
(`apt-get update`) is acceptable, and so is a simulated upgrade
(`apt-get -s`, `apt list --upgradable`, or equivalent). Writing APT's own index
and cache metadata is a normal, expected side effect and is not a violation.
Installing, upgrading, removing, autoremoving, or reconfiguring a workload
package is a violation, and may only happen inside an approved update job.

## What an approved update job may do

Package mutation is the one thing Hubinet Ops does that changes a workload,
and it is bounded on every side.

**Exactly one operation, exactly once.** One update job may cause at most one
real package operation, on one guest, owned by this backend, this job, and
this exact resource incarnation. A reused VMID, a moved or replaced guest, a
changed binding, or a second job never inherits it. A backend crash, an SSH
loss, a timeout, a lost response, or a restart never causes it to run again:
the operation is journaled durably on the Proxmox host before it can start,
and a recovering backend reattaches to that record instead of guessing.

**One fixed command.** The operation is a fixed, non-interactive
`apt-get upgrade` of already-installed packages. It cannot install a new
package and cannot remove one -- that is a property of the package manager's
own upgrade resolver, not a flag Hubinet sets. No package name, version,
option, or command text supplied by anything else ever reaches it; the
approved plan is used only to *refuse* the operation, never to build it.

**Operator configuration is preserved.** A configuration file the operator
edited is never silently overwritten by a package's version of it. The
package's version is left alongside as `<file>.dpkg-dist` for the operator to
review. Nothing prompts, and nothing waits for input that will never come.

**The plan is re-proved immediately before, not earlier.** Fresh package
manager state is read, the exact plan is recomputed, and it must still equal
the approved plan exactly. A changed plan fails closed (hard rule 2). A
package that someone else changed in the meantime fails closed. Unfinished
package-manager state on the guest fails closed.

**Finishing is proven, not assumed.** A zero exit code is never treated as
proof. Completion requires the guest's own package database to show every
approved package at exactly its approved new version, nothing else changed at
all, and no package left half-installed. Anything less is not "complete".

**A failed, partial, or unknown update never abandons the guest.** If the
operation fails, times out, is interrupted, or cannot be proven, the job
keeps its pre-update snapshot, keeps ownership, and stays the one job in
charge of that guest, so the healthcheck and rollback stages have something
to act on. It is never silently retried and never reported as success. The
only case that releases the job is a durable proof from the host that no
package operation ever started, and could never start.

**Package mutation succeeding is not the job succeeding.** The job is not
complete until it has been health-checked.

## Not the product

- Defending Hubinet against a hostile Proxmox administrator or a hostile root
  inside a managed guest. See the threat model in `AGENTS.md`.
- Cryptographic proof that a guest at a reused VMID is the same workload as
  before. Ordinary identity handling (see `ARCHITECTURE.md`) is sufficient.
- Arbitrary remote shell. Every host-side operation is typed and allowlisted.
- Managing Proxmox hosts Hubinet was not deliberately pointed at.

## Product lifecycle

Hubinet Ops itself is installed once and updated in place. Fresh bootstrap
(`deploy/bootstrap-proxmox-0.5.sh`) is the first-install, disaster-recovery,
and deliberate-rebuild path; ordinary code updates use
`deploy/update-proxmox-0.5.sh` against the existing installation and must
not require destroying it. See `ARCHITECTURE.md`, "In-place product
updates".

## Current state

What is built today and what comes next is in `STATUS.md`.
