#!/usr/bin/env bash
set -Eeuo pipefail

fail() {
  echo "0.4.1 HA installer runtime smoke failed: $*" >&2
  exit 1
}

[[ "${HUBINET_OPS_SYSTEM_SANDBOX:-0}" == 1 ]] ||
  fail "system sandbox marker missing"
[[ "$(id -u)" != 0 ]] || fail "sandbox user must be non-root"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf -- "$TMP"' EXIT

REAL_ENV="$(command -v env)"
REAL_PYTHON="${HUBINET_OPS_TEST_PYTHON:-$(command -v python3 || command -v python)}"
SAFE_TOOL_NAMES=(
  bash
  cat
  chmod
  cp
  dirname
  grep
  ln
  mkdir
  mktemp
  rm
)
declare -A SAFE_TOOL_PATHS=()
for safe_tool in "${SAFE_TOOL_NAMES[@]}"; do
  SAFE_TOOL_PATHS["$safe_tool"]="$(command -v "$safe_tool")" ||
    fail "required safe smoke tool is unavailable: $safe_tool"
done

"$REAL_PYTHON" \
  "$ROOT/scripts/validate_hermetic_shell_boundary.py" \
  "$ROOT/deploy/install-ha-0.4.1-from-pve.sh"

write_expected_remote_scripts() {
  local expected_dir="$1"
  mkdir -p "$expected_dir"

  printf '%s' 'cat /config/secrets.yaml' > "$expected_dir/secret-read"
  cat > "$expected_dir/preflight" <<'EOF'

  set -Eeuo pipefail
  test -s /config/secrets.yaml
  test -s /config/configuration.yaml
  grep -Rqs "lovelace-mushroom/mushroom.js" /config/.storage/lovelace_resources /config/configuration.yaml 2>/dev/null
EOF
  cat > "$expected_dir/backup" <<'EOF'
set -Eeuo pipefail
  install -d -m 0700 '/config/backups/hubinet-ops/20260102T030405Z-before-0.4.1'
  install -d -m 0755 /config/packages /config/dashboards
  cp -a /config/secrets.yaml '/config/backups/hubinet-ops/20260102T030405Z-before-0.4.1/secrets.yaml'
  cp -a /config/configuration.yaml '/config/backups/hubinet-ops/20260102T030405Z-before-0.4.1/configuration.yaml'
  [[ ! -f /config/packages/hubinet_ops.yaml ]] || cp -a /config/packages/hubinet_ops.yaml '/config/backups/hubinet-ops/20260102T030405Z-before-0.4.1/hubinet_ops.package.yaml'
  [[ ! -f /config/dashboards/hubinet_ops.yaml ]] || cp -a /config/dashboards/hubinet_ops.yaml '/config/backups/hubinet-ops/20260102T030405Z-before-0.4.1/hubinet_ops.dashboard.yaml'
  : > '/config/backups/hubinet-ops/20260102T030405Z-before-0.4.1/backup.complete'
EOF
  cat > "$expected_dir/install" <<'EOF'

  set -Eeuo pipefail
  install -m 0644 /config/packages/hubinet_ops.yaml.new /config/packages/hubinet_ops.yaml
  install -m 0644 /config/dashboards/hubinet_ops.yaml.new /config/dashboards/hubinet_ops.yaml
  rm -f /config/packages/hubinet_ops.yaml.new /config/dashboards/hubinet_ops.yaml.new
  chmod 600 /config/secrets.yaml
  ha core check
EOF
  cat > "$expected_dir/restart" <<'EOF'
set -Eeuo pipefail
    ha core restart
    for ((attempt=1; attempt<=1; attempt++)); do
      info="$(ha core info --raw-json 2>/dev/null || true)"
      if grep -Eq '"state"[[:space:]]*:[[:space:]]*"running"' <<<"$info"; then
        exit 0
      fi
      [[ "$attempt" -ge 1 ]] || sleep 0
    done
    echo 'Home Assistant Core did not return to running after restart' >&2
    exit 1
EOF
  cat > "$expected_dir/restore" <<'EOF'
set -Eeuo pipefail
      cp -a '/config/backups/hubinet-ops/20260102T030405Z-before-0.4.1/secrets.yaml' /config/secrets.yaml
      cp -a '/config/backups/hubinet-ops/20260102T030405Z-before-0.4.1/configuration.yaml' /config/configuration.yaml
      if [[ -f '/config/backups/hubinet-ops/20260102T030405Z-before-0.4.1/hubinet_ops.package.yaml' ]]; then
        cp -a '/config/backups/hubinet-ops/20260102T030405Z-before-0.4.1/hubinet_ops.package.yaml' /config/packages/hubinet_ops.yaml
      else
        rm -f /config/packages/hubinet_ops.yaml
      fi
      if [[ -f '/config/backups/hubinet-ops/20260102T030405Z-before-0.4.1/hubinet_ops.dashboard.yaml' ]]; then
        cp -a '/config/backups/hubinet-ops/20260102T030405Z-before-0.4.1/hubinet_ops.dashboard.yaml' /config/dashboards/hubinet_ops.yaml
      else
        rm -f /config/dashboards/hubinet_ops.yaml
      fi
      rm -f /config/packages/hubinet_ops.yaml.new /config/dashboards/hubinet_ops.yaml.new
      ha core check
EOF
  printf '%s' \
    'rm -f /config/packages/hubinet_ops.yaml.new /config/dashboards/hubinet_ops.yaml.new' \
    > "$expected_dir/cleanup"
}

