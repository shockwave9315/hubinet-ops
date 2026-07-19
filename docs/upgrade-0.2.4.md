# Upgrade Hubinet Ops to 0.2.4

Hubinet Ops 0.2.4 adds backend-authoritative operator capabilities, fixed Proxmox lifecycle actions, delayed recovery scans, explicit post-update verification, notification deduplication, and archive-safe YAML validation. It does not add command text, a terminal, MQTT command topics, or unattended approval.

Start and reboot report success only for the bounded LXC action and confirmed `running` state; service health remains explicitly pending until the next inspect. A confirmed graceful shutdown is marked intentional so the Home Assistant watchdog does not emit a false offline alert. Final APT refresh/scan repository failures are verification warnings (`update_status: unknown`) and do not trigger rollback when APT/dpkg and required services remain healthy.

## Safety boundary

- CT101 Cloudflared is observation-only. Every operator capability is false in backend configuration and no dashboard control is rendered.
- CT106 WeatherHub is the only lifecycle and live rollout target. Its rollback capability remains coupled to manual_rollback_allowed.
- The PVE forced-command wrapper requires lifecycle VMIDs in both the existing general allowlist and `/etc/hubinet-ops/lifecycle-vmids`; the 0.2.4 lifecycle list contains only `106`, so CT101 is denied even if the wrapper is invoked directly over SSH.
- MQTT remains telemetry/discovery only. The 10,000-byte UTF-8 state budget is unchanged.
- No upgrade script scans, updates, starts, shuts down, reboots, repairs, or rolls back a managed CT.
- The backend and SQLite remain authoritative; Home Assistant only invokes fixed authenticated REST endpoints.

## Backups and automatic rollback

The PVE upgrade script backs up:

1. the PVE forced-command wrapper and the dedicated lifecycle VMID allowlist;
2. hubinet-maint and its protected config in CT101 and CT106;
3. agent code, requirements, systemd unit, protected agent config, and ops.db files in CT110.

The CT110 service is stopped before SQLite is copied. Any failed install, import, compile, or health/version check restores all modified layers and restarts the previous agent. Tokens, MQTT credentials, SSH keys, the existing general VMID allowlist, and environment files are never replaced. The dedicated lifecycle allowlist is backed up and restored transactionally, and the upgrade refuses any source list other than exactly CT106.

The separate HA installer backs up the package, dashboard, and secrets.yaml. It preserves the existing webhook, notify target, bearer authorization, and every existing secret. It appends only missing lifecycle URL secrets, runs ha core check, and restores the backup on failure. It does not restart Home Assistant.

## Deployment order

From a reviewed git archive extraction on the Proxmox administration host:

    python3 scripts/validate_yaml.py
    bash -n deploy/upgrade-0.2.4-from-pve.sh
    bash -n deploy/install-ha-0.2.4-from-pve.sh
    bash deploy/upgrade-0.2.4-from-pve.sh 110 106
    bash deploy/install-ha-0.2.4-from-pve.sh HA_HOST http://AGENT_ADDRESS:8787 SSH_PORT

Review the printed backup paths. Reload the HA package using the operator's normal controlled procedure; the installer deliberately performs no automatic HA restart.

## Live test plan — CT106 only

Use an approved maintenance window and test only CT106:

1. confirm CT101 publishes all operator capabilities as false and every crafted CT101 action returns 409;
2. refresh CT106 and verify capabilities, lifecycle, verification, and recovery fields in REST/MQTT;
3. with CT106 already stopped by the operator outside this test, exercise start and confirm a manual recovery notification;
4. with no active job/plan, exercise graceful reboot and graceful shutdown/start, confirming serialization and navigation-only notifications;
5. simulate offline/degraded to healthy and verify exactly one scan after 90 seconds, no approval, and a 900-second cooldown;
6. approve one reviewed CT106 update plan and verify APT/dpkg, remaining packages, Docker healthy/total, reboot warning, and the single terminal success notification;
7. verify automatic/manual rollback only under the existing snapshot and policy requirements.

Do not run lifecycle or destructive live tests on CT101 or any newly onboarded CT.

## Onboarding another container

Add the CT once under containers and configure every operator_capabilities boolean plus recovery_scan. The backend, MQTT discovery, API checks, and state projection are generic by VMID; do not copy routes or service logic. Begin with every capability false and enable actions individually only after wrapper allowlisting, managed-executor validation, backup review, and a container-specific maintenance plan.
