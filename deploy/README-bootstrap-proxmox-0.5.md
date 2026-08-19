# Hubinet Ops 0.5 R0 — automated Proxmox bootstrap

`deploy/bootstrap-proxmox-0.5.sh` is the primary, product-facing way to
activate Hubinet Ops 0.5 R0 on a Proxmox VE host. It automates the manual
procedure documented in
[`docs/operations/0.5-r0-operational-activation.md`](../docs/operations/0.5-r0-operational-activation.md)
sections 1-4 (and the start/first-half of section 5): a fresh unprivileged
Debian 13 LXC, the R0 read-only runtime deployed into it via
`deploy/install-0.5.0-fresh.sh` (unmodified), a least-privilege PVE
identity, PVE TLS trust material, and the mandatory nftables firewall
boundary — in that order, ending with the service started and CT boot
enabled only after every gate passes.

This script adds **no runtime mutation capability**. R0 remains read-only
end to end; the `pct`/`pveum` commands this script runs are one-shot,
human-invoked, PVE-host provisioning steps — structurally separate from
Hubinet Ops's own GET-only production PVE transport
(`app/inventory_pve_transport.py`).

## Prerequisites

- A Proxmox VE host you can run commands on as root (the script must run
  **on** the PVE host itself — it is not SSH-orchestrated).
- The standard PVE toolchain already present on any PVE host: `pct`,
  `pveum`, `pveam`, `pvesh`, `pvesm`, `nft`.
- A storage backend supporting container rootdirs (`pvesm status
  --content rootdir`), and one supporting `vztmpl` if you want the script
  to auto-download the Debian 13 template.
- A network bridge (default `vmbr0`).
- The Home Assistant host/subnet's IPv4 CIDR that must be allowed to
  reach the R0 API (e.g. `203.0.113.50/32`) — there is no safe default
  for this; you must provide it.
- A checked-out copy of this repository on the PVE host (the script reads
  its own `--source-dir`, default: its own repository root, to deploy).

## Usage

Interactive (asks only for the Home Assistant source CIDR, and static
network details if you choose static networking; every other value has a
sensible auto-detected or documented default):

```bash
sudo bash deploy/bootstrap-proxmox-0.5.sh
```

Non-interactive, fully deterministic (for scripted/repeatable activation
or CI-adjacent automation against a real lab host):

```bash
sudo bash deploy/bootstrap-proxmox-0.5.sh \
  --non-interactive --yes \
  --vmid 110 \
  --hostname hubinet-ops \
  --bridge vmbr0 \
  --network dhcp \
  --ha-source 203.0.113.50/32 \
  --pve-endpoint https://203.0.113.10:8006
```

Static networking instead of DHCP:

```bash
sudo bash deploy/bootstrap-proxmox-0.5.sh \
  --non-interactive --yes \
  --ha-source 203.0.113.50/32 \
  --network static --ip 203.0.113.20/24 --gateway 203.0.113.1
```

Run `bash deploy/bootstrap-proxmox-0.5.sh --help` for the full flag
reference (all flags, all defaults).

## What it creates

On the Proxmox host:

- One new LXC container at the configured VMID (default `110`):
  unprivileged, `onboot=0` until the very last step, firewall enabled on
  `net0`, default resources 1 core / 1024 MiB memory / 512 MiB swap / 8
  GiB rootfs (all overridable), Debian 13 standard template (auto-
  selected/downloaded), with `nesting=1` enabled specifically because
  Debian 13's systemd (>=257) requires it inside an unprivileged LXC to
  reach a healthy `systemctl is-system-running` (see the script's phase 4
  comments for the exact failing units this fixes:
  `dev-mqueue.mount`/`run-lock.mount`/`tmp.mount`).
- One dedicated read-only PVE identity: user `hubinetops@pve`, role
  `HubinetOpsR0Auditor` (privileges exactly `Sys.Audit`,`VM.Audit`, never
  a mutation privilege), a privilege-separated API token
  `hubinetops@pve!r0-readonly`, with the role granted to **both** the
  user and the token at path `/` with propagation — see
  `docs/operations/0.5-r0-operational-activation.md` section 2 for the
  exact reasoning (this is the minimal set R0's own discovery contract
  requires, not a guess). The script verifies the token's effective
  permissions after creation and fails if any required privilege is
  missing or any mutation-shaped privilege is present.
- Inside the CT: the R0 runtime (via the unmodified
  `deploy/install-0.5.0-fresh.sh`), a generated
  `/etc/hubinet-ops/inventory.yaml` (source-centric only — no VMID/
  resource list of any kind) and `/etc/hubinet-ops/agent.env` (the PVE
  token from above plus the R0 API bearer token the installer already
  generates), both `0640 root:hubinetops`.
- Inside the CT: an `nftables` ruleset (`/etc/nftables.conf`) restricting
  inbound TCP 8787 to exactly the configured HA source CIDR, and
  outbound traffic from the `hubinetops` service user to exactly the
  configured PVE endpoint on TCP 8006 (plus, only if the endpoint is
  configured as a hostname rather than a literal IP, a narrowly-scoped
  DNS-resolver allow rule) — the same model documented in
  `deploy/README-0.5-firewall.md`.

## What it never creates or does

- No static VMID/resource/CT/QEMU inventory anywhere.
- No mutation-shaped PVE privilege on the created user, role, or token.
- No `verify=false`/`--insecure` TLS bypass — the script fails closed if
  it cannot determine valid CA trust material instead.
