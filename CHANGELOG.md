# Changelog

## 0.3.2

- restore the production `apt_check`, `dpkg_audit`, and `packages_remaining` entity IDs without changing unique IDs;
- move CT106 controls directly below status, hide final verification on observation-only CTs, clarify unknown verification, and cap live logs at ten entries;
- source VM100 CPU from `/cluster/resources --type vm` while retaining `status/current` for the remaining QEMU fields;
- expose QEMU and agent-self byte metrics as numeric GiB sensors and render uptime and agent refresh time readably;
- publish one deduplicated agent summary per full telemetry cycle with a second-precision UTC completion timestamp;
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
