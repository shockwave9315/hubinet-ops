# Hubinet Ops 0.4.0 rollout and rollback

This is an operator-run production procedure. Repository tests never execute it.

## Preconditions

1. Review the exact release commit and `docs/design-0.4.0.md`.
2. On PVE, create root-owned `/etc/hubinet-ops/hostd.json` from the example with the real management bind/client allowlist.
3. Put pairwise-distinct bearer values of at least 32 characters in root-only `/etc/hubinet-ops/hostd.env`: `HUBINET_OPS_HOSTD_TOKEN`, `HUBINET_OPS_HOSTD_BACKEND_TOKEN`, `HUBINET_OPS_HOSTD_UPDATE_TOKEN`, and `HUBINET_OPS_HOSTD_RECOVERY_TOKEN`. The upgrade can generate missing scoped values, but operators must provision the matching HA general/recovery secrets through their secret-management process. Do not add either production file to git.
4. Confirm CT110 is already running. The installer will not start it.
5. Confirm sufficient PVE/CT110 backup space and an explicit maintenance window.

## PVE and agent transaction

From the reviewed release tree on PVE:

```bash
bash deploy/upgrade-0.4.0-from-pve.sh
```

The script accepts no arguments. It backs up host files and complete CT110 state, backs up executor/profile in every CT101–CT109, uses `pct push/exec` for running CTs and `pct mount/unmount` for stopped CTs, and never changes their runtime. It migrates existing CT110 configuration without replacing inventory, addresses, MQTT, or health definitions.

Success requires hostd health, read-only wrapper inspection, executor capabilities/hashes, DB migration 400, fresh exact `/api/v1/state` inventory 100–110, healthy VM100/CT106/CT110, numeric VM100 CPU, and read-only snapshot lists. No operator action or update is run.

Any failure triggers reverse-order rollback of executors/profiles, hostd/wrapper/allowlists, and CT110. The printed backup directory remains for audit/manual recovery. If rollback reports `ROLLBACK INCOMPLETE`, stop and restore the named backup as one consistent set; never mix an old database with new application files.

## Home Assistant transaction

After reviewing host/agent validation:

```bash
bash deploy/install-ha-0.4.0-from-pve.sh HA_HOST 22
```

Required `secrets.yaml` keys are listed in `home-assistant/secrets.example.yaml`, including backend and hostd URLs/tokens. The script backs up `configuration.yaml`, `secrets.yaml`, package, and dashboard, stages replacements, and runs `ha core check`. It rolls back on error, never restarts Home Assistant, and never edits `.storage/core.entity_registry`.

Restart/reload Home Assistant only as a separate reviewed operator decision. Old Recorder rows remain until the existing purge policy removes them.

## Release rollback

Use the backup path printed by the failed/successful transaction. Restore all affected layers together: CT110 application/config/database, each CT101–CT109 executor/profile, shared host implementation, wrapper, hostd/unit/allowlists, and original hostd enabled/active state. Do not invoke lifecycle, snapshot rollback, update, or plan approval merely to test rollback.
