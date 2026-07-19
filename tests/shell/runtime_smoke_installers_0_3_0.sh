#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TMP_ROOT="$(mktemp -d)"
export TMP_ROOT
FAKE_BIN="$TMP_ROOT/bin"
LOG_DIR="$TMP_ROOT/logs"
CT_ROOT="$TMP_ROOT/stopped-ct"
HOST_ROOT="$TMP_ROOT/host"
mkdir -p "$FAKE_BIN" "$LOG_DIR" "$CT_ROOT/usr/local/sbin" "$CT_ROOT/etc" \
  "$HOST_ROOT/usr/local/sbin" "$HOST_ROOT/etc/hubinet-ops"
trap 'rm -rf "$TMP_ROOT"' EXIT
export HUBINET_OPS_TEST_MODE=1
export HUBINET_OPS_TEST_LOG_DIR="$LOG_DIR"
export HUBINET_OPS_HOST_BACKUP_BASE="$TMP_ROOT/backups"
export HUBINET_OPS_FAKE_CT_ROOT="$CT_ROOT"
export HUBINET_OPS_TEST_HOST_ROOT="$HOST_ROOT"
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
if [[ ${HUBINET_OPS_FAKE_ROLLBACK_ORDER:-no} == yes \
  && "${@: -2:1}" == "$HUBINET_OPS_HOST_BACKUP_BASE"/* ]]; then
  printf "host-restore\n" >> "$HUBINET_OPS_TEST_LOG_DIR/rollback-order.log"
fi
if [[ " $* " == *" -d "* ]]; then
  mkdir -p "${@: -1}"
  exit 0
fi
if [[ "${@: -1}" == "$HUBINET_OPS_FAKE_CT_ROOT"/* ]]; then
  exec /usr/bin/install "$@"
fi
'
write_stub cp '
if [[ -n ${HUBINET_OPS_FAKE_MOUNT_SIGNAL:-} && "${2:-}" == "$HUBINET_OPS_FAKE_CT_ROOT"/* ]]; then
  printf '%s\n' "$HUBINET_OPS_FAKE_MOUNT_SIGNAL" >> "$HUBINET_OPS_TEST_LOG_DIR/mount-signals.log"
  kill -"$HUBINET_OPS_FAKE_MOUNT_SIGNAL" "$PPID"
  sleep 1
fi
if [[ -n ${HUBINET_OPS_FAKE_BACKUP_CP_FAILURE:-} && "${2:-}" == "$HUBINET_OPS_FAKE_BACKUP_CP_FAILURE" ]]; then
  printf 'partial-backup\n' > "${@: -1}"
  exit 1
fi
exec /usr/bin/cp "$@"
'
write_stub touch 'exec /usr/bin/touch "$@"'
write_stub rm '
printf "<%s>" "$@" >> "$HUBINET_OPS_TEST_LOG_DIR/rm.args"
printf "\n" >> "$HUBINET_OPS_TEST_LOG_DIR/rm.args"
for value in "$@"; do
  if [[ ${HUBINET_OPS_FAKE_ROLLBACK_ORDER:-no} == yes \
    && "$value" == "$HUBINET_OPS_TEST_HOST_ROOT"/* ]]; then
    printf "host-restore\n" >> "$HUBINET_OPS_TEST_LOG_DIR/rollback-order.log"
  fi
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
  unmount)
    count_file="$HUBINET_OPS_TEST_LOG_DIR/unmount-${2:-unknown}.count"
    count=0
    [[ ! -f "$count_file" ]] || count="$(cat "$count_file")"
    count=$((count + 1))
    printf '%s' "$count" > "$count_file"
    if [[ ${2:-} == "${HUBINET_OPS_FAKE_UNMOUNT_VMID:-none}" ]] \
      && ((count <= ${HUBINET_OPS_FAKE_UNMOUNT_FAILURES:-0})); then
      exit 1
    fi
    ;;
  pull)
    if [[ -n ${HUBINET_OPS_FAKE_PULL_FAILURE:-} && "$3" == "$HUBINET_OPS_FAKE_PULL_FAILURE" ]]; then
      exit 1
    fi
    printf "existing-%s\n" "${3##*/}" > "$4"
    ;;
  push)
    if [[ ${HUBINET_OPS_FAKE_ROLLBACK_ORDER:-no} == yes \
      && "${3:-}" == "$HUBINET_OPS_HOST_BACKUP_BASE"/*/managed/"${2:-}"/* ]]; then
      printf "managed-restore-%s\n" "${2:-}" >> "$HUBINET_OPS_TEST_LOG_DIR/rollback-order.log"
    fi
    if [[ ${2:-} == "${HUBINET_OPS_FAKE_RESTORE_PUSH_FAIL_VMID:-none}" \
      && "${3:-}" == */managed/"${2:-}"/* ]]; then
      exit 1
    fi
    exit 0
    ;;
  exec)
    if [[ ${4:-} == systemctl && ${5:-} == stop && ${6:-} == hubinet-ops ]]; then
      if [[ ${HUBINET_OPS_FAKE_ROLLBACK_ORDER:-no} == yes ]]; then
        printf "new-agent-stop\n" >> "$HUBINET_OPS_TEST_LOG_DIR/rollback-order.log"
      fi
      [[ ${HUBINET_OPS_FAKE_ROLLBACK_AGENT_STOP_FAIL:-no} != yes ]]
    elif [[ ${4:-} == sh && ${5:-} == -c \
      && ${6:-} == *"systemctl is-active hubinet-ops"* ]]; then
      case "${HUBINET_OPS_FAKE_ROLLBACK_AGENT_STATE:-inactive}" in
        inactive) printf "inactive:3\n" ;;
        ambiguous) printf "activating:0\n" ;;
        exec-fail) exit 1 ;;
      esac
    elif [[ " $* " == *" bash -s "* ]]; then
      count_file="$HUBINET_OPS_TEST_LOG_DIR/bash-s-count"
      count=0
      [[ ! -f "$count_file" ]] || count="$(cat "$count_file")"
      count=$((count + 1))
      printf "%s" "$count" > "$count_file"
      cat > "$HUBINET_OPS_TEST_LOG_DIR/bash-s-$count.body"
      if [[ $count -eq 4 ]]; then
        if [[ ${HUBINET_OPS_FAKE_ROLLBACK_ORDER:-no} == yes ]]; then
          printf "agent-restore-110\n" >> "$HUBINET_OPS_TEST_LOG_DIR/rollback-order.log"
        fi
        if [[ ${HUBINET_OPS_FAKE_AGENT_RESTORE_FAIL:-no} == yes ]]; then
          exit 1
        fi
        if [[ ${HUBINET_OPS_FAKE_ROLLBACK_ORDER:-no} == yes ]]; then
          printf "old-agent-start-110\n" >> "$HUBINET_OPS_TEST_LOG_DIR/rollback-order.log"
        fi
      fi
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
    elif [[ " ${*:3} " == *" sh -c "* ]]; then
      case "${HUBINET_OPS_FAKE_PROBE:-present}" in
        present) printf "present\n" ;;
        absent) printf "absent\n" ;;
        exec-fail) exit 1 ;;
        ambiguous) printf "absent\nnoise\n" ;;
      esac
    fi
    ;;
esac
'

bash "$ROOT/deploy/install-ha-0.3.0-from-pve.sh" ha.test http://agent.test:8787 2222
bash "$ROOT/deploy/upgrade-0.3.0-from-pve.sh"

[[ -s "$HUBINET_OPS_HOST_BACKUP_BASE/20260719-120000-before-0.3.0/managed/101/hubinet-maint" ]]
[[ -s "$HUBINET_OPS_HOST_BACKUP_BASE/20260719-120000-before-0.3.0/managed/101/hubinet-maint.json" ]]

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
rm -f "$LOG_DIR/rollback-order.log"
: > "$LOG_DIR/pct.args"
: > "$LOG_DIR/install.args"
export HUBINET_OPS_FAKE_RESOURCE_COUNT=10 HUBINET_OPS_FAKE_ROLLBACK_ORDER=yes
if rollback_output="$(bash "$ROOT/deploy/upgrade-0.3.0-from-pve.sh" 2>&1)"; then
  echo "Inventory mismatch unexpectedly succeeded" >&2
  exit 1
fi
unset HUBINET_OPS_FAKE_RESOURCE_COUNT HUBINET_OPS_FAKE_ROLLBACK_ORDER
[[ "$rollback_output" == *'Rollback completed'* ]]

stop_line="$(grep -n '^new-agent-stop$' "$LOG_DIR/rollback-order.log" | head -n 1 | cut -d: -f1)"
host_line="$(grep -n '^host-restore$' "$LOG_DIR/rollback-order.log" | head -n 1 | cut -d: -f1)"
managed_first_line="$(grep -n '^managed-restore-101$' "$LOG_DIR/rollback-order.log" | head -n 1 | cut -d: -f1)"
managed_last_line="$(grep -n '^managed-restore-109$' "$LOG_DIR/rollback-order.log" | tail -n 1 | cut -d: -f1)"
agent_restore_line="$(grep -n '^agent-restore-110$' "$LOG_DIR/rollback-order.log" | head -n 1 | cut -d: -f1)"
old_agent_start_line="$(grep -n '^old-agent-start-110$' "$LOG_DIR/rollback-order.log" | head -n 1 | cut -d: -f1)"
[[ "$stop_line" -lt "$host_line" ]]
[[ "$host_line" -lt "$managed_first_line" ]]
[[ "$managed_last_line" -lt "$agent_restore_line" ]]
[[ "$agent_restore_line" -lt "$old_agent_start_line" ]]
[[ "$(tail -n 1 "$LOG_DIR/rollback-order.log")" == old-agent-start-110 ]]

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

# A failed CT101 rollback layer is reported but does not prevent CT102-109 or
# the CT110 agent restore from being attempted.
rm -f "$LOG_DIR/bash-s-count"
: > "$LOG_DIR/pct.args"
export HUBINET_OPS_FAKE_STAMP=20260719-120050
export HUBINET_OPS_FAKE_RESOURCE_COUNT=10
export HUBINET_OPS_FAKE_RESTORE_PUSH_FAIL_VMID=101
if rollback_output="$(bash "$ROOT/deploy/upgrade-0.3.0-from-pve.sh" 2>&1)"; then
  echo 'Incomplete managed rollback unexpectedly succeeded' >&2
  exit 1
fi
unset HUBINET_OPS_FAKE_STAMP HUBINET_OPS_FAKE_RESOURCE_COUNT HUBINET_OPS_FAKE_RESTORE_PUSH_FAIL_VMID
[[ "$rollback_output" == *'ROLLBACK INCOMPLETE'* ]]
[[ "$rollback_output" == *'CT101 managed executor'* ]]
for vmid in $(seq 102 109); do
  grep -Eq "^<push><$vmid><.*managed/$vmid/hubinet-maint></usr/local/sbin/hubinet-maint>" "$LOG_DIR/pct.args"
done
grep -Fq '<exec><110><--><bash><-s><--></root/hubinet-ops-backups/20260719-120050-before-0.3.0>' "$LOG_DIR/pct.args"

# An uncertain CT110 service state makes rollback incomplete, but host,
# managed, and final agent restore layers are still attempted.
rm -f "$LOG_DIR/bash-s-count"
: > "$LOG_DIR/pct.args"
export HUBINET_OPS_FAKE_STAMP=20260719-120060
export HUBINET_OPS_FAKE_RESOURCE_COUNT=10
export HUBINET_OPS_FAKE_ROLLBACK_AGENT_STATE=ambiguous
if rollback_output="$(bash "$ROOT/deploy/upgrade-0.3.0-from-pve.sh" 2>&1)"; then
  echo 'Rollback with uncertain CT110 service state unexpectedly succeeded' >&2
  exit 1
fi
unset HUBINET_OPS_FAKE_STAMP HUBINET_OPS_FAKE_RESOURCE_COUNT HUBINET_OPS_FAKE_ROLLBACK_AGENT_STATE
[[ "$rollback_output" == *'ROLLBACK INCOMPLETE'* ]]
[[ "$rollback_output" == *'CT110 pre-rollback stop verification'* ]]
grep -Eq '^<push><109><.*managed/109/hubinet-maint></usr/local/sbin/hubinet-maint>' "$LOG_DIR/pct.args"
grep -Fq '<exec><110><--><bash><-s><--></root/hubinet-ops-backups/20260719-120060-before-0.3.0>' "$LOG_DIR/pct.args"

# Agent restore failure (including bounded active/health verification failure)
# is propagated by the outer rollback as an incomplete CT110 layer.
rm -f "$LOG_DIR/bash-s-count"
: > "$LOG_DIR/pct.args"
export HUBINET_OPS_FAKE_STAMP=20260719-120070
export HUBINET_OPS_FAKE_RESOURCE_COUNT=10
export HUBINET_OPS_FAKE_AGENT_RESTORE_FAIL=yes
if rollback_output="$(bash "$ROOT/deploy/upgrade-0.3.0-from-pve.sh" 2>&1)"; then
  echo 'Rollback with failed CT110 agent restore unexpectedly succeeded' >&2
  exit 1
fi
unset HUBINET_OPS_FAKE_STAMP HUBINET_OPS_FAKE_RESOURCE_COUNT HUBINET_OPS_FAKE_AGENT_RESTORE_FAIL
[[ "$rollback_output" == *'ROLLBACK INCOMPLETE'* ]]
[[ "$rollback_output" == *'CT110 agent'* ]]
grep -Eq '^<push><109><.*managed/109/hubinet-maint></usr/local/sbin/hubinet-maint>' "$LOG_DIR/pct.args"

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

# A partial host backup is never used as a restore source before changes begin.
printf 'wrapper-before\n' > "$HOST_ROOT/usr/local/sbin/hubinet-ops-host"
printf 'allowlist-before\n' > "$HOST_ROOT/etc/hubinet-ops/allowed-vmids"
: > "$LOG_DIR/pct.args"
: > "$LOG_DIR/install.args"
: > "$LOG_DIR/rm.args"
export HUBINET_OPS_FAKE_STAMP=20260719-120110
export HUBINET_OPS_FAKE_BACKUP_CP_FAILURE="$HOST_ROOT/usr/local/sbin/hubinet-ops-host"
if host_backup_output="$(bash "$ROOT/deploy/upgrade-0.3.0-from-pve.sh" 2>&1)"; then
  echo 'Partial host backup unexpectedly succeeded' >&2
  exit 1
fi
unset HUBINET_OPS_FAKE_STAMP HUBINET_OPS_FAKE_BACKUP_CP_FAILURE
grep -Fxq 'wrapper-before' "$HOST_ROOT/usr/local/sbin/hubinet-ops-host"
grep -Fxq 'allowlist-before' "$HOST_ROOT/etc/hubinet-ops/allowed-vmids"
host_partial="$HUBINET_OPS_HOST_BACKUP_BASE/20260719-120110-before-0.3.0"
[[ -s "$host_partial/hubinet-ops-host" ]]
[[ ! -e "$host_partial/backup.complete" ]]
if grep -Fq '<push>' "$LOG_DIR/pct.args" || grep -Fq '<bash><-s><--></root/hubinet-ops-backups/' "$LOG_DIR/pct.args"; then
  echo 'Pre-change host backup failure attempted a production restore' >&2
  exit 1
fi

# The running-CT probe accepts only exact present/absent markers. Only an exact
# absent result creates .absent backup markers.
rm -f "$LOG_DIR/bash-s-count"
: > "$LOG_DIR/pct.args"
export HUBINET_OPS_FAKE_STAMP=20260719-120120 HUBINET_OPS_FAKE_PROBE=absent
bash "$ROOT/deploy/upgrade-0.3.0-from-pve.sh"
absent_backup="$HUBINET_OPS_HOST_BACKUP_BASE/20260719-120120-before-0.3.0/managed/101"
[[ -f "$absent_backup/hubinet-maint.absent" && -f "$absent_backup/hubinet-maint.json.absent" ]]
[[ ! -e "$absent_backup/hubinet-maint" && ! -e "$absent_backup/hubinet-maint.json" ]]
unset HUBINET_OPS_FAKE_STAMP HUBINET_OPS_FAKE_PROBE

for probe in exec-fail ambiguous; do
  rm -f "$LOG_DIR/bash-s-count"
  : > "$LOG_DIR/pct.args"
  export HUBINET_OPS_FAKE_STAMP="20260719-12013${probe:0:1}" HUBINET_OPS_FAKE_PROBE="$probe"
  if bash "$ROOT/deploy/upgrade-0.3.0-from-pve.sh"; then
    echo "$probe upgrader probe unexpectedly succeeded" >&2
    exit 1
  fi
  probe_backup="$HUBINET_OPS_HOST_BACKUP_BASE/${HUBINET_OPS_FAKE_STAMP}-before-0.3.0/managed/101"
  [[ ! -e "$probe_backup/hubinet-maint.absent" ]]
  [[ ! -e "$probe_backup/backup.complete" ]]
done
unset HUBINET_OPS_FAKE_STAMP HUBINET_OPS_FAKE_PROBE

# Upgrader mount tracking also retries transient unmount errors and retains the
# logical mounted state through persistent failures.
rm -f "$LOG_DIR/bash-s-count" "$LOG_DIR/unmount-101.count"
: > "$LOG_DIR/pct.args"
export HUBINET_OPS_FAKE_STAMP=20260719-120140 HUBINET_OPS_FAKE_STOPPED_VMID=101
export HUBINET_OPS_FAKE_UNMOUNT_VMID=101 HUBINET_OPS_FAKE_UNMOUNT_FAILURES=1
bash "$ROOT/deploy/upgrade-0.3.0-from-pve.sh"
[[ "$(cat "$LOG_DIR/unmount-101.count")" -ge 2 ]]
unset HUBINET_OPS_FAKE_STAMP HUBINET_OPS_FAKE_STOPPED_VMID HUBINET_OPS_FAKE_UNMOUNT_VMID HUBINET_OPS_FAKE_UNMOUNT_FAILURES

rm -f "$LOG_DIR/bash-s-count" "$LOG_DIR/unmount-101.count"
: > "$LOG_DIR/pct.args"
export HUBINET_OPS_FAKE_STAMP=20260719-120150 HUBINET_OPS_FAKE_STOPPED_VMID=101
export HUBINET_OPS_FAKE_UNMOUNT_VMID=101 HUBINET_OPS_FAKE_UNMOUNT_FAILURES=99
if unmount_output="$(bash "$ROOT/deploy/upgrade-0.3.0-from-pve.sh" 2>&1)"; then
  echo 'Persistent upgrader pct unmount failure unexpectedly succeeded' >&2
  exit 1
fi
unset HUBINET_OPS_FAKE_STAMP HUBINET_OPS_FAKE_STOPPED_VMID HUBINET_OPS_FAKE_UNMOUNT_VMID HUBINET_OPS_FAKE_UNMOUNT_FAILURES
[[ "$unmount_output" != *'installed transactionally'* ]]
[[ "$unmount_output" == *'CT101 remains mounted; manual intervention required: pct unmount 101'* ]]
[[ "$(cat "$LOG_DIR/unmount-101.count")" -ge 4 ]]
if grep -Fq '<push>' "$LOG_DIR/pct.args"; then
  echo 'Persistent pre-change unmount failure modified a production layer' >&2
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
