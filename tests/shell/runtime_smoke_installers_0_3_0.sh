#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TMP_ROOT="$(mktemp -d)"
export TMP_ROOT
FAKE_BIN="$TMP_ROOT/bin"
LOG_DIR="$TMP_ROOT/logs"
CT_ROOT="$TMP_ROOT/stopped-ct"
mkdir -p "$FAKE_BIN" "$LOG_DIR" "$CT_ROOT/usr/local/sbin" "$CT_ROOT/etc"
trap 'rm -rf "$TMP_ROOT"' EXIT
export HUBINET_OPS_TEST_MODE=1
export HUBINET_OPS_TEST_LOG_DIR="$LOG_DIR"
export HUBINET_OPS_HOST_BACKUP_BASE="$TMP_ROOT/backups"
export HUBINET_OPS_FAKE_CT_ROOT="$CT_ROOT"
export PATH="$FAKE_BIN:$PATH"

for script in \
  "$ROOT/deploy/install-ha-0.3.0-from-pve.sh" \
  "$ROOT/deploy/upgrade-0.3.0-from-pve.sh" \
  "$ROOT/deploy/managed/install-managed.sh" \
  "$ROOT/deploy/agent/backup-0.3.0.sh" \
  "$ROOT/deploy/agent/restore-0.3.0.sh"; do
  if grep -nE '(^|[[:space:]])\+([[:space:]]|$)' "$script"; then
    echo "Standalone diff marker found in $script" >&2
    exit 1
  fi
done

write_stub() {
  local name="$1"
  shift
  printf '%s\n' '#!/usr/bin/env bash' 'set -Eeuo pipefail' "$@" > "$FAKE_BIN/$name"
  chmod +x "$FAKE_BIN/$name"
}

write_stub date 'printf "%s\n" "${HUBINET_OPS_FAKE_STAMP:-20260719-120000}"'
write_stub python3 '
printf "<%s>" "$@" >> "$HUBINET_OPS_TEST_LOG_DIR/python3.args"
printf "\n" >> "$HUBINET_OPS_TEST_LOG_DIR/python3.args"
if [[ ${1:-} == -c ]]; then printf "%s\n" "${HUBINET_OPS_FAKE_RESOURCE_COUNT:-11}"; fi
if [[ ${1:-} == - ]]; then cat >/dev/null; fi
'
write_stub ssh '
printf "<%s>" "$@" >> "$HUBINET_OPS_TEST_LOG_DIR/ssh.args"
printf "\n" >> "$HUBINET_OPS_TEST_LOG_DIR/ssh.args"
'
write_stub scp '
printf "<%s>" "$@" >> "$HUBINET_OPS_TEST_LOG_DIR/scp.args"
printf "\n" >> "$HUBINET_OPS_TEST_LOG_DIR/scp.args"
'
write_stub install '
printf "<%s>" "$@" >> "$HUBINET_OPS_TEST_LOG_DIR/install.args"
printf "\n" >> "$HUBINET_OPS_TEST_LOG_DIR/install.args"
if [[ " $* " == *" -d "* ]]; then mkdir -p "${@: -1}"; fi
'
write_stub cp '
if [[ -n ${HUBINET_OPS_FAKE_MOUNT_SIGNAL:-} && "${2:-}" == "$HUBINET_OPS_FAKE_CT_ROOT"/* ]]; then
  printf '%s\n' "$HUBINET_OPS_FAKE_MOUNT_SIGNAL" >> "$HUBINET_OPS_TEST_LOG_DIR/mount-signals.log"
  kill -"$HUBINET_OPS_FAKE_MOUNT_SIGNAL" "$PPID"
  sleep 1
fi
exec /usr/bin/cp "$@"
'
write_stub touch 'exec /usr/bin/touch "$@"'
write_stub rm '
printf "<%s>" "$@" >> "$HUBINET_OPS_TEST_LOG_DIR/rm.args"
printf "\n" >> "$HUBINET_OPS_TEST_LOG_DIR/rm.args"
for value in "$@"; do
  if [[ "$value" == "$TMP_ROOT"/* || "$value" == /tmp/hubinet-ops-0.3.0-* ]]; then
    exec /usr/bin/rm "$@"
  fi
done
exit 0
'
write_stub qm '
printf "<%s>" "$@" >> "$HUBINET_OPS_TEST_LOG_DIR/qm.args"
printf "\n" >> "$HUBINET_OPS_TEST_LOG_DIR/qm.args"
[[ ${1:-} == status && ${2:-} == 100 ]]
printf "status: running\n"
'
write_stub pct '
printf "<%s>" "$@" >> "$HUBINET_OPS_TEST_LOG_DIR/pct.args"
printf "\n" >> "$HUBINET_OPS_TEST_LOG_DIR/pct.args"
case "${1:-}" in
  status)
    if [[ ${2:-} == "${HUBINET_OPS_FAKE_STOPPED_VMID:-none}" ]]; then
      printf "status: stopped\n"
    else
      printf "status: running\n"
    fi
    ;;
  mount)
    if [[ ${HUBINET_OPS_FAKE_MOUNT_OUTPUT:-valid} == malformed ]]; then
      printf "mounted without a parseable path\n"
    else
      printf "mounted on \047%s\047\n" "$HUBINET_OPS_FAKE_CT_ROOT"
    fi
    ;;
  unmount) exit 0 ;;
  pull)
    if [[ -n ${HUBINET_OPS_FAKE_PULL_FAILURE:-} && "$3" == "$HUBINET_OPS_FAKE_PULL_FAILURE" ]]; then
      exit 1
    fi
    printf "existing-%s\n" "${3##*/}" > "$4"
    ;;
  push) exit 0 ;;
  exec)
    if [[ " $* " == *" bash -s "* ]]; then
      count_file="$HUBINET_OPS_TEST_LOG_DIR/bash-s-count"
      count=0
      [[ ! -f "$count_file" ]] || count="$(cat "$count_file")"
      count=$((count + 1))
      printf "%s" "$count" > "$count_file"
      cat > "$HUBINET_OPS_TEST_LOG_DIR/bash-s-$count.body"
      if [[ $count -eq 3 ]]; then
        printf "["
        for vmid in $(seq 100 110); do
          [[ $vmid -eq 100 ]] || printf ","
          printf "{\"vmid\":%s}" "$vmid"
        done
        printf "]\n"
      fi
    elif [[ " $* " == *" curl "* ]]; then
      printf "{\"status\":\"ok\",\"version\":\"0.3.0\"}\n"
    elif [[ " $* " == *" hubinet-maint inspect"* ]]; then
      printf "{\"ok\":true,\"data\":{\"health_status\":\"healthy\"}}\n"
    fi
    ;;
