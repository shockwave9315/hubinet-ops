#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TMP_ROOT="$(mktemp -d)"
FAKE_BIN="$TMP_ROOT/bin"
AGENT_ROOT="$TMP_ROOT/agent"
LOG="$TMP_ROOT/systemctl.log"
STATE="$TMP_ROOT/service.state"
REAL_PYTHON="$(command -v python3 || command -v python)"
mkdir -p "$FAKE_BIN"
trap '/usr/bin/rm -rf "$TMP_ROOT"' EXIT
export HUBINET_OPS_TEST_MODE=1
export HUBINET_OPS_TEST_AGENT_ROOT="$AGENT_ROOT"
export HUBINET_OPS_AGENT_TEST_LOG="$LOG"
export HUBINET_OPS_AGENT_TEST_PYTHON="$REAL_PYTHON"
export HUBINET_OPS_AGENT_TEST_STATE="$STATE"
export HUBINET_OPS_TEST_RESTORE_HEALTH_ATTEMPTS=2
export HUBINET_OPS_TEST_RESTORE_HEALTH_DELAY=0
export PATH="$FAKE_BIN:$PATH"

cat > "$FAKE_BIN/python3" <<'SH'
#!/usr/bin/env bash
if [[ ${1:-} == -c && ${2:-} == 'import sqlite3' ]]; then
  exit 0
fi
if [[ ${1:-} == - ]]; then
  cat >/dev/null
  printf '<quick-check><%s>\n' "${2:-}" >> "$HUBINET_OPS_AGENT_TEST_LOG"
  exit 0
fi
exec "$HUBINET_OPS_AGENT_TEST_PYTHON" "$@"
SH
chmod +x "$FAKE_BIN/python3"

cat > "$FAKE_BIN/systemctl" <<'SH'
#!/usr/bin/env bash
printf '<%s>' "$@" >> "$HUBINET_OPS_AGENT_TEST_LOG"
printf '\n' >> "$HUBINET_OPS_AGENT_TEST_LOG"
case "${1:-}" in
  stop)
    if [[ ${HUBINET_OPS_AGENT_TEST_STOP_FAIL:-no} == yes ]]; then
      exit 1
    fi
    if [[ ${HUBINET_OPS_AGENT_TEST_STOP_STAYS_ACTIVE:-no} == yes ]]; then
      printf 'active\n' > "$HUBINET_OPS_AGENT_TEST_STATE"
    else
      printf 'inactive\n' > "$HUBINET_OPS_AGENT_TEST_STATE"
    fi
    ;;
  start)
    if [[ ${HUBINET_OPS_AGENT_TEST_START_STAYS_INACTIVE:-no} == yes ]]; then
      printf 'inactive\n' > "$HUBINET_OPS_AGENT_TEST_STATE"
    else
      printf 'active\n' > "$HUBINET_OPS_AGENT_TEST_STATE"
    fi
    ;;
  is-active)
    if [[ ${HUBINET_OPS_AGENT_TEST_STATE_UNCERTAIN:-no} == yes ]]; then
      printf 'unknown\n'
      exit 4
    fi
    state="$(cat "$HUBINET_OPS_AGENT_TEST_STATE")"
    printf '%s\n' "$state"
    [[ "$state" == active ]] || exit 3
    ;;
esac
SH
chmod +x "$FAKE_BIN/systemctl"
cat > "$FAKE_BIN/curl" <<'SH'
#!/usr/bin/env bash
printf '<curl>' >> "$HUBINET_OPS_AGENT_TEST_LOG"
printf '<%s>' "$@" >> "$HUBINET_OPS_AGENT_TEST_LOG"
printf '\n' >> "$HUBINET_OPS_AGENT_TEST_LOG"
if [[ ${HUBINET_OPS_AGENT_TEST_HEALTH_FAIL:-no} == yes ]]; then
  exit 22
fi
printf '{"status":"ok"}\n'
SH
chmod +x "$FAKE_BIN/curl"
cat > "$FAKE_BIN/chown" <<'SH'
#!/usr/bin/env bash
exit 0
SH
chmod +x "$FAKE_BIN/chown"
cat > "$FAKE_BIN/install" <<'SH'
#!/usr/bin/env bash
set -Eeuo pipefail
if [[ " $* " == *' -d '* ]]; then
  mkdir -p "${@: -1}"
  exit 0
fi
source_path="${@: -2:1}"
target_path="${@: -1}"
/usr/bin/cp "$source_path" "$target_path"
SH
chmod +x "$FAKE_BIN/install"

