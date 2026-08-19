#!/usr/bin/env bash
set -Eeuo pipefail

# Hubinet Ops 0.5 R0 -- one-shot Proxmox bootstrap.
#
# Automates the manual procedure documented in
# docs/operations/0.5-r0-operational-activation.md sections 1-4: creates a
# fresh unprivileged Debian 13 LXC, deploys the R0 read-only runtime into
# it (deploy/install-0.5.0-fresh.sh, unmodified), provisions a
# least-privilege PVE read-only identity (Sys.Audit+VM.Audit only, never
# a mutation privilege), provisions PVE TLS trust material (never
# verify=false), applies the mandatory nftables egress/ingress boundary,
# and only then starts the service and enables CT boot -- in that order,
# never reversed.
#
# This script does NOT add runtime mutation capability of any kind. R0
# remains read-only end to end; `pct`/`pveum` invoked here are one-shot,
# human-invoked PVE-host provisioning commands, not Hubinet Ops runtime
# code, and are structurally separate from app/inventory_runtime.py's own
# GET-only production transport.
#
# Run as root ON the Proxmox VE host itself (this script drives `pct`/
# `pveum`/`pveam`/`nft` locally; it is not SSH-orchestrated).
#
# See docs/operations/0.5-r0-operational-activation.md for the full
# reference procedure this automates, including everything an operator
# may still want to inspect/verify by hand.

BOOTSTRAP_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/bootstrap-common.sh
source "${BOOTSTRAP_SCRIPT_DIR}/lib/bootstrap-common.sh"

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

VMID="110"
HOSTNAME_="hubinet-ops"
STORAGE=""
TEMPLATE=""
TEMPLATE_STORAGE="local"
BRIDGE="vmbr0"
NETWORK_MODE="dhcp"
STATIC_IP_CIDR=""
STATIC_GATEWAY=""
CORES="1"
MEMORY_MIB="1024"
SWAP_MIB="512"
ROOTFS_GIB="8"
LXC_FEATURES=""
HA_SOURCE_CIDR=""
PVE_ENDPOINT=""
PVE_CA_PATH=""
TLS_TRUST_MODE="system"
DNS_RESOLVER_IP=""
FRESHNESS_SECONDS="300"
HA_DISPLAY_NAME="Home Proxmox"
SOURCE_DIR="$(cd "${BOOTSTRAP_SCRIPT_DIR}/.." && pwd)"
BOOTSTRAP_ASSUME_YES="0"
BOOTSTRAP_NON_INTERACTIVE="0"
BOOTSTRAP_CLEANUP_ON_FAILURE="0"
BOOTSTRAP_NET_TIMEOUT_SECONDS="${BOOTSTRAP_NET_TIMEOUT_SECONDS:-120}"
BOOTSTRAP_SERVICE_TIMEOUT_SECONDS="${BOOTSTRAP_SERVICE_TIMEOUT_SECONDS:-60}"
BOOTSTRAP_TEST_MODE="${BOOTSTRAP_TEST_MODE:-0}"

CT_IP=""
CT_CA_BUNDLE_PATH=""
BOOTSTRAP_CA_SOURCE_PATH=""
PVE_TOKEN_SECRET_FILE=""
R0_API_BEARER_TOKEN=""

usage() {
  cat <<'USAGE'
Usage: bootstrap-proxmox-0.5.sh [options]

Creates a fresh unprivileged Debian 13 LXC, deploys the Hubinet Ops 0.5 R0
read-only runtime into it, provisions a least-privilege PVE credential and
the mandatory firewall boundary, and starts the service.

Required:
  --ha-source <CIDR>        Home Assistant host/subnet allowed to reach
                             TCP 8787 (e.g. 203.0.113.50/32). No default --
                             this must be explicit.

Commonly overridden:
  --vmid <N>                 default: 110
  --hostname <name>          default: hubinet-ops
  --storage <name>           default: auto-detected
  --template <volid>         default: auto-selected/downloaded newest
                              local Debian 13 standard template
  --bridge <name>             default: vmbr0
  --network dhcp|static       default: dhcp
  --ip <address/prefix>       required if --network static
  --gateway <address>         required if --network static
  --pve-endpoint <url>        default: auto-detected https://<host-ip>:8006
  --pve-ca-path <path>        default: auto-detected /etc/pve/pve-root-ca.pem
  --tls-trust system|ca-file  default: system (only if no CA bundle found)
  --dns-resolver <ip>         required only if --pve-endpoint uses a
                               hostname rather than a literal IP
  --freshness-seconds <N>     default: 300
  --display-name <text>       default: "Home Proxmox"
  --source-dir <path>         default: this script's own repository root

Resources:
  --cores <N>                default: 1
  --memory <MiB>              default: 1024
  --swap <MiB>                 default: 512
  --rootfs-size <GiB>          default: 8

Behavior:
  --non-interactive           fail closed instead of prompting for any
                               missing required value
  --yes, -y                    skip interactive confirmations
  --cleanup-on-failure          destroy the container automatically if
                               bootstrap fails after creating it (default:
                               preserve it for forensic diagnosis)
  -h, --help                    show this help and exit

PVE identity (user hubinetops@pve, role HubinetOpsR0Auditor, token
r0-readonly) and the nftables firewall boundary are always created; there
are no flags to weaken or skip them.
USAGE
}

