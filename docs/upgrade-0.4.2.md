# Hubinet Ops 0.4.2 production stabilization rollout and rollback

This is an explicit operator-run procedure for an existing 0.4.1 installation. The installer fails closed for every other installed backend or hostd version. Repository tests use stubs and the runtime smoke executes only inside the system-enforced CI sandbox. Version 0.4.2 does not change the SQLite schema: `PRAGMA user_version` remains `400`.

## Preconditions

1. Review the exact 0.4.2 commit and `CHANGELOG.md`.
2. Confirm CT110 is already running. The installer does not start it and never changes VM100 lifecycle.
3. Keep root-owned `/etc/hubinet-ops/hostd.json` and mode-0600 `/etc/hubinet-ops/hostd.env` on PVE. The environment contains four pairwise-distinct general, backend, self-update, and recovery bearer values of at least 32 characters.
4. Confirm sufficient space for `/root/hubinet-ops-backups` and an explicit maintenance window.
5. Do not manually remove the current database, hostd job database, snapshots, or previous backup.

## PVE, managed executors, and CT110

From the reviewed release tree on PVE:

```bash
cd /root/hubinet-ops
bash deploy/upgrade-0.4.2-from-pve.sh
```

The installer accepts no arguments. It backs up every PVE destination, every CT101–CT109 executor/profile, and the complete CT110 application/config/database state before mutation. It creates `/var/lib/hubinet-ops-hostd` as root:root 0700 and defensively creates the PVE task, LXC lock/state, and LVM backup directories before restarting hostd. The strict hostd sandbox remains enabled; `/var/lib/lxc` is writable only because PVE start/reboot creates temporary `rules.seccomp.tmp` files there.

Safe, idempotent `pct push/exec` steps retry rc=129 at most three times. Other return codes fail immediately. A third rc=129 enters the normal global rollback. Running CTs receive executor/profile files through idempotent `install`; stopped CTs are mounted and never started.

Before CT110 starts, both upgrade and rollback normalize `/etc/hubinet-ops/keys` to `0750 root:hubinetops`, the existing `proxmox_ed25519` to `0600 hubinetops:hubinetops`, and `ssh_known_hosts` to `0640 root:hubinetops`. The existing private key is not regenerated or replaced. Any ownership or mode failure stops the rollout with the agent inactive.

The config migrator preserves inventory, addresses, MQTT, existing database, safety policies, and snapshots. It enables only typed snapshot create/list/delete for VM100, sets default managed retention to three, and adds MQTT CPU deadband and heartbeat defaults. It does not enable VM100 update, lifecycle, or restore. The executor remains version 0.4.1/protocol 1 because its guest protocol did not change.

Final validation performs only read-only health, inspect, capabilities, state, schema, and snapshot-list calls. The full state JSON is read through stdin, never argv or environment. A failure reports the exact failed condition and invokes rollback.

The backend obtains LXC runtime from PVE independently from guest inspect. Restoring an older snapshot that removes `hubinet-maint` leaves the container correctly reported as running while executor compatibility and guest health show drift. Backend-gated hostd lifecycle/snapshot operations remain available; APT scan/update/repair/verify and guest healthcheck remain blocked until the executor is restored.

## Home Assistant

Review and provision every key in `home-assistant/secrets.example.yaml`. In particular:

- approve URL: `/api/v1/resources/{{ vmid }}/plans/approve-active`;
- reject URL: `/api/v1/resources/{{ vmid }}/plans/reject-active`;
- delete-oldest snapshot URL: `/api/v1/resources/{{ vmid }}/snapshots/delete-oldest`;
- delete-unprotected snapshots URL: `/api/v1/resources/{{ vmid }}/snapshots/delete-unprotected`;
- backend, general hostd, and recovery authorizations remain separate;
- no token value is printed by the installer.

An existing 0.4.1 `secrets.yaml` does not contain the two snapshot-prune URLs.
The operator must deliberately add both values before running the 0.4.2 Home
Assistant installer. The installer validates the complete file before backup,
copy, or installation and fails closed when either value is missing or empty.

Install and validate without restarting Core:

```bash
cd /root/hubinet-ops
bash deploy/install-ha-0.4.2-from-pve.sh HA_HOST 22
```

The package and dashboard pass `ha core check`, but newly added scripts are not registered until Core restarts. The installer prints the exact restart command.

To include a reviewed Core restart after the successful check:

```bash
cd /root/hubinet-ops
bash deploy/install-ha-0.4.2-from-pve.sh --restart-core HA_HOST 22
```

This invokes `ha core restart` over SSH and waits for Core state `running`. It does not call Proxmox lifecycle for VM100 and does not use `SUPERVISOR_TOKEN`.

## Post-install read-only evidence

Confirm:

```bash
curl -fsS http://127.0.0.1:8787/health
systemctl status hubinet-ops-hostd --no-pager
```

Then use the authenticated UI or API to refresh state. Do not run an update merely as installer validation. During a separately approved functional maintenance test, a CT106 update should create `hubinet-ops-106-pre-<UTC timestamp>` and finish with operation success, completed stage, zero pending packages, and passed verification.

## Rollback

The PVE installer prints its backup path, normally:

```text
/root/hubinet-ops-backups/<UTC>-before-0.4.2
```

Any installer failure calls the built-in reverse-order rollback. If it reports an incomplete rollback, stop and restore the printed backup as one consistent set:

1. restore CT110 application, configuration, environment, and SQLite database together using the bundled agent restore procedure;
2. restore every visited CT101–CT109 executor and profile;
3. restore PVE host control, hostd, systemd unit, allowlists, and the previous enabled/active service state;
4. run `systemctl daemon-reload` and restore only the previous service state;
5. do not use snapshot rollback, update, lifecycle, or the recovery token as a substitute for file rollback.

The HA installer stores its backup under `/config/backups/hubinet-ops/<UTC>-before-0.4.2`. Its built-in rollback restores `secrets.yaml`, `configuration.yaml`, the package, and dashboard, then runs `ha core check`. If a Core restart was requested, review the restored configuration before a separate restart.
