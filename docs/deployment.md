# Deployment

No deployment is automatic. Release 0.3.2 is a narrow patch over an already installed 0.3.1 release and has two separately reviewed transactional steps:

1. `deploy/upgrade-0.3.2-from-pve.sh` updates the CT110 application directory and the PVE forced-command wrapper.
2. `deploy/install-ha-0.3.2-from-pve.sh HA_HOST [PORT]` updates only the Home Assistant package and generated dashboard.

The agent patch accepts no VMID override and requires CT110 to already be running. It does not install managed executors, modify inventory or allowlists, or invoke scan, update, lifecycle, snapshot, repair, or managed-resource rollback actions. It backs up the current PVE wrapper and the complete CT110 installation, validates Python and shell syntax, and restores both layers on failure. Success requires `/health` version 0.3.2 and exactly 11 authenticated inventory resources.

The Home Assistant step backs up package, dashboard, and secrets, checks the generated artifact, installs staged files, and runs `ha core check`. It rolls the files back if validation fails and never restarts Home Assistant or edits `.storage/core.entity_registry`.

The original release scripts remain available for historical installation and recovery. See [the exact 0.3.2 procedure](upgrade-0.3.2.md).
