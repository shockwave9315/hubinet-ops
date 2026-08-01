#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

[[ ${EUID} -eq 0 || ${HUBINET_OPS_TEST_MODE:-0} == 1 ]] || {
  echo "Run as root on PVE" >&2
  exit 1
}
[[ $# -eq 0 ]] || { echo "The 0.4.2 upgrade accepts no arguments" >&2; exit 2; }

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP_ROOT="${HUBINET_OPS_BACKUP_ROOT:-/root/hubinet-ops-backups}"
BACKUP="$BACKUP_ROOT/${STAMP}-before-0.4.2"
PVE_ROOT="${HUBINET_OPS_TEST_PVE_ROOT:-}"
AGENT_VMID=110
AGENT_BACKUP="$BACKUP/ct110"
ARCHIVE="${HUBINET_OPS_TEST_ARCHIVE:-/tmp/hubinet-ops-0.4.2-${STAMP}.tgz}"
HOSTD_CONFIG="${HUBINET_OPS_HOSTD_CONFIG:-/etc/hubinet-ops/hostd.json}"
HOSTD_ENV="${HUBINET_OPS_HOSTD_ENV:-/etc/hubinet-ops/hostd.env}"
HOSTD_SERVICE="${HUBINET_OPS_HOSTD_SERVICE:-hubinet-ops-hostd}"
VALIDATION_NOT_BEFORE=""
HOST_CONTROL_URL="${HUBINET_OPS_HOST_CONTROL_URL:-}"
TOKEN_STAGE=""
HOSTD_ENV_STAGE=""
changes_started=false
agent_backup_complete=false
agent_changes_started=false
hostd_was_active=false
hostd_was_enabled=false
declare -A mounted_cts=()
MOUNTED_PATH=""

pve_path() { printf '%s%s' "$PVE_ROOT" "$1"; }

pct_retry_129() {
  local attempt rc=0

  for attempt in 1 2 3; do
    if pct "$@"; then
      return 0
    else
      rc=$?
    fi

    [[ "$rc" -eq 129 ]] || return "$rc"

    if (( attempt < 3 )); then
      echo "pct received SIGHUP; retrying $((attempt + 1))/3: pct $*" >&2
      sleep "$attempt"
    fi
  done

  return "$rc"
}

HOST_DESTINATIONS=(
  /usr/local/sbin/hubinet-ops-host
  /usr/local/sbin/hubinet-ops-self-update
  /usr/local/lib/hubinet-ops/hubinet_ops_host_control.py
  /usr/local/lib/hubinet-ops/hubinet_ops_hostd.py
  /usr/local/lib/hubinet-ops/hubinet_ops_release.py
  /etc/systemd/system/hubinet-ops-hostd.service
  /etc/hubinet-ops/observation-vmids
  /etc/hubinet-ops/managed-vmids
  /etc/hubinet-ops/maintenance-vmids
  /etc/hubinet-ops/lifecycle-vmids
  /etc/hubinet-ops/host-control-vmids
  /etc/hubinet-ops/snapshot-create-vmids
  /etc/hubinet-ops/snapshot-restore-vmids
  /etc/hubinet-ops/snapshot-delete-vmids
  /etc/hubinet-ops/resource-types
  "$HOSTD_ENV"
)

required_source=(
  app
  config/config.example.yaml
  deploy/agent/backup-0.3.0.sh
  deploy/agent/restore-0.3.0.sh
  deploy/managed/hubinet-maint
  deploy/managed/profiles
  deploy/pve/hubinet-ops-host
  deploy/pve/hubinet-ops-self-update
  deploy/pve/hubinet_ops_host_control.py
  deploy/pve/hubinet_ops_hostd.py
  deploy/pve/hubinet_ops_release.py
  deploy/pve/hubinet-ops-hostd.service
  deploy/pve/observation-vmids
  deploy/pve/host-control-vmids
  deploy/pve/snapshot-create-vmids
  deploy/pve/snapshot-restore-vmids
  deploy/pve/snapshot-delete-vmids
  deploy/pve/resource-types
  scripts/validate_managed_profiles.py
  scripts/validate_pve_snapshot_policy.py
  scripts/migrate_config_0_4_0.py
  scripts/migrate_config_0_4_2.py
  scripts/validate_rollout_state_0_4_2.py
)
for item in "${required_source[@]}"; do
  [[ -e "$SOURCE_DIR/$item" ]] || { echo "Missing source artifact: $item" >&2; exit 1; }
done
[[ -s "$(pve_path "$HOSTD_CONFIG")" && -s "$(pve_path "$HOSTD_ENV")" ]] || {
  echo "Pre-provision root-owned hostd.json and hostd.env outside the repository" >&2
  exit 1
}
grep -Fq 'VERSION = "0.4.2"' "$SOURCE_DIR/app/mqtt.py"
grep -Fq 'VERSION = "0.4.1"' "$SOURCE_DIR/deploy/managed/hubinet-maint"
python3 "$SOURCE_DIR/scripts/validate_managed_profiles.py"
python3 "$SOURCE_DIR/scripts/validate_pve_snapshot_policy.py" \
  "$SOURCE_DIR/config/config.example.yaml" \
  --policy-dir "$SOURCE_DIR/deploy/pve"
python3 -m compileall -q "$SOURCE_DIR/app"
python3 -m py_compile \
  "$SOURCE_DIR/deploy/managed/hubinet-maint" \
  "$SOURCE_DIR/deploy/pve/hubinet_ops_host_control.py" \
  "$SOURCE_DIR/deploy/pve/hubinet_ops_hostd.py" \
  "$SOURCE_DIR/deploy/pve/hubinet_ops_release.py"
bash -n "$SOURCE_DIR/deploy/pve/hubinet-ops-host" \
  "$SOURCE_DIR/deploy/pve/hubinet-ops-self-update"
python3 -m json.tool "$(pve_path "$HOSTD_CONFIG")" >/dev/null
if [[ -z "$HOST_CONTROL_URL" ]]; then
  HOST_CONTROL_URL="$(python3 - "$(pve_path "$HOSTD_CONFIG")" <<'PY'
import json, sys
cfg=json.load(open(sys.argv[1], encoding="utf-8"))
bind=str(cfg["bind"])
if bind in {"0.0.0.0", "::"}: raise SystemExit("HUBINET_OPS_HOST_CONTROL_URL is required for wildcard bind")
host=f"[{bind}]" if ":" in bind else bind
print(f"http://{host}:{int(cfg.get('port',8741))}")
PY
)"
fi
installed_agent_health="$(pct exec "$AGENT_VMID" -- curl -fsS --max-time 5 http://127.0.0.1:8787/health)"
python3 - "$installed_agent_health" <<'PY'
import json, sys
data=json.loads(sys.argv[1])
if data.get("version") != "0.4.1":
    raise SystemExit("The 0.4.2 upgrade supports only an installed 0.4.1 backend")
PY
installed_hostd_health="$(curl -fsS --max-time 5 "${HOST_CONTROL_URL%/}/health")"
python3 - "$installed_hostd_health" <<'PY'
import json, sys
data=json.loads(sys.argv[1])
if data.get("status") != "ok" or data.get("version") != "0.4.1":
    raise SystemExit("The 0.4.2 upgrade supports only an installed 0.4.1 hostd")
PY
for vmid in $(seq 101 109); do
  python3 -m json.tool "$SOURCE_DIR/deploy/managed/profiles/ct${vmid}.json" >/dev/null
done

backup_host_file() {
  local destination="$1" actual backup_path
  actual="$(pve_path "$destination")"
  backup_path="$BACKUP/pve$destination"
  install -d -m 0700 "$(dirname "$backup_path")"
  if [[ -e "$actual" ]]; then
    cp -a "$actual" "$backup_path"
  else
    : > "$backup_path.absent"
  fi
}

restore_host_file() {
  local destination="$1" actual backup_path
  actual="$(pve_path "$destination")"
  backup_path="$BACKUP/pve$destination"
  if [[ -e "$backup_path" ]]; then
    install -d -m 0755 "$(dirname "$actual")"
    cp -a "$backup_path" "$actual"
  elif [[ -f "$backup_path.absent" ]]; then
    rm -f "$actual"
  fi
}

backup_running_ct_file() {
  local vmid="$1" remote="$2" backup_path="$3" probe
  probe="$(pct exec "$vmid" -- sh -c 'test -e "$1" && echo present || echo absent' sh "$remote")"
  if [[ "$probe" == present ]]; then
    pct pull "$vmid" "$remote" "$backup_path" >/dev/null
    [[ -s "$backup_path" ]]
  elif [[ "$probe" == absent ]]; then
    : > "$backup_path.absent"
  else
    echo "Ambiguous file probe for CT$vmid:$remote" >&2
    return 1
  fi
}

safe_mount() {
  local vmid="$1" output candidate
  MOUNTED_PATH=""
  if [[ "${mounted_cts[$vmid]:-false}" == true ]]; then
    echo "Refusing to mount CT$vmid again while it remains tracked; manual intervention required: pct unmount $vmid" >&2
    return 1
  fi
  if ! output="$(pct mount "$vmid")"; then
    return 1
  fi
  mounted_cts[$vmid]=true
  candidate="$(sed -n "s/.*'\([^']*\)'.*/\1/p" <<<"$output")"
  [[ -n "$candidate" && "$candidate" == /* && "$candidate" != / && -d "$candidate" ]] || {
    echo "Unsafe or unknown mountpoint for CT$vmid" >&2
    unmount_ct_with_retry "$vmid" || true
    return 1
  }
  MOUNTED_PATH="$candidate"
}

unmount_ct() {
  local vmid="$1"
  if [[ "${mounted_cts[$vmid]:-false}" == true ]]; then
    if pct_retry_129 unmount "$vmid" >/dev/null 2>&1; then
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
  local -a vmids=()
  if (($# > 0)); then
    vmids=("$@")
  else
    vmids=("${!mounted_cts[@]}")
  fi
  for vmid in "${vmids[@]}"; do
    if ! unmount_ct_with_retry "$vmid"; then
      cleanup_rc=1
    fi
  done
  return "$cleanup_rc"
}

backup_managed_ct() {
  local vmid="$1" status="$2" dir="$BACKUP/managed/ct$vmid" mountpoint
  install -d -m 0700 "$dir"
  printf '%s\n' "$status" > "$dir/runtime"
  if [[ "$status" == running ]]; then
    backup_running_ct_file "$vmid" /usr/local/sbin/hubinet-maint "$dir/hubinet-maint"
    backup_running_ct_file "$vmid" /etc/hubinet-maint.json "$dir/hubinet-maint.json"
  else
    if ! safe_mount "$vmid"; then
      return 1
    fi
    mountpoint="$MOUNTED_PATH"
    if [[ -f "$mountpoint/usr/local/sbin/hubinet-maint" ]]; then
      cp -a "$mountpoint/usr/local/sbin/hubinet-maint" "$dir/hubinet-maint"
    else
      : > "$dir/hubinet-maint.absent"
    fi
    if [[ -f "$mountpoint/etc/hubinet-maint.json" ]]; then
      cp -a "$mountpoint/etc/hubinet-maint.json" "$dir/hubinet-maint.json"
    else
      : > "$dir/hubinet-maint.json.absent"
    fi
    unmount_ct_with_retry "$vmid"
  fi
  : > "$dir/backup.complete"
}

restore_managed_ct() {
  local vmid="$1" dir="$BACKUP/managed/ct$vmid" status root="" layer_rc=0
  [[ -f "$dir/backup.complete" ]] || return 0
  if ! status="$(<"$dir/runtime")"; then
    echo "Failed to read managed rollback state for CT$vmid" >&2
    return 1
  fi
  if [[ "$status" == stopped ]]; then
    if ! safe_mount "$vmid"; then
      return 1
    fi
    root="$MOUNTED_PATH"
  fi
  if [[ "$status" == running ]]; then
    if [[ -s "$dir/hubinet-maint" ]]; then
      if ! pct_retry_129 push "$vmid" "$dir/hubinet-maint" /usr/local/sbin/hubinet-maint --perms 0755; then
        echo "Failed to restore CT$vmid:/usr/local/sbin/hubinet-maint" >&2
        layer_rc=1
      fi
    else
      if ! pct_retry_129 exec "$vmid" -- rm -f /usr/local/sbin/hubinet-maint; then
        echo "Failed to remove CT$vmid:/usr/local/sbin/hubinet-maint during rollback" >&2
        layer_rc=1
      fi
    fi
    if [[ -s "$dir/hubinet-maint.json" ]]; then
      if ! pct_retry_129 push "$vmid" "$dir/hubinet-maint.json" /etc/hubinet-maint.json --perms 0644; then
        echo "Failed to restore CT$vmid:/etc/hubinet-maint.json" >&2
        layer_rc=1
      fi
    else
      if ! pct_retry_129 exec "$vmid" -- rm -f /etc/hubinet-maint.json; then
        echo "Failed to remove CT$vmid:/etc/hubinet-maint.json during rollback" >&2
        layer_rc=1
      fi
    fi
    if ! pct_retry_129 exec "$vmid" -- rm -f /usr/local/sbin/.hubinet-maint.new /etc/.hubinet-maint.new.json; then
      echo "Failed to remove staged managed files from CT$vmid during rollback" >&2
      layer_rc=1
    fi
  else
    if ! install -d -m 0755 "$root/usr/local/sbin" "$root/etc"; then
      echo "Failed to prepare managed rollback directories for CT$vmid" >&2
      layer_rc=1
    fi
    if [[ -s "$dir/hubinet-maint" ]]; then
      if ! install -m 0755 "$dir/hubinet-maint" "$root/usr/local/sbin/hubinet-maint"; then
        echo "Failed to restore stopped CT$vmid managed executor" >&2
        layer_rc=1
      fi
    else
      if ! rm -f "$root/usr/local/sbin/hubinet-maint"; then
        echo "Failed to remove stopped CT$vmid managed executor during rollback" >&2
        layer_rc=1
      fi
    fi
    if [[ -s "$dir/hubinet-maint.json" ]]; then
      if ! install -m 0644 "$dir/hubinet-maint.json" "$root/etc/hubinet-maint.json"; then
        echo "Failed to restore stopped CT$vmid managed config" >&2
        layer_rc=1
      fi
    else
      if ! rm -f "$root/etc/hubinet-maint.json"; then
        echo "Failed to remove stopped CT$vmid managed config during rollback" >&2
        layer_rc=1
      fi
    fi
    if ! rm -f "$root/usr/local/sbin/.hubinet-maint.new" "$root/etc/.hubinet-maint.new.json"; then
      echo "Failed to remove staged managed files from stopped CT$vmid during rollback" >&2
      layer_rc=1
    fi
    if ! unmount_ct_with_retry "$vmid"; then
      layer_rc=1
    fi
  fi
  return "$layer_rc"
}

validate_capabilities() {
  local vmid="$1" payload="$2" profile="$SOURCE_DIR/deploy/managed/profiles/ct${vmid}.json"
  local executor_hash profile_hash
  executor_hash="$(sha256sum "$SOURCE_DIR/deploy/managed/hubinet-maint" | awk '{print $1}')"
  profile_hash="$(sha256sum "$profile" | awk '{print $1}')"
  python3 - "$payload" "$executor_hash" "$profile_hash" <<'PY'
import json, sys
payload, executor_hash, profile_hash = sys.argv[1:]
raw = json.loads(payload)
data = raw.get("data") if raw.get("ok") is True else None
required = {"capabilities", "inspect", "check-updates", "preflight", "update", "healthcheck", "repair", "verify"}
if not isinstance(data, dict): raise SystemExit("invalid capabilities response")
if data.get("version") != "0.4.1" or data.get("protocol_version") != 1: raise SystemExit("incompatible executor version/protocol")
if not required <= set(data.get("supported_actions") or []): raise SystemExit("missing executor actions")
if data.get("executor_sha256") != executor_hash: raise SystemExit("executor hash mismatch")
if data.get("profile_sha256") != profile_hash: raise SystemExit("profile hash mismatch")
if data.get("profile_validation_status") == "invalid": raise SystemExit("invalid profile")
PY
}

install_managed_ct() {
  local vmid="$1" status="$2" profile="$SOURCE_DIR/deploy/managed/profiles/ct${vmid}.json"
  local mountpoint payload
  if [[ "$status" == running ]]; then
    pct_retry_129 push "$vmid" "$SOURCE_DIR/deploy/managed/hubinet-maint" /usr/local/sbin/.hubinet-maint.new --perms 0755
    pct_retry_129 push "$vmid" "$profile" /etc/.hubinet-maint.new.json --perms 0644
    pct_retry_129 exec "$vmid" -- python3 -m py_compile /usr/local/sbin/.hubinet-maint.new
    pct_retry_129 exec "$vmid" -- install -m 0755 /usr/local/sbin/.hubinet-maint.new /usr/local/sbin/hubinet-maint
    pct_retry_129 exec "$vmid" -- install -m 0644 /etc/.hubinet-maint.new.json /etc/hubinet-maint.json
    pct_retry_129 exec "$vmid" -- rm -f /usr/local/sbin/.hubinet-maint.new /etc/.hubinet-maint.new.json
    payload="$(pct_retry_129 exec "$vmid" -- /usr/local/sbin/hubinet-maint capabilities)"
  else
    if ! safe_mount "$vmid"; then
      return 1
    fi
    mountpoint="$MOUNTED_PATH"
    install -d -m 0755 "$mountpoint/usr/local/sbin" "$mountpoint/etc"
    install -m 0755 "$SOURCE_DIR/deploy/managed/hubinet-maint" "$mountpoint/usr/local/sbin/.hubinet-maint.new"
    install -m 0644 "$profile" "$mountpoint/etc/.hubinet-maint.new.json"
    HUBINET_MAINT_CONFIG_PATH="$mountpoint/etc/.hubinet-maint.new.json" \
      python3 -m py_compile "$mountpoint/usr/local/sbin/.hubinet-maint.new"
    mv -f "$mountpoint/usr/local/sbin/.hubinet-maint.new" "$mountpoint/usr/local/sbin/hubinet-maint"
    mv -f "$mountpoint/etc/.hubinet-maint.new.json" "$mountpoint/etc/hubinet-maint.json"
    payload="$(HUBINET_MAINT_CONFIG_PATH="$mountpoint/etc/hubinet-maint.json" python3 "$mountpoint/usr/local/sbin/hubinet-maint" capabilities)"
    unmount_ct_with_retry "$vmid"
  fi
  validate_capabilities "$vmid" "$payload"
}

cleanup_secret_stages() {
  local failed=false
  if [[ -n "$TOKEN_STAGE" ]]; then
    rm -f -- "$TOKEN_STAGE" || failed=true
    TOKEN_STAGE=""
  fi
  if [[ -n "$HOSTD_ENV_STAGE" ]]; then
    rm -f -- "$HOSTD_ENV_STAGE" || failed=true
    HOSTD_ENV_STAGE=""
  fi
  [[ "$failed" == false ]]
}

rollback_all() {
  local rc="${1:-1}" failed=false
  local -a deferred_managed_restores=()
  local -a attempted_deferred_restores=()
  trap - ERR INT TERM EXIT
  echo "0.4.2 upgrade failed; restoring all modified layers" >&2
  if ! cleanup_mounts; then failed=true; fi
  if [[ "$agent_changes_started" == true && "$agent_backup_complete" == true ]]; then
    pct exec "$AGENT_VMID" -- bash -s -- "$AGENT_BACKUP" \
      < "$SOURCE_DIR/deploy/agent/restore-0.3.0.sh" || failed=true
  elif [[ "$agent_backup_complete" == true ]]; then
    pct exec "$AGENT_VMID" -- rm -f /etc/hubinet-ops/config.yaml.new /etc/hubinet-ops/agent.env.new || failed=true
    pct exec "$AGENT_VMID" -- systemctl start hubinet-ops || failed=true
  fi
  for vmid in $(seq 109 -1 101); do
    if [[ "${mounted_cts[$vmid]:-false}" == true ]]; then
      echo "Deferring managed restore for CT$vmid because its tracked mount remains active; run: pct unmount $vmid" >&2
      deferred_managed_restores+=("$vmid")
      failed=true
      continue
    fi
    if ! restore_managed_ct "$vmid"; then
      echo "Managed rollback restore failed for CT$vmid" >&2
      failed=true
    fi
  done
  if ! cleanup_mounts; then failed=true; fi
  for vmid in "${deferred_managed_restores[@]}"; do
    if [[ "${mounted_cts[$vmid]:-false}" == true ]]; then
      echo "Deferred managed restore for CT$vmid remains blocked by its tracked mount; run: pct unmount $vmid" >&2
      failed=true
      continue
    fi
    attempted_deferred_restores+=("$vmid")
    if ! restore_managed_ct "$vmid"; then
      echo "Deferred managed rollback restore failed for CT$vmid" >&2
      failed=true
    fi
  done
  if ((${#attempted_deferred_restores[@]} > 0)); then
    if ! cleanup_mounts "${attempted_deferred_restores[@]}"; then failed=true; fi
  fi
  if ((${#mounted_cts[@]} > 0)); then failed=true; fi
  for destination in "${HOST_DESTINATIONS[@]}"; do
    restore_host_file "$destination" || failed=true
  done
  systemctl daemon-reload || failed=true
  if [[ "$hostd_was_active" == true ]]; then
    systemctl restart "$HOSTD_SERVICE" || failed=true
  else
    systemctl stop "$HOSTD_SERVICE" >/dev/null 2>&1 || true
  fi
  if [[ "$hostd_was_enabled" == true ]]; then
    systemctl enable "$HOSTD_SERVICE" >/dev/null 2>&1 || failed=true
  else
    systemctl disable "$HOSTD_SERVICE" >/dev/null 2>&1 || true
  fi
  rm -f "$ARCHIVE"
  cleanup_secret_stages || failed=true
  [[ "$failed" == false ]] || rc=1
  exit "$rc"
}

exit_cleanup() {
  local rc="$1"
  trap - EXIT
  if ! cleanup_mounts; then
    rc=1
  fi
  rm -f "$ARCHIVE"
  if ! cleanup_secret_stages; then
    rc=1
  fi
  exit "$rc"
}

trap 'rollback_all $?' ERR
trap 'rollback_all 130' INT
trap 'rollback_all 143' TERM
trap 'exit_cleanup $?' EXIT

install -d -m 0700 "$BACKUP/pve" "$BACKUP/managed"
systemctl is-active --quiet "$HOSTD_SERVICE" && hostd_was_active=true || true
systemctl is-enabled --quiet "$HOSTD_SERVICE" && hostd_was_enabled=true || true
for destination in "${HOST_DESTINATIONS[@]}"; do backup_host_file "$destination"; done
for vmid in $(seq 101 109); do
  status="$(pct status "$vmid" | awk '{print $2}')"
  [[ "$status" == running || "$status" == stopped ]] || { echo "Unknown CT$vmid status" >&2; exit 1; }
  backup_managed_ct "$vmid" "$status"
done
agent_status="$(pct status "$AGENT_VMID" | awk '{print $2}')"
[[ "$agent_status" == running ]] || { echo "CT110 must already be running; no lifecycle was attempted" >&2; exit 1; }
pct exec "$AGENT_VMID" -- bash -s -- "$AGENT_BACKUP" < "$SOURCE_DIR/deploy/agent/backup-0.3.0.sh"
agent_backup_complete=true
pct pull "$AGENT_VMID" /etc/hubinet-ops/config.yaml "$BACKUP/agent-config.yaml" >/dev/null
pct pull "$AGENT_VMID" /etc/hubinet-ops/agent.env "$BACKUP/agent.env" >/dev/null
python3 "$SOURCE_DIR/scripts/migrate_config_0_4_2.py" \
  "$BACKUP/agent-config.yaml" "$BACKUP/agent-config-0.4.2.yaml" \
  --host-control-url "$HOST_CONTROL_URL"
python3 "$SOURCE_DIR/scripts/validate_pve_snapshot_policy.py" \
  "$BACKUP/agent-config-0.4.2.yaml" \
  --policy-dir "$SOURCE_DIR/deploy/pve"
PYTHONPATH="$SOURCE_DIR" python3 - "$BACKUP/agent-config-0.4.2.yaml" <<'PY'
import sys, yaml
from app.config import validate_config
with open(sys.argv[1], encoding="utf-8") as stream:
    validate_config(yaml.safe_load(stream))
PY
set -a
# The token remains outside the repository and is never printed.
source "$(pve_path "$HOSTD_ENV")"
set +a
[[ ${#HUBINET_OPS_HOSTD_TOKEN} -ge 32 ]] || { echo "Invalid hostd token" >&2; exit 1; }
if [[ -z ${HUBINET_OPS_HOSTD_BACKEND_TOKEN:-} ]]; then
  HUBINET_OPS_HOSTD_BACKEND_TOKEN="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
fi
if [[ -z ${HUBINET_OPS_HOSTD_UPDATE_TOKEN:-} ]]; then
  HUBINET_OPS_HOSTD_UPDATE_TOKEN="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
fi
if [[ -z ${HUBINET_OPS_HOSTD_RECOVERY_TOKEN:-} ]]; then
  HUBINET_OPS_HOSTD_RECOVERY_TOKEN="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
fi
[[ ${#HUBINET_OPS_HOSTD_BACKEND_TOKEN} -ge 32 ]] || { echo "Invalid hostd backend token" >&2; exit 1; }
[[ ${#HUBINET_OPS_HOSTD_UPDATE_TOKEN} -ge 32 ]] || { echo "Invalid hostd self-update token" >&2; exit 1; }
[[ ${#HUBINET_OPS_HOSTD_RECOVERY_TOKEN} -ge 32 ]] || { echo "Invalid hostd recovery token" >&2; exit 1; }
[[ "$HUBINET_OPS_HOSTD_TOKEN" != "$HUBINET_OPS_HOSTD_BACKEND_TOKEN" &&
   "$HUBINET_OPS_HOSTD_TOKEN" != "$HUBINET_OPS_HOSTD_UPDATE_TOKEN" &&
   "$HUBINET_OPS_HOSTD_TOKEN" != "$HUBINET_OPS_HOSTD_RECOVERY_TOKEN" &&
   "$HUBINET_OPS_HOSTD_BACKEND_TOKEN" != "$HUBINET_OPS_HOSTD_UPDATE_TOKEN" &&
   "$HUBINET_OPS_HOSTD_BACKEND_TOKEN" != "$HUBINET_OPS_HOSTD_RECOVERY_TOKEN" &&
   "$HUBINET_OPS_HOSTD_UPDATE_TOKEN" != "$HUBINET_OPS_HOSTD_RECOVERY_TOKEN" ]] || {
  echo "Hostd tokens for separate scopes must differ" >&2
  exit 1
}
HOSTD_ENV_STAGE="$(mktemp)"
grep -v -E '^HUBINET_OPS_HOSTD_(BACKEND_|UPDATE_|RECOVERY_)?TOKEN=' "$(pve_path "$HOSTD_ENV")" > "$HOSTD_ENV_STAGE" || true
printf 'HUBINET_OPS_HOSTD_TOKEN=%s\n' "$HUBINET_OPS_HOSTD_TOKEN" >> "$HOSTD_ENV_STAGE"
printf 'HUBINET_OPS_HOSTD_BACKEND_TOKEN=%s\n' "$HUBINET_OPS_HOSTD_BACKEND_TOKEN" >> "$HOSTD_ENV_STAGE"
printf 'HUBINET_OPS_HOSTD_UPDATE_TOKEN=%s\n' "$HUBINET_OPS_HOSTD_UPDATE_TOKEN" >> "$HOSTD_ENV_STAGE"
printf 'HUBINET_OPS_HOSTD_RECOVERY_TOKEN=%s\n' "$HUBINET_OPS_HOSTD_RECOVERY_TOKEN" >> "$HOSTD_ENV_STAGE"
chmod 0600 "$HOSTD_ENV_STAGE"
TOKEN_STAGE="$(mktemp)"
grep -v -E '^HUBINET_OPS_HOSTD_(BACKEND_|UPDATE_|RECOVERY_)?TOKEN=' "$BACKUP/agent.env" > "$TOKEN_STAGE" || true
printf 'HUBINET_OPS_HOSTD_BACKEND_TOKEN=%s\n' "$HUBINET_OPS_HOSTD_BACKEND_TOKEN" >> "$TOKEN_STAGE"
printf 'HUBINET_OPS_HOSTD_UPDATE_TOKEN=%s\n' "$HUBINET_OPS_HOSTD_UPDATE_TOKEN" >> "$TOKEN_STAGE"
chmod 0600 "$TOKEN_STAGE"
pct_retry_129 push "$AGENT_VMID" "$BACKUP/agent-config-0.4.2.yaml" /etc/hubinet-ops/config.yaml.new --perms 0640
pct_retry_129 push "$AGENT_VMID" "$TOKEN_STAGE" /etc/hubinet-ops/agent.env.new --perms 0600
rm -f "$TOKEN_STAGE"
TOKEN_STAGE=""
changes_started=true

install -m 0600 "$HOSTD_ENV_STAGE" "$(pve_path "$HOSTD_ENV")"
rm -f "$HOSTD_ENV_STAGE"
HOSTD_ENV_STAGE=""
install -d -m 0755 "$(pve_path /usr/local/lib/hubinet-ops)" "$(pve_path /usr/local/sbin)" "$(pve_path /etc/hubinet-ops)" "$(pve_path /etc/systemd/system)"
install -d -o root -g root -m 0700 "$(pve_path /var/lib/hubinet-ops-hostd)"
install -d -o root -g root -m 0755 \
  "$(pve_path /var/log/pve/tasks)" \
  "$(pve_path /run/lxc/lock)" \
  "$(pve_path /var/lib/lxc)" \
  "$(pve_path /etc/lvm/archive)" \
  "$(pve_path /etc/lvm/backup)"
install -m 0644 "$SOURCE_DIR/deploy/pve/hubinet_ops_host_control.py" "$(pve_path /usr/local/lib/hubinet-ops/hubinet_ops_host_control.py)"
install -m 0644 "$SOURCE_DIR/deploy/pve/hubinet_ops_hostd.py" "$(pve_path /usr/local/lib/hubinet-ops/hubinet_ops_hostd.py)"
install -m 0644 "$SOURCE_DIR/deploy/pve/hubinet_ops_release.py" "$(pve_path /usr/local/lib/hubinet-ops/hubinet_ops_release.py)"
install -m 0755 "$SOURCE_DIR/deploy/pve/hubinet-ops-host" "$(pve_path /usr/local/sbin/hubinet-ops-host)"
install -m 0755 "$SOURCE_DIR/deploy/pve/hubinet-ops-self-update" "$(pve_path /usr/local/sbin/hubinet-ops-self-update)"
install -m 0644 "$SOURCE_DIR/deploy/pve/hubinet-ops-hostd.service" "$(pve_path /etc/systemd/system/hubinet-ops-hostd.service)"
for name in observation-vmids managed-vmids maintenance-vmids lifecycle-vmids \
  host-control-vmids snapshot-create-vmids snapshot-restore-vmids \
  snapshot-delete-vmids resource-types; do
  install -m 0644 "$SOURCE_DIR/deploy/pve/$name" "$(pve_path /etc/hubinet-ops/$name)"
done
python3 -m py_compile \
  "$(pve_path /usr/local/lib/hubinet-ops/hubinet_ops_host_control.py)" \
  "$(pve_path /usr/local/lib/hubinet-ops/hubinet_ops_hostd.py)" \
  "$(pve_path /usr/local/lib/hubinet-ops/hubinet_ops_release.py)"
bash -n "$(pve_path /usr/local/sbin/hubinet-ops-host)" "$(pve_path /usr/local/sbin/hubinet-ops-self-update)"
systemctl daemon-reload
systemctl enable "$HOSTD_SERVICE" >/dev/null
systemctl restart "$HOSTD_SERVICE"

for vmid in $(seq 101 109); do
  status="$(<"$BACKUP/managed/ct$vmid/runtime")"
  install_managed_ct "$vmid" "$status"
done

wrapper="${HUBINET_OPS_TEST_WRAPPER_RUNNER:-$(pve_path /usr/local/sbin/hubinet-ops-host)}"
qemu_smoke="$(SSH_ORIGINAL_COMMAND='inspect 100' "$wrapper")"
lxc_smoke="$(SSH_ORIGINAL_COMMAND='inspect 106' "$wrapper")"
snapshot_smoke="$(SSH_ORIGINAL_COMMAND='list-snapshots 106' "$wrapper")"
python3 - "$qemu_smoke" "$lxc_smoke" "$snapshot_smoke" <<'PY'
import json, math, sys
qemu, lxc, snapshots = [json.loads(value) for value in sys.argv[1:]]
if qemu.get("ok") is not True or qemu.get("data", {}).get("resource_type") != "qemu": raise SystemExit("invalid VM100 inspect")
qdata = qemu["data"]
if qdata.get("adapter") != "haos" or qdata.get("qemu_status") not in {"running", "stopped"}: raise SystemExit("invalid VM100 QEMU state")
usage = qdata.get("cpu", {}).get("usage")
if qdata["qemu_status"] == "running" and (isinstance(usage, bool) or not isinstance(usage, (int, float)) or not math.isfinite(usage) or not 0 <= usage <= 1): raise SystemExit("running VM100 requires cluster CPU")
if qdata["qemu_status"] == "stopped" and usage is not None and not isinstance(usage, (int, float)): raise SystemExit("invalid stopped VM CPU")
if lxc.get("ok") is not True or lxc.get("data", {}).get("resource_type") != "lxc": raise SystemExit("invalid CT106 inspect")
if snapshots.get("ok") is not True or not isinstance(snapshots.get("data", {}).get("snapshots"), list): raise SystemExit("invalid read-only snapshot list")
PY

hostd_health="$(curl -fsS --max-time 5 "${HOST_CONTROL_URL%/}/health")"
python3 - "$hostd_health" <<'PY'
import json, sys
data=json.loads(sys.argv[1])
if data.get("status") != "ok" or data.get("version") != "0.4.2": raise SystemExit("hostd health failed")
PY

tar -C "$SOURCE_DIR" -czf "$ARCHIVE" app
pct_retry_129 push "$AGENT_VMID" "$ARCHIVE" /root/hubinet-ops-0.4.2.tgz --perms 0600
agent_changes_started=true
VALIDATION_NOT_BEFORE="$(date -u +%Y-%m-%dT%H:%M:%S+00:00)"
pct exec "$AGENT_VMID" -- bash -s <<'REMOTE_INSTALL_AGENT'
set -Eeuo pipefail
staging=/root/hubinet-ops-0.4.2
rm -rf "$staging"
install -d -m 0700 "$staging"
tar -xzf /root/hubinet-ops-0.4.2.tgz -C "$staging"
grep -Fq 'VERSION = "0.4.2"' "$staging/app/mqtt.py"
mv -f /etc/hubinet-ops/config.yaml.new /etc/hubinet-ops/config.yaml
mv -f /etc/hubinet-ops/agent.env.new /etc/hubinet-ops/agent.env
chown root:hubinetops /etc/hubinet-ops/config.yaml
chmod 0640 /etc/hubinet-ops/config.yaml
chown root:root /etc/hubinet-ops/agent.env
chmod 0600 /etc/hubinet-ops/agent.env
install -d -m 0750 -o root -g hubinetops /etc/hubinet-ops/keys
test -f /etc/hubinet-ops/keys/proxmox_ed25519
chown hubinetops:hubinetops /etc/hubinet-ops/keys/proxmox_ed25519
chmod 0600 /etc/hubinet-ops/keys/proxmox_ed25519
if [[ -f /etc/hubinet-ops/keys/proxmox_ed25519.pub ]]; then
  chown root:hubinetops /etc/hubinet-ops/keys/proxmox_ed25519.pub
  chmod 0644 /etc/hubinet-ops/keys/proxmox_ed25519.pub
fi
test -f /etc/hubinet-ops/ssh_known_hosts
chown root:hubinetops /etc/hubinet-ops/ssh_known_hosts
chmod 0640 /etc/hubinet-ops/ssh_known_hosts
rm -rf /opt/hubinet-ops/app
cp -a "$staging/app" /opt/hubinet-ops/app
chown -R hubinetops:hubinetops /opt/hubinet-ops/app
runuser -u hubinetops -- /opt/hubinet-ops/.venv/bin/python -m py_compile /opt/hubinet-ops/app/*.py
rm -rf "$staging" /root/hubinet-ops-0.4.2.tgz
systemctl start hubinet-ops
REMOTE_INSTALL_AGENT

validation_ok=false
for attempt in $(seq 1 "${HUBINET_OPS_VALIDATION_ATTEMPTS:-60}"); do
  health="$(pct exec "$AGENT_VMID" -- curl -fsS --max-time 3 http://127.0.0.1:8787/health 2>/dev/null || true)"
  if [[ "$health" == *'"version":"0.4.2"'* ]]; then
    states="$(pct exec "$AGENT_VMID" -- bash -s <<'REMOTE_READ_STATE'
set -Eeuo pipefail
set -a; source /etc/hubinet-ops/agent.env; set +a
curl -fsS --max-time 5 -H "Authorization: Bearer $HUBINET_OPS_API_TOKEN" http://127.0.0.1:8787/api/v1/state
REMOTE_READ_STATE
)" || states=""
    if printf '%s' "$states" | \
      python3 "$SOURCE_DIR/scripts/validate_rollout_state_0_4_2.py" \
        "$VALIDATION_NOT_BEFORE"
    then
      validation_ok=true
      break
    fi
  fi
  [[ "$attempt" -ge "${HUBINET_OPS_VALIDATION_ATTEMPTS:-60}" ]] || sleep "${HUBINET_OPS_VALIDATION_DELAY:-2}"
done
[[ "$validation_ok" == true ]] || { echo "Fresh 0.4.2 telemetry validation failed after all attempts" >&2; rollback_all 1; }

pct exec "$AGENT_VMID" -- /opt/hubinet-ops/.venv/bin/python - /var/lib/hubinet-ops/ops.db <<'PY'
import sqlite3, sys
with sqlite3.connect(sys.argv[1]) as db:
    version=db.execute("PRAGMA user_version").fetchone()[0]
if version != 400: raise SystemExit(f"unexpected database migration version {version}")
PY
snapshot_api="$(pct exec "$AGENT_VMID" -- bash -s <<'REMOTE_SNAPSHOT_LIST'
set -Eeuo pipefail
set -a; source /etc/hubinet-ops/agent.env; set +a
curl -fsS --max-time 5 -H "Authorization: Bearer $HUBINET_OPS_API_TOKEN" http://127.0.0.1:8787/api/v1/resources/106/snapshots
REMOTE_SNAPSHOT_LIST
)"
python3 - "$snapshot_api" <<'PY'
import json, sys
data=json.loads(sys.argv[1])
if not isinstance(data.get("snapshots"), list): raise SystemExit("backend snapshot list failed")
PY

trap - ERR INT TERM EXIT
rm -f "$ARCHIVE"
echo "Hubinet Ops 0.4.2 installed transactionally. Backup: $BACKUP"
echo "Validated hostd, /api/v1/state, fresh inventory 100-110, executor compatibility and read-only snapshot listing."
echo "No LXC/VM lifecycle, snapshot mutation, update or maintenance action was executed."
