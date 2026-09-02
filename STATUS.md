# Hubinet Ops — current state

## Implemented

- **Dynamic PVE discovery** — nodes, LXC and QEMU guests, discovered from the
  PVE API with no static VMID configuration anywhere.
- **Persistent backend inventory, scans, approvals, and internal jobs** —
  SQLite authority database (schema v13):
  identity, locator bindings and generations, presence/lifecycle, retained
  missing/replaced history, source health and freshness, discovery-run
  ownership with CAS/fencing and restart recovery, immutable package-scan
  source context, durable exact-plan approval facts, and internal durable
  package-update job authority. Jobs copy immutable approval/context provenance
  and exact package rows, use UUID request idempotency, own one global active
  slot, record append-only events, and are interrupted before package mutation
  on restart. Schema v10 added the job-owned snapshot operation identity, its
  write-ahead uncertainty checkpoint, the observed PVE task identity, and
  SQL-level state-machine invariants over all of them. Schema v11 added the
  explicit, material `architecture` column to package rows (see
  "Execution-time plan equality" below). Schema v12 added the job-owned
  package mutation operation identity and SQL-level invariants tying both
  mutation checkpoints to their durable facts in both directions. Schema v13
  adds `accepted_prepared_evidence_digest` — the exact preparation evidence
  the arming transaction accepted — written by the same single compare-and-set
  statement as the checkpoint, the operation identity, and the timestamp, so
  the mutation-arm facts are one indivisible write-ahead authority fact and
  only the invocation carrying that digest can submit (see "Crash-safe
  package mutation" below).
- **R0 HTTP API** — `GET /r0/v1/health`, `/backend`, `/snapshot`, plus exactly
  one authority-only mutation,
  `PUT /r0/v1/resources/{resource_id}/package-plan-approval`. Bearer
  authentication is required on every endpoint except the
  deliberately unauthenticated minimal `/r0/v1/health` liveness probe, which
  exposes no inventory or credential data.
- **Home Assistant integration** — config flow, coordinator, structural
  contract validation, dynamic devices and entities, package-scan summary and
  concise approval-status sensors, diagnostics with recursive secret redaction,
  and native `view_update_plan` / `approve_update_plan` actions. The view action
  uses the native Hubinet resource-device selector and returns exact package
  rows as response data, never as entity attributes. Distributed via HACS.
- **Automatic Debian/Ubuntu LXC package scanning** — configurable six-hour
  default interval, one worker, typed pinned-key SSH to a forced PVE helper,
  fixed `pct exec` operations, APT metadata refresh plus upgrade simulation,
  exact durable package rows/fingerprint, fencing, restart recovery, and
  failure-is-unknown semantics. It never installs packages.
- **Bootstrap and deployment** — `deploy/bootstrap-proxmox-0.5.sh` provisions a
  fresh unprivileged LXC, a least-privilege PVE identity, TLS trust, a dedicated
  forced-command scan boundary, the service, and an nftables boundary. This
  remains the first-install/disaster-recovery/deliberate-rebuild entrypoint
  only.
- **In-place product updates** — `deploy/update-proxmox-0.5.sh` updates an
  *existing* installation identified by `--vmid`, in place: install once,
  update many times. It cross-verifies the CT's ownership chain against the
  PVE identity before touching anything; classifies the app payload,
  `requirements.txt`, the systemd unit, the PVE host helper, and the
  authority schema against one exact target git commit; prints the exact
  plan and requires approval before any mutation (`--dry-run` stops there);
  stages every replacement while the old service is still healthy; then
  activates in a fixed order, with filesystem rollback material retained
  until acceptance passes. A schema-compatible target preserves the
  authority database, `backend_instance_id`, and every credential/config
  file untouched — no PVE identity rotation, no config rewrite, no venv
  rebuild unless `requirements.txt` changed, no PVE helper rewrite unless its
  content changed. An incompatible authority schema requires explicit
  operator authorization (a dedicated interactive confirmation, or
  `--yes --allow-authority-reset` non-interactively), makes one coherent
  SQLite backup of the current authority database (validated before
  anything is removed), then resets only that database — never the LXC,
  network, PVE identity, or other credentials — and reports that Home
  Assistant re-enrollment is required. A target failure after that reset is
  rolled back to the coherent pre-update installation, authority database
  included, never leaving old code paired with a new schema. Package/job
  execution updates are out of scope for this stage — see
  `deploy/README-update-proxmox-0.5.md`.

The PVE API inventory surface remains read-only. The backend's sole mutation
route records Hubinet approval authority state only. Internal update-job
issuance, job-owned snapshot safety, the execution-time plan equality gate,
and crash-safe package mutation are not production-reachable: there is no
HTTP/HA creation control and no worker or scheduler consuming jobs, and no
snapshot helper, execution helper, mutation helper, key, or PVE mutation
privilege is deployed. Package scanning may write APT index/cache metadata
but never changes workload packages, and neither does the execution-time
gate's or the mutation stage's own metadata refresh and simulation. There is
no production-reachable workload package mutation, and no healthcheck
execution, rollback execution, snapshot deletion, lifecycle mutation, policy,
or endpoint failover anywhere.

## Human0 validation

The implemented R0/bootstrap/discovery/package-scan/Home Assistant scope in
v0.5.0-rc3 has completed its first real operator Human0 validation on a
self-administered Proxmox host. This is separate from the automated CI evidence,
which uses fake transports, simulated process output, and an ephemeral smoke
sandbox.

- **PASS:** A fresh default-path bootstrap completed and created the backend
  LXC, least-privilege PVE read identity, forced-command package-scan boundary,
  firewall, service, and discovery state; final onboot was enabled only after
  acceptance passed.
- **PASS:** Dynamic discovery was healthy and fresh, and Home Assistant enrolled
  through the supported HACS/native integration path.
- **PASS:** A supported Debian LXC completed an automatic package scan. Durable
  exact rows and `pending_count` matched the Home Assistant summary.
- **PASS:** Unsupported QEMU/HAOS scanning published unavailable/unknown pending
  updates, not a false zero.
- **PASS:** Holding the Debian guest's APT lock produced a real
  `package_manager_busy` failure. Current publication became `status=failed`,
  `pending_count=null`, `plan_fingerprint=null`, and `packages=[]`; the Home
  Assistant entity became unavailable instead of reusing the previous success.
- **PASS:** After the lock was released, a later automatic scan recovered to
  success and Home Assistant again showed the correct count.
- **PASS:** An independent `apt-get -s upgrade` inside the guest reported 24
  upgrade operations, matching the backend exact plan and Home Assistant count.
- **PASS:** After the operator manually upgraded the test guest outside Hubinet
  Ops, a new scan changed the backend and Home Assistant count from 24 to 0.
- **PASS:** After the operator manually restored a pre-update PVE snapshot
  outside Hubinet Ops, a new scan restored the observed 24-package plan.

Hubinet Ops performed neither the package upgrade nor the snapshot rollback in
the last two checks. They were manual operator actions used only to verify that
Hubinet observes current guest state. Human0 validates only the scope that is
actually activated in production; job-owned snapshots, the execution-time plan
gate, and crash-safe package mutation exist internally but are dark and have
had no operator Human0 validation, and healthchecks and rollback execution
remain unimplemented.

## In-place product update lifecycle

Generic (non-workload) in-place Hubinet Ops updates are implemented and have
complete automated validation (focused pytest for the Python helpers,
sandboxed shell smoke coverage for `deploy/update-proxmox-0.5.sh` exercising
a code-only update, a `requirements.txt` change, a systemd-unit change, a PVE
helper change, an authorized destructive authority reset with coherent
backup, a refused reset, rollback after a target failure that followed a
destructive reset, ownership/provenance fail-closed paths, the installed-
source marker, repeated updates on one synthetic installation, filesystem
durability-barrier ordering and failure seams (forward activation, rollback
restoration including replay, and the final accepted-target barrier before
completion), and the immediately-before-mutation ownership/plan fence). The
first real operator Human0 validation of this updater completed against CT110
using installed source commit
`61d2bc6b04658db39d5120e1f52624450305e93b`: the service was enabled and
active, health passed, the test requirement was removed, and the authority
database was present. Workload package update execution remains a separate,
unimplemented future stage.

## Exact update-plan approval

- **Implemented:** fresh exact-plan presentation, explicit durable approval of
  the reviewed `(resource_id, scan_run_id, plan_fingerprint)`, and atomic
  fingerprint/resource/source-context revalidation. A later same material
  fingerprint remains effectively approved only while required resource and
  source context is unchanged. Changed, failed, interrupted, unsupported, or
  unavailable plans are not effectively approved.
- Approval is authority state only. This stage cannot install or upgrade
  packages or create PVE snapshots. Internal job issuance copies that approval
  provenance but is not exposed to production callers.

## Durable package-update job authority

- **Implemented internally:** atomic issuance of one non-empty current exact
  plan, historical approval provenance, frozen source/resource locator context,
  immutable copied package rows, request-id retry semantics, global durable
  single-flight, current-authority revalidation, append-only events, and
  pre-mutation restart interruption.
- **Not activated:** no HTTP or Home Assistant job control, executor, package
  mutation, healthcheck, or rollback execution path exists.

## Job-owned snapshot safety

- **Implemented internally:** a deterministic, restart-stable per-job snapshot
  identity; strict structured ownership metadata in the snapshot description as
  the authority proof (never the name); a `snapshot_may_have_started`
  write-ahead checkpoint committed before any mutation request can be sent;
  observed PVE task identity; verified PVE async-task semantics with mandatory
  fresh canonical snapshot re-read before confirmation; a durable per-operation
  host journal under a per-VMID `flock` that reattaches instead of resubmitting;
  transient host `absent`/`intent` routing evidence plus a durable
  `sealed_not_submitted` no-future-submit fence, serialized against delayed
  helpers by the same per-VMID lease and required before a pre-submission job
  may release the global slot; moved/gone-guest liveness without a successful
  PVE target read; terminal retention of canonically proven snapshots when
  current authority becomes stale, without granting rollback authority;
  fail-closed handling of every other ambiguity; startup recovery that fences
  an uncertain snapshot operation and keeps it owning the global slot; and the
  same-job rollback authorization contract.
- **Not activated:** no production HTTP, Home Assistant, scheduler, bootstrap,
  or updater path can create a PVE snapshot. The snapshot helper is a separate
  dark file that is **not deployed**, no key or `authorized_keys` entry exists
  for it, and no extra PVE privilege (`VM.Snapshot`) is provisioned. The
  package-scan helper remains scan-only.
- **Deliberately deferred:** rollback *submission*. Only the authorization and
  selection contract exists; executing a rollback is left to the activation
  stage rather than shipped to a lower safety bar. There is also no snapshot
  deletion or retention in this stage, and no workload package mutation.
- **Implemented internal safety/liveness infrastructure:** the two snapshot
  critical sections (`execute_snapshot_submission_if_current`,
  `resolve_pre_submission_block`) correctly hold the authority store's writer
  lock across one bounded host round trip each; that serialization is
  unchanged. `app/inventory/contention_policy.py` now sizes the authority
  store's SQLite writer wait budget (`AUTHORITY_WRITER_WAIT_BUDGET_MS`,
  105s) from an explicit, machine-enforced relationship to the maximum
  bounded snapshot host critical-section duration
  (`MAX_SNAPSHOT_HOST_CRITICAL_SECTION_SECONDS`, 95s) plus a scheduling
  margin, replacing the previous one fixed `BUSY_TIMEOUT_MS = 5000` shared by
  every writer. `SshPackageUpdateSnapshotHostControl` now rejects a
  `timeout_seconds` above `MAX_SNAPSHOT_HOST_TIMEOUT_SECONDS` (90s) before
  any SSH or process execution, so a snapshot host round trip long enough to
  legitimately exhaust that writer budget can no longer be configured. See
  `ARCHITECTURE.md`, "SQLite writer-contention policy" for the exact values
  and what this does and does not guarantee. This is not permission to hold a
  polling transaction open; task polling and canonical confirmation still run
  strictly outside both writer critical sections.

