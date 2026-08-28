# Hubinet Ops Agent Rules

These rules apply to every coding agent working in this repository.

## Start here

- `docs/architecture/README.md` — documentation index, authority hierarchy,
  **default reading set**, task-to-document matrix, and archive policy. Read it
  before opening any other architecture document.
- `docs/product-intent.md` — the current product target and its hard rules.
- `docs/architecture/0.5-implementation-status.md` — what is actually
  implemented today.

Read the minimum current material required for your task. Do not recursively
read all architecture and research documents. Material under `docs/archive/` is
historical, is authority for nothing, and must not be read by default or treated
as a roadmap.

## Current product intent (binding summary)

The full statement is `docs/product-intent.md`. The rules below are binding and
are not repeated in full here:

```text
STATIC 0.4.x -> PVE AUTODISCOVERY -> DYNAMIC BACKEND INVENTORY
  -> DYNAMIC HOME ASSISTANT RESOURCES / UI -> SAFE OPERATOR-DRIVEN UPDATE WORKFLOW
```

- PVE autodiscovery and dynamic inventory: adding or removing a guest in Proxmox
  must never require a repository or config change.
- Dynamic Home Assistant representation follows the accepted lifecycle/
  reconciliation model. A failed, partial, or unavailable scan is never deletion.
- Automatic **read-only** update/package scanning is allowed; the operator is to
  be shown exact package/update information (name, installed version, candidate
  version, origin, description, and further metadata — including security
  classification and reboot-required only where reliably establishable).
- **NO AUTO-UPDATE.** Package installation always requires explicit operator
  review and approval of the current update plan.
- A material change to the plan after approval invalidates that approval: fail
  closed and require approval of the new plan.
- Every update run takes a fresh, job-owned pre-update snapshot; a job may roll
  back only to **its own** snapshot; healthcheck then same-job controlled
  rollback.
- Automatic cleanup applies only to Hubinet-managed snapshots. Manual/operator
  snapshots are never touched by generic Hubinet retention.
- A failed, partial, or unavailable package scan is **never** "zero updates".

