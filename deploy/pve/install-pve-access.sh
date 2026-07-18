#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Uruchom na hoście Proxmox jako root." >&2
  exit 1
fi
if [[ $# -lt 2 ]]; then
  echo "Użycie: $0 'PUBLICZNY_KLUCZ_SSH' VMID [VMID...]" >&2
  exit 1
fi

PUBLIC_KEY="$1"
shift
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

[[ "$PUBLIC_KEY" == ssh-ed25519\ * ]] || { echo "Oczekuję klucza ssh-ed25519" >&2; exit 1; }

install -d -m 0700 /root/.ssh
install -d -m 0750 /etc/hubinet-ops
install -m 0755 "$SOURCE_DIR/hubinet-ops-host" /usr/local/sbin/hubinet-ops-host

: > /etc/hubinet-ops/allowed-vmids
for vmid in "$@"; do
  [[ "$vmid" =~ ^[1-9][0-9]{1,5}$ ]] || { echo "Nieprawidłowy VMID: $vmid" >&2; exit 1; }
  echo "$vmid" >> /etc/hubinet-ops/allowed-vmids
done
sort -nu -o /etc/hubinet-ops/allowed-vmids /etc/hubinet-ops/allowed-vmids
chmod 0640 /etc/hubinet-ops/allowed-vmids

AUTHORIZED_LINE="restrict,command=\"/usr/local/sbin/hubinet-ops-host\" ${PUBLIC_KEY}"
touch /root/.ssh/authorized_keys
chmod 0600 /root/.ssh/authorized_keys
if ! grep -Fq "${PUBLIC_KEY}" /root/.ssh/authorized_keys; then
  echo "$AUTHORIZED_LINE" >> /root/.ssh/authorized_keys
fi

echo "Gotowe. Agent może wykonywać tylko wrapper, a wrapper tylko akcje z allowlisty:"
cat /etc/hubinet-ops/allowed-vmids
