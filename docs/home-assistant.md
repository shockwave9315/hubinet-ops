# Home Assistant

`scripts/generate_ha_dashboard.py` reads the versioned inventory and the same entity contract used by MQTT discovery, then deterministically generates `home-assistant/dashboards/hubinet_ops.yaml`. CI checks that the generated file is current and that every dashboard sensor reference has a discovery entity.

The dashboard contains Centrum plus VM/CT 100–110 views. The main interface uses responsive Sections and Mushroom cards. VM100 remains observation-only. CT101–CT109 expose the complete guarded lifecycle, refresh/scan/plan, healthcheck, and snapshot control set. CT110 exposes hostd-backed lifecycle/snapshot/self-update actions that remain reachable while its agent is offline. Backend capabilities and guards remain authoritative even if a card is manually called.

Approve/reject sends only VMID to `approve-active`/`reject-active`; it never reads a plan UUID from health attributes. Every action has confirmation and reports success or an explicit backend error through a persistent notification. Force stop, snapshot rollback, and snapshot deletion use red destructive styling and explain the consequence.

MQTT discovery preserves stable `unique_id` values and declares production-compatible `default_entity_id` suffixes, including `apt_check`, `dpkg_audit`, and `packages_remaining`. VM100 exposes one primary IP as sensor state while retaining its bounded diagnostic address list on the health entity. QEMU and agent-self byte metrics remain raw in backend payloads but are published to HA as numeric GiB sensors.

Install the HA patch separately from the agent patch:

```bash
bash deploy/install-ha-0.4.0-from-pve.sh HA_HOST 22
```

The installer requires the existing private backend and hostd secrets/URL aliases, backs up configuration/package/dashboard/secrets, runs `ha core check`, rolls files back on error, and never restarts Home Assistant automatically or edits the entity registry.

The package excludes the 12 agent/resource `last_refresh` sensors and ephemeral job-progress sensors from Recorder. It does not change `db_url`, `purge_keep_days`, or other user Recorder settings. Historical rows recorded before installation are not deleted immediately; they disappear according to the existing Recorder purge configuration.

All displayed timestamps use Home Assistant local time (`dd.mm.yyyy HH:MM`); live logs show local `HH:MM` without microseconds. Invalid/unknown values render a readable fallback. Plan and job cards show at most the first eight ID characters rather than full UUIDs.
