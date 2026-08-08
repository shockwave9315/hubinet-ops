# Upstream attribution

Selected config-flow, coordinator, dynamic entity and diagnostics patterns were
adapted and substantially modified from the Home Assistant Core `proxmoxve`
integration:

- repository: `home-assistant/core`
- commit: `2d754bc290f644d2e0416d1616634471949f112e`
- upstream path: `homeassistant/components/proxmoxve/`
- license: Apache License 2.0

The pinned upstream source and full license text are available at
`vendor/home-assistant-core/`. The Hubinet Ops integration communicates only
with the Hubinet Ops backend and does not include upstream Proxmox mutations.
