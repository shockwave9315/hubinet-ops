#!/usr/bin/env bash
# In-place lifecycle for the five package-update forced-command boundaries.
#
# This module is what turns a pre-activation Hubinet installation into an
# activated one, and what keeps an already-activated one's privileged helpers
# byte-exact with the target commit. It handles five boundaries -- snapshot,
# execution (plan simulation), mutation, rollback, health -- plus their
# root-only operation journals and the one `package_update:` block that
# activates the lifecycle in the CT's configuration.
#
# ## The rule that shapes every function here
#
# A FAILED product update must not leave a NEW privileged access path behind.
# Creating a forced-command boundary means creating a key, an
# `authorized_keys` entry, and a root-owned helper that can mutate a workload;
# if the update then fails, all three must go. So every artifact this module
# creates is recorded in the run's durable journal before it exists, and
# `update_boundaries_rollback` removes exactly what THIS run created and
# restores exactly what this run replaced -- never more.
#
# Two consequences follow, and both are deliberate:
#
# - An `authorized_keys` line is only ever removed by matching this run's own
#   exact marker comment. An unrelated operator entry -- or a Hubinet entry
#   from the ORIGINAL bootstrap, which this run did not create -- is never
#   rewritten or deleted.
# - A journal directory that already existed is never removed. It may hold
#   another operation's durable at-most-once evidence, and destroying that to
#   tidy up a failed update would be strictly worse than leaving an empty
#   directory behind.
#
# The package-scan boundary is a separate, unchanged boundary with its own
# key and its own forced command. Nothing here rotates, rewrites, or reuses
# it: an installation being activated keeps scanning exactly as it did.

UPDATE_BOUNDARY_CT_DIR="/etc/hubinet-ops/host-control"
UPDATE_BOUNDARY_CONFIG_PATH="/etc/hubinet-ops/inventory.yaml"
UPDATE_BOUNDARY_JOURNAL_DIRS="/var/lib/hubinet-ops/snapshot-operations /var/lib/hubinet-ops/package-mutation-operations /var/lib/hubinet-ops/rollback-operations"

# Per-kind classification, set by update_boundaries_classify:
# "" (unchanged) | "changed" | "absent".
UPDATE_BOUNDARY_PLAN_SNAPSHOT=""
UPDATE_BOUNDARY_PLAN_EXECUTION=""
UPDATE_BOUNDARY_PLAN_MUTATION=""
UPDATE_BOUNDARY_PLAN_ROLLBACK=""
UPDATE_BOUNDARY_PLAN_HEALTH=""
# "" (already activated) | "add" (the config has no package_update block).
UPDATE_BOUNDARY_CONFIG_PLAN=""

_update_boundary_kinds() {
  printf 'snapshot execution mutation rollback health'
}

_update_boundary_source_name() {
  case "$1" in
    snapshot) printf 'deploy/hubinet-package-snapshot-helper.py' ;;
    execution) printf 'deploy/hubinet-package-update-helper.py' ;;
    mutation) printf 'deploy/hubinet-package-mutation-helper.py' ;;
    rollback) printf 'deploy/hubinet-package-rollback-helper.py' ;;
    health) printf 'deploy/hubinet-package-health-helper.py' ;;
    *) die "unknown package-update boundary kind '$1'" ;;
  esac
}

_update_boundary_key_path() {
  case "$1" in
    snapshot) printf '%s/id_ed25519_snapshot' "${UPDATE_BOUNDARY_CT_DIR}" ;;
    execution) printf '%s/id_ed25519_execution' "${UPDATE_BOUNDARY_CT_DIR}" ;;
    mutation) printf '%s/id_ed25519_mutation' "${UPDATE_BOUNDARY_CT_DIR}" ;;
    rollback) printf '%s/id_ed25519_rollback' "${UPDATE_BOUNDARY_CT_DIR}" ;;
    health) printf '%s/id_ed25519_health' "${UPDATE_BOUNDARY_CT_DIR}" ;;
    *) die "unknown package-update boundary kind '$1'" ;;
  esac
}

