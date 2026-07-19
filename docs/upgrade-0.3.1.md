# Upgrade 0.3.0 → 0.3.1

This patch repairs the MQTT/Home Assistant contract and does not change inventory, allowlists, lifecycle policy, maintenance policy, or managed executors.

Review and run the two steps separately from the repository checkout:

```bash
bash deploy/upgrade-0.3.1-from-pve.sh
bash deploy/install-ha-0.3.1-from-pve.sh HA_HOST 22
```

The first script backs up the complete CT110 agent state with the SQLite writer stopped, replaces only application code, and rolls back on any failed health or 11-resource inventory check. The second script backs up Home Assistant package/dashboard/secrets, installs the generated files, and runs `ha core check` with rollback on error.

Home Assistant is intentionally not restarted. Review the successful core check and perform any restart separately under the normal change procedure.
