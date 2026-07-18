#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Uruchom na hoście Proxmox jako root." >&2
  exit 1
fi

HA_IP="${1:-192.168.4.168}"
HA_PORT="${2:-22222}"
AGENT_VMID="${3:-110}"
AGENT_IP="${4:-192.168.4.200}"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SSH_ARGS=(-p "$HA_PORT" -i /root/.ssh/id_ed25519 root@"$HA_IP")
STAMP="$(date +%Y%m%d-%H%M%S)"

TOKEN="$(pct exec "$AGENT_VMID" -- bash -lc "sed -n 's/^HUBINET_OPS_API_TOKEN=//p' /etc/hubinet-ops/agent.env")"
[[ -n "$TOKEN" ]] || { echo "Brak tokenu agenta" >&2; exit 1; }

echo "=== BACKUP HA ==="
ssh "${SSH_ARGS[@]}" "
  set -e
  mkdir -p /config/backups/hubinet-ops/$STAMP /config/packages /config/dashboards
  cp -a /config/configuration.yaml /config/backups/hubinet-ops/$STAMP/
  cp -a /config/secrets.yaml /config/backups/hubinet-ops/$STAMP/
  cp -a /config/packages/hubinet_ops.yaml /config/backups/hubinet-ops/$STAMP/ 2>/dev/null || true
  cp -a /config/dashboards/hubinet_ops.yaml /config/backups/hubinet-ops/$STAMP/ 2>/dev/null || true
  cp -a /config/packages/hubinet_ops_smoke*.yaml /config/backups/hubinet-ops/$STAMP/ 2>/dev/null || true
"

echo "=== KOPIOWANIE PACKAGE I DASHBOARDU ==="
scp -P "$HA_PORT" -i /root/.ssh/id_ed25519 \
  "$SOURCE_DIR/home-assistant/packages/hubinet_ops.yaml" \
  root@"$HA_IP":/config/packages/hubinet_ops.yaml
scp -P "$HA_PORT" -i /root/.ssh/id_ed25519 \
  "$SOURCE_DIR/home-assistant/dashboards/hubinet_ops.yaml" \
  root@"$HA_IP":/config/dashboards/hubinet_ops.yaml

ssh "${SSH_ARGS[@]}" 'rm -f /config/packages/hubinet_ops_smoke.yaml /config/packages/hubinet_ops_smoke_v2.yaml'

echo "=== SECRETS 0.2 ==="
cat > /tmp/hubinet-ops-secrets-0.2 <<EOF
hubinet_ops_health_url: "http://${AGENT_IP}:8787/health"
hubinet_ops_ct101_state_url: "http://${AGENT_IP}:8787/api/v1/containers/101/state"
hubinet_ops_ct106_state_url: "http://${AGENT_IP}:8787/api/v1/containers/106/state"
hubinet_ops_approve_url: "http://${AGENT_IP}:8787/api/v1/plans/approve"
hubinet_ops_reject_url: "http://${AGENT_IP}:8787/api/v1/plans/reject"
hubinet_ops_authorization: "Bearer ${TOKEN}"
EOF
scp -P "$HA_PORT" -i /root/.ssh/id_ed25519 \
  /tmp/hubinet-ops-secrets-0.2 root@"$HA_IP":/tmp/hubinet-ops-secrets-0.2
rm -f /tmp/hubinet-ops-secrets-0.2

ssh "${SSH_ARGS[@]}" '
  set -e
  awk "!/^hubinet_ops_health_url:/ &&
       !/^hubinet_ops_ct101_state_url:/ &&
       !/^hubinet_ops_ct106_state_url:/ &&
       !/^hubinet_ops_approve_url:/ &&
       !/^hubinet_ops_reject_url:/ &&
       !/^hubinet_ops_authorization:/" /config/secrets.yaml > /config/secrets.yaml.new
  printf "\n# Hubinet Ops 0.2\n" >> /config/secrets.yaml.new
  cat /tmp/hubinet-ops-secrets-0.2 >> /config/secrets.yaml.new
  mv /config/secrets.yaml.new /config/secrets.yaml
  chmod 600 /config/secrets.yaml
  rm -f /tmp/hubinet-ops-secrets-0.2
'

echo "=== REJESTRACJA DASHBOARDU ==="
if ssh "${SSH_ARGS[@]}" 'grep -Eq "^lovelace:[[:space:]]*$" /config/configuration.yaml'; then
  if ssh "${SSH_ARGS[@]}" 'grep -q "hubinet-ops:" /config/configuration.yaml'; then
    echo "Dashboard Hubinet Ops był już zarejestrowany."
  else
    echo "STOP: masz już sekcję lovelace:, ale bez Hubinet Ops."
    echo "Nie wstrzykuję YAML na pałę. Dodaj ręcznie wpis z home-assistant/configuration.snippet.yaml."
    exit 2
  fi
else
  cat "$SOURCE_DIR/home-assistant/configuration.snippet.yaml" | ssh "${SSH_ARGS[@]}" 'cat >> /config/configuration.yaml'
  echo "Dodano sekcję lovelace z dashboardem Hubinet Ops."
fi

echo "=== HA CORE CHECK ==="
ssh "${SSH_ARGS[@]}" 'ha core check'

echo "=== RESTART HA ==="
ssh "${SSH_ARGS[@]}" 'ha core restart'

echo "=== GOTOWE ==="
echo "Po uruchomieniu dashboard będzie pod /hubinet-ops/overview i w pasku bocznym."