esac
'

bash "$ROOT/deploy/install-ha-0.3.0-from-pve.sh" ha.test http://agent.test:8787 2222
bash "$ROOT/deploy/upgrade-0.3.0-from-pve.sh"

mapfile -t scp_calls < "$LOG_DIR/scp.args"
[[ ${#scp_calls[@]} -eq 3 ]]
[[ ${scp_calls[0]} == *"hubinet_ops.yaml><root@ha.test:/config/packages/hubinet_ops.yaml.new>"* ]]
[[ ${scp_calls[1]} == *"hubinet_ops.yaml><root@ha.test:/config/dashboards/hubinet_ops.yaml.new>"* ]]
[[ ${scp_calls[2]} == *"<root@ha.test:/tmp/hubinet-ops-0.3.0-urls>"* ]]

grep -Fq '<status><100>' "$LOG_DIR/qm.args"
for vmid in $(seq 101 110); do
  grep -Fq "<status><$vmid>" "$LOG_DIR/pct.args"
done
for vmid in $(seq 101 109); do
  grep -Fq "<exec><$vmid><--></usr/local/sbin/hubinet-maint><inspect>" "$LOG_DIR/pct.args"
done
if grep -E '^<(start|shutdown|reboot|snapshot|rollback)>' "$LOG_DIR/pct.args"; then
  echo "Installer executed a forbidden resource action" >&2
  exit 1
fi
if grep -E 'check-updates| hubinet-maint update' "$LOG_DIR/pct.args"; then
  echo "Installer executed a forbidden managed action" >&2
  exit 1
fi

# A post-install inventory mismatch must traverse the full rollback rather than
# relying on ERR propagation from an explicit exit.
rm -f "$LOG_DIR/bash-s-count"
: > "$LOG_DIR/pct.args"
: > "$LOG_DIR/install.args"
export HUBINET_OPS_FAKE_RESOURCE_COUNT=10
if bash "$ROOT/deploy/upgrade-0.3.0-from-pve.sh"; then
  echo "Inventory mismatch unexpectedly succeeded" >&2
  exit 1
fi
unset HUBINET_OPS_FAKE_RESOURCE_COUNT

grep -Fq '<exec><110><--><bash><-s><--></root/hubinet-ops-backups/20260719-120000-before-0.3.0>' "$LOG_DIR/pct.args"
for restored in \
  /opt/hubinet-ops/app \
  /etc/hubinet-ops/config.yaml \
  /etc/hubinet-ops/agent.env \
  /var/lib/hubinet-ops; do
  grep -Fq "$restored" "$LOG_DIR/bash-s-4.body"
done
grep -Fq 'service_action start' "$ROOT/deploy/agent/restore-0.3.0.sh"
for vmid in $(seq 101 109); do
  grep -Eq "^<push><$vmid><.*managed/$vmid/hubinet-maint></usr/local/sbin/hubinet-maint><--perms><0755>" "$LOG_DIR/pct.args"
  grep -Eq "^<push><$vmid><.*managed/$vmid/hubinet-maint.json></etc/hubinet-maint.json><--perms><0644>" "$LOG_DIR/pct.args"
done
for target in \
  /usr/local/sbin/hubinet-ops-host \
  /etc/hubinet-ops/allowed-vmids \
  /etc/hubinet-ops/observation-vmids \
  /etc/hubinet-ops/managed-vmids \
  /etc/hubinet-ops/maintenance-vmids \
  /etc/hubinet-ops/lifecycle-vmids \
  /etc/hubinet-ops/resource-types; do
  grep -Fq "$target" "$LOG_DIR/rm.args"
done

# A partial backup of managed files is never marked complete and no production
# layer is modified when a confirmed-existing file cannot be pulled.
rm -f "$LOG_DIR/bash-s-count"
: > "$LOG_DIR/pct.args"
export HUBINET_OPS_FAKE_STAMP=20260719-120100
export HUBINET_OPS_FAKE_PULL_FAILURE=/etc/hubinet-maint.json
if bash "$ROOT/deploy/upgrade-0.3.0-from-pve.sh"; then
  echo "Partial managed backup unexpectedly succeeded" >&2
  exit 1
fi
unset HUBINET_OPS_FAKE_STAMP HUBINET_OPS_FAKE_PULL_FAILURE
partial="$HUBINET_OPS_HOST_BACKUP_BASE/20260719-120100-before-0.3.0/managed/101"
[[ -s "$partial/hubinet-maint" ]]
[[ ! -e "$partial/backup.complete" ]]
if grep -Fq '<push>' "$LOG_DIR/pct.args"; then
  echo "Upgrade modified a layer after partial managed backup" >&2
  exit 1
fi

printf 'executor-before\n' > "$CT_ROOT/usr/local/sbin/hubinet-maint"
printf 'config-before\n' > "$CT_ROOT/etc/hubinet-maint.json"
for scenario in malformed TERM INT; do
  [[ -d "$CT_ROOT" ]]
  rm -f "$LOG_DIR/bash-s-count"
  : > "$LOG_DIR/pct.args"
  export HUBINET_OPS_FAKE_STOPPED_VMID=101
  export HUBINET_OPS_FAKE_STAMP="20260719-1202${scenario:0:1}"
  if [[ "$scenario" == malformed ]]; then
    export HUBINET_OPS_FAKE_MOUNT_OUTPUT=malformed
  else
    export HUBINET_OPS_FAKE_MOUNT_OUTPUT=valid
    export HUBINET_OPS_FAKE_MOUNT_SIGNAL="$scenario"
  fi
  if bash "$ROOT/deploy/upgrade-0.3.0-from-pve.sh"; then
    echo "$scenario mounted-CT failure unexpectedly succeeded" >&2
    exit 1
  fi
  grep -Fq '<unmount><101>' "$LOG_DIR/pct.args"
  grep -Fxq 'executor-before' "$CT_ROOT/usr/local/sbin/hubinet-maint"
  grep -Fxq 'config-before' "$CT_ROOT/etc/hubinet-maint.json"
  if [[ "$scenario" != malformed ]]; then
    grep -Fq "$scenario" "$LOG_DIR/mount-signals.log"
  fi
  unset HUBINET_OPS_FAKE_MOUNT_SIGNAL
done
unset HUBINET_OPS_FAKE_STOPPED_VMID HUBINET_OPS_FAKE_MOUNT_OUTPUT HUBINET_OPS_FAKE_STAMP

echo "0.3.0 installer runtime smoke passed"
