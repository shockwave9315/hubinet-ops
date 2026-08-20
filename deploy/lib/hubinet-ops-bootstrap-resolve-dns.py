#!/usr/bin/env python3
"""Hubinet Ops 0.5 R0 bootstrap PVE-endpoint hostname resolution.

Invoked INSIDE the newly-created CT (via `pct exec <vmid> -- python3
<this-file> <hostname>`), never on the Proxmox host. python3 is
guaranteed present inside the CT because deploy/install-0.5.0-fresh.sh
already requires it there. Resolution is deliberately performed inside
the CT's own network/resolver context, since that is where the R0
runtime will actually operate -- not the PVE host's.

Why this exists: nftables (`nft`) resolves a bare hostname in an address
expression to numeric address(es) at rule-LOAD time; `nft list ruleset`
afterward reports the resolved numeric address, never the original
hostname text. A firewall-verification step that expects the literal
hostname string back would then fail closed against an otherwise-correct
configuration. The fix is to resolve the hostname to concrete IPv4
addresses ONCE, here, before generating the nftables ruleset at all, and
generate one exact egress-allow rule per resolved address -- see
deploy/lib/bootstrap-firewall.sh's phase10_firewall.

Prints one IPv4 address per line to stdout, deduplicated and
deterministically sorted, for every resolved A record. Exit 0 with no
output means "resolved to zero usable IPv4 addresses" -- the caller must
treat that as a hard stop, never an empty-but-acceptable firewall rule
set. Exit nonzero means resolution itself failed (e.g. NXDOMAIN, no
resolver reachable).
"""
from __future__ import annotations

import socket
import sys


def main() -> int:
    if len(sys.argv) != 2:
        print("FAIL usage: hubinet-ops-bootstrap-resolve-dns.py <hostname>")
        return 1
    host = sys.argv[1]
    try:
        # Port is irrelevant to which A records exist; a fixed placeholder
        # is used only because getaddrinfo requires one.
        infos = socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)
    except OSError as exc:
        print(f"FAIL dns-resolution-failed host={host!r} error={exc}")
        return 1
    addresses = sorted({info[4][0] for info in infos})
    if not addresses:
        print(f"FAIL no-ipv4-addresses-resolved host={host!r}")
        return 1
    for address in addresses:
        print(address)
    return 0


if __name__ == "__main__":
    sys.exit(main())
