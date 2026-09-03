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

_host_control_validate_authorized_keys() {
  local path="$1"
  if [[ -L "${path}" && ! -e "${path}" ]]; then
    die "${HOST_CONTROL_AUTHORIZED_KEYS} is a dangling symlink; refusing to modify it"
  fi
  if [[ -e "${path}" && ! -f "${path}" ]]; then
    die "${HOST_CONTROL_AUTHORIZED_KEYS} exists but is not a usable regular file; refusing to modify it"
  fi
}

# ---------------------------------------------------------------------------
# Family 2 (correction pass) -- ONE atomic, durable, idempotent
# authorized_keys mutation contract, shared by every production path that
# adds or removes a Hubinet-owned forced-command entry:
#
#   - the package-update boundaries' own runtime authorize/deauthorize
#     (deploy/lib/update-boundaries.sh);
#   - bootstrap's package-update boundary provisioning/rollback
#     (deploy/lib/bootstrap-update-boundaries.sh);
#   - bootstrap's package-scan boundary provisioning/rollback (this file).
#
# The bug this replaces: `awk ... >"${filtered}"; cat "${filtered}" >
# "${authorized_keys_path}"` TRUNCATES the live file before the filtered
# replacement is fully written back -- a crash mid-truncate can leave
# authorized_keys empty or half-rewritten -- and `printf ... >>"${
# authorized_keys_path}"` silently assumes the existing last line already
# ends in a newline. A hand-managed operator key with no final newline,
# followed by an appended Hubinet entry, concatenates the two into one
# invalid line; a later marker-based removal can then delete that whole
# concatenated line, destroying the unrelated operator key.
#
# Both functions below therefore only ever construct a COMPLETE
# replacement in a temp file in the SAME directory as the live file, fsync
# it, atomically rename it onto the live path, and fsync the containing
# directory -- exactly the write/fsync/rename/dir-fsync discipline already
# used for the maintenance fence and every other rollback-critical
# artifact in this codebase. A crash before the rename leaves the OLD file
# completely untouched; a crash after leaves the COMPLETE new file. Never
# an intermediate.
#
# Retry safety mirrors the exact fix already applied to the maintenance
# fence: a rename that already succeeded, with only ITS OWN following
# durability barrier having failed, must be re-proven on the next call
# rather than silently reported "nothing to do" from content alone -- see
# _host_control_authorized_keys_barrier's callers below.
#
# HUBINET_OPS_TEST_FAIL_HOST_SYNC (consulted only when HUBINET_OPS_TEST_
# MODE=1) is the same narrow substring fault-injection seam already used
# by deploy/lib/update-recovery.sh's own host-side durability barriers,
# reused here unmodified so one test suite covers both.
# HUBINET_OPS_TEST_FAIL_AUTHORIZED_KEYS_RENAME (same substring-match
# convention) is this module's own seam for the write/rename half: no fake
# command layer intercepts these direct filesystem calls the way `pct
# exec` mutations are intercepted, so a dedicated seam is needed to model
# a realistic ENOSPC/EIO-style failure before the atomic rename commits.
_host_control_authorized_keys_barrier() {
  local path="$1" dir needle
  dir="$(dirname -- "${path}")"
  if [[ "${HUBINET_OPS_TEST_MODE:-0}" == "1" ]]; then
    for needle in ${HUBINET_OPS_TEST_FAIL_HOST_SYNC:-}; do
      [[ -n "${needle}" && "${path}" == *"${needle}"* ]] && return 1
    done
  fi
  sync -f "${dir}"
}

_host_control_authorized_keys_rename_should_fail() {
  local path="$1" needle
  [[ "${HUBINET_OPS_TEST_MODE:-0}" == "1" ]] || return 1
  for needle in ${HUBINET_OPS_TEST_FAIL_AUTHORIZED_KEYS_RENAME:-}; do
    [[ -n "${needle}" && "${path}" == *"${needle}"* ]] && return 0
  done
  return 1
}

# _host_control_authorized_keys_test_fail_stage <point>: narrow test-only
# fault-injection seam (Family 2 correction pass, P1) for the fallible
# reads/writes that CONSTRUCT the staged replacement, distinct from
# HUBINET_OPS_TEST_FAIL_AUTHORIZED_KEYS_RENAME above (which models the
# atomic-replace half). <point> is one of: grep_read, grep_read_remove,
# copy_partial, trailing_read, entry_write -- see each call site. Inert
# whenever HUBINET_OPS_TEST_MODE is not "1".
_host_control_authorized_keys_test_fail_stage() {
  local point="$1" needle
  [[ "${HUBINET_OPS_TEST_MODE:-0}" == "1" ]] || return 1
  for needle in ${HUBINET_OPS_TEST_FAIL_AUTHORIZED_KEYS_STAGE:-}; do
    [[ "${needle}" == "${point}" ]] && return 0
  done
  return 1
}

