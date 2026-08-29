# Hubinet Ops — current state

## Implemented

- **Dynamic PVE discovery** — nodes, LXC and QEMU guests, discovered from the
  PVE API with no static VMID configuration anywhere.
- **Persistent backend inventory and scans** — SQLite authority database (schema v7):
  identity, locator bindings and generations, presence/lifecycle, retained
  missing/replaced history, source health and freshness, discovery-run
  ownership with CAS/fencing and restart recovery.
- **R0 HTTP API** — `GET /r0/v1/health`, `/backend`, `/snapshot`, no mutation
  route. Bearer authentication is required on every endpoint except the
  deliberately unauthenticated minimal `/r0/v1/health` liveness probe, which
  exposes no inventory or credential data.
- **Home Assistant integration** — config flow, coordinator, structural
  contract validation, dynamic devices and entities, package-scan summary
  sensors, diagnostics with recursive secret redaction. Distributed via HACS.
- **Automatic Debian/Ubuntu LXC package scanning** — configurable six-hour
  default interval, one worker, typed pinned-key SSH to a forced PVE helper,
  fixed `pct exec` operations, APT metadata refresh plus upgrade simulation,
  exact durable package rows/fingerprint, fencing, restart recovery, and
  failure-is-unknown semantics. It never installs packages.
- **Bootstrap and deployment** — `deploy/bootstrap-proxmox-0.5.sh` provisions a
  fresh unprivileged LXC, a least-privilege PVE identity, TLS trust, a dedicated
  forced-command scan boundary, the service, and an nftables boundary.

The HTTP/PVE API inventory surface remains read-only. Package scanning may
write APT index/cache metadata but never changes workload packages. There is no
policy, update job, or endpoint failover.

## Next

- **Update plan and explicit operator approval**, with exact scan fingerprint
  revalidation.

## Then

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
- Package origin, description, security classification, and reboot-required
  stay unknown unless reliable evidence is present. The first parser derives
  origin/security from stable-English APT simulation evidence and leaves
  descriptions unknown.
- PVE sshd must permit public-key login for the forced root authorization.
  Bootstrap verifies the boundary before starting Hubinet and never rewrites
  operator sshd configuration.
