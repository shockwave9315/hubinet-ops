# Hubinet Ops — current state

## Implemented

- **Dynamic PVE discovery** — nodes, LXC and QEMU guests, discovered from the
  PVE API with no static VMID configuration anywhere.
- **Persistent backend inventory, scans, approvals, and internal jobs** —
  SQLite authority database (schema v21):
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
  package mutation" below). Schema v14 adds the same-job rollback operation
  identity, its write-ahead uncertainty checkpoint, the observed PVE rollback
  task identity, and rollback completion -- and, critically, replaces v13's
  "any checkpoint at or beyond `mutation_completed` implies
  `mutation_completed_at`" implication with per-fact invariants, so a failed,
  partial, or unproven package mutation can reach the rollback boundary
  without fabricating a completion it never had (see "Same-job rollback
  execution" below). Schema v15 adds the operator-declared per-resource health
  contract, its complete required probe set, and a durable never-reused
  revision allocator, with SQL-level constraints rejecting an empty contract,
  an unsupported probe kind, an unbounded target, a duplicate probe, an
  orphaned probe row, an edited or shrunken live contract, and a reused or
  unallocated revision (see "Dynamic per-resource health contract" below).
  Schema v16 binds a package-update job to the exact health contract
  generation it froze at issuance -- revision, fingerprint, and immutable
  copied probe rows -- adds the durable definitive per-probe result rows and
  the `health_completed` checkpoint, and states the terminal `succeeded`
  contract in both directions, so a job can reach it only by proving every
  frozen probe passed (see "Job-bound healthcheck execution" below). Schema
  v17 adds a durable, per-resource `issuance_sequence` to package-update
  jobs, allocated atomically at issuance and never consumed a second time by
  a retried idempotent `request_id`; the latest-job readback and published
  summary order by `issuance_sequence`, not wall-clock `issued_at`, so an
  ordinary host clock correction between two issuances can never make an
  older job outrank a genuinely later one. Schema v18 adds one durable,
  job-keyed post-success package-scan request. Its scan link is write-once and
  accepts only a RUNNING scan for the same resource. Schema v19 makes the
  successful job row the atomic durable consumption fact for its exact
  approval and adds a unique fence permitting at most one successful job per
  approval. Schema v20 adds `guest_operational`, a fourth health-probe kind
  whose `target` column is the one nullable exception to the otherwise
  `NOT NULL` bounded-target `CHECK` (enforced positionally: exactly this kind
  may be `NULL`, every other kind still requires one), with its own partial
  unique index permitting at most one such probe per contract or per frozen
  job copy. Schema v21 (v0.5 health scope reduction) removes
  `docker_container_running` and `docker_container_healthy` from the
  generated `HealthProbeKind` CHECK entirely -- leaving exactly two supported
  kinds, `systemd_unit_active` and `guest_operational` -- and adds a second
  pair of triggers (`resource_health_contract_no_mixed_baseline`,
  `package_update_job_health_probes_no_mixed_baseline`) refusing a
  `guest_operational` row whose parent's declared `probe_count` is not
  exactly 1, so the built-in baseline and an explicit advanced contract can
  never share one contract, enforced in SQL independently of the domain
  validator.
- **R0 HTTP API** — `GET /r0/v1/health`, `/backend`, `/snapshot`,
  `/operator-availability`;
  authority-metadata mutations
  (`PUT /r0/v1/resources/{resource_id}/package-plan-approval`,
  `GET`/`PUT`/`DELETE /r0/v1/resources/{resource_id}/health-contract`,
  `POST .../health-contract/reset` to restore the built-in default); and the
  explicit operator update controls
  (`POST`/`GET /r0/v1/resources/{resource_id}/package-update`,
  `POST .../package-update/resume`, `POST .../package-update/rollback`,
  `GET /r0/v1/package-update/active`). Bearer authentication is required on
  every endpoint except the deliberately unauthenticated minimal
  `/r0/v1/health` liveness probe, which exposes no inventory or credential
  data.
- **Home Assistant integration** — config flow, an options flow (v20;
  Settings → Devices & Services → Hubinet Ops → Configure) for viewing a
  resource's health contract and restoring the built-in default, coordinator,
  structural
  contract validation, dynamic devices, sensors, a binary sensor, and buttons;
  package-scan summary and
  concise `none | approved | stale | consumed` approval-status sensors,
  diagnostics with recursive secret redaction,
  concise per-resource package-update job status/checkpoint/package-count and
  health-outcome sensors, authoritative rollback availability, and native
  `view_update_plan` / `approve_update_plan` / `view_health_contract` /
  `set_health_contract` / `reset_health_contract` / `start_update` /
  `view_update_job` / `resume_update` / `rollback_update` actions. Every
  response-capable action uses the native Hubinet resource-device selector and
  returns exact material — package rows, contract probes, job events — as
  response data, never as entity attributes. Per-resource buttons review and
  approve an exact plan, start/view/resume/roll back an update, and view the
  health contract. Exact plans, bounded job details/events, and contract probes
  are rendered in localized, Markdown-hardened persistent notifications only
  when explicitly requested.
  `start_update`, `resume_update`, and `rollback_update` are explicit operator
  actions and are unreachable from coordinator polling. Distributed via HACS.
  A backend that predates Human1 operator-availability publication (a
  definite 404 on `GET /operator-availability`) keeps inventory/sensors
  working with every Human1 control conservatively unavailable, never with
  invented authority; every other failure on that route still fails the
  refresh closed. An ordinary revision race between the `/snapshot` and
  `/operator-availability` reads gets one bounded retry of the complete pair
  before failing closed. Routine operator failure paths (review/approval/
  start/resume/rollback refusals, a changed reviewed plan, a health-contract
  read failure, a raced control) are localized in English and Polish, not
  only the setup/reauth/coordinator messages.
- **Automatic Debian/Ubuntu LXC package scanning** — configurable six-hour
  default interval, one worker, typed pinned-key SSH to a forced PVE helper,
  fixed `pct exec` operations, APT metadata refresh plus upgrade simulation,
  exact durable package rows/fingerprint, fencing, restart recovery, and
  failure-is-unknown semantics. Ordinary periodic selection skips LXCs already
  known stopped while the execution-time stale-target guard remains in force
  for running-to-stopped races. Successful package updates atomically enqueue
  one real fresh scan; its wake cannot move the ordinary scan lane's absolute
  monotonic deadline, and publication exposes durable
  `post_update_scan_pending` without replacing or synthesizing the last real
  0/N/UNKNOWN result. It never installs packages.
- **Bootstrap and deployment** — `deploy/bootstrap-proxmox-0.5.sh` provisions a
  fresh unprivileged LXC, a least-privilege PVE identity, TLS trust, a dedicated
  forced-command scan boundary, five further dedicated forced-command
  boundaries for the update lifecycle (snapshot, plan simulation, mutation,
  rollback, health) with one private key each and root-only operation journals,
  the service, and an nftables boundary. Acceptance verifies every boundary
  with a non-mutating structural refusal only: no snapshot is created, no
  package changed, nothing rolled back, and no workload probed. This remains
  the first-install/disaster-recovery/deliberate-rebuild entrypoint only.
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
  included, never leaving old code paired with a new schema. It also upgrades a
  pre-activation installation into the activated lifecycle, creating the five
  boundaries, their keys, their journals, and the `package_update` config
  block — and a failed activation update removes exactly the privileged access
  paths it created while leaving unrelated `authorized_keys` entries and the
  scan boundary untouched. It refuses outright, before touching any file, if
  the installation has an ACTIVE package-update job. See
  `deploy/README-update-proxmox-0.5.md`.

- **Production activation of the update lifecycle** — one authenticated
  operator action starts the currently approved update for one resource, and
  one bounded worker composes the existing stages through to a proven health
  verdict. Explicit operator resume and same-job rollback controls, a bounded
  job readback, an active-job witness for the product updater, four Home
  Assistant actions, and one concise per-resource job status entity. Five
  separate forced-command helpers with five dedicated keys are deployed by
  both bootstrap and the updater. See "Production activation" below.

The PVE API inventory surface remains read-only, and the provisioned PVE role
is still exactly `Sys.Audit,VM.Audit`: every workload mutation runs host-local
behind a root-owned forced command, so the inventory API identity never needed
a mutation privilege. Package scanning may write APT index/cache metadata but
never changes workload packages, and neither does the execution-time gate's or
the mutation stage's own metadata refresh and simulation. There is no
automatic update issuance, no automatic rollback, no retry policy, no snapshot
deletion or retention, no lifecycle mutation (start/stop/reboot), no manual
snapshot operation, no compensation policy, and no endpoint failover anywhere.

## Human0 production lifecycle — COMPLETE / PASS

Human0 completed on a real, self-administered Proxmox environment. This is
production evidence in addition to, not a substitute for, the hermetic
automated suites.

- **Fresh bootstrap and Home Assistant enrollment: PASS.** The default-path
  bootstrap, least-privilege read identity, typed forced-command boundaries,
  firewall, service, discovery, HACS install, and native enrollment all worked.
- **Discovery and package-scan behavior: PASS.** Supported LXC exact rows and
  counts matched Home Assistant; unsupported workloads stayed unknown rather
  than false-zero; a real APT-lock failure published UNKNOWN and a later scan
  recovered. Fresh scans also tracked operator-driven package/snapshot changes
  from 24 to 0 and back to 24 without inventing state.
- **Health-contract plumbing: PASS.** The declared per-resource contract was
  frozen into and evaluated by real update jobs.
- **F1 real package update: PASS.** An explicitly approved real 24-package plan
  produced a fresh job-owned snapshot, exact PVE task observation, confirmed
  snapshot, execution-time exact-plan equality, package mutation, independent
  completion proof, frozen health PASS, and terminal `SUCCEEDED`.
- **F2 fresh post-update package scan: PASS.** The real post-success scan
  observed `pending_count=0` and an empty package set. Zero was observed, never
  inferred from failure or absence.
- **F3 failed health and manual rollback: PASS.** A new explicit approval and
  fresh same-job snapshot preceded the package mutation; deterministic frozen
  health failure left the job active and rollback-capable with no automatic
  rollback. The operator explicitly requested rollback, the exact same-job
  snapshot and PVE task were observed, and the job terminalized `ROLLED_BACK`.
  Thus both **NO AUTO-ROLLBACK** and explicit same-job rollback passed.
- **RC6 `pvesh --noproxy` helper defect: RESOLVED.** Snapshot and rollback use
  the supported leading-global option position. Non-zero or ambiguous prior
  evidence remains UNKNOWN and never becomes retry authority.

Two observations are closed without product changes: stopping one CT did not
poison package state for other running LXCs (**OBS-01 not reproduced**), and a
later scan recovered normally from CT103's transient APT metadata-refresh
failure (**OBS-02 transient/recovered**).

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
database was present, on installed source that predates this activation.
Workload package update execution is a separate, production-reachable stage
(see "Human0 production lifecycle" above) with its own automated and completed
real-PVE Human0 evidence. It is not exercised by this updater's own automated
suite, which stays scoped to generic in-place product updates.

## Exact update-plan approval

- **Implemented:** fresh exact-plan presentation, explicit durable approval of
  the reviewed `(resource_id, scan_run_id, plan_fingerprint)`, and atomic
  fingerprint/resource/source-context revalidation. A successful update job
  consumes its exact approval durably and atomically with `SUCCEEDED`; a later
  same material fingerprint does not reactivate it. `consumed` is distinct
  from `stale`, and a new explicit approval receives a new approval identity.
  Changed, failed, interrupted, unsupported, unavailable, and empty plans are
  not effectively approved.
- Approval is authority state only. This stage cannot install or upgrade
  packages or create PVE snapshots. Job issuance copies that approval
  provenance; see "Durable package-update job authority" below for its own
  one production entry point.

## Durable package-update job authority

- **Implemented internally:** atomic issuance of one non-empty current exact
  plan, one-shot-on-success approval consumption, historical approval
  provenance, frozen source/resource locator context, immutable copied package
  rows, request-id retry semantics, global durable single-flight,
  current-authority revalidation, append-only events, and pre-mutation restart
  interruption.
- **Production reachable, through one door.** `issue_package_update_job` has
  exactly one production caller: the authenticated
  `POST /r0/v1/resources/{resource_id}/package-update` route. Issuance
  requires and freezes a declared health contract; see "Job-bound healthcheck
  execution" below.

## Job-owned snapshot safety

- **Implemented internally:** a deterministic, restart-stable per-job snapshot
  identity; strict structured ownership metadata in the snapshot description as
  the authority proof (never the name); a `snapshot_may_have_started`
  write-ahead checkpoint committed before any mutation request can be sent;
  observed PVE task identity; verified synchronous local `pvesh` CLI semantics
  with mandatory fresh canonical snapshot re-read before confirmation; a durable per-operation
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
- **Short host submission boundary:** local `pvesh create` waits for the
  physical snapshot and prints its final task id only afterwards. The helper
  therefore journals `submitted`, starts exactly one detached fixed runner,
  and returns while that runner durably captures bounded stdout/stderr. A
  later inspect promotes only an unambiguous exact terminal UPID to
  `task_known`; incomplete, malformed, truncated, or ambiguous capture stays
  UNKNOWN and is never resubmitted.
- **Single-node package mutation:** the privileged PVE host reached by the
  snapshot, mutation, or rollback connection must currently own the LXC. Each
  helper proves the local node through fixed read-only PVE state and refuses a
  mismatch before `submitted`; snapshot and rollback also use `--noproxy`.
  The option is passed in pvesh's supported leading-global position
  (`pvesh --noproxy create ...`), never after the API path where a closed
  endpoint schema would interpret it as an unsupported property.
- **Production reachable** through the explicit operator start control and the
  one worker, and through nothing else: no scheduler, scan, approval write, or
  Home Assistant poll can create a PVE snapshot. The snapshot helper is
  deployed behind its OWN dedicated key and forced command, separate from
  every other boundary, and still needs no extra PVE privilege — the
  provisioned role stays exactly the audit-only pair. The package-scan helper
  remains scan-only, with its own separate key.
- **Rollback submission** is no longer deferred: the authorization and
  selection contract established here is what "Same-job rollback execution"
  below builds on, unchanged. There is still no snapshot deletion or
  retention.
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
- **Production reachable** through the one worker, which runs this gate
  immediately before it enters the mutation stage. The execution helper is
  deployed behind its own dedicated key and forced command and needs no extra
  PVE privilege. This stage still performs zero workload package mutation, and
  a successful equality pass is still deliberately not a durable "safe to
  mutate" permit: the mutation stage re-runs this exact proof itself,
  immediately before it mutates, in the same transaction that commits its
  write-ahead boundary. The worker runs the gate anyway rather than relying on
  that, because a drifted plan should never reach the stage that owns the real
  package command.

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
- **Production reachable** through the one worker, which enters this stage
  only from `snapshot_confirmed` (submitting) or `mutation_may_have_started`
  (recovery-only, which can never submit). The mutation helper is deployed
  behind its OWN dedicated key and forced command, and the scan, snapshot,
  execution-plan, rollback, and health helpers gained no mutation capability
  whatsoever. It needs no PVE privilege: the real command runs host-local
  through `pct exec`. The Version 3 action gate is still never installed by
  bootstrap or the updater — it is generated per operation and written into
  one guest's tmpfs only while that operation runs. Human0 exercised this
  boundary successfully against a real 24-package plan.
- **Correction completed internally (schema v13).** Three confirmed blockers
  in this stage were closed: the real APT invocation is now bound to the
  accepted plan by its own pre-dpkg Version 3 action gate; the accepted
  preparation evidence is a durable authority fact that exactly one
  invocation can commit and only that invocation can submit with; and every
  guest command, including the detached runner's real package command,
  revalidates its own live PVE target. Healthcheck execution now exists too;
  see "Job-bound healthcheck execution" below.

## Same-job rollback execution

- **Implemented internally:** the product's compensation path, at most once
  per job, crash-safe on both sides. `app/package_update_rollback.py` drives,
  for one ACTIVE job at either `mutation_may_have_started` OR
  `mutation_completed`: the exact same-job target proof over a fresh canonical
  PVE listing, through the SAME
  `select_package_update_rollback_target` contract PR #67 established; ONE
  authority transaction (`InventoryAuthority.arm_package_update_rollback`)
  committing the write-ahead `rollback_may_have_started` checkpoint plus a
  deterministic, restart-stable `rollback_operation_id`; then a submission only
  from inside a short critical section
  (`execute_rollback_submission_if_current`) that re-proves the rollback
  context while holding the authority store's writer lock.
- **Both mutation checkpoints are legal entry points, without fabricating
  anything.** A mutation that failed, was partial, timed out, was killed, or
  could not be proven complete never reaches `mutation_completed` -- and is
  exactly the job that most needs compensating. Schema v14 replaces v13's
  "later rank implies `mutation_completed_at`" implication with per-fact
  invariants, so that job reaches rollback with `mutation_completed_at` still
  NULL. `mutation_completed` remains "independently proven complete" and is
  never a routing flag.
- **A successful rollback leaves the guest STOPPED.** Verified upstream:
  `PVE::AbstractConfig::snapshot_rollback` force-stops a running LXC through
  `PVE::LXC::vm_stop($vmid, 1)`, and the endpoint restarts it only when its own
  `start` parameter is set. This stage pins `start` to 0 as a code-owned
  host-side constant that is not a field of the typed request at all.
  Restarting the guest, and validating it afterwards, are separate future work.
- **Rollback authority is deliberately narrower than update authority.** It
  re-proves exact ACTIVE job ownership, the derived rollback identity, the
  job's own confirmed snapshot, and the exact resource/locator context -- but
  NOT current package-plan currency, and NOT that the guest is running. A newer
  scan or a stale approval is expected after an update ran, and must never
  withdraw the recovery path from a half-upgraded guest; a rollback candidate
  may legitimately already be stopped.
- **At-most-once across crashes:** `deploy/hubinet-package-rollback-helper.py`
  is a separate, deliberately narrower dark boundary exposing three typed
  operations (`inspect_rollback_state`, `submit_same_job_rollback`,
  `seal_rollback_never_submitted`), journaling each by operation identity with
  fsynced atomic renames under a non-blocking per-VMID `flock`
  (`intent -> sealed_not_submitted | submitted -> task_known -> terminal`).
  `submitted` is fsynced before `pvesh create` and is never resubmitted from;
  `sealed_not_submitted` is the only durable release proof; `absent`/`intent`
  are transient routing evidence. Every pre-flight refusal happens before
  `submitted`, so an operation PVE was always going to reject never enters the
  permanently uncertain window: that includes **any** non-empty PVE config
  lock (upstream `check_lock` dies on a truthy lock of any type, not just the
  snapshot family) and a final **ownership** proof of the target snapshot from
  the host's own fresh listing -- a snapshot name is a physical PVE key and is
  never ownership proof, and PVE state can change between authority's arming
  proof and the destructive call. Combining rollback into the snapshot helper
  was considered and rejected: keeping create and rollback in separate
  forced-command boundaries means one deployed key never carries both.
- **Short host submission boundary:** local `pvesh create` waits for the
  physical rollback and emits its final task id only afterwards. The helper
  starts one detached fixed runner after `submitted` is durable, returns to
  the backend, and durably captures bounded stdout/stderr for later exact-UPID
  recovery. Missing, incomplete, truncated, malformed, or ambiguous capture
  remains UNKNOWN and never permits a retry.
- **Completion is proven, never assumed:** `rollback_completed` requires the
  coherent set -- a terminal non-error PVE task by PVE's own rule, the durable
  `rollback_task_upid` this job recorded, fresh canonical evidence of exactly
  one complete job-owned snapshot, and PVE's `current` pseudo-entry reporting
  `parent` equal to that snapshot. `parent` is corroboration inside that set,
  never standalone; the source snapshot surviving is treated as no evidence at
  all, because upstream never deletes it. A `submitted` operation may advance
  only when its completed durable capture yields one exact terminal UPID;
  canonical state alone never proves rollback success.
- **Failure never releases ownership:** a terminal failed task, a running task,
  a timeout, a lost response, an unreadable status, a corrupt journal, or
  evidence about a different operation all leave the job ACTIVE at
  `rollback_may_have_started`, still owning the global destructive slot and its
  snapshot, and none is ever retried. The single exception is the host's
  durable `sealed_not_submitted` proof, which releases the job `blocked`. A
  recorded task identity permanently forbids that seal. A proven rollback
  terminalizes the job `ROLLED_BACK` -- never `SUCCEEDED`: a rolled-back update
  is not a successful update.
- **Production reachable only through an explicit operator request.**
  `arm_package_update_rollback` has exactly one production caller: the
  authenticated `POST .../package-update/rollback` route, which obtains a
  fresh canonical PVE listing through the existing read-only inspection, arms
  the write-ahead boundary, and only then acknowledges — so an accepted
  rollback is durable before the operator is told it was accepted. The worker
  enters this stage only at `rollback_may_have_started`, a checkpoint it
  cannot itself commit, and calls `arm_package_update_rollback` nowhere. The
  rollback helper is deployed behind its OWN dedicated key and forced command,
  separate from the snapshot boundary so one key never carries both create and
  rollback, and neither PVE snapshot privilege is provisioned -- the deployed
  role stays exactly the audit-only pair. Human0 proved an explicit real
  rollback to the exact snapshot owned by the same failed-health job.
- **Extended by schema v16.** Same-job rollback now has four legal entry
  points rather than two: `mutation_may_have_started`, `mutation_completed`,
  `health_started` (an interrupted or unresolved health evaluation), and
  `health_completed` with `health_outcome='failed'` (a proven health failure).
  This is exactly the v14 rule applied to a second branch -- requiring health
  SUCCESS before allowing compensation would fence exactly the guests that
  need it. A passing verdict is inseparable from `SUCCEEDED`, so a rollback
  after one is refused as terminal. Nothing else about rollback changed.
- **Out of scope here:** snapshot deletion and retention, restarting the guest
  after a rollback, and any automatic compensation policy. This stage ships
  the internal primitive only: a caller must ask for one exact job to be
  rolled back, and health execution never asks.

## Dynamic per-resource health contract (implemented)

The health-contract product decision that was open here is now settled and
built. Hubinet Ops still has **no generic inferred workload-health
definition** and will not invent one; instead the operator declares, per
resource, what healthy means. `PRODUCT.md`, "What healthy means", is the
durable statement; `ARCHITECTURE.md`, "Dynamic per-resource health contracts",
is how it is built. This stage shipped **configuration authority only**;
"Job-bound healthcheck execution" below is the stage that evaluates one.

- **Operator-declared, per `resource_id`.** Never a VMID, hostname, node, or a
  list in a repository or config file. A VMID-reused replacement is a different
  incarnation and inherits nothing; the same durable resource keeps its
  contract across a rename or a node move. Setting, reading, and clearing all
  go through the existing current-executable-binding proof plus an LXC check,
  so a missing, quarantined, retired, or replaced incarnation fails closed.
- **All configured probes are required.** Exactly two typed kinds:
  `systemd_unit_active` and `guest_operational`. A contract is EITHER exactly
  one `guest_operational` probe (the built-in baseline) OR one or more
  `systemd_unit_active` probes (an explicit advanced contract) — never both
  (v0.5 health scope reduction; Docker-specific probes existed through an
  earlier iteration and were removed end-to-end). No OR trees, no scoring, no
  percentages, no boolean expressions, and no caller-supplied command, argv,
  shell, script, or environment material — a probe names a target, and a
  target is data for a fixed argv operation the future executor builds
  itself.
- **Absence is not health.** No contract means *unconfigured*, which is never
  "healthy", "passed", or "nothing to check". `probe_count` is constrained to
  at least one, so an empty contract cannot be stored, and the HTTP read
  reports an unconfigured resource as a distinct `contract_unconfigured`
  failure rather than a successful empty contract.
- **Schema v15, atomic replacement.** One contract per resource, bounded to
  1-32 probes with bounded targets and no duplicate `(kind, target)`. A
  deterministic fingerprint covers only the canonical probe material, so
  declaration order never affects it. Replacement and clearing are single
  transactions and the triggers reject every unsafe write order, so no partial
  probe set is reachable through the authority; reads independently verify
  probe count and fingerprint and fail closed, which is what catches a
  direct-SQL repair that reconstructed an inconsistent row set.
- **Revisions are never reused.** `revision` advances by one per durable
  change (re-declaring identical material while the contract exists is not a
  change), and comes from a durable per-resource allocator that survives
  clearing. A contract cleared at revision 3 and re-declared becomes revision
  4 even if the material is identical — a new generation, not a continuation.
  That is what makes `expected_revision` a real compare-and-set: a stale
  positive revision can never become valid again, and `expected_revision=0`
  keeps meaning "currently unconfigured" rather than "never configured".
- **Operator surface.** `GET`/`PUT`/`DELETE
  /r0/v1/resources/{resource_id}/health-contract`, plus
  `POST .../health-contract/reset`, with bearer auth. Failures
  this API raises itself carry `{"detail": {"error", "message"}}`; a
  structurally invalid request is still rejected by FastAPI/Pydantic first,
  with its ordinary list-shaped validation body (see `ARCHITECTURE.md`). Plus
  the native Home Assistant
  `view_health_contract` / `set_health_contract` / `reset_health_contract`
  actions on the existing resource-device selector. The published snapshot
  carries a concise `unsupported | unconfigured | configured` summary and its
  identity; the probe list is response data from an explicitly invoked action,
  never entity attributes.
- **Out of scope in this stage, and now built in the next one:** every part
  of health *execution*. The contract layer itself still runs nothing — no
  `systemctl`, `pct`, or SSH lives in it, and declaring what healthy
  means stays a different file from checking it. What changed is that a
  contract is now **required** to issue a package-update job and is copied
  into it: see "Job-bound healthcheck execution" below. The durable shape here
  needed no redesign for that, which is what the never-reused revision was
  for.
- **Production reachability:** health-contract configuration is production
  reachable, because it is authority metadata only. The update *execution*
  lifecycle is now production reachable too, but only through an explicit
  operator action -- see "Production activation" below.

## Job-bound healthcheck execution

The last missing half of the update lifecycle: proving whether the workload an
update job changed actually came back. `PRODUCT.md`, "What healthy means", is
the durable product statement; `ARCHITECTURE.md`, "Job-bound healthcheck
execution", is how it is built. See "Production reachable" below for this
stage's current status -- the worker wires it into the same production
lifecycle "Production activation" describes.

- **The success criterion is frozen at issuance.** Issuance already freezes
  resource identity, source/transport authority, approval provenance, and the
  exact package plan, and at that moment nothing has been mutated -- so it
  also copies the resource's current health contract *generation* (revision,
  fingerprint, and the complete canonical probe set) into immutable job-owned
  rows. **A resource with no declared contract cannot be issued a job**:
  absence is not health, so a job whose success criterion does not exist could
  never truthfully be called successful. Configuration remains bounded opaque
  data, but issuance now separately requires every stored probe to be
  structurally representable by the exact executor; a bare/pattern systemd
  target produces no job and cannot reach snapshot or package mutation.
