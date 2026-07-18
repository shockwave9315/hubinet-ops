# Home Assistant installation

Prerequisites are Home Assistant MQTT integration connected to the broker, a private webhook secret already present in `secrets.yaml`, and a mobile notification target adjusted for the local HA installation.

Run `deploy/install-ha-0.2.1-from-pve.sh HA_HOST AGENT_BASE_URL AGENT_VMID [HA_SSH_PORT]` only after reviewing its SSH target. It backs up HA configuration, installs the package/dashboard, merges fixed REST URLs and the existing agent token, and runs `ha core check`. It does not restart HA automatically.

A one-time HA restart may be required when adding the package/dashboard registration. Normal state, discovery, log, and progress changes arrive through MQTT and require no restart or REST sensor refresh.

Views are `/hubinet-ops/overview`, `/hubinet-ops/ct-101`, and `/hubinet-ops/ct-106`. Add a future CT view by duplicating a CT view and changing the stable discovery entity prefix. Discovery creates the device/entities automatically from backend configuration.

Approval and rollback buttons live only on CT views and have confirmation dialogs. Push notifications contain no action buttons; tapping uses both `url` and `clickAction` to open the backend-provided dashboard path.
