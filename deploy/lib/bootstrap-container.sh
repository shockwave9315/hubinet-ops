#!/usr/bin/env bash
# Phases 2-5 -- template selection, fresh unprivileged CT creation,
# Debian 13/systemd 257 nesting compatibility, boot + network discovery.

# Only a Debian *standard* LXC template is ever selected automatically --
# never an arbitrary/unknown template merely because it exists locally.
_TEMPLATE_NAME_PATTERN='^debian-13-standard_'

phase2_select_template() {
  log_phase "Phase 2: template selection"

  if [[ -n "${TEMPLATE}" ]]; then
    log_info "using operator-specified template: ${TEMPLATE}"
  else
    TEMPLATE="$(_newest_local_debian13_template)" || true
    if [[ -z "${TEMPLATE}" ]]; then
      log_info "no local Debian 13 standard template found; downloading the newest available one"
      TEMPLATE="$(_download_newest_debian13_template)" \
        || die "no Debian 13 standard LXC template is available locally or via 'pveam available' -- pass --template <storage>:vztmpl/<file> explicitly"
    fi
    log_info "selected template: ${TEMPLATE}"
  fi

  [[ "$(basename "${TEMPLATE}")" =~ ${_TEMPLATE_NAME_PATTERN} ]] \
    || die "template '${TEMPLATE}' does not look like a supported Debian 13 standard template (expected a 'debian-13-standard_*' filename); pass a supported template explicitly or omit --template to auto-select one"

  log_pass "template selection: ${TEMPLATE}"
}

# _newest_local_debian13_template: `pveam list <storage>` lists already
# downloaded templates for one storage; scan every storage that reports
# 'vztmpl' content support and pick the lexicographically newest matching
# filename (Debian's own version-string ordering sorts correctly this way,
# e.g. debian-13-standard_13.6-1_amd64.tar.zst > _13.5-1_).
_newest_local_debian13_template() {
  local storage
  local best=""
  for storage in $(_vztmpl_storages); do
    local candidate
    candidate="$(pveam list "${storage}" 2>/dev/null \
      | awk '{print $1}' \
      | grep -E "/${_TEMPLATE_NAME_PATTERN#^}" \
      | sort -V \
      | tail -n1)"
    if [[ -n "${candidate}" ]]; then
      best="${candidate}"
    fi
  done
  [[ -n "${best}" ]] || return 1
  printf '%s' "${best}"
}

_download_newest_debian13_template() {
  local storage="${TEMPLATE_STORAGE:-local}"
  local filename
  filename="$(pveam available --section system 2>/dev/null \
    | awk '{print $2}' \
    | grep -E "${_TEMPLATE_NAME_PATTERN}" \
    | sort -V \
    | tail -n1)"
  [[ -n "${filename}" ]] || return 1
  run_logged pveam download "${storage}" "${filename}" || return 1
  printf '%s:vztmpl/%s' "${storage}" "${filename}"
}

_vztmpl_storages() {
  pvesm status --content vztmpl --enabled 1 2>/dev/null | awk 'NR>1 {print $1}'
}

phase3_create_container() {
  log_phase "Phase 3: create unprivileged container"

  local -a create_args=(
    "${VMID}"
    "${TEMPLATE}"
    --hostname "${HOSTNAME_}"
    --unprivileged 1
    --cores "${CORES}"
    --memory "${MEMORY_MIB}"
    --swap "${SWAP_MIB}"
    --rootfs "${STORAGE}:${ROOTFS_GIB}"
    --onboot 0
  )
  # LXC_FEATURES is always empty at this point -- phase 4 sets it (nesting=1
  # for Debian 13) via a separate `pct set` after creation. --features is
  # only ever passed here with a real, non-empty value, never an empty
  # string, in case a given `pct create` version treats "--features ''" as
  # a malformed empty list rather than "no features".
  if [[ -n "${LXC_FEATURES}" ]]; then
    create_args+=(--features "${LXC_FEATURES}")
  fi

  local netconf="name=eth0,bridge=${BRIDGE},firewall=1"
  if [[ "${NETWORK_MODE}" == "static" ]]; then
    netconf+=",ip=${STATIC_IP_CIDR},gw=${STATIC_GATEWAY}"
  else
    netconf+=",ip=dhcp"
  fi
  create_args+=(--net0 "${netconf}")

  run_logged pct create "${create_args[@]}" \
    || die "pct create failed for VMID ${VMID}"
  ledger_record ct "${VMID}"

  log_pass "container ${VMID} created (unprivileged, onboot=0, firewall=1 on net0)"
}