# The installed helper path. Derived from the installation's own run id --
# exactly how the scan helper's path is derived -- so this updater reads the
# same artifact bootstrap created rather than guessing a name.
_update_boundary_path() {
  printf '/usr/local/libexec/hubinet-package-%s-boundary-%s' "$1" "${UPDATE_INSTALLATION_RUN_ID}"
}

_update_boundary_host_path() {
  _host_control_host_path "$(_update_boundary_path "$1")"
}

# This run's marker for an authorized_keys entry it creates itself. An entry
# created by the original bootstrap carries the INSTALLATION run id instead
# and is deliberately never matched by this.
_update_boundary_marker() {
  printf 'hubinet-ops-package-%s-vmid-%s-%s' "$1" "${VMID}" "${UPDATE_RUN_ID}"
}

_update_boundary_plan_var() {
  case "$1" in
    snapshot) printf 'UPDATE_BOUNDARY_PLAN_SNAPSHOT' ;;
    execution) printf 'UPDATE_BOUNDARY_PLAN_EXECUTION' ;;
    mutation) printf 'UPDATE_BOUNDARY_PLAN_MUTATION' ;;
    rollback) printf 'UPDATE_BOUNDARY_PLAN_ROLLBACK' ;;
    health) printf 'UPDATE_BOUNDARY_PLAN_HEALTH' ;;
    *) die "unknown package-update boundary kind '$1'" ;;
  esac
}

update_boundary_plan() {
  local name
  name="$(_update_boundary_plan_var "$1")"
  printf '%s' "${!name}"
}

_update_boundary_set_plan() {
  local name
  name="$(_update_boundary_plan_var "$1")"
  printf -v "${name}" '%s' "$2"
}

# ---------------------------------------------------------------------------
# Phase U2 -- classification. Reads only; mutates nothing.
# ---------------------------------------------------------------------------

update_boundaries_classify() {
  local kind installed_tmp target_tmp installed_path
  for kind in $(_update_boundary_kinds); do
    installed_path="$(_update_boundary_host_path "${kind}")"
    target_tmp="$(secret_tmpfile "/tmp/hubinet-ops-update-boundary.XXXXXX")"
    _update_target_file_to_file "$(_update_boundary_source_name "${kind}")" "${target_tmp}" \
      || die "target commit ${SOURCE_HEAD_SHA} has no $(_update_boundary_source_name "${kind}") -- refusing to plan an update against an unreadable target"
    if [[ ! -f "${installed_path}" ]]; then
      # A pre-activation installation, or one whose boundary was removed.
      # Provisioning it is a NEW privileged access path, and is tracked and
      # rolled back as one.
      _update_boundary_set_plan "${kind}" absent
      continue
    fi
    installed_tmp="$(secret_tmpfile "/tmp/hubinet-ops-update-boundary.XXXXXX")"
    cat "${installed_path}" >"${installed_tmp}" 2>/dev/null || : >"${installed_tmp}"
    if _update_files_differ_exact "${installed_tmp}" "${target_tmp}"; then
      _update_boundary_set_plan "${kind}" changed
    else
      _update_boundary_set_plan "${kind}" ""
    fi
  done

  local config_tmp
  config_tmp="$(secret_tmpfile "/tmp/hubinet-ops-update-boundary-config.XXXXXX")"
  pct exec "${VMID}" -- cat "${UPDATE_BOUNDARY_CONFIG_PATH}" >"${config_tmp}" 2>/dev/null \
    || die "could not read ${UPDATE_BOUNDARY_CONFIG_PATH} from container ${VMID}"
  if grep -q '^package_update:' "${config_tmp}"; then
    UPDATE_BOUNDARY_CONFIG_PLAN=""
  else
    UPDATE_BOUNDARY_CONFIG_PLAN="add"
  fi
}

update_boundaries_plan_summary() {
  local kind plan created=0 replaced=0
  for kind in $(_update_boundary_kinds); do
    plan="$(update_boundary_plan "${kind}")"
    case "${plan}" in
      absent) created=$((created + 1)) ;;
      changed) replaced=$((replaced + 1)) ;;
    esac
  done
  if (( created == 0 && replaced == 0 )) && [[ -z "${UPDATE_BOUNDARY_CONFIG_PLAN}" ]]; then
    printf 'unchanged -- all five forced-command boundaries already match the target commit and the lifecycle is already activated'
    return
  fi
  printf '%d boundary/boundaries newly provisioned (helper + dedicated key + forced-command entry), %d replaced in place; configuration %s' \
    "${created}" "${replaced}" \
    "$( [[ -n "${UPDATE_BOUNDARY_CONFIG_PLAN}" ]] && printf 'gains the package_update activation block' || printf 'already activates the lifecycle' )"
}

