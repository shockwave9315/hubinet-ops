# Architecture and trust boundaries

`Settings.resources` is the canonical inventory. A legacy 0.2.4 `containers` mapping is converted in memory to LXC/APT resources; the file is not rewritten. Supplying both keys fails validation. `Settings.containers` remains a temporary LXC-only compatibility view for 0.3.x.

## Data flow

1. The telemetry loop calls `inspect` only when `monitoring.inspect` is enabled.
2. The independent `monitoring_scheduler` and recovery worker call APT scan only when `monitoring.update_scan` is enabled. Operator scan capability is not consulted for these read-only checks.
3. REST operator requests independently require the matching `operator_capabilities` flag.
4. Manual update rollback and explicit snapshot restore are distinct policies: the former is tied to a failed update job and `manual_rollback_allowed`; the latter is tied to `manual_snapshot_restore_allowed`, an eligible owned snapshot, explicit confirmation, and an action-specific root-owned PVE allowlist.
4. `ResourceExecutor` selects a validated adapter: LXC/APT, QEMU/HAOS read-only, or CT110 self-inspection.
5. The shared PVE host-control implementation revalidates action, VMID, resource type, observation, managed, maintenance, lifecycle, host-control access, and optional Hubinet-owned snapshot names. Both the forced-command wrapper and `hubinet-ops-hostd` call this implementation.
6. SQLite stores plans, jobs, events, and normalized resource state. MQTT and Home Assistant are projections.

Scheduled scans remain informational and never approve or install packages. VM100 (`haos`) and CT110 (`agent_self`) have `monitoring.update_scan: false` and are never routed to APT. CT101–CT109 share the manually approved update lifecycle:

`scan → waiting approval → preflight → snapshot (policy) → update → stabilization → verification → terminal result`

The recovery worker uses observed in-process unhealthy→healthy transitions, a single bounded worker, delay/cooldown, and active-work checks. It never approves or updates.

## Concurrency

Read-only refresh can proceed independently. Scans are serialized per resource, and by default only one destructive maintenance job can be queued/running globally. Every destructive job has an idempotent request ID, durable events, and a terminal state. Startup reconciliation observes real LXC/snapshot state but never automatically replays an uncertain destructive operation.

## CT110 offline boundary

CT110 self-inspection never recursively invokes SSH or its own API. Its in-guest agent may request ordinary host work while online, but Home Assistant lifecycle, snapshot, and self-update actions for CT110 go directly to `hubinet-ops-hostd` on PVE. Hostd stores jobs in its own SQLite database, so shutdown/restart results survive CT110 being offline. It binds only to a configured management address, optionally filters client IPs, requires a bearer token outside the repository, limits request bodies, and audits typed operations to journald.

## Compatibility

- `/api/v1/state` is canonical; the erroneous `/api/v1/states` spelling is not supported.
- `/api/v1/containers` and selected LXC action aliases remain for 0.3.x.
- `container_states` remains the SQLite table name; canonical database methods are aliases over it.
- old states without type normalize to `resource_type=lxc`, `adapter=apt`.
- CT101/CT106 MQTT entity IDs and legacy `hubinet/ops/ct/...` topics remain.
