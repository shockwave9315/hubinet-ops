#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Uruchom na hoście Proxmox jako root." >&2
  exit 1
fi

AGENT_VMID="${1:-110}"
CT106_VMID="${2:-106}"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAMP="$(date +%Y%m%d-%H%M%S)"

pct status "$AGENT_VMID" | grep -q running || { echo "CT$AGENT_VMID nie działa" >&2; exit 1; }
pct status "$CT106_VMID" | grep -q running || { echo "CT$CT106_VMID nie działa" >&2; exit 1; }

echo "=== BACKUP AGENTA ==="
pct exec "$AGENT_VMID" -- bash -lc "
  set -e
  mkdir -p /root/hubinet-ops-backups/$STAMP
  cp -a /opt/hubinet-ops/app /root/hubinet-ops-backups/$STAMP/app
  cp -a /etc/hubinet-ops/config.yaml /root/hubinet-ops-backups/$STAMP/config.yaml
  cp -a /var/lib/hubinet-ops/ops.db* /root/hubinet-ops-backups/$STAMP/ 2>/dev/null || true
"

echo "=== AKTUALIZACJA WRAPPERA PVE ==="
install -m 0755 "$SOURCE_DIR/deploy/pve/hubinet-ops-host" /usr/local/sbin/hubinet-ops-host
install -d -m 0755 /etc/hubinet-ops
touch /etc/hubinet-ops/allowed-vmids
for vmid in 101 "$CT106_VMID"; do
  grep -Fxq "$vmid" /etc/hubinet-ops/allowed-vmids || echo "$vmid" >> /etc/hubinet-ops/allowed-vmids
done
sort -n -u /etc/hubinet-ops/allowed-vmids -o /etc/hubinet-ops/allowed-vmids
chmod 0644 /etc/hubinet-ops/allowed-vmids
cat /etc/hubinet-ops/allowed-vmids

echo "=== PROFIL CT106: POGODA + DOCKER ==="
bash "$SOURCE_DIR/deploy/managed/install-managed.sh" \
  "$CT106_VMID" \
  --config "$SOURCE_DIR/deploy/managed/profiles/ct106-weather.json"

echo "=== AKTUALIZACJA HUBINET-MAINT CT101 ==="
pct push 101 "$SOURCE_DIR/deploy/managed/hubinet-maint" /usr/local/sbin/hubinet-maint --perms 0755
pct exec 101 -- /usr/local/sbin/hubinet-maint inspect

echo "=== PRZESŁANIE KODU 0.2 DO AGENTA ==="
tar -C "$SOURCE_DIR" -czf /tmp/hubinet-ops-0.2-agent.tgz app requirements.txt deploy/hubinet-ops.service
pct push "$AGENT_VMID" /tmp/hubinet-ops-0.2-agent.tgz /root/hubinet-ops-0.2-agent.tgz --perms 0600
rm -f /tmp/hubinet-ops-0.2-agent.tgz

pct exec "$AGENT_VMID" -- bash -lc "
  set -Eeuo pipefail
  rm -rf /root/hubinet-ops-0.2-agent
  mkdir -p /root/hubinet-ops-0.2-agent
  tar -xzf /root/hubinet-ops-0.2-agent.tgz -C /root/hubinet-ops-0.2-agent

  systemctl stop hubinet-ops
  rm -rf /opt/hubinet-ops/app
  cp -a /root/hubinet-ops-0.2-agent/app /opt/hubinet-ops/app
  cp /root/hubinet-ops-0.2-agent/requirements.txt /opt/hubinet-ops/requirements.txt
  chown -R hubinetops:hubinetops /opt/hubinet-ops/app /opt/hubinet-ops/requirements.txt

  /opt/hubinet-ops/.venv/bin/pip install -r /opt/hubinet-ops/requirements.txt
  install -m 0644 /root/hubinet-ops-0.2-agent/deploy/hubinet-ops.service /etc/systemd/system/hubinet-ops.service

  python3 - <<'PY'
from pathlib import Path
import yaml

path = Path('/etc/hubinet-ops/config.yaml')
data = yaml.safe_load(path.read_text()) or {}

scheduler = data.setdefault('scheduler', {})
scheduler.setdefault('enabled', False)
scheduler.setdefault('scan_interval_minutes', 360)
scheduler.setdefault('initial_scan_delay_seconds', 60)
scheduler.setdefault('approval_ttl_minutes', 1440)
scheduler['state_refresh_seconds'] = 30
scheduler['initial_refresh_delay_seconds'] = 5

containers = data.setdefault('containers', {})
ct101 = containers.setdefault(101, {})
ct101.setdefault('name', 'cloudflared')
ct101.setdefault('enabled', True)
ct101.setdefault('adapter', 'apt')
ct101.setdefault('criticality', 'medium')
ct101.setdefault('approval_mode', 'always')
ct101.setdefault('automatic_rollback', True)
ct101['dashboard_path'] = '/hubinet-ops/ct-101'

ct106 = containers.setdefault(106, {})
ct106.update({
    'name': 'pogoda',
    'enabled': True,
    'adapter': 'apt',
    'criticality': 'low',
    'approval_mode': 'always',
    'automatic_rollback': True,
    'dashboard_path': '/hubinet-ops/ct-106',
    'snapshot_retention_hours': 24,
})

path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
PY

  chown root:hubinetops /etc/hubinet-ops/config.yaml
  chmod 0640 /etc/hubinet-ops/config.yaml
  systemctl daemon-reload
  systemctl start hubinet-ops
"

sleep 4

echo "=== STATUS AGENTA ==="
pct exec "$AGENT_VMID" -- systemctl status hubinet-ops --no-pager --full

echo "=== WERSJA API ==="
curl -fsS http://192.168.4.200:8787/health
printf '\n'

TOKEN="$(pct exec "$AGENT_VMID" -- bash -lc "sed -n 's/^HUBINET_OPS_API_TOKEN=//p' /etc/hubinet-ops/agent.env")"

echo "=== REFRESH CT101 I CT106 ==="
for vmid in 101 "$CT106_VMID"; do
  curl -fsS -X POST \
    -H "Authorization: Bearer $TOKEN" \
    "http://192.168.4.200:8787/api/v1/containers/$vmid/refresh" \
    | python3 -m json.tool
  echo
done

echo "=== GOTOWE ==="
echo "Agent 0.2 działa. Scheduler skanów pozostaje zgodny z obecną konfiguracją."
echo "Nie wykonano aktualizacji APT żadnego kontenera."