prepare_agent() {
  /usr/bin/rm -rf "$AGENT_ROOT"
  mkdir -p \
    "$AGENT_ROOT/opt/hubinet-ops/app" \
    "$AGENT_ROOT/etc/systemd/system" \
    "$AGENT_ROOT/etc/hubinet-ops" \
    "$AGENT_ROOT/var/lib/hubinet-ops" \
    "$AGENT_ROOT/root"
  printf 'app\n' > "$AGENT_ROOT/opt/hubinet-ops/app/main.py"
  printf 'requirements\n' > "$AGENT_ROOT/opt/hubinet-ops/requirements.txt"
  printf 'unit\n' > "$AGENT_ROOT/etc/systemd/system/hubinet-ops.service"
  printf 'config\n' > "$AGENT_ROOT/etc/hubinet-ops/config.yaml"
  printf 'env\n' > "$AGENT_ROOT/etc/hubinet-ops/agent.env"
  "$REAL_PYTHON" -c 'import sqlite3,sys; db=sqlite3.connect(sys.argv[1]); db.execute("create table safety(value)"); db.commit(); db.close()' \
    "$AGENT_ROOT/var/lib/hubinet-ops/ops.db"
  printf 'active\n' > "$STATE"
  : > "$LOG"
}

prepare_agent
backup_without="$TMP_ROOT/backup-without-sidecars"
bash "$ROOT/deploy/agent/backup-0.3.0.sh" "$backup_without"
[[ -s "$backup_without/ops.db" && -f "$backup_without/backup.complete" ]]
[[ ! -e "$backup_without/ops.db-wal" && ! -e "$backup_without/ops.db-shm" ]]
grep -Fq '<quick-check>' "$LOG"

prepare_agent
printf 'wal\n' > "$AGENT_ROOT/var/lib/hubinet-ops/ops.db-wal"
printf 'shm\n' > "$AGENT_ROOT/var/lib/hubinet-ops/ops.db-shm"
backup_with="$TMP_ROOT/backup-with-sidecars"
bash "$ROOT/deploy/agent/backup-0.3.0.sh" "$backup_with"
[[ -s "$backup_with/ops.db" && -s "$backup_with/ops.db-wal" && -s "$backup_with/ops.db-shm" ]]
[[ -f "$backup_with/backup.complete" ]]
printf 'replacement-db\n' > "$AGENT_ROOT/var/lib/hubinet-ops/ops.db"
bash "$ROOT/deploy/agent/restore-0.3.0.sh" "$backup_with"
cmp "$backup_with/ops.db" "$AGENT_ROOT/var/lib/hubinet-ops/ops.db"
cmp "$backup_with/ops.db-wal" "$AGENT_ROOT/var/lib/hubinet-ops/ops.db-wal"
cmp "$backup_with/ops.db-shm" "$AGENT_ROOT/var/lib/hubinet-ops/ops.db-shm"
grep -Fq '<start><hubinet-ops>' "$LOG"
grep -Fq '<curl><-fsS><--max-time><2><http://127.0.0.1:8787/health>' "$LOG"

prepare_agent
cat > "$FAKE_BIN/cp" <<'SH'
#!/usr/bin/env bash
if [[ " $* " == *'/var/lib/hubinet-ops/ops.db '* ]]; then
  exit 1
fi
exec /usr/bin/cp "$@"
SH
chmod +x "$FAKE_BIN/cp"
failed_backup="$TMP_ROOT/backup-copy-failure"
if bash "$ROOT/deploy/agent/backup-0.3.0.sh" "$failed_backup"; then
  echo 'Mandatory SQLite copy failure unexpectedly succeeded' >&2
  exit 1
fi
[[ ! -e "$failed_backup/backup.complete" ]]
[[ -s "$AGENT_ROOT/var/lib/hubinet-ops/ops.db" ]]
grep -Fq '<start><hubinet-ops>' "$LOG"
/usr/bin/rm -f "$FAKE_BIN/cp"

prepare_agent
printf 'current-db-must-survive\n' > "$AGENT_ROOT/var/lib/hubinet-ops/ops.db"
incomplete="$TMP_ROOT/incomplete-backup"
mkdir -p "$incomplete"
touch "$incomplete/backup.complete"
if bash "$ROOT/deploy/agent/restore-0.3.0.sh" "$incomplete"; then
  echo 'Restore with missing mandatory DB unexpectedly succeeded' >&2
  exit 1
fi
grep -Fxq 'current-db-must-survive' "$AGENT_ROOT/var/lib/hubinet-ops/ops.db"
if grep -Fq '<stop><hubinet-ops>' "$LOG"; then
  echo 'Restore stopped the agent before validating all mandatory artifacts' >&2
  exit 1
fi

