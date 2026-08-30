# Hubinet Ops — current state

## Implemented

- **Dynamic PVE discovery** — nodes, LXC and QEMU guests, discovered from the
  PVE API with no static VMID configuration anywhere.
- **Persistent backend inventory, scans, and approvals** — SQLite authority
  database (schema v8):
  identity, locator bindings and generations, presence/lifecycle, retained
  missing/replaced history, source health and freshness, discovery-run
  ownership with CAS/fencing and restart recovery, immutable package-scan
  source context, and durable exact-plan approval facts.
- **R0 HTTP API** — `GET /r0/v1/health`, `/backend`, `/snapshot`, plus exactly
  one authority-only mutation,
  `PUT /r0/v1/resources/{resource_id}/package-plan-approval`. Bearer
  authentication is required on every endpoint except the
  deliberately unauthenticated minimal `/r0/v1/health` liveness probe, which
  exposes no inventory or credential data.
- **Home Assistant integration** — config flow, coordinator, structural
  contract validation, dynamic devices and entities, package-scan summary and
  concise approval-status sensors, diagnostics with recursive secret redaction,
  and native `view_update_plan` / `approve_update_plan` actions. The view action
  uses the native Hubinet resource-device selector and returns exact package
  rows as response data, never as entity attributes. Distributed via HACS.
- **Automatic Debian/Ubuntu LXC package scanning** — configurable six-hour
  default interval, one worker, typed pinned-key SSH to a forced PVE helper,
  fixed `pct exec` operations, APT metadata refresh plus upgrade simulation,
  exact durable package rows/fingerprint, fencing, restart recovery, and
  failure-is-unknown semantics. It never installs packages.
- **Bootstrap and deployment** — `deploy/bootstrap-proxmox-0.5.sh` provisions a
  fresh unprivileged LXC, a least-privilege PVE identity, TLS trust, a dedicated
  forced-command scan boundary, the service, and an nftables boundary. This
  remains the first-install/disaster-recovery/deliberate-rebuild entrypoint
  only.
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
  included, never leaving old code paired with a new schema. Package/job
  execution updates are out of scope for this stage — see
  `deploy/README-update-proxmox-0.5.md`.

The PVE API inventory surface remains read-only. The backend's sole mutation
route records Hubinet approval authority state only. Package scanning may write
APT index/cache metadata but never changes workload packages. There is no
update execution, update job, snapshot mutation, healthcheck, rollback,
lifecycle mutation, policy, or endpoint failover.

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
first real operator Human0 validation of this updater against an actual
Proxmox host is still pending — see `deploy/README-update-proxmox-0.5.md`.
Workload package update job execution remains a separate, unimplemented
future stage after that Human0.

## Exact update-plan approval

- **Implemented:** fresh exact-plan presentation, explicit durable approval of
  the reviewed `(resource_id, scan_run_id, plan_fingerprint)`, and atomic
  fingerprint/resource/source-context revalidation. A later same material
  fingerprint remains effectively approved only while required resource and
  source context is unchanged. Changed, failed, interrupted, unsupported, or
  unavailable plans are not effectively approved.
- Approval is authority state only. This stage cannot install or upgrade
  packages and does not create jobs or PVE snapshots.

## Next

- Job execution with a fresh job-owned snapshot and live output.
- Healthcheck after update.
- Same-job rollback.
- Lifecycle controls (start/stop/reboot) and manual snapshot operations.

## Known limitations

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
- Pre-release: schema v8 is incompatible with v7, and there is no v7-to-v8
  migration path. An existing installation now uses `deploy/update-proxmox-0.5.sh`
  for this: it detects the incompatible authority schema, backs it up, and
  resets only the authority database (see "In-place product updates" below)
  while preserving the LXC, its VMID/network, PVE identity/token, and every
  other credential/config file. Home Assistant re-enrollment is required only
  after that explicit reset, not for an ordinary code-only update.
- Package origin, description, security classification, and reboot-required
  stay unknown unless reliable evidence is present. The first parser derives
  origin/security from stable-English APT simulation evidence and leaves
  descriptions unknown.
- PVE sshd must permit public-key login for the forced root authorization.
  Bootstrap verifies the boundary before starting Hubinet and never rewrites
  operator sshd configuration.