make_case() {
  local case_root="$1" safe_tool
  mkdir -p \
    "$case_root/fake-bin" \
    "$case_root/safe-bin" \
    "$case_root/pycache" \
    "$case_root/expected"
  for safe_tool in "${SAFE_TOOL_NAMES[@]}"; do
    ln -s "${SAFE_TOOL_PATHS[$safe_tool]}" "$case_root/safe-bin/$safe_tool"
  done
  ln -s "$REAL_PYTHON" "$case_root/fake-bin/python3"
  write_expected_remote_scripts "$case_root/expected"

  cat > "$case_root/fake-bin/date" <<'SH'
#!/usr/bin/env bash
set -Eeuo pipefail
[[ "$#" -eq 2 && "$1" == -u && "$2" == +%Y%m%dT%H%M%SZ ]] || exit 64
printf '%s\n' 20260102T030405Z
SH

  cat > "$case_root/fake-bin/ssh" <<'SH'
#!/usr/bin/env bash
set -Eeuo pipefail

reject() {
  printf 'REJECTED_SSH\n' >> "$TEST_EVENT_LOG"
  echo "unsupported fake ssh invocation" >&2
  exit 64
}

load_expected() {
  local path="$1"
  EXPECTED_REMOTE=
  IFS= read -r -d '' EXPECTED_REMOTE < "$path" || true
  case "${path##*/}" in
    backup|restart|restore)
      EXPECTED_REMOTE="${EXPECTED_REMOTE%$'\n'}"
      ;;
  esac
}

[[ $# -eq 6 ]] || reject
[[ "$1" == -p && "$2" == 2222 ]] || reject
[[ "$3" == -i && "$4" == /root/.ssh/id_ed25519 ]] || reject
[[ "$5" == "$TEST_EXPECTED_SSH_TARGET" ]] || reject
case "$TEST_CASE_MODE" in
  success|secret-read-failure|install-failure|restart-failure-then-success|restart-failure-twice)
    ;;
  *)
    reject
    ;;
esac
remote="$6"
remote_kind=
for candidate in secret-read preflight backup install restart restore cleanup; do
  load_expected "$TEST_EXPECTED_DIR/$candidate"
  if [[ "$remote" == "$EXPECTED_REMOTE" ]]; then
    remote_kind="$candidate"
    break
  fi
done
[[ -n "$remote_kind" ]] || reject

