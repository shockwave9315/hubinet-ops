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
ARCHIVE="${HUBINET_OPS_TEST_ARCHIVE:-/tmp/hubinet-ops-0.3.2-${STAMP}.tgz}"
BACKUP_ROOT="${HUBINET_OPS_BACKUP_ROOT:-/root/hubinet-ops-backups}"
AGENT_BACKUP="$BACKUP_ROOT/${STAMP}-before-0.3.2"
WRAPPER_BACKUP_DIR="$BACKUP_ROOT/${STAMP}-before-0.3.2-pve"
HOST_WRAPPER="${HUBINET_OPS_HOST_WRAPPER:-/usr/local/sbin/hubinet-ops-host}"
SOURCE_WRAPPER="$SOURCE_DIR/deploy/pve/hubinet-ops-host"
WRAPPER_BACKUP="$WRAPPER_BACKUP_DIR/hubinet-ops-host"
AGENT_VMID=110
agent_backup_complete=false
agent_changes_started=false
wrapper_backup_complete=false
wrapper_changes_started=false

required_source=(
  app
  deploy/pve/hubinet-ops-host
  deploy/agent/backup-0.3.0.sh
  deploy/agent/restore-0.3.0.sh
)
for path in "${required_source[@]}"; do
  [[ -e "$SOURCE_DIR/$path" ]] || {
    echo "Missing source artifact: $path" >&2
    exit 1
  }
done
grep -Fq 'VERSION = "0.3.2"' "$SOURCE_DIR/app/mqtt.py" || {
  echo "Source tree is not Hubinet Ops 0.3.2" >&2
  exit 1
}
[[ -f "$HOST_WRAPPER" ]] || {
  echo "Existing forced-command wrapper is missing: $HOST_WRAPPER" >&2
  exit 1
}

rollback_patch() {
  local rc="${1:-1}" cleanup_failed=false
  trap - ERR INT TERM EXIT
  if [[ "$wrapper_changes_started" == true && "$wrapper_backup_complete" == true ]]; then
    echo "0.3.2 patch failed; restoring the PVE host wrapper" >&2
    if ! cp -a "$WRAPPER_BACKUP" "$HOST_WRAPPER" || ! bash -n "$HOST_WRAPPER"; then
      echo "ROLLBACK INCOMPLETE: PVE host wrapper restore failed" >&2
      cleanup_failed=true
    fi
  fi
  if [[ "$agent_changes_started" == true && "$agent_backup_complete" == true ]]; then
    echo "0.3.2 patch failed; restoring the complete CT110 backup" >&2
    if ! pct exec "$AGENT_VMID" -- bash -s -- "$AGENT_BACKUP" \
      < "$SOURCE_DIR/deploy/agent/restore-0.3.0.sh"; then
      echo "ROLLBACK INCOMPLETE: CT110 agent restore failed" >&2
      cleanup_failed=true
    fi
  elif [[ "$agent_backup_complete" == true ]]; then
    if ! pct exec "$AGENT_VMID" -- systemctl start hubinet-ops; then
      echo "ROLLBACK INCOMPLETE: the unchanged agent could not be restarted" >&2
      cleanup_failed=true
    fi
  fi
  if ! pct exec "$AGENT_VMID" -- rm -f /root/hubinet-ops-0.3.2.tgz 2>/dev/null; then
    echo "Warning: remove /root/hubinet-ops-0.3.2.tgz manually in CT110" >&2
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
python3 -m py_compile "$SOURCE_DIR"/app/*.py
bash -n "$SOURCE_WRAPPER"
tar -C "$SOURCE_DIR" -czf "$ARCHIVE" app

install -d -m 0700 "$WRAPPER_BACKUP_DIR"
cp -a "$HOST_WRAPPER" "$WRAPPER_BACKUP"
wrapper_backup_complete=true

# This helper stops the only SQLite writer before copying the complete database.
pct exec "$AGENT_VMID" -- bash -s -- "$AGENT_BACKUP" \
  < "$SOURCE_DIR/deploy/agent/backup-0.3.0.sh"
agent_backup_complete=true

wrapper_changes_started=true
install -o root -g root -m 0755 "$SOURCE_WRAPPER" "$HOST_WRAPPER"
bash -n "$HOST_WRAPPER"

pct push "$AGENT_VMID" "$ARCHIVE" /root/hubinet-ops-0.3.2.tgz --perms 0600
agent_changes_started=true

pct exec "$AGENT_VMID" -- bash -s <<'REMOTE_INSTALL_AGENT'
set -Eeuo pipefail
staging=/root/hubinet-ops-0.3.2
rm -rf "$staging"
install -d -m 0700 "$staging"
tar -xzf /root/hubinet-ops-0.3.2.tgz -C "$staging"
grep -Fq 'VERSION = "0.3.2"' "$staging/app/mqtt.py"
rm -rf /opt/hubinet-ops/app
cp -a "$staging/app" /opt/hubinet-ops/app
chown -R hubinetops:hubinetops /opt/hubinet-ops/app
runuser -u hubinetops -- /opt/hubinet-ops/.venv/bin/python -m py_compile /opt/hubinet-ops/app/*.py
rm -rf "$staging" /root/hubinet-ops-0.3.2.tgz
systemctl start hubinet-ops
REMOTE_INSTALL_AGENT

for attempt in $(seq 1 30); do
  health_rc=0
  health="$(pct exec "$AGENT_VMID" -- curl -fsS --max-time 3 http://127.0.0.1:8787/health 2>/dev/null)" || health_rc=$?
  if [[ "$health_rc" -eq 0 && "$health" == *'"version":"0.3.2"'* ]]; then
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
    trap - ERR INT TERM EXIT
    rm -f "$ARCHIVE"
    echo "Hubinet Ops 0.3.2 installed transactionally in CT110 and the PVE host wrapper."
    echo "No managed resource action or lifecycle action was executed."
    echo "Backups: CT110:$AGENT_BACKUP PVE:$WRAPPER_BACKUP_DIR"
    exit 0
  fi
  [[ "$attempt" -eq 30 ]] || sleep 2
done

echo "Agent 0.3.2 health validation failed" >&2
rollback_patch 1
