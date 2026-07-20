# Hubinet Ops 0.4.0

Hubinet Ops is a policy-controlled Proxmox inventory, telemetry, lifecycle, snapshot, and manually approved APT maintenance service. Version 0.4.0 manages LXC CT101–CT110 through typed, durable jobs while keeping VM100 Home Assistant observation-only.

The production inventory remains exactly VM/CT 100–110. CT101–CT109 use the versioned `hubinet-maint` 0.4.0 compatibility contract for APT operations. Lifecycle and Hubinet-owned snapshots are available for CT101–CT110. CT110 uses an independent PVE `hubinet-ops-hostd`, so Home Assistant can start it while its in-guest API is offline.

## Safety model

- Every `/api/v1` endpoint uses bearer authentication.
- The backend and SQLite are authoritative; Home Assistant only presents state and requests fixed actions.
- Updates still require a fingerprinted plan and explicit approval.
- MQTT is telemetry/discovery only and has no command topics.
- The PVE SSH key is restricted to `deploy/pve/hubinet-ops-host`; no shell or command text is accepted.
- Observation, managed-read, maintenance, lifecycle, host-control, and resource-type files are separate fail-closed controls.
- Every destructive operation checks capability, runtime, concurrency, unresolved plans, executor protocol/actions/hashes, and snapshot ownership in the backend and PVE boundary.
- Only `hubinet-ops-{vmid}-{pre-update|manual}-{UTC timestamp}` snapshots can be rolled back or deleted.
- Rich retained attributes remain separately bounded to 10,000 UTF-8 bytes and are deduplicated.

## Repository map

- `app/`: API, resource policy/service, adapters, SQLite, MQTT, and state normalization.
- `config/config.example.yaml`: complete production-shaped resource inventory without credentials.
- `deploy/pve/`: shared typed host control, forced-command wrapper, durable hostd, systemd unit, and versioned allowlists/type map.
- `deploy/managed/`: fixed LXC executor, transactional installer, and CT101–109 profiles.
- `home-assistant/`: package, generated dashboard, and secret examples.
- `scripts/generate_ha_dashboard.py`: deterministic inventory-driven Lovelace generator.
- `deploy/upgrade-0.3.0-from-pve.sh`: transactional 0.2.4 → 0.3.0 upgrade.
- `deploy/install-ha-0.3.0-from-pve.sh`: historical full-release HA installer.
- `deploy/upgrade-0.3.1-from-pve.sh`: transactional CT110-only 0.3.0 → 0.3.1 patch.
- `deploy/install-ha-0.3.1-from-pve.sh`: transactional package/dashboard patch; checks but never restarts HA.
- `deploy/upgrade-0.4.0-from-pve.sh`: transactional hostd/wrapper, CT101–CT109 executor/profile, CT110 application/config/database rollout.
- `deploy/install-ha-0.4.0-from-pve.sh`: transactional HA package/dashboard rollout with `ha core check` and no automatic restart.

## Local validation

```bash
python -m compileall -q app tests
pytest -q
python -m py_compile deploy/managed/hubinet-maint deploy/pve/hubinet_ops_host_control.py deploy/pve/hubinet_ops_hostd.py
python scripts/validate_managed_profiles.py
python scripts/validate_yaml.py
python scripts/generate_ha_dashboard.py --check
bash -n deploy/upgrade-0.4.0-from-pve.sh
bash -n deploy/install-ha-0.4.0-from-pve.sh
bash -n deploy/pve/hubinet-ops-host
bash tests/shell/runtime_smoke_0_4_0.sh
python scripts/check_tracked_files.py
```

Repository tests use fake executors, fake clocks, temporary SQLite databases, and stub commands. They do not contact Proxmox, guests, Home Assistant, or MQTT.

See [0.4.0 design](docs/design-0.4.0.md), [architecture](docs/architecture.md), [API](docs/api.md), [security](docs/security.md), [resource adapters](docs/resource-adapters.md), [production inventory](docs/production-inventory.md), [MQTT](docs/mqtt.md), [Home Assistant](docs/home-assistant.md), and the [0.4.0 rollout guide](docs/upgrade-0.4.0.md).
