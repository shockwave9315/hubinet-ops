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
genuinely exercised in a real container (CT110) via three explicitly-authorized manual
dogfood runs of the automated Proxmox bootstrap, all against the same physical PVE host
(dogfood #1 stopped at Phase 10; dogfood #2 reached Phase 12, and a later read-only
forensic replay proved the production provider and the exact Phase-12 acceptance path
both succeed once an environmental PVE root CA issue was corrected). **Dogfood #3, from
this exact merged `main` commit, completed the full bootstrap through Phase 13**: backend
`source_health=healthy`/`source_freshness=fresh`, `last_committed_run_sequence=1`,
`node_count=1`, `resource_count=11`, firewall verification PASS, mutation authority
confirmed NONE, and CT `onboot` enabled. This repository's own automated tests/CI remain
fully hermetic throughout — none of the above was exercised by CI, only by a real,
manual, explicitly-authorized operator run. Real-host **operational activation**
(the multi-day observation window and Home Assistant acceptance checklist) is a
separate, later step (`docs/operations/0.5-r0-operational-activation.md`); see
`docs/architecture/0.5-implementation-status.md` for the full dogfood record.

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

The Proxmox backend and the Home Assistant integration are two independent deployment
halves that share no credentials: the backend is provisioned on the Proxmox host via
`deploy/bootstrap-proxmox-0.5.sh` (see "Repository map" below), and the Home Assistant
integration (`custom_components/hubinet_ops/`) is distributed separately through
[HACS](https://hacs.xyz/).

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
  after a real discovery-acceptance check passes. Exercised twice against a real
  Proxmox host (the same host both times; dogfood #1 stopped at Phase 10, dogfood #2
  reached Phase 12) — neither run is a full bootstrap PASS, and a fresh, clean
  Phase-13 PASS remains outstanding. See
  `deploy/README-bootstrap-proxmox-0.5.md`'s "What this script proves, and what it
  does not" section for the exact current status.
- `docs/architecture/`: ADRs and implementation status (see `CLAUDE.md`/`AGENTS.md` for
  the authority order). `docs/operations/`: the R0 operational activation runbook and the
  Home Assistant clean-break/purge plan for a real deployed instance.

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

See [`CLAUDE.md`](CLAUDE.md) and [`AGENTS.md`](AGENTS.md) for the full architecture
authority order, [`docs/architecture/0.5-implementation-status.md`](docs/architecture/0.5-implementation-status.md)
for current implementation status, and
[`docs/operations/0.5-r0-operational-activation.md`](docs/operations/0.5-r0-operational-activation.md)
for the real-host activation runbook.
