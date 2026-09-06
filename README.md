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
- An HTTP API with read-only inventory/presentation routes (`GET /r0/v1/health`,
  `/backend`, `/snapshot`, `/operator-availability`), one authority-only
  exact-plan approval route, the
  authority-only per-resource health-contract routes
  (`GET`/`PUT`/`DELETE /r0/v1/resources/{resource_id}/health-contract`), and
  the explicit operator update controls
  (`POST`/`GET /r0/v1/resources/{resource_id}/package-update`,
  `POST .../package-update/resume`, `POST .../package-update/rollback`,
  `GET /r0/v1/package-update/active`). Bearer authentication is required on
  every endpoint except the deliberately unauthenticated minimal
  `/r0/v1/health` liveness probe, which exposes no inventory or credential
  data.
- A native Home Assistant integration with dynamic resource devices, sensors,
  a rollback binary sensor, and explicit operator buttons.
- Automatic Debian/Ubuntu LXC package scanning with exact durable plans and
  Home Assistant summary entities.
- Fresh exact-plan review through a per-resource button and persistent
  notification, followed by a separate **Approve reviewed plan** button. Home
  Assistant remembers only the exact backend/resource/scan/fingerprint
  reference reviewed during the current runtime, fresh-reads it again before
  approval, and forgets it on reload. The backend independently revalidates
  the same reference. Approval never executes an update.
- Operator-declared per-resource health contracts: for each resource, the list
  of typed probes (`systemd_unit_active`, `docker_container_running`,
  `docker_container_healthy`) that must **all** hold for that workload to count
  as up. Managed through the `view_health_contract` / `set_health_contract` /
  `clear_health_contract` Home Assistant actions and the routes above, with a
  concise contract-status sensor and a per-resource **View health contract**
  button. A resource with no contract is
  *unconfigured*, which is never "healthy" — and it can no longer be given an
  update job at all, because a job whose success criterion does not exist
  could never truthfully be called successful.
- **Operator-triggered package updates.** One explicit action starts the
  currently approved update for one resource; the backend takes a fresh
  job-owned snapshot, re-proves the exact plan, performs one bounded package
  operation, and health-checks the guest against the contract the job froze at
  issuance. Managed through the `start_update` / `view_update_job` /
  `resume_update` / `rollback_update` Home Assistant actions and the routes
  above, with per-resource **Start**, **View job**, **Resume**, and **Roll
  back** buttons. Concise sensors show the latest job status, checkpoint,
  package count, health outcome, and authoritative rollback availability;
  exact bounded job details and recent durable events appear in a persistent
  notification only when requested.
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

**The Human0 production lifecycle is complete.** A real 24-package update
passed through explicit approval, a fresh same-job snapshot, exact-plan
revalidation, proven mutation, frozen health PASS, and `SUCCEEDED`. A fresh
post-update scan observed zero pending packages. A second real update produced
a deterministic health failure, did not auto-rollback, and reached
`ROLLED_BACK` only after the operator explicitly requested the exact same-job
snapshot. See `STATUS.md` for the completed evidence and current product stage.

Pre-release authority schema versions are not migrated in place: the current
schema is v19. Schema v17 added the per-resource durable `issuance_sequence`
that orders "latest job" readback by issuance rather than wall-clock
`issued_at`. Schema v18 adds the durable job-keyed post-success package-scan
request and its write-once same-resource RUNNING scan link. Schema v19 makes a
successful job the durable consumption fact for its exact approval and permits
at most one successful job per approval. An existing schema-v18 (or earlier)
pre-release deployment is therefore incompatible in
place. `deploy/update-proxmox-0.5.sh` reports
`reset_required`, makes and validates a coherent authority backup, and resets
only the authority database after explicit operator authorization. The LXC,
its VMID/network, PVE identity/token, HA bearer, config, TLS material and
host-control credentials are preserved; authority identities/history,
package-scan/job history and exact-plan approvals are recreated with the fresh
database. Home Assistant re-enrollment is required after that reset because
`backend_instance_id` and resource identities are regenerated.

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
Today bootstrap stores the backend bearer in root-readable
`/etc/hubinet-ops/agent.env`. A dedicated operator-facing handoff/retrieval
path that keeps it out of ordinary logs and snapshots remains planned.

For integration development you may symlink `custom_components/hubinet_ops/`
into a Home Assistant `config/custom_components/` directory instead. That is a
development fallback, not a supported installation method.

## Operator update workflow

Open the discovered LXC's Hubinet Ops device in Home Assistant. Its entities
show current workload and package-scan state, pending count, plan approval,
health-contract configuration, latest job status/checkpoint/package count,
last definitive update health result, and rollback availability.

For an update:

1. Press **Review update plan**. Hubinet Ops fresh-reads the backend and opens
   a localized persistent notification containing every exact package row.
   Backend text is rendered literally so it cannot create fake Markdown rows,
   links, images, or instructions.
2. After reviewing it, press **Approve reviewed plan**. Approval is refused if
   the backend identity, resource, scan run, or material fingerprint changed —
   even when a new scan has the same fingerprint. A reload also requires a new
   review.
3. Press **Start approved update**. One press creates one request ID and one
   logical backend invocation. The backend still owns approval, freshness,
   targeting, single-flight, snapshot, mutation, health, and terminalization.
4. Press **View update job** for bounded durable facts and recent events.
   **Resume update job** is available only when the backend publishes an
   active resumable job. **Roll back this update** is available only when the
   backend publishes same-job rollback authority.

The variable-length typed health probe list remains edited through the
`set_health_contract` and `clear_health_contract` actions; forcing that list
into a scalar text/select entity would weaken the typed contract. The native
viewer and status sensor remove routine inspection from Developer Tools, while
the actions remain available for initial or occasional contract editing and
diagnostics.

Approval performs no workload mutation, one successful job consumes its exact
approval, and UNKNOWN never authorizes success or retry. There is no automatic
update or automatic rollback.

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
the scheduler supports controlled interval replacement. Home Assistant writes
exact-plan approval authority, health-contract configuration, and the
explicit operator update controls (start/resume/roll back one resource's
update) — none of them executes a package or workload mutation directly:
each durably records an authority fact or an explicit request, and only the
backend's own worker, through its forced-command helpers, ever runs a real
command against a guest. See
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
