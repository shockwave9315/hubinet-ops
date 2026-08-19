#!/usr/bin/env bash
# Phase 1 -- PVE host preflight. Fail closed before creating anything.
#
# Every check here is read-only: no `pct create`, no `pveum user/role/token
# add`, no firewall change, nothing mutating happens in this phase. If any
# check fails, the process must exit non-zero before phase 2 ever runs.

phase1_preflight() {
  log_phase "Phase 1: preflight"

  if [[ "${BOOTSTRAP_TEST_MODE:-0}" != "1" ]]; then
    [[ "${EUID}" -eq 0 ]] || die "must run as root on the Proxmox host"
  fi

  require_command pct "Proxmox container control"
  require_command pveum "Proxmox user/permission management"
  require_command pveam "Proxmox appliance/template management"
  require_command pvesh "Proxmox API shell (node/storage introspection)"
  require_command pvesm "Proxmox storage management"

  is_valid_vmid "${VMID}" || die "--vmid '${VMID}' is not a valid Proxmox VMID"

  # --- VMID must not already exist ------------------------------------
  if pct status "${VMID}" >/dev/null 2>&1; then
    die "VMID ${VMID} already exists -- refusing to destroy, overwrite, adopt, or repurpose it. Choose a different --vmid or remove it yourself first."
  fi

  # --- storage must exist and support container rootdirs --------------
  if [[ -z "${STORAGE}" ]]; then
    STORAGE="$(_detect_default_container_storage)" \
      || die "could not auto-detect a storage supporting container rootdirs; pass --storage explicitly (see 'pvesm status' on this host)"
    log_info "auto-detected storage: ${STORAGE}"
  fi
  _storage_supports_rootdir "${STORAGE}" \
    || die "storage '${STORAGE}' does not exist or does not support container rootdirs (content type 'rootdir')"

  # --- bridge must exist ------------------------------------------------
  _bridge_exists "${BRIDGE}" \
    || die "bridge '${BRIDGE}' does not exist on this host (check 'ip link show' / PVE network configuration)"

  # --- template situation understood -----------------------------------
  # Resolved fully in phase 2 (template selection/download); here we only
  # confirm the appliance manager itself is queryable.
  pveam update >/dev/null 2>&1 || log_warn "pveam update failed or is unavailable offline -- proceeding with locally cached template list only"

  # --- required source repository/release payload exists ----------------
  [[ -d "${SOURCE_DIR}/app" ]] || die "SOURCE_DIR (${SOURCE_DIR}) does not look like a Hubinet Ops 0.5 checkout -- app/ is missing"
  [[ -f "${SOURCE_DIR}/deploy/install-0.5.0-fresh.sh" ]] \
    || die "SOURCE_DIR (${SOURCE_DIR}) is missing deploy/install-0.5.0-fresh.sh"
  [[ -f "${SOURCE_DIR}/requirements.txt" ]] || die "SOURCE_DIR (${SOURCE_DIR}) is missing requirements.txt"

  # --- HA source CIDR is valid -------------------------------------------
  is_valid_ipv4_cidr "${HA_SOURCE_CIDR}" \
    || die "--ha-source '${HA_SOURCE_CIDR}' is not a valid IPv4 CIDR (e.g. 203.0.113.50/32)"

  # --- PVE API endpoint can be determined ---------------------------------
  if [[ -z "${PVE_ENDPOINT}" ]]; then
    PVE_ENDPOINT="$(_detect_pve_endpoint)" \
      || die "could not auto-detect the PVE API endpoint; pass --pve-endpoint https://<host-or-ip>:8006 explicitly"
    log_info "auto-detected PVE endpoint: ${PVE_ENDPOINT}"
  fi
  is_valid_https_url "${PVE_ENDPOINT}" \
    || die "--pve-endpoint '${PVE_ENDPOINT}' is not a valid https://host[:port] URL"

  # --- static network arguments, if requested ----------------------------
  if [[ "${NETWORK_MODE}" == "static" ]]; then
    [[ -n "${STATIC_IP_CIDR}" ]] || die "--network static requires --ip <address/prefix>"
    is_valid_ipv4_cidr "${STATIC_IP_CIDR}" || die "--ip '${STATIC_IP_CIDR}' is not a valid IPv4 CIDR"
    [[ -n "${STATIC_GATEWAY}" ]] || die "--network static requires --gateway <address>"
    is_valid_ipv4 "${STATIC_GATEWAY}" || die "--gateway '${STATIC_GATEWAY}' is not a valid IPv4 address"
  elif [[ "${NETWORK_MODE}" != "dhcp" ]]; then
    die "--network must be 'dhcp' or 'static', got '${NETWORK_MODE}'"
  fi

  is_positive_int "${CORES}" || die "--cores must be a positive integer"
  is_positive_int "${MEMORY_MIB}" || die "--memory must be a positive integer (MiB)"
  [[ "${SWAP_MIB}" =~ ^[0-9]+$ ]] || die "--swap must be a non-negative integer (MiB)"
  is_positive_int "${ROOTFS_GIB}" || die "--rootfs-size must be a positive integer (GiB)"

  # --- enough disk/resources exist where reasonably checkable ------------
  _storage_has_free_space "${STORAGE}" "${ROOTFS_GIB}" \
    || die "storage '${STORAGE}' does not report enough free space for a ${ROOTFS_GIB}GiB rootfs"

  log_pass "preflight"
}

