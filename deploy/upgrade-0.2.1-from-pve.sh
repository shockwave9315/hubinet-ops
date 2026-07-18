#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Run on the Proxmox host as root." >&2
  exit 1
fi

AGENT_VMID="${1:-110}"
CT106_VMID="${2:-106}"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAMP="$(date +%Y%m%d-%H%M%S)"
ARCHIVE="/tmp/hubinet-ops-0.2.1-agent-${STAMP}.tgz"
trap 'rm -f "$ARCHIVE"' EXIT

[[ "$AGENT_VMID" =~ ^[1-9][0-9]{1,5}$ ]] || { echo "Invalid agent VMID" >&2; exit 1; }
[[ "$CT106_VMID" =~ ^[1-9][0-9]{1,5}$ ]] || { echo "Invalid CT106 VMID" >&2; exit 1; }
pct status "$AGENT_VMID" | grep -q running || { echo "Agent CT is not running" >&2; exit 1; }

echo "=== BACKUP 0.2.0 AGENT ==="
pct exec "$AGENT_VMID" -- bash -s -- "$STAMP" <<'REMOTE_BACKUP'
set -Eeuo pipefail
stamp="$1"
backup="/root/hubinet-ops-backups/$stamp"
install -d -m 0700 "$backup"
cp -a /opt/hubinet-ops "$backup/opt-hubinet-ops"
cp -a /etc/hubinet-ops "$backup/etc-hubinet-ops"
cp -a /var/lib/hubinet-ops/ops.db* "$backup/" 2>/dev/null || true
printf '%s\n' "$backup" > /root/hubinet-ops-last-upgrade-backup
REMOTE_BACKUP

echo "=== PACKAGE 0.2.1 ==="
tar -C "$SOURCE_DIR" -czf "$ARCHIVE" app requirements.txt deploy/hubinet-ops.service
pct push "$AGENT_VMID" "$ARCHIVE" /root/hubinet-ops-0.2.1-agent.tgz --perms 0600

echo "=== UPDATE FORCED-COMMAND WRAPPER AND MANAGED EXECUTORS ==="
install -m 0755 "$SOURCE_DIR/deploy/pve/hubinet-ops-host" /usr/local/sbin/hubinet-ops-host
for vmid in 101 "$CT106_VMID"; do
  grep -Fxq "$vmid" /etc/hubinet-ops/allowed-vmids || {
    echo "CT$vmid is not in /etc/hubinet-ops/allowed-vmids; refusing to broaden the allowlist automatically." >&2
    exit 1
  }
  pct push "$vmid" "$SOURCE_DIR/deploy/managed/hubinet-maint" /usr/local/sbin/hubinet-maint --perms 0755
done
pct exec "$CT106_VMID" -- cp -a /etc/hubinet-maint.json "/etc/hubinet-maint.json.pre-0.2.1-$STAMP"
pct exec "$CT106_VMID" -- python3 -c '
import json
from pathlib import Path
p = Path("/etc/hubinet-maint.json")
d = json.loads(p.read_text(encoding="utf-8"))
d.setdefault("repair_actions", ["restart_services", "restart_required_containers"])
p.write_text(json.dumps(d, indent=2) + "\n", encoding="utf-8")
'

echo "=== INSTALL AND MIGRATE AGENT ==="
pct exec "$AGENT_VMID" -- bash -s -- "$STAMP" <<'REMOTE_UPGRADE'
set -Eeuo pipefail
stamp="$1"
staging="/root/hubinet-ops-0.2.1-agent"
rm -rf "$staging"
install -d -m 0700 "$staging"
tar -xzf /root/hubinet-ops-0.2.1-agent.tgz -C "$staging"

systemctl stop hubinet-ops
rm -rf /opt/hubinet-ops/app
cp -a "$staging/app" /opt/hubinet-ops/app
cp "$staging/requirements.txt" /opt/hubinet-ops/requirements.txt
chown -R hubinetops:hubinetops /opt/hubinet-ops/app /opt/hubinet-ops/requirements.txt
/opt/hubinet-ops/.venv/bin/pip install -r /opt/hubinet-ops/requirements.txt
install -m 0644 "$staging/deploy/hubinet-ops.service" /etc/systemd/system/hubinet-ops.service

/opt/hubinet-ops/.venv/bin/python - <<'PY'
from pathlib import Path
import yaml

path = Path("/etc/hubinet-ops/config.yaml")
data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
data.setdefault("scheduler", {}).setdefault("enabled", False)
mqtt = data.setdefault("mqtt", {})
for key, value in {
    "enabled": False,
    "host": "mqtt-broker.local",
    "port": 1883,
    "username": "",
    "password": "",
    "client_id": "hubinet-ops-agent",
    "base_topic": "hubinet/ops",
    "discovery_prefix": "homeassistant",
    "keepalive_seconds": 60,
    "reconnect_min_seconds": 2,
    "reconnect_max_seconds": 60,
    "retain_state": True,
}.items():
    mqtt.setdefault(key, value)
defaults = {
    "post_update_timeout_seconds": 300,
    "post_rollback_timeout_seconds": 300,
    "repair_timeout_seconds": 180,
    "poll_interval_seconds": 10,
    "initial_grace_seconds": 10,
    "required_consecutive_successes": 2,
}
containers = data.setdefault("containers", {})
resolved = {}
for vmid in (101, 106):
    key = vmid if vmid in containers else str(vmid) if str(vmid) in containers else vmid
    container = containers.setdefault(key, {})
    resolved[vmid] = container
    container.setdefault("dashboard_path", f"/hubinet-ops/ct-{vmid}")
    container.setdefault("manual_rollback_allowed", False)
    stabilization = container.setdefault("stabilization", {})
    for key, value in defaults.items():
        stabilization.setdefault(key, value)
resolved[101].setdefault("repair_actions", [])
resolved[106].setdefault("repair_actions", ["restart_services", "restart_required_containers"])
path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
PY

chown root:hubinetops /etc/hubinet-ops/config.yaml
chmod 0640 /etc/hubinet-ops/config.yaml
set -a
source /etc/hubinet-ops/agent.env
set +a
runuser -u hubinetops --preserve-environment -- env PYTHONPATH=/opt/hubinet-ops \
  /opt/hubinet-ops/.venv/bin/python -c \
  'from app.config import load_settings; from app.database import Database; s=load_settings(); Database(s.db_path)'
systemctl daemon-reload
systemctl start hubinet-ops
curl -fsS http://127.0.0.1:8787/health
printf '\n'
REMOTE_UPGRADE

cat <<EOF

Hubinet Ops 0.2.1 upgrade completed without scanning or updating a managed CT.
The scheduler was not enabled and MQTT remains disabled unless it was already configured.

Agent rollback:
  pct exec $AGENT_VMID -- systemctl stop hubinet-ops
  pct exec $AGENT_VMID -- cat /root/hubinet-ops-last-upgrade-backup
Restore opt-hubinet-ops, etc-hubinet-ops, and ops.db files from that directory, then start hubinet-ops.
EOF
