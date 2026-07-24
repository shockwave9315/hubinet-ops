# Changelog

## 0.4.1

- allow hostd to write LXC `rules.seccomp.tmp` files under `/var/lib/lxc` while retaining `ProtectSystem=strict`;
- source LXC runtime from PVE independently of guest executor inspect, so a missing executor degrades guest health without falsely reporting a running CT as stopped;
- decouple hostd lifecycle and snapshot operations from executor compatibility, keep APT scan/update/repair/verify gates, and record executor drift after a successful rollback without changing the rollback result;
- create the hostd state directory through systemd and the installer, and extend the strict sandbox only for PVE task logs, LXC locks, and LVM metadata backups required by real snapshot operations;
- replace unsupported `pct listsnapshot --output-format json` with node-resolved `pvesh` snapshot listing while preserving ownership, `current`, ordering, retention, rollback, and delete semantics;
- retry only `pct` rc=129 up to three times inside an ERR-trap-safe conditional and install running-CT executors/profiles idempotently;
- make the 0.4.x config migration idempotently align CT101–CT109 rollback capability with `manual_rollback_allowed`, without changing database schema version 400;
- validate large fresh-state payloads through stdin instead of argv and emit bounded, condition-specific rollout diagnostics;
- probe and persist the 0.4.1 executor contract during APT refresh while retaining safe inspect telemetry when the contract is incompatible;
- shorten new pre-update snapshot names to the `pre` physical alias, normalize it back to logical `pre-update`, retain legacy names, and enforce PVE's 40-character limit;
- remove timestamp device classes from nullable MQTT diagnostics and make every capability template safe when the capability object or key is absent;
- correct dashboard visibility for manual snapshots and waiting plans, retain numeric VMID script calls, and regenerate the deterministic dashboard;
- require complete Home Assistant secrets, reject legacy plan URLs, document the active-plan endpoints, and add an optional checked `--restart-core` workflow while keeping no-restart as the default;
- decouple `pre_update_snapshot` policy from `automatic_rollback` so a pre-update safety snapshot can be created independently of the automatic rollback requirement;
- display an explicit `insufficient_health_contract` warning in the Home Assistant dashboard when a missing healthcheck contract blocks automatic rollback.

## 0.4.0

- add the `hubinet-maint` 0.4.0/protocol 1 capabilities contract with required actions plus executor/profile hashes and profile validation status;
- expand policy-controlled APT maintenance and lifecycle/snapshot controls to CT101–CT109 while keeping VM100 observation-only;
- add shared typed PVE host control and a root-owned, bearer-authenticated, request-bounded, durable `hubinet-ops-hostd` path for CT110 offline control;
- unify update, lifecycle, force-stop, snapshot, retry-healthcheck, and self-update work as persistent idempotent jobs with startup reconciliation;
- add owned snapshot create/list/latest/rollback/delete, configurable retention, foreign-snapshot rejection, and post-rollback state invalidation;
- add VMID-only active-plan approval/rejection endpoints with explicit conflicts and remove Home Assistant's dependency on `active_plan_id` health attributes;
- expose executor and snapshot summary entities without restoring changing full-state health attributes;
- add complete guarded Mushroom controls for CT101–CT110, local timestamp formatting, short plan/job IDs, and independent CT110 host actions;
- add transactional PVE/CT110/managed-LXC and Home Assistant installers with full cross-layer rollback and read-only final validation.

## 0.3.2

- restore the production `apt_check`, `dpkg_audit`, and `packages_remaining` entity IDs without changing unique IDs;
- move CT106 controls directly below status, hide final verification on observation-only CTs, clarify unknown verification, and cap live logs at ten entries;
- source VM100 CPU from `/cluster/resources --type vm` while retaining `status/current` for the remaining QEMU fields;
- expose QEMU and agent-self byte metrics as numeric GiB sensors and render uptime and agent refresh time readably;
- publish one deduplicated agent summary per full telemetry cycle with a second-precision UTC completion timestamp;
- exclude only Hubinet Ops `last_refresh` sensors from future Recorder history without changing the user's Recorder retention or database settings;
- move health attributes to a separately bounded, retained, deduplicated MQTT topic so metric-only refreshes do not create Recorder attribute churn;
- add read-only VM100/CT106 wrapper and first-cycle telemetry smoke checks to the transactional 0.3.2 installer;
- require fresh per-resource timestamps and bounded VM100 CPU values before the 0.3.2 installer accepts its first telemetry cycle;
- add transactional 0.3.2 CT110/PVE-wrapper and Home Assistant installers with rollback and no automatic HA restart.

