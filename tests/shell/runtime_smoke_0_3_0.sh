#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TMP_ROOT"' EXIT
mkdir -p "$TMP_ROOT/bin"
PVE_ROOT="$TMP_ROOT/etc-pve"
mkdir -p "$PVE_ROOT/nodes/proxmox" "$PVE_ROOT/local"
export PVE_LOCAL_FIXTURE="$PVE_ROOT/local"
export PVE_LOCAL_TARGET="$PVE_ROOT/nodes/proxmox"
cp "$ROOT/deploy/pve/observation-vmids" "$TMP_ROOT/observation-vmids"
cp "$ROOT/deploy/pve/managed-vmids" "$TMP_ROOT/managed-vmids"
cp "$ROOT/deploy/pve/maintenance-vmids" "$TMP_ROOT/maintenance-vmids"
cp "$ROOT/deploy/pve/lifecycle-vmids" "$TMP_ROOT/lifecycle-vmids"
cp "$ROOT/deploy/pve/resource-types" "$TMP_ROOT/resource-types"

sed \
  -e "s|^OBSERVATION_ALLOWLIST=.*|OBSERVATION_ALLOWLIST=\"$TMP_ROOT/observation-vmids\"|" \
  -e "s|^MANAGED_ALLOWLIST=.*|MANAGED_ALLOWLIST=\"$TMP_ROOT/managed-vmids\"|" \
  -e "s|^MAINTENANCE_ALLOWLIST=.*|MAINTENANCE_ALLOWLIST=\"$TMP_ROOT/maintenance-vmids\"|" \
  -e "s|^LIFECYCLE_ALLOWLIST=.*|LIFECYCLE_ALLOWLIST=\"$TMP_ROOT/lifecycle-vmids\"|" \
  -e "s|^RESOURCE_TYPES=.*|RESOURCE_TYPES=\"$TMP_ROOT/resource-types\"|" \
  -e "s|^PVE_LOCAL_PATH=.*|PVE_LOCAL_PATH=\"$PVE_ROOT/local\"|" \
  -e "s|^PVE_NODES_PATH=.*|PVE_NODES_PATH=\"$PVE_ROOT/nodes\"|" \
  "$ROOT/deploy/pve/hubinet-ops-host" > "$TMP_ROOT/hubinet-ops-host"
chmod +x "$TMP_ROOT/hubinet-ops-host"

cat > "$TMP_ROOT/bin/timeout" <<'SH'
#!/usr/bin/env bash
set -Eeuo pipefail
while [[ "$1" == --* ]]; do shift; done
shift
exec "$@"
SH
cat > "$TMP_ROOT/bin/logger" <<'SH'
#!/usr/bin/env bash
exit 0
SH
cat > "$TMP_ROOT/bin/python3" <<'SH'
#!/usr/bin/env bash
exec python "$@"
SH
cat > "$TMP_ROOT/bin/pct" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$SMOKE_LOG"
case "$1" in
  status) printf 'status: running\n' ;;
  exec) printf '{"ok":true,"data":{"lxc_status":"running","health_status":"healthy"}}\n' ;;
esac
SH
cat > "$TMP_ROOT/bin/qm" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$SMOKE_LOG"
if [[ "$1" == status ]]; then
  printf 'status: running\n'
else
  printf '[{"name":"eth0","ip-addresses":[{"ip-address":"192.0.2.10"}]}]\n'
fi
SH
cat > "$TMP_ROOT/bin/pvesh" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$SMOKE_LOG"
printf '{"name":"haos16.0","uptime":10,"cpu":0.1,"cpus":2,"mem":1024,"maxmem":2048,"disk":1,"maxdisk":2,"netin":3,"netout":4}\n'
SH
cat > "$TMP_ROOT/bin/hostname" <<'SH'
#!/usr/bin/env bash
printf 'proxmox.local\n'
SH
cat > "$TMP_ROOT/bin/readlink" <<'SH'
#!/usr/bin/env bash
if [[ "${@: -1}" == "$PVE_LOCAL_FIXTURE" ]]; then
  printf '%s\n' "$PVE_LOCAL_TARGET"
  exit 0