- **One boundary decides which contract applies.** While the job is still
  pre-mutation, the live contract drifting away from the frozen copy makes the
  job stale and forbids the real package mutation, exactly as a changed
  package plan does. Because revisions are never reused, clearing and
  re-declaring byte-identical probes is correctly a NEW generation even though
  the fingerprint is unchanged. From `mutation_may_have_started` onward the
  check stops applying and the frozen copy is the only authority -- packages
  have already changed, and re-deciding success against a contract edited
  afterwards would be moving the goalposts.
- **PASS, FAIL, UNKNOWN are three different answers, and only a complete
  decisive set may ever be finalized.** A contract is an ALL-OF. PASS
  requires every frozen probe positively proven -- absence of an observed
  failure is not a pass. FAIL needs one probe positively proven false --
  inside a COMPLETE DECISIVE observation set, never beside one still
  UNKNOWN: the authority finalizer independently refuses ANY observation set
  containing an unresolved probe, defense in depth beside the orchestrator's
  own DECISIVE-round gate. A proven failure leaves the job ACTIVE with its
  snapshot and its rollback authority intact. UNKNOWN is never success and is
  never durable: nothing is written but a bounded event, and the evaluation
  may simply be repeated.
- **Exactly one legal success transition.** Schema v16 makes `succeeded`
  impossible without a proven mutation, a started evaluation, a durable
  completion, `health_outcome='passed'`, and the `health_completed`
  checkpoint -- and, in the other direction, makes a passing verdict
  inseparable from `succeeded`. Triggers make a passing verdict impossible
  unless every frozen probe carries its own durable `passed` result row, and a
  failing one impossible without a complete result set containing a proven
  failure. No package command exit code, proven mutation, reachable guest, or
  absence of observed failures can produce `SUCCEEDED` on its own.
