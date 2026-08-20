#!/usr/bin/env bash
# Phase 1 -- PVE host preflight. Fail closed before creating anything.
#
# Every check here is read-only: no `pct create`, no `pveum user/role/token
# add`, no firewall change, no `pveam update`/`download`, nothing mutating
# happens in this phase or in phase2_plan_template -- see
# bootstrap-proxmox-0.5.sh's top-level orchestration comment for why: no
# host mutation of any kind may occur before the operator has seen and
# confirmed the full plan (VMID, template, source commit).

phase1_preflight() {
  log_phase "Phase 1: preflight"

  if [[ "${HUBINET_OPS_TEST_MODE:-0}" != "1" ]]; then
    [[ "${EUID}" -eq 0 ]] || die "must run as root on the Proxmox host"
  fi

  require_command pct "Proxmox container control"
  require_command pveum "Proxmox user/permission management"
  require_command pveam "Proxmox appliance/template management"
  require_command pvesh "Proxmox API shell (node/storage/next-VMID introspection)"
  require_command pvesm "Proxmox storage management"
  require_command git "source commit verification (git archive of an exact, confirmed SHA)"

  # A real JSON parser is required (not optional) specifically for the
  # security-relevant checks in this bootstrap -- the exact-set PVE
  # effective-permission proof (bootstrap-identity.sh) and the discovery-
  # acceptance snapshot parsing (which runs inside the CT via python3,
  # guaranteed there by install-0.5.0-fresh.sh, so this host-side
  # requirement is for the PVE-host-side permission check only). A lexical
  # regex "JSON parser" is not an acceptable substitute for a security
  # verification gate.
  if ! command -v jq >/dev/null 2>&1 && ! command -v python3 >/dev/null 2>&1; then
    die "neither 'jq' nor 'python3' is available on this PVE host -- one of them is required to reliably parse PVE's JSON output for the effective-permission verification gate (this bootstrap will not fall back to a regex-based JSON parser for a security check)"
  fi

  case "${TLS_TRUST_MODE}" in
    ""|system) : ;;
    *) die "--tls-trust must be 'system' or omitted, got '${TLS_TRUST_MODE}'" ;;
  esac
  if [[ "${TLS_TRUST_MODE}" == "system" && -n "${PVE_CA_PATH}" ]]; then
    die "--pve-ca-path and --tls-trust system are mutually exclusive -- choose one"
  fi

  # --- VMID: explicit value is validated/checked; auto-detect mode plans
  #     a candidate now (read-only) and rechecks it immediately before
  #     `pct create` in phase3 to handle a same-host race safely. -----------
  if [[ -n "${VMID}" ]]; then
    VMID_EXPLICIT=1
    is_valid_vmid "${VMID}" || die "--vmid '${VMID}' is not a valid Proxmox VMID"
    if pct status "${VMID}" >/dev/null 2>&1; then
      die "VMID ${VMID} already exists -- refusing to destroy, overwrite, adopt, or repurpose it. Choose a different --vmid or remove it yourself first."
    fi
  else
    VMID_EXPLICIT=0
    VMID="$(_next_free_vmid)" \
      || die "could not auto-detect the next free VMID via 'pvesh get /cluster/nextid'; pass --vmid explicitly"
    log_info "auto-detected next free VMID: ${VMID} (via pvesh get /cluster/nextid; rechecked immediately before container creation)"
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

  # --- DNS resolver, hostname PVE endpoint mode only ---------------------
  # Validated here (read-only planning) rather than only at
  # firewall-generation time, because the declared resolver now also
  # becomes an authoritative container-creation argument (phase3's
  # --nameserver, see bootstrap-container.sh) -- this bootstrap must never
  # create a container it already knows cannot be given a verifiable
  # nameserver. Literal-IP endpoints need no resolver at all and ignore
  # --dns-resolver if it was given.
  # _endpoint_host is defined in bootstrap-firewall.sh; safe to call here
  # because bootstrap-proxmox-0.5.sh sources every lib/bootstrap-*.sh file
  # before any phase function is ever invoked, regardless of source order.
  local endpoint_host_for_dns
  endpoint_host_for_dns="$(_endpoint_host "${PVE_ENDPOINT}")"
  if is_valid_ipv4 "${endpoint_host_for_dns}"; then
    [[ -z "${DNS_RESOLVER_IP}" ]] \
      || log_warn "--dns-resolver was given but --pve-endpoint uses a literal IP address -- no DNS resolution is needed inside the container, so --dns-resolver is ignored"
    DNS_RESOLVER_IP=""
  else
    [[ -n "${DNS_RESOLVER_IP}" ]] \
      || die "--pve-endpoint ('${PVE_ENDPOINT}') uses a hostname, not a literal IP -- pass --dns-resolver <your-internal-resolver-ip> so the container can be given an authoritative, verifiable nameserver, or reconfigure --pve-endpoint with a literal IP and omit --dns-resolver entirely"
    is_valid_ipv4 "${DNS_RESOLVER_IP}" || die "--dns-resolver '${DNS_RESOLVER_IP}' is not a valid IPv4 address"
  fi

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
  is_positive_int "${BOOTSTRAP_DISCOVERY_TIMEOUT_SECONDS}" || die "--discovery-timeout must be a positive integer (seconds)"

  # --- enough disk/resources exist where reasonably checkable ------------
  _storage_has_free_space "${STORAGE}" "${ROOTFS_GIB}" \
    || die "storage '${STORAGE}' does not report enough free space for a ${ROOTFS_GIB}GiB rootfs"

  log_pass "preflight"
}

