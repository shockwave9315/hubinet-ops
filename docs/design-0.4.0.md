# Hubinet Ops 0.4.0 control-plane design

## Goals and invariants

Version 0.4.0 manages LXC resources CT101–CT110 while keeping VM100 strictly
observation-only. The backend and its SQLite databases remain authoritative;
Home Assistant is a presentation and controlled-input layer. No API, MQTT
topic, SSH command, or dashboard field accepts arbitrary command text.

The release adds lifecycle, snapshot, update-plan, executor-compatibility, and
self-update control without assuming that the CT110 agent is always online.
Every destructive operation is authorized and validated independently at the
API, job engine, and PVE execution boundary.

## Control-plane boundaries

```text
Home Assistant
  |-- CT101–CT109 actions --> CT110 agent API --> fixed SSH command --> PVE
  |-- CT110 host actions ---------------------> hubinet-ops-hostd on PVE
  `-- telemetry <---------------- retained MQTT projections from CT110

PVE
  |-- hubinet-ops-hostd (HTTP, durable host jobs)
  |-- hubinet-ops-host (forced-command compatibility entry point)
  `-- shared host-control implementation
        |-- fixed pct/qm/pvesh argv
        `-- pct exec ... hubinet-maint for managed APT actions
```

The CT110 agent owns inventory, plans, update jobs, resource state, MQTT, and
notifications. It may request fixed PVE actions but never sends shell text.
The PVE control plane owns LXC runtime and snapshot truth. The independent
`hubinet-ops-hostd` service is the only path intended to start CT110 while the
agent is offline and is also the safe supervisor for CT110 lifecycle,
snapshots, and self-update.

The forced-command wrapper and hostd call one shared Python implementation for
action validation, allowlists, snapshot ownership, and fixed subprocess argv.
This avoids two copies of the security policy. The wrapper remains available
for the CT110 agent's existing SSH transport; hostd exposes only a narrow HTTP
surface for Home Assistant and installation-time read-only checks.

## Resource and capability policy

| Resource | Observe | Managed APT | Maintenance | Lifecycle | Snapshots | Self-update |
| --- | --- | --- | --- | --- | --- | --- |
| VM100 | yes | no | no | no | no | no |
| CT101–CT109 | yes | yes | yes | yes | yes | no |
| CT110 | yes | no | host-only | host-only | host-only | host-only |

Repository allowlists are installed root-owned on PVE. The backend additionally
checks configured `operator_capabilities`; host control independently checks its
own allowlists. A permissive dashboard or YAML file cannot bypass either guard.

## Executor compatibility contract

`hubinet-maint 0.4.0` adds the read-only `capabilities` action. Its result is:

```json
{
  "version": "0.4.0",
  "protocol_version": 1,
  "supported_actions": [
    "capabilities", "inspect", "check-updates", "preflight", "update",
    "healthcheck", "repair", "verify"
  ],
  "executor_sha256": "<sha256 of installed executor>",
  "profile_sha256": "<sha256 of /etc/hubinet-maint.json>",
  "profile_validation_status": "valid"
}
```

For CT101–CT109 the agent compares version, protocol, the complete required
action set, the executor hash distributed with the release, and the expected
profile hash for that VMID. Compatibility is refreshed during inspection and
immediately before any destructive maintenance or snapshot operation. A
mismatch blocks the job before snapshot creation or package changes and is
reported in resource state with the installed values and missing actions.

Profiles are schema-validated before installation and by `capabilities`. A
profile without a meaningful required-service or Docker health contract is
reported as `insufficient_health_contract`. It may still use guarded manual
lifecycle and snapshots, but automatic rollback based on an empty healthcheck
is disabled.

## Job model

SQLite jobs gain these durable fields:

- `operation_type`: update, lifecycle_start, lifecycle_shutdown,
  lifecycle_reboot, lifecycle_force_stop, snapshot_create,
  snapshot_rollback, snapshot_delete, retry_healthcheck, or self_update;
- `request_id`: caller-supplied idempotency key or a generated UUID;
- nullable `plan_id` for non-update jobs;
- JSON `result`, terminal `error`, `status`, `stage`, and `progress`;
- snapshot/source metadata where applicable.

`request_id` is unique per resource. Repeating a request returns the existing
job and never repeats the operation. At most one destructive maintenance job is
active globally by default, and at most one mutating job is active for a
resource. Read-only refresh does not consume that lock. Scan has its own lock
and cannot overlap a mutating job.

Every job emits append-only events and reaches an explicit terminal state:
`success`, `failed`, `blocked`, `rolled_back`, or `interrupted`. Startup
reconciliation marks abandoned queued/running jobs interrupted, reads actual
LXC state for lifecycle jobs, and may record that the requested terminal state
was already reached. It never automatically replays a destructive operation.

Hostd stores host jobs in a root-owned SQLite file on PVE. On restart it applies
the same reconciliation rule, preserving request idempotency and audit history
while refusing automatic destructive retries.

## Operation guards

Before mutation, the backend checks all applicable conditions:

1. the resource exists, is LXC, and the capability is enabled;
2. VMID and action are allowed at the host boundary;
3. no conflicting maintenance job, lifecycle operation, or scan is active;
4. no unresolved update plan would be invalidated by the operation;
5. actual runtime state matches the operation;
6. CT101–CT109 executor compatibility is current and valid;
7. snapshot names and metadata prove Hubinet Ops ownership;
8. the requested snapshot exists and is eligible;
9. `request_id` is valid and either new or matches the existing request.

Force stop, rollback, delete, update, and self-update are always treated as
destructive. Guards live in backend and host control, not only in Home
Assistant card conditions.

## Update plans

The UUID endpoints remain compatible, but Home Assistant uses:

- `POST /api/v1/resources/{vmid}/plans/approve-active`
- `POST /api/v1/resources/{vmid}/plans/reject-active`

The backend requires exactly one non-expired waiting plan for the resource,
revalidates its fingerprint and executor contract, checks capability and job
guards, then atomically approves or rejects it. Zero or multiple waiting plans
return HTTP 409 with a human-readable reason. Home Assistant sends only VMID.

After rollback, package verification fields are reset to unknown: in
particular, `packages_remaining_count` is null and `verification_status` does
not claim success. Failed verification events remain append-only. A recovery
scan may create a separate plan with a new ID and fingerprint.

## Snapshot ownership and retention

Hubinet Ops snapshot names are strictly:

```text
hubinet-ops-{vmid}-{pre-update|manual}-{YYYYMMDDTHHMMSSZ}
```

Creation also writes a structured description containing ownership, kind,
source job ID, and UTC creation time. Listing parses PVE snapshot metadata and
marks each entry with `owned_by_hubinet_ops`, `rollback_eligible`, and
`delete_eligible`. Rollback and delete reject foreign or malformed names even
if a caller learns them.

Retention defaults to five newest owned snapshots per resource and orders by
parsed `created_at`, never lexicographic accident. It never deletes foreign
snapshots, the current rollback source, or a snapshot referenced by an active
job. Only eligible older owned snapshots are pruned.

MQTT publishes summary fields only: count, latest name/time/kind, and operation
status. Full lists remain API/SQLite data and never become changing health
attributes.

## CT110 offline and self-update behavior

CT110 cannot reliably supervise its own stop or restart. Its start, shutdown,
reboot, force stop, snapshot, rollback, delete, and self-update requests go to
hostd. Home Assistant stores the hostd bearer token in secrets and can therefore
start CT110 while the agent API and MQTT availability are offline.

Hostd accepts an idempotent request, persists it, returns a job identifier, and
executes fixed `pct` operations. CT110 shutdown/reboot responses do not depend
on the agent surviving the operation. Self-update is staged and supervised on
PVE; hostd records the result and validates the agent after it returns. Hostd
does not expose package-manager or arbitrary shell arguments.

## Hostd security

- root-owned systemd service and configuration;
- bearer token read from an environment file outside the repository;
- bind address and port configured explicitly, with optional client CIDR/IP
  allowlist;
- bounded request line/body and strict JSON schema;
- fixed action and VMID allowlists;
- strict request ID and snapshot-name validation;
- `subprocess` argv lists only, never `shell=True`;
- one shared host-control implementation with the forced-command wrapper;
- audit records to journald without tokens or request bodies;
- `/health` plus authenticated job/resource endpoints;
- root-owned SQLite job state surviving CT110 and hostd restarts.

## Home Assistant contract

Dashboard controls are generated from capabilities. VM100 has no controls.
CT101–CT109 expose update, lifecycle, force stop, health retry, and snapshot
actions. CT110 uses hostd-backed services for lifecycle/snapshots/self-update.
Every action requires confirmation and surfaces success or failure through a
persistent notification; approve/reject never reads `active_plan_id` from
health attributes.

Dashboard timestamps are parsed defensively and rendered in local
`DD.MM.YYYY HH:MM` form without microseconds. Unknown, null, and invalid values
render as unavailable. Plan/job presentation uses descriptive operation,
stage, progress, and IDs shortened to at most eight characters.

The dedicated bounded MQTT attributes topic, serialized-payload deduplication,
10 KB budget, and Recorder exclusions from 0.3.2 remain in force.

## Transactional rollout

`deploy/upgrade-0.4.0-from-pve.sh` performs one transaction across:

1. backups of wrapper/shared host code, hostd, units/config, and allowlists;
2. backups of CT101–CT109 executor and profile, using `pct exec` for running
   containers and bounded mount/unmount for stopped containers;
3. a consistent CT110 application/database backup with the SQLite writer
   stopped;
4. staged syntax/schema/hash/capability validation;
5. atomic replacement of host components and every managed executor/profile;
6. CT110 application/database migration and service start;
7. read-only validation of hostd health, `/api/v1/state`, inventory 100–110,
   fresh telemetry, executor compatibility, and snapshot listing.

The installer never starts or stops an LXC as an installation shortcut and
never runs lifecycle, snapshot mutation, update, or maintenance actions.

Any layer failure restores all layers changed in that attempt: CT executors and
profiles, hostd/shared wrapper/allowlists, CT110 application and database, and
prior service state. Rollback reports incomplete restoration explicitly.

The separate Home Assistant installer backs up package, generated dashboard,
secrets, and configuration, stages files, runs `ha core check`, and restores all
files on failure. It never edits the entity registry and never automatically
restarts Home Assistant.

## Test strategy

Module-level tests use fake executors, host controllers, clocks, MQTT clients,
temporary SQLite databases, and simulated process output. Runtime shell tests
replace `pct`, `qm`, `pvesh`, SSH, systemd, and network clients. They must not
contact PVE, LXC, Home Assistant, MQTT, Docker, or private-network endpoints.

Development runs target only changed modules. Immediately before the final
push, the complete Python suite, current 0.4.0 runtime smokes, compilation,
YAML/dashboard checks, shell syntax, tracked-file validation, and whitespace
checks run exactly once.
