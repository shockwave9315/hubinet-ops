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
Hubinet observes current guest state. Human0 validates only the currently
implemented scope; update execution, job-owned snapshots, healthchecks, and
rollback remain unimplemented future stages.

## Next

- **Exact update plan presentation and explicit operator approval**, with exact
  fingerprint and current-context binding/revalidation. This stage does not
  execute updates.

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