## Execution-time plan equality

- **Implemented internally:** a proven multiarch binary-package identity
  contract -- durable identity is `(package_name, architecture)`, and
  architecture is *proven* from the guest's own independent dpkg installed
  inventory (`dpkg-query -W`, `dpkg --print-architecture` -- fixed,
  argument-less commands) and cross-checked against APT's `-s upgrade`
  candidate description, never inferred from that candidate description
  alone; a package changing between an architecture-specific binary and
  `Architecture: all` is out of scope and fails closed. Architecture is now
  explicit material identity in the plan fingerprint, approval, and every
  durable package row (schema v11). Every APT `Conf` (configure) action is
  validated and must be bound to an approved `Inst` row -- a standalone,
  contradictory, or duplicate `Conf`, or any evidence of pre-existing
  unfinished dpkg state, fails the plan closed rather than silently
  disappearing. One canonical parser
  (`app/package_scan.py::parse_apt_simulation`) is shared, unchanged, by
  both package scanning and this gate. A dark orchestrator
  (`app/package_update_execution.py`) runs, for one job at exactly
  `snapshot_confirmed`: a fresh execution-time APT metadata refresh,
  simulation, and dpkg identity read over a separate dark pinned-key SSH
  transport and forced-command PVE helper
  (`deploy/hubinet-package-update-helper.py`, exposing exactly one
  non-mutating operation), then an atomic authority comparison
  (`InventoryAuthority.evaluate_package_update_execution_plan`) of that fresh
  canonical material against the job's immutable frozen rows -- complete-set
  equality only, never subset/name-only matching. An exact match changes
  nothing durable about the job (no checkpoint advance, no persisted
  permission flag); a mismatch terminalizes the job `blocked`, retaining its
  confirmed snapshot and releasing the global slot without granting rollback
  authority. A provably stale current-authority context at this same
  pre-mutation gate is likewise never left dangling ACTIVE: both the gate's
  cheap pre-host check
  (`InventoryAuthority.revalidate_or_release_stale_package_update_execution`)
  and the post-host comparison atomically terminalize the job `blocked` the
  moment staleness is proven, so the one global destructive slot can never
  be starved by an obsolete job with only a backend restart as a way out --
  a job that goes terminal for an unrelated reason while a host round trip
  is in flight is never mistaken for this and never overwritten. A newest
  RUNNING package scan is transient rather than stale: the gate returns a
  retryable result, preserves the ACTIVE snapshot-confirmed job and retained
  snapshot, and avoids the host round trip when the scan is already visible
  at the pre-host check. Once that scan completes failed/unknown, the old job
  is stale and releases normally.
