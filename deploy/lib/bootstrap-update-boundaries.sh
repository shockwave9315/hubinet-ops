#!/usr/bin/env bash
# Production package-update host-control provisioning.
#
# Five privileged forced-command boundaries, one per stage of the update
# lifecycle: create the job-owned snapshot, simulate the exact plan, mutate
# packages, roll the guest back, and read health. They are five separate
# root-owned helper files behind five separate `authorized_keys` entries with
# five separate dedicated keys, and that separation is the point -- a key is
# what selects which forced command a connection may run, so one key that
# reached two helpers would silently merge two different privilege
# boundaries.
#
# The package-scan boundary provisioned by bootstrap-host-control.sh is a
# sixth, unchanged, scan-only boundary. Nothing here touches it, and nothing
# here reuses its key.
#
# This module changes no PVE API privilege. Every mutation the update
# lifecycle performs runs host-local through these root-owned forced
# commands, so the deployed PVE API identity stays exactly `Sys.Audit` plus
# `VM.Audit` -- see deploy/lib/bootstrap-identity.sh.

# One row per boundary: <kind>|<helper source basename>|<CT key basename>
#
# `execution` uses the update-plan helper, whose file name predates this
# stage; the boundary is named for what it does, not for the file.
UPDATE_BOUNDARY_KINDS="snapshot execution mutation rollback health"

_update_boundary_helper_source() {
  case "$1" in
    snapshot) printf 'hubinet-package-snapshot-helper.py' ;;
    execution) printf 'hubinet-package-update-helper.py' ;;
    mutation) printf 'hubinet-package-mutation-helper.py' ;;
    rollback) printf 'hubinet-package-rollback-helper.py' ;;
    health) printf 'hubinet-package-health-helper.py' ;;
    *) die "unknown package-update boundary kind '$1'" ;;
  esac
}

_update_boundary_key_basename() {
  case "$1" in
    snapshot) printf 'id_ed25519_snapshot' ;;
    execution) printf 'id_ed25519_execution' ;;
    mutation) printf 'id_ed25519_mutation' ;;
    rollback) printf 'id_ed25519_rollback' ;;
    health) printf 'id_ed25519_health' ;;
    *) die "unknown package-update boundary kind '$1'" ;;
  esac
}

# Each helper's own bounded structural refusal of a request that is not one
# of its typed operations. This is the acceptance probe, and it is
# deliberately the ONLY thing bootstrap asks these boundaries to do: it
# proves key, host-key pinning, sshd policy, and forced-command wiring end to
# end while creating no snapshot, mutating no package, rolling nothing back,
# and probing no workload.
_update_boundary_probe_marker() {
  case "$1" in
    snapshot) printf 'request must have the exact snapshot-operation shape' ;;
    execution) printf 'unknown host-control operation' ;;
    mutation) printf 'request must have the exact package-mutation shape' ;;
    rollback) printf 'request does not have the exact expected shape' ;;
    health) printf 'request must have the exact health-evaluation shape' ;;
    *) die "unknown package-update boundary kind '$1'" ;;
  esac
}

# Root-owned durable operation journals for the three destructive stages.
# The health boundary is read-only and journals nothing; the execution
# boundary only simulates and journals nothing either.
UPDATE_BOUNDARY_JOURNAL_DIRS="/var/lib/hubinet-ops/snapshot-operations /var/lib/hubinet-ops/package-mutation-operations /var/lib/hubinet-ops/rollback-operations"

UPDATE_BOUNDARY_CT_DIR="${HOST_CONTROL_CT_DIR}"
UPDATE_BOUNDARY_MARKER_PREFIX=""

update_boundary_ct_private_key() {
  printf '%s/%s' "${UPDATE_BOUNDARY_CT_DIR}" "$(_update_boundary_key_basename "$1")"
}

update_boundary_helper_path() {
  printf '/usr/local/libexec/hubinet-package-%s-boundary-%s' "$1" "${BOOTSTRAP_RUN_ID}"
}

update_boundary_marker() {
  printf 'hubinet-ops-package-%s-vmid-%s-%s' "$1" "${VMID}" "${BOOTSTRAP_RUN_ID}"
}

