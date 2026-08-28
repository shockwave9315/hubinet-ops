#!/usr/bin/env bash
# Package-scan-only PVE host-control provisioning. The runtime key is
# dedicated, host-key pinned, and constrained by one forced command. This
# module never changes the read-only PVE API credential or its ACLs.

HOST_CONTROL_CT_DIR="/etc/hubinet-ops/host-control"
HOST_CONTROL_CT_PRIVATE_KEY="${HOST_CONTROL_CT_DIR}/id_ed25519"
HOST_CONTROL_CT_KNOWN_HOSTS="${HOST_CONTROL_CT_DIR}/known_hosts"
HOST_CONTROL_AUTHORIZED_KEYS="/root/.ssh/authorized_keys"
HOST_CONTROL_AUTH_MARKER=""
HOST_CONTROL_HELPER_PATH=""
HOST_CONTROL_SSH_PUBLIC_KEY_PATH=""
HOST_CONTROL_HOST_ROOT="${HUBINET_OPS_TEST_HOST_ROOT:-}"

if [[ -n "${HOST_CONTROL_HOST_ROOT}" ]]; then
  [[ "${HUBINET_OPS_TEST_MODE:-0}" == "1" && "${HOST_CONTROL_HOST_ROOT}" == /* && "${HOST_CONTROL_HOST_ROOT}" != "/" ]] \
    || die "HUBINET_OPS_TEST_HOST_ROOT is accepted only as a non-root absolute path in test mode"
fi

_host_control_host_path() {
  local logical_path="$1"
  printf '%s%s' "${HOST_CONTROL_HOST_ROOT}" "${logical_path}"
}

_host_control_install_dir() {
  local mode="$1" path="$2"
  if [[ -n "${HOST_CONTROL_HOST_ROOT}" ]]; then
    install -d -m "${mode}" "${path}"
  else
    install -d -o root -g root -m "${mode}" "${path}"
  fi
}

_host_control_install_file() {
  local mode="$1" source="$2" destination="$3"
  if [[ -n "${HOST_CONTROL_HOST_ROOT}" ]]; then
    install -m "${mode}" "${source}" "${destination}"
  else
    install -o root -g root -m "${mode}" "${source}" "${destination}"
  fi
}

_host_control_secure_root_file() {
  local path="$1"
  if [[ -z "${HOST_CONTROL_HOST_ROOT}" ]]; then
    chown root:root "${path}"
  fi
  chmod 0600 "${path}"
}

phase2c_plan_host_control() {
  [[ -f "${SOURCE_DIR}/deploy/hubinet-package-scan-helper.py" ]] \
    || die "SOURCE_DIR (${SOURCE_DIR}) is missing deploy/hubinet-package-scan-helper.py"

  local candidate
  for candidate in \
    /etc/ssh/ssh_host_ed25519_key.pub \
    /etc/ssh/ssh_host_ecdsa_key.pub \
    /etc/ssh/ssh_host_rsa_key.pub; do
    if [[ -f "$(_host_control_host_path "${candidate}")" ]]; then
      HOST_CONTROL_SSH_PUBLIC_KEY_PATH="$(_host_control_host_path "${candidate}")"
      break
    fi
  done
  [[ -n "${HOST_CONTROL_SSH_PUBLIC_KEY_PATH}" ]] \
    || die "PVE host has no supported SSH host public key to pin"
  local authorized_keys_path
  authorized_keys_path="$(_host_control_host_path "${HOST_CONTROL_AUTHORIZED_KEYS}")"
  if [[ -e "${authorized_keys_path}" && ! -f "${authorized_keys_path}" ]]; then
    die "${HOST_CONTROL_AUTHORIZED_KEYS} exists but is not a regular file; refusing to modify it"
  fi

  HOST_CONTROL_AUTH_MARKER="hubinet-ops-package-scan-vmid-${VMID}-${BOOTSTRAP_RUN_ID}"
  HOST_CONTROL_HELPER_PATH="/usr/local/libexec/hubinet-package-scan-helper-${BOOTSTRAP_RUN_ID}"
}

phase8c_provision_host_control() {
  log_phase "Phase 8c: provision package-scan-only PVE host control"

  local helper_dir_path helper_path authorized_keys_path root_ssh_dir_path
  helper_dir_path="$(_host_control_host_path /usr/local/libexec)"
  helper_path="$(_host_control_host_path "${HOST_CONTROL_HELPER_PATH}")"
  authorized_keys_path="$(_host_control_host_path "${HOST_CONTROL_AUTHORIZED_KEYS}")"
  root_ssh_dir_path="$(_host_control_host_path /root/.ssh)"
  if [[ ! -d "${helper_dir_path}" ]]; then
    _host_control_install_dir 0755 "${helper_dir_path}" \
      || die "failed to create /usr/local/libexec for the Hubinet host helper"
    ledger_record host-control-helper-dir "${helper_dir_path}"
  fi
  ledger_record host-control-helper-attempted "${helper_path}"
  _host_control_install_file 0755 \
    "${SOURCE_DIR}/deploy/hubinet-package-scan-helper.py" \
    "${helper_path}" \
    || die "failed to install the package-scan host helper"
  ledger_record host-control-helper "${helper_path}"

  run_logged pct exec "${VMID}" -- install -d -o hubinetops -g hubinetops -m 0700 "${HOST_CONTROL_CT_DIR}" \
    || die "failed to create the dedicated host-control directory inside container ${VMID}"
  ledger_record host-control-ct-dir "${HOST_CONTROL_CT_DIR}"
  run_logged pct exec "${VMID}" -- ssh-keygen -q -t ed25519 -N '' -C "${HOST_CONTROL_AUTH_MARKER}" -f "${HOST_CONTROL_CT_PRIVATE_KEY}" \
    || die "failed to generate the dedicated host-control SSH key inside container ${VMID}"
  run_logged pct exec "${VMID}" -- chown hubinetops:hubinetops "${HOST_CONTROL_CT_PRIVATE_KEY}" "${HOST_CONTROL_CT_PRIVATE_KEY}.pub" \
    || die "failed to set host-control SSH key ownership inside container ${VMID}"
  run_logged pct exec "${VMID}" -- chmod 0600 "${HOST_CONTROL_CT_PRIVATE_KEY}" \
    || die "failed to set the dedicated host-control private-key mode"
  run_logged pct exec "${VMID}" -- chmod 0644 "${HOST_CONTROL_CT_PRIVATE_KEY}.pub" \
    || die "failed to set the dedicated host-control public-key mode"

  local public_key_tmp key_type key_data key_comment extra
  public_key_tmp="$(secret_tmpfile "/tmp/hubinet-ops-bootstrap-host-control-pub.XXXXXX")"
  pct exec "${VMID}" -- cat "${HOST_CONTROL_CT_PRIVATE_KEY}.pub" >"${public_key_tmp}" \
    || die "failed to read the dedicated host-control public key"
  read -r key_type key_data key_comment extra <"${public_key_tmp}" || true
  [[ "${key_type}" == "ssh-ed25519" && "${key_data}" =~ ^[A-Za-z0-9+/]+={0,2}$ && "${key_comment}" == "${HOST_CONTROL_AUTH_MARKER}" && -z "${extra:-}" ]] \
    || die "generated host-control public key has an unexpected shape"

  if [[ ! -d "${root_ssh_dir_path}" ]]; then
    _host_control_install_dir 0700 "${root_ssh_dir_path}" \
      || die "failed to create root SSH directory for forced-command authorization"
    ledger_record host-control-root-ssh-dir "${root_ssh_dir_path}"
  fi
  if [[ ! -e "${authorized_keys_path}" ]]; then
    _host_control_install_file 0600 /dev/null "${authorized_keys_path}" \
      || die "failed to create ${HOST_CONTROL_AUTHORIZED_KEYS}"
    ledger_record host-control-authorized-keys-file "${authorized_keys_path}"
  fi
  _host_control_secure_root_file "${authorized_keys_path}" \
    || die "failed to secure ${HOST_CONTROL_AUTHORIZED_KEYS} ownership/mode"
  if grep -qF " ${HOST_CONTROL_AUTH_MARKER}" "${authorized_keys_path}"; then
    die "forced-command authorization marker already exists unexpectedly"
  fi
  printf 'command="%s",no-port-forwarding,no-agent-forwarding,no-X11-forwarding,no-pty %s %s %s\n' \
    "${HOST_CONTROL_HELPER_PATH}" "${key_type}" "${key_data}" "${HOST_CONTROL_AUTH_MARKER}" \
    >>"${authorized_keys_path}" \
    || die "failed to append the Hubinet-owned forced-command authorization"
  ledger_record host-control-authorization "${HOST_CONTROL_AUTH_MARKER}"

  local host_key_tmp known_hosts_tmp host_key_type host_key_data host_key_comment
  host_key_tmp="$(secret_tmpfile "/tmp/hubinet-ops-bootstrap-host-key.XXXXXX")"
  known_hosts_tmp="$(secret_tmpfile "/tmp/hubinet-ops-bootstrap-known-hosts.XXXXXX")"
  read -r host_key_type host_key_data host_key_comment <"${HOST_CONTROL_SSH_PUBLIC_KEY_PATH}" || true
  [[ "${host_key_type}" =~ ^(ssh-ed25519|ecdsa-sha2-nistp(256|384|521)|ssh-rsa)$ && "${host_key_data}" =~ ^[A-Za-z0-9+/]+={0,2}$ ]] \
    || die "selected PVE SSH host public key has an unexpected shape"
  printf '%s %s %s\n' "$(_endpoint_host "${PVE_ENDPOINT}")" "${host_key_type}" "${host_key_data}" >"${known_hosts_tmp}"
  run_logged pct push "${VMID}" "${known_hosts_tmp}" "${HOST_CONTROL_CT_KNOWN_HOSTS}" \
    || die "failed to pin the PVE SSH host key inside container ${VMID}"
  run_logged pct exec "${VMID}" -- chown hubinetops:hubinetops "${HOST_CONTROL_CT_KNOWN_HOSTS}" \
    || die "failed to set pinned PVE SSH host-key ownership"
  run_logged pct exec "${VMID}" -- chmod 0600 "${HOST_CONTROL_CT_KNOWN_HOSTS}" \
    || die "failed to set pinned PVE SSH host-key mode"

  # End-to-end boundary probe from the actual service user. The deliberately
  # unknown typed operation must reach the forced helper and be rejected by
  # that helper; exit 255/no structured response would instead mean SSH/key/
  # host-key policy is unusable. No remote command text is supplied.
  local probe_output probe_status probe_request
  probe_request='{"request_version":1,"operation":"probe","target":{},"context":{}}'
  probe_output="$(printf '%s' "${probe_request}" | pct exec "${VMID}" -- runuser -u hubinetops -- \
    ssh -T -p 22 -i "${HOST_CONTROL_CT_PRIVATE_KEY}" \
    -o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes \
    -o "UserKnownHostsFile=${HOST_CONTROL_CT_KNOWN_HOSTS}" \
    -o PasswordAuthentication=no -o KbdInteractiveAuthentication=no \
    -o ForwardAgent=no -o ClearAllForwardings=yes \
    "root@$(_endpoint_host "${PVE_ENDPOINT}")" 2>/dev/null)" && probe_status=0 || probe_status=$?
  (( probe_status == 2 )) \
    && printf '%s' "${probe_output}" | grep -qF 'unknown host-control operation' \
    || die "package-scan forced-command SSH boundary did not reject the typed probe as expected (check PVE sshd root public-key policy and the pinned host key)"

  log_pass "host control: dedicated key, pinned PVE SSH host key, and one package-scan-only forced command (PTY/forwarding/agent/X11 disabled)"
}

rollback_host_control() {
  if [[ -n "${VMID:-}" ]] && ledger_has host-control-ct-dir "${HOST_CONTROL_CT_DIR}"; then
    pct exec "${VMID}" -- rm -rf "${HOST_CONTROL_CT_DIR}" >/dev/null 2>&1 \
      || log_warn "could not remove Hubinet-owned host-control key directory inside preserved container ${VMID}"
  fi

  local authorized_keys_path helper_path helper_dir_path root_ssh_dir_path
  authorized_keys_path="$(_host_control_host_path "${HOST_CONTROL_AUTHORIZED_KEYS}")"
  helper_path="$(_host_control_host_path "${HOST_CONTROL_HELPER_PATH}")"
  helper_dir_path="$(_host_control_host_path /usr/local/libexec)"
  root_ssh_dir_path="$(_host_control_host_path /root/.ssh)"
  if [[ -n "${HOST_CONTROL_AUTH_MARKER:-}" && -f "${authorized_keys_path}" ]]; then
    local filtered
    filtered="$(mktemp "${root_ssh_dir_path}/hubinet-ops-authorized-keys.XXXXXX")" || {
      log_warn "could not allocate a temporary authorized_keys cleanup file"
      filtered=""
    }
    if [[ -n "${filtered}" ]]; then
      awk -v marker=" ${HOST_CONTROL_AUTH_MARKER}" 'index($0, marker) == 0 { print }' \
        "${authorized_keys_path}" >"${filtered}" \
        && _host_control_secure_root_file "${filtered}" \
        && mv "${filtered}" "${authorized_keys_path}" \
        || log_warn "could not remove the Hubinet-owned forced-command authorization"
    fi
  fi
  if ledger_has host-control-authorized-keys-file "${authorized_keys_path}" \
    && [[ -f "${authorized_keys_path}" && ! -s "${authorized_keys_path}" ]]; then
    rm -f "${authorized_keys_path}" \
      || log_warn "could not remove the empty Hubinet-created authorized_keys file"
  fi
  if [[ -n "${HOST_CONTROL_HELPER_PATH:-}" ]] \
    && ledger_has host-control-helper-attempted "${helper_path}"; then
    rm -f "${helper_path}" \
      || log_warn "could not remove the Hubinet-owned package-scan host helper"
  fi
  if ledger_has host-control-helper-dir "${helper_dir_path}"; then
    rmdir "${helper_dir_path}" >/dev/null 2>&1 || true
  fi
  if ledger_has host-control-root-ssh-dir "${root_ssh_dir_path}"; then
    rmdir "${root_ssh_dir_path}" >/dev/null 2>&1 || true
  fi
}
