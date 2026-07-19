# MQTT contract

MQTT is optional. Set `mqtt.enabled: false` or stop the broker and the REST agent/update worker continue operating. The client uses Paho MQTT 2.1.0 with MQTT 3.1.1, LWT, QoS 1, and bounded reconnect delay.

## Topics

| Topic | Retained | Content |
| --- | --- | --- |
| `hubinet/ops/agent/availability` | yes | `online` or LWT `offline` |
| `hubinet/ops/agent/state` | yes | version, configured CT count, active jobs, last refresh |
| `hubinet/ops/ct/<vmid>/state` | configurable, default yes | explicit CT state and bounded attributes |
| `hubinet/ops/ct/<vmid>/job` | configurable, default yes | current/recent job projection |
| `hubinet/ops/ct/<vmid>/event` | no | one structured live event |

Retained payloads are content-deduplicated. Reconnect clears the publication cache, republishes discovery, availability, and a full current-state snapshot. MQTT work runs on its own bounded queue and errors do not escape into jobs.

## Discovery

Discovery uses `homeassistant/sensor/<object_id>/config` by default. Stable identifiers are `hubinet_ops_agent_<field>` and `hubinet_ops_ct_<vmid>_<field>`. Devices are grouped as `Hubinet Ops Agent` and `Hubinet Ops CT<vmid>`; new configured VMIDs are discovered dynamically.

Agent entities include availability, version, configured container count, active job count, and last refresh. CT entities include health/status dimensions, score/progress, LXC, packages/risk, disk/RAM, Docker required counts, plan/job IDs, timestamps, error, rollback policy, and last event. There are no MQTT buttons or command topics.

Version 0.2.4 also publishes the complete operator-capability map and compact lifecycle, verification, recovery-scan, and terminal-event suppression fields. New scalar Discovery entities use stable field-derived IDs; pre-existing IDs are unchanged.

The retained CT state is also the attributes source for IP addresses, OS, services, failed units, package details, Docker details, recent events, and dashboard path.

## Bounds and redaction

- recent events: last 50;
- packages: first 200;
- failed units: first 100;
- IP addresses: first 20;
- event/log line: 1000 characters;
- error: 2000 characters;
- event details JSON: 16 KiB.

Authorization headers, bearer tokens, token/password fields, private keys, and webhook identifiers are redacted before persistence or publication.