# ---------------------------------------------------------------------------
# Phase U3 -- staging. The old service is still healthy and untouched.
# ---------------------------------------------------------------------------

update_boundaries_stage() {
  local kind plan helper_tmp staged
  for kind in $(_update_boundary_kinds); do
    plan="$(update_boundary_plan "${kind}")"
    [[ -n "${plan}" ]] || continue
    helper_tmp="$(mktemp /tmp/hubinet-ops-update-boundary-stage.XXXXXX)"
    # Read from the EXACT approved commit, never the mutable worktree: this
    # runs before activation's own immediately-before-mutation HEAD recheck,
    # so a worktree read could stage content nobody confirmed.
    git -C "${SOURCE_DIR}" show "${SOURCE_HEAD_SHA}:$(_update_boundary_source_name "${kind}")" >"${helper_tmp}" 2>/dev/null \
      || { rm -f "${helper_tmp}"; die "failed to read $(_update_boundary_source_name "${kind}") from the exact approved commit ${SOURCE_HEAD_SHA}"; }
    [[ -s "${helper_tmp}" ]] \
      || { rm -f "${helper_tmp}"; die "target commit ${SOURCE_HEAD_SHA} produced an empty $(_update_boundary_source_name "${kind}") -- refusing to stage it"; }
    staged="$(_update_boundary_host_path "${kind}").staged-${UPDATE_RUN_ID}"
    ledger_record update-boundary-staged "${staged}"
    _host_control_install_file 0755 "${helper_tmp}" "${staged}" \
      || { rm -f "${helper_tmp}"; die "failed to stage the ${kind} package-update boundary helper"; }
    rm -f "${helper_tmp}"
  done
}

update_boundaries_stage_cleanup() {
  local kind staged
  for kind in $(_update_boundary_kinds); do
    staged="$(_update_boundary_host_path "${kind}").staged-${UPDATE_RUN_ID}"
    if ledger_has update-boundary-staged "${staged}" \
      && ! ledger_has update-boundary-activated "${kind}"; then
      rm -f "${staged}" 2>/dev/null || true
    fi
  done
}

# ---------------------------------------------------------------------------
# Phase U4 -- activation. Inside the maintenance window.
# ---------------------------------------------------------------------------

update_boundaries_activate() {
  local kind plan live staged rollback_copy journal_dir journal_path
  for journal_dir in ${UPDATE_BOUNDARY_JOURNAL_DIRS}; do
    journal_path="$(_host_control_host_path "${journal_dir}")"
    if [[ ! -d "${journal_path}" ]]; then
      update_journal_record update-boundary-journal-created "${journal_path}"
      # Test-only (Family 1 correction pass): the journal-created marker is
      # now durable, but the directory it describes does not exist yet --
      # proves a restart can reconstruct that this run owns creating it.
      _update_test_kill_checkpoint "boundary-journal-created-${journal_dir##*/}"
      _host_control_install_dir 0700 "${journal_path}" \
        || die "failed to create the Hubinet operation journal ${journal_dir}"
    fi
  done

  for kind in $(_update_boundary_kinds); do
    plan="$(update_boundary_plan "${kind}")"
    [[ -n "${plan}" ]] || continue
    live="$(_update_boundary_host_path "${kind}")"
    staged="${live}.staged-${UPDATE_RUN_ID}"
    rollback_copy="${live}.rollback-${UPDATE_RUN_ID}"

    if [[ "${plan}" == "changed" ]]; then
      cp "${live}" "${rollback_copy}" \
        || die "failed to preserve the active ${kind} boundary helper before activation"
      _update_durability_barrier_host "${rollback_copy}"
      update_journal_record update-boundary-activated "${kind}"
      # Test-only (Family 1 correction pass, F1-D): the pre-update helper
      # is preserved and the "activated" (replaced) marker is durable, but
      # the staged target has not been moved live yet.
      _update_test_kill_checkpoint "boundary-activated-${kind}"
      mv "${staged}" "${live}" \
        || die "failed to activate the staged ${kind} boundary helper (same-path atomic rename)"
      # Test-only (Family 1 correction pass, F1-D): the replacement is
      # live. Recovery must restore the exact preserved pre-update helper.
      _update_test_kill_checkpoint "boundary-replaced-${kind}"
      continue
    fi

    # A NEW privileged access path. The journal marker is written BEFORE the
    # helper, the key, and the authorization exist, so a crash at any point
    # after this line leaves a durable record that rollback must undo them.
    update_journal_record update-boundary-created "${kind}"
    # Test-only (Family 1 correction pass, F1-A): the created marker is
    # durable, but the helper itself does not exist at this live path yet.
    _update_test_kill_checkpoint "boundary-created-${kind}"
    mv "${staged}" "${live}" \
      || die "failed to install the new ${kind} boundary helper"
    _update_durability_barrier_host "${live}"
    # Test-only (Family 1 correction pass, F1-B): the helper is installed,
    # but its dedicated key and forced-command authorization do not exist
    # yet -- recovery must remove the helper it finds here, not treat its
    # mere presence as a fully provisioned boundary.
    _update_test_kill_checkpoint "boundary-helper-installed-${kind}"
    _update_boundary_create_key "${kind}"
    _update_boundary_authorize "${kind}"
    ledger_record update-boundary-activated "${kind}"
  done

  if [[ -n "${UPDATE_BOUNDARY_CONFIG_PLAN}" ]]; then
    _update_boundary_activate_config
  fi
}

