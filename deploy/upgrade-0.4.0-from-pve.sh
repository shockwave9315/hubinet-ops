#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

[[ ${EUID} -eq 0 || ${HUBINET_OPS_TEST_MODE:-0} == 1 ]] || {
  echo "Run as root on PVE" >&2
  exit 1
}
[[ $# -eq 0 ]] || { echo "The 0.4.0 upgrade accepts no arguments" >&2; exit 2; }

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP_ROOT="${HUBINET_OPS_BACKUP_ROOT:-/root/hubinet-ops-backups}"
BACKUP="$BACKUP_ROOT/${STAMP}-before-0.4.0"
PVE_ROOT="${HUBINET_OPS_TEST_PVE_ROOT:-}"
AGENT_VMID=110
AGENT_BACKUP="$BACKUP/ct110"
ARCHIVE="${HUBINET_OPS_TEST_ARCHIVE:-/tmp/hubinet-ops-0.4.0-${STAMP}.tgz}"
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

pve_path() { printf '%s%s' "$PVE_ROOT" "$1"; }

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
)
for item in "${required_source[@]}"; do
  [[ -e "$SOURCE_DIR/$item" ]] || { echo "Missing source artifact: $item" >&2; exit 1; }
done
[[ -s "$(pve_path "$HOSTD_CONFIG")" && -s "$(pve_path "$HOSTD_ENV")" ]] || {
  echo "Pre-provision root-owned hostd.json and hostd.env outside the repository" >&2
  exit 1
}
grep -Fq 'VERSION = "0.4.0"' "$SOURCE_DIR/app/mqtt.py"
grep -Fq 'VERSION = "0.4.0"' "$SOURCE_DIR/deploy/managed/hubinet-maint"
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
print(f"http://{bind}:{int(cfg.get('port',8741))}")
PY
)"
fi
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
  local vmid="$1" output mountpoint
  output="$(pct mount "$vmid")"
  mountpoint="$(sed -n "s/.*'\([^']*\)'.*/\1/p" <<<"$output")"
  [[ -n "$mountpoint" && "$mountpoint" == /* && "$mountpoint" != / && -d "$mountpoint" ]] || {
    echo "Unsafe or unknown mountpoint for CT$vmid" >&2
    return 1
  }
  printf '%s' "$mountpoint"
}

backup_managed_ct() {
  local vmid="$1" status="$2" dir="$BACKUP/managed/ct$vmid" mountpoint
  install -d -m 0700 "$dir"
  printf '%s\n' "$status" > "$dir/runtime"
  if [[ "$status" == running ]]; then
    backup_running_ct_file "$vmid" /usr/local/sbin/hubinet-maint "$dir/hubinet-maint"
    backup_running_ct_file "$vmid" /etc/hubinet-maint.json "$dir/hubinet-maint.json"
  else
    mountpoint="$(safe_mount "$vmid")"
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
    pct unmount "$vmid"
  fi
  : > "$dir/backup.complete"
}

restore_managed_ct() {
  local vmid="$1" dir="$BACKUP/managed/ct$vmid" status mountpoint root=""
  [[ -f "$dir/backup.complete" ]] || return 0
  status="$(<"$dir/runtime")"
  if [[ "$status" == stopped ]]; then
    mountpoint="$(safe_mount "$vmid")" || return 1
    root="$mountpoint"
  fi
  if [[ "$status" == running ]]; then
    if [[ -s "$dir/hubinet-maint" ]]; then
      pct push "$vmid" "$dir/hubinet-maint" /usr/local/sbin/hubinet-maint --perms 0755
    else
      pct exec "$vmid" -- rm -f /usr/local/sbin/hubinet-maint
    fi
    if [[ -s "$dir/hubinet-maint.json" ]]; then
      pct push "$vmid" "$dir/hubinet-maint.json" /etc/hubinet-maint.json --perms 0644
    else
      pct exec "$vmid" -- rm -f /etc/hubinet-maint.json
    fi
    pct exec "$vmid" -- rm -f /usr/local/sbin/.hubinet-maint.new /etc/.hubinet-maint.new.json
  else
    install -d -m 0755 "$root/usr/local/sbin" "$root/etc"
    if [[ -s "$dir/hubinet-maint" ]]; then
      install -m 0755 "$dir/hubinet-maint" "$root/usr/local/sbin/hubinet-maint"
    else
      rm -f "$root/usr/local/sbin/hubinet-maint"
    fi
    if [[ -s "$dir/hubinet-maint.json" ]]; then
      install -m 0644 "$dir/hubinet-maint.json" "$root/etc/hubinet-maint.json"
    else
      rm -f "$root/etc/hubinet-maint.json"
    fi
    rm -f "$root/usr/local/sbin/.hubinet-maint.new" "$root/etc/.hubinet-maint.new.json"
    pct unmount "$vmid"
  fi
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
if data.get("version") != "0.4.0" or data.get("protocol_version") != 1: raise SystemExit("incompatible executor version/protocol")
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
    pct push "$vmid" "$SOURCE_DIR/deploy/managed/hubinet-maint" /usr/local/sbin/.hubinet-maint.new --perms 0755
    pct push "$vmid" "$profile" /etc/.hubinet-maint.new.json --perms 0644
    pct exec "$vmid" -- python3 -m py_compile /usr/local/sbin/.hubinet-maint.new
    pct exec "$vmid" -- mv -f /usr/local/sbin/.hubinet-maint.new /usr/local/sbin/hubinet-maint
    pct exec "$vmid" -- mv -f /etc/.hubinet-maint.new.json /etc/hubinet-maint.json
    payload="$(pct exec "$vmid" -- /usr/local/sbin/hubinet-maint capabilities)"
  else
    mountpoint="$(safe_mount "$vmid")"
    install -d -m 0755 "$mountpoint/usr/local/sbin" "$mountpoint/etc"
    install -m 0755 "$SOURCE_DIR/deploy/managed/hubinet-maint" "$mountpoint/usr/local/sbin/.hubinet-maint.new"
    install -m 0644 "$profile" "$mountpoint/etc/.hubinet-maint.new.json"
    HUBINET_MAINT_CONFIG_PATH="$mountpoint/etc/.hubinet-maint.new.json" \
      python3 -m py_compile "$mountpoint/usr/local/sbin/.hubinet-maint.new"
    mv -f "$mountpoint/usr/local/sbin/.hubinet-maint.new" "$mountpoint/usr/local/sbin/hubinet-maint"
    mv -f "$mountpoint/etc/.hubinet-maint.new.json" "$mountpoint/etc/hubinet-maint.json"
    payload="$(HUBINET_MAINT_CONFIG_PATH="$mountpoint/etc/hubinet-maint.json" python3 "$mountpoint/usr/local/sbin/hubinet-maint" capabilities)"
    pct unmount "$vmid"
  fi
  validate_capabilities "$vmid" "$payload"
}

rollback_all() {
  local rc="${1:-1}" failed=false
  trap - ERR INT TERM EXIT
  echo "0.4.0 upgrade failed; restoring all modified layers" >&2
  if [[ "$agent_changes_started" == true && "$agent_backup_complete" == true ]]; then
    pct exec "$AGENT_VMID" -- bash -s -- "$AGENT_BACKUP" \
      < "$SOURCE_DIR/deploy/agent/restore-0.3.0.sh" || failed=true
  elif [[ "$agent_backup_complete" == true ]]; then
    pct exec "$AGENT_VMID" -- rm -f /etc/hubinet-ops/config.yaml.new /etc/hubinet-ops/agent.env.new || failed=true
    pct exec "$AGENT_VMID" -- systemctl start hubinet-ops || failed=true
  fi
  for vmid in $(seq 109 -1 101); do
    restore_managed_ct "$vmid" || failed=true
  done
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
  [[ -z "$TOKEN_STAGE" ]] || rm -f "$TOKEN_STAGE"
  [[ "$failed" == false ]] || rc=1
  exit "$rc"
}
trap 'rollback_all $?' ERR
trap 'rollback_all 130' INT
trap 'rollback_all 143' TERM
trap 'rm -f "$ARCHIVE"; [[ -z "$TOKEN_STAGE" ]] || rm -f "$TOKEN_STAGE"; [[ -z "$HOSTD_ENV_STAGE" ]] || rm -f "$HOSTD_ENV_STAGE"' EXIT

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
python3 "$SOURCE_DIR/scripts/migrate_config_0_4_0.py" \
  "$BACKUP/agent-config.yaml" "$BACKUP/agent-config-0.4.0.yaml" \
  --host-control-url "$HOST_CONTROL_URL"
python3 "$SOURCE_DIR/scripts/validate_pve_snapshot_policy.py" \
  "$BACKUP/agent-config-0.4.0.yaml" \
  --policy-dir "$SOURCE_DIR/deploy/pve"
set -a
# The token remains outside the repository and is never printed.
source "$(pve_path "$HOSTD_ENV")"
set +a
[[ ${#HUBINET_OPS_HOSTD_TOKEN} -ge 32 ]] || { echo "Invalid hostd token" >&2; exit 1; }
if [[ -z ${HUBINET_OPS_HOSTD_UPDATE_TOKEN:-} ]]; then
  HUBINET_OPS_HOSTD_UPDATE_TOKEN="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
elif [[ ${#HUBINET_OPS_HOSTD_UPDATE_TOKEN} -lt 32 || "$HUBINET_OPS_HOSTD_UPDATE_TOKEN" == "$HUBINET_OPS_HOSTD_TOKEN" ]]; then
  echo "Invalid hostd self-update token" >&2
  exit 1
fi
HOSTD_ENV_STAGE="$(mktemp)"
grep -v -E '^HUBINET_OPS_HOSTD_(UPDATE_)?TOKEN=' "$(pve_path "$HOSTD_ENV")" > "$HOSTD_ENV_STAGE" || true
printf 'HUBINET_OPS_HOSTD_TOKEN=%s\n' "$HUBINET_OPS_HOSTD_TOKEN" >> "$HOSTD_ENV_STAGE"
printf 'HUBINET_OPS_HOSTD_UPDATE_TOKEN=%s\n' "$HUBINET_OPS_HOSTD_UPDATE_TOKEN" >> "$HOSTD_ENV_STAGE"
chmod 0600 "$HOSTD_ENV_STAGE"
TOKEN_STAGE="$(mktemp)"
grep -v -E '^HUBINET_OPS_HOSTD_(UPDATE_)?TOKEN=' "$BACKUP/agent.env" > "$TOKEN_STAGE" || true
printf 'HUBINET_OPS_HOSTD_TOKEN=%s\n' "$HUBINET_OPS_HOSTD_TOKEN" >> "$TOKEN_STAGE"
printf 'HUBINET_OPS_HOSTD_UPDATE_TOKEN=%s\n' "$HUBINET_OPS_HOSTD_UPDATE_TOKEN" >> "$TOKEN_STAGE"
chmod 0600 "$TOKEN_STAGE"
pct push "$AGENT_VMID" "$BACKUP/agent-config-0.4.0.yaml" /etc/hubinet-ops/config.yaml.new --perms 0640
pct push "$AGENT_VMID" "$TOKEN_STAGE" /etc/hubinet-ops/agent.env.new --perms 0600
rm -f "$TOKEN_STAGE"
TOKEN_STAGE=""
changes_started=true

install -m 0600 "$HOSTD_ENV_STAGE" "$(pve_path "$HOSTD_ENV")"
rm -f "$HOSTD_ENV_STAGE"
HOSTD_ENV_STAGE=""
install -d -m 0755 "$(pve_path /usr/local/lib/hubinet-ops)" "$(pve_path /usr/local/sbin)" "$(pve_path /etc/hubinet-ops)" "$(pve_path /etc/systemd/system)"
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

hostd_health_url="${HUBINET_OPS_HOSTD_HEALTH_URL:-}"
if [[ -z "$hostd_health_url" ]]; then
  hostd_health_url="$(python3 - "$(pve_path "$HOSTD_CONFIG")" <<'PY'
import json, sys
cfg=json.load(open(sys.argv[1], encoding="utf-8"))
bind=cfg["bind"]
print(f"http://{bind}:{int(cfg.get('port',8741))}/health")
PY
)"
fi
hostd_health="$(curl -fsS --max-time 5 "$hostd_health_url")"
python3 - "$hostd_health" <<'PY'
import json, sys
data=json.loads(sys.argv[1])
if data.get("status") != "ok" or data.get("version") != "0.4.0": raise SystemExit("hostd health failed")
PY

tar -C "$SOURCE_DIR" -czf "$ARCHIVE" app
pct push "$AGENT_VMID" "$ARCHIVE" /root/hubinet-ops-0.4.0.tgz --perms 0600
agent_changes_started=true
VALIDATION_NOT_BEFORE="$(date -u +%Y-%m-%dT%H:%M:%S+00:00)"
pct exec "$AGENT_VMID" -- bash -s <<'REMOTE_INSTALL_AGENT'
set -Eeuo pipefail
staging=/root/hubinet-ops-0.4.0
rm -rf "$staging"
install -d -m 0700 "$staging"
tar -xzf /root/hubinet-ops-0.4.0.tgz -C "$staging"
grep -Fq 'VERSION = "0.4.0"' "$staging/app/mqtt.py"
mv -f /etc/hubinet-ops/config.yaml.new /etc/hubinet-ops/config.yaml
mv -f /etc/hubinet-ops/agent.env.new /etc/hubinet-ops/agent.env
chown root:hubinetops /etc/hubinet-ops/config.yaml
chmod 0640 /etc/hubinet-ops/config.yaml
chown root:root /etc/hubinet-ops/agent.env
chmod 0600 /etc/hubinet-ops/agent.env
rm -rf /opt/hubinet-ops/app
cp -a "$staging/app" /opt/hubinet-ops/app
chown -R hubinetops:hubinetops /opt/hubinet-ops/app
runuser -u hubinetops -- /opt/hubinet-ops/.venv/bin/python -m py_compile /opt/hubinet-ops/app/*.py
rm -rf "$staging" /root/hubinet-ops-0.4.0.tgz
systemctl start hubinet-ops
REMOTE_INSTALL_AGENT

validation_ok=false
for attempt in $(seq 1 "${HUBINET_OPS_VALIDATION_ATTEMPTS:-60}"); do
  health="$(pct exec "$AGENT_VMID" -- curl -fsS --max-time 3 http://127.0.0.1:8787/health 2>/dev/null || true)"
  if [[ "$health" == *'"version":"0.4.0"'* ]]; then
    states="$(pct exec "$AGENT_VMID" -- bash -s <<'REMOTE_READ_STATE'
set -Eeuo pipefail
set -a; source /etc/hubinet-ops/agent.env; set +a
curl -fsS --max-time 5 -H "Authorization: Bearer $HUBINET_OPS_API_TOKEN" http://127.0.0.1:8787/api/v1/state
REMOTE_READ_STATE
)" || states=""
    if python3 - "$states" "$VALIDATION_NOT_BEFORE" <<'PY'
from datetime import UTC, datetime
import json, math, sys
try: payload=json.loads(sys.argv[1]); resources=payload["resources"]
except Exception: raise SystemExit(1)
def stamp(value):
    parsed=datetime.fromisoformat(str(value).replace("Z","+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None: raise ValueError
    return parsed.astimezone(UTC)
not_before=stamp(sys.argv[2])
expected={str(vmid) for vmid in range(100,111)}
if payload.get("version") != "0.4.0" or set(resources) != expected: raise SystemExit(1)
try:
    if any(stamp(resources[vmid].get("last_refresh")) < not_before for vmid in expected): raise SystemExit(1)
except Exception: raise SystemExit(1)
for vmid in map(str, range(101,110)):
    state=resources[vmid]
    if state.get("executor_compatible") is not True or state.get("executor_version") != "0.4.0" or state.get("executor_protocol_version") != 1: raise SystemExit(1)
vm100=resources["100"]; usage=vm100.get("cpu",{}).get("usage_percent")
if vm100.get("health_status") != "healthy" or isinstance(usage,bool) or not isinstance(usage,(int,float)) or not math.isfinite(usage) or not 0 <= usage <= 100: raise SystemExit(1)
if resources["106"].get("health_status") != "healthy": raise SystemExit(1)
if resources["110"].get("health_status") != "healthy" or resources["110"].get("health_score") != 100: raise SystemExit(1)
PY
    then
      validation_ok=true
      break
    fi
  fi
  [[ "$attempt" -ge "${HUBINET_OPS_VALIDATION_ATTEMPTS:-60}" ]] || sleep "${HUBINET_OPS_VALIDATION_DELAY:-2}"
done
[[ "$validation_ok" == true ]] || { echo "Fresh 0.4.0 telemetry validation failed" >&2; rollback_all 1; }

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
echo "Hubinet Ops 0.4.0 installed transactionally. Backup: $BACKUP"
echo "Validated hostd, /api/v1/state, fresh inventory 100-110, executor compatibility and read-only snapshot listing."
echo "No LXC/VM lifecycle, snapshot mutation, update or maintenance action was executed."
