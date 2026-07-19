#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TMP_ROOT"' EXIT
MOCK_BIN="$TMP_ROOT/bin"
LOG_DIR="$TMP_ROOT/log"
mkdir -p "$MOCK_BIN" "$LOG_DIR"
export LOG_DIR

cat > "$MOCK_BIN/date" <<'SH'
#!/usr/bin/env bash
printf '%s\n' 20260719-180000
SH

cat > "$MOCK_BIN/python3" <<'SH'
#!/usr/bin/env bash
if [[ "${1:-}" == -c ]]; then
  cat >/dev/null
  printf '%s\n' 11
fi
exit 0
SH

cat > "$MOCK_BIN/sleep" <<'SH'
#!/usr/bin/env bash
exit 0
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
    if [[ "${1:-}" == curl ]]; then
      printf '%s\n' '{"status":"ok","version":"0.3.1"}'
      exit 0
    fi
    if [[ "${1:-}" == bash && "${2:-}" == -s ]]; then
      payload="$(cat)"
      if [[ "$payload" == *'staging=/root/hubinet-ops-0.3.1'* ]]; then
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
      if [[ "$payload" == *'/api/v1/resources'* ]]; then
        printf '[%s]\n' '0,1,2,3,4,5,6,7,8,9,10'
        exit 0
      fi
    fi
    ;;
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
export HUBINET_OPS_TEST_ARCHIVE="$TMP_ROOT/hubinet-ops-0.3.1.tgz"

bash "$ROOT/deploy/upgrade-0.3.1-from-pve.sh"
grep -Fxq backup "$LOG_DIR/agent-layers.log"
grep -Fxq install "$LOG_DIR/agent-layers.log"
[[ ! -e "$TMP_ROOT/hubinet-ops-0.3.1.tgz" ]]

: > "$LOG_DIR/agent-layers.log"
export FAIL_AGENT_INSTALL=1
if bash "$ROOT/deploy/upgrade-0.3.1-from-pve.sh" >/dev/null 2>&1; then
  echo "0.3.1 upgrade unexpectedly succeeded after agent install failure" >&2
  exit 1
fi
unset FAIL_AGENT_INSTALL
grep -Fxq backup "$LOG_DIR/agent-layers.log"
grep -Fxq install "$LOG_DIR/agent-layers.log"
grep -Fxq restore "$LOG_DIR/agent-layers.log"

bash "$ROOT/deploy/install-ha-0.3.1-from-pve.sh" ha.test 2222
grep -Fq 'hubinet_ops.dashboard.yaml' "$LOG_DIR/ssh.log"
grep -Fq '/config/dashboards/hubinet_ops.yaml.new' "$LOG_DIR/scp.log"

export FAIL_HA_CHECK=1
if bash "$ROOT/deploy/install-ha-0.3.1-from-pve.sh" ha.test 2222 >/dev/null 2>&1; then
  echo "0.3.1 HA installer unexpectedly succeeded after ha core check failure" >&2
  exit 1
fi
unset FAIL_HA_CHECK
grep -Fq 'cp -a '\''/config/backups/hubinet-ops/20260719-180000-before-0.3.1/secrets.yaml' "$LOG_DIR/ssh.log"

echo "0.3.1 patch installer rollback smoke passed"
