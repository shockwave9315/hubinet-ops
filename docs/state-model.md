# Resource state and SQLite compatibility

Normalized state contains `resource_type`, `adapter`, `runtime_status`, health, hostname/OS/uptime/IPs, CPU/RAM/disk/network, services, Docker, Guest Agent status, monitoring, and operator capabilities. LXC also keeps `lxc_status`; QEMU has `qemu_status`.

HAOS and agent-self states represent unsupported APT values as `null`/unknown, never as a false zero. Explicit `null` after a failed final APT scan is preserved consistently in `packages_remaining_count`, `pending_updates`, and `updates.pending_count`.

SQLite keeps `/var/lib/hubinet-ops/ops.db`, VMID keys, tables, plans, jobs, events, snapshots, and state payload history. The schema migration is additive/idempotent (`user_version=300`); old state payloads default to LXC/APT. The historical `container_states` name remains on disk to avoid a destructive rebuild, with canonical resource methods layered over it.

Retained MQTT is a bounded projection, not an authority. Home Assistant never writes state back to SQLite.
