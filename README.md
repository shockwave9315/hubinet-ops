# Hubinet Ops

Hubinet Ops is a practical Proxmox operations application with a native Home
Assistant frontend. It discovers your Proxmox guests dynamically, keeps an
authoritative inventory in its own database, and presents that inventory in
Home Assistant — with a safe, operator-approved package/update workflow as the
product it is being built toward.

It runs in a trusted, self-administered Proxmox environment. Adding or removing
a guest in Proxmox never requires touching this repository or its config.

- **What the product is:** [`PRODUCT.md`](PRODUCT.md)
- **How it is built:** [`ARCHITECTURE.md`](ARCHITECTURE.md)
- **What works today:** [`STATUS.md`](STATUS.md)
- **Rules for coding agents:** [`AGENTS.md`](AGENTS.md)

## What works today

- PVE autodiscovery of every node, LXC, and QEMU guest.
- A durable SQLite inventory owned by the backend.
- An HTTP API with read-only inventory routes (`GET /r0/v1/health`, `/backend`,
  `/snapshot`), one authority-only exact-plan approval route, the
  authority-only per-resource health-contract routes
  (`GET`/`PUT`/`DELETE /r0/v1/resources/{resource_id}/health-contract`), and
  the explicit operator update controls
  (`POST`/`GET /r0/v1/resources/{resource_id}/package-update`,
  `POST .../package-update/resume`, `POST .../package-update/rollback`,
  `GET /r0/v1/package-update/active`). Bearer authentication is required on
  every endpoint except the deliberately unauthenticated minimal
  `/r0/v1/health` liveness probe, which exposes no inventory or credential
  data.
- A native Home Assistant integration with dynamic devices and entities.
- Automatic Debian/Ubuntu LXC package scanning with exact durable plans and
  Home Assistant summary entities.
- Fresh exact-plan viewing through a native Hubinet resource-device selector,
  explicit durable approval through a second native Home Assistant action, and
  a concise backend-published approval-status sensor. Approval never executes
  an update.
- Operator-declared per-resource health contracts: for each resource, the list
  of typed probes (`systemd_unit_active`, `docker_container_running`,
  `docker_container_healthy`) that must **all** hold for that workload to count
  as up. Managed through the `view_health_contract` / `set_health_contract` /
  `clear_health_contract` Home Assistant actions and the routes above, with a
  concise contract-status sensor. A resource with no contract is
  *unconfigured*, which is never "healthy" — and it can no longer be given an
  update job at all, because a job whose success criterion does not exist
  could never truthfully be called successful.
- **Operator-triggered package updates.** One explicit action starts the
  currently approved update for one resource; the backend takes a fresh
  job-owned snapshot, re-proves the exact plan, performs one bounded package
  operation, and health-checks the guest against the contract the job froze at
  issuance. Managed through the `start_update` / `view_update_job` /
  `resume_update` / `rollback_update` Home Assistant actions and the routes
  above, with a concise per-resource job status sensor.
- An automated Proxmox bootstrap that provisions the whole backend.
- An in-place updater for an existing installation: install once, update
  many times, preserving identity/config/credentials.

Package scanning refreshes APT metadata and runs `apt-get -s upgrade`; it never
installs packages.

**Nothing updates itself.** An update begins only because an authenticated
operator asked for it — not on a timer, not from a scan, not from an approval,
and not from a Home Assistant poll. Approving a plan records what you
reviewed; a separate explicit action installs it. Nothing rolls back on its
own either: a failed package operation, an unproven one, a failed healthcheck,
and an unknown one each leave the job owning its snapshot and waiting to be
asked. Snapshots a job created are kept — there is no automatic deletion or
retention policy yet. See `PRODUCT.md` and `STATUS.md`.

**The update lifecycle has not yet been validated against a real workload.**
Its automated coverage is complete, but no real package has been changed and
no real snapshot rolled back by Hubinet Ops. The runbook for that first
operator validation is below.

Pre-release authority schema versions are not migrated in place: the current
schema is v16 and has no migration from v15 or earlier. An existing
pre-release deployment uses
`deploy/update-proxmox-0.5.sh` for this: it detects the incompatible
authority schema, backs it up, and resets only the authority database (with
explicit operator authorization) while preserving the LXC, its VMID/network,
PVE identity/token, and every other credential/config file. Home Assistant
re-enrollment is required only after that explicit reset.

## Installation

The two halves deploy independently.

