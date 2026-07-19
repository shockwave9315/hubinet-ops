# State model

Hubinet Ops does not expose one overloaded status. Each container state has independent dimensions.

| Field | Values | Meaning |
| --- | --- | --- |
| `health_status` | `healthy`, `degraded`, `critical`, `unknown`, `offline` | Latest observed runtime health |
| `update_status` | `unknown`, `scanning`, `up_to_date`, `update_available` | Latest package scan result |
| `operation_status` | `idle`, `waiting_approval`, `running`, `success`, `failed`, `rolled_back`, `manual_intervention` | Current or last operator workflow |
| `job_stage` | `idle`, `scanning`, `preflight`, `snapshot`, `updating`, `waiting_services`, `healthcheck`, `repair`, `rollback`, `rollback_wait`, `rollback_healthcheck`, `completed`, `failed` | Detailed job phase |
| `last_operation_result` | `success`, `failed`, `rolled_back`, `manual_intervention`, `null` | Durable outcome independent of later scans |
| `lifecycle_status` | `idle`, `running`, `success`, `failed` | Fixed start/shutdown/reboot operation |
| `verification_status` | `unknown`, `running`, `passed`, `warning`, `failed` | Final APT/dpkg/service/Docker verification |
| `expected_lxc_status` | `running`, `stopped`, `null` | Expected result of the most recent lifecycle operation |
| `intentional_shutdown` | boolean | Suppresses only the availability alert caused by a confirmed graceful shutdown |
| `lifecycle_health_pending` | boolean | Start/reboot reached `running`, but service health still awaits telemetry |
| `recovery_scan_status` | `disabled`, `idle`, `scheduled`, `running`, `completed`, `blocked`, `cancelled`, `failed` | Delayed recovery scan state |

Examples:

- Healthy and current: `healthy / up_to_date / idle / null`.
- Healthy with a plan: `healthy / update_available / waiting_approval / null`.
- Update failed but rollback recovered health: `healthy / update_available / rolled_back / rolled_back`.
- A later scan may produce `healthy / update_available / failed / rolled_back`; this is valid because scan, operation, and historical result describe different facts.
- Rollback timeout: `critical / update_available / manual_intervention / manual_intervention`.

The dashboard shows each dimension separately. It must never label pending updates as the result of a failed operation.

## Migration

Opening a 0.2.0 database adds `jobs.progress`, `job_events`, indexes, and SQLite `user_version=201`. Legacy `status`, `health`, `job_status`, and old stage names are mapped into the explicit fields. Plans, jobs, snapshots, errors, and existing payload details remain intact. Interrupted queued/running jobs retain the existing restart behavior and become terminal `failed` stages rather than appearing live after restart.

## Progress

Progress is a best-effort UI estimate. It is monotonic within a job, clamped to `0..100`, and can reach 100 only with a terminal event. APT package counts and service restarts do not always expose exact completion, so no wall-clock guarantee is implied.
