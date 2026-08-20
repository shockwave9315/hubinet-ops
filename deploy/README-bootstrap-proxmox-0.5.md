# Hubinet Ops 0.5 R0 — automated Proxmox bootstrap

`deploy/bootstrap-proxmox-0.5.sh` automates the manual procedure documented
in
[`docs/operations/0.5-r0-operational-activation.md`](../docs/operations/0.5-r0-operational-activation.md)
sections 1-4 (and the start/first-half of section 5): a fresh unprivileged
Debian 13 LXC, the R0 read-only runtime deployed into it via
`deploy/install-0.5.0-fresh.sh` (unmodified), a least-privilege PVE
identity, PVE TLS trust material, and the mandatory nftables firewall
boundary — in that order, ending with the service started and CT boot
enabled only after a genuine, contract-verified discovery success.

This script adds **no runtime mutation capability**. R0 remains read-only
end to end; the `pct`/`pveum` commands this script runs are one-shot,
human-invoked, PVE-host provisioning steps — structurally separate from
Hubinet Ops's own GET-only production PVE transport
(`app/inventory_pve_transport.py`).

> **Status**: implemented and unit/smoke-tested against a hermetic fake
> command layer only. It has **not** been run against a real Proxmox host.
> Before a first real run, review the "What this script proves, and what
> it does not" section below and the REAL-HOST PRECHECK commands in this
> branch's corrective-pass report.

## Prerequisites

- A Proxmox VE host you can run commands on as root (the script must run
  **on** the PVE host itself — it is not SSH-orchestrated).
- The standard PVE toolchain already present on any PVE host: `pct`,
  `pveum`, `pveam`, `pvesh`, `pvesm`, `nft`.
- `git` on the PVE host, plus either `jq` or `python3` (used for the
  security-relevant effective-permission verification — the script fails
  closed at preflight if neither is present rather than falling back to a
  regex-based JSON parser for that check).
- A storage backend supporting container rootdirs (`pvesm status
  --content rootdir`), and one supporting `vztmpl` if you want the script
  to auto-download the Debian 13 template.
- A network bridge (default `vmbr0`).
- The Home Assistant host/subnet's IPv4 CIDR that must be allowed to
  reach the R0 API (e.g. `203.0.113.50/32`) — there is no safe default
  for this; you must provide it.
- A **clean git checkout** of this repository on the PVE host (no
  uncommitted changes) — the script deploys from a real git commit, never
  a non-git tarball; see "Source provenance" below.

## Usage

Interactive (asks for the Home Assistant source CIDR, static network
details if you choose static networking, and exactly **one** upfront
confirmation of the complete plan -- VMID, template, storage, network, PVE
endpoint, TLS strategy, the detected source commit, and the HA source CIDR
all together, not a separate prompt per item; every other value has a
sensible auto-detected or documented default, including the container's
VMID):

```bash
sudo bash deploy/bootstrap-proxmox-0.5.sh
```

Non-interactive, fully deterministic (for scripted/repeatable activation
or CI-adjacent automation against a real lab host) — note `--expected-sha`
is **required** in this mode:

```bash
sudo bash deploy/bootstrap-proxmox-0.5.sh \
  --non-interactive --yes \
  --expected-sha "$(git rev-parse HEAD)" \
  --hostname hubinet-ops \
  --bridge vmbr0 \
  --network dhcp \
  --ha-source 203.0.113.50/32 \
  --pve-endpoint https://203.0.113.10:8006
```

An explicit `--vmid` is honored and never silently overridden; omit it to
let the script auto-detect the next free VMID via the real Proxmox
`/cluster/nextid` API instead:

```bash
sudo bash deploy/bootstrap-proxmox-0.5.sh \
  --non-interactive --yes \
  --expected-sha "$(git rev-parse HEAD)" \
  --vmid 150 \
  --ha-source 203.0.113.50/32
```

Static networking instead of DHCP:

```bash
sudo bash deploy/bootstrap-proxmox-0.5.sh \
  --non-interactive --yes \
  --expected-sha "$(git rev-parse HEAD)" \
  --ha-source 203.0.113.50/32 \
  --network static --ip 203.0.113.20/24 --gateway 203.0.113.1
```

Run `bash deploy/bootstrap-proxmox-0.5.sh --help` for the full flag
reference (all flags, all defaults).

## What it creates

On the Proxmox host:

