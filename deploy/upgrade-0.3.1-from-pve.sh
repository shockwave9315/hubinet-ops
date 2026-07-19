#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

[[ ${EUID} -eq 0 || ${HUBINET_OPS_TEST_MODE:-0} == 1 ]] || {
  echo "Run as root on PVE" >&2
  exit 1
}
[[ $# -eq 0 ]] || { echo "This CT110 patch accepts no arguments" >&2; exit 2; }

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAMP="$(date +%Y%m%d-%H%M%S)"
ARCHIVE="${HUBINET_OPS_TEST_ARCHIVE:-/tmp/hubinet-ops-0.3.1-${STAMP}.tgz}"
AGENT_BACKUP="/root/hubinet-ops-backups/${STAMP}-before-0.3.1"
AGENT_VMID=110
backup_complete=false
changes_started=false
complete=false

required_source=(
  app
  deploy/agent/backup-0.3.0.sh
  deploy/agent/restore-0.3.0.sh
)
for path in "${required_source[@]}"; do
  [[ -e "$SOURCE_DIR/$path" ]] || {
    echo "Missing source artifact: $path" >&2
    exit 1
  }
done
grep -Fq 'VERSION = "0.3.1"' "$SOURCE_DIR/app/mqtt.py" || {
  echo "Source tree is not Hubinet Ops 0.3.1" >&2
  exit 1
}

rollback_patch() {
  local rc="${1:-1}" cleanup_failed=false
  trap - ERR INT TERM EXIT
  if [[ "$changes_started" == true && "$backup_complete" == true ]]; then
    echo "0.3.1 patch failed; restoring the complete CT110 backup" >&2
    if ! pct exec "$AGENT_VMID" -- bash -s -- "$AGENT_BACKUP" \
      < "$SOURCE_DIR/deploy/agent/restore-0.3.0.sh"; then
      echo "ROLLBACK INCOMPLETE: CT110 agent restore failed" >&2
      cleanup_failed=true
    fi
  elif [[ "$backup_complete" == true ]]; then
    if ! pct exec "$AGENT_VMID" -- systemctl start hubinet-ops; then
      echo "ROLLBACK INCOMPLETE: the unchanged 0.3.0 agent could not be restarted" >&2
      cleanup_failed=true
    fi
  fi
  if ! pct exec "$AGENT_VMID" -- rm -f /root/hubinet-ops-0.3.1.tgz 2>/dev/null; then
    echo "Warning: remove /root/hubinet-ops-0.3.1.tgz manually in CT110" >&2
  fi
  rm -f "$ARCHIVE"
  [[ "$cleanup_failed" == false ]] || rc=1
  exit "$rc"
}
trap 'rollback_patch $?' ERR
trap 'rollback_patch 130' INT
trap 'rollback_patch 143' TERM
trap 'rm -f "$ARCHIVE"' EXIT

status="$(pct status "$AGENT_VMID" | awk '{print $2}')"
[[ "$status" == running ]] || {
  echo "CT110 must already be running; no lifecycle action was attempted" >&2
  exit 1
}

python3 "$SOURCE_DIR/scripts/validate_yaml.py"
python3 -m compileall -q "$SOURCE_DIR/app"
tar -C "$SOURCE_DIR" -czf "$ARCHIVE" app

# This helper stops the only SQLite writer before copying the complete database.
pct exec "$AGENT_VMID" -- bash -s -- "$AGENT_BACKUP" \
  < "$SOURCE_DIR/deploy/agent/backup-0.3.0.sh"
backup_complete=true
pct push "$AGENT_VMID" "$ARCHIVE" /root/hubinet-ops-0.3.1.tgz --perms 0600
changes_started=true

pct exec "$AGENT_VMID" -- bash -s <<'REMOTE_INSTALL_AGENT'
set -Eeuo pipefail
staging=/root/hubinet-ops-0.3.1
rm -rf "$staging"
install -d -m 0700 "$staging"
tar -xzf /root/hubinet-ops-0.3.1.tgz -C "$staging"
grep -Fq 'VERSION = "0.3.1"' "$staging/app/mqtt.py"
rm -rf /opt/hubinet-ops/app
cp -a "$staging/app" /opt/hubinet-ops/app
chown -R hubinetops:hubinetops /opt/hubinet-ops/app
runuser -u hubinetops -- /opt/hubinet-ops/.venv/bin/python -m compileall -q /opt/hubinet-ops/app
rm -rf "$staging" /root/hubinet-ops-0.3.1.tgz
systemctl start hubinet-ops
REMOTE_INSTALL_AGENT

for attempt in $(seq 1 30); do
  health_rc=0
  health="$(pct exec "$AGENT_VMID" -- curl -fsS --max-time 3 http://127.0.0.1:8787/health 2>/dev/null)" || health_rc=$?
  if [[ "$health_rc" -eq 0 && "$health" == *'"version":"0.3.1"'* ]]; then
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
    [[ "$count" == 11 ]] || {
      echo "Resource inventory count is $count, expected 11" >&2
      rollback_patch 1
    }
    complete=true
    trap - ERR INT TERM EXIT
    rm -f "$ARCHIVE"
    echo "Hubinet Ops 0.3.1 installed transactionally in CT110. No managed resource action was executed."
    echo "Backup: CT110:$AGENT_BACKUP"
    exit 0
  fi
  [[ "$attempt" -eq 30 ]] || sleep 2
done

echo "Agent 0.3.1 health validation failed" >&2
rollback_patch 1
