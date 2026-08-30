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
database (marker `hubinet_ops_0_5_authority`, schema v8). Schema v8 adds
immutable source-context issuance facts to package scans and one durable exact
plan approval fact per resource. There is no migration from v7; pre-release
installs recreate the authority database and require Home Assistant
re-enrollment.

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
  -> activate in one fixed order (app, venv+requirements if changed, unit
     if changed, PVE helper same-path content swap if changed, authority
     preserve-or-reset), retaining rollback material
  -> start + accept (reused bootstrap discovery-acceptance contract,
     extended with an optional minimum-committed-sequence floor to prove a
     genuine post-restart cycle; host-control forced-boundary re-probe;
     firewall byte-identical + active)
  -> on any failure after the service was stopped: coherent rollback,
     including a validated authority-database backup restore when a
     destructive reset had already happened
```

**Authority schema decision.** `deploy/lib/hubinet-ops-authority-tool.py`
(run inside the CT) is a small, read-only-first inspector: it reads
`authority_schema`'s marker/version and `backend_instance.backend_instance_id`
and reports whether the database is recognizable — never a security proof,
just enough to classify. The target schema is read statically (a regex over
the target commit's `app/inventory/store.py` text, never an executed import)
so the updater never has to run target application code to plan. Same
schema: preserve, untouched. Different schema: no migration exists in this
product's current scope (see `AGENTS.md`) — a coherent backup
(`sqlite3`'s stdlib online backup API, integrity-checked and re-validated
against the pre-reset identity before the live file is removed) followed by
removal; the target runtime creates its own fresh schema on next start. The
updater never writes authority schema DDL itself.

**PVE host helper update contract.** The forced-command `authorized_keys`
line, the pinned host-control key, and `known_hosts` are never touched.
Only the helper file's *content* is ever replaced, staged as a temp file
in the same directory and atomically renamed over the existing path — same
path, new content, exactly like `deploy/hubinet-package-scan-helper.py`'s
own request/response version check already expects.

**Rollback.** Filesystem rollback material
(`app.rollback-<runid>`, `.venv.rollback-<runid>`, a preserved copy of the
systemd unit, a preserved copy of the PVE helper) is retained until
acceptance succeeds, using the same ledger mechanism
(`deploy/lib/bootstrap-common.sh`) bootstrap's own rollback already relies
on. A target failure after a destructive authority reset restores the
coherent *old* installation, database included — the updater never leaves
old code paired with a new, incompatible schema.

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

Update execution, job-owned snapshots, healthchecks, rollback, lifecycle
mutation, and QEMU package execution remain future work.

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
