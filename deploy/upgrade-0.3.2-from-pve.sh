#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

[[ ${EUID} -eq 0 || ${HUBINET_OPS_TEST_MODE:-0} == 1 ]] || {
  echo "Run as root on PVE" >&2
  exit 1
}
[[ $# -eq 0 ]] || { echo "This CT110 patch accepts no arguments" >&2; exit 2; }

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAMP="$(date +%Y%m%d-%H%M%S)"
ARCHIVE="${HUBINET_OPS_TEST_ARCHIVE:-/tmp/hubinet-ops-0.3.2-${STAMP}.tgz}"
BACKUP_ROOT="${HUBINET_OPS_BACKUP_ROOT:-/root/hubinet-ops-backups}"
AGENT_BACKUP="$BACKUP_ROOT/${STAMP}-before-0.3.2"
WRAPPER_BACKUP_DIR="$BACKUP_ROOT/${STAMP}-before-0.3.2-pve"
HOST_WRAPPER="${HUBINET_OPS_HOST_WRAPPER:-/usr/local/sbin/hubinet-ops-host}"
SOURCE_WRAPPER="$SOURCE_DIR/deploy/pve/hubinet-ops-host"
WRAPPER_BACKUP="$WRAPPER_BACKUP_DIR/hubinet-ops-host"
AGENT_VMID=110
agent_backup_complete=false
agent_changes_started=false
wrapper_backup_complete=false
wrapper_changes_started=false

validate_wrapper_inspect() {
  local expected="$1" payload="$2"
  python3 - "$expected" "$payload" <<'PY'
import json, math, sys

expected, raw = sys.argv[1:]
try:
    payload = json.loads(raw)
    data = payload["data"]
except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
    raise SystemExit(f"invalid wrapper JSON: {exc}")
if payload.get("ok") is not True or not isinstance(data, dict):
    raise SystemExit("wrapper response must contain ok=true and an object data")
if data.get("resource_type") != expected:
    raise SystemExit(f"unexpected resource_type: {data.get('resource_type')!r}")
if expected == "lxc":
    raise SystemExit(0)
if data.get("adapter") != "haos":
    raise SystemExit("VM100 adapter must be haos")
if data.get("qemu_status") not in {"running", "stopped"}:
    raise SystemExit("VM100 qemu_status must be running or stopped")
cpu = data.get("cpu")
if not isinstance(cpu, dict) or "usage" not in cpu:
    raise SystemExit("VM100 cpu must be an object containing usage")
usage = cpu["usage"]
if data["qemu_status"] == "running" and usage is None:
    raise SystemExit("running VM100 requires cluster/resources cpu.usage")
if usage is not None and (
    isinstance(usage, bool)
    or not isinstance(usage, (int, float))
    or not math.isfinite(usage)
    or not 0 <= usage <= 1
):
    raise SystemExit("VM100 cpu.usage must be null or a number from 0 to 1")
PY
}

required_source=(
  app
  deploy/pve/hubinet-ops-host
  deploy/agent/backup-0.3.0.sh
  deploy/agent/restore-0.3.0.sh
)
for path in "${required_source[@]}"; do
  [[ -e "$SOURCE_DIR/$path" ]] || {
    echo "Missing source artifact: $path" >&2
    exit 1
  }
done
grep -Fq 'VERSION = "0.3.2"' "$SOURCE_DIR/app/mqtt.py" || {
  echo "Source tree is not Hubinet Ops 0.3.2" >&2
  exit 1
}
[[ -f "$HOST_WRAPPER" ]] || {
  echo "Existing forced-command wrapper is missing: $HOST_WRAPPER" >&2
  exit 1
}

rollback_patch() {
  local rc="${1:-1}" cleanup_failed=false
  trap - ERR INT TERM EXIT
  if [[ "$wrapper_changes_started" == true && "$wrapper_backup_complete" == true ]]; then
    echo "0.3.2 patch failed; restoring the PVE host wrapper" >&2
    if ! cp -a "$WRAPPER_BACKUP" "$HOST_WRAPPER" || ! bash -n "$HOST_WRAPPER"; then
      echo "ROLLBACK INCOMPLETE: PVE host wrapper restore failed" >&2
      cleanup_failed=true
    fi
  fi
  if [[ "$agent_changes_started" == true && "$agent_backup_complete" == true ]]; then
    echo "0.3.2 patch failed; restoring the complete CT110 backup" >&2
    if ! pct exec "$AGENT_VMID" -- bash -s -- "$AGENT_BACKUP" \
      < "$SOURCE_DIR/deploy/agent/restore-0.3.0.sh"; then
      echo "ROLLBACK INCOMPLETE: CT110 agent restore failed" >&2
      cleanup_failed=true
    fi
  elif [[ "$agent_backup_complete" == true ]]; then
    if ! pct exec "$AGENT_VMID" -- systemctl start hubinet-ops; then
      echo "ROLLBACK INCOMPLETE: the unchanged agent could not be restarted" >&2
      cleanup_failed=true
    fi
  fi
  if ! pct exec "$AGENT_VMID" -- rm -f /root/hubinet-ops-0.3.2.tgz 2>/dev/null; then
    echo "Warning: remove /root/hubinet-ops-0.3.2.tgz manually in CT110" >&2
  fi
  rm -f "$ARCHIVE"
  [[ "$cleanup_failed" == false ]] || rc=1
  exit "$rc"
}
trap 'rollback_patch $?' ERR
trap 'rollback_patch 130' INT
trap 'rollback_patch 143' TERM
trap 'rm -f "$ARCHIVE"' EXIT

status="$(pct status "$AGENT_VMID" | awk '{print $2}')"
[[ "$status" == running ]] || {
  echo "CT110 must already be running; no lifecycle action was attempted" >&2
  exit 1
}

python3 "$SOURCE_DIR/scripts/validate_yaml.py"
python3 -m py_compile "$SOURCE_DIR"/app/*.py
bash -n "$SOURCE_WRAPPER"
tar -C "$SOURCE_DIR" -czf "$ARCHIVE" app

install -d -m 0700 "$WRAPPER_BACKUP_DIR"
cp -a "$HOST_WRAPPER" "$WRAPPER_BACKUP"
wrapper_backup_complete=true

# This helper stops the only SQLite writer before copying the complete database.
pct exec "$AGENT_VMID" -- bash -s -- "$AGENT_BACKUP" \
  < "$SOURCE_DIR/deploy/agent/backup-0.3.0.sh"
agent_backup_complete=true

wrapper_changes_started=true
install -o root -g root -m 0755 "$SOURCE_WRAPPER" "$HOST_WRAPPER"
bash -n "$HOST_WRAPPER"

if [[ ${HUBINET_OPS_TEST_MODE:-0} == 1 && -n ${HUBINET_OPS_TEST_WRAPPER_RUNNER:-} ]]; then
  if ! qemu_smoke="$(SSH_ORIGINAL_COMMAND='inspect 100' "$HUBINET_OPS_TEST_WRAPPER_RUNNER")"; then
    echo "PVE wrapper VM100 read-only smoke failed" >&2
    rollback_patch 1
  fi
  if ! lxc_smoke="$(SSH_ORIGINAL_COMMAND='inspect 106' "$HUBINET_OPS_TEST_WRAPPER_RUNNER")"; then
    echo "PVE wrapper CT106 read-only smoke failed" >&2
    rollback_patch 1
  fi
else
  if ! qemu_smoke="$(SSH_ORIGINAL_COMMAND='inspect 100' /usr/local/sbin/hubinet-ops-host)"; then
    echo "PVE wrapper VM100 read-only smoke failed" >&2
    rollback_patch 1
  fi
  if ! lxc_smoke="$(SSH_ORIGINAL_COMMAND='inspect 106' /usr/local/sbin/hubinet-ops-host)"; then
    echo "PVE wrapper CT106 read-only smoke failed" >&2
    rollback_patch 1
  fi
fi
if ! validate_wrapper_inspect qemu "$qemu_smoke"; then
  echo "PVE wrapper VM100 returned an invalid read-only inspect response" >&2
  rollback_patch 1
fi
if ! validate_wrapper_inspect lxc "$lxc_smoke"; then
  echo "PVE wrapper CT106 returned an invalid read-only inspect response" >&2
  rollback_patch 1
fi

pct push "$AGENT_VMID" "$ARCHIVE" /root/hubinet-ops-0.3.2.tgz --perms 0600
agent_changes_started=true
VALIDATION_NOT_BEFORE="$(date -u +%Y-%m-%dT%H:%M:%S+00:00)"

pct exec "$AGENT_VMID" -- bash -s <<'REMOTE_INSTALL_AGENT'
set -Eeuo pipefail
staging=/root/hubinet-ops-0.3.2
rm -rf "$staging"
install -d -m 0700 "$staging"
tar -xzf /root/hubinet-ops-0.3.2.tgz -C "$staging"
grep -Fq 'VERSION = "0.3.2"' "$staging/app/mqtt.py"
rm -rf /opt/hubinet-ops/app
cp -a "$staging/app" /opt/hubinet-ops/app
chown -R hubinetops:hubinetops /opt/hubinet-ops/app
runuser -u hubinetops -- /opt/hubinet-ops/.venv/bin/python -m py_compile /opt/hubinet-ops/app/*.py
rm -rf "$staging" /root/hubinet-ops-0.3.2.tgz
systemctl start hubinet-ops
REMOTE_INSTALL_AGENT

for attempt in $(seq 1 45); do
  health_rc=0
  health="$(pct exec "$AGENT_VMID" -- curl -fsS --max-time 3 http://127.0.0.1:8787/health 2>/dev/null)" || health_rc=$?
  if [[ "$health_rc" -eq 0 && "$health" == *'"version":"0.3.2"'* ]]; then
    states_rc=0
    states="$(pct exec "$AGENT_VMID" -- bash -s <<'REMOTE_CHECK_STATES'
set -Eeuo pipefail
set -a
source /etc/hubinet-ops/agent.env
set +a
curl -fsS --max-time 5 -H "Authorization: Bearer $HUBINET_OPS_API_TOKEN" \
  http://127.0.0.1:8787/api/v1/states
REMOTE_CHECK_STATES
)" || states_rc=$?
    if [[ "$states_rc" -eq 0 ]] && python3 - "$states" "$VALIDATION_NOT_BEFORE" <<'PY'
from datetime import UTC, datetime
import json, math, sys

try:
    payload = json.loads(sys.argv[1])
    resources = payload["resources"]
except (KeyError, TypeError, ValueError, json.JSONDecodeError):
    raise SystemExit(1)

def utc_timestamp(value):
    if not isinstance(value, str):
        raise ValueError
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError
    return parsed.astimezone(UTC)

try:
    validation_not_before = utc_timestamp(sys.argv[2])
except (TypeError, ValueError):
    raise SystemExit(1)
expected_vmids = {str(vmid) for vmid in range(100, 111)}
if payload.get("version") != "0.3.2" or not isinstance(resources, dict) or set(resources) != expected_vmids:
    raise SystemExit(1)
try:
    if any(
        utc_timestamp(resources[vmid].get("last_refresh")) < validation_not_before
        for vmid in expected_vmids
    ):
        raise SystemExit(1)
except (AttributeError, TypeError, ValueError):
    raise SystemExit(1)
vm100 = resources.get("100", {})
ct106 = resources.get("106", {})
ct110 = resources.get("110", {})
usage = vm100.get("cpu", {}).get("usage_percent")
valid_number = (
    isinstance(usage, (int, float))
    and not isinstance(usage, bool)
    and math.isfinite(usage)
    and 0 <= usage <= 100
)
if not valid_number or vm100.get("health_status") != "healthy":
    raise SystemExit(1)
if ct106.get("health_status") != "healthy":
    raise SystemExit(1)
if ct110.get("health_status") != "healthy" or ct110.get("health_score") != 100:
    raise SystemExit(1)
PY
    then
      trap - ERR INT TERM EXIT
      rm -f "$ARCHIVE"
      echo "Hubinet Ops 0.3.2 installed transactionally in CT110 and the PVE host wrapper."
      echo "No managed resource action or lifecycle action was executed."
      echo "Backups: CT110:$AGENT_BACKUP PVE:$WRAPPER_BACKUP_DIR"
      exit 0
    fi
  fi
  [[ "$attempt" -eq 45 ]] || sleep 2
done

echo "Agent 0.3.2 first telemetry validation failed" >&2
rollback_patch 1
