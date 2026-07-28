#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Uruchom jako root wewnątrz nowego LXC." >&2
  exit 1
fi

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y python3 python3-venv openssh-client ca-certificates openssl

id hubinetops >/dev/null 2>&1 || useradd --system --home /opt/hubinet-ops --shell /usr/sbin/nologin hubinetops
install -d -o hubinetops -g hubinetops /opt/hubinet-ops /var/lib/hubinet-ops
install -d -m 0750 -o root -g hubinetops /etc/hubinet-ops /etc/hubinet-ops/keys

normalize_ssh_permissions() {
  install -d -m 0750 -o root -g hubinetops /etc/hubinet-ops/keys
  chown hubinetops:hubinetops /etc/hubinet-ops/keys/proxmox_ed25519
  chmod 0600 /etc/hubinet-ops/keys/proxmox_ed25519
  if [[ -f /etc/hubinet-ops/keys/proxmox_ed25519.pub ]]; then
    chown root:hubinetops /etc/hubinet-ops/keys/proxmox_ed25519.pub
    chmod 0644 /etc/hubinet-ops/keys/proxmox_ed25519.pub
  fi
  if [[ ! -e /etc/hubinet-ops/ssh_known_hosts ]]; then
    install -m 0640 -o root -g hubinetops /dev/null /etc/hubinet-ops/ssh_known_hosts
  else
    chown root:hubinetops /etc/hubinet-ops/ssh_known_hosts
    chmod 0640 /etc/hubinet-ops/ssh_known_hosts
  fi
}

rsync_available=0
command -v rsync >/dev/null 2>&1 && rsync_available=1
rm -rf /opt/hubinet-ops/app /opt/hubinet-ops/requirements.txt
cp -a "$SOURCE_DIR/app" /opt/hubinet-ops/app
cp "$SOURCE_DIR/requirements.txt" /opt/hubinet-ops/requirements.txt
chown -R hubinetops:hubinetops /opt/hubinet-ops

python3 -m venv /opt/hubinet-ops/.venv
/opt/hubinet-ops/.venv/bin/pip install --upgrade pip
/opt/hubinet-ops/.venv/bin/pip install -r /opt/hubinet-ops/requirements.txt
chown -R hubinetops:hubinetops /opt/hubinet-ops/.venv

if [[ ! -f /etc/hubinet-ops/config.yaml ]]; then
  cp "$SOURCE_DIR/config/config.example.yaml" /etc/hubinet-ops/config.yaml
  chown root:hubinetops /etc/hubinet-ops/config.yaml
  chmod 0640 /etc/hubinet-ops/config.yaml
fi

if [[ ! -f /etc/hubinet-ops/agent.env ]]; then
  TOKEN="$(openssl rand -hex 32)"
  cat > /etc/hubinet-ops/agent.env <<ENV
HUBINET_OPS_CONFIG=/etc/hubinet-ops/config.yaml
HUBINET_OPS_DB=/var/lib/hubinet-ops/ops.db
HUBINET_OPS_API_TOKEN=${TOKEN}
ENV
  chown root:hubinetops /etc/hubinet-ops/agent.env
  chmod 0640 /etc/hubinet-ops/agent.env
fi

if [[ ! -f /etc/hubinet-ops/keys/proxmox_ed25519 ]]; then
  ssh-keygen -q -t ed25519 -N '' -C hubinet-ops-agent -f /etc/hubinet-ops/keys/proxmox_ed25519
fi
normalize_ssh_permissions

install -m 0644 "$SOURCE_DIR/deploy/hubinet-ops.service" /etc/systemd/system/hubinet-ops.service
systemctl daemon-reload
systemctl enable hubinet-ops.service

cat <<INFO

Agent zainstalowany, ale jeszcze go nie uruchamiam.

1. Edytuj: /etc/hubinet-ops/config.yaml
2. Dodaj klucz do hosta Proxmox:

$(cat /etc/hubinet-ops/keys/proxmox_ed25519.pub)

3. Dodaj fingerprint hosta:
   ssh-keyscan -H ADRES_PROXMOX >> /etc/hubinet-ops/ssh_known_hosts
   chown root:hubinetops /etc/hubinet-ops/ssh_known_hosts
   chmod 0640 /etc/hubinet-ops/ssh_known_hosts

4. Uruchom:
   systemctl start hubinet-ops
   systemctl status hubinet-ops --no-pager

Token API znajduje się w /etc/hubinet-ops/agent.env — nie wklejaj go publicznie.
INFO
