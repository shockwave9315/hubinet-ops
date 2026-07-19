#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TMP_ROOT="$(mktemp -d)"
FAKE_BIN="$TMP_ROOT/bin"
LOG_DIR="$TMP_ROOT/logs"
mkdir -p "$FAKE_BIN" "$LOG_DIR"
trap 'rm -rf "$TMP_ROOT"' EXIT

export HUBINET_OPS_TEST_MODE=1
export HUBINET_OPS_TEST_LOG_DIR="$LOG_DIR"
export PATH="$FAKE_BIN:$PATH"

for script in \
  "$ROOT/deploy/install-ha-0.2.4-from-pve.sh" \
  "$ROOT/deploy/upgrade-0.2.4-from-pve.sh"; do
  if grep -nE '(^|[[:space:]])\+([[:space:]]|$)' "$script"; then
    echo "Standalone diff marker found in $script" >&2
    exit 1
  fi
done

write_stub() {
  local name="$1"
  shift
  printf '%s\n' '#!/usr/bin/env bash' 'set -Eeuo pipefail' "$@" > "$FAKE_BIN/$name"
  chmod +x "$FAKE_BIN/$name"
}

write_stub date 'printf "%s\n" "20260719-120000"'
write_stub python3 '
printf "<%s>" "$@" >> "$HUBINET_OPS_TEST_LOG_DIR/python3.args"
printf "\n" >> "$HUBINET_OPS_TEST_LOG_DIR/python3.args"
[[ $# -eq 1 && $1 == */scripts/validate_yaml.py ]]
'
write_stub ssh '
printf "<%s>" "$@" >> "$HUBINET_OPS_TEST_LOG_DIR/ssh.args"
printf "\n" >> "$HUBINET_OPS_TEST_LOG_DIR/ssh.args"
'
write_stub scp '
printf "<%s>" "$@" >> "$HUBINET_OPS_TEST_LOG_DIR/scp.args"
printf "\n" >> "$HUBINET_OPS_TEST_LOG_DIR/scp.args"
'
write_stub install '
printf "<%s>" "$@" >> "$HUBINET_OPS_TEST_LOG_DIR/install.args"
printf "\n" >> "$HUBINET_OPS_TEST_LOG_DIR/install.args"
'
write_stub cp '
printf "<%s>" "$@" >> "$HUBINET_OPS_TEST_LOG_DIR/cp.args"
printf "\n" >> "$HUBINET_OPS_TEST_LOG_DIR/cp.args"
'
write_stub grep '
for arg in "$@"; do
  if [[ "$arg" == "/etc/hubinet-ops/allowed-vmids" ]]; then
    [[ " $* " == *" 101 "* || " $* " == *" 106 "* ]]
    exit
  fi
done
exec /usr/bin/grep "$@"
'
write_stub runuser '
printf "<%s>" "$@" >> "$HUBINET_OPS_TEST_LOG_DIR/runuser.args"
printf "\n" >> "$HUBINET_OPS_TEST_LOG_DIR/runuser.args"
if [[ ${1:-} == -u && ${2:-} == hubinetops && ${3:-} == --preserve-environment ]]; then
  [[ $# -eq 9 ]]
  [[ $4 == -- && $5 == env && $6 == PYTHONPATH=/opt/hubinet-ops ]]
  [[ $7 == /opt/hubinet-ops/.venv/bin/python && $8 == -c ]]
  [[ $9 == "from app.config import load_settings; from app.database import Database; s=load_settings(); Database(s.db_path)" ]]
elif [[ ${1:-} == -u && ${2:-} == hubinetops && ${3:-} == -- ]]; then
  [[ $# -eq 8 ]]
  [[ $4 == /opt/hubinet-ops/.venv/bin/python && $5 == -m ]]
  [[ $6 == compileall && $7 == -q && $8 == /opt/hubinet-ops/app ]]
else
  echo "Unexpected runuser arguments" >&2
  exit 1
fi
'
write_stub pct '
printf "<%s>" "$@" >> "$HUBINET_OPS_TEST_LOG_DIR/pct.args"
printf "\n" >> "$HUBINET_OPS_TEST_LOG_DIR/pct.args"
if [[ ${1:-} == status ]]; then
  printf "status: running\n"
  exit 0
fi
if [[ ${1:-} == exec && " $* " == *" bash -s "* ]]; then
  remote="$(mktemp)"
  commands="$(mktemp)"
  cat > "$remote"
  awk "/^runuser / { print }" "$remote" > "$commands"
  if [[ -s "$commands" ]]; then
    bash "$commands"
  fi
  rm -f "$remote" "$commands"
  exit 0
fi
if [[ ${1:-} == exec && " $* " == *" curl "* ]]; then
  printf "%s\n" "{\"status\":\"ok\",\"version\":\"0.2.4\"}"
fi
'

bash "$ROOT/deploy/install-ha-0.2.4-from-pve.sh" ha.test http://agent.test:8787 2222
bash "$ROOT/deploy/upgrade-0.2.4-from-pve.sh" 110 106

mapfile -t scp_calls < "$LOG_DIR/scp.args"
[[ ${#scp_calls[@]} -eq 3 ]]
[[ ${scp_calls[0]} == *"<${ROOT}/home-assistant/packages/hubinet_ops.yaml><root@ha.test:/config/packages/hubinet_ops.yaml.new>" ]]
[[ ${scp_calls[1]} == *"<${ROOT}/home-assistant/dashboards/hubinet_ops.yaml><root@ha.test:/config/dashboards/hubinet_ops.yaml.new>" ]]
[[ ${scp_calls[2]} == *"<root@ha.test:/tmp/hubinet-ops-0.2.4-urls>" ]]
grep -Fq '<-p><2222><-i></root/.ssh/id_ed25519><root@ha.test>' "$LOG_DIR/ssh.args"
grep -Fq '<status><101>' "$LOG_DIR/pct.args"
grep -Fq '<status><106>' "$LOG_DIR/pct.args"
grep -Fq '<status><110>' "$LOG_DIR/pct.args"
grep -Fq '<push><110>' "$LOG_DIR/pct.args"
grep -Fq '<-u><hubinetops><--preserve-environment><--><env><PYTHONPATH=/opt/hubinet-ops></opt/hubinet-ops/.venv/bin/python><-c>' "$LOG_DIR/runuser.args"

printf '%s\n' "0.2.4 installer runtime smoke passed"
