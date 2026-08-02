# Resource state and SQLite compatibility

Normalized resource state contains identity/runtime/health, telemetry, configured capabilities, and adapter-specific values. APT LXC adds plan/update/verification, executor compatibility, lifecycle, job, and snapshot summary fields. QEMU and agent-self represent unsupported values as null/unknown rather than false zero.

Executor state includes `executor_version`, `executor_protocol_version`, `executor_compatible`, `executor_sha256`, `executor_profile_sha256`, `executor_missing_actions`, `profile_validation_status`, and `executor_last_checked_at`. Snapshot state includes only count, latest name/time/kind, and operation status; full lists remain REST-only.

Every durable job stores `operation_type`, `request_id`, plan/resource identity, status, stage, progress, result, error, timestamps, optional snapshot, and ordered events. Operation types are update, lifecycle start/shutdown/reboot/force-stop, snapshot create/rollback/delete, retry-healthcheck, and self-update. `active_job_id` is present only while work is active; `last_job_id` retains terminal identity for the shortened dashboard display.

After rollback, `verification_status` returns to unknown and `last_verification`, APT/dpkg booleans, `packages_remaining_count`, pending count, and package projection are cleared. Failed verification events remain durable. Any recovery plan is a new row with a new ID/fingerprint.

SQLite remains `/var/lib/hubinet-ops/ops.db`. Migration to `user_version=400` is additive and idempotent; existing plans/jobs/events/state survive. Historical `container_states` remains on disk with canonical resource methods layered over it. Hostd uses a separate PVE SQLite database so CT110 host job results survive the guest being offline.

Retained MQTT state/attributes are bounded projections, not authority. Home Assistant never writes resource state back to SQLite.
# 0.4.3 ownership and CT110 state

Snapshot ownership is tri-state: `foreign`, `host_owned_unproven`, or `managed`. Only `managed` has durable backend proof and can become policy-eligible for mutation. CT110 state additionally separates `system_*` package/update/verification fields from `application_*` release/download/validation/deployment fields; a plan is explicitly typed as `ct110_system_update` or `self_update`.