_update_boundary_create_key() {
  local kind="$1" key_path
  key_path="$(_update_boundary_key_path "${kind}")"
  run_logged pct exec "${VMID}" -- install -d -o hubinetops -g hubinetops -m 0700 "${UPDATE_BOUNDARY_CT_DIR}" \
    || die "failed to ensure the host-control directory inside container ${VMID}"
  # Never overwrite an existing private key: a key file already at this path
  # may be the one an existing authorization trusts, and replacing it would
  # silently break that boundary while leaving its authorization in place.
  if pct exec "${VMID}" -- test -e "${key_path}" >/dev/null 2>&1; then
    die "the ${kind} boundary private key already exists at ${key_path} inside container ${VMID} but its forced-command helper does not -- refusing to overwrite key material; resolve this manually"
  fi
  run_logged pct exec "${VMID}" -- ssh-keygen -q -t ed25519 -N '' -C "$(_update_boundary_marker "${kind}")" -f "${key_path}" \
    || die "failed to generate the dedicated ${kind} boundary SSH key inside container ${VMID}"
  run_logged pct exec "${VMID}" -- chown hubinetops:hubinetops "${key_path}" "${key_path}.pub" \
    || die "failed to set ${kind} boundary SSH key ownership inside container ${VMID}"
  run_logged pct exec "${VMID}" -- chmod 0600 "${key_path}" \
    || die "failed to set the dedicated ${kind} boundary private-key mode"
  run_logged pct exec "${VMID}" -- chmod 0644 "${key_path}.pub" \
    || die "failed to set the dedicated ${kind} boundary public-key mode"
}

_update_boundary_authorize() {
  local kind="$1" marker authorized_keys_path public_key_tmp line
  local key_type key_data key_comment extra
  marker="$(_update_boundary_marker "${kind}")"
  authorized_keys_path="$(_host_control_host_path "${HOST_CONTROL_AUTHORIZED_KEYS}")"
  public_key_tmp="$(secret_tmpfile "/tmp/hubinet-ops-update-boundary-pub.XXXXXX")"
  pct exec "${VMID}" -- cat "$(_update_boundary_key_path "${kind}").pub" >"${public_key_tmp}" \
    || die "failed to read the dedicated ${kind} boundary public key"
  read -r key_type key_data key_comment extra <"${public_key_tmp}" || true
  [[ "${key_type}" == "ssh-ed25519" && "${key_data}" =~ ^[A-Za-z0-9+/]+={0,2}$ && "${key_comment}" == "${marker}" && -z "${extra:-}" ]] \
    || die "generated ${kind} boundary public key has an unexpected shape"
  # Family 2 correction pass: one shared atomic/durable/idempotent
  # add-or-reprove-durable primitive -- see bootstrap-host-control.sh's
  # own module header for the full contract this replaces.
  line="$(printf 'command="%s",no-port-forwarding,no-agent-forwarding,no-X11-forwarding,no-pty %s %s %s' \
    "$(_update_boundary_path "${kind}")" "${key_type}" "${key_data}" "${marker}")"
  _host_control_authorized_keys_add "${authorized_keys_path}" "${marker}" "${line}" \
    || die "failed to durably add the ${kind} forced-command authorization to ${HOST_CONTROL_AUTHORIZED_KEYS}"
}