case "$remote_kind" in
  secret-read)
    printf 'SECRET_READ\n' >> "$TEST_EVENT_LOG"
    if [[ "$TEST_CASE_MODE" == secret-read-failure ]]; then
      echo "simulated SSH secret read failure" >&2
      exit 42
    fi
    while IFS= read -r line || [[ -n "$line" ]]; do
      printf '%s\n' "$line"
    done < "$TEST_SECRETS_PATH"
    ;;
  preflight)
    printf 'PREFLIGHT\n' >> "$TEST_EVENT_LOG"
    ;;
  backup)
    printf 'BACKUP\n' >> "$TEST_EVENT_LOG"
    ;;
  install)
    printf 'INSTALL\n' >> "$TEST_EVENT_LOG"
    if [[ "$TEST_CASE_MODE" == install-failure ]]; then
      printf 'INITIAL_INSTALL_FAILURE\n' >> "$TEST_EVENT_LOG"
      echo "simulated install failure" >&2
      exit 43
    fi
    printf 'INITIAL_CHECK\n' >> "$TEST_EVENT_LOG"
    ;;
  restart)
    restart_count="$(< "$TEST_RESTART_COUNT")"
    restart_count=$((restart_count + 1))
    printf '%s\n' "$restart_count" > "$TEST_RESTART_COUNT"
    printf 'RESTART\nWAIT_RUNNING\n' >> "$TEST_EVENT_LOG"
    if [[ "$restart_count" -eq 1 && "$TEST_CASE_MODE" == restart-failure-then-success ]]; then
      printf 'INITIAL_RESTART_FAILURE\n' >> "$TEST_EVENT_LOG"
      echo "simulated initial restart failure" >&2
      exit 44
    fi
    if [[ "$restart_count" -eq 1 && "$TEST_CASE_MODE" == restart-failure-twice ]]; then
      printf 'INITIAL_RESTART_FAILURE\n' >> "$TEST_EVENT_LOG"
      echo "simulated initial restart failure" >&2
      exit 44
    fi
    if [[ "$restart_count" -eq 2 ]]; then
      printf 'ROLLBACK_RESTART\n' >> "$TEST_EVENT_LOG"
      if [[ "$TEST_CASE_MODE" == restart-failure-twice ]]; then
        printf 'ROLLBACK_RESTART_FAILURE\n' >> "$TEST_EVENT_LOG"
        echo "simulated rollback restart failure" >&2
        exit 45
      fi
    fi
    printf 'CORE_RUNNING\n' >> "$TEST_EVENT_LOG"
    ;;
  restore)
    printf '%s\n' \
      RESTORE_SECRETS \
      RESTORE_CONFIGURATION \
      RESTORE_PACKAGE \
      RESTORE_DASHBOARD \
      ROLLBACK_CHECK >> "$TEST_EVENT_LOG"
    ;;
  cleanup)
    printf 'CLEANUP\n' >> "$TEST_EVENT_LOG"
    ;;
  *)
    reject
    ;;
esac
SH

  cat > "$case_root/fake-bin/scp" <<'SH'
#!/usr/bin/env bash
set -Eeuo pipefail

reject() {
  printf 'REJECTED_SCP\n' >> "$TEST_EVENT_LOG"
  echo "unsupported fake scp invocation" >&2
  exit 64
}

