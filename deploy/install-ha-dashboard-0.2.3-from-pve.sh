#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

if [[ ${EUID} -ne 0 ]]; then
  echo "Run on the administration host as root." >&2
  exit 1
fi
if [[ $# -lt 1 ]]; then
  echo "Usage: $0 HA_HOST [HA_SSH_PORT]" >&2
  exit 2
fi

HA_HOST="$1"
HA_PORT="${2:-22}"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DASHBOARD="$SOURCE_DIR/home-assistant/dashboards/hubinet_ops.yaml"
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP_DIR="/config/backups/hubinet-ops/$STAMP"
SSH_ARGS=(-p "$HA_PORT" -i /root/.ssh/id_ed25519 "root@$HA_HOST")

[[ -f "$DASHBOARD" ]] || {
  echo "Missing Home Assistant dashboard in source tree" >&2
  exit 1
}
grep -Fq "attribute_payload" "$DASHBOARD" || {
  echo "Dashboard does not contain the 0.2.3 truncation metadata view" >&2
  exit 1
}
[[ "$(grep -Fc "limit atrybutów 10 KB" "$DASHBOARD")" -eq 2 ]] || {
  echo "Dashboard must expose truncation for exactly CT101 and CT106" >&2
  exit 1
}

python "$SOURCE_DIR/scripts/validate_yaml.py"

ssh "${SSH_ARGS[@]}" "
  set -e
  install -d -m 0700 '$BACKUP_DIR'
  if [[ -f /config/dashboards/hubinet_ops.yaml ]]; then
    cp -a /config/dashboards/hubinet_ops.yaml '$BACKUP_DIR/hubinet_ops.yaml'
  fi
"

scp -P "$HA_PORT" -i /root/.ssh/id_ed25519 \
  "$DASHBOARD" \
  "root@$HA_HOST:/config/dashboards/hubinet_ops.yaml.new"

ssh "${SSH_ARGS[@]}" "
  set -e
  install -m 0644 /config/dashboards/hubinet_ops.yaml.new /config/dashboards/hubinet_ops.yaml
  rm -f /config/dashboards/hubinet_ops.yaml.new
  grep -Fq 'attribute_payload' /config/dashboards/hubinet_ops.yaml
  [[ \"\$(grep -Fc 'limit atrybutów 10 KB' /config/dashboards/hubinet_ops.yaml)\" -eq 2 ]]
  ha core check
"

echo "Hubinet Ops 0.2.3 dashboard installed. Backup: $HA_HOST:$BACKUP_DIR"
echo "No Home Assistant restart is required; reload the dashboard in the browser/app."
