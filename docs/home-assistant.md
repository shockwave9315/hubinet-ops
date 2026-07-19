# Home Assistant

`scripts/generate_ha_dashboard.py` reads the versioned inventory and the same entity contract used by MQTT discovery, then deterministically generates `home-assistant/dashboards/hubinet_ops.yaml`. CI checks that the generated file is current and that every dashboard sensor reference has a discovery entity.

The dashboard contains Centrum plus VM/CT 100–110 views. The main interface uses responsive Sections and Mushroom cards. Adapter-specific layouts prevent HAOS and CT110 from displaying APT or unsupported metrics. Docker appears only where configured. Only true backend capabilities generate controls, so CT106 is the only controlled view.

MQTT discovery preserves stable `unique_id` values and declares the production-compatible `default_entity_id`. VM100 exposes one primary IP as sensor state while retaining its bounded diagnostic address list on the health entity. Numeric sensors render `None` when their source field is absent.

Install the HA patch separately from the agent patch:

```bash
bash deploy/install-ha-0.3.1-from-pve.sh HA_HOST 22
```

The installer requires the existing 0.3.0 private secrets and URL aliases, backs up package/dashboard/secrets, runs `ha core check`, rolls files back on error, and never restarts Home Assistant automatically.