# ---------------------------------------------------------------------------
# Argument parsing -- no eval, no arbitrary command construction from
# user input; every value below is used only as a quoted argv element or
# validated against a fixed pattern before use.
# ---------------------------------------------------------------------------

while [[ $# -gt 0 ]]; do
  case "$1" in
    --vmid) VMID="$2"; shift 2 ;;
    --hostname) HOSTNAME_="$2"; shift 2 ;;
    --storage) STORAGE="$2"; shift 2 ;;
    --template) TEMPLATE="$2"; shift 2 ;;
    --template-storage) TEMPLATE_STORAGE="$2"; shift 2 ;;
    --bridge) BRIDGE="$2"; shift 2 ;;
    --network) NETWORK_MODE="$2"; shift 2 ;;
    --ip) STATIC_IP_CIDR="$2"; shift 2 ;;
    --gateway) STATIC_GATEWAY="$2"; shift 2 ;;
    --cores) CORES="$2"; shift 2 ;;
    --memory) MEMORY_MIB="$2"; shift 2 ;;
    --swap) SWAP_MIB="$2"; shift 2 ;;
    --rootfs-size) ROOTFS_GIB="$2"; shift 2 ;;
    --ha-source) HA_SOURCE_CIDR="$2"; shift 2 ;;
    --pve-endpoint) PVE_ENDPOINT="$2"; shift 2 ;;
    --pve-ca-path) PVE_CA_PATH="$2"; shift 2 ;;
    --tls-trust) TLS_TRUST_MODE="$2"; shift 2 ;;
    --dns-resolver) DNS_RESOLVER_IP="$2"; shift 2 ;;
    --freshness-seconds) FRESHNESS_SECONDS="$2"; shift 2 ;;
    --display-name) HA_DISPLAY_NAME="$2"; shift 2 ;;
    --source-dir) SOURCE_DIR="$2"; shift 2 ;;
    --non-interactive) BOOTSTRAP_NON_INTERACTIVE="1"; shift ;;
    --yes|-y) BOOTSTRAP_ASSUME_YES="1"; shift ;;
    --cleanup-on-failure) BOOTSTRAP_CLEANUP_ON_FAILURE="1"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1 (see --help)" ;;
  esac
done

# ---------------------------------------------------------------------------
# Interactive fill-in for missing required values (only --ha-source has no
# safe default). Skipped entirely in --non-interactive mode, which fails
# closed instead if a required value is still missing.
# ---------------------------------------------------------------------------

if [[ -z "${HA_SOURCE_CIDR}" ]]; then
  if [[ "${BOOTSTRAP_NON_INTERACTIVE}" == "1" ]]; then
    die "--ha-source is required in --non-interactive mode"
  fi
  if [[ ! -t 0 ]]; then
    die "--ha-source is required (no controlling terminal to prompt on)"
  fi
  read -r -p "Home Assistant host/subnet allowed to reach TCP 8787 (CIDR, e.g. 203.0.113.50/32): " HA_SOURCE_CIDR
fi

if [[ "${NETWORK_MODE}" == "static" && ( -z "${STATIC_IP_CIDR}" || -z "${STATIC_GATEWAY}" ) ]]; then
  if [[ "${BOOTSTRAP_NON_INTERACTIVE}" == "1" ]]; then
    die "--network static requires --ip and --gateway in --non-interactive mode"
  fi
  [[ -t 0 ]] || die "--network static requires --ip and --gateway (no controlling terminal to prompt on)"
  [[ -n "${STATIC_IP_CIDR}" ]] || read -r -p "Static IP address/prefix for the container (e.g. 203.0.113.110/24): " STATIC_IP_CIDR
  [[ -n "${STATIC_GATEWAY}" ]] || read -r -p "Gateway address: " STATIC_GATEWAY
fi

# ---------------------------------------------------------------------------
# Rollback/cleanup framework -- see the module docstrings for the exact
# semantics (PVE identity always rolled back on failure; CT preserved by
# default, destroyed only with --cleanup-on-failure; service/onboot never
# left exposed on any failure path).
# ---------------------------------------------------------------------------

BOOTSTRAP_LEDGER="$(mktemp /tmp/hubinet-ops-bootstrap-ledger.XXXXXX)"
BOOTSTRAP_SECRET_FILES="$(mktemp /tmp/hubinet-ops-bootstrap-secrets.XXXXXX)"
chmod 0600 "${BOOTSTRAP_LEDGER}" "${BOOTSTRAP_SECRET_FILES}"

