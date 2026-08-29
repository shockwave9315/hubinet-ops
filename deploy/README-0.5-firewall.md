# Hubinet Ops 0.5 (R0) firewall policy

This document is the mandatory, non-optional network policy paired with
`deploy/hubinet-ops-0.5.service`'s `0.0.0.0:8787` bind, per
`ARCHITECTURE.md`. The reachable bind is only safe together with this
policy -- do not start the service before applying it.

## Required rules

```text
ALLOW  egress   R0 host  -> configured PVE endpoint HTTPS port (normally 8006)
ALLOW  egress   R0 host  -> configured PVE host SSH TCP 22 (forced scan helper)
ALLOW  ingress  HA host/subnet -> R0 host TCP 8787

DENY   ingress  everything else -> R0 host TCP 8787
DENY   (no special allowance needed for anything else --
        R0 never initiates any other class of outbound connection:
        no MQTT and no direct guest network access)
```

R0 needs network access only for:

- outbound HTTPS to the one configured, active Proxmox VE endpoint;
- outbound SSH to that PVE host for the pinned-key, package-scan-only forced
  command;
- inbound read-only HTTP from the Home Assistant host/subnet, on the
  port this service listens on (8787 by default).

R0 needs no MQTT or direct guest-network access. `pct` runs only behind the PVE
forced helper; it is never exposed as a backend command-string interface.

This policy must ALSO account for two things a naive "only the explicit
rules above" reading misses:

- **Loopback.** Any local process on the R0 host itself (a health check,
  a one-shot diagnostic curl, this repository's own bootstrap acceptance
  checks) reaches the service over `127.0.0.1`, which is not part of the
  HA host/subnet -- an ingress rule scoped only to the HA CIDR, with no
  loopback exemption, silently blocks every local caller too. Loopback
  traffic must always be permitted, independent of the HA-scoped rule.
