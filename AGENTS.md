# Hubinet Ops Agent Rules

These rules apply to every coding agent working in this repository.

## Architecture source of truth

Read and follow these documents before architecture or implementation work:

- `docs/architecture/0.5-foundation.md`
- `docs/architecture/0.5-inventory-model.md`
- `docs/architecture/adr/0001-resource-identity-incarnation.md`
- `docs/architecture/adr/0002-proxmox-discovery-reconciliation.md`

ACCEPTED ADRs are the normative architecture contract. Do not weaken or bypass
their fail-closed invariants without an explicit architecture change and review.
If implementation conflicts with an accepted ADR, stop and report the conflict;
do not silently adapt the architecture. Do not change architecture merely to
make a test pass.

ADR acceptance does not mean the Phase 0 Amendment is implemented. Respect the
documented phase gates and prerequisites before starting later-phase work.

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
- Security-sensitive identity, binding, revision, freshness, provenance, and
  trust state must not be reconstructed optimistically after gaps, failures,
  races, or restart. Missing or ambiguous evidence removes authority.
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
- Keep the Proxmox SSH account behind `deploy/pve/hubinet-ops-host`; do not provide
  a general-purpose shell. Validate every typed action and all identity, binding,
  revision, VMID, and optional snapshot arguments at the backend and independent
  hostd/forced-command boundary.
- Keep bearer authentication on every `/api/v1` endpoint. Never commit API tokens,
  MQTT passwords, webhook IDs, SSH keys, production addresses, runtime databases,
  or logs. Never log authorization headers, bearer tokens, MQTT passwords,
  private keys, or webhook identifiers.
- Updates require explicit backend policy and manual plan approval. Automatic
  rollback additionally requires an existing eligible snapshot and explicit
  `automatic_rollback` policy for the exact resource incarnation.
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
