#!/usr/bin/env bash
# Phase U1 -- prove that --vmid names the expected Hubinet installation
# before any managed-state mutation is attempted.
#
# Uses only facts the existing bootstrap already creates (see AGENTS.md
# task prompt section 6 / bootstrap-identity.sh, bootstrap-host-control.sh):
# the CT-side host-control public-key comment, the PVE-host authorized_keys
# forced-command marker/path, and the PVE user/token comment run-id, all
# cross-checked against one recovered BOOTSTRAP_RUN_ID. No installation
# manifest is introduced -- this is read-only verification of an existing
# ownership chain, never a new durable fact.
#
# Every check below is read-only. Any mismatch is a hard `die` (fail
# closed) before update_ownership_verify's caller records the first
# rollback-relevant ledger marker.

UPDATE_VMID_RUN_ID=""
UPDATE_HELPER_PATH=""
UPDATE_AUTH_MARKER=""

_update_require_ct_path() {
  local vmid="$1" path="$2" label="$3"
  pct exec "${vmid}" -- test -e "${path}" >/dev/null 2>&1 \
    || die "ownership verification failed: container ${vmid} is missing ${label} (${path}) -- this does not look like an existing Hubinet Ops installation"
}

update_ownership_verify() {
  local vmid="$1"
  log_phase "Phase U1: verify installation ownership (VMID ${vmid})"

  local status_output
  status_output="$(pct status "${vmid}" 2>&1)" \
    || die "ownership verification failed: container ${vmid} does not exist or 'pct status' failed: ${status_output}"
  [[ "${status_output}" == *running* ]] \
    || die "ownership verification failed: container ${vmid} is not running (${status_output}) -- start it before updating"

  _update_require_ct_path "${vmid}" /etc/hubinet-ops/inventory.yaml "the R0 config file"
  _update_require_ct_path "${vmid}" /etc/hubinet-ops/agent.env "agent.env"
  _update_require_ct_path "${vmid}" "${HOST_CONTROL_CT_PRIVATE_KEY}.pub" "the host-control public key"
  _update_require_ct_path "${vmid}" /opt/hubinet-ops/app "the application payload directory"
  _update_require_ct_path "${vmid}" /opt/hubinet-ops/requirements.txt "requirements.txt"
  _update_require_ct_path "${vmid}" /opt/hubinet-ops/.venv "the service virtualenv"
  _update_require_ct_path "${vmid}" /etc/systemd/system/hubinet-ops.service "the systemd unit"
  _update_require_ct_path "${vmid}" /var/lib/hubinet-ops/authority.db "the authority database"

  local pubkey_tmp key_type key_data key_comment extra
  pubkey_tmp="$(mktemp /tmp/hubinet-ops-update-hostcontrol-pub.XXXXXX)"
  chmod 0600 "${pubkey_tmp}"
  pct exec "${vmid}" -- cat "${HOST_CONTROL_CT_PRIVATE_KEY}.pub" >"${pubkey_tmp}" 2>/dev/null \
    || { rm -f "${pubkey_tmp}"; die "ownership verification failed: could not read the host-control public key from container ${vmid}"; }
  read -r key_type key_data key_comment extra <"${pubkey_tmp}" || true
  rm -f "${pubkey_tmp}"
  [[ "${key_type}" == "ssh-ed25519" && -n "${key_data}" && -z "${extra:-}" ]] \
    || die "ownership verification failed: container ${vmid}'s host-control public key has an unexpected shape"

  local marker_prefix="hubinet-ops-package-scan-vmid-${vmid}-"
  [[ "${key_comment}" == "${marker_prefix}"* ]] \
    || die "ownership verification failed: container ${vmid}'s host-control public-key comment ('${key_comment}') does not carry the expected marker shape (${marker_prefix}<run-id>) -- refusing to adopt an installation this bootstrap did not create for this exact VMID"
  UPDATE_VMID_RUN_ID="${key_comment#"${marker_prefix}"}"
  [[ -n "${UPDATE_VMID_RUN_ID}" ]] \
    || die "ownership verification failed: could not recover a non-empty run-id from container ${vmid}'s host-control public-key comment"

  UPDATE_AUTH_MARKER="hubinet-ops-package-scan-vmid-${vmid}-${UPDATE_VMID_RUN_ID}"

  local authorized_keys_path
  authorized_keys_path="$(_host_control_host_path "${HOST_CONTROL_AUTHORIZED_KEYS}")"
  [[ -f "${authorized_keys_path}" ]] \
    || die "ownership verification failed: ${HOST_CONTROL_AUTHORIZED_KEYS} does not exist on this PVE host"

  local match_count
  match_count="$(grep -cF " ${UPDATE_AUTH_MARKER}" "${authorized_keys_path}" || true)"
  [[ "${match_count}" == "1" ]] \
    || die "ownership verification failed: expected exactly one authorized_keys line carrying marker '${UPDATE_AUTH_MARKER}', found ${match_count}"

  local auth_line
  auth_line="$(grep -F " ${UPDATE_AUTH_MARKER}" "${authorized_keys_path}")"

  # Extract the forced-command helper path from
  #   command="<path>",no-port-forwarding,... ssh-ed25519 AAAA... <marker>
  # -- a fixed, narrow shape (never eval'd, never used as arbitrary text).
  local command_field
  command_field="${auth_line#*command=\"}"
  command_field="${command_field%%\"*}"
  [[ -n "${command_field}" && "${command_field}" != "${auth_line}" ]] \
    || die "ownership verification failed: could not parse a forced-command helper path from the matched authorized_keys line"

  local expected_helper_path="/usr/local/libexec/hubinet-package-scan-helper-${UPDATE_VMID_RUN_ID}"
  [[ "${command_field}" == "${expected_helper_path}" ]] \
    || die "ownership verification failed: authorized_keys forced-command path ('${command_field}') does not match the expected Hubinet helper shape ('${expected_helper_path}')"
  UPDATE_HELPER_PATH="${command_field}"

  local helper_host_path
  helper_host_path="$(_host_control_host_path "${UPDATE_HELPER_PATH}")"
  [[ -f "${helper_host_path}" ]] \
    || die "ownership verification failed: expected helper file '${UPDATE_HELPER_PATH}' does not exist on this PVE host"
  [[ -x "${helper_host_path}" ]] \
    || die "ownership verification failed: expected helper file '${UPDATE_HELPER_PATH}' is not executable"
  if [[ -z "${HOST_CONTROL_HOST_ROOT}" ]]; then
    local helper_owner
    helper_owner="$(stat -c '%U' "${helper_host_path}" 2>/dev/null || true)"
    [[ "${helper_owner}" == "root" ]] \
      || die "ownership verification failed: expected helper file '${UPDATE_HELPER_PATH}' is not root-owned (owner: ${helper_owner:-unknown})"
  fi

  local user_list_file
  user_list_file="$(mktemp /tmp/hubinet-ops-update-userlist.XXXXXX.json)"
  chmod 0600 "${user_list_file}"
  pveum user list --output-format json >"${user_list_file}" 2>/dev/null \
    || { rm -f "${user_list_file}"; die "ownership verification failed: could not read the PVE user list"; }
  _json_list_has_string_field_schema "${user_list_file}" "userid" \
    || { rm -f "${user_list_file}"; die "ownership verification failed: PVE user list did not match the expected JSON shape"; }
  local user_comment
  user_comment="$(_json_list_field_value "${user_list_file}" "userid" "${PVE_USER}" "comment")"
  rm -f "${user_list_file}"
  [[ "${user_comment}" == *"run=${UPDATE_VMID_RUN_ID}"* ]] \
    || die "ownership verification failed: PVE user '${PVE_USER}' comment does not carry run=${UPDATE_VMID_RUN_ID} -- this PVE identity does not match the CT's own recovered run-id"

  local token_list_file
  token_list_file="$(mktemp /tmp/hubinet-ops-update-tokenlist.XXXXXX.json)"
  chmod 0600 "${token_list_file}"
  pveum user token list "${PVE_USER}" --output-format json >"${token_list_file}" 2>/dev/null \
    || { rm -f "${token_list_file}"; die "ownership verification failed: could not read the PVE token list for ${PVE_USER}"; }
  _json_list_has_string_field_schema "${token_list_file}" "tokenid" \
    || { rm -f "${token_list_file}"; die "ownership verification failed: PVE token list for ${PVE_USER} did not match the expected JSON shape"; }
  local token_comment
  token_comment="$(_json_list_field_value "${token_list_file}" "tokenid" "${PVE_TOKEN_ID}" "comment")"
  rm -f "${token_list_file}"
  [[ "${token_comment}" == *"run=${UPDATE_VMID_RUN_ID}"* ]] \
    || die "ownership verification failed: PVE token '${PVE_FULL_TOKEN_ID}' comment does not carry run=${UPDATE_VMID_RUN_ID}"

  _verify_effective_permissions

  log_pass "installation ownership verified: VMID ${vmid}, run-id ${UPDATE_VMID_RUN_ID}, helper ${UPDATE_HELPER_PATH}, PVE identity exactly ${PVE_REQUIRED_PRIVS}"
}
