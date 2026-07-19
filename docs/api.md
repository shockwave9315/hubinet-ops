# REST API 0.3.0

Every `/api/v1` route requires `Authorization: Bearer …`. A policy, adapter, state, or concurrency conflict returns HTTP 409; unknown resources return 404.

Canonical inventory routes:

- `GET /api/v1/resources`
- `GET /api/v1/resources/{vmid}`
- `GET /api/v1/resources/{vmid}/state`
- `GET /api/v1/resources/{vmid}/events`
- `POST /api/v1/resources/{vmid}/refresh`
- `POST /api/v1/resources/{vmid}/scan`
- `POST /api/v1/resources/{vmid}/start|shutdown|reboot`

Each inventory item includes VMID, resource type, adapter, names, monitoring policy, operator capabilities, and current state. `/api/v1/containers` returns LXC only, preserves the 0.2.4 shape for CT101/CT106, and never includes VM100. Existing LXC `/containers/{vmid}/...` routes remain compatibility aliases.

There is no generic command endpoint. VMID or plan selection never accepts shell text. Plans, jobs, retry-healthcheck, and rollback retain their 0.2.4 fixed endpoints and policies.