## 0.3.1

- unify MQTT discovery and dashboard entity IDs in one adapter-aware contract while preserving existing unique IDs;
- publish numeric unknown values as `None`, keep byte metrics inside the 10 KB state budget, and use one bounded primary IP state;
- correct CT110 self-health scoring and remove unsupported QEMU/network discovery from the self adapter;
- clear only the exact retained CT110 discovery topics retired by this release;
- restore a responsive, deterministic Mushroom dashboard for the complete VM/CT 100–110 inventory;
- add transactional CT110-only and Home Assistant-only patch installers with rollback and no automatic HA restart.

## 0.2.3

- cap the complete Home Assistant MQTT attribute payload at 10,000 UTF-8 bytes, leaving margin below Recorder's 16,384-byte hard limit;
- preserve the existing `health_status` attribute contract so the 0.2.2 dashboard and approval scripts keep working without an entity migration;
- trim package and live-event previews by encoded byte size rather than only by item count;
- keep up to 200 compact package previews and 50 newest compact job events when they fit, with visible/total/truncated metadata;
- compact oversized Docker, failed-unit, IP-address, and error details while preserving all scalar Discovery and dashboard-control fields;
- show the authoritative package total and an explicit MQTT-preview truncation notice on both CT dashboard views;
- add regression coverage with long Unicode package names, malformed collection shapes, nested Docker data, and oversized errors;
- add transactional agent and dashboard-only Proxmox installers with backups, rollback, venv permission validation, and health/config checks.

## 0.2.2

- redesigned the Home Assistant UI as a responsive Sections dashboard built with Mushroom cards;
- added compact container summaries, resource cards, guarded action cards, and Polish operator labels;
- changed live job logs to a reverse-chronological bounded table with stage icons and current-operation summary;
- limited the visible pending-package list to 30 entries while preserving the full bounded MQTT state;
- replaced the legacy 0.2.1 webhook automation with dedicated event, live-progress, and health-watchdog automations;
- added one replaceable mobile progress notification per container, updated every 10 seconds during a job;
- added high-priority agent/CT offline and critical alerts with tagged recovery notifications;
- kept every mobile notification navigation-only: approval, rejection, retry, and rollback remain dashboard REST actions;
- added a Mushroom prerequisite check to the HA installer and expanded dashboard/notification safety tests.

## 0.2.1

- added optional non-blocking MQTT telemetry with LWT, retained state, reconnect backoff, and Home Assistant Discovery;
- added indexed persistent `job_events`, monotonic progress, bounded redaction, and authenticated event APIs;
- added incremental NDJSON executor output while preserving legacy JSON responses;
- separated health, update, operation, job stage, and last operation result;
- added interruptible post-update, post-repair, and post-rollback Docker/systemd stabilization;
- restricted repair to explicitly configured actions and retained rollback policy checks;
- replaced HA REST polling with MQTT entities and live event/package dashboard cards;
- kept approval, rejection, retry, and rollback on authenticated REST dashboard controls only;
- added navigation-only notifications for the correct CT dashboard;
- added a backed-up 0.2.0 upgrade path, Python 3.13 CI, repository validators, and expanded fake-based tests;
- revalidate the exact approved package fingerprint immediately before snapshot/update and serialize scans, approvals, retries, and jobs per VMID;
- preserve FIFO MQTT delivery across disconnects and retry the same failed publish before later messages;
- require configured Docker healthchecks to report `healthy` instead of treating a missing healthcheck as success;
- terminate the full APT process group on timeout and prevent event callback failures from repeating destructive actions;
- make the 0.2.1 upgrade transactional across the agent, forced-command wrapper, and managed executors;
- canonicalize forced-command SSH keys so an older unrestricted copy cannot survive installation;
- fix CI so pytest failures propagate through `tee` and upload a diagnostic test log on every run;
- move the Home Assistant notification target to the private `hubinet_ops_notify_service` secret.

## 0.2.0

- persistent container state;
- state and refresh API;
- dashboard-first approval flow and reject endpoint;
- CT106 Docker health profile;
- Home Assistant REST sensors and dashboard;
- notification deep links;
- job stage/progress state;
- post-update scan and safer plan terminal states.
