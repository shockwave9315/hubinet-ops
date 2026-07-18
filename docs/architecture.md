# Architecture and trust boundaries

## Components

1. Home Assistant displays MQTT Discovery entities and invokes a small set of authenticated REST actions.
2. The FastAPI agent validates VMIDs and plan IDs, owns policy, stores state/jobs/events in SQLite, and runs one update worker.
3. The SSH client invokes a single remote command string assembled from an internal action enum, allowlisted VMID, and validated snapshot name.
4. `hubinet-ops-host` is the SSH forced command on Proxmox. It repeats action, VMID, and argument validation and consults `/etc/hubinet-ops/allowed-vmids`.
5. `hubinet-maint` exposes only inspect, scan, preflight, update, healthcheck, and configured repair behavior inside a managed CT.
6. MQTT carries availability, state, discovery, current job, and events. It carries no commands.

## Trust boundaries

- The REST bearer token crosses only HA-to-agent requests. It is never included in MQTT or events.
- MQTT credentials are read from protected agent config and are never logged or published.
- The SSH private key remains in `/etc/hubinet-ops/keys`; the Proxmox account does not receive a general shell.
- A Home Assistant user can request a configured action but cannot supply command text. Backend validation remains authoritative.
- Webhook payloads are outbound notification hints. HA cannot use them to approve an update.

## Update lifecycle

`scan -> waiting approval -> preflight -> snapshot (policy) -> update -> wait services -> stabilized healthcheck -> post-scan -> success`

APT update output may emit NDJSON events. The SSH wrapper passes them unchanged; the agent parses each line, persists sanitized events, updates job progress, and enqueues MQTT publication. A malformed line is bounded and ignored. The final result remains mandatory.

If stabilization times out, only configured repair actions run. A second stabilization window follows. Rollback is considered only after repair fails and only if `automatic_rollback` created a snapshot. Rollback completion means the CT started and passed consecutive systemd/Docker polls, not merely that `pct start` returned.

## Data ownership

SQLite stores plans, jobs, container state, and job events. Retained MQTT state is a projection for fast HA recovery. HA entity state never overwrites SQLite.
