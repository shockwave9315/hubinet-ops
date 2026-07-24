# Security model

The model is deny-by-default at independent boundaries:

- validated resource configuration controls monitoring and operator capabilities;
- the adapter exposes only fixed typed operations for a resource type;
- executor compatibility checks version, protocol, required actions, executor hash, profile hash, and profile validity;
- shared PVE host control rechecks VMID/type/action/snapshot ownership against root-owned allowlists;
- `hubinet-ops-hostd` additionally authenticates explicit general/read/start, backend-operation, self-update, and offline-recovery scopes; bounds requests, optionally filters client IPs, deduplicates request IDs, and persists/audits jobs and recovery events.

Repository allowlists are: observation 100–110; managed APT and maintenance 101–109; lifecycle and host-control snapshot/lifecycle 101–110. VM100 remains QEMU/HAOS observation-only. CT110 alone has self-update permission. Membership in observation never grants mutation, and YAML alone cannot expand the PVE allowlists.

The wrapper parses at most `action vmid [validated argument]`. Hostd accepts only typed JSON for fixed routes and limits bodies to 16 KiB. Both call argv-based subprocesses with `shell=False`; there is no arbitrary shell, console, terminal, MQTT command topic, or Home Assistant command field. Snapshot rollback/delete accept only the Hubinet namespace. New update snapshots use physical alias `pre`, which is normalized to logical `pre-update`; legacy `pre-update`, `manual`, and bounded compact-manual names remain readable. Ownership metadata returned by Proxmox is always rechecked.

Destructive operations fail before mutation when another maintenance job, lifecycle, or scan is active; an unresolved plan could be invalidated; PVE runtime is incompatible; or a snapshot is missing/foreign. Guest-maintenance actions also fail on an incompatible executor/profile. Hostd lifecycle and snapshot operations intentionally remain available when a restored guest no longer contains the executor. By default only one destructive maintenance job runs globally. Read-only refresh remains independent and reports PVE runtime separately from guest health.

Hostd runs as root because Proxmox lifecycle/snapshot commands require it, but the systemd unit applies `NoNewPrivileges`, `ProtectSystem=strict`, a root-owned 0700 state directory, management-address bind, and optional client allowlist. The only writable paths are `/etc/pve`, `/var/lib/hubinet-ops-hostd`, `/run/lock`, `/var/log/pve/tasks`, `/run/lxc/lock`, `/var/lib/lxc`, `/etc/lvm/archive`, and `/etc/lvm/backup`; `/var/lib/lxc` is required for bounded `rules.seccomp.tmp` writes during LXC start/reboot, while whole `/etc`, `/run`, and `/var` are never opened. Four pairwise-distinct bearer values live in root-only `/etc/hubinet-ops/hostd.env`, never in the repository. CT110 receives only backend and self-update values; Home Assistant receives only general and recovery values. The fixed self-update supervisor accepts no arbitrary arguments and executes only a root-owned, non-writable approved release path.

General HA credentials cannot submit online shutdown/reboot/force-stop or snapshot mutations. Normal mutations require the backend scope after durable backend gating. Offline restore is a typed VMID-110-only recovery endpoint with exact confirmation, stopped-runtime and snapshot checks, and a durable marker created before rollback; it is never a best-effort backend check or automatic fallback. No backend, Home Assistant, or installer path automatically falls back to the recovery token.

Tokens, MQTT passwords, webhook IDs, SSH keys, authorization headers, hostd bearer values, and full commands are never logged or published. Updates still require manual approval. Automatic rollback additionally requires a real pre-update snapshot, a compatible executor/profile with a meaningful health contract, and the per-resource policy.
