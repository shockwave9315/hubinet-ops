#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

if [[ ${EUID} -ne 0 ]]; then
  echo "Run on the administration host as root." >&2
  exit 1
fi
if [[ $# -lt 3 ]]; then
  echo "Usage: $0 HA_HOST AGENT_BASE_URL AGENT_VMID [HA_SSH_PORT]" >&2
  exit 2
fi

HA_HOST="$1"
AGENT_BASE_URL="${2%/}"
AGENT_VMID="$3"
HA_PORT="${4:-22}"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAMP="$(date +%Y%m%d-%H%M%S)"
SECRETS_FILE="/tmp/hubinet-ops-secrets-${STAMP}"
trap 'rm -f "$SECRETS_FILE"' EXIT
SSH_ARGS=(-p "$HA_PORT" -i /root/.ssh/id_ed25519 "root@$HA_HOST")

[[ "$AGENT_VMID" =~ ^[1-9][0-9]{1,5}$ ]] || {
  echo "Invalid agent VMID" >&2
  exit 1
}
[[ "$AGENT_BASE_URL" =~ ^https?://[^[:space:]]+$ ]] || {
  echo "AGENT_BASE_URL must be an HTTP(S) URL" >&2
  exit 1
}
[[ -f "$SOURCE_DIR/home-assistant/packages/hubinet_ops.yaml" ]] || {
  echo "Missing Home Assistant package in source tree" >&2
  exit 1
}

TOKEN="$(pct exec "$AGENT_VMID" -- sed -n 's/^HUBINET_OPS_API_TOKEN=//p' /etc/hubinet-ops/agent.env)"
[[ ${#TOKEN} -ge 32 ]] || { echo "Agent token not found or too short" >&2; exit 1; }

# Refuse to append a second top-level lovelace key. Existing installations must
# already contain the dashboard entry or be edited deliberately by the operator.
if ! ssh "${SSH_ARGS[@]}" 'grep -Eq "^[[:space:]]{4}hubinet-ops:[[:space:]]*$" /config/configuration.yaml'; then
  if ssh "${SSH_ARGS[@]}" 'grep -Eq "^lovelace:[[:space:]]*$" /config/configuration.yaml'; then
    cat >&2 <<'EOF'
Home Assistant already has a top-level lovelace: section, but no hubinet-ops dashboard entry.
Refusing to append a duplicate lovelace: key. Add the following under lovelace: dashboards: and rerun:

    hubinet-ops:
      mode: yaml
      title: Hubinet Ops
      icon: mdi:server-network
      show_in_sidebar: true
      filename: dashboards/hubinet_ops.yaml
EOF
    exit 1
  fi
fi

ssh "${SSH_ARGS[@]}" "
  set -e
  install -d -m 0700 /config/backups/hubinet-ops/$STAMP
  install -d -m 0755 /config/packages /config/dashboards
  cp -a /config/configuration.yaml /config/secrets.yaml /config/backups/hubinet-ops/$STAMP/
  cp -a /config/packages/hubinet_ops.yaml /config/dashboards/hubinet_ops.yaml \
    /config/backups/hubinet-ops/$STAMP/ 2>/dev/null || true
"

scp -P "$HA_PORT" -i /root/.ssh/id_ed25519 \
  "$SOURCE_DIR/home-assistant/packages/hubinet_ops.yaml" \
  "root@$HA_HOST:/config/packages/hubinet_ops.yaml"
scp -P "$HA_PORT" -i /root/.ssh/id_ed25519 \
  "$SOURCE_DIR/home-assistant/dashboards/hubinet_ops.yaml" \
  "root@$HA_HOST:/config/dashboards/hubinet_ops.yaml"

cat > "$SECRETS_FILE" <<EOF
hubinet_ops_scan_url: "$AGENT_BASE_URL/api/v1/containers/{{ vmid }}/scan"
hubinet_ops_refresh_url: "$AGENT_BASE_URL/api/v1/containers/{{ vmid }}/refresh"
hubinet_ops_retry_healthcheck_url: "$AGENT_BASE_URL/api/v1/containers/{{ vmid }}/retry-healthcheck"
hubinet_ops_rollback_url: "$AGENT_BASE_URL/api/v1/containers/{{ vmid }}/rollback"
hubinet_ops_approve_url: "$AGENT_BASE_URL/api/v1/plans/approve"
hubinet_ops_reject_url: "$AGENT_BASE_URL/api/v1/plans/reject"
hubinet_ops_authorization: "Bearer $TOKEN"
EOF
scp -P "$HA_PORT" -i /root/.ssh/id_ed25519 \
  "$SECRETS_FILE" "root@$HA_HOST:/tmp/hubinet-ops-secrets"

ssh "${SSH_ARGS[@]}" '
  set -e
  awk "!/^hubinet_ops_(scan_url|refresh_url|retry_healthcheck_url|rollback_url|approve_url|reject_url|authorization):/" \
    /config/secrets.yaml > /config/secrets.yaml.new
  printf "\n# Hubinet Ops 0.2.1\n" >> /config/secrets.yaml.new
  cat /tmp/hubinet-ops-secrets >> /config/secrets.yaml.new
  mv /config/secrets.yaml.new /config/secrets.yaml
  chmod 600 /config/secrets.yaml
  rm -f /tmp/hubinet-ops-secrets
  grep -q "^hubinet_ops_webhook_id:" /config/secrets.yaml || {
    echo "Missing existing hubinet_ops_webhook_id secret" >&2
    exit 1
  }
'

if ! ssh "${SSH_ARGS[@]}" 'grep -Eq "^[[:space:]]{4}hubinet-ops:[[:space:]]*$" /config/configuration.yaml'; then
  cat "$SOURCE_DIR/home-assistant/configuration.snippet.yaml" |
    ssh "${SSH_ARGS[@]}" 'cat >> /config/configuration.yaml'
fi

ssh "${SSH_ARGS[@]}" 'ha core check'

echo "HA files validated. Restart Home Assistant once only if the dashboard/package was not already loaded."
echo "Normal MQTT state and discovery updates do not require a Home Assistant restart."
