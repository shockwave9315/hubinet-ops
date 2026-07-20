# Home Assistant

`scripts/generate_ha_dashboard.py` reads the versioned inventory and the same entity contract used by MQTT discovery, then deterministically generates `home-assistant/dashboards/hubinet_ops.yaml`. CI checks that the generated file is current and that every dashboard sensor reference has a discovery entity.

The dashboard contains Centrum plus VM/CT 100–110 views. The main interface uses responsive Sections and Mushroom cards. Adapter-specific layouts prevent HAOS and CT110 from displaying APT or unsupported metrics. Docker appears only where configured. Only true backend capabilities generate controls, so CT106 is the only controlled view.

MQTT discovery preserves stable `unique_id` values and declares production-compatible `default_entity_id` suffixes, including `apt_check`, `dpkg_audit`, and `packages_remaining`. VM100 exposes one primary IP as sensor state while retaining its bounded diagnostic address list on the health entity. QEMU and agent-self byte metrics remain raw in backend payloads but are published to HA as numeric GiB sensors.

Install the HA patch separately from the agent patch:

```bash
bash deploy/install-ha-0.3.2-from-pve.sh HA_HOST 22
```

The installer requires the existing private secrets and URL aliases, backs up package/dashboard/secrets, runs `ha core check`, rolls files back on error, and never restarts Home Assistant automatically.
