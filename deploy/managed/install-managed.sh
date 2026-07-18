#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Uruchom na hoście Proxmox jako root." >&2
  exit 1
fi

usage() {
  cat >&2 <<'EOF'
Użycie:
  install-managed.sh VMID SERVICE [HEALTH_URL]
  install-managed.sh VMID --config PLIK_JSON

Przykłady:
  install-managed.sh 101 cloudflared
  install-managed.sh 106 --config deploy/managed/profiles/ct106-weather.json
EOF
  exit 1
}

[[ $# -ge 2 ]] || usage
VMID="$1"
shift
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

[[ "$VMID" =~ ^[1-9][0-9]{1,5}$ ]] || { echo "Nieprawidłowy VMID" >&2; exit 1; }
pct status "$VMID" | grep -q running || { echo "CT$VMID musi być uruchomiony" >&2; exit 1; }

TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT

if [[ "${1:-}" == "--config" ]]; then
  [[ $# -eq 2 ]] || usage
  CONFIG_SOURCE="$2"
  [[ -s "$CONFIG_SOURCE" ]] || { echo "Brak configu: $CONFIG_SOURCE" >&2; exit 1; }
  python3 -m json.tool "$CONFIG_SOURCE" > "$TMP"
else
  [[ $# -le 2 ]] || usage
  SERVICE="$1"
  HEALTH_URL="${2:-}"
  [[ "$SERVICE" =~ ^[A-Za-z0-9_.@-]+$ ]] || { echo "Nieprawidłowa nazwa usługi" >&2; exit 1; }
  python3 - "$SERVICE" "$HEALTH_URL" > "$TMP" <<'PY'
import json, sys
service, url = sys.argv[1], sys.argv[2]
config = {
    "services": [service],
    "health_urls": ([{"url": url, "expected_status": 200}] if url else []),
    "min_free_mb": 1024,
    "ignore_failed_units": [],
    "docker": {"enabled": False},
}
print(json.dumps(config, indent=2))
PY
fi

pct exec "$VMID" -- apt-get update
pct exec "$VMID" -- env DEBIAN_FRONTEND=noninteractive apt-get install -y python3-minimal ca-certificates
pct push "$VMID" "$SOURCE_DIR/hubinet-maint" /usr/local/sbin/hubinet-maint --perms 0755
pct push "$VMID" "$TMP" /etc/hubinet-maint.json --perms 0644

pct exec "$VMID" -- /usr/local/sbin/hubinet-maint inspect
pct exec "$VMID" -- /usr/local/sbin/hubinet-maint healthcheck

echo "CT$VMID przygotowany dla Hubinet Ops 0.2. Najpierw testuj scan/preflight."
