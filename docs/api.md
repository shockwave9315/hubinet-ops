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

Jobs expose operation type, request ID, status, stage, progress, result, error, and durable events. Hostd additionally exposes authenticated `GET /api/v1/jobs/by-request/{vmid}/{request_id}` so the backend can locate an already persisted host job without submitting work. The lookup returns 404 when no matching job exists and never creates a record or starts a runner. Polling a live host job is read-only; self-update reads may refresh the existing supervisor result marker but never launch a rollout.

At backend startup, lifecycle, snapshot, and self-update jobs backed by hostd are looked up by their persisted VMID/request ID, contract-checked, and attached to that exact host job until it becomes terminal. Missing, mismatched, or unreachable host jobs become interrupted/unknown locally; they are never replayed with POST or DELETE. Locally executed work retains observational reconciliation.
