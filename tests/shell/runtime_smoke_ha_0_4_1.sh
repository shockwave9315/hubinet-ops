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
  date
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

make_case() {
  local case_root="$1" safe_tool
  mkdir -p "$case_root/fake-bin" "$case_root/safe-bin" "$case_root/pycache"
  for safe_tool in "${SAFE_TOOL_NAMES[@]}"; do
    ln -s "${SAFE_TOOL_PATHS[$safe_tool]}" "$case_root/safe-bin/$safe_tool"
  done
  ln -s "$REAL_PYTHON" "$case_root/fake-bin/python3"

  cat > "$case_root/fake-bin/ssh" <<'SH'
#!/usr/bin/env bash
set -Eeuo pipefail

reject() {
  printf 'REJECTED_SSH\n' >> "$TEST_EVENT_LOG"
  echo "unsupported fake ssh invocation" >&2
  exit 64
}

[[ $# -eq 6 ]] || reject
[[ "$1" == -p && "$2" == 2222 ]] || reject
[[ "$3" == -i && "$4" == /root/.ssh/id_ed25519 ]] || reject
[[ "$5" == root@home-assistant.local ]] || reject
remote="$6"
if [[ "$remote" == *python3* ]]; then
  printf 'REMOTE_PYTHON_ATTEMPT\n' >> "$TEST_EVENT_LOG"
  reject
fi

case "$remote" in
  'cat /config/secrets.yaml')
    printf 'SECRET_READ\n' >> "$TEST_EVENT_LOG"
    if [[ "$TEST_FAIL_SECRET_READ" == 1 ]]; then
      echo "simulated SSH secret read failure" >&2
      exit 42
    fi
    while IFS= read -r line || [[ -n "$line" ]]; do
      printf '%s\n' "$line"
    done < "$TEST_SECRETS_PATH"
    ;;
  *'test -s /config/secrets.yaml'*'lovelace-mushroom/mushroom.js'*)
    printf 'PREFLIGHT\n' >> "$TEST_EVENT_LOG"
    ;;
  *'install -d -m 0700 '*'backup.complete'*)
    printf 'BACKUP\n' >> "$TEST_EVENT_LOG"
    ;;
  *'install -m 0644 /config/packages/hubinet_ops.yaml.new'*)
    printf 'INSTALL\n' >> "$TEST_EVENT_LOG"
    ;;
  *'ha core restart'*'ha core info --raw-json'*)
    printf 'RESTART\nWAIT_RUNNING\n' >> "$TEST_EVENT_LOG"
    ;;
  'rm -f /config/packages/hubinet_ops.yaml.new /config/dashboards/hubinet_ops.yaml.new')
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
case "$5|$6" in
  "$TEST_ROOT/home-assistant/packages/hubinet_ops.yaml|root@home-assistant.local:/config/packages/hubinet_ops.yaml.new")
    printf 'SCP_PACKAGE\n' >> "$TEST_EVENT_LOG"
    ;;
  "$TEST_ROOT/home-assistant/dashboards/hubinet_ops.yaml|root@home-assistant.local:/config/dashboards/hubinet_ops.yaml.new")
    printf 'SCP_DASHBOARD\n' >> "$TEST_EVENT_LOG"
    ;;
  *)
    reject
    ;;
esac
SH
  chmod +x "$case_root/fake-bin/ssh" "$case_root/fake-bin/scp"
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
  local name="$1" variant="$2" restart_requested="$3" fail_read="$4"
  local case_root="$TMP/$name" isolated_path rc
  mkdir -p "$case_root"
  make_case "$case_root"
  write_secrets "$variant" "$case_root/secrets.yaml"
  : > "$case_root/events.log"
  isolated_path="$case_root/fake-bin:$case_root/safe-bin"
  args=()
  [[ "$restart_requested" == 1 ]] && args+=(--restart-core)
  args+=(home-assistant.local 2222)

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
      TEST_SECRETS_PATH="$case_root/secrets.yaml" \
      TEST_FAIL_SECRET_READ="$fail_read" \
      "$case_root/safe-bin/bash" \
      "$ROOT/deploy/install-ha-0.4.1-from-pve.sh" "${args[@]}"
  ) > "$case_root/stdout" 2> "$case_root/stderr"
  rc=$?
  set -e
  printf '%s\n' "$rc" > "$case_root/rc"
}

assert_no_remote_python() {
  local case_root="$1"
  if grep -Fq REMOTE_PYTHON_ATTEMPT "$case_root/events.log"; then
    fail "installer attempted to run python3 on Home Assistant"
  fi
}

