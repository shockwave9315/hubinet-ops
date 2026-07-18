# Home Assistant installation

Prerequisites are:

- Home Assistant MQTT integration connected to the broker;
- Mushroom installed through HACS and loaded as a Lovelace resource;
- a private `hubinet_ops_webhook_id` already present in `secrets.yaml`;
- `hubinet_ops_notify_service` pointing to the operator's mobile app notification service.

Run `deploy/install-ha-0.2.1-from-pve.sh HA_HOST AGENT_BASE_URL AGENT_VMID [HA_SSH_PORT]` only after reviewing its SSH target. The script checks Mushroom before changing HA, backs up configuration, installs the package/dashboard, merges fixed REST URLs and the existing agent token, and runs `ha core check`. It never creates or replaces the private phone target or webhook ID.

Restart Home Assistant after replacing the package so the automation set is reloaded. Normal state, discovery, logs, progress, and package changes arrive through MQTT and do not use REST polling.

The source-controlled YAML dashboard uses Home Assistant Sections and Mushroom cards. Views are `/hubinet-ops/overview`, `/hubinet-ops/ct-101`, and `/hubinet-ops/ct-106`. The CT views include health/resources, guarded REST controls, a reverse-chronological live job log, and a bounded pending-package list.

The package owns exactly three Hubinet Ops automations:

1. webhook notifications for approval, job start, success, rollback, and failures;
2. a replaceable phone progress notification updated every 10 seconds while a job is running;
3. an availability/health watchdog for the agent and both managed containers.

Notification tags replace earlier Hubinet Ops messages instead of creating a new phone notification for every progress sample. Critical/offline alerts use high-priority delivery; recovery and success replace the same tagged alert.

Approval, rejection, retry, and rollback remain authenticated REST actions available only from the CT dashboard. Push notifications contain navigation data only. Tapping a notification opens the relevant CT view and cannot approve an update.
