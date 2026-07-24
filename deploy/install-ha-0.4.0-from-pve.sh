#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

[[ ${EUID} -eq 0 || ${HUBINET_OPS_TEST_MODE:-0} == 1 ]] || {
  echo "Run on the PVE administration host as root" >&2
  exit 1
}
[[ $# -ge 1 && $# -le 2 ]] || { echo "Usage: $0 HA_HOST [HA_SSH_PORT]" >&2; exit 2; }

HA_HOST="$1"
HA_PORT="${2:-22}"
[[ "$HA_HOST" =~ ^[A-Za-z0-9_.:-]+$ ]] || { echo "Invalid HA_HOST" >&2; exit 1; }
[[ "$HA_PORT" =~ ^[0-9]{1,5}$ ]] && (( HA_PORT >= 1 && HA_PORT <= 65535 )) || {
  echo "Invalid HA SSH port" >&2
  exit 1
}
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP_DIR="/config/backups/hubinet-ops/${STAMP}-before-0.4.0"
SSH_KEY="${HUBINET_OPS_HA_SSH_KEY:-/root/.ssh/id_ed25519}"
SSH_ARGS=(-p "$HA_PORT" -i "$SSH_KEY" "root@$HA_HOST")
backup_complete=false
changes_started=false

python3 "$SOURCE_DIR/scripts/validate_yaml.py"
python3 "$SOURCE_DIR/scripts/generate_ha_dashboard.py" --check

rollback_ha() {
  local rc="${1:-1}" failed=false
  trap - ERR INT TERM EXIT
  if [[ "$changes_started" == true && "$backup_complete" == true ]]; then
    if ! ssh "${SSH_ARGS[@]}" "set -Eeuo pipefail
      cp -a '$BACKUP_DIR/secrets.yaml' /config/secrets.yaml
      cp -a '$BACKUP_DIR/configuration.yaml' /config/configuration.yaml
      if [[ -f '$BACKUP_DIR/hubinet_ops.package.yaml' ]]; then
        cp -a '$BACKUP_DIR/hubinet_ops.package.yaml' /config/packages/hubinet_ops.yaml
      else
        rm -f /config/packages/hubinet_ops.yaml
      fi
      if [[ -f '$BACKUP_DIR/hubinet_ops.dashboard.yaml' ]]; then
        cp -a '$BACKUP_DIR/hubinet_ops.dashboard.yaml' /config/dashboards/hubinet_ops.yaml
      else
        rm -f /config/dashboards/hubinet_ops.yaml
      fi
      rm -f /config/packages/hubinet_ops.yaml.new /config/dashboards/hubinet_ops.yaml.new
      ha core check"; then
      echo "ROLLBACK INCOMPLETE: restore HA files from $HA_HOST:$BACKUP_DIR" >&2
      failed=true
    fi
  else
    ssh "${SSH_ARGS[@]}" 'rm -f /config/packages/hubinet_ops.yaml.new /config/dashboards/hubinet_ops.yaml.new' >/dev/null 2>&1 || true
  fi
  [[ "$failed" == false ]] || rc=1
  exit "$rc"
}
trap 'rollback_ha $?' ERR
trap 'rollback_ha 130' INT
trap 'rollback_ha 143' TERM

ssh "${SSH_ARGS[@]}" '
  set -Eeuo pipefail
  test -s /config/secrets.yaml
  test -s /config/configuration.yaml
  for key in \
    hubinet_ops_webhook_id hubinet_ops_notify_service hubinet_ops_authorization \
    hubinet_ops_scan_url hubinet_ops_refresh_url hubinet_ops_retry_healthcheck_url \
    hubinet_ops_rollback_url hubinet_ops_approve_url hubinet_ops_reject_url \
    hubinet_ops_start_url hubinet_ops_shutdown_url hubinet_ops_reboot_url \
    hubinet_ops_force_stop_url hubinet_ops_snapshot_create_url \
    hubinet_ops_snapshot_restore_url hubinet_ops_snapshot_delete_url \
    hubinet_ops_host_authorization hubinet_ops_host_start_url \
    hubinet_ops_host_recovery_authorization \
    hubinet_ops_host_offline_snapshot_restore_url \
    hubinet_ops_host_offline_force_stop_url; do
    grep -q "^$key:" /config/secrets.yaml || { echo "Missing required secret: $key" >&2; exit 1; }
  done
  grep -Rqs "lovelace-mushroom/mushroom.js" /config/.storage/lovelace_resources /config/configuration.yaml 2>/dev/null
'

ssh "${SSH_ARGS[@]}" "set -Eeuo pipefail
  install -d -m 0700 '$BACKUP_DIR'
  install -d -m 0755 /config/packages /config/dashboards
  cp -a /config/secrets.yaml '$BACKUP_DIR/secrets.yaml'
  cp -a /config/configuration.yaml '$BACKUP_DIR/configuration.yaml'
  [[ ! -f /config/packages/hubinet_ops.yaml ]] || cp -a /config/packages/hubinet_ops.yaml '$BACKUP_DIR/hubinet_ops.package.yaml'
  [[ ! -f /config/dashboards/hubinet_ops.yaml ]] || cp -a /config/dashboards/hubinet_ops.yaml '$BACKUP_DIR/hubinet_ops.dashboard.yaml'
  : > '$BACKUP_DIR/backup.complete'"
backup_complete=true

scp -P "$HA_PORT" -i "$SSH_KEY" "$SOURCE_DIR/home-assistant/packages/hubinet_ops.yaml" \
  "root@$HA_HOST:/config/packages/hubinet_ops.yaml.new"
scp -P "$HA_PORT" -i "$SSH_KEY" "$SOURCE_DIR/home-assistant/dashboards/hubinet_ops.yaml" \
  "root@$HA_HOST:/config/dashboards/hubinet_ops.yaml.new"

changes_started=true
ssh "${SSH_ARGS[@]}" '
  set -Eeuo pipefail
  install -m 0644 /config/packages/hubinet_ops.yaml.new /config/packages/hubinet_ops.yaml
  install -m 0644 /config/dashboards/hubinet_ops.yaml.new /config/dashboards/hubinet_ops.yaml
  rm -f /config/packages/hubinet_ops.yaml.new /config/dashboards/hubinet_ops.yaml.new
  chmod 600 /config/secrets.yaml
  ha core check
'

trap - ERR INT TERM EXIT
echo "Hubinet Ops 0.4.0 HA files validated. Backup: $HA_HOST:$BACKUP_DIR"
echo "Home Assistant was not restarted and the entity registry was not edited."
