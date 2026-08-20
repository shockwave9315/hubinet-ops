#!/usr/bin/env bash
# Phases 2-5 -- template selection, fresh unprivileged CT creation,
# Debian 13/systemd 257 nesting compatibility, boot + network discovery.
#
# Template selection is split into a read-only planning step
# (phase2_plan_template, runs before the operator confirms the plan) and a
# provisioning step that may mutate PVE-side template caches
# (phase2b_provision_template, `pveam update`/`download` -- runs only
# after confirm_or_abort). No host mutation of any kind happens before the
# operator has seen and confirmed the plan.

# Only a Debian *standard* LXC template is ever selected automatically --
# never an arbitrary/unknown template merely because it exists locally.
_TEMPLATE_NAME_PATTERN='^debian-13-standard_'

TEMPLATE_PLAN_NOTE=""

phase2_plan_template() {
  log_phase "Phase 2: template selection (planning, read-only)"

  if [[ -n "${TEMPLATE}" ]]; then
    [[ "$(basename "${TEMPLATE}")" =~ ${_TEMPLATE_NAME_PATTERN} ]] \
      || die "template '${TEMPLATE}' does not look like a supported Debian 13 standard template (expected a 'debian-13-standard_*' filename); pass a supported template explicitly or omit --template to auto-select one"
    TEMPLATE_PLAN_NOTE="operator-specified: ${TEMPLATE}"
    log_info "using operator-specified template: ${TEMPLATE}"
    log_pass "template selection: ${TEMPLATE}"
    return 0
  fi

  local local_best
  local_best="$(_newest_local_debian13_template)" || true
  if [[ -n "${local_best}" ]]; then
    TEMPLATE="${local_best}"
    TEMPLATE_PLAN_NOTE="already cached locally: ${TEMPLATE}"
    log_info "found cached template: ${TEMPLATE}"
  else
    TEMPLATE=""
    TEMPLATE_PLAN_NOTE="not cached locally -- will be downloaded during provisioning (newest available Debian 13 standard template at that time; 'pveam update'/'download' run only after this plan is confirmed)"
    log_info "no local Debian 13 standard template found; the newest available one will be downloaded after the plan is confirmed"
  fi

  log_pass "template selection (planned): ${TEMPLATE_PLAN_NOTE}"
}

# phase2b_provision_template: the only step in this bootstrap allowed to
# run `pveam update`/`pveam download` -- always after confirm_or_abort.
phase2b_provision_template() {
  log_phase "Phase 2b: template provisioning"

  if [[ -n "${TEMPLATE}" ]]; then
    log_pass "template already resolved: ${TEMPLATE}"
    return 0
  fi

  run_logged pveam update \
    || log_warn "pveam update failed or is unavailable offline -- proceeding with the locally cached template list only"

  TEMPLATE="$(_newest_local_debian13_template)" || true
  if [[ -z "${TEMPLATE}" ]]; then
    TEMPLATE="$(_download_newest_debian13_template)" \
      || die "no Debian 13 standard LXC template is available locally or via 'pveam available' -- pass --template <storage>:vztmpl/<file> explicitly"
  fi

  [[ "$(basename "${TEMPLATE}")" =~ ${_TEMPLATE_NAME_PATTERN} ]] \
    || die "resolved template '${TEMPLATE}' does not look like a supported Debian 13 standard template"

  log_pass "template provisioned: ${TEMPLATE}"
}

# _newest_local_debian13_template: `pveam list <storage>` lists already
# downloaded templates for one storage. Candidates are collected from
# EVERY storage that reports 'vztmpl' content support, then compared by
# basename (the version-bearing filename, independent of storage-name
# prefix) so the globally newest template wins regardless of which
# storage happens to be enumerated last -- a per-storage "last one wins"
# loop would silently pick an older template merely because it lives on
# whichever storage iterated last.
_newest_local_debian13_template() {
  local storage
  local -a candidates=()
  for storage in $(_vztmpl_storages); do
    while IFS= read -r volid; do
      [[ -n "${volid}" ]] && candidates+=("${volid}")
    done < <(pveam list "${storage}" 2>/dev/null \
      | awk '{print $1}' \
      | grep -E "/${_TEMPLATE_NAME_PATTERN#^}")
  done
  [[ ${#candidates[@]} -gt 0 ]] || return 1

  local best="" best_base="" volid base newest
  for volid in "${candidates[@]}"; do
    base="$(basename "${volid}")"
    if [[ -z "${best_base}" ]]; then
      best="${volid}"
      best_base="${base}"
      continue
    fi
    newest="$(printf '%s\n%s\n' "${best_base}" "${base}" | sort -V | tail -n1)"
    if [[ "${newest}" == "${base}" && "${base}" != "${best_base}" ]]; then
      best="${volid}"
      best_base="${base}"
    fi
  done
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

  # Recheck VMID immediately before creation: closes the window between
  # planning (phase1/confirm) and this mutating step. An auto-detected
  # VMID may be safely recomputed on a collision (cheap, no operator
  # commitment was ever made to a specific auto-detected number); an
  # explicit --vmid is NEVER silently overridden -- a collision there is
  # always a hard stop, exactly like the phase1 check.
  local attempt
  for (( attempt = 1; attempt <= 5; attempt++ )); do
    if ! pct status "${VMID}" >/dev/null 2>&1; then
      break
    fi
    if [[ "${VMID_EXPLICIT}" == "1" ]]; then
      die "VMID ${VMID} was created by something else between preflight and container creation -- refusing to destroy, overwrite, adopt, or repurpose it. Re-run with a different --vmid."
    fi
    log_warn "auto-detected VMID ${VMID} was claimed by another process since planning -- recomputing"
    VMID="$(_next_free_vmid)" \
      || die "could not recompute a free VMID via 'pvesh get /cluster/nextid' after a collision"
  done
  if pct status "${VMID}" >/dev/null 2>&1; then
    die "VMID ${VMID} still exists after ${attempt} auto-detect attempts -- refusing to proceed under contention; re-run or pass --vmid explicitly"
  fi

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
