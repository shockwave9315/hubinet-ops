#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TMP_ROOT="$(mktemp -d)"
FAKE_BIN="$TMP_ROOT/bin"
LOG_DIR="$TMP_ROOT/logs"
mkdir -p "$FAKE_BIN" "$LOG_DIR"
trap 'rm -rf "$TMP_ROOT"' EXIT
export HUBINET_OPS_TEST_MODE=1
export HUBINET_OPS_TEST_LOG_DIR="$LOG_DIR"
export HUBINET_OPS_HOST_BACKUP_BASE="$TMP_ROOT/backups"
export PATH="$FAKE_BIN:$PATH"

for script in \
  "$ROOT/deploy/install-ha-0.3.0-from-pve.sh" \
  "$ROOT/deploy/upgrade-0.3.0-from-pve.sh" \
  "$ROOT/deploy/managed/install-managed.sh"; do
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

write_stub date 'printf "20260719-120000\n"'
write_stub python3 '
printf "<%s>" "$@" >> "$HUBINET_OPS_TEST_LOG_DIR/python3.args"
printf "\n" >> "$HUBINET_OPS_TEST_LOG_DIR/python3.args"
if [[ ${1:-} == -c ]]; then printf "11\n"; fi
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
write_stub cp 'exit 0'
write_stub touch 'exit 0'
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
  status) printf "status: running\n" ;;
  pull) exit 1 ;;
  push) exit 0 ;;
  exec)
    if [[ " $* " == *" bash -s "* ]]; then
      cat >/dev/null
      count_file="$HUBINET_OPS_TEST_LOG_DIR/bash-s-count"
      count=0
      [[ ! -f "$count_file" ]] || count="$(cat "$count_file")"
      count=$((count + 1))
      printf "%s" "$count" > "$count_file"
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

echo "0.3.0 installer runtime smoke passed"
