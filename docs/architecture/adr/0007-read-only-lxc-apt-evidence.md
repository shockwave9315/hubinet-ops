# ADR 0007: read-only LXC APT package evidence

Status: **PROPOSED**

While PROPOSED this ADR authorizes no implementation, schema, runtime code, or
test package. It amends no accepted ADR, adding a narrow read-only layer.

## 1. Context

The R0 runtime observes Proxmox through one GET-only HTTP transport
(`app/inventory_pve_transport.py`) whose token has effective permissions
exactly `{Sys.Audit, VM.Audit}`. It reads node/guest inventory, config, and
status, but cannot observe APT state inside a guest, and the PVE API exposes no
structured LXC command-execution endpoint suitable for this fixed-operation
package probe. Interactive console proxy paths exist, require broader `VM.Console`
authority, and do not provide the required fixed-operation
contract. **ADR 0007 must not widen the token.** Package/update *scanning* is
read-only, but has had no accepted evidence or transport contract.

## 2. Scope / non-goals

In scope: the Hubinet Ops backend container → a **running** Proxmox LXC → Debian 13 / Ubuntu
24.04+ APT package observation. Not decided here: QEMU, Windows, RPM, Alpine,
Docker image updates, self-update, package installation, approval, snapshots,
rollback, multi-node cluster routing architecture. If the evidence channel
cannot reach a resource under this narrow scope, package evidence is simply
**unavailable** for it; inventory discovery, reconciliation, and Home Assistant
presentation are unaffected.

## 3. Decision

Package evidence is obtained over a **separate, host-side, fixed-operation
channel**, not over the R0 PVE API token:

```text
backend container -> separate SSH credential/channel -> restricted PVE identity
      -> fixed wrapper -> pct exec <current VMID> -> fixed package observation
```

The deployment mechanism (dedicated user plus a single-purpose sudo rule, or an
equivalent forced-command arrangement) is an implementation detail; the
normative properties are not:

- the credential is separate from the R0 PVE API token, which gains nothing;
- no arbitrary SSH command, no shell supplied by the backend container, no generic action
  grammar, no package mutation verb;
- the caller supplies only narrowly validated locator/correlation data required
  for the scan; the wrapper performs package observation only;
- no static VMID allowlist; the VMID stays a locator, never identity (ADR 0001);
- compromise of it must never yield a general PVE shell or update capability.

## 4. Read-only APT semantics

A fresh observation reads the guest's real installed dpkg state, sources and
source parts, preferences/pinning, auth configuration, trusted keys, and
legitimate Acquire/proxy settings, then:

- downloads fresh repository indexes into transient scratch only, with APT
  lists, cache, log, and extended state redirected there and binary caches
  disabled where not needed;
- runs a **simulation only**, never invoking dpkg installation;
- clears every known update/package script hook through a command-line-loaded
  `-c` configuration file: `APT::Update::Pre-Invoke`, `Post-Invoke`,
  `Post-Invoke-Success`, `Post-Invoke-Stats` (all under `APT::Update`),
  `DPkg::Pre-Invoke`, `DPkg::Post-Invoke`, `DPkg::Pre-Install-Pkgs`,
  `AptCli::Hooks`;
- pins the APT solver and planner to their internal implementations.

The probe may create transient scan scratch state and nothing else; it must not
change persistent package, system, or APT configuration state. Scratch is
deleted on completion and lives in a transient location, so an interrupted
probe can never become durable package state.

**Guest-controlled execution fail-closed rule.** The probe may preserve guest repository
semantics: sources/source parts, preferences/pinning, auth configuration, trusted keys,
ordinary proxy URI configuration, and non-executable Acquire/network settings. It supports
only a fixed, reviewed APT execution profile. Any guest-controlled configuration capable of
launching an external command or selecting an unexpected executable/helper is **unsupported**
unless that exact mechanism is explicitly in that profile, including, but not limited to,
custom `Dir::Bin` paths, `Acquire::*::Proxy-Auto-Detect`, and custom `APT::Compressor::*::Binary`.
Before APT runs, the probe must reject such out-of-profile behavior as unavailable without executing the configured helper; ordinary proxy URIs remain supported, while proxy auto-detection commands require explicit admission to the fixed profile.

## 5. Evidence consistency and resource binding

Concurrency fails closed: if apt/dpkg locks indicate active package work,
evidence is unavailable and retried later. A consistency fingerprint is also
computed **before and after** the scan, binding at minimum SHA-256 of
`/var/lib/dpkg/status` plus the effective APT source/config/pinning inputs that
determine the candidate set; mtime and size alone are never the proof. Secrets
such as `auth.conf` contents are never published, and may contribute only to a
local digest or context check. If the before/after context differs, the
observation is discarded and classified as `consistency changed`; evidence is unavailable.

Package evidence belongs to the backend resource, never to a VMID. A scan
starts from the current authoritative inventory binding, and the evidence binds
`resource_id`, `inventory_source_id`, the current node and VMID locator,
`locator_generation`, the relevant inventory revision/context, the scan
timestamp, and a deterministic package-set fingerprint. After the probe
returns, the backend re-checks current inventory context; the evidence is
discarded if the resource is no longer current at that locator, the locator
generation changed, the source/context changed materially, or presence is no
longer suitable.

This does **not** solve the unobserved A→B same-VMID reuse case. Package
evidence is read-only and non-authorizing: it never grants
`security_continuity=trusted`, never transfers destructive policy, never
authorizes installation, and is never proof of workload incarnation continuity.
Blocker B stays OPEN and is not a prerequisite for it.

## 6. Failure semantics

The observable result distinguishes at least: success/current evidence;
unsupported; guest stopped or not observable; package manager busy; transport
unavailable; probe failed; consistency changed / evidence unavailable; stale (previously successful evidence aged past its freshness boundary). Only
a successful scan may state `pending_updates = 0`; a failed or unavailable one
is **unknown**, never zero, never a deletion signal (ADR 0002).

Successful evidence may carry, only where reliably established: package name,
installed and candidate version, origin/repository metadata, description and
other metadata, security classification, reboot information, a deterministic
observation fingerprint, and `scanned_at`. Unavailable metadata is not
invented.

## 7. Security / authority boundary

Even once accepted this ADR defines read-only package evidence architecture
only. It does not authorize `apt install`/`upgrade`/`full-upgrade`, update
jobs, approval, snapshots, rollback, lifecycle operations, mutation authority,
Phase 1C, or `security_continuity=trusted`. The product rule stands unchanged:
**NO AUTO-UPDATE** — installing updates always requires explicit operator
approval.

## 8. Implementation acceptance gate

Later implementation/test gates, not further research: root-side execution of the fixed probe
confirmed in a disposable Debian environment; Ubuntu 24.04 compatibility tested before
claiming that platform supported; tests prove hooks do not execute, persistent APT/dpkg state
is unchanged, concurrent package-manager or config changes discard evidence, and guest-configured
external helpers (at minimum `Proxy-Auto-Detect`) are rejected or suppressed before APT runs;
arbitrary command text remains unreachable through the channel.
