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

restore_failed() {
  local rc="${1:-1}"
  trap - ERR INT TERM
  rm -f "$(root_path /var/lib/hubinet-ops/.ops.db.restore)"
  service_action start || true
  exit "$rc"
}
trap 'restore_failed $?' ERR
trap 'restore_failed 130' INT
trap 'restore_failed 143' TERM

service_action stop 2>/dev/null || true
if [[ ! -f "$backup/backup.complete" ]]; then
  echo "Agent backup is incomplete; preserving current agent files and ops.db" >&2
  restore_failed 1
fi
if [[ ! -s "$backup/ops.db" ]]; then
  echo "Rollback DB backup is missing or empty; preserving current ops.db" >&2
  restore_failed 1
fi

database_dir="$(root_path /var/lib/hubinet-ops)"
database="$database_dir/ops.db"
database_stage="$database_dir/.ops.db.restore"
install -d -m 0750 "$database_dir"
install -m 0600 "$backup/ops.db" "$database_stage"
test -s "$database_stage"

rm -rf "$(root_path /opt/hubinet-ops/app)"
cp -a "$backup/app" "$(root_path /opt/hubinet-ops/app)"
cp -a "$backup/requirements.txt" "$(root_path /opt/hubinet-ops/requirements.txt)"
install -m 0644 "$backup/hubinet-ops.service" "$(root_path /etc/systemd/system/hubinet-ops.service)"
install -m 0640 -o root -g hubinetops \
  "$backup/config.yaml" "$(root_path /etc/hubinet-ops/config.yaml)"
install -m 0600 "$backup/agent.env" "$(root_path /etc/hubinet-ops/agent.env)"

mv -f "$database_stage" "$database"
for suffix in wal shm; do
  if [[ -f "$backup/ops.db-$suffix" ]]; then
    install -m 0600 "$backup/ops.db-$suffix" "$database-$suffix"
  else
    rm -f "$database-$suffix"
  fi
done
chown hubinetops:hubinetops "$database" "$database"-* 2>/dev/null || true
chown -R hubinetops:hubinetops \
  "$(root_path /opt/hubinet-ops/app)" \
  "$(root_path /opt/hubinet-ops/requirements.txt)"
systemctl daemon-reload
service_action start
trap - ERR INT TERM