**Backend (on the Proxmox host).** `deploy/bootstrap-proxmox-0.5.sh` creates a
fresh unprivileged Debian LXC at the next free VMID, provisions a
least-privilege PVE token, sets up TLS trust, installs the service, provisions
a dedicated pinned-key/forced-command package-scan boundary plus five further
dedicated boundaries for the update lifecycle (snapshot, plan simulation,
mutation, rollback, health), writes the config, and applies an nftables
boundary — after one upfront confirmation of the full plan. Each boundary gets
its own private key, because the key is what selects which forced command a
connection may run. The PVE API token stays exactly `Sys.Audit,VM.Audit`:
every workload mutation runs host-local behind a root-owned forced command.
This is the first-install / disaster-recovery / deliberate-rebuild path only. See [`deploy/README-bootstrap-proxmox-0.5.md`](deploy/README-bootstrap-proxmox-0.5.md)
and [`deploy/README-0.5-firewall.md`](deploy/README-0.5-firewall.md).

**Updating an existing backend.** `deploy/update-proxmox-0.5.sh --vmid <N>`
updates an already-bootstrapped installation in place — it verifies
ownership, prints an exact plan, and preserves identity/config/credentials
(and the authority database, unless an incompatible pre-release schema
requires an explicit, backed-up reset). It never re-runs the fresh installer.
It also upgrades a pre-activation installation into the update lifecycle,
creating the five boundaries and their keys. **It refuses outright, before
touching any file, while a package-update job is active** — let the update
finish, or resolve it with `resume_update` or `rollback_update`, then run the
updater again. See [`deploy/README-update-proxmox-0.5.md`](deploy/README-update-proxmox-0.5.md).

**Home Assistant integration (via HACS).**

1. Open HACS → **Custom repositories**.
2. Add `https://github.com/shockwave9315/hubinet-ops`, category
   **Integration**.
3. Download **Hubinet Ops**, restart Home Assistant if prompted.
4. **Settings → Devices & services → Add Integration → Hubinet Ops**.
5. Enter the backend's **Base URL** (e.g. `http://<hubinet-backend>:8787`) and
   its **Bearer token** — the backend's `HUBINET_OPS_R0_API_TOKEN`, generated
   during backend deployment.

That bearer token is **not** the Proxmox API token. Home Assistant never
receives, stores, or handles a Proxmox credential — the integration has no
PVE-facing code path at all, and HACS distributes code only.

For integration development you may symlink `custom_components/hubinet_ops/`
into a Home Assistant `config/custom_components/` directory instead. That is a
development fallback, not a supported installation method.

## Running your first real update

The update lifecycle is production reachable but has never touched a real
workload. This is the operator procedure for that first validation. Use a
disposable Debian LXC you are willing to roll back — not a guest you care
about.

Everything below is done from Home Assistant (**Developer tools → Actions**)
except step 1.

1. **Update the backend to an activation build.** On the Proxmox host, run
   `deploy/update-proxmox-0.5.sh --vmid <N>`. Confirm the plan; it will report
   the five package-update boundaries it creates or leaves alone. Afterwards
   check the service is enabled, active, and healthy, and that Home Assistant
   still shows the backend's resources.

2. **Pick a disposable Debian/Ubuntu LXC** that is running and appears in Home
   Assistant with a successful package scan.

3. **Declare its health contract.** `hubinet_ops.set_health_contract`, naming
   the resource device and one or more probes that are genuinely true right
   now, for example `{"kind": "systemd_unit_active", "target": "ssh.service"}`.
   Read it back with `view_health_contract`. Without a contract the guest
   cannot be given an update job at all.

4. **Get a real, small plan.** Wait for (or wait out) an automatic scan so the
   guest has pending updates. A handful of packages is ideal.

5. **View the plan.** `hubinet_ops.view_update_plan`. Read the exact package
   rows: name, architecture, installed version, candidate version.

6. **Approve exactly that plan.** `hubinet_ops.approve_update_plan`, using the
   `approval_reference` the previous step returned. This still installs
   nothing.

7. **Start the update.** `hubinet_ops.start_update`, naming the resource
   device. This is the first action in the product's history that can change a
   real workload package. It returns the job it created.

8. **Watch it.** `hubinet_ops.view_update_job` (or the resource's *Package
   update job* sensor). Expect `snapshot_confirmed` → `mutation_completed` →
   `health_completed` with `health_outcome: passed`, and a final status of
   `succeeded`.

9. **Verify on the Proxmox host, independently of Hubinet:**
   - `pct listsnapshot <vmid>` shows exactly one new Hubinet-owned snapshot,
     created before the update, and every snapshot you took by hand is
     untouched;
   - `pct exec <vmid> -- apt list --upgradable` no longer lists the packages
     from step 5;
   - your health probe's subject really is up.

10. **Then test a failure, deliberately.** Repeat steps 4-7 on the same
    disposable guest, but first set the health contract to something that will
    NOT hold after the update — for example a systemd unit you stop by hand
    while the update runs. Expect the job to end ACTIVE at `health_completed`
    with `health_outcome: failed`, `rollback_available: true`, and **no
    rollback attempted**. Confirm nothing rolled back on its own.

