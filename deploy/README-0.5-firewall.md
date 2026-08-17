# Hubinet Ops 0.5 (R0) firewall policy

This document is the mandatory, non-optional network policy paired with
`deploy/hubinet-ops-0.5.service`'s `0.0.0.0:8787` bind, per
`docs/architecture/0.5-r0-read-only-runtime-activation.md` sections 10,
25, and 26. The reachable bind is only safe together with this policy --
do not start the service before applying it.

## Required rules

```text
ALLOW  egress   R0 host  -> configured PVE endpoint HTTPS port (normally 8006)
ALLOW  ingress  HA host/subnet -> R0 host TCP 8787

DENY   ingress  everything else -> R0 host TCP 8787
DENY   (no special allowance needed for anything else --
        R0 never initiates any other class of outbound connection:
        no SSH, no MQTT, no direct guest network access)
```

R0 needs network access only for:

- outbound HTTPS to the one configured, active Proxmox VE endpoint;
- inbound read-only HTTP from the Home Assistant host/subnet, on the
  port this service listens on (8787 by default).

R0 needs no network access for SSH, `pct`/`qm`, hostd/forced-command, MQTT,
or any direct guest connection -- it has no dependency on any of those at
all (see `docs/architecture/0.5-r0-read-only-runtime-activation.md`
section 2's import denylist and section 26).

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

    # Inbound: only the Home Assistant host/subnet may reach the R0 API.
    ip saddr 192.0.2.50/32 tcp dport 8787 accept
    tcp dport 8787 drop
  }

  chain output {
    type filter hook output priority 0; policy accept;

    # Outbound, scoped to the R0 process only: PVE HTTPS, nothing else.
    meta skuid "hubinetops" ip daddr 192.0.2.10 tcp dport 8006 accept
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
dedicated, single-purpose rebuild (sections 0/3) -- not a general-purpose
box running unrelated services. If you run anything else on this host
that needs its own outbound access (package manager updates, NTP, ...),
add explicit allow rules for that separately; this policy only covers
what R0 itself needs, and it is your responsibility to extend it for
anything beyond R0 you choose to run on the same host.

```bash
ufw default deny outgoing
ufw allow out to 192.0.2.10 port 8006 proto tcp comment "R0 -> PVE"
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
   fallthrough for the `hubinetops` user's own traffic; for `ufw`,
   `ufw status verbose` should report `Default: deny (outgoing)`.
4. Only then enable and start the service together:
   `systemctl enable --now hubinet-ops`.

Do not expose `0.0.0.0:8787` to the whole LAN without this policy in
place -- the bind address alone is not a security boundary; the firewall
restriction to the Home Assistant host/subnet is the compensating
control this design depends on. Do not `systemctl enable` (with or
without `--now`) before step 2 -- an enabled-but-not-yet-started unit
will still auto-start unprotected on the next reboot.
