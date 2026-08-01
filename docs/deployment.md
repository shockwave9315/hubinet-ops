# Deployment

Deployment is always explicit and is never run by repository tests.

Hubinet Ops 0.4.2 has two reviewed transactional steps:

1. `deploy/upgrade-0.4.2-from-pve.sh` updates PVE host control/hostd/allowlists and the CT110 agent/config while preserving the unchanged CT101–CT109 executor contract and schema version 400. It accepts only an installed 0.4.1 backend and hostd.
2. `deploy/install-ha-0.4.2-from-pve.sh [--restart-core] HA_HOST [PORT]` updates the Home Assistant package and generated dashboard.

Before step 1, provision root-owned `/etc/hubinet-ops/hostd.json` and `/etc/hubinet-ops/hostd.env` on PVE. The environment file holds pairwise-distinct general, backend, self-update, and recovery bearer values and is never committed or printed. The installer passes only backend/self-update scopes into CT110; Home Assistant secrets hold only general/recovery scopes. The installer migrates the existing CT110 configuration in place while preserving inventory, addresses, MQTT, monitoring, and health profiles.

The PVE transaction backs up every destination before mutation. Running managed CTs use `pct push/exec`; stopped managed CTs use bounded `pct mount/unmount` and are never started. Safe idempotent `pct` operations retry rc=129 up to three times without bypassing the global rollback trap. Every executor is staged, compiled, installed idempotently, profile-validated, and checked through `capabilities`. A failure restores all already visited executors/profiles, hostd/wrapper/allowlists, and the complete CT110 backup or restarts the unchanged agent depending on the stage.

The hostd unit keeps `ProtectSystem=strict`. Its writable paths are limited to `/etc/pve`, `/var/lib/hubinet-ops-hostd`, `/run/lock`, `/var/log/pve/tasks`, `/run/lxc/lock`, `/var/lib/lxc`, `/etc/lvm/archive`, and `/etc/lvm/backup`. These cover PVE configuration, durable host jobs, general/LXC locks, UPID task logs, LXC `rules.seccomp.tmp` files created during start/reboot, and LVM metadata safety copies. `StateDirectory=hubinet-ops-hostd` creates the root-owned 0700 state directory; the installer defensively creates it and the required PVE subdirectories before service start.

Refresh reads LXC runtime from the PVE host before probing the guest executor. A missing `/usr/local/sbin/hubinet-maint` therefore reports the real `running`/`stopped` runtime and separate executor drift. Lifecycle and snapshot jobs remain backend-gated and hostd-backed but do not require a working guest executor; scan, preflight, update, repair, verify, and guest healthcheck still do.

Final validation is read-only: hostd `/health`, wrapper `inspect 100`, `inspect 106`, `list-snapshots 106`, CT110 `/health`, canonical `/api/v1/state`, exact fresh inventory 100–110, executor compatibility CT101–CT109, DB schema version 400, and backend snapshot list. No update, plan approval, lifecycle, snapshot mutation, repair, or rollback is invoked.

The HA transaction validates every required secret at once, rejects legacy `/api/v1/plans/approve` and `/api/v1/plans/reject` URLs, backs up `configuration.yaml`, `secrets.yaml`, package, and dashboard, stages files, runs `ha core check`, and restores them on failure. By default it does not restart Home Assistant and prints the exact restart command. `--restart-core` restarts Core only after a successful check and waits for Core state `running`; it never changes VM100 lifecycle through PVE. See [the exact 0.4.2 rollout](upgrade-0.4.2.md).