- **Not activated:** no HTTP, Home Assistant, scheduler, bootstrap, or updater
  path can invoke this gate; the execution helper is a separate dark file
  that is **not deployed**, with no key or `authorized_keys` entry and no
  extra PVE privilege. This stage performs zero workload package mutation,
  and a successful equality pass is deliberately not a durable "safe to
  mutate" permit: the future mutation stage must re-run this exact gate again
  immediately before it mutates anything, not trust an earlier pass.

## Crash-safe package mutation

- **Implemented internally:** the product's one real workload package
  mutation, at most once per job, crash-safe on both sides.
  `app/package_update_mutation.py` drives, for one job at exactly
  `snapshot_confirmed`: a read-only host *preparation* (APT metadata refresh,
  `apt-get -s upgrade`, and the two fixed dpkg identity reads) whose exact
  evidence the host journals a digest of; a canonical parse through the SAME
  shared parser package scanning uses; then ONE authority transaction
  (`InventoryAuthority.arm_package_update_mutation`) that re-proves the job's
  checkpoint and complete current authority, re-proves exact complete-set
  equality against the job's immutable frozen rows, and commits the
  write-ahead `mutation_may_have_started` checkpoint plus a deterministic,
  restart-stable `mutation_operation_id` derived from immutable authority
  facts. Only after that boundary is durable may a package command be
  submitted, and only from inside a short critical section
  (`execute_package_mutation_submission_if_current`) that re-proves current
  authority while holding the authority store's writer lock -- a bounded
  round trip that never waits for the package command. A stale context there
  refuses before the host is ever called and is routed to the durable seal.