# Read one scalar out of the installation's OWN existing configuration at an
# arbitrary bounded key path (e.g. `source pve_endpoint`, or `package_scan
# host_control host`). Prints the scalar (quotes stripped) and exits 0 if the
# exact key path is present; prints nothing if it is not -- callers decide
# what an absent key means (an error, or a runtime default), this only
# reports what is literally written in the file.
#
# Standard library only. PyYAML is not guaranteed present on a Proxmox host,
# and every other static read this updater performs is likewise a bounded
# scan rather than an import -- see _update_target_authority_schema. This
# walks indentation to find the exact nested key path, so a same-named key
# under any other section can never be mistaken for it.
_update_boundary_config_scalar() {
  local config_file="$1"
  shift
  python3 -c '
import re
import sys

wanted = tuple(sys.argv[1:-1])
depth = 0
with open(sys.argv[-1], encoding="utf-8") as handle:
    for raw in handle:
        line = raw.rstrip("\n")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        match = re.match(r"([A-Za-z0-9_]+):\s*(.*)$", line.strip())
        if match is None:
            continue
        key, inline = match.groups()
        # Two spaces per level, exactly as bootstrap generates it.
        if indent != depth * 2:
            if indent > depth * 2:
                continue
            depth = indent // 2
        if depth >= len(wanted) or key != wanted[depth]:
            continue
        if depth == len(wanted) - 1:
            print(inline.strip().strip("\"" + chr(39)))
            break
        depth += 1
' "$@" "${config_file}" 2>/dev/null
}

# _update_boundary_effective_host_control_field <field> <config_file>: the
# SAME effective value app/inventory_runtime_config.py's parse_r0_runtime_
# config would compute for package_scan.host_control.<field> on this
# installation's OWN existing configuration -- an explicit scalar when
# present, or the identical runtime default when the field is omitted.
#
# The update boundaries reach the SAME PVE SSH endpoint the scan boundary
# already reaches -- it is the one configured source -- so the endpoint facts
# are taken from the running installation rather than re-derived from a flag
# this updater does not have. That also means an operator who moved their
# endpoint has moved it for every boundary at once, instead of leaving the new
# ones pointed somewhere the old one is not.
#
# The activation path only ever needs these four fields (host, port, user,
# known_hosts_path -- see update_boundaries_activate's own module header).
# An EARLIER version of this reader (_update_boundary_scan_host_control_field)
# demanded every one of the four be LITERALLY present in the YAML, which
# disagreed with parse_r0_runtime_config: a perfectly valid, already-running
# installation that omits port/user/known_hosts_path (or derives host from
# source.pve_endpoint, exactly as the runtime already does) failed
# activation and rolled back a config change that was never actually
# invalid. Fixed by reproducing the SAME four runtime defaults here -- never
# inventing a new one, and never applying this fallback to any other
# package_scan.host_control field.
_update_boundary_effective_host_control_field() {
  local field="$1" config_file="$2" value default_pve_endpoint

  value="$(_update_boundary_config_scalar "${config_file}" package_scan host_control "${field}")"
  if [[ -n "${value}" ]]; then
    printf '%s' "${value}"
    return 0
  fi

  case "${field}" in
    host)
      # Same fallback as parse_r0_runtime_config's own `default_host =
      # urlsplit(transport_locator).hostname`.
      default_pve_endpoint="$(_update_boundary_config_scalar "${config_file}" source pve_endpoint)"
      [[ -n "${default_pve_endpoint}" ]] \
        || die "cannot activate the package-update lifecycle: ${UPDATE_BOUNDARY_CONFIG_PATH} has no package_scan.host_control.host and no source.pve_endpoint to derive it from"
      value="$(python3 -c '