fi
exec /usr/bin/readlink "$@"
SH
chmod +x "$TMP_ROOT/bin/"*
export PATH="$TMP_ROOT/bin:$PATH"
export SMOKE_LOG="$TMP_ROOT/calls.log"

for vmid in $(seq 100 110); do
  result="$(SSH_ORIGINAL_COMMAND="status $vmid" "$TMP_ROOT/hubinet-ops-host")"
  [[ "$result" == *'"ok":true'* ]]
done

qemu="$(SSH_ORIGINAL_COMMAND='inspect 100' "$TMP_ROOT/hubinet-ops-host")"
[[ "$qemu" == *'"qemu_status":"running"'* ]]
[[ "$qemu" == *'"guest_agent_status":"available"'* ]]
grep -Fxq 'get /nodes/proxmox/qemu/100/status/current --output-format json' "$SMOKE_LOG"
if grep -Fq '/nodes/proxmox.local/' "$SMOKE_LOG"; then
  echo 'QEMU inspect used the system hostname instead of the local PVE node' >&2
  exit 1
fi

rm -rf "$PVE_ROOT/local"
if node_error="$(SSH_ORIGINAL_COMMAND='inspect 100' "$TMP_ROOT/hubinet-ops-host")"; then
  echo 'QEMU inspect unexpectedly succeeded without /etc/pve/local' >&2
  exit 1
fi
[[ "$node_error" == *'"ok": false'* ]]
[[ "$node_error" == *'Local PVE node could not be resolved'* ]]
mkdir -p "$PVE_ROOT/not-a-node"
mkdir -p "$PVE_ROOT/local"
export PVE_LOCAL_TARGET="$PVE_ROOT/not-a-node"
if node_error="$(SSH_ORIGINAL_COMMAND='inspect 100' "$TMP_ROOT/hubinet-ops-host")"; then
  echo 'QEMU inspect unexpectedly succeeded with invalid /etc/pve/local target' >&2
  exit 1
fi
[[ "$node_error" == *'"ok": false'* ]]
[[ "$node_error" == *'Local PVE node could not be resolved'* ]]

if SSH_ORIGINAL_COMMAND='start 100' "$TMP_ROOT/hubinet-ops-host" >/dev/null 2>&1; then
  echo "VM100 lifecycle unexpectedly allowed" >&2
  exit 1
fi
if SSH_ORIGINAL_COMMAND='start 110' "$TMP_ROOT/hubinet-ops-host" >/dev/null 2>&1; then
  echo "CT110 lifecycle unexpectedly allowed" >&2
  exit 1
fi
if SSH_ORIGINAL_COMMAND='scan 100' "$TMP_ROOT/hubinet-ops-host" >/dev/null 2>&1; then
  echo "VM100 APT scan unexpectedly allowed" >&2
  exit 1
fi
if SSH_ORIGINAL_COMMAND='update 101' "$TMP_ROOT/hubinet-ops-host" >/dev/null 2>&1; then
  echo "CT101 update unexpectedly allowed" >&2
  exit 1
fi
if SSH_ORIGINAL_COMMAND='inspect 110' "$TMP_ROOT/hubinet-ops-host" >/dev/null 2>&1; then
  echo "CT110 managed inspect unexpectedly allowed" >&2
  exit 1
fi
if SSH_ORIGINAL_COMMAND='start 106 extra' "$TMP_ROOT/hubinet-ops-host" >/dev/null 2>&1; then
  echo "Additional lifecycle argument unexpectedly allowed" >&2
  exit 1
fi

SSH_ORIGINAL_COMMAND='start 106' "$TMP_ROOT/hubinet-ops-host" >/dev/null
SSH_ORIGINAL_COMMAND='scan 101' "$TMP_ROOT/hubinet-ops-host" >/dev/null
grep -Fxq 'start 106' "$SMOKE_LOG"
grep -Fxq 'exec 101 -- /usr/local/sbin/hubinet-maint check-updates' "$SMOKE_LOG"

printf '100 qemu\n' >> "$TMP_ROOT/resource-types"
if SSH_ORIGINAL_COMMAND='status 100' "$TMP_ROOT/hubinet-ops-host" >/dev/null 2>&1; then
  echo "Duplicate type mapping unexpectedly allowed" >&2
  exit 1
fi

echo "0.3.0 wrapper runtime smoke passed"
