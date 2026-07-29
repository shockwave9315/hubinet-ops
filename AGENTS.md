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
- Do not run real `apt`, `pct`, `ssh`, `systemctl`, deployment operations, or private-network connections as part of repository tests. Docker may be used only by the controlled sandbox manager on an ephemeral GitHub-hosted CI runner with the workflow-owned `HUBINET_OPS_EPHEMERAL_CI=1` marker; it is unavailable to the production script inside the sandbox.
- A deployment script may be executed only inside the repository's system-enforced smoke sandbox on an ephemeral CI runner. The sandbox is the security boundary: it must have no network, host PID namespace, capabilities, privileges, host sockets, host secrets, or writable host filesystem; it runs as a non-root user with a read-only repository/root filesystem and only one-shot writable workspace and `/tmp`. The production script must never execute directly on the pytest host; local Linux does not invoke the manager, while a marked Linux CI run must fail closed if its marker or sandbox is unavailable.
- Inside that sandbox, the hermetic smoke harness must set `HUBINET_OPS_TEST_MODE=1`, use an isolated `PATH` without inheriting the host `PATH`, replace every privileged or external command through a temporary fake command layer, and add only an explicit allowlist of unprivileged local tools. The isolated command layer contains no real network, deployment, container, or hypervisor programs. It must redirect PVE, archive, backup, mount, and runtime paths into the one-shot workspace, use no production addresses or credentials, and fail if any real lifecycle or snapshot mutation escapes the fakes.
- The static shell validator is defense-in-depth, not the execution boundary. It must fail closed on host `PATH` access, explicit or lexically shell-assembled standard absolute executable paths (including non-canonical spellings), `command -p`, and explicit or assembled Bash `/dev/tcp` or `/dev/udp` networking before the sandbox executes a production deployment script.
- The validator is a conservative lexical scanner, not a Bash parser: it never executes shell text and does not interpret parameter expansion, command substitution, arithmetic expansion, or other arbitrary dynamic expansion. Those constructs are not a security boundary; even unknown syntax remains confined by the system-enforced sandbox.
- The deployment script must not read or modify `PATH` or reference executables under `/bin`, `/sbin`, `/usr/bin`, or `/usr/sbin`; this boundary is deliberately fail-closed, with the exact first-line `#!/usr/bin/env bash` shebang as its only exception.
- Run Python compilation, pytest, `bash -n`, YAML parsing, and tracked-runtime-file checks before publishing.
