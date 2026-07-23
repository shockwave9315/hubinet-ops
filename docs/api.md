# REST API 0.4.0

Every `/api/v1` route requires `Authorization: Bearer …`. Unknown resources return 404. Policy, compatibility, state, ownership, idempotency, or concurrency conflicts return an explicit 409; no endpoint accepts command text.

Canonical state and inventory:

- `GET /api/v1/resources`
- `GET /api/v1/resources/{vmid}`
- `GET /api/v1/state`
- `GET /api/v1/resources/{vmid}/state`
- `GET /api/v1/resources/{vmid}/events`
- `POST /api/v1/resources/{vmid}/refresh|scan|retry-healthcheck`

`/api/v1/states` does not exist. Historical `/containers/{vmid}/...` LXC aliases remain where documented for 0.3.x clients.

Active plans use VMID as the primary operator contract:

- `POST /api/v1/resources/{vmid}/plans/approve-active`
- `POST /api/v1/resources/{vmid}/plans/reject-active`

The backend requires exactly one unexpired waiting plan, an unchanged fingerprint, an allowed capability, no active destructive job, and a compatible executor. The older endpoints that accept `plan_id` remain compatibility aliases. Missing or ambiguous active plans return 409 rather than silently doing nothing.

Lifecycle and self-update:

- `POST /api/v1/resources/{vmid}/start|shutdown|reboot|force-stop`
- `POST /api/v1/resources/110/self-update`

Each accepts optional JSON `{"request_id":"..."}`. Repeating the same VMID/request ID and operation returns the same persisted job; reusing it for another operation returns 409.

Snapshots:

- `GET /api/v1/resources/{vmid}/snapshots`
- `POST /api/v1/resources/{vmid}/snapshots`
- `POST /api/v1/resources/{vmid}/snapshots/{name}/restore`
- `DELETE /api/v1/resources/{vmid}/snapshots/{name}`

List entries contain `name`, `description`, `created_at`, `kind`, `owned_by_hubinet_ops`, `rollback_eligible`, `delete_eligible`, and `source_job_id`. Explicit snapshot restore requires `manual_snapshot_restore_allowed`, `snapshot_rollback`, an existing rollback-eligible Hubinet-owned snapshot, no active destructive job, and the independent PVE restore allowlist. The compatibility `/snapshots/{name}/rollback` route has the same explicit-restore checks; it is not the manual update-rollback contract. Delete rejects names not owned by Hubinet Ops. `latest` is selected by parsed creation time, not lexical accident.

Manual update rollback remains `POST /api/v1/containers/{vmid}/rollback`. It requires `manual_rollback_allowed`, a failed/blocked/interrupted update job, and the snapshot recorded by that job.

Jobs expose operation type, request ID, status, stage, progress, result, error, and durable events. Terminal jobs are never replayed; startup reconciliation marks uncertain destructive work interrupted unless the real host state proves the requested terminal condition.
