#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

[[ ${EUID} -eq 0 || ${HUBINET_OPS_TEST_MODE:-0} == 1 ]] || {
  echo "Run as root on PVE" >&2
  exit 1
}
[[ $# -eq 1 ]] || { echo "Usage: $0 VMID" >&2; exit 1; }
VMID="$1"
[[ "$VMID" =~ ^(101|102|103|104|105|106|107|108|109)$ ]] || {
  echo "Managed executor installation is limited to CT101-CT109" >&2
  exit 1
}

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXECUTOR_SOURCE="$SOURCE_DIR/hubinet-maint"
CONFIG_SOURCE="$SOURCE_DIR/profiles/ct${VMID}.json"
[[ -s "$EXECUTOR_SOURCE" && -s "$CONFIG_SOURCE" ]] || {
  echo "Missing executor or CT profile" >&2
  exit 1
}
python3 -m py_compile "$EXECUTOR_SOURCE"
python3 -m json.tool "$CONFIG_SOURCE" >/dev/null
status="$(pct status "$VMID" | awk '{print $2}')"
[[ "$status" =~ ^(running|stopped)$ ]] || { echo "Unknown CT runtime status" >&2; exit 1; }

BACKUP="$(mktemp -d)"
NEW_EXECUTOR="/usr/local/sbin/.hubinet-maint.hubinet-ops-new"
NEW_CONFIG="/etc/.hubinet-maint.hubinet-ops-new.json"
had_executor=false
had_config=false
committed=false
content_installed=false
changes_started=false
mounted=false
mountpoint=""

unmount_ct_once() {
  if [[ "$mounted" == true ]]; then
    if pct unmount "$VMID" >/dev/null 2>&1; then
      mounted=false
      mountpoint=""
      return 0
    fi
    return 1
  fi
}

unmount_ct_with_retry() {
  [[ "$mounted" == true ]] || return 0
  if unmount_ct_once; then
    return 0
  fi
  echo "Failed to unmount CT$VMID; retrying pct unmount $VMID" >&2
  if unmount_ct_once; then
    return 0
  fi
  echo "CT$VMID remains mounted; manual intervention required: pct unmount $VMID" >&2
  return 1
}

cleanup() {
  local cleanup_rc=0
  if ! unmount_ct_with_retry; then
    cleanup_rc=1
  fi
  rm -rf "$BACKUP"
  return "$cleanup_rc"
}

rollback() {
  local rc="${1:-1}"
  trap - ERR INT TERM EXIT
  if [[ "$committed" != true && "$content_installed" != true && "$changes_started" == true ]]; then
    if [[ "$status" == "running" ]]; then
      if [[ "$had_executor" == true ]]; then
        pct push "$VMID" "$BACKUP/hubinet-maint" /usr/local/sbin/hubinet-maint --perms 0755 || true
      else
        pct exec "$VMID" -- rm -f /usr/local/sbin/hubinet-maint || true
      fi
      if [[ "$had_config" == true ]]; then
        pct push "$VMID" "$BACKUP/hubinet-maint.json" /etc/hubinet-maint.json --perms 0644 || true
      else
        pct exec "$VMID" -- rm -f /etc/hubinet-maint.json || true
      fi
      pct exec "$VMID" -- rm -f "$NEW_EXECUTOR" "$NEW_CONFIG" || true
    elif [[ -n "$mountpoint" ]]; then
      if [[ "$had_executor" == true ]]; then
        install -m 0755 "$BACKUP/hubinet-maint" "$mountpoint/usr/local/sbin/hubinet-maint" || true
      else
        rm -f "$mountpoint/usr/local/sbin/hubinet-maint" || true
      fi
      if [[ "$had_config" == true ]]; then
        install -m 0644 "$BACKUP/hubinet-maint.json" "$mountpoint/etc/hubinet-maint.json" || true
      else
        rm -f "$mountpoint/etc/hubinet-maint.json" || true
      fi
    fi
  fi
  if ! cleanup; then
    rc=1
  fi
  exit "$rc"
}
trap 'rollback $?' ERR
trap 'rollback 130' INT
trap 'rollback 143' TERM
trap cleanup EXIT

backup_running_file() {
  local remote_path="$1" backup_path="$2" absent_path="$3" probe
  case "$remote_path" in
    /usr/local/sbin/hubinet-maint|/etc/hubinet-maint.json) ;;
    *)
      echo "Refusing unsafe managed-file probe path: $remote_path" >&2
      return 1
      ;;
  esac
  if ! probe="$(pct exec "$VMID" -- sh -c \
    'if test -e "$1"; then printf "present\n"; else printf "absent\n"; fi' \
    sh "$remote_path")"; then
    echo "Could not determine whether CT$VMID:$remote_path exists: pct exec failed" >&2
    return 1
  fi
  case "$probe" in
    present)
      pct pull "$VMID" "$remote_path" "$backup_path" >/dev/null
      [[ -s "$backup_path" ]] || {
        echo "Backup of CT$VMID:$remote_path is empty or missing" >&2
        return 1
      }
      ;;
    absent)
      : > "$absent_path"
      ;;
    *)
      echo "Could not determine whether CT$VMID:$remote_path exists: ambiguous probe output" >&2
      return 1
      ;;
  esac
}