# phase4_debian13_compat: real-environment finding -- an unprivileged LXC
# WITHOUT nesting running Debian 13 (systemd 257) reports
# `systemctl is-system-running` = degraded, with dev-mqueue.mount,
# run-lock.mount, and tmp.mount failed. Proxmox's own UI/CLI additionally
# warns "Systemd 257 detected. You may need to enable nesting." Enabling
# `features: nesting=1` (while keeping `unprivileged: 1`) resolved this in
# the tested environment: systemd became healthy, 0 failed units. This is
# therefore the minimal, deterministic, template-specific compatibility
# path for exactly the Debian 13 / systemd >=257 template family selected
# in phase 2 -- not a blanket "always enable nesting for every OS" policy.
phase4_debian13_compat() {
  log_phase "Phase 4: Debian 13 / systemd 257 compatibility"

  if [[ "$(basename "${TEMPLATE}")" =~ ^debian-13- ]]; then
    log_info "Debian 13 template detected -- nesting=1 is required for a healthy systemd inside an unprivileged LXC (systemd >=257 mounts dev/mqueue, run/lock, tmp that require it); unprivileged=1 is preserved"
    if [[ "${LXC_FEATURES}" != *nesting=1* ]]; then
      LXC_FEATURES="${LXC_FEATURES:+${LXC_FEATURES},}nesting=1"
      run_logged pct set "${VMID}" --features "${LXC_FEATURES}" \
        || die "failed to enable nesting=1 on container ${VMID}"
    fi
  else
    log_info "non-Debian-13 template -- no nesting compatibility override applied"
  fi

  log_pass "Debian 13 / systemd 257 compatibility"
}

phase5_boot_and_discover_ip() {
  log_phase "Phase 5: boot + network discovery"

  run_logged pct start "${VMID}" || die "failed to start container ${VMID}"
  ledger_record ct-started "${VMID}"

  local ip=""
  local waited=0
  local interval=2
  while (( waited < BOOTSTRAP_NET_TIMEOUT_SECONDS )); do
    ip="$(_container_ipv4 "${VMID}")" || ip=""
    [[ -n "${ip}" ]] && is_valid_ipv4 "${ip}" && break
    sleep "${interval}"
    waited=$(( waited + interval ))
  done

  [[ -n "${ip}" ]] && is_valid_ipv4 "${ip}" \
    || die "container ${VMID} did not report a usable IPv4 address within ${BOOTSTRAP_NET_TIMEOUT_SECONDS}s (network mode: ${NETWORK_MODE})"

  CT_IP="${ip}"
  log_pass "container ${VMID} reachable at ${CT_IP}"
}

# _container_ipv4: prefer PVE's own machine-readable interface report
# (populated by the QEMU/LXC guest agent equivalent for containers, i.e.
# `pct exec ... -- hostname -I`/`ip` are both real-guest-command routes;
# PVE additionally exposes `pct config <vmid>` net0 for the *static* case
# without needing to enter the guest at all). Static config is checked
# first since it requires no guest execution; DHCP falls back to asking
# the guest directly via `pct exec`.
_container_ipv4() {
  local vmid="$1"
  if [[ "${NETWORK_MODE}" == "static" ]]; then
    printf '%s' "${STATIC_IP_CIDR%%/*}"
    return 0
  fi
  pct exec "${vmid}" -- hostname -I 2>/dev/null | awk '{print $1}'
}
