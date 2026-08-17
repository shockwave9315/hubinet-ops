#!/usr/bin/env bash
set -euo pipefail

# Clean-install-only installer for the Hubinet Ops 0.5 R0 read-only
# runtime, per docs/architecture/0.5-r0-read-only-runtime-activation.md
# section 3/25/26. Intended for a freshly rebuilt host (e.g. a rebuilt
# CT110) that has never run the 0.4 production service.
#
# This script does NOT:
#   - upgrade an existing 0.4 installation;
#   - import 0.4 config or ops.db;
#   - install, start, or coexist with deploy/hubinet-ops.service;
#   - open a Proxmox connection, contact any private-network host, or
#     perform any discovery -- it only prepares the host, the venv, and
#     the systemd unit, then leaves the service disabled/unconfigured
#     until the operator fills in config/inventory.yaml and agent.env.
#
# Run as root on the target host itself (mirrors deploy/install-agent.sh's
# existing self-install posture, not the SSH-orchestrated
# install-ha-*-from-pve.sh family).

if [[ ${EUID} -ne 0 ]]; then
  echo "Run as root on the target host." >&2
  exit 1
fi

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

LEGACY_UNIT_PATH="/etc/systemd/system/hubinet-ops.service"
if [[ -e "${LEGACY_UNIT_PATH}" ]]; then
  echo "Refusing to install: ${LEGACY_UNIT_PATH} already exists." >&2
  echo "This installer targets a clean rebuilt host with no prior 0.4" >&2
  echo "Hubinet Ops installation -- see docs/architecture/0.5-r0-read-only-runtime-activation.md section 3." >&2
  exit 1
fi

LEGACY_DB_PATH="/var/lib/hubinet-ops/ops.db"
if [[ -e "${LEGACY_DB_PATH}" ]]; then
  echo "Refusing to install: ${LEGACY_DB_PATH} already exists." >&2
  echo "This installer never migrates or reads a legacy 0.4 database." >&2
  exit 1
fi

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y python3 python3-venv ca-certificates

id hubinetops >/dev/null 2>&1 || useradd --system --home /opt/hubinet-ops --shell /usr/sbin/nologin hubinetops
install -d -o hubinetops -g hubinetops /opt/hubinet-ops
# The authority directory holds the R0 SQLite database; §4 requires an
# explicit restrictive mode rather than relying on the process umask or
# a distribution default.
install -d -m 0750 -o hubinetops -g hubinetops /var/lib/hubinet-ops
install -d -m 0750 -o root -g hubinetops /etc/hubinet-ops

rm -rf /opt/hubinet-ops/app /opt/hubinet-ops/requirements.txt
cp -a "${SOURCE_DIR}/app" /opt/hubinet-ops/app
cp "${SOURCE_DIR}/requirements.txt" /opt/hubinet-ops/requirements.txt
chown -R hubinetops:hubinetops /opt/hubinet-ops

python3 -m venv /opt/hubinet-ops/.venv
/opt/hubinet-ops/.venv/bin/pip install --upgrade pip
/opt/hubinet-ops/.venv/bin/pip install -r /opt/hubinet-ops/requirements.txt
chown -R hubinetops:hubinetops /opt/hubinet-ops/.venv

if [[ ! -f /etc/hubinet-ops/inventory.yaml ]]; then
  cp "${SOURCE_DIR}/config/inventory.example.yaml" /etc/hubinet-ops/inventory.yaml
  chown root:hubinetops /etc/hubinet-ops/inventory.yaml
  chmod 0640 /etc/hubinet-ops/inventory.yaml
fi

if [[ ! -f /etc/hubinet-ops/agent.env ]]; then
  R0_API_TOKEN="$(openssl rand -hex 32)"
  cat > /etc/hubinet-ops/agent.env <<ENV
HUBINET_OPS_R0_CONFIG=/etc/hubinet-ops/inventory.yaml
HUBINET_OPS_R0_PVE_TOKEN=
HUBINET_OPS_R0_API_TOKEN=${R0_API_TOKEN}
ENV
  chown root:hubinetops /etc/hubinet-ops/agent.env
  chmod 0640 /etc/hubinet-ops/agent.env
fi

install -m 0644 "${SOURCE_DIR}/deploy/hubinet-ops-0.5.service" /etc/systemd/system/hubinet-ops.service
systemctl daemon-reload

# Deliberately does NOT enable or start the unit here. The mandatory
# firewall policy (deploy/README-0.5-firewall.md) must be applied and
# verified first -- an enabled-but-not-started unit would still
# auto-start unprotected on the next reboot, which is exactly the
# 0.0.0.0:8787-without-firewall exposure this installer must not create.

cat <<INFO

Hubinet Ops 0.5 (R0 read-only runtime) installed, but NOT started or
enabled for boot.

Before starting the service:

1. Edit /etc/hubinet-ops/inventory.yaml -- set source.pve_endpoint,
   source.display_name, source.freshness_duration_seconds, and
   source.credential_reference (an opaque secret:// reference, never
   the PVE token itself).

2. Edit /etc/hubinet-ops/agent.env -- set HUBINET_OPS_R0_PVE_TOKEN to
   your Proxmox API token in the exact shape user@realm!tokenid=secret.
   HUBINET_OPS_R0_API_TOKEN has already been generated for you; this is
   the bearer token Home Assistant must use to reach this backend.

3. Apply and verify the required firewall policy -- see
   deploy/README-0.5-firewall.md. This service binds 0.0.0.0:8787 and is
   only safe to enable once that policy is active and verified. Do not
   run step 4 before this step.

4. Only after step 3, enable and start the service together:
     systemctl enable --now hubinet-ops
     systemctl status hubinet-ops --no-pager

The R0 API bearer token is in /etc/hubinet-ops/agent.env -- do not paste
it publicly. This is a clean install: no 0.4 config, database, or
service is touched or required by this installer.
INFO