- **Only the accepted evidence may submit.** The arming transaction also
  commits `accepted_prepared_evidence_digest` in that same statement, and
  reports `ARMED_NOW` only to the invocation that committed it; everyone
  else gets `ALREADY_ARMED` and becomes recovery-only. The submission
  critical section re-proves that digest before invoking the host callback,
  refusing a mismatch with a narrow type that is deliberately not
  seal-eligible. On the host side a PREPARE that finds an `intent` already
  journaled refuses rather than overwriting its digest, so a concurrent
  PREPARE can never replace the material authority bound itself to. An
  orphaned intent — one whose backend died before arming — is therefore
  never permission to execute: no later invocation can obtain its digest,
  and the job, never having crossed the write-ahead boundary, is resolved by
  the existing startup contract that interrupts `snapshot_confirmed` jobs
  and frees the global slot.
- **A pre-dpkg action gate binds the REAL invocation to the approved plan.**
  The one real command installs a fixed, code-owned `DPkg::Pre-Install-Pkgs`
  hook at protocol Version 3, so APT's own resolved action stream must
  exactly equal the authority-accepted material before dpkg receives any
  package operation. This closes the window in which APT metadata,
  candidates, holds, pins, or sources change between preparation and
  execution while installed versions still match. Verified against real APT
  in an isolated APT root with a fake dpkg: a refusing hook leaves the dpkg
  package-operation count at zero, a protocol below Version 3 is rejected,
  and an ordinary guest `apt.conf.d` snippet can neither clear the hook nor
  downgrade it. The gate is `/bin/sh` plus `sort`/`tail` -- `dash` and
  `coreutils` are `Essential: yes`, so no new guest prerequisite -- staged
  with the approved material as stdin payload bytes into the guest's own
  tmpfs, never as command text. The independent dpkg post-state completion
  proof is unchanged and still required: the gate prevents, the proof proves.
