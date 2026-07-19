# Deployment

No deployment is automatic. Release 0.3.0 has two separately reviewed transactional steps:

1. `deploy/upgrade-0.3.0-from-pve.sh` on PVE updates the wrapper/maps, CT101–109 managed executors, and CT110 agent.
2. `deploy/install-ha-0.3.0-from-pve.sh HA_HOST AGENT_BASE_URL [PORT]` updates Home Assistant files.

The PVE script accepts no VMID override, validates VM/QEMU 100 and LXC 101–110 existence, and accepts stopped resources. It does not scan, update, start, stop, reboot, snapshot, repair, or roll back any managed resource. It stops only the CT110 agent service to back up SQLite consistently, then validates `/health` version 0.3.0 and the authenticated 11-resource inventory.

Backups cover agent code/config/env/SQLite/unit, wrapper, old/general and new allowlists/type map, and every managed executor/profile. Any failure invokes the full restore path. Home Assistant installation is separate and has its own backup/rollback.

See [the exact 0.3.0 procedure](upgrade-0.3.0.md).
