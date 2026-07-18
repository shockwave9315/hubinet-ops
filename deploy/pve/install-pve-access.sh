#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

if [[ ${EUID} -ne 0 ]]; then
  echo "Uruchom na hoście Proxmox jako root." >&2
  exit 1
fi
if [[ $# -lt 2 ]]; then
  echo "Użycie: HUBINET_OPS_AGENT_IP=192.168.x.x $0 'PUBLICZNY_KLUCZ_SSH' VMID [VMID...]" >&2
  exit 1
fi

PUBLIC_KEY="$1"
shift
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENT_IP="${HUBINET_OPS_AGENT_IP:-}"

[[ "$PUBLIC_KEY" != *$'\n'* && "$PUBLIC_KEY" != *$'\r'* ]] || {
  echo "Klucz publiczny nie może zawierać nowych linii" >&2
  exit 1
}
read -r KEY_TYPE KEY_BLOB KEY_COMMENT EXTRA <<<"$PUBLIC_KEY"
[[ "$KEY_TYPE" == "ssh-ed25519" ]] || { echo "Oczekuję klucza ssh-ed25519" >&2; exit 1; }
[[ "$KEY_BLOB" =~ ^[A-Za-z0-9+/]+={0,2}$ ]] || { echo "Nieprawidłowy format klucza" >&2; exit 1; }
[[ -z "${EXTRA:-}" ]] || { echo "Komentarz klucza nie może zawierać spacji" >&2; exit 1; }
if [[ -n "$AGENT_IP" ]]; then
  [[ "$AGENT_IP" =~ ^[0-9A-Fa-f:.]+(/[0-9]{1,3})?$ ]] || {
    echo "Nieprawidłowy HUBINET_OPS_AGENT_IP" >&2
    exit 1
  }
fi

TMP_ALLOWLIST="$(mktemp)"
TMP_AUTHORIZED="$(mktemp)"
trap 'rm -f "$TMP_ALLOWLIST" "$TMP_AUTHORIZED"' EXIT
for vmid in "$@"; do
  [[ "$vmid" =~ ^[1-9][0-9]{1,5}$ ]] || {
    echo "Nieprawidłowy VMID: $vmid" >&2
    exit 1
  }
  printf '%s\n' "$vmid" >> "$TMP_ALLOWLIST"
done
sort -nu -o "$TMP_ALLOWLIST" "$TMP_ALLOWLIST"
[[ -s "$TMP_ALLOWLIST" ]] || { echo "Allowlista nie może być pusta" >&2; exit 1; }

install -d -m 0700 /root/.ssh
install -d -m 0750 /etc/hubinet-ops
install -m 0755 "$SOURCE_DIR/hubinet-ops-host" /usr/local/sbin/hubinet-ops-host
install -m 0640 "$TMP_ALLOWLIST" /etc/hubinet-ops/allowed-vmids

AUTHORIZED_KEYS=/root/.ssh/authorized_keys
touch "$AUTHORIZED_KEYS"
chmod 0600 "$AUTHORIZED_KEYS"
# Remove every pre-existing form of the same key, including an accidentally
# unrestricted entry, then append exactly one forced-command line.
awk -v blob="$KEY_BLOB" 'index($0, blob) == 0' "$AUTHORIZED_KEYS" > "$TMP_AUTHORIZED"
OPTIONS='restrict,command="/usr/local/sbin/hubinet-ops-host"'
if [[ -n "$AGENT_IP" ]]; then
  OPTIONS="from=\"$AGENT_IP\",$OPTIONS"
fi
printf '%s %s %s' "$OPTIONS" "$KEY_TYPE" "$KEY_BLOB" >> "$TMP_AUTHORIZED"
if [[ -n "${KEY_COMMENT:-}" ]]; then
  printf ' %s' "$KEY_COMMENT" >> "$TMP_AUTHORIZED"
fi
printf '\n' >> "$TMP_AUTHORIZED"
install -m 0600 "$TMP_AUTHORIZED" "$AUTHORIZED_KEYS"

echo "Gotowe. Agent może wykonywać tylko wrapper, a wrapper tylko akcje z allowlisty:"
cat /etc/hubinet-ops/allowed-vmids
