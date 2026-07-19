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

restore_started=false
agent_stopped=false

restore_failed() {
  local rc="${1:-1}"
  trap - ERR INT TERM
  if [[ "$restore_started" == true ]]; then
    rm -f "$(root_path /var/lib/hubinet-ops/.ops.db.restore)"
  fi
  if [[ "$agent_stopped" == true ]]; then
    service_action start || echo "Failed to restart the existing hubinet-ops service after aborted restore" >&2
  fi
  exit "$rc"
}
trap 'restore_failed $?' ERR
trap 'restore_failed 130' INT
trap 'restore_failed 143' TERM

required_artifacts=(
  app requirements.txt hubinet-ops.service config.yaml agent.env backup.complete
)
for artifact in "${required_artifacts[@]}"; do
  if [[ "$artifact" == app ]]; then
    [[ -d "$backup/$artifact" ]] || {
      echo "Agent backup is incomplete: missing app/; preserving current agent files and ops.db" >&2
      restore_failed 1
    }
  else
    [[ -f "$backup/$artifact" ]] || {
      echo "Agent backup is incomplete: missing $artifact; preserving current agent files and ops.db" >&2
      restore_failed 1
    }
  fi
done
[[ -s "$backup/ops.db" ]] || {
  echo "Agent backup is incomplete: ops.db is missing or empty; preserving current ops.db and agent files" >&2
  restore_failed 1
}

stop_rc=0
service_action stop || stop_rc=$?
service_state_rc=0
service_state="$(systemctl is-active hubinet-ops 2>/dev/null)" || service_state_rc=$?
case "$service_state" in
  inactive|failed)
    [[ "$service_state_rc" -eq 3 ]] && agent_stopped=true
    ;;
esac
if [[ "$stop_rc" -ne 0 ]]; then
  echo "Cannot restore Hubinet Ops: systemctl stop hubinet-ops failed; no agent files or ops.db were modified" >&2
  restore_failed "$stop_rc"
fi
if [[ "$agent_stopped" != true ]]; then
  echo "Cannot restore Hubinet Ops: hubinet-ops is still active or its inactive state could not be confirmed; no agent files or ops.db were modified" >&2
  restore_failed 1
fi

database_dir="$(root_path /var/lib/hubinet-ops)"
database="$database_dir/ops.db"
database_stage="$database_dir/.ops.db.restore"
restore_started=true
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