- One new LXC container. **VMID**: auto-detected via `pvesh get
  /cluster/nextid` and shown in the pre-confirmation plan by default; an
  explicit `--vmid` is validated and never silently overridden, including
  on a same-host creation-time race (an auto-detected candidate may be
  safely recomputed on collision; an explicit `--vmid` collision is
  always a hard stop). Unprivileged, `onboot=0` until the very last step,
  firewall enabled on `net0`, default resources 1 core / 1024 MiB memory
  / 512 MiB swap / 8 GiB rootfs (all overridable), the newest Debian 13
  standard template found across **all** configured storages (auto-
  selected/downloaded), with `nesting=1` enabled specifically because
  Debian 13's systemd (>=257) requires it inside an unprivileged LXC to
  reach a healthy `systemctl is-system-running` (see the script's phase 4
  comments for the exact failing units this fixes:
  `dev-mqueue.mount`/`run-lock.mount`/`tmp.mount`).
- One dedicated read-only PVE identity: user `hubinetops@pve`, role
  `HubinetOpsR0Auditor`, a privilege-separated API token
  `hubinetops@pve!r0-readonly`, with the role granted to **both** the
  user and the token at path `/` with propagation — see
  `docs/operations/0.5-r0-operational-activation.md` section 2 for the
  exact reasoning (this is the minimal set R0's own discovery contract
  requires, not a guess). The script reads back the token's actual
  effective permissions afterward and asserts the sorted set of truthy
  privilege keys is **exactly** `{Sys.Audit, VM.Audit}` — an exact-set
  equality proof, not a check against a fixed list of privileges to
  avoid, so any unexpected privilege of any name fails the run closed.
  A random per-run identifier is embedded in the user's and token's own
  `--comment` — see "Ownership and rollback" below for why.
- Preflight's storage free-space check compares `pvesm status`'s
  Total/Used/Available columns correctly as **KiB** (PVE's actual unit for
  those columns) against the requested rootfs size — an earlier version of
  this check compared them as bytes, understating available space by
  roughly 1024x and falsely rejecting ordinary real storage. An
  unparseable Available value is a hard stop, never a silently-skipped
  check.
- Inside the CT: `nftables`, `curl`, and `iproute2` are explicitly
  installed and their presence verified (never assumed present on the
  base template) before the firewall and acceptance phases depend on
  them.
- Inside the CT: the R0 runtime (via the unmodified
  `deploy/install-0.5.0-fresh.sh`), a generated
  `/etc/hubinet-ops/inventory.yaml` (source-centric only — no VMID/
  resource list of any kind) and `/etc/hubinet-ops/agent.env` (the PVE
  token from above plus the R0 API bearer token the installer already
  generates), both `0640 root:hubinetops`.
- Inside the CT: an `nftables` ruleset (`/etc/nftables.conf`) restricting
  inbound TCP 8787 to exactly the configured HA source CIDR, and
  outbound traffic from the `hubinetops` service user to exactly the
  configured PVE endpoint on the port that endpoint is actually
  configured with (never an independently hardcoded `8006` — derived from
  `--pve-endpoint`; defaults to `8006` only when the endpoint itself
  omits a port), plus, only if the endpoint is configured as a hostname
  rather than a literal IP, a narrowly-scoped DNS-resolver allow rule (both
  UDP and TCP port 53, since a legitimate DNS response can require TCP
  fallback) — the same model documented in `deploy/README-0.5-firewall.md`.
  Post-activation verification checks the exact rule content **and order**
  within each chain, not a loose substring match.

  **Hostname PVE endpoints** are resolved to concrete IPv4 addresses
  *inside the CT's own network/resolver context* (via
  `deploy/lib/hubinet-ops-bootstrap-resolve-dns.py`) *before* the firewall
  ruleset is generated, and one exact egress-allow rule is written per
  resolved address — nftables itself resolves a bare hostname in an
  address expression to numeric addresses at rule-load time, and
  `nft list ruleset` afterward reports only the numeric address, never the
  hostname text, so an exact-text verifier comparing against the literal
  hostname would otherwise fail closed against an entirely valid
  configuration. Zero resolved addresses, or a resolution failure, is
  always a hard stop. **This resolved address set is fixed for the
  lifetime of the CT** — if internal DNS later moves the hostname to a new
  address, re-run this bootstrap to regenerate the firewall before the
  service can reach the new address; it is never silently re-resolved
  after the fact. The R0 runtime's own configured `pve_endpoint` (used for
  TLS certificate hostname verification) always stays the original
  hostname — only the firewall's permitted destination addresses are
  affected by this resolution step.

