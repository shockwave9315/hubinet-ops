# Resource adapters

## `apt` / LXC

Uses PVE `pct status`, the fixed `hubinet-maint` 0.4.1/protocol 1 actions, Hubinet-owned LXC snapshots, stabilization, configured services, optional Docker requirements, verification, repair policy, and rollback policy. Before any destructive operation and during telemetry refresh the backend verifies required actions, executor hash, profile hash, and profile-validation status—not only a version string. It is valid only for `resource_type: lxc`.

Profiles for CT101–CT109 are versioned in `deploy/managed/profiles`. A schema-valid but insufficient health contract is reported as `insufficient_health_contract`; it blocks automatic health-based rollback but does not invent services. Manual lifecycle/snapshot remains subject to the ordinary backend/PVE guards.

## `haos` / QEMU

Read-only in 0.4.0. The PVE wrapper uses `qm status`, `pvesh .../status/current`, `pvesh get /cluster/resources --type vm`, and the fixed `qm guest cmd VMID network-get-interfaces` request. RAM, disk, uptime, status, and identity come from `status/current`; CPU comes only from the matching cluster-resource entry and remains a 0–1 share in the backend payload. A missing cluster entry produces `null`, never a false zero. Missing Guest Agent is a field value (`unavailable`), not a VM health failure. No Home Assistant Core, Supervisor, or HAOS update API is implemented.

## `agent_self` / CT110

Combines read-only PVE LXC status with fixed local observations: `hubinet-ops.service`, in-process API health/version, inventory/job counts, MQTT availability, `/proc`/disk resource usage, and at most 20 sanitized journal warning/error lines. It never SSHes to itself and never runs `hubinet-maint` in CT110. Normal lifecycle, snapshot, and self-update requests are gated and persisted by the backend before delegation to independent PVE hostd. Direct hostd access while CT110 is stopped is limited to start and separately scoped break-glass recovery.

Invalid adapter/type combinations fail configuration validation. Adding another resource is configuration-driven; backend logic is not copied per VMID.