assert_before_mutation() {
  local case_root="$1"
  if grep -Eq '^(BACKUP|SCP_PACKAGE|SCP_DASHBOARD|INSTALL|RESTART|WAIT_RUNNING)$' \
    "$case_root/events.log"; then
    fail "preflight failure reached backup, SCP, install, or restart"
  fi
}

run_case success-no-restart valid 0 0
[[ "$(cat "$TMP/success-no-restart/rc")" == 0 ]] ||
  fail "valid no-restart scenario failed"
[[ "$(grep -c '^SECRET_READ$' "$TMP/success-no-restart/events.log")" == 1 ]]
[[ "$(grep -c '^SCP_' "$TMP/success-no-restart/events.log")" == 2 ]]
if grep -Eq '^(RESTART|WAIT_RUNNING)$' "$TMP/success-no-restart/events.log"; then
  fail "no-restart scenario restarted Home Assistant Core"
fi
grep -Fq "Home Assistant Core was not restarted" "$TMP/success-no-restart/stdout"
assert_no_remote_python "$TMP/success-no-restart"

run_case success-restart valid 1 0
[[ "$(cat "$TMP/success-restart/rc")" == 0 ]] ||
  fail "valid restart scenario failed"
[[ "$(grep -c '^RESTART$' "$TMP/success-restart/events.log")" == 1 ]]
[[ "$(grep -c '^WAIT_RUNNING$' "$TMP/success-restart/events.log")" == 1 ]]
grep -Fq "Home Assistant Core was restarted" "$TMP/success-restart/stdout"
assert_no_remote_python "$TMP/success-restart"

run_case missing-secret missing 0 0
[[ "$(cat "$TMP/missing-secret/rc")" != 0 ]] ||
  fail "missing required secret was accepted"
grep -Fq hubinet_ops_force_stop_url "$TMP/missing-secret/stderr"
assert_before_mutation "$TMP/missing-secret"
assert_no_remote_python "$TMP/missing-secret"
if grep -Fq sandbox-secret-marker-041 \
  "$TMP/missing-secret/stdout" "$TMP/missing-secret/stderr"; then
  fail "secret marker leaked to installer output"
fi

run_case legacy-endpoints legacy 0 0
[[ "$(cat "$TMP/legacy-endpoints/rc")" != 0 ]] ||
  fail "legacy approve/reject endpoints were accepted"
grep -Fq approve-active "$TMP/legacy-endpoints/stderr"
grep -Fq reject-active "$TMP/legacy-endpoints/stderr"
assert_before_mutation "$TMP/legacy-endpoints"
assert_no_remote_python "$TMP/legacy-endpoints"

run_case secret-read-failure read-failure 0 1
[[ "$(cat "$TMP/secret-read-failure/rc")" != 0 ]] ||
  fail "SSH secret read failure was ignored"
grep -Fq "simulated SSH secret read failure" "$TMP/secret-read-failure/stderr"
assert_before_mutation "$TMP/secret-read-failure"
assert_no_remote_python "$TMP/secret-read-failure"

probe_root="$TMP/fail-closed-probes"
mkdir -p "$probe_root"
make_case "$probe_root"
: > "$probe_root/events.log"
probe_path="$probe_root/fake-bin:$probe_root/safe-bin"
if "$REAL_ENV" -i PATH="$probe_path" \
  TEST_ROOT="$ROOT" TEST_EVENT_LOG="$probe_root/events.log" \
  TEST_SECRETS_PATH="$ROOT/home-assistant/secrets.example.yaml" \
  TEST_FAIL_SECRET_READ=0 \
  "$probe_root/fake-bin/ssh" -p 2222 -i /root/.ssh/id_ed25519 \
  root@home-assistant.local "unknown remote command" >/dev/null 2>&1; then
  fail "fake SSH accepted an unknown remote command"
fi
if "$REAL_ENV" -i PATH="$probe_path" \
  TEST_ROOT="$ROOT" TEST_EVENT_LOG="$probe_root/events.log" \
  "$probe_root/fake-bin/scp" -P 2222 -i /root/.ssh/id_ed25519 \
  "$ROOT/README.md" \
  root@home-assistant.local:/config/packages/unexpected.yaml >/dev/null 2>&1; then
  fail "fake SCP accepted an unexpected source or destination"
fi
[[ "$(grep -c '^REJECTED_SSH$' "$probe_root/events.log")" == 1 ]]
[[ "$(grep -c '^REJECTED_SCP$' "$probe_root/events.log")" == 1 ]]

echo "0.4.1 HA installer runtime smoke: passed"