- **Every guest command revalidates its own live target.** The invariant
  lives in the helper's single fixed guest-command dispatcher rather than
  with its callers, so no caller can amortize one check across two commands,
  and the detached runner revalidates immediately before the real package
  command. A VMID freed and reused after `submitted` is durable therefore
  never receives the mutation; the operation journals a truthful terminal
  failure, keeps ownership, and is never sealed as never-submitted or
  retried.
- **The one real command** is fixed argv with no package name, version,
  option, or command text from any caller: a non-interactive `-y` APT
  *upgrade* under `DEBIAN_FRONTEND=noninteractive` with
  `--force-confdef --force-confold`. Traced against current upstream apt:
  `-s` and the real run share the identical resolver
  (`pkgAllUpgradeNoNewPackages`), which structurally cannot install a new
  package or remove one; `-y` changes no resolver behaviour; the explicit
  `-o` options pin the dangerous defaults against a guest `apt.conf.d`
  override. Traced against current upstream dpkg: a conffile prompt on
  end-of-file is a fatal abort, not a default, so the conffile policy is
  mandatory -- it preserves the operator's file and leaves the distributor's
  as `.dpkg-dist`.
- **At-most-once across crashes:** `deploy/hubinet-package-mutation-helper.py`
  is a separate, deliberately stronger dark boundary exposing four typed
  operations, journaling each by operation identity on the PVE host with
  fsynced atomic renames under a non-blocking per-VMID `flock`
  (`intent -> sealed_not_submitted | submitted -> terminal_success |
  terminal_failure`). `submitted` is fsynced before the command is launched
  and is never resubmitted from; `sealed_not_submitted` is the only durable
  release proof; `absent`/`intent` are transient routing evidence only. The
  real command runs in a runner double-forked into its own session and
  reparented to PID 1, holding the per-VMID lease for its whole life, so an
  SSH loss, a client timeout, or a backend crash can neither kill it nor
  cause a second invocation -- and if it is killed anyway, the journal stays
  at `submitted`, which is durably uncertain and never retried. Only the
  invocation that itself prepared and armed may submit; every recovery
  invocation can observe, seal, or complete, never submit.
