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
  `/snapshot`) and one authority-only exact-plan approval route. Bearer
  authentication is required on every endpoint except the deliberately
  unauthenticated minimal `/r0/v1/health` liveness probe, which exposes no
  inventory or credential data.
- A native Home Assistant integration with dynamic devices and entities.
- Automatic Debian/Ubuntu LXC package scanning with exact durable plans and
  Home Assistant summary entities.
- Fresh exact-plan viewing through a native Hubinet resource-device selector,
  explicit durable approval through a second native Home Assistant action, and
  a concise backend-published approval-status sensor. Approval never executes
  an update.
- An automated Proxmox bootstrap that provisions the whole backend.
- An in-place updater for an existing installation: install once, update
  many times, preserving identity/config/credentials.

Package scanning refreshes APT metadata and runs `apt-get -s upgrade`; it never
installs packages. Update execution, jobs, snapshots, healthchecks, and
rollback are not built yet — see `STATUS.md`.

Schema v8 has no v7 migration. An existing pre-release deployment uses
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
a dedicated pinned-key/forced-command package-scan boundary, writes the config,
and applies an nftables boundary — after one upfront confirmation of the full
plan. This is the first-install / disaster-recovery / deliberate-rebuild path
only. See [`deploy/README-bootstrap-proxmox-0.5.md`](deploy/README-bootstrap-proxmox-0.5.md)
and [`deploy/README-0.5-firewall.md`](deploy/README-0.5-firewall.md).

**Updating an existing backend.** `deploy/update-proxmox-0.5.sh --vmid <N>`
updates an already-bootstrapped installation in place — it verifies
ownership, prints an exact plan, and preserves identity/config/credentials
(and the authority database, unless an incompatible pre-release schema
requires an explicit, backed-up reset). It never re-runs the fresh
installer. See [`deploy/README-update-proxmox-0.5.md`](deploy/README-update-proxmox-0.5.md).

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

## Development

```bash
python -m pip install -r requirements.txt -r requirements-dev.txt
pytest -q
```

Tests use a fake provider transport, fake clocks, and temporary SQLite
databases. They never contact Proxmox, Home Assistant, or any private-network
endpoint.

Full validation, mirroring CI — run before publishing or merging a runtime
change:

```bash
python -m compileall -q app custom_components tests scripts
pytest -q
bash -n deploy/install-0.5.0-fresh.sh
for f in deploy/bootstrap-proxmox-0.5.sh deploy/update-proxmox-0.5.sh deploy/lib/*.sh; do bash -n "$f"; done
for f in deploy/bootstrap-proxmox-0.5.sh deploy/update-proxmox-0.5.sh deploy/lib/*.sh; do python scripts/validate_hermetic_shell_boundary.py "$f"; done
python scripts/validate_yaml.py
python scripts/check_tracked_files.py
```

The Home Assistant integration suite is a separate, pinned dependency set and
needs Python ≥ 3.14.2 on Linux:

```bash
python -m pip install -r requirements-ha-test.txt
python -m pytest -q --tb=short -o asyncio_mode=auto tests/test_hubinet_ops_integration.py
```

`tests/test_bootstrap_proxmox_0_5_smoke.py` executes the real bootstrap script
and runs only inside `tests/shell/run_bootstrap_smoke_sandbox.sh`'s
ephemeral-CI Docker sandbox; it skips everywhere else by design.

Run the backend locally with:

```bash
uvicorn app.inventory_runtime:create_app_from_env --factory --host 127.0.0.1 --port 8787
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
| `app/inventory/` | durable authority subsystem: identity, discovery, reconciliation, publication |
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
