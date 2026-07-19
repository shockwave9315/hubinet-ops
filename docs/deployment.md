# Deployment and upgrade

No deployment script is run by CI or tests. Review every target and backup location before using these scripts on an administration host.

## Upgrade 0.2.3 to 0.2.4

Follow [the 0.2.4 upgrade guide](upgrade-0.2.4.md). The source may be a git archive without .git metadata. The PVE upgrade and HA installation are separate transactional steps; live lifecycle testing is limited to CT106.

## Upgrade 0.2.0 to 0.2.1

Run the syntax check first, then invoke `deploy/upgrade-0.2.1-from-pve.sh AGENT_VMID CT106_VMID` from an unpacked release on the Proxmox host.

The script:

1. backs up `/opt/hubinet-ops`, `/etc/hubinet-ops`, and `ops.db*` inside the agent CT;
2. refuses to broaden the Proxmox VMID allowlist automatically;
3. updates the forced wrapper and fixed managed executors;
4. installs Python dependencies into the existing virtualenv;
5. adds only missing MQTT/stabilization/config keys;
6. uses `/opt/hubinet-ops/.venv/bin/python` for YAML validation and SQLite migration;
7. starts the API and checks only its local `/health` endpoint.

It performs no managed-container inspect, scan, approval, update, repair, or rollback. It preserves `agent.env`, the API token, SSH keys, configured CT101/CT106 values, and the scheduler's existing disabled state. MQTT is added disabled unless it already existed.

The script prints rollback commands and records the backup directory. Follow [recovery](recovery.md) rather than mixing old code with a migrated live database.

## Fresh install

`deploy/install-agent.sh` is intended for a new isolated agent CT and installs operating-system packages. It is not an upgrade script. After installation, edit protected config, set up the forced SSH key, validate config, and start the service manually. Never use example host names or tokens unchanged.