import sys
from urllib.parse import urlsplit

hostname = urlsplit(sys.argv[1]).hostname
if hostname:
    print(hostname)
' "${default_pve_endpoint}" 2>/dev/null)"
      [[ -n "${value}" ]] \
        || die "cannot activate the package-update lifecycle: source.pve_endpoint (${default_pve_endpoint}) in ${UPDATE_BOUNDARY_CONFIG_PATH} has no host-control hostname"
      ;;
    port) value="22" ;;
    user) value="root" ;;
    known_hosts_path) value="/etc/hubinet-ops/host-control/known_hosts" ;;
    *) die "internal error: no runtime default is defined for package_scan.host_control.${field}" ;;
  esac
  printf '%s' "${value}"
}

# The activation block is APPENDED, never merged: it is one self-contained
# top-level YAML key, and appending it leaves every other line of the
# operator's configuration byte-for-byte as it was. The updater still never
# regenerates configuration.
_update_boundary_activate_config() {
  local backup_ct_path="${UPDATE_BOUNDARY_CONFIG_PATH}.rollback-${UPDATE_RUN_ID}"
  local block_tmp merged_tmp current_tmp
  local endpoint_host endpoint_port endpoint_user endpoint_known_hosts

  current_tmp="$(secret_tmpfile "/tmp/hubinet-ops-update-config-read.XXXXXX")"
  pct exec "${VMID}" -- cat "${UPDATE_BOUNDARY_CONFIG_PATH}" >"${current_tmp}" \
    || die "failed to read ${UPDATE_BOUNDARY_CONFIG_PATH} from container ${VMID}"
  # Resolved BEFORE anything is preserved or written: a configuration this
  # updater cannot read the endpoint out of must fail before it has changed
  # a byte, not halfway through appending a block.
  endpoint_host="$(_update_boundary_effective_host_control_field host "${current_tmp}")"
  endpoint_port="$(_update_boundary_effective_host_control_field port "${current_tmp}")"
  endpoint_user="$(_update_boundary_effective_host_control_field user "${current_tmp}")"
  endpoint_known_hosts="$(_update_boundary_effective_host_control_field known_hosts_path "${current_tmp}")"

  run_logged pct exec "${VMID}" -- cp "${UPDATE_BOUNDARY_CONFIG_PATH}" "${backup_ct_path}" \
    || die "failed to preserve ${UPDATE_BOUNDARY_CONFIG_PATH} before activating the update lifecycle"
  _update_durability_barrier_ct "${backup_ct_path}"
  update_journal_record update-boundary-config-activated "${VMID}"
  # Test-only (Family 1 correction pass, F1-C): the pre-activation
  # configuration is preserved and durable, and the config-activated
  # marker is durable, but the live configuration still has NOT been
  # overwritten yet -- recovery's rollback must restore from the
  # preserved backup, which at this point is identical to the still-live
  # content anyway.
  _update_test_kill_checkpoint "boundary-config-marker-before-write"

  merged_tmp="$(secret_tmpfile "/tmp/hubinet-ops-update-config-write.XXXXXX")"
  cat "${current_tmp}" >"${merged_tmp}"
  block_tmp="$(secret_tmpfile "/tmp/hubinet-ops-update-config-block.XXXXXX")"
  cat >"${block_tmp}" <<YAML

# Added by deploy/update-proxmox-0.5.sh when this installation was activated
# for operator-triggered package updates. Execution-boundary information
# only: no VMID, no resource id, no per-guest setting, and no managed-resource
# list. Every stage's timeout stays code-owned.
#
# The host, port, user, and pinned known_hosts are the SAME ones the existing
# package-scan boundary uses -- one configured source, one SSH endpoint. Only
# the private keys differ, because the key is what selects which forced
# command a connection may run.
package_update:
  enabled: true
  host_control:
    host: "${endpoint_host}"
    port: ${endpoint_port}
    user: "${endpoint_user}"
    known_hosts_path: "${endpoint_known_hosts}"
    snapshot_private_key_path: "$(_update_boundary_key_path snapshot)"
    execution_private_key_path: "$(_update_boundary_key_path execution)"
    mutation_private_key_path: "$(_update_boundary_key_path mutation)"
    rollback_private_key_path: "$(_update_boundary_key_path rollback)"
    health_private_key_path: "$(_update_boundary_key_path health)"
YAML
  cat "${block_tmp}" >>"${merged_tmp}"
  grep -q '^package_update:' "${merged_tmp}" \
    || die "internal error: the package_update activation block was not written"
  run_logged pct push "${VMID}" "${merged_tmp}" "${UPDATE_BOUNDARY_CONFIG_PATH}" \
    || die "failed to write the activated ${UPDATE_BOUNDARY_CONFIG_PATH} into container ${VMID}"
  run_logged pct exec "${VMID}" -- chown root:hubinetops "${UPDATE_BOUNDARY_CONFIG_PATH}" \
    || die "failed to restore ${UPDATE_BOUNDARY_CONFIG_PATH} ownership inside container ${VMID}"
  run_logged pct exec "${VMID}" -- chmod 0640 "${UPDATE_BOUNDARY_CONFIG_PATH}" \
    || die "failed to restore ${UPDATE_BOUNDARY_CONFIG_PATH} mode inside container ${VMID}"
  _update_durability_barrier_ct "${UPDATE_BOUNDARY_CONFIG_PATH}"
  # Test-only (Family 1 correction pass, F1-C): the NEW activated
  # configuration is now durably live. Recovery must still restore the
  # preserved pre-activation content from the durable backup path BEFORE
  # any created boundary's credentials are deleted (see update_boundaries_
  # rollback's own config-first ordering).
  _update_test_kill_checkpoint "boundary-config-written"
}

