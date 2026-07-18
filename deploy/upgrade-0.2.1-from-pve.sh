#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

if [[ ${EUID} -ne 0 ]]; then
  echo "Run on the Proxmox host as root." >&2
  exit 1
fi

AGENT_VMID="${1:-110}"
CT106_VMID="${2:-106}"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAMP="$(date +%Y%m%d-%H%M%S)"
ARCHIVE="/tmp/hubinet-ops-0.2.1-agent-${STAMP}.tgz"
HOST_BACKUP="/root/hubinet-ops-upgrade-backups/${STAMP}"
AGENT_BACKUP="/root/hubinet-ops-backups/${STAMP}"
COMPLETE=0

[[ "$AGENT_VMID" =~ ^[1-9][0-9]{1,5}$ ]] || { echo "Invalid agent VMID" >&2; exit 1; }
[[ "$CT106_VMID" =~ ^[1-9][0-9]{1,5}$ ]] || { echo "Invalid CT106 VMID" >&2; exit 1; }
[[ -d "$SOURCE_DIR/app" && -f "$SOURCE_DIR/requirements.txt" ]] || {
  echo "Run this script from a complete 0.2.1 source tree." >&2
  exit 1
}
pct status "$AGENT_VMID" | grep -q running || {
  echo "Agent CT is not running" >&2
  exit 1
}
for vmid in 101 "$CT106_VMID"; do
  pct status "$vmid" | grep -q running || {
    echo "Managed CT$vmid is not running" >&2
    exit 1
  }
  grep -Fxq "$vmid" /etc/hubinet-ops/allowed-vmids || {
    echo "CT$vmid is not in /etc/hubinet-ops/allowed-vmids; refusing to broaden the allowlist automatically." >&2
    exit 1
  }
done

restore_all() {
  local rc=$?
  trap - ERR INT TERM
  if [[ $COMPLETE -eq 1 ]]; then
    rm -f "$ARCHIVE"
    return 0
  fi

  echo >&2
  echo "=== UPGRADE FAILED: RESTORING PREVIOUS COMPONENTS ===" >&2

  if [[ -f "$HOST_BACKUP/hubinet-ops-host" ]]; then
    install -m 0755 "$HOST_BACKUP/hubinet-ops-host" /usr/local/sbin/hubinet-ops-host || true
  fi

  for vmid in 101 "$CT106_VMID"; do
    pct exec "$vmid" -- bash -s -- "$STAMP" <<'REMOTE_RESTORE_MANAGED' || true
set -Eeuo pipefail
stamp="$1"
backup="/root/hubinet-ops-upgrade-backups/$stamp"
if [[ -f "$backup/hubinet-maint" ]]; then
  install -m 0755 "$backup/hubinet-maint" /usr/local/sbin/hubinet-maint
fi
if [[ -f "$backup/hubinet-maint.json" ]]; then
  install -m 0644 "$backup/hubinet-maint.json" /etc/hubinet-maint.json
fi
REMOTE_RESTORE_MANAGED
  done

  pct exec "$AGENT_VMID" -- bash -s -- "$AGENT_BACKUP" <<'REMOTE_RESTORE_AGENT' || true
set -Eeuo pipefail
backup="$1"
systemctl stop hubinet-ops 2>/dev/null || true
if [[ -d "$backup/opt-hubinet-ops" ]]; then
  rm -rf /opt/hubinet-ops
  cp -a "$backup/opt-hubinet-ops" /opt/hubinet-ops
fi
if [[ -d "$backup/etc-hubinet-ops" ]]; then
  rm -rf /etc/hubinet-ops
  cp -a "$backup/etc-hubinet-ops" /etc/hubinet-ops
fi
if compgen -G "$backup/ops.db*" >/dev/null; then
  rm -f /var/lib/hubinet-ops/ops.db*
  cp -a "$backup"/ops.db* /var/lib/hubinet-ops/
  chown hubinetops:hubinetops /var/lib/hubinet-ops/ops.db*
fi
systemctl daemon-reload
systemctl start hubinet-ops
REMOTE_RESTORE_AGENT

  rm -f "$ARCHIVE"
  echo "Previous Hubinet Ops components were restored. Original exit code: $rc" >&2
  exit "$rc"
}
trap restore_all ERR INT TERM
trap 'rm -f "$ARCHIVE"' EXIT

install -d -m 0700 "$HOST_BACKUP"
cp -a /usr/local/sbin/hubinet-ops-host "$HOST_BACKUP/hubinet-ops-host"

for vmid in 101 "$CT106_VMID"; do
  pct exec "$vmid" -- bash -s -- "$STAMP" <<'REMOTE_BACKUP_MANAGED'
set -Eeuo pipefail
stamp="$1"
backup="/root/hubinet-ops-upgrade-backups/$stamp"
install -d -m 0700 "$backup"
cp -a /usr/local/sbin/hubinet-maint "$backup/hubinet-maint"
cp -a /etc/hubinet-maint.json "$backup/hubinet-maint.json"
REMOTE_BACKUP_MANAGED
done

