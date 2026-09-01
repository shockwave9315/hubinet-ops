# Hubinet Ops — architecture

Current architecture only. What the product is for is in `PRODUCT.md`; what is
built today is in `STATUS.md`.

## Shape

```text
Proxmox VE
  -> Hubinet backend            (app/inventory_runtime.py composition root)
  -> authoritative inventory/scan DB (SQLite, app/inventory/)
  -> package scan scheduler      (typed SSH -> forced PVE helper -> pct exec)
  -> HTTP API                   (/r0/v1, bearer auth)
  -> Home Assistant             (custom_components/hubinet_ops/)
```

The backend reads PVE over the API, reconciles what it sees into a durable
inventory, and publishes one consistent snapshot. Home Assistant polls that
snapshot and presents it. **Home Assistant is presentation and controlled
input; it is never an authority and never talks to Proxmox.**

## Backend

`app/inventory/` is an independently instantiable subsystem with its own SQLite
database (marker `hubinet_ops_0_5_authority`, schema v10). Schema v10 retains
the immutable scan/approval authority and internal durable package-update jobs,
and adds the job-owned snapshot operation identity, its write-ahead uncertainty
checkpoint, the observed PVE task identity, and SQL-level state-machine
invariants over all of them. There is no migration from v9; pre-release installs
use the product updater's explicit backed-up authority reset and require Home
Assistant re-enrollment.

- `store.py` — schema, transactions, CAS/fencing for discovery-run ownership,
  backend/source/global-revision bookkeeping.
- `authority.py` — the typed mutation boundary. Every durable state change goes
  through it; nothing else writes the database.
- `provider.py` / `discovery.py` — the PVE provider contract and snapshot
  normalization. A baseline is `complete` only when permission coverage and
  boundary hashes agree; anything else is `partial`, `source_unavailable`,
  `configuration_error`, or `invalid`.
- `reconciliation.py` — applies one complete normalized snapshot inside the
  caller's transaction.
- `publication.py` — assembles the published snapshot (backend, sources, nodes,
  resources, revisions) in one consistent read transaction.

`app/inventory_runtime.py` is the production composition root, served via its
`create_app_from_env` factory
(`uvicorn app.inventory_runtime:create_app_from_env --factory`). It builds the store, authority,
publication, PVE transport, and scheduler, and serves `GET /r0/v1/health`,
`/backend`, `/snapshot`, plus the single narrow authority mutation
`PUT /r0/v1/resources/{resource_id}/package-plan-approval`. Bearer
authentication is required on every endpoint except the deliberately
unauthenticated minimal `/r0/v1/health` liveness probe, which exposes no
inventory or credential data. Approval changes only the authority database and
has no host-control path.

`app/inventory_pve_transport.py` is GET-only with mandatory TLS verification
and no mutation-verb escape hatch. `app/inventory_scheduler.py` is a thin
orchestrator over authority methods — it never touches tables directly.

Configuration (`app/inventory_runtime_config.py`, `config/inventory.example.yaml`)
describes how to reach a Proxmox **source**. It never enumerates workloads.

`app/package_scan_scheduler.py` is an independent single-worker scheduler. It
reads the validated runtime interval (default six hours), issues durable
per-resource scan ownership through `InventoryAuthority`, and scans only
current LXC resources. `app/package_scan_host_control.py` sends one bounded JSON
request over a dedicated pinned-key SSH connection to the bootstrap PVE node.
The PVE forced helper accepts only `scan_packages`, rechecks live type/node/
status before each fixed operation, and uses fixed `pct exec` shapes for OS
inspection, `apt-get update -qq`, `apt-get -s upgrade`, and the
reboot-required marker. The cluster is multi-node: when the guest's validated
current node differs from the PVE node the helper is running on, it routes
that same fixed `pct exec` shape to the guest's node over root's existing
Proxmox cluster-member SSH trust rather than executing locally — no per-node
Hubinet credential. QEMU is published as unsupported.

Package-update job authority is persistence-only in this stage. A directly
instantiated `InventoryAuthority` can issue and revalidate one globally
single-flight job from a current exact approval, and startup interrupts any
pre-package-mutation active job so it cannot auto-run after restart. The
production HTTP and Home Assistant surfaces cannot issue a job, and there is
no job consumer, workload package mutation, healthcheck, or rollback
execution. Authority revalidation is necessary but not sufficient permission
for future mutation: the activation stage must also prove exact APT
simulation/equality immediately before execution.

## Job-owned snapshot safety

The safety primitives for one update job's single pre-update PVE snapshot
exist internally and are **not production-reachable**. Nothing on the HTTP,
Home Assistant, scheduler, bootstrap, or updater path can create a PVE
snapshot, and no snapshot helper, key, or PVE mutation privilege is deployed.
There is still no workload package mutation anywhere.