**What is implemented today:** PVE autodiscovery, dynamic backend inventory, and
the dynamic Home Assistant representation — all strictly read-only. Package
scanning, update plans, jobs, snapshots, healthchecks, and rollback are **not
implemented yet**; the rules above are what they must satisfy when they are
built. Building them does **not** require a new ADR first (see "When to write an
ADR" below).

## Threat model

Hubinet Ops is a practical operations application for a **trusted,
self-administered** Proxmox environment.

**TRUSTED:**

- the Proxmox administrator/root;
- the Proxmox host;
- root inside a managed LXC;
- the Hubinet operator;
- normal `apt`/`dpkg` behavior.

**OUT OF SCOPE — do not design defenses for these:**

- a malicious root inside a managed guest deliberately racing or modifying files
  to fool Hubinet;
- a malicious or compromised Proxmox root;
- an administrator deliberately replacing Hubinet-owned state;
- security proofs intended to survive full administrative compromise of the
  managed environment.

**This does not mean weakening ordinary application safety.** These are KEPT and
are binding:

- least-privilege PVE credentials;
- TLS verification;
- secret redaction — no secrets in argv or logs;
- fixed, typed, allowlisted operations, never arbitrary shell command text;
- correct target/VMID validation;
- a failed or unavailable discovery is never resource deletion;
- a failed package scan is never zero updates;
- concurrency protection where it prevents ordinary operational races;
- update-plan revalidation before an approved future update;
- job-owned snapshots and same-job rollback semantics;
- protection of non-Hubinet snapshots;
- **NO AUTO-UPDATE.**

The earlier security-proof architecture written for a hostile-administrator
model — source attestation, attestation epochs, relationship gates,
candidate-endpoint attestation proofs, dual-evidence confirmed removal, and
persistent workload-incarnation proof ("Blocker B") — is **superseded**. Its
ADRs and evidence are archived under
`docs/archive/superseded-security-model/`; its code, schema, and tests have been
removed. Do not reintroduce those concepts, and do not treat Blocker B as a
prerequisite for the operator-driven update roadmap.

## Architecture source of truth

Read and follow these documents before architecture or implementation work:

- `docs/architecture/0.5-foundation.md`
- `docs/architecture/0.5-inventory-model.md`
- `docs/architecture/adr/0001-resource-identity-incarnation.md`
- `docs/architecture/adr/0002-proxmox-discovery-reconciliation.md`

ADR 0001 and ADR 0002 are the only ACCEPTED ADRs. ADRs 0003-0006 are SUPERSEDED
and archived — see the table in `docs/architecture/README.md`.

### When to write an ADR

An ADR records a genuinely architectural, hard-to-reverse decision: a new
persistence/authority owner, a new trust boundary, a new external mutation path.

**ADR creation is not a prerequisite for ordinary implementation.** Package
scanning, update plans, jobs, snapshots, healthchecks, and rollback do not need
a new ADR merely because they touch a managed guest — they proceed under the
accepted inventory architecture, this threat model, and the product rules above.

ACCEPTED ADRs are the normative architecture contract. Do not weaken or bypass
their fail-closed invariants without an explicit architecture change and review.
If implementation conflicts with an accepted ADR, stop and report the conflict;
do not silently adapt the architecture. Do not change architecture merely to
make a test pass.

ADR acceptance does not mean the Phase 0 Amendment is implemented. Respect the
documented distinction between implementation staging and runtime activation
when starting later-phase work. Phase 1 components may be developed
incrementally only where the architecture explicitly keeps them dormant. Before
the complete Phase 1 runtime activation gate is satisfied, agents must not wire
a partial Phase 1 subsystem into application startup, production provider I/O,
HTTP or Home Assistant runtime, scheduling/background work, mutation authority,
or any legacy 0.4 dependency, fallback, or dual-write path.

## Code Review Rules

- Review the exact current diff against `docs/architecture/0.5-implementation-status.md`
  and the relevant ACCEPTED architecture sources. Green tests are evidence, not
  proof that a contract is correct.
- A P1/P2 finding must identify a concrete witness or failure mode and the exact
  accepted contract, security boundary, or runtime behavior it violates. A new
  hardening idea that is not required by accepted architecture is a NEW
  architectural decision or non-blocking follow-up, not an invented blocker.
- Respect the Phase 0 / Phase 1 boundary. Do not require SQLite, CAS, durable run
  history, persistent binding history, or other backend-owned Phase 1 guarantees
  from the Phase 0 Home Assistant validator.
- Home Assistant polling may skip arbitrary published revisions. Never infer
  numeric `N -> N+1` adjacency, hidden intermediate state, or backend transaction
  adjacency from two snapshots observed by HA.
- When one concrete defect is found, inspect only the bounded same-family surface
  needed to determine whether it is isolated or systemic. Once that exact family
  is closed by code and regression evidence, stop; do not restart a broad audit
  or expand into unrelated parser/schema/general hardening without new evidence.
- A targeted fix review must remain targeted. Re-open previously closed review
  scope only when the new diff or a concrete new witness materially affects it.
- Preserve existing regression tests and fail-closed behavior. Do not weaken a
  test or accepted invariant merely to make a proposed fix pass.
- For detailed repository review procedure, use
  `.agents/skills/hubinet-contract-review/SKILL.md`; skills are procedures and do
  not override accepted ADRs or this repository-wide review boundary.
- Acceptance is a ratchet, not immunity. Committed artifacts are immutable
  historical facts and are never rewritten to look unaccepted; an accepted
  document may still be explicitly superseded or revoked when a concrete later
  witness falsifies a load-bearing claim, at which point dependent work stops and
  reassesses and the superseded material is clearly indexed as historical. The
  full rule is `docs/architecture/README.md` section 7.

## Authority, identity, and discovery

- The backend and SQLite are authoritative for inventory identity, policy,
  operation state, revisions, trust, and audit. Home Assistant is a presentation
  and controlled-input frontend, never an authority.
- Autodiscovery records read-only facts and evidence. It never grants management,
  update, maintenance, rollback, restore, or other destructive authority.
- `discovered != observed != managed != maintenance`. No state automatically
  promotes to the next, and every capability or permission is fail-closed.
- A Proxmox VMID is a reusable slot locator, not durable resource identity.
  A `resource_id` is an opaque backend UUID for an inventory incarnation, not a
  value derived from VMID, name, type, or node. Resource identity, presence,
  binding generations, incarnation continuity, and terminal history must follow
  accepted ADR 0001 and ADR 0002. Never key retained inventory, policy, HA
  devices, or operations by VMID alone.
- Identity, binding, revision, freshness, and provenance state must not be
  reconstructed optimistically after gaps, failures, races, or restart. Missing
  or ambiguous evidence fails closed rather than being guessed.
- Discovery must not create or copy management/update policy, permissions,
  approvals, plans, jobs, or locks. Inventory selection and policy belong to the
  backend; do not encode CT-specific/manual profiles or hardcoded VMIDs as
  lasting identity or authority.
- MQTT Discovery is not the foundation inventory/UI contract. If MQTT remains in
  use, treat it as optional telemetry/discovery presentation, never authority.

## Mutation and security boundaries

### Normal 0.5 mutation path

Every normal Hubinet Ops 0.5 mutation must retain the complete authoritative
path:

```text
Home Assistant
-> Hubinet Ops API
-> backend policy/plans/jobs/locks/audit
-> typed host-control
-> hostd/forced-command
-> Proxmox
```

- Home Assistant must never communicate directly with Proxmox for mutation and
  must never supply shell commands, host routes, policy, or capabilities.
- Notifications are presentation/navigation only; they never approve or trigger
  an operation.
- Never add an API, MQTT topic, SSH path, host-control operation, or wrapper action
  that accepts arbitrary command text. Keep host-control operations typed and
  allowlisted.
- Keep the Proxmox SSH account behind a typed, allowlisted forced-command wrapper; do
  not provide a general-purpose shell. (The retired legacy 0.4 tree's
  `deploy/pve/hubinet-ops-host` was this wrapper; that path no longer exists in the
  current 0.5-only tree, and its historical source is available through Git
  history/tags. A future Phase 1C mutation path requires an equivalent typed wrapper,
  not a reuse of the deleted file.) Validate every typed action and all identity, binding,
  revision, VMID, and optional snapshot arguments at the backend and independent
  hostd/forced-command boundary.
- Keep bearer authentication on every `/api/v1` and `/r0/v1` endpoint. Never commit API tokens,
  MQTT passwords, webhook IDs, SSH keys, production addresses, runtime databases,
  or logs. Never log authorization headers, bearer tokens, MQTT passwords,
  private keys, or webhook identifiers.
- **NO AUTO-UPDATE.** Package/update *scanning* may run automatically, but only
  read-only; *installing* updates requires explicit backend policy and explicit
  operator review and approval of the current update plan. A material change to
  the plan after approval invalidates that approval — fail closed and require
  approval of the new plan. Never treat "N updates available" as permission to
  install them. Automatic rollback additionally requires an existing eligible
  snapshot and explicit `automatic_rollback` policy for the exact resource
  incarnation.
- A pre-update snapshot is created by, owned by, and named for the job that
  creates it, on the actual current PVE guest; a job may roll back only to its
  own recovery snapshot, never to an arbitrary earlier one. Generic Hubinet
  snapshot retention/cleanup applies only to Hubinet-managed snapshots and never
  deletes a manually created or operator-owned snapshot.
- Manual update rollback requires `manual_rollback_allowed` plus a failed,
  blocked, or interrupted update operation and its recorded eligible snapshot.
- Normal explicit snapshot restore remains backend-authoritative and requires all
  of these fail-closed gates:
  - `manual_snapshot_restore_allowed`;
  - the exact backend capability `snapshot_rollback`;
  - an existing snapshot that is both rollback-eligible and Hubinet-owned;
  - explicit Home Assistant/operator confirmation;
  - no waiting backend plan of any kind and no approved backend plan of any kind;
  - no active global destructive job;
  - an atomic backend recheck of `manual_snapshot_restore_allowed`,
    `snapshot_rollback`, snapshot existence, rollback eligibility, Hubinet
    ownership, explicit confirmation, absence of every waiting backend plan,
    absence of every approved backend plan, and absence of any active global
    destructive job while inserting the local restore job, before the first
    hostd POST;
  - independent PVE/host snapshot-restore policy enforcement; and
  - where applicable under 0.5, exact `resource_id`/incarnation, active binding,
    identity/binding revisions, trust, and freshness checks in addition to every
    gate above. A current VMID may be only the validated execution locator/context
    for the host policy and operation, never durable identity.
  None of these gates is optional, and normal restore is never an automatic
  fallback.

### Transitional legacy 0.4 break-glass exception

- The existing offline recovery path for the backend-hosting CT110 is a narrow,
  transitional legacy 0.4 exception because the backend itself may be stopped or
  damaged. It may bypass the normal backend path, including while the backend is
  unavailable, only through its dedicated recovery credential and only for the
  existing typed, allowlisted direct hostd offline force-stop and snapshot-restore
  operations. It never accepts arbitrary command text and is not an automatic
  fallback or a path to extend normal mutations.
- A successful offline recovery must leave a durable recovery event on PVE and
  require post-start backend reconciliation before acknowledgement. The restored
  backend deliberately supersedes waiting plans, marks approved plans recovered,
  and interrupts restored queued or running jobs according to the existing
  recovery contract.
- CT110 and VMID 110 name the current legacy deployment target only. They are not
  normative identity or permanent constants in the Hubinet Ops 0.5 architecture.
  Future 0.5 self-host/offline recovery requires a separately designed durable
  recovery-target binding whose identity is not based on a hardcoded VMID. A
  current VMID may be only an execution locator/context after that target has
  been independently bound. The final break-glass contract requires a separate
  later architecture decision; do not design or implement it opportunistically.

## Test boundaries

- Test failure, race, restart, fencing, idempotency, and fail-closed paths, not
  only happy paths. Tests must enforce the architecture rather than redefine it.
- Do not contact real Proxmox, LXC, Docker, Home Assistant, MQTT, or private-network endpoints.
- Use fake executors, fake MQTT clients, fake clocks, temporary directories, and simulated process output.
- Do not run real `apt`, `pct`, `ssh`, `systemctl`, deployment operations, or private-network connections as part of repository tests. Docker may be used only by the controlled sandbox manager on an ephemeral GitHub-hosted CI runner with the workflow-owned `HUBINET_OPS_EPHEMERAL_CI=1` marker; it is unavailable to the production script inside the sandbox.
- A deployment script may be executed only inside the repository's system-enforced smoke sandbox on an ephemeral CI runner. The sandbox is the security boundary: it must have no network, host PID namespace, capabilities, privileges, host sockets, host secrets, or writable host filesystem; it runs as a non-root user with a read-only repository/root filesystem and only one-shot writable workspace and `/tmp`. The production script must never execute directly on the pytest host; local Linux does not invoke the manager, while a marked Linux CI run must fail closed if its marker or sandbox is unavailable.
- Inside that sandbox, the hermetic smoke harness must set `HUBINET_OPS_TEST_MODE=1`, use an isolated `PATH` without inheriting the host `PATH`, replace every privileged or external command through a temporary fake command layer, and add only an explicit allowlist of unprivileged local tools. The isolated command layer contains no real network, deployment, container, or hypervisor programs. It must redirect PVE, archive, backup, mount, and runtime paths into the one-shot workspace, use no production addresses or credentials, and fail if any real lifecycle or snapshot mutation escapes the fakes.
- The static shell validator is defense-in-depth, not the execution boundary. It must fail closed on host `PATH` access, explicit or lexically shell-assembled standard absolute executable paths (including non-canonical spellings), `command -p`, and explicit or assembled Bash `/dev/tcp` or `/dev/udp` networking before the sandbox executes a production deployment script.
- The validator is a conservative lexical scanner, not a Bash parser: it never executes shell text and does not interpret parameter expansion, command substitution, arithmetic expansion, or other arbitrary dynamic expansion. Those constructs are not a security boundary; even unknown syntax remains confined by the system-enforced sandbox.
- The deployment script must not read or modify `PATH` or reference executables under `/bin`, `/sbin`, `/usr/bin`, or `/usr/sbin`; this boundary is deliberately fail-closed, with the exact first-line `#!/usr/bin/env bash` shebang as its only exception.
- Run Python compilation, pytest, `bash -n`, YAML parsing, and tracked-runtime-file checks before publishing runtime changes. For docs/instructions-only changes, run the relevant scoped validation and `git diff --check`.
