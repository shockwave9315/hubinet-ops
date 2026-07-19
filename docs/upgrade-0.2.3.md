# Upgrade agent 0.2.1 to 0.2.3

Version 0.2.3 is an agent-only MQTT payload hotfix. It does not change the managed-container executors, Proxmox forced-command wrapper, database schema, scheduler, Home Assistant package, dashboard controls, or notification permissions.

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
7. restores the previous agent automatically if any step or health validation fails.

The restart republishes retained MQTT Discovery and container state. Home Assistant does not require a restart. The next state publication is capped at 10,000 UTF-8 bytes, so the Recorder warning about attributes exceeding 16,384 bytes should stop.

The path to the rollback backup is written to `/root/hubinet-ops-last-upgrade-backup` inside CT110 and printed at the end.
