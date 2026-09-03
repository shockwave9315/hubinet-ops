# Hubinet Ops — architecture

Current architecture only. What the product is for is in `PRODUCT.md`; what is
built today is in `STATUS.md`.

## Shape

```text
Proxmox VE
  -> Hubinet backend            (app/inventory_runtime.py composition root)
  -> authoritative inventory/scan DB (SQLite, app/inventory/)
  -> package scan scheduler      (typed SSH -> forced PVE helper -> pct exec)
  -> package update worker       (one worker, woken only by operator action)
  -> HTTP API                   (/r0/v1, bearer auth)
  -> Home Assistant             (custom_components/hubinet_ops/)
```

The backend reads PVE over the API, reconciles what it sees into a durable
inventory, and publishes one consistent snapshot. Home Assistant polls that
snapshot and presents it. **Home Assistant is presentation and controlled
input; it is never an authority and never talks to Proxmox.**

## Backend

`app/inventory/` is an independently instantiable subsystem with its own SQLite
database (marker `hubinet_ops_0_5_authority`, schema v16). Schema v10 added
the job-owned snapshot operation identity, its write-ahead uncertainty
checkpoint, the observed PVE task identity, and SQL-level state-machine
invariants over all of them. Schema v11 added the explicit, material
`architecture` column to `package_scan_packages` and
`package_update_job_packages` (see "Binary package identity" below). Schema
v12 adds the job-owned package mutation operation identity and SQL-level
invariants tying the mutation checkpoints to their durable facts in both
directions. Schema v13 adds `accepted_prepared_evidence_digest`, the digest
of the exact preparation evidence the arming transaction accepted, so the
mutation-arm facts are one indivisible write-ahead authority fact and only
the invocation carrying that digest can submit (see "Crash-safe package
mutation" below). Schema v14 adds the same-job rollback operation identity,
its write-ahead uncertainty checkpoint, the observed PVE rollback task
identity, and rollback completion (see "Same-job rollback execution" below).
Schema v15 adds the operator-declared per-resource health contract (see
"Dynamic per-resource health contracts" below). Schema v16 binds a job to the
exact health contract generation it froze at issuance, adds that generation's
immutable probe rows and its durable definitive result rows, inserts the
`health_completed` checkpoint, and states the terminal `succeeded` contract in
both directions so a passing health verdict is the only route to it (see
"Job-bound healthcheck execution" below). There is no migration from v9
through v15; pre-release installs use the product updater's explicit
backed-up authority reset and require Home Assistant re-enrollment.

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
publication, PVE transport, the scheduler, and -- when `package_update.enabled`
is configured true -- the five production update host controls and the one
`PackageUpdateWorker`. It serves `GET /r0/v1/health`, `/backend`, `/snapshot`,
two families of authority-metadata mutation
(`PUT /r0/v1/resources/{resource_id}/package-plan-approval` and
`GET`/`PUT`/`DELETE /r0/v1/resources/{resource_id}/health-contract`), and the
explicit operator update controls described under "Production update
activation" below. Bearer authentication is required on every endpoint except
the deliberately unauthenticated minimal `/r0/v1/health` liveness probe, which
exposes no inventory or credential data. The two metadata families change only
the authority database and have no host-control path of their own.

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

Package-update job authority owns one globally single-flight job issued from a
current exact approval, and startup interrupts any pre-package-mutation active
job so it cannot auto-run after restart. Authority revalidation is necessary
but not sufficient permission for mutation: the execution-time equality gate
below proves exact fresh APT simulation/equality against the job's frozen
material, and even a successful pass is not a durable mutation permit -- the
mutation stage re-runs that exact equality proof itself, immediately before it
mutates, in the same transaction that commits its write-ahead uncertainty
boundary (see "Execution-time plan equality" and "Crash-safe package mutation"
below).

## Production update activation

The stages below -- job-owned snapshot safety, execution-time plan equality,
crash-safe package mutation, same-job rollback, and job-bound healthcheck
execution -- are production-reachable, and this section is the whole of how.

### The two operator entry points, and nothing else

```text
POST /r0/v1/resources/{resource_id}/package-update
  -> resolve THIS resource's current durable approval
  -> InventoryAuthority.issue_package_update_job(resource, approval, request_id)
  -> 202                                  (the job is already durable)
  -> wake the worker                      (a hint, never authority)

POST /r0/v1/resources/{resource_id}/package-update/rollback
  -> resolve the one applicable ACTIVE job
  -> read a FRESH canonical PVE listing   (read-only inspect_job_snapshot_state)
  -> InventoryAuthority.arm_package_update_rollback(job, listing)
  -> 202                                  (the intent is already durable)
  -> wake the worker
```

`issue_package_update_job` has exactly one production caller: that POST route.
`arm_package_update_rollback` has exactly one: the rollback route. Neither
scheduler calls either, no scan callback does, the approval write does not,
the Home Assistant coordinator does not, and the worker itself does not.
`tests/test_r0_architecture_regression.py` proves both statements by AST over
the composition root rather than by assertion.

The caller-controlled surface of the whole lifecycle is one UUID. The start
body carries `request_id` and nothing else; the resume and rollback bodies are
empty. Every request model is `extra="forbid"`, so a body naming a VMID, a
node, a package, a version, an architecture, a plan fingerprint, a snapshot, a
probe, a contract revision, a command, an argv, a host, or a helper operation
is a 422 rather than a field quietly ignored.

Two further routes are read-only and available whether or not activation is
enabled, because an installation that has switched it off may still own a
durable job and reporting nothing about it would report a false absence:
`GET /r0/v1/resources/{resource_id}/package-update` (one job plus a bounded
event tail) and `GET /r0/v1/package-update/active` (whether ANY job owns the
global slot -- the product updater's fence witness).

`POST /r0/v1/resources/{resource_id}/package-update/resume` is production
liveness for the states that deliberately have no retry policy. It wakes the
worker, which re-reads the durable checkpoint and invokes only the existing
safe continuation semantics for whatever state that turns out to be. It never
means "submit the destructive command again", and no stage, checkpoint,
operation, or target comes from the caller.

### The worker composes; it does not decide

`app/package_update_worker.py` is one bounded worker owning one thread. It
contains no state machine, no host I/O, no transaction, and no policy. Each
cycle re-reads the one globally active job from the authority database and
runs the ONE stage that job's durable checkpoint calls for:

```text
issued | preflight_passed | snapshot_may_have_started
                              -> PackageUpdateSnapshotOrchestrator
snapshot_confirmed            -> run_package_update_execution_gate
                              -> PackageUpdateMutationOrchestrator (submitting)
mutation_may_have_started     -> PackageUpdateMutationOrchestrator (recovery)
mutation_completed | health_started
                              -> PackageUpdateHealthOrchestrator
health_completed (failed)     -> stop; ACTIVE and rollback-capable
rollback_may_have_started     -> PackageUpdateRollbackOrchestrator
```

The execution gate runs even though the mutation stage re-proves exact
material itself. That is deliberate: the gate is the cheap, entirely
non-mutating refusal that keeps a drifted plan from reaching the stage that
owns the real package command, and a successful pass changes nothing durable
about the job.

Every stage transition must strictly advance the durable checkpoint or the
cycle stops, so no checkpoint is ever attempted twice within one cycle. That
is a termination guarantee, not a retry budget.

**Wake-driven, not polled.** With nothing to do the worker blocks on an
`Event` with no timeout. Only an explicit operator action or shutdown sets it.
There is no interval, no backoff, no grace period, no attempt count, and no
threshold anywhere in the composition: one wake performs at most one attempt
of the stage the job is at, and a stop leaves the worker idle for that job
until an operator asks again. Its stop reasons are a closed set
(`PACKAGE_UPDATE_WORKER_STOP_REASONS`), and none of them names an interval, a
deadline, or a compensating action.

**Durable single-flight remains the concurrency authority.** The
`one_active_package_update_job_globally` unique index is what makes at most
one job possible; the worker's in-process cycle lock only stops one worker
running two cycles at once and is never the thing that stops two mutations.
That is also why there is one worker and no per-resource pool: a pool would be
concurrency this product has already made impossible.

**Error boundary.** An unexpected exception in one cycle is logged bounded and
redacted -- the exception TYPE and the job id, never a message, never helper
output, never a credential -- and the worker stays alive. No durable authority
fact is written, no success is synthesized, and the global slot is never
cleared merely to regain liveness.

### Restart

The order at startup is exact: `InventoryAuthority.
recover_interrupted_package_update_jobs()` first, then the worker. Authority
recovery terminalizes only the provably pre-mutation checkpoints (`issued`,
`preflight_passed`, `snapshot_confirmed`) as `interrupted`; every durable
uncertain state is left ACTIVE, fenced, and owning the global slot with its
evidence intact. The worker then re-observes whatever remains through the
stage that owns it -- `snapshot_may_have_started` reattaches through the
host's journal, `mutation_may_have_started` enters the mutation stage's
recovery-only path which can never submit, `health_started` simply evaluates
again because health is read-only, and `rollback_may_have_started`
re-observes the exact task rather than submitting a second rollback.

A restart can therefore never duplicate a destructive submission, and can
never mark a job `SUCCEEDED`: the only route to that status is a proven
passing verdict against the job's own frozen contract.

A crash immediately after a 202 is likewise truthful rather than optimistic.
The start route's acknowledgement means a durable job exists; if the process
dies before the worker continues it, startup recovery terminalizes a
still-pre-mutation job as interrupted and the operator asks again. Nothing
fabricates a success.

### Deployed privilege boundaries

Five separate root-owned forced-command helpers, five separate dedicated
keys, five separate `authorized_keys` entries:

| Boundary | Helper | Key |
| --- | --- | --- |
| snapshot | `hubinet-package-snapshot-helper.py` | `id_ed25519_snapshot` |
| execution (plan simulation) | `hubinet-package-update-helper.py` | `id_ed25519_execution` |
| mutation | `hubinet-package-mutation-helper.py` | `id_ed25519_mutation` |
| rollback | `hubinet-package-rollback-helper.py` | `id_ed25519_rollback` |
| health | `hubinet-package-health-helper.py` | `id_ed25519_health` |

The separation is the property, not the file count: the key is what selects
which forced command a connection may run, so one key reaching two helpers
would silently merge two different privileges. The configuration loader
refuses a configuration that points two boundaries at one key. The
package-scan boundary is a sixth, separate, unchanged one, and nothing here
rotates or reuses it.

Host, port, user, and the pinned `known_hosts` ARE shared, and legitimately:
they describe the one configured source's SSH endpoint, which is the same
endpoint for every boundary.

The three destructive boundaries keep root-only (`0700`) durable operation
journals under `/var/lib/hubinet-ops/` on the PVE host. Those are what make
them at-most-once across a crash.

**No PVE API privilege was broadened.** Every mutation runs host-local behind
a root-owned forced command, so the inventory API identity never needs one:
the provisioned role stays exactly `Sys.Audit,VM.Audit`, and `VM.Snapshot`
appears in no deployment script at all.

### Runtime configuration

`package_update.enabled` is the whole activation switch. False (or absent)
means no host control is built, no worker is started, and the three operator
control routes answer `503 package_update_not_activated`. True means every one
of the five dedicated credentials must be present, absolute, and readable at
startup: a missing privileged credential fails startup closed rather than
being discovered when an operator asks for a real package mutation.

The section carries execution-boundary information only -- no VMID, no
resource id, no per-guest setting, no managed-resource list -- and no timeout
knobs. Three of the stage bounds are load-bearing ceilings on how long a
bounded host round trip may hold the authority store's writer lock (see
"SQLite writer-contention policy"), and a per-installation override would let
them be widened quietly.

### The exclusive product-update maintenance fence

Replacing the backend or its privileged helpers while a package-update job
owns a snapshot, mutation, or rollback journal can pair a new backend with a
half-replaced helper set for an operation already in flight. A Hubinet PRODUCT
update and a WORKLOAD update must therefore never overlap.

**Asking is not enough.** `deploy/update-proxmox-0.5.sh` does read
`GET /r0/v1/package-update/active` during Phase U2 and refuses if a job is
already active -- but that is a courtesy, not the invariant. It stops the
operator confirming a plan that is going to be refused and stops the updater
staging artifacts it will never activate. It cannot make the two exclusive,
because between that answer and the updater's first mutation an authenticated
operator may legitimately start an update, and a second, later poll would only
move the window rather than close it: the update API stays live right up to
the service stop, and again from the moment the target service starts in Step
10 until Phase U5 acceptance is terminal.

**The fence is what makes them exclusive.** Immediately before its mutation
window -- after every check that could still refuse the run harmlessly, so a
run that declines to proceed never leaves workload updates blocked -- the
updater calls `POST /r0/v1/package-update/maintenance-fence` with its own run
id. The backend performs acquisition inside the authority store's single
`BEGIN IMMEDIATE` writer lock, the same lock `issue_package_update_job` takes:

```text
ACQUIRE (product updater)                    ISSUE (operator start_update)
  BEGIN IMMEDIATE  ---------------------------  BEGIN IMMEDIATE
    is any package-update job ACTIVE?             is the fence present?
      yes -> refuse, ROLLBACK                       yes -> refuse
    write + fsync the fence file                  insert the job row
  COMMIT                                        COMMIT
```

SQLite permits one writer, so the two critical sections are strictly ordered
and whichever enters first wins. Acquire first: the fence is durable *before*
the COMMIT that releases the lock, so no issuing transaction can have missed
it. Issue first: the job row is durable before acquisition can begin. There is
no interleaving in which both succeed, and no check-then-act gap -- the
existence check is a read performed inside the lock, never the lock itself.

The fence is one file beside the authority database
(`product-update-maintenance.fence`), and it is a file precisely so it
survives the backend process restart the product update performs: the target
backend started in Step 10 is a different process, possibly a different build,
possibly against a freshly reset authority database, and it must still refuse
workload starts while acceptance is in progress. A fence that exists but
cannot be read truthfully is treated as held, never as absent.

**Release is deliberately asymmetric.** Removing the fence only ever widens
what is permitted, so it cannot race anything into existence and needs no
atomicity. The updater does it directly on the filesystem, which also keeps
working in the one case an API release could not: a failed activation update
that has rolled back to a pre-activation backend with no fence route at all.
It happens only at a terminal point --

- a proven successful product update, after acceptance passed and the
  `completed` checkpoint is durable; or
- a proven complete rollback, after the pre-update installation is restored,
  enabled, running, and healthy; or
- the equivalent points in startup recovery for an interrupted run.

A crash anywhere before that leaves the fence in place, which is exactly what
keeps workload issuance refused while the run still owns rollback-capable
state. The holding run's marker is recovery-relevant, so it survives the
journal reload and that run's own recovery releases it; a *different* product
update is refused rather than allowed to steal it.

There is no bypass flag. An operator whose update is stuck resolves the
workload job through the product's own controls and runs the updater again.

The Phase U2 witness still distinguishes three answers, and that distinction
carries over to acquisition. `true` refuses. `false` proceeds. A real HTTP 404
means the route does not exist, so this backend predates activation, has no
update worker, and cannot own a workload job -- there is nothing to fence.
Every other failure refuses: "we could not ask" is never read as "the answer
was no".

## Job-owned snapshot safety

One update job's single pre-update PVE snapshot. Production-reachable through
the explicit operator start control and the one worker (see "Production update
activation"), and through nothing else: no scheduler, scan, approval write, or
Home Assistant poll can create a PVE snapshot. The snapshot helper is deployed
behind its OWN dedicated key and forced command, and it still needs no PVE API
mutation privilege -- it runs host-local.

```text
issued -> preflight_passed -> snapshot_may_have_started
       -> snapshot_confirmed -> execution-time plan equality -> mutation
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

**Transient observation versus durable release proof.** Because that
checkpoint is committed before the host is ever called, an ordinary
pre-flight refusal — a moved or gone guest, or a failed PVE read — would
otherwise fence the global destructive slot forever. The host journal has
three evidence classes. `absent`/`intent` are transient pre-submission
observations: they permit a NEW submission and may route the backend into its
release path, but they may not release a job. A helper launched by a backend
that then died may not have acquired its host lease yet.
`sealed_not_submitted` is the durable no-future-submit fence and the only
pre-submission release proof. `submitted`/`task_known`/`terminal` are
post-submission evidence and remain fenced or recover through the ordinary
evidence path.

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
helper, which stays scan-only. It exposes three typed operations
(`inspect_job_snapshot_state`, `ensure_pre_update_snapshot_submitted`, and
`seal_operation_never_submitted`), no delete, no rollback submission, no
arbitrary shell or argv, and it re-derives
the snapshot identity from the request's own ownership facts rather than
trusting the name it was handed. `ensure_pre_update_snapshot_submitted` is
submission-only: it never polls a PVE task to completion, so it returns the
instant the operation has crossed (or is already past) its submission
boundary. Each operation is journaled by operation identity on the PVE host,
with submission and sealing serialized by an `flock` held per VMID. From
`intent`, either seal wins and writes `sealed_not_submitted`, or submission
wins and advances through
`submitted -> task_known -> terminal`; every transition is an fsynced atomic
rename. `sealed_not_submitted` can never transition to submission.
`submitted` is the genuinely uncertain window and is **never** resubmitted.
Taskless success uses one strict bar in both ensure and inspect recovery: the
exact complete snapshot plus no relevant in-flight container lock. An
identical retry reattaches; a mismatched request is refused. Successful
operation-state responses carry the exact typed `submission_state`. Error
responses use the separate typed `error.submission` field instead, to state
whether the helper still has transient pre-submission evidence
(`not_submitted`) or whether submission must be treated as unknown
(`may_have_been_submitted`); a malformed request or a main-level failure may
carry neither. Neither channel lets the backend infer release from an error
string or canonical absence: only `submission_state=sealed_not_submitted` is
a durable release proof.

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
permit a NEW submission; every other state skips the submission critical
section. A seal is routed to the durable release path, while post-submission
states go to read-only recovery. Task polling and canonical
confirmation both happen strictly *after* the critical section releases its
writer lock, through the same read-only `inspect_job_snapshot_state`
operation, bounded by `PackageUpdateSnapshotOrchestrator`'s own retry loop —
never by holding a database transaction open across it. This is not a claim
that SQLite and PVE are proved atomic, and not a claim that a snapshot is
proved to belong to the same LXC PVE showed several minutes ago (see
"Identity" below): the claim is that Hubinet's own current authority is held
stable through the submission boundary, and the host independently
re-validates the live PVE target immediately before it ever submits.

**SQLite writer-contention policy.** `execute_snapshot_submission_if_current`
and `resolve_pre_submission_block` deliberately hold the authority store's
`BEGIN IMMEDIATE` writer lock across one bounded SSH host round trip each —
that serialization is load-bearing (see above and "the pre-submission block
critical section" below) and must remain. Holding that lock across a host
round trip means any other writer (discovery, a package scan, approval)
waiting on the same lock must be willing to wait at least as long as that
round trip can legitimately run, or it fails with `database is locked` for no
reason of its own. `app/inventory/contention_policy.py` is the single source
of truth for that relationship instead of an unrelated fixed timeout:

- `MAX_SNAPSHOT_HOST_TIMEOUT_SECONDS = 90` is the deliberate upper bound
  `SshPackageUpdateSnapshotHostControl` accepts for `timeout_seconds`,
  enforced in its constructor before any SSH or subprocess execution. It
  replaces the historical, unrelated 3600s ceiling: both snapshot mutations
  are submission/seal-only and never poll a PVE task to completion (see
  above and `deploy/hubinet-package-snapshot-helper.py`), so this bounds one
  `pvesh` trigger plus a durable journal write, not PVE's own asynchronous
  task. The canonical 60s test timeout is ordinary evidence of a healthy
  round trip, not this ceiling.
- `BOUNDED_PROCESS_CLEANUP_SECONDS = 5` is the bounded process runner's own
  `Popen.wait` reap allowance after it kills a timed-out subprocess
  (`app.package_scan_host_control._bounded_process_runner`, reused by the
  snapshot transport) — real wall-clock time the critical section spends
  before returning, not free slack.
- `MAX_SNAPSHOT_HOST_CRITICAL_SECTION_SECONDS = 95` is their sum: the
  worst-case duration one snapshot critical section may hold the writer
  lock.
- `WRITER_SCHEDULING_MARGIN_SECONDS = 10` is an explicit margin on top of
  that worst case for scheduling jitter and SQLite's own lock handoff.
- `AUTHORITY_WRITER_WAIT_BUDGET_SECONDS = 105` (`_MS = 105_000`) is what
  `InventoryAuthorityStore` now sizes both `PRAGMA busy_timeout` and
  `sqlite3.connect(timeout=...)` from, for every connection, replacing the
  previous fixed `BUSY_TIMEOUT_MS = 5000`.

Crash-safe package mutation adds a second pair of such critical sections
(`execute_package_mutation_submission_if_current` and
`resolve_pre_mutation_block`), with the same shape and the same reason, so
the module also defines `MAX_PACKAGE_MUTATION_SUBMISSION_TIMEOUT_SECONDS`
(90s, enforced in `SshPackageUpdateMutationHostControl`'s constructor before
any SSH or subprocess execution) and derives
`MAX_PACKAGE_MUTATION_CRITICAL_SECTION_SECONDS` (95s) from it. The writer
budget is sized from `MAX_HOST_CRITICAL_SECTION_SECONDS`, the worst case
over *every* such critical section, so adding one can never silently leave
ordinary writers with a budget shorter than a healthy one of them. Neither
mutation critical section ever waits for `apt-get`: the host journals
`submitted` and hands the real package command to a detached runner, and the
transport's own read-only operations (preparation and inspection), which may
legitimately take minutes, run strictly outside the writer lock under a
separate, longer timeout.

Module-level assertions in `contention_policy.py` enforce
`AUTHORITY_WRITER_WAIT_BUDGET_SECONDS > MAX_SNAPSHOT_HOST_CRITICAL_SECTION_SECONDS`
and the same relationship for the package-mutation critical section at
import time, and each host-control constructor's timeout ceiling makes a
legal critical section longer than that impossible to construct — so it is
no longer possible to configure a snapshot host round trip long enough to
legitimately exhaust the store's writer wait budget. This guarantees exactly
one thing: one healthy bounded snapshot critical section cannot, by itself,
cause an ordinary concurrent writer to fail merely because it waited less
than a valid snapshot host round trip could legitimately take. It does not
guarantee fairness across arbitrarily many queued writers, freedom from
starvation under continuous write load, or recovery from a permanently
wedged transaction; a writer still blocked past the budget still fails with
`database is locked`, deliberately — this is bounded waiting, not infinite
retry. This is NOT permission to lengthen or hold any polling transaction
open — task polling and canonical confirmation stay strictly outside both
writer critical sections (see above), and this policy does not change that.
Production snapshot execution is still not reachable (see below), so no
production writer can yet actually contend with either critical section;
this closes the policy gap regardless, before that activation.

**Liveness after a refusal, and the pre-submission block critical section.**
A stale authority context always refuses a NEW submission —
`execute_snapshot_submission_if_current` never authorizes one on stale
authority, full stop. But if the underlying resource or source is gone or
replaced for good, every future retry would repeat that identical refusal
forever, permanently occupying the one global destructive slot with a job
that can never advance. The refusal alone therefore does not decide the
job's fate, and neither does a single fresh read taken outside any lock:
that would only narrow the very race it exists to close, since another
invocation's authorized submission could still cross the door in the gap
between such a read and a later, separately-committed block.

So a job is only ever released as unsubmitted from inside
`InventoryAuthority.resolve_pre_submission_block` — the mirror image of
`execute_snapshot_submission_if_current`, serialized against it through the
SAME authority-store writer lock. It invokes ONE bounded host seal while its
transaction owns that lock and terminalizes in the SAME transaction only when
the host durably returns `sealed_not_submitted`. Current package-update
authority is deliberately not required; the job must still be active at
`snapshot_may_have_started`, with no observed task and no confirmation.
Anything other than the seal — post-submission state, lease contention, an
older helper, or a lost/malformed response — remains fenced and follows the
ordinary evidence path. The seal performs no PVE reads, so a moved or deleted
guest does not need to exist on the frozen node for liveness.

**Two serialization layers, not one.** The SQLite writer lock serializes
Hubinet's submission and release transactions. The host's SAME non-blocking
per-VMID `VmidMutationLock` serializes submission, inspection, and sealing
across independent helper processes. If submit takes the host lease first, it
durably reaches at least `submitted` before `pvesh create`, and the later seal
refuses. If seal takes the lease first, it durably writes
`sealed_not_submitted`; a helper launched earlier but delayed before its own
lease acquisition reads that phase when it finally starts and must never
submit. `operation_in_progress` stays UNCERTAIN. Each inspection releases the
host lease before returning, and task polling holds neither the host lease nor
the SQLite writer lock across a wait. This serialization claims no historical
LXC incarnation identity; the backend retains `resource_id`/continuity
authority while the helper validates only current PVE facts.

**Successful snapshot with stale authority.** Confirmation still requires
current authority because confirmation grants rollback authority. If a fresh
canonical listing independently proves the exact complete same-job snapshot
but current package/resource/source authority is stale, a separate authority
transaction retains all snapshot/task evidence and terminalizes the job as
`blocked` without setting `snapshot_confirmed_at` or advancing the checkpoint.
The global slot is released, but rollback selection refuses the terminal,
unconfirmed job. If authority is current again when that resolver runs, it
does not terminalize and normal confirmation may proceed.

**Same-job rollback.** A job may roll back only to the snapshot that exact job
created and confirmed. `select_package_update_rollback_target` re-proves the
name, the full structured ownership metadata, `resource_id`, the continuity
revision, that the entry is a real snapshot rather than the `current`
pseudo-entry, and that canonical state is unambiguous. There is no
caller-supplied snapshot name, no "latest Hubinet snapshot", and no fallback
to another job's snapshot; a reused VMID never transfers rollback authority to
a different incarnation. Rollback *execution* is built on exactly this
contract — see "Same-job rollback execution" below.

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

`view_health_contract`, `set_health_contract`, and `clear_health_contract` use
that same resource-device selector rather than a second selection model. All
three return response data; contract material — the probe list — is response
data only, never entity attributes. A second concise resource sensor displays
the backend-published `unsupported | unconfigured | configured` contract state,
which is a statement about configuration and never a health result: no health
result exists to publish.

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
- **Exact plan fingerprint.** Successful scans sort the material quadruples
  `(package name, architecture, installed version, candidate version)` and
  hash canonical JSON with SHA-256. Architecture is material identity, not
  optional metadata (see "Binary package identity" below); origin,
  description, security, and reboot-required cannot change the fingerprint.
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

Healthchecks, lifecycle mutation, and QEMU package execution remain future
work; job-owned snapshot safety, the execution-time plan equality gate,
crash-safe package mutation, and same-job rollback execution below all exist
internally but cannot be invoked by production.

## Binary package identity

The durable identity of one installed Debian/Ubuntu binary package is
**`(package_name, architecture)`**, never `package_name` alone. dpkg's own
multiarch model keys every installed package by this exact pair (see
`dpkg-query(1)`: "The package name will be architecture qualified for
packages with a Multi-Arch field with the value same or with a foreign
architecture..."), and `foo:amd64`/`foo:i386` are two fully independent
installed packages, never one row that can collapse or overwrite the other.

**Architecture is proven from dpkg's own installed state, never inferred
from APT's candidate description alone.** An earlier revision of this stage
took the trailing architecture bracket in APT's `-s upgrade` output (see
below) as sufficient proof of the *installed* package's architecture. That
is not sound: that bracket describes the **candidate** version specifically
(`RelStr()` is called on the candidate `VerIterator`), and a version's own
`MultiArch::All` flag -- which controls whether the bracket reads `all` --
is a property of that one version, not of the package's underlying cache
slot; APT's `Architecture: all` version generation can attach such a version
to a Package object whose own architecture is a real triplet. dpkg's status
database can independently and correctly record a currently installed
package's `Architecture` field as literally `all` (confirmed live: 508 such
packages on this repository's own devbox), while APT's cache internals
consider that Package to occupy the *native* architecture's slot. The two
views can legitimately diverge for one version's own reported architecture
without the installed package's *identity* having changed at all -- and,
separately, an outright cross-architecture transition between versions is a
real (if rare) possibility this stage does not need to support. Guessing the
installed architecture from the candidate bracket alone can therefore be
wrong in exactly the cases that matter for identity.

So the installed architecture is instead read independently from the guest's
own dpkg status database and cross-checked, never guessed:

- `dpkg-query -W -f='${Package}\t${Architecture}\t${Version}\t${db:Status-Status}\n'`
  (no package-name arguments -- lists everything; a fixed, non-caller-
  controlled command) is the guest's complete installed-package inventory:
  bare `Package` name, its own `Architecture` field, `Version`, and
  `db:Status-Status` (dpkg's literal status word -- see `dpkg-query(1)`),
  filtered to rows dpkg itself reports as `installed`. `dpkg --print-
  architecture` (also fixed, no arguments) gives the guest's native
  architecture.
- For each `Inst` line, the canonical parser
  (`app/package_scan.py::parse_apt_simulation`) resolves exactly one
  installed `(name, architecture)` row from that independent inventory:
  APT's own `:<arch>` name qualifier (present only for a foreign
  architecture, per `pkgCache::PkgIterator::FullName()`) pins it directly;
  a bare name tries the native architecture and `all` and requires
  *exactly one* to match dpkg's inventory. Zero or two matches -- an
  unrecorded package, or a genuine ambiguity -- fails closed rather than
  guessing.
- The resolved installed architecture must then agree with APT's own
  candidate-description bracket (from `pkgCache::VerIterator::RelStr()`,
  which unconditionally appends `` [<Arch()>]`` -- traced in
  `apt-pkg/pkgcache.cc`, current upstream `apt-team/apt`, and independently
  confirmed live against this devbox's own `apt-get -s upgrade` output,
  including a real `[all]` package among native `[amd64]` ones). This is the
  scope boundary, not an identity source: for an ordinary,
  approvable upgrade, **installed architecture must equal candidate
  architecture**. A package changing between an architecture-specific
  binary and `Architecture: all` -- in either direction -- is a
  cross-architecture transition and is out of this stage's supported scope;
  it fails closed (`PackageScanParseError`) rather than being silently
  relabeled from whichever side happened to be read.
- dpkg's own installed version for that resolved `(name, architecture)` must
  also agree exactly with APT's own displayed installed-version bracket.
  Reading dpkg's inventory as close as possible to the APT simulation (the
  scan and execution helpers both read it immediately *after* the
  simulation call) bounds the ordinary concurrent-package-manager race
  between the two reads; any disagreement -- from that race, or from
  anything else -- fails closed rather than trusting either source alone.

The canonical parser is shared verbatim by scanning and the execution gate
below -- never two independent implementations -- and fails closed
(`PackageScanParseError`) on a missing, malformed, or contradictory
architecture, a missing or ambiguous installed identity, an APT/dpkg
installed-version disagreement, a duplicate `(name, architecture)` row, or
any removal/new-install line (including explicit `Remv` and `Purg` actions) --
the scanner's existing scope stays an
ordinary upgrade plan only (see "Package scanning for LXC" above); it is not
broadened to dist-upgrade, autoremove, install, or remove semantics by this.

**Every `Conf` (configure) action must be bound to an approved `Inst` row.**
`pkgSimulate::RealConfigure` (`apt-pkg/algorithms.cc`) prints one `Conf`
line per package APT would configure, in the exact same candidate-
description shape an `Inst` line's parenthesized tail uses (traced
authoritatively, not guessed from one fixture). A standalone `Conf` -- one
whose exact `(name, candidate_version)` label does not match an approved
`Inst` row in the very same simulation -- means a real future upgrade would
configure a package this plan never approved, which would silently violate
`PRODUCT.md` rule 2; final binding requires the same raw package identity,
candidate version, and candidate/proven architecture. A `Conf` that
contradicts an `Inst` row's version or architecture fails closed, as does a
duplicate `Conf` for the same
action, and the distinct `"Conf <name> broken"` shape APT prints for an
already-broken configure (which also registers an internal APT error).
Separately, APT's summary printer (`apt-private/private-output.cc::Stats()`)
appends an unconditional extra line, `"N not fully installed or removed."`,
whenever dpkg reports a nonzero broken/unfinished-package count
(`pkgDepCache::BadCount()`) -- pre-existing unfinished dpkg state left over
from something else entirely, never attributable to this plan's own
approved rows. Seeing that line at all (its count is only ever printed when
positive) fails the plan closed rather than being silently dropped.

**What is material and what is not.** The material identity/change tuple is
`(package_name, architecture, installed_version, candidate_version)`.
`libfoo`/`amd64` and `libfoo`/`i386` are two different binary packages in
every durable row, the plan fingerprint, approval, and the job's frozen
package rows. Origin, description, security classification, and
reboot-required remain non-material presentation metadata, exactly as
before. Schema v11 (see "Backend" above) makes `architecture` an explicit,
required, validated column on `package_scan_packages` and
`package_update_job_packages`, with `UNIQUE(..., package_name, architecture)`
replacing the old name-only uniqueness -- there is no dual-read compatibility
mode and no "unknown architecture but still approvable" plan: if
architecture cannot be established, scanning fails closed instead of
collapsing distinct packages into one identity.

## Execution-time plan equality

This is the missing proof between a package-update job's confirmed pre-update
snapshot and (future, unimplemented) package mutation:

```text
snapshot_confirmed
  -> fresh execution-time APT metadata refresh + simulation (host I/O,
     outside any authority-store transaction)
  -> canonical material plan (the SAME parser package scanning uses)
  -> one short authority-store writer transaction:
       atomically re-read the durable job
       re-prove current job/source/resource/approval authority
       compare the fresh material set against the job's IMMUTABLE
         copied package rows -- complete-set equality, never
         subset/superset/name-only matching
  -> MATCHED: typed result only; the job is untouched (still ACTIVE at
       snapshot_confirmed; no checkpoint advance; no new persisted flag)
  -> TEMPORARILY_UNAVAILABLE: a latest scan is RUNNING, so authority is
       undecided; job and snapshot untouched, no host call when seen pre-host
  -> MISMATCHED: the job is terminalized `blocked` in the same transaction --
       snapshot retained, global slot released, no rollback authority
```

`app/package_update_execution.py` is the dark orchestrator
(`run_package_update_execution_gate`), `app/package_update_execution_host_control.py`
is its purpose-specific pinned-key SSH transport, and
`deploy/hubinet-package-update-helper.py` is a separate dark forced-command
PVE boundary exposing exactly one typed, non-mutating operation
(`simulate_exact_update_plan`): a fixed metadata refresh
(`apt-get update -qq --error-on=any`), a fixed simulation
(`apt-get -s upgrade`), fixed OS/APT inspection (`cat /etc/os-release`,
`apt-get --version`), and the two fixed, read-only dpkg identity commands
described above (`dpkg --print-architecture`, `dpkg-query -W -f='...'`),
against the job's own frozen expected VMID/node, re-validated live before
each guest command -- the same non-mutating contract `PRODUCT.md`, "What
package scanning may do" already allows. It is a separate file and a
separate logical privilege boundary from the deployed scan helper and from
the snapshot helper, so this stage cannot accidentally make job execution
production-reachable by extending an already-deployed boundary.

`InventoryAuthority.evaluate_package_update_execution_plan` is the equality
transition. It requires the job ACTIVE at exactly `snapshot_confirmed`
(never overwriting or reopening a job that went terminal for some other
reason, or one that has not reached this checkpoint yet), re-proves current
authority with the same `_package_update_job_authority_is_current` predicate
every other package-update transition uses, and holds the authority store's
one writer lock only across the in-memory comparison -- never across the
host round trip, which the orchestrator always performs first, outside any
transaction (see "SQLite writer-contention policy" above; this closes the
same class of gap that policy already closed for the snapshot critical
sections, before a second writer could ever actually contend with them).

**A provably stale current-authority context at this gate is released, not
left dangling.** Every other package-update transition that finds current
authority stale simply refuses (`AuthorityConflict`) and leaves the job
exactly as it was -- correct for checkpoints a job can still legitimately
reach again. This gate is different: it is the last checkpoint before
(future) package mutation, and a job sitting there is the *only* thing
occupying the one global destructive slot. Leaving a job whose frozen
approval context can never become current again (a rotated transport trust
revision, a replaced resource, ...) permanently ACTIVE at this checkpoint
would starve every future package-update job forever, with a backend restart
as the only way out -- and a restart must never be the ordinary release
mechanism (see "Job-owned snapshot safety" above; the same principle applied
one checkpoint later). So both the gate's own cheap pre-host check
(`InventoryAuthority.revalidate_or_release_stale_package_update_execution`,
which lets the orchestrator skip the host round trip entirely for a job
already known stale) and the post-host equality transition re-prove current
authority and, if it is stale, atomically terminalize the job `blocked` in
that SAME transaction (`_terminalize_execution_gate_job_if_authority_stale`)
-- the proof and the release can never be split across two transactions,
which would reopen exactly the check-then-commit race the rest of this
stage is built to close. The confirmed snapshot is retained,
`mutation_may_have_started_at` stays NULL, and the job never gains rollback
authority; the operator must obtain and approve a fresh plan. This is a
deliberately conservative policy -- current authority for this exact frozen
material could in principle become available again later -- chosen because
global-slot liveness matters more than preserving one old pre-mutation job
through authority drift, and issuing a fresh plan/job is always available.
A job that goes terminal for some *other* reason (an ordinary startup
interruption, say) while a host round trip is in flight is never swept into
this path: the checkpoint/status guard both entry points share raises an
ordinary `AuthorityConflict` instead, and the job's actual terminal reason
is never overwritten.

**"Stale" means every decided way current authority can move past a frozen
job, not only a moved resource or source.** The shared underlying proof
(`_package_update_job_current_authority_detail`) classifies four ways:
**current** (everything still matches); **temporarily unavailable** (the
newest scan is RUNNING, so no new exact plan or failure exists yet and the
job remains ACTIVE for retry); **stale** -- the resource or source context
drifted (a rotated transport trust revision, a replaced resource, ...), *or*
the current world has decisively moved past the approved plan itself (the
latest scan completed unsuccessfully, which per `PRODUCT.md` means unknown,
not zero; or it completed successfully but its context, fingerprint, or exact
material changed); and **hard failure** -- the job already terminal, an unsupported frozen
resource type, or a stored fingerprint that no longer matches its own
recomputation (structurally unreachable under the schema's own immutability
triggers, but never silently reclassified if it ever were). Every "stale"
case releases the job identically; only "hard failure" propagates as an
exception instead, exactly as it always has. This one predicate backs two
call sites with different needs: `_package_update_job_authority_is_current`
(the pre-existing bool-returning form every other package-update transition
still uses, completely unchanged -- `False` for context drift, an
`AuthorityConflict` raise for plan drift, preserving each of their exact
prior contracts) and `_terminalize_execution_gate_job_if_authority_stale`
(which this gate uses instead, releasing every stale case while returning a
narrow retryable result for temporary unavailability). Generic callers retain
their prior bool/`AuthorityConflict` behavior.

A `MATCHED` result is deliberately not a durable mutation permit: it changes
nothing about the job besides an append-only diagnostic event, and the
package-mutation stage does NOT trust an earlier pass from possibly minutes
ago -- it re-runs this exact equality proof itself, against material it
obtained moments earlier, in the same transaction that commits its
write-ahead uncertainty boundary (see "Crash-safe package mutation" below).
That is exactly the TOCTOU discipline `PRODUCT.md` rule 2 requires. This
read-only gate remains useful on its own for diagnostics and tests. A crash or restart at any
point in this gate is safe by construction: because it never performs
package mutation, "no package mutation may be assumed" (see "Job-owned
snapshot safety" above) remains true, and ordinary startup recovery already
safely interrupts a job sitting at `snapshot_confirmed` (including one this
gate already matched or released -- neither creates a new state startup
recovery does not already know how to handle).

Nothing on the production HTTP, Home Assistant, scheduler, bootstrap, or
updater path can reach any of this; `tests/test_r0_architecture_regression.py`
proves it, alongside the equivalent proof for job-owned snapshot safety.

## Crash-safe package mutation

This is the product's one real workload package mutation, and the only place
in the repository that can change a package inside a managed guest:

```text
ACTIVE @ snapshot_confirmed
  -> host PREPARE (read-only: APT metadata refresh, `apt-get -s upgrade`,
     the two fixed dpkg identity reads; returns a digest of the exact
     evidence it produced and writes NO durable host state)
  -> canonical material (the SAME parser package scanning uses)
  -> ONE authority-store writer transaction:
       re-prove ACTIVE @ snapshot_confirmed
       re-prove current job/source/resource/approval authority
       exact complete-set equality against the job's IMMUTABLE frozen rows
       COMMIT checkpoint = mutation_may_have_started
              + mutation_operation_id
              + mutation_may_have_started_at
              + accepted_prepared_evidence_digest     (one indivisible fact)
       -> ARMED_NOW (may submit) | ALREADY_ARMED (recovery only)
  -> short submission critical section: re-prove current authority AND that
     this caller carries the accepted digest, then, while still holding the
     writer lock, ask the host to EXECUTE
  -> host journals `intent`, binding that accepted digest as this
     operation's immutable evidence from its first durable byte
  -> host stages this operation's pre-dpkg action gate into the guest
  -> host journals `submitted` (fsynced) BEFORE launching anything, hands the
     real package command to a detached runner, and returns
  -> runner revalidates the live target, then runs the one real command,
     whose own APT invocation must pass the action gate before dpkg
  -> read-only polling, strictly outside every transaction
  -> terminal host evidence
  -> independent dpkg completion proof
  -> mutation_completed  (the job stays ACTIVE; this is not job success)
```

**The one real package command.** Fixed argv, no shell, no caller-supplied
option, package name, version, or command text:

```text
env LC_ALL=C DEBIAN_FRONTEND=noninteractive
  apt-get -y
    -o APT::Get::Upgrade-Allow-New=false  -o APT::Get::Remove=false
    -o APT::Get::Force-Yes=false          -o APT::Get::allow-downgrades=false
    -o APT::Get::allow-remove-essential=false
    -o APT::Get::allow-change-held-packages=false
    -o APT::Get::AllowUnauthenticated=false
    -o APT::Ignore-Hold=false
    -o Dpkg::Options::=--force-confdef    -o Dpkg::Options::=--force-confold
    -o DPkg::Pre-Install-Pkgs::=/run/hubinet-ops/package-mutation/<op>/verify-action-set
    -o DPkg::Tools::Options::/run/hubinet-ops/package-mutation/<op>/verify-action-set::Version=3
    upgrade
```

`<op>` is the job's own canonical `mutation_operation_id` UUID and is the
only interpolated value; everything else is a literal. The approved material
never appears in it. It travels to the host only so the
host can *refuse*, so there is structurally no value a package name or
version could take that changes what runs.

**Why the real command cannot exceed the approved plan.** Read from current
upstream `apt-team/apt`, not inherited or assumed. `apt-get upgrade` is
`DoUpgrade` -> `DoUpgradeNoNewPackages` ->
`APT::Upgrade::Upgrade(FORBID_REMOVE_PACKAGES|FORBID_INSTALL_NEW_PACKAGES)` ->
`pkgAllUpgradeNoNewPackages` (`apt-private/private-upgrade.cc`,
`apt-pkg/upgrade.cc`). That resolver marks install ONLY for packages with
`I->CurrentVer != 0 && Cache[I].InstallVer != 0`, with `AutoInst=false` so it
never pulls in a new dependency, and resolves everything left over by *keep*
(`ResolveByKeepInternal`). Installing a new package and removing an existing
one are structurally impossible, not merely discouraged. `apt-get -s upgrade`
-- the simulation the plan is proved from -- takes the identical path: `-s`
only replaces the final package manager with `pkgSimulate` at the end of
`InstallPackages` (`apt-private/private-install.cc`), after the resolver has
already run, so the simulation is the real plan for the same cache inputs.
`-y` does not change the resolver either; it replaces the confirmation prompt
and turns an essential removal, a downgrade, or a held-package change into a
hard pre-mutation error. The explicit `-o` options are belt-and-braces
against a guest `apt.conf.d` snippet flipping a default in the dangerous
direction (`Upgrade-Allow-New` would let `upgrade` install new packages;
`Ignore-Hold` would let it change held ones); command-line `-o` wins over
configuration files.

**The pre-dpkg action gate.** Everything above bounds what the *resolver*
may choose. It does not bound what the resolver chooses *from*. APT locking
does not span two separate `apt-get` invocations, so between PREPARE's
simulation and the real upgrade an ordinary actor can complete an `apt-get
update`, release a hold, add a source, or change a pin, and the real
resolver can then legitimately pick a DIFFERENT action set while every
installed version still matches the approved plan. The post-state proof
would notice afterwards -- but the unapproved package would already be
installed. So the real invocation's own resolved action stream is made the
thing that must equal the authority-accepted material, before dpkg is
reached.

The mechanism is a `DPkg::Pre-Install-Pkgs` hook running APT's protocol
**Version 3**. Both properties it depends on are upstream behaviour
(`apt-pkg/deb/dpkgpm.cc`), and both were re-verified against a real `apt`
in an isolated APT root with a fake `dpkg`:

- `RunScriptsWithPkgs("DPkg::Pre-Install-Pkgs")` runs once per
  `pkgDPkgPM::Go()`, ahead of the loop that invokes dpkg, and `SendPkgsInfo`
  passes the COMPLETE action list for the whole transaction -- not one batch.
  Observed: one hook invocation covering every action, followed by two
  separate dpkg calls (`--unpack`, then `--configure --pending`).
- a hook exiting non-zero makes APT abort. Observed: with the hook exiting
  1, dpkg's package-operation count was **zero**.

A Version 3 action record is nine whitespace-separated fields:

```text
<name> <old ver> <old arch> <old MA> <dir> <new ver> <new arch> <new MA> <action>
```

`<dir>` is `<`, `>`, or `=`; `<action>` is `**CONFIGURE**`, `**REMOVE**`, or
the `.deb` path being unpacked; absent versions and their architectures are
`-`. An upgraded binary therefore contributes exactly two records, an unpack
and a configure. The name carries **no** architecture qualifier even for a
foreign-architecture package, so the architecture fields are the only thing
separating `foo:amd64` from `foo:i386` -- and both are bound exactly. They
can be bound to one value because the canonical simulation parser already
refuses any row whose candidate architecture differs from its proven
installed architecture, so no approved row can ever need two.

Three fields are canonicalized rather than bound, for stated reasons:

- the two MultiArch-type fields become `-`. APT reports the type *of the
  version being acted on*, and it legitimately differs between a package's
  installed and candidate versions -- observed in real APT as
  `becomesall 2.0 amd64 none < 2.1 all foreign`. PREPARE cannot learn the
  candidate's type from the simulation, so binding it would fail-close on
  legal upgrades while adding no precision: the binary identity dpkg acts on
  is already pinned by name, version, and architecture. The gate still
  requires each field to be one of APT's four documented type words.
- the `.deb` path becomes the class token `UNPACK`. Where APT cached the
  archive is not part of the approved transition, and the archive's contents
  are already pinned by name, version, and architecture. An action that is
  neither `**CONFIGURE**`, `**REMOVE**`, nor an absolute `.deb` path is
  refused rather than assumed to be an unpack, so a future action word
  cannot be silently absorbed.

Comparison is exact multiset equality: both sides are sorted inside the
guest under one collation, so a different but equivalent dependency ordering
stays legal while any extra, missing, changed, downgraded, removed, newly
installed, or wrong-architecture action refuses.

**Protocol downgrade fails closed.** APT sends its highest supported version
when an unsupported one is requested, and a hook command it cannot key an
option to simply gets Version 1. Both were observed. The gate therefore
requires the literal first line `VERSION 3` and refuses anything else. This
is also why the hook command must stay a bare path: APT keys
`DPkg::Tools::Options::<cmd>::Version` on the exact command string, and a
command containing a space does not resolve its own option -- observed
falling back to Version 1.

**The gate cannot be configured away.** Command-line `-o` is applied after
every configuration file, so an ordinary guest `apt.conf`/`apt.conf.d`
snippet can neither `#clear DPkg::Pre-Install-Pkgs` the hook out of the list
nor pin it back to an older protocol; both were tested against real APT and
the hook still ran at Version 3. The guest's own legitimate hooks
(`apt-listchanges`, `dpkg-preconfigure`) are left alone and simply run
alongside it -- ordering does not matter, because *any* hook failing aborts
APT before dpkg.

**Runtime and staging.** The gate is `/bin/sh` plus `sort`, `tail`, and
`printf`. `dash` and `coreutils` are both `Essential: yes` under Debian
Policy, so this adds no prerequisite to the supported guest contract and
needs no Python, Perl, or awk inside the guest. It reads the stream from
stdin -- APT's default `InfoFD` -- deliberately avoiding
`<&$APT_HOOK_INFO_FD`, which is a syntax error in `dash`.

The verifier and a canonical manifest of the approved action set are staged
into `/run/hubinet-ops/package-mutation/<op>/` (tmpfs, mode `0700`, verifier
`0500`) by fixed argv shapes whose only interpolated value is the canonical
operation UUID. **The manifest travels as payload bytes on the command's
stdin, never as command text, argv, or a shell fragment**, and package
names, versions, and architectures are re-validated against a strict
grammar first, so a value containing whitespace or a newline is refused
before anything is staged rather than forging an extra approved action.
Staging happens while the journal is still at `intent`, so a staging failure
is an ordinary pre-submission refusal that remains sealable, and the staged
bytes are read back and digest-compared before submission, which binds the
gate to *this* operation rather than to whatever sits at the path.

Stale artifacts cannot authorize anything. The whole Hubinet staging root is
removed and recreated under the guest's per-VMID lease before every
submission; the manifest header and the verifier's own literal each name an
operation id and must agree; and `/run` is tmpfs, so nothing survives a guest
reboot. There is no garbage collector, and none is needed.

**Why this is not an arbitrary command string across a privileged
boundary.** APT natively invokes a `DPkg::Pre-Install-Pkgs` command through a
shell. The command here is one fixed, code-owned bare path containing no
metacharacter, no argument, and no expansion, and no request-provided text
reaches it -- there is no value a package name or version could take that
changes what executes. The approved material is data in a staged file, used
only to refuse.

**Both protections stay.** The pre-dpkg gate and the independent dpkg
post-state completion proof do different jobs and neither replaces the
other: the gate PREVENTS unapproved material reaching dpkg, the post-state
proof PROVES the exact approved transition actually completed. dpkg's own
`--configure --pending` runs after the gate, which is precisely why an
independent post-state reading is still required.

**Why it cannot hang.** dpkg prompts about a conffile only when it was BOTH
modified locally and changed by the package (`conffoptcells`,
`src/main/configure.c`), and on end-of-file at that prompt it does **not**
fall back to a default -- it calls `ohshit("end of file on <standard input>
at conffile prompt")` and aborts mid-transaction. The helper's stdin is
`/dev/null`, so a deterministic conffile policy is mandatory rather than
cosmetic. `--force-confdef --force-confold` resolves every such case without
a prompt and **keeps the operator's file**, leaving the distributor's version
as `<file>.dpkg-dist`. The product consequence is explicit and deliberate: a
configuration change shipped by a package is never silently applied over a
locally modified conffile, and the operator is left to merge it. That is the
right default for a rollback-capable updater -- the guest's behaviour stays
as close to pre-update as the package code allows. `DEBIAN_FRONTEND=noninteractive`
puts debconf in its non-interactive frontend, and `needrestart` reads that
same variable to disable its own interactive service-restart prompt. Every
command additionally runs with no controlling terminal and a bounded
wall-clock timeout.

**The simulation-to-mutation race.** A simulation and a real run are separate
operations, and `apt-get -s` deliberately disables locking, so the window is
real and is closed by four independent mechanisms rather than waved away:

1. The proof and the command live in **one host operation pair with no
   metadata refresh between them**. Preparation performs the last refresh;
   the real command resolves against exactly the on-disk index state that
   simulation was computed from.
2. The host takes a **fresh dpkg reading immediately before launching**, under
   the same per-VMID lease, and refuses unless every approved
   `(package_name, architecture)` is still installed at exactly its approved
   pre-update version and dpkg is in no unfinished state.
3. APT's own locking: a concurrent real `apt`/`dpkg` makes the command fail
   **before** mutating anything, never part-way into a different plan.
4. Anything that still slips through is **detected, never silent**, by the
   completion proof below, and the job stays fenced with its rollback
   authority instead of being called complete.

**The write-ahead uncertainty boundary.** `mutation_may_have_started` is
committed durably *before* any real package command can be sent. Once a job
reaches it, nothing may ever infer that no workload package changed. It is
deliberately not "mark safe, mutate later": the exact equality proof and the
checkpoint commit in the SAME transaction
(`InventoryAuthority.arm_package_update_mutation`), because proving in one
transaction and committing in another is the check-then-commit race the rest
of this stage exists to close. A mismatch, or a provably stale current
authority, terminalizes the job `blocked` in that same transaction --
snapshot retained, global slot released, no rollback authority, exactly like
the execution-time plan gate. A newest RUNNING package scan is retryable and
writes nothing.

**The submission critical section.** The checkpoint alone leaves the same
narrower race the snapshot stage has: another writer can invalidate this
job's resource incarnation after authority was proved and before the host is
asked to mutate. So `execute_package_mutation_submission_if_current`
re-proves the whole current-authority context and calls the host's
submission-only operation while it still holds the authority store's one
writer lock. That call never waits for `apt-get` -- the host journals
`submitted` and detaches -- so the lock is held for one bounded round trip.
A stale context refuses BEFORE the callback runs, raising the narrow
`MutationSubmissionRefusedBeforeCallback`, which is routed to the durable
seal so the global slot is released rather than fenced forever.

**Only the invocation that proved it may submit.** A real package mutation is
submitted only by the same invocation that just prepared the fresh evidence
and armed the job with it. Every later invocation -- a retry, a restart
recovery, a concurrent racer -- can observe, seal, or complete, never submit.
That makes "no blind resubmission after a crash, timeout, or lost response"
structural rather than a judgment call at each recovery branch, and it is
enforced twice: in the orchestrator, and by the host, which refuses to
execute unless the caller presents the exact evidence digest its own journal
recorded.

**Host-side durable journal and at-most-once execution.**
`deploy/hubinet-package-mutation-helper.py` is a separate file and a
deliberately stronger logical privilege boundary than the scan, snapshot, and
execution-plan helpers, none of which gained any mutation capability. It
exposes four typed operations (`prepare_exact_package_mutation`,
`execute_exact_package_mutation`, `seal_mutation_never_submitted`,
`inspect_package_mutation_state`), no delete, no rollback, no snapshot, no
arbitrary shell or argv. Each operation is journaled by the job's
deterministic `mutation_operation_id` on the PVE host, with fsynced atomic
renames, and preparation, submission, and sealing are serialized by a
non-blocking per-VMID `flock`:

```text
absent -> intent -> sealed_not_submitted        (durably never submitted)
                 -> submitted -> terminal_success | terminal_failure
```

The journal binds a request fingerprint over the VMID, node, operation id,
plan fingerprint, and the full backend/job/resource/binding/continuity
context, so a request differing in any of them is a different request and is
refused rather than allowed to reuse the operation. `submitted` is written
and fsynced BEFORE the command is launched and is never resubmitted from --
it is the genuinely uncertain window. `sealed_not_submitted` is the durable
no-future-submit fence and the ONLY evidence that may release a job past the
write-ahead checkpoint; `absent`/`intent` are transient routing evidence that
may send the backend into the release path but may never release a job, since
a helper launched by a dead backend may not have taken its lease yet.

**Preparation writes nothing durable, and that is a safety property, not a
shortcut.** PREPARE runs strictly BEFORE the write-ahead arming transaction,
so a journal record written there would be mutation-operation state for an
operation that may never be armed -- and, being immutable once written,
would turn every ordinary pre-arm transient (a newer package scan still
RUNNING, a lost PREPARE response, a backend that died before arming) into an
operation identity that could not be prepared again until a backend restart
interrupted the job. `AUTHORITY_TEMPORARILY_UNAVAILABLE` advertises "retry
later", so retrying later has to work. The journal's first record is
therefore written by `execute_exact_package_mutation`, the only path that may
submit, from the digest the arming transaction accepted; `intent` means
exactly "an already-armed operation reached the submit-capable boundary and
has not yet crossed `submitted`", and `absent` before that point is ordinary
rather than suspicious. Nothing about at-most-once changes: the real command
is still launched only after `submitted` is fsynced under the per-VMID lease,
so no package mutation can precede the write-ahead checkpoint, and an armed
job with no host record is still resolved by durably SEALING that absence
under the same lease -- never by inferring anything from it.

**The mutation outlives SSH and backend loss.** The helper hands the real
command to a runner it double-forks into its own session, reparented to PID
1, with stdio detached, so neither closing the SSH channel nor the client
timing out signals or waits on it. The runner inherits the per-VMID lease's
open file description, so the lease stays held for exactly as long as the
mutation runs -- which is also how "a mutation is running right now" is
observed, with no PID bookkeeping and no PID reuse hazard. Correctness never
depends on the runner surviving: if it is killed anyway (a host reboot, say),
the journal simply stays at `submitted` with the lease free, which is
durably UNCERTAIN and is never retried.

**Package mutation completion proof.** An `apt-get` exit code is never
equated with "the exact approved mutation is durably complete".
`app/inventory/mutation_completion.py` is a pure prover the authority runs
itself, inside the transaction that would commit `mutation_completed`, over
the guest's own dpkg status database read independently on both sides of the
mutation. It requires all of: every frozen `(package_name, architecture)` now
installed at exactly its approved candidate version; every frozen row having
started at exactly its approved installed version; the complete set of
installed version differences between the two readings being exactly the
approved set; no installed package appearing or disappearing; and no package
left in any unfinished dpkg state. A caller supplies parsed evidence and
never a verdict, so there is no way to complete a job by asserting success.
Two legal-but-refused cases are accounted for explicitly rather than
tolerated: a package that *disappeared* because another package overwrote all
its files (a real dpkg outcome apt reports) is still an unapproved workload
change and fails the proof; and a partially applied plan fails it for the
rows that did not land.

**Failure is never release, and completion is never success.** Once the
write-ahead checkpoint exists, an `apt-get` failure, a lost response, a
timeout, a restart, a running operation, an unreadable post-state, host
evidence about a different operation, journal corruption, or any other
ambiguity leaves the job ACTIVE at `mutation_may_have_started`: still owning
the one global destructive slot, still owning its confirmed pre-update
snapshot, still holding rollback authority, with truthful append-only
evidence. Packages may be partly changed, and terminalizing would strand a
half-upgraded guest with no owner. The single exception is the host's durable
`sealed_not_submitted` proof, which releases the job as `blocked` -- no
rollback authority is fabricated for a mutation that provably did not happen.
A proven completion advances the checkpoint to `mutation_completed` and sets
`mutation_completed_at`, and the job stays ACTIVE: mutation success is not
job success, because the healthcheck has not run. Startup recovery continues
to leave `mutation_may_have_started` and later checkpoints active and fenced.

**Only the accepted evidence may submit.** Two invocations can both observe
ACTIVE @ `snapshot_confirmed`, both prepare fresh evidence, and both derive
the SAME deterministic `mutation_operation_id`. Identity therefore cannot be
what decides who may cause a package command. Three independent locks decide
it instead:

- **The host intent is immutable, and only the winner creates it.** Two
  concurrent PREPAREs are simply two read-only readings with nothing durable
  to contend over. The host journal's first record is created by the EXECUTE
  the arming transaction authorized, bound to the digest it accepted, and no
  later caller may substitute another: an EXECUTE presenting a different
  digest for an operation already at `intent` is refused. An `intent` whose
  backend then died is never permission to execute, and never strands
  anything either -- it belongs to an armed job, so the host's
  `sealed_not_submitted` proof is exactly the release path for it.
- **The arming transition names its winner.** It returns `ARMED_NOW` only to
  the invocation that atomically committed this accepted digest, and
  `ALREADY_ARMED` to everyone else, who become recovery-only.
- **Submission re-proves the digest.** The bounded submission critical
  section requires the caller's digest to equal the durable
  `accepted_prepared_evidence_digest` before invoking the host callback. A
  mismatch raises a narrow `PackageMutationEvidenceNotAccepted` with the
  callback having run zero times. That type is deliberately NOT the
  seal-eligible `MutationSubmissionRefusedBeforeCallback`: "my evidence is
  not the accepted one" means some OTHER invocation legitimately holds the
  right to submit and may be exercising it right now, so sealing the
  operation "never submitted" on its behalf would be false.

**Schema v13.** The mutation facts are not merely stored, they are
constrained. Schema v12 added the write-once `mutation_operation_id` column
with a partial UNIQUE index, and CHECK constraints in both directions tying
the checkpoint to its durable facts: rank 5 (`mutation_may_have_started`)
iff both the operation identity and `mutation_may_have_started_at` exist,
rank 6 (`mutation_completed`) iff `mutation_completed_at` exists, and
completion never without the boundary that must precede it. Schema v13 adds
`accepted_prepared_evidence_digest` to that set: exactly 64 lowercase hex
characters, NULL before `mutation_may_have_started` and required from it
onward, write-once once set, and required for a completed mutation. It is
written by the SAME single `UPDATE` as the checkpoint, the operation
identity, and the timestamp, whose `IS NULL` guards make that statement a
compare-and-set — so the arm facts are one indivisible write-ahead authority
fact, exactly one of two concurrent invocations can win it, and the loser's
evidence is never what authority accepted. Triggers make the identity, both
timestamps, and the accepted digest write-once, on top of the existing
checkpoint-never-regresses and terminal-once triggers. This is a DDL
semantics change, so it is a version bump rather than new constraints bolted
onto v12: an existing v12 database must never be structurally different from
a fresh one at the same version. There is no in-place migration; the
pre-release contract is the product updater's explicit, backed-up authority
reset.

**Every guest command revalidates its own target.** A VMID is an execution
locator, never durable identity: PVE can free one and reuse it for an
unrelated guest at any moment, including after the journal has durably
reached `submitted` and before the detached runner reaches `apt-get`. So the
invariant lives in the helper's single fixed guest-command dispatcher
(`_run_guest_command`), not with its callers: every architecture read, dpkg
inventory read, staging step, `apt-get update`, simulation, post-state read,
and the one real package command is immediately preceded by its own fresh
`revalidate_live_target`. Callers cannot opt out and cannot amortize one
check across two commands, which is what let a "validate once, then run
several commands" caller send its second command into a replacement guest.
`revalidate_live_target` and the local-node read issue `pvesh` commands
directly, never through the dispatcher, so the invariant cannot recurse.

This is deliberately not workload-incarnation attestation, which was removed
and stays removed. It is the same PVE-independent continuity model the rest
of the product uses: revalidate at the last practical instant before each
guest operation. If the runner's final check fails, `apt-get` is never
launched and the operation journals a truthful terminal failure. It is
NOT sealed as never-submitted -- `submitted` was already durable, so the
pre-submission release contract no longer applies, and the operation keeps
its ownership and its fence rather than being retried. A replacement guest
also has no staged verifier at the hook path, so APT would fail the hook and
abort before dpkg even if the check somehow passed.

`app/package_update_mutation.py` (orchestration) and
`app/package_update_mutation_host_control.py` (a purpose-specific pinned-key
SSH client) are instantiated only by hermetic tests, exactly like the
snapshot and execution-gate modules. Nothing on the production HTTP, Home
Assistant, scheduler, bootstrap, or updater path can reach any of it; no
helper, key, `authorized_keys` entry, or PVE privilege is deployed for it,
and `tests/test_r0_architecture_regression.py` proves it.

## Same-job rollback execution

The compensation half of the update lifecycle exists internally and is **not
production-reachable**. Nothing on the HTTP, Home Assistant, scheduler,
bootstrap, or updater path can roll anything back; the rollback helper is a
separate dark file that is **not deployed**, with no key, no `authorized_keys`
entry, and neither of the two PVE snapshot privileges upstream accepts for the
rollback endpoint provisioned anywhere. The deployed role stays exactly
`Sys.Audit` plus the VM audit privilege.

```text
mutation_may_have_started ---+
                             |--> rollback_may_have_started -> rollback_completed
mutation_completed ----------+                                  (status = rolled_back)
```

**Both mutation checkpoints are legal entry points, and that is the point.**
A package mutation that failed, was partial, timed out, was killed, or simply
could not be proven complete never reaches `mutation_completed` — and it is
exactly that job which most needs compensating. Schema v13 made every
checkpoint at or beyond rank 6 imply `mutation_completed_at IS NOT NULL`,
which meant the only way to reach rollback from a failed mutation was to write
a completion that never happened. `mutation_completed` means the exact
approved package mutation was *independently proven complete*; it is never a
routing flag. Schema v14 therefore replaces that implication with facts tied
to their own checkpoints in both directions:

- `checkpoint = 'mutation_completed'` requires `mutation_completed_at`, and
  `mutation_completed_at` requires rank ≥ 6 — but a later rank no longer
  implies it;
- `checkpoint = 'health_started'` (rank 7) STILL requires
  `mutation_completed_at`, deliberately unlike the ranks above it. Health
  validation is never compensation for an uncertain mutation the way rollback
  is — it is the next stage of a mutation that already succeeded — so a job
  stuck at rank 5 with `mutation_completed_at` still NULL must route to
  rollback, never to health. This is the one place schema v14 keeps the old
  v13 shape, narrowed to exactly the checkpoint that needs it, rather than
  reinstating the broad "rank ≥ 6 implies completed" rule the rest of this
  section removed;
- rank ≥ 8 (`rollback_may_have_started`) iff both `rollback_operation_id` and
  `rollback_may_have_started_at` exist; rank ≥ 9 (`rollback_completed`) iff
  `rollback_completed_at` exists;
- a rollback operation requires this job's own `snapshot_confirmed_at` (there
  must be something to roll back *to*) and its `mutation_may_have_started_at`
  (there must be something to compensate);
- `rollback_completed_at` requires `rollback_task_upid`. A rollback has no
  unique canonical witness of its own — the source snapshot survives either
  way, and `parent == snapname` is equally true after any earlier rollback to
  the same snapshot — so the recorded UPID is the only durable fact tying a
  completion to *this* operation. Without this, one buggy backend `UPDATE`
  could persist a materially false `rolled_back`, releasing the global
  destructive slot and discarding recovery ownership for a rollback PVE was
  never durably known to have accepted;
- `checkpoint = 'rollback_completed'` and `status = 'rolled_back'` imply each
  other. The reverse half follows transitively (`rolled_back` requires
  `rollback_completed_at`, which requires rank 9); the forward half is
  explicit, so a job can never sit at rank 9 while still `active`, claiming a
  completed rollback yet holding the one global destructive slot;
- `status = 'succeeded'` is impossible without `mutation_completed_at`.

These are constraints on the *durable state*, not merely on the well-behaved
caller: the application proof in `complete_package_update_rollback` already
requires every one of them, and the schema exists to reject the same
impossible rows when a backend statement is wrong.

The checkpoint order remains a monotonic no-regression fence; it is no longer
read as a chain of implied successes. Triggers make the rollback operation
identity, the task identity, and the completion timestamp write-once, and a
partial UNIQUE index keeps one rollback operation to one job.

**Identity.** `app/inventory/rollback_identity.py` derives the operation id
from immutable authority — backend instance, job, resource incarnation,
continuity revision — plus the job's own confirmed snapshot identity and name,
under its own domain separator. The snapshot half is what makes the id mean
"roll THIS job back to THIS exact snapshot": a rollback aimed at anything else
is structurally a different operation with a different journal. A caller can
never supply an operation id, and the same job derives the same id after any
restart.

**Verified PVE rollback semantics.** Read from current Proxmox VE sources, not
inherited from snapshot create:
`POST /nodes/{node}/lxc/{vmid}/snapshot/{snapname}/rollback` is `protected`,
`proxyto => 'node'`, takes an optional `start` boolean (default 0), and returns
a task id from `fork_worker('vzrollback', ...)` — so a returned POST proves
nothing. `PVE::AbstractConfig::snapshot_rollback` refuses a template, a
missing snapshot, a snapshot still carrying `snapstate`, a config under
another lock, and a container still running after its own forced stop. It
holds the config lock as `rollback`, replaces the current config from the
snapshot, moves displaced volumes to `unused`, and sets `parent` to the
snapshot name. `PVE::LXC::Config::__snapshot_rollback_vm_stop` is
`PVE::LXC::vm_stop($vmid, 1)` — a **forced stop** — so rollback is materially
more destructive than create. It never deletes its source snapshot. Task
terminal semantics are PVE's own: only `OK` and `WARNINGS: <n>` are non-errors.

**The guest is left stopped.** This stage pins `start` to 0 as a code-owned
host-side constant; it is not a field of the typed request at all. A
successful rollback therefore always leaves the container stopped. Restarting
it, and validating it afterwards, are deliberately separate future work rather
than three destructive concerns fused into one operation.

**Rollback authority is narrower than update authority, deliberately.** The
predicate re-proved at the write-ahead boundary and again inside the
submission critical section proves the *identity of the thing being rolled
back*: same source, an LXC, present and active, with this job's exact VMID,
binding, locator generation, continuity revision, node, and an available node.
It deliberately does **not** re-prove package-plan currency, and does **not**
require the guest to be `running`. A newer scan, a stale approval, or exact
material that moved on are all expected *after* an update ran — a successful
update guarantees them — so requiring plan currency would let the ordinary
passage of time withdraw the recovery path from a half-upgraded guest. And a
rollback candidate may legitimately already be stopped, which PVE's own
forced stop would produce anyway. This is the same evidence-preservation rule
the snapshot and mutation stages established.

**The write-ahead boundary and the submission critical section.**
`arm_package_update_rollback` commits `rollback_may_have_started` plus the
deterministic operation identity in ONE `BEGIN IMMEDIATE` transaction, after
proving the exact same-job target against a fresh canonical listing through
the same helper `select_package_update_rollback_target` uses. Only after that
boundary is durable may a rollback be submitted, and only from inside
`execute_rollback_submission_if_current`, which re-proves the rollback
predicate while holding the authority store's one writer lock across a single
bounded submission-only host round trip. The instant that call (or a
reattaching read) returns a known PVE task identity, the orchestrator records
it durably — in its own short transaction, which ends immediately — BEFORE
any polling begins. So a failed poll, a lost SSH session, a timeout, or a
backend crash during that bounded, read-only wait can never cost authority a
task identity it already observed: the host's journal is not the only place
that identity survives. Task polling and canonical confirmation run strictly
outside the writer lock, after that write.
`app/inventory/contention_policy.py` gains
`MAX_ROLLBACK_SUBMISSION_TIMEOUT_SECONDS` (90s, enforced in the transport's
constructor) and derives `MAX_ROLLBACK_CRITICAL_SECTION_SECONDS` (95s); the
writer budget is still the worst case over *every* such critical section, so
adding this third one cannot leave ordinary writers short.

**Host-side durable journal.** `deploy/hubinet-package-rollback-helper.py` is a
separate file and a separate logical privilege boundary. Combining rollback
into the snapshot helper was considered and rejected: snapshot create adds a
recovery point, while rollback force-stops a container and replaces its
volumes and config, and keeping them apart means one deployed forced-command
entry never carries both capabilities. It exposes exactly three typed
operations (`inspect_rollback_state`, `submit_same_job_rollback`,
`seal_rollback_never_submitted`), no create, no delete, no lifecycle control,
no generic dispatcher, and no caller-supplied argv or snapshot name. Each
operation is journaled by operation identity, with fsynced atomic renames
under a non-blocking per-VMID `flock`
(`intent -> sealed_not_submitted | submitted -> task_known -> terminal`), the
same crash-durable directory primitive the mutation helper uses (every caller
proves its own parent-fsync barrier, because "already exists" is not "already
durable"), and complete-write journal semantics. `submitted` is fsynced before
`pvesh create` runs and is never resubmitted from; `sealed_not_submitted` is
the only durable release proof; `absent`/`intent` are transient routing
evidence that report `uncertain`, never `not_submitted`. Every pre-flight
refusal — a wrong live target, **any** config lock, a missing, incomplete, or
no-longer-job-owned target snapshot — happens *before* the journal reaches
`submitted`, so an operation PVE was always going to reject never enters the
permanently uncertain window.

Two of those refusals are worth stating exactly, because a narrower version of
each would be wrong:

- **Any config lock refuses, not a curated list.** Upstream
  `PVE::AbstractConfig::check_lock` dies whenever `$conf->{lock}` is truthy; it
  does not accept `backup`, `migrate`, or any other lock type. Treating only
  the snapshot-family locks as blockers would let a legitimately locked
  container reach the durable `submitted` record, after which PVE refuses the
  rollback and the operation can no longer be sealed or retried — the job
  would hold the one global destructive slot for a refusal that was observable
  beforehand.
- **The final proof is ownership, never the name.** A snapshot name is a
  physical PVE key and is never ownership proof. Authority proves the strict
  structured metadata when it arms the rollback, but PVE state can change
  between that proof and the destructive call: the same name can come to exist
  carrying absent, malformed, foreign, or another job's metadata. So the host
  re-proves ownership from its OWN fresh listing immediately before
  `submitted`, against the `expected_snapshot_ownership` the request carries —
  derived by the same `_snapshot_ownership_in_transaction` the arming
  transaction uses, bound into the request fingerprint, and never chooseable by
  a caller. It parses the description through the same strict protocol the
  snapshot helper writes, and fails closed on malformed Hubinet-looking
  metadata, on a second entry claiming this job under another name, and on any
  mismatch.

**`submitted` without a UPID never recovers into success**, which is a
deliberate difference from the snapshot helper. A snapshot's existence with
this job's exact ownership metadata is unique canonical proof that *that*
operation completed. Rollback has no such witness: the source snapshot
survives either way, and `parent == snapname` is equally true after any
earlier rollback to the same snapshot. Without the exact task identity there
is nothing distinguishing "this rollback completed" from "some rollback
completed once", so the answer is uncertain and the job stays fenced.

**Completion proof.** `rollback_completed` requires the coherent set, never
one part of it: the job ACTIVE at `rollback_may_have_started` with its
re-derived operation identity; a terminal non-error PVE task classified by
PVE's own rule; the durable `rollback_task_upid` this job itself recorded; a
fresh canonical listing still proving exactly one complete snapshot carrying
this job's strict ownership metadata; and PVE's `current` pseudo-entry
reporting `parent` equal to that snapshot's name. The `parent` check is
corroboration inside that set, never a standalone proof, and the snapshot
still existing is treated as no evidence at all.

**Failure and uncertainty never release ownership.** A terminal failed task, a
running task, a lost SSH answer, a timeout, an unreadable task status, a
corrupt journal, or host evidence about a different operation all leave the
job ACTIVE at `rollback_may_have_started`, still owning the one global
destructive slot and its confirmed snapshot, with truthful append-only
evidence. None is ever retried: PVE's rollback stops the container and then
replaces volumes and config in two config-locked phases, so a failure anywhere
in that sequence can leave the guest stopped, partially rolled back, or still
config-locked. The single exception is the host's durable
`sealed_not_submitted` proof, which releases the job `blocked`. A recorded
task identity permanently forbids that seal, because PVE already accepted the
rollback.

**Startup recovery** leaves `rollback_may_have_started` active and fenced with
its operation identity and any recorded task, so a restarted backend
re-observes that exact task through the read-only inspection rather than
submitting a second destructive rollback. Nothing in startup ever submits,
seals, or completes a rollback.

**No deletion, and no healthcheck.** This stage deletes nothing — retention
stays future work — and implements no healthcheck: a rolled-back job reaches
`ROLLED_BACK`, and `SUCCEEDED` has no transition at all. Health is now
*defined* per resource (see "Dynamic per-resource health contracts" below),
but nothing evaluates a contract, so no job can be shown to have succeeded.

`app/package_update_rollback.py` (orchestration) and
`app/package_update_rollback_host_control.py` (a purpose-specific pinned-key
SSH client) are instantiated only by hermetic tests.
`tests/test_r0_architecture_regression.py` proves production reachability did
not increase.

## Dynamic per-resource health contracts

Schema v15 adds the authority that says what "healthy" means for one workload.
It is CONFIGURATION, and this stage builds only the configuration: nothing here
executes, schedules, or evaluates a probe. See `PRODUCT.md`, "What healthy
means", for why the definition is operator-declared rather than inferred.

**Two tables, one current contract per resource.**
`resource_health_contracts` holds one row per `resource_id` — `revision`,
`fingerprint`, `probe_count`, `created_at`, `updated_at` — with a foreign key
to `resource_incarnations`. `resource_health_contract_probes` holds that
contract's complete required set as `(resource_id, probe_index, kind, target)`,
with `kind` constrained in SQL to the three kinds
`app/inventory/models.py:HealthProbeKind` declares, and `target` constrained to
one bounded, non-empty string with no NUL, whitespace, or newline. The
constraint list is generated from the enum and the shared bounds in
`app/inventory/health_contract.py`, so SQL and Python cannot drift.

**Absence is the schema's first rule.** There is no row for "unconfigured" —
absence *is* unconfigured — and `probe_count` is constrained to at least one,
so an empty contract cannot be stored.

The schema constrains what ordinary writes can express; it is not a claim that
a trusted administrator with direct SQL access cannot reconstruct an
inconsistent row set, and per `AGENTS.md` that administrator is trusted rather
than defended against. So there is a second, independent line:
`app/inventory/store.py` verifies `probe_count` and recomputes the fingerprint
on every read, and refuses to return a contract whose probe rows disagree with
its own header. Reporting a short contract as complete would understate what
the operator required, so the read fails closed instead.

**Replacement is atomic, and a partial probe set is never readable.** The
probes' parent foreign key is `DEFERRABLE INITIALLY DEFERRED`, which lets one
transaction replace a contract in the only safe order: allocate the next
revision, drop the live contract row (compare-and-set on the revision that was
read), drop its now-unparented probes, insert the new contract row, then fill
it. Triggers reject every other write order a statement could attempt — a
contract row can never be `UPDATE`d, a probe row can never be edited, a probe
may only be inserted contiguously from index 0 and within the declared
`probe_count`, a live contract may not lose a probe, and a contract row cannot
carry a revision the allocator has not handed out. The deferred check at
`COMMIT` rejects an orphaned probe row. Clearing is the same transaction
without the rebuild, and never removes the revision history.

Together with the read-time verification above, that is the honest boundary:
partial and edit-in-place contract states are unreachable through the
authority and rejected by the schema, and anything a direct-SQL repair
nevertheless reconstructs is caught when the contract is read.

**Fingerprint and revision.** `health_contract_fingerprint` is SHA-256 over a
domain-separated canonical JSON encoding of the `(kind, target)` set in
`(kind, target)` order, so declaration order never affects it and two resources
requiring the same probes share a fingerprint — exactly as two identical
package plans do. Provenance fields are deliberately excluded.

`revision` advances by one per durable *change*: re-declaring identical
material while the contract still exists is not a change and consumes no
revision. Revisions come from `resource_health_contract_revision_state`, a
separate durable per-resource counter, and are **never reused**. That table
exists because the contract row is deleted on clear, and a counter living
inside it would restart at 1 — which would quietly turn `expected_revision`
from a compare-and-set into a coin flip, since an operator holding revision 1
of a deleted contract would match a completely different contract that also
happened to be revision 1. Clearing therefore removes the contract material
and keeps the allocation history, so a contract cleared at revision 3 and
re-declared becomes revision 4 even when the material is byte-identical: that
is a new generation, not a continuation of the deleted one. The pair
`(revision, fingerprint)` is consequently a stable name for one generation of
one resource's contract, which is what a later health-execution stage needs to
record truthfully.

**Identity across replace, move, and rename.** The contract belongs to the
exact resource incarnation. Setting, reading, and clearing all go through the
same current-executable-binding proof the package-scan and approval paths use,
plus an LXC check, so a missing, quarantined, retired, or replaced incarnation
fails closed. A VMID-reused replacement is a different `resource_id` and simply
has no contract; the predecessor keeps its own historical row as ordinary
foreign-key provenance, which is not something the replacement can use. A
rename or a node move is the same durable resource and keeps its contract,
because neither is part of contract identity.

**Compare-and-set.** `expected_revision` is optional on replace and clear:
`0` asserts the resource is *currently* unconfigured — not that it has never
had a contract, so it stays valid after any number of clear/recreate cycles —
and a positive value asserts exactly that current revision. Because revisions
are never reused, a stale positive value can never become valid again for the
same `resource_id`. Omitting it keeps the ordinary single-editor path
unconditional.

**HTTP.** `GET`/`PUT`/`DELETE
/r0/v1/resources/{resource_id}/health-contract`, bearer-authenticated.

Failures this API raises itself carry a machine-distinguishable body,
`{"detail": {"error": ..., "message": ...}}`, with `error` one of
`resource_not_found` (404), `contract_unconfigured` (404),
`resource_not_current` (409), `revision_conflict` (409 — a lost
compare-and-set, which is a different problem from a stale resource), or
`invalid_contract` (422). The unconfigured case is deliberately *not* a 200
with an empty probe list: a caller must never be able to read absence as a
contract that nothing has to satisfy.

Not every 422 has that shape, and callers must not assume it does. A request
whose *structure* is wrong — a missing or empty `probes` list, an unknown
probe kind, a target outside its length bounds, more than 32 probes, or any
unknown field — is rejected by FastAPI/Pydantic before the handler runs, and
answers with FastAPI's ordinary validation body whose `detail` is a *list* of
field errors. `invalid_contract` is for a structurally valid request whose
contract *material* the authority rejects: a whitespace-bearing target, a
duplicate `(kind, target)`. No custom validation handler is installed to
paper over the difference; the two layers are genuinely different rejections
and the taxonomy says so. `PUT` forbids unknown fields, so no `command`,
`argv`, `shell`, `script`, `executable`, `working_directory`, or `environment`
can be smuggled into caller-controlled contract material.

**The published snapshot carries the summary, not the material.** Each
resource publishes `health_contract` as `status`
(`unsupported | unconfigured | configured`) plus `revision`, `fingerprint`,
`probe_count`, and `updated_at`. QEMU is `unsupported` because this product
updates packages only inside Debian/Ubuntu LXC guests. The probe list is read
through the dedicated endpoint an operator explicitly invokes, not carried into
entity state on every Home Assistant poll.

**The contract layer itself still never executes anything.** There is no probe
executor, scheduler, worker, or host-control boundary *in this layer*, and no
code in `app/inventory/health_contract.py` or the Home Assistant contract
validation runs `systemctl`, `docker`, `pct`, or SSH. Declaring what healthy
means and checking it are two different jobs in two different files, and
`tests/test_r0_architecture_regression.py` keeps them that way.

Execution itself now exists, one layer up, and is described in the next
section. The `(revision, fingerprint)` pair above is what it names: a job
freezes exactly one generation of one resource's contract, and "never reused"
is what makes that name mean something a year later.

## Job-bound healthcheck execution

Schema v16 adds the last missing half of the update lifecycle: proving whether
the workload an update job changed actually came back. `PRODUCT.md`, "What
healthy means", is the durable product statement; this section is how it is
built. The stage is **implemented internally and dark** — no HTTP route, Home
Assistant action, scheduler, or worker reaches it, and its helper is not
deployed.

### The contract is frozen at issuance

A job's success criterion is decided when the job is issued, and that timing is
the design. Issuance already atomically freezes resource identity, source and
transport authority, approval provenance, and the exact package plan, and at
that moment nothing has been snapshotted and no package has changed. So the
same transaction copies the resource's current health contract *generation*
into immutable job-owned state:

- `package_update_jobs.health_contract_revision`,
  `health_contract_fingerprint`, `health_contract_probe_count` — all `NOT
  NULL`, so a job with no success criterion is unstorable rather than merely
  unreachable;
- `package_update_job_health_probes` — the complete `(probe_index, kind,
  target)` set in the same canonical order the contract's fingerprint covers,
  under the same bounds and the same `kind`/`target` SQL constraints v15
  applies to the live contract.

**A resource with no declared contract cannot be issued a job.** Absence is not
health, so a job whose success criterion does not exist could never truthfully
be called successful, and would reach the mutation boundary with nothing to
validate against. It is refused at issuance, before any snapshot or package
operation exists.

**A stored contract must also be execution-eligible before issuance.** Schema
v15 intentionally stores bounded opaque targets and remains unchanged; being
valid configuration is not proof that the stricter executor can represent it.
The pure validator in `app/inventory/health_execution.py` therefore checks the
exact contract inside the issuance transaction before any job row is written.
It covers all three probe kinds, requires the executor's explicit systemd unit
suffix and exact non-pattern grammar, and requires the exact Docker name
grammar. It performs no host I/O. A non-executable contract remains readable
configuration but produces no job, snapshot, or package mutation. Because the
standalone host helper cannot import backend code, one regression compares its
compiled patterns and suffix set byte-for-byte with this backend definition.

The frozen rows are written before the parent job row through the same
`DEFERRABLE INITIALLY DEFERRED` foreign key the frozen package rows use, and
triggers make the three columns and the probe rows immutable from then on.

### One boundary decides which contract wins

The live contract is mutable; the job's copy is not. Exactly one boundary
decides which applies, and it is the write-ahead package-mutation checkpoint.

**Before `mutation_may_have_started`,** the live contract must still *be* the
generation the job froze, and `InventoryAuthority._frozen_health_contract_drift`
is checked as part of the ordinary current-authority proof every pre-mutation
transition shares — preflight, snapshot intent, snapshot submission, snapshot
confirmation, the execution-time plan gate, and mutation arming. Drift makes
the job stale in exactly the way a changed package plan does, and the execution
gate terminalizes it `blocked` with its snapshot retained. The comparison is
over the whole generation: same `resource_id`, same never-reused `revision`,
same fingerprint, same probe count, and the same exact canonical probe
material.

Revision *and* material are both compared because neither is sufficient. A
clear-and-recreate of byte-identical probes leaves the fingerprint unchanged,
so a fingerprint-only check would call the old job current — but v15 never
reuses a revision, so the revision moved, and that is a new statement by the
operator rather than a continuation of the deleted one. Conversely a durable
row set that became incoherent while the revision stayed equal is caught by the
material comparison.

**From `mutation_may_have_started` onward** the drift check stops applying, by
an explicit checkpoint-rank gate. Packages may already have changed, and
re-deciding success against a contract the operator edited afterwards is moving
the goalposts. The frozen copy is the only authority; the live contract may be
replaced or cleared with no effect on the job.

### The checkpoint, and what it does not imply

`CHECKPOINT_ORDER` gains `health_completed` between `health_started` and
`rollback_may_have_started`, so the ranks are `issued`(1) …
`mutation_completed`(6), `health_started`(7), `health_completed`(8),
`rollback_may_have_started`(9), `rollback_completed`(10).

`health_completed` means **"this job's frozen contract reached a DEFINITIVE
verdict"**, not "the workload is healthy". The verdict is `health_outcome`, and
a job sitting at this checkpoint with `health_outcome='failed'` is ACTIVE,
still owns the one global destructive slot, still owns its confirmed snapshot,
and can still arm a same-job rollback.

That distinction is the v14 lesson applied to a second branch, and the SQL
keeps it: each health fact is tied to its OWN checkpoint in both directions
(`checkpoint='health_started'` ⟺ `health_started_at`; `checkpoint =
'health_completed'` ⟺ `health_completed_at` and `health_outcome`), and no rank
implication is used. Ranks 9 and 10 therefore still do not imply
`mutation_completed_at`, and now also do not imply any health fact — a job
whose mutation could not be proven, or whose health evaluation never reached a
verdict, reaches rollback without fabricating either.

`health_started` remains deliberately narrower than the rollback ranks: it may
only be reached from a mutation that was *proven* complete, because health
validation is the next stage of a success, not compensation for an uncertain
mutation.

### There is exactly one legal success transition

Schema v16 states the complete `succeeded` contract, in both directions:

```sql
CHECK(status != 'succeeded' OR
      (mutation_completed_at IS NOT NULL AND
       health_started_at IS NOT NULL AND
       health_completed_at IS NOT NULL AND
       health_outcome = 'passed' AND
       checkpoint = 'health_completed')),
CHECK(health_outcome IS NOT 'passed' OR status = 'succeeded'),
```

plus triggers making `health_outcome='passed'` impossible unless every frozen
probe carries its own durable `passed` result row, and `health_outcome='failed'`
impossible without a complete result set containing a proven failure. So no
package command exit code, no proven mutation, no reachable guest, and no
absence of observed failures can independently produce `SUCCEEDED`, and a
passing verdict and a succeeded job are one indivisible durable event written
by one statement.

`package_update_job_health_probe_results` holds that evidence: one row per
frozen probe, bound by foreign key to the exact `(job_id, probe_index)` it
reports on, immutable, and insertable only for a job that has actually started
a health evaluation and not yet completed one. `reason` is a bounded token from
a closed taxonomy the executor owns — raw stdout, stderr, command text, and
guest output never reach durable state.

### PASS, FAIL, UNKNOWN

`aggregate_health_outcome` is a pure, total ALL-OF with no threshold, majority,
percentage, or OR anywhere in it:

| Frozen probe results | Verdict | What is written |
| --- | --- | --- |
| every probe `passed` | PASSED | completion, verdict, results, `SUCCEEDED` |
| any probe `failed` | FAILED | completion, verdict, results; job stays ACTIVE |
| otherwise | UNKNOWN | nothing but a bounded event |

A proven failure beside an unevaluable probe is still FAILED: one false
conjunct proves an ALL-OF false whatever the others did. PASSED, by contrast,
requires every member positively proven — absence of an observed failure is not
a pass.

UNKNOWN is refused by the definitive finalizer outright. It is not a verdict,
it never becomes durable, and the job stays ACTIVE at `health_started` with its
snapshot and rollback authority intact.

**Retrying is safe here, and is not safe for any other stage**, for one
structural reason: health execution is READ-ONLY. It runs `systemctl show` and
`docker inspect`. There is therefore deliberately no host operation journal, no
`may_have_started` uncertainty checkpoint, no lease, and no at-most-once fence
in this stage — inventing one would mimic the shape of the snapshot, mutation,
and rollback boundaries without their reason for existing. What *is* kept is
at-most-once *acceptance*: exactly one definitive completion can commit, and
the write-once triggers stop a late result overwriting an accepted verdict or a
rollback that moved the job on.

### The host boundary

`app/package_update_health.py` (orchestrator),
`app/package_update_health_host_control.py` (pinned-key SSH transport), and
`deploy/hubinet-package-health-helper.py` (forced-command PVE boundary,
**undeployed**) — a separate, purpose-specific channel, not an operation added
to the production scan helper and not a revival of the removed generic
`app/host_control.py`. It exposes **one** typed, read-only operation,
`evaluate_health_contract`.

The request is assembled entirely from durable job authority by
`InventoryAuthority.package_update_health_request`. No caller may supply a
VMID, node, contract revision, fingerprint, probe, probe kind, or probe target;
they are facts the job froze. The host echoes the contract revision and
fingerprint back, and the backend re-proves them against the job before
believing a single probe result.

Health execution needs **no new PVE API privilege**: it reads through
host-local `pct exec` behind its own forced-command SSH boundary, so the
provisioned production role stays exactly the audit-only pair.

### Live target revalidation, including atomic acceptance

A false PASS against a *replacement* guest would be a serious authority failure
even though the probes change nothing — it would be a false statement that this
job's workload is healthy. So the existing layered model applies:

- **Backend, before host I/O.** `package_update_health_request` re-proves the
  exact resource/locator context through
  `_post_mutation_job_context_is_current`, the same narrow predicate rollback
  arming uses: same source, an LXC, present and active, this job's exact VMID,
  binding, locator generation, continuity revision, node, and an available
  node. Deliberately NOT current package-plan currency (packages legitimately
  changed) and NOT the current health contract (the frozen copy wins).
- **Host helper, before every `pct exec`.** The helper's single guest-command
  dispatcher owns the invariant, exactly as the mutation helper's does: every
  guest command is preceded by its own fresh `revalidate_live_target`, so no
  caller can amortize one check across two commands.
- **Backend, after the host answered.** The orchestrator runs the same proof as
  an early rejection. The load-bearing proof then runs once more inside
  `complete_package_update_health`'s `BEGIN IMMEDIATE` transaction, after the
  ACTIVE/checkpoint/frozen-contract guards and before observation validation,
  result insertion, aggregation, and verdict commit. Discovery reconciliation,
  rollback arming, and a second finalizer cannot interleave between that final
  proof and commit. A replacement in the old post-host-read/pre-finalizer gap
  yields UNKNOWN/`resource_context_changed`, zero result rows, no completion or
  verdict, and an ACTIVE rollback-capable job.

### The exact commands, and why they are these

Every argv is fixed, and every one was **verified against the real tools**
(systemd 257, Docker 26.1.5), not assumed. A probe target is data: it becomes
one argv element and never command text, never a format string, never a
template, never a shell fragment.

For a guest on the local node there is no shell at all — it is `pct exec`
argv, straight through. The one place a command *line* exists is routing to
another cluster member, because that is what `ssh` hands the remote login
shell, and the scan, execution, and mutation helpers all route the same way
over the passwordless inter-node trust Proxmox itself provisions.

Health execution is the first helper to route an element that came from
outside the file, and **shell quoting is deliberately not the mechanism that
makes it safe.** The kind-specific validation below already restricts a target
to characters a shell reads as nothing at all; the guest-command dispatcher
names the request-derived element explicitly and refuses to route it if it
would need a single quote adding, reporting the probe unevaluable instead. So
the property is checked rather than claimed. The constants around it — notably
the Docker `--format` template, whose braces this file owns — are quoted
normally: their content is fixed and reviewed, the caller's is not, and only
the caller's is subject to that rule.

**`systemd_unit_active`**

```text
env LC_ALL=C systemctl show --no-pager     --property=Id --property=LoadState --property=ActiveState -- <unit>
```

`systemctl is-active` is unusable and is not used. Verified: it expands glob
patterns and exits 0 if **any** matching unit is active — `systemctl is-active
'ssh*'` prints four lines and succeeds — and an explicit `--` does *not* stop
that expansion. A probe built on it could pass because some other unit is up.

`systemctl show` prints one blank-line-separated property block per matched
unit, and three things together make the requested object exact:

1. `--` **is** honoured as end-of-options here (verified: `systemctl show … --
   --help` reports `Id=--help.service` instead of printing usage), so an
   option-like target can never be consumed as an option;
2. the target must match a strict unit-name charset containing none of
   systemd's glob characters `*`, `?`, `[`. This is necessary, not belt and
   braces: a pattern can legitimately match exactly **one** unit (verified:
   `ssh?service` matched only `ssh.service`), so "exactly one block" alone is
   not a sufficient defence;
3. exactly one property block must come back.

An explicit unit-type suffix is required. `systemctl show nginx` silently
resolves to `nginx.service`, and quietly broadening `nginx` would be deciding
on the operator's behalf which object the contract meant; a target that does
not say is reported UNKNOWN (`probe_target_not_exact`) rather than guessed at.
A unit *alias* is accepted, because an alias is the same unit rather than a
pattern matching it.

`ActiveState=active` is the only PASS. Any other known state is a definitive
FAIL — including `LoadState=not-found`, which systemd reports as an ordinary
success with `ActiveState=inactive`: a unit that is not there is definitively
not active. An unreadable, empty, multi-block, or unrecognised answer is
UNKNOWN. "The command ran" is never a PASS.

**`docker_container_running` and `docker_container_healthy`**

```text
env LC_ALL=C docker ps --all --no-trunc --quiet              # daemon oracle
env LC_ALL=C docker inspect --type container --format <CONST> -- <name>
env LC_ALL=C docker ps --all --no-trunc --format '{{json .Names}}'
                                                               # absence proof
```

No pipelines, no `docker ps | grep`, and no interpolated format string: the
template is a constant owned by Hubinet
(`{{.Name}}\t{{.State.Running}}\t{{if .State.Health}}{{.State.Health.Status}}{{else}}<none>{{end}}`).
`--type container` stops an image of the same name matching, and `--` is
honoured (verified: `-- --help` is treated as a container name).

`docker inspect` resolves a container by name **or by ID prefix** (verified),
so the returned `.Name` must equal exactly `/<target>`: an ID-prefix resolution
reports a different name and is refused rather than accepted as the named
container.

An inspect timeout or output overflow is classified before its normally
non-zero killed-process return code. Any other non-zero inspect is not absence
merely because the daemon answers. The fixed final command above was verified
against Docker 26.1.5 on the development host: it is accepted by `docker ps`
and emits each listed container's complete `.Names` value as one JSON string.
The helper decodes every bounded line as JSON and compares the requested name
exactly. Only a successful, well-formed listing in which that exact name is
absent yields `container_absent`; an unavailable/timed-out/overflowing/
malformed listing, or a generic inspect failure while the name remains listed,
is UNKNOWN. No English stderr is parsed.

`docker_container_healthy` is never downgraded to "running". It requires
`.State.Running` true **and** `.State.Health.Status` exactly `healthy`. Not
running, `unhealthy`, `starting`, and *no HEALTHCHECK at all* are each a
definitive FAIL, because the operator specifically demanded Docker health.

### Restart, retry, and rollback

Startup recovery is unchanged and deliberately so: `health_started` and
`health_completed` are not in `_STARTUP_INTERRUPTIBLE_CHECKPOINTS`, so a
restarted backend leaves such a job ACTIVE and fenced, owning its global slot
and its snapshot. A restart is never evidence about a workload, so it never
marks a `health_started` job succeeded — but because the evaluation is
read-only, it may simply be run again, and duplicate reads cannot produce
duplicate destructive actions because there are no destructive actions.

Same-job rollback gains two entry points, under exactly the v14 rule. The legal
set is now `mutation_may_have_started` (failed, partial, or unproven mutation),
`mutation_completed` (an operator rolling back before or without health),
`health_started` (an interrupted or unresolved evaluation), and
`health_completed` with `health_outcome='failed'` (a proven health failure).
Requiring health *success* before allowing compensation would fence exactly the
guests that need it, in the same way requiring mutation success once did. A
PASSED verdict is inseparable from `SUCCEEDED`, so a rollback after one is
refused as terminal. Everything else about rollback is unchanged: exact
same-job snapshot ownership, the live resource/locator proof, the deterministic
operation identity, the host journal, and at-most-once submission.

**There is no automatic health-triggered rollback.** A failing verdict reports
and stops; `tests/test_package_update_health.py` proves it makes zero calls
into the rollback host control and arms nothing. Deciding *when* compensation
should happen automatically is a product decision this product has not made,
and there is no retry count, grace period, delayed-health policy, threshold,
majority, or OR logic anywhere in this stage.

### Concurrency

Every host round trip happens strictly outside the authority store's writer
transactions. Nothing holds `BEGIN IMMEDIATE` across SSH, `pct`, `systemctl`,
`docker`, or the probe loop — affordable precisely because a read-only
evaluation needs no critical section to prevent a second destructive
submission, unlike the snapshot, mutation, and rollback boundaries. The
authority transitions are short and local: start the evaluation, or atomically
re-prove live context and accept its exact result. The latter proof, complete
observation validation, shared kind/outcome/reason semantic validation,
aggregation, result insertion, and verdict commit all share one
`BEGIN IMMEDIATE`. Concurrent or repeated orchestrator calls are safe, at most
one definitive completion can commit, and a late result can never overwrite an
accepted verdict or a rollback that advanced the job.

### How production reaches it

Through the one worker, at `mutation_completed` or `health_started`, and
through nothing else -- see "Production update activation". One wake performs
at most one truthful read-only attempt. A PASS terminalizes the job
`SUCCEEDED`; a FAIL leaves it ACTIVE and rollback-capable and the worker idle;
an UNKNOWN writes no verdict at all and leaves the evaluation repeatable,
which an operator asks for through `resume_update`. There is still no retry
interval, backoff, grace period, attempt count, or threshold, and this stage
still makes zero calls into the rollback stage.

The health helper is deployed behind its own dedicated key and forced command
and needs **no new PVE API privilege**: it reads through host-local `pct exec`,
so the provisioned role stays exactly the audit-only pair.

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