phase2d_plan_update_boundaries() {
  local kind source_name
  for kind in ${UPDATE_BOUNDARY_KINDS}; do
    source_name="$(_update_boundary_helper_source "${kind}")"
    [[ -f "${SOURCE_DIR}/deploy/${source_name}" ]] \
      || die "SOURCE_DIR (${SOURCE_DIR}) is missing deploy/${source_name}"
  done
  UPDATE_BOUNDARY_MARKER_PREFIX="hubinet-ops-package-"
}

phase8d_provision_update_boundaries() {
  log_phase "Phase 8d: provision the five package-update forced-command boundaries"

  local helper_dir_path authorized_keys_path journal_dir journal_path
  helper_dir_path="$(_host_control_host_path /usr/local/libexec)"
  authorized_keys_path="$(_host_control_host_path "${HOST_CONTROL_AUTHORIZED_KEYS}")"
  if [[ ! -d "${helper_dir_path}" ]]; then
    _host_control_install_dir 0755 "${helper_dir_path}" \
      || die "failed to create /usr/local/libexec for the Hubinet update helpers"
    ledger_record host-control-helper-dir "${helper_dir_path}"
  fi

  # Root-owned, root-only durable journals. These are what make the
  # destructive stages at-most-once across a crash, so their ownership and
  # mode are part of the boundary, not incidental.
  for journal_dir in ${UPDATE_BOUNDARY_JOURNAL_DIRS}; do
    journal_path="$(_host_control_host_path "${journal_dir}")"
    if [[ ! -d "${journal_path}" ]]; then
      _host_control_install_dir 0700 "${journal_path}" \
        || die "failed to create the Hubinet operation journal ${journal_dir}"
      ledger_record update-boundary-journal-dir "${journal_path}"
    fi
  done

  local kind helper_path marker key_path public_key_tmp
  local key_type key_data key_comment extra
  for kind in ${UPDATE_BOUNDARY_KINDS}; do
    helper_path="$(update_boundary_helper_path "${kind}")"
    marker="$(update_boundary_marker "${kind}")"
    key_path="$(update_boundary_ct_private_key "${kind}")"

    ledger_record update-boundary-helper-attempted "$(_host_control_host_path "${helper_path}")"
    _host_control_install_file 0755 \
      "${SOURCE_DIR}/deploy/$(_update_boundary_helper_source "${kind}")" \
      "$(_host_control_host_path "${helper_path}")" \
      || die "failed to install the ${kind} package-update host helper"
    ledger_record update-boundary-helper "$(_host_control_host_path "${helper_path}")"

    run_logged pct exec "${VMID}" -- ssh-keygen -q -t ed25519 -N '' -C "${marker}" -f "${key_path}" \
      || die "failed to generate the dedicated ${kind} boundary SSH key inside container ${VMID}"
    run_logged pct exec "${VMID}" -- chown hubinetops:hubinetops "${key_path}" "${key_path}.pub" \
      || die "failed to set ${kind} boundary SSH key ownership inside container ${VMID}"
    run_logged pct exec "${VMID}" -- chmod 0600 "${key_path}" \
      || die "failed to set the dedicated ${kind} boundary private-key mode"
    run_logged pct exec "${VMID}" -- chmod 0644 "${key_path}.pub" \
      || die "failed to set the dedicated ${kind} boundary public-key mode"
    ledger_record update-boundary-key "${key_path}"

    public_key_tmp="$(secret_tmpfile "/tmp/hubinet-ops-bootstrap-update-boundary-pub.XXXXXX")"
    pct exec "${VMID}" -- cat "${key_path}.pub" >"${public_key_tmp}" \
      || die "failed to read the dedicated ${kind} boundary public key"
    read -r key_type key_data key_comment extra <"${public_key_tmp}" || true
    [[ "${key_type}" == "ssh-ed25519" && "${key_data}" =~ ^[A-Za-z0-9+/]+={0,2}$ && "${key_comment}" == "${marker}" && -z "${extra:-}" ]] \
      || die "generated ${kind} boundary public key has an unexpected shape"

    _host_control_validate_authorized_keys "${authorized_keys_path}"
    if grep -qF " ${marker}" "${authorized_keys_path}"; then
      die "the ${kind} forced-command authorization marker already exists unexpectedly"
    fi
    printf 'command="%s",no-port-forwarding,no-agent-forwarding,no-X11-forwarding,no-pty %s %s %s\n' \
      "${helper_path}" "${key_type}" "${key_data}" "${marker}" \
      >>"${authorized_keys_path}" \
      || die "failed to append the Hubinet-owned ${kind} forced-command authorization"
    ledger_record update-boundary-authorization "${marker}"
  done

  _accept_update_boundaries
  log_pass "package-update host control: five dedicated keys, five root-owned forced commands, three root-only operation journals (PTY/forwarding/agent/X11 disabled)"
}

