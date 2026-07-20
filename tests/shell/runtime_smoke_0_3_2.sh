#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TMP_ROOT"' EXIT
MOCK_BIN="$TMP_ROOT/bin"
LOG_DIR="$TMP_ROOT/log"
HOST_WRAPPER="$TMP_ROOT/installed-hubinet-ops-host"
mkdir -p "$MOCK_BIN" "$LOG_DIR"
export LOG_DIR

cat > "$MOCK_BIN/date" <<'SH'
#!/usr/bin/env bash
printf '%s\n' 20260720-180000
SH

cat > "$MOCK_BIN/python3" <<'SH'
#!/usr/bin/env bash
if [[ "${1:-}" == - ]]; then
  exec python "$@"
fi
exit 0
SH

cat > "$MOCK_BIN/sleep" <<'SH'
#!/usr/bin/env bash
exit 0
SH

cat > "$MOCK_BIN/install" <<'SH'
#!/usr/bin/env bash
set -Eeuo pipefail
args=("$@")
last="${args[${#args[@]}-1]}"
if [[ " $* " == *' -d '* ]]; then
  mkdir -p "$last"
else
  source="${args[${#args[@]}-2]}"
  cp "$source" "$last"
  chmod 0755 "$last"
fi
SH

cat > "$MOCK_BIN/pct" <<'SH'
#!/usr/bin/env bash
printf '<%s>' "$@" >> "$LOG_DIR/pct.log"
printf '\n' >> "$LOG_DIR/pct.log"
action="${1:-}"
shift || true
case "$action" in
  status)
    printf '%s\n' 'status: running'
    ;;
  push)
    ;;
  exec)
    vmid="${1:-}"
    shift || true
    [[ "${1:-}" == -- ]] && shift
    if [[ "${1:-}" == systemctl && "${2:-}" == start ]]; then
      printf '%s\n' start-unchanged >> "$LOG_DIR/agent-layers.log"
      exit 0
    fi
    if [[ "${1:-}" == curl ]]; then
      printf '%s\n' '{"status":"ok","version":"0.3.2"}'
      exit 0
    fi
    if [[ "${1:-}" == bash && "${2:-}" == -s ]]; then
      payload="$(cat)"
      if [[ "$payload" == *'staging=/root/hubinet-ops-0.3.2'* ]]; then
        printf '%s\n' install >> "$LOG_DIR/agent-layers.log"
        [[ ${FAIL_AGENT_INSTALL:-0} != 1 ]]
        exit
      fi
      if [[ "$payload" == *'restore_started=false'* ]]; then
        printf '%s\n' restore >> "$LOG_DIR/agent-layers.log"
        exit 0
      fi
      if [[ "$payload" == *'backup_failed()'* ]]; then
        printf '%s\n' backup >> "$LOG_DIR/agent-layers.log"
        exit 0
      fi
      if [[ "$payload" == *'/api/v1/states'* ]]; then
        printf '%s\n' '/api/v1/states' >> "$LOG_DIR/pct.log"
        printf '%s\n' '{"version":"0.3.2","resources":{"100":{"health_status":"healthy","cpu":{"usage_percent":3.05257}},"101":{},"102":{},"103":{},"104":{},"105":{},"106":{"health_status":"healthy"},"107":{},"108":{},"109":{},"110":{"health_status":"healthy","health_score":100}}}'
        exit 0
      fi
    fi
    ;;
esac
SH

cat > "$MOCK_BIN/wrapper-smoke" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$SSH_ORIGINAL_COMMAND" >> "$LOG_DIR/wrapper-smoke.log"
case "$SSH_ORIGINAL_COMMAND" in
  'inspect 100')
    if [[ ${BAD_WRAPPER_JSON:-0} == 1 ]]; then
      printf '%s\n' 'not-json'
    else
      printf '%s\n' '{"ok":true,"data":{"resource_type":"qemu","adapter":"haos","qemu_status":"running","cpu":{"usage":0.0305257}}}'
    fi
    ;;
  'inspect 106')
    printf '%s\n' '{"ok":true,"data":{"resource_type":"lxc","adapter":"apt","lxc_status":"running"}}'
    ;;
  *) exit 1 ;;
esac
SH

cat > "$MOCK_BIN/scp" <<'SH'
#!/usr/bin/env bash
printf '<%s>' "$@" >> "$LOG_DIR/scp.log"
printf '\n' >> "$LOG_DIR/scp.log"
SH

