#!/usr/bin/env bash
set -Eeuo pipefail

[[ "${HUBINET_OPS_SYSTEM_SANDBOX:-0}" == 1 ]] || {
  echo "runtime smoke must execute inside the system sandbox" >&2
  exit 2
}
[[ "$(id -u)" != 0 ]] || {
  echo "runtime smoke sandbox must use a non-root user" >&2
  exit 2
}

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
ORIGINAL_HOST_PATH="$PATH"
REAL_BASH="$(command -v bash)"
REAL_PYTHON="${HUBINET_OPS_TEST_PYTHON:-$(command -v python3 || command -v python)}"
REAL_CP="$(command -v cp)"
REAL_MV="$(command -v mv)"
SAFE_TOOL_NAMES=(
  bash
  awk
  basename
  cat
  chmod
  date
  dirname
  grep
  gzip
  mkdir
  rm
  sed
  seq
  sha256sum
  sleep
  tar
)
declare -A SAFE_TOOL_PATHS=()
for safe_tool in "${SAFE_TOOL_NAMES[@]}"; do
  SAFE_TOOL_PATHS["$safe_tool"]="$(command -v "$safe_tool")" || {
    echo "required safe smoke tool is unavailable: $safe_tool" >&2
    exit 1
  }
done
HOST_ONLY_BIN="$TMP/host-only-bin"
HOST_PATH_SENTINEL="hubinet-host-path-sentinel"
mkdir -p "$HOST_ONLY_BIN"
printf '#!/usr/bin/env bash\nexit 0\n' > "$HOST_ONLY_BIN/$HOST_PATH_SENTINEL"
chmod +x "$HOST_ONLY_BIN/$HOST_PATH_SENTINEL"

make_fakes() {
  local root="$1" bin safe_bin pve ct safe_tool
  bin="$root/bin"; safe_bin="$root/safe-bin"; pve="$root/pve"; ct="$root/ct"
  mkdir -p "$bin" "$safe_bin" "$root/pycompat" "$pve/etc/hubinet-ops" "$pve/usr/local/sbin" "$ct/ct110"
  for safe_tool in "${SAFE_TOOL_NAMES[@]}"; do
    ln -s "${SAFE_TOOL_PATHS[$safe_tool]}" "$safe_bin/$safe_tool"
  done
  printf 'LOCK_EX=2\nLOCK_NB=4\ndef flock(*args, **kwargs): return None\n' > "$root/pycompat/fcntl.py"
  cp "$ROOT/config/config.example.yaml" "$ct/ct110/etc-config.yaml"
  printf 'HUBINET_OPS_API_TOKEN=%064d\n' 0 > "$ct/ct110/agent.env"
  printf '{"bind":"192.0.2.10","port":8741,"database":"/var/lib/hubinet-ops-hostd/jobs.db","client_allowlist":[]}\n' > "$pve/etc/hubinet-ops/hostd.json"
  printf 'HUBINET_OPS_HOSTD_TOKEN=%064d\n' 1 > "$pve/etc/hubinet-ops/hostd.env"
  printf 'old-wrapper\n' > "$pve/usr/local/sbin/hubinet-ops-host"
  for vmid in $(seq 101 110); do
    mkdir -p "$ct/ct$vmid/usr/local/sbin" "$ct/ct$vmid/etc"
    printf 'old-executor-%s\n' "$vmid" > "$ct/ct$vmid/usr/local/sbin/hubinet-maint"
    printf '{"old_profile":%s}\n' "$vmid" > "$ct/ct$vmid/etc/hubinet-maint.json"
  done
  cat > "$bin/pct" <<'SH'
#!/usr/bin/env bash
set -Eeuo pipefail
printf 'pct %s\n' "$*" >> "$TEST_LOG"
if [[ -n "${PCT_FAIL_129_ONCE_MATCH:-}" &&
      "$*" == *"$PCT_FAIL_129_ONCE_MATCH"* &&
      ! -e "$TEST_CT_ROOT/pct-129-injected" ]]; then
  : > "$TEST_CT_ROOT/pct-129-injected"
  exit 129
fi
action="$1"; vmid="${2:-}"; shift 2 || true
root="$TEST_CT_ROOT/ct$vmid"
case "$action" in
  status)
    if (( vmid >= 106 && vmid <= 109 )); then echo "status stopped"; else echo "status running"; fi
    ;;
  mount)
    if [[ "${HUBINET_OPS_FAKE_MOUNT_OUTPUT:-}" == malformed && "$vmid" == 106 ]]; then
      printf 'MARK mount-output-malformed %s\n' "$vmid" >> "$TEST_LOG"
      echo "mounted without a parseable path"
      exit 0
    fi
    mkdir -p "$root"
    echo "CT $vmid mounted at '$root'"
    ;;
  unmount)
    count_file="$TEST_CT_ROOT/unmount-$vmid.count"
    count=$(( $(cat "$count_file" 2>/dev/null || echo 0) + 1 ))
    echo "$count" > "$count_file"
    inject=false
    if [[ "${HUBINET_OPS_FAKE_UNMOUNT_VMID:-}" == "$vmid" ]]; then
      if [[ -z "${HUBINET_OPS_FAKE_UNMOUNT_AFTER_MARKER:-}" ]] ||
         grep -Fq "$HUBINET_OPS_FAKE_UNMOUNT_AFTER_MARKER" "$TEST_LOG"; then
        inject=true
      fi
    fi
    failure_count_file="$TEST_CT_ROOT/unmount-failure-$vmid.count"
    failure_count=$(( $(cat "$failure_count_file" 2>/dev/null || echo 0) + 1 ))
    if [[ "$inject" == true ]]; then
      echo "$failure_count" > "$failure_count_file"
    fi
    if [[ "$inject" == true &&
          "${HUBINET_OPS_FAKE_UNMOUNT_FAILURES:-0}" -ge "$failure_count" ]]; then
      printf 'MARK unmount-failure %s %s\n' "$vmid" "$failure_count" >> "$TEST_LOG"
      echo "injected unmount failure" >&2
      exit 1
    fi
    printf 'MARK unmount-success %s\n' "$vmid" >> "$TEST_LOG"
    ;;
  pull)
    src="$1"; dst="$2"
    if [[ "$vmid" == 110 && "$src" == /etc/hubinet-ops/config.yaml ]]; then
      cp "$TEST_CT_ROOT/ct110/etc-config.yaml" "$dst"
    elif [[ "$vmid" == 110 && "$src" == /etc/hubinet-ops/agent.env ]]; then
      cp "$TEST_CT_ROOT/ct110/agent.env" "$dst"
    else
      cp "$root$src" "$dst"
    fi
    ;;
  push)
    src="$1"; dst="$2"
    mkdir -p "$(dirname "$root$dst")"
    cp "$src" "$root$dst"
    ;;
  exec)
    [[ "$1" == -- ]] && shift
    if [[ "$1" == bash && "$2" == -s ]]; then
      script="$(cat)"
      if grep -q '/api/v1/state' <<<"$script"; then
        "$REAL_PYTHON" - <<'PY'