# Non-mutating acceptance. Every boundary is exercised with one deliberately
# malformed typed request, which each helper refuses structurally before it
# can do anything at all. No snapshot is created, no package is changed, no
# rollback is submitted, and no health probe runs against any guest. There is
# no generic "ping" operation, because this existing refusal is already the
# proof that the key reached exactly this forced command.
_accept_update_boundaries() {
  local kind key_path probe_output probe_status probe_request
  probe_request='{"request_version":1,"operation":"probe","target":{},"context":{}}'
  for kind in ${UPDATE_BOUNDARY_KINDS}; do
    key_path="$(update_boundary_ct_private_key "${kind}")"
    probe_output="$(printf '%s' "${probe_request}" | pct exec "${VMID}" -- runuser -u hubinetops -- \
      ssh -T -p 22 -i "${key_path}" \
      -o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes \
      -o "UserKnownHostsFile=${HOST_CONTROL_CT_KNOWN_HOSTS}" \
      -o PasswordAuthentication=no -o KbdInteractiveAuthentication=no \
      -o ForwardAgent=no -o ClearAllForwardings=yes \
      "root@$(_endpoint_host "${PVE_ENDPOINT}")" 2>/dev/null)" && probe_status=0 || probe_status=$?
    # A structured refusal proves the forced command ran. Exit 255 or an
    # empty body would instead mean SSH/key/host-key policy is unusable.
    (( probe_status != 0 && probe_status != 255 )) \
      && printf '%s' "${probe_output}" | grep -qF "$(_update_boundary_probe_marker "${kind}")" \
      || die "the ${kind} forced-command SSH boundary did not reject the typed probe as expected (check PVE sshd root public-key policy and the pinned host key)"
  done
}

rollback_update_boundaries() {
  local authorized_keys_path helper_path marker kind journal_dir journal_path filtered
  authorized_keys_path="$(_host_control_host_path "${HOST_CONTROL_AUTHORIZED_KEYS}")"
  local root_ssh_dir_path
  root_ssh_dir_path="$(_host_control_host_path /root/.ssh)"

  for kind in ${UPDATE_BOUNDARY_KINDS}; do
    marker="$(update_boundary_marker "${kind}")"
    if ledger_has update-boundary-authorization "${marker}" && [[ -f "${authorized_keys_path}" ]]; then
      # Filter by this run's exact marker only. An unrelated operator
      # authorized_keys line is never rewritten or removed.
      filtered="$(mktemp "${root_ssh_dir_path}/hubinet-ops-authorized-keys.XXXXXX")" || {
        log_warn "could not allocate a temporary authorized_keys cleanup file"
        filtered=""
      }
      if [[ -n "${filtered}" ]]; then
        if awk -v marker=" ${marker}" 'index($0, marker) == 0 { print }' \
          "${authorized_keys_path}" >"${filtered}" \
          && cat "${filtered}" >"${authorized_keys_path}"; then
          rm -f "${filtered}" || log_warn "could not remove the temporary authorized_keys cleanup file"
        else
          log_warn "could not remove the Hubinet-owned ${kind} forced-command authorization"
          rm -f "${filtered}" >/dev/null 2>&1 || true
        fi
      fi
    fi
    helper_path="$(_host_control_host_path "$(update_boundary_helper_path "${kind}")")"
    if ledger_has update-boundary-helper-attempted "${helper_path}"; then
      rm -f "${helper_path}" \
        || log_warn "could not remove the Hubinet-owned ${kind} package-update host helper"
    fi
  done

  # Only journals this run created. A journal directory that already existed
  # may hold another installation's durable at-most-once evidence and is
  # never removed.
  for journal_dir in ${UPDATE_BOUNDARY_JOURNAL_DIRS}; do
    journal_path="$(_host_control_host_path "${journal_dir}")"
    if ledger_has update-boundary-journal-dir "${journal_path}"; then
      rmdir "${journal_path}" >/dev/null 2>&1 || true
    fi
  done
}