# _host_control_authorized_keys_marker_count <path> <marker>: wraps
# `grep -cF` so a real read failure (permission, I/O error) can be
# distinguished from a legitimate zero-match answer -- see ADD's own
# caller below for why collapsing them was the confirmed P1 sibling.
_host_control_authorized_keys_marker_count() {
  local path="$1" marker="$2"
  if _host_control_authorized_keys_test_fail_stage "grep_read"; then
    return 2
  fi
  grep -cF " ${marker}" "${path}" 2>/dev/null
}

# _host_control_authorized_keys_marker_present <path> <marker>: the same
# wrapper for REMOVE's presence probe (`grep -qF`), whose exit contract
# ADD's own wrapper above deliberately mirrors: 0=present, 1=positively
# absent, anything else=read failure.
_host_control_authorized_keys_marker_present() {
  local path="$1" marker="$2"
  if _host_control_authorized_keys_test_fail_stage "grep_read_remove"; then
    return 2
  fi
  grep -qF " ${marker}" "${path}" 2>/dev/null
}

# _host_control_authorized_keys_copy_into_stage <src> <dst>: copies the
# existing live content into the staged replacement. In production this
# is exactly `cat "$src" >"$dst"`; its exit status is what the caller
# checks. The test-only fault path below models a REALISTIC partial
# read/write failure (EIO/ENOSPC partway through a real `cat`) -- some
# bytes land in <dst>, then the copy fails -- rather than a clean failure
# before anything is written, so a fix that only checked "did <dst> stay
# empty" would still be caught out by it.
_host_control_authorized_keys_copy_into_stage() {
  local src="$1" dst="$2" content
  if _host_control_authorized_keys_test_fail_stage "copy_partial"; then
    content="$(cat "${src}" 2>/dev/null)"
    printf '%s' "${content:0:$(( (${#content} + 1) / 2 ))}" >"${dst}" 2>/dev/null || true
    return 1
  fi
  cat "${src}" >"${dst}"
}

# _host_control_authorized_keys_trailing_byte <path>: wraps `tail -c1` so
# a read failure is distinguishable from "the file ends in a newline"
# (both would otherwise produce empty output).
_host_control_authorized_keys_trailing_byte() {
  local path="$1"
  if _host_control_authorized_keys_test_fail_stage "trailing_read"; then
    return 2
  fi
  tail -c1 "${path}"
}

# _host_control_authorized_keys_write_entry <dst> <text>: appends the new
# forced-command line onto the staged replacement, checked like every
# other fallible write that constructs it.
_host_control_authorized_keys_write_entry() {
  local dst="$1" text="$2"
  if _host_control_authorized_keys_test_fail_stage "entry_write"; then
    return 1
  fi
  printf '%s\n' "${text}" >>"${dst}"
}

# _host_control_authorized_keys_real_path <path>: the real regular-file
# location a mutation must stage and rename onto. A live authorized_keys
# is commonly a symlink -- PVE's own /root/.ssh/authorized_keys is
# conventionally -> /etc/pve/priv/authorized_keys -- and `mv`/`rename()`
# replaces whatever is named at its DESTINATION argument's own directory
# entry; renaming a staged file directly onto a symlink location would
# delete the symlink and put a plain regular file in its place, breaking
# whatever the symlink was for (cluster-wide sync, in PVE's case).
# Resolving to the real target first, and staging/renaming there instead,
# replaces the target's content atomically while leaving the symlink
# itself completely untouched. A path that does not exist yet, or is not
# a symlink, resolves to itself.
_host_control_authorized_keys_real_path() {
  local path="$1" resolved
  if [[ -L "${path}" ]]; then
    resolved="$(readlink -f -- "${path}")" && [[ -n "${resolved}" ]] || resolved="${path}"
  else
    resolved="${path}"
  fi
  printf '%s' "${resolved}"
}

