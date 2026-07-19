# Home Assistant

`scripts/generate_ha_dashboard.py` reads the versioned inventory and deterministically generates `home-assistant/dashboards/hubinet_ops.yaml`. CI checks that the generated file is current.

The dashboard contains Centrum plus VM/CT 100–110 views. It keeps `/hubinet-ops/ct-101` and `/hubinet-ops/ct-106`; new paths follow `/hubinet-ops/vm-100` or `/hubinet-ops/ct-NNN`. Adapter-specific cards prevent HAOS/self resources from displaying APT. Docker appears only when configured; VM100 shows Guest Agent; CT110 shows self-health. Only true backend capabilities generate controls, so CT106 is the only controlled view.

The package uses authenticated fixed REST actions and navigation-only notifications. It never sends command text and does not put approve/reject/lifecycle buttons in notifications.

Install separately from the agent upgrade:

```bash
bash deploy/install-ha-0.3.0-from-pve.sh 192.168.4.168 http://192.168.4.200:8787 22
```

Those addresses document the current environment and are arguments, not repository runtime defaults. The installer backs up package/dashboard/secrets, preserves existing private values, adds only missing URL secrets, runs `ha core check`, rolls files back on error, and never restarts Home Assistant automatically.
