# Deployment

No deployment is automatic. Release 0.3.1 is a narrow patch over an already installed 0.3.0 release and has two separately reviewed transactional steps:

1. `deploy/upgrade-0.3.1-from-pve.sh` updates only the CT110 application directory.
2. `deploy/install-ha-0.3.1-from-pve.sh HA_HOST [PORT]` updates only the Home Assistant package and generated dashboard.

The CT110 patch accepts no VMID override and requires CT110 to already be running. It does not install managed executors, modify inventory or allowlists, or invoke scan, update, lifecycle, snapshot, repair, or managed-resource rollback actions. The existing backup helper stops the only SQLite writer before copying application code, requirements, unit, configuration, environment, and the non-empty database. On failure, the complete backup is restored. Success requires `/health` version 0.3.1 and exactly 11 authenticated inventory resources.

The Home Assistant step backs up package, dashboard, and secrets, checks the generated artifact, installs staged files, and runs `ha core check`. It rolls the files back if validation fails and never restarts Home Assistant or edits `.storage/core.entity_registry`.

The original 0.3.0 full-release scripts remain unchanged for historical installation and recovery. See [the exact 0.3.1 procedure](upgrade-0.3.1.md).
