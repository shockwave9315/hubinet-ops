#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

[[ $# -eq 1 ]] || { echo "Usage: $0 BACKUP_DIRECTORY" >&2; exit 2; }
backup="$1"
root_prefix="${HUBINET_OPS_TEST_AGENT_ROOT:-}"
if [[ -n "$root_prefix" && ${HUBINET_OPS_TEST_MODE:-0} != 1 ]]; then
  echo "Test root override requires HUBINET_OPS_TEST_MODE=1" >&2
  exit 2
fi

root_path() {
  printf '%s%s' "$root_prefix" "$1"
}

service_action() {
  systemctl "$1" hubinet-ops
}

backup_failed() {
  local rc="${1:-1}"
  trap - ERR INT TERM
  rm -f "$backup/backup.complete"
  service_action start || true
  exit "$rc"
}
trap 'backup_failed $?' ERR
trap 'backup_failed 130' INT
trap 'backup_failed 143' TERM

install -d -m 0700 "$backup"
rm -f "$backup/backup.complete"
# This is the only SQLite writer and must be stopped before any database copy.
service_action stop

cp -a "$(root_path /opt/hubinet-ops/app)" "$backup/app"
cp -a "$(root_path /opt/hubinet-ops/requirements.txt)" "$backup/requirements.txt"
cp -a "$(root_path /etc/systemd/system/hubinet-ops.service)" "$backup/hubinet-ops.service"
cp -a "$(root_path /etc/hubinet-ops/config.yaml)" "$backup/config.yaml"
cp -a "$(root_path /etc/hubinet-ops/agent.env)" "$backup/agent.env"

database="$(root_path /var/lib/hubinet-ops/ops.db)"
test -s "$database"
cp -a "$database" "$backup/ops.db"
test -s "$backup/ops.db"
for suffix in wal shm; do
  optional="${database}-${suffix}"
  if [[ -f "$optional" ]]; then
    cp -a "$optional" "$backup/ops.db-$suffix"
  fi
done

python_bin=""
if command -v python3 >/dev/null 2>&1 && python3 -c 'import sqlite3' >/dev/null 2>&1; then
  python_bin=python3
elif [[ -x "$(root_path /opt/hubinet-ops/.venv/bin/python)" ]] \
  && "$(root_path /opt/hubinet-ops/.venv/bin/python)" -c 'import sqlite3' >/dev/null 2>&1; then
  python_bin="$(root_path /opt/hubinet-ops/.venv/bin/python)"
fi
if [[ -n "$python_bin" ]]; then
  "$python_bin" - "$backup/ops.db" <<'PY'
import sqlite3
import sys

with sqlite3.connect(sys.argv[1]) as connection:
    result = connection.execute("PRAGMA quick_check").fetchone()
if not result or result[0] != "ok":
    raise SystemExit(f"SQLite quick_check failed: {result!r}")
PY
fi

touch "$backup/backup.complete"
printf '%s\n' "$backup" > "$(root_path /root/hubinet-ops-last-upgrade-backup)"
trap - ERR INT TERM
