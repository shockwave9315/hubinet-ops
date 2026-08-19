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
is implemented and constructible in this repository; it has not yet been deployed to any
real host — real-host operational activation is a separate, later, explicitly-authorized
step (`docs/operations/0.5-r0-operational-activation.md`).

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
  `deploy/README-0.5-firewall.md`: the sole current deployment path (clean install only;
  there is no 0.4→0.5 upgrade path).
- `docs/architecture/`: ADRs and implementation status (see `CLAUDE.md`/`AGENTS.md` for
  the authority order). `docs/operations/`: the R0 operational activation runbook and the
  Home Assistant clean-break/purge plan for a real deployed instance.

## Local validation

```bash
python -m compileall -q app custom_components tests scripts
pytest -q
bash -n deploy/install-0.5.0-fresh.sh
python scripts/validate_yaml.py
python scripts/check_tracked_files.py
```

Repository tests use a fake `ReadOnlyProviderTransport`, fake clocks, and temporary
SQLite authority databases. They do not contact Proxmox or Home Assistant.

See [`CLAUDE.md`](CLAUDE.md) and [`AGENTS.md`](AGENTS.md) for the full architecture
authority order, [`docs/architecture/0.5-implementation-status.md`](docs/architecture/0.5-implementation-status.md)
for current implementation status, and
[`docs/operations/0.5-r0-operational-activation.md`](docs/operations/0.5-r0-operational-activation.md)
for the real-host activation runbook.
