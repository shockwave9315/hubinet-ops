#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

if [[ ${EUID} -ne 0 && ${HUBINET_OPS_TEST_MODE:-0} != 1 ]]; then
  echo "Run on the Proxmox host as root." >&2
  exit 1
fi

AGENT_VMID="${1:-110}"
CT106_VMID="${2:-106}"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAMP="$(date +%Y%m%d-%H%M%S)"
ARCHIVE="/tmp/hubinet-ops-0.2.4-${STAMP}.tgz"
HOST_BACKUP="/root/hubinet-ops-upgrade-backups/${STAMP}-before-0.2.4"
AGENT_BACKUP="/root/hubinet-ops-backups/${STAMP}-before-0.2.4"
COMPLETE=0

[[ "$AGENT_VMID" =~ ^[1-9][0-9]{1,5}$ ]] || { echo "Invalid agent VMID" >&2; exit 1; }
[[ "$CT106_VMID" =~ ^[1-9][0-9]{1,5}$ ]] || { echo "Invalid CT106 VMID" >&2; exit 1; }
[[ "$CT106_VMID" == "106" ]] || {
  echo "0.2.4 lifecycle capabilities may be enabled only for CT106." >&2
  exit 1
}
[[ -d "$SOURCE_DIR/app" && -f "$SOURCE_DIR/requirements.txt" ]] || {
  echo "Run from a complete Hubinet Ops source archive." >&2
  exit 1
}
[[ -f "$SOURCE_DIR/deploy/pve/lifecycle-vmids" ]] || {
  echo "Missing lifecycle VMID allowlist in source archive." >&2
  exit 1
}
[[ "$(sed '/^[[:space:]]*$/d' "$SOURCE_DIR/deploy/pve/lifecycle-vmids")" == "106" ]] || {
  echo "0.2.4 lifecycle VMID allowlist must contain only CT106." >&2
  exit 1
}
grep -Fq 'VERSION = "0.2.4"' "$SOURCE_DIR/app/mqtt.py" || {
  echo "Source tree is not Hubinet Ops 0.2.4" >&2
  exit 1
}
python3 "$SOURCE_DIR/scripts/validate_yaml.py"

for vmid in 101 "$CT106_VMID"; do
  grep -Fxq "$vmid" /etc/hubinet-ops/allowed-vmids || {
    echo "CT$vmid is not allowlisted; refusing to broaden the allowlist." >&2
    exit 1
  }
  pct status "$vmid" | grep -q running || {
    echo "Managed CT$vmid must already be running; no lifecycle action will be attempted." >&2
    exit 1
  }
done
pct status "$AGENT_VMID" | grep -q running || {
  echo "Agent CT$AGENT_VMID must already be running." >&2
  exit 1
}