```text
issued -> preflight_passed -> snapshot_may_have_started
       -> snapshot_confirmed -> (mutation, still unimplemented)
```

**The uncertainty boundary.** `snapshot_may_have_started` is a write-ahead
checkpoint committed durably *before* any snapshot mutation request can be
sent. Once a job reaches it, nothing may *infer* that no PVE mutation
happened. Startup recovery therefore interrupts `issued`, `preflight_passed`,
and `snapshot_confirmed` jobs — all provably before package mutation, with a
confirmed snapshot simply retained — but leaves a
`snapshot_may_have_started` job active, still owning the global destructive
slot, with its evidence fenced. It never replays a snapshot operation and
never guesses an outcome.

**Proved non-submission is not an inference.** Because that checkpoint is
committed before the host is ever called, an ordinary pre-flight refusal on
the host — a guest that moved off the job's frozen node, a guest that is
gone, a failed PVE read — would otherwise fence the single global
destructive slot forever, with no PVE mutation having been attempted at all.
The host therefore answers every failure with a typed submission proof drawn
from its durable operation journal. `not_submitted` is emitted only while
that journal is still at `intent`, which — because the `intent -> submitted`
transition is an fsynced atomic rename performed strictly before the
submission subprocess is launched — proves the mutation was never launched
for that operation identity. Only that proof terminalizes the job (as
`blocked`, evidence retained), and authority additionally refuses it for any
job that ever recorded a PVE task, since such a job provably *was* submitted.

Everything else stays uncertain and fenced: a canonical absence, an
in-flight guest lock, ambiguous ownership, a polling timeout, a lost SSH
answer, an unreadable or corrupt journal, a lease held by another
invocation, and every failure at or after the `submitted` transition. The
releasing token is matched exactly, so a helper that reports no proof at all
can never release a job.

**One transaction per authorized transition.** Every transition whose safety
depends on current authority — preflight, the write-ahead snapshot intent,
and snapshot confirmation — re-proves the *complete* current-authority
predicate inside the same SQLite transaction that commits it (`BEGIN
IMMEDIATE`, so no other writer can interleave). Proving in one transaction and
committing in another is a check-then-commit race: discovery reconciliation
can invalidate the job's resource incarnation in between while the VMID, node,
resource type, and running status all stay identical, so neither the
checkpoint CAS (which sees only job status and checkpoint) nor the host helper
(which can verify only live PVE facts, never a backend incarnation) would
catch it. Transitions that merely preserve evidence about a possible PVE
mutation — recording the observed task, recording uncertainty, startup
fencing — deliberately do **not** require current authority, because
staleness must never discard evidence.

**Identity.** `app/inventory/snapshot_identity.py` derives one job's snapshot
name and snapshot operation id purely from immutable identity (backend
instance, job, resource incarnation, continuity revision), so the same job
derives the same identity after any restart and two jobs never collide. The
name satisfies PVE's verified `pve-snapshot-name` contract (`pve-configid`,
maxLength 40, `current`/`vzdump` reserved). **The name is only the physical
PVE key and is never ownership proof.** Ownership is a strict structured
metadata record carried in the snapshot's PVE description; malformed,
incomplete, duplicate, or mismatching metadata fails closed, and a
Hubinet-looking snapshot that will not parse is reported as ambiguous rather
than silently skipped.

**Verified PVE semantics.** Read from current Proxmox VE sources rather than
inherited from Hubinet 0.4: snapshot create is asynchronous and returns a UPID
immediately, so a returned POST proves nothing; task status is
`running`/`stopped` plus an optional `exitstatus`, and PVE's own rule treats
only `OK` and `WARNINGS: <n>` as non-errors, so `stopped` alone is never
success; the snapshot listing includes PVE's synthetic `current` pseudo-entry
and carries `snapstate` for unfinished snapshots; and an LXC snapshot
description does not round-trip byte-identically, because the config parser
appends a newline to every line it reads back.

Strict fresh canonical evidence is therefore mandatory in every case before
`snapshot_confirmed`, and a terminal successful task is never sufficient on
its own. Task evidence is not, however, universally *necessary*: the normal
path observes a PVE task and requires it to have reached a terminal non-error
state, while the submitted-without-recorded-UPID recovery path establishes
completion from its durable operation-journal state plus that same strict
canonical evidence, rather than fabricating a task identity it never saw.