11. **Roll it back explicitly.** `hubinet_ops.rollback_update`, naming the
    resource device. Expect the job to reach `rolled_back`.

12. **Verify the rollback:** the guest is **stopped** — that is the documented
    final state, because Proxmox force-stops a container to roll it back and
    Hubinet deliberately does not restart it; the packages are back at their
    pre-update versions; the job's own snapshot is still present; and no
    unrelated or manual snapshot was touched.

13. **Start the guest again** yourself (`pct start <vmid>`) when you are done.

If anything is uncertain rather than wrong — an unknown health verdict, an
unproven package operation — the job stays ACTIVE and owned and the backend
waits. `hubinet_ops.resume_update` asks it to look again; it never re-runs a
destructive step.

## Development

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

On the established Linux devbox, reuse the existing `.venv` and `.venv-ha`
described in `AGENTS.md`; do not recreate or reinstall them.

Tests use a fake provider transport, fake clocks, and temporary SQLite
databases. They never contact Proxmox, Home Assistant, or any private-network
endpoint.

Full validation, mirroring CI — run before publishing or merging a runtime
change:

```bash
.venv/bin/python -m compileall -q app custom_components tests scripts
.venv/bin/python -m pytest -q
bash -n deploy/install-0.5.0-fresh.sh
for f in deploy/bootstrap-proxmox-0.5.sh deploy/update-proxmox-0.5.sh deploy/lib/*.sh; do bash -n "$f"; done
for f in deploy/bootstrap-proxmox-0.5.sh deploy/update-proxmox-0.5.sh deploy/lib/*.sh; do .venv/bin/python scripts/validate_hermetic_shell_boundary.py "$f"; done
.venv/bin/python scripts/validate_yaml.py
.venv/bin/python scripts/check_tracked_files.py
```

The Home Assistant integration suite is a separate, pinned dependency set and
needs Python ≥ 3.14.2 on Linux:

```bash
python3.14 -m venv .venv-ha
.venv-ha/bin/python -m pip install -r requirements-ha-test.txt
.venv-ha/bin/python -m pytest -q --tb=short -o asyncio_mode=auto tests/test_hubinet_ops_integration.py
```

`tests/test_bootstrap_proxmox_0_5_smoke.py` executes the real bootstrap script
and runs only inside `tests/shell/run_bootstrap_smoke_sandbox.sh`'s
ephemeral-CI Docker sandbox; it skips everywhere else by design.

Run the backend locally with:

```bash
.venv/bin/python -m uvicorn app.inventory_runtime:create_app_from_env --factory --host 127.0.0.1 --port 8787
```

`create_app_from_env` builds the app from a runtime config file — selected via
`HUBINET_OPS_R0_CONFIG`, or the configured/default runtime config path — and
from the environment variables that config references, currently
`HUBINET_OPS_R0_PVE_TOKEN` and `HUBINET_OPS_R0_API_TOKEN`. The validated
`package_scan.interval_seconds` runtime setting defaults to 21,600 seconds;
the scheduler supports controlled interval replacement. The only Home
Assistant write is exact-plan approval authority; it cannot execute package or
workload mutations. See
[`config/inventory.example.yaml`](config/inventory.example.yaml) and
[`.env.r0.example`](.env.r0.example) for the config shape and required
variables. Never run a deployment script against a real host from a
development or agent session.

## Repository map

| Path | What it is |
| --- | --- |
| `app/inventory/` | durable authority subsystem: identity, discovery, reconciliation, publication, internal package-update job authority |
| `app/inventory_runtime.py` | production composition root and bounded HTTP API |
| `app/inventory_runtime_config.py` | source-centric config loader |
| `app/inventory_scheduler.py` | discovery scheduler and restart recovery |
| `app/inventory_pve_transport.py` | GET-only PVE HTTP transport |
| `app/package_scan.py`, `app/package_scan_scheduler.py` | exact APT-plan parsing and automatic scan worker |
| `app/package_scan_host_control.py` | bounded typed SSH client for the forced PVE helper |
| `custom_components/hubinet_ops/` | Home Assistant integration |
| `config/inventory.example.yaml`, `.env.r0.example` | config and secrets templates |
| `deploy/` | Proxmox bootstrap, in-place updater, in-CT installer, systemd unit, firewall docs |
| `scripts/` | YAML, tracked-file, and shell-boundary validators |
| `tests/` | pytest suite |

## License

See [`custom_components/hubinet_ops/NOTICE.md`](custom_components/hubinet_ops/NOTICE.md)
for integration attribution notices.