restore_all() {
  local rc="${1:-$?}"
  [[ "$rc" -ne 0 ]] || rc=1
  trap - ERR INT TERM
  if [[ $COMPLETE -eq 1 ]]; then
    rm -f "$ARCHIVE"
    return 0
  fi

  echo "0.2.4 upgrade failed; restoring every modified layer." >&2
  if [[ -f "$HOST_BACKUP/hubinet-ops-host" ]]; then
    install -m 0755 "$HOST_BACKUP/hubinet-ops-host" /usr/local/sbin/hubinet-ops-host || true
  fi
  if [[ -f "$HOST_BACKUP/lifecycle-vmids" ]]; then
    install -m 0640 "$HOST_BACKUP/lifecycle-vmids" /etc/hubinet-ops/lifecycle-vmids || true
  elif [[ -f "$HOST_BACKUP/lifecycle-vmids.absent" ]]; then
    rm -f /etc/hubinet-ops/lifecycle-vmids || true
  fi
  for vmid in 101 "$CT106_VMID"; do
    pct exec "$vmid" -- bash -s -- "$STAMP" <<'REMOTE_RESTORE_MANAGED' || true
set -Eeuo pipefail
backup="/root/hubinet-ops-upgrade-backups/$1-before-0.2.4"
[[ -f "$backup/hubinet-maint" ]] && install -m 0755 "$backup/hubinet-maint" /usr/local/sbin/hubinet-maint
[[ -f "$backup/hubinet-maint.json" ]] && install -m 0644 "$backup/hubinet-maint.json" /etc/hubinet-maint.json
REMOTE_RESTORE_MANAGED
  done
  pct exec "$AGENT_VMID" -- bash -s -- "$AGENT_BACKUP" <<'REMOTE_RESTORE_AGENT' || true
set -Eeuo pipefail
backup="$1"
[[ -d "$backup/app" && -f "$backup/config.yaml" ]] || exit 0
systemctl stop hubinet-ops 2>/dev/null || true
rm -rf /opt/hubinet-ops/app
cp -a "$backup/app" /opt/hubinet-ops/app
cp -a "$backup/requirements.txt" /opt/hubinet-ops/requirements.txt
install -m 0644 "$backup/hubinet-ops.service" /etc/systemd/system/hubinet-ops.service
install -m 0640 -o root -g hubinetops "$backup/config.yaml" /etc/hubinet-ops/config.yaml
rm -f /var/lib/hubinet-ops/ops.db*
cp -a "$backup"/ops.db* /var/lib/hubinet-ops/ 2>/dev/null || true
chown hubinetops:hubinetops /var/lib/hubinet-ops/ops.db* 2>/dev/null || true
chown -R hubinetops:hubinetops /opt/hubinet-ops/app /opt/hubinet-ops/requirements.txt
systemctl daemon-reload
systemctl start hubinet-ops
REMOTE_RESTORE_AGENT
  rm -f "$ARCHIVE"
  exit "$rc"
}
trap 'restore_all $?' ERR
trap 'restore_all 130' INT
trap 'restore_all 143' TERM
trap 'rm -f "$ARCHIVE"' EXIT

install -d -m 0700 "$HOST_BACKUP"
cp -a /usr/local/sbin/hubinet-ops-host "$HOST_BACKUP/hubinet-ops-host"
if [[ -f /etc/hubinet-ops/lifecycle-vmids ]]; then
  cp -a /etc/hubinet-ops/lifecycle-vmids "$HOST_BACKUP/lifecycle-vmids"
else
  touch "$HOST_BACKUP/lifecycle-vmids.absent"
fi
for vmid in 101 "$CT106_VMID"; do
  pct exec "$vmid" -- bash -s -- "$STAMP" <<'REMOTE_BACKUP_MANAGED'
set -Eeuo pipefail
backup="/root/hubinet-ops-upgrade-backups/$1-before-0.2.4"
install -d -m 0700 "$backup"
cp -a /usr/local/sbin/hubinet-maint "$backup/hubinet-maint"
cp -a /etc/hubinet-maint.json "$backup/hubinet-maint.json"
REMOTE_BACKUP_MANAGED
done

tar -C "$SOURCE_DIR" -czf "$ARCHIVE" app requirements.txt deploy/hubinet-ops.service
pct push "$AGENT_VMID" "$ARCHIVE" /root/hubinet-ops-0.2.4.tgz --perms 0600

# Stop the only SQLite writer before copying the database and optional WAL/SHM.
pct exec "$AGENT_VMID" -- bash -s -- "$AGENT_BACKUP" <<'REMOTE_BACKUP_AGENT'
set -Eeuo pipefail
backup="$1"
install -d -m 0700 "$backup"
systemctl stop hubinet-ops
cp -a /opt/hubinet-ops/app "$backup/app"
cp -a /opt/hubinet-ops/requirements.txt "$backup/requirements.txt"
cp -a /etc/systemd/system/hubinet-ops.service "$backup/hubinet-ops.service"
cp -a /etc/hubinet-ops/config.yaml "$backup/config.yaml"
cp -a /var/lib/hubinet-ops/ops.db* "$backup/" 2>/dev/null || true
printf '%s\n' "$backup" > /root/hubinet-ops-last-upgrade-backup
REMOTE_BACKUP_AGENT

