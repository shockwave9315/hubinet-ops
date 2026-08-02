# Architecture and trust boundaries

`Settings.resources` is the canonical inventory. A legacy 0.2.4 `containers` mapping is converted in memory to LXC/APT resources; the file is not rewritten. Supplying both keys fails validation. `Settings.containers` remains a temporary LXC-only compatibility view for 0.3.x.

## Data flow

1. The telemetry loop calls `inspect` only when `monitoring.inspect` is enabled.
2. The independent `monitoring_scheduler` and recovery worker call APT scan only when `monitoring.update_scan` is enabled. Operator scan capability is not consulted for these read-only checks.
3. REST operator requests independently require the matching `operator_capabilities` flag.
4. Manual update rollback and explicit snapshot restore are distinct policies. Normal restore enters through the CT110 backend, which atomically checks waiting/approved plans and the global destructive-job lock while inserting the local job before any hostd POST. The PVE layer independently checks its restore allowlist.
4. `ResourceExecutor` selects a validated adapter: LXC/APT, QEMU/HAOS read-only, or CT110 self-inspection.
5. The shared PVE host-control implementation revalidates action, VMID, resource type, observation, managed, maintenance, lifecycle, host-control access, and optional Hubinet-owned snapshot names. Both the forced-command wrapper and `hubinet-ops-hostd` call this implementation.
6. SQLite stores plans, jobs, events, and normalized resource state. MQTT and Home Assistant are projections.

Scheduled scans remain informational and never approve or install packages. VM100 (`haos`) and CT110 (`agent_self`) have `monitoring.update_scan: false` and are never routed to APT. CT101–CT109 share the manually approved update lifecycle:

`scan → waiting approval → preflight → snapshot (policy) → update → stabilization → verification → terminal result`

The recovery worker uses observed in-process unhealthy→healthy transitions, a single bounded worker, delay/cooldown, and active-work checks. It never approves or updates.

## Concurrency

Read-only refresh can proceed independently. Scans are serialized per resource, and by default only one destructive maintenance job can be queued/running globally. Every destructive job has an idempotent request ID, durable events, and a terminal state. Startup reconciliation never replays uncertain destructive work. For hostd-backed operations it performs authenticated read-only lookup by VMID/request ID, verifies the operation and argument, and polls only the matching persisted host job; locally executed work uses read-only state observation.

## CT110 offline boundary

CT110 self-inspection never recursively invokes SSH or its own API. Normal shutdown, reboot, force-stop, snapshot create/restore/delete, and self-update enter through the backend; only starting an already stopped CT110 may go directly from Home Assistant to hostd. Hostd separates general read/start, backend-operation, self-update, and offline-recovery bearer scopes.

Offline CT110 restore and emergency force-stop are distinct break-glass endpoints with exact confirmations and the recovery scope. Offline restore additionally requires CT110 stopped, no active host job, and an owned rollback-eligible snapshot. Hostd writes a durable recovery event while queued and atomically records `mutation_started_at` immediately before the destructive controller call. On the next backend start, a succeeded restore event invalidates restored active plans/jobs. A failed or interrupted restore whose mutation marker exists preserves that terminal status and unknown outcome while conservatively applying the same invalidation. A queued interruption without the marker is audit-only, and force-stop events never use snapshot invalidation. The backend clears stale state, writes the local audit record, and acknowledges only after that commit. Reprocessing the same recovery ID is idempotent and never replays the restore.

## Compatibility

- `/api/v1/state` is canonical; the erroneous `/api/v1/states` spelling is not supported.
- `/api/v1/containers` and selected LXC action aliases remain for 0.3.x.
- `container_states` remains the SQLite table name; canonical database methods are aliases over it.
- old states without type normalize to `resource_type=lxc`, `adapter=apt`.
- CT101/CT106 MQTT entity IDs and legacy `hubinet/ops/ct/...` topics remain.
# 0.4.3 supervised update split

CT110 Debian maintenance and Hubinet Ops application rollout are independent state machines. Both cross a durable PVE boundary before mutation, persist request identity outside CT110, and reconcile terminal supervisor evidence after backend/hostd restart. Debian APT never starts without exact snapshot proof; application code never executes before immutable release verification.