[[ $# -eq 6 ]] || reject
[[ "$1" == -P && "$2" == 2222 ]] || reject
[[ "$3" == -i && "$4" == /root/.ssh/id_ed25519 ]] || reject
if [[ "$5" == "$TEST_ROOT/home-assistant/packages/hubinet_ops.yaml" &&
      "$6" == "$TEST_EXPECTED_SCP_TARGET:/config/packages/hubinet_ops.yaml.new" ]]; then
  printf 'SCP_PACKAGE\n' >> "$TEST_EVENT_LOG"
elif [[ "$5" == "$TEST_ROOT/home-assistant/dashboards/hubinet_ops.yaml" &&
        "$6" == "$TEST_EXPECTED_SCP_TARGET:/config/dashboards/hubinet_ops.yaml.new" ]]; then
  printf 'SCP_DASHBOARD\n' >> "$TEST_EVENT_LOG"
else
  reject
fi
printf '<%s>' "$@" >> "$TEST_SCP_ARGS_LOG"
printf '\n' >> "$TEST_SCP_ARGS_LOG"
SH
  chmod +x \
    "$case_root/fake-bin/date" \
    "$case_root/fake-bin/ssh" \
    "$case_root/fake-bin/scp"
}

write_secrets() {
  local variant="$1" destination="$2" line
  case "$variant" in
    valid|read-failure)
      cp "$ROOT/home-assistant/secrets.example.yaml" "$destination"
      ;;
    missing)
      while IFS= read -r line || [[ -n "$line" ]]; do
        [[ "$line" == hubinet_ops_force_stop_url:* ]] || printf '%s\n' "$line"
      done < "$ROOT/home-assistant/secrets.example.yaml" > "$destination"
      printf 'unused_secret_marker: sandbox-secret-marker-041\n' >> "$destination"
      ;;
    legacy)
      while IFS= read -r line || [[ -n "$line" ]]; do
        case "$line" in
          hubinet_ops_approve_url:*)
            printf 'hubinet_ops_approve_url: "http://agent/api/v1/plans/approve"\n'
            ;;
          hubinet_ops_reject_url:*)
            printf 'hubinet_ops_reject_url: "http://agent/api/v1/plans/reject"\n'
            ;;
          *)
            printf '%s\n' "$line"
            ;;
        esac
      done < "$ROOT/home-assistant/secrets.example.yaml" > "$destination"
      ;;
    *)
      fail "unknown secrets variant: $variant"
      ;;
  esac
}

run_case() {
  local name="$1" variant="$2" restart_requested="$3"
  local host="$4" scp_target="$5" case_mode="$6"
  local case_root="$TMP/$name" isolated_path rc
  local -a args=()
  mkdir -p "$case_root"
  make_case "$case_root"
  write_secrets "$variant" "$case_root/secrets.yaml"
  : > "$case_root/events.log"
  : > "$case_root/scp.args"
  printf '0\n' > "$case_root/restart.count"
  isolated_path="$case_root/fake-bin:$case_root/safe-bin"
  [[ "$restart_requested" == 1 ]] && args+=(--restart-core)
  args+=("$host" 2222)

  set +e
  (
    cd "$ROOT"
    "$REAL_ENV" -i \
      PATH="$isolated_path" \
      HOME=/workspace/home \
      TMPDIR=/tmp \
      PYTHONPYCACHEPREFIX="$case_root/pycache" \
      HUBINET_OPS_TEST_MODE=1 \
      HUBINET_OPS_HA_RESTART_ATTEMPTS=1 \
      HUBINET_OPS_HA_RESTART_DELAY=0 \
      TEST_ROOT="$ROOT" \
      TEST_EVENT_LOG="$case_root/events.log" \
      TEST_SCP_ARGS_LOG="$case_root/scp.args" \
      TEST_SECRETS_PATH="$case_root/secrets.yaml" \
      TEST_EXPECTED_DIR="$case_root/expected" \
      TEST_EXPECTED_SSH_TARGET="root@$host" \
      TEST_EXPECTED_SCP_TARGET="$scp_target" \
      TEST_RESTART_COUNT="$case_root/restart.count" \
      TEST_CASE_MODE="$case_mode" \
      "$case_root/safe-bin/bash" \
      "$ROOT/deploy/install-ha-0.4.1-from-pve.sh" "${args[@]}"
  ) > "$case_root/stdout" 2> "$case_root/stderr"
  rc=$?
  set -e
  printf '%s\n' "$rc" > "$case_root/rc"
}

assert_before_mutation() {
  local case_root="$1"
  if grep -Eq \
    '^(BACKUP|SCP_PACKAGE|SCP_DASHBOARD|INSTALL|RESTART|WAIT_RUNNING|RESTORE_)' \
    "$case_root/events.log"; then
    fail "preflight failure reached backup, SCP, install, restart, or restore"
  fi
}