- **Completion is proven, never assumed:** `mutation_completed` requires the
  authority's own pure proof over the guest's dpkg status database read
  independently on both sides of the mutation -- every frozen
  `(package_name, architecture)` at exactly its approved candidate version,
  every one having started at exactly its approved installed version, the
  complete set of installed version differences equal to the approved set,
  nothing appearing or disappearing, and no unfinished dpkg state. A caller
  supplies parsed evidence, never a verdict.
- **Failure never releases ownership:** a package-command failure, timeout,
  lost response, restart, running operation, unreadable post-state, corrupt
  or contradictory journal, or host evidence about a different operation all
  leave the job ACTIVE at `mutation_may_have_started`, still owning the one
  global destructive slot, its confirmed snapshot, and its rollback
  authority, with truthful append-only evidence. The single exception is the
  host's durable `sealed_not_submitted` proof, which releases the job
  `blocked` without fabricating rollback authority. A proven completion
  leaves the job ACTIVE: mutation success is not job success.
- **Not activated:** no HTTP, Home Assistant, scheduler, bootstrap, or
  updater path can reach any of this. The mutation helper is a separate dark
  file that is **not deployed**, with no key, no `authorized_keys` entry, and
  no PVE privilege, and the scan, snapshot, and execution-plan helpers gained
  no mutation capability whatsoever. The Version 3 action gate is likewise
  never installed by bootstrap or the updater: it is generated per operation
  and written into one guest's tmpfs only while that operation runs.
  Healthcheck execution, rollback submission, snapshot retention, and
  production activation remain later stages. No real package mutation has
  been performed against any live guest; operator Human0 validation of this
  stage has not been done.
- **Correction completed internally (schema v13).** Three confirmed blockers
  in this stage were closed: the real APT invocation is now bound to the
  accepted plan by its own pre-dpkg Version 3 action gate; the accepted
  preparation evidence is a durable authority fact that exactly one
  invocation can commit and only that invocation can submit with; and every
  guest command, including the detached runner's real package command,
  revalidates its own live PVE target. The stage remains dark, no Human0
  mutation has been performed, and healthcheck and rollback execution remain
  future work.

## Next

- Healthcheck execution and same-job rollback execution.
- Production activation of the update lifecycle.
- Snapshot retention, then lifecycle controls (start/stop/reboot) and manual
  snapshot operations.

## Known limitations

- The Home Assistant test suite requires Python ≥ 3.14.2 with
  `homeassistant==2026.8.1` and does not run on native Windows, because Home
  Assistant imports POSIX `fcntl` at collection time. The pinned Linux suite in
  the existing local CI equivalent and GitHub CI is the compatibility gate. Do
  not patch Home Assistant or fake `fcntl` around this.
- `deploy/bootstrap-proxmox-0.5.sh` and `deploy/update-proxmox-0.5.sh` are
  only executed for real inside the hardened Docker smoke sandbox. GitHub
  uses the guarded `tests/shell/run_bootstrap_smoke_sandbox.sh` wrapper; the
  existing Linux devbox local CI invokes the same Dockerfile and sandbox
  entrypoint directly without faking GitHub runner markers.
- Pre-release: schema v13 is incompatible with v12, v11, v10, and v9, and
  there is no in-place migration path. An existing installation now uses
  `deploy/update-proxmox-0.5.sh` for this: it detects the incompatible authority schema, backs it up, and
  resets only the authority database (see "In-place product updates" below)
  while preserving the LXC, its VMID/network, PVE identity/token, and every
  other credential/config file. Home Assistant re-enrollment is required only
  after that explicit reset, not for an ordinary code-only update.
- Package origin, description, security classification, and reboot-required
  stay unknown unless reliable evidence is present. The first parser derives
  origin/security from stable-English APT simulation evidence and leaves
  descriptions unknown.
- PVE sshd must permit public-key login for the forced root authorization.
  Bootstrap verifies the boundary before starting Hubinet and never rewrites
  operator sshd configuration.