# _host_control_authorized_keys_add <path> <marker> <line>: idempotently
# and atomically ensure exactly one forced-command entry -- <line>,
# identified by the space-delimited <marker> comment field it ends with --
# exists in the authorized_keys file at <path>. Never truncates the live
# file; never assumes it ends in a newline.
#
# Returns 0 once the marker is present AND its presence has been durably
# proven, whether THIS call just added it or an earlier, interrupted call
# already did.
_host_control_authorized_keys_add() {
  local path="$1" marker="$2" line="$3"
  local real_path dir tmp count
  local grep_output grep_status trailing_byte trailing_status

  _host_control_validate_authorized_keys "${path}"
  real_path="$(_host_control_authorized_keys_real_path "${path}")"
  dir="$(dirname -- "${real_path}")"

  count=0
  if [[ -f "${real_path}" ]]; then
    # Family 2 correction pass (P1 sibling): a read failure here must
    # never be folded into "zero matches" -- see this module's header.
    # 0 = matched (the printed count is trustworthy), 1 = grep's own
    # positive "no match" answer, anything else = the read itself
    # failed and must fail closed rather than being treated as absent.
    grep_output="$(_host_control_authorized_keys_marker_count "${real_path}" "${marker}")" \
      && grep_status=0 || grep_status=$?
    case "${grep_status}" in
      0) count="${grep_output}" ;;
      1) count=0 ;;
      *)
        log_warn "could not read ${path} to check for an existing '${marker}' entry"
        return 1
        ;;
    esac
  fi
  case "${count}" in
    0) : ;;
    1)
      # Idempotent replay: the entry is already there. Re-prove durability
      # rather than trusting that an earlier attempt's own barrier
      # succeeded -- see this module's header. `if` (not a bare statement
      # followed by `return $?`) so this is correct under `set -e`
      # regardless of how the caller wraps this function.
      if _host_control_authorized_keys_barrier "${real_path}"; then
        return 0
      else
        return 1
      fi
      ;;
    *)
      log_warn "authorized_keys at ${path} already has ${count} entries for marker '${marker}'; refusing to add another"
      return 1
      ;;
  esac

  tmp="$(mktemp "${dir}/hubinet-ops-authorized-keys.XXXXXX")" || return 1
  if [[ -f "${real_path}" ]]; then
    # Family 2 correction pass (P1): mode setup must not silently
    # continue if BOTH the reference chmod and the safe fallback fail --
    # a staged file left at the wrong mode could still be renamed live.
    if ! chmod --reference="${real_path}" "${tmp}" 2>/dev/null && ! chmod 0600 "${tmp}"; then
      rm -f -- "${tmp}"
      log_warn "could not set the staged authorized_keys replacement mode for ${path}"
      return 1
    fi
    if [[ -s "${real_path}" ]]; then
      # Family 2 correction pass (P1): the confirmed witness. Every byte
      # used to construct the staged replacement must be positively read/
      # written -- an interrupted/partial copy (EIO/ENOSPC) must discard
      # the staged file and fail here, never proceed to append onto,
      # fsync, and atomically rename a TRUNCATED copy of the live file
      # over it.
      if ! _host_control_authorized_keys_copy_into_stage "${real_path}" "${tmp}"; then
        rm -f -- "${tmp}"
        log_warn "could not copy the existing authorized_keys content at ${path} into the staged replacement"
        return 1
      fi
      # Preserve every existing byte, but never let this run's own
      # appended entry land on the same physical line as a hand-managed
      # key that does not itself end in a newline. A failed read here is
      # UNKNOWN, never "already ends in a newline".
      trailing_byte="$(_host_control_authorized_keys_trailing_byte "${real_path}")" \
        && trailing_status=0 || trailing_status=$?
      if (( trailing_status != 0 )); then
        rm -f -- "${tmp}"
        log_warn "could not determine whether ${path} ends in a newline"
        return 1
      fi
      if [[ -n "${trailing_byte}" ]]; then
        if ! printf '\n' >>"${tmp}"; then
          rm -f -- "${tmp}"
          log_warn "could not write the separator newline for the staged authorized_keys replacement for ${path}"
          return 1
        fi
      fi
    fi
  else
    if ! chmod 0600 "${tmp}"; then
      rm -f -- "${tmp}"
      log_warn "could not set the staged authorized_keys replacement mode for ${path}"
      return 1
    fi
  fi
  if ! _host_control_authorized_keys_write_entry "${tmp}" "${line}"; then
    rm -f -- "${tmp}"
    log_warn "could not write the new forced-command entry into the staged authorized_keys replacement for ${path}"
    return 1
  fi
  sync -f "${tmp}" \
    || { rm -f -- "${tmp}"; log_warn "could not flush the staged authorized_keys replacement for ${path}"; return 1; }
  if _host_control_authorized_keys_rename_should_fail "${real_path}"; then
    rm -f -- "${tmp}"
    log_warn "could not atomically replace ${path} (simulated test failure)"
    return 1
  fi
  mv -f -- "${tmp}" "${real_path}" \
    || { rm -f -- "${tmp}" 2>/dev/null || true; log_warn "could not atomically replace ${path}"; return 1; }
  if _host_control_authorized_keys_barrier "${real_path}"; then
    return 0
  else
    return 1
  fi
}