**Host-side durable journal.** `deploy/hubinet-package-snapshot-helper.py` is
a separate file and a separate logical privilege boundary from the scan
helper, which stays scan-only. It exposes two typed operations
(`inspect_job_snapshot_state`, `ensure_pre_update_snapshot_submitted`), no
delete, no rollback submission, no arbitrary shell or argv, and it re-derives
the snapshot identity from the request's own ownership facts rather than
trusting the name it was handed. `ensure_pre_update_snapshot_submitted` is
submission-only: it never polls a PVE task to completion, so it returns the
instant the operation has crossed (or is already past) its submission
boundary. Each mutation is journaled on the PVE host under an `flock` held
per VMID, keyed by the operation identity:
`intent -> submitted -> task_known -> terminal`, each transition an fsynced
atomic rename. A journal still reading `intent` proves submission was never
attempted; `submitted` is the genuinely uncertain window and is **never**
resubmitted — it may only be recovered as success on strict evidence (the
exact snapshot, exact ownership metadata, complete, and the guest carrying no
in-flight config lock), otherwise the answer is `uncertain`. An identical
retry reattaches; the same operation identity with a different request is
refused. Every response from either operation carries this exact phase as a
typed `submission_state` field, so the backend never infers it from canonical
PVE state or an error string.

**The submission critical section.** The write-ahead checkpoint alone leaves
a second, narrower race open: another Hubinet writer (discovery
reconciliation, a package scan) can invalidate this job's authority *after*
it was proved and *before* a NEW PVE submission is actually sent, in the gap
between the intent transaction and the host-control call. Proving authority
and calling the host in two separate transactions is a check-then-commit race
exactly like the one the write-ahead checkpoint itself was built to close. So
a NEW submission runs only inside
`InventoryAuthority.execute_snapshot_submission_if_current`, which re-proves
the job's whole current-authority context and calls
`ensure_pre_update_snapshot_submitted` while it still holds the authority
store's one writer lock — nothing else may write to that store for the
whole of it. Because the host call it invokes never polls a PVE task to
completion, the writer lock is held only for one bounded round trip, never
for PVE's own asynchronous task. Before attempting any of this, the
orchestrator first reads the host's durable `submission_state` — a pure read
that never requires current authority, because recovering evidence about an
operation that may already have been submitted must never be discarded
merely because authority has gone stale. Only `absent` and `intent` states
permit a NEW submission; every other state skips the critical section
entirely and goes straight to read-only recovery. Task polling and canonical
confirmation both happen strictly *after* the critical section releases its
writer lock, through the same read-only `inspect_job_snapshot_state`
operation, bounded by `PackageUpdateSnapshotOrchestrator`'s own retry loop —
never by holding a database transaction open across it. This is not a claim
that SQLite and PVE are proved atomic, and not a claim that a snapshot is
proved to belong to the same LXC PVE showed several minutes ago (see
"Identity" below): the claim is that Hubinet's own current authority is held
stable through the submission boundary, and the host independently
re-validates the live PVE target immediately before it ever submits.

**Liveness after a refusal.** A stale authority context always refuses a NEW
submission — `execute_snapshot_submission_if_current` never authorizes one on
stale authority, full stop. But if the underlying resource or source is gone
or replaced for good, every future retry would repeat that identical refusal
forever, permanently occupying the one global destructive slot with a job
that can never advance. The refusal alone therefore does not decide the
job's fate: `PackageUpdateSnapshotOrchestrator` takes one FRESH host read —
never the read this attempt started with, since another invocation could
have been authorized and actually submitted in the gap between this
attempt's first read and its own refusal — and only THAT fresh read decides.
If it still proves `absent`/`intent`, the job is safely terminalized via
`block_package_update_before_snapshot_submission` and the slot is released;
anything else it shows (a submission already in flight, a terminal outcome,
or the read itself failing to prove anything) is recovered through the
ordinary evidence pipeline exactly as if this attempt had never tried to
submit — never released as unsubmitted, and never resubmitted. This is safe
because `execute_snapshot_submission_if_current` only serializes Hubinet's
own writers against each other: by the time one invocation observes a
refusal, any concurrent submission attempt that WAS authorized has already
crossed whatever durable host journal phase it reached and released the
writer lock, so a fresh read can see it even though the stale first read
could not.

**Same-job rollback.** A job may roll back only to the snapshot that exact job
created and confirmed. `select_package_update_rollback_target` re-proves the
name, the full structured ownership metadata, `resource_id`, the continuity
revision, that the entry is a real snapshot rather than the `current`
pseudo-entry, and that canonical state is unambiguous. There is no
caller-supplied snapshot name, no "latest Hubinet snapshot", and no fallback
to another job's snapshot; a reused VMID never transfers rollback authority to
a different incarnation. **Rollback submission is deliberately not
implemented**: only the authorization/selection contract exists, and execution
is left to the later activation stage rather than being shipped to a lower
safety bar.

