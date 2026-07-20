# MQTT telemetry and Discovery

MQTT is output-only. There are no command topics.

Canonical retained/resource topics are:

- `hubinet/ops/resource/{vmid}/state`
- `hubinet/ops/resource/{vmid}/attributes`
- `hubinet/ops/resource/{vmid}/job`
- `hubinet/ops/resource/{vmid}/event` (not retained)

During 0.3.x every LXC is dual-published to `hubinet/ops/ct/{vmid}/...`; this preserves CT101/CT106 consumers. Discovery keeps `sensor.hubinet_ops_ct101_*` and `sensor.hubinet_ops_ct106_*`. New LXC use `ctXYZ`; VM100 uses `vm100` and the QEMU device model. CT110 uses the Hubinet Ops Agent model.

The deprecated `sensor.hubinet_ops_agent_configured_container_count` remains. New counters are `configured_resource_count`, `configured_lxc_count`, and `configured_qemu_count`.

Agent state is published once after a complete inventory refresh instead of once per resource. `agent_last_refresh` is an ISO 8601 UTC timestamp, rounded to seconds, for the completion of the most recent full telemetry cycle. Discovery marks it as a diagnostic timestamp.

Only the health sensor receives rich JSON attributes, from the dedicated retained `attributes` topic rather than the changing resource state topic. This payload contains only bounded package updates, recent job events, recent warnings, payload metadata, and optional failed units. It is independently limited to 10,000 UTF-8 bytes and published only when its serialized content changes; CPU, RAM, disk, network, uptime, refresh time, health score, and runtime status remain available through their own state-backed sensors without becoming health attributes. Reconnect republishes both the current state and attributes with force enabled.
