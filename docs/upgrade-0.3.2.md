# Upgrade 0.3.1 → 0.3.2

Review the release archive and run both steps manually from the PVE administration host:

```bash
bash deploy/upgrade-0.3.2-from-pve.sh
bash deploy/install-ha-0.3.2-from-pve.sh HA_HOST 22
```

The first step requires CT110 to be running and updates only its application code plus `/usr/local/sbin/hubinet-ops-host` on PVE. It backs up both layers, validates Python and shell syntax, restores both on failure, and verifies version 0.3.2 with exactly 11 inventory resources. It does not change inventory, allowlists, policies, managed executors, or resource lifecycle.

The second step backs up Home Assistant package, dashboard, and secrets, installs staged files, and runs `ha core check`. A failed check restores the previous files. It does not edit the entity registry and does not restart Home Assistant automatically.
