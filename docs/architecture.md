# Architecture and trust boundaries

`Settings.resources` is the canonical inventory. A legacy 0.2.4 `containers` mapping is converted in memory to LXC/APT resources; the file is not rewritten. Supplying both keys fails validation. `Settings.containers` remains a temporary LXC-only compatibility view for 0.3.x.

## Data flow

1. The telemetry loop calls `inspect` only when `monitoring.inspect` is enabled.
2. The independent `monitoring_scheduler` and recovery worker call APT scan only when `monitoring.update_scan` is enabled. Operator scan capability is not consulted for these read-only checks.
3. REST operator requests independently require the matching `operator_capabilities` flag.
4. `ResourceExecutor` selects a validated adapter: LXC/APT, QEMU/HAOS read-only, or CT110 self-inspection.
5. The PVE forced-command wrapper revalidates action, VMID, resource type, observation access, managed-read access, maintenance access, lifecycle access, and optional snapshot names.
6. SQLite stores plans, jobs, events, and normalized resource state. MQTT and Home Assistant are projections.

Observation-only APT resources therefore continue to collect telemetry and update availability without creating an unapprovable plan. Scheduled scans are informational: they never create an update job or install packages. VM100 (`haos`) and CT110 (`agent_self`) have `monitoring.update_scan: false` and are never routed to APT. CT106 retains the manually approved update lifecycle:

`scan → waiting approval → preflight → snapshot (policy) → update → stabilization → verification → terminal result`

The recovery worker uses observed in-process unhealthy→healthy transitions, a single bounded worker, delay/cooldown, and active-work checks. It never approves or updates.

## Concurrency

The existing per-VMID scan/manual-operation lock serializes scan, approval, rollback, and lifecycle for a resource. Update jobs remain single-worker and terminal-state writes are ordered before follow-up observations. Telemetry I/O for one resource does not hold the locks of another resource. CT110 self-inspection never recursively invokes SSH or the Hubinet Ops API.

## Compatibility

- `/api/v1/containers` and its LXC action aliases remain for 0.3.x.
- `container_states` remains the SQLite table name; canonical database methods are aliases over it.
- old states without type normalize to `resource_type=lxc`, `adapter=apt`.
- CT101/CT106 MQTT entity IDs and legacy `hubinet/ops/ct/...` topics remain.