if [[ "$status" == "running" ]]; then
  backup_running_file /usr/local/sbin/hubinet-maint \
    "$BACKUP/hubinet-maint" "$BACKUP/hubinet-maint.absent"
  if [[ -s "$BACKUP/hubinet-maint" ]]; then
    had_executor=true
  fi
  backup_running_file /etc/hubinet-maint.json \
    "$BACKUP/hubinet-maint.json" "$BACKUP/hubinet-maint.json.absent"
  if [[ -s "$BACKUP/hubinet-maint.json" ]]; then
    had_config=true
  fi
  : > "$BACKUP/backup.complete"
  changes_started=true
  pct push "$VMID" "$EXECUTOR_SOURCE" "$NEW_EXECUTOR" --perms 0755
  pct push "$VMID" "$CONFIG_SOURCE" "$NEW_CONFIG" --perms 0644
  pct exec "$VMID" -- python3 -m py_compile "$NEW_EXECUTOR"
  pct exec "$VMID" -- mv -f "$NEW_EXECUTOR" /usr/local/sbin/hubinet-maint
  pct exec "$VMID" -- mv -f "$NEW_CONFIG" /etc/hubinet-maint.json
  pct exec "$VMID" -- /usr/local/sbin/hubinet-maint inspect
  content_installed=true
else
  mount_output="$(pct mount "$VMID")"
  mounted=true
  mountpoint="$(sed -n "s/.*'\([^']*\)'.*/\1/p" <<<"$mount_output")"
  [[ -n "$mountpoint" && "$mountpoint" == /* && -d "$mountpoint" ]] || {
    echo "Could not determine a safe CT mountpoint" >&2
    exit 1
  }
  if [[ -f "$mountpoint/usr/local/sbin/hubinet-maint" ]]; then
    cp -a "$mountpoint/usr/local/sbin/hubinet-maint" "$BACKUP/hubinet-maint"
    [[ -s "$BACKUP/hubinet-maint" ]]
    had_executor=true
  fi
  if [[ -f "$mountpoint/etc/hubinet-maint.json" ]]; then
    cp -a "$mountpoint/etc/hubinet-maint.json" "$BACKUP/hubinet-maint.json"
    [[ -s "$BACKUP/hubinet-maint.json" ]]
    had_config=true
  fi
  : > "$BACKUP/backup.complete"
  changes_started=true
  install -d -m 0755 "$mountpoint/usr/local/sbin" "$mountpoint/etc"
  install -m 0755 "$EXECUTOR_SOURCE" "$mountpoint$NEW_EXECUTOR"
  install -m 0644 "$CONFIG_SOURCE" "$mountpoint$NEW_CONFIG"
  mv -f "$mountpoint$NEW_EXECUTOR" "$mountpoint/usr/local/sbin/hubinet-maint"
  mv -f "$mountpoint$NEW_CONFIG" "$mountpoint/etc/hubinet-maint.json"
  content_installed=true
  echo "CT$VMID is stopped; safe inspect smoke is deferred until it is running."
fi

unmount_ct_with_retry
committed=true
trap - ERR INT TERM
echo "CT$VMID managed executor installed atomically; no update or service restart was run."
