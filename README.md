# Hubinet Ops 0.5

Hubinet Ops is a policy-controlled Proxmox inventory service with a native Home Assistant
frontend. This repository is **0.5-only**: it is a clean-break rewrite, and the obsolete
0.2.x/0.3.x/0.4.x implementation has been retired from `main`. Git history/tags remain
the archive if that old source ever needs to be inspected again.

The current release, **R0**, is a **read-only** runtime activation: real discovery
against a real Proxmox source, a durable SQLite authority database, and a read-only HTTP
API consumed by the native Home Assistant integration. It has no policy, jobs, mutation,
endpoint activation/failover, or attestation enrollment automation, and no code path in
this release can grant `security_continuity=trusted`. R0 has been merged into `main` and
is implemented and constructible in this repository. It has since been installed and
genuinely exercised on a real Proxmox host through explicitly-authorized manual dogfood
runs of the automated bootstrap, the first real Home Assistant enrollment passed its R0
acceptance checks, the recommended observation window completed, and the scheduled
2026-08-23 operator re-check passed every remaining observation. **The current R0
operational decision is GO**, strictly read-only, mutation authority NONE. This closes
only the read-only operational observation gate; Blocker B remains open. This
repository's own automated tests/CI remain fully hermetic throughout — none of the
real-host work was exercised by CI. See
`docs/architecture/0.5-implementation-status.md` for current status and
`docs/archive/project-history/0.5-r0-activation-chronology.md` for the full historical
chronology.

## What it does today, and where it is going

Implemented and running today, strictly read-only:

- PVE autodiscovery — every node/LXC/QEMU resource is discovered dynamically;
- dynamic backend inventory in the durable SQLite authority database;
- dynamic Home Assistant representation of that inventory.

The intended next step is a **safe, operator-driven update workflow**. Its hard rules,
stated in full in [`docs/product-intent.md`](docs/product-intent.md):

- package/update **scanning** may run automatically, but only **read-only**, and the
  operator is shown the exact package detail (name, installed and candidate version,
  origin, description, and further metadata where it can be established reliably);
- **NO AUTO-UPDATE** — installing updates always requires explicit operator review and
  approval of the current update plan, and a material change to that plan after approval
  invalidates the approval;
- each approved run creates a fresh, **job-owned** pre-update snapshot on the actual
  current guest, and may roll back only to **its own** snapshot;
- automatic snapshot cleanup touches only Hubinet-managed snapshots — operator snapshots
  are never touched.

None of that update surface is implemented yet, and none of it is authorized to begin;
each piece needs its own accepted architecture first.

## Safety model

- Every `/r0/v1` endpoint uses bearer authentication; the API exposes no mutation route.
- The backend and its SQLite authority database are the source of truth; Home Assistant
  only presents the published snapshot.
- A Proxmox VMID is a reusable **slot locator**, never durable identity — durable
  identity is an opaque, backend-generated `resource_id` per inventory incarnation. See
  `docs/architecture/adr/0001-resource-identity-incarnation.md`.
- Inventory is **fully dynamic and source-centric**: there is no static VMID/resource
  configuration anywhere in the runtime or config surface. Adding or removing a guest in
  Proxmox requires zero repository or config changes.
- Discovery (`app/inventory/discovery.py`, `reconciliation.py`, `provider.py`) is
  strictly read-only: it can create/update only `discovered`-level facts and never
  grants management, trust, or destructive capability.
- The production PVE transport (`app/inventory_pve_transport.py`) is GET-only, with
  mandatory TLS verification and no mutation-verb escape hatch.

## Installation