# ---------------------------------------------------------------------------
# Rollback. Undoes exactly what THIS run created or replaced.
# ---------------------------------------------------------------------------

update_boundaries_rollback() {
  local kind live rollback_copy restore_tmp

  # The configuration goes back FIRST, and it is not optional.
  #
  # This rollback is about to delete the five private keys this run created.
  # A configuration left saying `enabled: true` while pointing at keys that no
  # longer exist would fail the restored service's own startup closed -- the
  # loader deliberately refuses a missing privileged credential -- so a failed
  # update would not merely fail, it would leave the installation unable to
  # come back at all. Every branch here therefore hard stops rather than
  # skipping: preserving the journal and every artifact for manual recovery is
  # strictly safer than proceeding to delete keys a live config still names.
  if ledger_has update-boundary-config-activated "${VMID}"; then
    local backup_ct_path="${UPDATE_BOUNDARY_CONFIG_PATH}.rollback-${UPDATE_RUN_ID}"
    pct exec "${VMID}" -- test -e "${backup_ct_path}" >/dev/null 2>&1 \
      || _update_rollback_hard_stop "the preserved pre-activation ${UPDATE_BOUNDARY_CONFIG_PATH} (${backup_ct_path}) is absent inside container ${VMID}; restore it manually before retrying -- the activated configuration still names key material this rollback is about to remove"
    pct exec "${VMID}" -- cp "${backup_ct_path}" "${UPDATE_BOUNDARY_CONFIG_PATH}" >/dev/null 2>&1 \
      || _update_rollback_hard_stop "could not restore the pre-activation ${UPDATE_BOUNDARY_CONFIG_PATH} from ${backup_ct_path} inside container ${VMID}"
    _update_durability_barrier_ct_or_hard_stop "${UPDATE_BOUNDARY_CONFIG_PATH}" "restoring the pre-activation configuration"
    # Positively PROVE the restore rather than trusting the copy's exit
    # status: the property that matters is that the live configuration no
    # longer activates a lifecycle whose credentials are about to be deleted.
    local restored
    restored="$(pct exec "${VMID}" -- cat "${UPDATE_BOUNDARY_CONFIG_PATH}" 2>/dev/null)" \
      || _update_rollback_hard_stop "could not read back the restored ${UPDATE_BOUNDARY_CONFIG_PATH} inside container ${VMID}"
    if printf '%s\n' "${restored}" | grep -q '^package_update:'; then
      _update_rollback_hard_stop "the restored ${UPDATE_BOUNDARY_CONFIG_PATH} still activates the package-update lifecycle; refusing to remove the key material it names -- restore the pre-activation configuration manually before retrying"
    fi
  fi

  for kind in $(_update_boundary_kinds); do
    live="$(_update_boundary_host_path "${kind}")"
    rollback_copy="${live}.rollback-${UPDATE_RUN_ID}"
    restore_tmp="${live}.restore-tmp-${UPDATE_RUN_ID}"

    # A boundary this run CREATED: remove the helper, the authorization, and
    # the key, so a failed activation leaves no new privileged access path.
    # The authorization goes first: while it exists the key can still reach
    # the helper, so removing it last would leave the shortest-lived window
    # in the most dangerous order.
    if ledger_has update-boundary-created "${kind}"; then
      _update_boundary_deauthorize "${kind}"
      rm -f -- "${live}" \
        || _update_rollback_hard_stop "could not remove the newly created ${kind} boundary helper ${live}"
      pct exec "${VMID}" -- rm -f "$(_update_boundary_key_path "${kind}")" "$(_update_boundary_key_path "${kind}").pub" >/dev/null 2>&1 \
        || log_warn "could not remove the newly created ${kind} boundary key inside container ${VMID}"
      continue
    fi

    # A boundary this run REPLACED: restore the exact preserved content.
    if ledger_has update-boundary-activated "${kind}"; then
      if [[ -e "${rollback_copy}" ]]; then
        [[ -f "${rollback_copy}" && -s "${rollback_copy}" ]] \
          || _update_rollback_hard_stop "the preserved pre-update ${kind} boundary helper (${rollback_copy}) is not a usable non-empty regular file -- restore it manually before retrying"
        rm -f -- "${restore_tmp}" \
          || _update_rollback_hard_stop "could not clear the run-owned ${kind} boundary restore temporary ${restore_tmp}"
        _host_control_install_file 0755 "${rollback_copy}" "${restore_tmp}" \
          || _update_rollback_hard_stop "could not stage the preserved pre-update ${kind} boundary helper for restoration"
        if ! mv "${restore_tmp}" "${live}" 2>/dev/null; then
          rm -f -- "${restore_tmp}" 2>/dev/null || true
          _update_rollback_hard_stop "could not atomically restore the pre-update ${kind} boundary helper onto ${live}"
        fi
        _update_durability_barrier_host_or_hard_stop "${live}" "restoring the pre-update ${kind} boundary helper"
      else
        # Replay of an already-restored artifact.
        [[ -f "${live}" && -x "${live}" ]] \
          || _update_rollback_hard_stop "the preserved pre-update ${kind} boundary helper is absent and ${live} is not an executable regular file -- restore it manually before retrying"
        _update_durability_barrier_host_or_hard_stop "${live}" "replaying the already-restored ${kind} boundary helper"
      fi
    fi
  done

  # Journal directories: remove only ones this run created, and only while
  # they are empty. A non-empty journal holds durable at-most-once evidence
  # about a real host operation and is never destroyed to tidy up.
  local journal_dir journal_path
  for journal_dir in ${UPDATE_BOUNDARY_JOURNAL_DIRS}; do
    journal_path="$(_host_control_host_path "${journal_dir}")"
    if ledger_has update-boundary-journal-created "${journal_path}"; then
      rmdir "${journal_path}" >/dev/null 2>&1 || true
    fi
  done
}

_update_boundary_deauthorize() {
  local kind="$1" marker authorized_keys_path
  marker="$(_update_boundary_marker "${kind}")"
  authorized_keys_path="$(_host_control_host_path "${HOST_CONTROL_AUTHORIZED_KEYS}")"
  # Family 2 correction pass: one shared atomic/durable/idempotent
  # remove-or-reprove-durable primitive -- see bootstrap-host-control.sh's
  # own module header for the full contract this replaces. Every other
  # line -- an unrelated operator key, or a Hubinet entry this run did not
  # create -- is copied through byte-for-byte.
  _host_control_authorized_keys_remove "${authorized_keys_path}" "${marker}" \
    || _update_rollback_hard_stop "could not durably remove the newly created ${kind} forced-command authorization from ${HOST_CONTROL_AUTHORIZED_KEYS}"
}
