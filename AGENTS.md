# Hubinet Ops Agent Rules

These rules apply to every future coding agent working in this repository.

## Security invariants

- Never add an API, MQTT topic, SSH path, or wrapper action that accepts arbitrary command text.
- Keep the Proxmox SSH account behind `deploy/pve/hubinet-ops-host`; do not provide a general-purpose shell.
- Validate every action, VMID, and optional snapshot argument in both the agent and forced-command wrapper.
- Home Assistant may select a configured VMID or plan ID, but it must never provide shell command text.
- Keep bearer authentication on every `/api/v1` endpoint. MQTT is telemetry and discovery only.
- Updates always require manual plan approval. Push notifications only navigate to the matching dashboard.
- Automatic rollback requires an existing snapshot and the per-container `automatic_rollback` policy.
- Manual update rollback requires the `manual_rollback_allowed` policy and a failed/blocked/interrupted update operation with its recorded snapshot.
- Normal explicit snapshot restore is a backend operation. It requires `manual_snapshot_restore_allowed`, the `snapshot_rollback` capability, an existing rollback-eligible Hubinet-owned snapshot, no waiting/approved backend plan, no active global destructive job, explicit Home Assistant confirmation, and the independent PVE snapshot-restore policy.
- Offline CT110 snapshot restore is a separate break-glass recovery operation for a stopped CT110, authenticated with a dedicated recovery token. It is not a normal exception to backend control and never runs as an automatic fallback.
- A successful offline CT110 restore is recorded durably on PVE and, on the next backend start, deliberately supersedes waiting plans, marks approved plans recovered, and interrupts restored queued/running jobs before the event is acknowledged.
- The backend and SQLite database are the source of truth; Home Assistant is presentation and controlled input.
- Never commit API tokens, MQTT passwords, webhook IDs, SSH keys, production addresses, runtime databases, or logs.
- Never log authorization headers, bearer tokens, MQTT passwords, private keys, or webhook identifiers.

## Test boundaries

- Do not contact real Proxmox, LXC, Docker, Home Assistant, MQTT, or private-network endpoints.
- Use fake executors, fake MQTT clients, fake clocks, temporary directories, and simulated process output.
- Do not run real `apt`, `pct`, `ssh`, `systemctl`, Docker, deployment operations, or private-network connections as part of repository tests.
- A deployment script may be executed only by a hermetic smoke harness that sets `HUBINET_OPS_TEST_MODE=1`, uses an isolated `PATH` without inheriting the host `PATH`, replaces every privileged or external command through a temporary fake command layer, and adds only an explicit allowlist of unprivileged local tools. The isolated `PATH` must contain no real network, deployment, container, or hypervisor programs and must fail closed on every non-allowlisted command. The harness must redirect PVE, archive, backup, mount, and runtime paths into temporary directories, use no real or private-network endpoints and no production addresses or credentials, and fail if any real lifecycle or snapshot mutation escapes the fakes.
- A production deployment script executed by a repository smoke test must not reference real network, deployment, container, or hypervisor programs through absolute paths; reject such paths before executing the script.
- Run Python compilation, pytest, `bash -n`, YAML parsing, and tracked-runtime-file checks before publishing.