import json
import os
resources={}
vm100_status=os.environ.get("HUBINET_OPS_FAKE_VM100_STATUS", "running")
for vmid in range(100,111):
    state={"vmid":vmid,"last_refresh":"2099-01-01T00:00:00+00:00","health_status":"healthy","health_score":100}
    if vmid==100:
        state["qemu_status"]=vm100_status
        if vm100_status=="running": state["cpu"]={"usage_percent":3.05257}
        else: state["health_status"]="offline"
    if 101 <= vmid <= 109: state.update(executor_compatible=True,executor_version="0.4.1",executor_protocol_version=1)
    resources[str(vmid)]=state
print(json.dumps({"version":"0.4.2","resources":resources}))
PY
      elif grep -q '/api/v1/resources/106/snapshots' <<<"$script"; then
        echo '{"snapshots":[],"latest":null}'
      elif grep -q 'hubinet-ops-0.4.2.tgz' <<<"$script"; then
        grep -Fq 'install -d -m 0750 -o root -g hubinetops /etc/hubinet-ops/keys' <<<"$script"
        grep -Fq 'chown hubinetops:hubinetops /etc/hubinet-ops/keys/proxmox_ed25519' <<<"$script"
        grep -Fq 'chmod 0600 /etc/hubinet-ops/keys/proxmox_ed25519' <<<"$script"
        grep -Fq 'chown root:hubinetops /etc/hubinet-ops/ssh_known_hosts' <<<"$script"
        grep -Fq 'chmod 0640 /etc/hubinet-ops/ssh_known_hosts' <<<"$script"
        permissions_line="$(grep -nF 'chmod 0640 /etc/hubinet-ops/ssh_known_hosts' <<<"$script" | awk -F: 'NR == 1 { print $1 }')"
        start_line="$(grep -nF 'systemctl start hubinet-ops' <<<"$script" | awk -F: 'NR == 1 { print $1 }')"
        [[ "$permissions_line" -lt "$start_line" ]]
        echo AGENT_SSH_PERMISSIONS_NORMALIZED >> "$TEST_LOG"
        mv "$root/etc/hubinet-ops/config.yaml.new" "$TEST_CT_ROOT/ct110/etc-config.yaml"
        mv "$root/etc/hubinet-ops/agent.env.new" "$TEST_CT_ROOT/ct110/agent.env"
        echo AGENT_INSTALLED >> "$TEST_LOG"
      elif grep -q 'restore_started' <<<"$script"; then
        echo AGENT_RESTORED >> "$TEST_LOG"
      else
        echo AGENT_BACKED_UP >> "$TEST_LOG"
      fi
    elif [[ "$1" == sh && "$2" == -c ]]; then
      remote="${@: -1}"
      [[ -e "$root$remote" ]] && echo present || echo absent
    elif [[ "$1" == python3 && "$2" == -m && "$3" == py_compile ]]; then
      "$REAL_PYTHON" -m py_compile "$root$4"
    elif [[ "$1" == install ]]; then
      src="${@: -2:1}"; dst="${@: -1}"
      mkdir -p "$(dirname "$root$dst")"
      cp "$root$src" "$root$dst"
    elif [[ "$1" == mv ]]; then
      src="${@: -2:1}"; dst="${@: -1}"
      mkdir -p "$(dirname "$root$dst")"; mv "$root$src" "$root$dst"
    elif [[ "$1" == rm ]]; then
      for value in "$@"; do [[ "$value" == /* ]] && rm -f "$root$value" || true; done
    elif [[ "$1" == /usr/local/sbin/hubinet-maint && "$2" == capabilities ]]; then
      if [[ "${FAIL_CAPABILITIES_VMID:-}" == "$vmid" ]]; then echo '{"ok":false,"data":{}}'; else
        HUBINET_MAINT_CONFIG_PATH="$root/etc/hubinet-maint.json" "$REAL_PYTHON" "$root/usr/local/sbin/hubinet-maint" capabilities
      fi
    elif [[ "$1" == systemctl && "$2" == start ]]; then
      echo AGENT_STARTED >> "$TEST_LOG"
    elif [[ "$1" == curl ]]; then
      if grep -Fq 'AGENT_INSTALLED' "$TEST_LOG"; then
        echo '{"status":"ok","version":"0.4.2"}'
      else
        echo '{"status":"ok","version":"0.4.1"}'
      fi
    elif [[ "$1" == /opt/hubinet-ops/.venv/bin/python ]]; then
      cat >/dev/null
    else
      echo "unsupported fake pct exec: $*" >&2; exit 1
    fi
    ;;
  *) echo "unsupported fake pct: $action" >&2; exit 1 ;;
esac
SH
  cat > "$bin/python3" <<'SH'
#!/usr/bin/env bash
if [[ "$1" == *"/ct108/usr/local/sbin/hubinet-maint" && "${2:-}" == capabilities && "${FAIL_CAPABILITIES_VMID:-}" == "108" ]]; then
  echo "FAKE PYTHON INTERCEPTED!" >&2
  echo '{"ok":false,"data":{}}'
  exit 0
fi
exec "$REAL_PYTHON" "$@"
SH
  cat > "$bin/install" <<'SH'
#!/usr/bin/env bash
set -Eeuo pipefail
directory=false
values=()
while (($#)); do
  case "$1" in
    -d) directory=true; shift ;;
    -m|-o|-g) shift 2 ;;
    -*) shift ;;
    *) values+=("$1"); shift ;;
  esac
done
if [[ "$directory" == true ]]; then
  mkdir -p "${values[@]}"
else
  ((${#values[@]} >= 2))
  src="${values[${#values[@]}-2]}"; dst="${values[${#values[@]}-1]}"
  if [[ -n "${FAIL_INSTALL_ONCE_MATCH:-}" &&
        "$dst" == *"$FAIL_INSTALL_ONCE_MATCH"* &&
        ! -e "$TEST_CT_ROOT/install-once-failure-injected" ]]; then
    if [[ -n "${EXPECT_HOSTD_STAGE_MARKER:-}" ]]; then
      grep -Fq "$EXPECT_HOSTD_STAGE_MARKER" "$src"
      [[ "$(grep -Ec '^HUBINET_OPS_HOSTD_(BACKEND_|UPDATE_|RECOVERY_)?TOKEN=' "$src")" == 4 ]]
      printf 'MARK hostd-stage-populated\n' >> "$TEST_LOG"
    fi
    : > "$TEST_CT_ROOT/install-once-failure-injected"
    printf 'MARK install-once-failure %s\n' "$dst" >> "$TEST_LOG"
    exit 1
  fi
  if [[ -n "${FAIL_INSTALL_MATCH:-}" && "$dst" == *"$FAIL_INSTALL_MATCH"* ]]; then
    printf 'MARK install-failure %s\n' "$dst" >> "$TEST_LOG"
    exit 1
  fi
  printf 'install %s -> %s\n' "$src" "$dst" >> "$TEST_LOG"
  mkdir -p "$(dirname "$dst")"; cp "$src" "$dst"
fi
SH
  cat > "$bin/mktemp" <<'SH'
#!/usr/bin/env bash
set -Eeuo pipefail
stage_dir="$TEST_CT_ROOT/secret-stages"
mkdir -p "$stage_dir"
count_file="$stage_dir/mktemp.count"
count=$(( $(cat "$count_file" 2>/dev/null || echo 0) + 1 ))
echo "$count" > "$count_file"
case "$count" in
  1) stage="$stage_dir/hostd-env.stage" ;;
  2) stage="$stage_dir/agent-env.stage" ;;
  *) stage="$stage_dir/stage-$count" ;;
esac
: > "$stage"
printf 'MARK secret-stage-created %s\n' "$(basename "$stage")" >> "$TEST_LOG"
printf '%s\n' "$stage"
SH
  cat > "$bin/mv" <<'SH'
#!/usr/bin/env bash
set -Eeuo pipefail
src="${@: -2:1}"; dst="${@: -1}"
printf 'mv %s -> %s\n' "$src" "$dst" >> "$TEST_LOG"
if [[ -n "${FAIL_MV_MATCH:-}" && "$dst" == *"$FAIL_MV_MATCH"* ]]; then
  printf 'MARK partial-install-failure %s\n' "$dst" >> "$TEST_LOG"
  exit 1
fi
exec "$REAL_MV" "$@"
SH
  cat > "$bin/systemctl" <<'SH'
#!/usr/bin/env bash
printf 'systemctl %s\n' "$*" >> "$TEST_LOG"
case "$1" in is-active) exit 3;; is-enabled) exit 1;; esac
exit 0
SH
cat > "$bin/curl" <<'SH'
#!/usr/bin/env bash
if [[ -f "$TEST_PVE_ROOT/usr/local/lib/hubinet-ops/hubinet_ops_hostd.py" ]] &&
   grep -Fq 'VERSION = "0.4.2"' "$TEST_PVE_ROOT/usr/local/lib/hubinet-ops/hubinet_ops_hostd.py"; then
  echo '{"status":"ok","version":"0.4.2"}'
else
  echo '{"status":"ok","version":"0.4.1"}'
fi
SH
  cat > "$bin/wrapper-smoke" <<'SH'
#!/usr/bin/env bash
case "$SSH_ORIGINAL_COMMAND" in
  'inspect 100') echo '{"ok":true,"data":{"resource_type":"qemu","adapter":"haos","qemu_status":"running","cpu":{"usage":0.0305257}}}' ;;
  'inspect 106') echo '{"ok":true,"data":{"resource_type":"lxc"}}' ;;
  'list-snapshots 106') echo '{"ok":true,"data":{"snapshots":[]}}' ;;
  *) exit 1 ;;
esac
SH
  cat > "$bin/cp" <<'SH'
#!/usr/bin/env bash
printf 'cp %s\n' "$*" >> "$TEST_LOG"
if [[ -n "${FAIL_BACKUP_COPY_VMID:-}" &&
      "${@: -1}" == *"/managed/ct${FAIL_BACKUP_COPY_VMID}/hubinet-maint" ]]; then
  printf 'MARK backup-copy-failure %s\n' "$FAIL_BACKUP_COPY_VMID" >> "$TEST_LOG"
  exit 1
fi
exec "$REAL_CP" "$@"
SH
  chmod +x "$bin"/*
}

verify_isolated_path() {
  local root="$1" isolated_path tool resolved
  isolated_path="$root/bin:$root/safe-bin"

  PATH="$HOST_ONLY_BIN:$ORIGINAL_HOST_PATH" command -v "$HOST_PATH_SENTINEL" >/dev/null
  for tool in "$HOST_PATH_SENTINEL" apt apt-get ssh scp pvesh qm docker podman wget; do
    if PATH="$isolated_path" command -v "$tool" >/dev/null 2>&1; then
      echo "isolated smoke PATH exposed forbidden host command: $tool" >&2
      return 1
    fi
  done
  for tool in pct systemctl curl python3 install mktemp mv cp wrapper-smoke; do
    resolved="$(PATH="$isolated_path" command -v "$tool")"
    [[ "$resolved" == "$root/bin/$tool" ]] || {
      echo "fake smoke command escaped case bin: $tool -> $resolved" >&2
      return 1
    }
  done
  for tool in "${SAFE_TOOL_NAMES[@]}"; do
    resolved="$(PATH="$isolated_path" command -v "$tool")"
    [[ "$resolved" == "$root/safe-bin/$tool" ]] || {
      echo "safe smoke command escaped allowlist: $tool -> $resolved" >&2
      return 1
    }
  done
}

run_case() {
  local name="$1" fail_vmid="${2:-}" retry_match="${3:-}" case_root isolated_path
  local test_pve_root test_backup_root test_archive test_wrapper
  case_root="$TMP/$name"
  mkdir -p "$case_root"
  make_fakes "$case_root"
  isolated_path="$case_root/bin:$case_root/safe-bin"
  test_pve_root="$case_root/pve"
  test_backup_root="$case_root/backups"
  test_archive="$case_root/release.tgz"
  test_wrapper="$case_root/bin/wrapper-smoke"
  for test_path in \
    "$test_pve_root" "$test_backup_root" "$test_archive" "$test_wrapper" \
    "$case_root/ct"
  do
    [[ "$test_path" == "$case_root/"* ]]
  done
  verify_isolated_path "$case_root"
  : > "$case_root/actions.log"
  set +e
  PATH="$isolated_path" \
  TEST_LOG="$case_root/actions.log" \
  TEST_CT_ROOT="$case_root/ct" \
  TEST_PVE_ROOT="$test_pve_root" \
  REAL_PYTHON="$REAL_PYTHON" \
  REAL_CP="$REAL_CP" \
  REAL_MV="$REAL_MV" \
  PYTHONPATH="$case_root/pycompat" \
  FAIL_CAPABILITIES_VMID="$fail_vmid" \
  PCT_FAIL_129_ONCE_MATCH="$retry_match" \
  HUBINET_OPS_TEST_MODE=1 \
  HUBINET_OPS_TEST_PVE_ROOT="$test_pve_root" \
  HUBINET_OPS_BACKUP_ROOT="$test_backup_root" \
  HUBINET_OPS_TEST_ARCHIVE="$test_archive" \
  HUBINET_OPS_TEST_WRAPPER_RUNNER="$test_wrapper" \
  HUBINET_OPS_HOSTD_HEALTH_URL="http://hostd.test/health" \
  HUBINET_OPS_VALIDATION_ATTEMPTS=1 \
  HUBINET_OPS_VALIDATION_DELAY=0 \
    "$REAL_BASH" ${HUBINET_OPS_TEST_BASH_X:+-x} "$ROOT/deploy/upgrade-0.4.2-from-pve.sh" >"$case_root/stdout" 2>"$case_root/stderr"
  rc=$?
  set -e
  printf '%s' "$rc"
}

first_line_after() {
  local file="$1" pattern="$2" after="${3:-0}"
  awk -v pattern="$pattern" -v after="$after" 'NR > after && index($0, pattern) { print NR; exit }' "$file"
}

last_line_before() {
  local file="$1" pattern="$2" before="$3"
  awk -v pattern="$pattern" -v before="$before" 'NR < before && index($0, pattern) { line=NR } END { if (line) print line }' "$file"
}

assert_ordered() {
  local previous=0 value
  for value in "$@"; do
    [[ -n "$value" && "$value" -gt "$previous" ]] || {
      echo "Expected strictly ordered event lines, got: $*" >&2
      return 1
    }
    previous="$value"
  done
}

"$REAL_PYTHON" \
  "$ROOT/scripts/validate_hermetic_shell_boundary.py" \
  "$ROOT/deploy/upgrade-0.4.2-from-pve.sh"

success_rc="$(run_case success)"
if [[ "$success_rc" != 0 ]]; then
  cat "$TMP/success/stderr" >&2
  exit 1
fi

export HUBINET_OPS_FAKE_VM100_STATUS=stopped
stopped_vm_rc="$(run_case stopped_vm)"
unset HUBINET_OPS_FAKE_VM100_STATUS
if [[ "$stopped_vm_rc" != 0 ]]; then
  cat "$TMP/stopped_vm/stderr" >&2
  exit 1
fi
if grep -Eq 'pct (start|stop|shutdown|reboot) 100' "$TMP/stopped_vm/actions.log"; then
  echo "upgrade changed intentionally stopped VM100 lifecycle state" >&2
  exit 1
fi

retry_rc="$(run_case retry "" "push 101")"
if [[ "$retry_rc" != 0 ]]; then
  cat "$TMP/retry/stderr" >&2
  exit 1
fi
[[ "$(grep -c '^pct push 101 ' "$TMP/retry/actions.log")" -ge 2 ]]
grep -Fq 'pct received SIGHUP; retrying 2/3: pct push 101' "$TMP/retry/stderr"
if grep -Fq '0.4.2 upgrade failed' "$TMP/retry/stderr"; then
  echo "global ERR trap fired before the rc=129 retry succeeded" >&2
  exit 1
fi
grep -Fq 'VERSION = "0.4.1"' "$TMP/success/ct/ct101/usr/local/sbin/hubinet-maint"
grep -Fq 'VERSION = "0.4.1"' "$TMP/success/ct/ct109/usr/local/sbin/hubinet-maint"
grep -Fq 'AGENT_INSTALLED' "$TMP/success/actions.log"
grep -Fq 'AGENT_SSH_PERMISSIONS_NORMALIZED' "$TMP/success/actions.log"
grep -Fq 'VERSION = "0.4.2"' "$TMP/success/pve/usr/local/lib/hubinet-ops/hubinet_ops_hostd.py"
for policy in snapshot-create-vmids snapshot-restore-vmids snapshot-delete-vmids; do
  cmp "$ROOT/deploy/pve/$policy" "$TMP/success/pve/etc/hubinet-ops/$policy"
done
if grep -Eq 'pct (start|stop|shutdown|reboot|snapshot|rollback|delsnapshot)' "$TMP/success/actions.log"; then
  echo "upgrade executed a forbidden resource lifecycle or snapshot mutation" >&2
  exit 1
fi

failure_rc="$(run_case rollback 105)"
if [[ "$failure_rc" == 0 ]]; then
  cat "$TMP/rollback/stdout" >&2
  echo "expected the injected executor failure" >&2
  exit 1
fi
for vmid in $(seq 101 109); do
  grep -Fq "old-executor-$vmid" "$TMP/rollback/ct/ct$vmid/usr/local/sbin/hubinet-maint"
  grep -Fq "old_profile" "$TMP/rollback/ct/ct$vmid/etc/hubinet-maint.json"
done
grep -Fq 'old-wrapper' "$TMP/rollback/pve/usr/local/sbin/hubinet-ops-host"
for policy in snapshot-create-vmids snapshot-restore-vmids snapshot-delete-vmids; do
  [[ ! -e "$TMP/rollback/pve/etc/hubinet-ops/$policy" ]]
done
grep -Fq 'AGENT_STARTED' "$TMP/rollback/actions.log"
if grep -Eq 'pct (start|stop|shutdown|reboot|snapshot|rollback|delsnapshot)' "$TMP/rollback/actions.log"; then
  echo "rollback executed a forbidden resource lifecycle or snapshot mutation" >&2
  exit 1
fi

echo "0.4.2 runtime smoke: success, retry and cross-layer rollback passed"

echo "Testing mount tracking and cleanup..."

# 1. pct mount succeeds, registration precedes rejected parsing, and cleanup
# unmounts that same VMID without starting managed-file copying.
export HUBINET_OPS_FAKE_MOUNT_OUTPUT=malformed
rc="$(run_case mount_malformed)"
[[ "$rc" != 0 ]]
mount_log="$TMP/mount_malformed/actions.log"
malformed_mount="$(first_line_after "$mount_log" 'pct mount 106')"
malformed_marker="$(first_line_after "$mount_log" 'MARK mount-output-malformed 106' "$malformed_mount")"
malformed_unmount="$(first_line_after "$mount_log" 'pct unmount 106' "$malformed_marker")"
assert_ordered "$malformed_mount" "$malformed_marker" "$malformed_unmount"
grep -Fq 'Unsafe or unknown mountpoint for CT106' "$TMP/mount_malformed/stderr"
if awk -v after="$malformed_marker" \
  'NR > after && /cp .*\/ct106\/.* \/.*\/managed\/ct106\// { found=1 } END { exit !found }' \
  "$mount_log"; then
  echo "Managed-file copying started after CT106 mountpoint parsing was rejected" >&2
  exit 1
fi
unset HUBINET_OPS_FAKE_MOUNT_OUTPUT

# 2. A backup copy failure for CT108 is marked precisely; backup.complete is
# absent and the cleanup unmount happens after the injected failure.
export FAIL_BACKUP_COPY_VMID=108
rc="$(run_case backup_fails_after_mount)"
[[ "$rc" != 0 ]]
backup_log="$TMP/backup_fails_after_mount/actions.log"
backup_mount="$(first_line_after "$backup_log" 'pct mount 108')"
backup_failure="$(first_line_after "$backup_log" 'MARK backup-copy-failure 108' "$backup_mount")"
backup_unmount="$(first_line_after "$backup_log" 'pct unmount 108' "$backup_failure")"
assert_ordered "$backup_mount" "$backup_failure" "$backup_unmount"
if find "$TMP/backup_fails_after_mount/backups" \
  -path '*/managed/ct108/backup.complete' -print -quit | grep -q .; then
  echo "CT108 backup.complete exists despite the injected backup failure" >&2
  exit 1
fi
unset FAIL_BACKUP_COPY_VMID

# 3. An install failure for stopped CT106 is followed by cleanup unmount before
# rollback restore is allowed to mount CT106 again.
export FAIL_INSTALL_MATCH="ct106/usr/local/sbin/.hubinet-maint.new"
rc="$(run_case install_fails_after_mount)"
[[ "$rc" != 0 ]]
install_log="$TMP/install_fails_after_mount/actions.log"
install_failure="$(first_line_after "$install_log" 'MARK install-failure ')"
install_mount="$(last_line_before "$install_log" 'pct mount 106' "$install_failure")"
cleanup_unmount="$(first_line_after "$install_log" 'pct unmount 106' "$install_failure")"
cleanup_success="$(first_line_after "$install_log" 'MARK unmount-success 106' "$cleanup_unmount")"
restore_mount="$(first_line_after "$install_log" 'pct mount 106' "$cleanup_success")"
assert_ordered "$install_mount" "$install_failure" "$cleanup_unmount" "$cleanup_success" "$restore_mount"
if awk -v start="$install_failure" -v finish="$cleanup_success" \
  'NR > start && NR < finish && $0 == "pct mount 106" { found=1 } END { exit !found }' \
  "$install_log"; then
  echo "CT106 was mounted again before its tracked mount was successfully unmounted" >&2
  exit 1
fi
unset FAIL_INSTALL_MATCH

# 4. A one-shot hostd.env install failure occurs after both secret stages are
# populated but before the hostd stage is normally removed. Rollback must
# remove both stages and still restore the backed-up host files.
export FAIL_INSTALL_ONCE_MATCH="/etc/hubinet-ops/hostd.env"
export EXPECT_HOSTD_STAGE_MARKER="hostd-stage-marker"
export HUBINET_OPS_HOSTD_BACKEND_TOKEN="hostd-stage-marker-backend-000000000001"
export HUBINET_OPS_HOSTD_UPDATE_TOKEN="hostd-stage-marker-update-0000000000002"
export HUBINET_OPS_HOSTD_RECOVERY_TOKEN="hostd-stage-marker-recovery-0000000003"
rc="$(run_case hostd_secret_stage_cleanup)"
[[ "$rc" != 0 ]]
secret_log="$TMP/hostd_secret_stage_cleanup/actions.log"
stage_dir="$TMP/hostd_secret_stage_cleanup/ct/secret-stages"
grep -Fq 'MARK secret-stage-created hostd-env.stage' "$secret_log"
grep -Fq 'MARK secret-stage-created agent-env.stage' "$secret_log"
grep -Fq 'MARK hostd-stage-populated' "$secret_log"
agent_stage_push="$(first_line_after "$secret_log" '/secret-stages/agent-env.stage')"
stage_failure="$(first_line_after "$secret_log" 'MARK install-once-failure ')"
host_restore="$(first_line_after "$secret_log" '/pve/etc/hubinet-ops/hostd.env ' "$stage_failure")"
assert_ordered "$agent_stage_push" "$stage_failure" "$host_restore"
grep -Fq '0.4.2 upgrade failed; restoring all modified layers' \
  "$TMP/hostd_secret_stage_cleanup/stderr"
[[ ! -e "$stage_dir/hostd-env.stage" ]]
[[ ! -e "$stage_dir/agent-env.stage" ]]
if find "$stage_dir" -type f ! -name 'mktemp.count' -print -quit | grep -q .; then
  echo "Secret staging file remained after rollback" >&2
  exit 1
fi
if grep -Rqs 'hostd-stage-marker' "$stage_dir"; then
  echo "Test hostd credential marker remained in the staging directory" >&2
  exit 1
fi
grep -Fq 'old-wrapper' \
  "$TMP/hostd_secret_stage_cleanup/pve/usr/local/sbin/hubinet-ops-host"
unset FAIL_INSTALL_ONCE_MATCH EXPECT_HOSTD_STAGE_MARKER
unset HUBINET_OPS_HOSTD_BACKEND_TOKEN HUBINET_OPS_HOSTD_UPDATE_TOKEN
unset HUBINET_OPS_HOSTD_RECOVERY_TOKEN

# 5. Rollback restore mounts CT108, records a copy failure, preserves the
# non-zero layer result after its unmount attempts, and the second cleanup
# retries after the remaining CT restores before finally clearing the map.
export FAIL_INSTALL_MATCH="ct108/usr/local/sbin/hubinet-maint"
export HUBINET_OPS_FAKE_UNMOUNT_VMID=108
export HUBINET_OPS_FAKE_UNMOUNT_FAILURES=2
export HUBINET_OPS_FAKE_UNMOUNT_AFTER_MARKER="MARK install-failure"
rc="$(run_case restore_fails_while_mounted 108)"
[[ "$rc" != 0 ]]
restore_log="$TMP/restore_fails_while_mounted/actions.log"
restore_failure="$(first_line_after "$restore_log" 'MARK install-failure ')"
restore_mount="$(last_line_before "$restore_log" 'pct mount 108' "$restore_failure")"
restore_unmount_1="$(first_line_after "$restore_log" 'MARK unmount-failure 108 1' "$restore_failure")"
restore_unmount_2="$(first_line_after "$restore_log" 'MARK unmount-failure 108 2' "$restore_unmount_1")"
later_layer="$(first_line_after "$restore_log" 'pct mount 107' "$restore_unmount_2")"
final_cleanup_success="$(first_line_after "$restore_log" 'MARK unmount-success 108' "$later_layer")"
assert_ordered \
  "$restore_mount" "$restore_failure" "$restore_unmount_1" "$restore_unmount_2" \
  "$later_layer" "$final_cleanup_success"
grep -Fq 'Failed to restore stopped CT108 managed executor' \
  "$TMP/restore_fails_while_mounted/stderr"
grep -Fq 'Managed rollback restore failed for CT108' \
  "$TMP/restore_fails_while_mounted/stderr"
if [[ "$(awk -v after="$restore_failure" \
  'NR > after && $0 == "pct unmount 108" { count++ } END { print count+0 }' "$restore_log")" != 3 ]]; then
  echo "CT108 was not removed from mount tracking after the final cleanup succeeded" >&2
  exit 1
fi
unset FAIL_CAPABILITIES_VMID FAIL_INSTALL_MATCH
unset HUBINET_OPS_FAKE_UNMOUNT_VMID HUBINET_OPS_FAKE_UNMOUNT_FAILURES
unset HUBINET_OPS_FAKE_UNMOUNT_AFTER_MARKER

# 6. A partially modified stopped CT is deferred after the first cleanup
# exhausts both unmount attempts. Independent restores continue, the delayed
# cleanup succeeds, and only then is the deferred CT restored exactly once.
export FAIL_MV_MATCH="ct108/etc/hubinet-maint.json"
export HUBINET_OPS_FAKE_UNMOUNT_VMID=108
export HUBINET_OPS_FAKE_UNMOUNT_FAILURES=2
export HUBINET_OPS_FAKE_UNMOUNT_AFTER_MARKER="MARK partial-install-failure"
rc="$(run_case deferred_restore_after_delayed_unmount)"
[[ "$rc" != 0 ]]
deferred_log="$TMP/deferred_restore_after_delayed_unmount/actions.log"
partial_failure="$(first_line_after "$deferred_log" 'MARK partial-install-failure ')"
partial_executor_mv="$(last_line_before "$deferred_log" '.hubinet-maint.new -> ' "$partial_failure")"
deferred_unmount_1="$(first_line_after "$deferred_log" 'MARK unmount-failure 108 1' "$partial_failure")"
deferred_unmount_2="$(first_line_after "$deferred_log" 'MARK unmount-failure 108 2' "$deferred_unmount_1")"
independent_restore="$(first_line_after "$deferred_log" 'pct mount 107' "$deferred_unmount_2")"
delayed_unmount_success="$(first_line_after "$deferred_log" 'MARK unmount-success 108' "$independent_restore")"
deferred_restore_mount="$(first_line_after "$deferred_log" 'pct mount 108' "$delayed_unmount_success")"
deferred_executor_restore="$(first_line_after "$deferred_log" 'install ' "$deferred_restore_mount")"
deferred_profile_restore="$(first_line_after "$deferred_log" '/managed/ct108/hubinet-maint.json -> ' "$deferred_executor_restore")"
deferred_restore_unmount="$(first_line_after "$deferred_log" 'pct unmount 108' "$deferred_profile_restore")"
deferred_restore_unmount_success="$(first_line_after "$deferred_log" 'MARK unmount-success 108' "$deferred_restore_unmount")"
assert_ordered \
  "$partial_executor_mv" "$partial_failure" "$deferred_unmount_1" "$deferred_unmount_2" \
  "$independent_restore" "$delayed_unmount_success" "$deferred_restore_mount" \
  "$deferred_executor_restore" "$deferred_profile_restore" \
  "$deferred_restore_unmount" "$deferred_restore_unmount_success"
grep -Fq 'Deferring managed restore for CT108 because its tracked mount remains active; run: pct unmount 108' \
  "$TMP/deferred_restore_after_delayed_unmount/stderr"
if awk -v start="$partial_failure" -v finish="$delayed_unmount_success" \
  'NR > start && NR < finish && $0 == "pct mount 108" { found=1 } END { exit !found }' \
  "$deferred_log"; then
  echo "Deferred CT108 was remounted before its delayed unmount succeeded" >&2
  exit 1
fi
if [[ "$(grep -Ec 'install .*/managed/ct108/hubinet-maint -> .*/ct108/usr/local/sbin/hubinet-maint$' "$deferred_log")" != 1 ||
      "$(grep -Ec 'install .*/managed/ct108/hubinet-maint.json -> .*/ct108/etc/hubinet-maint.json$' "$deferred_log")" != 1 ||
      "$(awk -v after="$delayed_unmount_success" \
        'NR > after && $0 == "pct mount 108" { count++ } END { print count+0 }' "$deferred_log")" != 1 ]]; then
  echo "Deferred CT108 managed restore was not executed exactly once" >&2
  exit 1
fi
grep -Fq 'old-executor-108' \
  "$TMP/deferred_restore_after_delayed_unmount/ct/ct108/usr/local/sbin/hubinet-maint"
grep -Fq 'old_profile' \
  "$TMP/deferred_restore_after_delayed_unmount/ct/ct108/etc/hubinet-maint.json"
unset FAIL_MV_MATCH HUBINET_OPS_FAKE_UNMOUNT_VMID
unset HUBINET_OPS_FAKE_UNMOUNT_FAILURES HUBINET_OPS_FAKE_UNMOUNT_AFTER_MARKER

# 7. Successful paths pair every stopped-CT mount with one successful unmount.
# Exact counts also prove that EXIT cleanup is a no-op once tracking is empty.
for vmid in $(seq 106 109); do
  mount_count="$(grep -c "^pct mount $vmid$" "$TMP/success/actions.log" || true)"
  unmount_count="$(grep -c "^pct unmount $vmid$" "$TMP/success/actions.log" || true)"
  unmount_success_count="$(grep -c "^MARK unmount-success $vmid$" "$TMP/success/actions.log" || true)"
  if [[ "$mount_count" != 2 ||
        "$unmount_count" != "$mount_count" ||
        "$unmount_success_count" != "$mount_count" ]]; then
    echo "CT$vmid success mount accounting is inconsistent: mounts=$mount_count unmounts=$unmount_count successes=$unmount_success_count" >&2
    exit 1
  fi
done

# 8. Persistent unmount failure exhausts both attempts in each cleanup pass,
# remains tracked across best-effort rollback, never mounts CT108 again, and
# exits non-zero with the exact manual-intervention command.
export FAIL_INSTALL_MATCH="ct108/usr/local/sbin/.hubinet-maint.new"
export HUBINET_OPS_FAKE_UNMOUNT_VMID=108
export HUBINET_OPS_FAKE_UNMOUNT_FAILURES=99
export HUBINET_OPS_FAKE_UNMOUNT_AFTER_MARKER="MARK install-failure"
rc="$(run_case unmount_fails_persistently)"
[[ "$rc" != 0 ]]
persistent_log="$TMP/unmount_fails_persistently/actions.log"
persistent_failure="$(first_line_after "$persistent_log" 'MARK install-failure ')"
if [[ "$(awk -v after="$persistent_failure" \
  'NR > after && $0 == "pct unmount 108" { count++ } END { print count+0 }' "$persistent_log")" != 4 ]]; then
  echo "Expected two CT108 unmount attempts in each of the two rollback cleanup passes" >&2
  exit 1
fi
if awk -v after="$persistent_failure" \
  'NR > after && $0 == "pct mount 108" { found=1 } END { exit !found }' "$persistent_log"; then
  echo "Rollback attempted pct mount 108 while CT108 was still tracked" >&2
  exit 1
fi
grep -Fq 'pct mount 107' "$persistent_log"
grep -Fq 'Deferring managed restore for CT108 because its tracked mount remains active; run: pct unmount 108' \
  "$TMP/unmount_fails_persistently/stderr"
grep -Fq 'Deferred managed restore for CT108 remains blocked by its tracked mount; run: pct unmount 108' \
  "$TMP/unmount_fails_persistently/stderr"
manual_message='CT108 remains mounted; manual intervention required: pct unmount 108'
if [[ "$(grep -Fc "$manual_message" "$TMP/unmount_fails_persistently/stderr")" != 2 ]]; then
  echo "Expected the exact CT108 manual-intervention message after both failed cleanup passes" >&2
  exit 1
fi
unset FAIL_INSTALL_MATCH HUBINET_OPS_FAKE_UNMOUNT_VMID
unset HUBINET_OPS_FAKE_UNMOUNT_FAILURES HUBINET_OPS_FAKE_UNMOUNT_AFTER_MARKER

echo "Mount tracking tests passed"