_detect_default_container_storage() {
  # pvesm status --content rootdir --enabled 1: one line per usable storage.
  # Header + rows; first data row's first column is the storage name.
  pvesm status --content rootdir --enabled 1 2>/dev/null \
    | awk 'NR==2 {print $1; found=1} END {exit found ? 0 : 1}'
}

_storage_supports_rootdir() {
  local storage="$1"
  pvesm status --content rootdir --enabled 1 2>/dev/null \
    | awk -v want="${storage}" 'NR>1 && $1==want {found=1} END {exit found ? 0 : 1}'
}

_storage_has_free_space() {
  local storage="$1" required_gib="$2"
  local required_bytes=$(( required_gib * 1024 * 1024 * 1024 ))
  # pvesm status columns: Name Type Status Total Used Available %
  local avail_bytes
  avail_bytes="$(pvesm status --storage "${storage}" 2>/dev/null | awk 'NR==2 {print $6}')"
  [[ "${avail_bytes}" =~ ^[0-9]+$ ]] || { log_warn "could not parse free space for storage '${storage}' -- skipping this check"; return 0; }
  (( avail_bytes >= required_bytes ))
}

_bridge_exists() {
  local bridge="$1"
  pvesh get /nodes/"$(_local_node_name)"/network --output-format json 2>/dev/null \
    | grep -Eq "\"iface\"[[:space:]]*:[[:space:]]*\"${bridge}\"" \
    && return 0
  # Fallback: direct kernel view, in case pvesh's network listing is
  # unavailable in a given PVE version/test double.
  ip link show "${bridge}" >/dev/null 2>&1
}

_local_node_name() {
  if [[ -n "${BOOTSTRAP_NODE_NAME:-}" ]]; then
    printf '%s' "${BOOTSTRAP_NODE_NAME}"
    return 0
  fi
  hostname
}

_detect_pve_endpoint() {
  local ip
  ip="$(_local_primary_ipv4)" || return 1
  [[ -n "${ip}" ]] || return 1
  printf 'https://%s:8006' "${ip}"
}

_local_primary_ipv4() {
  # Machine-readable route lookup rather than scraping `ip addr` human
  # tables: `ip -j route get` emits one JSON object with a "prefsrc" field
  # naming the source address the kernel would actually use.
  if command -v ip >/dev/null 2>&1; then
    ip -4 -j route get 1.1.1.1 2>/dev/null \
      | grep -o '"prefsrc":"[0-9.]*"' \
      | head -n1 \
      | cut -d'"' -f4
  fi
}
