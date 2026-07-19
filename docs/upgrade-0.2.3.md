# Upgrade Hubinet Ops to 0.2.3

Version 0.2.3 fixes oversized Home Assistant MQTT attributes and updates the package preview cards to report the authoritative package total when the 10 KB preview is truncated. It does not change the managed-container executors, Proxmox forced-command wrapper, database schema, scheduler, dashboard controls, Home Assistant package automations, or notification permissions.

## 1. Upgrade CT110 agent

Run from a reviewed 0.2.3 source tree on the Proxmox host:

```bash
bash deploy/upgrade-0.2.3-from-pve.sh 110
```

The script:

1. verifies the source version and running agent CT;
2. packages only `app`, `requirements.txt`, and the systemd unit;
3. stops the agent before copying its SQLite files;
4. backs up the current app, requirements, unit, and `ops.db*` inside CT110;
5. installs the new agent code and validates virtualenv readability for the unprivileged service;
6. starts the service and requires `/health` to return version `0.2.3`;
7. explicitly restores the previous agent if any step or final health validation fails.

The agent restart republishes retained MQTT Discovery and container state. The complete state used as `health_status` attributes is capped at 10,000 UTF-8 bytes, leaving margin below Home Assistant Recorder's 16,384-byte maximum.

Existing Recorder warning entries remain visible in historical logs. After Home Assistant receives the retained 0.2.3 state, no new oversized-attribute warning should be created. A managed-container scan or package update is not required to publish the corrected payload.

The path to the rollback backup is written to `/root/hubinet-ops-last-upgrade-backup` inside CT110 and printed at the end.

## 2. Install the 0.2.3 dashboard view

The dashboard now displays both the authoritative total and the number of package details available in the bounded MQTT preview. Install only the dashboard file:

```bash
bash deploy/install-ha-dashboard-0.2.3-from-pve.sh 192.168.4.168 22222
```

This script validates the repository YAML, backs up the current HA dashboard, copies the new file, verifies both CT truncation messages, and runs `ha core check`. It does not replace the HA package, webhook, secrets, or notification target.

No Home Assistant restart is required. Reload the Hubinet Ops dashboard in the browser or fully reopen the mobile app.
