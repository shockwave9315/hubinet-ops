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
for your environment.

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

    # Outbound: only the configured PVE endpoint's HTTPS port.
    ip daddr 192.0.2.10/32 tcp dport 8006 accept
  }
}
```

## Example: `ufw`

```bash
ufw allow from 192.0.2.50/32 to any port 8787 proto tcp comment "HA -> R0"
ufw deny 8787/tcp
ufw allow out to 192.0.2.10 port 8006 proto tcp comment "R0 -> PVE"
```

## Verifying the pairing before go-live

1. Confirm `deploy/hubinet-ops-0.5.service` is not yet started
   (`systemctl status hubinet-ops` should show `inactive`/`disabled`).
2. Apply the firewall policy above.
3. Confirm the policy is active (`nft list ruleset` / `ufw status verbose`).
4. Only then `systemctl enable --now hubinet-ops`.

Do not expose `0.0.0.0:8787` to the whole LAN without this policy in
place -- the bind address alone is not a security boundary; the firewall
restriction to the Home Assistant host/subnet is the compensating
control this design depends on.