assert_event_order() {
  local event_log="$1"
  shift
  local expected line wanted_index=0
  local -a wanted=("$@")
  while IFS= read -r line; do
    expected="${wanted[$wanted_index]:-}"
    if [[ -n "$expected" && "$line" == "$expected" ]]; then
      wanted_index=$((wanted_index + 1))
    fi
  done < "$event_log"
  [[ "$wanted_index" -eq "${#wanted[@]}" ]] ||
    fail "event order mismatch in $event_log; missing ${wanted[$wanted_index]}"
}

assert_success_target() {
  local name="$1" host="$2" scp_target="$3"
  local case_root="$TMP/$name"
  local package_source="$ROOT/home-assistant/packages/hubinet_ops.yaml"
  local dashboard_source="$ROOT/home-assistant/dashboards/hubinet_ops.yaml"
  local package_call dashboard_call

  run_case "$name" valid 0 "$host" "$scp_target" success
  [[ "$(< "$case_root/rc")" == 0 ]] || fail "$name failed"
  [[ "$(grep -c '^SCP_' "$case_root/events.log")" == 2 ]]
  [[ "$(grep -c '^<' "$case_root/scp.args")" == 2 ]]
  package_call="<-P><2222><-i></root/.ssh/id_ed25519><$package_source><$scp_target:/config/packages/hubinet_ops.yaml.new>"
  dashboard_call="<-P><2222><-i></root/.ssh/id_ed25519><$dashboard_source><$scp_target:/config/dashboards/hubinet_ops.yaml.new>"
  [[ "$(grep -Fxc "$package_call" "$case_root/scp.args")" == 1 ]]
  [[ "$(grep -Fxc "$dashboard_call" "$case_root/scp.args")" == 1 ]]
  [[ "$(grep -c '^SECRET_READ$' "$case_root/events.log")" == 1 ]]
  if grep -Eq '^(RESTART|WAIT_RUNNING)$' "$case_root/events.log"; then
    fail "$name restarted Home Assistant Core"
  fi
  grep -Fq "Home Assistant Core was not restarted" "$case_root/stdout"
}

assert_success_target \
  success-hostname \
  home-assistant.local \
  root@home-assistant.local
assert_success_target \
  success-ipv4 \
  192.168.4.100 \
  root@192.168.4.100
assert_success_target \
  success-ipv6 \
  2001:db8::100 \
  'root@[2001:db8::100]'
if grep -Fq \
  'root@2001:db8::100:/config/' \
  "$TMP/success-ipv6/scp.args"; then
  fail "IPv6 SCP target was not bracket-normalized"
fi
[[ "$(grep -c '^REJECTED_SSH$' "$TMP/success-ipv6/events.log")" == 0 ]]

run_case \
  success-restart \
  valid \
  1 \
  home-assistant.local \
  root@home-assistant.local \
  success
[[ "$(< "$TMP/success-restart/rc")" == 0 ]] ||
  fail "valid restart scenario failed"
[[ "$(grep -c '^RESTART$' "$TMP/success-restart/events.log")" == 1 ]]
[[ "$(grep -c '^WAIT_RUNNING$' "$TMP/success-restart/events.log")" == 1 ]]
grep -Fq "Home Assistant Core was restarted" "$TMP/success-restart/stdout"

run_case \
  missing-secret \
  missing \
  0 \
  home-assistant.local \
  root@home-assistant.local \
  success
[[ "$(< "$TMP/missing-secret/rc")" != 0 ]] ||
  fail "missing required secret was accepted"
grep -Fq hubinet_ops_force_stop_url "$TMP/missing-secret/stderr"
assert_before_mutation "$TMP/missing-secret"
if grep -Fq sandbox-secret-marker-041 \
  "$TMP/missing-secret/stdout" "$TMP/missing-secret/stderr"; then
  fail "secret marker leaked to installer output"
