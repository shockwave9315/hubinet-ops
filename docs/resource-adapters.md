# Resource adapters

## `apt` / LXC

Uses PVE `pct status`, the fixed `hubinet-maint` actions, LXC snapshots, stabilization, configured services, optional Docker requirements, verification, repair policy, and rollback policy. It is valid only for `resource_type: lxc`.

## `haos` / QEMU

Read-only in 0.3.0. The PVE wrapper uses `qm status`, `pvesh .../status/current`, and the fixed `qm guest cmd VMID network-get-interfaces` request. It reports runtime, CPU, RAM, disk, network, uptime, name, Guest Agent availability, and bounded IP addresses. Missing Guest Agent is a field value (`unavailable`), not a VM health failure. No Home Assistant Core, Supervisor, or HAOS update API is implemented.

## `agent_self` / CT110

Combines read-only PVE LXC status with fixed local observations: `hubinet-ops.service`, in-process API health/version, inventory/job counts, MQTT availability, `/proc`/disk resource usage, and at most 20 sanitized journal warning/error lines. It never SSHes to itself and never runs `hubinet-maint` in CT110.

Invalid adapter/type combinations fail configuration validation. Adding another resource is configuration-driven; backend logic is not copied per VMID.
