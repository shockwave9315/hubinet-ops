# Security model

The model is deny-by-default at independent boundaries:

- validated resource configuration controls monitoring and operator capabilities;
- the adapter exposes only fixed typed operations for a resource type;
- executor compatibility checks version, protocol, required actions, executor hash, profile hash, and profile validity;
- shared PVE host control rechecks VMID/type/action/snapshot ownership against root-owned allowlists;
- `hubinet-ops-hostd` additionally authenticates, bounds requests, optionally filters client IPs, deduplicates request IDs, and persists/audits jobs.

Repository allowlists are: observation 100–110; managed APT and maintenance 101–109; lifecycle and host-control snapshot/lifecycle 101–110. VM100 remains QEMU/HAOS observation-only. CT110 alone has self-update permission. Membership in observation never grants mutation, and YAML alone cannot expand the PVE allowlists.

The wrapper parses at most `action vmid [validated argument]`. Hostd accepts only typed JSON for fixed routes and limits bodies to 16 KiB. Both call argv-based subprocesses with `shell=False`; there is no arbitrary shell, console, terminal, MQTT command topic, or Home Assistant command field. Snapshot rollback/delete accept only the strict `hubinet-ops-{vmid}-{pre-update|manual}-{UTC timestamp}` namespace and recheck ownership metadata returned by Proxmox.

Destructive operations fail before mutation when another maintenance job, lifecycle, or scan is active; an unresolved plan could be invalidated; runtime is incompatible; executor/profile is incompatible; or a snapshot is missing/foreign. By default only one destructive maintenance job runs globally. Read-only refresh remains independent.

Hostd runs as root because Proxmox lifecycle/snapshot commands require it, but the systemd unit applies `NoNewPrivileges`, a strict protected filesystem, a bounded writable-state directory, management-address bind, and optional client allowlist. Its bearer token lives in `/etc/hubinet-ops/hostd.env`, never in the repository. The fixed self-update supervisor accepts no arguments and executes only a root-owned, non-writable approved release path.

Tokens, MQTT passwords, webhook IDs, SSH keys, authorization headers, hostd bearer values, and full commands are never logged or published. Updates still require manual approval. Automatic rollback additionally requires a real pre-update snapshot, a compatible executor/profile with a meaningful health contract, and the per-resource policy.
