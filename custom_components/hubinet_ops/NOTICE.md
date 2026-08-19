# Upstream attribution

Selected config-flow, coordinator, dynamic entity and diagnostics patterns were
adapted and substantially modified from the Home Assistant Core `proxmoxve`
integration:

- repository: `home-assistant/core`
- tag: `2026.8.1`
- commit: `53998d7710b4ac280658511c24a2a3e2651f9873`
- upstream path: `homeassistant/components/proxmoxve/`
- license: Apache License 2.0

The pinned upstream source is not vendored in this repository; retrieve it at
the exact tag/commit above from `home-assistant/core` if needed. The Hubinet
Ops integration communicates only with the Hubinet Ops backend and does not
include upstream Proxmox mutations.
