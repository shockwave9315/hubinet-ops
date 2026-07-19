#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

if [[ ${EUID} -ne 0 ]]; then
  echo "Run on the Proxmox host as root." >&2
  exit 1
fi

AGENT_VMID="${1:-110}"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAMP="$(date +%Y%m%d-%H%M%S)"
ARCHIVE="/tmp/hubinet-ops-0.2.3-agent-${STAMP}.tgz"
BACKUP="/root/hubinet-ops-backups/${STAMP}-before-0.2.3"
COMPLETE=0

[[ "$AGENT_VMID" =~ ^[1-9][0-9]{1,5}$ ]] || {
  echo "Invalid agent VMID" >&2
  exit 1
}
[[ -d "$SOURCE_DIR/app" && -f "$SOURCE_DIR/requirements.txt" ]] || {
  echo "Run this script from a complete 0.2.3 source tree." >&2
  exit 1
}
grep -Fq 'VERSION = "0.2.3"' "$SOURCE_DIR/app/mqtt_v023.py" || {
  echo "Source tree is not Hubinet Ops 0.2.3" >&2
  exit 1
}
pct status "$AGENT_VMID" | grep -q running || {
  echo "Agent CT$AGENT_VMID is not running" >&2
  exit 1
}

restore_agent() {
  local rc="${1:-$?}"
  [[ "$rc" -ne 0 ]] || rc=1
  trap - ERR INT TERM
  if [[ $COMPLETE -eq 1 ]]; then
    rm -f "$ARCHIVE"
    return 0
  fi

  echo >&2
  echo "=== 0.2.3 UPGRADE FAILED: RESTORING AGENT ===" >&2
  pct exec "$AGENT_VMID" -- bash -s -- "$BACKUP" <<'REMOTE_RESTORE' || true
set -Eeuo pipefail
backup="$1"
systemctl stop hubinet-ops 2>/dev/null || true
if [[ -d "$backup/app" ]]; then
  rm -rf /opt/hubinet-ops/app
  cp -a "$backup/app" /opt/hubinet-ops/app
fi
if [[ -f "$backup/requirements.txt" ]]; then
  cp -a "$backup/requirements.txt" /opt/hubinet-ops/requirements.txt
fi
if [[ -f "$backup/hubinet-ops.service" ]]; then
  install -m 0644 "$backup/hubinet-ops.service" /etc/systemd/system/hubinet-ops.service
fi
if compgen -G "$backup/ops.db*" >/dev/null; then
  rm -f /var/lib/hubinet-ops/ops.db*
  cp -a "$backup"/ops.db* /var/lib/hubinet-ops/
  chown hubinetops:hubinetops /var/lib/hubinet-ops/ops.db*
fi
chown -R hubinetops:hubinetops /opt/hubinet-ops/app /opt/hubinet-ops/requirements.txt
systemctl daemon-reload
systemctl start hubinet-ops
REMOTE_RESTORE
  rm -f "$ARCHIVE"
  echo "Previous Hubinet Ops agent was restored. Original exit code: $rc" >&2
  exit "$rc"
}
trap 'restore_agent $?' ERR
trap 'restore_agent 130' INT
trap 'restore_agent 143' TERM
trap 'rm -f "$ARCHIVE"' EXIT

printf '=== PACKAGE AGENT 0.2.3 ===\n'
tar -C "$SOURCE_DIR" -czf "$ARCHIVE" app requirements.txt deploy/hubinet-ops.service
pct push "$AGENT_VMID" "$ARCHIVE" /root/hubinet-ops-0.2.3-agent.tgz --perms 0600

printf '=== STOP AND BACK UP AGENT ===\n'
pct exec "$AGENT_VMID" -- bash -s -- "$BACKUP" <<'REMOTE_BACKUP'
set -Eeuo pipefail
backup="$1"
install -d -m 0700 "$backup"
systemctl stop hubinet-ops
cp -a /opt/hubinet-ops/app "$backup/app"
cp -a /opt/hubinet-ops/requirements.txt "$backup/requirements.txt"
cp -a /etc/systemd/system/hubinet-ops.service "$backup/hubinet-ops.service"
cp -a /var/lib/hubinet-ops/ops.db* "$backup/" 2>/dev/null || true
printf '%s\n' "$backup" > /root/hubinet-ops-last-upgrade-backup
REMOTE_BACKUP

printf '=== INSTALL AGENT 0.2.3 ===\n'
pct exec "$AGENT_VMID" -- bash -s <<'REMOTE_INSTALL'
set -Eeuo pipefail
staging="/root/hubinet-ops-0.2.3-agent"
rm -rf "$staging"
install -d -m 0700 "$staging"
tar -xzf /root/hubinet-ops-0.2.3-agent.tgz -C "$staging"
rm -rf /opt/hubinet-ops/app
cp -a "$staging/app" /opt/hubinet-ops/app
cp "$staging/requirements.txt" /opt/hubinet-ops/requirements.txt
chown -R hubinetops:hubinetops /opt/hubinet-ops/app /opt/hubinet-ops/requirements.txt
(
  umask 022
  /opt/hubinet-ops/.venv/bin/pip install -r /opt/hubinet-ops/requirements.txt
)
chown -R root:root /opt/hubinet-ops/.venv
chmod -R a+rX /opt/hubinet-ops/.venv
runuser -u hubinetops -- /opt/hubinet-ops/.venv/bin/python -m compileall -q /opt/hubinet-ops/app
install -m 0644 "$staging/deploy/hubinet-ops.service" /etc/systemd/system/hubinet-ops.service
systemctl daemon-reload
systemctl start hubinet-ops
rm -rf "$staging" /root/hubinet-ops-0.2.3-agent.tgz
REMOTE_INSTALL

printf '=== VALIDATE 0.2.3 ===\n'
for attempt in $(seq 1 30); do
  result="$(pct exec "$AGENT_VMID" -- curl -fsS --max-time 3 http://127.0.0.1:8787/health 2>/dev/null || true)"
  if [[ "$result" == *'"status":"ok"'* && "$result" == *'"version":"0.2.3"'* ]]; then
    printf '%s\n' "$result"
    COMPLETE=1
    rm -f "$ARCHIVE"
    echo "Agent 0.2.3 installed. Backup: CT$AGENT_VMID:$BACKUP"
    exit 0
  fi
  sleep 2
done

echo "Agent 0.2.3 health validation failed" >&2
restore_agent 1
