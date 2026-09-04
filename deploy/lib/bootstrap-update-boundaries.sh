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
  local journal_state
  for journal_dir in ${UPDATE_BOUNDARY_JOURNAL_DIRS}; do
    journal_path="$(_host_control_host_path "${journal_dir}")"
    # Positively classified (Family A correction pass): see
    # _host_control_dir_state's own docstring -- a false ABSENT here would
    # make this run wrongly claim (and durably record) ownership of a
    # journal directory it did not create.
    journal_state="$(_host_control_dir_state "${journal_path}")"
    case "${journal_state}" in
      ABSENT)
        _host_control_install_dir 0700 "${journal_path}" \
          || die "failed to create the Hubinet operation journal ${journal_dir}"
        ledger_record update-boundary-journal-dir "${journal_path}"
        ;;
      DIRECTORY) ;;
      *)
        die "could not positively classify the Hubinet operation journal directory ${journal_dir} (${journal_state}) -- refusing to guess whether this run would be creating it before mutation"
        ;;
    esac
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

    # Family 2 correction pass: one shared atomic/durable/idempotent
    # add-or-reprove-durable primitive -- see bootstrap-host-control.sh's
    # own module header for the full contract this replaces.
    local line
    line="$(printf 'command="%s",no-port-forwarding,no-agent-forwarding,no-X11-forwarding,no-pty %s %s %s' \
      "${helper_path}" "${key_type}" "${key_data}" "${marker}")"
    _host_control_authorized_keys_add "${authorized_keys_path}" "${marker}" "${line}" \
      || die "failed to durably add the ${kind} forced-command authorization to ${HOST_CONTROL_AUTHORIZED_KEYS}"
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
  local kind key_path
  for kind in ${UPDATE_BOUNDARY_KINDS}; do
    key_path="$(update_boundary_ct_private_key "${kind}")"
    # Shared mechanism (bootstrap-host-control.sh's own
    # _host_control_probe_forced_command_boundary) -- see its docstring.
    # Fresh bootstrap always uses its own just-written defaults (root@22,
    # the pinned host-control known_hosts) because there is no existing
    # installation to inherit an endpoint from yet.
    _host_control_probe_forced_command_boundary \
      "${key_path}" "$(_endpoint_host "${PVE_ENDPOINT}")" 22 root "${HOST_CONTROL_CT_KNOWN_HOSTS}" \
      "$(_update_boundary_probe_marker "${kind}")" \
      || die "the ${kind} forced-command SSH boundary did not reject the typed probe as expected (check PVE sshd root public-key policy and the pinned host key)"
  done
}

rollback_update_boundaries() {
  local authorized_keys_path helper_path marker kind journal_dir journal_path
  authorized_keys_path="$(_host_control_host_path "${HOST_CONTROL_AUTHORIZED_KEYS}")"

  for kind in ${UPDATE_BOUNDARY_KINDS}; do
    marker="$(update_boundary_marker "${kind}")"
    if ledger_has update-boundary-authorization "${marker}"; then
      # Family 2 correction pass: one shared atomic/durable/idempotent
      # remove-or-reprove-durable primitive -- see bootstrap-host-control.sh's
      # own module header. Filtered by this run's exact marker only: an
      # unrelated operator authorized_keys line is never rewritten or removed.
      _host_control_authorized_keys_remove "${authorized_keys_path}" "${marker}" \
        || log_warn "could not remove the Hubinet-owned ${kind} forced-command authorization"
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
