# Upgrade 0.4.2 → 0.4.3

0.4.3 is the final manually bootstrapped Hubinet Ops release. Do not use this procedure for any source version other than a healthy 0.4.2 backend and hostd. It does not update Debian packages, create/delete/restore snapshots, or change VM/CT lifecycle state.

## Preflight

1. Check that the release checkout is the reviewed 0.4.3 commit and that CI is green.
2. Confirm that no hostd job is queued/running and resolve any waiting/approved plan intentionally. Do not delete the failed CT109 job or its physical snapshot.
3. Keep the root-owned `hostd.env`, `hostd.json`, CT110 `agent.env`, API/MQTT credentials, SSH key, known-hosts file, backend database, and hostd job database in place.
4. Run repository validation. Linux runtime smoke is accepted only from the system-enforced GitHub-hosted CI sandbox.

## Manual bootstrap

On PVE, from the immutable reviewed 0.4.3 source tree:

```bash
sudo ./deploy/upgrade-0.4.3-from-pve.sh
sudo ./deploy/install-ha-0.4.3-from-pve.sh HA_HOST
# Optional only after the successful HA check:
sudo ./deploy/install-ha-0.4.3-from-pve.sh --restart-core HA_HOST
```

The PVE installer accepts no arguments and verifies exactly 0.4.2 as the installed backend/hostd. Before its first mutation it creates `/root/hubinet-ops-backups/<UTC>-before-0.4.3/`, including PVE host files, configured hostd SQLite state, CT101–110 helpers/profiles, and CT110 application/config/database/key state. HA backups are stored at `/config/backups/hubinet-ops/<UTC>-before-0.4.3/`.

The installer preserves `api.port` from the existing CT110 configuration and renders `/etc/hubinet-maint.json` from that migrated value. For the standard production configuration this produces `http://127.0.0.1:8787/health`. Final validation fails and rolls back if the installed profile disagrees with `api.port`, `hubinet-ops.service` is not active, or the real read-only `/usr/local/sbin/hubinet-maint healthcheck` cannot reach the backend.

Any failure invokes reverse rollback: CT110 application/config/database, CT110–101 helpers/profiles, host files and hostd database, unit state, and service enabled/active state. An incomplete rollback is a manual-intervention condition; use the printed backup path and never retry an outcome-unknown mutation.

## Production acceptance after bootstrap

1. Confirm hostd and backend `/health` report 0.4.3 and exact inventory 100–110.
2. Refresh VM100 and one LXC; confirm physical snapshots refresh and no mutation occurs.
3. Refresh the resource whose snapshot was manually removed; confirm count/latest clear in one cycle.
4. Verify the CT109 production snapshot is shown as host-owned/unproven unless exact durable reconciliation succeeds; do not delete or restore it automatically.
5. Scan and approve one noncritical CT101–109 plan; confirm snapshot proof is persisted before APT starts.
6. Click both prune actions with no candidate; confirm `nothing_to_delete`, no job, no hostd mutation, and no red HA log.
7. Check the CT110 Debian section; scan only, verify the expected package list, then schedule a separately approved system update window.
8. Check the application section. With 0.4.3 current it must return `up_to_date` over HTTP 200. Do not reinstall or downgrade it.

## Releases from 0.4.4

A successful CI run for a push to `main` publishes only when `app/mqtt.py` contains a stable version greater than the latest stable tag and that tag does not exist. A merge without a version bump publishes nothing. CI builds the bundle from the exact merge SHA, with a manifest, checksums, minimum source version, no symlinks, and exactly one `deploy/upgrade-<version>-from-pve.sh` entrypoint.

Home Assistant first checks the fixed repository. `up_to_date` and `no_release_published` are normal HTTP 200 results. For `update_available`, the operator reviews version/tag/commit/published time and explicitly installs the exact fingerprint. PVE downloads privately, verifies bounds/paths/checksums/manifest/source compatibility, atomically stages, and executes the durable supervisor. Backend or hostd restart never authorizes a second rollout.
