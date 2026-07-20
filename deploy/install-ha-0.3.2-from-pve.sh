#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

[[ ${EUID} -eq 0 || ${HUBINET_OPS_TEST_MODE:-0} == 1 ]] || {
  echo "Run on the PVE administration host as root" >&2
  exit 1
}
[[ $# -ge 1 && $# -le 2 ]] || {
  echo "Usage: $0 HA_HOST [HA_SSH_PORT]" >&2
  exit 2
}

HA_HOST="$1"
HA_PORT="${2:-22}"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP_DIR="/config/backups/hubinet-ops/${STAMP}-before-0.3.2"
SSH_ARGS=(-p "$HA_PORT" -i /root/.ssh/id_ed25519 "root@$HA_HOST")
backup_complete=false
changes_started=false

[[ "$HA_HOST" =~ ^[A-Za-z0-9_.:-]+$ ]] || { echo "Invalid HA_HOST" >&2; exit 1; }
[[ "$HA_PORT" =~ ^[0-9]{1,5}$ ]] || { echo "Invalid HA SSH port" >&2; exit 1; }
python3 "$SOURCE_DIR/scripts/validate_yaml.py"
python3 "$SOURCE_DIR/scripts/generate_ha_dashboard.py" --check

restore_ha() {
  local rc="${1:-1}" restore_failed=false
  trap - ERR INT TERM EXIT
  if [[ "$changes_started" == true && "$backup_complete" == true ]]; then
    if ! ssh "${SSH_ARGS[@]}" "set -e
      cp -a '$BACKUP_DIR/secrets.yaml' /config/secrets.yaml
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
      echo "ROLLBACK INCOMPLETE: Home Assistant files require manual restoration from $BACKUP_DIR" >&2
      restore_failed=true
    else
      echo "Home Assistant files restored after installer failure" >&2
    fi
  else
    ssh "${SSH_ARGS[@]}" \
      'rm -f /config/packages/hubinet_ops.yaml.new /config/dashboards/hubinet_ops.yaml.new' \
      >/dev/null 2>&1 || echo "Warning: remove staged Hubinet Ops HA files manually" >&2
  fi
  [[ "$restore_failed" == false ]] || rc=1
  exit "$rc"
}
trap 'restore_ha $?' ERR
trap 'restore_ha 130' INT
trap 'restore_ha 143' TERM

ssh "${SSH_ARGS[@]}" '
  set -e
  for key in \
    hubinet_ops_webhook_id hubinet_ops_notify_service hubinet_ops_authorization \
    hubinet_ops_scan_url hubinet_ops_refresh_url hubinet_ops_retry_healthcheck_url \
    hubinet_ops_rollback_url hubinet_ops_approve_url hubinet_ops_reject_url \
    hubinet_ops_start_url hubinet_ops_shutdown_url hubinet_ops_reboot_url; do
    grep -q "^$key:" /config/secrets.yaml || {
      echo "Missing required existing secret: $key" >&2
      exit 1
    }
  done
  grep -Rqs "lovelace-mushroom/mushroom.js" \
    /config/.storage/lovelace_resources /config/configuration.yaml 2>/dev/null
'

ssh "${SSH_ARGS[@]}" "set -e
  install -d -m 0700 '$BACKUP_DIR'
  install -d -m 0755 /config/packages /config/dashboards
  cp -a /config/secrets.yaml '$BACKUP_DIR/secrets.yaml'
  [[ ! -f /config/packages/hubinet_ops.yaml ]] || cp -a /config/packages/hubinet_ops.yaml '$BACKUP_DIR/hubinet_ops.package.yaml'
  [[ ! -f /config/dashboards/hubinet_ops.yaml ]] || cp -a /config/dashboards/hubinet_ops.yaml '$BACKUP_DIR/hubinet_ops.dashboard.yaml'
  touch '$BACKUP_DIR/backup.complete'"
backup_complete=true

scp -P "$HA_PORT" -i /root/.ssh/id_ed25519 \
  "$SOURCE_DIR/home-assistant/packages/hubinet_ops.yaml" \
  "root@$HA_HOST:/config/packages/hubinet_ops.yaml.new"
scp -P "$HA_PORT" -i /root/.ssh/id_ed25519 \
  "$SOURCE_DIR/home-assistant/dashboards/hubinet_ops.yaml" \
  "root@$HA_HOST:/config/dashboards/hubinet_ops.yaml.new"

changes_started=true
ssh "${SSH_ARGS[@]}" '
  set -e
  install -m 0644 /config/packages/hubinet_ops.yaml.new /config/packages/hubinet_ops.yaml
  install -m 0644 /config/dashboards/hubinet_ops.yaml.new /config/dashboards/hubinet_ops.yaml
  rm -f /config/packages/hubinet_ops.yaml.new /config/dashboards/hubinet_ops.yaml.new
  chmod 600 /config/secrets.yaml
  ha core check
'

trap - ERR INT TERM EXIT
echo "Hubinet Ops 0.3.2 HA files validated. Backup: $HA_HOST:$BACKUP_DIR"
echo "Home Assistant was not restarted automatically."