fi

run_case \
  legacy-endpoints \
  legacy \
  0 \
  home-assistant.local \
  root@home-assistant.local \
  success
[[ "$(< "$TMP/legacy-endpoints/rc")" != 0 ]] ||
  fail "legacy approve/reject endpoints were accepted"
grep -Fq approve-active "$TMP/legacy-endpoints/stderr"
grep -Fq reject-active "$TMP/legacy-endpoints/stderr"
assert_before_mutation "$TMP/legacy-endpoints"

run_case \
  secret-read-failure \
  read-failure \
  0 \
  home-assistant.local \
  root@home-assistant.local \
  secret-read-failure
[[ "$(< "$TMP/secret-read-failure/rc")" != 0 ]] ||
  fail "SSH secret read failure was ignored"
grep -Fq "simulated SSH secret read failure" \
  "$TMP/secret-read-failure/stderr"
assert_before_mutation "$TMP/secret-read-failure"

run_case \
  install-failure-no-restart \
  valid \
  0 \
  home-assistant.local \
  root@home-assistant.local \
  install-failure
case_root="$TMP/install-failure-no-restart"
[[ "$(< "$case_root/rc")" != 0 ]] ||
  fail "install failure returned success"
assert_event_order "$case_root/events.log" \
  BACKUP \
  SCP_PACKAGE \
  SCP_DASHBOARD \
  INSTALL \
  INITIAL_INSTALL_FAILURE \
  RESTORE_SECRETS \
  RESTORE_CONFIGURATION \
  RESTORE_PACKAGE \
  RESTORE_DASHBOARD \
  ROLLBACK_CHECK
[[ "$(grep -c '^RESTORE_' "$case_root/events.log")" == 4 ]]
[[ "$(grep -c '^ROLLBACK_CHECK$' "$case_root/events.log")" == 1 ]]
[[ "$(grep -c '^RESTART$' "$case_root/events.log")" == 0 ]]
[[ "$(grep -c '^ROLLBACK_RESTART$' "$case_root/events.log")" == 0 ]]
if grep -Fq "ROLLBACK INCOMPLETE" "$case_root/stderr"; then
  fail "successful no-restart rollback was reported incomplete"
fi

run_case \
  initial-restart-failure-rollback-restart-success \
  valid \
  1 \
  home-assistant.local \
  root@home-assistant.local \
  restart-failure-then-success
case_root="$TMP/initial-restart-failure-rollback-restart-success"
[[ "$(< "$case_root/rc")" != 0 ]] ||
  fail "initial restart failure returned success"
assert_event_order "$case_root/events.log" \
  BACKUP \
  SCP_PACKAGE \
  SCP_DASHBOARD \
  INSTALL \
  INITIAL_CHECK \
  RESTART \
  WAIT_RUNNING \
  INITIAL_RESTART_FAILURE \
  RESTORE_SECRETS \
  RESTORE_CONFIGURATION \
  RESTORE_PACKAGE \
  RESTORE_DASHBOARD \
  ROLLBACK_CHECK \
  RESTART \
  WAIT_RUNNING \
  ROLLBACK_RESTART \
  CORE_RUNNING
[[ "$(grep -c '^RESTART$' "$case_root/events.log")" == 2 ]]
[[ "$(grep -c '^WAIT_RUNNING$' "$case_root/events.log")" == 2 ]]
[[ "$(< "$case_root/restart.count")" == 2 ]]
if grep -Fq "ROLLBACK INCOMPLETE" "$case_root/stderr"; then
  fail "successful rollback restart was reported incomplete"
fi

run_case \
  initial-restart-failure-rollback-restart-failure \
  valid \
  1 \
  home-assistant.local \
  root@home-assistant.local \
  restart-failure-twice
case_root="$TMP/initial-restart-failure-rollback-restart-failure"
[[ "$(< "$case_root/rc")" != 0 ]] ||
  fail "double restart failure returned success"
