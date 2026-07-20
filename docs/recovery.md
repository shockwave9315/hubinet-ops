# Recovery and diagnosis

## Diagnose a stuck job

1. Read the container's `operation_status`, `job_stage`, `job_progress`, `active_job_id`, and `last_error` separately.
2. Read `GET /api/v1/jobs/<job_id>/events?limit=200` with the bearer token.
3. Check whether the latest events show initial grace, Docker unavailable, container counts, repair, rollback wait, or rollback timeout.
4. Use the dashboard `Refresh` action. It performs inspect only and cannot approve or update.
5. Use `Retry healthcheck` only after services have had time to recover. It creates an idempotent durable `retry_healthcheck` job with its own events.
6. Use snapshot rollback only for a listed Hubinet-owned snapshot. The backend rechecks ownership, compatibility, policy, active work, runtime, and snapshot presence even when the UI card is visible.

An agent restart reconciles queued/running jobs with actual LXC/snapshot state. It can confirm an already reached terminal condition, otherwise it marks the job interrupted; it never silently replays APT, lifecycle, snapshot deletion, or rollback. Host jobs for CT110 remain in hostd's independent SQLite store while CT110 is offline.

After rollback, current verification and remaining-package fields are cleared to unknown/null rather than presenting pre-rollback success. The failed verification remains in job events. A recovery scan may create a new plan with a new ID and fingerprint; the rolled-back plan is never reused.

## Disable MQTT

Set `mqtt.enabled: false`, validate the config with the agent virtualenv, and restart only the agent service. REST and update behavior are unchanged. HA MQTT entities become unavailable through LWT; deleting retained discovery is an explicit broker administration task and is not performed by the agent.

## Broken 0.2.1 upgrade

The upgrade writes its backup path to `/root/hubinet-ops-last-upgrade-backup` inside the agent CT. Stop the agent, restore the backed-up `/opt/hubinet-ops`, `/etc/hubinet-ops`, and `ops.db*` files, restore ownership, reload systemd, and start the previous service. Do not restore only the database after the new code has resumed jobs; restore one consistent backup set.

The upgrade does not replace `agent.env` or SSH keys and does not enable MQTT, operator scan, approval, or update. Version 0.3.0 intentionally enables the separate read-only `monitoring_scheduler` for APT resources whose `monitoring.update_scan` is true; observation-only results cannot create an approvable plan.

## Read-only diagnosis without changing a resource

Use a non-production copy of runtime config and keep `scheduler.enabled: false`. Call only authenticated `refresh`, wrapper `inspect`, executor `capabilities`, or snapshot list. These are read-only. A deliberate scan may refresh APT metadata and create a waiting plan, so it is not part of installer validation. Never test update, lifecycle, snapshot mutation, or rollback against production as a smoke check.