The Proxmox backend and the Home Assistant integration are two independently deployed
and independently distributed halves: the backend is provisioned on the Proxmox host via
`deploy/bootstrap-proxmox-0.5.sh` (see "Repository map" below), and the Home Assistant
integration (`custom_components/hubinet_ops/`) is distributed separately through
[HACS](https://hacs.xyz/). Their credential boundary:

- HA never receives, stores, or handles the Proxmox API credential — the integration has
  no PVE-facing code path at all.
- HA authenticates only to the Hubinet Ops backend, using the backend's own dedicated
  `HUBINET_OPS_R0_API_TOKEN` read-only bearer token, entered through the config flow
  described below.
- HACS itself distributes integration **code only** and transfers no credential of any
  kind.

### Home Assistant integration (via HACS)

1. Open HACS.
2. Go to **Custom repositories**.
3. Add `https://github.com/shockwave9315/hubinet-ops`, category **Integration**.
4. Download **Hubinet Ops**.
5. Restart Home Assistant if required.
6. Go to **Settings → Devices & services → Add Integration → Hubinet Ops**.
7. Enter the backend's **Base URL** (e.g. `http://<hubinet-backend>:8787`) and its
   **Bearer token** — the backend's `HUBINET_OPS_R0_API_TOKEN` value, generated during
   backend deployment. **This is not the Proxmox API token**; the integration never
   sees, stores, or handles Proxmox credentials, only the backend's own read-only R0
   bearer token, via the existing config flow described in
   `docs/operations/0.5-ha-clean-break.md` section 5.

This HACS flow is the supported, normal installation path — there is no manual-copy step
in ordinary operation.

### Developer / local-testing fallback

For local integration development only, `custom_components/hubinet_ops/` can be
symlinked or copied directly into a Home Assistant `config/custom_components/` directory
instead of installing through HACS. This is not a supported end-user installation
method — use HACS for real deployments.

## Repository map

- `app/inventory/`: the durable 0.5 authority subsystem (identity, discovery,
  reconciliation, publication).
- `app/inventory_runtime.py`, `inventory_runtime_config.py`, `inventory_scheduler.py`,
  `inventory_pve_transport.py`: the R0 read-only runtime composition root, config loader,
  discovery scheduler, and production Proxmox transport.
- `custom_components/hubinet_ops/`: the native Home Assistant integration consuming the
  R0 backend over HTTP.
- `config/inventory.example.yaml`, `.env.r0.example`: the source-centric R0 bootstrap
  config and paired secrets template — no static resource inventory.
- `deploy/hubinet-ops-0.5.service`, `deploy/install-0.5.0-fresh.sh`,
  `deploy/README-0.5-firewall.md`: the in-CT install path (clean install only; there
  is no 0.4→0.5 upgrade path).
- `deploy/bootstrap-proxmox-0.5.sh`, `deploy/lib/bootstrap-*.sh`,
  `deploy/README-bootstrap-proxmox-0.5.md`: the primary, product-facing PVE-host
  entrypoint — automates next-free-VMID-auto-detected CT creation, least-privilege
  PVE identity (exact-set-verified permissions), TLS trust (explicit opt-in only
  for system-trust fallback), git-commit-provenance-gated source deployment via
  the in-CT install above, source-centric config, and the mandatory firewall
  boundary (exact rule content/order verified), ending with CT boot enabled only
  after a real discovery-acceptance check passes. Exercised against a real Proxmox
  host through explicitly-authorized manual dogfood runs, the last of which achieved a
  fresh, clean Phase 1-13 PASS with no manual repair mid-run. Home Assistant acceptance
  and the observation window were completed separately; the current R0 operational
  decision is GO. See `deploy/README-bootstrap-proxmox-0.5.md`'s "What this script
  proves, and what it does not" section for the exact evidence boundary, and
  `docs/archive/project-history/0.5-r0-activation-chronology.md` for the dogfood
  chronology.
- `docs/architecture/`: the documentation index (`README.md`), ADRs, and implementation
  status (see `CLAUDE.md`/`AGENTS.md` for the authority order). `docs/operations/`: the
  R0 operational activation runbook and the Home Assistant clean-break/purge plan for a
  real deployed instance. `docs/product-intent.md`: the current product target.
  `docs/archive/`: non-authoritative history (superseded research, postmortems, project
  chronology).

## Local validation

```bash
python -m compileall -q app custom_components tests scripts
pytest -q
bash -n deploy/install-0.5.0-fresh.sh
for f in deploy/bootstrap-proxmox-0.5.sh deploy/lib/*.sh; do bash -n "$f"; done
for f in deploy/bootstrap-proxmox-0.5.sh deploy/lib/*.sh; do python scripts/validate_hermetic_shell_boundary.py "$f"; done
python scripts/validate_yaml.py
python scripts/check_tracked_files.py
```

Repository tests use a fake `ReadOnlyProviderTransport`, fake clocks, and temporary
SQLite authority databases. They do not contact Proxmox or Home Assistant.

## Documentation

Start at [`docs/architecture/README.md`](docs/architecture/README.md) — the documentation
index, authority hierarchy, default agent reading set, task-to-document matrix, and
archive policy. [`docs/product-intent.md`](docs/product-intent.md) states the current
product target. Anything under `docs/archive/` is non-authoritative history and is not a
roadmap.

See [`CLAUDE.md`](CLAUDE.md) and [`AGENTS.md`](AGENTS.md) for the full architecture
authority order, [`docs/architecture/0.5-implementation-status.md`](docs/architecture/0.5-implementation-status.md)
for current implementation status, and
[`docs/operations/0.5-r0-operational-activation.md`](docs/operations/0.5-r0-operational-activation.md)
for the real-host activation runbook.