**No deletion.** This stage deletes nothing — not foreign snapshots, not
manual PVE snapshots, not old, failed, or interrupted-job Hubinet snapshots.
Retention is separate future work.

`app/package_update_snapshot.py` (orchestration) and
`app/package_update_snapshot_host_control.py` (a purpose-specific pinned-key
SSH client, not a revival of the removed generic `app/host_control.py`) are
instantiated only by hermetic tests. `tests/test_r0_architecture_regression.py`
proves production reachability did not increase.

## Identity

Practical, not metaphysical:

- **VMID** is a current Proxmox locator: which slot a guest occupies right now.
  It is reusable and is never durable identity.
- **`resource_id`** is an opaque backend-generated UUID — the inventory
  identity of one guest incarnation. Home Assistant entities key off it.
- **`inventory_source_id`** identifies the Proxmox source.
- **Locator bindings and generations** record which `resource_id` occupied
  which `(source, vmid)` slot over which run range.
- **Retained history.** A guest absent from a complete baseline becomes
  `missing`/`quarantined` and is kept. A guest whose type changed in place is
  `replaced`: the old incarnation is retired with a successor pointer, and a
  new `resource_id` takes the slot.

This exists so that an observed gap or replacement does not silently transfer
policy to a different workload, and so Home Assistant's device/entity registry
does not get corrupted when a VMID is reused. **That is ordinary correctness,
not a security proof.** Nothing here claims to prove physical workload
continuity, and no feature is gated on such a proof.

### Inert compatibility fields

Two names survive from the removed security-proof architecture. They are wire
compatibility only, and neither is a requirement:

- **`security_continuity`** — present in the schema, the published snapshot,
  and the HA contract. The backend writes only `unverified`; the schema
  constrains it to exactly that. There is no trust-granting state machine and
  no code path produces any other value. The HA contract's enum still lists
  `trusted`/`revoked` so the wire format did not have to change.
- **`presence = confirmed_removed`** — retained in the HA contract enum and its
  validators. The backend has no writer for it: the operation that used to
  produce it was removed, and the backend's own schema no longer permits the
  value.

Both should disappear when the snapshot contract is next revised. Do not build
anything on them.

## Home Assistant integration

`custom_components/hubinet_ops/` — one `DataUpdateCoordinator`, one snapshot
fetch per refresh, structural validation of the payload in `contract/`, then
devices and entities. The response-capable `view_update_plan` action accepts a
native Hubinet resource-device selection, resolves its backend-owned
`resource_id`, performs a separate fresh snapshot read, and returns exact
package rows plus the exact approval reference. `approve_update_plan` forwards
that caller-supplied reference unchanged to the backend and refreshes the
coordinator after success. One concise resource sensor displays the
backend-published `none | approved | stale` approval state. Package rows do not
become entity attributes or package-per-entity state.

The coordinator is **not** a reconciler. It never infers `missing` from a diff
between two polls, and it never assumes revision `N -> N+1` means backend
transaction adjacency: publications can skip arbitrary intermediate states.
Any invariant that needs durable history belongs in the backend.

## Deployment

`deploy/bootstrap-proxmox-0.5.sh` (+ `deploy/lib/`) is the product-facing PVE
host entrypoint: it creates a fresh unprivileged Debian LXC at the next free
VMID, provisions a least-privilege PVE identity whose effective permissions are
verified as the exact set `{Sys.Audit, VM.Audit}`, establishes PVE TLS trust,
deploys the source into the CT via `deploy/install-0.5.0-fresh.sh`, generates
the source-centric config, and installs a mandatory nftables boundary — in a
fixed fail-closed order, after one upfront confirmation of the whole plan.

The Home Assistant half ships separately through HACS. HA never receives a
Proxmox credential; it authenticates only to the Hubinet backend with the
backend's own bearer token.

## In-place product updates

`deploy/update-proxmox-0.5.sh` (+ `deploy/lib/update-*.sh`) is the
product-facing update entrypoint for an *existing* installation — install
once, update many times. It is a separate PVE-host operator action from
bootstrap, never a second bootstrap: it never invokes
`deploy/install-0.5.0-fresh.sh`, never recreates the LXC or changes its
VMID/network, and never rotates the PVE identity/token secret, the HA
bearer, or the host-control key.

Every invocation, including `--dry-run`, first takes a non-blocking
kernel-backed `flock` lease on the PVE host at
`/var/lib/hubinet-ops/update-state/vmid-<vmid>.lock`. The descriptor remains
open across recovery, ownership verification, planning, confirmation,
staging, activation, acceptance, rollback, and cleanup. Legitimate updates
for one VMID are therefore single-flight while different VMIDs remain
independent; an unheld lock file after process death or reboot is not a stale
lock.