assert_event_order "$case_root/events.log" \
  BACKUP \
  SCP_PACKAGE \
  SCP_DASHBOARD \
  INSTALL \
  INITIAL_CHECK \
  RESTART \
  WAIT_RUNNING \
  INITIAL_RESTART_FAILURE \
  RESTORE_SECRETS \
  RESTORE_CONFIGURATION \
  RESTORE_PACKAGE \
  RESTORE_DASHBOARD \
  ROLLBACK_CHECK \
  RESTART \
  WAIT_RUNNING \
  ROLLBACK_RESTART \
  ROLLBACK_RESTART_FAILURE
[[ "$(grep -c '^RESTART$' "$case_root/events.log")" == 2 ]]
[[ "$(grep -c '^WAIT_RUNNING$' "$case_root/events.log")" == 2 ]]
[[ "$(< "$case_root/restart.count")" == 2 ]]
[[ "$(grep -Fxc \
  'ROLLBACK INCOMPLETE: HA files were restored but Core did not return to running' \
  "$case_root/stderr")" == 1 ]]

probe_root="$TMP/fail-closed-probes"
mkdir -p "$probe_root"
make_case "$probe_root"
: > "$probe_root/events.log"
: > "$probe_root/scp.args"
printf '0\n' > "$probe_root/restart.count"
probe_path="$probe_root/fake-bin:$probe_root/safe-bin"
probe_ssh() {
  local remote="$1"
  if "$REAL_ENV" -i \
    PATH="$probe_path" \
    TEST_ROOT="$ROOT" \
    TEST_EVENT_LOG="$probe_root/events.log" \
    TEST_SCP_ARGS_LOG="$probe_root/scp.args" \
    TEST_SECRETS_PATH="$ROOT/home-assistant/secrets.example.yaml" \
    TEST_EXPECTED_DIR="$probe_root/expected" \
    TEST_EXPECTED_SSH_TARGET=root@home-assistant.local \
    TEST_EXPECTED_SCP_TARGET=root@home-assistant.local \
    TEST_RESTART_COUNT="$probe_root/restart.count" \
    TEST_CASE_MODE=success \
    "$probe_root/fake-bin/ssh" \
    -p 2222 -i /root/.ssh/id_ed25519 \
    root@home-assistant.local \
    "$remote" >/dev/null 2>&1; then
    fail "fake SSH accepted remote text outside its exact allowlist"
  fi
}

for remote_kind in \
  secret-read \
  preflight \
  backup \
  install \
  restart \
  restore \
  cleanup; do
  expected_remote=
  IFS= read -r -d '' expected_remote \
    < "$probe_root/expected/$remote_kind" || true
  case "$remote_kind" in
    backup|restart|restore)
      expected_remote="${expected_remote%$'\n'}"
      ;;
  esac
  probe_ssh "$expected_remote; echo unexpected"
  probe_ssh "echo unexpected;$expected_remote"
  probe_ssh "$expected_remote"$'\n'"echo unexpected"
  probe_ssh "$expected_remote"'$(echo unexpected)'
done
probe_ssh "unknown remote command"

if "$REAL_ENV" -i \
  PATH="$probe_path" \
  TEST_ROOT="$ROOT" \
  TEST_EVENT_LOG="$probe_root/events.log" \
  TEST_SCP_ARGS_LOG="$probe_root/scp.args" \
  TEST_EXPECTED_SCP_TARGET=root@home-assistant.local \
  "$probe_root/fake-bin/scp" \
  -P 2222 -i /root/.ssh/id_ed25519 \
  "$ROOT/README.md" \
  root@home-assistant.local:/config/packages/unexpected.yaml \
  >/dev/null 2>&1; then
  fail "fake SCP accepted an unexpected source or destination"
fi
[[ "$(grep -c '^REJECTED_SSH$' "$probe_root/events.log")" == 29 ]]
[[ "$(grep -c '^REJECTED_SCP$' "$probe_root/events.log")" == 1 ]]

echo "0.4.1 HA installer runtime smoke: passed"