## Source provenance

This bootstrap deploys **only** from a real git checkout with a **clean**
working tree, at an exact commit you have explicitly confirmed:

- The script computes the full 40-character HEAD SHA of `--source-dir`.
- **Interactive mode**: the detected SHA is validated and printed as part
  of the single upfront plan; confirming that one plan (see "Usage" above)
  is what authorizes deploying exactly that commit -- there is no separate
  second "deploy this commit?" prompt.
- **Non-interactive mode**: you must pass `--expected-sha <full-sha>`; the
  script fails closed if it does not match HEAD exactly. `--yes` never
  substitutes for this -- an interactive run with `--yes` still validates
  the detected commit (clean tree, real git checkout) exactly the same
  way, it only skips the one remaining confirmation prompt.
- A dirty working tree (any uncommitted change) is always a hard stop.
- The transfer itself uses `git archive <the-confirmed-sha>` (never a
  moving `HEAD` reference, and never a non-git tarball fallback — an
  earlier implementation had one; it has been removed entirely, so a
  non-git `--source-dir` is now also a hard stop). `git archive` transfers
  tracked files only, so `.git`, gitignored venvs/caches/logs/runtime DBs,
  and any local untracked developer files (including secrets you may have
  sitting in your working tree) are never included.
- The deployed commit SHA is always logged in full (`Deploying source
  commit: <sha>`) — never a secret, always worth knowing exactly what was
  deployed.

A signed-release / `curl | bash` distribution trust chain is **explicitly
out of scope** for this wave — see "What remains manual" below. Run this
script from a real, trusted git checkout of this repository.

## What it never creates or does

- No static VMID/resource/CT/QEMU inventory anywhere.
- No mutation-shaped PVE privilege on the created user, role, or token —
  proven by an exact-set check, not merely a blacklist.
- No `verify=false`/`--insecure` TLS bypass — the script fails closed if
  it cannot determine valid CA trust material and the operator has not
  explicitly opted into system trust with `--tls-trust system`.
- No implicit fallback to the CT's system TLS trust store — that path
  requires an explicit `--tls-trust system` flag; omitting both
  `--pve-ca-path` and `--tls-trust` fails closed when no CA is found.
- No non-git source payload of any kind.
- No 0.4 migration, import, or coexistence of any kind.
- No SSH, MQTT, hostd, or forced-command wrapper of any kind.
- No arbitrary command execution — every `pct`/`pveum` invocation uses a
  fixed, quoted argument list; nothing here accepts free-form command
  text, and `eval` is never used.
- No host mutation of any kind (including `pveam update`/`download`)
  before you have seen and confirmed the full plan (VMID, template,
  source commit).

## Security model

- **Least privilege by construction, verified as an exact set**: the PVE
  role created is exactly `Sys.Audit,VM.Audit`, matching
  `app.inventory.provider.ENDPOINT_ACL_MATRIX`'s actual requirement (see
  the operational-activation runbook section 2.1). Verification asserts
  the token's actual effective privilege set equals that pair exactly —
  any missing required privilege OR any unexpected extra one (of any
  name) fails the run closed.
- **Secrets never appear as a literal command-line argument.** The PVE
  API token and the R0 API bearer token are never passed to
  jq/python3/awk/curl (or any other process) as a `-v`/`--arg`/positional
  argument or an exported environment variable — every helper that needs
  the secret's content takes a **file path** and reads it internally
  (e.g. the `awk` step that writes `agent.env` reads the PVE token via
  `getline` from a restrictive temp file inside its own script, never via
  `-v`; the discovery-acceptance check reads the R0 bearer token directly
  from `/etc/hubinet-ops/agent.env` inside the CT, never via a `curl -H`
  argument). Secrets exist only in restrictive (`0600`), guaranteed-
  cleaned-up temp files on the PVE host during the run, and in
  `/etc/hubinet-ops/agent.env` (`0640 root:hubinetops`) inside the CT.
- **TLS verification is never disabled**, and relying on the CT's system
  trust store instead of an explicit CA bundle requires an explicit
  `--tls-trust system` opt-in — never an implicit default.
- **The firewall boundary is applied and verified (exact content and
  order) before the service is ever started**, and CT boot (`onboot=1`)
  is enabled only after a real, contract-grounded discovery success (see
  below) — never merely because the service process is listening.

