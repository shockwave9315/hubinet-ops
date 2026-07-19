#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

[[ ${EUID} -eq 0 || ${HUBINET_OPS_TEST_MODE:-0} == 1 ]] || {
  echo "Run as root on PVE" >&2
  exit 1
}
[[ $# -eq 0 ]] || { echo "This production-inventory upgrade accepts no VMID overrides" >&2; exit 1; }

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAMP="$(date +%Y%m%d-%H%M%S)"
ARCHIVE="/tmp/hubinet-ops-0.3.0-${STAMP}.tgz"
HOST_BACKUP_BASE="${HUBINET_OPS_HOST_BACKUP_BASE:-/root/hubinet-ops-upgrade-backups}"
HOST_BACKUP="$HOST_BACKUP_BASE/${STAMP}-before-0.3.0"
AGENT_BACKUP="/root/hubinet-ops-backups/${STAMP}-before-0.3.0"
AGENT_VMID=110
complete=false

required_source=(
  app requirements.txt config/config.example.yaml deploy/hubinet-ops.service
  deploy/pve/hubinet-ops-host deploy/pve/observation-vmids
  deploy/pve/managed-vmids deploy/pve/lifecycle-vmids deploy/pve/resource-types
  deploy/pve/maintenance-vmids
  deploy/managed/hubinet-maint deploy/managed/install-managed.sh
)
for path in "${required_source[@]}"; do
  [[ -e "$SOURCE_DIR/$path" ]] || { echo "Missing source artifact: $path" >&2; exit 1; }
done
grep -Fq 'VERSION = "0.3.0"' "$SOURCE_DIR/app/mqtt.py" || {
  echo "Source tree is not Hubinet Ops 0.3.0" >&2
  exit 1
}
[[ "$(sed '/^[[:space:]]*$/d' "$SOURCE_DIR/deploy/pve/lifecycle-vmids")" == "106" ]] || {
  echo "Lifecycle allowlist must contain exactly CT106" >&2
  exit 1
}
diff -u <(seq 100 110) "$SOURCE_DIR/deploy/pve/observation-vmids"
diff -u <(seq 101 109) "$SOURCE_DIR/deploy/pve/managed-vmids"
[[ "$(sed '/^[[:space:]]*$/d' "$SOURCE_DIR/deploy/pve/maintenance-vmids")" == "106" ]] || {
  echo "Maintenance allowlist must contain exactly CT106" >&2
  exit 1
}
python3 "$SOURCE_DIR/scripts/validate_yaml.py"
python3 -m py_compile "$SOURCE_DIR/deploy/managed/hubinet-maint"

for vmid in $(seq 100 110); do
  if [[ "$vmid" == 100 ]]; then
    raw="$(qm status "$vmid")"
  else
    raw="$(pct status "$vmid")"
  fi
  state="$(awk '{print $2}' <<<"$raw")"
  [[ "$state" =~ ^(running|stopped)$ ]] || {
    echo "Resource $vmid has an unsupported preflight state: $state" >&2
    exit 1
  }
done

backup_optional() {
  local source="$1" name="$2"
  if [[ -e "$source" ]]; then
    cp -a "$source" "$HOST_BACKUP/$name"
  else
    : > "$HOST_BACKUP/$name.absent"
  fi
}

restore_optional() {
  local target="$1" name="$2" mode="$3"
  if [[ -e "$HOST_BACKUP/$name" ]]; then
    install -m "$mode" "$HOST_BACKUP/$name" "$target" || true
  elif [[ -e "$HOST_BACKUP/$name.absent" ]]; then
    rm -f "$target" || true
  fi
}

backup_managed() {
  local vmid="$1" target="$HOST_BACKUP/managed/$1" status mount_output mountpoint
  install -d -m 0700 "$target"
  status="$(pct status "$vmid" | awk '{print $2}')"
  if [[ "$status" == running ]]; then
    pct pull "$vmid" /usr/local/sbin/hubinet-maint "$target/hubinet-maint" >/dev/null 2>&1 \
      || : > "$target/hubinet-maint.absent"
    pct pull "$vmid" /etc/hubinet-maint.json "$target/hubinet-maint.json" >/dev/null 2>&1 \
      || : > "$target/hubinet-maint.json.absent"
    : > "$target/backup.complete"
    return
  fi
  mount_output="$(pct mount "$vmid")"
  mountpoint="$(sed -n "s/.*'\([^']*\)'.*/\1/p" <<<"$mount_output")"
  [[ -n "$mountpoint" && "$mountpoint" == /* && -d "$mountpoint" ]] || {
    echo "Could not determine a safe CT$vmid mountpoint for backup" >&2
    exit 1
  }
  if [[ -f "$mountpoint/usr/local/sbin/hubinet-maint" ]]; then
    cp -a "$mountpoint/usr/local/sbin/hubinet-maint" "$target/hubinet-maint"
  else
    : > "$target/hubinet-maint.absent"
  fi
  if [[ -f "$mountpoint/etc/hubinet-maint.json" ]]; then
    cp -a "$mountpoint/etc/hubinet-maint.json" "$target/hubinet-maint.json"
  else
    : > "$target/hubinet-maint.json.absent"
  fi
  pct unmount "$vmid"
  : > "$target/backup.complete"
}

remove_ct_file() {
  local vmid="$1" path="$2" status mount_output mountpoint
  status="$(pct status "$vmid" | awk '{print $2}')"
  if [[ "$status" == running ]]; then
    pct exec "$vmid" -- rm -f "$path" || true
    return
  fi
  mount_output="$(pct mount "$vmid")" || return
  mountpoint="$(sed -n "s/.*'\([^']*\)'.*/\1/p" <<<"$mount_output")"
  if [[ -n "$mountpoint" && "$mountpoint" == /* ]]; then
    rm -f "$mountpoint$path" || true
  fi
  pct unmount "$vmid" >/dev/null 2>&1 || true
}

restore_managed() {
  local vmid="$1" source="$HOST_BACKUP/managed/$1" status mount_output mountpoint
  [[ -f "$source/backup.complete" ]] || return
  status="$(pct status "$vmid" | awk '{print $2}')"
  if [[ "$status" == running ]]; then
    if [[ -f "$source/hubinet-maint" ]]; then
      pct push "$vmid" "$source/hubinet-maint" /usr/local/sbin/hubinet-maint --perms 0755 || true
    else
      remove_ct_file "$vmid" /usr/local/sbin/hubinet-maint
    fi
    if [[ -f "$source/hubinet-maint.json" ]]; then
      pct push "$vmid" "$source/hubinet-maint.json" /etc/hubinet-maint.json --perms 0644 || true
    else
      remove_ct_file "$vmid" /etc/hubinet-maint.json
    fi
    return
  fi
  mount_output="$(pct mount "$vmid")" || return
  mountpoint="$(sed -n "s/.*'\([^']*\)'.*/\1/p" <<<"$mount_output")"
  if [[ -n "$mountpoint" && "$mountpoint" == /* && -d "$mountpoint" ]]; then
    if [[ -f "$source/hubinet-maint" ]]; then
      install -m 0755 "$source/hubinet-maint" "$mountpoint/usr/local/sbin/hubinet-maint" || true
    else
      rm -f "$mountpoint/usr/local/sbin/hubinet-maint" || true
    fi
    if [[ -f "$source/hubinet-maint.json" ]]; then
      install -m 0644 "$source/hubinet-maint.json" "$mountpoint/etc/hubinet-maint.json" || true
    else
      rm -f "$mountpoint/etc/hubinet-maint.json" || true
    fi
  fi
  pct unmount "$vmid" >/dev/null 2>&1 || true
}

restore_all() {
  local rc="${1:-$?}"
  trap - ERR INT TERM
  if [[ "$complete" == true ]]; then
    rm -f "$ARCHIVE"
    return 0
  fi
  echo "0.3.0 upgrade failed; restoring every modified layer" >&2
  restore_optional /usr/local/sbin/hubinet-ops-host hubinet-ops-host 0755
  restore_optional /etc/hubinet-ops/allowed-vmids allowed-vmids 0640
  restore_optional /etc/hubinet-ops/observation-vmids observation-vmids 0640
  restore_optional /etc/hubinet-ops/managed-vmids managed-vmids 0640
  restore_optional /etc/hubinet-ops/maintenance-vmids maintenance-vmids 0640
  restore_optional /etc/hubinet-ops/lifecycle-vmids lifecycle-vmids 0640
  restore_optional /etc/hubinet-ops/resource-types resource-types 0640
  for vmid in $(seq 101 109); do restore_managed "$vmid"; done
  pct exec "$AGENT_VMID" -- bash -s -- "$AGENT_BACKUP" <<'REMOTE_RESTORE_AGENT' || true
set -Eeuo pipefail
backup="$1"
systemctl stop hubinet-ops 2>/dev/null || true
if [[ -f "$backup/backup.complete" ]]; then
  rm -rf /opt/hubinet-ops/app
  cp -a "$backup/app" /opt/hubinet-ops/app
  cp -a "$backup/requirements.txt" /opt/hubinet-ops/requirements.txt
  install -m 0644 "$backup/hubinet-ops.service" /etc/systemd/system/hubinet-ops.service
  install -m 0640 -o root -g hubinetops "$backup/config.yaml" /etc/hubinet-ops/config.yaml
  install -m 0600 "$backup/agent.env" /etc/hubinet-ops/agent.env
  rm -f /var/lib/hubinet-ops/ops.db /var/lib/hubinet-ops/ops.db-wal /var/lib/hubinet-ops/ops.db-shm
  cp -a "$backup"/ops.db* /var/lib/hubinet-ops/ 2>/dev/null || true
  chown hubinetops:hubinetops /var/lib/hubinet-ops/ops.db* 2>/dev/null || true
  chown -R hubinetops:hubinetops /opt/hubinet-ops/app /opt/hubinet-ops/requirements.txt
  systemctl daemon-reload
fi
systemctl start hubinet-ops
REMOTE_RESTORE_AGENT
  rm -f "$ARCHIVE"
  exit "$rc"
}
trap 'restore_all $?' ERR
trap 'restore_all 130' INT
trap 'restore_all 143' TERM
trap 'rm -f "$ARCHIVE"' EXIT

install -d -m 0700 "$HOST_BACKUP/managed"
backup_optional /usr/local/sbin/hubinet-ops-host hubinet-ops-host
backup_optional /etc/hubinet-ops/allowed-vmids allowed-vmids
backup_optional /etc/hubinet-ops/observation-vmids observation-vmids
backup_optional /etc/hubinet-ops/managed-vmids managed-vmids
backup_optional /etc/hubinet-ops/maintenance-vmids maintenance-vmids
backup_optional /etc/hubinet-ops/lifecycle-vmids lifecycle-vmids
backup_optional /etc/hubinet-ops/resource-types resource-types
for vmid in $(seq 101 109); do backup_managed "$vmid"; done

tar -C "$SOURCE_DIR" -czf "$ARCHIVE" \
  app requirements.txt config/config.example.yaml deploy/hubinet-ops.service
pct push "$AGENT_VMID" "$ARCHIVE" /root/hubinet-ops-0.3.0.tgz --perms 0600
pct exec "$AGENT_VMID" -- bash -s -- "$AGENT_BACKUP" <<'REMOTE_BACKUP_AGENT'
set -Eeuo pipefail
backup="$1"
install -d -m 0700 "$backup"
# Stop the only SQLite writer before copying the database and WAL/SHM files.
systemctl stop hubinet-ops
cp -a /opt/hubinet-ops/app "$backup/app"
cp -a /opt/hubinet-ops/requirements.txt "$backup/requirements.txt"
cp -a /etc/systemd/system/hubinet-ops.service "$backup/hubinet-ops.service"
cp -a /etc/hubinet-ops/config.yaml "$backup/config.yaml"
cp -a /etc/hubinet-ops/agent.env "$backup/agent.env"
cp -a /var/lib/hubinet-ops/ops.db* "$backup/" 2>/dev/null || true
touch "$backup/backup.complete"
printf '%s\n' "$backup" > /root/hubinet-ops-last-upgrade-backup
REMOTE_BACKUP_AGENT

pct exec "$AGENT_VMID" -- bash -s <<'REMOTE_INSTALL_AGENT'
set -Eeuo pipefail
staging=/root/hubinet-ops-0.3.0
rm -rf "$staging"
install -d -m 0700 "$staging"
tar -xzf /root/hubinet-ops-0.3.0.tgz -C "$staging"
rm -rf /opt/hubinet-ops/app
cp -a "$staging/app" /opt/hubinet-ops/app
cp -a "$staging/requirements.txt" /opt/hubinet-ops/requirements.txt
install -m 0644 "$staging/deploy/hubinet-ops.service" /etc/systemd/system/hubinet-ops.service
/opt/hubinet-ops/.venv/bin/python - "$staging/config/config.example.yaml" <<'PY'
from pathlib import Path
import sys, yaml

target = Path("/etc/hubinet-ops/config.yaml")
old = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
template = yaml.safe_load(Path(sys.argv[1]).read_text(encoding="utf-8")) or {}
for key in ("api", "executor", "scheduler", "mqtt", "home_assistant"):
    if key in old:
        template[key] = old[key]
target.write_text(yaml.safe_dump(template, sort_keys=False, allow_unicode=True), encoding="utf-8")
PY
chown root:hubinetops /etc/hubinet-ops/config.yaml
chmod 0640 /etc/hubinet-ops/config.yaml
chown -R hubinetops:hubinetops /opt/hubinet-ops/app /opt/hubinet-ops/requirements.txt
set -a
source /etc/hubinet-ops/agent.env
set +a
runuser -u hubinetops --preserve-environment -- env PYTHONPATH=/opt/hubinet-ops \
  /opt/hubinet-ops/.venv/bin/python -c 'from app.config import load_settings; from app.database import Database; s=load_settings(); Database(s.db_path)'
runuser -u hubinetops -- /opt/hubinet-ops/.venv/bin/python -m compileall -q /opt/hubinet-ops/app
systemctl daemon-reload
rm -rf "$staging" /root/hubinet-ops-0.3.0.tgz
REMOTE_INSTALL_AGENT

install -m 0755 "$SOURCE_DIR/deploy/pve/hubinet-ops-host" /usr/local/sbin/hubinet-ops-host
install -m 0640 "$SOURCE_DIR/deploy/pve/observation-vmids" /etc/hubinet-ops/observation-vmids
install -m 0640 "$SOURCE_DIR/deploy/pve/observation-vmids" /etc/hubinet-ops/allowed-vmids
install -m 0640 "$SOURCE_DIR/deploy/pve/managed-vmids" /etc/hubinet-ops/managed-vmids
install -m 0640 "$SOURCE_DIR/deploy/pve/maintenance-vmids" /etc/hubinet-ops/maintenance-vmids
install -m 0640 "$SOURCE_DIR/deploy/pve/lifecycle-vmids" /etc/hubinet-ops/lifecycle-vmids
install -m 0640 "$SOURCE_DIR/deploy/pve/resource-types" /etc/hubinet-ops/resource-types
for vmid in $(seq 101 109); do
  "$SOURCE_DIR/deploy/managed/install-managed.sh" "$vmid"
done

pct exec "$AGENT_VMID" -- systemctl start hubinet-ops
for _attempt in $(seq 1 30); do
  health="$(pct exec "$AGENT_VMID" -- curl -fsS --max-time 3 http://127.0.0.1:8787/health 2>/dev/null || true)"
  if [[ "$health" == *'"version":"0.3.0"'* ]]; then
    resources="$(pct exec "$AGENT_VMID" -- bash -s <<'REMOTE_CHECK_RESOURCES'
set -Eeuo pipefail
set -a
source /etc/hubinet-ops/agent.env
set +a
curl -fsS --max-time 5 -H "Authorization: Bearer $HUBINET_OPS_API_TOKEN" \
  http://127.0.0.1:8787/api/v1/resources
REMOTE_CHECK_RESOURCES
)"
    count="$(python3 -c 'import json,sys; print(len(json.load(sys.stdin)))' <<<"$resources")"
    [[ "$count" == 11 ]] || { echo "Resource inventory count is $count, expected 11" >&2; exit 1; }
    complete=true
    trap - ERR INT TERM
    rm -f "$ARCHIVE"
    echo "Hubinet Ops 0.3.0 installed transactionally. No resource management action was executed."
    echo "Backups: $HOST_BACKUP and CT110:$AGENT_BACKUP"
    exit 0
  fi
  sleep 2
done

echo "Agent 0.3.0 health validation failed" >&2
restore_all 1