- **Replies.** The R0 process itself runs as the dedicated `hubinetops`
  user (see `deploy/hubinet-ops-0.5.service`'s `User=hubinetops`). Its
  own HTTP *replies* to an already-accepted inbound connection -- from
  the HA host, or from a local loopback client -- are themselves
  `hubinetops`-owned outbound packets. An `output` policy that only
  allows `hubinetops` to reach the configured PVE endpoint/DNS resolver
  and drops everything else would drop those replies too: the inbound
  SYN gets accepted, but every response silently vanishes, and the
  connection hangs until the client times out. The fix is the standard
  stateful-firewall shape -- explicitly allow reply traffic on
  connections this firewall already decided to accept
  (`ct state established,related`), which is **not** the same as opening
  outbound access: it only ever matches packets belonging to a flow this
  firewall itself already let in, so it grants `hubinetops` no ability to
  originate any *new* outbound connection beyond the explicit PVE/DNS
  allow-list below.

## Example: `nftables`

Adjust interface names, the PVE endpoint address, and the HA host/subnet
for your environment. Outbound restriction is scoped to the dedicated
`hubinetops` service user (`meta skuid`) rather than a host-wide default-
deny `output` policy, so this table cannot silently break unrelated host
traffic (SSH, package updates, other services) that this document does
not cover -- it genuinely restricts only what R0 itself is allowed to
reach, which is the exact claim this policy makes.

```nft
table inet hubinet_ops_r0 {
  chain input {
    type filter hook input priority 0; policy accept;

    # Loopback is always reachable -- local health checks/diagnostics
    # (127.0.0.1) never match the HA-scoped rule below and must not be
    # silently dropped by the fallthrough deny. Interface-based, not
    # address-based: loopback traffic can only ever originate from
    # within this host's own network namespace, so this is not a
    # spoofable widening of trust.
    iifname "lo" accept
    # Inbound: only the Home Assistant host/subnet may reach the R0 API.
    ip saddr 192.0.2.50/32 tcp dport 8787 accept
    tcp dport 8787 drop
  }

  chain output {
    type filter hook output priority 0; policy accept;

    # Reply traffic on a connection this firewall already accepted
    # (the R0 process's own HTTP responses to HA or to a local loopback
    # client) must always be allowed back out, or every accepted inbound
    # connection above will hang -- see "Replies" above. This does NOT
    # permit hubinetops to originate any NEW outbound connection; a new
    # connection attempt is still evaluated only against the explicit
    # allow-list immediately below.
    ct state established,related accept
    # Outbound, scoped to the R0 process only: PVE HTTPS, nothing else.
    meta skuid "hubinetops" ip daddr 192.0.2.10 tcp dport 8006 accept
    # Package scanning: the same pinned PVE host, forced-command SSH only.
    meta skuid "hubinetops" ip daddr 192.0.2.10 tcp dport 22 accept
    # If source.pve_endpoint in inventory.yaml uses a hostname rather
    # than a literal IP, hubinetops also needs to resolve it -- allow
    # only your own local/internal DNS resolver, never DNS at large.
    # Simplest alternative: configure pve_endpoint as a literal IP
    # address instead, and omit this rule entirely.
    # meta skuid "hubinetops" ip daddr <your-resolver-ip> udp dport 53 accept
    meta skuid "hubinetops" drop
  }
}
```

## Example: `ufw`

`ufw` has no built-in per-process/per-user matching, so this example
uses a host-wide default-deny outgoing policy instead. This is
appropriate specifically because the R0 target host is a clean,
dedicated, single-purpose rebuild -- not a general-purpose
box running unrelated services. If you run anything else on this host
that needs its own outbound access (package manager updates, NTP, ...),
add explicit allow rules for that separately; this policy only covers
what R0 itself needs, and it is your responsibility to extend it for
anything beyond R0 you choose to run on the same host.

Unlike the raw `nftables` example above, `ufw` does not need an explicit
loopback/established-related rule added here: `ufw`'s own default
`/etc/ufw/before.rules` (applied automatically on `ufw enable`,
independent of any rule shown below) already unconditionally accepts
loopback interface traffic and `state RELATED,ESTABLISHED` traffic in
both directions. This is standard `ufw` behavior, not something this
policy adds.

```bash
ufw default deny outgoing
ufw allow out to 192.0.2.10 port 8006 proto tcp comment "R0 -> PVE"
ufw allow out to 192.0.2.10 port 22 proto tcp comment "R0 -> PVE forced package scan"
# If source.pve_endpoint uses a hostname, also allow resolution against
# your own local/internal DNS resolver only (never DNS at large):
#   ufw allow out to <your-resolver-ip> port 53 proto udp comment "R0 -> DNS resolver"

ufw allow from 192.0.2.50/32 to any port 8787 proto tcp comment "HA -> R0"
ufw deny 8787/tcp
```

## Verifying the pairing before go-live

The installer (`deploy/install-0.5.0-fresh.sh`) installs the unit but
deliberately does **not** start or enable it -- a host reboot before the
firewall is in place must not silently bring the service up unprotected.
Apply the firewall before the service is ever started or enabled:

1. Confirm `deploy/hubinet-ops-0.5.service` is not yet started or enabled
   (`systemctl status hubinet-ops` should show `inactive`/`disabled`).
2. Apply the firewall policy above.
3. Confirm the policy is active (`nft list ruleset` / `ufw status verbose`)
   and genuinely restrictive -- for the nftables example, `nft list
   ruleset` should show `meta skuid "hubinetops" drop` as the effective
   fallthrough for the `hubinetops` user's own traffic, `iifname "lo"
   accept` as the first `input` rule, and `ct state established,related
   accept` as the first `output` rule (without it, HA's own requests will
   be accepted but every reply from R0 will silently hang); for `ufw`,
   `ufw status verbose` should report `Default: deny (outgoing)`.
4. Only then enable and start the service together:
   `systemctl enable --now hubinet-ops`.

Do not expose `0.0.0.0:8787` to the whole LAN without this policy in
place -- the bind address alone is not a security boundary; the firewall
restriction to the Home Assistant host/subnet is the compensating
control this design depends on. Do not `systemctl enable` (with or
without `--now`) before step 2 -- an enabled-but-not-yet-started unit
will still auto-start unprotected on the next reboot.