## Discovery acceptance — what "Discovery: PASS" actually proves

The final gate before CT `onboot=1` is enabled runs
`deploy/lib/hubinet-ops-bootstrap-accept.py` **inside** the new container
(python3, already guaranteed present by `install-0.5.0-fresh.sh`). It
proves a genuinely **committed, fresh, current** discovery success — not
merely `health == "healthy"` in isolation, which alone cannot distinguish
a committed/fresh/current result from a stale or partially-updated
combination of independently-updated fields:

1. Calls the authenticated `GET /r0/v1/backend` endpoint and validates a
   real, non-empty `backend_instance_id`.
2. Polls the authenticated `GET /r0/v1/snapshot` endpoint (bounded by
   `--discovery-timeout`, default 180 seconds), parsing the response as
   real JSON against the exact field and enum names defined by
   `app/inventory/publication.py` and
   `custom_components/hubinet_ops/contract/enums.py` in this repository —
   never a substring match against raw HTTP output.
3. Requires the configured source to report `SourceHealth.HEALTHY` and
   `SourceFreshness.FRESH` with a matching display name.
   `SourceHealth.SOURCE_UNAVAILABLE` and `SourceHealth.CONFIGURATION_ERROR`
   are terminal failures (never retried); `NOT_YET_OBSERVED`, `DEGRADED`,
   and healthy-but-stale are all treated as legitimate transient states
   worth polling through, up to the timeout.
4. Requires `latest_completed_outcome == "success"` (the literal value
   only a genuine committed success sets — never `"invalid"`/`"partial"`/
   `"configuration_error"`/`"source_unavailable"` from a degraded or
   rejected completion path), a real positive `last_committed_run_sequence`,
   a non-null `last_successful_observed_at`, and a non-null
   `committed_context` that matches `current_context` field-for-field
   (`source_config_revision`, `endpoint_id`, `canonical_transport_locator`,
   `canonicalization_contract_version`, `transport_trust_revision`) —
   proving the committed state corresponds to the currently active
   configuration, not a stale prior commit.
5. Requires `nodes` to be a non-empty list — a real PVE source necessarily
   has at least its own node; an empty `nodes[]` after an otherwise-healthy
   commit is suspicious and withheld from PASS. `resources[]` is
   deliberately **not** required to be non-empty (a fresh install may
   legitimately have zero guests yet).
6. For every resource the backend reports, validates
   `state_level == "discovered"`, `security_continuity != "trusted"`,
   `presence != "confirmed_removed"`, and that `effective_capabilities` is
   empty.

An unauthenticated `GET /r0/v1/health` response alone (checked earlier,
as a fast liveness probe) is **not** sufficient for a pass. A source stuck
in `not_yet_observed`, stale, an unsuccessful completion outcome, a
missing or mismatched committed context, zero nodes, a terminal failure
health state, an authentication failure, or a TLS failure never yields
`Discovery: PASS`, and CT `onboot` is never enabled unless this check
actually returns success. This script only ever reads the already-
published snapshot (`app/inventory/publication.py`'s read side) — it does
not activate attestation and does not change runtime architecture.

## Ownership and rollback (PVE identity)

The fixed-name PVE user/role/token this bootstrap creates live in a
**cluster-wide** identity namespace — two concurrent bootstrap runs (same
or different node), or any other creator, can race for that same name.
PVE itself resolves the race (only one `pveum user add` can win), but the
*losing* run's own call then fails with the object now existing regardless
— and rollback must never assume "an object matching our fixed name
exists" means "this run created it."

The fix: a random per-invocation `BOOTSTRAP_RUN_ID` is embedded in every
PVE object comment this bootstrap creates that supports one (the user, the
token — PVE roles have no comment/description field at all). On any
failure, rollback deletes an object only if this run can **prove**
ownership:

- a clean success already recorded in this run's own internal ledger, or
- for an ambiguous mutate-then-error result (this run's own call reported
  failure, but the object now exists), a read-back comment carrying this
  exact run's `BOOTSTRAP_RUN_ID` — which only this run's own successful
  create call could have written.

An object that cannot be proven owned this way is **preserved**, never
deleted, with a loud log message giving the exact manual-remediation
command to inspect and remove it yourself if you confirm it is safe. PVE
roles, having no provenance field at all, can only ever be proven owned
via the ledger — an ambiguous role-creation failure is always preserved.

## Failure and rollback behavior

- **Before the service ever goes live** (any failure through the end of
  phase 10), nothing is exposed: the service is never started, and CT
  `onboot` is never enabled until phase 13, the very last step.
- **PVE identity objects this run can prove it owns are rolled back
  automatically** on any failure (see "Ownership and rollback" above),
  gated on an "attempted" marker recorded *before* the first mutating call
  in that phase (not a success-only marker recorded after) — so a `pveum`
  command that mutates state but still reports a nonzero exit for an
  unrelated reason is still evaluated for cleanup. Every rollback action
  is idempotent. An object this run cannot prove it owns is always
  preserved, never deleted.
- **The container itself is preserved by default** on failure, for
  forensic diagnosis (its Hubinet Ops service, if it was ever touched, is
  stopped and disabled; its `onboot` is forced back to `0`). Pass
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
  container, e.g. via the preserved-container path above) and re-run, or
  simply omit `--vmid` to let the script pick a different free one.