```text
--vmid <N>
  -> prove installation ownership (host-control key comment, authorized_keys
     forced-command marker, PVE user/token comments, all cross-checked
     against one recovered run-id; exact effective PVE privilege set)
  -> classify target artifacts against one exact confirmed git commit
     (app payload always replaced; requirements.txt/unit/PVE helper/
     authority schema each compared exact-content)
  -> print the exact plan; require approval (a dedicated second
     confirmation, or --yes --allow-authority-reset non-interactively, when
     the authority schema requires a reset)
  -> stage every replacement while the old service is still healthy
  -> temporarily disable the service's boot activation (the first mutation
     of the window), then stop it
  -> activate in one fixed order (app, venv+requirements if changed, unit
     if changed, PVE helper same-path content swap if changed, authority
     preserve-or-reset), retaining rollback material; a changed
     requirements.txt BUILDS the new virtualenv at its final live path
     inside this window
  -> start, prove systemd active + local HTTP health within the existing
     service timeout, then accept (reused bootstrap discovery contract,
     extended with an optional minimum-committed-sequence floor to prove a
     genuine post-restart cycle — a committed source that is otherwise
     fully coherent but has not yet published a run past that floor is a
     TRANSIENT condition and keeps polling within the existing discovery
     timeout, never an immediate failure; every other incoherence is still
     immediate and terminal; host-control forced-boundary re-probe;
     firewall byte-identical + active)
  -> restore boot activation and positively prove it enabled, then record
     the run completed
  -> on any failure after boot activation or a service stop was attempted:
     first positively prove the service non-running, then perform coherent
     rollback, including a validated authority-database backup restore when
     a destructive reset had already happened, and re-prove the restored
     service enabled before declaring recovery complete
```