- No 0.4 migration, import, or coexistence of any kind.
- No SSH, MQTT, hostd, or forced-command wrapper of any kind.
- No arbitrary command execution — every `pct`/`pveum` invocation uses a
  fixed, quoted argument list; nothing here accepts free-form command
  text, and `eval` is never used.

## Security model

- **Least privilege by construction**, not by convention: the PVE role
  created is exactly `Sys.Audit,VM.Audit`, matching
  `app.inventory.provider.ENDPOINT_ACL_MATRIX`'s actual requirement (see
  the operational-activation runbook section 2.1) — not a broader
  built-in role.
- **Verified, not assumed**: after granting the role, the script reads
  back the token's own effective permissions and fails closed if a
  required privilege is missing or a forbidden (mutation-shaped)
  privilege is present. It never performs a real mutating PVE API call
  merely to prove it would fail.
- **Secrets are never persisted outside their approved location**: the
  PVE token secret exists only in restrictive (`0600`), guaranteed-
  cleaned-up temp files on the PVE host during the run, and in
  `/etc/hubinet-ops/agent.env` (`0640 root:hubinetops`) inside the CT —
  never in `inventory.yaml`, never printed to the console, never present
  in the fake/real command-invocation log.
- **TLS verification is never disabled.** The script only ever deploys CA
  trust material (the PVE cluster's own root CA when found, or an
  explicit `--pve-ca-path`) or relies on the CT's system trust store; it
  has no code path that writes `verify: false`.
- **The firewall boundary is applied and verified before the service is
  ever started**, and CT boot (`onboot=1`) is enabled only after every
  acceptance check in phase 12 passes — never earlier.

## Failure and rollback behavior

- **Before the service ever goes live** (any failure through the end of
  phase 10), nothing is exposed: the service is never started, and CT
  `onboot` is never enabled until phase 13, the very last step.
- **PVE identity objects this run created** (the user, role, and token,
  and their ACL grants) **are always rolled back automatically** on any
  failure — they are cheap to recreate and would otherwise block a
  retry, since this bootstrap refuses to reuse an existing identity of
  unknown provenance (see "Retrying" below).
- **The container itself is preserved by default** on failure, for
  forensic diagnosis (its Hubinet Ops service, if it was ever started, is
  stopped and disabled first; its `onboot` is forced back to `0`). Pass
  `--cleanup-on-failure` to have the script destroy it automatically
  instead.
- The script **never destroys, overwrites, adopts, or repurposes a VMID
  or PVE user/role/token that existed before this run started** — a
  pre-existing conflict is always a hard `STOP`, on every failure path,
  including cleanup.

## Retrying

This is a fresh-install bootstrap, not an idempotent/adopt-existing tool:

- A VMID that already exists → the script refuses to start (§ preflight).
  Remove it yourself (after confirming it's the failed run's own
  container, e.g. via the preserved-container path above) and re-run.
- A conflicting `hubinetops@pve` user, `HubinetOpsR0Auditor` role, or
  `r0-readonly` token that still exists (e.g. left over from an
  interrupted run, or a `--cleanup-on-failure`-less earlier attempt where
  the PVE-identity rollback nonetheless failed loudly and was reported)
  → the script refuses to reuse it. Either remove it manually after
  confirming it is safe to do so (`pveum user delete hubinetops@pve`,
  `pveum role delete HubinetOpsR0Auditor`), or — since PVE-identity
  rollback happens automatically on every failure — simply re-run the
  script; a normal failure never leaves these behind.

## Manually inspecting the generated PVE permissions

```bash
pveum acl list | grep hubinetops
pveum role list | grep HubinetOpsR0Auditor
pveum user token permissions hubinetops@pve r0-readonly --path /
```

The last command's output must show exactly `Sys.Audit` and `VM.Audit`
(propagated) and nothing else — see
`docs/operations/0.5-r0-operational-activation.md` section 2.5 for the
full manual verification procedure this script automates.

## After a successful run

The script prints a summary (VMID, CT address, PVE endpoint, credential
identity, permission profile, and PASS/FAIL for firewall/backend/
discovery) — never a secret. The R0 API bearer token Home Assistant needs
is in `/etc/hubinet-ops/agent.env` inside the new CT, not printed to the
console.

Next step: add the native Hubinet Ops integration in Home Assistant,
using the printed CT address on port 8787 and that bearer token — see
`docs/operations/0.5-ha-clean-break.md` for the full HA-side procedure
(including the mandatory zero-active-0.4-surface gate if this Home
Assistant instance still has the legacy 0.4 package/dashboard installed).

## What remains manual

- Real Home Assistant enrollment itself (config entry creation) — this
  script only prepares the R0 backend; HA-side steps are covered by
  `docs/operations/0.5-ha-clean-break.md` and
  `docs/operations/0.5-r0-operational-activation.md` section 6.
- The multi-day observation window
  (`docs/operations/0.5-r0-operational-activation.md` section 7) before
  declaring R0 operationally accepted.
- Multi-source bootstrap (this script, like the R0 config format itself,
  supports exactly one PVE source per run — see
  `docs/architecture/0.5-r0-read-only-runtime-activation.md` section 5).
- A `curl | bash` one-line distribution path — deferred as future
  packaging work; run this script from a real checkout of this
  repository for now.