- A conflicting `hubinetops@pve` user, `HubinetOpsR0Auditor` role, or
  `r0-readonly` token that still exists (e.g. left over from an
  interrupted run) → the script refuses to reuse it. Either remove it
  manually after confirming it is safe to do so (`pveum user delete
  hubinetops@pve`, `pveum role delete HubinetOpsR0Auditor`), or — since
  PVE-identity rollback happens automatically on every failure — simply
  re-run the script; a normal failure never leaves these behind.

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

## What this script proves, and what it does not

- It proves the created PVE credential's **effective permission set** is
  exactly `Sys.Audit,VM.Audit` at the moment of verification, the
  **firewall ruleset actually active** matches the intended exact
  content/order, and the backend reports a **genuinely healthy, matching
  discovery result** at acceptance time.
- It does **not** prove long-term operational health — see
  `docs/operations/0.5-r0-operational-activation.md` section 7 for the
  required multi-day observation window before declaring R0 operationally
  accepted.
- It has **not** been exercised against a real Proxmox host. All
  verification to date is against the hermetic fake-command test harness
  in `tests/_bootstrap_fake_pve.py` (`tests/test_bootstrap_proxmox_0_5.py`,
  local-safe) and `tests/test_bootstrap_proxmox_0_5_smoke.py` (the only
  file that executes the real script, and only inside
  `tests/shell/run_bootstrap_smoke_sandbox.sh`'s ephemeral-CI-only Docker
  sandbox). Real PVE-specific behavior this cannot exercise (exact `nft
  list ruleset` textual formatting, real `pveam`/`pvesh` output shapes,
  real DNS resolution behavior inside a fresh Debian 13 CT, etc.) is a
  known residual risk before a first real run — see the REAL-HOST
  PRECHECK block in this branch's corrective-pass reports for read-only
  commands to sanity-check these assumptions manually before a first real
  run.
- `.github/workflows/bootstrap-smoke.yml` wires the compliant sandbox
  (`tests/shell/run_bootstrap_smoke_sandbox.sh`) into GitHub Actions,
  narrowly path-filtered to bootstrap/sandbox-related changes and
  triggered on `pull_request` (plus manual `workflow_dispatch`) — the
  workflow itself sets only the `HUBINET_OPS_EPHEMERAL_CI=1` marker the
  launcher requires and invokes the launcher unmodified; it never sets
  `HUBINET_OPS_SYSTEM_SANDBOX` directly. As of this writing the workflow
  is wired but has not yet actually executed in a real GitHub Actions run
  (no PR has been opened) — treat that CI gate as **pending**, not passed,
  until it has.

## After a successful run

The script prints a summary (VMID, CT address, PVE endpoint, source
commit, credential identity, permission profile, and PASS/FAIL for
firewall/discovery) — never a secret. The R0 API bearer token Home
Assistant needs is in `/etc/hubinet-ops/agent.env` inside the new CT, not
printed to the console.

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
- A signed-release / `curl | bash` one-line distribution path — deferred
  as future packaging work; run this script from a real, trusted git
  checkout of this repository for now.

## REAL-HOST PRECHECK

Read-only, non-mutating commands to run **manually, yourself**, on a real
lab Proxmox host before a first real bootstrap run — to sanity-check the
assumptions this bootstrap's hermetic test harness cannot exercise (real
`pveum`/`pveam`/`pvesh`/`pvesm` output shapes, real DNS resolution
behavior, etc.). None of these mutate anything. An agent session must
never run these against a real host itself — this list is for your own
manual use.

