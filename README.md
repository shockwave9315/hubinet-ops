# Hubinet Ops 0.3.0

Hubinet Ops is a policy-controlled Proxmox inventory, telemetry, and manually approved APT maintenance service. Version 0.3.0 models LXC, QEMU/HAOS, and the agent itself as explicit resources while preserving the 0.2.4 update, rollback, lifecycle, SQLite, MQTT, and Home Assistant safety contracts.

The production inventory contains VM/CT 100–110. VM100 (HAOS), CT101–105, CT107–110 are observation-only. CT106 WeatherHub is the only lifecycle-enabled and live-test target. Observation-only policy does not disable internal telemetry or scheduled APT scans.

## Safety model

- Every `/api/v1` endpoint uses bearer authentication.
- The backend and SQLite are authoritative; Home Assistant only presents state and requests fixed actions.
- Updates still require a fingerprinted plan and explicit approval.
- MQTT is telemetry/discovery only and has no command topics.
- The PVE SSH key is restricted to `deploy/pve/hubinet-ops-host`; no shell or command text is accepted.
- Observation, managed-read, maintenance, lifecycle, and resource-type files are separate fail-closed controls.
- Lifecycle contains exactly CT106. VM100 and CT110 cannot be managed through the wrapper.
- Rich retained state remains bounded to 10,000 UTF-8 bytes.

## Repository map

- `app/`: API, resource policy/service, adapters, SQLite, MQTT, and state normalization.
- `config/config.example.yaml`: complete production-shaped resource inventory without credentials.
- `deploy/pve/`: forced-command wrapper and versioned allowlists/type map.
- `deploy/managed/`: fixed LXC executor, transactional installer, and CT101–109 profiles.
- `home-assistant/`: package, generated dashboard, and secret examples.
- `scripts/generate_ha_dashboard.py`: deterministic inventory-driven Lovelace generator.
- `deploy/upgrade-0.3.0-from-pve.sh`: transactional 0.2.4 → 0.3.0 upgrade.
- `deploy/install-ha-0.3.0-from-pve.sh`: backed-up HA file installer; validates but never restarts HA.

## Local validation

```bash
python -m compileall -q app tests
pytest -q
python -m py_compile deploy/managed/hubinet-maint
python scripts/validate_yaml.py
python scripts/generate_ha_dashboard.py --check
bash -n deploy/upgrade-0.3.0-from-pve.sh
bash -n deploy/install-ha-0.3.0-from-pve.sh
bash -n deploy/pve/hubinet-ops-host
python scripts/check_tracked_files.py
```

Repository tests use fake executors, fake clocks, temporary SQLite databases, and stub commands. They do not contact Proxmox, guests, Home Assistant, or MQTT.

See [architecture](docs/architecture.md), [API](docs/api.md), [security](docs/security.md), [resource adapters](docs/resource-adapters.md), [production inventory](docs/production-inventory.md), [MQTT](docs/mqtt.md), [Home Assistant](docs/home-assistant.md), and the [0.3.0 upgrade guide](docs/upgrade-0.3.0.md).
