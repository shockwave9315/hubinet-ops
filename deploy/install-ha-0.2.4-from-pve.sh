#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

if [[ ${EUID} -ne 0 ]]; then
  echo "Run on the administration host as root." >&2
  exit 1
fi
if [[ $# -lt 2 ]]; then
  echo "Usage: $0 HA_HOST AGENT_BASE_URL [HA_SSH_PORT]" >&2
  exit 2
fi

HA_HOST="$1"
AGENT_BASE_URL="${2%/}"
HA_PORT="${3:-22}"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP_DIR="/config/backups/hubinet-ops/${STAMP}-before-0.2.4"
URL_FILE="/tmp/hubinet-ops-0.2.4-urls-${STAMP}"
SSH_ARGS=(-p "$HA_PORT" -i /root/.ssh/id_ed25519 "root@$HA_HOST")
COMPLETE=0

[[ "$AGENT_BASE_URL" =~ ^https?://[^[:space:]]+$ ]] || {
  echo "AGENT_BASE_URL must be an HTTP(S) URL" >&2
  exit 1
}
python3 "$SOURCE_DIR/scripts/validate_yaml.py"

restore_ha() {
  local rc="${1:-$?}"
  [[ "$rc" -ne 0 ]] || rc=1
  trap - ERR INT TERM
  if [[ $COMPLETE -eq 1 ]]; then
    rm -f "$URL_FILE"
    return 0
  fi
  ssh "${SSH_ARGS[@]}" "set -e
    if [[ -f '$BACKUP_DIR/backup.complete' ]]; then
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
    fi" || true
  rm -f "$URL_FILE"
  echo "Home Assistant files restored after installer failure." >&2
  exit "$rc"
}
trap 'restore_ha $?' ERR
trap 'restore_ha 130' INT
trap 'restore_ha 143' TERM
trap 'rm -f "$URL_FILE"' EXIT

ssh "${SSH_ARGS[@]}" '
  set -e
  for key in hubinet_ops_webhook_id hubinet_ops_notify_service hubinet_ops_authorization; do
    grep -q "^$key:" /config/secrets.yaml || {
      echo "Missing required existing secret: $key" >&2
      exit 1
    }
  done
  grep -Rqs "lovelace-mushroom/mushroom.js" +    /config/.storage/lovelace_resources /config/configuration.yaml 2>/dev/null
'

ssh "${SSH_ARGS[@]}" "set -e
  install -d -m 0700 '$BACKUP_DIR'
  install -d -m 0755 /config/packages /config/dashboards
  cp -a /config/secrets.yaml '$BACKUP_DIR/secrets.yaml'
  if [[ -f /config/packages/hubinet_ops.yaml ]]; then
    cp -a /config/packages/hubinet_ops.yaml '$BACKUP_DIR/hubinet_ops.package.yaml'
  fi
  if [[ -f /config/dashboards/hubinet_ops.yaml ]]; then
    cp -a /config/dashboards/hubinet_ops.yaml '$BACKUP_DIR/hubinet_ops.dashboard.yaml'
  fi
  touch '$BACKUP_DIR/backup.complete'"

scp -P "$HA_PORT" -i /root/.ssh/id_ed25519 +  "$SOURCE_DIR/home-assistant/packages/hubinet_ops.yaml" +  "root@$HA_HOST:/config/packages/hubinet_ops.yaml.new"
scp -P "$HA_PORT" -i /root/.ssh/id_ed25519 +  "$SOURCE_DIR/home-assistant/dashboards/hubinet_ops.yaml" +  "root@$HA_HOST:/config/dashboards/hubinet_ops.yaml.new"

cat > "$URL_FILE" <<EOF
hubinet_ops_start_url: "$AGENT_BASE_URL/api/v1/containers/{{ vmid }}/start"
hubinet_ops_shutdown_url: "$AGENT_BASE_URL/api/v1/containers/{{ vmid }}/shutdown"
hubinet_ops_reboot_url: "$AGENT_BASE_URL/api/v1/containers/{{ vmid }}/reboot"
EOF
scp -P "$HA_PORT" -i /root/.ssh/id_ed25519 +  "$URL_FILE" "root@$HA_HOST:/tmp/hubinet-ops-0.2.4-urls"

ssh "${SSH_ARGS[@]}" '
  set -e
  install -m 0644 /config/packages/hubinet_ops.yaml.new /config/packages/hubinet_ops.yaml
  install -m 0644 /config/dashboards/hubinet_ops.yaml.new /config/dashboards/hubinet_ops.yaml
  rm -f /config/packages/hubinet_ops.yaml.new /config/dashboards/hubinet_ops.yaml.new
  while IFS= read -r line; do
    key="${line%%:*}"
    if ! grep -q "^$key:" /config/secrets.yaml; then
      printf "%s\n" "$line" >> /config/secrets.yaml
    fi
  done < /tmp/hubinet-ops-0.2.4-urls
  chmod 600 /config/secrets.yaml
  rm -f /tmp/hubinet-ops-0.2.4-urls
  ha core check
'

COMPLETE=1
trap - ERR INT TERM
rm -f "$URL_FILE"
echo "Hubinet Ops 0.2.4 HA files validated. Backup: $HA_HOST:$BACKUP_DIR"
echo "Home Assistant was not restarted automatically."