# _next_free_vmid: the real, verified Proxmox mechanism for an unused VMID
# -- `/cluster/nextid` (see `man pvesh` / PVE API docs). Returns a bare
# integer. This is called again, unconditionally, immediately before
# `pct create` in phase3 to recheck for a same-host race between planning
# and creation; an auto-detected ID may be safely recomputed on collision,
# but this function is never used to override an operator-supplied
# --vmid (see VMID_EXPLICIT in phase1/phase3).
_next_free_vmid() {
  local raw
  raw="$(pvesh get /cluster/nextid --output-format json 2>/dev/null)" || return 1
  raw="${raw//\"/}"
  raw="$(printf '%s' "${raw}" | tr -d '[:space:]')"
  is_valid_vmid "${raw}" || return 1
  printf '%s' "${raw}"
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

# _storage_has_free_space: `pvesm status`'s Total/Used/Available columns
# are reported in KiB (1024-byte units) by PVE, NOT bytes -- confirmed
# against real `pvesm status` output. A prior version of this function
# compared a byte-scaled requirement against this KiB-scaled value
# directly, which understated available space by a factor of ~1024 (an
# 8 GiB request was effectively compared as if it needed ~8 TiB free) and
# would have falsely rejected ordinary real Proxmox storage. Fail closed
# (never silently skip the check) if the reported value cannot be
# reliably parsed as an integer -- a preflight capacity check that can be
# silently bypassed by an unexpected output shape is not a real gate.
_storage_has_free_space() {
  local storage="$1" required_gib="$2"
  local required_kib=$(( required_gib * 1024 * 1024 ))
  # pvesm status columns: Name Type Status Total Used Available %
  local avail_kib
  avail_kib="$(pvesm status --storage "${storage}" 2>/dev/null | awk 'NR==2 {print $6}')"
  [[ "${avail_kib}" =~ ^[0-9]+$ ]] \
    || die "could not reliably parse available free space for storage '${storage}' from 'pvesm status --storage ${storage}' (expected an integer KiB value in column 6) -- refusing to guess; verify the storage and PVE version manually, or pass a different --storage"
  (( avail_kib >= required_kib ))
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