```bash
# Next-free VMID mechanism (Blocker: removed the hardcoded VMID=110 default)
pvesh get /cluster/nextid --output-format json

# `pct config <vmid>` output shape -- confirm one "key: value" line per
# config entry, including a literal "nameserver: <ip>" line after
# `pct create --nameserver <ip>` (P2-2, third pass; read back by
# bootstrap-firewall.sh::_verify_ct_dns_resolver_matches_declared before
# any hostname PVE endpoint firewall is generated)
pct config <an-existing-test-vmid>

# pveum JSON output support, and the exact shape of user/token/role
# listings (including the `comment` field the rollback ownership-proof
# read-back requires on BOTH user and token listings -- P2-3, third pass;
# note PVE roles have no comment field at all, so role rollback is never
# automatic, see bootstrap-identity.sh's _role_object_owned_by_this_run)
pveum user list --output-format json
pveum role list --output-format json
pveum user token list <existing-user> --output-format json
pveum user token permissions <existing-user> <existing-token> \
  --path / --output-format json
# CONFIRMED against a real PVE 9.2.3 host: this returns a PATH-KEYED
# object -- {"/": {"Sys.Audit": 1, "VM.Audit": 1}} for a token holding
# those privileges at "/", observed literally as {"/":{}} for an empty
# grant -- never a flat object of privilege names directly at the top
# level. deploy/lib/bootstrap-identity.sh::_verify_effective_permissions
# reads privilege names only from the object at the exact requested path.
# If your PVE version's actual output differs from this, confirm the
# shape here BEFORE trusting phase 6's exact-set verification.

# Storage capacity reporting -- CONFIRM these Total/Used/Available values
# are in KiB on your PVE version, matching what
# deploy/lib/bootstrap-preflight.sh::_storage_has_free_space now assumes
# (Blocker 1: an earlier version of this parser incorrectly treated this
# column as bytes)
pvesm status --content rootdir
pvesm status --content vztmpl
pvesm status --storage <your-target-storage>

# Template listing/naming convention
pveam list local
pveam available --section system | grep -i debian-13

# PVE CA trust material
ls -la /etc/pve/pve-root-ca.pem

# jq/python3 availability (required by phase 1 preflight on the PVE host
# itself, for the effective-permission exact-set check)
command -v jq; command -v python3; python3 --version

# nftables availability (required inside the CT, provisioned by phase 8b)
nft --version

# Hostname PVE endpoint resolution sanity check (Blocker 4) -- if you plan
# to use a hostname --pve-endpoint, confirm it resolves the way
# deploy/lib/hubinet-ops-bootstrap-resolve-dns.py will resolve it INSIDE a
# fresh CT (i.e. using that CT's own resolver configuration, which may
# differ from the PVE host's own resolver):
#   pct exec <a-test-ct> -- python3 -c \
#     "import socket; print(sorted({i[4][0] for i in socket.getaddrinfo('<your-pve-hostname>', 443, socket.AF_INET, socket.SOCK_STREAM)}))"

# DNS resolver authority (P2-2, third pass) -- if you plan to use a
# hostname --pve-endpoint (--dns-resolver required), confirm on a real
# test CT built from the SAME template this bootstrap will select
# (phase 2) that `pct create --nameserver <ip>` is accepted and persisted,
# and that PVE actually regenerates /etc/resolv.conf inside the guest from
# it at container start -- this bootstrap's own
# _verify_ct_dns_resolver_matches_declared (bootstrap-firewall.sh) hard-
# stops before the firewall is ever generated if it cannot confirm this,
# so a real-environment mismatch here is a safe failure, not a silent one,
# but it is worth confirming ahead of time on a real host:
#   pct create <test-vmid> <same-template> --nameserver <your-resolver-ip> ...
#   pct start <test-vmid>
#   pct config <test-vmid> | grep '^nameserver:'
#   pct exec <test-vmid> -- cat /etc/resolv.conf
# IMPORTANT: if the selected Debian 13 standard template manages DNS via a
# stub resolver (e.g. systemd-resolved active by default), the last command
# above will show 127.0.0.53 rather than PVE's injected value regardless of
# the real upstream resolver -- in that case hostname PVE endpoint mode is
# not currently supported by this bootstrap (it will correctly, safely
# refuse to proceed rather than silently trust an unprovable resolver
# destination); use a literal-IP --pve-endpoint instead, which needs no DNS
# resolver at all and is unaffected by any of this.
```
