# Hubinet Ops — current state

## Implemented

- **Dynamic PVE discovery** — nodes, LXC and QEMU guests, discovered from the
  PVE API with no static VMID configuration anywhere.
- **Persistent backend inventory** — SQLite authority database (schema v6):
  identity, locator bindings and generations, presence/lifecycle, retained
  missing/replaced history, source health and freshness, discovery-run
  ownership with CAS/fencing and restart recovery.
- **R0 HTTP API** — `GET /r0/v1/health`, `/backend`, `/snapshot`, no mutation
  route. Bearer authentication is required on every endpoint except the
  deliberately unauthenticated minimal `/r0/v1/health` liveness probe, which
  exposes no inventory or credential data.
- **Home Assistant integration** — config flow, coordinator, structural
  contract validation, dynamic devices and entities, diagnostics with recursive
  secret redaction. Distributed via HACS.
- **Bootstrap and deployment** — `deploy/bootstrap-proxmox-0.5.sh` provisions a
  fresh unprivileged LXC, a least-privilege PVE identity, TLS trust, the
  service, and an nftables boundary. Exercised against a real Proxmox host.

The runtime is read-only today. It has no policy, jobs, mutation, or endpoint
failover.

## Next

- **LXC Debian/Ubuntu package scanning** — collect installed and available
  package detail from managed LXC guests and publish it in the snapshot.
  Non-installing, per `PRODUCT.md`.

## Then

- Update plan and explicit operator approval, with plan revalidation.
- Job execution with a fresh job-owned snapshot and live output.
- Healthcheck after update.
- Same-job rollback.
- Lifecycle controls (start/stop/reboot) and manual snapshot operations.

## Known limitations

- The Home Assistant test suite requires Python ≥ 3.14.2 with
  `homeassistant==2026.8.1` and does not run on native Windows, because Home
  Assistant imports POSIX `fcntl` at collection time. Linux CI is the real
  compatibility gate. Do not patch Home Assistant or fake `fcntl` around this.
- `deploy/bootstrap-proxmox-0.5.sh` is only executed for real inside the
  ephemeral-CI Docker sandbox (`tests/shell/run_bootstrap_smoke_sandbox.sh`).
  Local runs validate it statically.
- Pre-release: the authority database is disposable. A schema change means a
  fresh database and Home Assistant re-enrollment. There is no migration path
  and none is planned before the first release.
