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
- Manual rollback requires the `manual_rollback_allowed` policy and a failed operation with a recorded snapshot.
- The backend and SQLite database are the source of truth; Home Assistant is presentation and controlled input.
- Never commit API tokens, MQTT passwords, webhook IDs, SSH keys, production addresses, runtime databases, or logs.
- Never log authorization headers, bearer tokens, MQTT passwords, private keys, or webhook identifiers.

## Test boundaries

- Do not contact real Proxmox, LXC, Docker, Home Assistant, MQTT, or private-network endpoints.
- Use fake executors, fake MQTT clients, fake clocks, temporary directories, and simulated process output.
- Do not run `apt`, `pct`, `ssh`, Docker, or deployment scripts as part of repository tests.
- Run Python compilation, pytest, `bash -n`, YAML parsing, and tracked-runtime-file checks before publishing.