assert_agent_unchanged() {
  local snapshot="$1"
  diff -ru "$snapshot/app" "$AGENT_ROOT/opt/hubinet-ops/app"
  cmp "$snapshot/requirements.txt" "$AGENT_ROOT/opt/hubinet-ops/requirements.txt"
  cmp "$snapshot/hubinet-ops.service" "$AGENT_ROOT/etc/systemd/system/hubinet-ops.service"
  cmp "$snapshot/config.yaml" "$AGENT_ROOT/etc/hubinet-ops/config.yaml"
  cmp "$snapshot/agent.env" "$AGENT_ROOT/etc/hubinet-ops/agent.env"
  cmp "$snapshot/ops.db" "$AGENT_ROOT/var/lib/hubinet-ops/ops.db"
}

prepare_agent
printf 'current-app\n' > "$AGENT_ROOT/opt/hubinet-ops/app/main.py"
printf 'current-requirements\n' > "$AGENT_ROOT/opt/hubinet-ops/requirements.txt"
printf 'current-unit\n' > "$AGENT_ROOT/etc/systemd/system/hubinet-ops.service"
printf 'current-config\n' > "$AGENT_ROOT/etc/hubinet-ops/config.yaml"
printf 'current-env\n' > "$AGENT_ROOT/etc/hubinet-ops/agent.env"
printf 'current-db\n' > "$AGENT_ROOT/var/lib/hubinet-ops/ops.db"
snapshot="$TMP_ROOT/current-agent-snapshot"
mkdir -p "$snapshot"
/usr/bin/cp -a "$AGENT_ROOT/opt/hubinet-ops/app" "$snapshot/app"
/usr/bin/cp "$AGENT_ROOT/opt/hubinet-ops/requirements.txt" "$snapshot/requirements.txt"
/usr/bin/cp "$AGENT_ROOT/etc/systemd/system/hubinet-ops.service" "$snapshot/hubinet-ops.service"
/usr/bin/cp "$AGENT_ROOT/etc/hubinet-ops/config.yaml" "$snapshot/config.yaml"
/usr/bin/cp "$AGENT_ROOT/etc/hubinet-ops/agent.env" "$snapshot/agent.env"
/usr/bin/cp "$AGENT_ROOT/var/lib/hubinet-ops/ops.db" "$snapshot/ops.db"

export HUBINET_OPS_AGENT_TEST_STOP_FAIL=yes
if bash "$ROOT/deploy/agent/restore-0.3.0.sh" "$backup_with"; then
  echo 'Restore unexpectedly succeeded after systemctl stop failure' >&2
  exit 1
fi
unset HUBINET_OPS_AGENT_TEST_STOP_FAIL
assert_agent_unchanged "$snapshot"
grep -Fq '<start><hubinet-ops>' "$LOG"

: > "$LOG"
export HUBINET_OPS_AGENT_TEST_STOP_STAYS_ACTIVE=yes
if bash "$ROOT/deploy/agent/restore-0.3.0.sh" "$backup_with"; then
  echo 'Restore unexpectedly succeeded while hubinet-ops remained active' >&2
  exit 1
fi
unset HUBINET_OPS_AGENT_TEST_STOP_STAYS_ACTIVE
assert_agent_unchanged "$snapshot"
grep -Fq '<is-active><hubinet-ops>' "$LOG"
grep -Fq '<start><hubinet-ops>' "$LOG"

: > "$LOG"
export HUBINET_OPS_AGENT_TEST_STATE_UNCERTAIN=yes
if bash "$ROOT/deploy/agent/restore-0.3.0.sh" "$backup_with"; then
  echo 'Restore unexpectedly succeeded with an uncertain post-stop service state' >&2
  exit 1
fi
unset HUBINET_OPS_AGENT_TEST_STATE_UNCERTAIN
assert_agent_unchanged "$snapshot"
grep -Fq '<start><hubinet-ops>' "$LOG"

prepare_agent
export HUBINET_OPS_AGENT_TEST_START_STAYS_INACTIVE=yes
if bash "$ROOT/deploy/agent/restore-0.3.0.sh" "$backup_with"; then
  echo 'Restore unexpectedly succeeded while the restored service stayed inactive' >&2
  exit 1
fi
unset HUBINET_OPS_AGENT_TEST_START_STAYS_INACTIVE
grep -Fq '<start><hubinet-ops>' "$LOG"
if grep -Fq '<curl>' "$LOG"; then
  echo 'Health endpoint was probed while the restored service was inactive' >&2
  exit 1
fi

prepare_agent
export HUBINET_OPS_AGENT_TEST_HEALTH_FAIL=yes
if bash "$ROOT/deploy/agent/restore-0.3.0.sh" "$backup_with"; then
  echo 'Restore unexpectedly succeeded while the health endpoint was unavailable' >&2
  exit 1
fi
unset HUBINET_OPS_AGENT_TEST_HEALTH_FAIL
[[ "$(grep -Fc '<curl>' "$LOG")" -eq 2 ]]

echo '0.3.0 agent SQLite backup and restore safety smoke passed'
