# Hubinet Ops 0.2.3

Hubinet Ops is a safety-focused operations agent for approved APT updates in allowlisted Proxmox LXC containers. The FastAPI agent runs in a dedicated container, reaches the Proxmox host through a forced-command SSH wrapper, and delegates a fixed action set to `hubinet-maint` inside managed containers.

Version 0.2.3 keeps the 0.2.1 update/stabilization engine and 0.2.2 Mushroom operator dashboard, while bounding the complete Home Assistant MQTT attribute payload to 10,000 UTF-8 bytes so Recorder does not reject oversized rich state.

## Safety model

- REST state-changing endpoints require a bearer token.
- Updates require a plan and manual dashboard approval.
- MQTT publishes telemetry and discovery only; it has no command topics or buttons.
- SSH remains a forced command with explicit action, VMID, and snapshot-name validation.
- Automatic and manual rollback are separate per-container policies.
- Home Assistant is presentation and controlled input; SQLite and the agent are authoritative.
- The scheduler and MQTT are disabled by default.

See [architecture](docs/architecture.md), [state model](docs/state-model.md), and [recovery](docs/recovery.md) for the full contract.

## Repository layout

- `app/`: FastAPI, SQLite, executor, state machine, stabilization, and MQTT.
- `deploy/pve/`: forced-command Proxmox wrapper and access installer.
- `deploy/managed/`: fixed-action managed-container executor and profiles.
- `home-assistant/`: package, MQTT-backed dashboard, and example secrets.
- `deploy/upgrade-0.2.1-from-pve.sh`: backed-up 0.2.0 to 0.2.1 platform upgrade.
- `deploy/upgrade-0.2.3-from-pve.sh`: transactional agent-only MQTT payload upgrade.
- `deploy/install-ha-dashboard-0.2.3-from-pve.sh`: backed-up dashboard-only 0.2.3 deployment.
- `tests/`: fake-only unit and integration workflow tests.

## API

All `/api/v1` endpoints require `Authorization: Bearer ...`.

- `GET /health`
- `GET /api/v1/state`
- `GET /api/v1/containers`
- `GET /api/v1/containers/{vmid}/state`
- `GET /api/v1/containers/{vmid}/events?limit=50`
- `GET /api/v1/jobs/{job_id}/events?limit=50`
- `POST /api/v1/containers/{vmid}/refresh`
- `POST /api/v1/containers/{vmid}/scan`
- `POST /api/v1/containers/{vmid}/retry-healthcheck`
- `POST /api/v1/containers/{vmid}/rollback`
- `POST /api/v1/plans/approve`
- `POST /api/v1/plans/reject`

Rollback is rejected unless policy and recorded state permit it. No endpoint accepts command text.

## Configuration

Copy values from `config/config.example.yaml` into the protected runtime config. MQTT remains disabled until `mqtt.enabled: true` is set with a reachable broker. Paho MQTT `2.1.0` is pinned for Python 3.13.

The per-container stabilization defaults are:

```yaml
stabilization:
  post_update_timeout_seconds: 300
  post_rollback_timeout_seconds: 300
  repair_timeout_seconds: 180
  poll_interval_seconds: 10
  initial_grace_seconds: 10
  required_consecutive_successes: 2
```

Progress is best-effort, stage-weighted, monotonic, and capped at 99 until a terminal event. Package output may not map one-to-one to APT's internal work.

Home Assistant rich attributes are encoded with a strict 10,000-byte budget. Scalar Discovery entities remain complete; package and recent-event previews carry authoritative total/visible/truncated metadata when details must be shortened.

## Development validation

Use Python 3.13 and do not point tests at infrastructure:

```text
python -m pip install -r requirements.txt -r requirements-dev.txt
python -m compileall app
pytest -q
python scripts/validate_yaml.py
python scripts/check_tracked_files.py
```

Validate every `.sh` file and `deploy/pve/hubinet-ops-host` with `bash -n`. CI performs the same checks.

## Deployment

Deployment is intentionally not automatic. Follow [deployment](docs/deployment.md), [Home Assistant installation](docs/home-assistant.md), and the [0.2.3 upgrade guide](docs/upgrade-0.2.3.md). Upgrade scripts back up the components they replace, validate before declaring success, and perform no managed-container scan or package update.