- **Read-only, and that shapes the whole stage.** It runs `systemctl show`
  (or, for the built-in baseline, one fixed `/bin/true`) and changes nothing,
  so there is deliberately no host operation journal, no write-ahead
  uncertainty checkpoint, no lease, and no
  at-most-once submission fence -- inventing one would mimic the destructive
  stages without their reason for existing. What is kept is at-most-once
  *acceptance*: one definitive completion can commit, and write-once triggers
  stop a late result overwriting an accepted verdict or a rollback that moved
  the job on. A restart leaves a `health_started` job ACTIVE and fenced and
  never marks it succeeded; the evaluation is then simply run again.
- **Fixed argv, verified against the real tool** (systemd 257) rather than
  assumed. `systemctl is-active` expands globs and succeeds if ANY match is
  active, and `--` does not stop it, so it is not used; `systemctl show` plus
  a glob-free target charset plus an exactly-one-block rule is what names one
  unit (a glob can match exactly one, so the block rule alone is not enough),
  and an explicit unit-type suffix is required rather than guessed. Timeout
  and overflow are classified before the killed process's non-zero return
  code, and a generic non-zero is never a verdict merely because the command
  ran. systemd's `activating`/`deactivating`/`reloading`/pending-`Job` states
  are all UNKNOWN, never definitive failures (post-Human1 Stage 1: bounded
  health settling, ADVANCED contract only) -- each is a transient
  post-restart bookkeeping state, entered automatically, never a workload
  verdict. The built-in `guest_operational` baseline runs one fixed,
  argument-less `/bin/true` and is structurally independent of this settling
  machinery entirely (v0.5 health scope reduction; see "Job-bound healthcheck
  execution" below and `ARCHITECTURE.md`). Docker-specific package-update
  health probes (`docker_container_running`, `docker_container_healthy`)
  existed through an earlier iteration of this stage and were removed
  end-to-end.
- **Atomic final live-target proof.** The backend re-proves the exact
  resource/locator context before the host call and once as an early rejection
  after it; the helper's single guest dispatcher revalidates before every `pct
  exec`. The load-bearing proof is inside the same `BEGIN IMMEDIATE` that
  validates the complete observation set, aggregates it, inserts every result,
  and commits PASS/FAIL. A guest replaced in the former post-check/pre-commit
  gap yields UNKNOWN with zero result or verdict rows. The same acceptance
  boundary also independently enforces the shared probe-kind/outcome/reason
  semantic matrix rather than trusting the orchestrator to have done so.
- **No automatic compensation.** A failing verdict reports and stops. Health
  execution makes zero calls into the rollback host control and arms nothing,
  and there is no retry count, grace period, delayed-health policy, threshold,
  majority, or OR logic anywhere in the stage. Same-job rollback did gain two
  entry points (`health_started`, and `health_completed` with a failed
  verdict) so a job that needs compensating is not fenced out -- but an
  operator, not this stage, asks for it.
- **Production reachable** through the one worker, at `mutation_completed` or
  `health_started`. One wake performs at most one truthful attempt, and PR
  #73's deliberate absence of a retry policy ABOVE the health stage is
  preserved exactly. What one attempt means depends on the contract shape
  (v0.5 health scope reduction): for the built-in `guest_operational`
  baseline it is exactly ONE `/bin/true` execution, no sleep, no settling
  window at all (see "Major fix: baseline independence" below); for an
  explicit `systemd_unit_active` contract it remains the **bounded settling
  window** from post-Human1 Stage 1, up to 180 seconds, entirely inside one
  host round trip, observing the complete frozen probe set every 5 seconds in
  rounds batched into one `systemctl show` call, never one guest command per
  probe, until a decisive round (at least the second, completed within 15
  seconds, with no probe transient) reaches PASSED or FAILED, or the window
  and round bounds are exhausted. An unresolved evaluation leaves the job
  ACTIVE at `health_started`
  with its snapshot and rollback authority intact and the worker idle for it,
  now carrying (Stage 2) the last complete round's bounded per-probe evidence
  and settling metadata in its event history, and an operator asks again
  through the distinct `can_rerun_health_evaluation`-gated control (the
  generic `can_resume_update` is narrowed to exclude this checkpoint) rather
  than a timer doing it. A FAILED verdict leaves the job ACTIVE and
  rollback-capable and submits nothing. The health helper is deployed behind
  its OWN dedicated key and forced command and needs **no new PVE privilege**
  at all -- it reads through host-local `pct exec`, so the provisioned role
  stays exactly the audit-only pair. Human0 proved both a passing frozen
  contract and a deterministic failed frozen contract against real update
  jobs.
- **Post-Human1 Stage 1: bounded health settling closes the entire transient
  family, not only one state.** A real Human1 operator test approved a
  package plan that legitimately restarted the declared workload's runtime
  (at the time, Docker/containerd); every declared object was observed in a
  transient state at the instant health ran and settled back within seconds
  with no operator action in between, but the original classification
  durably recorded a FAILED verdict anyway. The immediate fix was UNKNOWN,
  not FAILED, for that one state; the frozen follow-up generalized it to the
  whole family (systemd's `activating`/`deactivating`/`reloading`/
  pending-`Job`) and gave the backend a bounded internal settling window (see
  above), for the advanced `systemd_unit_active` contract, so an ORDINARY
  restart resolves automatically within it instead of needing a manual
  re-run every time. See `ARCHITECTURE.md`, "Job-bound healthcheck
  execution".
- **v0.5 health scope reduction: Docker package-update health probes removed
  end-to-end, and the built-in baseline made structurally independent of
  settling.** `docker_container_running` and `docker_container_healthy` are
  no longer supported anywhere -- not hidden from Home Assistant, not an
  undocumented backend-only mode, removed from the domain model, the SQL
  schema (bump to v21), the execution-eligibility grammar, the deployed
  health helper, and every mirrored HA taxonomy. A confirmed review finding
  proved the ADVANCED settling rules above (`MAX_ROUND_SPAN_SECONDS`,
  `MIN_DECISIVE_ROUND`) were incorrectly applied to a `guest_operational`-only
  contract: a `/bin/true` that genuinely took longer than 15 seconds to exit
  `0` was discarded as non-decisive, and the job could get stuck ACTIVE at
  `health_started` for a workload that never failed. The built-in baseline is
  now a structurally SEPARATE one-shot code path
  (`_evaluate_guest_operational_once`) that the settling loop never runs at
  all: exactly one execution, no sleep, no second confirmation round, exit
  `0` is DECISIVE PASS immediately however long it took, and anything else is
  UNKNOWN after that one attempt. A second confirmed finding proved the
  durable finalizer could combine a FAILED probe with a still-UNKNOWN sibling
  into a durable FAILED verdict from a non-decisive round;
  `InventoryAuthority.complete_package_update_health` and Home Assistant's
  own job-view validation now both independently refuse ANY observation set
  containing an UNKNOWN probe before finalizing one, as defense in depth
  beside the orchestrator's own DECISIVE-round gate. The two contract shapes
  (the baseline singleton, or one-or-more `systemd_unit_active` probes) are
  enforced as mutually exclusive at every layer: domain validation, SQL
  triggers, the backend HTTP API, and Home Assistant's own validation. See
  `ARCHITECTURE.md`, "The `guest_operational` baseline is independent of
  settling", and `PRODUCT.md`, "Exactly two supported contract shapes".
- **Post-Human1 Stage 2: an unresolved health evaluation is actionable, and
  truthfully re-runnable.** The job readback's `health.evidence` is now
  `null`, `"observation"` (bounded per-probe evidence from an unresolved
  evaluation -- never a verdict, never `definitive`), or `"verdict"` (a
  durable, non-recheckable PASSED/FAILED result, `definitive: true`). The
  `can_resume_update` capability excludes `health_started`; the new
  `can_rerun_health_evaluation` capability is true exactly there, rendered by
  Home Assistant as a distinctly labelled **Re-run health evaluation**
  control that calls the same `/resume` liveness entrypoint. A definitive
  FAILED verdict still offers neither control and remains rollback-capable
  and non-recheckable.
- **Post-Human1 addition: per-probe health evidence is readable from Home
  Assistant.** The explicit job readback (`GET .../package-update`, the
  `start_update`/`resume_update`/`rollback_update`/`view_update_job`
  responses, and the native `view_update_job` HA action/notification) now
  include each probe's `kind`, `target`, `outcome`, `checked_at`, bounded
  `reason` token, and (Stage 2) a `definitive` flag -- once a definitive
  verdict exists (`evidence == "verdict"`, `definitive: true`), or, now, once
  an unresolved evaluation's bounded observation evidence exists
  (`evidence == "observation"`, `definitive: false`). A real operator had to
  read the authority SQLite database directly to learn that three probes had
  failed, or which probe an unresolved evaluation was still waiting on; this
  closes both gaps without exposing raw helper stdout/stderr, command text, or
  unbounded attributes -- every field is a durable, typed, bounded authority
  fact already computed by this stage.

## Production activation (implemented)

The operator-triggered update lifecycle is production reachable.
`ARCHITECTURE.md`, "Production update activation", is how it is built.

- **One way in.** `issue_package_update_job` has exactly one production
  caller: `POST /r0/v1/resources/{resource_id}/package-update`. The caller
  supplies a `request_id` and nothing else -- `extra="forbid"` makes a body
  naming a VMID, node, package, version, architecture, plan fingerprint,
  snapshot, probe, contract revision, command, argv, host, or helper operation
  a 422 rather than a field quietly ignored. The backend resolves the
  resource's own current durable approval. The 202 means a durable job already
  exists; a crash after it never fabricates a success.
- **One worker, composing existing stages.** `app/package_update_worker.py`
  owns one thread, contains no state machine of its own, and performs no
  authority transition: it re-reads the durable job before every action and
  runs the one stage that checkpoint calls for. It is wake-driven with no
  timeout -- with nothing to do it blocks rather than polling -- and only an
  explicit operator action or shutdown wakes it. Durable global single-flight
  remains the concurrency authority; the in-process cycle lock only stops one
  worker running two cycles at once.
- **NO AUTO-UPDATE, structurally.** Neither scheduler issues a job, no scan
  callback does, the approval write does not, the Home Assistant coordinator
  does not, and the worker cannot. Continuing a job an operator started after
  a restart is recovery, not auto-update.
- **NO AUTO-ROLLBACK, structurally.** `arm_package_update_rollback` has
  exactly one production caller: the authenticated rollback route. A failed
  mutation, an unproven mutation, a FAILED health verdict, and an UNKNOWN one
  each leave the job ACTIVE and rollback-CAPABLE with zero submissions.
- **No retry policy.** One wake, one attempt. No interval, backoff, grace
  period, attempt count, or threshold anywhere in the composition. Production
  liveness for a transient uncertainty is the explicit `resume_update`
  control, which re-reads the durable checkpoint and invokes only the existing
  safe continuation semantics -- never "submit the destructive command again".
- **Restart safety.** Authority startup recovery runs first and terminalizes
  only provably pre-mutation jobs; the worker then re-observes the durable
  uncertain states through the stage that owns each. No duplicate destructive
  submission, and no status becomes `SUCCEEDED` because a process came back.
- **Five separate privilege boundaries.** Snapshot, plan simulation, mutation,
  rollback, and health each get their own root-owned forced-command helper and
  their own dedicated key, because the key is what selects which command a
  connection may run. The three destructive ones keep root-only operation
  journals. The scan boundary is a sixth, separate, unchanged one.
- **No PVE API privilege was broadened.** Every mutation runs host-local, so
  the provisioned role stays exactly `Sys.Audit,VM.Audit` and `VM.Snapshot`
  appears in no deployment script.
- **Product update and workload update are mutually exclusive.** The Phase U2
  active-job read is a courtesy that refuses early and avoids pointless
  staging; it is not the invariant, because an operator may legitimately start
  an update between that answer and the first mutation. Immediately before its
  mutation window the updater takes an exclusive maintenance fence, and the
  backend acquires it inside the same `BEGIN IMMEDIATE` writer lock
  `issue_package_update_job` takes -- so exactly one of the two can win, with
  no check-then-act gap. The fence is a durable file beside the authority
  database, so it survives the backend restart the product update performs and
  keeps refusing workload starts throughout Phase U5 acceptance. It is
  released only at a terminal point: a proven successful update, or a proven
  complete rollback/recovery, and only when the fence's own recorded holder is
  this run -- which is also what makes a crash around acquisition recoverable
  rather than orphaning it. A pre-activation installation, whose backend has
  no fence route to ask, gets the same durable fence written directly before
  the mutation window, so the activated target backend refuses workload starts
  from the moment it comes up. There is no bypass flag, and a fence another
  product update holds is never stolen or removed.
- **This activation itself changes no schema.** The durable job is the
  execution queue and the recovery authority; a worker wakeup is an in-memory
  hint and needed no second durable queue. Making the lifecycle reachable
  caused no authority migration and no reset on its own. The current authority
  schema is v20; see "Implemented" and "Known limitations" below.
- **Human0: COMPLETE / PASS.** See "Human0 production lifecycle" above.

## Product stages

### Current — Human1 Home Assistant operator controls

- Existing dynamic LXC devices now carry eight explicit buttons: review the
  exact plan, approve the reviewed plan, start an approved update, view the
  latest job, resume an active job, re-run an unresolved health evaluation,
  request same-job rollback, and view the health contract.
- Exact package rows, contract probes, and bounded job facts/events are shown
  in on-demand persistent notifications rather than stored as large entity
  attributes. Sensors expose concise state, scan, pending-count, approval,
  job, checkpoint, package-count, and health-result facts; one binary sensor
  exposes rollback availability.
- The backend publishes conservative read-only operator availability through a
  separate authenticated endpoint. HA aligns it to the immutable revisioned
  snapshot by backend identity, authority revision, and exact resource set.
  Runtime activation and the filesystem product-update fence can therefore
  change button availability without producing two different snapshots with
  the same `published_state_revision`. These presentation facts grant no
  authority: every mutation endpoint independently revalidates its full rule.
- Plan review memory is deliberately ephemeral HA UX state. Approval performs
  a fresh read and requires the exact backend identity, resource ID, scan run,
  and material fingerprint previously reviewed. A changed plan, a new scan run
  with identical material, a zero plan, or an HA reload requires review again.
- The existing actions remain as response-capable diagnostic/configuration
  interfaces. `approve_update_plan` now also requires the current-runtime
  reviewed reference, closing the former blind caller-supplied approval path.
- The authority schema remains v19. Operator availability is transient
  presentation data and richer job summaries are revisioned publication facts;
  no new durable authority state was needed.
- **Post-Human1 live-defect remediation.** A real operator test exposed two
  further HA-facing gaps, both closed without new durable authority: (1) an
  operator who reviews and approves a plan but has not yet declared a health
  contract now sees a native Home Assistant Repair (Settings → Repairs)
  naming the existing `set_health_contract` action, instead of a silent dead
  end after a disabled Start button; (2) the explicit job readback
  (`view_update_job`, and the response of `start_update`/`resume_update`/
  `rollback_update`) now includes each frozen probe's kind, target, outcome,
  checked-at time, and bounded reason token once a definitive verdict exists,
  so a FAILED or UNKNOWN health result is answerable from Home Assistant
  without shell/SQLite access. Neither change stores new durable HA state or
  lets Home Assistant choose a health contract on the operator's behalf.

- **Post-Human1 Stage 1+2: bounded health settling and actionable UNKNOWN
  evidence.** Closes the live-defect family the item above only partially
  closed: `deploy/hubinet-package-health-helper.py` runs (at the time,
  historically, for both Docker and systemd probes; Docker-specific probes
  were later removed end-to-end -- see "v0.5 health scope reduction" above) a
  bounded internal settling window (up to 180s, batched per-round
  observation, decisive-round rules) inside its one host round trip, so an
  ORDINARY restart after a package update settles automatically instead of
  durably failing or needing a manual re-run every few seconds. Every
  transient state (not only `starting`) is UNKNOWN, never a definitive
  failure. An unresolved evaluation persists
  bounded per-probe observation evidence and settling metadata in the job's
  event history (`health.evidence == "observation"`, `definitive: false`,
  distinct from a durable verdict's `definitive: true`), and the new
  `can_rerun_health_evaluation` capability (narrowed out of the generic
  `can_resume_update`) renders a truthfully distinct **Re-run health
  evaluation** control. The authority schema remains v19; no new durable
  authority state was needed for either stage. See `ARCHITECTURE.md`,
  "Job-bound healthcheck execution".

- **Post-Human1 PR #80 review remediation, schema v20, backend discovery,
  and native onboarding.** A code review of Stage 1+2 found five concrete
  gaps in the bounded-settling stage, each closed structurally: an explicit
  typed `evaluation_status` (`decisive`/`unresolved`) now travels on the wire
  and is checked *before* aggregation, never re-inferred from probe outcomes;
  the 180s settling window is now one real absolute monotonic deadline
  enforced before every round/sleep/command, with every per-command timeout
  clamped to the remaining budget; a structural target problem (a bad
  target, an ambiguous pattern) now returns immediately, never waiting out
  the window; Home Assistant independently proves kind/outcome/reason and
  verdict/probe-set coherence with exact JSON-type checks (closing a
  `bool("false") is True` coercion trap), not just bounded-set membership;
  and the backend now states its own timing policy
  (`settling_policy: {deadline_seconds, observation_interval_seconds}`) on
  the wire, with the helper validating and clamping against its own hard
  ceilings rather than trusting Home Assistant to supply correct values —
  Home Assistant never states or overrides this policy.

  **The authority schema is v20** (bumped from v19, a normal pre-release
  reset per `AGENTS.md`): a fourth probe kind, `guest_operational`, with no
  target at all (`CHECK`-enforced nullable `target`, a partial unique index
  permitting at most one per contract). It proves guest liveness
  (`/bin/true`, fixed, no operator input), never application health, and
  structurally can only ever PASS or UNKNOWN, never FAIL.

  **`guest_operational` is the v0.5 built-in default health criterion for a
  package-managed LXC.** The backend provisions it inside the
  successful-reconciliation transaction for every current managed LXC with no
  contract of its own, so an approved update can start with no separate
  health-onboarding step. It is a product decision, not an observation:
  nothing reads the guest to create it, an operator's explicit contract is
  never overwritten by it, and repeated reconciliation is idempotent.
  `POST .../health-contract/reset` (and the `reset_health_contract` action, or
  the Options flow) restores that baseline under the usual compare-and-set
  discipline; `DELETE` remains the low-level clear.

  **v0.5 does not automatically discover or recommend `systemd_unit_active`
  application health probes.** Absence of a workload observer is not proof of
  workload absence, so v0.5 does not infer workload health automatically. The
  candidate-discovery route, DTOs, adapter-presence oracle, recommendation
  ranking, and the HA discovery flow were removed rather than left as a dead
  architecture. `systemd_unit_active` remains fully supported as explicit
  advanced operator configuration, executed by the same job-bound helper as
  before; its absence or failure has no influence on the default path. (At
  this point in the product's history Docker probes were still supported the
  same way; "v0.5 health scope reduction" above records their later,
  complete removal.)

  **An unresolved health evaluation now publishes WHY, not only that it is
  unresolved.** Independent review of the pivot found that the reason was
  computed, bounded, and durably recorded — and then lost before Home
  Assistant. With `guest_operational` as the default contract, PVE proving
  the exact current LXC STOPPED is the ORDINARY post-update failure: the
  helper refuses the whole request before any probe round, so the job
  truthfully has no verdict, no evidence kind, and no probe rows. Those
  three absences were all an operator saw; the actual classification
  (`guest_unavailable`) lived only in the durable event's `details`, which
  the HA transport does not carry. `GET .../package-update` now publishes
  `health.reason` — exactly one token from the closed UNKNOWN taxonomy, from
  the LATEST attempt only, never merged, and retired the moment a durable
  verdict exists. Home Assistant re-proves the taxonomy itself rather than
  trusting the string, refuses a reason published beside a definitive
  verdict, and renders a fixed EN/PL description of the token — never
  backend prose, never helper output. Nothing else about the event reaches
  the wire. Rollback is unaffected and was reverified: `health_started` stays
  rollback-eligible and `_post_mutation_job_context_is_current` still imposes
  no running-status requirement, so a dead guest keeps its explicit same-job
  recovery path. Still no auto-rollback, and still no new Repair family.

  **The `health_contract_unconfigured` Repair is a non-fixable safety net.**
  The backend default normally makes its premise unreachable; when it does
  occur it names the two explicit remedies (Options → reset, or
  `set_health_contract`) and clears itself once a contract exists. See
  `ARCHITECTURE.md`, "The built-in `guest_operational` default" and "Native HA
  health maintenance".

### Next — Human1 follow-ons deliberately deferred

- Typed CT start/stop/restart and manual-snapshot operations were not added in
  this slice. They require new dedicated backend operations and privileged
  forced-command boundaries; combining that deployment/host-control work with
  the package operator surface would make the boundary harder to review.
  Manual snapshots must remain distinct from job-owned rollback authority.
- A dedicated operator-facing bearer-token handoff/retrieval path remains to
  be designed. The token must not enter ordinary logs, diagnostics, or the
  published snapshot.

### Later — snapshot retention

Define and implement retention only for Hubinet-owned snapshots. Manual and
external snapshots remain untouched.

### Later first-class stage — supported uninstall

Provide one supported reverse-installation flow rather than ad-hoc shell
snippets. It must deliberately cover the backend CT/application, host helpers,
forced-command boundaries, Hubinet-created PVE API user/token/ACL/role bindings,
Hubinet-owned firewall rules, Home Assistant integration/config entry/entities,
and Hubinet runtime/config state.

The stage must explicitly choose and document how evidence and snapshots are
handled — for example, preserved by default with a separate explicit purge.
This is ordinary lifecycle removal in the trusted administrator environment,
not attestation or defense against an omnipotent PVE root.

## Known limitations

- A manual out-of-band rollback by `root@PVE` can make an older package scan
  appear current until a new scan observes the guest. This is a robustness
  backlog item, not a release blocker: PVE root is outside Hubinet's trust
  boundary, and no filesystem attestation or cryptographic continuity system
  is planned for it.
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
- Pre-release: schema v21 is incompatible with v20 and every earlier version,
  and there is no in-place migration path. Schema v17 added the durable
  per-resource `issuance_sequence` package-update jobs now use for latest-job
  ordering, and schema v18 adds the durable post-success package-scan request
  and constrained scan link. Schema v19 adds durable one-shot-on-success
  approval consumption and the unique successful-job-per-approval fence.
  Schema v20 adds the nullable-target `guest_operational` probe kind and its
  partial unique index. Schema v21 (v0.5 health scope reduction) removes the
  two Docker health-probe kinds and adds the mixed-baseline-contract triggers
  (see "Implemented" above), so an existing schema-v20 (or earlier)
  installation is incompatible. An existing installation now uses
  `deploy/update-proxmox-0.5.sh` for
  this: it detects the incompatible authority schema, backs it up, and resets
  only the authority database (see "In-place product updates" below) while
  preserving the LXC, its VMID/network, PVE identity/token, and every other
  credential/config file. Home Assistant re-enrollment is required only after
  that explicit reset, not for an ordinary code-only update.
- Package origin, description, security classification, and reboot-required
  stay unknown unless reliable evidence is present. The first parser derives
  origin/security from stable-English APT simulation evidence and leaves
  descriptions unknown.
- PVE sshd must permit public-key login for the forced root authorization.
  Bootstrap verifies the boundary before starting Hubinet and never rewrites
  operator sshd configuration.
