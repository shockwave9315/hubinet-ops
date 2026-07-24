#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

[[ ${EUID} -eq 0 || ${HUBINET_OPS_TEST_MODE:-0} == 1 ]] || {
  echo "Run on the PVE administration host as root" >&2
  exit 1
}
restart_core=false
if [[ ${1:-} == "--restart-core" ]]; then
  restart_core=true
  shift
fi
[[ $# -ge 1 && $# -le 2 ]] || {
  echo "Usage: $0 [--restart-core] HA_HOST [HA_SSH_PORT]" >&2
  exit 2
}

HA_HOST="$1"
HA_PORT="${2:-22}"
[[ "$HA_HOST" =~ ^[A-Za-z0-9_.:-]+$ ]] || { echo "Invalid HA_HOST" >&2; exit 1; }
[[ "$HA_PORT" =~ ^[0-9]{1,5}$ ]] && (( HA_PORT >= 1 && HA_PORT <= 65535 )) || {
  echo "Invalid HA SSH port" >&2
  exit 1
}
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP_DIR="/config/backups/hubinet-ops/${STAMP}-before-0.4.1"
SSH_KEY="${HUBINET_OPS_HA_SSH_KEY:-/root/.ssh/id_ed25519}"
SSH_ARGS=(-p "$HA_PORT" -i "$SSH_KEY" "root@$HA_HOST")
backup_complete=false
changes_started=false

python3 "$SOURCE_DIR/scripts/validate_yaml.py"
python3 "$SOURCE_DIR/scripts/generate_ha_dashboard.py" --check
python3 -m py_compile "$SOURCE_DIR/scripts/validate_ha_secrets_0_4_1.py"

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

ssh "${SSH_ARGS[@]}" "python3 - /config/secrets.yaml" \
  < "$SOURCE_DIR/scripts/validate_ha_secrets_0_4_1.py"

ssh "${SSH_ARGS[@]}" '
  set -Eeuo pipefail
  test -s /config/secrets.yaml
  test -s /config/configuration.yaml
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

if [[ "$restart_core" == true ]]; then
  ssh "${SSH_ARGS[@]}" '
    set -Eeuo pipefail
    ha core restart
    for attempt in $(seq 1 60); do
      info="$(ha core info --raw-json 2>/dev/null || true)"
      if grep -Eq "\"state\"[[:space:]]*:[[:space:]]*\"running\"" <<<"$info"; then
        exit 0
      fi
      sleep 2
    done
    echo "Home Assistant Core did not return to running after restart" >&2
    exit 1
  '
fi

trap - ERR INT TERM EXIT
echo "Hubinet Ops 0.4.1 HA files validated. Backup: $HA_HOST:$BACKUP_DIR"
if [[ "$restart_core" == true ]]; then
  echo "Home Assistant Core was restarted after a successful configuration check and is running."
else
  echo "Home Assistant Core was not restarted and the entity registry was not edited."
  echo "The package is valid, but new scripts are unavailable until Core is restarted."
  echo "Restart command: ssh -p $HA_PORT -i $SSH_KEY root@$HA_HOST 'ha core restart'"
fi
