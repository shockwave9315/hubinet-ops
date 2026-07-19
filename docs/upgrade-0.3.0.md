# Upgrade 0.2.4 → 0.3.0

This release adds the full Proxmox resource inventory and explicit adapters. It preserves 0.2.4 security, database history, secrets, legacy API aliases, CT101/CT106 entity IDs/topics, and dashboard paths.

## Safety boundary

- CT106 is the only live lifecycle/update test target.
- VM100 and CT110 are observation-only.
- CT102/103/104/105/107/109 are critical/high production resources and observation-only; CT101 and CT108 are also observation-only.
- Upgrade performs no scan, update, lifecycle, snapshot, repair, or rollback action on resources.
- HA is validated but never restarted automatically.

## Preflight and local validation

Build from a clean archive-safe checkout and run the repository checks in README. Review these exact versioned files before rollout:

- `config/config.example.yaml`
- `deploy/pve/observation-vmids` (100–110)
- `deploy/pve/managed-vmids` (101–109)
- `deploy/pve/maintenance-vmids` (only 106)
- `deploy/pve/lifecycle-vmids` (only 106)
- `deploy/pve/resource-types`

## Exact rollout order

1. Create a source archive; `.git` is not required.
2. On PVE, run `bash deploy/upgrade-0.3.0-from-pve.sh` with no VMID arguments.
3. Record the printed host and CT110 backup paths.
4. Review `/health` and `GET /api/v1/resources` without requesting an action.
5. Install HA separately, for the current environment:
   `bash deploy/install-ha-0.3.0-from-pve.sh 192.168.4.168 http://192.168.4.200:8787 22`.
6. Review `ha core check`; schedule any HA restart as a separate operator decision.

The upgrade stops the CT110 service before SQLite copy, installs profiles only in CT101–109, and performs a safe `inspect` smoke only for already-running APT guests. Stopped guests are not started; their smoke is deferred.

## Rollback

Any script failure restores its touched layers automatically. For a later manual rollback, use the printed backup paths: restore PVE wrapper/allowlists/map and CT101–109 executor/profile files, stop CT110 writer, restore agent code/config/env/unit and all `ops.db*` files, fix ownership, reload systemd, then start the old service. Restore HA from its separate `/config/backups/hubinet-ops/...` directory. Do not mix agent and HA backup generations.

## Restricted live test plan

All general checks are read-only: inventory count/type, telemetry freshness, QEMU Guest Agent availability behavior, CT110 self-health, MQTT topics/entities, and dashboard paths. Any live scan, approval/update, lifecycle, stabilization, or rollback-policy test is limited to CT106 and requires a separate explicit operator decision. Manual rollback is expected to remain blocked.

## Onboarding another resource

Add a validated `resources` entry, an observation/type mapping, an APT profile only for an LXC/APT guest, regenerate the dashboard, and run tests. Do not copy backend VMID logic. Lifecycle requires a separate deliberate allowlist change in addition to a capability; neither grants the other.