# _host_control_authorized_keys_remove <path> <marker>: idempotently and
# atomically remove every line containing " <marker>" from the
# authorized_keys file at <path>. Every other line -- an unrelated
# operator key, or a Hubinet entry a DIFFERENT run/marker owns -- is
# copied through byte-for-byte. A live path that does not exist, or that
# already has no such line, is a true no-op EXCEPT for re-proving the
# durability barrier (see this module's header: an earlier attempt's
# rename may already have removed it while its own following barrier
# failed).
_host_control_authorized_keys_remove() {
  local path="$1" marker="$2" real_path dir tmp present_status

  [[ -e "${path}" ]] || return 0
  _host_control_validate_authorized_keys "${path}"
  real_path="$(_host_control_authorized_keys_real_path "${path}")"
  dir="$(dirname -- "${real_path}")"

  # Family 2 correction pass (P1 direct sibling): the same fail-closed
  # classification as ADD's own marker check above. 0 = positively
  # PRESENT (build the removal stage below), 1 = positively ABSENT (an
  # idempotent replay -- re-prove durability, never claim removal from a
  # read this run never actually performed), anything else = the read
  # itself failed and must never be folded into "already absent".
  _host_control_authorized_keys_marker_present "${real_path}" "${marker}" \
    && present_status=0 || present_status=$?
  case "${present_status}" in
    0) : ;;
    1)
      if _host_control_authorized_keys_barrier "${real_path}"; then
        return 0
      else
        return 1
      fi
      ;;
    *)
      log_warn "could not read ${path} to check for the '${marker}' entry to remove"
      return 1
      ;;
  esac

  tmp="$(mktemp "${dir}/hubinet-ops-authorized-keys.XXXXXX")" || return 1
  # Family 2 correction pass (P1): mode setup must not silently continue
  # if BOTH the reference chmod and the safe fallback fail -- the same
  # unchecked-write family as ADD's own mode setup above.
  if ! chmod --reference="${real_path}" "${tmp}" 2>/dev/null && ! chmod 0600 "${tmp}"; then
    rm -f -- "${tmp}"
    log_warn "could not set the staged authorized_keys replacement mode for ${path}"
    return 1
  fi
  if ! awk -v marker=" ${marker}" 'index($0, marker) == 0 { print }' "${real_path}" >"${tmp}"; then
    rm -f -- "${tmp}" 2>/dev/null || true
    log_warn "could not filter the Hubinet-owned entry for marker '${marker}' out of ${path}"
    return 1
  fi
  sync -f "${tmp}" \
    || { rm -f -- "${tmp}"; log_warn "could not flush the staged authorized_keys replacement for ${path}"; return 1; }
  if _host_control_authorized_keys_rename_should_fail "${real_path}"; then
    rm -f -- "${tmp}"
    log_warn "could not atomically replace ${path} (simulated test failure)"
    return 1
  fi
  mv -f -- "${tmp}" "${real_path}" \
    || { rm -f -- "${tmp}" 2>/dev/null || true; log_warn "could not atomically replace ${path}"; return 1; }
  if _host_control_authorized_keys_barrier "${real_path}"; then
    return 0
  else
    return 1
  fi
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
  _host_control_validate_authorized_keys "${authorized_keys_path}"

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
  # Family 2 correction pass: one shared atomic/durable/idempotent
  # add-or-reprove-durable primitive -- see this module's own header above
  # for the full contract this replaces.
  local line
  line="$(printf 'command="%s",no-port-forwarding,no-agent-forwarding,no-X11-forwarding,no-pty %s %s %s' \
    "${HOST_CONTROL_HELPER_PATH}" "${key_type}" "${key_data}" "${HOST_CONTROL_AUTH_MARKER}")"
  _host_control_authorized_keys_add "${authorized_keys_path}" "${HOST_CONTROL_AUTH_MARKER}" "${line}" \
    || die "failed to durably add the Hubinet-owned forced-command authorization to ${HOST_CONTROL_AUTHORIZED_KEYS}"
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
  if [[ -n "${HOST_CONTROL_AUTH_MARKER:-}" ]]; then
    # Family 2 correction pass: one shared atomic/durable/idempotent
    # remove-or-reprove-durable primitive -- see this module's own header
    # above. Filtered by the exact marker only: an unrelated operator
    # authorized_keys line is never rewritten or removed.
    _host_control_authorized_keys_remove "${authorized_keys_path}" "${HOST_CONTROL_AUTH_MARKER}" \
      || log_warn "could not remove the Hubinet-owned forced-command authorization"
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
