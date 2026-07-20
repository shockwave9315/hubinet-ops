# Upgrade 0.3.1 → 0.3.2

Review the release archive and run both steps manually from the PVE administration host:

```bash
bash deploy/upgrade-0.3.2-from-pve.sh
bash deploy/install-ha-0.3.2-from-pve.sh HA_HOST 22
```

The first step requires CT110 to be running and updates only its application code plus `/usr/local/sbin/hubinet-ops-host` on PVE. It backs up both layers, validates Python and shell syntax, runs read-only wrapper inspections for VM100 and CT106, restores both on failure, and waits for the first complete telemetry result. Success requires version 0.3.2, exactly 11 inventory resources, healthy VM100/CT106/CT110 state, numeric VM100 CPU, and CT110 health score 100. It does not change inventory, allowlists, policies, managed executors, or resource lifecycle.

The second step backs up Home Assistant package, dashboard, and secrets, installs staged files, and runs `ha core check`. A failed check restores the previous files. It does not edit the entity registry and does not restart Home Assistant automatically.

The Home Assistant package excludes the 12 Hubinet Ops `last_refresh` sensors from future Recorder history. Entries written before the upgrade are not removed immediately and expire under the user's existing Recorder purge policy; the installer does not change `db_url`, `purge_keep_days`, or any other Recorder setting.
