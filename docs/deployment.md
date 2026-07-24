# Deployment

Deployment is always explicit and is never run by repository tests.

Hubinet Ops 0.4.0 has two reviewed transactional steps:

1. `deploy/upgrade-0.4.0-from-pve.sh` updates PVE host control/hostd/allowlists, executors and profiles in CT101–CT109, then the CT110 agent/config/database.
2. `deploy/install-ha-0.4.0-from-pve.sh HA_HOST [PORT]` updates the Home Assistant package and generated dashboard.

Before step 1, provision root-owned `/etc/hubinet-ops/hostd.json` and `/etc/hubinet-ops/hostd.env` on PVE. The environment file holds pairwise-distinct general, backend, self-update, and recovery bearer values and is never committed or printed. The installer passes only backend/self-update scopes into CT110; Home Assistant secrets hold only general/recovery scopes. The installer migrates the existing CT110 configuration in place while preserving inventory, addresses, MQTT, monitoring, and health profiles.

The PVE transaction backs up every destination before mutation. Running managed CTs use `pct push/exec`; stopped managed CTs use bounded `pct mount/unmount` and are never started. Every executor is staged, compiled, profile-validated, atomically renamed, and checked through `capabilities`. A failure restores all already visited executors/profiles, hostd/wrapper/allowlists, and the complete CT110 backup or restarts the unchanged agent depending on the stage.

Final validation is read-only: hostd `/health`, wrapper `inspect 100`, `inspect 106`, `list-snapshots 106`, CT110 `/health`, canonical `/api/v1/state`, exact fresh inventory 100–110, executor compatibility CT101–CT109, DB schema version 400, and backend snapshot list. No update, plan approval, lifecycle, snapshot mutation, repair, or rollback is invoked.

The HA transaction backs up `configuration.yaml`, `secrets.yaml`, package, and dashboard; stages files; runs `ha core check`; and restores them on failure. It does not restart Home Assistant or edit the entity registry. See [the exact 0.4.0 rollout](upgrade-0.4.0.md).
