# Changelog

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
