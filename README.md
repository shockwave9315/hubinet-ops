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
- A read-only HTTP API (`GET /r0/v1/health`, `/backend`, `/snapshot`). Bearer
  authentication is required on every endpoint except the deliberately
  unauthenticated minimal `/r0/v1/health` liveness probe, which exposes no
  inventory or credential data.
- A native Home Assistant integration with dynamic devices and entities.
- An automated Proxmox bootstrap that provisions the whole backend.

The runtime is read-only. Package scanning, update plans, jobs, snapshots,
healthchecks, and rollback are not built yet — see `STATUS.md`.

## Installation

The two halves deploy independently.

**Backend (on the Proxmox host).** `deploy/bootstrap-proxmox-0.5.sh` creates a
fresh unprivileged Debian LXC at the next free VMID, provisions a
least-privilege PVE token, sets up TLS trust, installs the service, writes the
config, and applies an nftables boundary — after one upfront confirmation of
the full plan. See [`deploy/README-bootstrap-proxmox-0.5.md`](deploy/README-bootstrap-proxmox-0.5.md)
and [`deploy/README-0.5-firewall.md`](deploy/README-0.5-firewall.md).

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
for f in deploy/bootstrap-proxmox-0.5.sh deploy/lib/*.sh; do bash -n "$f"; done
for f in deploy/bootstrap-proxmox-0.5.sh deploy/lib/*.sh; do python scripts/validate_hermetic_shell_boundary.py "$f"; done
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

Run the backend locally with `uvicorn app.inventory_runtime:app`. Never run a
deployment script against a real host from a development or agent session.

## Repository map

| Path | What it is |
| --- | --- |
| `app/inventory/` | durable authority subsystem: identity, discovery, reconciliation, publication |
| `app/inventory_runtime.py` | production composition root and read-only HTTP API |
| `app/inventory_runtime_config.py` | source-centric config loader |
| `app/inventory_scheduler.py` | discovery scheduler and restart recovery |
| `app/inventory_pve_transport.py` | GET-only PVE HTTP transport |
| `custom_components/hubinet_ops/` | Home Assistant integration |
| `config/inventory.example.yaml`, `.env.r0.example` | config and secrets templates |
| `deploy/` | Proxmox bootstrap, in-CT installer, systemd unit, firewall docs |
| `scripts/` | YAML, tracked-file, and shell-boundary validators |
| `tests/` | pytest suite |

## License

See [`custom_components/hubinet_ops/NOTICE.md`](custom_components/hubinet_ops/NOTICE.md)
for integration attribution notices.
