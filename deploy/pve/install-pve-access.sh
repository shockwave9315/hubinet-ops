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

for required in observation-vmids managed-vmids maintenance-vmids lifecycle-vmids \
  host-control-vmids snapshot-create-vmids snapshot-restore-vmids \
  snapshot-delete-vmids resource-types ct110-system-update-vmids \
  ct110-system-automatic-rollback-vmids hubinet_ops_host_control.py \
  hubinet_ops_hostd.py hubinet_ops_release.py hubinet_ops_ct110_system.py \
  hubinet-ops-self-update hubinet-ops-ct110-system-update; do
  [[ -f "$SOURCE_DIR/$required" ]] || {
    echo "Missing $required in the installation package" >&2
    exit 1
  }
done
cmp -s "$SOURCE_DIR/managed-vmids" "$SOURCE_DIR/maintenance-vmids" || {
  echo "Managed and maintenance allowlists must match for 0.4.3" >&2
  exit 1
}
cmp -s "$SOURCE_DIR/lifecycle-vmids" "$SOURCE_DIR/snapshot-restore-vmids" || {
  echo "Lifecycle and snapshot restore allowlists must match for 0.4.3" >&2
  exit 1
}
for policy in snapshot-create-vmids snapshot-delete-vmids; do
  cmp -s "$SOURCE_DIR/host-control-vmids" "$SOURCE_DIR/$policy" || {
    echo "$policy must match host-control-vmids for 0.4.3" >&2
    exit 1
  }
done
python3 -m py_compile "$SOURCE_DIR/hubinet_ops_host_control.py"
python3 -m py_compile "$SOURCE_DIR/hubinet_ops_hostd.py" \
  "$SOURCE_DIR/hubinet_ops_release.py" \
  "$SOURCE_DIR/hubinet_ops_ct110_system.py"

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
if ! cmp -s "$TMP_ALLOWLIST" "$SOURCE_DIR/observation-vmids"; then
  echo "Installation VMIDs must match the versioned observation allowlist" >&2
  exit 1
fi
[[ -s "$TMP_ALLOWLIST" ]] || { echo "Allowlista nie może być pusta" >&2; exit 1; }

install -d -m 0700 /root/.ssh
install -d -m 0750 /etc/hubinet-ops
install -d -m 0755 /usr/local/lib/hubinet-ops
install -m 0755 "$SOURCE_DIR/hubinet_ops_host_control.py" /usr/local/lib/hubinet-ops/hubinet_ops_host_control.py
install -m 0755 "$SOURCE_DIR/hubinet-ops-host" /usr/local/sbin/hubinet-ops-host
install -m 0755 "$SOURCE_DIR/hubinet-ops-self-update" /usr/local/sbin/hubinet-ops-self-update
install -m 0755 "$SOURCE_DIR/hubinet-ops-ct110-system-update" /usr/local/sbin/hubinet-ops-ct110-system-update
install -m 0644 "$SOURCE_DIR/hubinet_ops_hostd.py" /usr/local/lib/hubinet-ops/hubinet_ops_hostd.py
install -m 0644 "$SOURCE_DIR/hubinet_ops_release.py" /usr/local/lib/hubinet-ops/hubinet_ops_release.py
install -m 0644 "$SOURCE_DIR/hubinet_ops_ct110_system.py" /usr/local/lib/hubinet-ops/hubinet_ops_ct110_system.py
# Deprecated rollback copy for 0.2.x; the wrapper uses observation-vmids.
install -m 0640 "$TMP_ALLOWLIST" /etc/hubinet-ops/allowed-vmids
install -m 0640 "$SOURCE_DIR/observation-vmids" /etc/hubinet-ops/observation-vmids
install -m 0640 "$SOURCE_DIR/managed-vmids" /etc/hubinet-ops/managed-vmids
install -m 0640 "$SOURCE_DIR/maintenance-vmids" /etc/hubinet-ops/maintenance-vmids
install -m 0640 "$SOURCE_DIR/lifecycle-vmids" /etc/hubinet-ops/lifecycle-vmids
install -m 0640 "$SOURCE_DIR/host-control-vmids" /etc/hubinet-ops/host-control-vmids
install -m 0640 "$SOURCE_DIR/snapshot-create-vmids" /etc/hubinet-ops/snapshot-create-vmids
install -m 0640 "$SOURCE_DIR/snapshot-restore-vmids" /etc/hubinet-ops/snapshot-restore-vmids
install -m 0640 "$SOURCE_DIR/snapshot-delete-vmids" /etc/hubinet-ops/snapshot-delete-vmids
install -m 0640 "$SOURCE_DIR/resource-types" /etc/hubinet-ops/resource-types
install -m 0640 "$SOURCE_DIR/ct110-system-update-vmids" /etc/hubinet-ops/ct110-system-update-vmids
install -m 0640 "$SOURCE_DIR/ct110-system-automatic-rollback-vmids" /etc/hubinet-ops/ct110-system-automatic-rollback-vmids

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
cat /etc/hubinet-ops/observation-vmids
echo "Akcje lifecycle są dodatkowo ograniczone do:"
cat /etc/hubinet-ops/lifecycle-vmids
