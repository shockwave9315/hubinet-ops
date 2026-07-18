# Recovery and diagnosis

## Diagnose a stuck job

1. Read the container's `operation_status`, `job_stage`, `job_progress`, `active_job_id`, and `last_error` separately.
2. Read `GET /api/v1/jobs/<job_id>/events?limit=200` with the bearer token.
3. Check whether the latest events show initial grace, Docker unavailable, container counts, repair, rollback wait, or rollback timeout.
4. Use the dashboard `Refresh` action. It performs inspect only and cannot approve or update.
5. Use `Retry healthcheck` only after services have had time to recover. The retry receives a new follow-up job/event history.
6. Use dashboard rollback only when the backend publishes `rollback_allowed=allowed`; the backend still rechecks policy, failed state, and snapshot presence.

An agent restart marks queued/running jobs interrupted. It does not silently resume APT or rollback. Inspect the managed CT and snapshot state before creating a new plan.

## Disable MQTT

Set `mqtt.enabled: false`, validate the config with the agent virtualenv, and restart only the agent service. REST and update behavior are unchanged. HA MQTT entities become unavailable through LWT; deleting retained discovery is an explicit broker administration task and is not performed by the agent.

## Broken 0.2.1 upgrade

The upgrade writes its backup path to `/root/hubinet-ops-last-upgrade-backup` inside the agent CT. Stop the agent, restore the backed-up `/opt/hubinet-ops`, `/etc/hubinet-ops`, and `ops.db*` files, restore ownership, reload systemd, and start the previous service. Do not restore only the database after the new code has resumed jobs; restore one consistent backup set.

The upgrade does not replace `agent.env` or SSH keys and does not enable MQTT/scheduler, scan, approve, or update.

## Observe CT106 without updating it

Use a non-production copy of runtime config and keep `scheduler.enabled: false`. Verify CT106 is allowlisted, then call only the authenticated CT106 `refresh` endpoint. Refresh maps to fixed `inspect` and does not run APT. A deliberate `scan` runs package metadata refresh/simulation and may create a waiting plan, but it cannot update packages. Reject the plan from the dashboard; do not approve it. Never test with the update or rollback endpoint unless an explicit maintenance window and rollback policy have been reviewed.