cat > "$MOCK_BIN/ssh" <<'SH'
#!/usr/bin/env bash
printf '<%s>' "$@" >> "$LOG_DIR/ssh.log"
printf '\n' >> "$LOG_DIR/ssh.log"
command_text="$*"
if [[ ${FAIL_HA_CHECK:-0} == 1 \
  && "$command_text" == *'install -m 0644 /config/packages/hubinet_ops.yaml.new'* \
  && ! -e "$LOG_DIR/ha-check-failed" ]]; then
  : > "$LOG_DIR/ha-check-failed"
  exit 1
fi
exit 0
SH

chmod +x "$MOCK_BIN"/*
export PATH="$MOCK_BIN:$PATH"
export HUBINET_OPS_TEST_MODE=1
export HUBINET_OPS_TEST_ARCHIVE="$TMP_ROOT/hubinet-ops-0.3.2.tgz"
export HUBINET_OPS_HOST_WRAPPER="$HOST_WRAPPER"
export HUBINET_OPS_BACKUP_ROOT="$TMP_ROOT/backups"
export HUBINET_OPS_TEST_WRAPPER_RUNNER="$MOCK_BIN/wrapper-smoke"

printf '%s\n' '#!/usr/bin/env bash' 'echo old-wrapper' > "$HOST_WRAPPER"
chmod 0755 "$HOST_WRAPPER"
bash "$ROOT/deploy/upgrade-0.3.2-from-pve.sh"
grep -Fxq backup "$LOG_DIR/agent-layers.log"
grep -Fxq install "$LOG_DIR/agent-layers.log"
grep -Fq 'cluster/resources' "$HOST_WRAPPER"
grep -Fxq 'inspect 100' "$LOG_DIR/wrapper-smoke.log"
grep -Fxq 'inspect 106' "$LOG_DIR/wrapper-smoke.log"
grep -Fq '/api/v1/states' "$LOG_DIR/pct.log"
[[ ! -e "$TMP_ROOT/hubinet-ops-0.3.2.tgz" ]]

printf '%s\n' '#!/usr/bin/env bash' 'echo old-wrapper' > "$HOST_WRAPPER"
: > "$LOG_DIR/agent-layers.log"
export BAD_WRAPPER_JSON=1
if bash "$ROOT/deploy/upgrade-0.3.2-from-pve.sh" >/dev/null 2>&1; then
  echo "0.3.2 upgrade unexpectedly succeeded with invalid wrapper JSON" >&2
  exit 1
fi
unset BAD_WRAPPER_JSON
grep -Fxq backup "$LOG_DIR/agent-layers.log"
grep -Fxq start-unchanged "$LOG_DIR/agent-layers.log"
if grep -Fxq install "$LOG_DIR/agent-layers.log"; then
  echo "Agent was replaced after the wrapper smoke had already failed" >&2
  exit 1
fi
grep -Fxq 'echo old-wrapper' "$HOST_WRAPPER"

printf '%s\n' '#!/usr/bin/env bash' 'echo old-wrapper' > "$HOST_WRAPPER"
: > "$LOG_DIR/agent-layers.log"
export FAIL_AGENT_INSTALL=1
if bash "$ROOT/deploy/upgrade-0.3.2-from-pve.sh" >/dev/null 2>&1; then
  echo "0.3.2 upgrade unexpectedly succeeded after agent install failure" >&2
  exit 1
fi
unset FAIL_AGENT_INSTALL
grep -Fxq backup "$LOG_DIR/agent-layers.log"
grep -Fxq install "$LOG_DIR/agent-layers.log"
grep -Fxq restore "$LOG_DIR/agent-layers.log"
grep -Fxq 'echo old-wrapper' "$HOST_WRAPPER"

bash "$ROOT/deploy/install-ha-0.3.2-from-pve.sh" ha.test 2222
grep -Fq 'hubinet_ops.dashboard.yaml' "$LOG_DIR/ssh.log"
grep -Fq '/config/dashboards/hubinet_ops.yaml.new' "$LOG_DIR/scp.log"

export FAIL_HA_CHECK=1
if bash "$ROOT/deploy/install-ha-0.3.2-from-pve.sh" ha.test 2222 >/dev/null 2>&1; then
  echo "0.3.2 HA installer unexpectedly succeeded after ha core check failure" >&2
  exit 1
fi
unset FAIL_HA_CHECK
grep -Fq 'cp -a '\''/config/backups/hubinet-ops/20260720-180000-before-0.3.2/secrets.yaml' "$LOG_DIR/ssh.log"

echo "0.3.2 agent, wrapper, and HA installer rollback smoke passed"