pct exec "$AGENT_VMID" -- bash -s <<'REMOTE_INSTALL_AGENT'
set -Eeuo pipefail
staging="/root/hubinet-ops-0.2.4"
rm -rf "$staging"
install -d -m 0700 "$staging"
tar -xzf /root/hubinet-ops-0.2.4.tgz -C "$staging"
rm -rf /opt/hubinet-ops/app
cp -a "$staging/app" /opt/hubinet-ops/app
cp -a "$staging/requirements.txt" /opt/hubinet-ops/requirements.txt
install -m 0644 "$staging/deploy/hubinet-ops.service" /etc/systemd/system/hubinet-ops.service
chown -R hubinetops:hubinetops /opt/hubinet-ops/app /opt/hubinet-ops/requirements.txt
(
  umask 022
  /opt/hubinet-ops/.venv/bin/pip install -r /opt/hubinet-ops/requirements.txt
)
chown -R root:root /opt/hubinet-ops/.venv
chmod -R a+rX /opt/hubinet-ops/.venv

/opt/hubinet-ops/.venv/bin/python - <<'PY'
from pathlib import Path
import yaml

path = Path("/etc/hubinet-ops/config.yaml")
data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
containers = data.setdefault("containers", {})
all_caps = (
    "refresh", "scan", "approve", "reject", "retry_healthcheck",
    "rollback", "start", "shutdown", "reboot",
)
for vmid in (101, 106):
    key = vmid if vmid in containers else str(vmid)
    if key not in containers:
        raise RuntimeError(f"Required CT{vmid} is missing from config")
    cfg = containers[key]
    if vmid == 101:
        cfg["operator_capabilities"] = {name: False for name in all_caps}
        cfg["recovery_scan"] = {
            "enabled": False, "delay_seconds": 90, "cooldown_seconds": 900,
        }
    else:
        cfg["operator_capabilities"] = {name: True for name in all_caps}
        cfg["operator_capabilities"]["rollback"] = bool(
            cfg.get("manual_rollback_allowed", False)
        )
        cfg["recovery_scan"] = {
            "enabled": True, "delay_seconds": 90, "cooldown_seconds": 900,
        }
path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
PY
chown root:hubinetops /etc/hubinet-ops/config.yaml
chmod 0640 /etc/hubinet-ops/config.yaml
set -a
source /etc/hubinet-ops/agent.env
set +a
runuser -u hubinetops --preserve-environment -- env PYTHONPATH=/opt/hubinet-ops /opt/hubinet-ops/.venv/bin/python -c 'from app.config import load_settings; from app.database import Database; s=load_settings(); Database(s.db_path)'
runuser -u hubinetops -- /opt/hubinet-ops/.venv/bin/python -m compileall -q /opt/hubinet-ops/app
systemctl daemon-reload
rm -rf "$staging" /root/hubinet-ops-0.2.4.tgz
REMOTE_INSTALL_AGENT

install -m 0755 "$SOURCE_DIR/deploy/pve/hubinet-ops-host" /usr/local/sbin/hubinet-ops-host
install -m 0640 "$SOURCE_DIR/deploy/pve/lifecycle-vmids" /etc/hubinet-ops/lifecycle-vmids
for vmid in 101 "$CT106_VMID"; do
  pct push "$vmid" "$SOURCE_DIR/deploy/managed/hubinet-maint" /usr/local/sbin/hubinet-maint --perms 0755
  pct exec "$vmid" -- python3 -m py_compile /usr/local/sbin/hubinet-maint
done

pct exec "$AGENT_VMID" -- systemctl start hubinet-ops
for attempt in $(seq 1 30); do
  result="$(pct exec "$AGENT_VMID" -- curl -fsS --max-time 3 http://127.0.0.1:8787/health 2>/dev/null || true)"
  if [[ "$result" == *'"status":"ok"'* && "$result" == *'"version":"0.2.4"'* ]]; then
    printf '%s\n' "$result"
    COMPLETE=1
    trap - ERR INT TERM
    rm -f "$ARCHIVE"
    echo "Hubinet Ops 0.2.4 installed. No managed CT action was executed."
    echo "Backups: $HOST_BACKUP and CT$AGENT_VMID:$AGENT_BACKUP"
    exit 0
  fi
  sleep 2
done

echo "Agent 0.2.4 health validation failed" >&2
restore_all 1
