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
stop_attempted=false

restore_failed() {
  local rc="${1:-1}"
  trap - ERR INT TERM
  if [[ "$restore_started" == true ]]; then
    rm -f "$(root_path /var/lib/hubinet-ops/.ops.db.restore)"
  fi
  if [[ "$stop_attempted" == true ]]; then
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
stop_attempted=true
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
ssh_key_dir="$(root_path /etc/hubinet-ops/keys)"
ssh_private_key="$ssh_key_dir/proxmox_ed25519"
ssh_public_key="$ssh_private_key.pub"
ssh_known_hosts="$(root_path /etc/hubinet-ops/ssh_known_hosts)"
ssh_permissions_ok=true
install -d -m 0750 -o root -g hubinetops "$ssh_key_dir" || ssh_permissions_ok=false
[[ "$ssh_permissions_ok" == true && -f "$ssh_private_key" ]] || ssh_permissions_ok=false
if [[ "$ssh_permissions_ok" == true ]]; then
  chown hubinetops:hubinetops "$ssh_private_key" || ssh_permissions_ok=false
  chmod 0600 "$ssh_private_key" || ssh_permissions_ok=false
fi
if [[ "$ssh_permissions_ok" == true && -f "$ssh_public_key" ]]; then
  chown root:hubinetops "$ssh_public_key" || ssh_permissions_ok=false
  chmod 0644 "$ssh_public_key" || ssh_permissions_ok=false
fi
[[ "$ssh_permissions_ok" == true && -f "$ssh_known_hosts" ]] || ssh_permissions_ok=false
if [[ "$ssh_permissions_ok" == true ]]; then
  chown root:hubinetops "$ssh_known_hosts" || ssh_permissions_ok=false
  chmod 0640 "$ssh_known_hosts" || ssh_permissions_ok=false
fi
if [[ "$ssh_permissions_ok" != true ]]; then
  stop_attempted=false
  echo "Restored agent SSH permissions are incomplete; hubinet-ops remains stopped" >&2
  restore_failed 1
fi
systemctl daemon-reload
service_action start

health_attempts=10
health_delay=1
if [[ ${HUBINET_OPS_TEST_MODE:-0} == 1 ]]; then
  health_attempts="${HUBINET_OPS_TEST_RESTORE_HEALTH_ATTEMPTS:-2}"
  health_delay="${HUBINET_OPS_TEST_RESTORE_HEALTH_DELAY:-0}"
  [[ "$health_attempts" =~ ^[1-9][0-9]*$ && "$health_delay" =~ ^[0-9]+$ ]] || {
    echo "Invalid restore health retry test override" >&2
    restore_failed 1
  }
fi

restored_agent_healthy=false
for attempt in $(seq 1 "$health_attempts"); do
  active_rc=0
  active_state="$(systemctl is-active hubinet-ops 2>/dev/null)" || active_rc=$?
  if [[ "$active_rc" -eq 0 && "$active_state" == active ]]; then
    health_rc=0
    health="$(curl -fsS --max-time 2 http://127.0.0.1:8787/health 2>/dev/null)" || health_rc=$?
    if [[ "$health_rc" -eq 0 && -n "$health" ]]; then
      restored_agent_healthy=true
      break
    fi
  fi
  if [[ "$attempt" -lt "$health_attempts" ]]; then
    sleep "$health_delay"
  fi
done
if [[ "$restored_agent_healthy" != true ]]; then
  echo "Restored hubinet-ops service failed bounded active/health verification" >&2
  restore_failed 1
fi
trap - ERR INT TERM