# shellcheck source=lib/bootstrap-preflight.sh
source "${BOOTSTRAP_SCRIPT_DIR}/lib/bootstrap-preflight.sh"
# shellcheck source=lib/bootstrap-container.sh
source "${BOOTSTRAP_SCRIPT_DIR}/lib/bootstrap-container.sh"
# shellcheck source=lib/bootstrap-identity.sh
source "${BOOTSTRAP_SCRIPT_DIR}/lib/bootstrap-identity.sh"
# shellcheck source=lib/bootstrap-deploy.sh
source "${BOOTSTRAP_SCRIPT_DIR}/lib/bootstrap-deploy.sh"
# shellcheck source=lib/bootstrap-firewall.sh
source "${BOOTSTRAP_SCRIPT_DIR}/lib/bootstrap-firewall.sh"
# shellcheck source=lib/bootstrap-finish.sh
source "${BOOTSTRAP_SCRIPT_DIR}/lib/bootstrap-finish.sh"

rollback_on_failure() {
  local exit_code="$1"

  log_warn "bootstrap failed (exit ${exit_code}) -- running cleanup"

  if ledger_has service-started "${VMID}"; then
    pct exec "${VMID}" -- systemctl disable --now hubinet-ops >/dev/null 2>&1 \
      || log_warn "could not stop/disable hubinet-ops inside container ${VMID} during cleanup -- manual check required"
  fi

  if ledger_has ct "${VMID}"; then
    pct set "${VMID}" --onboot 0 >/dev/null 2>&1 || true
  fi

  # PVE identity objects created in THIS run are always rolled back
  # automatically: they are cheap to recreate, carry no material forensic
  # value, and (per the fresh-install-only conflict policy) would block
  # every future retry if left behind. Reverse creation order.
  if ledger_has pve-acl-token "${PVE_FULL_TOKEN_ID:-}"; then
    pveum acl delete / --tokens "${PVE_FULL_TOKEN_ID}" --roles "${PVE_ROLE}" >/dev/null 2>&1 \
      || log_warn "could not remove ACL grant for token ${PVE_FULL_TOKEN_ID}"
  fi
  if ledger_has pve-token "${PVE_FULL_TOKEN_ID:-}"; then
    pveum user token remove "${PVE_USER}" "${PVE_TOKEN_ID}" >/dev/null 2>&1 \
      || log_warn "could not remove token ${PVE_FULL_TOKEN_ID}"
  fi
  if ledger_has pve-acl-user "${PVE_USER:-}"; then
    pveum acl delete / --users "${PVE_USER}" --roles "${PVE_ROLE}" >/dev/null 2>&1 \
      || log_warn "could not remove ACL grant for user ${PVE_USER}"
  fi
  if ledger_has pve-role "${PVE_ROLE:-}"; then
    pveum role delete "${PVE_ROLE}" >/dev/null 2>&1 \
      || log_warn "could not remove role ${PVE_ROLE}"
  fi
  if ledger_has pve-user "${PVE_USER:-}"; then
    pveum user delete "${PVE_USER}" >/dev/null 2>&1 \
      || log_warn "could not remove user ${PVE_USER}"
  fi

  # The CT itself is preserved by default for forensic diagnosis --
  # destroyed only if the operator explicitly opted in.
  if ledger_has ct "${VMID}"; then
    if [[ "${BOOTSTRAP_CLEANUP_ON_FAILURE}" == "1" ]]; then
      log_warn "--cleanup-on-failure set -- destroying container ${VMID}"
      pct stop "${VMID}" >/dev/null 2>&1 || true
      pct destroy "${VMID}" >/dev/null 2>&1 \
        || log_warn "could not destroy container ${VMID} during cleanup -- manual removal required (pct stop ${VMID} && pct destroy ${VMID})"
    else
      log_warn "preserving container ${VMID} for forensic diagnosis. It is NOT started for boot (onboot=0) and its Hubinet Ops service is stopped/disabled if it was ever started. Re-run with --cleanup-on-failure to destroy it automatically, or remove it yourself: pct stop ${VMID} && pct destroy ${VMID}"
    fi
  fi

  cleanup_secret_files
  rm -f -- "${BOOTSTRAP_LEDGER}"
  log_warn "bootstrap did not complete -- exiting ${exit_code}"
}

_bootstrap_exit_trap() {
  local exit_code=$?
  trap - EXIT
  if (( exit_code == 0 )); then
    cleanup_secret_files
    rm -f -- "${BOOTSTRAP_LEDGER}"
  else
    rollback_on_failure "${exit_code}"
  fi
  exit "${exit_code}"
}

trap _bootstrap_exit_trap EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# ---------------------------------------------------------------------------
# Orchestration -- fixed phase order, never reordered.
# ---------------------------------------------------------------------------

phase1_preflight
phase2_select_template

log_info "Plan: create VMID ${VMID} (${HOSTNAME_}) from ${TEMPLATE} on storage ${STORAGE}, bridge ${BRIDGE}, network ${NETWORK_MODE}; PVE endpoint ${PVE_ENDPOINT}; HA source ${HA_SOURCE_CIDR} -> TCP 8787 only."
confirm_or_abort "Proceed with this plan?"

phase3_create_container
phase4_debian13_compat
phase5_boot_and_discover_ip
phase6_pve_identity
phase7_tls_trust
phase8_deploy_source
phase9_generate_config
phase10_firewall
phase11_start_service
phase12_acceptance
phase13_finish

exit 0