echo "=== PACKAGE 0.2.1 ==="
tar -C "$SOURCE_DIR" -czf "$ARCHIVE" app requirements.txt deploy/hubinet-ops.service
pct push "$AGENT_VMID" "$ARCHIVE" /root/hubinet-ops-0.2.1-agent.tgz --perms 0600

echo "=== STOP AND BACK UP 0.2.0 AGENT ==="
pct exec "$AGENT_VMID" -- bash -s -- "$STAMP" <<'REMOTE_BACKUP_AGENT'
set -Eeuo pipefail
stamp="$1"
backup="/root/hubinet-ops-backups/$stamp"
install -d -m 0700 "$backup"
systemctl stop hubinet-ops
cp -a /opt/hubinet-ops "$backup/opt-hubinet-ops"
cp -a /etc/hubinet-ops "$backup/etc-hubinet-ops"
# The writer is stopped, so copying the SQLite database and optional WAL/SHM is consistent.
cp -a /var/lib/hubinet-ops/ops.db* "$backup/" 2>/dev/null || true
printf '%s\n' "$backup" > /root/hubinet-ops-last-upgrade-backup
REMOTE_BACKUP_AGENT

echo "=== INSTALL AND MIGRATE AGENT ==="
pct exec "$AGENT_VMID" -- bash -s -- "$STAMP" <<'REMOTE_UPGRADE'
set -Eeuo pipefail
stamp="$1"
staging="/root/hubinet-ops-0.2.1-agent"
rm -rf "$staging"
install -d -m 0700 "$staging"
tar -xzf /root/hubinet-ops-0.2.1-agent.tgz -C "$staging"

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
for raw_vmid, container in containers.items():
    vmid = int(raw_vmid)
    container.setdefault("dashboard_path", f"/hubinet-ops/ct-{vmid}")
    container.setdefault("manual_rollback_allowed", False)
    stabilization = container.setdefault("stabilization", {})
    for key, value in defaults.items():
        stabilization.setdefault(key, value)

for vmid, default_actions in {
    101: [],
    106: ["restart_services", "restart_required_containers"],
}.items():
    key = vmid if vmid in containers else str(vmid)
    if key in containers:
        containers[key].setdefault("repair_actions", default_actions)

path.write_text(
    yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
    encoding="utf-8",
)
PY

chown root:hubinetops /etc/hubinet-ops/config.yaml
chmod 0640 /etc/hubinet-ops/config.yaml
set -a
source /etc/hubinet-ops/agent.env
set +a
runuser -u hubinetops --preserve-environment -- env PYTHONPATH=/opt/hubinet-ops \
  /opt/hubinet-ops/.venv/bin/python -c \
  'from app.config import load_settings; from app.database import Database; s=load_settings(); Database(s.db_path)'
/opt/hubinet-ops/.venv/bin/python -m compileall -q /opt/hubinet-ops/app
systemctl daemon-reload
REMOTE_UPGRADE

echo "=== UPDATE FORCED-COMMAND WRAPPER AND MANAGED EXECUTORS ==="
install -m 0755 "$SOURCE_DIR/deploy/pve/hubinet-ops-host" /usr/local/sbin/hubinet-ops-host
for vmid in 101 "$CT106_VMID"; do
  pct push "$vmid" "$SOURCE_DIR/deploy/managed/hubinet-maint" /usr/local/sbin/hubinet-maint --perms 0755
  pct exec "$vmid" -- python3 -m py_compile /usr/local/sbin/hubinet-maint
done
pct exec "$CT106_VMID" -- python3 - <<'PY'
import json
from pathlib import Path

path = Path("/etc/hubinet-maint.json")
data = json.loads(path.read_text(encoding="utf-8"))
data.setdefault("repair_actions", ["restart_services", "restart_required_containers"])
path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
PY

echo "=== START AND VERIFY AGENT ==="
pct exec "$AGENT_VMID" -- systemctl start hubinet-ops
for attempt in $(seq 1 30); do
  if pct exec "$AGENT_VMID" -- curl -fsS --max-time 3 http://127.0.0.1:8787/health; then
    printf '\n'
    break
  fi
  if [[ "$attempt" -eq 30 ]]; then
    echo "Agent health endpoint did not recover" >&2
    false
  fi
  sleep 1
done

COMPLETE=1
trap - ERR INT TERM

cat <<EOF

Hubinet Ops 0.2.1 upgrade completed without scanning or updating a managed CT.
The scheduler was not enabled and MQTT remains disabled unless it was already configured.

Backups:
  PVE components: $HOST_BACKUP
  Agent components: $AGENT_BACKUP
  Managed CT backups: /root/hubinet-ops-upgrade-backups/$STAMP inside CT101 and CT$CT106_VMID

Agent rollback marker:
  pct exec $AGENT_VMID -- cat /root/hubinet-ops-last-upgrade-backup
EOF