**Authority schema decision.** `deploy/lib/hubinet-ops-authority-tool.py`
(run inside the CT) is a small, read-only-first inspector: it reads
`authority_schema`'s marker/version, `backend_instance.backend_instance_id`,
and the live set of table/index/trigger names, and reports whether the
database is recognizable — never a security proof, just enough to classify.
The target schema is read statically (a regex over the target commit's
`app/inventory/store.py` text, never an executed import) so the updater
never has to run target application code to plan; the same static read also
extracts the target's *required* schema-object set. Same marker/version:
before ever stopping the service, the updater additionally proves the live
database's actual schema objects match that required set exactly — a
matching marker/version alone is weaker than the target runtime's own
schema validation (`app/inventory/store.py`'s `_REQUIRED_SCHEMA_OBJECTS`
check), so a structurally drifted database that would otherwise be
misclassified "preserve" and then get rejected by the target runtime at
restart instead fails closed here, before any mutation. Different schema:
no migration exists in this product's current scope (see `AGENTS.md`) — a
coherent backup (`sqlite3`'s stdlib online backup API, integrity-checked and
re-validated against the pre-reset identity before the live file is
removed) followed by removal; the target runtime creates its own fresh
schema on next start. The updater never writes authority schema DDL itself.
`remove` fails closed: a present-but-unremovable database or WAL/SHM
sidecar is an immediate reported failure (independently re-verified absent,
never assumed from the unlink call's own success alone), and rollback
never copies the validated pre-update backup over an authority database
whose removal it cannot prove.

**Virtualenv replacement.** A code-only update — the common case — never
rebuilds the environment, never runs `pip`, and leaves `/opt/hubinet-ops/
.venv` untouched. When `requirements.txt` actually changes, the target
environment is created **directly at `/opt/hubinet-ops/.venv`**, inside the
mutation window, after the old environment has been renamed to
`.venv.rollback-<runid>` and that live path has been positively proven
absent. It is deliberately *not* built at a staging path and renamed into
place: a Python virtualenv is not generally relocatable, because the
console entrypoints `pip`/`ensurepip` generate embed the absolute
interpreter path of the environment they were created in, and a rename
rewrites none of them. Rewriting shebangs is not an accepted alternative.
The cost is a longer maintenance window whenever dependencies change; that
is accepted rather than optimized away with wheel caches, download stages,
or a package mirror. A failed or interrupted build leaves a partial
environment at the live path; it is never resumed — rollback removes it,
proves the path absent, and restores the preserved old environment.

**PVE host helper update contract.** The forced-command `authorized_keys`
line, the pinned host-control key, and `known_hosts` are never touched.
Only the helper file's *content* is ever replaced, staged as a temp file
in the same directory and atomically renamed over the existing path — same
path, new content, exactly like `deploy/hubinet-package-scan-helper.py`'s
own request/response version check already expects.

**Rollback.** Filesystem rollback material
(`app.rollback-<runid>`, `.venv.rollback-<runid>`, `requirements.txt.
rollback-<runid>`, a preserved copy of the systemd unit, a preserved copy
of the PVE helper, and a preserved copy of the installed-source marker) is
retained until acceptance succeeds, using the same ledger mechanism
(`deploy/lib/bootstrap-common.sh`) bootstrap's own rollback already relies
on. For every one of those artifacts, a durable "attempted" ledger marker
is recorded *before* its first destructive mutation (not after its swap
completes), and rollback restores based on which of that artifact's own
fixed, owned paths actually exist rather than trusting the marker to imply
a fully completed swap — correct for a failure at any intermediate point,
not only one after a complete swap, because a real rename is atomic. A
target failure after a destructive authority reset restores the coherent
*old* installation, database included — the updater never leaves old code
paired with a new, incompatible schema, and never a new installed-source
marker paired with a rolled-back old installation.

**Temporary service-autostart guard.** Bootstrap leaves the CT at
`onboot=1` and `hubinet-ops.service` enabled, so without a guard a PVE host
power loss part-way through an update would bring the CT back and let
systemd boot-activate a half-swapped runtime — a target app paired with the
old venv, or a freshly activated unit paired with an old helper or database
— before any later updater invocation could read the journal and roll back.
The updater therefore removes the service's *boot activation* for the whole
mutation window, using the minimum existing systemd mechanism: immediately
before the first mutation it re-proves the unit enabled, arms rollback,
durably journals an autostart-disable-attempted marker, and only then runs
`systemctl disable hubinet-ops`, positively proving the resulting
`UnitFileState`. The CT's own `onboot` setting is never changed, the unit is
never masked or replaced, and the updater still starts the disabled unit by
hand for target acceptance, exactly as systemd permits. The unit stays
disabled through target start, discovery/host-control/firewall acceptance,
and installed-source marker activation; `systemctl enable hubinet-ops` is
issued and *proven* only once the target is fully accepted with a coherent
marker, before the journal records the run completed — and equally on every
rollback and startup-recovery path, before recovery may be declared
complete. A reboot during the mutation window therefore leaves the service
inactive; the one remaining narrow window (accepted target, coherent marker,
enablement restored, journal not yet completed) can only start the fully
accepted target installation, never a mixed one.

The disabled/enabled unit-file state is itself ordinary filesystem state
under `/etc/systemd/system`, not a fact `systemctl`'s exit status or a
running-kernel probe alone proves durable — so it crosses the same CT
filesystem durability barrier (below) as every other rollback-critical
artifact: immediately after the disable request is proven and before the
service is stopped or anything else is mutated; immediately after the
final restore-enable is proven on success and before the journal records
the run completed; and immediately after restore-enable is proven during
rollback/recovery and before the old service is started again.

Service, unit-file-enablement, and rollback-path inspection are explicitly
three-valued: a failed or malformed probe is unknown, never evidence that
the service is stopped, that boot activation is disabled or restored, or
that a path is absent. Enablement in particular is read from systemd's own
`UnitFileState` rather than inferred from a command's exit status — a
`disable`/`enable` request may mutate state and still fail, or report
success without changing anything. Rollback does not mutate any managed file until systemd
positively reports the service non-running; every load-bearing removal is
independently re-proved absent before restoration; and a restored authority
database must have its service ownership and mode successfully reinstated
before restart. Whenever unit activation was ever attempted, rollback
requires a successful `daemon-reload` before the restored old service may
be started — including on a replay that finds the old unit file already
back on the live path, because that is a fact about the filesystem and
never proof that the systemd *manager* stopped holding the target
definition. Rollback's terminal proof that the restored installation is in
service is a bounded *poll* of both required runtime facts (systemd
`active`, plus a non-empty answer from the unauthenticated health
endpoint) against the existing startup deadline, not a single request:
`hubinet-ops.service` is `Type=simple`, so systemd reports `active`
strictly before uvicorn has bound `127.0.0.1:8787`, and a one-shot probe
misclassified that ordinary readiness race as a failed rollback. A unit
systemd positively reports as `failed` is terminal and fails earlier.

The same PVE-host directory contains at most one active bounded recovery
journal per VMID (`vmid-<vmid>.journal`). It records the update run-id, the
already-verified installation run-id, rollback-armed state, requirements and
authority classifications, the authority backup path when applicable, and
only the existing rollback ledger markers needed by `update-activate.sh`.
Each load-bearing checkpoint uses a flushed temporary file, atomic rename,
and directory flush before its destructive transition. On the next
invocation, the journal is inspected after taking the VMID lease and before
any new-run ownership or planning read. The updater re-verifies the same
installation and then either cleans a pre-mutation interruption or re-enters
the existing fail-closed rollback machinery with the prior run-id.

Because that rollback machinery runs entirely through the run-owned authority
helper in the container's volatile `/tmp` — three-valued path-state probes,
and the fail-closed database removal and validated-backup restore after a
destructive authority reset — recovery first re-pushes the same bounded
updater-owned tool to the same reconstructed run-owned path and positively
proves it usable. A real PVE/CT restart clears that `/tmp`, and recovery
deliberately never starts a new plan, so nothing else would restore it. The
re-push is recovery infrastructure only: no Phase U2, no target
application/configuration/identity content, no pre-update HTTP probe, and
bounded to the loaded run ID. If it cannot be restored and proven, recovery
hard stops with the journal, rollback artifacts, and authority backup
preserved.

Rollback is replayable. A first rollback may restore artifacts and then hard
stop at a later terminal step, retaining the active journal, so a later
invocation re-enters the same rollback for the same run-id. Every rollback
helper therefore tolerates already-restored state by inspecting the bounded
set of paths its artifact owns, while still failing closed on an unknown path
state; the PVE host helper keeps its canonical rollback copy unconsumed
(restore temporary plus atomic rename onto the live path) so retries retain
the original recovery material.

The authority database is the one rollback-managed artifact whose restore
is *not* idempotent: once the restored old service is running again it may
legitimately write new authority state. So the journal carries one more
durable fact, `update-authority-restored`, recorded only after the restored
database has been positively inspected and before the old service can be
started. A replay that sees it never removes the live database and never
re-applies the backup; it instead re-proves that the live database's
durable identity lineage (schema marker, schema version,
`backend_instance_id`, read from the retained validated backup itself,
since a recovery invocation has no planning facts) is still the restored
old authority. Content is expected to have advanced, so no whole-database
byte or hash comparison is ever made. A missing, corrupt, or
differently-identified database is a hard stop with the journal and backup
retained — manual diagnosis is safer than automatically overwriting a
database that may hold valuable post-rollback state.

Successful recovery proves the restored service enabled, active, and
healthy, clears the journal, and exits with an instruction to rerun; it never starts the
requested new plan in that invocation. If any ownership, service-state,
path-state, restore, start, or health proof is unavailable, the active
journal and referenced artifacts remain and every new update is blocked for
manual recovery. This covers ordinary races, process death, and host restart
under normal local-filesystem semantics. It is not a defense against a
malicious PVE root/administrator, hostile filesystem or kernel behavior, or
deliberate manual mutation of updater state.

**Filesystem durability barriers.** The durable host journal above proves a
namespace `mv`/`cp`/`rm` completed in the *running kernel*; it does not by
itself prove the data+metadata ordering a later transition depends on would
survive a subsequent PVE host power loss. Recovery material and activated
artifacts live on the Hubinet CT filesystem and, for the PVE host
package-scan helper, the PVE host filesystem itself. The one explicit rule:
before proceeding past a recovery-critical transition, the filesystem
containing the state the *next* transition relies on must have completed a
durability barrier — GNU coreutils `sync -f <path>` (already required and
already used by the journal itself), never a bare `sync`, a snapshot, a
WAL, or a transaction library. Applied throughout `update-activate.sh`: the
preserved old app/venv/requirements/unit/PVE-helper/installed-source-marker
is flushed before its replacement is activated; a restored artifact
(forward rollback or a later replay that finds it already restored) is
flushed again before rollback proceeds to the next artifact or the terminal
service restart; and the accepted target's own live filesystem state is
flushed once more — after acceptance and the installed-source marker, before
boot activation is restored and the journal records the run completed — so
an accepted target can never end up durably marked "completed" while its
live content still only exists in cache. The authority database's own two
durability transitions (the pre-reset backup, and the reset removal) are
implemented *inside* `hubinet-ops-authority-tool.py` itself: `backup` and
`remove` report `"ok": true` only after fsync-ing their own result (the
backup file's data and its immediate containing directory; the removed
directory entries, for `remove`). For `remove` that already is the whole
durability proof, because the database's containing directory
(`/var/lib/hubinet-ops`) predates this run. For `backup` it is *not* the
whole proof: the backup's own containing directory
(`update-backups/${UPDATE_RUN_ID}`, and possibly `update-backups/` itself)
is typically newly created by this same run, and fsync-ing a file's
immediate directory does not prove the directory-entry link that ties a
newly-created directory into *its own* parent survived a crash — a distinct
fact from the leaf file's own durability. So the caller crosses one more
explicit CT filesystem-level `sync -f` barrier over the backup's run
directory itself, closing that ancestry, before ever treating the backup as
destructively usable: only after that barrier passes is the reset-attempted
marker journaled and the live database removed. A barrier failure anywhere
in this file is load-bearing — it fails the run and triggers the same
coherent rollback as any other activation-window failure, never
warn-and-continue.

**Immediately-before-mutation ownership and plan fence.** The per-VMID
`flock` only serializes legitimate updater invocations; it does not stop a
legitimate PVE operator/tool action — removing this CT and restoring
another as the same VMID, or restoring a snapshot of the *same*
installation identity that rolls its live software/database state
backward — between planning and mutation. Immediately before the first
managed-state mutation (the autostart-disable request), the updater
re-verifies the full ownership chain against the originally-approved
installation run-id (`update_ownership_verify`'s `revalidate` mode) and
re-derives a small, bounded, in-memory plan fingerprint whose immutable
baseline comes directly from the original classification reads, before
the plan is displayed — installation run-id, the installed
requirements.txt/systemd-unit/PVE-helper content, the authority schema
marker/version, the pre-update `backend_instance_id`, and the planned
authority action, plus the target-required table/index/trigger set for a
`preserve` action. Deliberately excludes every naturally-changing runtime
fact (discovery sequence, timestamps, ordinary authority DB contents,
package-scan rows), so an ordinary background discovery cycle while the
operator reads the plan never invalidates it. A mismatch fails before
autostart is touched, before the service is stopped, and before any live
artifact is mutated — the operator is told to rerun planning/approval. This
is validation/fencing for ordinary operational races, not a second lock
system, and it does not attempt to prevent every ordinary PVE lifecycle
command in general.

See `deploy/README-update-proxmox-0.5.md` for the operator runbook.

## Package scanning for LXC

Implemented channel:

```text
Hubinet backend
  -> restricted typed host-control channel
  -> small PVE host helper / forced-operation boundary
  -> pct exec <validated current VMID>
  -> package manager inside the guest
```

Properties that channel must have:

- **Typed operations only.** A fixed, allowlisted set of operations with typed
  arguments. It never accepts arbitrary shell command text, and the host helper
  validates every argument independently of the backend.
- **Target validation.** The current VMID is resolved and validated against the
  live inventory immediately before the operation. A VMID is an execution
  locator, never durable identity.
- **Scan is non-installing.** Metadata refresh and simulation only; see
  `PRODUCT.md`, "What package scanning may do".
- **Exact plan fingerprint.** Successful scans sort the material triples
  `(package name, installed version, candidate version)` and hash canonical
  JSON with SHA-256. Optional metadata cannot change the fingerprint.
- **Ordinary concurrency control.** One scan per resource at a time; attempts
  are durably owned, fenced against binding/generation changes, and unfinished
  attempts recover as interrupted/unknown after restart.
- **Latest attempt wins.** A failure after an earlier success publishes null
  pending count and no stale package plan. Full exact rows remain available in
  the backend/coordinator snapshot but never become HA entity attributes.
- **Approval is an exact durable fact.** One per-resource row records the
  reviewed scan, its material fingerprint, and approval time. Effective
  `none`/`approved`/`stale` state is derived rather than persisted. Approval
  atomically requires the latest successful scan, a recomputed exact-row
  fingerprint matching stored and caller-supplied values, current resource
  binding/generation/continuity/VMID/node context, and the same fresh healthy
  committed source context captured when the scan was issued.

Update execution, healthchecks, rollback execution, lifecycle mutation, and
QEMU package execution remain future work; job-owned snapshot safety exists
internally but cannot be invoked by production. Exact APT execution must also
resolve multiarch package identity rather than guessing it.

## Ordinary safety rules (all layers, now and later)

- Least privilege — the PVE credential stays an exact verified minimum set.
- TLS verification is mandatory; a system-trust fallback requires explicit
  operator opt-in.
- Secrets never appear in argv, logs, diagnostics, or the published snapshot.
- Typed, allowlisted operations only; never arbitrary command text.
- Validate the current target before any mutation.
- Bearer authentication is required on every API endpoint except the
  deliberately unauthenticated minimal `/r0/v1/health` liveness probe, which
  exposes no inventory or credential data.
- Failed, partial, or unavailable discovery never deletes a resource.
- A failed scan is unknown, never zero updates.
- Concurrency protection against ordinary operational races: durable ownership
  CAS, single-flight per source, fencing of stale workers, restart recovery.
- A run finalized after the source's configuration context changed is fenced
  out rather than committed. The run-context CAS covers
  `source_config_revision`, `endpoint_id`, canonical transport locator and its
  canonicalization version, `transport_trust_revision`, and
  `provider_contract_version`.
