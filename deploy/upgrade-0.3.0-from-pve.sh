#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

[[ ${EUID} -eq 0 || ${HUBINET_OPS_TEST_MODE:-0} == 1 ]] || {
  echo "Run as root on PVE" >&2
  exit 1
}
[[ $# -eq 0 ]] || { echo "This production-inventory upgrade accepts no VMID overrides" >&2; exit 1; }

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAMP="$(date +%Y%m%d-%H%M%S)"
ARCHIVE="/tmp/hubinet-ops-0.3.0-${STAMP}.tgz"
HOST_BACKUP_BASE="${HUBINET_OPS_HOST_BACKUP_BASE:-/root/hubinet-ops-upgrade-backups}"
HOST_BACKUP="$HOST_BACKUP_BASE/${STAMP}-before-0.3.0"
AGENT_BACKUP="/root/hubinet-ops-backups/${STAMP}-before-0.3.0"
AGENT_VMID=110
host_root="${HUBINET_OPS_TEST_HOST_ROOT:-}"
if [[ -n "$host_root" && ${HUBINET_OPS_TEST_MODE:-0} != 1 ]]; then
  echo "Test host root override requires HUBINET_OPS_TEST_MODE=1" >&2
  exit 2
fi

host_path() {
  printf '%s%s' "$host_root" "$1"
}

complete=false
changes_started=false
agent_backup_complete=false
agent_backup_attempted=false
agent_archive_push_attempted=false
declare -A mounted_cts=()
mounted_path=""

unmount_ct() {
  local vmid="$1"
  if [[ "${mounted_cts[$vmid]:-false}" == true ]]; then
    if pct unmount "$vmid" >/dev/null 2>&1; then
      unset 'mounted_cts[$vmid]'
      return 0
    fi
    return 1
  fi
}

unmount_ct_with_retry() {
  local vmid="$1"
  [[ "${mounted_cts[$vmid]:-false}" == true ]] || return 0
  if unmount_ct "$vmid"; then
    return 0
  fi
  echo "Failed to unmount CT$vmid; retrying pct unmount $vmid" >&2
  if unmount_ct "$vmid"; then
    return 0
  fi
  echo "CT$vmid remains mounted; manual intervention required: pct unmount $vmid" >&2
  return 1
}

cleanup_mounts() {
  local vmid cleanup_rc=0
  for vmid in "${!mounted_cts[@]}"; do
    if ! unmount_ct_with_retry "$vmid"; then
      cleanup_rc=1
    fi
  done
  return "$cleanup_rc"
}

mount_ct() {
  local vmid="$1" purpose="$2" output candidate
  if ! output="$(pct mount "$vmid")"; then
    echo "Failed to mount CT$vmid for $purpose" >&2
    return 1
  fi
  mounted_cts[$vmid]=true
  candidate="$(sed -n "s/.*'\([^']*\)'.*/\1/p" <<<"$output")"
  if [[ -z "$candidate" || "$candidate" != /* || ! -d "$candidate" ]]; then
    echo "Could not determine a safe CT$vmid mountpoint for $purpose" >&2
    if ! unmount_ct_with_retry "$vmid"; then
      : # The mount flag remains set so the outer cleanup can try again.
    fi
    return 1
  fi
  mounted_path="$candidate"
}

required_source=(
  app requirements.txt config/config.example.yaml deploy/hubinet-ops.service
  deploy/pve/hubinet-ops-host deploy/pve/observation-vmids
  deploy/pve/managed-vmids deploy/pve/lifecycle-vmids deploy/pve/resource-types
  deploy/pve/maintenance-vmids
  deploy/managed/hubinet-maint deploy/managed/install-managed.sh
  deploy/agent/backup-0.3.0.sh deploy/agent/restore-0.3.0.sh
)
for path in "${required_source[@]}"; do
  [[ -e "$SOURCE_DIR/$path" ]] || { echo "Missing source artifact: $path" >&2; exit 1; }
done
grep -Fq 'VERSION = "0.3.0"' "$SOURCE_DIR/app/mqtt.py" || {
  echo "Source tree is not Hubinet Ops 0.3.0" >&2
  exit 1
}
[[ "$(sed '/^[[:space:]]*$/d' "$SOURCE_DIR/deploy/pve/lifecycle-vmids")" == "106" ]] || {
  echo "Lifecycle allowlist must contain exactly CT106" >&2
  exit 1
}
diff -u <(seq 100 110) "$SOURCE_DIR/deploy/pve/observation-vmids"
diff -u <(seq 101 109) "$SOURCE_DIR/deploy/pve/managed-vmids"
[[ "$(sed '/^[[:space:]]*$/d' "$SOURCE_DIR/deploy/pve/maintenance-vmids")" == "106" ]] || {
  echo "Maintenance allowlist must contain exactly CT106" >&2
  exit 1
}
python3 "$SOURCE_DIR/scripts/validate_yaml.py"
python3 -m py_compile "$SOURCE_DIR/deploy/managed/hubinet-maint"

for vmid in $(seq 100 110); do
  if [[ "$vmid" == 100 ]]; then
    raw="$(qm status "$vmid")"
  else
    raw="$(pct status "$vmid")"
  fi
  state="$(awk '{print $2}' <<<"$raw")"
  [[ "$state" =~ ^(running|stopped)$ ]] || {
    echo "Resource $vmid has an unsupported preflight state: $state" >&2
    exit 1
  }
done

backup_optional() {
  local source="$1" name="$2"
  if [[ -e "$source" ]]; then
    cp -a "$source" "$HOST_BACKUP/$name"
  else
    : > "$HOST_BACKUP/$name.absent"
  fi
}

restore_optional() {
  local target="$1" name="$2" mode="$3"
  if [[ -e "$HOST_BACKUP/$name" ]]; then
    install -m "$mode" "$HOST_BACKUP/$name" "$target"
  elif [[ -e "$HOST_BACKUP/$name.absent" ]]; then
    rm -f "$target"
  else
    echo "Host rollback artifact is missing: $name" >&2
    return 1
  fi
}

backup_managed() {
  local vmid="$1" target="$HOST_BACKUP/managed/$1" status mountpoint
  install -d -m 0700 "$target"
  status="$(pct status "$vmid" | awk '{print $2}')"
  if [[ "$status" == running ]]; then
    backup_running_managed_file \
      "$vmid" /usr/local/sbin/hubinet-maint \
      "$target/hubinet-maint" "$target/hubinet-maint.absent"
    backup_running_managed_file \
      "$vmid" /etc/hubinet-maint.json \
      "$target/hubinet-maint.json" "$target/hubinet-maint.json.absent"
    : > "$target/backup.complete"
    return
  fi
  [[ "$status" == stopped ]] || {
    echo "CT$vmid changed to unsupported state during managed backup: $status" >&2
    return 1
  }
  mount_ct "$vmid" backup
  mountpoint="$mounted_path"
  if [[ -f "$mountpoint/usr/local/sbin/hubinet-maint" ]]; then
    cp -a "$mountpoint/usr/local/sbin/hubinet-maint" "$target/hubinet-maint"
    [[ -s "$target/hubinet-maint" ]]
  else
    : > "$target/hubinet-maint.absent"
  fi
  if [[ -f "$mountpoint/etc/hubinet-maint.json" ]]; then
    cp -a "$mountpoint/etc/hubinet-maint.json" "$target/hubinet-maint.json"
    [[ -s "$target/hubinet-maint.json" ]]
  else
    : > "$target/hubinet-maint.json.absent"
  fi
  unmount_ct_with_retry "$vmid"
  : > "$target/backup.complete"
}

backup_running_managed_file() {
  local vmid="$1" remote_path="$2" backup_path="$3" absent_path="$4" probe
  case "$remote_path" in
    /usr/local/sbin/hubinet-maint|/etc/hubinet-maint.json) ;;
    *)
      echo "Refusing unsafe managed-file probe path: $remote_path" >&2
      return 1
      ;;
  esac
  if ! probe="$(pct exec "$vmid" -- sh -c \
    'if test -e "$1"; then printf "present\n"; else printf "absent\n"; fi' \
    sh "$remote_path")"; then
    echo "Could not determine whether CT$vmid:$remote_path exists: pct exec failed" >&2
    return 1
  fi
  case "$probe" in
    present)
      pct pull "$vmid" "$remote_path" "$backup_path" >/dev/null
      [[ -s "$backup_path" ]] || {
        echo "Backup of CT$vmid:$remote_path is empty or missing" >&2
        return 1
      }
      ;;
    absent)
      : > "$absent_path"
      ;;
    *)
      echo "Could not determine whether CT$vmid:$remote_path exists: ambiguous probe output" >&2
      return 1
      ;;
  esac
}

restore_managed() {
  local vmid="$1" source="$HOST_BACKUP/managed/$1" raw status mountpoint layer_rc=0
  [[ -f "$source/backup.complete" ]] || {
    echo "Managed rollback backup for CT$vmid is incomplete" >&2
    return 1
  }
  if ! raw="$(pct status "$vmid")"; then
    echo "Could not read CT$vmid status during rollback" >&2
    return 1
  fi
  status="$(awk '{print $2}' <<<"$raw")"
  if [[ "$status" == running ]]; then
    if [[ -f "$source/hubinet-maint" ]]; then
      if ! pct push "$vmid" "$source/hubinet-maint" /usr/local/sbin/hubinet-maint --perms 0755; then
        echo "Failed to restore CT$vmid:/usr/local/sbin/hubinet-maint" >&2
        layer_rc=1
      fi
    elif [[ -f "$source/hubinet-maint.absent" ]]; then
      if ! pct exec "$vmid" -- rm -f /usr/local/sbin/hubinet-maint; then
        echo "Failed to remove CT$vmid:/usr/local/sbin/hubinet-maint during rollback" >&2
        layer_rc=1
      fi
    else
      echo "Managed executor rollback artifact for CT$vmid is missing" >&2
      layer_rc=1
    fi
    if [[ -f "$source/hubinet-maint.json" ]]; then
      if ! pct push "$vmid" "$source/hubinet-maint.json" /etc/hubinet-maint.json --perms 0644; then
        echo "Failed to restore CT$vmid:/etc/hubinet-maint.json" >&2
        layer_rc=1
      fi
    elif [[ -f "$source/hubinet-maint.json.absent" ]]; then
      if ! pct exec "$vmid" -- rm -f /etc/hubinet-maint.json; then
        echo "Failed to remove CT$vmid:/etc/hubinet-maint.json during rollback" >&2
        layer_rc=1
      fi
    else
      echo "Managed config rollback artifact for CT$vmid is missing" >&2
      layer_rc=1
    fi
    return "$layer_rc"
  fi
  [[ "$status" == stopped ]] || {
    echo "CT$vmid has unsupported rollback state: $status" >&2
    return 1
  }
  if ! mount_ct "$vmid" rollback-restore; then
    return 1
  fi
  mountpoint="$mounted_path"
  if [[ -f "$source/hubinet-maint" ]]; then
    if ! install -m 0755 "$source/hubinet-maint" "$mountpoint/usr/local/sbin/hubinet-maint"; then
      echo "Failed to restore stopped CT$vmid managed executor" >&2
      layer_rc=1
    fi
  elif [[ -f "$source/hubinet-maint.absent" ]]; then
    if ! rm -f "$mountpoint/usr/local/sbin/hubinet-maint"; then
      echo "Failed to remove stopped CT$vmid managed executor" >&2
      layer_rc=1
    fi
  else
    echo "Managed executor rollback artifact for CT$vmid is missing" >&2
    layer_rc=1
  fi
  if [[ -f "$source/hubinet-maint.json" ]]; then
    if ! install -m 0644 "$source/hubinet-maint.json" "$mountpoint/etc/hubinet-maint.json"; then
      echo "Failed to restore stopped CT$vmid managed config" >&2
      layer_rc=1
    fi
  elif [[ -f "$source/hubinet-maint.json.absent" ]]; then
    if ! rm -f "$mountpoint/etc/hubinet-maint.json"; then
      echo "Failed to remove stopped CT$vmid managed config" >&2
      layer_rc=1
    fi
  else
    echo "Managed config rollback artifact for CT$vmid is missing" >&2
    layer_rc=1
  fi
  if ! unmount_ct_with_retry "$vmid"; then
    layer_rc=1
  fi
  return "$layer_rc"
}

declare -a rollback_errors=()

record_rollback_error() {
  rollback_errors+=("$1")
}

stop_agent_for_rollback() {
  local stop_failed=false probe
  if ! pct exec "$AGENT_VMID" -- systemctl stop hubinet-ops; then
    echo "Failed to stop CT$AGENT_VMID hubinet-ops before infrastructure rollback" >&2
    stop_failed=true
  fi
  if ! probe="$(pct exec "$AGENT_VMID" -- sh -c \
    'state=$(systemctl is-active hubinet-ops 2>/dev/null); rc=$?; printf "%s:%s\n" "$state" "$rc"; exit 0')"; then
    echo "Could not verify CT$AGENT_VMID hubinet-ops state before infrastructure rollback" >&2
    return 1
  fi
  case "$probe" in
    inactive:3|failed:3) ;;
    *)
      echo "CT$AGENT_VMID hubinet-ops was not confirmed inactive before infrastructure rollback" >&2
      return 1
      ;;
  esac
  [[ "$stop_failed" == false ]]
}

restore_all() {
  local rc="${1:-$?}" vmid target name mode
  trap - ERR INT TERM EXIT
  rollback_errors=()
  if [[ "$complete" == true ]]; then
    rm -f "$ARCHIVE"
    return 0
  fi
  echo "0.3.0 upgrade failed; restoring every modified layer" >&2
  if ! stop_agent_for_rollback; then
    record_rollback_error "CT$AGENT_VMID pre-rollback stop verification"
  fi
  if ! cleanup_mounts; then
    for vmid in "${!mounted_cts[@]}"; do
      record_rollback_error "CT$vmid unmount (run: pct unmount $vmid)"
    done
  fi
  if [[ -f "$HOST_BACKUP/backup.complete" ]]; then
    while read -r target name mode; do
      target="$(host_path "$target")"
      if ! restore_optional "$target" "$name" "$mode"; then
        record_rollback_error "host $name"
      fi
    done <<'HOST_RESTORE_TARGETS'
/usr/local/sbin/hubinet-ops-host hubinet-ops-host 0755
/etc/hubinet-ops/allowed-vmids allowed-vmids 0640
/etc/hubinet-ops/observation-vmids observation-vmids 0640
/etc/hubinet-ops/managed-vmids managed-vmids 0640
/etc/hubinet-ops/maintenance-vmids maintenance-vmids 0640
/etc/hubinet-ops/lifecycle-vmids lifecycle-vmids 0640
/etc/hubinet-ops/resource-types resource-types 0640
HOST_RESTORE_TARGETS
    for vmid in $(seq 101 109); do
      if ! restore_managed "$vmid"; then
        record_rollback_error "CT$vmid managed executor"
      fi
    done
  else
    record_rollback_error "verified host backup marker"
  fi
  if [[ "$agent_backup_complete" == true ]]; then
    if ! pct exec "$AGENT_VMID" -- bash -s -- "$AGENT_BACKUP" \
      < "$SOURCE_DIR/deploy/agent/restore-0.3.0.sh"; then
      record_rollback_error "CT$AGENT_VMID agent"
    fi
  else
    record_rollback_error "CT$AGENT_VMID agent backup"
  fi
  if ! cleanup_mounts; then
    for vmid in "${!mounted_cts[@]}"; do
      record_rollback_error "CT$vmid final unmount (run: pct unmount $vmid)"
    done
  fi
  rm -f "$ARCHIVE"
  if ((${#rollback_errors[@]} == 0)); then
    echo "Rollback completed" >&2
    [[ "$rc" -ne 0 ]] || rc=1
    exit "$rc"
  fi
  echo "ROLLBACK INCOMPLETE" >&2
  printf ' - %s\n' "${rollback_errors[@]}" >&2
  exit 1
}

abort_before_changes() {
  local rc="${1:-1}" cleanup_failed=false
  trap - ERR INT TERM EXIT
  if ! cleanup_mounts; then
    cleanup_failed=true
  fi
  rm -f "$ARCHIVE"
  if [[ "$agent_archive_push_attempted" == true ]]; then
    if ! pct exec "$AGENT_VMID" -- rm -f /root/hubinet-ops-0.3.0.tgz; then
      echo "Failed to remove the CT$AGENT_VMID staging archive after pre-change abort" >&2
      cleanup_failed=true
    fi
  fi
  if [[ "$agent_backup_attempted" == true ]]; then
    if ! pct exec "$AGENT_VMID" -- systemctl start hubinet-ops; then
      echo "Failed to restart the unchanged CT$AGENT_VMID agent after pre-change abort" >&2
      cleanup_failed=true
    fi
  fi
  if [[ "$cleanup_failed" == true ]]; then
    echo "Pre-change cleanup incomplete; see manual unmount/restart instructions above" >&2
  fi
  exit "$rc"
}

handle_failure() {
  local rc="${1:-1}"
  if [[ "$changes_started" == true ]]; then
    restore_all "$rc"
  else
    abort_before_changes "$rc"
  fi
}

fail_upgrade() {
  local message="$1"
  echo "$message" >&2
  handle_failure 1
}

exit_cleanup() {
  local rc="$1"
  trap - EXIT
  if ! cleanup_mounts; then
    rc=1
  fi
  rm -f "$ARCHIVE"
  exit "$rc"
}

trap 'handle_failure $?' ERR
trap 'handle_failure 130' INT
trap 'handle_failure 143' TERM
trap 'exit_cleanup $?' EXIT

install -d -m 0700 "$HOST_BACKUP/managed"
backup_optional "$(host_path /usr/local/sbin/hubinet-ops-host)" hubinet-ops-host
backup_optional "$(host_path /etc/hubinet-ops/allowed-vmids)" allowed-vmids
backup_optional "$(host_path /etc/hubinet-ops/observation-vmids)" observation-vmids
backup_optional "$(host_path /etc/hubinet-ops/managed-vmids)" managed-vmids
backup_optional "$(host_path /etc/hubinet-ops/maintenance-vmids)" maintenance-vmids
backup_optional "$(host_path /etc/hubinet-ops/lifecycle-vmids)" lifecycle-vmids
backup_optional "$(host_path /etc/hubinet-ops/resource-types)" resource-types
for vmid in $(seq 101 109); do backup_managed "$vmid"; done
: > "$HOST_BACKUP/backup.complete"

tar -C "$SOURCE_DIR" -czf "$ARCHIVE" \
  app requirements.txt config/config.example.yaml deploy/hubinet-ops.service
agent_backup_attempted=true
pct exec "$AGENT_VMID" -- bash -s -- "$AGENT_BACKUP" \
  < "$SOURCE_DIR/deploy/agent/backup-0.3.0.sh"
agent_backup_complete=true
agent_archive_push_attempted=true
pct push "$AGENT_VMID" "$ARCHIVE" /root/hubinet-ops-0.3.0.tgz --perms 0600
changes_started=true

pct exec "$AGENT_VMID" -- bash -s <<'REMOTE_INSTALL_AGENT'
set -Eeuo pipefail
staging=/root/hubinet-ops-0.3.0
rm -rf "$staging"
install -d -m 0700 "$staging"
tar -xzf /root/hubinet-ops-0.3.0.tgz -C "$staging"
rm -rf /opt/hubinet-ops/app
cp -a "$staging/app" /opt/hubinet-ops/app
cp -a "$staging/requirements.txt" /opt/hubinet-ops/requirements.txt
install -m 0644 "$staging/deploy/hubinet-ops.service" /etc/systemd/system/hubinet-ops.service
/opt/hubinet-ops/.venv/bin/python - "$staging/config/config.example.yaml" <<'PY'
from pathlib import Path
import sys, yaml

target = Path("/etc/hubinet-ops/config.yaml")
old = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
template = yaml.safe_load(Path(sys.argv[1]).read_text(encoding="utf-8")) or {}
for key in ("api", "executor", "scheduler", "mqtt", "home_assistant"):
    if key in old:
        template[key] = old[key]
# The read-only monitoring scheduler is separate from the legacy operator/job
# scheduler. Preserve its intervals, but enable observation scans intentionally.
old_scheduler = old.get("monitoring_scheduler") or old.get("scheduler") or {}
monitoring_scheduler = template.setdefault("monitoring_scheduler", {})
for key in ("scan_interval_minutes", "initial_scan_delay_seconds"):
    if key in old_scheduler:
        monitoring_scheduler[key] = old_scheduler[key]
monitoring_scheduler["enabled"] = True
target.write_text(yaml.safe_dump(template, sort_keys=False, allow_unicode=True), encoding="utf-8")
PY
chown root:hubinetops /etc/hubinet-ops/config.yaml
chmod 0640 /etc/hubinet-ops/config.yaml
chown -R hubinetops:hubinetops /opt/hubinet-ops/app /opt/hubinet-ops/requirements.txt
set -a
source /etc/hubinet-ops/agent.env
set +a
runuser -u hubinetops --preserve-environment -- env PYTHONPATH=/opt/hubinet-ops \
  /opt/hubinet-ops/.venv/bin/python -c 'from app.config import load_settings; from app.database import Database; s=load_settings(); Database(s.db_path)'
runuser -u hubinetops -- /opt/hubinet-ops/.venv/bin/python -m compileall -q /opt/hubinet-ops/app
systemctl daemon-reload
rm -rf "$staging" /root/hubinet-ops-0.3.0.tgz
REMOTE_INSTALL_AGENT

install -m 0755 "$SOURCE_DIR/deploy/pve/hubinet-ops-host" "$(host_path /usr/local/sbin/hubinet-ops-host)"
install -m 0640 "$SOURCE_DIR/deploy/pve/observation-vmids" "$(host_path /etc/hubinet-ops/observation-vmids)"
install -m 0640 "$SOURCE_DIR/deploy/pve/observation-vmids" "$(host_path /etc/hubinet-ops/allowed-vmids)"
install -m 0640 "$SOURCE_DIR/deploy/pve/managed-vmids" "$(host_path /etc/hubinet-ops/managed-vmids)"
install -m 0640 "$SOURCE_DIR/deploy/pve/maintenance-vmids" "$(host_path /etc/hubinet-ops/maintenance-vmids)"
install -m 0640 "$SOURCE_DIR/deploy/pve/lifecycle-vmids" "$(host_path /etc/hubinet-ops/lifecycle-vmids)"
install -m 0640 "$SOURCE_DIR/deploy/pve/resource-types" "$(host_path /etc/hubinet-ops/resource-types)"
for vmid in $(seq 101 109); do
  # git archive and extracted release bundles do not guarantee executable mode
  # preservation. Invoke the fixed installer through Bash explicitly.
  bash "$SOURCE_DIR/deploy/managed/install-managed.sh" "$vmid"
done

pct exec "$AGENT_VMID" -- systemctl start hubinet-ops
for _attempt in $(seq 1 30); do
  health="$(pct exec "$AGENT_VMID" -- curl -fsS --max-time 3 http://127.0.0.1:8787/health 2>/dev/null || true)"
  if [[ "$health" == *'"version":"0.3.0"'* ]]; then
    resources="$(pct exec "$AGENT_VMID" -- bash -s <<'REMOTE_CHECK_RESOURCES'
set -Eeuo pipefail
set -a
source /etc/hubinet-ops/agent.env
set +a
curl -fsS --max-time 5 -H "Authorization: Bearer $HUBINET_OPS_API_TOKEN" \
  http://127.0.0.1:8787/api/v1/resources
REMOTE_CHECK_RESOURCES
)"
    count="$(python3 -c 'import json,sys; print(len(json.load(sys.stdin)))' <<<"$resources")"
    if [[ "$count" != 11 ]]; then
      fail_upgrade "Resource inventory count is $count, expected 11"
    fi
    if ! cleanup_mounts; then
      fail_upgrade "Final CT unmount verification failed; upgrade cannot be marked complete"
    fi
    complete=true
    trap - ERR INT TERM
    rm -f "$ARCHIVE"
    echo "Hubinet Ops 0.3.0 installed transactionally. No resource management action was executed."
    echo "Backups: $HOST_BACKUP and CT110:$AGENT_BACKUP"
    exit 0
  fi
  sleep 2
done

echo "Agent 0.3.0 health validation failed" >&2
restore_all 1
