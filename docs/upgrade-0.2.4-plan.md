# Hubinet Ops 0.2.4 implementation plan

Version 0.2.4 extends the existing safety model without adding arbitrary shell or MQTT command surfaces.

## Goals

- schedule one delayed update scan after a managed container genuinely recovers from offline, critical, or degraded health;
- keep the recovery scan independent from the periodic scheduler and never approve a plan automatically;
- run explicit package-integrity verification after a successful update and report remaining packages, reboot requirement, Docker health, and verification outcome;
- suppress duplicate `recovered` notifications when a successful update or rollback already produced a terminal notification;
- make YAML validation and deployment work from a `git archive` source tree without requiring `.git`;
- expose authenticated, policy-controlled container actions in Home Assistant.

## Container policy

Every container publishes operator capabilities from backend configuration. The backend remains authoritative even if Home Assistant renders a stale or manually edited card.

Initial production policy:

- CT101 Cloudflared: observation only. No lifecycle control actions are allowed or rendered.
- CT106 WeatherHub: laboratory target for start, graceful shutdown, reboot, refresh, scan, approve, reject, retry-healthcheck, and policy-gated rollback.
- future containers: capabilities are enabled individually after CT106 validation.

## Lifecycle controls

The only new Proxmox lifecycle actions are fixed verbs:

- `start`
- `shutdown` using graceful LXC shutdown
- `reboot` using graceful LXC reboot

There is no arbitrary command text, force-stop, destroy, snapshot deletion, console, or terminal endpoint. Lifecycle operations are serialized with scans and update jobs, rejected while a job is active, restricted by VMID allowlist, and checked against per-container capabilities.

## Recovery scan

A recovery scan is eligible only when:

1. health transitions from offline, critical, or degraded to healthy;
2. the container remains healthy for the configured delay;
3. no scan, approved plan execution, or lifecycle operation is active;
4. no active approval plan already exists;
5. the per-container cooldown has expired.

The result follows the normal scan path. Available packages create a waiting-approval plan; no update starts automatically.

## Post-update verification

After service stabilization, the managed executor verifies:

- `apt-get check`;
- `dpkg --audit`;
- `/var/run/reboot-required`;
- final APT update count;
- systemd and required Docker health.

APT or dpkg integrity failure is treated as an update failure and follows the existing rollback policy. A reboot requirement is reported but is not itself a failure.

## Notifications

The terminal success notification reports updated package count, remaining package count, reboot requirement, Docker required healthy/total, and verification status. Health recovery notifications are suppressed for a short window after an update or rollback terminal event so `gotowe` and `odzyskano` do not duplicate one another.

## Test rollout

All destructive and lifecycle live tests are limited to CT106. CT101 is used only to verify telemetry and that backend/UI policy denies lifecycle controls. Automated tests use fake executors and never contact production infrastructure.
